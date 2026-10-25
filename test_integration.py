"""Run on a test DB only. Uses a separate schema; never uses DATABASE_URL."""
import asyncio, os, unittest, uuid
from datetime import timedelta
from pathlib import Path
import asyncpg
from domain import TZ, RuleError, now, price
from store import Store

@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'),'TEST_DATABASE_URL is not set')
class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema='test_'+uuid.uuid4().hex
        self.admin=await asyncpg.connect(os.environ['TEST_DATABASE_URL'])
        await self.admin.execute(f'CREATE SCHEMA {self.schema}')
        self.pool=await asyncpg.create_pool(os.environ['TEST_DATABASE_URL'],min_size=1,max_size=5,server_settings={'search_path':self.schema})
        async with self.pool.acquire() as c:
            await c.execute(Path(__file__).with_name('schema.sql').read_text(encoding='utf-8'))
            await c.execute("INSERT INTO staff VALUES(1,'owner')")
            await c.execute("INSERT INTO service_catalog(code,name,price) VALUES('towel','Полотенце',10000)")
        self.db=Store(self.pool)
        self.start=(now()+timedelta(days=1)).replace(hour=10,minute=0,second=0,microsecond=0)
    async def asyncTearDown(self):
        await self.pool.close();await self.admin.execute(f'DROP SCHEMA {self.schema} CASCADE');await self.admin.close()
    def data(self,actor=2):
        return {'start':self.start.isoformat(),'hours':2,'guests':4,'children':0,'name':'Test','phone':'+79990000000','consent':True,'quote':price(self.start,2,4,0)[0],'guest_id':actor}
    async def create(self,actor=2):return await self.db.hold(actor,self.data(actor),'rules','hold:'+str(actor))
    async def test_concurrent_holds(self):
        results=await asyncio.gather(self.create(2),self.create(3),return_exceptions=True)
        self.assertEqual(sum(isinstance(x,str) for x in results),1)
        self.assertEqual(sum(isinstance(x,RuleError) for x in results),1)
    async def test_claim_duplicate_and_receipt(self):
        ident=await self.create();claim=await self.db.claim(2,ident,200000)
        self.assertEqual(claim,await self.db.claim(2,ident,200000))
        await self.db.decide_payment(1,claim,True);await self.db.decide_payment(1,claim,True)
        self.assertEqual((await self.db.totals(ident))[1],200000)
        self.assertEqual((await self.db.get(ident,2))['status'],'confirmed')
    async def test_expire_and_overdue(self):
        ident=await self.create();await self.pool.execute("UPDATE booking SET release_at=now()-interval '1 second' WHERE id=$1",ident)
        await self.db.maintenance();self.assertEqual((await self.db.get(ident,2))['status'],'expired')
        ident=await self.create(3);await self.db.claim(3,ident,200000)
        await self.pool.execute("UPDATE booking SET verification_due=now()-interval '1 second' WHERE id=$1",ident)
        await self.db.maintenance();self.assertEqual((await self.db.get(ident,3))['status'],'review_overdue')
        with self.assertRaises(RuleError):await self.db.available(self.pool,self.start,self.start+timedelta(hours=3))
    async def test_cancel_separate_refund(self):
        ident=await self.create();claim=await self.db.claim(2,ident,200000);await self.db.decide_payment(1,claim,True)
        rid=await self.db.change(2,ident,'cancel')
        with self.assertRaises(RuleError):await self.db.available(self.pool,self.start,self.start+timedelta(hours=3))
        await self.db.decide_change(1,rid,True)
        await self.db.available(self.pool,self.start,self.start+timedelta(hours=3))
        self.assertEqual(await self.pool.fetchval('SELECT count(*) FROM refund'),1)
    async def test_permissions(self):
        ident=await self.create()
        with self.assertRaises(RuleError):await self.db.get(ident,3)
        claim=await self.db.claim(2,ident,200000)
        with self.assertRaises(RuleError):await self.db.decide_payment(3,claim,True)
    async def test_link_lifecycle(self):
        ident=await self.create();rid=await self.db.request_link(2,ident)
        self.assertEqual(rid,await self.db.request_link(2,ident))
        with self.assertRaises(RuleError):await self.db.send_link(3,rid,'https://example.com/payment')
        await self.db.send_link(1,rid,'https://example.com/payment')
        self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM outbox WHERE kind='payment_link'"),1)
    async def test_services_do_not_move(self):
        ident=await self.create();await self.db.service(2,ident,'towel',3,'s1');await self.db.service(2,ident,'towel',3,'s1')
        service=await self.pool.fetchval('SELECT id FROM booking_service')
        await self.db.decide_service(1,service,True)
        self.assertEqual((await self.db.totals(ident))[0],self.data()['quote']+30000)
        self.assertEqual((await self.db.get(ident,2))['start_at'].astimezone(TZ),self.start)

    async def test_move_conflict_rolls_back_then_moves_atomically(self):
        ident=await self.create();claim=await self.db.claim(2,ident,200000)
        await self.db.decide_payment(1,claim,True)
        other=self.data(3);other['start']=(self.start+timedelta(hours=3)).isoformat()
        other['quote']=price(self.start+timedelta(hours=3),2,4,0)[0]
        await self.db.hold(3,other,'rules','hold-other')
        request=await self.db.change(2,ident,'move','13:00')
        with self.assertRaises(RuleError):await self.db.decide_change(1,request,True,self.start+timedelta(hours=3))
        self.assertEqual((await self.db.get(ident,2))['start_at'].astimezone(TZ),self.start)
        target=self.start+timedelta(hours=6)
        await self.db.decide_change(1,request,True,target)
        moved=await self.db.get(ident,2)
        self.assertEqual(moved['start_at'].astimezone(TZ),target)
        self.assertEqual(moved['revision'],2)
        await self.db.available(self.pool,self.start,self.start+timedelta(hours=3))

    async def test_payment_link_rejected_after_expiry(self):
        ident=await self.create();rid=await self.db.request_link(2,ident)
        await self.pool.execute("UPDATE booking SET release_at=now()-interval '1 second' WHERE id=$1",ident)
        with self.assertRaises(RuleError):await self.db.send_link(1,rid,'https://example.com/pay')

    async def test_reopen_expired_slot_only_if_free(self):
        ident=await self.create()
        await self.pool.execute("UPDATE booking SET release_at=now()-interval '1 second' WHERE id=$1",ident)
        await self.db.maintenance()
        old=await self.db.get(ident,2)
        await self.db.reopen_for_payment(2,ident,'rules')
        reopened=await self.db.get(ident,2)
        self.assertEqual(reopened['status'],'held');self.assertGreater(reopened['release_at'],old['release_at'])
        deadline=reopened['release_at']
        await self.db.reopen_for_payment(2,ident,'rules')
        self.assertEqual((await self.db.get(ident,2))['release_at'],deadline)
        await self.pool.execute("UPDATE booking SET release_at=now()-interval '1 second' WHERE id=$1",ident)
        await self.db.maintenance();await self.create(3)
        with self.assertRaises(RuleError):await self.db.reopen_for_payment(2,ident,'rules')
        self.assertEqual((await self.db.get(ident,2))['status'],'expired')

    async def test_basket_atomic_and_idempotent(self):
        ident=await self.create()
        await self.pool.execute("INSERT INTO service_catalog VALUES('oak','Веник дубовый',60000,true)")
        await self.db.service_basket(2,ident,{'towel':3,'oak':2},'basket-one')
        await self.db.service_basket(2,ident,{'towel':3,'oak':2},'basket-one')
        self.assertEqual(await self.pool.fetchval('SELECT count(*) FROM booking_service'),2)
        with self.assertRaises(RuleError):await self.db.service_basket(2,ident,{'towel':1,'missing':2},'basket-two')
        self.assertEqual(await self.pool.fetchval('SELECT count(*) FROM booking_service'),2)

    async def test_saved_contact_is_owned(self):
        await self.db.save_phone(2,'89812440050')
        self.assertEqual(await self.db.saved_phone(2),'+79812440050')
        self.assertIsNone(await self.db.saved_phone(3))

    async def test_cart_parallel_increments(self):
        ident=await self.create()
        await self.db.set_session(2,'cart',{'booking':ident,'cart':{},'token':'token'})
        await asyncio.gather(self.db.cart_adjust(2,ident,'towel','token',1),self.db.cart_adjust(2,ident,'towel','token',1))
        _,data=await self.db.session(2)
        self.assertEqual(data['cart']['towel'],2)
        with self.assertRaises(RuleError):await self.db.cart_adjust(2,ident,'towel','stale',1)

    async def test_finish_cart_once_with_current_quantity(self):
        ident=await self.create()
        await self.db.set_session(2,'cart',{'booking':ident,'cart':{'towel':1},'token':'token'})
        await self.db.cart_adjust(2,ident,'towel','token',1)
        results=await asyncio.gather(self.db.finish_cart(2,ident,'token'),self.db.finish_cart(2,ident,'token'),return_exceptions=True)
        self.assertEqual(sum(isinstance(r,RuleError) for r in results),1)
        self.assertEqual(await self.pool.fetchval('SELECT quantity FROM booking_service'),2)
        self.assertEqual((await self.db.session(2))[0],'')
        with self.assertRaises(RuleError):await self.db.cart_adjust(2,ident,'towel','token',1)

    async def test_empty_cart_stays_open(self):
        ident=await self.create()
        await self.db.set_session(2,'cart',{'booking':ident,'cart':{},'token':'token'})
        with self.assertRaises(RuleError):await self.db.finish_cart(2,ident,'token')
        self.assertEqual((await self.db.session(2))[0],'cart')
        await self.db.finish_cart(2,ident,'token',False)
        self.assertEqual(await self.pool.fetchval('SELECT count(*) FROM booking_service'),0)

    async def checkout_cart(self,actor=2,hours=2):
        from checkout_store import CheckoutStore
        cs=CheckoutStore(self.db);d=self.data(actor);d['hours']=hours
        token=await cs.start(actor,d)
        await cs.action(actor,token,'rental')
        await cs.quote(actor,token)
        return cs,token

    async def test_checkout_product_selection_and_atomic_creation(self):
        cs,token=await self.checkout_cart()
        await cs.action(2,token,'product','towel');_,d=await cs.read(2,token);nonce=d['item']['nonce']
        await asyncio.gather(cs.action(2,token,'plus',nonce=nonce),cs.action(2,token,'plus',nonce=nonce))
        _,d=await cs.read(2,token);self.assertEqual(d['item']['qty'],3);self.assertEqual(d['cart'],{})
        await cs.action(2,token,'add',nonce=nonce)
        with self.assertRaises(RuleError):await cs.action(2,token,'add',nonce=nonce)
        await cs.quote(2,token);await cs.action(2,token,'consent')
        ids=await asyncio.gather(cs.checkout(2,token,'rules'),cs.checkout(2,token,'rules'))
        self.assertEqual(ids[0],ids[1]);self.assertEqual(await self.pool.fetchval('SELECT count(*) FROM booking'),1)
        self.assertEqual(await self.pool.fetchval('SELECT quantity FROM booking_service'),3)
        self.assertEqual(await self.pool.fetchval('SELECT count(*) FROM payment_link_request'),1)

    async def test_checkout_consent_and_prices(self):
        cs,token=await self.checkout_cart()
        with self.assertRaises(RuleError):await cs.checkout(2,token,'rules')
        await cs.action(2,token,'product','towel');_,d=await cs.read(2,token)
        await cs.action(2,token,'add',nonce=d['item']['nonce']);await cs.quote(2,token);await cs.action(2,token,'consent')
        await self.pool.execute("UPDATE service_catalog SET price=20000 WHERE code='towel'")
        with self.assertRaises(RuleError):await cs.checkout(2,token,'rules')
        _,d=await cs.quote(2,token);self.assertFalse(d['consent']);self.assertEqual(d['cart']['towel']['price'],20000)
        self.assertEqual(await self.pool.fetchval('SELECT count(*) FROM booking'),0)

    async def test_long_checkout_requires_administrator_and_blocks_cleanup(self):
        cs,token=await self.checkout_cart(hours=3);await cs.action(2,token,'consent')
        ident=await cs.checkout(2,token,'rules');b=await self.db.get(ident,2)
        self.assertFalse(b['duration_approved'])
        rid=await self.pool.fetchval('SELECT id FROM payment_link_request')
        with self.assertRaises(RuleError):await self.db.send_link(1,rid,'https://example.com/pay')
        claim=await self.db.claim(2,ident,200000)
        with self.assertRaises(RuleError):await self.db.decide_payment(1,claim,True)
        req=await self.pool.fetchval('SELECT id FROM duration_request')
        with self.assertRaises(RuleError):await cs.decide_duration(3,req,True)
        await cs.decide_duration(1,req,True);await self.db.decide_payment(1,claim,True)
        other=self.data(3);other['start']=(self.start+timedelta(hours=3)).isoformat();other['quote']=price(self.start+timedelta(hours=3),2,4,0)[0]
        with self.assertRaises(RuleError):await self.db.hold(3,other,'rules','conflict-cleaning')

    async def test_checkout_owned_and_stale_cards(self):
        cs,token=await self.checkout_cart()
        with self.assertRaises(RuleError):await cs.action(3,token,'rental')
        await cs.action(2,token,'product','towel');_,d=await cs.read(2,token);nonce=d['item']['nonce']
        await cs.action(2,token,'product','towel')
        with self.assertRaises(RuleError):await cs.action(2,token,'plus',nonce=nonce)
        self.assertEqual(await self.pool.fetchval('SELECT count(*) FROM booking'),0)

    async def test_extra_hour_conflict_keeps_original_booking(self):
        ident=await self.create();cl=await self.db.claim(2,ident,200000);await self.db.decide_payment(1,cl,True)
        from checkout_store import CheckoutStore
        cs=CheckoutStore(self.db);token=await cs.start(2,{},ident)
        await cs.action(2,token,'hourplus');await cs.quote(2,token);await cs.action(2,token,'consent')
        await cs.checkout(2,token,'rules');req=await self.pool.fetchval('SELECT id FROM duration_request')
        other=self.data(3);other['start']=(self.start+timedelta(hours=3)).isoformat();other['quote']=price(self.start+timedelta(hours=3),2,4,0)[0]
        await self.db.hold(3,other,'rules','other')
        with self.assertRaises(RuleError):await cs.decide_duration(1,req,True)
        self.assertEqual((await self.db.get(ident,2))['end_at'],self.start+timedelta(hours=2))
        self.assertEqual(await self.pool.fetchval('SELECT status FROM duration_request'),'pending')

    async def test_extra_hour_approval_preserves_original_rent(self):
        ident=await self.create();cl=await self.db.claim(2,ident,200000);await self.db.decide_payment(1,cl,True)
        from checkout_store import CheckoutStore
        cs=CheckoutStore(self.db);token=await cs.start(2,{},ident)
        old=(await self.db.get(ident,2))['rent']
        await cs.action(2,token,'hourplus');_,d=await cs.quote(2,token)
        target=d['quote'];await cs.action(2,token,'consent');await cs.checkout(2,token,'rules')
        req=await self.pool.fetchval('SELECT id FROM duration_request');await cs.decide_duration(1,req,True)
        b=await self.db.get(ident,2);self.assertEqual(b['rent'],target);self.assertGreater(b['rent'],old)
        self.assertEqual(b['cleanup_end'],self.start+timedelta(hours=4))

    async def test_new_payment_amount_not_silently_ignored(self):
        ident=await self.create();await self.db.claim(2,ident,200000)
        with self.assertRaises(RuleError):await self.db.claim(2,ident,150000)
        self.assertEqual(await self.pool.fetchval('SELECT count(*) FROM payment_claim'),1)

    async def test_long_rejection_closes_pending_services_and_links(self):
        cs,token=await self.checkout_cart(hours=3)
        await cs.action(2,token,'product','towel');_,d=await cs.read(2,token)
        await cs.action(2,token,'add',nonce=d['item']['nonce']);await cs.quote(2,token);await cs.action(2,token,'consent')
        ident=await cs.checkout(2,token,'rules');req=await self.pool.fetchval('SELECT id FROM duration_request')
        await cs.decide_duration(1,req,False)
        self.assertEqual((await self.db.get(ident,2))['status'],'rejected')
        self.assertEqual(await self.pool.fetchval('SELECT status FROM booking_service'),'rejected')
        self.assertEqual(await self.pool.fetchval('SELECT status FROM payment_link_request'),'closed')

    async def test_complete_guest_checkout_handlers(self):
        import bot
        from unittest.mock import AsyncMock
        from aiogram.types import Message,Chat,User,CallbackQuery,PhotoSize
        saved_db,saved_enabled=bot.db,bot.BOOKING_ENABLED
        bot.db=self.db;bot.BOOKING_ENABLED=True
        def msg(text=None,photo=False):
            m=Message(message_id=1,date=now(),chat=Chat(id=2,type='private'),from_user=User(id=2,is_bot=False,first_name='Test'),text=text)
            for attr in ('answer','answer_photo','edit_text','edit_caption','edit_reply_markup'):object.__setattr__(m,attr,AsyncMock())
            if photo:object.__setattr__(m,'photo',[PhotoSize(file_id='x',file_unique_id='x',width=900,height=270)])
            return m
        async def click(data):
            m=msg(photo=True)
            call=CallbackQuery(id=uuid.uuid4().hex,from_user=User(id=2,is_bot=False,first_name='Test'),chat_instance='x',data=data,message=m)
            object.__setattr__(call,'answer',AsyncMock())
            await bot.callback_handler(call)
            for args in call.answer.call_args_list:
                self.assertFalse(args.kwargs.get('show_alert'),args)
            return m
        try:
            await click('slot:'+self.start.isoformat())
            await bot.message_handler(msg('4'));await bot.message_handler(msg('0'))
            token=await self.pool.fetchval('SELECT token FROM checkout_draft WHERE telegram_id=2')
            await click(f'shop:rental:{token}::')
            await click(f'shop:product:{token}:towel:')
            from checkout_store import CheckoutStore
            cs=CheckoutStore(self.db);_,d=await cs.read(2,token);nonce=d['item']['nonce']
            await click(f'shop:plus:{token}::{nonce}');await click(f'shop:add:{token}::{nonce}')
            await click(f'shop:cart:{token}::');await click(f'shop:checkout:{token}::')
            self.assertEqual((await self.db.session(2))[0],'name')
            await bot.message_handler(msg('Test'));await bot.message_handler(msg('79990000000'))
            await click(f'phoneyes:{token}');await click(f'shop:consent:{token}::')
            await click(f'shop:checkout:{token}::')
            self.assertEqual(await self.pool.fetchval('SELECT count(*) FROM booking'),1)
            self.assertEqual(await self.pool.fetchval('SELECT quantity FROM booking_service'),2)
            self.assertEqual(await self.pool.fetchval('SELECT count(*) FROM payment_link_request'),1)
        finally:bot.db=saved_db;bot.BOOKING_ENABLED=saved_enabled

    async def test_backup_restore_in_empty_schema(self):
        import gzip,json,tempfile
        from backup import snapshot,TABLES
        ident=await self.create();await self.db.claim(2,ident,200000)
        other='restore_'+uuid.uuid4().hex
        await self.admin.execute(f'CREATE SCHEMA {other}')
        try:
            with tempfile.TemporaryDirectory() as folder:
                output=await snapshot(os.environ['TEST_DATABASE_URL'],folder,self.schema)
                with gzip.open(output,'rt',encoding='utf-8') as f:data=json.load(f)
            await self.admin.execute(f'SET search_path TO {other}')
            await self.admin.execute(Path(__file__).with_name('schema.sql').read_text(encoding='utf-8'))
            await self.admin.execute('TRUNCATE bath_resource')
            for table in TABLES:
                await self.admin.execute(f'INSERT INTO {table} SELECT * FROM jsonb_populate_recordset(NULL::{table},$1::jsonb)',json.dumps(data['tables'][table]))
            self.assertEqual(await self.admin.fetchval('SELECT status FROM booking WHERE id=$1',ident),'payment_review')
            self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM payment_claim'),1)
            self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM outbox'),2)
        finally:
            await self.admin.execute('SET search_path TO public')
            await self.admin.execute(f'DROP SCHEMA {other} CASCADE')

if __name__=='__main__':unittest.main()
