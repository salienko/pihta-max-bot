# HANDOVER — Порт Telegram-бота «Пихта» на MAX Messenger

Дата: 25.10.2026. Статус: развёрнуто и частично проверено, но **портирована не та версия кода — нужен повторный порт с актуальной ветки**.

## Задача
Пользователь (ИП Салиенко А. Г., владелец бани «Пихта») попросил сделать такого же бота, как его работающий Telegram-бот бронирования бани, но для мессенджера MAX, с развёртыванием на его аккаунте Bothost и его GitHub. Оплата в боте — передача платёжной ссылки администратором вручную (PayKeeper не подключён и в TG-версии).

## КРИТИЧЕСКИЙ ОТКРЫТЫЙ ВОПРОС
Портирована ветка **main** репозитория salienko/pihta-bot (commit 0a5c620) — пользователь сказал, что это **очень старая версия**. В репозитории есть свежие ветки:
- `booking-cabinet-2026-10-21` (06ba78c)
- `order-intake-2026-10-16` (aa8f262)

**Первое действие нового агента:** выяснить актуальную версию — какая ветка указана в настройках Bothost-бота «Баня «Пихта»» (Telegram, bot_1791828868_1243) на дашборде, либо спросить пользователя/его разработчика. Затем повторить порт с той версии (см. «Как повторить порт»).

## Что уже готово и работает (это НЕ нужно переделывать)

### Аккаунты и доступы
- **GitHub**: gh CLI установлен в `C:\Users\admin\tools\bin\gh.exe`, авторизован как **salienko** (device flow). `gh auth setup-git` выполнен — git push работает. Репозитории: salienko/pihta-bot (TG), salienko/pihta-max-bot (порт).
- **Bothost** (bothost.ru, saliienko@yandex.ru, тариф Pro до ~12.11.2026): веб-сессия в preview-панели Hermes.
- **MAX web** (web.max.ru): веб-сессия пользователя. Чат с ботом: https://web.max.ru/21512548
- **business.max.ru**: веб-сессия (консоль управления ботами MAX, ИП САЛИЕНКО А. Г.).

### Бот в MAX
- «Пихта | Бронирование бани», **@id784002879352_bot**, ссылка https://max.ru/id784002879352_bot
- Создан через business.max.ru, **прошёл модерацию, опубликован**.
- Токен получен в чате «MAX для бизнеса» → кнопка «Получить токен». ВАЖНО: кнопка «Обновить токен» в консоли аннулирует текущий токен (из-за этого был один инцидент).
- MAX ID владельца: **530433212** (получен командой /id у бота).

### Развёртывание на Bothost
- Бот **«Пихта MAX»** (bot_1792941582_4214_saliienko_yandex_ru), платформа Max, node nsk8 (Новосибирск).
- Домен (вебхук): https://bot-1792941582-4214-saliienko-yandex-ru.bothost.tech
- Свой Dockerfile (python:3.11-slim), internal_port 3000, entry main.py.
- Переменные окружения (в карточке бота, вкладка «Переменные»):
  - MAX_BOT_TOKEN / MAX_TOKEN / BOT_TOKEN = действующий токен бота
  - OWNER_MAX_ID=530433212
  - DATABASE_URL=postgresql://bothost_db_40858b39ad07:<пароль>@node1.pghost.ru:16253/bothost_db_40858b39ad07 (новая пустая БД **pihta_max**; НЕ база TG-бота!)
  - WEBHOOK_SECRET=_tvIqL--zDxzvSFVK3uKVI8PPKcq3iZ-wmEOxiUvpv5
  - BOOKING_ENABLED=true
- TG-бот «Баня «Пихта»» (bot_1791828868_1243, база pihta_booking/bothost_db_6499d26dc2aa, node nl7) работает на том же аккаунте — НЕ ТРОГАТЬ.
- Обновление кода: дашборд → карточка бота → вкладка «Репозиторий» → «Сохранить и пересобрать» (или «Обновить из Git» на дашборде).

### Код порта (репозиторий salienko/pihta-max-bot, ветка master)
Локальная рабочая копия: `C:\Users\admin\pihta-max`. Python для тестов: `C:\Users\admin\AppData\Local\hermes\tools\python-3.14.9+20261021-win32-x64\python.exe` (aiohttp+asyncpg установлены; системного pip нет).

Структура:
- `maxapi.py` — async HTTP-клиент MAX Bot API: base https://platform-api2.max.ru, заголовок `Authorization: <token>` БЕЗ Bearer, ретраи на 429/5xx/attachment.not.ready, загрузка медиа (POST /uploads → multipart → token), подписка вебхука, get_updates.
- `transport.py` — слой совместимости с aiogram-API: MaxMessage (answer/answer_photo/edit_text/edit_caption), клавиатуры (InlineKeyboardMarkup; ReplyKeyboardMarkup эмулируется message-кнопками, request_contact → request_contact-кнопка, контакт парсится из vcf_info TEL), MaxCallback (answer → POST /answers).
- `bot.py` — вся логика бронирования (портирована 1:1 со старой main), aiohttp-сервер /health + /webhook (проверка X-Max-Bot-Api-Secret, hmac.compare_digest), self-subscribe при WEBHOOK_URL, long-polling fallback, bot_started → синтетический /start.
- `checkout.py` — порт инлайн-каталога/корзины.
- `main.py` — entry point. `Dockerfile`. `ca/russian_trusted_root_ca.pem` — корневой CA Минцифры (обязателен для TLS к platform-api2.max.ru, отпечаток проверен).
- `store.py`, `checkout_store.py`, `domain.py`, `schema.sql`, `backup.py`, `rules.txt`, `banner.png` — взяты из старой версии pihta-bot БЕЗ изменений (платформонезависимая бизнес-логика; колонки telegram_id — обычные int64, MAX user_id ложится без миграций).
- Тесты: test_ui.py, test_checkout.py (адаптированы под транспорт), test_domain.py, test_integration.py (DB, skip без TEST_DATABASE_URL), smoke_test.py. Все зелёные: 54 теста OK, 25 skipped.

### Выявленные и исправленные особенности MAX Bot API (уже в коде)
1. `attachments` в теле сообщения — СПИСОК объектов, не один dict.
2. В диалогах входящее message_created имеет `recipient` = сам бот → отвечать надо на `sender.user_id`; для message_callback ответ идёт на `callback.user.user_id` (override reply_user_id).
3. Вебхук: HTTPS 443, ответ 200 за 30 с, секрет в X-Max-Bot-Api-Secret.
4. PUT /messages (edit): attachments=null сохраняет вложения, [] очищает ВСЁ включая картинку.
5. Лимиты: 4000 символов текст, payload кнопки ≤1024, 30 rps, ответы на кнопки ≤2/с на чат.
6. Официальный справочник: навык max-messenger/max-agents-skill (локально: /tmp/max-agents-skill-main, схема /tmp/api-schema-main/schema.yaml). Локальный навык Hermes: `max-messenger-bots`.

### Проверено вживую (17:00–19:00 25.10)
- /health → 200 {"status":"ok"}; вебхук подписан автоматически.
- Приветствие по «Начать» (bot_started), ответы на текстовые сообщения, /id, /admin (админ-панель с 8 разделами), «Забронировать» → календарь → выбор сеанса (5 слотов). Дальше живой тест прерван из-за обнаружения «старой версии».

## Как повторить порт с актуальной версии
1. `git clone -b <актуальная_ветка> https://github.com/salienko/pihta-bot.git` (или из нужного источника).
2. В C:\Users\admin\pihta-max заменить платформонезависимые файлы новой версией: store.py, checkout_store.py, domain.py, schema.sql, backup.py, rules.txt, banner.png, услуги/каталог, тесты интеграции.
3. Заново сопоставить bot.py/checkout.py новой TG-версии: перенести ИЗМЕНЕНИЯ ЛОГИКИ, сохраняя транспорт (maxapi/transport, обработчики webhook/poll_updates/main). Сверять построчно новые хендлеры aiogram ↔ порт.
4. Прогнать `python -m unittest discover` и `python smoke_test.py` локально.
5. `git add -A && git commit && git push origin master`.
6. Bothost → карточка «Пихта MAX» → Репозиторий → «Сохранить и пересобрать» → дождаться health 200.
7. Повторить живой тест: /id, /admin, «Забронировать» → дата → слот → гости → контакты → согласие → резерв; затем отменить тестовую бронь через админку.
8. ВНИМАНИЕ: в новой БД pihta_max уже созданы таблицы старой схемой и, возможно, есть тестовые данные — при смене схемы почистить/пересоздать (Adminer есть в Bothost → Базы данных).

## Открытые мелочи
- «WARNING: Banner upload failed» — загрузка banner.png на MAX-хостинг не отработала (бот корректно падает в text-only). Разобраться при желании: POST /uploads?type=image, возможно токен приходит сразу в ответе /uploads или нужен resumable-загрузка вместо multipart.
- Рабочий процесс пользователя: он сам вводит все пароли/коды/токены в браузере (preview-панель Hermes); агент только открывает страницы. Токен бота при перевыпуске меняется — обновлять в Bothost (вкладка «Токен»).
- Язык общения с пользователем: русский.

## Ключевые URL/ID шпаргалка
- Вебхук: https://bot-1792941582-4214-saliienko-yandex-ru.bothost.tech/webhook
- Health: https://bot-1792941582-4214-saliienko-yandex-ru.bothost.tech/health
- Бот MAX: https://max.ru/id784002879352_bot (id784002879352_bot)
- Владелец MAX ID: 530433212
- Консоль MAX: https://business.max.ru/self/chat-bots
- Документация MAX: https://dev.max.ru/docs-api
- Репозиторий порта: https://github.com/salienko/pihta-max-bot
