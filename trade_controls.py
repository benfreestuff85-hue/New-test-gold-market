"""15-minute-only Kalshi execution. No autonomous orders."""
import asyncio
import html
import json
import time
import uuid
from datetime import datetime
from decimal import Decimal as D
from urllib.parse import quote

class TradeError(Exception): pass

class Execution:
    def __init__(self,bot):
        self.bot=bot
        self.locks={}
        self.uncertain={}

    async def request(self,session,method,suffix,payload=None,params=None):
        path=self.bot.KALSHI_API_PREFIX+suffix
        headers=self.bot.build_kalshi_auth_headers(method,path)
        async with session.request(method,self.bot.KALSHI_HOST+path,headers=headers,
                                   json=payload,params=params,timeout=10) as response:
            body=await response.text()
            if response.status not in (200,201):raise TradeError(f'Kalshi HTTP {response.status}: {body[:200]}')
            return json.loads(body)

    def gate(self,ticker):
        if not self.bot.ENABLE_LIVE_ORDERS:raise TradeError('Live orders disabled: ENABLE_LIVE_ORDERS=false')
        if not self.bot.KALSHI_KEY_ID or self.bot.PRIVATE_KEY_OBJ is None:raise TradeError('Kalshi credentials missing')
        if ticker in self.uncertain:raise TradeError('Submission uncertain. Check Kalshi client ID '+self.uncertain[ticker]+' before trading again.')

    async def market(self,session,ticker):
        if not ticker.startswith('KXGOLD15M-') or len(ticker)>128:raise TradeError('Only KXGOLD15M contracts are tradable with these controls')
        data=await self.request(session,'GET','/markets/'+quote(ticker,safe=''))
        m=data.get('market',{})
        if m.get('ticker')!=ticker or not m.get('event_ticker','').startswith('KXGOLD15M-'):raise TradeError('Market/event mismatch')
        if m.get('status') not in ('active','open'):raise TradeError('Market is not trading')
        try:
            opened=datetime.fromisoformat(m['open_time'].replace('Z','+00:00'))
            close=datetime.fromisoformat(m['close_time'].replace('Z','+00:00'))
            if opened.tzinfo is None or close.tzinfo is None or not opened.timestamp()<=time.time()<close.timestamp():raise ValueError()
        except (KeyError,TypeError,ValueError):raise TradeError('Cannot validate trading window')
        return m

    async def position(self,session,ticker):
        cursor=None;seen=set();found=[]
        for _ in range(100):
            params=dict(ticker=ticker,count_filter='position',limit=100)
            if cursor:params['cursor']=cursor
            data=await self.request(session,'GET','/portfolio/positions',params=params)
            found.extend(p for p in data.get('market_positions',[]) if p.get('ticker')==ticker)
            cursor=data.get('cursor')
            if not cursor:break
            if cursor in seen:raise TradeError('Repeated position cursor')
            seen.add(cursor)
        else:raise TradeError('Position pagination limit exceeded')
        if not found:return D('0')
        if len(found)!=1:raise TradeError('Ambiguous position records')
        p=found[0]
        if p.get('netting_enabled') is not True:raise TradeError('Unsupported or unknown netting mode')
        if 'position_fp' not in p:raise TradeError('Missing position_fp')
        value=D(str(p['position_fp']))
        if not value.is_finite() or value!=value.quantize(D('.01')):raise TradeError('Invalid position precision')
        return value

    async def book(self,session,ticker):
        data=await self.request(session,'GET','/markets/'+quote(ticker,safe='')+'/orderbook')
        ob=data.get('orderbook_fp') or data.get('orderbook') or {}
        def levels(side):
            rows=ob.get(side+'_dollars');scale=D('1')
            if rows is None:rows=ob.get(side,[]);scale=D('.01')
            out=[]
            for price,qty in rows:
                price,qty=D(str(price))*scale,D(str(qty))
                if price.is_finite() and qty.is_finite() and 0<price<1 and qty>0:out.append((price,qty))
            return sorted(out,reverse=True)
        return dict(yes=levels('yes'),no=levels('no'))

    async def submit(self,session,ticker,side,count,price,reduce_only):
        client=str(uuid.uuid4())
        payload=dict(ticker=ticker,client_order_id=client,side=side,count=f'{count:.2f}',price=f'{price:.4f}',
                     reduce_only=reduce_only,time_in_force='immediate_or_cancel',self_trade_prevention_type='taker_at_cross')
        self.uncertain[ticker]=client
        # Acceptance may precede a timeout. Never auto-retry a POST.
        result=await self.request(session,'POST','/portfolio/events/orders',payload=payload)
        try:
            filled=D(str(result['fill_count']))
            if not result.get('order_id') or not filled.is_finite() or not 0<=filled<=count:raise ValueError()
        except (KeyError,ValueError):raise TradeError('Unrecognized response; check client ID '+client)
        self.uncertain.pop(ticker,None)
        return result,filled

    def failure(self,ticker,exc):
        extra=(' Submission may have reached Kalshi. Inspect client ID '+self.uncertain[ticker]+' before retrying.') if ticker in self.uncertain else ''
        return False,html.escape(str(exc)+extra)

    async def sell_all(self,session,ticker):
        lock=self.locks.setdefault(ticker,asyncio.Lock())
        if lock.locked():return False,'An order is already in progress for this contract.'
        async with lock:
            try:
                self.gate(ticker);await self.market(session,ticker)
                position=await self.position(session,ticker)
                if not position:return True,'No shares held in this 15-minute contract. No order sent.'
                book=await self.book(session,ticker);outcome='YES' if position>0 else 'NO'
                if not book[outcome.lower()]:return False,f'No executable {outcome} bids. No order sent.'
                bid=book[outcome.lower()][0][0]
                side='ask' if position>0 else 'bid';price=bid if position>0 else 1-bid
                count=abs(position)
                result,filled=await self.submit(session,ticker,side,count,price,True)
                text=(f'<b>15-MINUTE SELL RESULT</b>\n<code>{html.escape(ticker)}</code>\n'
                    f'Sold: {filled:.2f}/{count:.2f} {outcome}\nUnfilled requested shares: {count-filled:.2f}\n'
                    f'Minimum sale price: {bid*100:.2f}¢ before fees\nOrder: <code>{html.escape(str(result["order_id"]))}</code>')
                if result.get('average_fill_price') is not None:
                    avg=D(str(result['average_fill_price']));avg=avg if outcome=='YES' else 1-avg
                    text+=f'\nAverage sale price: {avg*100:.4f}¢ before fees'
                try:text+=f'\nCurrent signed net position: {await self.position(session,ticker):.2f}'
                except Exception:text+='\nPosition refresh unavailable. Check Kalshi holdings.'
                text+='\nUnfilled IOC shares are not left resting. Existing orders were not canceled.'
                return filled==count,text
            except Exception as exc:return self.failure(ticker,exc)

    async def buy(self,session,ticker,outcome,quantity,max_ask):
        lock=self.locks.setdefault(ticker,asyncio.Lock())
        if lock.locked():return False,'An order is already in progress for this contract.'
        async with lock:
            try:
                self.gate(ticker)
                if outcome not in ('YES','NO') or quantity not in (1,5,10):raise TradeError('Invalid buy choice')
                if max_ask is None:raise TradeError('Select a side with an available quote first')
                cap=D(str(max_ask))
                if not cap.is_finite() or not 0<cap<1:raise TradeError('Invalid price cap')
                if not self.bot.MONITOR.fresh():raise TradeError('Spot proxy stale; buy blocked')
                await self.market(session,ticker)
                book=await self.book(session,ticker);opposite=book['no' if outcome=='YES' else 'yes']
                if not opposite:raise TradeError('No executable ask')
                ask=1-opposite[0][0]
                if ask>cap:raise TradeError('Ask moved above displayed price; send /predict or reselect side')
                side='bid' if outcome=='YES' else 'ask';price=ask if outcome=='YES' else 1-ask
                result,filled=await self.submit(session,ticker,side,D(quantity),price,False)
                return True,(f'<b>15-MINUTE BUY RESULT</b>\n<code>{html.escape(ticker)}</code>\n'
                    f'Filled: {filled:.2f}/{quantity} {outcome}; limit {ask*100:.2f}¢\n'
                    f'Order: <code>{html.escape(str(result["order_id"]))}</code>')
            except Exception as exc:return self.failure(ticker,exc)
