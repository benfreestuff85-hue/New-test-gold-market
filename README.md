# Free gold proxy / Kalshi bot update

This updates your supplied original bot.py and adds free_gold.py. Keep both together.
No Pyth Pro key, subscription, or Swissquote fallback is used. No secrets included.

## Setup
Python 3.10+. python -m pip install -r requirements.txt
Export your existing Telegram/Kalshi variables using env.example as a guide.
python bot.py
The original server binds 0.0.0.0, using PORT (default 10000).
Original root and /health endpoints remain; they expose price/tracker counts, not credentials.
Dashboard: http://127.0.0.1:10000/dashboard
For remote dashboard access, set a strong DASHBOARD_TOKEN and use HTTPS on your host.
Enter that token in the dashboard prompt. The dashboard itself cannot place orders.
The token is stored in browser sessionStorage, never in URLs.

## Price source and cost
GET https://api.gold-api.com/price/XAU ; no key.
The provider describes live endpoints as free with no rate limit. Provider terms and
availability may change. No payment or account signup was performed.
Official docs: https://gold-api.com/docs
The fetched response during this session had an OLD timestamp. We verified its schema,
NOT a currently fresh quote. The bot will reject that response. You must confirm current
updatedAt freshness from your deployment; the server may have delivered cached data here.
Do not raise the age limit merely to make an old quote appear live.

## What it preserves/adds
Existing Kalshi auth, balance, manual BUY callbacks, Telegram commands, RSI/Bollinger
signals, edge/spread checks and official-result paper tracker remain in bot.py.
New gauge/predict/feed/mute/unmute/threshold/help commands work in the terminal,
dashboard and authorized Telegram chat (use / for Telegram).
New 15m and 60m gauges, numeric projected spot and indicative target scores refresh
on the original 15-second scan schedule, plus network latency. Prices poll every
5 seconds after worker activity. Dashboard polls cached state every 2 seconds.
Real-time alerts are sent through existing Telegram and terminal logs; no browser push.
Hourly discovery uses KXGOLDH, not the original KXGOLD1H. If absent, the hourly rolling
gauge still appears with no contract; no invented ticker is used.

## Freshness and model limitations
updatedAt is required and checked; currency/symbol and finite price are validated.
Quotes older than GOLD_MAX_AGE_SECONDS (default 60), future >5s and out-of-order
quotes are rejected. Network/request failure disables freshness immediately.
No other provider fallback. Last known price remains visible with stale state.
Identical repeated provider timestamps are not added as new candle observations.
Gaps >120s reset both minute histories; noncontiguous history blocks model output.
Warm-up requires 20 completed one-minute candles. No historical candles are fabricated.
Gauge uses up to 15/60 observed completed minute candles. Once warming is complete,
the hourly gauge initially has less than a full hour of observed history.
Projection: clipped log-return drift with normal-return uncertainty. Scores are
uncalibrated, not validated probabilities. Provider may change its own upstream sources;
this bot cannot detect or guarantee consistency of those upstream provider changes.

## Proxy is NOT exact settlement
Free XAU/USD is NOT Kalshi's specified Pyth index. These values can differ.
Projected spot is a SPOT PROXY forecast. A structured contract threshold and explicit
strike direction are required for an indicative YES score. Unknown direction shows
above-target score only, not an invented YES interpretation.
Forecast horizon uses expected_expiration_time or expiration_time if present;
otherwise it is an explicitly labeled rolling 15/60-minute horizon. These timestamps
are NOT claimed to be verified contractual observation/aggregation times.
Original reversal-alert math still uses trading close_time as an approximate horizon,
not a replica of exact contractual observation. Do not interpret its model edge as
settlement-exact. No averaging/candlestick settlement adapter is implemented.
Prices are not rounded before indicators. Kalshi published yes/no results alone settle
paper trades; no local price comparison is used for official outcome tracking.
Paper PnL retains original fee-excluding, per-signal behavior, not account execution PnL.

## Live orders
ENABLE_LIVE_ORDERS=false by default. Existing manual BUY code remains but is blocked.
If explicitly enabled, feed freshness and quantity/outcome are checked, but the original
order endpoint/schema and callback price are unchanged and NOT live validated here.
The existing BUY button can retain an older ask price. Do not enable without testing
current API compatibility and execution behavior in your own account.
Mute suppresses alerts; it does not cancel trades or disable explicitly enabled BUYs.
No automatic orders were added or submitted. Test alerts are not actual market quotes.

## Validation
Run python -m unittest test_free_gold.py
11 offline tests passed: decoding, freshness, currency/symbol, invalid price, timestamp,
commands, stale masking, removed providers, structured targets, default order blocking.
Syntax checked. No live Kalshi account, Telegram delivery, browser, paid endpoint,
or deployment was tested. No claim of guaranteed forecast performance.

## Files
bot.py, free_gold.py, requirements.txt, env.example, test_free_gold.py, changes.diff.
Replace your prior paid-feed build with these files; no earlier generated module needed.
