"""Free spot-gold proxy feed and monitoring features. No paid API or orders."""
import os
import math
import time
import json
import asyncio
import statistics
from datetime import datetime, timezone
from collections import deque
from aiohttp import web

URL = 'https://api.gold-api.com/price/XAU'
MAX_AGE = float(os.getenv('GOLD_MAX_AGE_SECONDS', '60'))

class FreeGold:
    def __init__(self):
        self.status='Waiting for Gold-API.com'
        self.publish_time=0.
        self.price=None
        self.rows=[]
        self.threshold=65.
        self.muted=False
        self.alerted={}
        self.armed={}
        self.messages=deque(maxlen=30)
        self.last_scan=0.
        self.last_receipt=0.

    def fresh(self):
        age=time.time()-self.publish_time
        return self.price is not None and -5<=age<=MAX_AGE and time.time()-self.last_receipt<=MAX_AGE

    def decode(self,data,now=None):
        now=time.time() if now is None else now
        if data.get('symbol')!='XAU' or data.get('currency','USD')!='USD':
            raise ValueError('Unexpected symbol/currency')
        price=float(data['price'])
        stamp=datetime.fromisoformat(data['updatedAt'].replace('Z','+00:00'))
        if stamp.tzinfo is None:
            raise ValueError('Timestamp must include timezone')
        ts=stamp.timestamp()
        if not math.isfinite(price) or price<=0:
            raise ValueError('Invalid gold price')
        if not -5<=now-ts<=MAX_AGE:
            raise ValueError('Stale/future provider timestamp')
        if ts<self.publish_time:
            raise ValueError('Out-of-order provider timestamp')
        return price,ts

    async def fetch(self,session,state):
        try:
            async with session.get(URL,timeout=6,headers={'Accept':'application/json'}) as response:
                response.raise_for_status()
                data=await response.json()
            price,ts=self.decode(data)
            is_new=ts>self.publish_time
            self.price,self.publish_time=price,ts
            self.last_receipt=time.time()
            self.status='Live spot proxy (not Pyth settlement index)'
            state.spot_source='Gold-API.com XAU/USD — proxy, not Pyth'
            state.latest_spot=price
            state.latest_spot_ts=ts
            if is_new:
                if getattr(self,'previous_ts',0) and ts-self.previous_ts>120:
                    state.candles_1m=type(state.candles_1m)(60,max_candles=120)
                    state.candles_5m=type(state.candles_5m)(300,max_candles=120)
                state.candles_1m.update(price,ts)
                state.candles_5m.update(price,ts)
                self.previous_ts=ts
            return price
        except Exception as exc:
            self.status='Feed unavailable: '+str(exc)
            # Fail closed immediately after any failed poll; retain last price for display only.
            self.last_receipt=0.
            return None

    def model(self,state,minutes):
        candles=list(state.candles_1m.candles)[-60:]
        if not self.fresh() or len(candles)<20:
            return None
        if any(b['timestamp']-a['timestamp']!=60 for a,b in zip(candles,candles[1:])):
            return None
        returns=[math.log(c['close']/c['open']) for c in candles]
        sigma=max(statistics.pstdev(returns),1e-7)
        n=min(len(candles),minutes)
        delta=math.log(self.price/candles[-n]['open'])
        gauge=100*math.tanh(delta/(2*sigma*math.sqrt(n)))
        drift=statistics.mean(returns[-10:])
        return gauge,drift,sigma

    async def refresh(self,session,bot):
        markets=await bot.fetch_kalshi_gold_markets(session)
        bot.STATE.active_markets_count=len(markets)
        rows=[]
        for minutes,series in ((15,'KXGOLD15M'),(60,'KXGOLDH')):
            model=self.model(bot.STATE,minutes)
            group=[m for m in markets if m.get('series_ticker')==series]
            # Always show the two gauges, even when no contract is returned.
            if not group:
                group=[{'ticker':None,'series_ticker':series}]
            for market in group:
                ticker=market.get('ticker')
                row=dict(timeframe=f'{minutes}m',ticker=ticker,momentum=model[0] if model else None,
                    projected_spot=None,yes_score=None,above_target_score=None,target=None,
                    forecast_basis='Free spot proxy, not Pyth settlement index',
                    reason='Warm-up (20 completed 1m candles), stale data, or gaps' if not model else '',
                    horizon_basis='Approximate market expiry; not a verified settlement observation',
                    quotes=None,official_result=market.get('result'),updated_at=time.time())
                expiry=market.get('expected_expiration_time') or market.get('expiration_time')
                kind=market.get('strike_type')
                key='cap_strike' if kind in ('less','less_or_equal') else 'floor_strike'
                try:
                    strike=float(market[key])
                    row['target']=strike
                except (KeyError,TypeError,ValueError):
                    strike=None
                remaining=minutes
                if expiry:
                    try:
                        dt=datetime.fromisoformat(expiry.replace('Z','+00:00'))
                        if dt.tzinfo is None: raise ValueError()
                        remaining=(dt.timestamp()-time.time())/60
                    except (TypeError,ValueError):
                        expiry=None
                if model and remaining>0:
                    gauge,drift,sigma=model
                    projected=self.price*math.exp(max(-.1,min(.1,drift*remaining)))
                    row['projected_spot']=projected
                    row['minutes_left']=remaining
                    if not expiry:
                        row['horizon_basis']=f'Rolling {minutes}-minute horizon, not contract expiry'
                    if strike and math.isfinite(strike) and strike>0:
                        z=math.log(projected/strike)/(sigma*math.sqrt(remaining))
                        p=.5*(1+math.erf(z/math.sqrt(2)))
                        row['above_target_score']=round(p*100,2)
                        # Only map to indicative YES when the threshold direction is explicit.
                        if kind in ('greater','greater_or_equal','less','less_or_equal'):
                            row['yes_score']=round(100*(1-p if kind.startswith('less') else p),2)
                        else:
                            row['reason']='Unknown comparison: showing above-target score only'
                if ticker:
                    row['quotes']=await bot.fetch_orderbook_details(session,ticker)
                rows.append(row)
            if model:
                g=model[0]
                if abs(g)<self.threshold-10:
                    self.armed[series]=True
                if (not self.muted and abs(g)>=self.threshold and self.armed.get(series,True)
                        and time.time()-self.alerted.get(series,0)>bot.ALERT_COOLDOWN_SECONDS):
                    self.armed[series]=False
                    self.alerted[series]=time.time()
                    message=f'{minutes}m spot-proxy momentum {g:+.1f}. Not a Pyth settlement prediction.'
                    self.messages.append(message)
                    bot.logger.info(message)
                    await bot.send_telegram_message(session,message)
        self.rows=rows
        self.last_scan=time.time()

    def snapshot(self):
        fresh=self.fresh()
        rows=[]
        for original in self.rows:
            row=dict(original)
            if not fresh or time.time()-row['updated_at']>45:
                row.update(momentum=None,projected_spot=None,yes_score=None,above_target_score=None,reason='Stale data/scan')
            rows.append(row)
        return dict(provider='Gold-API.com',source_url=URL,status=self.status,fresh=fresh,
            price=self.price,publish_time=self.publish_time,quote_age_seconds=max(0,time.time()-self.publish_time) if self.publish_time else None,
            settlement_source='Kalshi official result; underlying follows each contract rules',
            proxy_warning='Spot forecasts are not calibrated probabilities or exact Pyth index forecasts',
            threshold=self.threshold,muted=self.muted,markets=rows,messages=list(self.messages))

    def command(self,text):
        parts=text.strip().lower().lstrip('/').split()
        if not parts:return 'Empty command'
        cmd=parts[0].split('@')[0]
        if cmd in ('mute','unmute'):
            self.muted=cmd=='mute'
            return 'Alerts muted' if self.muted else 'Alerts enabled'
        if cmd=='threshold':
            try:
                value=float(parts[1])
                if not 10<=value<=95:raise ValueError()
                self.threshold=value
                return f'Threshold: {value}'
            except (ValueError,IndexError):return 'Usage: threshold 10–95'
        if cmd=='feed':return json.dumps(self.snapshot(),indent=2)
        if cmd in ('gauge','predict','status'):
            snap=self.snapshot()
            lines=[f"Gold-API.com | {self.status} | fresh={snap['fresh']} | spot={self.price}",'SPOT PROXY ONLY — official results come from Kalshi']
            for row in snap['markets']:
                gauge=row['momentum'];bar=['-']*41;bar[20]='|'
                if gauge is not None:bar[max(0,min(40,round((gauge+100)/5))) ]='*'
                lines.append(f"{row['timeframe']} {row['ticker'] or 'no open contract'} -[{''.join(bar)}]+ {gauge}")
                lines.append(f"  projected spot={row['projected_spot']} | indicative YES={row['yes_score']}/100 | {row['reason']}")
            return '\n'.join(lines)
        return 'gauge | predict | feed | mute | unmute | threshold 65 | status | help'

MONITOR=FreeGold()

HTML = """<!doctype html><meta charset=utf-8><title>Free gold monitor</title>
<style>body{background:#15202e;color:#eee;font:16px system-ui;max-width:1000px;margin:30px auto;padding:15px}article{background:#253446;padding:20px;margin:15px 0}.bar{position:relative;height:20px;background:linear-gradient(90deg,#c44,#777,#3a7)}.needle{position:absolute;top:-5px;height:30px;width:4px;background:white}pre{white-space:pre-wrap}</style>
<h1>Free spot-gold / Kalshi monitor</h1><p>Proxy forecasts, not exact Pyth prices. Official settlement comes from Kalshi.</p><p id=state></p><div id=rows></div><form id=form><input id=cmd placeholder="predict / mute / threshold 65"><button>Command</button></form><pre id=reply></pre>
<script>const $=s=>document.querySelector(s);const token=sessionStorage.getItem('gold_token')||prompt('Dashboard token (blank if local)')||'';sessionStorage.setItem('gold_token',token);const headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'};
function esc(x){let e=document.createElement('span');e.textContent=x??'unavailable';return e.innerHTML;}
async function update(){try{let r=await fetch('/api/gold',{headers});if(!r.ok)throw Error(r.status);let s=await r.json();$('#state').textContent=s.status+' | '+s.price+' | quote age '+s.quote_age_seconds+'s';$('#rows').innerHTML=s.markets.map(m=>`<article><h2>${esc(m.timeframe)} ${esc(m.ticker)}</h2><div class=bar>${m.momentum===null?'':`<i class=needle style="left:${(m.momentum+100)/2}%"></i>`}</div><p>−100 ← ${esc(m.momentum)} → +100</p><p>Projected spot: ${esc(m.projected_spot)} | indicative YES score: ${esc(m.yes_score)}/100</p><p>Target ${esc(m.target)} | ${esc(m.horizon_basis)}</p><p>Kalshi quotes: ${esc(JSON.stringify(m.quotes))}</p><p>${esc(m.reason)}</p></article>`).join('');}catch(e){$('#state').textContent='Disconnected: '+e.message;}}
$('#form').onsubmit=async e=>{e.preventDefault();try{let r=await fetch('/api/command',{method:'POST',headers,body:JSON.stringify({command:$('#cmd').value})});$('#reply').textContent=await r.text();}catch(e){$('#reply').textContent=e.message;}};update();setInterval(update,2000);</script>"""

async def terminal_commands():
    import sys
    loop=asyncio.get_running_loop();queue=asyncio.Queue()
    def read():
        line=sys.stdin.readline()
        if line:queue.put_nowait(line)
        else:loop.remove_reader(sys.stdin)
    try:loop.add_reader(sys.stdin,read)
    except (OSError,ValueError,NotImplementedError):return
    try:
        while True:print(MONITOR.command(await queue.get()),flush=True)
    finally:loop.remove_reader(sys.stdin)

@web.middleware
async def dashboard_auth(request,handler):
    import hmac
    if request.path not in ('/dashboard','/api/gold','/api/command'):return await handler(request)
    token=os.getenv('DASHBOARD_TOKEN','')
    if token:
        if request.path!='/dashboard' and not hmac.compare_digest(request.headers.get('Authorization',''),'Bearer '+token):
            raise web.HTTPUnauthorized()
    elif request.remote not in ('127.0.0.1','::1'):
        raise web.HTTPForbidden(text='Configure DASHBOARD_TOKEN for remote dashboard access')
    if request.method=='POST':
        origin=request.headers.get('Origin')
        if origin and origin!=f'{request.scheme}://{request.host}':raise web.HTTPForbidden()
    return await handler(request)

async def dashboard(request):return web.Response(text=HTML,content_type='text/html')
async def state_api(request):return web.json_response(MONITOR.snapshot(),headers={'Cache-Control':'no-store'})
async def command_api(request):
    try:
        if request.content_type!='application/json':raise ValueError('JSON required')
        command=(await request.json())['command']
        if not isinstance(command,str) or len(command)>200:raise ValueError('Invalid command')
        return web.Response(text=MONITOR.command(command))
    except (ValueError,KeyError,TypeError) as exc:return web.Response(status=400,text=str(exc))
