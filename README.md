# Complete gold bot — explicit YES/NO selector

## Deploy this complete package
Replace bot.py, free_gold.py AND trade_controls.py together in your Render-linked
repository; also upload requirements.txt. No earlier generated runtime module needed.
This extends the supplied original bot.py and includes the agreed free-feed build,
compact reports and trade controls. It was not retrieved from your live deployment.
changes.diff is cumulative against the supplied original, not your deployed copy.
Python 3.10+ required.

Render Build Command: python -m pip install -r requirements.txt
Render Start Command: python bot.py
Commit the files, then deploy latest commit. Do not paste shell commands into Python.
Keep Telegram/Kalshi keys in Render Environment, not source control.
Existing DASHBOARD_TOKEN works. No paid Pyth subscription or gold feed API key.
ENABLE_LIVE_ORDERS defaults false; keep false during deployment/demo testing.
The package does NOT reset existing Render environment values (including true).

## Trading UI: 15-minute only
/predict and /test send one formatted report, with ONE 15-minute and ONE hourly forecast.
Initially:
  SELL ALL — actual held side/quantity at last check (15M)
  Select YES | Select NO
No buy buttons until you explicitly select a side.
After choosing NO:
  SELL ALL — actual held-side snapshot (15M)
  Select YES | NO SELECTED
  Buy 1 NO @ price | Buy 5 NO @ price | Buy 10 NO @ price

Choosing a side does not submit an order. The model prediction never auto-selects it.
Selection updates the SAME Telegram message keyboard, refreshing that side's ask.
Switching sides invalidates the old generation BEFORE any await, so delayed old buy
buttons cannot execute the former side. Failed keyboard update fails closed; /predict.
Each Buy binds exact ticker, selected side, quantity and displayed price cap.
The three quantities remain 1/5/10. Current ask is fetched again at execution; higher
than displayed cap is rejected rather than accepting hidden slippage.
Each buy/sell attempt consumes the message controls; /predict for a new set afterward.
Buys expire 90 seconds after side selection; sell/selector contexts expire after 30 min.

SELL ALL ignores the purchase selector and fetches ACTUAL holdings again. Choosing
YES while holding NO still causes SELL ALL to close held NO, not buy YES or sell
imaginary YES. Label is a snapshot, not a guarantee of your current holdings.
No side selection needed for selling existing shares. Only exact selected 15m ticker,
not every 15m market in your account. Default/key-bound primary subaccount only.
Hourlies have NO controls; context creation and execution independently reject them.
Old BUY/G2 callbacks and legacy place_kalshi_order cannot bypass the selector.
/test is now a preview of the REAL current contract. If live orders are enabled,
its buy/sell controls are real, not simulated. No further confirmation dialog after click.

## Owner binding
Callbacks require authorized TELEGRAM_CHAT_ID AND owner user ID. Private chat defaults
to that chat ID. For a group set TELEGRAM_USER_ID to your personal numeric user ID.
Random opaque callback tokens bind ticker/side/price/message/chat in server memory.
Message mismatch, stale/unknown/replaced tokens are rejected. Duplicate/concurrent
button actions cannot reuse a consumed generation. Restart invalidates all old tokens.

## Forecasts, alerts and gauges
15m: one projected spot finish, structured target, model YES/NO, model score, time,
center-fill rectangular gauge. Hourly: one projected spot finish, time and gauge only.
Negative fills left from fixed center; positive fills right. Monospace in Telegram.
/predict, /test and live forecast alerts share one HTML report formatter.
/gauge gives two small lines. /feed is short, not raw JSON. /markets is the explicit
contract list. /status /stats /balance retain original diagnostics.
/mute /unmute /threshold 65 /help available through Telegram; omit / in terminal/dashboard.
Mute suppresses alerts, not manual trades or official-result tracking.
Original RSI/Bollinger reversal and edge/spread filters retained for selected 15m.
Combined alert triggers on qualifying reversal or momentum threshold crossing, with
original 300s cooldown and 10-point hysteresis. No auto orders added.
Qualifying 15m reversal signals paper logged independently of manual selection.
Original paper accounting is per-signal and excludes fees; not account execution PnL.

For each series choose earliest future expiry of currently active/open markets that
have actually opened and have not reached trading close. At same expiry, nearest
structured strike to current spot, then ticker tie-break. Never output every hourly
strike. When no active 15m contract is found, no trade controls are invented.
Model missing or target unknown does not prevent explicit manual side selection,
but buys require fresh proxy data and executable quote. Missing quotes keep buys hidden.

## Free feed and limitations
Only https://api.gold-api.com/price/XAU; no key, no Swissquote fallback, no paid Pyth.
Validate XAU/USD, finite positive price and provider updatedAt. Default age max 60s,
future skew max 5s. Out-of-order/stale timestamps rejected. Network failure marks
feed unavailable immediately. Last known price may display as stale; forecasts suppressed.
Free provider availability, rate/usage terms, upstream changes and update frequency
not guaranteed. A prior public response was stale; verify freshness in your deployment.

Provider timestamps build candles; repeated timestamp does not fabricate new observations.
>120s gaps reset 1m/5m histories; noncontiguous minute history blocks model output.
~20 completed 1m candles warm-up. Early hourly gauge uses less than full-hour history.
Projection uses clipped log-return drift and normal uncertainty; scores UNCALIBRATED.
Free spot is a PROXY, not exact Pyth index. No profitability or accuracy guarantee.
Kalshi published yes/no is sole source of official paper-trade settlement.
Approximate horizon: expected_expiration_time, expiration_time, then close_time.
These are not independently verified contractual observation windows. No averaging,
rounding or candlestick settlement adapter is implemented. Do not confuse forecast
with exact index settlement replication.

## Sell implementation and guardrails
Fresh identity, event prefix, market status/open/close time, exact position and book.
Position pagination supported; requires position_fp and explicit netting_enabled=true.
Non-netted/ambiguous positions unsupported and rejected, not guessed.
Positive net YES: YES-book ask at current best YES bid.
Negative net NO: YES-book bid at 1 minus current best NO bid.
POST /portfolio/events/orders; reduce_only=true; immediate_or_cancel;
fixed-point price/count; self_trade_prevention_type=taker_at_cross.
Fractional net positions supported to 0.01. No position or empty bids -> no order.
Gold proxy outage does NOT block a sell; it uses current Kalshi market/position data.

This is an ATTEMPT to sell all at best bid or better, not guaranteed liquidation.
No sweeping to arbitrary worse prices. Partial/zero fills possible. IOC remainder
is canceled, not left resting. Report counts filled/unfilled, order ID, average price
if provided and refreshed signed net position if readable. Prices shown before fees.
Existing resting orders are NOT canceled and may change holdings afterward.
No other market/subaccount liquidated. Server reduce-only prevents opening reverse holdings.
Per-contract lock serializes local buy/sell. External clients can still change positions.

POST exception/timeout/malformed result leaves ticker in uncertain-submission block;
no automatic retry. Check client_order_id in Kalshi. No uncertainty-clear command.
Restart clears in-memory block but DOES NOT prove old order failed. Inspect account
before trying again. Duplicate prevention is not durable across restarts/deploys.
ENABLE_LIVE_ORDERS=false blocks all writes. KALSHI_USE_DEMO chooses demo vs production.
Use correct environment credentials; keys differ. No test here used a real account.

## Dashboard and Render
Server: 0.0.0.0, PORT default 10000. Public root/health retain original health summaries.
Open service URL + /dashboard. Set a strong DASHBOARD_TOKEN for remote API/commands.
Token stored in sessionStorage, not URLs; use HTTPS. No dashboard order buttons.
Dashboard center-out gauges refresh from cached state every 2s. Market scans ~15s plus
network latency; feed polling ~5s plus worker activity. No browser push notifications.
Terminal stdin commands where async input supported; Render may not provide a terminal.
No persistence for candles, paper tracker, controls, locks or uncertainty state.
Restart/deploy starts warm-up again. Free Render may sleep after 15 minutes without
incoming requests; not guaranteed continuous 24/7 alerting.

## Tests actually performed
python -m unittest -v test_update.py
53 automated tests passed. Captured output: TEST_RESULTS.txt.
Coverage: explicit selection, independent model/manual sides, switching revocation,
message/owner binding, no orders on selector taps, duplicate handling, missing asks,
UI-edit failures, 15m-only trading, partial/zero/fractional/full liquidation, netting,
pagination, timeout/no retry, live disable, buy price cap, report cardinality and gauges.
Local HTTP mock exercised sell request/response and dashboard auth. Runtime bot import,
route registration, syntax and legacy write blocking checked. No live Kalshi order,
live Telegram UI, browser rendering or deployment test performed. Nothing deployed.

## Package contents
bot.py, free_gold.py, trade_controls.py, requirements.txt, env.example,
test_update.py, TEST_RESULTS.txt, changes.diff, README.md, MANIFEST.json.
env.example is a reference, not automatically loaded. No credentials included.
Manifest provides SHA256/size; ZIP verified for integrity before delivery.
