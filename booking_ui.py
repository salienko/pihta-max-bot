"""Native Telegram rental booking: one journey, short screens, durable drafts."""
import asyncio
import calendar
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from transport_v3 import Router
from transport_v3 import (ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton,
                           InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, InputMediaPhoto, TelegramBadRequest)
from booking_domain import (TZ, SLOTS, LABELS, RuleError, estimate, card, can_cancel,
                            validate_start, clean_name, normalize_phone, horizon, rub)
from booking_flow import VERSION, prepare, require, first_missing, change_guests, callback, collected_fields, accept_field
from booking_payments import total as payment_total


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=text, **({'url': value} if value.startswith('https://') else {'callback_data': value}))
        for text, value in row] for row in rows])


def nav(): return [[('🏠 Главное меню', 'home')]]


class BookingUI:
    def __init__(self, settings, store, features=None):
        self.settings, self.store = settings, store
        self.features=features
        self.router = Router()
        self.router.message.register(self.message)
        self.router.callback_query.register(self.callback)
        self.locks = defaultdict(asyncio.Lock)

    async def say(self, m, text, rows=None, photo=None, edit=False, reply_markup=None):
        markup = reply_markup if reply_markup is not None else kb(rows if rows is not None else nav())
        if reply_markup is not None:edit=False
        file = Path(__file__).with_name('assets') / f'{photo}.jpg' if photo else None
        if edit and len(text) <= 1000:
            try:
                if m.photo and edit=='caption':
                    await m.edit_caption(caption=text,reply_markup=markup)
                elif m.photo and file and file.exists():
                    await m.edit_media(InputMediaPhoto(media=FSInputFile(file), caption=text), reply_markup=markup)
                elif m.photo:
                    await m.edit_caption(caption=text, reply_markup=markup)
                elif not file:
                    await m.edit_text(text, reply_markup=markup)
                else:
                    raise TelegramBadRequest(method=None, message='message to edit not found')
                return
            except TelegramBadRequest as error:
                message = str(error).lower()
                if 'message is not modified' in message: return
                if not any(s in message for s in ('message to edit not found', "message can't be edited", 'message is too long')):
                    raise
        if file and file.exists() and len(text) <= 1000:
            await m.answer_photo(FSInputFile(file), caption=text, reply_markup=markup)
            return
        for pos in range(0, len(text), 3500):
            await m.answer(text[pos:pos+3500], reply_markup=markup if pos+3500 >= len(text) else None)

    async def unfinished(self, actor):
        try: return await self.store.draft(actor)
        except RuleError: return None

    async def leave_contact(self, m, actor):
        d = await self.unfinished(actor)
        if d and d['state'] in ('phone_contact', 'profile_contact'):
            await self.store.save(actor, d, 'phone' if d['state']=='phone_contact' else 'profile_phone', d['data'])
            await m.answer('Выбор номера закрыт.', reply_markup=ReplyKeyboardRemove())

    async def phone_screen(self,m,actor,d,text,profile=False,edit=False):
        if edit and hasattr(m,'edit_reply_markup'):
            try:await m.edit_reply_markup(reply_markup=None)
            except TelegramBadRequest:pass
        if not profile and d['data'].get('phone'):
            if d['state']=='phone_contact':
                await self.leave_contact(m,actor)
                d=await self.store.draft(actor,d['token'])
            b=lambda action,value='':callback(action,d,value)
            await self.say(m,text,[[('📱 Использовать мой номер',b('keep','phone'))],[('← Назад',b('back','name'))],[('✖ Отменить оформление',b('discardask'))]]+nav(),photo='booking',edit=edit)
            return
        target='profile_contact' if profile else 'phone_contact'
        if d['state']!=target:await self.store.save(actor,d,target,d['data'])
        rows=[[KeyboardButton(text='📱 Поделиться номером',request_contact=True)]]
        rows.append([KeyboardButton(text='← Назад')])
        if not profile:rows.append([KeyboardButton(text='✖ Отменить оформление')])
        rows.append([KeyboardButton(text='🏠 Главное меню')])
        keyboard=ReplyKeyboardMarkup(keyboard=rows,resize_keyboard=True,one_time_keyboard=True)
        await self.say(m,text,[],photo='booking',reply_markup=keyboard)

    async def home(self, m, actor=None, edit=False):
        actor = actor or m.from_user.id
        await self.leave_contact(m, actor)
        await self.cancel_profile(actor)
        d = await self.unfinished(actor)
        rows = [[('📅 Забронировать', 'begin')], [('📋 Мои бронирования', 'list:0:0')],
                [('💰 Цены', 'info:Цены'), ('📖 Правила', 'info:Правила')],
                [('📍 Контакты', 'info:Контакты'), ('❓ Помощь', 'info:Помощь')]]
        if self.features:rows.extend(self.features.menu())
        if d:
            rows.insert(0, [('✏️ Продолжить бронирование', 'resume')])
        if actor in self.settings.admin_ids: rows.append([('🔐 Панель администратора', 'staffweb')])
        await self.say(m, 'ПИХТА · баня на дровах\n\nБаня целиком для вашей компании.\nВыберите действие.', rows, photo='home', edit=edit)

    async def info(self, m, actor, section, back=None, edit=False):
        c = self.settings.content
        rows = []
        if back: rows.append([('← Назад', back)])
        if section == 'Контакты':
            text = f'📍 Как нас найти\n\n{c["name"]}\nПосёлок Приладожское\n\n☎️ {c["phone"]}\nНестандартное время — по согласованию с администратором.'
            rows = [[('📍 Построить маршрут', c['map_url'])]] + rows
        elif section == 'Правила':
            text = ('📖 Правила бронирования\n\nДо 8 гостей вместе с детьми.\nДети до 7 лет включительно — без доплаты.\nПростыня на каждого гостя включена.\n\n'
                    'Для подтверждения внесите предоплату 2 000 ₽ или всю стоимость. Остаток — до начала сеанса.\n\nЗаявка занимает время и ждёт решения администратора.\n'
                    'Неоплаченную ожидающую заявку можно отменить самостоятельно. Подтверждённую или оплаченную отменяет администратор по вашему запросу.\n\n'
                    'Отправленную бронь нельзя изменить: отмените её и создайте новую. Опоздание не продлевает сеанс.\n\n'
                    'Не более двух действующих броней на разные даты. Запись за час — два месяца до сеанса.')
            rows.insert(0,[('📄 Полные правила','rulesfull')])
        elif section in ('Цены', 'Цены и услуги'):
            text = ('💰 Аренда бани · 2 часа\n\nПн–пт до 19:00 — 5 000 ₽\nС 19:00 и в выходные — 6 000 ₽\nПраздничные даты — 6 500 ₽*\n\n'
                    'Включены 4 гостя от 8 лет.\nЗа каждого следующего — 250 ₽/час.\nДети до 7 лет включительно — бесплатно.\n\n'
                    'Точная сумма будет перед отправкой.\n*Особые даты и нестандартная длительность — через администратора.')
            rows = [[('📅 Забронировать', 'begin')]] + rows
        else:
            text = ('❓ Как записаться\n\nИмя → телефон → гости → дата → время.\nПроверьте карточку и отправьте её администратору.\n\n'
                    'Черновик сохраняется: можно вернуться позже.\nВ «Моих бронированиях» — статус, детали и отмена.\n\n'
                    'Нужно другое время или помощь? Свяжитесь с администратором.')
            rows.append([('☎️ Администратор', 'contact')])
        await self.say(m, text, rows + nav(), photo='home', edit=edit)

    async def start(self, m, actor, edit=False):
        await self.leave_contact(m, actor)
        d = await self.unfinished(actor)
        if d:
            await self.say(m, '✏️ У вас есть незавершённое бронирование.\n\nПродолжить с сохранённого шага или начать заново?',
                           [[('Продолжить', 'resume')], [('Начать заново', callback('resetask', d))]] + nav(), edit=edit)
            return
        if len(await self.store.listing(actor)) >= 2:
            await self.say(m, 'У вас уже две действующие брони.\nОткройте их, чтобы посмотреть детали или отменить.', [[('📋 Мои бронирования', 'list:0:0')]]+nav(), edit=edit)
            return
        token = await self.store.begin(actor)
        await self.render(m, actor, await self.store.draft(actor, token), edit=edit)

    async def render(self, m, actor, d, edit=False):
        state, data = prepare(d['state'], d['data'])
        if state != d['state'] or data != d['data']:
            await self.store.save(actor, d, state, data)
            d = await self.store.draft(actor, d['token'])
        state, data = d['state'], d['data']
        if '_filled' not in data:
            data['_filled']=collected_fields(state,data)
            await self.store.save(actor,d,state,data)
            d=await self.store.draft(actor,d['token'])
        if state in ('phone_contact','profile_contact'):
            state='phone' if state=='phone_contact' else 'profile_phone'
        b = lambda action, value='': callback(action, d, value)
        if state.startswith('profile_'):
            key='name' if state=='profile_name' else 'phone'
            text='👤 Мои данные\n\n'+('Введите новое имя.' if key=='name' else 'Введите телефон для будущих бронирований.\nНапример: +7 981 244-00-50')
            rows=[]
            if key=='phone':
                await self.phone_screen(m,actor,d,'👤 Мои данные\n\nУкажите телефон для будущих бронирований.',profile=True,edit=edit);return
            rows.append([('← Назад','profilecancel')])
            await self.say(m,text,rows+nav(),edit=edit)
            return
        try: require(data, state)
        except RuleError:
            state=first_missing(data)
            data['consent']=False
            await self.store.save(actor,d,state,data)
            d=await self.store.draft(actor,d['token'])
            b=lambda action,value='':callback(action,d,value)
        heading=''
        footer=[[('✖ Отменить оформление',b('discardask'))]]+nav()
        if state=='name':
            await self.say(m,'👤 Как к Вам обращаться?\n\nНапишите Ваше имя ↓',nav(),photo='booking',edit=edit)
        elif state=='phone':
            await self.phone_screen(m,actor,d,'📱 Как с вами связаться?\n\nУкажите ваш номер телефона ↓',edit=edit)
        elif state=='guests':
            paid=data['guests']-data['children'];kids=data['children']
            def count_button(category,delta,enabled):return ('−' if delta<0 else '+',b('guest',category+('+' if delta>0 else '-')) if enabled else b('limit','min' if delta<0 else 'max'))
            rows=[[('Гости старше 7 лет',b('noop'))],
                  [count_button('paid',-1,paid>0 and data['guests']>1),(f'{paid} чел.',b('noop')),count_button('paid',1,data['guests']<8)],
                  [('Гости младше 7 лет',b('noop'))],
                  [count_button('kid',-1,kids>0 and data['guests']>1),(f'{kids} чел.',b('noop')),count_button('kid',1,data['guests']<8)],
                  [('📅 Выбрать дату',b('next','date'))],[('← Назад',b('back','phone'))]]
            text=f'👥 Уточните какое кол-во гостей планирует отдыхать?\n\nВсего гостей: {data["guests"]} чел.\n\nУкажите кол-во гостей ↓'
            await self.say(m,text,rows+footer,photo='guests',edit=edit)
        elif state=='date':
            today=datetime.now(TZ).date();last=horizon(today)
            month=date.fromisoformat(data.get('month',today.replace(day=1).isoformat()))
            if month<today.replace(day=1) or month>last.replace(day=1):month=today.replace(day=1)
            months=('Январь','Февраль','Март','Апрель','Май','Июнь','Июль','Август','Сентябрь','Октябрь','Ноябрь','Декабрь')
            rows=[[(f'{months[month.month-1]} {month.year}',b('noop'))],[(day,b('noop')) for day in ('Пн','Вт','Ср','Чт','Пт','Сб','Вс')]]
            active=await self.store.listing(actor)
            occupied={r['starts_at'].astimezone(TZ).date() for r in active}
            weeks=calendar.Calendar().monthdatescalendar(month.year,month.month)
            available_days=await self.store.available_days([day for week in weeks for day in week if day.month==month.month and today<=day<=last])
            for week in weeks:
                rows.append([(str(day.day),b('day',day.isoformat())) if day.month==month.month and today<=day<=last and day not in occupied and day in available_days else ('·',b('noop')) for day in week])
            target=(month+timedelta(days=32)).replace(day=1)
            if target>last.replace(day=1):target=today.replace(day=1)
            if target!=month:rows.append([('Месяц >',b('month',target.isoformat()))])
            rows += [[('← Назад',b('back','guests'))]]
            await self.say(m,'📅 Выберите удобную дату\n\nВыберите день в календаре ↓',rows+footer,photo='date',edit=edit)
        elif state=='slot':
            day=date.fromisoformat(data['day']);rows=[]
            for hour in SLOTS:
                start=datetime.combine(day,datetime.min.time(),TZ)+timedelta(hours=hour)
                try: available=await self.store.free(start)
                except RuleError:continue
                if available:rows.append([(f'{hour:02}:00 - {(hour+2)%24:02}:00',b('slot',str(hour)))])
            text='🕒 Выберите удобное время' if rows else 'Свободных сеансов нет.\nВыберите другую дату.'
            rows += [[('← Назад',b('back','date'))],[('💬 Другое время','contact')]]
            await self.say(m,heading+text,rows+footer,photo='time',edit=edit)
        elif state=='review':
            catalog,holidays=await self.store.pricing()
            try:
                validate_start(datetime.fromisoformat(data['start']),datetime.now(TZ))
                if not await self.store.free(datetime.fromisoformat(data['start'])):raise RuleError('Время уже занято. Выберите другое.')
                q=estimate(data,catalog,holidays)
            except RuleError as error:
                await self.store.save(actor,d,'slot',dict(data,consent=False))
                d=await self.store.draft(actor,d['token'])
                await self.say(m,str(error),[[('🕒 Выбрать время',callback('resume_step',d))],[('☎️ Администратор','contact')]]+nav(),edit=edit)
                return
            if data.get('quote')!=q or data.get('rules_hash')!=self.settings.rules_hash:data['consent']=False
            data.update(quote=q,rules_hash=self.settings.rules_hash,cart={})
            await self.store.save(actor,d,'review',data)
            d=await self.store.draft(actor,d['token']);b=lambda action,value='':callback(action,d,value)
            start=datetime.fromisoformat(data['start']);end=start+timedelta(hours=2)
            text=(f'📋 Проверьте, все ли верно?\n\n👤 {data["name"]}\n📱 {data["phone"]}\n'
                  f'👥 Кол-во гостей: {data["guests"]} чел.\nСтарше 7 лет {data["guests"]-data["children"]} чел\n'
                  f'Младше 7 лет {data["children"]} чел\n📅 Дата: {start:%d.%m.%Y}\n🕒 Время {start:%H:%M}–{end:%H:%M}\n\n'
                  f'Общая стоимость составит {rub(q["total"])}\n\nНажмите для подтверждения ↓\n\n'
                  'Подтверждая бронирование, вы соглашаетесь с правилами.')
            rows=[[('✅ Подтвердить бронирование',b('submit'))],[('← Назад',b('back','slot'))]]
            await self.say(m,text,rows+footer,photo='booking',edit=edit)

    async def listing(self,m,actor,history=False,offset=0,edit=False):
        await self.leave_contact(m,actor)
        await self.cancel_profile(actor)
        rows=await self.store.listing(actor,history,offset=offset);buttons=[]
        for r in rows[:20]:buttons.append([(f'{r["starts_at"].astimezone(TZ):%d.%m · %H:%M} · {LABELS[r["status"]]}','open:'+r['id'])])
        if len(rows)>20:buttons.append([('Далее →',f'list:{int(history)}:{offset+20}')])
        if offset:buttons.append([('← Назад',f'list:{int(history)}:{max(0,offset-20)}')])
        if history and not offset:
            for r in await self.store.legacy(actor):buttons.append([('Прежняя заявка № '+r['id'],'legacy:'+r['id'])])
        buttons += [[('📋 Действующие','list:0:0'),('🗂 История','list:1:0')],[('👤 Мои данные','profile')],[('📅 Забронировать','begin')]]
        await self.say(m,('🗂 История бронирований' if history else '📋 Мои бронирования')+('\n\nЗдесь пока пусто.' if not rows else '\n\nВыберите бронирование.'),buttons+nav(),photo='booking',edit=edit)

    async def show(self,m,actor,ident,edit=False):
        r=await self.store.get(actor,ident);rows=[]
        if r['status'] in ('pending','confirmed') and r['paid']<payment_total(r):
            if not r['paid']:
                rows += [[('💳 Внести предоплату 2 000 ₽','payrequest:deposit:'+ident)],
                         [('💳 Оплатить полностью — '+rub(payment_total(r)),'payrequest:full:'+ident)]]
            else:rows.append([('💳 Оплатить — '+rub(payment_total(r)-r['paid']),'payrequest:balance:'+ident)])
            requests=await self.store.payment_requests(actor,ident)
            linked=next((req for req in requests if req['url']),None)
            if linked:
                automatic=bool(await self.store.pool.fetchval('SELECT 1 FROM paykeeper_invoices WHERE request_id=$1',linked['id']))
                check_label='Проверить оплату '+rub(linked['amount']) if automatic else '✅ Уже оплатил'
                check_action=('paycheck:' if automatic else 'payreport:')+linked['id']
                if automatic:
                    index=1 if linked['kind']=='full' and not r['paid'] else 0
                    rows[index]=[(check_label,check_action)]
                    rows.append([('🔗 Перейти к оплате',linked['url'])])
                else:
                    rows += [[('🔗 Перейти к оплате',linked['url'])],[(check_label,check_action)]]
        if self.settings.paykeeper_enabled:
            rows.append([('Платежи и чеки','payhistory:'+ident)])
        if r['status'] in ('pending','confirmed'):rows.append([('Отменить бронирование','cancelask:'+ident)])
        rows += [[('☎️ Администратор','contact')],[('← Назад','list:0:0' if r['status'] in ('pending','confirmed') else 'list:1:0')]]
        await self.say(m,card(r),rows+nav(),photo='booking',edit=edit)

    async def admin_link(self,m,actor):
        self.store.admin(actor)
        if not self.settings.web_url:await self.say(m,'Панель пока недоступна.');return
        token=await self.store.login_token(actor)
        await self.say(m,'🔐 Панель администратора\n\nЛичная ссылка действует 5 минут.\nНе пересылайте её.',[[('Открыть панель',self.settings.web_url+'/login#'+token)]]+nav())

    async def cancel_profile(self,actor):
        d=await self.unfinished(actor)
        if d and d['state'].startswith('profile_'):
            data=dict(d['data']);previous=data.pop('profile_return','name');data.pop('profile_name',None)
            await self.store.save(actor,d,previous,data)

    async def profile(self,m,actor,edit=False):
        p=await self.store.profile(actor)
        await self.say(m,('👤 Мои данные\n\n'+p['name']+'\n'+p['phone'] if p else '👤 Мои данные\n\nПока не сохранены.')+'\n\nТолько для будущих бронирований.',[[('✏️ Изменить данные','profileedit')],[('← Назад','list:0:0')]]+nav(),edit=edit)

    async def message(self,m):
        if m.chat.type!='private':return
        actor=m.from_user.id
        async with self.locks[actor]:
            await self._message(m,actor)

    async def _message(self,m,actor):
        text=(m.text or '').strip()
        try:
            reply=getattr(m,'reply_to_message',None)
            if actor in self.settings.admin_ids and reply:
                known=await self.store.pool.fetchval('SELECT request_id FROM payment_messages WHERE chat_id=$1 AND message_id=$2',actor,reply.message_id)
                if known:
                    url=text
                    for entity in getattr(m,'entities',None) or []:
                        if str(entity.type)=='text_link' and entity.url:url=entity.url;break
                    await self.store.link_payment(actor,reply.message_id,url)
                    await m.answer('🔗 Ссылка передана гостю. После проверки поступления нажмите кнопку подтверждения в запросе.');return
            if text.startswith('/start'):
                await m.answer('Главное меню',reply_markup=ReplyKeyboardRemove())
                await self.home(m,actor);return
            if text in ('/menu','Главное меню','🏠 Главное меню'):
                await m.answer('Главное меню',reply_markup=ReplyKeyboardRemove())
                await self.home(m,actor);return
            if text=='/id':await m.answer(str(actor));return
            if text=='/admin':await self.admin_link(m,actor);return
            if text in ('/book','Забронировать'):await self.start(m,actor);return
            if text in ('/bookings','Мои бронирования'):await self.listing(m,actor);return
            if text in ('/help','Правила','Контакты','Цены','Цены и услуги','Помощь'):
                await self.leave_contact(m,actor);await self.info(m,actor,'Помощь' if text=='/help' else text);return
            d=await self.unfinished(actor)
            if not d:
                await self.home(m,actor);return
            state,data=d['state'],dict(d['data'])
            if data.get('flow_version')!=VERSION:
                await self.render(m,actor,d);return
            if '_filled' not in data:data['_filled']=collected_fields(state,data)
            profile=state.startswith('profile_')
            if state in ('phone_contact','profile_contact') and text in ('← Назад','✕ Отменить оформление','✖ Отменить оформление'):
                await self.leave_contact(m,actor)
                d=await self.store.draft(actor,d['token']);data=dict(d['data'])
                if text in ('✕ Отменить оформление','✖ Отменить оформление'):
                    await self.say(m,'Отменить оформление?\n\nОтправленные бронирования сохранятся.',[[('Да, отменить оформление',callback('discard',d))],[('← Назад','resume')]]+nav());return
                previous='profile_name' if profile else 'review' if data.pop('editing',False) and data.get('start') else 'name'
                await self.store.save(actor,d,previous,data)
                await self.render(m,actor,await self.store.draft(actor,d['token']));return
            if state in ('phone','phone_contact','profile_phone','profile_contact'):
                contact=getattr(m,'contact',None)
                if contact:
                    if contact.user_id!=actor:raise RuleError('Поделитесь своим номером или введите телефон вручную.')
                    phone=normalize_phone(contact.phone_number)
                elif text=='✓ Использовать '+data.get('phone',''):phone=normalize_phone(data['phone'])
                else:phone=normalize_phone(text)
                if state.endswith('contact'):await m.answer('Номер получен.',reply_markup=ReplyKeyboardRemove())
                if profile:
                    await self.store.profile(actor,data['profile_name'],phone)
                    previous=data.pop('profile_return','name');data.pop('profile_name',None)
                    await self.store.save(actor,d,previous,data)
                    await self.profile(m,actor);return
                data['phone']=phone
                accept_field(data,'phone')
                state='review' if data.pop('editing',False) and data.get('start') else 'guests'
            elif state in ('name','profile_name'):
                name=clean_name(text)
                if profile:data['profile_name']=name;state='profile_phone'
                else:
                    data['name']=name
                    accept_field(data,'name')
                    state='review' if data.pop('editing',False) and data.get('start') else 'phone'
            else:
                await self.say(m,'На этом шаге выберите действие кнопкой.',[[('Продолжить','resume')]]+nav());return
            data['consent']=False
            await self.store.save(actor,d,state,data)
            await self.render(m,actor,await self.store.draft(actor,d['token']))
        except RuleError as error:
            await self.say(m,str(error),[[('Продолжить','resume')]]+nav())

    async def callback(self,c):
        if not c.message or c.message.chat.type!='private':return
        await c.answer()
        async with self.locks[c.from_user.id]:
            await self._callback(c)

    async def _callback(self,c):
        actor,m=c.from_user.id,c.message
        parts=(c.data or '').split(':');action=parts[0]
        try:
            if action=='payrequest':
                req,created=await self.store.request_payment(actor,parts[2],parts[1])
                await self.show(m,actor,parts[2],edit=True)
                await m.answer(('Готовим ссылку на оплату. Она придёт отдельным сообщением.' if created else 'Запрос уже сохранён. Используйте действующую ссылку или дождитесь её получения.') if self.settings.paykeeper_enabled else ('Запрос ссылки передан администратору.' if created else 'Запрос уже передан администратору. Ожидайте ссылку.'));return
            if action=='paycheck':
                status=await self.store.check_paykeeper(actor,parts[1])
                ident=await self.store.pool.fetchval('SELECT booking_id FROM paykeeper_invoices WHERE request_id=$1',parts[1])
                await self.show(m,actor,ident,edit=True)
                from booking_domain import payment_confirmation
                current=await self.store.get(actor,ident)
                message=payment_confirmation(current) if status=='paid' else 'Срок счёта истёк. Получите новую ссылку; бронь сохранена.' if status=='expired' else 'Мы не видим вашей оплаты. Повторите позже.'
                if status=='paid' and not current['paid']:message='Ваша оплата принята. Спасибо. Администратор проверит её привязку к брони. Повторно оплачивать не нужно.'
                await m.answer(message);return
            if action=='payhistory':
                payments=await self.store.payment_history(actor,parts[1]);rows=[];text='Платежи и чеки'
                for p in payments:
                    text+='\n\n'+p['created_at'].astimezone(TZ).strftime('%d.%m.%Y %H:%M')+' · '+rub(p['amount'])
                    if p['receipt_url']:rows.append([('Открыть чек · '+rub(p['amount']),p['receipt_url'])])
                    else:text+='\nЧек формируется.'
                    if p['disposition']=='review':text+='\nПлатёж требует сверки администратором.'
                if not payments:text+='\nПоступивших платежей пока нет.'
                final=await self.store.pool.fetchrow('SELECT state,url FROM paykeeper_settlements WHERE booking_id=$1',parts[1])
                if final:
                    text+='\n\nИтоговый чек на оказанную услугу. Повторная оплата не требуется.'
                    if final['state']=='success':rows.append([('Открыть итоговый чек',final['url'])])
                    else:text+='\nЧек формируется или проверяется администратором.'
                await self.say(m,text,rows+[[('← Назад','open:'+parts[1])]]+nav());return
            if action=='payreport':
                ident,created=await self.store.report_payment(actor,parts[1])
                await self.show(m,actor,ident,edit=True)
                await m.answer('Администратор получил ваше обращение и скоро ответит.');return
            if action=='paystaff':
                if parts[1] not in ('yes','no'):raise RuleError('Неизвестное действие.')
                ident,changed=await self.store.decide_payment(actor,parts[2],parts[1]=='yes')
                await m.answer(('✅ Поступление денег подтверждено.' if parts[1]=='yes' else 'Гостю передано: поступление пока не найдено.') if changed else 'Это решение уже учтено.')
                await self.say(m,card(await self.store.get(actor,ident,staff=True)),[[('🔐 Веб-панель','staffweb')]]);return
            if action=='home':await self.home(m,actor,edit=True);return
            if action=='begin':await self.start(m,actor,edit=True);return
            if action=='staffweb':await self.admin_link(m,actor);return
            if action=='contact':
                await self.leave_contact(m,actor)
                await self.say(m,'☎️ Администратор\n\n'+self.settings.content['phone']+'\n\nПозвоните по этому номеру.\nДля нестандартного времени согласуйте длительность заранее.',[[('📍 Маршрут',self.settings.content['map_url'])],[('← Назад','resume' if await self.unfinished(actor) else 'home')]]+nav(),edit=True);return
            if action=='rulesfull':
                await self.say(m,self.settings.rules,[[('← Назад','resume')] if await self.unfinished(actor) else [('← Назад','home')]]+nav());return
            if action=='info':
                await self.leave_contact(m,actor);await self.info(m,actor,parts[1],edit=True);return
            if action=='list':await self.listing(m,actor,bool(int(parts[1])),int(parts[2]),edit=True);return
            if action=='open':await self.leave_contact(m,actor);await self.show(m,actor,parts[1],edit=True);return
            if action=='legacy':
                r=await self.store.legacy(actor,parts[1])
                if not r:raise RuleError('Запись недоступна.')
                data=r['data'];text=f'Прежняя заявка № {r["id"]}\n{data.get("start", "")}\nГостей: {data.get("guests", "")}\n{data.get("name", "")}\n{data.get("phone", "")}\n\nАрхив прежней версии. Статус уточните у администратора.'
                await self.say(m,text,[[('← Назад','list:1:0')]]+nav(),edit=True);return
            if action=='cancelask':
                r=await self.store.get(actor,parts[1])
                if r['status']=='confirmed' or r['paid']:
                    await self.store.request_cancellation(actor,parts[1])
                    await m.answer('Ваш запрос передан администратору.')
                    await self.show(m,actor,parts[1],edit=True);return
                if not can_cancel(r['status'],r['starts_at'],datetime.now(TZ)):raise RuleError('Самостоятельная отмена недоступна. Свяжитесь с администратором.')
                await self.say(m,'Отменить бронирование?\n\n'+card(r)+'\n\nОсвободившееся время могут занять другие гости.',[[('Да, отменить','cancel:'+parts[1])],[('← Назад','open:'+parts[1])]]+nav(),edit=True);return
            if action=='cancel':
                r=await self.store.get(actor,parts[1])
                if r['status']=='confirmed' or r['paid']:
                    await self.store.request_cancellation(actor,parts[1]);await m.answer('Ваш запрос передан администратору.')
                else:await self.store.transition(actor,parts[1],'cancel')
                await self.show(m,actor,parts[1],edit=True);return
            if action=='profile':await self.leave_contact(m,actor);await self.profile(m,actor,edit=True);return
            if action=='profileedit':
                token=await self.store.begin(actor);d=await self.store.draft(actor,token)
                if not d['state'].startswith('profile_'):
                    data=dict(d['data'],profile_return=d['state'])
                    await self.store.save(actor,d,'profile_name',data)
                await self.render(m,actor,await self.store.draft(actor,token),edit=True);return
            if action=='profilecancel':
                d=await self.store.draft(actor)
                if d['state'].startswith('profile_'):
                    data=dict(d['data']);previous=data.pop('profile_return','name');data.pop('profile_name',None)
                    await self.store.save(actor,d,previous,data)
                    await m.answer('Изменение отменено.',reply_markup=ReplyKeyboardRemove())
                await self.profile(m,actor,edit=True);return
            if action=='resume':
                await self.leave_contact(m,actor)
                d=await self.unfinished(actor)
                if d:await self.render(m,actor,d,edit=True)
                else:await self.start(m,actor,edit=True)
                return
            if len(parts)!=4:raise RuleError('Это кнопка прежнего экрана. Откройте текущее бронирование.')
            action,token,revision,value=parts
            if action=='submit':
                ident,created=await self.store.submit(actor,token,expected_revision=int(revision),accept_rules=True)
                await self.say(m,'✅ Заявка отправлена\n\nВремя занято за вами.\nДля подтверждения внесите предоплату 2 000 ₽ или всю стоимость.\nОткройте бронирование и запросите ссылку для оплаты.' if created else 'Эта заявка уже отправлена.',[[('📋 Открыть бронирование','open:'+ident)]]+nav(),photo='booking',edit=True);return
            d=await self.store.draft(actor,token)
            if d['revision']!=int(revision):
                await self.say(m,'Экран уже изменился.\nПродолжите с актуального шага.',[[('Продолжить','resume')]]+nav());return
            state,data=d['state'],dict(d['data']);new=state
            if action in ('noop','limit'):
                if action=='limit':await self.say(m,'Вместимость — до 8 гостей вместе с детьми.' if value=='max' else 'Укажите хотя бы одного гостя.',[[('← Назад','resume')]]+nav())
                return
            if action in ('discardask','resetask','discard','reset') and state in ('phone_contact','profile_contact'):
                await self.leave_contact(m,actor);d=await self.store.draft(actor,token);data=dict(d['data'])
            if action=='discardask':
                await self.say(m,'Отменить оформление?\n\nОтправленные бронирования сохранятся.',[[('Да, отменить оформление',callback('discard',d))],[('← Назад','resume')]]+nav(),edit=True);return
            if action=='discard':
                await self.store.abandon_draft(actor,d)
                await self.home(m,actor,edit=True);return
            if action=='resetask':
                await self.say(m,'Начать заново?\n\nТолько незавершённый черновик будет сброшен. Отправленные брони сохранятся.',[[('Начать заново',callback('reset',d))],[('← Назад', 'resume')]]+nav(),edit=True);return
            if action=='reset':
                await self.store.reset_draft(actor,d)
                await self.render(m,actor,await self.store.draft(actor),edit=True);return
            if action=='resume_step':await self.render(m,actor,d,edit=True);return
            if action=='editmenu' and state=='review':
                rows=[[('👤 Имя',callback('edit',d,'name')),('📱 Телефон',callback('edit',d,'phone'))],[('👥 Гости',callback('edit',d,'guests'))],[('📅 Дата',callback('edit',d,'date')),('🕒 Время',callback('edit',d,'slot'))],[('← Назад','resume')]]
                await self.say(m,'✏️ Что изменить?\n\nОстальные данные сохранятся.',rows+nav(),edit=True);return
            if action=='rules' and state=='review':
                await self.info(m,actor,'Правила',back='resume',edit=True);return
            if action=='share' and state in ('phone','profile_phone','phone_contact','profile_contact'):
                await self.render(m,actor,d,edit=True);return
            if action in ('back','edit'):
                if state in ('phone_contact','profile_contact'):
                    await self.leave_contact(m,actor);d=await self.store.draft(actor,token);data=dict(d['data'])
                target='review' if action=='back' and data.get('editing') and data.get('start') else value
                require(data,target)
                new=target
                if action=='edit':data['editing']=True
                else:data.pop('editing',None)
            elif action=='keep' and ((value=='name' and state=='name') or (value=='phone' and state=='phone')):
                if value=='name':data['name']=clean_name(data.get('name',''));accept_field(data,'name');new='phone'
                else:data['phone']=normalize_phone(data.get('phone',''));accept_field(data,'phone');new='guests'
                if data.pop('editing',False) and data.get('start'):new='review'
            elif action=='guest' and state=='guests':data=change_guests(data,value[:-1],1 if value.endswith('+') else -1)
            elif action=='next' and state=='guests' and value=='date':
                accept_field(data,'guests');require(data,'date');new='review' if data.pop('editing',False) and data.get('start') else 'date'
            elif action=='month' and state=='date':
                month=date.fromisoformat(value)
                today=datetime.now(TZ).date()
                if not today.replace(day=1)<=month<=horizon(today).replace(day=1) or month.day!=1:raise RuleError('Выберите доступный месяц.')
                data['month']=value
            elif action=='day' and state=='date':
                day=date.fromisoformat(value);today=datetime.now(TZ).date()
                if not today<=day<=horizon(today):raise RuleError('Выберите доступную дату.')
                if any(r['starts_at'].astimezone(TZ).date()==day for r in await self.store.listing(actor)):raise RuleError('На эту дату у вас уже есть бронь.')
                if data.get('day')!=value:data.pop('start',None)
                data['day']=value;accept_field(data,'day');new='slot'
            elif action=='refresh' and state=='slot':pass
            elif action=='slot' and state=='slot':
                hour=int(value)
                if hour not in SLOTS:raise RuleError('Выберите время кнопкой.')
                start=datetime.combine(date.fromisoformat(data['day']),datetime.min.time(),TZ)+timedelta(hours=hour)
                validate_start(start,datetime.now(TZ))
                if not await self.store.free(start):raise RuleError('Это время уже занято. Обновите список сеансов.')
                data['start']=start.isoformat();accept_field(data,'start');data.pop('editing',None);new='review'
            elif action=='consent' and state=='review':data['consent']=not data.get('consent',False)
            else:raise RuleError('Кнопка недоступна на этом шаге. Продолжите бронирование.')
            if action!='consent':data['consent']=False
            await self.store.save(actor,d,new,data)
            await self.render(m,actor,await self.store.draft(actor,token),edit='caption' if action in ('guest','month','consent') else True)
        except (RuleError,ValueError,IndexError,KeyError) as error:
            await self.say(m,str(error) if isinstance(error,RuleError) else 'Откройте актуальный экран.',[[('Продолжить бронирование','resume')],[('📋 Мои бронирования','list:0:0')],[('☎️ Администратор','contact')]]+nav())
