import unittest
from gold_model import extract_features, orderbook_features, probability


def candles(prices, tf=60):
    base=1_800_000_000
    return [{'timestamp':base+i*tf,'open':p,'high':p,'low':p,'close':p} for i,p in enumerate(prices)]


class GoldModelTests(unittest.TestCase):
    def test_15m_features_include_momentum_and_acceleration(self):
        prices=[4000+i*.12 for i in range(80)]
        f=extract_features(candles(prices),[],prices[-1],15)
        self.assertGreater(f['momentum_z'],0)
        self.assertIn('acceleration_z',f)
        self.assertEqual(f['source_timeframe_minutes'],1)

    def test_hourly_uses_five_minute_history(self):
        p1=[4000+i*.01 for i in range(90)]
        p5=[4000+i*.04 for i in range(20)]
        f=extract_features(candles(p1),candles(p5,300),p1[-1],60)
        self.assertEqual(f['source_timeframe_minutes'],5)

    def test_hourly_does_not_forge_full_horizon_from_short_history(self):
        p=[4000+i*.1 for i in range(30)]
        self.assertIsNone(extract_features(candles(p),[],p[-1],60))

    def test_jump_detection(self):
        p=[4000.0+i*.01 for i in range(40)]
        p[-1]=p[-2]*1.003
        f=extract_features(candles(p),[],p[-1],15)
        self.assertTrue(f['jump_detected'])

    def test_top_five_depth_imbalance(self):
        b=orderbook_features({'yes_bid':50,'yes_ask':52,
            'yes_bid_levels':[(50,100),(49,50)],'yes_ask_levels':[(52,20),(53,10)]})
        self.assertGreater(b['depth_imbalance'],0)
        self.assertGreater(b['microprice_offset'],0)

    def test_book_adjustment_is_bounded_and_changes_probability(self):
        plain=probability(4000,4000,600,.0002,'greater',basis_usd=.75)
        book=orderbook_features({'yes_bid':50,'yes_ask':52,
            'yes_bid_levels':[(50,100)],'yes_ask_levels':[(52,10)]})
        adjusted=probability(4000,4000,600,.0002,'greater',book_features=book,basis_usd=.75)
        self.assertGreater(adjusted,plain)
        self.assertLessEqual(adjusted,.98)

if __name__=='__main__': unittest.main()
