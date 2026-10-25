import unittest
from unittest.mock import AsyncMock
from types import SimpleNamespace
from transport import MaxMessage
from domain import SERVICES
import bot


def make_message(text, user=2, contact=None):
    raw = {'body': {'mid': 'mid-1', 'text': text,
                    'attachments': ([{'type': 'contact',
                                      'payload': {'vcf_info': 'TEL;TYPE=CELL:+79210000000',
                                                  'max_info': {'user_id': user}}}] if contact else None)},
           'sender': {'user_id': user},
           'recipient': {'chat_type': 'dialog', 'chat_id': 100, 'user_id': user}}
    api = AsyncMock()
    message = MaxMessage(api, raw)
    message.answer = AsyncMock()
    return message


class UITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.previous = bot.db

    async def asyncTearDown(self):
        bot.db = self.previous

    async def test_menu_without_database(self):
        bot.db = None
        for text in ['/start', '/id', 'Цены', 'Правила', 'О бане', 'Связаться с нами', 'Забронировать']:
            with self.subTest(text=text):
                message = make_message(text)
                await bot.message_handler(message)
                self.assertTrue(message.answer.called)

    async def test_guests_validation(self):
        bot.db = AsyncMock()
        bot.db.session.return_value = ('guests', {})
        message = make_message('9')
        await bot.message_handler(message)
        bot.db.set_session.assert_not_called()
        self.assertIn('1 до 8', message.answer.call_args.args[0])

    async def test_children_validation(self):
        bot.db = AsyncMock()
        bot.db.session.return_value = ('children', {'guests': 4})
        message = make_message('5')
        await bot.message_handler(message)
        bot.db.set_session.assert_not_called()

    async def test_phone_validation(self):
        bot.db = AsyncMock()
        bot.db.session.return_value = ('phone', {})
        message = make_message('abc')
        await bot.message_handler(message)
        self.assertIn('российский номер', message.answer.call_args.args[0])

    async def test_contact_phone(self):
        bot.db = AsyncMock()
        bot.db.session.return_value = ('phone', {'operation': 'tok'})
        message = make_message('', contact=True)
        await bot.message_handler(message)
        args = bot.db.set_session.call_args.args
        self.assertEqual(args[1], 'phone_confirm')
        self.assertEqual(args[2]['phone'], '+79210000000')

    async def test_id_reports_max_id(self):
        bot.db = None
        message = make_message('/id', user=77)
        await bot.message_handler(message)
        self.assertIn('77', message.answer.call_args.args[0])
        self.assertIn('MAX ID', message.answer.call_args.args[0])


class WebhookTests(unittest.IsolatedAsyncioTestCase):
    async def test_updates_from(self):
        single = {'update_type': 'message_created', 'message': {}}
        self.assertEqual(bot._updates_from(single), [single])
        listed = {'updates': [single]}
        self.assertEqual(bot._updates_from(listed), [single])
        self.assertEqual(bot._updates_from([single]), [single])
        self.assertEqual(bot._updates_from({}), [])


if __name__ == '__main__':
    unittest.main()
