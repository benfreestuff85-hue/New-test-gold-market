# Gold forecast bot — independent 15m / 1h engines, free data, paper-first

## Current update

This update replaces the prior zero-drift/display-only momentum implementation with a transparent, bounded feature model. It is still **research software**, not a validated profitable strategy.

### Forecast engines
- **15-minute:** sampled 1-minute Gold-API spot candles; close-to-close realized volatility, short/medium momentum, acceleration, robust jump detection, and volatility regime.
- **1-hour:** its own 5-minute candles and 1-hour contract/strike. It falls back to 1-minute candles while 5-minute history warms up, and refuses to call a 30-minute sample a full hourly forecast.
- The engines share utility functions, not horizon inputs. Both remain correlated because they observe the same underlying gold market.
- Probability combines log-distance to the contract strike, remaining time, volatility-regime adjustment, a bounded momentum drift prior, and jump-risk volatility inflation. Probabilities are clamped to 2–98%.
- **Order book:** captures best quotes and up to five levels per side. A small bounded adjustment uses top-five depth imbalance and microprice offset. This is a single-snapshot feature, not a learned order-flow model; it can be noisy.
- Feature values, regime, acceleration, jump z-score, and order-book metrics are logged with forecast snapshots and included in CSV export.

### Critical data limitation
- Live underlying input remains free Gold-API XAU/USD spot. Public reporting for KXGOLD15M identifies Pyth `Metal.Index.1OZGOLD/USD`, but this package does not subscribe to that feed or prove the current per-series feed ID from a live rules response. KXGOLDH must be checked independently. The Gold-API input is **not** the contractual Pyth index, so forecasts are a proxy model, not exact settlement replication.
- The bot fetches Kalshi series settlement-source metadata and official market result/expiration value after settlement. That verifies outcomes but does not provide a real-time exact Pyth observation. Sampled proxy candle closes are now accepted for basis diagnostics only when the last sample is within 20 seconds of the minute boundary; this still does not make them Pyth candle closes.
- No paid data subscription is added. Do not substitute another Pyth feed ID without verifying the exact series `settlement_sources` and contract terms from Kalshi.
- `GOLD_BASIS_USD=0.75` remains an uncalibrated placeholder for proxy/index uncertainty. Do not regard probabilities as calibrated until basis residuals are measured from a sufficiently large, time-aligned sample.

### Evaluation and trading safety
- SQLite logs model probability, Kalshi bid/ask mid, feature diagnostics, and official settlement. `/calibration` reports Brier score at fixed lead times and proxy-vs-settlement diagnostics. Download `/api/log` before redeploy if storage is ephemeral.
- Hourly fixed-strike contracts are excluded from the 15-minute opening-strike basis statistic; these are different contract structures.
- Existing reversal/edge alerts remain alerts only. Feature weights are heuristic, not fitted. Run chronological walk-forward tests, final-minute replay, regime-sliced Brier/log-loss, and paper execution before considering live use.
- `ENABLE_LIVE_ORDERS=false` remains the default. **Keep it false.** No live deployment or order was made by this update. Existing manual 15m trade controls remain in the package, but must not be enabled during model testing.
- The paper tracker is not actual fill accounting and does not establish profitability. Fees, slippage, partial fills, and actual execution must be measured separately.

## Tests
The package includes unit tests for trading gates, selectors, logging, independent horizon warm-up, regime/jump features, and top-five order-book calculations. Passing tests checks software behavior only; it does not establish forecast quality.

## Deploy
Replace the files in your repository with this ZIP as a complete set. Python 3.10+ is required. Render build: `python -m pip install -r requirements.txt`; start: `python bot.py`. Keep API credentials in Render environment variables, never in source control. No paid resources are created by this package.

# (Earlier README follows; superseded where it conflicts with the section above)

## Deploy this complete package
Replace bot.py, free_gold.py, forecast_log.py (new) AND trade_controls.py together in your Render-linked
repository; also upload requirements.txt. No earlier generated runtime module needed.
This extends the supplied original bot.py and includes the agreed free-feed build,
compact reports and trade controls. It was not retrieved from your live deployment.
changes.diff is cumulative against the supplied original, not your deployed copy.
Python 3.10+ required.

Render Build Command: python -m pip install -r requirements.txt
Render Start Command: python bot.py
Commit the files, then deploy latest commit. Do not paste shell commands into Python.
Keep Telegram/Kalshi keys in Render Environment, not source control.
Existing DASHBOARD_TOKEN works. New optional env: GOLD_VOL_FLOOR, GOLD_BASIS_USD, MOMENTUM_ALERTS, FORECAST_DB. No paid Pyth subscription or gold feed API key.
ENABLE_LIVE_ORDERS defaults false; keep false during deployment/demo testing.
The package does NOT reset existing Render environment values (including true).

## Trading UI: 15-minute only
/predict and /test send one formatted report, with ONE 15-minute and ONE hourly forecast (hourly has no controls).
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
Alerts trigger on a qualifying reversal+edge (and, only if MOMENTUM_ALERTS=true, a momentum threshold crossing), with
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
>300s gaps reset 1m/5m histories; missing single minutes are tolerated.
~20 completed 1m candles warm-up (hourly: 12 completed 5m candles, 1m fallback before). Gauges need a full lookback.
Heuristic momentum/regime/jump model with a bounded book adjustment and proxy-basis uncertainty; scores UNCALIBRATED until /calibration has enough data.
Free spot is a PROXY, not exact Pyth index. No profitability or accuracy guarantee.
Kalshi published yes/no is sole source of official paper-trade settlement.
The code currently prefers expected_expiration_time, then close_time, then expiration_time; validate this against each market's actual contract terms before treating the timestamp as the contractual observation endpoint.
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
python -m unittest -v
79 automated tests passed, including new independent-model, order-book, jump, and boundary-freshness tests. Captured output: TEST_RESULTS.txt.
Coverage added in this update: regime classification, jump detection, acceleration/momentum, top-five book imbalance and bounded book adjustment,
full-lookback gauges, expiry order and implausible expiry, hourly own prediction and pinned strike selection, settlement recording and boundary-sample freshness, unpublished results,
Brier vs Kalshi mid, log failure safety, fee multiplier, min-time alert gate, momentum alerts off, /api/log auth.
Coverage earlier: explicit selection, independent model/manual sides, switching revocation,
message/owner binding, no orders on selector taps, duplicate handling, missing asks,
UI-edit failures, 15m-only trading, partial/zero/fractional/full liquidation, netting,
pagination, timeout/no retry, live disable, buy price cap, report cardinality and gauges.
Local HTTP mock exercised sell request/response and dashboard auth. Runtime bot import,
route registration, syntax and legacy write blocking checked. No live Kalshi order,
live Telegram UI, browser rendering or deployment test performed. Nothing deployed.

## Package contents
bot.py, free_gold.py, trade_controls.py, requirements.txt, env.example,
test_update.py, test_gold_model.py, gold_model.py, forecast_log.py, TEST_RESULTS.txt, README.md, MANIFEST.json, env.example.
env.example is a reference, not automatically loaded. No credentials included.
Manifest provides SHA256/size; ZIP verified for integrity before delivery.
