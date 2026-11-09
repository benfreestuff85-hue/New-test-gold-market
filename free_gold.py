"""Free XAU/USD feed; exactly one forecast for each timeframe."""
import asyncio
import html
import json
import math
import os
import secrets
import statistics
import time
from datetime import datetime,timezone
from aiohttp import web

URL='https://api.gold-api.com/price/XAU'
MAX_AGE=float(os.getenv('GOLD_MAX_AGE_SECONDS','60'))

def timestamp(value):
    dt=datetime.fromisoformat(value.replace('Z','+00:00'))
    if dt.tzinfo is None:raise ValueError('Timezone missing')
    return dt.timestamp()

def gauge(value,width=8):
    if value is None:return '− ['+'░'*width+'│'+'░'*width+'] +  N/A'
    value=max(-100,min(100,value))
    n=round(abs(value)*width/100)
    left='░'*(width-n)+'█'*n if value<0 else '░'*width
    right='█'*n+'░'*(width-n) if value>0 else '░'*width
    return f'− [{left}│{right}] + {value:+.0f}'

def expiry(m):
    for field in ('expected_expiration_time','expiration_time','close_time'):
        try:return timestamp(m[field])
        except (KeyError,TypeError,ValueError):pass
    return None

def select_one(markets,series,now,spot=None):
    candidates=[]
    for m in markets:
        if m.get('series_ticker')!=series or m.get('status') not in ('active','open'):continue
        end=expiry(m)
        if end is None or end<=now:continue
        try:
            if timestamp(m['open_time'])>now or timestamp(m['close_time'])<=now:continue
        except (KeyError,ValueError,TypeError):continue
        strike=m.get('floor_strike',m.get('cap_strike'))
        try:distance=abs(float(strike)-spot) if spot else 0
        except (TypeError,ValueError):distance=float('inf')
        candidates.append((end,distance,m.get('ticker',''),m))
    return min(candidates,key=lambda v:v[:3])[3] if candidates else None

class GoldMonitor:
    def __init__(self):
        self.price=None;self.publish_time=0.;self.received=0.;self.previous_ts=0.
        self.status='Waiting for free gold feed';self.rows=[];self.updated=0.
        self.muted=False;self.threshold=65.;self.last_alert=0.;self.armed=True
        self.contexts={};self.used=set();self.executed_updates=set()

    def fresh(self):
        return (self.price is not None and -5<=time.time()-self.publish_time<=MAX_AGE
                and time.time()-self.received<=MAX_AGE)

    def decode(self,data):
        if data.get('symbol')!='XAU' or data.get('currency','USD')!='USD':raise ValueError('Unexpected symbol/currency')
        price=float(data['price']);ts=timestamp(data['updatedAt'])
        if not math.isfinite(price) or price<=0:raise ValueError('Invalid price')
        if not -5<=time.time()-ts<=MAX_AGE:raise ValueError('Stale provider timestamp')
        if ts<self.publish_time:raise ValueError('Out-of-order timestamp')
        return price,ts

    async def fetch(self,session,state):
        try:
            async with session.get(URL,timeout=6) as r:
                r.raise_for_status();data=await r.json()
            price,ts=self.decode(data)
            if ts>self.publish_time:
                if self.previous_ts and ts-self.previous_ts>120:
                    state.candles_1m=type(state.candles_1m)(60,max_candles=120)
                    state.candles_5m=type(state.candles_5m)(300,max_candles=120)
                state.candles_1m.update(price,ts);state.candles_5m.update(price,ts)
                self.previous_ts=ts
            self.price=price;self.publish_time=ts;self.received=time.time()
            state.latest_spot=price;state.latest_spot_ts=ts
            state.spot_source='Gold-API.com spot proxy (not Pyth index)'
            self.status='Fresh free spot proxy'
            return price
        except Exception as exc:
            self.received=0.;self.status='Feed unavailable: '+str(exc)
            return None

    def model(self,state,minutes):
        candles=list(state.candles_1m.candles)[-60:]
        if not self.fresh() or len(candles)<20:return None
        if any(b['timestamp']-a['timestamp']!=60 for a,b in zip(candles,candles[1:])):return None
        returns=[math.log(c['close']/c['open']) for c in candles]
        vol=max(statistics.pstdev(returns),1e-7)
        n=min(minutes,len(candles))
        delta=math.log(self.price/candles[-n]['open'])
        return 100*math.tanh(delta/(2*vol*math.sqrt(n))),statistics.mean(returns[-10:]),vol

    async def refresh(self,session,bot):
        markets=await bot.fetch_kalshi_gold_markets(session)
        bot.STATE.active_markets_count=len(markets)
        rows=[];now=time.time()
        for minutes,series in ((15,'KXGOLD15M'),(60,'KXGOLDH')):
            market=select_one(markets,series,now,self.price)
            model=self.model(bot.STATE,minutes)
            end=expiry(market) if market else None
            row=dict(minutes=minutes,ticker=market.get('ticker') if market else None,
                momentum=model[0] if model else None,projected_price=None,yes_score=None,
                prediction=None,target=None,seconds_left=max(0,int(end-now)) if end else None,
                outcome=None,reason='',quotes=None,updated_at=now)
            if not market:row['reason']='No active contract found'
            elif not model:row['reason']='Warm-up (20 minute candles), stale feed or history gap'
            else:
                horizon=(end-now)/60
                g,drift,vol=model
                predicted=self.price*math.exp(max(-.1,min(.1,drift*horizon)))
                row['projected_price']=predicted
                if minutes==15:
                    strike=bot.parse_strike(market)
                    kind=market.get('strike_type')
                    row['target']=strike
                    if strike and kind in ('greater','greater_or_equal','less','less_or_equal'):
                        p=.5*(1+math.erf(math.log(predicted/strike)/(vol*math.sqrt(horizon)*math.sqrt(2))))
                        if kind.startswith('less'):p=1-p
                        row['yes_score']=round(p*100,1)
                        row['prediction']='YES' if p>=.5 else 'NO'
                        row['outcome']=row['prediction']
                    else:row['reason']='Target/comparison missing: YES/NO unavailable'
            if market and minutes==15:
                row['quotes']=await bot.fetch_orderbook_details(session,market['ticker'])
            rows.append(row)
        self.rows=rows;self.updated=now
        self.contexts={k:v for k,v in self.contexts.items() if now-v['created']<1800}

    def snapshot(self):
        rows=[]
        for original in self.rows:
            row=dict(original)
            if not self.fresh() or time.time()-self.updated>45 or (row['seconds_left'] is not None and time.time()-self.updated>=row['seconds_left']):
                row.update(momentum=None,projected_price=None,yes_score=None,prediction=None,outcome=None,reason='Stale/unavailable data')
            elif row['seconds_left'] is not None:
                row['seconds_left']=max(0,row['seconds_left']-int(time.time()-self.updated))
            rows.append(row)
        return dict(provider='Gold-API.com',status=self.status,fresh=self.fresh(),price=self.price,
            quote_age=round(time.time()-self.publish_time,1) if self.publish_time else None,
            proxy_warning='Not exact Pyth index; scores uncalibrated; Kalshi reports official settlement',
            muted=self.muted,threshold=self.threshold,markets=rows)

    def report(self):
        snap=self.snapshot()
        price=f'${self.price:,.2f}' if self.price else 'unavailable'
        lines=['<b>GOLD FINISH FORECAST</b>',f'<b>Current spot:</b> {price}']
        for minutes in (15,60):
            row=next((r for r in snap['markets'] if r['minutes']==minutes),None)
            lines+=['',f'<b>{"15-MINUTE" if minutes==15 else "1-HOUR"} MARKET</b>']
            if not row:lines+=['Waiting for first scan'];continue
            projected=f"${row['projected_price']:,.2f}" if row['projected_price'] is not None else 'unavailable'
            lines+=[f'<b>Projected finish:</b> {projected}',f'<code>{gauge(row["momentum"])}</code>']
            if minutes==15:
                if row['target'] is not None:lines+=[f'<b>Target:</b> ${row["target"]:,.2f}']
                prediction=row['prediction'] or 'unavailable'
                lines+=[f'<b>Prediction:</b> {prediction}']
                if row['yes_score'] is not None:
                    score=row['yes_score'] if prediction=='YES' else 100-row['yes_score']
                    lines+=[f'<b>Model score for {prediction}:</b> {score:.1f}/100']
                if row['ticker']:lines+=[f'<b>Contract:</b> <code>{html.escape(row["ticker"])}</code>']
            seconds=row['seconds_left']
            if seconds is not None:lines+=[f'<b>Time remaining:</b> {seconds//60}m {seconds%60:02d}s']
            if row['reason']:lines+=[html.escape(row['reason'])]
        lines+=['','<i>Free spot proxy—not exact Pyth settlement. Uncalibrated forecast.</i>']
        return '\n'.join(lines)

    def keyboard(self):
        row=next((r for r in self.snapshot()['markets'] if r['minutes']==15),None)
        if not row or not row['ticker'] or not row['ticker'].startswith('KXGOLD15M-'):return None
        token=secrets.token_hex(6)
        ask=None
        if row['outcome'] and row.get('quotes'):
            ask=row['quotes'].get('yes_ask' if row['outcome']=='YES' else 'no_ask')
        self.contexts[token]=dict(ticker=row['ticker'],outcome=row['outcome'],created=time.time(),max_ask=ask/100 if ask is not None else None)
        buttons=[[dict(text='SELL ALL — 15M CONTRACT',callback_data=f'G2|S|{token}')]]
        if row['outcome'] and ask is not None:
            buttons.append([dict(text=f'Buy {n} {row["outcome"]} @ {ask:.1f}¢',callback_data=f'G2|B|{token}|{n}') for n in (1,5,10)])
        return dict(inline_keyboard=buttons)

    async def callback(self,session,bot,query):
        chat=str(query.get('message',{}).get('chat',{}).get('id',''))
        user=str(query.get('from',{}).get('id',''))
        owner=os.getenv('TELEGRAM_USER_ID','').strip() or bot.TELEGRAM_CHAT_ID
        if not bot.is_authorized(chat) or user!=owner:return
        # Consume once before awaits: duplicate Telegram updates cannot submit twice.
        qid=str(query.get('id',''))
        if not qid or qid in self.executed_updates:return
        self.executed_updates.add(qid)
        data=query.get('data','')
        parts=data.split('|')
        try:
            async with session.post(f'https://api.telegram.org/bot{bot.TELEGRAM_TOKEN}/answerCallbackQuery',
                json={'callback_query_id':qid,'text':'Checking current contract and holdings…'},timeout=5) as response:
                await response.read()
        except Exception:pass
        if len(parts)<3 or parts[0]!='G2':
            await bot.send_telegram_message(session,'Old buttons disabled. Send /predict for current 15-minute controls.',chat);return
        action,token=parts[1:3];context=self.contexts.get(token)
        if not context:
            await bot.send_telegram_message(session,'Buttons expired after restart or timeout. Send /predict.',chat);return
        age=time.time()-context['created']
        action_key=(token,action)
        if action_key in self.used:
            await bot.send_telegram_message(session,'This action was already used. Send /predict to refresh.',chat);return
        if action=='B' and (age>90 or len(parts)!=4 or not context['outcome']):
            await bot.send_telegram_message(session,'Buy quote expired. Send /predict for a fresh forecast.',chat);return
        if action=='S' and age>1800:
            await bot.send_telegram_message(session,'Sell button expired. Send /predict.',chat);return
        if action not in ('B','S'):return
        self.used.add(action_key)
        if action=='S':ok,message=await bot.EXECUTION.sell_all(session,context['ticker'])
        else:
            try:n=int(parts[3])
            except ValueError:return
            ok,message=await bot.EXECUTION.buy(session,context['ticker'],context['outcome'],n,max_ask=context.get('max_ask'))
        await bot.send_telegram_message(session,message,chat)

    async def send_prediction(self,session,bot,chat=None):
        await self.refresh(session,bot)
        await bot.send_telegram_message(session,self.report(),chat,reply_markup=self.keyboard())

    async def maybe_alert(self,session,bot):
        if self.muted or not self.fresh():return
        scores=[abs(r['momentum']) for r in self.rows if r['momentum'] is not None]
        strength=max(scores,default=0)
        if strength<self.threshold-10:self.armed=True
        # Preserve reversal trigger/edge checks, but only the selected 15m may carry trades.
        reversal=False
        row=next((r for r in self.rows if r['minutes']==15),None)
        if row and row['outcome'] and row['quotes'] and row['yes_score'] is not None:
            sig=bot.detect_reversal_signal(bot.STATE.candles_1m,'1m')
            if sig['direction']=='NEUTRAL':sig=bot.detect_reversal_signal(bot.STATE.candles_5m,'5m')
            outcome='YES' if sig['direction']=='BULLISH' else 'NO' if sig['direction']=='BEARISH' else None
            ob=row['quotes'];ask=ob.get('yes_ask' if outcome=='YES' else 'no_ask')
            fair=row['yes_score'] if outcome=='YES' else 100-row['yes_score']
            if (outcome and outcome==row['outcome'] and ask is not None and ob['spread']<=bot.MAX_SPREAD_CENTS):
                edge=fair-ask-bot.kalshi_taker_fee_cents(ask)
                reversal=edge>=bot.MIN_NET_EDGE_CENTS and 8<=ask<=85
        now=time.time()
        if now-self.last_alert<bot.ALERT_COOLDOWN_SECONDS:return
        if not ((strength>=self.threshold and self.armed) or reversal):return
        self.last_alert=now;self.armed=False
        await bot.send_telegram_message(session,self.report(),reply_markup=self.keyboard())
        if reversal:
            m=next((r for r in self.rows if r['minutes']==15),None)
            close=datetime.fromtimestamp(now+m['seconds_left'],timezone.utc).isoformat()
            bot.STATE.tracker.log_signal(m['ticker'],m['outcome'],ask,m['target'],edge,close)

    def command(self,text):
        parts=text.strip().lower().lstrip('/').split()
        if not parts:return 'Empty command'
        cmd=parts[0].split('@')[0]
        if cmd in ('mute','unmute'):
            self.muted=cmd=='mute';return 'Alerts muted' if self.muted else 'Alerts enabled'
        if cmd=='threshold':
            try:
                value=float(parts[1])
                if not 10<=value<=95:raise ValueError()
                self.threshold=value;return f'Threshold {value}'
            except (IndexError,ValueError):return 'Usage: threshold 10–95'
        if cmd=='feed':return f'{self.status}; fresh={self.fresh()}; age={self.snapshot()["quote_age"]}s; free spot proxy'
        if cmd=='gauge':return '\n'.join(f'{r["minutes"]}m {gauge(r["momentum"])}' for r in self.snapshot()['markets']) or 'Waiting for scan'
        if cmd in ('predict','status'):
            import re
            return html.unescape(re.sub('<[^>]+>','',self.report()))
        return '/predict /gauge /feed /mute /unmute /threshold 65 /status /stats /balance /markets'

MONITOR=GoldMonitor()

async def terminal_commands():
    import sys
    loop=asyncio.get_running_loop();queue=asyncio.Queue()
    def ready():
        line=sys.stdin.readline()
        if line:queue.put_nowait(line)
        else:loop.remove_reader(sys.stdin)
    try:loop.add_reader(sys.stdin,ready)
    except (OSError,ValueError,NotImplementedError):return
    try:
        while True:print(MONITOR.command(await queue.get()),flush=True)
    finally:loop.remove_reader(sys.stdin)

HTML=r"""<!doctype html><meta charset=utf-8><title>Gold forecasts</title><style>body{font:16px system-ui;background:#14202e;color:#eee;max-width:900px;margin:30px auto;padding:15px}pre{white-space:pre-wrap}article{background:#25374a;padding:18px;margin:15px 0}.g{position:relative;width:240px;height:18px;border:1px solid #aaa}.center{position:absolute;left:50%;height:100%;border-left:1px solid white}.fill{position:absolute;height:100%}input,button{padding:8px}</style><h1>Gold finish forecast</h1><p>Free spot proxy; official settlement from Kalshi. Trading controls are Telegram-only.</p><p id=status></p><div id=rows></div><form id=f><input id=cmd placeholder="predict / gauge / feed"><button>Command</button></form><pre id=reply></pre><script>
const $=s=>document.querySelector(s);const token=sessionStorage.getItem('gold_token')||prompt('Dashboard token')||'';sessionStorage.setItem('gold_token',token);const headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'};function esc(x){const e=document.createElement('span');e.textContent=x??'unavailable';return e.innerHTML;}
async function update(){try{let r=await fetch('/api/gold',{headers});if(!r.ok)throw Error(r.status);let s=await r.json();$('#status').textContent=s.status+' | price '+s.price+' | age '+s.quote_age+'s';$('#rows').innerHTML=s.markets.map(m=>{let g=m.momentum||0;let width=Math.abs(g)/2;let left=g<0?50-width:50;return `<article><h2>${m.minutes===15?'15-minute':'1-hour'}</h2><p>Projected finish: ${esc(m.projected_price)}</p><div class=g><span class=fill style="left:${left}%;width:${width}%;background:${g<0?'#d66':'#5b8'}"></span><span class=center></span></div><p>−100 ← ${esc(m.momentum)} → +100</p>${m.minutes===15?`<p>Target: ${esc(m.target)} | prediction: ${esc(m.prediction)}</p>`:''}<p>Seconds remaining: ${esc(m.seconds_left)}</p><p>${esc(m.reason)}</p></article>`}).join('');}catch(e){$('#status').textContent='Disconnected: '+e.message;}}$('#f').onsubmit=async e=>{e.preventDefault();const r=await fetch('/api/command',{method:'POST',headers,body:JSON.stringify({command:$('#cmd').value})});$('#reply').textContent=await r.text();};update();setInterval(update,2000);</script>"""

@web.middleware
async def dashboard_auth(request,handler):
    if request.path not in ('/dashboard','/api/gold','/api/command'):return await handler(request)
    import hmac
    token=os.getenv('DASHBOARD_TOKEN','')
    if token:
        if request.path!='/dashboard' and not hmac.compare_digest(request.headers.get('Authorization',''),'Bearer '+token):raise web.HTTPUnauthorized()
    elif request.remote not in ('127.0.0.1','::1'):raise web.HTTPForbidden(text='DASHBOARD_TOKEN required')
    if request.method=='POST':
        origin=request.headers.get('Origin')
        if origin and origin!=f'{request.scheme}://{request.host}':raise web.HTTPForbidden()
    return await handler(request)
async def dashboard(request):return web.Response(text=HTML,content_type='text/html')
async def state_api(request):return web.json_response(MONITOR.snapshot(),headers={'Cache-Control':'no-store'})
async def command_api(request):
    try:
        if request.content_type!='application/json':raise ValueError('JSON required')
        text=(await request.json())['command']
        if not isinstance(text,str) or len(text)>200:raise ValueError('Invalid command')
        return web.Response(text=MONITOR.command(text))
    except (ValueError,KeyError,TypeError) as exc:return web.Response(status=400,text=str(exc))
