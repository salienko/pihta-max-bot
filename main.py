"""Entry point for hosting platforms (Bothost): runs the MAX bot."""
import asyncio
import logging

import bot
from maxapi import MaxApi

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    MaxApi.quiet_loggers()
    try:
        asyncio.run(bot.main())
    except Exception:
        raise SystemExit('Бот остановлен из-за ошибки; секреты скрыты.') from None
