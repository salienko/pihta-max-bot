"""Consistent JSON snapshot of this application's PostgreSQL schema."""
import asyncio
from datetime import datetime, timezone
import gzip
import json
import os
from pathlib import Path
import re

import asyncpg
from booking_store import SCHEMA

TABLES = ('profiles','drafts','bookings','audit','outbox','operations','calendar_blocks','service_settings','holiday_rates','payment_requests','payment_messages','paykeeper_invoices','paykeeper_payments','paykeeper_fiscal_receipts','paykeeper_unmatched','paykeeper_settlements','booking_payment_windows')


async def snapshot(url, folder, schema=SCHEMA):
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,62}', schema):
        raise ValueError('Invalid schema name')
    connection = await asyncpg.connect(url, server_settings={'search_path': schema})
    try:
        async with connection.transaction(isolation='repeatable_read', readonly=True):
            tables = {table: json.loads(await connection.fetchval(
                f"SELECT COALESCE(json_agg(row_to_json(t)), '[]'::json) FROM {table} t")) for table in TABLES}
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        result = folder / f'pihta-orders-{stamp}.json.gz'
        temporary = result.with_suffix('.tmp')
        with gzip.open(temporary, 'wt', encoding='utf-8') as handle:
            json.dump({'format': 2, 'schema': schema, 'created_at': stamp, 'tables': tables}, handle, ensure_ascii=False)
        temporary.replace(result)
        return result
    finally:
        await connection.close()


if __name__ == '__main__':
    try:
        print(asyncio.run(snapshot(os.environ['DATABASE_URL'], os.getenv('BACKUP_DIR', 'backups'))))
    except Exception as exc:
        raise SystemExit('Не удалось создать копию: ' + type(exc).__name__) from None
