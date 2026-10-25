"""Pihta booking interface for MAX Messenger. PayKeeper is deferred."""
import asyncio
import hashlib
import hmac
import logging
import os
from datetime import datetime, timedelta, date
from pathlib import Path

from aiohttp import web

from maxapi import MaxApi
from transport import (InlineKeyboardButton, InlineKeyboardMarkup,
                       KeyboardButton, MaxCallback, MaxMessage,
                       ReplyKeyboardMarkup)
from domain import TZ, SLOTS, RuleError, now, horizon, rub, money, window, normalize_phone, sorted_services
from store import Store, uid

SITE = 'https://banyapihta.ru/'
PHONE = '+7-981-244-00-50'
RULES = Path(__file__).with_name('rules.txt').read_text(encoding='utf-8')
RULES_HASH = hashlib.sha256(RULES.encode()).hexdigest()
OWNER = int(os.getenv('OWNER_MAX_ID', os.getenv('OWNER_TELEGRAM_ID', '0')))
BOOKING_ENABLED = os.getenv('BOOKING_ENABLED', 'false').lower() == 'true'
WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET', '').strip()
USE_BRAND = os.getenv('USE_BRAND', 'true').lower() == 'true'
db = None
api = None
BANNER = {'token': None}
STATUS = {'held': 'Резерв, ожидается оплата', 'payment_review': 'Оплата на проверке',
          'review_overdue': 'Проверка задерживается, время сохранено', 'confirmed': 'Подтверждена',
          'in_progress': 'Сеанс идёт', 'completed': 'Сеанс завершён', 'cancelled': 'Отменена',
          'expired': 'Резерв истёк', 'rejected': 'Отклонена'}
MENU = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=x) for x in row] for row in
                                     [('Забронировать', 'Корзина'), ('Мои бронирования', 'Услуги'),
                                      ('Цены',), ('Правила', 'О бане'), ('Связаться с нами',)]])


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


def report_error(scope):
    import sys
    import traceback
    exc = sys.exc_info()[1]
    frames = traceback.extract_tb(sys.exc_info()[2])
    location = ' > '.join(f'{Path(f.filename).name}:{f.lineno}:{f.name}' for f in frames[-5:])
    logging.error('%s failed: %s at %s; values suppressed', scope, type(exc).__name__, location)


def shop():
    import sys
    from checkout import CheckoutUI
    return CheckoutUI(sys.modules[__name__])


async def orders(message, actor):
    rows = await db.pool.fetch('SELECT id,start_at,status FROM booking WHERE guest_id=$1 ORDER BY start_at DESC LIMIT 20', actor)
    await message.answer('Ваши бронирования' if rows else 'У вас пока нет бронирований.',
                         reply_markup=kb([[(r['start_at'].astimezone(TZ).strftime('%d.%m %H:%M') + ' · ' + STATUS[r['status']], f'order:{r["id"]}')] for r in rows] + [[('Главное меню', 'home')]]))


async def chunks(message, text, markup=None):
    parts = [text[i:i + 3500] for i in range(0, len(text), 3500)]
    for i, part in enumerate(parts):
        await message.answer(part, reply_markup=markup if i == len(parts) - 1 else None)


async def ready(message):
    if not db:
        await message.answer('Бронирование сейчас настраивается. Администратор: ' + PHONE)
        return False
    return True


async def ask_phone(message, actor, data, use_saved=True):
    saved = await db.saved_phone(actor) if use_saved else None
    if saved:
        data['phone'] = saved
        await db.set_session(actor, 'phone_confirm', data)
        await confirm_phone(message, data)
    else:
        await db.set_session(actor, 'phone', data)
        await message.answer('Поделитесь своим номером через кнопку или введите российский номер для связи.',
                             reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text='Поделиться номером', request_contact=True)], [KeyboardButton(text='Отмена')]]))


async def confirm_phone(message, data):
    token = data['operation']
    await message.answer(f'Использовать этот номер для связи: {data["phone"]}?',
                         reply_markup=kb([[('Да', f'phoneyes:{token}'), ('Использовать другой номер', f'phoneother:{token}')]]))


async def show_cart(message, actor, edit=False):
    state, d = await db.session(actor)
    if state != 'cart':
        raise RuleError('Откройте услуги заново.')
    items = sorted_services(await db.pool.fetch('SELECT * FROM service_catalog WHERE active'))
    rows = []
    total = 0
    for item in items:
        code = item['code']
        quantity = d['cart'].get(code, 0)
        total += quantity * item['price']
        base = f'{d["booking"]}:{code}:{d["token"]}'
        rows.append([(f'{item["name"]} · {rub(item["price"])}' + (f' · {quantity} шт.' if quantity else ''), f'cplus:{base}')])
        if quantity:
            rows.append([('−', f'cminus:{base}'), (f'{quantity} шт.', f'cartinfo:{d["token"]}'), ('+', f'cplus:{base}')])
    rows.append([(f'Отправить выбранное · {rub(total)}', f'csend:{d["booking"]}:{d["token"]}')])
    rows.append([('Отмена', f'cdiscard:{d["booking"]}:{d["token"]}')])
    text = 'Выберите несколько услуг. Нажатие на название добавляет одну единицу; «−» и «+» меняют количество. Затем нажмите «Отправить выбранное».\nПредварительная сумма ' + rub(total) + '. Выполнение подтвердит администратор.'
    if edit:
        await message.edit_text(text, reply_markup=kb(rows))
    else:
        await message.answer(text, reply_markup=kb(rows))


async def dates(message, offset=0):
    today = now().date()
    last = horizon(today)
    rows = []
    for i in range(offset, min(offset + 7, (last - today).days + 1)):
        day = today + timedelta(days=i)
        rows.append([(day.strftime('%d.%m.%Y'), f'date:{day.isoformat()}')])
    nav = []
    if offset >= 7:
        nav.append(('←', f'dates:{offset - 7}'))
    if today + timedelta(days=offset + 7) <= last:
        nav.append(('Далее →', f'dates:{offset + 7}'))
    if nav:
        rows.append(nav)
    rows.append([('Нужны 3–5 часов', 'long')])
    await message.answer('Выберите дату. Запись минимум за час, на два месяца вперёд.', reply_markup=kb(rows))


async def show_order(message, ident, actor):
    b = await db.get(ident, actor)
    total, paid, left = await db.totals(ident)
    start = b['start_at'].astimezone(TZ)
    end = b['end_at'].astimezone(TZ)
    rows = await db.pool.fetch('SELECT * FROM booking_service WHERE booking_id=$1 ORDER BY id', ident)
    lines = [f'{r["name"]} × {r["quantity"]}: {rub(r["price"] * r["quantity"])} — ' + {'requested': 'на подтверждении', 'approved': 'подтверждено', 'rejected': 'отклонено'}[r['status']] for r in rows]
    text = f'Бронь {ident}\n{start:%d.%m.%Y %H:%M}–{end:%H:%M}\n{STATUS[b["status"]]}\nГостей {b["guests"]}, детей до 6 включительно {b["children"]}\nАренда и гости: {rub(b["rent"])}\nИтого подтверждено: {rub(total)}\nОплачено: {rub(paid)}\nОстаток: {rub(left)}'
    if paid > total:
        text += f'\nПереплата: {rub(paid - total)} — свяжитесь с администратором.'
    if lines:
        text += '\nУслуги:\n' + '\n'.join(lines)
    buttons = []
    if b['status'] == 'held':
        text += f'\nРезерв до {b["release_at"].astimezone(TZ):%H:%M}. Заявите оплату за 15 минут; без заявления резерв освобождается через 20 минут.'
    if not b['duration_approved']:
        text += '\nДлительность ожидает подтверждения администратора.\nПлатёжную ссылку передадут после решения.'
    live = b['status'] in ('held', 'payment_review', 'review_overdue', 'confirmed', 'in_progress')
    if actor == b['guest_id'] and live:
        if left:
            buttons.append([('Запросить ссылку для оплаты', f'link:{ident}')])
            buttons.append([('Я оплатил' if b['status'] == 'held' else 'Доплатить', f'pay:{ident}')])
        buttons.append([('Добавить услуги', f'services:{ident}')])
        buttons.append([('Запрос отмены', f'cancel:{ident}'), ('Запрос переноса', f'move:{ident}')])
    if actor == b['guest_id'] and b['status'] == 'expired':
        if b['start_at'] >= now() + timedelta(hours=1):
            buttons.append([('Заплатить сейчас', f'paynow:{ident}')])
        else:
            buttons.append([('Выбрать новый сеанс', 'dates:0')])
    if await db.role(actor):
        text += f'\nГость: {b["name"]}, {b["phone"]}, MAX ID {b["guest_id"]}'
        for c in await db.pool.fetch("SELECT * FROM payment_claim WHERE booking_id=$1 AND status='pending'", ident):
            text += f'\nЗаявлена оплата {rub(c["amount"])}'
            buttons.append([(f'Подтвердить {rub(c["amount"])}', f'pc:{c["id"]}'), ('Не подтверждать', f'pr:{c["id"]}')])
        for r in rows:
            if r['status'] == 'requested':
                buttons.append([(f'Да: {r["name"]} × {r["quantity"]}', f'sa:{r["id"]}'), ('Нет', f'sr:{r["id"]}')])
        for r in await db.pool.fetch("SELECT * FROM change_request WHERE booking_id=$1 AND status='pending'", ident):
            label = {'cancel': 'отмены', 'move': 'переноса', 'no_show': 'отметки неявки'}[r['kind']]
            text += f'\nЗапрос {label}: {r["id"]}\n{r["target"]}'
            buttons.append([('Указать время и одобрить' if r['kind'] == 'move' else 'Одобрить запрос', f'ca:{r["id"]}'), ('Отклонить запрос', f'cr:{r["id"]}')])
        for r in await db.pool.fetch("SELECT * FROM duration_request WHERE booking_id=$1 AND status='pending'", ident):
            text += f'\nЗапрос длительности: {r["target_hours"]} ч.'
            buttons.append([('Подтвердить длительность', f'da:{r["id"]}'), ('Отклонить', f'dr:{r["id"]}')])
        for r in await db.pool.fetch("SELECT * FROM payment_link_request WHERE booking_id=$1 AND status='pending'", ident):
            buttons.append([('Передать ссылку гостю', f'sendlink:{r["id"]}')])
        if b['status'] in ('held', 'payment_review', 'review_overdue'):
            buttons.append([('Отклонить бронь и освободить', f'rejectask:{ident}')])
        if b['status'] in ('confirmed', 'in_progress'):
            buttons.append([('Сообщить о неявке', f'noshow:{ident}')])
    buttons.append([('Мои бронирования', 'orders'), ('Главное меню', 'home')])
    await chunks(message, text, kb(buttons))


async def admin_panel(message, actor):
    if not await db.role(actor):
        raise RuleError('Нет доступа к админ панели.')
    await message.answer('Админ панель', reply_markup=kb([[('Ближайшие брони', 'admin:list'), ('Ожидают решения', 'admin:pending')],
                                                          [('Ручная бронь', 'admin:manual'), ('Блокировка времени', 'admin:block')],
                                                          [('Праздники и Новый год', 'admin:holiday'), ('Возвраты без PayKeeper', 'admin:refund')],
                                                          [('Услуги и цены', 'admin:catalog'), ('Роли', 'admin:roles')]]))


async def summary(message, actor):
    state, data = await db.session(actor)
    if data.get('checkout_token'):
        await shop().store.contacts(actor, data['checkout_token'], data)
        await db.set_session(actor, '', {})
        await shop().cart(message, actor, data['checkout_token'])
        return
    start = datetime.fromisoformat(data['start'])
    amount, _ = await db.quote(start, data['hours'], data['guests'], data['children'])
    data['quote'] = amount
    await db.set_session(actor, 'consent', data)
    agreed = data.get('consent', False)
    await message.answer(f'Сеанс {start:%d.%m.%Y %H:%M}, {data["hours"]} ч.\nВсего гостей {data["guests"]}, детей до 6 {data["children"]}.\n{data["name"]}, {data["phone"]}\nАренда и гости {rub(amount)}. Предоплата 2000 ₽ в счёт заказа. Услуги можно добавить после создания резерва.\nОтмена и перенос по одобрению администратора; за ≥3 часа полный возврат, позднее возможны подтверждённые расходы на подготовку после проверки.\nОзнакомьтесь с правилами до оплаты.',
                         reply_markup=kb([[('Прочитать правила', 'rules')], [(('☑' if agreed else '□') + ' Я ознакомился и согласен с правилами бани „Пихта“', 'consent')], [('Создать резерв на 20 минут', 'hold')]]))


async def message_handler(message: MaxMessage):
    if message.chat.type != 'private' or not message.from_user or not message.from_user.id:
        return
    actor = message.from_user.id
    text = (message.text or '').strip()
    try:
        if text.startswith('/id'):
            await message.answer(f'Ваш MAX ID: {actor}')
            return
        if text in ('/start', '/cancel', 'Отмена'):
            if db:
                await db.set_session(actor, '', {})
            await message.answer('Добро пожаловать в баню «Пихта»! Выберите раздел.', reply_markup=MENU)
            if db and await db.role(actor):
                await message.answer('Управление: /admin')
            return
        if text == 'О бане':
            await message.answer('Баня на дровах «Пихта», посёлок Приладожское, территория Ladoga Home. До 8 гостей.\n' + SITE)
            return
        if text in ('Связаться с нами', 'Связаться с администратором'):
            await message.answer('Администратор: ' + PHONE + '\n' + SITE)
            return
        if text == 'Правила':
            await chunks(message, RULES)
            return
        if text == 'Цены':
            await message.answer('Первые 2 часа / дополнительный час:\nБудни до 19:00: 5000 / 2300 ₽\nБудни с 19:00 и выходные: 6000 / 2800 ₽\nПраздники: 6500 / 3000 ₽, либо тариф указанной администратором даты.\n4 платных гостя включены; сверх — 250 ₽/ч на человека. Дети до 6 включительно без доплаты, но в лимите 8.\nДлинная аренда по фактическим часам; оформляет администратор.')
            return
        if text in ('Услуги', 'Товары и услуги'):
            if await ready(message):
                await shop().catalog(message, actor)
            return
        if text == 'Корзина':
            if await ready(message):
                await shop().cart(message, actor)
            return
        if not await ready(message):
            return
        if text == 'Забронировать':
            if not BOOKING_ENABLED:
                await message.answer('Запись в боте пока настраивается. ' + PHONE)
                return
            await dates(message)
            return
        if text == 'Мои бронирования':
            await orders(message, actor)
            return
        if text.startswith('/'):
            await commands(message, actor, text)
            return
        state, data = await db.session(actor)
        if state == 'guests':
            value = int(text)
            if not 1 <= value <= 8:
                raise RuleError('Введите общее число гостей от 1 до 8.')
            data['guests'] = value
            await db.set_session(actor, 'children', data)
            await message.answer('Сколько среди них детей до 6 включительно? Введите 0, если нет.')
            return
        if state == 'children':
            value = int(text)
            if not 0 <= value <= data['guests']:
                raise RuleError('Детей не больше общего числа гостей.')
            data['children'] = value
            await shop().begin(message, actor, data)
            return
        if state == 'name':
            if not 2 <= len(text) <= 100:
                raise RuleError('Имя от 2 до 100 символов.')
            data['name'] = text
            await ask_phone(message, actor, data)
            return
        if state == 'phone':
            if message.contact and message.contact.user_id and message.contact.user_id != actor:
                raise RuleError('Поделитесь своим номером, а не контактом другого человека.')
            if message.contact and not message.contact.phone_number and not text:
                raise RuleError('Контакт без номера. Введите номер сообщением.')
            phone = message.contact.phone_number if message.contact and message.contact.phone_number else text
            data['phone'] = normalize_phone(phone)
            await db.set_session(actor, 'phone_confirm', data)
            await confirm_phone(message, data)
            return
        if state == 'phone_confirm':
            await confirm_phone(message, data)
            return
        if state == 'pay':
            await db.claim(actor, data['booking'], money(text))
            await db.set_session(actor, '', {})
            await message.answer('Заявление принято. Бронь остаётся занятой до решения администратора.')
            await show_order(message, data['booking'], actor)
            return
        if state == 'qty':
            await db.service(actor, data['booking'], data['code'], int(text), f'service:{actor}:{message.message_id}')
            await db.set_session(actor, '', {})
            await message.answer('Услуга отправлена на подтверждение.')
            await show_order(message, data['booking'], actor)
            return
        if state == 'move':
            if len(text) < 5:
                raise RuleError('Укажите желаемую дату и время.')
            await db.change(actor, data['booking'], 'move', text)
            await db.set_session(actor, '', {})
            await message.answer('Запрос переноса отправлен. Исходное время остаётся занято до решения.')
            return
        if state == 'approve_move':
            start = datetime.strptime(text, '%Y-%m-%d %H:%M').replace(tzinfo=TZ)
            ident = await db.decide_change(actor, data['request'], True, start)
            await db.set_session(actor, '', {})
            await show_order(message, ident, actor)
            return
        if state == 'sendlink':
            ident = await db.send_link(actor, data['request'], text)
            await db.set_session(actor, '', {})
            await message.answer('Ссылка поставлена в очередь доставки гостю.')
            await show_order(message, ident, actor)
            return
        await message.answer('Выберите раздел меню или /start.', reply_markup=MENU)
    except (ValueError, RuleError) as e:
        await message.answer(str(e) if isinstance(e, RuleError) else 'Проверьте формат ввода. Для выхода /cancel.')
    except Exception:
        report_error('Message')
        await message.answer('Операция не завершена. Повторите позже или свяжитесь с администратором; не оплачивайте повторно без сверки.')


async def commands(message, actor, text):
    parts = text.split()
    cmd = parts[0].split('@')[0]
    if cmd == '/admin':
        await admin_panel(message, actor)
        return
    if cmd == '/order' and len(parts) == 2:
        await show_order(message, parts[1], actor)
        return
    async with db.write() as c:
        await db.staff(actor, c)
    if cmd == '/paylink' and len(parts) == 3:
        await db.send_link(actor, parts[1], parts[2])
        await message.answer('Ссылка поставлена в очередь доставки.')
        return
    if cmd == '/move' and len(parts) == 4:
        start = datetime.fromisoformat(parts[2] + 'T' + parts[3]).replace(tzinfo=TZ)
        ident = await db.decide_change(actor, parts[1], True, start)
        await show_order(message, ident, actor)
        return
    if cmd == '/holiday' and len(parts) >= 5:
        day = date.fromisoformat(parts[1])
        base = money(parts[2])
        extra = money(parts[3])
        if base % 2:
            raise RuleError('Двухчасовой тариф должен делиться на два до копейки.')
        async with db.write() as c:
            await db.staff(actor, c)
            await c.execute('INSERT INTO holiday VALUES($1,$2,$3,$4,$5) ON CONFLICT(day) DO UPDATE SET base=$2,extra=$3,label=$4,actor=$5', day, base, extra, ' '.join(parts[4:])[:100], actor)
            await db.audit(c, actor, 'set_holiday', str(day), {'base': base, 'extra': extra})
        await message.answer('Праздничный тариф сохранён. Старые брони не пересчитаны.')
        return
    if cmd == '/unholiday' and len(parts) == 2:
        async with db.write() as c:
            await db.staff(actor, c)
            await c.execute('DELETE FROM holiday WHERE day=$1', date.fromisoformat(parts[1]))
            await db.audit(c, actor, 'remove_holiday', parts[1])
        await message.answer('Настройка праздничной даты снята.')
        return
    if cmd == '/block' and len(parts) >= 5:
        start = datetime.fromisoformat(parts[1] + 'T' + parts[2]).replace(tzinfo=TZ)
        end = datetime.fromisoformat(parts[1] + 'T' + parts[3]).replace(tzinfo=TZ)
        if end <= start:
            raise RuleError('Конец должен быть позже начала.')
        async with db.write() as c:
            await db.staff(actor, c)
            await db.available(c, start, end)
            ident = uid()
            await c.execute('INSERT INTO calendar_block(id,start_at,end_at,reason,actor) VALUES($1,$2,$3,$4,$5)', ident, start, end, ' '.join(parts[4:])[:500], actor)
            await db.audit(c, actor, 'block', ident)
        await message.answer(f'Время заблокировано. Снять: /unblock {ident}')
        return
    if cmd == '/unblock' and len(parts) == 2:
        async with db.write() as c:
            await db.staff(actor, c)
            await c.execute('UPDATE calendar_block SET active=false WHERE id=$1', parts[1])
            await db.audit(c, actor, 'unblock', parts[1])
        await message.answer('Блокировка снята.')
        return
    if cmd == '/manual' and len(parts) >= 10:
        if not BOOKING_ENABLED:
            raise RuleError('Приём броней ещё не включён.')
        if parts[8] != 'СОГЛАСИЕ':
            raise RuleError('Нужно явное согласие гостя с правилами; поле СОГЛАСИЕ подтверждает его получение.')
        d = {'start': datetime.fromisoformat(parts[1] + 'T' + parts[2]).replace(tzinfo=TZ).isoformat(), 'hours': int(parts[3]), 'guest_id': int(parts[4]), 'guests': int(parts[5]), 'children': int(parts[6]), 'phone': parts[7], 'name': ' '.join(parts[9:]), 'consent': True}
        ident = await db.hold(actor, d, RULES_HASH, f'manual:{actor}:{message.message_id}', True)
        await show_order(message, ident, actor)
        return
    if cmd == '/serviceprice' and len(parts) == 3:
        async with db.write() as c:
            await db.staff(actor, c, owner=True)
            amount = money(parts[2])
            result = await c.execute('UPDATE service_catalog SET price=$2 WHERE code=$1', parts[1], amount)
            if result == 'UPDATE 0':
                raise RuleError('Код услуги не найден.')
            await db.audit(c, actor, 'service_price', parts[1], {'price': amount})
        await message.answer('Цена услуги обновлена для новых заявок.')
        return
    if cmd in ('/addadmin', '/deladmin') and len(parts) == 2:
        user = int(parts[1])
        async with db.write() as c:
            await db.staff(actor, c, owner=True)
            if user == OWNER:
                raise RuleError('Роль основного владельца защищена настройками запуска.')
            if cmd == '/addadmin':
                await c.execute("INSERT INTO staff VALUES($1,'admin') ON CONFLICT(telegram_id) DO UPDATE SET role='admin'", user)
            else:
                await c.execute("DELETE FROM staff WHERE telegram_id=$1 AND role='admin'", user)
            await db.audit(c, actor, cmd, str(user))
        await message.answer('Роли обновлены.')
        return
    raise RuleError('Команда не распознана. Откройте /admin для инструкции.')


async def callback_handler(call):
    if not call.message:
        await call.answer('Сообщение недоступно. Откройте /start для актуального меню.', show_alert=True)
        return
    if call.message.chat.type != 'private':
        return
    actor = call.from_user.id
    message = call.message
    data = call.data or ''
    try:
        if data == 'rules':
            await call.answer()
            await chunks(message, RULES)
            return
        if not db:
            raise RuleError('Бронирование пока настраивается.')
        if data.startswith('shop:'):
            await shop().handle(call)
            return
        action, _, value = data.partition(':')
        if action == 'home':
            await db.set_session(actor, '', {})
            await message.answer('Главное меню', reply_markup=MENU)
            if await db.role(actor):
                await message.answer('Админ панель: /admin')
            await call.answer()
            return
        if action == 'orders':
            await orders(message, actor)
            await call.answer()
            return
        if action == 'order':
            await show_order(message, value, actor)
        elif action in ('phoneyes', 'phoneother'):
            state, d = await db.session(actor)
            if state != 'phone_confirm' or d.get('operation') != value:
                raise RuleError('Это подтверждение устарело. Продолжите текущее оформление.')
            if action == 'phoneother':
                await ask_phone(message, actor, d, False)
            else:
                await db.save_phone(actor, d['phone'])
                await message.answer('Номер для связи подтверждён.', reply_markup=MENU)
                await summary(message, actor)
        elif action == 'paynow':
            if not BOOKING_ENABLED:
                raise RuleError('Запись временно отключена.')
            ident = await db.reopen_for_payment(actor, value, RULES_HASH)
            await db.request_link(actor, ident)
            b = await db.get(ident, actor)
            await message.answer(f'Резерв действует до {b["release_at"].astimezone(TZ):%H:%M}. Запрос ссылки отправлен администратору; повторное нажатие срок не продлевает.')
            await show_order(message, ident, actor)
        elif action == 'dates':
            await dates(message, max(0, int(value)))
        elif action == 'date':
            if not BOOKING_ENABLED:
                raise RuleError('Запись пока отключена.')
            day = date.fromisoformat(value)
            buttons = []
            if not now().date() <= day <= horizon(now().date()):
                raise RuleError('Дата вне горизонта записи.')
            for hour in SLOTS:
                start = datetime.combine(day, datetime.min.time(), TZ) + timedelta(hours=hour)
                try:
                    _, clean = window(start)
                    await db.available(db.pool, start, clean)
                    buttons.append([(f'{hour:02}:00–{(hour + 2) % 24:02}:00', f'slot:{start.isoformat()}')])
                except RuleError:
                    pass
            await message.answer('Выберите сеанс.' if buttons else 'Свободных сеансов нет.', reply_markup=kb(buttons + [[('Другие даты', 'dates:0')]]))
        elif action == 'slot':
            if not BOOKING_ENABLED:
                raise RuleError('Запись пока отключена.')
            window(datetime.fromisoformat(value))
            await db.set_session(actor, 'guests', {'start': value, 'hours': 2, 'operation': uid()})
            await message.answer('Сколько всего гостей, включая детей? От 1 до 8.')
        elif action == 'long':
            await message.answer('Аренду на 3–5 часов оформляет администратор. ' + PHONE)
        elif action == 'consent':
            state, d = await db.session(actor)
            if state != 'consent':
                raise RuleError('Оформление устарело. Начните заново.')
            d['consent'] = not d.get('consent', False)
            await db.set_session(actor, state, d)
            await summary(message, actor)
        elif action == 'hold':
            if not BOOKING_ENABLED:
                raise RuleError('Запись пока отключена.')
            state, d = await db.session(actor)
            if state != 'consent':
                raise RuleError('Оформление устарело. Откройте Мои бронирования.')
            amount, _ = await db.quote(datetime.fromisoformat(d['start']), d['hours'], d['guests'], d['children'])
            if amount != d['quote']:
                d['consent'] = False
                await db.set_session(actor, 'consent', d)
                await summary(message, actor)
                raise RuleError('Цена изменилась; подтвердите новый расчёт.')
            ident = await db.hold(actor, d, RULES_HASH, d['operation'])
            await db.set_session(actor, '', {})
            await message.answer('Резерв создан. Предоплата 2000 ₽ входит в заказ. Запросите ссылку у администратора через кнопку ниже. Ожидание ссылки не продлевает резерв.')
            await show_order(message, ident, actor)
        elif action == 'link':
            await db.request_link(actor, value)
            await message.answer('Запрос отправлен администратору. Ссылка придёт сюда. Ожидание ссылки не продлевает резерв.')
        elif action == 'pay':
            b = await db.get(value, actor)
            if b['guest_id'] != actor:
                raise RuleError('Это действие гостя.')
            if b['status'] == 'held':
                await db.claim(actor, value, 200000)
                await message.answer('Вы заявили оплату предоплаты 2000 ₽. Три часа проверки отсчитываются от этого нажатия. Время сохранено до решения администратора.')
            else:
                await db.set_session(actor, 'pay', {'booking': value})
                await message.answer('После фактической доплаты введите перечисленную сумму в рублях: всю или часть остатка. Для выхода /cancel.')
        elif action == 'services':
            token = await shop().store.start(actor, {}, value)
            await shop().catalog(message, actor, token)
        elif action in ('cplus', 'cminus'):
            ident, code, token = value.split(':')
            await db.cart_adjust(actor, ident, code, token, 1 if action == 'cplus' else -1)
            await show_cart(message, actor, True)
        elif action == 'cartinfo':
            await call.answer('Нажимайте «−» и «+», чтобы изменить количество.')
            return
        elif action in ('csend', 'cdiscard'):
            ident, token = value.split(':')
            await db.finish_cart(actor, ident, token, action == 'csend')
            await message.answer('Все выбранные услуги отправлены на подтверждение.' if action == 'csend' else 'Выбор услуг отменён.')
            await show_order(message, ident, actor)
        elif action == 'svc':
            ident, code = value.split(':')
            await db.get(ident, actor)
            await db.set_session(actor, 'qty', {'booking': ident, 'code': code})
            await message.answer('Введите количество. Для выхода /cancel.')
        elif action == 'cancel':
            await db.get(value, actor)
            await message.answer('Отправить запрос отмены? До одобрения время занято.', reply_markup=kb([[('Да, отправить', f'cancelok:{value}')]]))
        elif action == 'cancelok':
            await db.change(actor, value, 'cancel')
            await message.answer('Запрос отмены отправлен. До решения время занято.')
        elif action == 'move':
            await db.get(value, actor)
            await db.set_session(actor, 'move', {'booking': value})
            await message.answer('Укажите желаемую дату и время. Перенос после одобрения.')
        elif action in ('pc', 'pr'):
            await show_order(message, await db.decide_payment(actor, value, action == 'pc'), actor)
        elif action in ('sa', 'sr'):
            await show_order(message, await db.decide_service(actor, value, action == 'sa'), actor)
        elif action in ('ca', 'cr'):
            r = await db.pool.fetchrow('SELECT * FROM change_request WHERE id=$1', value)
            async with db.write() as c:
                await db.staff(actor, c)
            if action == 'ca' and r and r['kind'] == 'move':
                await db.set_session(actor, 'approve_move', {'request': value})
                await message.answer('Введите новое начало: ГГГГ-ММ-ДД ЧЧ:ММ. Время и уборка будут проверены перед переносом. Для выхода /cancel.')
            else:
                await show_order(message, await db.decide_change(actor, value, action == 'ca'), actor)
        elif action in ('da', 'dr'):
            await show_order(message, await shop().store.decide_duration(actor, value, action == 'da'), actor)
        elif action == 'noshow':
            await db.change(actor, value, 'no_show')
            await message.answer('Отметка передана администратору. Деньги автоматически не изменены.')
        elif action == 'sendlink':
            async with db.write() as c:
                await db.staff(actor, c)
            await db.set_session(actor, 'sendlink', {'request': value})
            await message.answer('Введите HTTPS ссылку для оплаты. Бот передаст её гостю.')
        elif action == 'rejectask':
            async with db.write() as c:
                await db.staff(actor, c)
            await message.answer('Проверьте поступление денег перед отказом. Отклонить бронь и освободить время?', reply_markup=kb([[('Подтверждаю отказ', f'reject:{value}')]]))
        elif action == 'reject':
            await db.reject_booking(actor, value)
            await show_order(message, value, actor)
        elif action == 'admin':
            await admin_action(message, actor, value)
        else:
            raise RuleError('Кнопка устарела. Откройте /start.')
        await call.answer()
    except (ValueError, RuleError) as e:
        await call.answer(str(e)[:190] if isinstance(e, RuleError) else 'Проверьте формат данных.', show_alert=True)
    except Exception:
        report_error('Callback')
        await call.answer('Ошибка. Проверьте актуальный заказ перед повтором.', show_alert=True)


async def admin_action(message, actor, action):
    async with db.write() as c:
        await db.staff(actor, c)
    if action in ('list', 'pending'):
        if action == 'list':
            rows = await db.pool.fetch('SELECT id,start_at,status FROM booking WHERE cleanup_end>=now() ORDER BY start_at LIMIT 30')
        else:
            rows = await db.pool.fetch("SELECT DISTINCT b.id,b.start_at,b.status FROM booking b WHERE b.status IN ('held','payment_review','review_overdue') OR EXISTS(SELECT 1 FROM payment_claim p WHERE p.booking_id=b.id AND p.status='pending') OR EXISTS(SELECT 1 FROM booking_service s WHERE s.booking_id=b.id AND s.status='requested') OR EXISTS(SELECT 1 FROM change_request r WHERE r.booking_id=b.id AND r.status='pending') OR EXISTS(SELECT 1 FROM payment_link_request l WHERE l.booking_id=b.id AND l.status='pending') OR EXISTS(SELECT 1 FROM duration_request d WHERE d.booking_id=b.id AND d.status='pending') ORDER BY start_at LIMIT 30")
        await message.answer('Брони' if rows else 'Нет заявок.', reply_markup=kb([[(r['start_at'].astimezone(TZ).strftime('%d.%m %H:%M') + ' ' + STATUS[r['status']], f'order:{r["id"]}')] for r in rows]) if rows else None)
    elif action == 'manual':
        await message.answer('Ручная бронь после явного согласия гостя с правилами:\n/manual ГГГГ-ММ-ДД ЧЧ:ММ ЧАСЫ MAX_ID ВСЕГО ДЕТЕЙ ТЕЛЕФОН СОГЛАСИЕ ИМЯ\nСначала гость открывает бота. Резерв 20 минут; затем заявление оплаты и проверка.')
    elif action == 'block':
        rows = await db.pool.fetch('SELECT * FROM calendar_block WHERE active AND end_at>now() ORDER BY start_at')
        await chunks(message, 'Блокировать: /block ГГГГ-ММ-ДД ЧЧ:ММ ЧЧ:ММ ПРИЧИНА\nСнять: /unblock ID\n' + '\n'.join(f'{r["id"]} {r["start_at"].astimezone(TZ):%d.%m %H:%M} {r["reason"]}' for r in rows))
    elif action == 'holiday':
        rows = await db.pool.fetch('SELECT * FROM holiday WHERE day>=CURRENT_DATE ORDER BY day LIMIT 60')
        await chunks(message, 'Задать: /holiday ГГГГ-ММ-ДД ЦЕНА_2Ч ДОП_ЧАС НАЗВАНИЕ\nСнять: /unholiday ГГГГ-ММ-ДД\nНовогодние цены задаются таким же способом.\n' + '\n'.join(f'{r["day"]}: {rub(r["base"])}/{rub(r["extra"])} {r["label"]}' for r in rows))
    elif action == 'catalog':
        rows = sorted_services(await db.pool.fetch('SELECT * FROM service_catalog'))
        await message.answer('\n'.join(f'{r["code"]}: {r["name"]} {rub(r["price"])}' for r in rows) + '\nВладелец: /serviceprice КОД ЦЕНА. Подтверждайте доступные услуги.')
    elif action == 'roles':
        rows = await db.pool.fetch('SELECT * FROM staff')
        await message.answer('\n'.join(f'{r["telegram_id"]}: ' + {'owner': 'Владелец', 'admin': 'Администратор'}[r['role']] for r in rows) + '\nВладелец: /addadmin ID или /deladmin ID. Администратор сначала открывает /start.')
    elif action == 'refund':
        rows = await db.pool.fetch('SELECT * FROM refund ORDER BY created_at DESC LIMIT 20')
        await chunks(message, 'PayKeeper отложен. Автоматической отправки возвратов нет. Заявки для ручного рассмотрения:\n' + '\n'.join(f'{r["id"]}: {rub(r["amount"])} · {r["reason"]} · заказ {r["booking_id"]}' for r in rows))


async def worker():
    last_backup = 0
    while True:
        try:
            loop = asyncio.get_running_loop()
            if loop.time() - last_backup >= 21600:
                from backup import snapshot
                try:
                    await snapshot(os.environ['DATABASE_URL'], os.getenv('BACKUP_DIR', '/app/data/backups'))
                    last_backup = loop.time()
                except Exception:
                    logging.error('Backup failed; booking timers continue')
                    last_backup = loop.time() - 21540
            await db.maintenance()
            for _ in range(20):
                row = await db.next_message()
                if not row:
                    break
                text = await db.outgoing_text(row)
                try:
                    if text:
                        markup = kb([[('Добавить услуги', f'services:{row["booking_id"]}'), ('Доплатить', f'pay:{row["booking_id"]}')]]) if row['kind'] == 'remind_1' else None
                        if row['kind'] == 'expired':
                            markup = kb([[('Заплатить сейчас', f'paynow:{row["booking_id"]}')]])
                        await api.send(user_id=row['chat_id'], text=text,
                                       attachments=[markup.to_max()] if markup else None)
                    await db.sent(row['id'])
                except Exception:
                    await db.sent(row['id'], 'MAX delivery failed')
        except Exception:
            logging.error('Background operation failed; details suppressed')
        await asyncio.sleep(5)


# ---------- updates dispatch ----------

async def handle_update(update):
    kind = update.get('update_type')
    if kind == 'message_created':
        message = MaxMessage(api, update.get('message') or {}, banner_token_cache=BANNER)
        await message_handler(message)
    elif kind == 'message_callback':
        call = MaxCallback(api, update, banner_token_cache=BANNER)
        await callback_handler(call)
    elif kind == 'bot_started':
        user = update.get('user') or {}
        synthetic = {'body': {'mid': '', 'text': '/start'},
                     'sender': user,
                     'recipient': {'chat_type': 'dialog', 'chat_id': update.get('chat_id'),
                                   'user_id': user.get('user_id')}}
        await message_handler(MaxMessage(api, synthetic, banner_token_cache=BANNER))


def _updates_from(body):
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        if 'update_type' in body:
            return [body]
        if isinstance(body.get('updates'), list):
            return body['updates']
    return []


# ---------- web server ----------

async def health(request):
    return web.json_response({'status': 'ok'})


async def webhook(request):
    secret = request.headers.get('X-Max-Bot-Api-Secret', '')
    if WEBHOOK_SECRET and not hmac.compare_digest(secret, WEBHOOK_SECRET):
        return web.Response(status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'ok': True})
    for update in _updates_from(body):
        try:
            await handle_update(update)
        except Exception:
            report_error('Update')
    return web.json_response({'ok': True})


async def poll_updates():
    marker = None
    while True:
        try:
            result = await api.get_updates(marker=marker, timeout=25,
                                           types=['message_created', 'message_callback', 'bot_started'])
            marker = result.get('marker', marker)
            for update in result.get('updates') or []:
                try:
                    await handle_update(update)
                except Exception:
                    report_error('Update')
        except Exception:
            await asyncio.sleep(3)


async def upload_banner():
    image = Path(__file__).with_name('banner.png')
    if USE_BRAND and image.exists():
        try:
            BANNER['token'] = (await api.upload('image', image.read_bytes(),
                                                filename='banner.png'))['payload']['token']
            logging.info('Banner uploaded')
        except Exception:
            logging.warning('Banner upload failed; text-only messages')


async def main():
    global db, api
    token = (os.getenv('MAX_BOT_TOKEN') or os.getenv('BOT_TOKEN') or '').strip()
    if not token:
        raise SystemExit('Задайте MAX_BOT_TOKEN (или BOT_TOKEN) в хостинге.')
    api = MaxApi(token)
    url = os.getenv('DATABASE_URL', '').strip()
    if url:
        if OWNER <= 0:
            raise SystemExit('Задайте OWNER_MAX_ID в настройках хостинга.')
        try:
            db = await Store.connect(url, OWNER)
        except Exception:
            raise SystemExit('Не удалось подключить PostgreSQL. Проверьте DATABASE_URL. Секреты скрыты.') from None
    elif BOOKING_ENABLED:
        raise SystemExit('При включённой записи необходим DATABASE_URL.')
    webhook_url = os.getenv('WEBHOOK_URL', '').strip()
    task = None
    poll_task = None
    try:
        if db:
            async with db.pool.acquire() as lock:
                if not await lock.fetchval('SELECT pg_try_advisory_lock(7319842)'):
                    raise SystemExit('Другой экземпляр бота уже работает.')
        identity = await api.get_me()
        await api.patch_commands([{'name': 'start', 'description': 'Главное меню'},
                                  {'name': 'id', 'description': 'Мой MAX ID'},
                                  {'name': 'admin', 'description': 'Управление'}])
        await upload_banner()
        logging.info('Бот %s готов; PostgreSQL %s; запись %s', identity.get('name'),
                     'подключена' if db else 'отключена', 'включена' if BOOKING_ENABLED else 'выключена')
        if db:
            task = asyncio.create_task(worker())
        if webhook_url:
            try:
                await api.subscribe(webhook_url, secret=WEBHOOK_SECRET or None,
                                    update_types=['message_created', 'message_callback', 'bot_started'])
                logging.info('Webhook подписан: %s', webhook_url)
            except Exception:
                logging.error('Не удалось подписать вебхук %s', webhook_url)
            app = web.Application()
            app.router.add_get('/health', health)
            app.router.add_head('/webhook', webhook)
            app.router.add_post('/webhook', webhook)
            runner = web.AppRunner(app)
            await runner.setup()
            port = int(os.getenv('PORT', '3000'))
            site = web.TCPSite(runner, '0.0.0.0', port)
            await site.start()
            logging.info('HTTP сервер на порту %d', port)
            while True:
                await asyncio.sleep(3600)
        else:
            poll_task = asyncio.create_task(poll_updates())
            await asyncio.Event().wait()
    finally:
        for t in (task, poll_task):
            if t:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        if db:
            await db.pool.close()
        await api.close()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    MaxApi.quiet_loggers()
    try:
        asyncio.run(main())
    except Exception:
        raise SystemExit('Бот остановлен из-за ошибки; секреты скрыты.') from None
