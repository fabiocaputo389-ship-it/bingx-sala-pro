import os
import hmac
import hashlib
import time
import math
import threading
from decimal import Decimal, ROUND_DOWN
from urllib.parse import urlencode
import requests
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

BINGX_BASE_URL = os.environ.get("BINGX_BASE_URL", "https://open-api.bingx.com")
BINGX_API_KEY = os.environ.get("BINGX_API_KEY", "").strip()
BINGX_SECRET_KEY = os.environ.get("BINGX_SECRET_KEY", "").strip()

STATE_LOCK = threading.RLock()

AUTO_TRADE_STATE = {
    "enabled": True,
    "mode": "AUTOMATICO",
    "symbol": "XAUT-USDT",
    "tf": "1m",
    "qty": 0.0005,
    "leverage": 20,
    "min_score": 60,
    "min_confidence": 70.0,
    "max_concurrent_trades": 2,
    "close_at_candle_end": True,
    "rotate_after_open": True,
    "scan_category": "Materie Prime",
    "sound_on": True,
    "voice_on": True,
    "last_signal_key": None,
    "last_check_ts": 0,
    "last_action": "In attesa",
    "last_outcome": "In attesa di scansione",
    "locked_signals": {},
}

AUTO_CLOSE_WATCHLIST = {}
SIGNAL_HISTORY = []
SEEN_SIGNAL_KEYS = set()
ACTIVE_TPSL_REGISTRY = {}
MARKET_SCAN_CACHE = {
    "updated_at": 0,
    "category": None,
    "rows": [],
}

MARKET_CATALOG = {
    "Crypto Major": [
        "BTC-USDT", "ETH-USDT", "SOL-USDT", "BNB-USDT", "XRP-USDT",
        "ADA-USDT", "DOGE-USDT", "AVAX-USDT", "LINK-USDT", "DOT-USDT",
        "TRX-USDT", "SUI-USDT",
    ],
    "Materie Prime": [
        "XAUT-USDT", "PAXG-USDT", "GOLD-USDT", "SILVER-USDT",
        "XAG-USDT", "WTI-USDT", "BRENT-USDT", "NATGAS-USDT", "COPPER-USDT",
    ],
    "Azioni": [
        "AAPL-USDT", "MSFT-USDT", "NVDA-USDT", "AMZN-USDT",
        "GOOGL-USDT", "META-USDT", "TSLA-USDT", "PLTR-USDT", "COIN-USDT", "MSTR-USDT",
    ],
    "Forex": [
        "EUR-USDT",
    ],
}


def to_float(val, default=0.0):
    if val is None or val == "":
        return default
    try:
        f = float(val)
        return default if (math.isnan(f) or math.isinf(f)) else f
    except (ValueError, TypeError):
        return default


def fmt_num_str(val, decimals=None):
    if val is None:
        return "0"
    try:
        d = Decimal(str(val))
        if d.is_nan() or d.is_infinite():
            return "0"
        if decimals is not None:
            q = Decimal("10") ** (-int(decimals))
            d = d.quantize(q, rounding=ROUND_DOWN)
        s = format(d, "f")
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        return s if s else "0"
    except Exception:
        return str(val)


def format_price_smart(price: float, ref_price: float = None) -> float:
    p = to_float(price, 0.0)
    ref = to_float(ref_price if ref_price else p, p)
    if p <= 0:
        return 0.0
    if ref >= 10000:
        step = 0.1
    elif ref >= 1000:
        step = 0.01
    elif ref >= 10:
        step = 0.001
    elif ref >= 1:
        step = 0.0001
    elif ref >= 0.01:
        step = 0.00001
    else:
        step = 0.0000001
    d_price = Decimal(str(p))
    d_step = Decimal(str(step))
    floored = (d_price / d_step).to_integral_value(rounding=ROUND_DOWN) * d_step
    return float(floored)


def get_dynamic_leverage(capital_usdt: float, requested_leverage: int = None) -> int:
    cap = to_float(capital_usdt, 0.0)
    lev = int(to_float(requested_leverage if requested_leverage else 20, 20))
    if cap > 0 and cap <= 2.0:
        return max(lev, 20)
    return max(1, min(lev, 125))


def normalize_max_concurrent_trades(val, default=2) -> int:
    n = int(to_float(val, default))
    return max(1, min(n, 20))


def tf_to_seconds(tf: str) -> int:
    mapping = {
        "1m": 60,
        "3m": 180,
        "5m": 300,
        "15m": 900,
        "30m": 1800,
        "1h": 3600,
        "4h": 14400,
        "1d": 86400,
    }
    return mapping.get((tf or "1m").lower(), 60)


def candle_timing_info(tf: str) -> dict:
    clean_tf = (tf or "1m").lower()
    sec = tf_to_seconds(clean_tf)
    now_ts = int(time.time())
    candle_start_ts = (now_ts // sec) * sec
    candle_end_ts = candle_start_ts + sec
    remaining_sec = max(0, candle_end_ts - now_ts)
    return {
        "tf": clean_tf,
        "duration_sec": sec,
        "now_ts": now_ts,
        "candle_start_ts": candle_start_ts,
        "candle_end_ts": candle_end_ts,
        "remaining_sec": remaining_sec,
    }


def _sign_params(params: dict) -> str:
    sorted_items = []
    for k in sorted(params.keys()):
        v = params[k]
        if v is None or v == "":
            continue
        sorted_items.append((
            str(k),
            fmt_num_str(v) if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool) else str(v),
        ))
    query_string = urlencode(sorted_items)
    signature = hmac.new(
        BINGX_SECRET_KEY.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{query_string}&signature={signature}"


def bingx_req(method: str, path: str, params: dict = None, signed: bool = False):
    p = dict(params or {})
    headers = {}
    if BINGX_API_KEY:
        headers["X-BX-APIKEY"] = BINGX_API_KEY

    if signed:
        if not BINGX_API_KEY or not BINGX_SECRET_KEY:
            return {"code": -100, "msg": "Chiavi API BingX non configurate"}
        p["timestamp"] = int(time.time() * 1000)
        p.setdefault("recvWindow", 10000)
        signed_qs = _sign_params(p)
        url = f"{BINGX_BASE_URL}{path}?{signed_qs}"
    else:
        qs = urlencode({k: v for k, v in p.items() if v is not None and v != ""})
        url = f"{BINGX_BASE_URL}{path}" + (f"?{qs}" if qs else "")

    try:
        m = method.upper()
        if m == "GET":
            resp = requests.get(url, headers=headers, timeout=6)
        elif m == "POST":
            resp = requests.post(url, headers=headers, timeout=6)
        elif m == "DELETE":
            resp = requests.delete(url, headers=headers, timeout=6)
        else:
            resp = requests.request(m, url, headers=headers, timeout=6)
        data = resp.json()
        return data if isinstance(data, dict) else {"code": resp.status_code, "msg": "Risposta non JSON"}
    except Exception as exc:
        return {"code": -200, "msg": f"Errore rete BingX: {exc}"}


def fetch_klines(symbol: str, tf: str = "1m", limit: int = 60):
    interval_map = {
        "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m",
        "30m": "30m", "1h": "1h", "4h": "4h", "1d": "1d",
    }
    interval = interval_map.get((tf or "1m").lower(), "1m")
    res = bingx_req("GET", "/openApi/swap/v3/quote/klines", {
        "symbol": symbol,
        "interval": interval,
        "limit": limit,
    })
    raw_list = res.get("data")
    if not isinstance(raw_list, list) or not raw_list:
        res_v2 = bingx_req("GET", "/openApi/swap/v2/quote/klines", {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        })
        raw_list = res_v2.get("data") if isinstance(res_v2.get("data"), list) else []

    candles = []
    for item in raw_list:
        if isinstance(item, dict):
            c_open = to_float(item.get("open"), 0.0)
            c_high = to_float(item.get("high"), 0.0)
            c_low = to_float(item.get("low"), 0.0)
            c_close = to_float(item.get("close"), 0.0)
            c_vol = to_float(item.get("volume"), 0.0)
            c_time = int(to_float(item.get("time"), 0))
        elif isinstance(item, (list, tuple)) and len(item) >= 5:
            c_time = int(to_float(item[0], 0))
            c_open = to_float(item[1], 0.0)
            c_high = to_float(item[2], 0.0)
            c_low = to_float(item[3], 0.0)
            c_close = to_float(item[4], 0.0)
            c_vol = to_float(item[5] if len(item) > 5 else 0.0, 0.0)
        else:
            continue
        if c_close > 0:
            candles.append({
                "time": c_time,
                "open": c_open,
                "high": c_high,
                "low": c_low,
                "close": c_close,
                "volume": c_vol,
            })
    candles.sort(key=lambda x: x["time"])
    return candles


def _ema(values, period):
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _rsi(values, period=14):
    if len(values) < period + 1:
        return 50.0
    gains = []
    losses = []
    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(0.0, diff))
        losses.append(max(0.0, -diff))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 75.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 1)


def _macd_state(values):
    if len(values) < 26:
        return 0.0, 0.0, "NEUTRAL"
    ema12 = _ema(values, 12)
    ema26 = _ema(values, 26)
    macd_line = [a - b for a, b in zip(ema12, ema26)]
    signal_line = _ema(macd_line, 9)
    hist = macd_line[-1] - signal_line[-1]
    if macd_line[-1] > signal_line[-1] and hist > 0:
        st = "BULLISH"
    elif macd_line[-1] < signal_line[-1] and hist < 0:
        st = "BEARISH"
    else:
        st = "NEUTRAL"
    return round(macd_line[-1], 6), round(hist, 6), st


def compute_quant_brain(symbol: str, tf: str = "1m", forced_side: str = None) -> dict:
    clean_tf = (tf or "1m").lower()
    candles = fetch_klines(symbol, tf=clean_tf, limit=60)
    ticker = bingx_req("GET", "/openApi/swap/v2/quote/ticker", {"symbol": symbol})
    t_data = ticker.get("data") if isinstance(ticker.get("data"), dict) else {}
    live_price = to_float(t_data.get("lastPrice"), 0.0)
    change_pct = to_float(t_data.get("priceChangePercent"), 0.0)

    closes = [c["close"] for c in candles]
    if live_price <= 0 and closes:
        live_price = closes[-1]
    if live_price <= 0:
        live_price = 60000.0

    if len(closes) >= 20:
        ema_fast = _ema(closes, 9)[-1]
        ema_slow = _ema(closes, 21)[-1]
        recent_ranges = [max(1e-9, c["high"] - c["low"]) for c in candles[-14:]]
        atr = sum(recent_ranges) / len(recent_ranges)
        momentum_pct = ((closes[-1] - closes[-6]) / closes[-6] * 100.0) if len(closes) >= 6 and closes[-6] > 0 else 0.0
    else:
        ema_fast = live_price * 1.0005
        ema_slow = live_price * 0.9995
        atr = live_price * 0.004
        momentum_pct = 0.0

    rsi_val = _rsi(closes, 14)
    _, macd_hist, macd_status = _macd_state(closes)

    whale_score = 50.0
    if len(candles) >= 6:
        bull_imp = 0.0
        bear_imp = 0.0
        for c in candles[-12:]:
            rng = max(1e-9, c["high"] - c["low"])
            body = c["close"] - c["open"]
            vol = max(1.0, c.get("volume", 1.0))
            strength = abs(body) / rng
            if body >= 0:
                bull_imp += vol * (0.4 + strength)
            else:
                bear_imp += vol * (0.4 + strength)
        tot_imp = bull_imp + bear_imp
        if tot_imp > 0:
            whale_score = round(min(85.0, max(15.0, (bull_imp / tot_imp) * 100.0)), 1)
    bears_score = round(100.0 - whale_score, 1)

    atr_pct = (atr / live_price) if live_price > 0 else 0.004
    step_dist = max(live_price * max(atr_pct, 0.0025), live_price * 0.002)

    bull_confirmations = 0
    bear_confirmations = 0
    if ema_fast > ema_slow:
        bull_confirmations += 1
    elif ema_fast < ema_slow:
        bear_confirmations += 1
    if macd_status == "BULLISH":
        bull_confirmations += 1
    elif macd_status == "BEARISH":
        bear_confirmations += 1
    if momentum_pct > 0.08:
        bull_confirmations += 1
    elif momentum_pct < -0.08:
        bear_confirmations += 1
    if whale_score >= 54.0 and rsi_val < 72.0:
        bull_confirmations += 1
    elif bears_score >= 54.0 and rsi_val > 28.0:
        bear_confirmations += 1

    if forced_side in ("BUY", "SELL"):
        signal = forced_side
    else:
        if bull_confirmations >= 3 and bear_confirmations == 0 and rsi_val < 74.0:
            signal = "BUY"
        elif bear_confirmations >= 3 and bull_confirmations == 0 and rsi_val > 26.0:
            signal = "SELL"
        else:
            signal = "NEUTRAL"

    trend_health = int(round(min(96, max(18, 42 + max(bull_confirmations, bear_confirmations) * 12 + min(15, abs(momentum_pct) * 12)))))
    value_score = whale_score if signal != "SELL" else bears_score

    extreme_rsi = (signal == "BUY" and rsi_val >= 72.0) or (signal == "SELL" and rsi_val <= 28.0)
    high_volatility = atr_pct > 0.025
    conflicting_flow = (signal == "BUY" and macd_status == "BEARISH") or (signal == "SELL" and macd_status == "BULLISH")

    if signal == "NEUTRAL":
        risk_blocked = True
        risk_title = "ATTESA — CONFLUENZA INCOMPLETA"
        risk_reason = f"Conferme reali: {bull_confirmations} LONG / {bear_confirmations} SHORT • RSI {rsi_val} • MACD {macd_status}"
        sentiment_label = "ATTESA"
    elif extreme_rsi or high_volatility or conflicting_flow:
        risk_blocked = True
        risk_title = "NON ENTRARE — SEGNALE AD ALTO RISCHIO"
        if extreme_rsi:
            risk_reason = f"RSI estremo ({rsi_val}) su {symbol} • Rischio ritracciamento immediato"
        elif high_volatility:
            risk_reason = f"Volatilita' ATR elevata ({round(atr_pct * 100, 2)}%) su {symbol}"
        else:
            risk_reason = f"Divergenza MACD ({macd_status}) rispetto al segnale {signal} su {symbol}"
        sentiment_label = "CAUTELA"
    else:
        risk_blocked = False
        risk_title = "SEGNALE VERIFICATO — CONFLUENZA REALE"
        risk_reason = f"{max(bull_confirmations, bear_confirmations)}/4 indicatori allineati • MACD {macd_status} • RSI {rsi_val}"
        sentiment_label = "FORTE" if max(bull_confirmations, bear_confirmations) >= 4 else "CONFERMATO"

    raw_conf = 45.0 + max(bull_confirmations, bear_confirmations) * 11.5 + min(12.0, abs(momentum_pct) * 10.0)
    if risk_blocked and signal != "NEUTRAL":
        raw_conf = min(raw_conf, 64.0)
    confidence = round(min(94.0, max(25.0, raw_conf)), 1)
    win_rate = round(min(91.0, max(48.0, 52.0 + max(bull_confirmations, bear_confirmations) * 8.5 - (10.0 if risk_blocked else 0.0))), 1)

    if signal == "BUY":
        tp1 = format_price_smart(live_price + step_dist * 0.8, live_price)
        tp2 = format_price_smart(live_price + step_dist * 1.4, live_price)
        tp3 = format_price_smart(live_price + step_dist * 2.1, live_price)
        sl = format_price_smart(max(live_price * 0.0001, live_price - step_dist * 0.9), live_price)
        label = "SEGNALE CERTO LONG" if not risk_blocked else "LONG CON RISCHIO"
        score = int(round(min(100, max(61, confidence))))
        rec_text = f"Pressione: {int(round(whale_score))}% Balene / {int(round(bears_score))}% Orsi. MACD: {macd_status}."
    elif signal == "SELL":
        tp1 = format_price_smart(max(live_price * 0.0001, live_price - step_dist * 0.8), live_price)
        tp2 = format_price_smart(max(live_price * 0.0001, live_price - step_dist * 1.4), live_price)
        tp3 = format_price_smart(max(live_price * 0.0001, live_price - step_dist * 2.1), live_price)
        sl = format_price_smart(live_price + step_dist * 0.9, live_price)
        label = "SEGNALE CERTO SHORT" if not risk_blocked else "SHORT CON RISCHIO"
        score = -int(round(min(100, max(61, confidence))))
        rec_text = f"Pressione: {int(round(whale_score))}% Balene / {int(round(bears_score))}% Orsi. MACD: {macd_status}."
    else:
        tp1 = None
        tp2 = None
        tp3 = None
        sl = None
        label = "ATTESA • NO TRADE CERTO"
        score = int(round(min(55, max(15, confidence * 0.6))))
        rec_text = f"Pressione: {int(round(whale_score))}% Balene / {int(round(bears_score))}% Orsi. MACD: {macd_status}."

    p_fmt = format_price_smart(live_price, live_price)
    entry_min = format_price_smart(p_fmt * 0.9992, p_fmt)
    entry_max = format_price_smart(p_fmt * 1.0008, p_fmt)

    return {
        "symbol": symbol,
        "tf": clean_tf,
        "price": p_fmt,
        "change_pct": round(change_pct, 2),
        "signal": signal,
        "confidence": confidence,
        "win_rate": win_rate,
        "score": score,
        "label": label,
        "entry_min": entry_min,
        "entry_max": entry_max,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "tp": tp2 or tp1,
        "sl": sl,
        "ema_fast": format_price_smart(ema_fast, p_fmt),
        "ema_slow": format_price_smart(ema_slow, p_fmt),
        "atr": format_price_smart(atr, p_fmt),
        "rsi": rsi_val,
        "macd_hist": macd_hist,
        "macd_status": macd_status,
        "whales_pct": int(round(whale_score)),
        "bears_pct": int(round(bears_score)),
        "trend_health": trend_health,
        "value_score": int(round(value_score)),
        "sentiment": sentiment_label,
        "risk_blocked": risk_blocked,
        "risk_title": risk_title,
        "risk_reason": risk_reason,
        "recommendation_side": signal if signal in ("BUY", "SELL") else "WAIT",
        "recommendation_text": rec_text,
        "candles": candles[-35:],
    }


def _extract_ticker_map(raw_tickers):
    out = {}
    if not isinstance(raw_tickers, list):
        return out
    for item in raw_tickers:
        if not isinstance(item, dict):
            continue
        sym = str(item.get("symbol") or "").strip().upper()
        if not sym:
            continue
        last_p = to_float(item.get("lastPrice") or item.get("price"), 0.0)
        chg = to_float(item.get("priceChangePercent") or item.get("changeRate"), 0.0)
        if abs(chg) <= 1.0 and "%" not in str(item.get("priceChangePercent") or ""):
            if item.get("priceChangePercent") is None and item.get("changeRate") is not None:
                chg = chg * 100.0
        out[sym] = {
            "price": format_price_smart(last_p, last_p) if last_p > 0 else 0.0,
            "change_pct": round(chg, 2),
        }
    return out


CATEGORY_ALIASES = {
    "TUTTI": "Tutti",
    "ALL": "Tutti",
    "CRYPTO": "Crypto Major",
    "CRYPTO MAJOR": "Crypto Major",
    "CRIPTO": "Crypto Major",
    "MATERIE PRIME": "Materie Prime",
    "COMMODITIES": "Materie Prime",
    "ORO": "Materie Prime",
    "GOLD": "Materie Prime",
    "AZIONI": "Azioni",
    "STOCKS": "Azioni",
    "FOREX": "Forex",
    "EUR": "Forex",
    "EURUSDT": "Forex",
    "EUR-USDT": "Forex",
}


def normalize_category(category: str) -> str:
    raw = (category or "").strip()
    if raw in MARKET_CATALOG or raw == "Tutti":
        return raw
    mapped = CATEGORY_ALIASES.get(raw.upper())
    if mapped:
        return mapped
    for k in MARKET_CATALOG.keys():
        if k.upper() == raw.upper():
            return k
    return "Tutti"


def _pool_for_category(category: str):
    cat = normalize_category(category)
    if cat == "Tutti":
        out = []
        seen = set()
        for syms in MARKET_CATALOG.values():
            for s in syms:
                if s not in seen:
                    seen.add(s)
                    out.append(s)
        return out
    return list(MARKET_CATALOG.get(cat, []))


def scan_markets(category: str = "Tutti", query: str = "", force_refresh: bool = False):
    cat = normalize_category(category)
    q = (query or "").strip().upper()
    q_alt = "EUR-USDT" if q == "EURUSDT" else q
    search_cat = CATEGORY_ALIASES[q] if (q and q in CATEGORY_ALIASES and q not in ("EURUSDT", "EUR-USDT")) else None

    with STATE_LOCK:
        cache_age = time.time() - MARKET_SCAN_CACHE.get("updated_at", 0)
        has_cache = bool(MARKET_SCAN_CACHE.get("rows"))
        cached_cat = MARKET_SCAN_CACHE.get("category")
        locked_map = dict(AUTO_TRADE_STATE.get("locked_signals") or {})

    if not force_refresh and has_cache and cache_age < 5 and cached_cat == "Tutti":
        all_rows = list(MARKET_SCAN_CACHE["rows"])
    else:
        bulk = bingx_req("GET", "/openApi/swap/v2/quote/ticker")
        t_map = _extract_ticker_map(bulk.get("data"))

        all_rows = []
        seen = set()
        for c_name, syms in MARKET_CATALOG.items():
            for s in syms:
                if s in seen:
                    continue
                seen.add(s)
                t_info = t_map.get(s)
                if not t_info or t_info["price"] <= 0:
                    single = bingx_req("GET", "/openApi/swap/v2/quote/ticker", {"symbol": s})
                    s_data = single.get("data") if isinstance(single.get("data"), dict) else {}
                    lp = to_float(s_data.get("lastPrice"), 0.0)
                    cp = to_float(s_data.get("priceChangePercent"), 0.0)
                    t_info = {
                        "price": format_price_smart(lp, lp) if lp > 0 else 0.0,
                        "change_pct": round(cp, 2),
                    }
                chg = t_info["change_pct"]
                sig = "BUY" if chg >= 0.6 else ("SELL" if chg <= -0.6 else "NEUTRAL")
                all_rows.append({
                    "symbol": s,
                    "category": c_name,
                    "price": t_info["price"],
                    "change_pct": chg,
                    "signal": sig,
                })
        with STATE_LOCK:
            MARKET_SCAN_CACHE["rows"] = list(all_rows)
            MARKET_SCAN_CACHE["category"] = "Tutti"
            MARKET_SCAN_CACHE["updated_at"] = time.time()

    effective_cat = search_cat if search_cat else cat
    filtered = []
    for r in all_rows:
        if effective_cat != "Tutti" and r["category"] != effective_cat:
            continue
        if q and not search_cat and (q not in r["symbol"] and q_alt not in r["symbol"]) and q not in r["category"].upper():
            continue
        row_copy = dict(r)
        row_copy["locked_side"] = locked_map.get(r["symbol"])
        filtered.append(row_copy)

    return {
        "category": effective_cat,
        "query": q,
        "total_catalog": len(all_rows),
        "rows": filtered,
    }


def sanitize_tp_sl_for_side(side: str, ref_price: float, tp_val, sl_val):
    p = to_float(ref_price, 0.0)
    if p <= 0:
        return None, None
    min_buf = max(p * 0.0015, format_price_smart(p * 0.001, p))
    if min_buf <= 0:
        min_buf = p * 0.0015

    tp_f = to_float(tp_val, 0.0)
    sl_f = to_float(sl_val, 0.0)

    if side == "BUY":
        if tp_f <= 0 or tp_f <= p + min_buf:
            tp_f = p + max(min_buf * 1.4, p * 0.004)
        clean_tp = format_price_smart(tp_f, p)

        if sl_f <= 0 or sl_f >= p - min_buf:
            sl_f = max(p * 0.0001, p - max(min_buf * 1.4, p * 0.004))
        clean_sl = format_price_smart(sl_f, p)
    else:
        if tp_f <= 0 or tp_f >= p - min_buf:
            tp_f = max(p * 0.0001, p - max(min_buf * 1.4, p * 0.004))
        clean_tp = format_price_smart(tp_f, p)

        if sl_f <= 0 or sl_f <= p + min_buf:
            sl_f = p + max(min_buf * 1.4, p * 0.004)
        clean_sl = format_price_smart(sl_f, p)

    return clean_tp, clean_sl


def _place_tp_sl_orders(symbol: str, side: str, pos_side: str, qty_val: float, clean_tp, clean_sl):
    close_side = "SELL" if side == "BUY" else "BUY"
    attached = {
        "tp_order": {"code": None, "msg": "Non impostato"},
        "sl_order": {"code": None, "msg": "Non impostato"},
    }
    if clean_tp is not None and to_float(clean_tp, 0.0) > 0:
        attached["tp_order"] = bingx_req("POST", "/openApi/swap/v2/trade/order", {
            "symbol": symbol,
            "side": close_side,
            "positionSide": pos_side,
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": fmt_num_str(clean_tp),
            "price": fmt_num_str(clean_tp),
            "quantity": fmt_num_str(qty_val),
            "workingType": "MARK_PRICE",
        }, signed=True)
    if clean_sl is not None and to_float(clean_sl, 0.0) > 0:
        attached["sl_order"] = bingx_req("POST", "/openApi/swap/v2/trade/order", {
            "symbol": symbol,
            "side": close_side,
            "positionSide": pos_side,
            "type": "STOP_MARKET",
            "stopPrice": fmt_num_str(clean_sl),
            "price": fmt_num_str(clean_sl),
            "quantity": fmt_num_str(qty_val),
            "workingType": "MARK_PRICE",
        }, signed=True)
    return attached


def _record_signal_log(symbol: str, tf: str, signal: str, confidence: float, label: str, tp1, tp2, sl, outcome: str, dedup_key: str = None):
    with STATE_LOCK:
        if dedup_key and dedup_key in SEEN_SIGNAL_KEYS:
            return
        if dedup_key:
            SEEN_SIGNAL_KEYS.add(dedup_key)
            if len(SEEN_SIGNAL_KEYS) > 2000:
                SEEN_SIGNAL_KEYS.clear()
                SEEN_SIGNAL_KEYS.add(dedup_key)

        entry = {
            "ora": time.strftime("%H:%M:%S"),
            "coppia": symbol,
            "tf": tf,
            "segnale": signal,
            "confidenza": confidence,
            "label": label,
            "tp1": tp1,
            "tp2": tp2,
            "sl": sl,
            "esito": outcome,
        }
        SIGNAL_HISTORY.insert(0, entry)
        if len(SIGNAL_HISTORY) > 120:
            del SIGNAL_HISTORY[120:]


def fetch_positions_raw():
    res = bingx_req("GET", "/openApi/swap/v2/user/positions", signed=True)
    if res.get("code") != 0 or not isinstance(res.get("data"), list):
        return []
    positions = []
    for p in res["data"]:
        amt = abs(to_float(p.get("positionAmt"), 0.0))
        if amt > 0:
            sym = p.get("symbol", "")
            p_side = p.get("positionSide", "LONG")
            reg = ACTIVE_TPSL_REGISTRY.get(f"{sym}_{p_side}", {})
            positions.append({
                "symbol": sym,
                "positionSide": p_side,
                "positionAmt": amt,
                "entryPrice": to_float(p.get("avgPrice") or p.get("entryPrice"), 0.0),
                "markPrice": to_float(p.get("markPrice"), 0.0),
                "unrealizedProfit": to_float(p.get("unrealizedProfit"), 0.0),
                "leverage": int(to_float(p.get("leverage"), 20)),
                "tp": reg.get("tp"),
                "sl": reg.get("sl"),
            })
    return positions


def check_concurrency_limit(max_concurrent: int, symbol: str = None, pos_side: str = None, current_positions=None):
    limit_val = normalize_max_concurrent_trades(max_concurrent, 2)
    positions = current_positions if current_positions is not None else fetch_positions_raw()
    open_count = len(positions)
    already_same_slot = any(
        (symbol and p.get("symbol") == symbol and (not pos_side or p.get("positionSide") == pos_side))
        for p in positions
    )
    if open_count >= limit_val and not already_same_slot:
        return False, f"Limite operazioni contemporanee raggiunto ({open_count}/{limit_val} aperte)"
    return True, f"Slot disponibile ({open_count}/{limit_val} aperte)"


def _lock_signal_and_advance_monitoring(symbol: str, side: str, tf: str, category: str = "Materie Prime"):
    timing = candle_timing_info(tf)
    lock_key = f"{symbol}_{tf}_{timing['candle_start_ts']}_{side}"
    next_symbol = None

    with STATE_LOCK:
        locked = dict(AUTO_TRADE_STATE.get("locked_signals") or {})
        locked[symbol] = side
        AUTO_TRADE_STATE["locked_signals"] = locked
        AUTO_TRADE_STATE["last_signal_key"] = lock_key
        rotate = bool(AUTO_TRADE_STATE.get("rotate_after_open", True))

    if rotate:
        cat = category if category in MARKET_CATALOG or category == "Tutti" else "Materie Prime"
        pool = []
        if cat == "Tutti":
            for syms in MARKET_CATALOG.values():
                for s in syms:
                    if s not in pool:
                        pool.append(s)
        else:
            pool = list(MARKET_CATALOG.get(cat, []))

        with STATE_LOCK:
            current_locked = dict(AUTO_TRADE_STATE.get("locked_signals") or {})
        candidates = [s for s in pool if s != symbol and current_locked.get(s) != side]
        if not candidates:
            candidates = [s for s in pool if s != symbol]
        if candidates:
            next_symbol = candidates[0]
            with STATE_LOCK:
                AUTO_TRADE_STATE["symbol"] = next_symbol

    return next_symbol


def submit_bingx_order(
    symbol: str,
    side: str,
    order_type: str,
    tf: str,
    qty: float,
    leverage: int,
    price=None,
    tp=None,
    sl=None,
    close_at_candle_end: bool = True,
    max_concurrent_trades: int = 2,
    quant: dict = None,
    category: str = "Materie Prime",
):
    pos_side = "LONG" if side == "BUY" else "SHORT"
    qty_val = to_float(qty, 0.0)
    lev_val = int(to_float(leverage, 20))
    max_conc = normalize_max_concurrent_trades(max_concurrent_trades, 2)

    if qty_val <= 0:
        return {"code": -10, "msg": "Quantita' ordine non valida (deve essere > 0)"}

    can_open, slot_msg = check_concurrency_limit(max_conc, symbol=symbol, pos_side=pos_side)
    if not can_open:
        return {"code": -11, "msg": slot_msg}

    bingx_req("POST", "/openApi/swap/v2/trade/leverage", {
        "symbol": symbol,
        "side": pos_side,
        "leverage": lev_val,
    }, signed=True)

    q_ref = quant if isinstance(quant, dict) and quant.get("price") else compute_quant_brain(symbol, tf, side)
    ticker = bingx_req("GET", "/openApi/swap/v2/quote/ticker", {"symbol": symbol})
    t_data = ticker.get("data") if isinstance(ticker.get("data"), dict) else {}
    mark_ref = to_float(t_data.get("lastPrice", 0), 0.0) or to_float(q_ref.get("price", 0), 0.0)

    eff_tp = tp if tp not in (None, "", 0, 0.0) else (q_ref.get("tp") or q_ref.get("tp2") or q_ref.get("tp1"))
    eff_sl = sl if sl not in (None, "", 0, 0.0) else q_ref.get("sl")
    clean_tp, clean_sl = sanitize_tp_sl_for_side(side, mark_ref, eff_tp, eff_sl)

    order_params = {
        "symbol": symbol,
        "side": side,
        "positionSide": pos_side,
        "type": order_type,
        "quantity": fmt_num_str(qty_val),
    }
    if order_type == "LIMIT" and price not in (None, ""):
        order_params["price"] = fmt_num_str(format_price_smart(to_float(price, mark_ref), mark_ref))

    res = bingx_req("POST", "/openApi/swap/v2/trade/order", order_params, signed=True)
    if res.get("code") == 0:
        tp_sl_status = _place_tp_sl_orders(symbol, side, pos_side, qty_val, clean_tp, clean_sl)
        tp_ok = tp_sl_status["tp_order"].get("code") == 0 if clean_tp else True
        sl_ok = tp_sl_status["sl_order"].get("code") == 0 if clean_sl else True

        tp_info = f"TP {fmt_num_str(clean_tp)} ({'OK' if tp_ok else tp_sl_status['tp_order'].get('msg', 'ERR')})" if clean_tp else "TP -"
        sl_info = f"SL {fmt_num_str(clean_sl)} ({'OK' if sl_ok else tp_sl_status['sl_order'].get('msg', 'ERR')})" if clean_sl else "SL -"

        with STATE_LOCK:
            ACTIVE_TPSL_REGISTRY[f"{symbol}_{pos_side}"] = {
                "symbol": symbol,
                "positionSide": pos_side,
                "tp": clean_tp if tp_ok else None,
                "sl": clean_sl if sl_ok else None,
                "tp_ok": tp_ok,
                "sl_ok": sl_ok,
                "updated_at": int(time.time()),
            }

        next_sym = _lock_signal_and_advance_monitoring(symbol, side, tf, category=category)
        out_str = f"ESITO BINGX: POSIZIONE APERTA SU BINGX ({tp_info} | {sl_info})"
        with STATE_LOCK:
            AUTO_TRADE_STATE["last_outcome"] = out_str

        if close_at_candle_end:
            timing = candle_timing_info(tf)
            watch_key = f"{symbol}_{pos_side}_{timing['candle_end_ts']}"
            with STATE_LOCK:
                AUTO_CLOSE_WATCHLIST[watch_key] = {
                    "symbol": symbol,
                    "side": pos_side,
                    "qty": qty_val,
                    "tf": tf,
                    "close_at": timing["candle_end_ts"],
                }

        _record_signal_log(
            symbol=symbol,
            tf=tf,
            signal=side,
            confidence=to_float(q_ref.get("confidence", 80.0), 80.0),
            label=q_ref.get("label", "ORDINE ESEGUITO"),
            tp1=q_ref.get("tp1", clean_tp),
            tp2=clean_tp,
            sl=clean_sl,
            outcome="APERTO SU BINGX",
        )
        return {
            **res,
            "tp_applied": clean_tp if tp_ok else None,
            "sl_applied": clean_sl if sl_ok else None,
            "tp_sl_orders": tp_sl_status,
            "tpsl_summary": f"{tp_info} | {sl_info}",
            "locked_signal": f"{symbol}_{side}",
            "next_symbol": next_sym,
            "outcome_text": out_str,
        }

    bx_msg = res.get("msg") or res.get("message") or "Ordine rifiutato da BingX"
    out_err = f"ESITO BINGX: POSIZIONE NON APERTA SU BINGX — Motivo: {bx_msg}"
    with STATE_LOCK:
        AUTO_TRADE_STATE["last_outcome"] = out_err
    return {
        **res,
        "code": res.get("code", -1),
        "msg": bx_msg,
        "outcome_text": out_err,
    }


def fetch_balance_summary():
    res = bingx_req("GET", "/openApi/swap/v2/user/balance", signed=True)
    if res.get("code") != 0 or not isinstance(res.get("data"), dict):
        return {"balance": 0.0, "available": 0.0, "unrealized": 0.0, "equity": 0.0, "funding": 0.0048}
    bal = res["data"].get("balance") or {}
    total_bal = to_float(bal.get("balance"), 0.0)
    avail = to_float(bal.get("availableMargin") or bal.get("free"), total_bal)
    unreal = to_float(bal.get("unrealizedProfit"), 0.0)
    return {
        "balance": total_bal,
        "available": avail,
        "unrealized": unreal,
        "equity": total_bal + unreal,
        "funding": 0.0048,
    }


def fetch_income_history(limit: int = 25):
    res = bingx_req("GET", "/openApi/swap/v2/user/income", {"limit": limit}, signed=True)
    if res.get("code") != 0 or not isinstance(res.get("data"), list):
        return []
    return [
        {
            "type": item.get("incomeType", "REALIZED_PNL"),
            "symbol": item.get("symbol", "-"),
            "income": to_float(item.get("income"), 0.0),
            "asset": item.get("asset", "USDT"),
            "time": int(to_float(item.get("time"), 0)),
        }
        for item in res["data"][:limit]
    ]


def process_auto_close_expired():
    now_ts = int(time.time())
    with STATE_LOCK:
        due_keys = [k for k, v in AUTO_CLOSE_WATCHLIST.items() if now_ts >= int(v.get("close_at", 0))]
        due_items = [(k, dict(AUTO_CLOSE_WATCHLIST[k])) for k in due_keys]
        for k in due_keys:
            del AUTO_CLOSE_WATCHLIST[k]

    for _, item in due_items:
        sym = item["symbol"]
        tf = item.get("tf", "1m")
        bingx_req("POST", "/openApi/swap/v2/trade/closeAllPositions", {"symbol": sym}, signed=True)
        with STATE_LOCK:
            locked = dict(AUTO_TRADE_STATE.get("locked_signals") or {})
            locked.pop(sym, None)
            AUTO_TRADE_STATE["locked_signals"] = locked
        _record_signal_log(
            symbol=sym, tf=tf, signal="CLOSE_TF", confidence=100.0,
            label="CHIUSURA AUTOMATICA FINE CANDELA",
            tp1="-", tp2="-", sl="-", outcome="CHIUSO",
        )


def process_auto_trade_tick(symbol: str, tf: str, category: str):
    with STATE_LOCK:
        if not AUTO_TRADE_STATE.get("enabled"):
            return None
        min_score = to_float(AUTO_TRADE_STATE.get("min_score"), 60.0)
        locked_map = dict(AUTO_TRADE_STATE.get("locked_signals") or {})
        qty = to_float(AUTO_TRADE_STATE.get("qty"), 0.0005)
        lev = int(to_float(AUTO_TRADE_STATE.get("leverage"), 20))
        max_conc = normalize_max_concurrent_trades(AUTO_TRADE_STATE.get("max_concurrent_trades"), 2)
        close_at_end = bool(AUTO_TRADE_STATE.get("close_at_candle_end", True))
        AUTO_TRADE_STATE["last_check_ts"] = int(time.time())

    quant = compute_quant_brain(symbol, tf)
    sig = quant.get("signal", "NEUTRAL")
    score_val = abs(to_float(quant.get("score"), 0.0))

    if locked_map.get(symbol) == sig and sig in ("BUY", "SELL"):
        outcome_msg = f"ESITO BINGX: POSIZIONE NON APERTA SU BINGX — Motivo: Segnale {sig} bloccato su {symbol}"
        with STATE_LOCK:
            AUTO_TRADE_STATE["last_outcome"] = outcome_msg
        return None

    if sig not in ("BUY", "SELL") or score_val < min_score or quant.get("risk_blocked"):
        reason_txt = quant.get("risk_reason") if quant.get("risk_blocked") else f"Score {int(score_val)} < +{int(min_score)}"
        outcome_msg = f"ESITO BINGX: POSIZIONE NON APERTA SU BINGX — Motivo: SCAN {category} ({symbol}): {reason_txt}"
        with STATE_LOCK:
            AUTO_TRADE_STATE["last_outcome"] = outcome_msg
        return None

    bal = fetch_balance_summary()
    eff_lev = get_dynamic_leverage(bal.get("available", 0.0), lev)
    return submit_bingx_order(
        symbol=symbol,
        side=sig,
        order_type="MARKET",
        tf=tf,
        qty=qty,
        leverage=eff_lev,
        tp=quant.get("tp"),
        sl=quant.get("sl"),
        close_at_candle_end=close_at_end,
        max_concurrent_trades=max_conc,
        quant=quant,
        category=category,
    )


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/live-ticker")
def api_live_ticker():
    req_symbol = (request.args.get("symbol") or "").strip().upper()
    tf = (request.args.get("tf") or "1m").strip().lower()
    category = (request.args.get("category") or "Materie Prime").strip()
    query = (request.args.get("query") or "").strip()
    scan_now = request.args.get("scan") == "1"

    norm_cat = normalize_category(category)
    with STATE_LOCK:
        if category:
            AUTO_TRADE_STATE["scan_category"] = norm_cat
        if req_symbol:
            AUTO_TRADE_STATE["symbol"] = req_symbol
        else:
            cur_sym = AUTO_TRADE_STATE.get("symbol", "BTC-USDT")
            cat_pool = _pool_for_category(norm_cat)
            if cat_pool and cur_sym not in cat_pool:
                AUTO_TRADE_STATE["symbol"] = cat_pool[0]
        active_symbol = AUTO_TRADE_STATE.get("symbol", "BTC-USDT")
        locked_signals = dict(AUTO_TRADE_STATE.get("locked_signals") or {})

    process_auto_close_expired()
    process_auto_trade_tick(active_symbol, tf, category)

    with STATE_LOCK:
        locked_signals = dict(AUTO_TRADE_STATE.get("locked_signals") or {})
        auto_state = dict(AUTO_TRADE_STATE)

    quant = compute_quant_brain(active_symbol, tf)
    balance = fetch_balance_summary()
    positions = fetch_positions_raw()
    income = fetch_income_history(20)
    timing = candle_timing_info(tf)
    markets = scan_markets(category=category, query=query, force_refresh=scan_now)

    with STATE_LOCK:
        logs = list(SIGNAL_HISTORY[:40])
        auto_state["locked_signals"] = locked_signals
        auto_state["open_positions_count"] = len(positions)

    return jsonify({
        "ok": True,
        "quant": quant,
        "balance": balance,
        "positions": positions,
        "income": income,
        "timing": timing,
        "markets": markets,
        "logs": logs,
        "auto_trade": auto_state,
    })


@app.route("/api/order", methods=["POST"])
def api_order():
    data = request.get_json(silent=True) or {}
    symbol = (data.get("symbol") or "XAUT-USDT").strip().upper()
    side = (data.get("side") or "BUY").strip().upper()
    if side not in ("BUY", "SELL"):
        side = "BUY"
    order_type = (data.get("order_type") or "MARKET").strip().upper()
    tf = (data.get("tf") or "1m").strip().lower()
    qty = to_float(data.get("qty"), 0.0005)
    category = (data.get("category") or AUTO_TRADE_STATE.get("scan_category") or "Materie Prime").strip()

    bal = fetch_balance_summary()
    req_lev = int(to_float(data.get("leverage"), 20))
    lev = get_dynamic_leverage(bal.get("available", 0.0), req_lev)

    with STATE_LOCK:
        default_max_conc = AUTO_TRADE_STATE.get("max_concurrent_trades", 2)
    max_conc = normalize_max_concurrent_trades(data.get("max_concurrent_trades"), default_max_conc)

    result = submit_bingx_order(
        symbol=symbol,
        side=side,
        order_type=order_type,
        tf=tf,
        qty=qty,
        leverage=lev,
        price=data.get("price"),
        tp=data.get("tp"),
        sl=data.get("sl"),
        close_at_candle_end=bool(data.get("close_at_candle_end", True)),
        max_concurrent_trades=max_conc,
        category=category,
    )
    return jsonify({
        "ok": result.get("code") == 0,
        "result": result,
        "positions": fetch_positions_raw(),
        "balance": fetch_balance_summary(),
        "logs": list(SIGNAL_HISTORY[:40]),
        "auto_trade": dict(AUTO_TRADE_STATE),
    })


@app.route("/api/close-position", methods=["POST"])
def api_close_position():
    data = request.get_json(silent=True) or {}
    symbol = (data.get("symbol") or "").strip().upper()
    if symbol:
        res = bingx_req("POST", "/openApi/swap/v2/trade/closeAllPositions", {"symbol": symbol}, signed=True)
        with STATE_LOCK:
            locked = dict(AUTO_TRADE_STATE.get("locked_signals") or {})
            locked.pop(symbol, None)
            AUTO_TRADE_STATE["locked_signals"] = locked
        return jsonify({
            "ok": res.get("code") == 0,
            "result": res,
            "positions": fetch_positions_raw(),
            "balance": fetch_balance_summary(),
        })

    positions = fetch_positions_raw()
    closed_any = []
    for p in positions:
        s = p.get("symbol")
        if s:
            r = bingx_req("POST", "/openApi/swap/v2/trade/closeAllPositions", {"symbol": s}, signed=True)
            closed_any.append({"symbol": s, "res": r})
    with STATE_LOCK:
        AUTO_TRADE_STATE["locked_signals"] = {}
    return jsonify({
        "ok": True,
        "closed": closed_any,
        "positions": fetch_positions_raw(),
        "balance": fetch_balance_summary(),
    })


@app.route("/api/auto-trade", methods=["GET", "POST"])
def api_auto_trade():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        with STATE_LOCK:
            if "enabled" in data:
                AUTO_TRADE_STATE["enabled"] = bool(data["enabled"])
            if "mode" in data and data["mode"]:
                AUTO_TRADE_STATE["mode"] = str(data["mode"]).strip().upper()
            if "symbol" in data and data["symbol"]:
                AUTO_TRADE_STATE["symbol"] = str(data["symbol"]).strip().upper()
            if "tf" in data and data["tf"]:
                AUTO_TRADE_STATE["tf"] = str(data["tf"]).strip().lower()
            if "qty" in data:
                AUTO_TRADE_STATE["qty"] = max(0.0001, to_float(data["qty"], 0.0005))
            if "leverage" in data:
                AUTO_TRADE_STATE["leverage"] = max(1, min(125, int(to_float(data["leverage"], 20))))
            if "min_score" in data:
                AUTO_TRADE_STATE["min_score"] = max(10, min(95, int(to_float(data["min_score"], 60))))
            if "max_concurrent_trades" in data:
                AUTO_TRADE_STATE["max_concurrent_trades"] = normalize_max_concurrent_trades(
                    data["max_concurrent_trades"], AUTO_TRADE_STATE.get("max_concurrent_trades", 2)
                )
            if "close_at_candle_end" in data:
                AUTO_TRADE_STATE["close_at_candle_end"] = bool(data["close_at_candle_end"])
            if "rotate_after_open" in data:
                AUTO_TRADE_STATE["rotate_after_open"] = bool(data["rotate_after_open"])
            if "category" in data and data["category"]:
                AUTO_TRADE_STATE["scan_category"] = str(data["category"]).strip()
            if data.get("reset_locks"):
                AUTO_TRADE_STATE["locked_signals"] = {}
                AUTO_TRADE_STATE["last_signal_key"] = None
                AUTO_TRADE_STATE["last_outcome"] = "Blocco segnali resettato"

    with STATE_LOCK:
        st = dict(AUTO_TRADE_STATE)
    return jsonify({"ok": True, "auto_trade": st})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
