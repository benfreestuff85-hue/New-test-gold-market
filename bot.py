import os
import math
import time
import json
import asyncio
import logging
import statistics
from datetime import datetime, timezone
from collections import deque
import aiohttp
from aiohttp import web

# =====================================================================
# CONFIGURATION & ENVIRONMENT VARIABLES
# =====================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("KalshiGoldBot")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
PORT = int(os.getenv("PORT", 10000))

# Pyth Network Hermes API for Metal.XAU/USD (Spot Gold)
PYTH_XAU_FEED_ID = "0x765d2ba906dbc32ca17cc11f5310a89e9ee1f6420508c63861f2f8ba4ee34bb2"
PYTH_HERMES_URL = (
    f"https://hermes.pyth.network/v2/updates/price/latest?ids[]={PYTH_XAU_FEED_ID}"
)

# Kalshi Public Trade API v2
KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
GOLD_SERIES_TICKERS = ["KXGOLD15M", "KXGOLD1H", "KXGOLDD"]

# Strategy & Edge Parameters
POLL_INTERVAL_SECONDS = 5          # Spot price sampling interval
SCAN_INTERVAL_SECONDS = 15         # Kalshi market & reversal scan interval
RSI_PERIOD = 7                     # Fast RSI for 15m/1h scalping
BB_PERIOD = 20                     # Bollinger Band period
BB_STD_DEV = 2.0                   # Bollinger Band standard deviation
RSI_OVERSOLD = 28.0                # Bullish exhaustion threshold
RSI_OVERBOUGHT = 72.0              # Bearish exhaustion threshold
MIN_NET_EDGE_CENTS = 6.0           # Minimum edge (in cents) AFTER Kalshi taker fees
ALERT_COOLDOWN_SECONDS = 300       # 5m cooldown per contract ticker

# Time-Decay Guardrails (minutes before expiration)
MIN_MINUTES_15M = 3.5              # Skip last 3.5 mins of 15m windows
MIN_MINUTES_1H = 10.0              # Skip last 10 mins of 1h windows


# =====================================================================
# CANDLE BUILDER & TECHNICAL ANALYSIS ENGINE
# =====================================================================
class CandleBuilder:
    """Builds rolling 1-minute and 5-minute OHLC candles in memory."""

    def __init__(self, timeframe_seconds: int, max_candles: int = 120):
        self.tf = timeframe_seconds
        self.candles = deque(maxlen=max_candles)
        self.current_bucket = None
        self.current_candle = None

    def update(self, price: float, ts: float):
        bucket = int(ts // self.tf) * self.tf
        if self.current_bucket != bucket:
            if self.current_candle is not None:
                self.candles.append(self.current_candle)
            self.current_bucket = bucket
            self.current_candle = {
                "timestamp": bucket,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
            }
        else:
            self.current_candle["high"] = max(self.current_candle["high"], price)
            self.current_candle["low"] = min(self.current_candle["low"], price)
            self.current_candle["close"] = price

    def get_closes(self) -> list[float]:
        closes = [c["close"] for c in self.candles]
        if self.current_candle:
            closes.append(self.current_candle["close"])
        return closes

    def get_all_candles(self) -> list[dict]:
        all_c = list(self.candles)
        if self.current_candle:
            all_c.append(self.current_candle)
        return all_c


def compute_rsi(closes: list[float], period: int = 7) -> float | None:
    if len(closes) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(-period, 0):
        delta = closes[i] - closes[i - 1]
        if delta >= 0:
            gains.append(delta)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(delta))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def compute_bollinger_bands(
    closes: list[float], period: int = 20, num_std: float = 2.0
) -> tuple[float, float, float] | None:
    if len(closes) < period:
        return None
    window = closes[-period:]
    sma = sum(window) / period
    std = statistics.pstdev(window)
    upper = sma + (num_std * std)
    lower = sma - (num_std * std)
    return lower, sma, upper


def compute_realized_vol_per_min(closes: list[float]) -> float:
    """Calculates per-minute log return standard deviation (with safe floor)."""
    if len(closes) < 10:
        # Default fallback: ~15% annualized gold volatility scaled to 1 minute
        return 0.00025
    log_returns = [
        math.log(closes[i] / closes[i - 1])
        for i in range(1, len(closes))
        if closes[i - 1] > 0
    ]
    if len(log_returns) < 5:
        return 0.00025
    vol = statistics.pstdev(log_returns)
    return max(vol, 0.00012)


def detect_reversal_signal(candle_builder: CandleBuilder, label: str) -> dict:
    """
    Checks 1m or 5m candles for Bullish or Bearish mean-reversion setups.
    Returns dict with direction ('BULLISH', 'BEARISH', or 'NEUTRAL') and reason.
    """
    closes = candle_builder.get_closes()
    candles = candle_builder.get_all_candles()
    rsi = compute_rsi(closes, RSI_PERIOD)
    bb = compute_bollinger_bands(closes, BB_PERIOD, BB_STD_DEV)

    if rsi is None or bb is None or len(candles) < 2:
        return {
            "direction": "NEUTRAL",
            "rsi": rsi,
            "bb": bb,
            "reason": f"Warming up {label} candles ({len(closes)}/{BB_PERIOD})",
        }

    lower_bb, mid_bb, upper_bb = bb
    last_c = candles[-1]
    prev_c = candles[-2]
    price = last_c["close"]

    # Wick calculations for rejection confirmation
    candle_range = max(last_c["high"] - last_c["low"], 0.01)
    lower_wick = min(last_c["open"], last_c["close"]) - last_c["low"]
    upper_wick = last_c["high"] - max(last_c["open"], last_c["close"])

    # 1. BULLISH REVERSAL: Oversold RSI + Lower BB touch/pierce + bounce/wick
    touched_lower_bb = last_c["low"] <= lower_bb or prev_c["low"] <= lower_bb
    bullish_bounce = (price > last_c["open"]) or (lower_wick / candle_range >= 0.45)

    if rsi <= RSI_OVERSOLD and touched_lower_bb and bullish_bounce:
        return {
            "direction": "BULLISH",
            "rsi": rsi,
            "bb": bb,
            "reason": (
                f"{label} RSI({RSI_PERIOD}) oversold at {rsi:.1f} + "
                f"Lower BB (${lower_bb:,.2f}) bounce"
            ),
        }

    # 2. BEARISH REVERSAL: Overbought RSI + Upper BB touch/pierce + rejection
    touched_upper_bb = last_c["high"] >= upper_bb or prev_c["high"] >= upper_bb
    bearish_reject = (price < last_c["open"]) or (upper_wick / candle_range >= 0.45)

    if rsi >= RSI_OVERBOUGHT and touched_upper_bb and bearish_reject:
        return {
            "direction": "BEARISH",
            "rsi": rsi,
            "bb": bb,
            "reason": (
                f"{label} RSI({RSI_PERIOD}) overbought at {rsi:.1f} + "
                f"Upper BB (${upper_bb:,.2f}) rejection"
            ),
        }

    return {
        "direction": "NEUTRAL",
        "rsi": rsi,
        "bb": bb,
        "reason": f"{label} within normal range (RSI {rsi:.1f})",
    }


# =====================================================================
# BINARY FAIR-VALUE & KALSHI FEE MATH
# =====================================================================
def norm_cdf(x: float) -> float:
    """Standard normal cumulative distribution function."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def estimate_prob_above_strike(
    spot: float,
    strike: float,
    minutes_left: float,
    vol_per_min: float,
    reversal_direction: str,
    bb_mid: float | None,
) -> float:
    """
    Calculates probability (0.0 to 1.0) that Gold finishes at or above `strike`.
    Applies a slight mean-reversion drift toward the Bollinger midline when a
    confirmed technical reversal triggers.
    """
    if minutes_left <= 0 or spot <= 0 or strike <= 0:
        return 1.0 if spot >= strike else 0.0

    sigma_t = vol_per_min * math.sqrt(minutes_left)
    if sigma_t <= 1e-6:
        return 1.0 if spot >= strike else 0.0

    # Mean-reversion drift adjustment (capped at 35% of distance to midline)
    expected_spot = spot
    if bb_mid is not None:
        if reversal_direction == "BULLISH" and bb_mid > spot:
            expected_spot = spot + 0.35 * (bb_mid - spot)
        elif reversal_direction == "BEARISH" and bb_mid < spot:
            expected_spot = spot - 0.35 * (spot - bb_mid)

    z_score = math.log(expected_spot / strike) / sigma_t
    return max(0.01, min(0.99, norm_cdf(z_score)))


def kalshi_taker_fee_cents(price_cents: float) -> float:
    """
    Kalshi's taker fee formula on short-term series: 0.07 * P * (1 - P) dollars.
    Returns the fee in cents per contract.
    """
    p = max(0.01, min(0.99, price_cents / 100.0))
    return 0.07 * p * (1.0 - p) * 100.0


# =====================================================================
# BOT STATE & API CLIENTS
# =====================================================================
class GoldAnalyzerState:
    def __init__(self):
        self.latest_spot: float | None = None
        self.latest_spot_ts: float = 0.0
        self.spot_source: str = "Initializing"
        self.candles_1m = CandleBuilder(60, max_candles=120)
        self.candles_5m = CandleBuilder(300, max_candles=120)
        self.last_alerts: dict[str, float] = {}
        self.active_markets_count: int = 0
        self.last_telegram_update_id: int = 0


STATE = GoldAnalyzerState()


async def fetch_spot_gold_price(session: aiohttp.ClientSession) -> float | None:
    """
    Primary: Pyth Network Hermes v2 Metal.XAU/USD (exact Kalshi settlement source).
    Fallback: Swissquote public XAU/USD feed if Hermes is unreachable.
    """
    try:
        async with session.get(PYTH_HERMES_URL, timeout=4) as resp:
            if resp.status == 200:
                data = await resp.json()
                parsed = data.get("parsed", [])
                if parsed:
                    price_obj = parsed[0].get("price", {})
                    raw_price = float(price_obj.get("price", 0))
                    expo = int(price_obj.get("expo", 0))
                    spot = raw_price * (10 ** expo)
                    if spot > 500:
                        STATE.spot_source = "Pyth XAU/USD"
                        return round(spot, 2)
    except Exception as e:
        logger.debug(f"Pyth fetch warning: {e}")

    # Fallback: Swissquote public XAU/USD quote
    try:
        fallback_url = "https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/XAU/USD"
        async with session.get(fallback_url, timeout=4) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data and "spreadProfilePrices" in data[0]:
                    prices = data[0]["spreadProfilePrices"][0]
                    mid = ( float(prices["bid"]) + float(prices["ask"]) ) / 2.0
                    if mid > 500:
                        STATE.spot_source = "Swissquote XAU/USD (Fallback)"
                        return round(mid, 2)
    except Exception as e:
        logger.debug(f"Fallback gold fetch warning: {e}")

    return None


def parse_strike_from_market(m: dict) -> float | None:
    """Extracts numeric gold target strike from a Kalshi market object."""
    for key in ("floor_strike", "cap_strike", "strike_price", "custom_strike"):
        val = m.get(key)
        if isinstance(val, (int, float)) and val > 500:
            return float(val)
    # Parse from subtitle or title if numeric field isn't populated
    text = f"{m.get('subtitle', '')} {m.get('yes_sub_title', '')} {m.get('title', '')}"
    cleaned = text.replace(",", "").replace("$", "")
    for token in cleaned.split():
        try:
            val = float(token)
            if 1500 < val < 15000:
                return val
        except ValueError:
            continue
    return None


def parse_minutes_remaining(m: dict) -> float | None:
    close_str = m.get("close_time") or m.get("expiration_time")
    if not close_str:
        return None
    try:
        dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return (dt - now).total_seconds() / 60.0
    except Exception:
        return None


async def fetch_kalshi_gold_markets(session: aiohttp.ClientSession) -> list[dict]:
    """Polls Kalshi public market endpoint for open 15m and 1h Gold markets."""
    markets = []
    for series in GOLD_SERIES_TICKERS:
        url = f"{KALSHI_BASE_URL}/markets?series_ticker={series}&status=open&limit=50"
        try:
            async with session.get(url, timeout=6) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for m in data.get("markets", []):
                        m["series_ticker"] = series
                        markets.append(m)
        except Exception as e:
            logger.debug(f"Kalshi market fetch error ({series}): {e}")
    return markets


async def fetch_orderbook_best_asks(
    session: aiohttp.ClientSession, ticker: str
) -> dict | None:
    """
    Fetches Kalshi orderbook and computes implied YES Ask and NO Ask.
    In Kalshi's binary order book:
      - YES Ask = 100 - highest NO Bid
      - NO Ask  = 100 - highest YES Bid
    """
    url = f"{KALSHI_BASE_URL}/markets/{ticker}/orderbook"
    try:
        async with session.get(url, timeout=5) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            ob = data.get("orderbook", {})
            yes_bids = ob.get("yes") or []
            no_bids = ob.get("no") or []

            best_yes_bid = yes_bids[-1][0] if yes_bids else None
            best_yes_bid_qty = yes_bids[-1][1] if yes_bids else 0

            best_no_bid = no_bids[-1][0] if no_bids else None
            best_no_bid_qty = no_bids[-1][1] if no_bids else 0

            yes_ask = (100 - best_no_bid) if best_no_bid is not None else None
            yes_ask_qty = best_no_bid_qty if best_no_bid is not None else 0

            no_ask = (100 - best_yes_bid) if best_yes_bid is not None else None
            no_ask_qty = best_yes_bid_qty if best_yes_bid is not None else 0

            return {
                "yes_bid": best_yes_bid,
                "yes_ask": yes_ask,
                "yes_ask_qty": yes_ask_qty,
                "no_bid": best_no_bid,
                "no_ask": no_ask,
                "no_ask_qty": no_ask_qty,
            }
    except Exception as e:
        logger.debug(f"Orderbook error for {ticker}: {e}")
        return None


# =====================================================================
# TELEGRAM MESSAGING & COMMAND HANDLER
# =====================================================================
async def send_telegram_message(
    session: aiohttp.ClientSession, text: str, chat_id: str | None = None
):
    target_chat = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_TOKEN or not target_chat:
        logger.warning("Telegram credentials missing; skipping message send.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": target_chat,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        async with session.post(url, json=payload, timeout=6) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.warning(f"Telegram API warning ({resp.status}): {body}")
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")


def build_status_report() -> str:
    spot_str = f"${STATE.latest_spot:,.2f}" if STATE.latest_spot else "Waiting..."
    sig_1m = detect_reversal_signal(STATE.candles_1m, "1m")
    sig_5m = detect_reversal_signal(STATE.candles_5m, "5m")

    rsi_1m = f"{sig_1m['rsi']:.1f}" if sig_1m["rsi"] is not None else "Warming up"
    rsi_5m = f"{sig_5m['rsi']:.1f}" if sig_5m["rsi"] is not None else "Warming up"

    bb_1m_str = "Warming up"
    if sig_1m["bb"]:
        l_bb, m_bb, u_bb = sig_1m["bb"]
        bb_1m_str = f"${l_bb:,.2f} / ${m_bb:,.2f} /${u_bb:,.2f}"

    return (
        f"<b>📊 KALSHI GOLD ANALYZER STATUS</b>\n"
        f"<b>Spot Gold:</b> <code>{spot_str}</code> ({STATE.spot_source})\n"
        f"<b>1m Candles Built:</b> {len(STATE.candles_1m.get_closes())}\n"
        f"<b>1m RSI(7):</b> <code>{rsi_1m}</code> | <b>Signal:</b> {sig_1m['direction']}\n"
        f"<b>1m Bollinger:</b> <code>{bb_1m_str}</code>\n"
        f"<b>5m RSI(7):</b> <code>{rsi_5m}</code> | <b>Signal:</b> {sig_5m['direction']}\n"
        f"<b>Open Kalshi Gold Markets:</b> {STATE.active_markets_count}\n\n"
        f"<i>Commands:</i> /status | /markets | /test"
    )


async def poll_telegram_commands(session: aiohttp.ClientSession):
    """Listens for /status, /markets, and /test commands from your phone."""
    if not TELEGRAM_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    params = {"offset": STATE.last_telegram_update_id + 1, "timeout": 1}
    try:
        async with session.get(url, params=params, timeout=5) as resp:
            if resp.status != 200:
                return
            data = await resp.json()
            for item in data.get("result", []):
                STATE.last_telegram_update_id = item["update_id"]
                msg = item.get("message", {})
                text = (msg.get("text") or "").strip().lower()
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if not text or not chat_id:
                    continue

                if text.startswith("/status") or text.startswith("/start"):
                    await send_telegram_message(session, build_status_report(), chat_id)

                elif text.startswith("/test"):
                    await send_telegram_message(
                        session,
                        "✅ <b>Test Alert Successful!</b>\nYour Kalshi Gold 15m/1h Reversal Scanner is online and connected.",
                        chat_id,
                    )

                elif text.startswith("/markets"):
                    markets = await fetch_kalshi_gold_markets(session)
                    if not markets:
                        await send_telegram_message(
                            session,
                            "ℹ️ No open <code>KXGOLD15M</code> or <code>KXGOLD1H</code> markets found right now (note: Kalshi 15m Gold markets pause over the weekend).",
                            chat_id,
                        )
                    else:
                        lines = ["<b>🏅 ACTIVE KALSHI GOLD MARKETS</b>"]
                        for m in markets[:8]:
                            tkr = m.get("ticker")
                            strike = parse_strike_from_market(m)
                            mins = parse_minutes_remaining(m)
                            s_str = f"${strike:,.2f}" if strike else "N/A"
                            m_str = f"{mins:.1f}m" if mins is not None else "N/A"
                            lines.append(f"• <code>{tkr}</code> | Strike: <b>{s_str}</b> | Closes: {m_str}")
                        await send_telegram_message(session, "\n".join(lines), chat_id)
    except Exception as e:
        logger.debug(f"Telegram poll error: {e}")


# =====================================================================
# CORE SCANNER & OPPORTUNITY ENGINE
# =====================================================================
async def evaluate_markets_and_alert(session: aiohttp.ClientSession):
    spot = STATE.latest_spot
    if spot is None:
        return

    sig_1m = detect_reversal_signal(STATE.candles_1m, "1m")
    sig_5m = detect_reversal_signal(STATE.candles_5m, "5m")

    # Prioritize 1m signal for 15m markets, or 5m signal if active
    active_signal = sig_1m if sig_1m["direction"] != "NEUTRAL" else sig_5m
    direction = active_signal["direction"]

    markets = await fetch_kalshi_gold_markets(session)
    STATE.active_markets_count = len(markets)

    if direction == "NEUTRAL" or not markets:
        return

    vol_per_min = compute_realized_vol_per_min(STATE.candles_1m.get_closes())
    bb_mid = active_signal["bb"][1] if active_signal["bb"] else None
    now_ts = time.time()

    for m in markets:
        ticker = m.get("ticker", "")
        series = m.get("series_ticker", "")
        if not ticker:
            continue

        # Check alert cooldown per contract
        if now_ts - STATE.last_alerts.get(ticker, 0) < ALERT_COOLDOWN_SECONDS:
            continue

        strike = parse_strike_from_market(m)
        mins_left = parse_minutes_remaining(m)
        if strike is None or mins_left is None:
            continue

        # Enforce Time-Decay Guardrails
        if "15M" in series and (mins_left < MIN_MINUTES_15M or mins_left > 16.0):
            continue
        if "1H" in series and (mins_left < MIN_MINUTES_1H or mins_left > 65.0):
            continue

        ob = await fetch_orderbook_best_asks(session, ticker)
        if not ob:
            continue

        prob_yes = estimate_prob_above_strike(
            spot=spot,
            strike=strike,
            minutes_left=mins_left,
            vol_per_min=vol_per_min,
            reversal_direction=direction,
            bb_mid=bb_mid,
        )
        fair_yes_cents = prob_yes * 100.0
        fair_no_cents = (1.0 - prob_yes) * 100.0

        # Evaluate BULLISH reversal -> Look for cheap YES ask
        if direction == "BULLISH" and ob["yes_ask"] is not None:
            ask_cents = float(ob["yes_ask"])
            fee_cents = kalshi_taker_fee_cents(ask_cents)
            net_edge = fair_yes_cents - ask_cents - fee_cents

            if net_edge >= MIN_NET_EDGE_CENTS and 8 <= ask_cents <= 85:
                STATE.last_alerts[ticker] = now_ts
                alert_msg = (
                    f"🟢 <b>BULLISH GOLD REVERSAL — BUY YES</b>\n"
                    f"<b>Contract:</b> <code>{ticker}</code> ({mins_left:.1f}m left)\n"
                    f"<b>Target Strike:</b> <code>${strike:,.2f}</code> \vert{} <b>Spot:</b> <code>${spot:,.2f}</code>\n"
                    f"<b>Trigger:</b> {active_
