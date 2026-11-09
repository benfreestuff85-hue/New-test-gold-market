import asyncio
import html
import os
import time
import unittest
from datetime import datetime,timezone
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock
from trade_controls import Execution
from free_gold import GoldMonitor,gauge,select_one

T='KXGOLD15M-26NOV09-A'
H='KXGOLDH-26NOV09-B'
def stamp(delta):return datetime.fromtimestamp(time.time()+delta,timezone.utc).isoformat()
def market(ticker=T):
    return dict(ticker=ticker,event_ticker=ticker.rsplit('-',1)[0],status='active',
                open_time=stamp(-60),close_time=stamp(600),expiration_time=stamp(600),
                series_ticker='KXGOLD15M' if ticker==T else 'KXGOLDH',strike_type='greater',floor_strike=4000)
def pos(n):return dict(market_positions=[dict(ticker=T,position_fp=str(n),netting_enabled=True)])
def book(yes='.42',no='.55'):
    return dict(orderbook_fp=dict(yes_dollars=[[yes,'100']] if yes else [],no_dollars=[[no,'100']] if no else []))
def order(filled):return dict(order_id='order-123',fill_count=str(filled),remaining_count='0.00',average_fill_price='.42')
def bot_stub():
    return SimpleNamespace(ENABLE_LIVE_ORDERS=True,KALSHI_KEY_ID='mock',PRIVATE_KEY_OBJ=object(),
        KALSHI_API_PREFIX='/trade-api/v2',KALSHI_HOST='https://mock.invalid',MONITOR=SimpleNamespace(fresh=lambda:True))

class SellTests(unittest.IsolatedAsyncioTestCase):
    def setup_execution(self,responses):
        e=Execution(bot_stub());e.request=AsyncMock(side_effect=responses);return e
    def payload(self,e):return next(c.kwargs['payload'] for c in e.request.call_args_list if c.args[1]=='POST')
    async def test_sell_yes_full(self):
        e=self.setup_execution([{'market':market()},pos(10),book(),order(10),pos(0)])
        ok,msg=await e.sell_all(None,T);self.assertTrue(ok);self.assertIn('Sold: 10.00 / 10.00 YES',msg)
        p=self.payload(e);self.assertEqual(p['side'],'ask');self.assertEqual(p['price'],'0.4200')
        self.assertTrue(p['reduce_only']);self.assertEqual(p['count'],'10.00');self.assertEqual(p['time_in_force'],'immediate_or_cancel')
    async def test_sell_no_full(self):
        e=self.setup_execution([{'market':market()},pos(-5),book(),order(5),pos(0)])
        ok,msg=await e.sell_all(None,T);self.assertTrue(ok);self.assertIn('NO',msg)
        p=self.payload(e);self.assertEqual(p['side'],'bid');self.assertEqual(p['price'],'0.4500')
    async def test_partial_fill(self):
        e=self.setup_execution([{'market':market()},pos(10),book(),order(3),pos(7)])
        ok,msg=await e.sell_all(None,T);self.assertFalse(ok);self.assertIn('Unfilled from this request: 7.00',msg)
    async def test_zero_fill(self):
        e=self.setup_execution([{'market':market()},pos(5),book(),order(0),pos(5)])
        ok,msg=await e.sell_all(None,T);self.assertFalse(ok);self.assertIn('Sold: 0.00',msg)
    async def test_no_position(self):
        e=self.setup_execution([{'market':market()},pos(0)])
        ok,msg=await e.sell_all(None,T);self.assertTrue(ok);self.assertEqual(e.request.await_count,2)
    async def test_no_liquidity(self):
        e=self.setup_execution([{'market':market()},pos(5),book(yes=None)])
        ok,msg=await e.sell_all(None,T);self.assertFalse(ok);self.assertIn('No executable',msg)
        self.assertFalse(any(c.args[1]=='POST' for c in e.request.call_args_list))
    async def test_hourly_rejected(self):
        e=self.setup_execution([]);ok,msg=await e.sell_all(None,H)
        self.assertFalse(ok);e.request.assert_not_awaited()
    async def test_closed_rejected(self):
        m=market();m['status']='closed'
        e=self.setup_execution([{'market':m}]);ok,msg=await e.sell_all(None,T)
        self.assertFalse(ok);self.assertEqual(e.request.await_count,1)
    async def test_expired_rejected(self):
        m=market();m['close_time']=stamp(-1)
        e=self.setup_execution([{'market':m}]);ok,msg=await e.sell_all(None,T);self.assertFalse(ok)
    async def test_live_disabled(self):
        e=self.setup_execution([]);e.bot.ENABLE_LIVE_ORDERS=False
        ok,msg=await e.sell_all(None,T);self.assertFalse(ok);e.request.assert_not_awaited()
    async def test_timeout_not_retried(self):
        e=self.setup_execution([{'market':market()},pos(5),book(),asyncio.TimeoutError()])
        ok,msg=await e.sell_all(None,T);self.assertFalse(ok);self.assertIn('may have reached Kalshi',msg)
        calls=e.request.await_count;ok,msg=await e.sell_all(None,T)
        self.assertEqual(e.request.await_count,calls);self.assertIn('uncertain',msg)
    async def test_fractional_position(self):
        e=self.setup_execution([{'market':market()},pos('2.50'),book(),order('2.50'),pos(0)])
        ok,msg=await e.sell_all(None,T);self.assertTrue(ok);self.assertEqual(self.payload(e)['count'],'2.50')
    async def test_malformed_response_blocks_retry(self):
        e=self.setup_execution([{'market':market()},pos(5),book(),{'status':'ok'}])
        ok,msg=await e.sell_all(None,T);self.assertFalse(ok);self.assertIn(T,e.uncertain)
    async def test_wrong_position_record_not_sold(self):
        data={'market_positions':[dict(ticker=H,position_fp='99')]}
        e=self.setup_execution([{'market':market()},data]);ok,msg=await e.sell_all(None,T)
        self.assertTrue(ok);self.assertIn('No shares held',msg)
    async def test_nonnetted_rejected(self):
        p=pos(5);p['market_positions'][0]['netting_enabled']=False
        e=self.setup_execution([{'market':market()},p]);ok,msg=await e.sell_all(None,T);self.assertFalse(ok)
    async def test_position_pagination(self):
        e=self.setup_execution([{'market_positions':[],'cursor':'abc'},pos(3)])
        self.assertEqual(await e.position(None,T),D(3));self.assertEqual(e.request.await_count,2)
    async def test_best_bid_unsorted(self):
        b=book();b['orderbook_fp']['yes_dollars']=[['.42','10'],['.47','2'],['.40','99']]
        e=self.setup_execution([{'market':market()},pos(5),b,order(2),pos(3)])
        await e.sell_all(None,T);self.assertEqual(self.payload(e)['price'],'0.4700')
    async def test_sell_not_dependent_on_proxy_feed(self):
        e=self.setup_execution([{'market':market()},pos(1),book(),order(1),pos(0)])
        e.bot.MONITOR.fresh=lambda:False
        ok,msg=await e.sell_all(None,T);self.assertTrue(ok)
    async def test_buy_yes_fresh_ask(self):
        e=self.setup_execution([{'market':market()},book(),order(1)])
        ok,msg=await e.buy(None,T,'YES',1);self.assertTrue(ok)
        p=self.payload(e);self.assertEqual(p['side'],'bid');self.assertEqual(p['price'],'0.4500');self.assertFalse(p['reduce_only'])
    async def test_buy_no_fresh_ask(self):
        e=self.setup_execution([{'market':market()},book(),order(5)])
        ok,msg=await e.buy(None,T,'NO',5);self.assertTrue(ok)
        self.assertEqual(self.payload(e)['side'],'ask');self.assertEqual(self.payload(e)['price'],'0.4200')
    async def test_buy_rejects_higher_ask(self):
        e=self.setup_execution([{'market':market()},book()])
        ok,msg=await e.buy(None,T,'YES',1,max_ask='.44')
        self.assertFalse(ok);self.assertIn('above the displayed',msg)
        self.assertFalse(any(c.args[1]=='POST' for c in e.request.call_args_list))
    async def test_buy_hourly_rejected(self):
        e=self.setup_execution([]);ok,msg=await e.buy(None,H,'YES',1)
        self.assertFalse(ok);e.request.assert_not_awaited()
    async def test_concurrent_orders_blocked(self):
        e=self.setup_execution([]);lock=asyncio.Lock();await lock.acquire();e.locks[T]=lock
        ok,msg=await e.sell_all(None,T);self.assertFalse(ok);e.request.assert_not_awaited();lock.release()

class DisplayTests(unittest.TestCase):
    def ready(self):
        g=GoldMonitor();g.price=4000;g.publish_time=g.received=g.updated=time.time()
        g.rows=[dict(minutes=n,ticker=T if n==15 else H,momentum=63,projected_price=4010+n,
                     yes_score=80 if n==15 else None,prediction='YES' if n==15 else None,
                     target=4000 if n==15 else None,seconds_left=600,outcome='YES' if n==15 else None,
                     reason='',quotes={'yes_ask':45,'no_ask':58},updated_at=time.time()) for n in (15,60)]
        return g
    def test_center_positive(self):self.assertIn('░'*8+'│'+'█'*5,gauge(63))
    def test_center_negative(self):self.assertIn('░'*3+'█'*5+'│'+'░'*8,gauge(-63))
    def test_zero(self):self.assertIn('░'*8+'│'+'░'*8,gauge(0))
    def test_one_forecast_each(self):
        report=self.ready().report();self.assertEqual(report.count('Projected finish:'),2)
        hourly=report.split('<b>1-HOUR MARKET</b>')[1]
        self.assertNotIn('YES',hourly);self.assertNotIn('Target:',hourly);self.assertNotIn(H,hourly)
    def test_button_layout(self):
        g=self.ready();keyboard=g.keyboard()['inline_keyboard']
        self.assertEqual(len(keyboard),2);self.assertEqual(len(keyboard[0]),1);self.assertEqual(len(keyboard[1]),3)
        for button in keyboard[0]+keyboard[1]:
            token=button['callback_data'].split('|')[2]
            self.assertEqual(g.contexts[token]['ticker'],T)
            self.assertLessEqual(len(button['callback_data'].encode()),64)
    def test_no_hourly_only_controls(self):
        g=self.ready();g.rows=g.rows[1:];self.assertIsNone(g.keyboard())
    def test_select_only_current_expiry(self):
        a=market(H);b=market(H+'-NEXT');b['series_ticker']='KXGOLDH';b['expiration_time']=stamp(3600)
        self.assertEqual(select_one([b,a],'KXGOLDH',time.time(),4000)['ticker'],H)
    def test_stale_masks_predictions(self):
        g=self.ready();g.received=0
        self.assertIsNone(g.snapshot()['markets'][0]['prediction'])
    def test_free_feed_timestamp(self):
        g=GoldMonitor()
        with self.assertRaises(ValueError):g.decode(dict(symbol='XAU',price=4000,updatedAt=stamp(-3600)))

class CallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_sell_callback(self):
        g=GoldMonitor();token='abc';g.contexts[token]=dict(ticker=T,outcome='YES',created=time.time())
        b=SimpleNamespace(TELEGRAM_CHAT_ID='123',TELEGRAM_TOKEN='',is_authorized=lambda c:c=='123',
            EXECUTION=SimpleNamespace(sell_all=AsyncMock(return_value=(True,'sold'))),send_telegram_message=AsyncMock())
        q=dict(id='q1',data='G2|S|abc',message={'chat':{'id':123}},**{'from':{'id':123}})
        await g.callback(None,b,q);await g.callback(None,b,q)
        b.EXECUTION.sell_all.assert_awaited_once_with(None,T)
    async def test_unauthorized_callback(self):
        g=GoldMonitor();b=SimpleNamespace(TELEGRAM_CHAT_ID='123',is_authorized=lambda c:False,
            EXECUTION=SimpleNamespace(sell_all=AsyncMock()))
        await g.callback(None,b,dict(id='q',data='G2|S|abc'))
        b.EXECUTION.sell_all.assert_not_awaited()


class HTTPIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_sell_through_real_http_client_to_local_mock(self):
        import aiohttp
        from aiohttp import web
        from aiohttp.test_utils import TestServer
        calls=[]
        holdings=[D('5')]
        async def get_market(request):
            return web.json_response({'market':market()})
        async def get_positions(request):
            self.assertEqual(request.query['ticker'],T)
            return web.json_response(pos(holdings[0]))
        async def get_book(request):
            return web.json_response(book())
        async def post_order(request):
            p=await request.json();calls.append(p)
            self.assertEqual(request.headers.get('KALSHI-ACCESS-KEY'),'mock-key')
            self.assertTrue(p['reduce_only']);self.assertEqual(p['side'],'ask')
            self.assertEqual(p['count'],'5.00');self.assertEqual(p['price'],'0.4200')
            holdings[0]=D('2')
            return web.json_response(order(3),status=201)
        app=web.Application()
        app.router.add_get('/trade-api/v2/markets/'+T,get_market)
        app.router.add_get('/trade-api/v2/portfolio/positions',get_positions)
        app.router.add_get('/trade-api/v2/markets/'+T+'/orderbook',get_book)
        app.router.add_post('/trade-api/v2/portfolio/events/orders',post_order)
        async with TestServer(app) as server:
            b=bot_stub();b.KALSHI_HOST=str(server.make_url('')).rstrip('/')
            b.build_kalshi_auth_headers=lambda method,path:{'KALSHI-ACCESS-KEY':'mock-key'}
            e=Execution(b)
            async with aiohttp.ClientSession() as session:
                ok,msg=await e.sell_all(session,T)
            self.assertFalse(ok);self.assertIn('Sold: 3.00 / 5.00 YES',msg)
            self.assertIn('Current signed net position: 2.00',msg)
        self.assertEqual(len(calls),1)

    async def test_dashboard_auth_and_command(self):
        import aiohttp
        from aiohttp import web
        from aiohttp.test_utils import TestServer
        from free_gold import dashboard_auth,state_api,command_api
        before=os.environ.get('DASHBOARD_TOKEN');os.environ['DASHBOARD_TOKEN']='test-secret-only'
        try:
            app=web.Application(middlewares=[dashboard_auth])
            app.router.add_get('/api/gold',state_api);app.router.add_post('/api/command',command_api)
            async with TestServer(app) as server:
                async with aiohttp.ClientSession() as session:
                    async with session.get(server.make_url('/api/gold')) as r:self.assertEqual(r.status,401)
                    headers={'Authorization':'Bearer test-secret-only'}
                    async with session.get(server.make_url('/api/gold'),headers=headers) as r:
                        self.assertEqual(r.status,200);data=await r.json();self.assertIn('markets',data)
                    async with session.post(server.make_url('/api/command'),headers=headers,json={'command':'help'}) as r:
                        self.assertEqual(r.status,200);self.assertIn('/predict',await r.text())
        finally:
            if before is None:os.environ.pop('DASHBOARD_TOKEN',None)
            else:os.environ['DASHBOARD_TOKEN']=before


class ForecastCardinalityTests(unittest.IsolatedAsyncioTestCase):
    async def test_many_hourly_strikes_produce_one_hourly_forecast(self):
        g=GoldMonitor();g.price=4000;g.publish_time=g.received=time.time()
        g.model=lambda state,minutes:(63,.00001,.001)
        fifteen=market()
        hourly=[]
        for n in range(20):
            m=market(H+str(n));m['series_ticker']='KXGOLDH';m['floor_strike']=3900+n*10
            m['expected_expiration_time']=stamp(1800);hourly.append(m)
        b=SimpleNamespace(STATE=SimpleNamespace(active_markets_count=0),
            fetch_kalshi_gold_markets=AsyncMock(return_value=[fifteen]+hourly),
            fetch_orderbook_details=AsyncMock(return_value={'yes_ask':45,'no_ask':58,'spread':3}),
            parse_strike=lambda m:float(m['floor_strike']))
        await g.refresh(None,b)
        self.assertEqual(len(g.rows),2)
        self.assertEqual(sum(r['minutes']==60 for r in g.rows),1)
        self.assertIsNone(g.rows[1]['yes_score'])
        self.assertEqual(g.report().count('Projected finish:'),2)

if __name__=='__main__':unittest.main()
