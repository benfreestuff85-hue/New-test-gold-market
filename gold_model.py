"""Independent, transparent gold forecasting features for 15m and 1h contracts.

All inputs are sampled free spot-proxy candles, not the contractual Pyth index.
Features are deliberately bounded; they are diagnostics/priors, not trained claims.
"""
import math
import statistics


def _safe_logret(a, b):
    if a is None or b is None or a <= 0 or b <= 0:
        return None
    return math.log(b / a)


def _ema(values, span):
    if not values:
        return None
    alpha = 2.0 / (span + 1.0)
    out = values[0]
    for x in values[1:]:
        out = alpha * x + (1.0 - alpha) * out
    return out


def extract_features(candles_1m, candles_5m, spot, minutes):
    """Return timeframe-specific volatility, trend, acceleration, jump and regime features."""
    use_five = minutes == 60 and len(candles_5m) >= 14
    candles = candles_5m if use_five else candles_1m
    tf_min = 5 if use_five else 1
    if minutes == 60 and not use_five and len(candles) >= 2:
        span = (candles[-1].get('timestamp', 0) - candles[0].get('timestamp', 0)) / 60.0
        if span < 58:
            return None  # never label a short lookback as an hourly model
    if minutes == 15 and len(candles) >= 2:
        span = (candles[-1].get('timestamp', 0) - candles[0].get('timestamp', 0)) / 60.0
        if span < 18:
            return None
    closes = [float(c['close']) for c in candles if c.get('close') and c['close'] > 0]
    if len(closes) < 12:
        return None
    rets = [_safe_logret(a, b) for a, b in zip(closes, closes[1:])]
    rets = [r for r in rets if r is not None]
    if len(rets) < 10:
        return None
    per_min = [r / math.sqrt(tf_min) for r in rets]
    recent = per_min[-min(12, len(per_min)):]
    long = per_min[-min(48, len(per_min)):]
    sigma = math.sqrt(sum(x*x for x in long) / len(long))
    sigma = max(sigma, 1e-7)
    recent_sigma = math.sqrt(sum(x*x for x in recent) / len(recent))
    regime_ratio = recent_sigma / sigma
    regime = 'high' if regime_ratio >= 1.6 else ('quiet' if regime_ratio <= 0.65 else 'normal')

    # Returns are computed from current spot against sampled candle closes.
    def horizon_return(n):
        idx = max(0, len(closes) - 1 - max(1, int(round(n / tf_min))))
        return _safe_logret(closes[idx], spot)
    r1 = horizon_return(1)
    r3 = horizon_return(3)
    r5 = horizon_return(5)
    r15 = horizon_return(15)
    r60 = horizon_return(60)
    short = r5 if r5 is not None else r3
    medium = r15 if r15 is not None else r5
    longret = r60 if r60 is not None else r15
    # A blended, horizon-aware trend score. This is a capped drift prior, not a fitted coefficient.
    def z(ret, mins):
        return ret / max(sigma * math.sqrt(max(mins, 1)), 1e-8) if ret is not None else 0.0
    trend_z = 0.50*z(short, 5) + 0.35*z(medium, 15) + 0.15*z(longret, 60)
    momentum = max(-3.0, min(3.0, trend_z))
    acceleration = (z(short, 5) - z(medium, 15)) if short is not None and medium is not None else 0.0

    last_z = per_min[-1] / sigma
    median = statistics.median(per_min[-min(30, len(per_min)):])
    mad = statistics.median(abs(x - median) for x in per_min[-min(30, len(per_min)):])
    robust_scale = max(1.4826 * mad, sigma * 0.35, 1e-8)
    jump_z = (per_min[-1] - median) / robust_scale
    jump = abs(jump_z) >= 3.0
    # Momentum drift is attenuated during jump conditions and in a high-volatility regime.
    attenuation = 0.45 if jump else (0.65 if regime == 'high' else 1.0)
    drift_per_min = max(-0.0008, min(0.0008, momentum * sigma / math.sqrt(max(minutes, 1)) * 0.35)) * attenuation
    return {
        'sigma': sigma, 'recent_sigma': recent_sigma, 'regime_ratio': regime_ratio,
        'volatility_regime': regime, 'momentum_z': momentum,
        'momentum_score': 100.0 * math.tanh(momentum / 2.0),
        'acceleration_z': acceleration, 'jump_z': jump_z, 'jump_detected': jump,
        'drift_per_min': drift_per_min, 'return_1m': r1, 'return_3m': r3,
        'return_5m': r5, 'return_15m': r15, 'return_60m': r60,
        'source_timeframe_minutes': tf_min,
    }


def orderbook_features(book):
    """Compute depth-weighted imbalance and microprice offset from top levels."""
    if not isinstance(book, dict):
        return None
    bid = book.get('yes_bid'); ask = book.get('yes_ask')
    bids = book.get('yes_bid_levels') or []
    asks = book.get('yes_ask_levels') or []
    if bid is None or ask is None or ask < bid:
        return None
    bid_depth = sum(max(0.0, float(q)) for _, q in bids[:5])
    ask_depth = sum(max(0.0, float(q)) for _, q in asks[:5])
    total = bid_depth + ask_depth
    if total <= 0:
        return None
    imbalance = (bid_depth - ask_depth) / total
    mid = (float(bid) + float(ask)) / 2.0
    micro = (float(ask) * bid_depth + float(bid) * ask_depth) / total
    spread = max(0.0, float(ask) - float(bid))
    micro_offset = max(-1.0, min(1.0, (micro - mid) / max(spread / 2.0, 0.5)))
    return {'depth_imbalance': imbalance, 'microprice_offset': micro_offset,
            'bid_depth_5': bid_depth, 'ask_depth_5': ask_depth}


def probability(spot, strike, seconds, sigma_per_min, kind, features=None, book_features=None, basis_usd=0.75):
    """Bounded P(YES): drift/vol regime/jump-aware proxy; order-book term is deliberately tiny."""
    if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in (spot, strike, seconds, sigma_per_min)) or spot <= 0 or strike <= 0:
        return None
    horizon = max(float(seconds), 1.0) / 60.0
    f = features or {}
    sigma = max(float(sigma_per_min), 1e-7)
    regime = f.get('volatility_regime', 'normal')
    if regime == 'high':
        sigma *= 1.12
    elif regime == 'quiet':
        sigma *= 0.92
    if f.get('jump_detected'):
        sigma *= 1.15
    drift = float(f.get('drift_per_min') or 0.0)
    variance = sigma*sigma*horizon + (float(basis_usd) / spot)**2
    z = (math.log(spot / strike) + drift*horizon - 0.5*sigma*sigma*horizon) / math.sqrt(max(variance, 1e-12))
    p = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    # Book contributes a small log-odds nudge only when both sides have depth.
    if book_features:
        signal = 0.65*book_features.get('depth_imbalance', 0.0) + 0.35*book_features.get('microprice_offset', 0.0)
        p = 1.0 / (1.0 + math.exp(-max(-12, min(12, math.log(max(p,1e-6)/max(1-p,1e-6)) + 0.10*signal))))
    if str(kind).startswith('less'):
        p = 1.0 - p
    return min(0.98, max(0.02, p))
