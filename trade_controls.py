"""Kalshi 15m-only execution. All network writes are gated by bot configuration."""
import asyncio
import html
import time
import uuid
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

D=Decimal
class ExecutionError(Exception): pass

class Execution:
    def __init__(self, bot):
        self.bot=bot
        self.locks={}
        self.uncertain={}

    async def request(self, session, method, suffix, payload=None, params=None):
        path=self.bot.KALSHI_API_PREFIX+suffix
        headers=self.bot.build_kalshi_auth_headers(method,path)
        async with session.request(method,self.bot.KALSHI_HOST+path,headers=headers,
                                   json=payload,params=params,timeout=10) as response:
            text=await response.text()
            if response.status not in (200,201):
                raise ExecutionError(f'Kalshi HTTP {response.status}: {text[:200]}')
            import json
            return json.loads(text)

    async def market(self,session,ticker):
        if not ticker.startswith('KXGOLD15M-') or len(ticker)>128:
            raise ExecutionError('Only KXGOLD15M contracts are eligible. Hourly trading is blocked.')
        data=await self.request(session,'GET','/markets/'+quote(ticker,safe=''))
        m=data.get('market',{})
        if m.get('ticker')!=ticker or not m.get('event_ticker','').startswith('KXGOLD15M-'):
            raise ExecutionError('Market/event identity mismatch')
        if m.get('status') not in ('active','open'):
            raise ExecutionError('This contract is not open for trading')
        from datetime import datetime
        try:
            dt=datetime.fromisoformat(m['close_time'].replace('Z','+00:00'))
            if dt.tzinfo is None or dt.timestamp()<=time.time():
                raise ExecutionError('Contract has closed')
        except (KeyError,ValueError):
            raise ExecutionError('Cannot validate market trading close')
        return m

    async def position(self,session,ticker):
        cursor=None
        found=[]
        seen=set()
        while True:
            params={'ticker':ticker,'count_filter':'position','limit':100}
            if cursor:params['cursor']=cursor
            data=await self.request(session,'GET','/portfolio/positions',params=params)
            found += [p for p in data.get('market_positions',[]) if p.get('ticker')==ticker]
            cursor=data.get('cursor')
            if not cursor:break
            if cursor in seen:raise ExecutionError('Repeated position cursor')
            seen.add(cursor)
            if len(seen)>100:raise ExecutionError('Position pagination limit exceeded')
        if not found:return D('0')
        if len(found)!=1:raise ExecutionError('Multiple position records; refusing ambiguous liquidation')
        p=found[0]
        if p.get('netting_enabled') is not True:
            raise ExecutionError('Non-netted position is unsupported; use Kalshi to close it')
        if 'position_fp' not in p:raise ExecutionError('Missing position_fp')
        value=D(str(p['position_fp']))
        if not value.is_finite() or value!=value.quantize(D('.01')):
            raise ExecutionError('Invalid position precision')
        return value

    async def book(self,session,ticker):
        data=await self.request(session,'GET','/markets/'+quote(ticker,safe='')+'/orderbook')
        ob=data.get('orderbook_fp') or data.get('orderbook') or {}
        def levels(side):
            rows=ob.get(side+'_dollars')
            scale=D('1')
            if rows is None:rows=ob.get(side,[]);scale=D('.01')
            out=[]
            for p,q in rows:
                p,q=D(str(p))*scale,D(str(q))
                if p.is_finite() and q.is_finite() and 0<p<1 and q>0:out.append((p,q))
            return sorted(out,reverse=True)
        return {'yes':levels('yes'),'no':levels('no')}

    def gate(self,ticker):
        if not self.bot.ENABLE_LIVE_ORDERS:raise ExecutionError('Live orders disabled: ENABLE_LIVE_ORDERS=false')
        if not self.bot.KALSHI_KEY_ID or self.bot.PRIVATE_KEY_OBJ is None:raise ExecutionError('Kalshi credentials missing')
        if ticker in self.uncertain:
            raise ExecutionError('Prior submission outcome uncertain. Check Kalshi before further trading: '+self.uncertain[ticker])

    async def submit(self,session,ticker,side,count,price,reduce_only):
        client_id=str(uuid.uuid4())
        payload=dict(ticker=ticker,client_order_id=client_id,side=side,
                     count=f'{count:.2f}',price=f'{price:.4f}',time_in_force='immediate_or_cancel',
                     self_trade_prevention_type='taker_at_cross',reduce_only=reduce_only)
        # Record before sending: timeout/cancellation may occur after exchange acceptance.
        self.uncertain[ticker]=client_id
        try:
            data=await self.request(session,'POST','/portfolio/events/orders',payload=payload)
        except BaseException:
            # No automatic retry, including errors with ambiguous acceptance.
            raise
        try:
            filled=D(str(data['fill_count']))
            if not filled.is_finite() or not 0<=filled<=count:raise ValueError()
            if not data.get('order_id'):raise ValueError()
        except (KeyError,ValueError,InvalidOperation):
            raise ExecutionError('Unrecognized order response; check client_order_id '+client_id)
        self.uncertain.pop(ticker,None)
        return data,filled

    async def sell_all(self,session,ticker):
        lock=self.locks.setdefault(ticker,asyncio.Lock())
        if lock.locked():return False,'An order for this contract is already in progress.'
        async with lock:
            try:
                self.gate(ticker)
                await self.market(session,ticker)
                position=await self.position(session,ticker)
                if not position:return True,'No shares held in this 15-minute contract. No order sent.'
                book=await self.book(session,ticker)
                outcome='YES' if position>0 else 'NO'
                levels=book[outcome.lower()]
                if not levels:return False,f'No executable {outcome} bids. No order sent; all shares remain.'
                bid=levels[0][0]
                # YES liquidation: ask at YES bid. NO liquidation: bid at 1 - NO bid.
                side='ask' if position>0 else 'bid'
                yes_price=bid if position>0 else 1-bid
                requested=abs(position)
                data,filled=await self.submit(session,ticker,side,requested,yes_price,True)
                remaining=requested-filled
                report=(f'<b>15-MINUTE SELL RESULT</b>\n<code>{html.escape(ticker)}</code>\n'
                        f'Sold: {filled:.2f} / {requested:.2f} {outcome}\n'
                        f'Unfilled from this request: {remaining:.2f}\n'
                        f'Limit: at least {bid*100:.2f}¢ per {outcome} share\n'
                        f'Order: <code>{html.escape(str(data["order_id"]))}</code>')
                if data.get('average_fill_price') is not None:
                    avg=D(str(data['average_fill_price']))
                    realized=avg if outcome=='YES' else 1-avg
                    report+=f'\nAverage sale price: {realized*100:.4f}¢ (before fees)'
                try:
                    actual=await self.position(session,ticker)
                    report+=f'\nCurrent signed net position: {actual:.2f}'
                except Exception:
                    report+='\nPosition refresh unavailable; check Kalshi holdings.'
                report+='\nIOC: unfilled shares were not left resting. Existing orders were not canceled.'
                return remaining==0,report
            except Exception as exc:
                extra=('\nSubmission may have reached Kalshi; do not retry before checking account. Client ID: '+self.uncertain[ticker]) if ticker in self.uncertain else ''
                return False,html.escape(str(exc)+extra)

    async def buy(self,session,ticker,outcome,quantity,max_ask=None):
        lock=self.locks.setdefault(ticker,asyncio.Lock())
        if lock.locked():return False,'An order for this contract is already in progress.'
        async with lock:
            try:
                self.gate(ticker)
                if outcome not in ('YES','NO') or quantity not in (1,5,10):raise ExecutionError('Invalid buy choice')
                if not self.bot.MONITOR.fresh():raise ExecutionError('Spot proxy stale; buy blocked')
                await self.market(session,ticker)
                book=await self.book(session,ticker)
                opposite=book['no' if outcome=='YES' else 'yes']
                if not opposite:raise ExecutionError('No current executable ask')
                ask=1-opposite[0][0]
                if max_ask is not None and ask>D(str(max_ask)):
                    raise ExecutionError('Ask moved above the displayed price. Send /predict for a new quote.')
                # Use fresh executable quote without adding a slippage buffer.
                price=ask if outcome=='YES' else 1-ask
                side='bid' if outcome=='YES' else 'ask'
                data,filled=await self.submit(session,ticker,side,D(quantity),price,False)
                return True,(f'<b>15-MINUTE BUY RESULT</b>\n<code>{html.escape(ticker)}</code>\n'
                    f'Filled: {filled:.2f}/{quantity} {outcome}; limit {ask*100:.2f}¢\n'
                    f'Order: <code>{html.escape(str(data["order_id"]))}</code>')
            except Exception as exc:
                extra=(' Client ID: '+self.uncertain[ticker]+'; check Kalshi before retrying.') if ticker in self.uncertain else ''
                return False,html.escape(str(exc)+extra)
