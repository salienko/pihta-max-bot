"""PostgreSQL service. Calendar writes serialize on the single resource row."""
import json
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
import asyncpg
from domain import ACTIVE, SERVICES, RuleError, now, price, window, claim_allowed, rub, normalize_phone

def uid():
    return uuid.uuid4().hex

class Store:
    def __init__(self, pool):
        self.pool = pool

    @classmethod
    async def connect(cls, url, owner):
        pool = await asyncpg.create_pool(url, min_size=1, max_size=5, command_timeout=20)
        async with pool.acquire() as c:
            await c.execute(Path(__file__).with_name('schema.sql').read_text(encoding='utf-8'))
            await c.execute('INSERT INTO staff VALUES($1,$2) ON CONFLICT(telegram_id) DO UPDATE SET role=$2', owner, 'owner')
            for code, name, cost in SERVICES:
                await c.execute('INSERT INTO service_catalog(code,name,price) VALUES($1,$2,$3) ON CONFLICT DO NOTHING',code,name,cost)
        return cls(pool)

    @asynccontextmanager
    async def write(self):
        async with self.pool.acquire() as c:
            async with c.transaction():
                # All state-changing booking commands take this same lock first.
                await c.fetchval('SELECT id FROM bath_resource WHERE id=1 FOR UPDATE')
                yield c

    async def role(self, actor, c=None):
        return await (c or self.pool).fetchval('SELECT role FROM staff WHERE telegram_id=$1',actor)

    async def staff(self, actor, c, owner=False):
        role = await self.role(actor,c)
        if not role or (owner and role != 'owner'):
            raise RuleError('Это действие доступно только уполномоченному администратору.')

    async def audit(self,c,actor,action,entity,details=None):
        await c.execute('INSERT INTO audit(actor,action,entity_id,details) VALUES($1,$2,$3,$4::jsonb)',actor,action,entity,json.dumps(details or {},ensure_ascii=False))

    async def queue(self,c,chat,text,key,booking=None,kind=None,due=None):
        await c.execute('INSERT INTO outbox(chat_id,text,dedupe_key,booking_id,revision,kind,due_at) VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT DO NOTHING',chat,text,key,booking['id'] if booking else None,booking['revision'] if booking else None,kind,due or now())

    async def notify_staff(self,c,text,key):
        for row in await c.fetch('SELECT telegram_id FROM staff'):
            await self.queue(c,row['telegram_id'],text,f'{key}:{row["telegram_id"]}')

    async def session(self,actor):
        r=await self.pool.fetchrow('SELECT state,data FROM ui_session WHERE telegram_id=$1',actor)
        return (r['state'],json.loads(r['data'])) if r else ('',{})

    async def set_session(self,actor,state,data):
        await self.pool.execute('INSERT INTO ui_session VALUES($1,$2,$3::jsonb,now()) ON CONFLICT(telegram_id) DO UPDATE SET state=$2,data=$3::jsonb,updated_at=now()',actor,state,json.dumps(data))

    async def saved_phone(self,actor):
        value=await self.pool.fetchval('SELECT phone FROM guest_contact WHERE telegram_id=$1',actor)
        if not value:value=await self.pool.fetchval('SELECT phone FROM booking WHERE guest_id=$1 ORDER BY created_at DESC LIMIT 1',actor)
        if value:
            try:return normalize_phone(value)
            except RuleError:pass
        return None

    async def save_phone(self,actor,phone):
        await self.pool.execute('INSERT INTO guest_contact VALUES($1,$2,now()) ON CONFLICT(telegram_id) DO UPDATE SET phone=$2,updated_at=now()',actor,normalize_phone(phone))

    async def reopen_for_payment(self,actor,ident,rules_hash):
        async with self.write() as c:
            b=await self.get(ident,actor,c)
            if b['guest_id']!=actor:raise RuleError('Оплатить может только гость этой брони.')
            clock=await c.fetchval('SELECT clock_timestamp()')
            if b['status']=='held' and b['release_at']>clock:return ident
            if b['status'] not in ('expired','held'):raise RuleError('Откройте актуальную карточку брони.')
            if b['rules_hash']!=rules_hash:raise RuleError('Правила обновились. Оформите бронь заново и ознакомьтесь с новой редакцией.')
            start=b['start_at'].astimezone(now().tzinfo)
            hours=int((b['end_at']-b['start_at']).total_seconds()/3600)
            window(start,hours,clock.astimezone(start.tzinfo),True)
            await self.available(c,start,b['cleanup_end'],ident)
            _,paid,_=await self.totals(ident,c)
            if paid:raise RuleError('Есть поступления. Свяжитесь с администратором для сверки.')
            await c.execute("UPDATE booking SET status='held',release_at=$2,claim_at=NULL,verification_due=NULL,revision=revision+1 WHERE id=$1",ident,clock+timedelta(minutes=20))
            await c.execute("UPDATE payment_link_request SET status='closed' WHERE booking_id=$1 AND status='pending'",ident)
            await self.audit(c,actor,'reopen_for_payment',ident)
            return ident

    async def service_basket(self,actor,ident,selection,operation):
        async with self.write() as c:
            await self._service_basket(c,actor,ident,selection,operation)

    async def finish_cart(self,actor,ident,token,submit=True):
        async with self.write() as c:
            session=await c.fetchrow('SELECT state,data FROM ui_session WHERE telegram_id=$1 FOR UPDATE',actor)
            d=json.loads(session['data']) if session else {}
            if not session or session['state']!='cart' or d.get('booking')!=ident or d.get('token')!=token:
                raise RuleError('Эта корзина уже обработана или устарела.')
            await self.get(ident,actor,c)
            if submit:await self._service_basket(c,actor,ident,d['cart'],f'basket:{actor}:{ident}:{token}')
            await c.execute("UPDATE ui_session SET state='',data='{}'::jsonb,updated_at=now() WHERE telegram_id=$1",actor)

    async def _service_basket(self,c,actor,ident,selection,operation):
        b=await self.get(ident,actor,c)
        if b['status'] not in ('held','payment_review','review_overdue','confirmed','in_progress'):raise RuleError('Заказ закрыт. Свяжитесь с администратором.')
        if not selection:raise RuleError('Выберите хотя бы одну услугу.')
        existing=await c.fetchval("SELECT EXISTS(SELECT 1 FROM booking_service WHERE operation_key LIKE $1)",operation+':%')
        if existing:return
        lines=[]
        for code,qty in selection.items():
            if not isinstance(qty,int) or not 1<=qty<=100:raise RuleError('Количество каждой услуги от 1 до 100.')
            item=await c.fetchrow('SELECT * FROM service_catalog WHERE code=$1 AND active',code)
            if not item:raise RuleError('Одна из услуг больше недоступна. Проверьте корзину.')
            await c.execute('INSERT INTO booking_service(id,booking_id,code,name,quantity,price,operation_key) VALUES($1,$2,$3,$4,$5,$6,$7)',uid(),ident,code,item['name'],qty,item['price'],operation+':'+code)
            lines.append(f'{item["name"]} × {qty}')
        await self.notify_staff(c,'Корзина услуг:\n'+'\n'.join(lines)+f'\n/order {ident}',f'basket:{operation}')
        await self.audit(c,actor,'request_service_basket',ident,selection)

    async def cart_adjust(self,actor,ident,code,token,delta):
        async with self.write() as c:
            await self.get(ident,actor,c)
            session=await c.fetchrow('SELECT state,data FROM ui_session WHERE telegram_id=$1 FOR UPDATE',actor)
            d=json.loads(session['data']) if session else {}
            if not session or session['state']!='cart' or d.get('booking')!=ident or d.get('token')!=token:
                raise RuleError('Эта корзина устарела. Откройте услуги заново.')
            if not await c.fetchval('SELECT active FROM service_catalog WHERE code=$1',code):raise RuleError('Услуга недоступна.')
            cart=d.setdefault('cart',{});quantity=cart.get(code,0)+delta
            if not 0<=quantity<=100:raise RuleError('Количество от 0 до 100.')
            if quantity:cart[code]=quantity
            else:cart.pop(code,None)
            await c.execute('UPDATE ui_session SET data=$2::jsonb,updated_at=now() WHERE telegram_id=$1',actor,json.dumps(d))

    async def get(self,ident,actor,c=None):
        c=c or self.pool
        r=await c.fetchrow('SELECT * FROM booking WHERE id=$1',ident)
        if not r or (r['guest_id']!=actor and not await self.role(actor,c)):
            raise RuleError('Бронь не найдена или недоступна.')
        return r

    async def quote(self,start,hours,guests,children,c=None):
        h=await (c or self.pool).fetchrow('SELECT base,extra FROM holiday WHERE day=$1',start.date())
        return price(start,hours,guests,children,(h['base'],h['extra']) if h else None)

    async def available(self,c,start,end,exclude=''):
        overlap=await c.fetchval('SELECT EXISTS(SELECT 1 FROM booking WHERE id<>$1 AND status=ANY($2::text[]) AND start_at<$4 AND cleanup_end>$3)',exclude,list(ACTIVE),start,end)
        block=await c.fetchval('SELECT EXISTS(SELECT 1 FROM calendar_block WHERE active AND start_at<$2 AND end_at>$1)',start,end)
        if overlap or block:
            raise RuleError('Это время уже занято, включая уборку. Выберите другой сеанс.')

    async def hold(self,actor,data,rules_hash,operation,manual=False):
        async with self.write() as c:
            return await self._hold(c,actor,data,rules_hash,operation,manual)

    async def _hold(self,c,actor,data,rules_hash,operation,manual=False,requested_long=False):
        from datetime import datetime
        start=datetime.fromisoformat(data['start'])
        clock=await c.fetchval('SELECT clock_timestamp()')
        clock=clock.astimezone(start.tzinfo)
        if manual:await self.staff(actor,c)
        old=await c.fetchval('SELECT id FROM booking WHERE operation_key=$1',operation)
        if old:return old
        if requested_long:window(start,2,clock)
        end,cleanup=window(start,data.get('hours',2),clock,manual or requested_long)
        rent,lines=await self.quote(start,data.get('hours',2),data['guests'],data['children'],c)
        if not manual and data.get('quote')!=rent:
            raise RuleError('Тариф изменился. Проверьте новый расчёт и подтвердите оформление заново.')
        if not data.get('consent'):
            raise RuleError('Сначала ознакомьтесь с правилами и отметьте согласие.')
        phone=normalize_phone(data['phone'])
        await self.available(c,start,cleanup)
        ident=uid()
        await c.execute('''INSERT INTO booking(id,operation_key,guest_id,name,phone,start_at,end_at,cleanup_end,status,guests,children,rent,price_lines,rules_hash,consent_at,release_at)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,'held',$9,$10,$11,$12::jsonb,$13,$14,$15)''',ident,operation,data.get('guest_id',actor),data['name'],phone,start,end,cleanup,data['guests'],data['children'],rent,json.dumps(lines),rules_hash,clock,clock+timedelta(minutes=20))
        await self.audit(c,actor,'create_hold',ident,{'manual':manual,'rent':rent})
        await self.notify_staff(c,f'Новый резерв {ident}\n{start:%d.%m %H:%M}, {data.get("hours",2)} ч.\n{data["name"]}, {data["phone"]}\nПроверка: /order {ident}',f'hold:{ident}')
        return ident

    async def totals(self,ident,c=None):
        c=c or self.pool
        rent=await c.fetchval('SELECT rent FROM booking WHERE id=$1',ident)
        services=await c.fetchval("SELECT COALESCE(sum(quantity*price),0)::bigint FROM booking_service WHERE booking_id=$1 AND status='approved'",ident)
        paid=await c.fetchval('SELECT COALESCE(sum(amount),0)::bigint FROM receipt WHERE booking_id=$1',ident)
        total=rent+services
        return total,paid,max(0,total-paid)

    async def claim(self,actor,ident,amount):
        async with self.write() as c:
            b=await self.get(ident,actor,c)
            if actor!=b['guest_id']:raise RuleError('Заявить оплату может только гость этого заказа.')
            clock=await c.fetchval('SELECT clock_timestamp()')
            claim_allowed(b['status'],b['release_at'],clock)
            pending=await c.fetchrow("SELECT id,amount FROM payment_claim WHERE booking_id=$1 AND status='pending'",ident)
            if pending:
                if pending['amount']==amount:return pending['id']
                raise RuleError('Предыдущая оплата ещё на проверке. Новая сумма не зарегистрирована: свяжитесь с администратором для сверки.')
            total,paid,left=await self.totals(ident,c)
            if not 0<amount<=left:raise RuleError('Сумма должна быть положительной и не больше остатка.')
            claim_id=uid()
            await c.execute('INSERT INTO payment_claim(id,booking_id,amount) VALUES($1,$2,$3)',claim_id,ident,amount)
            if b['status']=='held':
                await c.execute("UPDATE booking SET status='payment_review',claim_at=$2,verification_due=$3 WHERE id=$1",ident,clock,clock+timedelta(hours=3))
            await self.audit(c,actor,'claim_payment',claim_id,{'amount':amount})
            await self.notify_staff(c,f'Гость заявил оплату {rub(amount)}.\nБронь {ident}\nПроверьте поступление: /order {ident}',f'claim:{claim_id}')
            return claim_id

    async def decide_payment(self,actor,claim_id,approve):
        async with self.write() as c:
            await self.staff(actor,c)
            cl=await c.fetchrow('SELECT * FROM payment_claim WHERE id=$1',claim_id)
            if not cl:raise RuleError('Заявление не найдено.')
            if cl['status']!='pending':return cl['booking_id']
            b=await self.get(cl['booking_id'],actor,c)
            if b['status'] in ('expired','cancelled','rejected'):
                raise RuleError('Бронь закрыта. Поздний платёж разберите вручную без восстановления времени.')
            await c.execute('UPDATE payment_claim SET status=$2 WHERE id=$1',claim_id,'confirmed' if approve else 'rejected')
            if approve:
                if not b['duration_approved']:raise RuleError('Сначала подтвердите длительность аренды.')
                await c.execute('INSERT INTO receipt VALUES($1,$2,$3,$4,$5,now()) ON CONFLICT(claim_id) DO NOTHING',uid(),b['id'],claim_id,cl['amount'],actor)
                total,paid,left=await self.totals(b['id'],c)
                if paid>=200000 and b['status'] in ('held','payment_review','review_overdue'):
                    await c.execute("UPDATE booking SET status='confirmed' WHERE id=$1",b['id'])
                    b=await self.get(b['id'],actor,c)
                    await self.reminders(c,b)
                message=f'Подтверждено поступление {rub(cl["amount"])}. Остаток {rub(left)}.\nЗаказ {b["id"]}'
            else:
                # Payment rejection never silently frees a potentially paid reservation.
                message=f'Администратор не подтвердил заявленную оплату. Свяжитесь с ним для уточнения.\nЗаказ {b["id"]}'
            await self.queue(c,b['guest_id'],message,f'payment-decision:{claim_id}')
            await self.audit(c,actor,'payment_confirm' if approve else 'payment_reject',claim_id)
            return b['id']

    async def request_link(self,actor,ident):
        async with self.write() as c:
            return await self._request_link(c,actor,ident)

    async def _request_link(self,c,actor,ident):
        b=await self.get(ident,actor,c)
        if actor!=b['guest_id']:raise RuleError('Ссылку запрашивает гость этого заказа.')
        clock=await c.fetchval('SELECT clock_timestamp()')
        claim_allowed(b['status'],b['release_at'],clock)
        _,_,left=await self.totals(ident,c)
        pending_services=await c.fetchval("SELECT EXISTS(SELECT 1 FROM booking_service WHERE booking_id=$1 AND status='requested')",ident)
        pending_duration=await c.fetchval("SELECT EXISTS(SELECT 1 FROM duration_request WHERE booking_id=$1 AND status='pending')",ident)
        if not left and not pending_services and not pending_duration:raise RuleError('Заказ уже оплачен.')
        old=await c.fetchval("SELECT id FROM payment_link_request WHERE booking_id=$1 AND status='pending'",ident)
        if old:return old
        rid=uid()
        await c.execute('INSERT INTO payment_link_request(id,booking_id) VALUES($1,$2)',rid,ident)
        await self.notify_staff(c,f'Гость запросил ссылку для оплаты. Остаток {rub(left)}.\n/order {ident}\nОтправка: /paylink {rid} https://ссылка\nОжидание ссылки не продлевает резерв.',f'paylink-request:{rid}')
        await self.audit(c,actor,'request_payment_link',rid)
        return rid

    async def send_link(self,actor,rid,url):
        from urllib.parse import urlsplit
        parsed=urlsplit(url)
        if parsed.scheme!='https' or not parsed.netloc or parsed.username or parsed.password or len(url)>1500 or any(x.isspace() for x in url):
            raise RuleError('Введите полную HTTPS ссылку без пробелов, логина и пароля.')
        async with self.write() as c:
            await self.staff(actor,c)
            r=await c.fetchrow('SELECT * FROM payment_link_request WHERE id=$1',rid)
            if not r:raise RuleError('Запрос ссылки не найден.')
            if r['status']!='pending':raise RuleError('Этот запрос уже обработан.')
            b=await self.get(r['booking_id'],actor,c)
            if not b['duration_approved']:raise RuleError('Сначала подтвердите длительность аренды.')
            clock=await c.fetchval('SELECT clock_timestamp()')
            claim_allowed(b['status'],b['release_at'],clock)
            _,_,left=await self.totals(b['id'],c)
            if not left:raise RuleError('Заказ уже оплачен.')
            await c.execute("UPDATE payment_link_request SET status='sent',url=$2,actor=$3 WHERE id=$1",rid,url,actor)
            await self.queue(c,b['guest_id'],f'Администратор передал ссылку для оплаты заказа {b["id"]}:\n{url}\nПосле оплаты нажмите «Я оплатил» в карточке заказа. Сверьте получателя и сумму перед оплатой.',f'paylink:{rid}',b,'payment_link')
            await self.audit(c,actor,'send_payment_link',rid)
            return b['id']

    async def reminders(self,c,b):
        clock=await c.fetchval('SELECT clock_timestamp()')
        for hours in (24,1):
            due=b['start_at']-timedelta(hours=hours)
            if due>=clock:
                await self.queue(c,b['guest_id'],'',f'remind:{b["id"]}:{b["revision"]}:{hours}',b,f'remind_{hours}',due)

    async def service(self,actor,ident,code,qty,operation):
        async with self.write() as c:
            b=await self.get(ident,actor,c)
            if b['status'] not in ('held','payment_review','review_overdue','confirmed','in_progress'):
                raise RuleError('По закрытому заказу свяжитесь с администратором для заказа услуг.')
            if not 1<=qty<=100:raise RuleError('Количество от 1 до 100; доступность подтвердит администратор.')
            item=await c.fetchrow('SELECT * FROM service_catalog WHERE code=$1 AND active',code)
            if not item:raise RuleError('Услуга недоступна.')
            ident_service=uid()
            await c.execute('INSERT INTO booking_service(id,booking_id,code,name,quantity,price,operation_key) VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT(operation_key) DO NOTHING',ident_service,ident,code,item['name'],qty,item['price'],operation)
            await self.notify_staff(c,f'Заявка на услугу: {item["name"]} × {qty}.\n/order {ident}',f'service:{operation}')
            await self.audit(c,actor,'request_service',ident,{'code':code,'qty':qty})

    async def decide_service(self,actor,ident,approve):
        async with self.write() as c:
            await self.staff(actor,c)
            r=await c.fetchrow('SELECT * FROM booking_service WHERE id=$1',ident)
            if not r:raise RuleError('Услуга не найдена.')
            b=await self.get(r['booking_id'],actor,c)
            if r['status']!='requested':return b['id']
            if b['status'] in ('cancelled','expired','rejected','completed'):raise RuleError('Заказ закрыт.')
            await c.execute('UPDATE booking_service SET status=$2,actor=$3 WHERE id=$1',ident,'approved' if approve else 'rejected',actor)
            _,_,left=await self.totals(b['id'],c)
            await self.queue(c,b['guest_id'],f'Услуга {r["name"]} × {r["quantity"]}: '+('подтверждена' if approve else 'отклонена')+f'.\nОстаток {rub(left)}. /start → Мои бронирования',f'service-decision:{ident}')
            await self.audit(c,actor,'service_decision',ident,{'approved':approve})
            return b['id']

    async def change(self,actor,ident,kind,target=''):
        if kind not in ('cancel','move','no_show'):raise RuleError('Неверное действие.')
        async with self.write() as c:
            b=await self.get(ident,actor,c)
            if kind=='no_show':await self.staff(actor,c)
            if b['status'] not in ('held','confirmed','in_progress','payment_review','review_overdue'):
                raise RuleError('Для этой брони запрос недоступен.')
            old=await c.fetchval("SELECT id FROM change_request WHERE booking_id=$1 AND status='pending'",ident)
            if old:return old
            rid=uid()
            await c.execute('INSERT INTO change_request(id,booking_id,kind,target) VALUES($1,$2,$3,$4)',rid,ident,kind,target[:500])
            await self.notify_staff(c,f'Запрос {kind}, бронь {ident}.\n{target[:500]}\n/order {ident}',f'change:{rid}')
            await self.audit(c,actor,'request_'+kind,rid)
            return rid

    async def decide_change(self,actor,rid,approve,new_start=None):
        async with self.write() as c:
            await self.staff(actor,c)
            r=await c.fetchrow('SELECT * FROM change_request WHERE id=$1',rid)
            if not r:raise RuleError('Запрос не найден.')
            b=await self.get(r['booking_id'],actor,c)
            if r['status']!='pending':return b['id']
            if b['status'] in ('expired','rejected','cancelled'):
                raise RuleError('Заказ уже закрыт. Этот запрос нельзя применить к его прежнему состоянию.')
            if approve and r['kind']=='move':
                if not new_start:raise RuleError('Для переноса используйте /move ID_запроса ГГГГ-ММ-ДД ЧЧ:ММ.')
                hours=int((b['end_at']-b['start_at']).total_seconds()/3600)
                clock=await c.fetchval('SELECT clock_timestamp()')
                end,clean=window(new_start,hours,clock.astimezone(new_start.tzinfo),True)
                await self.available(c,new_start,clean,b['id'])
                rent,lines=await self.quote(new_start,hours,b['guests'],b['children'],c)
                await c.execute('UPDATE booking SET start_at=$2,end_at=$3,cleanup_end=$4,rent=$5,price_lines=$6::jsonb,revision=revision+1 WHERE id=$1',b['id'],new_start,end,clean,rent,json.dumps(lines))
                await self.reminders(c,await self.get(b['id'],actor,c))
                text=f'Перенос одобрен: {new_start:%d.%m.%Y %H:%M}. Финансовую разницу уточните у администратора.'
            elif approve and r['kind']=='cancel':
                await c.execute("UPDATE booking SET status='cancelled',revision=revision+1 WHERE id=$1",b['id'])
                await c.execute("UPDATE duration_request SET status='rejected' WHERE booking_id=$1 AND status='pending'",b['id'])
                await c.execute("UPDATE payment_link_request SET status='closed' WHERE booking_id=$1 AND status='pending'",b['id'])
                await c.execute("UPDATE booking_service SET status='rejected',actor=$2 WHERE booking_id=$1 AND status='requested'",b['id'],actor)
                _,paid,_=await self.totals(b['id'],c)
                late=(b['start_at']-r['created_at']).total_seconds()<10800
                if paid:
                    await c.execute('INSERT INTO refund(id,booking_id,request_id,amount,reason) VALUES($1,$2,$3,$4,$5) ON CONFLICT(request_id) DO NOTHING',uid(),b['id'],rid,paid,'Ручная проверка подтверждённых расходов' if late else 'Полный возврат')
                text='Отмена одобрена. Время освобождено. Возврат рассматривает администратор; автоматическая отправка денег пока не подключена.'
            elif approve and r['kind']=='no_show':
                text='Администратор получил отметку о неявке. Финансовое решение принимается отдельно.'
            else:text='Администратор отклонил запрос. Исходная бронь сохраняется.'
            await c.execute('UPDATE change_request SET status=$2,actor=$3 WHERE id=$1',rid,'approved' if approve else 'rejected',actor)
            await self.queue(c,b['guest_id'],text,f'change-decision:{rid}')
            await self.audit(c,actor,'decide_change',rid,{'approve':approve,'target':new_start.isoformat() if new_start else None})
            return b['id']

    async def reject_booking(self,actor,ident):
        async with self.write() as c:
            await self.staff(actor,c)
            b=await self.get(ident,actor,c)
            if b['status'] not in ('held','payment_review','review_overdue'):
                raise RuleError('Подтверждённую бронь отменяйте через запрос отмены.')
            _,paid,_=await self.totals(ident,c)
            if paid:raise RuleError('Есть подтверждённые деньги. Сначала рассмотрите отмену и возврат.')
            await c.execute("UPDATE booking SET status='rejected',revision=revision+1 WHERE id=$1",ident)
            await c.execute("UPDATE payment_claim SET status='rejected' WHERE booking_id=$1 AND status='pending'",ident)
            await self.queue(c,b['guest_id'],'Администратор отклонил бронь. Если деньги были отправлены, свяжитесь с ним для сверки и возврата.',f'reject:{ident}')
            await c.execute("UPDATE duration_request SET status='rejected' WHERE booking_id=$1 AND status='pending'",ident)
            await c.execute("UPDATE payment_link_request SET status='closed' WHERE booking_id=$1 AND status='pending'",ident)
            await c.execute("UPDATE booking_service SET status='rejected' WHERE booking_id=$1 AND status='requested'",ident)
            await self.audit(c,actor,'reject_booking',ident)

    async def maintenance(self):
        async with self.write() as c:
            clock=await c.fetchval('SELECT clock_timestamp()')
            for b in await c.fetch("UPDATE booking SET status='expired',revision=revision+1 WHERE status='held' AND release_at<=$1 RETURNING *",clock):
                await self.queue(c,b['guest_id'],'Резерв истёк через 20 минут без заявления оплаты. Нажмите «Заплатить сейчас»: проверим, свободно ли ещё это время, и запросим ссылку у администратора.',f'expired:{b["id"]}:{b["revision"]}',b,'expired')
                await c.execute("UPDATE duration_request SET status='rejected' WHERE booking_id=$1 AND status='pending'",b['id'])
                await c.execute("UPDATE payment_link_request SET status='closed' WHERE booking_id=$1 AND status='pending'",b['id'])
                await c.execute("UPDATE booking_service SET status='rejected' WHERE booking_id=$1 AND status='requested'",b['id'])
                await self.audit(c,0,'expire',b['id'])
            for b in await c.fetch("UPDATE booking SET status='review_overdue' WHERE status='payment_review' AND verification_due<=$1 RETURNING *",clock):
                await self.notify_staff(c,f'Просрочена проверка оплаты. Время остаётся занято.\n/order {b["id"]}',f'overdue:{b["id"]}')
                await self.queue(c,b['guest_id'],'Проверка оплаты задерживается. Резерв сохранён; свяжитесь с администратором.',f'overdue-guest:{b["id"]}')
            for b in await c.fetch("SELECT * FROM booking WHERE status IN ('payment_review','review_overdue') AND start_at<=$1",clock):
                await self.notify_staff(c,f'Начало сеанса при незавершённой проверке: /order {b["id"]}',f'start-review:{b["id"]}')
            await c.execute("UPDATE booking SET status='in_progress' WHERE status='confirmed' AND start_at<=$1 AND end_at>$1",clock)
            await c.execute("UPDATE booking SET status='completed' WHERE status IN ('confirmed','in_progress') AND end_at<=$1",clock)

    async def next_message(self):
        async with self.pool.acquire() as c:
            async with c.transaction():
                row=await c.fetchrow('''SELECT * FROM outbox WHERE sent_at IS NULL AND attempts<8 AND due_at<=now()
                    AND (locked_until IS NULL OR locked_until<now()) ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1''')
                if row:
                    await c.execute("UPDATE outbox SET locked_until=now()+interval '2 minutes',attempts=attempts+1 WHERE id=$1",row['id'])
                return row

    async def outgoing_text(self,row):
        if not row['kind']:return row['text']
        b=await self.pool.fetchrow('SELECT * FROM booking WHERE id=$1',row['booking_id'])
        if row['kind']=='expired':
            return row['text'] if b and b['status']=='expired' and b['revision']==row['revision'] else None
        if row['kind']=='payment_link':
            if not b or b['revision']!=row['revision'] or b['status'] not in ('held','payment_review','review_overdue','confirmed','in_progress') or (b['status']=='held' and b['release_at']<=now()):
                return None
            return row['text']
        if not b or b['revision']!=row['revision'] or b['status'] not in ('confirmed','in_progress'):
            return None
        total,paid,left=await self.totals(b['id'])
        start=b['start_at'].astimezone(now().tzinfo)
        return f'Напоминание: баня «Пихта» {start:%d.%m.%Y %H:%M}.\nЗаказ {b["id"]}\nОстаток {rub(left)}.\nДобавить услуги и доплатить: /start → Мои бронирования.'

    async def sent(self,ident,error=None):
        if error:
            await self.pool.execute("UPDATE outbox SET locked_until=NULL,last_error=$2,due_at=now()+interval '5 minutes' WHERE id=$1",ident,error[:80])
        else:await self.pool.execute('UPDATE outbox SET sent_at=now(),locked_until=NULL WHERE id=$1',ident)
