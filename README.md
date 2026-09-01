# Americanfortress

Бот для ежедневной автоматизации квестов на **[quests.americanfortress.io/loyalty](https://quests.americanfortress.io/loyalty)**
(лоялти-платформа [Snag Solutions](https://snagsolutions.io)).

Работает по **session-токену NextAuth** — без сид-фраз и приватных ключей. Каждому
аккаунту привязывается свой прокси; битый прокси автоматически меняется на живой.

---

## Что делает

1. **CSRF** — сам получает NextAuth CSRF-токен (`GET /api/auth/csrf`) один раз на запуск.
2. **Сессия** — по каждому токену читает `GET /api/auth/session` → `userId`, адрес кошелька.
   Невалидный/протухший токен → аккаунт пропускается (нужен ручной ре-пейст).
3. **Check-In** — определяет **в коде**, пришло ли время недельного чек-ина, и если да —
   выполняет (`POST /api/loyalty/rules/{id}/complete`).
4. **Time Limited Quests** — парсит секцию `Time Limited Quests`, берёт только открытые
   сейчас квесты, **пропускает** «engage» с твитами `x.com/Americanfort_io`
   (`SKIP_AF_OWN_TWEETS`), остальные выполняет по списку.
5. **Учёт выполненного** — отсекает уже сделанное по `GET /api/loyalty/rules/status`
   и `GET /api/loyalty/transaction_entries` (с учётом частоты: `daily` / `weekly` / `once`).
6. **Персист токена** — после каждого запроса берёт обновлённый
   `__Secure-next-auth.session-token` из `Set-Cookie` и пишет обратно в `accounts.txt`.
   NextAuth «катит» сессию, поэтому токен живёт, пока бот запускается хотя бы раз в ~3 недели.
7. **Параллельность** — все аккаунты одновременно, но не больше `CONCURRENCY` за раз.

---

## Логика «пора ли чек-ин»

Недельный чек-ин на сайте сбрасывается в **воскресенье 00:00 UTC** (неделя с воскресенья —
это видно и по таймеру `RESETS IN` на сайте: `now + (7 − Date().getDay())`, и по серверному
ключу идемпотентности `YYYY-WW`).

`checkin_due()` возвращает `(due, reason)`:

| Условие | Итог |
|---|---|
| правило закрыто (`deletedAt` / `hideInUi` / вне окна `startTime`–`endTime`) | не пора |
| `rules/status` = `completed` или `processing` | не пора (уже в этом цикле) |
| начислений по правилу нет вообще | **ПОРА** |
| дата последнего начисления `<` начала недели (вс, UTC) | **ПОРА** |
| дата последнего начисления `>=` начала недели | не пора |

Плюс быстрый выход по `daily_state.json` (если неделя уже отмечена — блок чек-ина
не трогаем). Финальный арбитр — сервер: ранний `POST` вернёт `failed` без вреда,
двойного начисления нет.

---

## Прокси-модель

- **1 прокси = 1 аккаунт.**
- Нет прокси в строке аккаунта → берётся первая строка `Proxy.txt`, привязывается, **удаляется** из файла.
- Прокси не отвечает → то же самое: следующий спейр, привязка, удаление из пула.
- Использованный прокси **не возвращается** в пул → одному прокси не достанется два аккаунта.
- Все операции с пулом под локом → в параллельных потоках нельзя взять один и тот же прокси.
- **Каждый** запрос (включая CSRF) идёт через прокси аккаунта. Если пул пуст — аккаунт
  пропускается, напрямую бот не ходит.

---

## Установка

```bash
git clone git@github.com:<you>/Americanfortress.git
cd Americanfortress

python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # Linux/macOS

pip install -r requirements.txt
```

## Настройка

```bash
cp accounts.txt.example accounts.txt
cp Proxy.txt.example     Proxy.txt
```

- **`accounts.txt`** — по строке на аккаунт: `<session-token>` или `<session-token>,<proxy>`.
  Токен: DevTools → Application → Cookies → `quests.americanfortress.io` →
  значение `__Secure-next-auth.session-token` (начинается с `eyJ...`).
- **`Proxy.txt`** — пул запасных прокси, по одному на строку: `user:pass@host:port`.
  Держи с запасом (несколько на аккаунт).

Формат — см. `*.example`.

## Конфиг (вверху `main.py`)

```python
DO_CHECKIN         = 1     # недельный Check-In, когда пришло время
DO_TIME_LIMITED    = 1     # квесты из секции "Time Limited Quests"
SKIP_AF_OWN_TWEETS = 1     # пропускать engage с твитами x.com/Americanfort_io
CONCURRENCY        = 15    # сколько аккаунтов одновременно
POLL_TRIES         = 6     # опросов статуса после completion (по 5 сек)
```

## Запуск

```bash
python main.py
```

Повторный запуск в тот же день безопасен: выполненное отсекается по статусу/начислениям
и по `daily_state.json`.

---

## Файлы

| Файл | Назначение | В гите |
|------|------------|:---:|
| `main.py` | весь бот | ✅ |
| `requirements.txt` | зависимости (`aiohttp`) | ✅ |
| `accounts.txt.example` / `Proxy.txt.example` | шаблоны | ✅ |
| `accounts.txt` | токены (+ прокси) аккаунтов | ❌ `.gitignore` |
| `Proxy.txt` | пул прокси | ❌ `.gitignore` |
| `daily_state.json` | отметки недели чек-ина по кошелькам — создаётся сам | ❌ `.gitignore` |

Секреты (`accounts.txt`, `Proxy.txt`, `daily_state.json`, `tokens_cache.json`) в
`.gitignore` и в репозиторий не попадают.

---

## Используемые эндпоинты

```
GET  /api/auth/csrf                                  CSRF-токен (без авторизации)
GET  /api/auth/session                               userId + walletAddress по токену
GET  /api/loyalty/rules?websiteId&organizationId     список всех правил (пагинация)
GET  /api/loyalty/rules/status?...&userId            статусы недавних попыток (completed/processing/failed)
GET  /api/loyalty/transaction_entries?...&userId&direction=credit   факт начисления очков
POST /api/loyalty/rules/{id}/complete                поставить квест в очередь на проверку
```

Требуют куки `__Secure-next-auth.session-token` + `__Host-next-auth.csrf-token`
(последнюю бот получает сам). `websiteId` / `organizationId` / id правила Check-In
зашиты в `main.py` как константы (публичные идентификаторы сайта).

---

## Дисклеймер

Личный инструмент для автоматизации собственных аккаунтов. Используй на свой риск.
