"""SQLite log of every forecast and its official Kalshi settlement. Stdlib only.

Purpose: measure whether the model beats the Kalshi mid price (Brier score) and how far
the free spot proxy is from the Pyth index Kalshi settles on (basis). Every method is
best-effort and never raises into the trading loop.
"""
import csv
import io
import logging
import math
import sqlite3
import statistics

logger = logging.getLogger("KalshiGoldBot")

# Forecast snapshots scored at these seconds-before-end (nearest row within LEAD_TOL).
LEADS = {15: (600, 300, 180), 60: (2700, 1800, 900)}
LEAD_TOL = 45
LOG_EVERY_SECONDS = 30
MIN_SAMPLE = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets(
  ticker TEXT PRIMARY KEY, minutes INTEGER, strike REAL, strike_type TEXT,
  close_time TEXT, expected_exp TEXT, expiration_time TEXT, end_ts REAL, first_seen REAL,
  result TEXT, expiration_value REAL, settle_strike REAL,
  proxy_start REAL, proxy_end REAL, settled_at REAL);
CREATE TABLE IF NOT EXISTS forecasts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, ticker TEXT, minutes INTEGER, spot REAL,
  strike REAL, seconds_left INTEGER, sigma_min REAL, p_yes REAL, momentum REAL,
  yes_bid REAL, yes_ask REAL, feed_age REAL);
CREATE INDEX IF NOT EXISTS forecasts_ticker ON forecasts(ticker);
"""


def number(value):
    try:
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


class ForecastLog:
    def __init__(self, path):
        self.path = path
        self.db = None
        self.disabled = False
        self.last = {}

    def _db(self):
        if self.db is None and not self.disabled:
            try:
                self.db = sqlite3.connect(self.path)
                self.db.executescript(SCHEMA)
            except Exception as exc:
                logger.warning("Forecast log disabled (%s): %s", self.path, exc)
                self.disabled = True
                self.db = None
        return self.db

    def record(self, market, row, sigma_min, spot, feed_age, now, end_ts):
        """Register the contract and, if the model produced a probability, log one forecast."""
        db = self._db()
        if db is None or not market or end_ts is None:
            return
        try:
            ticker = market["ticker"]
            db.execute(
                "INSERT OR IGNORE INTO markets(ticker,minutes,first_seen,close_time,expected_exp,expiration_time)"
                " VALUES(?,?,?,?,?,?)",
                (ticker, row["minutes"], now, market.get("close_time"),
                 market.get("expected_expiration_time"), market.get("expiration_time")),
            )
            db.execute(
                "UPDATE markets SET strike=?, strike_type=?, end_ts=? WHERE ticker=? AND result IS NULL",
                (row.get("target"), market.get("strike_type"), end_ts, ticker),
            )
            if row.get("yes_score") is not None and now - self.last.get(ticker, 0) >= LOG_EVERY_SECONDS:
                quotes = row.get("quotes") or {}
                db.execute(
                    "INSERT INTO forecasts(ts,ticker,minutes,spot,strike,seconds_left,sigma_min,p_yes,momentum,"
                    "yes_bid,yes_ask,feed_age) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (now, ticker, row["minutes"], spot, row.get("target"), row.get("seconds_left"), sigma_min,
                     row["yes_score"] / 100.0, row.get("momentum"), quotes.get("yes_bid"), quotes.get("yes_ask"),
                     feed_age),
                )
                self.last[ticker] = now
                if len(self.last) > 500:
                    self.last = {ticker: now}
            db.commit()
        except Exception as exc:
            logger.warning("Forecast log write failed: %s", exc)

    def pending(self, now):
        db = self._db()
        if db is None:
            return []
        try:
            return db.execute(
                "SELECT ticker,minutes,end_ts FROM markets WHERE settled_at IS NULL AND end_ts IS NOT NULL"
                " AND end_ts<=? AND end_ts>=? ORDER BY end_ts LIMIT 20",
                (now - 30, now - 172800),
            ).fetchall()
        except Exception as exc:
            logger.warning("Forecast log read failed: %s", exc)
            return []

    def settle(self, ticker, result, expiration_value, settle_strike, proxy_start, proxy_end, now):
        db = self._db()
        if db is None:
            return
        try:
            db.execute(
                "UPDATE markets SET result=?, expiration_value=?, settle_strike=?, proxy_start=?, proxy_end=?,"
                " settled_at=? WHERE ticker=?",
                (result, expiration_value, settle_strike, proxy_start, proxy_end, now, ticker),
            )
            db.commit()
        except Exception as exc:
            logger.warning("Forecast log settle failed: %s", exc)

    # ------------------------------------------------------------------ reports
    def _scored(self, db, minutes, lead):
        pairs = []
        markets = db.execute(
            "SELECT ticker,result FROM markets WHERE minutes=? AND result IN ('yes','no')", (minutes,)
        ).fetchall()
        for ticker, result in markets:
            hit = db.execute(
                "SELECT p_yes,yes_bid,yes_ask FROM forecasts WHERE ticker=? AND ABS(seconds_left-?)<=?"
                " ORDER BY ABS(seconds_left-?) LIMIT 1",
                (ticker, lead, LEAD_TOL, lead),
            ).fetchone()
            if hit:
                p, bid, ask = hit
                mid = (bid + ask) / 200.0 if bid is not None and ask is not None else None
                pairs.append((p, mid, 1.0 if result == "yes" else 0.0))
        return pairs

    @staticmethod
    def _stats(values):
        if not values:
            return "n=0"
        sd = statistics.pstdev(values) if len(values) > 1 else 0.0
        return (f"n={len(values)} mean {statistics.mean(values):+.2f} sd {sd:.2f} "
                f"max|x| {max(abs(v) for v in values):.2f} USD")

    def calibration(self):
        db = self._db()
        if db is None:
            return "Forecast log unavailable."
        try:
            out = ["CALIBRATION (settled markets, official Kalshi result)",
                   "Brier: lower is better; 0.250 = always saying 50%."]
            total = 0
            for minutes, label in ((15, "15m"), (60, "1h")):
                for lead in LEADS[minutes]:
                    pairs = self._scored(db, minutes, lead)
                    total += len(pairs)
                    if not pairs:
                        out.append(f"{label} ~{lead // 60}m left: no scored markets yet")
                        continue
                    model = statistics.mean((p - y) ** 2 for p, _, y in pairs)
                    with_mid = [(p, m, y) for p, m, y in pairs if m is not None]
                    line = f"{label} ~{lead // 60}m left: n={len(pairs)} model {model:.3f}"
                    if with_mid:
                        same = statistics.mean((p - y) ** 2 for p, _, y in with_mid)
                        mid = statistics.mean((m - y) ** 2 for _, m, y in with_mid)
                        line += f" | same-rows model {same:.3f} vs Kalshi mid {mid:.3f} (n={len(with_mid)})"
                    if len(pairs) < MIN_SAMPLE:
                        line += f" [n<{MIN_SAMPLE}: too few]"
                    out.append(line)
            end, start, flips, checked = [], [], 0, 0
            for kind, ev, strike, ps, pe, result in db.execute(
                "SELECT strike_type,expiration_value,settle_strike,proxy_start,proxy_end,result FROM markets"
                " WHERE result IN ('yes','no')"
            ):
                if ev is not None and pe is not None:
                    end.append(pe - ev)
                if strike is not None and ps is not None:
                    start.append(ps - strike)
                if strike is not None and pe is not None and kind:
                    above = pe > strike if kind.startswith("greater") else pe < strike
                    checked += 1
                    flips += (("yes" if above else "no") != result)
            out += ["", "Proxy minus Kalshi settlement values (basis):",
                    "end   (proxy close vs expiration_value): " + self._stats(end),
                    "start (proxy close vs floor_strike):     " + self._stats(start),
                    f"Proxy end price on the wrong side of the strike vs official result: {flips}/{checked}"]
            if total == 0 and not end:
                out.append("No settled markets logged yet. Keep the bot running.")
            return "\n".join(out)
        except Exception as exc:
            return f"Calibration unavailable: {exc}"

    def export_csv(self, limit=50000):
        db = self._db()
        if db is None:
            return "log unavailable\n"
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["ts", "ticker", "minutes", "spot", "strike", "seconds_left", "sigma_min", "p_yes",
                         "momentum", "yes_bid", "yes_ask", "feed_age", "result", "expiration_value",
                         "settle_strike", "proxy_start", "proxy_end"])
        try:
            writer.writerows(db.execute(
                "SELECT f.ts,f.ticker,f.minutes,f.spot,f.strike,f.seconds_left,f.sigma_min,f.p_yes,f.momentum,"
                "f.yes_bid,f.yes_ask,f.feed_age,m.result,m.expiration_value,m.settle_strike,m.proxy_start,"
                "m.proxy_end FROM forecasts f LEFT JOIN markets m ON m.ticker=f.ticker ORDER BY f.id DESC LIMIT ?",
                (limit,),
            ))
        except Exception as exc:
            logger.warning("Forecast log export failed: %s", exc)
        return buf.getvalue()
