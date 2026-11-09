import asyncio
import os
import time
import unittest
from datetime import datetime,timezone
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from free_gold import GoldMonitor,gauge
from trade_controls import Execution

T='KXGOLD15M-26NOV09-A';H='KXGOLDH-26NOV09-B'
def stamp(seconds):return datetime.fromtimestamp(time.time()+seconds,timezone.utc).isoformat()
def market(ticker=T):
    return dict(ticker=ticker,event_ticker=ticker.rsplit('-',1)[0],status='active',open_time=stamp(-60),
        close_time=stamp(600),expiration_time=stamp(600),series_ticker='KXGOLD15M' if ticker==T else 'KXGOLDH',
        strike_type='greater',floor_strike=4000)
def pos(n):return dict(market_positions=[dict(ticker=T,position_fp=str(n),netting_enabled=True)])
def book(y='.42',n='.55'):
    return dict(orderbook_fp=dict(yes_dollars=[[y,'100']] if y else [],no_dollars=[[n,'100']] if n else []))
def order(n):return dict(order_id='mock-order',fill_count=str(n),remaining_count='0.00',average_fill_price='.42')
def bot_stub():
    return SimpleNamespace(ENABLE_LIVE_ORDERS=True,KALSHI_KEY_ID='mock',PRIVATE_KEY_OBJ=object(),
        KALSHI_API_PREFIX='/trade-api/v2',KALSHI_HOST='https://mock.invalid',MONITOR=SimpleNamespace(fresh=lambda:True))

class TradeTests(unittest.IsolatedAsyncioTestCase):
    def execution(self,responses):
        e=Execution(bot_stub());e.request=AsyncMock(side_effect=responses);return e
    def payload(self,e):return next(c.kwargs['payload'] for c in e.request.call_args_list if c.args[1]=='POST')
    async def test_yes_sell(self):
        e=self.execution([{'market':market()},pos(10),book(),order(10),pos(0)])
        ok,msg=await e.sell_all(None,T);self.assertTrue(ok)
        p=self.payload(e);self.assertEqual(p['side'],'ask');self.assertEqual(p['price'],'0.4200');self.assertTrue(p['reduce_only'])
        self.assertEqual(p['count'],'10.00');self.assertEqual(p['time_in_force'],'immediate_or_cancel')
    async def test_no_sell(self):
        e=self.execution([{'market':market()},pos(-5),book(),order(5),pos(0)])
        ok,msg=await e.sell_all(None,T);self.assertTrue(ok)
        p=self.payload(e);self.assertEqual(p['side'],'bid');self.assertEqual(p['price'],'0.4500');self.assertIn('NO',msg)
    async def test_partial_sell(self):
        e=self.execution([{'market':market()},pos(10),book(),order(3),pos(7)])
        ok,msg=await e.sell_all(None,T);self.assertFalse(ok);self.assertIn('Unfilled requested shares: 7.00',msg)
    async def test_zero_fill(self):
        e=self.execution([{'market':market()},pos(5),book(),order(0),pos(5)])
        ok,msg=await e.sell_all(None,T);self.assertFalse(ok);self.assertIn('Sold: 0.00',msg)
    async def test_fractional(self):
        e=self.execution([{'market':market()},pos('2.50'),book(),order('2.50'),pos(0)])
        ok,msg=await e.sell_all(None,T);self.assertTrue(ok);self.assertEqual(self.payload(e)['count'],'2.50')
    async def test_zero_position_no_order(self):
        e=self.execution([{'market':market()},pos(0)]);ok,msg=await e.sell_all(None,T)
        self.assertTrue(ok);self.assertEqual(e.request.await_count,2)
    async def test_empty_book_no_order(self):
        e=self.execution([{'market':market()},pos(5),book(y=None)]);ok,msg=await e.sell_all(None,T)
        self.assertFalse(ok);self.assertEqual(e.request.await_count,3)
    async def test_hourly_sell_rejected(self):
        e=self.execution([]);ok,msg=await e.sell_all(None,H);self.assertFalse(ok);e.request.assert_not_awaited()
    async def test_hourly_buy_rejected(self):
        e=self.execution([]);ok,msg=await e.buy(None,H,'YES',1,'.45');self.assertFalse(ok);e.request.assert_not_awaited()
    async def test_closed(self):
        m=market();m['status']='closed';e=self.execution([{'market':m}]);ok,msg=await e.sell_all(None,T)
        self.assertFalse(ok);self.assertEqual(e.request.await_count,1)
    async def test_future_market(self):
        m=market();m['open_time']=stamp(300);e=self.execution([{'market':m}]);ok,msg=await e.sell_all(None,T);self.assertFalse(ok)
    async def test_disabled(self):
        e=self.execution([]);e.bot.ENABLE_LIVE_ORDERS=False;ok,msg=await e.sell_all(None,T)
        self.assertFalse(ok);e.request.assert_not_awaited()
    async def test_unsupported_netting(self):
        p=pos(5);p['market_positions'][0]['netting_enabled']=False;e=self.execution([{'market':market()},p])
        ok,msg=await e.sell_all(None,T);self.assertFalse(ok)
    async def test_position_pagination(self):
        e=self.execution([{'market_positions':[],'cursor':'next'},pos(5)])
        self.assertEqual(await e.position(None,T),D(5));self.assertEqual(e.request.await_count,2)
    async def test_timeout_no_retry(self):
        e=self.execution([{'market':market()},pos(5),book(),asyncio.TimeoutError()])
        ok,msg=await e.sell_all(None,T);self.assertFalse(ok);self.assertIn(T,e.uncertain)
        calls=e.request.await_count;await e.sell_all(None,T);self.assertEqual(e.request.await_count,calls)
    async def test_unknown_response_blocks_retry(self):
        e=self.execution([{'market':market()},pos(5),book(),{'status':'ok'}]);ok,msg=await e.sell_all(None,T)
        self.assertFalse(ok);self.assertIn(T,e.uncertain)
    async def test_sell_allowed_stale_proxy(self):
        e=self.execution([{'market':market()},pos(1),book(),order(1),pos(0)]);e.bot.MONITOR.fresh=lambda:False
        ok,msg=await e.sell_all(None,T);self.assertTrue(ok)
    async def test_buy_yes(self):
        e=self.execution([{'market':market()},book(),order(1)]);ok,msg=await e.buy(None,T,'YES',1,'.45')
        self.assertTrue(ok);p=self.payload(e);self.assertEqual(p['side'],'bid');self.assertEqual(p['price'],'0.4500');self.assertFalse(p['reduce_only'])
    async def test_buy_no(self):
        e=self.execution([{'market':market()},book(),order(5)]);ok,msg=await e.buy(None,T,'NO',5,'.58')
        self.assertTrue(ok);p=self.payload(e);self.assertEqual(p['side'],'ask');self.assertEqual(p['price'],'0.4200')
    async def test_buy_cap(self):
        e=self.execution([{'market':market()},book()]);ok,msg=await e.buy(None,T,'YES',1,'.44')
        self.assertFalse(ok);self.assertIn('above displayed',msg)
    async def test_buy_requires_quote(self):
        e=self.execution([]);ok,msg=await e.buy(None,T,'YES',1,None);self.assertFalse(ok);e.request.assert_not_awaited()
    async def test_concurrent_order_blocked(self):
        e=self.execution([]);lock=asyncio.Lock();await lock.acquire();e.locks[T]=lock
        ok,msg=await e.sell_all(None,T);self.assertFalse(ok);e.request.assert_not_awaited();lock.release()

    async def test_missing_netting_mode(self):
        p=pos(5);del p['market_positions'][0]['netting_enabled']
        e=self.execution([{'market':market()},p]);ok,msg=await e.sell_all(None,T);self.assertFalse(ok)
    async def test_buy_blocked_stale_proxy(self):
        e=self.execution([]);e.bot.MONITOR.fresh=lambda:False
        ok,msg=await e.buy(None,T,'YES',1,'.45');self.assertFalse(ok);e.request.assert_not_awaited()

class SelectorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.g=GoldMonitor();self.g.edit_keyboard=AsyncMock();self.g.held=AsyncMock(return_value=D('-5'))
        self.b=SimpleNamespace(TELEGRAM_CHAT_ID='123',TELEGRAM_TOKEN='test',KALSHI_KEY_ID='mock',PRIVATE_KEY_OBJ=object(),
            is_authorized=lambda c:c=='123',fetch_orderbook_details=AsyncMock(return_value={'yes_ask':45,'no_ask':58}),
            send_telegram_message=AsyncMock(),EXECUTION=SimpleNamespace(buy=AsyncMock(return_value=(True,'bought')),sell_all=AsyncMock(return_value=(True,'sold'))))
        self.token=self.g.new_context(T,held=D(-5));self.g.contexts[self.token].update(chat_id='123',message_id=7)
        self.env=patch.dict(os.environ,{'TELEGRAM_USER_ID':'123'});self.env.start()
    def tearDown(self):self.env.stop()
    def q(self,action,token=None,qty=None):
        return dict(id='query',data=f'G3|{action}|{token or self.token}'+(f'|{qty}' if qty is not None else ''),
            message={'chat':{'id':123},'message_id':7},**{'from':{'id':123}})
    def active(self):return next((k for k,v in self.g.contexts.items() if v['active']),None)
    async def test_initial_side_unselected(self):
        self.assertIsNone(self.g.contexts[self.token]['side']);self.assertEqual(len(self.g.keyboard(self.token)['inline_keyboard']),2)
    async def test_choose_yes_no_order(self):
        await self.g.callback(None,self.b,self.q('Y'));token=self.active()
        self.assertEqual(self.g.contexts[token]['side'],'YES');self.b.EXECUTION.buy.assert_not_awaited()
        rows=self.g.keyboard(token)['inline_keyboard'];self.assertEqual(len(rows[2]),3)
        self.assertTrue(all('YES' in b['text'] for b in rows[2]))
    async def test_choose_no_no_order(self):
        await self.g.callback(None,self.b,self.q('N'));token=self.active()
        self.assertEqual(self.g.contexts[token]['side'],'NO');self.b.EXECUTION.buy.assert_not_awaited()
        self.assertIn('NO SELECTED',self.g.keyboard(token)['inline_keyboard'][1][1]['text'])
    async def test_switch_revokes_old_buttons(self):
        await self.g.callback(None,self.b,self.q('Y'));yes=self.active()
        await self.g.callback(None,self.b,self.q('N',yes));no=self.active()
        await self.g.callback(None,self.b,self.q('B',yes,1));self.b.EXECUTION.buy.assert_not_awaited()
        await self.g.callback(None,self.b,self.q('B',no,5))
        self.b.EXECUTION.buy.assert_awaited_once_with(None,T,'NO',5,max_ask=.58)
    async def test_no_buy_before_selection(self):
        await self.g.callback(None,self.b,self.q('B',qty=1));self.b.EXECUTION.buy.assert_not_awaited()
    async def test_manual_side_independent_model(self):
        self.g.rows=[{'prediction':'YES'}];await self.g.callback(None,self.b,self.q('N'));no=self.active()
        await self.g.callback(None,self.b,self.q('B',no,1))
        self.b.EXECUTION.buy.assert_awaited_once_with(None,T,'NO',1,max_ask=.58)
    async def test_sell_without_selection(self):
        await self.g.callback(None,self.b,self.q('S'));self.b.EXECUTION.sell_all.assert_awaited_once_with(None,T)
    async def test_sell_actual_side_not_selection(self):
        await self.g.callback(None,self.b,self.q('Y'));yes=self.active()
        self.assertEqual(self.g.contexts[yes]['held'],D(-5));await self.g.callback(None,self.b,self.q('S',yes))
        self.b.EXECUTION.sell_all.assert_awaited_once_with(None,T)
    async def test_duplicate_buy(self):
        await self.g.callback(None,self.b,self.q('Y'));token=self.active();q=self.q('B',token,1)
        await self.g.callback(None,self.b,q);await self.g.callback(None,self.b,q);self.b.EXECUTION.buy.assert_awaited_once()
    async def test_duplicate_sell(self):
        q=self.q('S');await self.g.callback(None,self.b,q);await self.g.callback(None,self.b,q)
        self.b.EXECUTION.sell_all.assert_awaited_once()
    async def test_missing_ask_no_buy_buttons(self):
        self.b.fetch_orderbook_details.return_value={'yes_ask':None,'no_ask':58}
        await self.g.callback(None,self.b,self.q('Y'));self.assertEqual(len(self.g.keyboard(self.active())['inline_keyboard']),2)
    async def test_failed_edit_leaves_no_active_buy(self):
        self.g.edit_keyboard.side_effect=ValueError('edit failed');await self.g.callback(None,self.b,self.q('Y'))
        self.assertIsNone(self.active());self.b.EXECUTION.buy.assert_not_awaited()
    async def test_unauthorized(self):
        q=self.q('S');q['from']['id']=999;await self.g.callback(None,self.b,q);self.b.EXECUTION.sell_all.assert_not_awaited()
    async def test_wrong_message(self):
        q=self.q('S');q['message']['message_id']=99;await self.g.callback(None,self.b,q);self.b.EXECUTION.sell_all.assert_not_awaited()
    async def test_old_callbacks_disabled(self):
        q=self.q('B');q['data']=f'BUY|{T}|YES|45|1';await self.g.callback(None,self.b,q);self.b.EXECUTION.buy.assert_not_awaited()
    async def test_expired_buy(self):
        await self.g.callback(None,self.b,self.q('Y'));token=self.active();self.g.contexts[token]['created']-=100
        await self.g.callback(None,self.b,self.q('B',token,1));self.b.EXECUTION.buy.assert_not_awaited()
    async def test_hourly_context_rejected(self):self.assertIsNone(self.g.new_context(H))
    async def test_button_payload_size(self):
        await self.g.callback(None,self.b,self.q('N'))
        for row in self.g.keyboard(self.active())['inline_keyboard']:
            for b in row:self.assertLessEqual(len(b['callback_data'].encode()),64)
    async def test_sell_label_held_no(self):self.assertIn('5.00 NO',self.g.keyboard(self.token)['inline_keyboard'][0][0]['text'])

class ForecastTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_forecast_per_timeframe(self):
        g=GoldMonitor();g.price=4000;g.received=g.publish_time=time.time();g.model=lambda state,minutes:(63,.00001,.001)
        markets=[market()]
        for i in range(20):
            m=market(H+str(i));m.update(series_ticker='KXGOLDH',floor_strike=3900+10*i,expiration_time=stamp(1800));markets.append(m)
        b=SimpleNamespace(STATE=SimpleNamespace(active_markets_count=0),fetch_kalshi_gold_markets=AsyncMock(return_value=markets),
            fetch_orderbook_details=AsyncMock(return_value={'yes_ask':45,'no_ask':58,'spread':3}),parse_strike=lambda m:float(m['floor_strike']))
        await g.refresh(None,b);self.assertEqual(len(g.rows),2);self.assertIsNone(g.rows[1]['yes_score'])
        report=g.report();self.assertEqual(report.count('Projected finish:'),2)
        hour=report.split('<b>1-HOUR MARKET</b>')[1].split('<i>')[0];self.assertNotIn('Model prediction:',hour)
    async def test_positive_gauge(self):self.assertIn('░'*8+'│'+'█'*5,gauge(63))
    async def test_negative_gauge(self):self.assertIn('░'*3+'█'*5+'│'+'░'*8,gauge(-63))
    async def test_stale_feed(self):
        with self.assertRaises(ValueError):GoldMonitor().decode(dict(symbol='XAU',price=4000,updatedAt=stamp(-3600)))
    async def test_precision(self):
        price,_=GoldMonitor().decode(dict(symbol='XAU',price=4000.123456,updatedAt=stamp(0)));self.assertAlmostEqual(price,4000.123456)
    async def test_invalid_symbol(self):
        with self.assertRaises(ValueError):GoldMonitor().decode(dict(symbol='XAG',price=4000,updatedAt=stamp(0)))
    async def test_nan_threshold(self):self.assertIn('Usage',GoldMonitor().command('threshold nan'))

class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_sell_http_local_mock(self):
        import aiohttp
        from aiohttp import web
        from aiohttp.test_utils import TestServer
        held=[D(5)];writes=[]
        async def get_market(request):return web.json_response({'market':market()})
        async def get_pos(request):
            self.assertEqual(request.query['ticker'],T);return web.json_response(pos(held[0]))
        async def get_book(request):return web.json_response(book())
        async def post_order(request):
            p=await request.json();writes.append(p);self.assertTrue(p['reduce_only']);self.assertEqual(p['side'],'ask')
            self.assertEqual(p['price'],'0.4200');self.assertEqual(p['count'],'5.00')
            held[0]=D(2);return web.json_response(order(3),status=201)
        app=web.Application();app.router.add_get('/trade-api/v2/markets/'+T,get_market)
        app.router.add_get('/trade-api/v2/portfolio/positions',get_pos)
        app.router.add_get('/trade-api/v2/markets/'+T+'/orderbook',get_book)
        app.router.add_post('/trade-api/v2/portfolio/events/orders',post_order)
        async with TestServer(app) as server:
            b=bot_stub();b.KALSHI_HOST=str(server.make_url('')).rstrip('/');b.build_kalshi_auth_headers=lambda *args:{}
            async with aiohttp.ClientSession() as session:ok,msg=await Execution(b).sell_all(session,T)
            self.assertFalse(ok);self.assertIn('Current signed net position: 2.00',msg)
        self.assertEqual(len(writes),1)
    async def test_dashboard_auth(self):
        import aiohttp
        from aiohttp import web
        from aiohttp.test_utils import TestServer
        from free_gold import dashboard_auth,state_api,command_api
        with patch.dict(os.environ,{'DASHBOARD_TOKEN':'test-secret'}):
            app=web.Application(middlewares=[dashboard_auth]);app.router.add_get('/api/gold',state_api);app.router.add_post('/api/command',command_api)
            async with TestServer(app) as server:
                async with aiohttp.ClientSession() as session:
                    async with session.get(server.make_url('/api/gold')) as r:self.assertEqual(r.status,401)
                    headers={'Authorization':'Bearer test-secret'}
                    async with session.get(server.make_url('/api/gold'),headers=headers) as r:self.assertEqual(r.status,200)
                    async with session.post(server.make_url('/api/command'),headers=headers,json={'command':'help'}) as r:self.assertEqual(r.status,200)
    async def test_integrated_bot(self):
        import bot
        app=bot.create_app();self.assertIn('/dashboard',[r.resource.canonical for r in app.router.routes()])
        self.assertEqual(bot.GOLD_SERIES_TICKERS,['KXGOLD15M','KXGOLDH'])
        ok,msg=await bot.place_kalshi_order(None,T,'YES',45,1);self.assertFalse(ok)
        self.assertIn('select YES or NO',msg);self.assertIsNone(bot.build_keyboard(H,'YES',45))

if __name__=='__main__':unittest.main()
