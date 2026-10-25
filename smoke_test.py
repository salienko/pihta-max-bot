"""Smoke test: webhook server + update dispatch with mocked MAX API."""
import asyncio
import json
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import bot
from transport import MaxMessage


async def run():
    bot.api = AsyncMock()
    bot.api.get_me.return_value = {'name': '@pihta_test'}
    bot.db = None
    app = web.Application()
    app.router.add_get('/health', bot.health)
    app.router.add_head('/webhook', bot.webhook)
    app.router.add_post('/webhook', bot.webhook)
    client = TestClient(TestServer(app))
    await client.start_server()

    r = await client.get('/health')
    assert r.status == 200 and (await r.json())['status'] == 'ok', 'health failed'

    update = {'update_type': 'message_created',
              'message': {'body': {'mid': 'm1', 'text': '/start'},
                          'sender': {'user_id': 42},
                          'recipient': {'chat_type': 'dialog', 'chat_id': 900, 'user_id': 42}}}
    r = await client.post('/webhook', json=update)
    assert r.status == 200, f'webhook status {r.status}'
    bot.api.send.assert_awaited()
    kwargs = bot.api.send.await_args.kwargs
    assert kwargs['user_id'] == 42, f'unexpected target {kwargs}'
    att = kwargs.get('attachments')
    assert att and att[0]['type'] == 'inline_keyboard', 'menu keyboard missing'
    kinds = {b['type'] for row in att[0]['payload']['buttons'] for b in row}
    assert 'message' in kinds, f'menu should use message buttons, got {kinds}'

    started = {'update_type': 'bot_started', 'chat_id': 900, 'user': {'user_id': 42}}
    bot.api.send.reset_mock()
    r = await client.post('/webhook', json=started)
    assert r.status == 200
    bot.api.send.assert_awaited()

    r = await client.post('/webhook', data=b'not json')
    assert r.status == 200

    await client.close()
    print('SMOKE OK: health, message_created, bot_started, menu keyboard, bad body')


asyncio.run(run())
