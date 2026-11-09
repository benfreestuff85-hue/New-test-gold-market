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
import html
import sys
from free_gold import MONITOR, dashboard_auth, dashboard, state_api, command_api, terminal_commands
from trade_controls import Execution
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

# Kalshi API Credentials
KALSHI_KEY_ID = os.getenv("KALSHI_KEY_ID", "").strip()
KALSHI_PRIVATE_KEY_RAW = os.getenv("KALSHI_PRIVATE_KEY", "").strip()
KALSHI_USE_DEMO = os.getenv("KALSHI_USE_DEMO", "false").strip().lower() == "true"

KALSHI_HOST = (
    "https://external-api.demo.kalshi.co"
    if KALSHI_USE_DEMO
    else "https://external-api.kalshi.com"
)
KALSHI_API_PREFIX = "/trade-api/v2"
KALSHI_BASE_URL = f"{KALSHI_HOST}{KALSHI_API_PREFIX}"
GOLD_SERIES_TICKERS = ["KXGOLD15M", "KXGOLDH"]

# Free proxy mode: no Pyth subscription and no Swissquote fallback.
ENABLE_LIVE_ORDERS = os.getenv("ENABLE_LIVE_ORDERS", "false").lower() == "true"
TERMINAL_OUTPUT = os.getenv("TERMINAL_OUTPUT", "true").lower() == "true"

# Strategy, Filter & Paper Trading Parameters
POLL_INTERVAL_SECONDS = 5
SCAN_INTERVAL_SECONDS = 15
RSI_PERIOD = 7
BB_PERIOD = 20
BB_STD_DEV = 2.0
RSI_OVERSOLD = 28.0
RSI_OVERBOUGHT = 72.0
MIN_NET_EDGE_CENTS = 6.0
MAX_SPREAD_CENTS = 12.0
ALERT_COOLDOWN_SECONDS = 300
SLIPPAGE_BUFFER_CENTS = 1

MIN_MINUTES_15M = 3.5
MIN_MINUTES_1H = 10.0

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    logger.warning(
        "TELEGRAM_TOKEN / TELEGRAM_CHAT_ID not set: alerts and commands are disabled."
    )


def is_authorized(chat_id: str) -> bool:
    """Only the owner chat may run commands or press BUY buttons."""
    return bool(TELEGRAM_CHAT_ID) and chat_id == TELEGRAM_CHAT_ID


# =====================================================================
# KALSHI CRYPTOGRAPHIC AUTHENTICATION
# =====================================================================
def load_kalshi_private_key(raw_pem: str):
    if not raw_pem:
        return None
    cleaned = raw_pem.strip().strip('"').strip("'").replace("\\n", "\n")
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
    if not KALSHI_KEY_ID or PRIVATE_KEY_OBJ is None:
        raise ValueError("Kalshi API credentials not configured.")

    path_clean = path_with_prefix.split("?")[0]
    timestamp_ms = str(int(time.time() * 1000))
    msg_bytes = f"{timestamp_ms}{method.upper()}{path_clean}".encode("utf-8")

    # Ed25519 and RSA-PSS sign differently
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

    return {
        "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig_bytes).decode("utf-8"),
        "Content-Type": "application/json",
    }


async def place_kalshi_order(session, ticker, outcome, ask_cents, contracts):
    # Compatibility wrapper: stale alert price is ignored; fresh ask fetched by Execution.
    return await EXECUTION.buy(session, ticker, outcome, contracts)


async def fetch_kalshi_balance(session: aiohttp.ClientSession) -> str:
    if not KALSHI_KEY_ID or PRIVATE_KEY_OBJ is None:
        return "⚠️ Kalshi API credentials not set."
    path = f"{KALSHI_API_PREFIX}/portfolio/balance"
    url = f"{KALSHI_HOST}{path}"
    try:
        headers = build_kalshi_auth_headers("GET", path)
        async with session.get(url, headers=headers, timeout=6) as resp:
            if resp.status == 200:
                data = json.loads(await resp.text())
                bal = data.get("balance", 0)
                bal_usd = (bal / 100.0) if isinstance(bal, (int, float)) else float(bal)
                return f"💰 <b>Kalshi Balance:</b> <code>${bal_usd:,.2f}</code>"
            return f"⚠️ Balance check error (HTTP {resp.status})"
    except Exception as e:
        return f"⚠️ Balance check exception: {e}"


# =====================================================================
# CANDLE BUILDER & TECHNICAL ANALYSIS
# =====================================================================
class CandleBuilder:
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
            self.current_candle = {"timestamp": bucket, "open": price, "high": price, "low": price, "close": price}
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
        d = closes[i] - closes[i - 1]
        gains.append(d if d >= 0 else 0.0)
        losses.append(0.0 if d >= 0 else abs(d))
    avg_gain, avg_loss = sum(gains) / period, sum(losses) / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + (avg_gain / avg_loss)))


def compute_bollinger_bands(closes: list[float], period: int = 20, num_std: float = 2.0):
    if len(closes) < period:
        return None
    w = closes[-period:]
    sma = sum(w) / period
    std = statistics.pstdev(w)
    return sma - (num_std * std), sma, sma + (num_std * std)


def compute_realized_vol_per_min(closes: list[float]) -> float:
    if len(closes) < 10:
        return 0.00025
    log_rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
    return max(statistics.pstdev(log_rets) if len(log_rets) >= 5 else 0.00025, 0.00012)


def detect_reversal_signal(cb: CandleBuilder, label: str) -> dict:
    closes = cb.get_closes()
    candles = cb.get_all_candles()
    rsi = compute_rsi(closes, RSI_PERIOD)
    bb = compute_bollinger_bands(closes, BB_PERIOD, BB_STD_DEV)

    if rsi is None or bb is None or len(candles) < 2:
        return {"direction": "NEUTRAL", "rsi": rsi, "bb": bb, "reason": f"Warming up ({len(closes)}/{BB_PERIOD})"}

    lower_bb, mid_bb, upper_bb = bb
    last_c, prev_c = candles[-1], candles[-2]
    price = last_c["close"]
    cr = max(last_c["high"] - last_c["low"], 0.01)
    lw = min(last_c["open"], last_c["close"]) - last_c["low"]
    uw = last_c["high"] - max(last_c["open"], last_c["close"])

    if rsi <= RSI_OVERSOLD and (last_c["low"] <= lower_bb or prev_c["low"] <= lower_bb) and ((price > last_c["open"]) or (lw / cr >= 0.45)):
        return {"direction": "BULLISH", "rsi": rsi, "bb": bb, "reason": f"{label} RSI oversold ({rsi:.1f}) + Lower BB bounce"}

    if rsi >= RSI_OVERBOUGHT and (last_c["high"] >= upper_bb or prev_c["high"] >= upper_bb) and ((price < last_c["open"]) or (uw / cr >= 0.45)):
        return {"direction": "BEARISH", "rsi": rsi, "bb": bb, "reason": f"{label} RSI overbought ({rsi:.1f}) + Upper BB rejection"}

    return {"direction": "NEUTRAL", "rsi": rsi, "bb": bb, "reason": f"Normal (RSI {rsi:.1f})"}


# =====================================================================
# BINARY MATH & FEES
# =====================================================================
def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def estimate_prob_above_strike(spot, strike, mins_left, vol_min, direction, bb_mid) -> float:
    if mins_left <= 0 or spot <= 0 or strike <= 0:
        return 1.0 if spot >= strike else 0.0
    sigma_t = vol_min * math.sqrt(mins_left)
    if sigma_t <= 1e-6:
        return 1.0 if spot >= strike else 0.0
    exp_spot = spot
    if bb_mid:
        if direction == "BULLISH" and bb_mid > spot:
            exp_spot = spot + 0.35 * (bb_mid - spot)
        elif direction == "BEARISH" and bb_mid < spot:
            exp_spot = spot - 0.35 * (spot - bb_mid)
    return max(0.01, min(0.99, norm_cdf(math.log(exp_spot / strike) / sigma_t)))


def kalshi_taker_fee_cents(price_cents: float) -> float:
    p = max(0.01, min(0.99, price_cents / 100.0))
    return 0.07 * p * (1.0 - p) * 100.0


# =====================================================================
# PAPER TRADING WIN/LOSS TRACKER
# =====================================================================
class PaperTradingTracker:
    def __init__(self):
        self.open_trades: list[dict] = []
        self.settled_trades: list[dict] = []

    def log_signal(self, ticker: str, outcome: str, entry_cents: float, strike: float, edge: float, close_time_str: str):
        self.open_trades.append({
            "ticker": ticker,
            "outcome": outcome,
            "entry_cents": entry_cents,
            "strike": strike,
            "edge": edge,
            "close_time": close_time_str,
        })
        logger.info(f"Paper trade logged for {ticker} ({outcome} @ {entry_cents}¢)")

    async def check_settlements(self, session: aiohttp.ClientSession) -> list[str]:
        if not self.open_trades:
            return []

        now = datetime.now(timezone.utc)
        reports = []
        still_open = []

        for trade in self.open_trades:
            try:
                dt = datetime.fromisoformat(trade["close_time"].replace("Z", "+00:00"))
                if now < dt:
                    still_open.append(trade)
                    continue

                url = f"{KALSHI_BASE_URL}/markets/{trade['ticker']}"
                async with session.get(url, timeout=5) as resp:
                    if resp.status != 200:
                        still_open.append(trade)
                        continue
                    data = await resp.json()
                    m = data.get("market", {})
                    status = (m.get("status") or "").lower()
                    result = (m.get("result") or "").lower()

                    # Only score a trade once Kalshi has actually published yes/no.
                    # (status "closed" can appear before the result is known.)
                    if result in ("yes", "no"):
                        won = result == trade["outcome"].lower()
                        entry = trade["entry_cents"]
                        pnl = (100.0 - entry) if won else (-entry)

                        trade["result"] = result
                        trade["pnl_cents"] = pnl
                        self.settled_trades.append(trade)

                        icon = "✅ WIN" if won else "❌ LOSS"
                        reports.append(
                            f"🏁 <b>PAPER TRADE SETTLED: {icon}</b>\n"
                            f"<b>Ticker:</b> <code>{trade['ticker']}</code>\n"
                            f"<b>Prediction:</b> BUY {trade['outcome']} @ {entry:.1f}¢\n"
                            f"<b>Market Result:</b> {result.upper()} | <b>PnL:</b> <b>{pnl:+.1f}¢</b>"
                        )
                    elif status in ("finalized", "settled"):
                        logger.info(f"Dropping {trade['ticker']}: finalized with no yes/no result")
                    else:
                        still_open.append(trade)
            except Exception as e:
                logger.debug(f"Settlement error for {trade['ticker']}: {e}")
                still_open.append(trade)

        self.open_trades = still_open
        return reports

    def get_stats_summary(self) -> str:
        total = len(self.settled_trades)
        if total == 0:
            return "📊 <b>Paper Tracker:</b> No settled trades yet."
        wins = sum(1 for t in self.settled_trades if t.get("pnl_cents", 0) > 0)
        losses = total - wins
        win_rate = (wins / total) * 100.0
        net_pnl = sum(t.get("pnl_cents", 0) for t in self.settled_trades)
        return (
            f"<b>📊 PAPER TRADING ACCURACY STATS</b>\n"
            f"• Total Settled Signals: <b>{total}</b>\n"
            f"• Wins: {wins} | Losses: {losses}\n"
            f"• Win Rate: <b>{win_rate:.1f}%</b>\n"
            f"• Net Paper PnL: <b>{net_pnl:+,.1f}¢</b> per contract"
        )


# =====================================================================
# BOT STATE & CLIENTS
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
        self.tracker = PaperTradingTracker()


STATE = GoldAnalyzerState()
EXECUTION = Execution(sys.modules[__name__])


async def fetch_spot_gold_price(session):
    return await MONITOR.fetch(session, STATE)


def parse_strike(m):
    kind = m.get("strike_type")
    key = "floor_strike" if kind in ("greater", "greater_or_equal") else "cap_strike" if kind in ("less", "less_or_equal") else None
    if not key:
        return None
    try:
        value = float(m[key])
        return value if math.isfinite(value) and value > 0 else None
    except (KeyError, ValueError, TypeError):
        return None


def parse_close_time(m: dict) -> str | None:
    return m.get("close_time") or m.get("expiration_time")


async def fetch_kalshi_gold_markets(session):
    markets = []
    for series in GOLD_SERIES_TICKERS:
        cursor = None
        seen = set()
        for _ in range(20):
            params = {"series_ticker": series, "status": "open", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            try:
                async with session.get(f"{KALSHI_BASE_URL}/markets", params=params, timeout=6) as response:
                    response.raise_for_status()
                    data = await response.json()
                for market in data.get("markets", []):
                    market["series_ticker"] = series
                    markets.append(market)
                cursor = data.get("cursor")
                if not cursor or cursor in seen:
                    break
                seen.add(cursor)
            except Exception as exc:
                logger.warning("Market lookup failed for %s: %s", series, exc)
                break
    return markets


async def fetch_orderbook_details(session: aiohttp.ClientSession, ticker: str) -> dict | None:
    url = f"{KALSHI_BASE_URL}/markets/{ticker}/orderbook"
    try:
        async with session.get(url, timeout=5) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            ob = data.get("orderbook_fp") or data.get("orderbook", {})

            yd, nd = ob.get("yes_dollars"), ob.get("no_dollars")
            if yd: yd = sorted(yd, key=lambda level: float(level[0]))
            if nd: nd = sorted(nd, key=lambda level: float(level[0]))
            if yd is not None or nd is not None:
                yes_bid = round(float(yd[-1][0]) * 100) if yd else None
                yes_qty = float(yd[-1][1]) if yd else 0
                no_bid = round(float(nd[-1][0]) * 100) if nd else None
                no_qty = float(nd[-1][1]) if nd else 0
            else:
                yb, nb = ob.get("yes") or [], ob.get("no") or []
                yes_bid = int(yb[-1][0]) if yb else None
                yes_qty = yb[-1][1] if yb else 0
                no_bid = int(nb[-1][0]) if nb else None
                no_qty = nb[-1][1] if nb else 0

            yes_ask = (100 - no_bid) if no_bid is not None else None
            no_ask = (100 - yes_bid) if yes_bid is not None else None
            spread = (yes_ask - yes_bid) if (yes_ask is not None and yes_bid is not None) else 99.0

            return {
                "yes_bid": yes_bid,
                "yes_ask": yes_ask,
                "yes_ask_qty": no_qty if no_bid is not None else 0,
                "no_bid": no_bid,
                "no_ask": no_ask,
                "no_ask_qty": yes_qty if yes_bid is not None else 0,
                "spread": spread,
            }
    except Exception:
        return None


# =====================================================================
# TELEGRAM BOT & COMMANDS
# =====================================================================
def build_keyboard(ticker, outcome, ask_cents):
    # Legacy callers cannot attach hourly controls.
    if not ticker.startswith("KXGOLD15M-"):
        return None
    row = next((row for row in MONITOR.rows if row["minutes"] == 15), None)
    if not row or row["ticker"] != ticker:
        return None
    return MONITOR.keyboard()


async def send_telegram_message(session: aiohttp.ClientSession, text: str, chat_id: str | None = None, reply_markup: dict | None = None):
    target = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_TOKEN or not target:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": target, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        async with session.post(url, json=payload, timeout=6) as resp:
            if resp.status != 200:
                logger.warning(f"Telegram sendMessage failed: HTTP {resp.status}")
    except Exception as e:
        logger.warning(f"Telegram sendMessage error: {e}")


async def poll_telegram_updates(session: aiohttp.ClientSession):
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

                cb = item.get("callback_query")
                if cb:
                    await MONITOR.callback(session, sys.modules[__name__], cb)
                    continue

                msg = item.get("message", {})
                text = (msg.get("text") or "").strip().lower()
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if not text or not is_authorized(chat_id):
                    continue

                command = text.split()[0].split("@")[0]
                if command in ("/predict", "/test"):
                    await MONITOR.send_prediction(session, sys.modules[__name__], chat_id)
                    continue
                if command in ("/gauge", "/feed", "/mute", "/unmute", "/threshold", "/help"):
                    await send_telegram_message(session, "<pre>" + html.escape(MONITOR.command(text)) + "</pre>", chat_id)
                    continue

                if text.startswith("/status") or text.startswith("/start"):
                    if STATE.latest_spot is not None:
                        spot_line = f"${STATE.latest_spot:,.2f} ({STATE.spot_source})"
                    else:
                        spot_line = "waiting for price feed"
                    await send_telegram_message(
                        session,
                        STATE.tracker.get_stats_summary() + f"\n\n<b>Spot Gold:</b> {spot_line}",
                        chat_id,
                    )
                elif text.startswith("/balance"):
                    b_msg = await fetch_kalshi_balance(session)
                    await send_telegram_message(session, b_msg, chat_id)
                elif text.startswith("/stats"):
                    await send_telegram_message(session, STATE.tracker.get_stats_summary(), chat_id)
                elif text.startswith("/markets"):
                    markets = await fetch_kalshi_gold_markets(session)
                    lines = ["<b>🏅 OPEN KALSHI GOLD MARKETS</b>"]
                    if not markets:
                        lines.append("No open markets found.")
                    for m in markets[:8]:
                        strike = parse_strike(m)
                        strike_txt = f"${strike:,.2f}" if strike else "n/a"
                        lines.append(f"• <code>{m.get('ticker')}</code> | Strike: {strike_txt}")
                    await send_telegram_message(session, "\n".join(lines), chat_id)
    except Exception as e:
        logger.warning(f"Telegram poll error: {e}")


# =====================================================================
# SCANNER & OPPORTUNITY ENGINE
# =====================================================================
async def evaluate_markets_and_alert(session):
    await MONITOR.maybe_alert(session, sys.modules[__name__])


async def background_worker_loop(app: web.Application):
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        mode = "DEMO" if KALSHI_USE_DEMO else "LIVE"
        await send_telegram_message(
            session,
            f"🚀 <b>Kalshi Gold Bot online</b> ({mode})\n"
            "Commands: /predict | /gauge | /feed | /status | /stats | /markets | /balance",
        )

        last_scan = 0.0
        last_settlement_check = 0.0
        while True:
            try:
                now = time.time()
                spot = await fetch_spot_gold_price(session)
                await poll_telegram_updates(session)

                if now - last_scan >= SCAN_INTERVAL_SECONDS:
                    last_scan = now
                    await MONITOR.refresh(session, sys.modules[__name__])
                    await evaluate_markets_and_alert(session)
                    if TERMINAL_OUTPUT:
                        print(MONITOR.command("predict"), flush=True)

                if now - last_settlement_check >= 60.0:
                    last_settlement_check = now
                    settled_reports = await STATE.tracker.check_settlements(session)
                    for rep in settled_reports:
                        await send_telegram_message(session, rep)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Worker loop error: {e}")

            await asyncio.sleep(POLL_INTERVAL_SECONDS)


# =====================================================================
# RENDER WEB SERVER
# =====================================================================
async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({
        "status": "online",
        "spot_gold": STATE.latest_spot,
        "spot_source": STATE.spot_source,
        "quote_fresh": MONITOR.fresh(),
        "feed_status": MONITOR.status,
        "open_trades": len(STATE.tracker.open_trades),
        "settled_trades": len(STATE.tracker.settled_trades),
    })


async def worker_context(app: web.Application):
    """Start the background worker with the server and stop it cleanly on shutdown."""
    task = asyncio.create_task(background_worker_loop(app))
    commands = asyncio.create_task(terminal_commands())
    yield
    task.cancel()
    commands.cancel()
    await asyncio.gather(task, commands, return_exceptions=True)


def create_app() -> web.Application:
    app = web.Application(middlewares=[dashboard_auth], client_max_size=4096)
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/dashboard", dashboard)
    app.router.add_get("/api/gold", state_api)
    app.router.add_post("/api/command", command_api)
    app.cleanup_ctx.append(worker_context)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=PORT)
