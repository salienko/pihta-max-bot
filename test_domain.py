import unittest
from datetime import datetime, date, timedelta
from domain import TZ, RuleError, horizon, price, window, claim_allowed, money, SLOTS, normalize_phone, sorted_services, SERVICES

class RulesTests(unittest.TestCase):
    def dt(self,hour,day=15):return datetime(2026,10,day,hour,tzinfo=TZ)
    def test_fixed_slots(self):self.assertEqual(SLOTS,(10,13,16,19,22))
    def test_rates(self):
        cases=[(16,2,500000),(19,2,600000),(18,3,830000),(16,4,1010000)]
        for hour,hours,amount in cases:
            with self.subTest(hour=hour):self.assertEqual(price(self.dt(hour),hours,4,0)[0],amount)
    def test_weekend(self):self.assertEqual(price(self.dt(10,17),3,4,0)[0],880000)
    def test_holiday_priority(self):self.assertEqual(price(self.dt(10,17),5,4,0,(650000,300000))[0],1550000)
    def test_children(self):
        self.assertEqual(price(self.dt(10),2,8,2)[0],600000)
        self.assertEqual(price(self.dt(10),2,8,4)[0],500000)
        self.assertEqual(price(self.dt(10),2,8,0)[0],700000)
    def test_invalid_guests(self):
        for guests,children in [(9,0),(0,0),(3,4),(3,-1)]:
            with self.subTest(guests=guests,children=children),self.assertRaises(RuleError):price(self.dt(10),2,guests,children)
    def test_cleaning_after_midnight(self):
        end,clean=window(self.dt(22),clock=self.dt(8))
        self.assertEqual(end,self.dt(0,16));self.assertEqual(clean,self.dt(1,16))
    def test_exact_hour_cutoff(self):
        window(self.dt(10),clock=self.dt(9))
        with self.assertRaises(RuleError):window(self.dt(10),clock=self.dt(9)+timedelta(seconds=1))
    def test_calendar_months(self):
        self.assertEqual(horizon(date(2026,10,14)),date(2026,12,14))
        self.assertEqual(horizon(date(2026,12,31)),date(2027,2,28))
    def test_horizon_boundary(self):
        clock=self.dt(8,14)
        window(datetime(2026,12,14,10,tzinfo=TZ),clock=clock)
        with self.assertRaises(RuleError):window(datetime(2026,12,15,10,tzinfo=TZ),clock=clock)
    def test_manual_duration(self):
        with self.assertRaises(RuleError):window(self.dt(10),4,clock=self.dt(8))
        end,clean=window(self.dt(10),4,clock=self.dt(8),manual=True)
        self.assertEqual(clean,self.dt(15))
    def test_claim_16_19_minutes(self):
        deadline=self.dt(10)+timedelta(minutes=20)
        for minute in (14,15,16,19):claim_allowed('held',deadline,self.dt(10)+timedelta(minutes=minute))
        with self.assertRaises(RuleError):claim_allowed('held',deadline,deadline)
    def test_overdue_keeps_claimable(self):claim_allowed('review_overdue',self.dt(9),self.dt(14))
    def test_money(self):
        self.assertEqual(money('1500,50'),150050)
        for value in ('NaN','Infinity','-1','0','1.001','bad'):
            with self.subTest(value=value),self.assertRaises(RuleError):money(value)

    def test_russian_phone_formats(self):
        for phone in ['+7 (981) 244-00-50','8 981 244 00 50','79812440050','9812440050']:
            self.assertEqual(normalize_phone(phone),'+79812440050')
        for phone in ['981781345','+3809812440050','123','phone','+89812440050','+798124400500']:
            with self.subTest(phone=phone),self.assertRaises(RuleError):normalize_phone(phone)

    def test_services_brooms_first_then_alphabetical(self):
        items=sorted_services([{'code':c,'name':n} for c,n,_ in SERVICES])
        self.assertEqual([r['code'] for r in items[:2]],['birch','oak'])
        self.assertEqual([r['name'] for r in items[2:]],sorted(r['name'] for r in items[2:]))

if __name__=='__main__':unittest.main()
