"""Consistent logical snapshots, no secrets. Run python backup.py with DATABASE_URL."""
import asyncio, gzip, json, os
from datetime import datetime, timezone
from pathlib import Path
import asyncpg

TABLES=('bath_resource','staff','settings','holiday','service_catalog','booking','calendar_block','payment_claim','payment_link_request','receipt','booking_service','change_request','refund','audit','ui_session','guest_contact','checkout_draft','duration_request','outbox')

async def snapshot(url,folder='/app/data/backups',schema=None):
    path=Path(folder);path.mkdir(parents=True,exist_ok=True)
    c=await asyncpg.connect(url,server_settings={'search_path':schema} if schema else None)
    try:
        async with c.transaction(isolation='repeatable_read',readonly=True):
            data={t:json.loads(await c.fetchval(f"SELECT COALESCE(json_agg(row_to_json(t)), '[]'::json) FROM {t} t")) for t in TABLES}
        stamp=datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
        output=path/f'pihta-{stamp}.json.gz';temporary=output.with_suffix('.tmp')
        with gzip.open(temporary,'wt',encoding='utf-8') as f:json.dump({'format':1,'created_at':stamp,'tables':data},f,ensure_ascii=False)
        temporary.replace(output)
        return output
    finally:await c.close()

if __name__=='__main__':
    try:print(asyncio.run(snapshot(os.environ['DATABASE_URL'],os.getenv('BACKUP_DIR','/app/data/backups'))))
    except Exception:raise SystemExit('Backup failed; details suppressed to protect secrets.') from None
