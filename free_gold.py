"""Free spot-gold proxy, compact forecasts and explicit 15m purchase selection."""
import asyncio
import html
import math
import os
import secrets
import statistics
import time
from datetime import datetime,timezone
from urllib.parse import quote
from aiohttp import web
from forecast_log import ForecastLog,number

URL='https://api.gold-api.com/price/XAU'
MAX_AGE=float(os.getenv('GOLD_MAX_AGE_SECONDS','60'))
# Model settings (all uncalibrated until /calibration has enough settled markets).
VOL_FLOOR=float(os.getenv('GOLD_VOL_FLOOR','0.0001'))      # per-minute log-vol floor
VOL_MULT=float(os.getenv('GOLD_VOL_MULT','1.1'))           # fat-tail inflation of measured vol
BASIS_USD=float(os.getenv('GOLD_BASIS_USD','0.75'))        # 1-sigma proxy-vs-Pyth-index error, USD
MOMENTUM_ALERTS=os.getenv('MOMENTUM_ALERTS','false').lower()=='true'
GAP_RESET_SECONDS=300     # feed silent longer than this: discard candle history
P_MIN,P_MAX=.02,.98       # never display certainty
MAX_SECONDS={15:1200,60:4500}   # longer than this means the expiry field is wrong, not the market
SERIES={15:'KXGOLD15M',60:'KXGOLDH'}

def norm_cdf(x):return .5*(1+math.erf(x/math.sqrt(2)))

def realized_vol(candles,tf,max_returns,min_returns,max_gap=3):
    """RMS close-to-close log return per sqrt(minute). Tolerates missing candles (gap-normalised)."""
    rets=[]
    for a,b in zip(candles,candles[1:]):
        dt=(b['timestamp']-a['timestamp'])/60
        if 0<dt<=max_gap*tf/60 and a['close']>0 and b['close']>0:rets.append(math.log(b['close']/a['close'])/math.sqrt(dt))
    rets=rets[-max_returns:]
    if len(rets)<min_returns:return None
    return math.sqrt(sum(r*r for r in rets)/len(rets))

def price_at(candles,t,tolerance=120):
    """Close of the last completed 1m candle ending at or before t (None if older than tolerance)."""
    best=None
    for c in candles:
        if c['timestamp']+60<=t:best=c
        else:break
    return best['close'] if best and t-(best['timestamp']+60)<=tolerance else None

def candle_close_at(candles,end):
    """Close of the 1m candle that ENDS at `end` (Kalshi/Pyth label candles by their opening minute)."""
    for c in candles:
        if c['timestamp']==int(end)-60:return c['close']
    return None

def stamp(value):
    dt=datetime.fromisoformat(value.replace('Z','+00:00'))
    if dt.tzinfo is None:raise ValueError('Timezone missing')
    return dt.timestamp()

def gauge(value,width=8):
    if value is None:return '− ['+'░'*width+'│'+'░'*width+'] + N/A'
    value=max(-100,min(100,value));n=round(abs(value)*width/100)
    left='░'*(width-n)+'█'*n if value<0 else '░'*width
    right='█'*n+'░'*(width-n) if value>0 else '░'*width
    return f'− [{left}│{right}] + {value:+.0f}'

def expiry(m):
    # expiration_time is Kalshi's LATEST possible expiry (hours after the window on 15m gold), so it is last resort.
    for field in ('expected_expiration_time','close_time','expiration_time'):
        try:return stamp(m[field])
        except (KeyError,TypeError,ValueError):pass
    return None

def select_one(markets,series,now,spot):
    candidates=[]
    for m in markets:
        if m.get('series_ticker')!=series or m.get('status') not in ('active','open'):continue
        try:
            if not stamp(m['open_time'])<=now<stamp(m['close_time']):continue
        except (KeyError,TypeError,ValueError):continue
        end=expiry(m)
        if end is None or end<=now:continue
        try:distance=abs(float(m.get('floor_strike',m.get('cap_strike')))-spot) if spot else 0
        except (TypeError,ValueError):distance=float('inf')
        candidates.append((end,distance,m.get('ticker',''),m))
    return min(candidates,key=lambda v:v[:3])[3] if candidates else None

class GoldMonitor:
    def __init__(self):
        self.price=None;self.publish_time=0.;self.received=0.;self.previous_ts=0.
        self.rows=[];self.updated=0.;self.status='Waiting for free gold feed'
        self.muted=False;self.threshold=65.;self.last_alert=0.;self.armed=True
        self.contexts={}
        self.log=ForecastLog(os.getenv('FORECAST_DB','forecast_log.db'))
        self.min_seconds={15:210,60:600};self.series_info={};self.series_loaded=0.

    def fresh(self):
        return self.price is not None and -5<=time.time()-self.publish_time<=MAX_AGE and time.time()-self.received<=MAX_AGE

    def decode(self,data):
        if data.get('symbol')!='XAU' or data.get('currency','USD')!='USD':raise ValueError('Wrong symbol/currency')
        price=float(data['price']);ts=stamp(data['updatedAt'])
        if not math.isfinite(price) or price<=0:raise ValueError('Invalid price')
        if not -5<=time.time()-ts<=MAX_AGE:raise ValueError('Stale/future provider timestamp')
        if ts<self.publish_time:raise ValueError('Out-of-order provider timestamp')
        return price,ts

    async def fetch(self,session,state):
        try:
            async with session.get(URL,timeout=6) as r:
                r.raise_for_status();data=await r.json()
            price,ts=self.decode(data)
            if ts>self.publish_time:
                if self.previous_ts and ts-self.previous_ts>GAP_RESET_SECONDS:
                    state.candles_1m=type(state.candles_1m)(60,max_candles=120)
                    state.candles_5m=type(state.candles_5m)(300,max_candles=120)
                state.candles_1m.update(price,ts);state.candles_5m.update(price,ts)
                self.previous_ts=ts
            self.price=price;self.publish_time=ts;self.received=time.time()
            state.latest_spot=price;state.latest_spot_ts=ts
            state.spot_source='Gold-API.com spot proxy (not exact Pyth index)'
            self.status='Fresh free spot proxy'
            return price
        except Exception as exc:
            self.received=0.;self.status='Feed unavailable: '+str(exc)
            return None

    def model(self,state,minutes):
        """Zero-drift inputs for one timeframe: per-minute volatility and a display-only momentum gauge.
        15m uses 1m candles; 1h uses its own 5m candles (falls back to 1m while 5m history warms up)."""
        if not self.fresh():return None
        one=list(state.candles_1m.candles);fallback=False
        if minutes>=60:
            sigma=realized_vol(list(state.candles_5m.candles),300,48,12)
            if sigma is None:sigma=realized_vol(one,60,60,20);fallback=True
        else:sigma=realized_vol(one,60,60,20)
        if sigma is None:return None
        sigma=max(sigma,VOL_FLOOR)*VOL_MULT
        ref=price_at(one,self.publish_time-minutes*60)   # None until a full lookback of history exists
        momentum=100*math.tanh(math.log(self.price/ref)/(2*sigma*math.sqrt(minutes))) if ref else None
        return dict(sigma=sigma,momentum=momentum,fallback=fallback)

    def probability(self,spot,strike,kind,seconds,sigma):
        """P(YES) for a fixed strike: zero drift, plus uncertainty for proxy-vs-index basis. Clamped, uncalibrated."""
        horizon=max(seconds,1)/60;basis=BASIS_USD/spot
        total=math.sqrt(sigma*sigma*horizon+basis*basis)
        p=norm_cdf((math.log(spot/strike)-sigma*sigma*horizon/2)/total)
        if str(kind).startswith('less'):p=1-p
        return min(P_MAX,max(P_MIN,p))

    async def load_series(self,session,bot):
        """Fetch settlement source / fee terms from Kalshi (public). Shown by /source; fee multiplier feeds edge maths."""
        if time.time()-self.series_loaded<6*3600:return
        self.series_loaded=time.time();failed=False
        for series in SERIES.values():
            try:
                async with session.get(f'{bot.KALSHI_BASE_URL}/series/{quote(series,safe="")}',timeout=6) as r:
                    r.raise_for_status();data=(await r.json()).get('series',{})
                self.series_info[series]=dict(title=data.get('title'),sources=data.get('settlement_sources'),
                    terms=data.get('contract_terms_url'),fee_type=data.get('fee_type'),fee_multiplier=data.get('fee_multiplier'))
            except Exception as exc:
                failed=True;self.series_info[series]=dict(error=str(exc)[:120])
        if failed:self.series_loaded=time.time()-6*3600+300   # retry in 5 minutes
        info=self.series_info.get('KXGOLD15M',{});mult=number(info.get('fee_multiplier'))
        if str(info.get('fee_type') or '').startswith('quadratic') and mult and mult>0:bot.FEE_RATE=.07*mult

    async def refresh(self,session,bot):
        markets=await bot.fetch_kalshi_gold_markets(session);bot.STATE.active_markets_count=len(markets)
        self.min_seconds={15:int(getattr(bot,'MIN_MINUTES_15M',3.5)*60),60:int(getattr(bot,'MIN_MINUTES_1H',10)*60)}
        await self.load_series(session,bot)
        rows=[];now=time.time()
        for minutes,series in SERIES.items():
            market=select_one(markets,series,now,self.price);model=self.model(bot.STATE,minutes)
            end=expiry(market) if market else None
            row=dict(minutes=minutes,ticker=market.get('ticker') if market else None,
                momentum=model['momentum'] if model else None,sigma_usd=None,prediction=None,
                yes_score=None,target=None,kind=market.get('strike_type') if market else None,
                seconds_left=max(0,int(end-now)) if end else None,reason='',quotes=None,updated_at=now)
            if not market:row['reason']='No active contract found'
            elif end is None:row['reason']='Contract has no usable expiry time'
            elif end-now>MAX_SECONDS[minutes]:row['reason']='Expiry time implausible; forecast withheld'
            elif not model:row['reason']='Warm-up (about 20 min of candles; hourly needs 1 h), stale feed or outage'
            else:
                seconds=end-now;strike=bot.parse_strike(market);kind=market.get('strike_type');row['target']=strike
                row['sigma_usd']=round(self.price*model['sigma']*math.sqrt(max(seconds,1)/60),2)
                if strike and kind in ('greater','greater_or_equal','less','less_or_equal'):
                    p=self.probability(self.price,strike,kind,seconds,model['sigma'])
                    row['yes_score']=round(p*100,1);row['prediction']='YES' if p>=.5 else 'NO'
                    if model['fallback']:row['reason']='Hourly volatility from 1m candles (5m history warming up)'
                else:row['reason']='Missing structured target/direction; YES/NO unavailable'
            if market:row['quotes']=await bot.fetch_orderbook_details(session,market['ticker'])
            rows.append(row)
            if market and end is not None:
                age=now-self.publish_time if self.publish_time else None
                self.log.record(market,row,model['sigma'] if model else None,self.price,age,now,end)
        self.rows=rows;self.updated=now
        self.contexts={k:v for k,v in self.contexts.items() if now-v['created']<1800}
        if len(self.contexts)>2000:
            for key in sorted(self.contexts,key=lambda k:self.contexts[k]['created'])[:-2000]:self.contexts.pop(key,None)

    async def settle(self,session,bot):
        """Record Kalshi's official result + expiration_value for finished markets, with our proxy's candle closes."""
        now=time.time();candles=list(bot.STATE.candles_1m.candles)
        for ticker,minutes,end_ts in self.log.pending(now):
            try:
                async with session.get(f'{bot.KALSHI_BASE_URL}/markets/{quote(ticker,safe="")}',timeout=5) as r:
                    if r.status!=200:continue
                    market=(await r.json()).get('market',{})
            except Exception:continue
            result=(market.get('result') or '').lower()
            if result not in ('yes','no'):continue          # official result not published yet
            window=900 if minutes==15 else 3600
            self.log.settle(ticker,result,number(market.get('expiration_value')),
                number(market.get('floor_strike') or market.get('cap_strike')),
                candle_close_at(candles,end_ts-window),candle_close_at(candles,end_ts),now)

    def snapshot(self):
        rows=[]
        for original in self.rows:
            row=dict(original);seconds=row['seconds_left']
            if not self.fresh() or time.time()-self.updated>45 or (seconds is not None and time.time()-self.updated>=seconds):
                row.update(momentum=None,sigma_usd=None,prediction=None,yes_score=None,reason='Stale/unavailable data')
            elif seconds is not None:row['seconds_left']=max(0,seconds-int(time.time()-self.updated))
            rows.append(row)
        return dict(provider='Gold-API.com',status=self.status,fresh=self.fresh(),price=self.price,
            quote_age=round(time.time()-self.publish_time,1) if self.publish_time else None,
            proxy_warning='Not exact Pyth settlement index. Zero-drift model, uncalibrated until /calibration has data. Kalshi supplies official results.',
            muted=self.muted,threshold=self.threshold,markets=rows)

    def report(self):
        snap=self.snapshot();price=f'${self.price:,.2f}' if self.price else 'unavailable'
        lines=['<b>GOLD FINISH FORECAST</b>',f'<b>Current spot:</b> {price}']
        for minutes in (15,60):
            row=next((r for r in snap['markets'] if r['minutes']==minutes),None)
            lines+=['',f'<b>{"15-MINUTE" if minutes==15 else "1-HOUR"} MARKET</b>']
            if not row:lines+=['Waiting for first scan'];continue
            if row['target'] is not None:
                word='above' if str(row['kind']).startswith('greater') else 'below' if str(row['kind']).startswith('less') else ''
                lines+=[f'<b>Target:</b> ${row["target"]:,.2f}'+(f' (YES if finish {word})' if word else '')]
            prediction=row['prediction'] or 'unavailable';lines+=[f'<b>Model prediction:</b> {prediction}']
            if row['yes_score'] is not None:
                score=row['yes_score'] if prediction=='YES' else 100-row['yes_score']
                lines+=[f'<b>Model score for {prediction}:</b> {score:.1f}/100']
            if row['sigma_usd'] is not None:lines+=[f'<b>Typical move to finish (1σ):</b> ±${row["sigma_usd"]:,.2f}']
            lines+=[f'<code>{gauge(row["momentum"])}</code> momentum (display only)']
            if row['ticker']:lines+=[f'<b>Contract:</b> <code>{html.escape(row["ticker"])}</code>']
            seconds=row['seconds_left']
            if seconds is not None:lines+=[f'<b>Time remaining:</b> {seconds//60}m {seconds%60:02d}s']
            if seconds is not None and seconds<self.min_seconds[minutes]:lines+=['Final minutes: proxy-vs-Pyth difference dominates; low confidence.']
            if row['reason']:lines+=[html.escape(row['reason'])]
        lines+=['','<i>Free spot proxy—not exact Pyth settlement. Zero-drift model; scores uncalibrated (see /calibration).</i>',
                'Only the 15-minute market is tradable. Choose YES or NO below to enable buys. Model prediction does not choose your purchase side.']
        return '\n'.join(lines)

    def new_context(self,ticker,side=None,ask=None,held=None,parent=None):
        if not ticker or not ticker.startswith('KXGOLD15M-'):return None
        token=secrets.token_hex(6)
        ctx=dict(ticker=ticker,side=side,ask=ask,held=held,created=time.time(),active=True,chat_id=None,message_id=None)
        if parent:ctx.update(chat_id=parent['chat_id'],message_id=parent['message_id'])
        self.contexts[token]=ctx
        return token

    def keyboard(self,token):
        ctx=self.contexts.get(token)
        if not ctx or not ctx['active']:return None
        held=ctx['held'];side=ctx['side'];label='SELL ALL — HELD SIDE (15M)'
        if held is not None:
            label=f'SELL ALL — {abs(held):.2f} {"YES" if held>0 else "NO"} (15M)' if held else 'SELL ALL — NO SHARES AT LAST CHECK (15M)'
        rows=[[dict(text=label,callback_data=f'G3|S|{token}')],
              [dict(text='YES SELECTED' if side=='YES' else 'Select YES',callback_data=f'G3|Y|{token}'),
               dict(text='NO SELECTED' if side=='NO' else 'Select NO',callback_data=f'G3|N|{token}')]]
        if side and ctx['ask'] is not None:
            rows.append([dict(text=f'Buy {n} {side} @ {ctx["ask"]*100:.2f}¢',callback_data=f'G3|B|{token}|{n}') for n in (1,5,10)])
        return dict(inline_keyboard=rows)

    async def held(self,session,bot,ticker):
        if not bot.KALSHI_KEY_ID or bot.PRIVATE_KEY_OBJ is None:return None
        try:return await bot.EXECUTION.position(session,ticker)
        except Exception:return None

    async def send_prediction(self,session,bot,chat=None,refresh=True):
        if refresh:await self.refresh(session,bot)
        row=next((r for r in self.rows if r['minutes']==15),None);token=None
        if row and row['ticker']:
            token=self.new_context(row['ticker'],held=await self.held(session,bot,row['ticker']))
        result=await bot.send_telegram_message(session,self.report(),chat,reply_markup=self.keyboard(token))
        if token and isinstance(result,dict):
            self.contexts[token].update(chat_id=str(result.get('chat',{}).get('id',chat or bot.TELEGRAM_CHAT_ID)),message_id=result.get('message_id'))

    async def edit_keyboard(self,session,bot,ctx,markup):
        payload=dict(chat_id=ctx['chat_id'],message_id=ctx['message_id'],reply_markup=markup or {'inline_keyboard':[]})
        async with session.post(f'https://api.telegram.org/bot{bot.TELEGRAM_TOKEN}/editMessageReplyMarkup',json=payload,timeout=6) as r:
            data=await r.json()
            if r.status!=200 or not data.get('ok'):raise ValueError('Could not update Telegram controls; send /predict')

    async def notice(self,session,bot,text,chat):await bot.send_telegram_message(session,html.escape(text),chat)

    async def callback(self,session,bot,query):
        message=query.get('message',{});chat=str(message.get('chat',{}).get('id',''))
        owner=os.getenv('TELEGRAM_USER_ID','').strip() or bot.TELEGRAM_CHAT_ID
        if not bot.is_authorized(chat) or str(query.get('from',{}).get('id',''))!=owner:return
        parts=query.get('data','').split('|')
        try:
            async with session.post(f'https://api.telegram.org/bot{bot.TELEGRAM_TOKEN}/answerCallbackQuery',
                json={'callback_query_id':query.get('id'),'text':'Checking controls…'},timeout=4) as r:await r.read()
        except Exception:pass
        if len(parts)<3 or parts[0]!='G3':
            await self.notice(session,bot,'Old controls disabled; send /predict.',chat);return
        action,token=parts[1:3];ctx=self.contexts.get(token)
        if not ctx or not ctx['active']:
            await self.notice(session,bot,'Controls replaced, used, expired or reset. Send /predict.',chat);return
        if action not in ('Y','N','S','B'):return
        if ctx['chat_id'] is not None and (ctx['chat_id']!=chat or ctx['message_id']!=message.get('message_id')):
            await self.notice(session,bot,'These controls belong to a different message.',chat);return
        age=time.time()-ctx['created']
        if age>1800 or (action=='B' and age>90):
            ctx['active']=False;await self.notice(session,bot,'Controls expired; send /predict or reselect side.',chat);return
        if action=='B':
            if len(parts)!=4 or ctx['side'] not in ('YES','NO') or ctx['ask'] is None:
                await self.notice(session,bot,'Select YES or NO with an available ask first. No order sent.',chat);return
            try:quantity=int(parts[3])
            except ValueError:return
            if quantity not in (1,5,10):return
        # Revoke before awaits: delayed old buttons cannot execute the former side.
        ctx.update(active=False,chat_id=chat,message_id=message.get('message_id'))
        if action in ('Y','N'):
            side='YES' if action=='Y' else 'NO';ask=None
            try:
                quotes=await bot.fetch_orderbook_details(session,ctx['ticker'])
                value=quotes.get('yes_ask' if side=='YES' else 'no_ask') if quotes else None
                if value is not None and math.isfinite(float(value)) and 0<float(value)<100:ask=float(f'{float(value)/100:.4f}')
                new=self.new_context(ctx['ticker'],side=side,ask=ask,held=await self.held(session,bot,ctx['ticker']),parent=ctx)
                try:await self.edit_keyboard(session,bot,self.contexts[new],self.keyboard(new))
                except Exception:
                    self.contexts[new]['active']=False;raise
                if ask is None:await self.notice(session,bot,f'{side} selected; no executable ask. Buy controls remain disabled.',chat)
            except Exception as exc:await self.notice(session,bot,str(exc)+' Send /predict to refresh.',chat)
            return
        try:await self.edit_keyboard(session,bot,ctx,None)
        except Exception:pass # Server token is consumed even if UI edit fails.
        if action=='S':ok,text=await bot.EXECUTION.sell_all(session,ctx['ticker'])
        else:ok,text=await bot.EXECUTION.buy(session,ctx['ticker'],ctx['side'],quantity,max_ask=ctx['ask'])
        await bot.send_telegram_message(session,text,chat)

    async def maybe_alert(self,session,bot):
        if self.muted or not self.fresh():return
        strength=max((abs(r['momentum']) for r in self.rows if r['momentum'] is not None),default=0)
        if strength<self.threshold-10:self.armed=True
        reversal=False;row=next((r for r in self.rows if r['minutes']==15),None)
        if row and row['prediction'] and row['yes_score'] is not None and row['quotes'] and (row['seconds_left'] or 0)>=self.min_seconds[15]:
            sig=bot.detect_reversal_signal(bot.STATE.candles_1m,'1m')
            if sig['direction']=='NEUTRAL':sig=bot.detect_reversal_signal(bot.STATE.candles_5m,'5m')
            side='YES' if sig['direction']=='BULLISH' else 'NO' if sig['direction']=='BEARISH' else None
            ob=row['quotes'];ask=ob.get('yes_ask' if side=='YES' else 'no_ask')
            fair=row['yes_score'] if side=='YES' else 100-row['yes_score']
            if side and side==row['prediction'] and ask is not None and ob['spread']<=bot.MAX_SPREAD_CENTS:
                edge=fair-ask-bot.kalshi_taker_fee_cents(ask);reversal=edge>=bot.MIN_NET_EDGE_CENTS and 8<=ask<=85
        now=time.time()
        if now-self.last_alert<bot.ALERT_COOLDOWN_SECONDS:return
        if not ((MOMENTUM_ALERTS and strength>=self.threshold and self.armed) or reversal):return
        self.last_alert=now;self.armed=False
        await self.send_prediction(session,bot,refresh=False)
        if reversal:
            close=datetime.fromtimestamp(now+row['seconds_left'],timezone.utc).isoformat()
            bot.STATE.tracker.log_signal(row['ticker'],row['prediction'],ask,row['target'],edge,close)

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
        if cmd=='feed':return f'{self.status}; fresh={self.fresh()}; age={self.snapshot()["quote_age"]}s; spot proxy only'
        if cmd=='gauge':return '\n'.join(f'{r["minutes"]}m {gauge(r["momentum"])}' for r in self.snapshot()['markets']) or 'Waiting for scan'
        if cmd=='calibration':return self.log.calibration()
        if cmd=='source':
            if not self.series_info:return 'Series info not loaded yet; wait for the next scan.'
            return '\n'.join(f'{k}: '+(info['error'] if 'error' in info else f'{info.get("title")}; sources={info.get("sources")}; terms={info.get("terms")}; fee={info.get("fee_type")} x{info.get("fee_multiplier")}') for k,info in self.series_info.items())
        if cmd in ('predict','status'):
            import re
            return html.unescape(re.sub('<[^>]+>','',self.report()))
        return '/predict /gauge /feed /calibration /source /mute /unmute /threshold 65 /status /stats /balance /markets'

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

HTML=r"""<!doctype html><meta charset=utf-8><title>Gold forecasts</title><style>body{font:16px system-ui;background:#14202e;color:#eee;max-width:900px;margin:30px auto;padding:15px}pre{white-space:pre-wrap}article{background:#25374a;padding:18px;margin:15px 0}.g{position:relative;width:240px;height:18px;border:1px solid #aaa}.center{position:absolute;left:50%;height:100%;border-left:1px solid white}.fill{position:absolute;height:100%}input,button{padding:8px}</style><h1>Gold finish forecast</h1><p>Free spot proxy. Official settlement from Kalshi. Trade controls are Telegram-only.</p><p id=status></p><div id=rows></div><form id=f><input id=cmd placeholder="predict / gauge / feed"><button>Command</button></form><pre id=reply></pre><script>
const $=s=>document.querySelector(s);const token=sessionStorage.getItem('gold_token')||prompt('Dashboard token')||'';sessionStorage.setItem('gold_token',token);const headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'};function esc(x){let e=document.createElement('span');e.textContent=x??'unavailable';return e.innerHTML;}
async function update(){try{let r=await fetch('/api/gold',{headers});if(!r.ok)throw Error(r.status);let s=await r.json();$('#status').textContent=s.status+' | spot '+s.price+' | age '+s.quote_age+'s';$('#rows').innerHTML=s.markets.map(m=>{let g=m.momentum||0,w=Math.abs(g)/2,left=g<0?50-w:50;return `<article><h2>${m.minutes===15?'15-minute':'1-hour'}</h2><p>Typical move to finish (1σ): ±${esc(m.sigma_usd)}</p><div class=g><span class=fill style="left:${left}%;width:${w}%;background:${g<0?'#d66':'#5b8'}"></span><span class=center></span></div><p>−100 ← ${esc(m.momentum)} → +100 (momentum, display only)</p><p>Target: ${esc(m.target)} | model prediction: ${esc(m.prediction)} (YES ${esc(m.yes_score)}/100)</p><p>Seconds remaining: ${esc(m.seconds_left)}</p><p>${esc(m.reason)}</p></article>`}).join('');}catch(e){$('#status').textContent='Disconnected: '+e.message;}}$('#f').onsubmit=async e=>{e.preventDefault();let r=await fetch('/api/command',{method:'POST',headers,body:JSON.stringify({command:$('#cmd').value})});$('#reply').textContent=await r.text();};update();setInterval(update,2000);</script>"""

@web.middleware
async def dashboard_auth(request,handler):
    if request.path not in ('/dashboard','/api/gold','/api/command','/api/log'):return await handler(request)
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
async def log_api(request):return web.Response(text=MONITOR.log.export_csv(),content_type='text/csv',headers={'Cache-Control':'no-store'})
async def state_api(request):return web.json_response(MONITOR.snapshot(),headers={'Cache-Control':'no-store'})
async def command_api(request):
    try:
        if request.content_type!='application/json':raise ValueError('JSON required')
        text=(await request.json())['command']
        if not isinstance(text,str) or len(text)>200:raise ValueError('Invalid command')
        return web.Response(text=MONITOR.command(text))
    except (KeyError,TypeError,ValueError) as exc:return web.Response(status=400,text=str(exc))
