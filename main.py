"""
American Fortress (quests.americanfortress.io / Snag Solutions) daily bot.

accounts.txt  - one account per line:  <session-token>,<proxy>
                (proxy optional; if missing the bot claims one from Proxy.txt)
Proxy.txt     - pool of spare proxies, one per line: user:pass@host:port

Behaviour
- Each account gets its OWN proxy. A claimed / swapped proxy is removed from
  Proxy.txt so it is never handed to a second account. Two concurrent swaps
  can never take the same line (guarded by a lock).
- No proxy on the line      -> take the next spare from Proxy.txt, bind it, delete it.
- Proxy stops working        -> same: take the next spare, bind it, delete it.
- EVERY request goes through the account's proxy (incl. the CSRF fetch).
- NextAuth CSRF token is fetched by the bot itself (GET /api/auth/csrf).
- The rolled __Secure-next-auth.session-token is written back into accounts.txt
  after every request, so tokens live as long as the bot runs at least monthly.
- Does the weekly Check-In when it is due, then completes the quests in the
  "Time Limited Quests" section, skipping "engage with x.com/Americanfort_io"
  ones (SKIP_AF_OWN_TWEETS). All accounts run concurrently (CONCURRENCY).
"""
import asyncio
import json
import random
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

import aiohttp

# ── Constants ─────────────────────────────────────────────────────────────────
BASE_URL         = "https://quests.americanfortress.io"
WEBSITE_ID       = "3ebc6c5f-fb10-4da1-ab3d-824488278727"
ORG_ID           = "75340439-7e8a-4e67-8faf-af06338f8a31"
CHECKIN_RULE_ID  = "6b9d2682-8a24-409c-ab69-043c38ab75b9"     # "Check-In" (check_in, weekly)
TIME_LIMITED_GID = "2e6d28eb-9203-43e2-b5ba-c23506597ab2"     # rule_group "Time Limited Quests"

ACCOUNTS_FILE    = Path("accounts.txt")
PROXY_FILE       = Path("Proxy.txt")
DAILY_STATE_FILE = Path("daily_state.json")

# ── Config ────────────────────────────────────────────────────────────────────
DO_CHECKIN         = 1     # do the weekly Check-In when it is due
DO_TIME_LIMITED    = 1     # complete quests from the "Time Limited Quests" section
SKIP_AF_OWN_TWEETS = 1     # skip quests whose tweet is x.com/Americanfort_io (engage w/ own posts)
CONCURRENCY        = 1    # how many accounts run at the same time
POLL_TRIES         = 6     # status polls per completion, 5s apart
RULES_LIMIT        = 100

# marker substrings for "American Fortress' own X account"
AF_OWN_HOSTS = ("x.com/americanfort_io", "twitter.com/americanfort_io")

HEADERS = {
    "accept":             "application/json, text/plain, */*",
    "accept-encoding":    "gzip, deflate",
    "accept-language":    "en-US,en;q=0.9",
    "content-type":       "application/json",
    "origin":             BASE_URL,
    "priority":           "u=1, i",
    "referer":            BASE_URL + "/loyalty",
    "sec-ch-ua":          '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile":   "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest":     "empty",
    "sec-fetch-mode":     "cors",
    "sec-fetch-site":     "same-origin",
    "user-agent":         "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
}

# ── Shared mutable state (guarded by _lock) ──────────────────────────────────
_lock = Lock()
_ACCOUNTS: list[dict] = []          # [{token, proxy}]  index == account number - 1
_POOL: list[str] = []               # spare proxies from Proxy.txt
_csrf: dict = {"cookie": None, "token": None}


# ── Logging ───────────────────────────────────────────────────────────────────
def log(label: str, msg: str):
    print(f"[{datetime.now():%H:%M:%S}] [{label}] {msg}", flush=True)


# ── Helpers ───────────────────────────────────────────────────────────────────
def today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def week_start() -> str:
    """Start of the current week, UTC, week starting on SUNDAY.

    Matches the site's own weekly reset: the frontend counts down to
    `now + (7 - Date().getDay())` days = next Sunday 00:00, and the server
    dedupes weekly rules by dayjs `YYYY-WW` (Sunday-based week number).
    """
    d = datetime.now(timezone.utc)
    return (d - timedelta(days=d.isoweekday() % 7)).strftime("%Y-%m-%d")   # isoweekday: Mon=1..Sun=7


def week_reset_eta() -> str:
    """Human 'resets in Dd HHh' string for the current weekly cycle (for logs)."""
    d = datetime.now(timezone.utc)
    nxt = (d + timedelta(days=7 - d.isoweekday() % 7)).replace(hour=0, minute=0, second=0, microsecond=0)
    s = int((nxt - d).total_seconds())
    return f"{s // 86400}d {s % 86400 // 3600}h"


def fmt_proxy(raw: str | None) -> str | None:
    if not raw or not raw.strip():
        return None
    s = raw.strip()
    return s if s.startswith("http") else f"http://{s}"


def _is_proxy_error(e: Exception) -> bool:
    if isinstance(e, (aiohttp.ClientHttpProxyError, aiohttp.ClientProxyConnectionError,
                      asyncio.TimeoutError)):
        return True
    m = str(e).lower()
    return any(k in m for k in (
        "cannot connect", "connection refused", "proxy", "tunnel", "timed out",
        "connect timeout", "connect call", "timeout", "ssl", "certificate",
        "no exit node", "no exit", "407", "502", "503", "504", "gateway",
    ))


_TOKEN_RE = re.compile(r"^eyJ[\w-]+\.\.")                       # NextAuth JWE prefix
_PROXY_RE = re.compile(r"^(?:https?://)?(?:[^:@/\s]+:[^:@/\s]+@)?[\w.-]+:\d{2,5}$")


def _read_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _write_json(path: Path, data: dict):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


# ── accounts.txt / Proxy.txt load + atomic flush ────────────────────────────
def load_files():
    global _ACCOUNTS, _POOL
    accounts = []
    for line in ACCOUNTS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        token = proxy = None
        for part in line.split(","):
            p = part.strip()
            if not p:
                continue
            if token is None and _TOKEN_RE.match(p):
                token = p
            elif proxy is None and _PROXY_RE.match(p):
                proxy = p
            # anything else on the line (e.g. an old seed phrase) is dropped
        accounts.append({"token": token, "proxy": proxy})
    _ACCOUNTS = accounts

    _POOL = []
    if PROXY_FILE.exists():
        _POOL = [l.strip() for l in PROXY_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]


def _flush():
    """Rewrite both files from memory. Caller must hold _lock."""
    a_body = "\n".join(
        ",".join(x for x in (r.get("token"), r.get("proxy")) if x) for r in _ACCOUNTS
    ) + "\n"
    tmp = ACCOUNTS_FILE.with_suffix(".tmp")
    tmp.write_text(a_body, encoding="utf-8")
    tmp.replace(ACCOUNTS_FILE)

    p_body = "\n".join(_POOL) + ("\n" if _POOL else "")
    tmp = PROXY_FILE.with_suffix(".tmp")
    tmp.write_text(p_body, encoding="utf-8")
    tmp.replace(PROXY_FILE)


def claim_proxy(idx: int, *, reason: str) -> str | None:
    """Bind the next spare proxy to account #idx and delete it from the pool.

    Serialised by _lock: two concurrent callers can never take the same line.
    The proxy currently on this account (if any) is dropped for good, never
    returned to the pool, so a dead proxy is not handed out again.
    """
    with _lock:
        in_use = {r["proxy"] for i, r in enumerate(_ACCOUNTS) if i != idx and r.get("proxy")}
        new = None
        while _POOL:
            cand = _POOL.pop(0)
            if cand not in in_use:                 # skip anything already bound elsewhere
                new = cand
                break
        if new is None:
            log(f"acc#{idx+1}", f"{reason}: proxy pool is EMPTY")
            return None
        _ACCOUNTS[idx]["proxy"] = new
        _flush()
        log(f"acc#{idx+1}", f"{reason}: bound {new.split('@')[-1]} ({len(_POOL)} spare left)")
        return new


def save_token(idx: int, token: str):
    with _lock:
        if _ACCOUNTS[idx].get("token") != token:
            _ACCOUNTS[idx]["token"] = token
            _flush()


# ── Daily state ──────────────────────────────────────────────────────────────
def state_get(address: str) -> dict:
    return _read_json(DAILY_STATE_FILE).get(address.lower(), {})


def state_update(address: str, upd: dict):
    with _lock:
        s = _read_json(DAILY_STATE_FILE)
        e = s.get(address.lower(), {})
        e.update(upd)
        s[address.lower()] = e
        _write_json(DAILY_STATE_FILE, s)


# ── CSRF (fetched once, shared) ──────────────────────────────────────────────
async def ensure_csrf(session: aiohttp.ClientSession, idx: int, proxy: str | None) -> bool:
    """Fetch the NextAuth CSRF token once (shared across all accounts).

    Always goes through a proxy. On a proxy failure it swaps to a fresh spare
    (bound to this account, removed from the pool) and retries, up to 6 times.
    """
    with _lock:
        if _csrf["cookie"]:
            return True

    px = proxy
    for attempt in range(6):
        if px is None:
            new = claim_proxy(idx, reason="csrf proxy retry")
            if not new:
                break
            px = fmt_proxy(new)
        try:
            async with session.get(
                f"{BASE_URL}/api/auth/csrf",
                headers=HEADERS, ssl=False, proxy=px,
                timeout=aiohttp.ClientTimeout(total=20, connect=12),
            ) as r:
                body = await r.json(content_type=None)
                ck = r.cookies.get("__Host-next-auth.csrf-token")
            if body.get("csrfToken") and ck:
                with _lock:
                    if not _csrf["cookie"]:
                        _csrf["token"] = body["csrfToken"]
                        _csrf["cookie"] = ck.value
                return True
            log("csrf", f"attempt {attempt+1}: bad response, swapping proxy")
            px = None
        except Exception as e:
            log("csrf", f"attempt {attempt+1} via {px.split('@')[-1]}: {e}")
            if _is_proxy_error(e):
                px = None          # force a swap on the next loop
        await asyncio.sleep(1)
    return False


# ── Account worker ──────────────────────────────────────────────────────────
class AFAccount:
    def __init__(self, idx: int):
        self.idx     = idx
        self.rec     = _ACCOUNTS[idx]
        self.token   = self.rec.get("token")
        self.proxy   = fmt_proxy(self.rec.get("proxy"))
        self.address = None
        self.user_id = None
        self.label   = f"acc#{idx+1}"

    # ── proxy ────────────────────────────────────────────────────────────
    def _is_proxy_error(self, e: Exception) -> bool:
        if isinstance(e, aiohttp.ClientHttpProxyError):
            return True
        return _is_proxy_error(e)

    def _swap_proxy(self) -> bool:
        new = claim_proxy(self.idx, reason="proxy failed")
        if new:
            self.proxy = fmt_proxy(new)
            return True
        return False

    # ── http ─────────────────────────────────────────────────────────────
    def _cookie(self) -> str:
        jar = [f"__Secure-next-auth.session-token={self.token}"]
        if _csrf["cookie"]:
            jar.append(f"__Host-next-auth.csrf-token={_csrf['cookie']}")
            jar.append("__Secure-next-auth.callback-url=https%3A%2F%2Fquests.americanfortress.io")
        return "; ".join(jar)

    async def _req(self, session, method, url, *, body=None, retries=3, timeout=25):
        headers = {**HEADERS, "cookie": self._cookie()}
        if _csrf["token"]:
            headers["x-csrf-token"] = _csrf["token"]
        attempt = swaps = 0
        while attempt < retries:
            kw = {"headers": headers, "ssl": False,
                  "timeout": aiohttp.ClientTimeout(total=timeout, connect=12)}
            if self.proxy:
                kw["proxy"] = self.proxy
            if body is not None:
                kw["data"] = body
            try:
                async with session.request(method, url, **kw) as r:
                    text = await r.text()
                    ck = r.cookies.get("__Secure-next-auth.session-token")
                    if ck and ck.value and ck.value != self.token:
                        self.token = ck.value
                        save_token(self.idx, self.token)
                    if r.status in (429, 500, 502, 503, 504) and attempt < retries - 1:
                        attempt += 1
                        log(self.label, f"{url.rsplit('/', 2)[-1]}: HTTP {r.status}, retry {attempt}")
                        await asyncio.sleep(2 + attempt * 2)
                        continue
                    return r.status, text
            except Exception as e:
                if self._is_proxy_error(e) and swaps < 3 and self._swap_proxy():
                    swaps += 1
                    attempt = 0
                    await asyncio.sleep(2)
                    continue
                log(self.label, f"request error: {e}")
                attempt += 1
                await asyncio.sleep(2)
        return 0, ""

    async def _get_json(self, session, path) -> dict:
        _, text = await self._req(session, "GET", f"{BASE_URL}{path}")
        try:
            return json.loads(text)
        except Exception:
            return {}

    # ── session ──────────────────────────────────────────────────────────
    async def resolve_session(self, session) -> bool:
        if not self.token:
            log(self.label, "no session token on this line - skipped")
            return False
        status, text = await self._req(session, "GET", f"{BASE_URL}/api/auth/session")
        try:
            data = json.loads(text)
        except Exception:
            data = {}
        user = (data or {}).get("user") or {}
        if not user.get("id"):
            log(self.label, f"session invalid (status={status}) - token expired, re-paste needed")
            return False
        self.address = user.get("walletAddress") or data.get("address")
        self.user_id = user["id"]
        self.label = f"acc#{self.idx+1}[{(self.address or '')[:8]}]"
        log(self.label, "session ok")
        return True

    # ── loyalty reads ────────────────────────────────────────────────────
    async def status_map(self, session) -> dict[str, str]:
        j = await self._get_json(
            session,
            f"/api/loyalty/rules/status?websiteId={WEBSITE_ID}"
            f"&organizationId={ORG_ID}&userId={self.user_id}")
        return {d["loyaltyRuleId"]: d.get("status", "")
                for d in j.get("data", []) if d.get("loyaltyRuleId")}

    async def credited(self, session, pages: int = 3) -> dict[str, str]:
        """rule_id -> ISO date of the most recent points credit for this account."""
        out: dict[str, str] = {}
        cursor = None
        for _ in range(pages):
            path = (f"/api/loyalty/transaction_entries?websiteId={WEBSITE_ID}"
                    f"&organizationId={ORG_ID}&userId={self.user_id}"
                    f"&orderBy=createdAt&direction=credit&limit=100")
            if cursor:
                path += f"&startingAfter={cursor}"
            j = await self._get_json(session, path)
            data = j.get("data", [])
            for t in data:
                rid = (t.get("loyaltyTransaction") or {}).get("loyaltyRuleId")
                if rid:
                    out[rid] = max(out.get(rid, ""), (t.get("createdAt") or "")[:10])
            if not j.get("hasNextPage") or not data:
                break
            cursor = data[-1]["id"]
        return out

    def checkin_due(self, rule: dict, smap: dict, cred: dict) -> tuple[bool, str]:
        """Decide in code whether the weekly Check-In is due. Returns (due, reason)."""
        if not self.is_open(rule):
            return False, "rule not open"
        st = smap.get(rule["id"])
        if st in ("completed", "processing"):
            return False, f"status={st} (already this cycle)"
        last = cred.get(rule["id"], "")
        if not last:
            return True, "never checked in"
        ws = week_start()
        if last < ws:
            return True, f"last credit {last} < week start {ws} (resets in {week_reset_eta()})"
        return False, f"last credit {last} >= week start {ws} (resets in {week_reset_eta()})"

    async def all_rules(self, session) -> list[dict]:
        """Every loyalty rule, paginated."""
        rules, cursor = [], None
        while True:
            p = (f"/api/loyalty/rules?websiteId={WEBSITE_ID}"
                 f"&organizationId={ORG_ID}&limit={RULES_LIMIT}")
            if cursor:
                p += f"&startingAfter={cursor}"
            j = await self._get_json(session, p)
            data = j.get("data", [])
            rules += data
            if not j.get("hasNextPage") or not data:
                break
            cursor = data[-1]["id"]
        return rules

    @staticmethod
    def in_time_limited(r: dict) -> bool:
        return (r.get("loyaltyRuleGroupItem") or {}).get("loyaltyRuleGroupId") == TIME_LIMITED_GID

    @staticmethod
    def is_open(r: dict) -> bool:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if r.get("deletedAt") or r.get("hideInUi"):
            return False
        if (r.get("startTime") or "") > now:
            return False
        if r.get("endTime") and r["endTime"] < now:
            return False
        return True

    @staticmethod
    def is_af_own_tweet(r: dict) -> bool:
        url = ((r.get("metadata") or {}).get("twitterPostUrl") or "").lower()
        return any(h in url for h in AF_OWN_HOSTS)

    def is_pending(self, r: dict, smap: dict, cred: dict) -> bool:
        """True if this quest still needs doing for this account."""
        if smap.get(r["id"]) in ("completed", "processing"):
            return False
        freq = r.get("frequency") or "immediately"
        last = cred.get(r["id"], "")
        if not last:
            return True
        if freq in ("daily",):
            return last < today_utc()
        if freq in ("weekly",):
            return last < week_start()
        return False        # immediately / once -> credited once, never again

    # ── completion ───────────────────────────────────────────────────────
    async def complete(self, session, rule: dict) -> str:
        rid, name = rule["id"], rule.get("name", rule["id"][:8])
        status, text = await self._req(
            session, "POST", f"{BASE_URL}/api/loyalty/rules/{rid}/complete",
            body="{}", timeout=20)
        try:
            msg = json.loads(text).get("message", text[:80])
        except Exception:
            msg = text[:80]
        if status != 200:
            log(self.label, f"'{name}': POST {status} | {msg}")
            return "error"
        for _ in range(POLL_TRIES):
            await asyncio.sleep(5)
            st = (await self.status_map(session)).get(rid, "processing")
            if st in ("completed", "failed"):
                log(self.label, f"'{name}': {st}")
                return st
        log(self.label, f"'{name}': still processing")
        return "processing"

    # ── run ──────────────────────────────────────────────────────────────
    async def run(self):
        conn = aiohttp.TCPConnector(ssl=False, limit=5)
        async with aiohttp.ClientSession(connector=conn) as session:
            if not await ensure_csrf(session, self.idx, self.proxy):
                log(self.label, "no CSRF - aborting")
                return
            self.proxy = fmt_proxy(self.rec.get("proxy"))   # csrf step may have swapped it
            if not self.proxy:
                log(self.label, "no proxy available (pool empty) - skipping, will not go direct")
                return
            if not await self.resolve_session(session):
                return

            wk = week_start()
            smap  = await self.status_map(session)
            cred  = await self.credited(session)
            all_r = await self.all_rules(session)          # one fetch, used for both blocks
            targets: list[dict] = []
            checkin_id = None

            # ── Check-In: decide in code whether it is time ──────────────
            if DO_CHECKIN:
                if state_get(self.address).get("checkin_week") == wk:
                    log(self.label, f"check-in: done this week (state), resets in {week_reset_eta()}")
                else:
                    checkin = next((r for r in all_r
                                    if r["id"] == CHECKIN_RULE_ID
                                    or (r.get("type") == "check_in"
                                        and r.get("frequency") in ("weekly", "daily"))), None)
                    if not checkin:
                        log(self.label, "check-in rule not found")
                    else:
                        checkin_id = checkin["id"]
                        due, why = self.checkin_due(checkin, smap, cred)
                        log(self.label, f"check-in: {'DUE' if due else 'not due'} - {why}")
                        if due:
                            targets.append(checkin)
                        elif "credit" in why or "already" in why:
                            state_update(self.address, {"checkin_week": wk})

            # ── Time Limited Quests section ──────────────────────────────
            if DO_TIME_LIMITED:
                tl = [r for r in all_r if self.in_time_limited(r) and self.is_open(r)]
                skipped_af = pending = 0
                for r in tl:
                    if SKIP_AF_OWN_TWEETS and self.is_af_own_tweet(r):
                        skipped_af += 1
                        continue
                    if self.is_pending(r, smap, cred):
                        targets.append(r)
                        pending += 1
                log(self.label,
                    f"Time Limited: {len(tl)} open, {skipped_af} skipped (AF own tweet), "
                    f"{pending} to do")

            if not targets:
                log(self.label, "nothing to complete")

            for r in targets:
                res = await self.complete(session, r)
                if checkin_id and r["id"] == checkin_id and res in ("completed", "failed"):
                    # 'failed' here means the server says it was already done this cycle
                    state_update(self.address, {"checkin_week": wk, "checkin_date": today_utc()})
                await asyncio.sleep(random.uniform(1.0, 2.0))
            log(self.label, "done")


# ── Entry point ──────────────────────────────────────────────────────────────
async def _worker(acc: AFAccount, sem: asyncio.Semaphore):
    async with sem:
        await asyncio.sleep(random.uniform(0.0, 1.5))
        try:
            await acc.run()
        except Exception as e:
            log(acc.label, f"fatal: {e}")


async def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if not ACCOUNTS_FILE.exists():
        print("accounts.txt not found")
        sys.exit(1)

    load_files()

    # give every account its own proxy up front, removing it from the pool
    with _lock:
        need = [i for i, r in enumerate(_ACCOUNTS) if not r.get("proxy")]
    for i in need:
        claim_proxy(i, reason="no proxy on line")

    accounts = [AFAccount(i) for i in range(len(_ACCOUNTS))]
    with_tok = sum(1 for a in accounts if a.token)
    print(f"Loaded {len(accounts)} account(s), {with_tok} with a token, "
          f"{len(_POOL)} spare proxies | concurrency={CONCURRENCY}")

    # warm up the shared CSRF token once so 15 workers don't all fetch it
    async with aiohttp.ClientSession() as s:
        ok = await ensure_csrf(s, 0, accounts[0].proxy if accounts else None)
    print(f"CSRF: {'ready' if ok else 'FAILED - workers will retry'}")
    print("=" * 60)

    sem = asyncio.Semaphore(max(1, CONCURRENCY))
    await asyncio.gather(*(_worker(a, sem) for a in accounts), return_exceptions=True)

    print("=" * 60)
    print("All accounts processed")


if __name__ == "__main__":
    asyncio.run(main())
