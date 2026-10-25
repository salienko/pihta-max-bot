"""Inline catalogue: quantity is selected in product cards, never in the cart.

MAX Messenger port: rendering goes through the transport shim; the banner
image is uploaded once and its token reused (MAX has no inline media, the
image is a separate attachment).
"""
import asyncio
from pathlib import Path
from datetime import datetime, timedelta
from transport import InlineKeyboardMarkup, InlineKeyboardButton
from domain import RuleError, rub, sorted_services
from checkout_store import CheckoutStore

_locks = {}


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=v) for t, v in row] for row in rows])


class CheckoutUI:
    def __init__(self, context):
        self.b = context
        self.db = context.db
        self.store = CheckoutStore(self.db)

    def button(self, action, token, value='', nonce=''):
        return ':'.join(('shop', action, token, value, nonce))

    async def render(self, message, text, rows, edit=False):
        markup = kb(rows)
        banner = getattr(self.b, 'BANNER', {}).get('token') if getattr(self.b, 'use_brand', True) else None
        if edit and message.photo:
            if len(text) <= 1000:
                await message.edit_caption(text, reply_markup=markup)
                return
            edit = False
        if edit:
            await message.edit_text(text, reply_markup=markup)
            return
        if banner and len(text) <= 1000:
            await message.answer_photo(banner, caption=text, reply_markup=markup)
        else:
            await message.answer(text, reply_markup=markup)

    async def begin(self, message, actor, data):
        token = await self.store.start(actor, data)
        await self.rental(message, actor, token)

    async def rental(self, message, actor, token, edit=False):
        token, d = await self.store.quote(actor, token)
        if d.get('submitted'):
            return await self.b.show_order(message, d['submitted'], actor)
        start = datetime.fromisoformat(d['start'])
        end = start + timedelta(hours=d['hours'])
        extra = d['hours'] - d['base_hours']
        text = f'🌲 Сеанс бани «Пихта»\n{start:%d.%m.%Y %H:%M}–{end:%H:%M}\nОсновной сеанс: {d["base_hours"]} ч.\nДополнительное время: {extra} ч.\nВсего гостей: {d["guests"]}; детей до 6 включительно: {d["children"]}\nАренда и гости: {rub(d["quote"])}\nУборка после сеанса: 1 час.'
        if d['hours'] > 2:
            text += '\nДополнительное время подтвердит администратор. До одобрения продление существующей брони не закреплено.'
        btn = lambda action: self.button(action, token)
        rows = [[('−', btn('hourminus')), (f'+{extra} ч.', btn('info')), ('+', btn('hourplus'))],
                [('Добавить сеанс и выбрать товары' if not d.get('booking') else 'Сохранить выбор и выбрать товары', btn('rental'))],
                [('🛒 Корзина', btn('cart'))], [('🏠 Главное меню', 'home')]]
        await self.render(message, text, rows, edit)

    async def catalog(self, message, actor, token=None, edit=False):
        try:
            token, d = await self.store.read(actor, token)
        except RuleError:
            items = sorted_services(await self.db.pool.fetch('SELECT * FROM service_catalog WHERE active'))
            return await self.render(message, '🌿 Товары и услуги\n' + '\n'.join(f'{r["name"]}: {rub(r["price"])}' for r in items) + '\nСначала выберите сеанс бани. Выполнение услуг подтверждает администратор.', [[('📅 Выбрать сеанс', 'dates:0')], [('📋 Мои бронирования', 'orders')], [('🏠 Главное меню', 'home')]], edit)
        if d.get('submitted'):
            return await self.b.show_order(message, d['submitted'], actor)
        if not d['rental_added']:
            return await self.rental(message, actor, token, edit)
        items = sorted_services(await self.db.pool.fetch('SELECT * FROM service_catalog WHERE active'))
        rows = [[(f'{r["name"]} · {rub(r["price"])}', self.button('product', token, r['code']))] for r in items]
        rows.extend([[('🕒 Дополнительный час', self.button('hours', token))], [('🛒 Корзина', self.button('cart', token))], [('⬅ Назад к сеансу', self.button('hours', token)), ('🏠 Главное меню', 'home')]])
        await self.render(message, '🌿 Товары и услуги\nВыберите товар, укажите количество и добавьте в корзину.\nВеники первыми, остальные товары по алфавиту.', rows, edit)

    async def product(self, message, actor, token, edit=False):
        _, d = await self.store.read(actor, token)
        item = d.get('item', {})
        if not item:
            raise RuleError('Выберите товар из каталога.')
        text = f'🌿 {item["name"]}\nЦена за единицу: {rub(item["price"])}\nКоличество: {item["qty"]}\nСумма: {rub(item["qty"] * item["price"])}\nДоступность и выполнение подтвердит администратор.'
        btn = lambda action: self.button(action, token, '', item['nonce'])
        rows = []
        if not item['added']:
            rows.extend([[('−', btn('minus')), (f'{item["qty"]} шт.', btn('info')), ('+', btn('plus'))], [('🛒 Добавить в корзину', btn('add'))]])
        else:
            text += '\n✅ Этот выбор уже добавлен в корзину.'
        rows.extend([[('✅ Оформить / получить ссылку', self.button('cart', token))], [('⬅ К списку', self.button('catalog', token))], [('🏠 Главное меню', 'home')]])
        await self.render(message, text, rows, edit)

    async def cart(self, message, actor, token=None, edit=False):
        try:
            token, d = await self.store.quote(actor, token)
        except RuleError as e:
            return await self.render(message, str(e), [[('🌿 Каталог товаров', self.button('catalog', token or ''))], [('📅 Выбрать сеанс', 'dates:0')], [('Очистить черновик', self.button('clearask', token))]] if token else [[('📅 Выбрать сеанс', 'dates:0')], [('🏠 Главное меню', 'home')]], edit)
        if d.get('submitted'):
            return await self.b.show_order(message, d['submitted'], actor)
        if not d['rental_added']:
            return await self.rental(message, actor, token, edit)
        start = datetime.fromisoformat(d['start'])
        end = start + timedelta(hours=d['hours'])
        goods = sum(r['quantity'] * r['price'] for r in d['cart'].values())
        total = d['quote'] + goods
        if d.get('booking'):
            approved, paid, left = await self.db.totals(d['booking'])
            b = await self.db.get(d['booking'], actor)
            total = approved + d['quote'] - b['rent'] + goods
            head = f'🛒 Дополнения к брони {d["booking"]}\nПодтверждено ранее: {rub(approved)}; оплачено: {rub(paid)}\n'
        else:
            head = '🛒 Ваша корзина\nЧерновик: время пока не забронировано.\n'
        text = head + f'\n🌲 Баня: {start:%d.%m.%Y %H:%M}–{end:%H:%M}\nОсновной сеанс {d["base_hours"]} ч.; дополнительно {d["hours"] - d["base_hours"]} ч.\nГостей {d["guests"]}, детей до 6 включительно {d["children"]}\nАренда и гости: {rub(d["quote"])}\n'
        for line in sorted_services([dict(v, code=k) for k, v in d['cart'].items()]):
            text += f'\n{line["name"]} · {rub(line["price"])} × {line["quantity"]} = {rub(line["price"] * line["quantity"])}'
        text += f'\n\nПредварительный итог: {rub(total)}\nТовары и дополнительное время подтвердит администратор.\nКоличество выбирается в карточке товара до добавления в корзину.'
        if not d.get('booking'):
            text += '\nПредоплата 2000 ₽ в счёт заказа. Резерв после оформления — 20 минут; ожидание ссылки срок не продлевает.'
        rows = [[('🌿 Добавить товары', self.button('catalog', token))], [('🕒 Дополнительный час', self.button('hours', token))]]
        if d.get('name') and d.get('phone'):
            text += f'\n\nКонтакт: {d["name"]}, {d["phone"]}'
            rows.extend([[('📖 Прочитать правила', 'rules')], [(('☑' if d.get('consent') else '□') + ' Я ознакомился и согласен с правилами бани „Пихта“', self.button('consent', token))]])
        rows.extend([[('🔗 Получить ссылку для оплаты', self.button('checkout', token))], [('🗑 Очистить черновик', self.button('clearask', token))], [('⬅ К каталогу', self.button('catalog', token)), ('🏠 Главное меню', 'home')]])
        await self.render(message, text, rows, edit)

    async def handle(self, call):
        actor = call.from_user.id
        async with _locks.setdefault(actor, asyncio.Lock()):
            parts = (call.data or '').split(':')
            parts += [''] * (5 - len(parts))
            _, action, token, value, nonce = parts[:5]
            m = call.message
            if not m:
                await call.answer('Сообщение недоступно. Откройте /start.')
                return
            if action in ('product', 'plus', 'minus', 'add', 'hourplus', 'hourminus', 'rental', 'clear', 'consent'):
                await self.store.action(actor, token, action, value, nonce)
            if action in ('product', 'plus', 'minus', 'add'):
                await self.product(m, actor, token, True)
                if action == 'add':
                    await call.answer('Добавлено в корзину')
                    return
            elif action in ('hours', 'hourplus', 'hourminus'):
                await self.rental(m, actor, token, True)
            elif action in ('catalog', 'rental'):
                await self.catalog(m, actor, token, True)
            elif action in ('cart', 'consent', 'clear'):
                await self.cart(m, actor, token, True)
            elif action == 'clearask':
                await self.render(m, 'Очистить выбранные позиции черновика? Оформленные брони сохраняются.', [[('Да, очистить', self.button('clear', token))], [('⬅ В корзину', self.button('cart', token))]], True)
            elif action == 'checkout':
                _, d = await self.store.quote(actor, token)
                if d.get('submitted'):
                    await self.b.show_order(m, d['submitted'], actor)
                elif not d.get('name') or not d.get('phone'):
                    await self.db.set_session(actor, 'name', dict(d, checkout_token=token, operation=token))
                    await m.answer('Укажите Ваше имя для брони')
                else:
                    if not self.b.BOOKING_ENABLED:
                        raise RuleError('Запись временно отключена.')
                    ident = await self.store.checkout(actor, token, self.b.RULES_HASH)
                    await self.db.set_session(actor, '', {})
                    await m.answer('Заказ оформлен. Запрос ссылки отправлен администратору. Товары и дополнительное время ожидают его подтверждения. Ожидание ссылки не продлевает резерв.')
                    await self.b.show_order(m, ident, actor)
            elif action == 'info':
                await call.answer('Количество выбирается только в карточке до добавления.')
                return
            else:
                raise RuleError('Кнопка устарела.')
            await call.answer()
