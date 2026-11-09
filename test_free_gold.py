import unittest
import time
from datetime import datetime,timezone
from pathlib import Path
from free_gold import FreeGold
import bot

def payload(age=0,**kw):
    data=dict(symbol='XAU',currency='USD',price=4077.123456,
        updatedAt=datetime.fromtimestamp(time.time()-age,timezone.utc).isoformat())
    data.update(kw)
    return data

class Tests(unittest.TestCase):
    def test_decode(self):
        p,t=FreeGold().decode(payload())
        self.assertAlmostEqual(p,4077.123456)
    def test_stale(self):
        with self.assertRaises(ValueError):FreeGold().decode(payload(age=3600))
    def test_wrong_symbol(self):
        with self.assertRaises(ValueError):FreeGold().decode(payload(symbol='XAG'))
    def test_wrong_currency(self):
        with self.assertRaises(ValueError):FreeGold().decode(payload(currency='EUR'))
    def test_bad_price(self):
        with self.assertRaises(ValueError):FreeGold().decode(payload(price='nan'))
    def test_missing_timestamp(self):
        data=payload();del data['updatedAt']
        with self.assertRaises(KeyError):FreeGold().decode(data)
    def test_commands(self):
        g=FreeGold()
        self.assertIn('65',g.command('threshold 65'))
        self.assertIn('Usage',g.command('threshold nan'))
        g.command('mute');self.assertTrue(g.muted)
        g.command('unmute');self.assertFalse(g.muted)
    def test_stale_snapshot(self):
        g=FreeGold();g.rows=[dict(updated_at=time.time(),momentum=75,projected_spot=4100,yes_score=85)]
        self.assertIsNone(g.snapshot()['markets'][0]['yes_score'])
    def test_no_other_provider(self):
        code=Path('bot.py').read_text()
        self.assertNotIn('swissquote',code.lower())
        self.assertNotIn('PYTH_API_KEY',code)
        self.assertEqual(bot.GOLD_SERIES_TICKERS,['KXGOLD15M','KXGOLDH'])
    def test_structured_strike(self):
        self.assertEqual(bot.parse_strike(dict(strike_type='greater',floor_strike='4000.12')),4000.12)
        self.assertIsNone(bot.parse_strike(dict(title='Gold above $4000')))

class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_orders_disabled(self):
        before=bot.ENABLE_LIVE_ORDERS;bot.ENABLE_LIVE_ORDERS=False
        try:
            ok,msg=await bot.place_kalshi_order(None,'TEST','YES',50,1)
            self.assertFalse(ok);self.assertIn('disabled',msg)
        finally:bot.ENABLE_LIVE_ORDERS=before

if __name__=='__main__':unittest.main()
