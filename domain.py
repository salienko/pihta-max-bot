"""Pure booking rules. Amounts are integer kopecks, dates use bath timezone."""
import calendar
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Europe/Moscow')
SLOTS = (10, 13, 16, 19, 22)
ACTIVE = ('held', 'payment_review', 'review_overdue', 'confirmed', 'in_progress', 'completed')
SERVICES = (
    ('towel', 'Полотенце в аренду', 10000),
    ('sheet', 'Дополнительная простыня', 10000),
    ('birch', 'Веник берёзовый', 50000),
    ('oak', 'Веник дубовый', 60000),
    ('eucalyptus', 'Аромапарение эвкалипт', 50000),
    ('lavender', 'Аромапарение лаванда', 50000),
    ('slippers', 'Тапочки новые', 35000),
    ('hat', 'Шапочка новая', 20000),
)

class RuleError(ValueError):
    pass

def now():
    return datetime.now(TZ)

def horizon(today):
    month = today.month + 2
    year = today.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    return date(year, month, min(today.day, calendar.monthrange(year, month)[1]))

def window(start, hours=2, clock=None, manual=False):
    clock = clock or now()
    start = start.astimezone(TZ)
    if start.minute or start.second or start.microsecond:
        raise RuleError('Начало должно быть в целый час.')
    if hours not in (2, 3, 4, 5) or (not manual and (hours != 2 or start.hour not in SLOTS)):
        raise RuleError('Выберите стандартный сеанс; 3–5 часов оформляет администратор.')
    end = start + timedelta(hours=hours)
    midnight = datetime.combine(start.date() + timedelta(days=1), time(), TZ)
    if start.hour < 10 or end > midnight:
        raise RuleError('Сеанс должен проходить с 10:00 до 00:00.')
    if start < clock + timedelta(hours=1) or start.date() > horizon(clock.date()):
        raise RuleError('Запись минимум за час, максимум на два календарных месяца вперёд.')
    return end, end + timedelta(hours=1)

def price(start, hours, guests, children, holiday=None):
    if not 1 <= guests <= 8 or not 0 <= children <= guests:
        raise RuleError('Всего 1–8 гостей; число детей не больше общего числа.')
    if hours not in (2, 3, 4, 5):
        raise RuleError('Допустимая длительность 2–5 часов.')
    lines = []
    for i in range(hours):
        hour = start.astimezone(TZ) + timedelta(hours=i)
        base, extra = holiday if holiday else ((600000, 280000) if hour.weekday() >= 5 or hour.hour >= 19 else (500000, 230000))
        amount = base // 2 if i < 2 else extra
        lines.append({'kind': 'rent', 'hour': hour.isoformat(), 'amount': amount})
    surcharge = max(0, guests - children - 4) * 25000 * hours
    lines.append({'kind': 'guests', 'amount': surcharge})
    return sum(x['amount'] for x in lines), lines

def claim_allowed(status, release_at, clock):
    if status == 'held' and clock >= release_at:
        raise RuleError('Резерв истёк. Свяжитесь с администратором для проверки оплаты.')
    if status not in ('held', 'payment_review', 'review_overdue', 'confirmed', 'in_progress'):
        raise RuleError('Заявить оплату для этого заказа нельзя.')

def money(value):
    try:
        amount = Decimal(str(value).replace(',', '.'))
        if not amount.is_finite() or amount <= 0 or amount != amount.quantize(Decimal('.01')):
            raise RuleError('Введите положительную сумму с точностью до копеек.')
        result = int(amount * 100)
        if result > 100000000:
            raise RuleError('Слишком большая сумма.')
        return result
    except (InvalidOperation, ValueError):
        raise RuleError('Введите сумму в рублях, например 2000 или 1500.50.') from None

def rub(amount):
    return f'{amount / 100:,.2f}'.replace(',', ' ').replace('.00', '') + ' ₽'

def normalize_phone(value):
    value=str(value).strip()
    if not re.fullmatch(r'\+?[0-9\s().-]+',value):
        raise RuleError('Введите российский номер: +7XXXXXXXXXX, 8XXXXXXXXXX или 10 цифр без кода страны.')
    digits=re.sub(r'[^0-9]','',value)
    if value.startswith('+') and not (len(digits)==11 and digits.startswith('7')):
        raise RuleError('Нужен российский номер с кодом +7.')
    if len(digits)==10 and not value.startswith('+'):digits='7'+digits
    elif len(digits)==11 and digits.startswith('8'):digits='7'+digits[1:]
    if len(digits)!=11 or not digits.startswith('7') or digits[1]=='0':
        raise RuleError('Проверьте номер: нужно 10 цифр после +7.')
    return '+'+digits

def sorted_services(items):
    return sorted(items,key=lambda r:(0 if r['code'] in ('birch','oak') else 1,r['name'].casefold()))
