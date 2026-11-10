# Gold bot update notes

## Implemented in this package

- Added `gold_model.py` with independent 15m/1h feature extraction, close-to-close realized volatility, regime ratio, multi-horizon returns, momentum, acceleration, robust jump detection, bounded drift prior, and bounded probability adjustment from top-five Kalshi book imbalance/microprice.
- Updated `free_gold.py` to call the separate engines, display/log diagnostics, include top-five book features in probability, and pin the selected hourly strike during the active contract window so it does not hop between strikes as spot moves.
- Updated `bot.py` order-book parser to retain top-five bid/ask depth and `CandleBuilder` to retain sample timestamp/count.
- Updated settlement-proxy sampling: a minute close is excluded from basis diagnostics unless the last proxy sample is within 20 seconds of the minute boundary. This is still a sampled spot proxy, not the contractual Pyth candle.
- Updated `forecast_log.py` with safe additive SQLite migrations for feature columns, CSV export of diagnostics, separate 15m opening-strike basis calculation (hourly fixed strikes excluded), and orderly SQLite close on application shutdown.
- Alert edge now subtracts the configured slippage buffer in addition to taker fee.
- Kept the existing Telegram/dashboard/trade controls. `ENABLE_LIVE_ORDERS` defaults to false; no deployment or live order was performed.
- Updated README and env example to remove the obsolete arbitrary volatility multiplier from the active model.

## Verification

- `python -m compileall -q .` completed successfully.
- `python -m unittest -v` — **79 tests passed**.
- Tests cover feature calculations, 15m/1h history handling, hourly strike pinning, jump detection, order-book depth, bounded book effect, sampled settlement-boundary freshness, forecast logging/calibration, trading gates, selectors, and HTTP mocks.
- The test suite includes mocked Kalshi HTTP interactions; it does not prove live API compatibility, live Telegram delivery, browser rendering, deployment health, forecast accuracy, or profitability.

## Important items intentionally not claimed as complete

- Exact real-time Pyth gold index integration is not implemented. The free Gold-API spot feed remains a proxy. Verify each series' current `settlement_sources` and contract terms against Kalshi before relying on the horizon/strike interpretation.
- The probability adjustments are heuristic, not fitted or calibrated. The 0.75 USD proxy-basis default is still an uncalibrated placeholder. Do not use the displayed probabilities as trading truth.
- The order-book feature is a single snapshot; book changes/velocity and synchronized quote/underlying timestamps are not yet modeled.
- No chronological out-of-sample replay, final-minute exact-index replay, regime-sliced Brier report, or paper-fill execution ledger was performed by this code update. These require historical synchronized Pyth/Kalshi data and must be completed before any live use.
- The bot continues to select one hourly fixed-strike contract, now pinned for its active window. It does not produce an aggregate probability across the entire hourly strike ladder.
- `ENABLE_LIVE_ORDERS=false` must remain false. No paid service, paid data feed, Render resource, or live deployment was created.
