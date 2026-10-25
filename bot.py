"""MAX adapter for the current Telegram booking v3 branch."""
import asyncio, hashlib, hmac, logging, os
from aiohttp import web
from maxapi import MaxApi
from transport_v3 import MaxMessage, MaxCallback, InlineKeyboardMarkup, InlineKeyboardButton, markup_attachments
from settings import load
from booking_store import BookingStore
from booking_ui import BookingUI

async def deliver_once(api, store):
    row = await store.next_delivery()
    if not row:
        return False
    try:
        owner = await store.pool.fetchval('SELECT telegram_id FROM bookings WHERE id=$1', row['booking_id']) if row['booking_id'] else row['recipient']
        if row['recipient'] != owner and row['recipient'] not in store.settings.admin_ids:
            await store.failed(row['id'], 'recipient_access_removed', permanent=True)
            return True
        buttons = [[('Открыть веб-панель', 'staffweb')]] if row['recipient'] in store.settings.admin_ids else [[('Открыть бронь', 'open:' + row['booking_id'])]]
        payment = await store.payment_delivery(row['event'])
        if row['event'].startswith(('payreq:', 'paylink:', 'payreported:')) and not payment:
            await store.delivered(row['id'])
            return True
        if payment and row['event'].startswith('paylink:') and payment.get('url'):
            buttons = [[('🔗 Перейти к оплате', payment['url'])], [('✅ Уже оплатил', 'payreport:' + payment['id'])], [('Открыть бронь', 'open:' + row['booking_id'])]]
        markup = InlineKeyboardMarkup([[InlineKeyboardButton(t, url=v if v.startswith('https://') else None, callback_data=None if v.startswith('https://') else v) for t, v in r] for r in buttons])
        await api.send(user_id=row['recipient'], text=row['text'], attachments=markup_attachments(markup))
    except Exception as exc:
        await store.failed(row['id'], type(exc).__name__, min(3600, 5 * 2 ** min(row['attempts'], 9)))
    else:
        await store.delivered(row['id'])
    return True

async def worker(api, store):
    last_backup = 0
    while True:
        try:
            await store.tick()
            for _ in range(20):
                if not await deliver_once(api, store):
                    break
            if asyncio.get_running_loop().time() - last_backup >= 21600:
                from backup import snapshot
                await snapshot(store.settings.database_url, os.getenv('BACKUP_DIR', '/app/data/backups'))
                last_backup = asyncio.get_running_loop().time()
        except Exception as exc:
            logging.error('Background job failed: %s', type(exc).__name__)
        await asyncio.sleep(10)

async def main():
    settings = load()
    if not settings.token or not settings.admin_ids or not settings.database_url:
        raise SystemExit('Configure MAX_BOT_TOKEN/BOT_TOKEN, DATABASE_URL and OWNER_MAX_ID.')
    api = MaxApi(settings.token)
    store = await BookingStore.connect(settings)
    task = None
    try:
        async with store.pool.acquire() as lock:
            if not await lock.fetchval('SELECT pg_try_advisory_lock(7319842)'):
                raise SystemExit('Another bot instance is running.')
        ui = BookingUI(settings, store)

        async def dispatch(update):
            kind = update.get('update_type')
            if kind == 'message_created':
                await ui.message(MaxMessage(api, update.get('message') or {}))
            elif kind == 'message_callback':
                await ui.callback(MaxCallback(api, update))
            elif kind == 'bot_started':
                user = update.get('user') or {}
                raw = {'body': {'mid': '', 'text': '/start'}, 'sender': user,
                       'recipient': {'chat_type': 'dialog', 'user_id': user.get('user_id'), 'chat_id': update.get('chat_id')}}
                await ui.message(MaxMessage(api, raw))

        async def health(request):
            return web.json_response({'status': 'ok'})

        async def webhook(request):
            secret = request.headers.get('X-Max-Bot-Api-Secret', '')
            expected = os.getenv('WEBHOOK_SECRET', '')
            if expected and not hmac.compare_digest(secret, expected):
                return web.Response(status=403)
            try:
                body = await request.json()
            except Exception:
                return web.json_response({'ok': True})
            updates = body if isinstance(body, list) else body.get('updates', [body]) if isinstance(body, dict) else []
            for update in updates:
                try:
                    await dispatch(update)
                except Exception:
                    logging.exception('Update failed')
            return web.json_response({'ok': True})

        await api.get_me()
        await api.patch_commands([{'name': 'start', 'description': 'Главное меню'}, {'name': 'book', 'description': 'Забронировать баню'}, {'name': 'bookings', 'description': 'Мои бронирования'}, {'name': 'help', 'description': 'Помощь'}])
        webhook_url = os.getenv('WEBHOOK_URL', '').strip()
        if webhook_url:
            await api.subscribe(webhook_url, secret=os.getenv('WEBHOOK_SECRET', '') or None, update_types=['message_created', 'message_callback', 'bot_started'])
        app = web.Application()
        app.router.add_get('/health', health)
        app.router.add_head('/webhook', webhook)
        app.router.add_post('/webhook', webhook)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, '0.0.0.0', int(os.getenv('PORT', '3000'))).start()
        task = asyncio.create_task(worker(api, store))
        logging.info('Pihta booking v3 MAX ready; shared-schema adapter')
        while True:
            await asyncio.sleep(3600)
    finally:
        if task:
            task.cancel()
        await store.pool.close()
        await api.close()

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    try:
        asyncio.run(main())
    except Exception as exc:
        raise SystemExit('Startup failed: ' + type(exc).__name__) from None
