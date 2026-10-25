import unittest
from unittest.mock import AsyncMock
from datetime import datetime, timedelta
from types import SimpleNamespace
from transport import MaxMessage
from checkout import CheckoutUI
from domain import TZ, SERVICES


class CheckoutUITests(unittest.IsolatedAsyncioTestCase):
    def context(self):
        db = AsyncMock()
        db.pool.fetch.return_value = [{'code': c, 'name': n, 'price': p} for c, n, p in SERVICES]
        return SimpleNamespace(db=db, show_order=AsyncMock(), use_brand=False, BANNER={'token': None}, BOOKING_ENABLED=True, RULES_HASH='h' * 64)

    def message(self, photo=None):
        raw = {'body': {'mid': 'mid-1', 'text': '',
                        'attachments': ([{'type': 'image', 'payload': {'token': 'img-token'}}] if photo else None)},
               'sender': {'user_id': 2},
               'recipient': {'chat_type': 'dialog', 'chat_id': 100, 'user_id': 2}}
        m = MaxMessage(AsyncMock(), raw, banner_token_cache={'token': 'img-token'})
        m.answer = AsyncMock()
        m.edit_text = AsyncMock()
        m.edit_caption = AsyncMock()
        return m

    def data(self):
        return {'start': (datetime.now(TZ) + timedelta(days=1)).replace(hour=10, minute=0).isoformat(), 'hours': 2, 'base_hours': 2, 'guests': 4, 'children': 0, 'quote': 500000, 'rental_added': True, 'cart': {'birch': {'name': 'Веник берёзовый', 'quantity': 2, 'price': 50000}}, 'name': 'Тест', 'phone': '+79900000000', 'consent': False}

    async def test_cart_is_read_only_and_has_checkout(self):
        ui = CheckoutUI(self.context())
        ui.store.quote = AsyncMock(return_value=('a' * 12, self.data()))
        m = self.message()
        await ui.cart(m, 2)
        rows = m.answer.call_args.kwargs['reply_markup'].inline_keyboard
        buttons = [b for row in rows for b in row]
        self.assertFalse(any(b.text in ('+', '−', 'Удалить') for b in buttons))
        self.assertTrue(any('Получить ссылку' in b.text for b in buttons))
        self.assertIn('6 000 ₽', m.answer.call_args.args[0])
        self.assertIn('× 2', m.answer.call_args.args[0])

    async def test_product_has_quantity_only_before_add(self):
        ui = CheckoutUI(self.context())
        d = self.data()
        d['item'] = {'name': 'Веник берёзовый', 'price': 50000, 'qty': 2, 'nonce': 'b' * 8, 'added': False}
        ui.store.read = AsyncMock(return_value=('a' * 12, d))
        m = self.message()
        await ui.product(m, 2, 'a' * 12)
        buttons = [b for row in m.answer.call_args.kwargs['reply_markup'].inline_keyboard for b in row]
        self.assertIn('+', [b.text for b in buttons])
        self.assertIn('−', [b.text for b in buttons])
        self.assertTrue(any('Добавить в корзину' in b.text for b in buttons))
        self.assertTrue(all(len(b.callback_data.encode()) <= 1024 for b in buttons))
        d['item']['added'] = True
        m = self.message()
        await ui.product(m, 2, 'a' * 12)
        buttons = [b for row in m.answer.call_args.kwargs['reply_markup'].inline_keyboard for b in row]
        self.assertFalse(any(b.text in ('+', '−') for b in buttons))

    async def test_catalog_brooms_first_and_navigation(self):
        ui = CheckoutUI(self.context())
        ui.store.read = AsyncMock(return_value=('a' * 12, self.data()))
        m = self.message()
        await ui.catalog(m, 2)
        buttons = [b for row in m.answer.call_args.kwargs['reply_markup'].inline_keyboard for b in row]
        self.assertTrue(buttons[0].text.startswith('Веник берёзовый'))
        self.assertTrue(buttons[1].text.startswith('Веник дубовый'))
        self.assertTrue(any('Главное меню' in b.text for b in buttons))

    async def test_quantity_edits_existing_message(self):
        ui = CheckoutUI(self.context())
        d = self.data()
        d['item'] = {'name': 'Веник', 'price': 50000, 'qty': 2, 'nonce': 'b' * 8, 'added': False}
        ui.store.read = AsyncMock(return_value=('a' * 12, d))
        m = self.message()
        await ui.product(m, 2, 'a' * 12, True)
        m.edit_text.assert_awaited_once()
        m.answer.assert_not_awaited()

    async def test_duration_display_has_hour_step_and_approval(self):
        ui = CheckoutUI(self.context())
        d = self.data()
        d['hours'] = 3
        d['quote'] = 730000
        ui.store.quote = AsyncMock(return_value=('a' * 12, d))
        m = self.message()
        await ui.rental(m, 2, 'a' * 12)
        self.assertIn('Дополнительное время: 1 ч.', m.answer.call_args.args[0])
        self.assertIn('подтвердит администратор', m.answer.call_args.args[0])

    async def test_photo_caption_is_edited_without_new_message(self):
        ui = CheckoutUI(self.context())
        d = self.data()
        d['item'] = {'name': 'Веник', 'price': 50000, 'qty': 2, 'nonce': 'b' * 8, 'added': False}
        ui.store.read = AsyncMock(return_value=('a' * 12, d))
        m = self.message(photo=True)
        await ui.product(m, 2, 'a' * 12, True)
        m.edit_caption.assert_awaited_once()
        m.edit_text.assert_not_awaited()
        m.answer.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
