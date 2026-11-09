import os
import re
import uuid
import math
import time
import json
import base64
import asyncio
import logging
import statistics
from datetime import datetime, timezone
from collections import deque
import aiohttp
from aiohttp import web

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa, ed25519

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

# Kalshi API Credentials (from Render Environment Variables)
KALSHI_KEY_ID = os.getenv("KALSHI_KEY_ID", "").strip()
KALSHI_PRIVATE_KEY_RAW = os.getenv("KALSHI_PRIVATE_KEY", "").strip()
# Set KALSHI_USE_DEMO="true" in Render if using demo.kalshi.co keys
KALSHI_USE_DEMO = os.getenv("KALSHI_USE_DEMO", "false").strip().lower() == "true"

KALSHI_HOST = (
    "https://external-api.demo.kalshi.co"
    if KALSHI_USE_DEMO
    else "https://external-api.kalshi.com"
)
KALSHI_API_PREFIX = "/trade-api/v2"
KALSHI_BASE_URL = f"{KALSHI_HOST}{KALSHI_API_PREFIX}"
GOLD_SERIES_TICKERS = ["KXGOLD15M", "KXGOLD1H", "KXGOLDD"]

# Pyth Network Hermes API for Metal.XAU/USD (Spot Gold)
PYTH_XAU_FEED_ID = "0x765d2ba906dbc32ca17cc11f5310a89e9ee1f6420508c63861f2f8ba4ee34bb2"
PYTH_HERMES_URL = (
    f"https://hermes.pyth.network/v2/updates/price/latest?ids[]={PYTH_XAU_FEED_ID}"
)

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
SLIPPAGE_BUFFER_CENTS = 1          # 1c buffer on button tap for immediate fill

# Time-Decay Guardrails (minutes before expiration)
MIN_MINUTES_15M = 3.5              # Skip last 3.5 mins of 15m windows
MIN_MINUTES_1H = 10.0              # Skip last 10 mins of 1h windows


# =====================================================================
# KALSHI CRYPTOGRAPHIC AUTHENTICATION (Ed25519 & RSA-PSS)
# =====================================================================
def load_kalshi_private_key(raw_pem: str):
    """
    Normalizes single-line or multiline PEM strings from Render env vars
    and loads either an Ed25519 or RSA private key.
    """
    if not raw_pem:
        return None
    cleaned = raw_pem.strip().strip('"').strip("'").replace("\\n", "\n")

    # Reconstruct PEM line breaks if Render flattened the string into spaces
    if "-----BEGIN" in cleaned and "\n" not in cleaned:
        m = re.match(
            r"(-----BEGIN [A-Z0-9 ]+-----)\s*(.+?)\s*(-----END [A-Z0-9 ]+-----)",
            cleaned,
        )
        if m:
            header, body, footer = m.groups()
            body_clean = re.sub(r"\s+", "", body)
            cleaned = f"{header}\n{body_clean}\n{footer}"

    try:
        return serialization.load_pem_private_key(
            cleaned.encode("utf-8"), password=None
        )
    except Exception as e:
        logger.error(f"Failed to parse KALSHI_PRIVATE_KEY: {e}")
        return None


PRIVATE_KEY_OBJ = load_kalshi_private_key(KALSHI_PRIVATE_KEY_RAW)


def build_kalshi_auth_headers(method: str, path_with_prefix: str) -> dict:
    """
    Signs `timestamp_ms + METHOD + path_without_query` using Ed25519 or RSA-PSS.
    """
    if not KALSHI_KEY_ID or PRIVATE_KEY_OBJ is None:
        raise ValueError("Kalshi API credentials (KALSHI_KEY_ID / KALSHI_PRIVATE_KEY) not configured.")

    path_clean = path_with_prefix.split("?")[0]
    timestamp_ms = str(int(time.time() * 1000))
    msg_bytes = f"{timestamp_ms}{method.upper()}{path_clean}".encode("utf-8")

    if isinstance(PRIVATE_KEY_OBJ, ed25519.Ed25519PrivateKey):
        sig_bytes = PRIVATE_KEY_OBJ.sign(msg_bytes)
    elif isinstance(PRIVATE_KEY_OBJ, rsa.RSAPrivateKey):
        sig_bytes = PRIVATE_KEY_OBJ.sign(
            msg_bytes,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
    else:
        raise ValueError("Unsupported private key type.")

    signature_b64 = base64.b64encode(sig_bytes).decode("utf-8")
    return {
        "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        "KALSHI-ACCESS-SIGNATURE": signature_b64,
        "Content-Type": "application/json",
    }


async def place_kalshi_order(
    session: aiohttp.ClientSession,
    ticker: str,
    outcome: str,
    ask_cents: int,
    contracts: int,
) -> tuple[bool, str]:
    """
    Submits an Immediate-Or-Cancel limit order via Kalshi V2 (`/portfolio/events/orders`).
    - Buying YES at `ask_cents` -> V2 `side: "bid"`, `price: ask_cents / 100`
    - Buying NO  at `ask_cents` -> V2 `side: "ask"`, `price: (100 - ask_cents) / 100`
    """
    if not KALSHI_KEY_ID or PRIVATE_KEY_OBJ is None:
        return (
            False,
            "⚠️ <b>Missing Kalshi Credentials:</b> Add <code>KALSHI_KEY_ID</code> and <code>KALSHI_PRIVATE_KEY</code> in your Render Environment settings.",
        )

    capped_price_cents = min(99, max(1, ask_cents + SLIPPAGE_BUFFER_CENTS))

    if outcome.upper() == "YES":
        v2_side = "bid"
        yes_price_dollars = capped_price_cents / 100.0
    else:
        # Buying NO at X cents is selling YES (ask) at (100 - X) cents on the YES book
        v2_side = "ask"
        yes_price_dollars = (100 - capped_price_cents) / 100.0

    path = f"{KALSHI_API_PREFIX}/portfolio/events/orders"
    url = f"{KALSHI_HOST}{path}"
    payload = {
        "ticker": ticker,
        "client_order_id": str(uuid.uuid4()),
        "side": v2_side,
        "count": f"{contracts:.2f}",
        "price": f"{yes_price_dollars:.4f}",
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
    }

    try:
        headers = build_kalshi_auth_headers("POST", path)
        async with session.post(url, headers=headers, json=payload, timeout=8) as resp:
            body_text = await resp.text()
            if resp.status in (200, 201):
                data = json.loads(body_text)
                order_id = data.get("order_id", "N/A")
                filled = data.get("fill_count", "0.00")
                remaining = data.get("remaining_count", "0.00")
                return (
                    True,
                    f"✅ <b>KALSHI ORDER SUBMITTED (IOC)</b>\n"
                    f"<b>Ticker:</b> <code>{ticker}</code>\n"
                    f"<b>Action:</b> BUY {contracts}x <b>{outcome.upper()}</b> (Limit ≤ {capped_price_cents}¢)\n"
                    f"<b>Filled Contracts:</b> <code>{filled}</code> (Remaining: {remaining})\n"
                    f"<b>Order ID:</b> <code>{order_id}</code>",
                )
            else:
                return (
                    False,
                    f"❌ <b>Order Rejected (HTTP {resp.status}):</b>\n<code>{body_text[:300]}</code>",
                )
    except Exception as e:
        return False, f"❌ <b>Execution Exception:</b> <code>{e}</code>"


async def fetch_kalshi_balance(session: aiohttp.ClientSession) -> str:
    """Checks Kalshi portfolio balance to verify API authentication."""
    if not KALSHI_KEY_ID or PRIVATE_KEY_OBJ is None:
        return "⚠️ <code>KALSHI_KEY_ID</code> or <code>KALSHI_PRIVATE_KEY</code> not set in Render."

    path = f"{KALSHI_API_PREFIX}/portfolio/balance"
    url = f"{KALSHI_HOST}{path}"
    try:
        headers = build_kalshi_auth_headers("GET", path)
        async with session.get(url, headers=headers, timeout=6) as resp:
            text = await resp.text()
            if resp.status == 200:
                data = json.loads(text)
                # Handle both integer cents and dollar string representations
                bal_raw = data.get("balance", 0)
                if isinstance(bal_raw, (int, float)):
                    bal_usd = bal_raw / 100.0
                else:
                    bal_usd = float(bal_raw)
                return f"💰 <b>Kalshi Account Balance:</b> <code>${bal_usd:,.2f}</code>"
            return f"⚠️ <b>Auth Error (HTTP {resp.status}):</b> <code>{text[:200]}</code>"
    except Exception as e:
        return f"⚠️ <b>Balance Check Error:</b> <code>{e}</code>"


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
    gains, losses = [], []
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
    return sma - (num_std * std), sma, sma + (num_std * std)


def compute_realized_vol_per_min(closes: list[float]) -> float:
    if len(closes) < 10:
        return 0.00025
    log_returns = [
        math.log(closes[i] / closes[i - 1])
        for i in range(1, len(closes))
        if closes[i - 1] > 0
    ]
    if len(log_returns) < 5:
        return 0.00025
    return max(statistics.pstdev(log_returns), 0.00012)


def detect_reversal_signal(candle_builder: CandleBuilder, label: str) -> dict:
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

    candle_range = max(last_c["high"] - last_c["low"], 0.01)
    lower_wick = min(last_c["open"], last_c["close"]) - last_c["low"]
    upper_wick = last_c["high"] - max(last_c["open"], last_c["close"])

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
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def estimate_prob_above_strike(
    spot: float,
    strike: float,
    minutes_left: float,
    vol_per_min: float,
    reversal_direction: str,
    bb_mid: float | None,
) -> float:
    if minutes_left <= 0 or spot <= 0 or strike <= 0:
        return 1.0 if spot >= strike else 0.0
    sigma_t = vol_per_min * math.sqrt(minutes_left)
    if sigma_t <= 1e-6:
        return 1.0 if spot >= strike else 0.0

    expected_spot = spot
    if bb_mid is not None:
        if reversal_direction == "BULLISH" and bb_mid > spot:
            expected_spot = spot + 0.35 * (bb_mid - spot)
        elif reversal_direction == "BEARISH" and bb_mid < spot:
            expected_spot = spot - 0.35 * (spot - bb_mid)

    z_score = math.log(expected_spot / strike) / sigma_t
    return max(0.01, min(0.99, norm_cdf(z_score)))


def kalshi_taker_fee_cents(price_cents: float) -> float:
    p = max(0.01, min(0.99, price_cents / 100.0))
    return 0.07 * p * (1.0 - p) * 100.0


# =====================================================================
# BOT STATE & MARKET DATA CLIENTS
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

    try:
        fallback_url = "https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/XAU/USD"
        async with session.get(fallback_url, timeout=4) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data and "spreadProfilePrices" in data[0]:
                    prices = data[0]["spreadProfilePrices"][0]
                    mid = (float(prices["bid"]) + float(prices["ask"])) / 2.0
                    if mid > 500:
                        STATE.spot_source = "Swissquote XAU/USD (Fallback)"
                        return round(mid, 2)
    except Exception as e:
        logger.debug(f"Fallback gold fetch warning: {e}")
    return None


def parse_strike_from_market(m: dict) -> float | None:
    for key in ("floor_strike", "cap_strike", "strike_price", "custom_strike"):
        val = m.get(key)
        if isinstance(val, (int, float)) and val > 500:
            return float(val)
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
    markets = []
    for series in GOLD_SERIES_TICKERS:
        url = f"{KALSHI_BASE_URL}/markets?series_ticker={series}&status=open&limit=50"
        try:
            async with session.get(url, timeout=
