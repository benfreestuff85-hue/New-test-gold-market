# Compact 15m / 1h forecast + 15m-only trading controls

## Package and deployment
This package extends the supplied original bot.py with the free spot-feed build and
new compact reports/trade controls. It was not fetched from your deployed repository.
Replace bot.py and free_gold.py and ADD trade_controls.py in the same project folder.
Keep your credentials in Render Environment, never in GitHub.
requirements.txt, test_update.py, changes.diff and TEST_RESULTS.txt are included.

Render Build Command: python -m pip install -r requirements.txt
Render Start Command: python bot.py
Commit all three runtime .py files and requirements.txt to the connected repo.
Deploy the latest commit. Old buttons expire/reject after restart; send /predict.
Dashboard: your Render service URL + /dashboard; use existing DASHBOARD_TOKEN.
Dashboard commands cannot submit orders. Server uses 0.0.0.0 and PORT (default 10000).
Root and /health retain the original public health/price summary. Remote dashboard
API access requires DASHBOARD_TOKEN. Use HTTPS; token is stored in sessionStorage.

## Requested compact layout
/predict and /test now use the same HTML report formatter as live forecast alerts.
They show ONE 15-minute section with projected finishing spot price, target,
YES/NO prediction, score and center-out filled rectangular gauge.
They show ONE hourly section with projected finish, time and gauge ONLY.
No hourly per-strike forecast list; no hourly YES/NO list; no hourly buttons.
/gauge gives just the two one-line gauges. /feed is a short diagnostic, not raw JSON.
Full contract lists remain confined to /markets (original limited listing).
/status and /stats preserve original paper-tracker diagnostics.

Negative gauge fills LEFT from center; positive fills RIGHT. Telegram uses monospace.
For each series, select the nearest unexpired active market that has opened and has
not reached trading close. At the same expiry choose nearest threshold to spot,
then lexical ticker for a deterministic tie-break. The 15m is normally a single market.
Hourly forecast uses the chosen expiry once, regardless of number of strikes.

## Buttons: ONLY the selected KXGOLD15M contract
Top inline-keyboard row: SELL ALL — 15M CONTRACT
Bottom row: Buy 1 / Buy 5 / Buy 10, for the predicted 15m YES or NO side, with quoted price.
Telegram places the keyboard below the body; top means top button row, not above text.
Buy buttons appear only with an available prediction and executable snapshot quote.
Sell may still appear during forecast warm-up, to permit exits of existing holdings.
No buttons if no active 15m contract is found. Missing forecasts do not invent a side.
/test is now a preview of the REAL current market, not the old fake TEST ticker.
If trading is enabled, its controls ARE real. Keep live trading disabled during testing.
Legacy BUY callbacks are rejected; they cannot bypass the new 15m restriction.

## Sell-all behavior and limits
Fresh position GET /portfolio/positions with exact ticker and pagination.
Fresh market status/close-time and identity check; only KXGOLD15M-prefix market AND
matching KXGOLD15M event are allowed. Hourly rejected even if a callback is forged.
Fresh orderbook, sorted bids, fixed-point quantities/prices.
Positive net YES -> ask on YES book at current best YES bid.
Negative net NO -> bid on YES book at 1 minus current best NO bid.
POST /portfolio/events/orders with reduce_only=true and immediate_or_cancel.
Server-side reduce-only caps quantity to the position; it cannot reverse the holding.
Quantity requests the full current net position, including fractional contracts.
Zero positions, non-netted/ambiguous records, closed markets, and empty books send no order.
Netting_enabled must explicitly be true. This does not liquidate separate hedged lots.
Only the default primary/key-bound subaccount is accessed; not every subaccount.
An unavailable gold proxy DOES NOT block selling; exit decisions use Kalshi data.

This is a price-protected attempt to sell all, NOT guaranteed liquidation. It sells
at the observed best bid or better. It does not sweep down to arbitrarily worse prices.
Thin liquidity or price movement may produce partial/zero fills. Unfilled IOC remainder
is canceled, not left resting. Report shows executed count, unfilled requested count,
average proceeds where available, order id and refreshed net position if accessible.
Fees excluded from sale-price display. Existing outstanding orders are NOT canceled;
those orders could subsequently alter your position. Other tickers are never sold.

## Buy safety
Same 15m market check and lock as selling. Quantities only 1/5/10.
Fresh executable ask; refuse if above the price displayed when buttons were created.
Fresh proxy required. Buy contexts expire after 90 seconds; sell contexts after 30 minutes.
Each context allows one buy action and one sell action. Repeat /predict for new controls.
Callbacks require owner chat AND owner Telegram user id. For groups set TELEGRAM_USER_ID.
Duplicate callback ids/actions are consumed before awaits. Buy/sell locks prevent overlap.
Opaque random callback tokens bind the original ticker/outcome on the server.

All writes default DISABLED: ENABLE_LIVE_ORDERS=false. To enable actual manual trades
set true deliberately. KALSHI_USE_DEMO controls which environment receives them.
Neither /predict nor live alerts submit orders by themselves. No automatic trades added.
Timeout/malformed response/other POST error creates an in-memory uncertain-submission
block for that ticker. No automatic retry. Check client_order_id in Kalshi before
further trading. There is no automatic uncertainty-clear command. Restart clears the
block and button contexts, but DOES NOT prove the old submission failed. Check account
first; duplicate prevention is not durable across a restart/deploy. Existing pending
orders and actions from another client may change positions concurrently.

## Free source and forecast caveats
https://api.gold-api.com/price/XAU (no key); no Pyth Pro, no Swissquote fallback.
Strict provider updatedAt validation: default max age 60s, max future skew 5s, reject
out-of-order, invalid symbol/currency and nonfinite price. Request failure immediately
marks feed unavailable. Last-known price stays displayed as stale; forecasts suppressed.
No guarantee of third-party uptime, refresh rate, data license or unchanged free terms.
A previous fetched public response was stale; verify freshness from your own deployment.

Candles use provider time, not receive time. Identical timestamps do not generate new
candles. >120s gaps reset history; noncontiguous minute history blocks model output.
Warm-up: 20 completed 1m candles. Hourly gauge initially uses less than a full hour.
Drift projection is clipped; uncertainty uses a normal-return approximation. Scores
are uncalibrated, not trained/validated probabilities, not recommendations.
Forecast expiry uses expected_expiration_time, expiration_time then close_time. These
are approximate horizons, NOT independently verified contractual observation windows.
No averaging/rounding/candlestick settlement adapter; FREE SPOT IS NOT EXACT PYTH INDEX.
Kalshi published yes/no remains the sole settlement source for original paper tracking.
Paper PnL retains the original fee-excluding per-signal behavior, not account PnL.

## Alerts and persistence
One combined alert message, same formatter as /predict. Trigger on a momentum threshold
crossing or the selected 15m RSI/Bollinger reversal passing original edge/spread filters.
Cooldown remains original 300 seconds; threshold default 65 with 10-point hysteresis.
No hourly paper-trade or hourly BUY/SELL buttons. 15m qualifying reversal paper signals
are still recorded. /mute suppresses alerts, not settlement tracking or manual trades.
Dashboard polls state every 2 seconds; scan interval 15 seconds plus network latency.
Terminal input is supported when stdin allows async readers; Telegram/dashboard remain
available when a host does not expose interactive stdin. No browser push notifications.
History, paper tracker, locks, callback tokens and uncertainty state are in memory.
Restart/deploy resets these. Free Render can sleep after 15 minutes without inbound
traffic, so it is not guaranteed 24/7 monitoring.

## Tests actually performed
python -m unittest -v test_update.py
37 automated tests passed: YES/NO liquidation mapping, partial/zero fills, fractional
positions, closed/expired/hourly rejection, empty books, non-netted rejection,
position pagination, duplicate/concurrent actions, POST uncertainty and no retries,
fresh-ask buys with cap, one report per timeframe, center-fill gauges, button binding,
stale suppression, owner authorization, plus local HTTP order-flow and dashboard-auth tests.
The HTTP order-flow test used an in-process MOCK server only, never Kalshi.
Bot import, route registration and syntax checked.
No real orders, live credentials, Telegram delivery, visual browser layout, or deployment
tested. Not a guarantee of execution or profitability. Test in Kalshi demo first.

## API references
https://docs.kalshi.com/api-reference/orders/create-order-v2.md
https://docs.kalshi.com/api-reference/portfolio/get-positions
https://docs.kalshi.com/api-reference/market/get-market
