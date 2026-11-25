import hashlib
import hmac
import json
import math
import os
import time
from datetime import datetime, timezone
from urllib.parse import urlencode
from flask import Flask, jsonify, render_template, request
import requests

app = Flask(__name__)

BINGX_BASE_URL = "https://open-api.bingx.com"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config", "runtime_config.json")
LOCKS_PATH = os.path.join(BASE_DIR, "data", "locked_signals.json")
LOGS_PATH = os.path.join(BASE_DIR, "data", "signal_room_log.json")

TIMEFRAME_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}

ASSET_CATALOG = {
    # CRYPTO PIÙ FORTI
    "BTC-USDT": {"label": "BTC-USDT", "category": "Crypto", "price_decimals": 2, "win_rate": 77},
    "ETH-USDT": {"label": "ETH-USDT", "category": "Crypto", "price_decimals": 2, "win_rate": 75},
    "SOL-USDT": {"label": "SOL-USDT", "category": "Crypto", "price_decimals": 2, "win_rate": 74},
    "BNB-USDT": {"label": "BNB-USDT", "category": "Crypto", "price_decimals": 2, "win_rate": 76},
    "XRP-USDT": {"label": "XRP-USDT", "category": "Crypto", "price_decimals": 4, "win_rate": 72},
    "ADA-USDT": {"label": "ADA-USDT", "category": "Crypto", "price_decimals": 4, "win_rate": 71},
    "AVAX-USDT": {"label": "AVAX-USDT", "category": "Crypto", "price_decimals": 2, "win_rate": 72},
    "LINK-USDT": {"label": "LINK-USDT", "category": "Crypto", "price_decimals": 3, "win_rate": 73},
    "SUI-USDT": {"label": "SUI-USDT", "category": "Crypto", "price_decimals": 4, "win_rate": 73},
    # MEME / ALT
    "DOGE-USDT": {"label": "DOGE-USDT", "category": "Meme/Alt", "price_decimals": 5, "win_rate": 70},
    "SFP-USDT": {"label": "SFP-USDT", "category": "Meme/Alt", "price_decimals": 5, "win_rate": 71},
    "PEPE-USDT": {"label": "PEPE-USDT", "category": "Meme/Alt", "price_decimals": 8, "win_rate": 69},
    # FOREX
    "EUR-USDT": {"label": "EURUSDT", "category": "Forex", "price_decimals": 5, "win_rate": 74},
    "GBP-USDT": {"label": "GBPUSDT", "category": "Forex", "price_decimals": 5, "win_rate": 72},
    "USDJPY-USDT": {"label": "USDJPY", "category": "Forex", "price_decimals": 3, "win_rate": 71},
    # MATERIE PRIME
    "XAUT-USDT": {"label": "XAUT-USDT", "category": "Materie Prime", "price_decimals": 2, "win_rate": 75},
    "PAXG-USDT": {"label": "PAXG-USDT", "category": "Materie Prime", "price_decimals": 2, "win_rate": 75},
    "XAU-USDT": {"label": "ORO (XAU)", "category": "Materie Prime", "price_decimals": 2, "win_rate": 75},
    "XAG-USDT": {"label": "ARGENTO", "category": "Materie Prime", "price_decimals": 3, "win_rate": 72},
    "OIL-USDT": {"label": "PETROLIO", "category": "Materie Prime", "price_decimals": 3, "win_rate": 71},
    # AZIONI
    "TSLA-USDT": {"label": "TSLA", "category": "Azioni", "price_decimals": 2, "win_rate": 72},
    "NVDA-USDT": {"label": "NVDA", "category": "Azioni", "price_decimals": 2, "win_rate": 76},
    "AAPL-USDT": {"label": "AAPL", "category": "Azioni", "price_decimals": 2, "win_rate": 73},
    "MSFT-USDT": {"label": "MSFT", "category": "Azioni", "price_decimals": 2, "win_rate": 74},
    "AMZN-USDT": {"label": "AMZN", "category": "Azioni", "price_decimals": 2, "win_rate": 72},
    "META-USDT": {"label": "META", "category": "Azioni", "price_decimals": 2, "win_rate": 73},
}

DEFAULT_CONFIG = {
    "api_key": os.getenv("BINGX_API_KEY", ""),
    "secret_key": os.getenv("BINGX_SECRET_KEY", ""),
    "mode": "AUTOMATICO",
    "auto_trade": True,
    "scan_mode": "Auto Scan + Blocco Certo",
    "min_signal_score": 60.0,
    "certain_min_pct": 70.0,
    "order_type": "MARKET",
    "default_qty": 0.0005,
    "default_leverage": 20,
    "rsi_period": 14,
    "rsi_oversold": 30.0,
    "rsi_overbought": 70.0,
    "macd_fast": 12,
    "macd_slow": 26,
    "macd_signal": 9,
    "ema_fast": 9,
    "ema_mid": 21,
    "ema_trend": 50,
    "ema_slow": 200,
    "atr_period": 14,
    "w_rsi": 0.30,
    "w_macd": 0.40,
    "w_ema": 0.30,
    "close_at_candle_end": False,
    "anti_fake_enabled": True,
    "anti_breakout_enabled": True,
    "allow_manual_under_threshold": False,
    "sound_enabled": True,
    "voice_it_enabled": True,
    "fakeout_lookback": 20,
    "atr_spike_mult": 2.5,
    "wick_ratio_limit": 0.55,
    "min_breakout_vol_mult": 0.70,
}


def load_json(path: str, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(default, dict) and isinstance(data, dict):
                    merged = dict(default)
                    merged.update(data)
                    return merged
                if isinstance(default, list) and isinstance(data, list):
                    return data
        except Exception:
            pass
    return dict(default) if isinstance(default, dict) else list(default)


def save_json(path: str, payload) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


RUNTIME_CONFIG = load_json(CONFIG_PATH, DEFAULT_CONFIG)
LOCKED_SIGNALS = load_json(LOCKS_PATH, [])
ROOM_LOGS = load_json(LOGS_PATH, [])


def append_log(entry: dict) -> None:
    global ROOM_LOGS
    ROOM_LOGS.insert(0, entry)
    ROOM_LOGS = ROOM_LOGS[:60]
    save_json(LOGS_PATH, ROOM_LOGS)


def mask_key(k: str) -> str:
    if not k or len(k) < 8:
        return "NON IMPOSTATA"
    return f"{k[:4]}...{k[-4:]}"


def normalize_symbol(raw_symbol: str) -> str:
    cleaned = (raw_symbol or "BTC-USDT").strip().upper().replace("/", "-")
    if cleaned == "EURUSDT":
        return "EUR-USDT"
    if "-" not in cleaned and cleaned.endswith("USDT"):
        return f"{cleaned[:-4]}-USDT"
    return cleaned


def candle_close_info(interval: str, now_ts: float | None = None) -> dict:
    tf = interval if interval in TIMEFRAME_SECONDS else "15m"
    step = TIMEFRAME_SECONDS[tf]
    now = now_ts if now_ts is not None else time.time()
    current_open_ts = math.floor(now / step) * step
    next_close_ts = current_open_ts + step
    remaining_sec = max(0, int(next_close_ts - now))
    pct_elapsed = min(100, max(0, int(((step - remaining_sec) / step) * 100)))
    pct_remaining = 100 - pct_elapsed

    hrs = remaining_sec // 3600
    mins = (remaining_sec % 3600) // 60
    secs = remaining_sec % 60
    countdown = f"{hrs:02d}:{mins:02d}:{secs:02d}" if hrs > 0 else f"{mins:02d}:{secs:02d}"

    close_utc = datetime.fromtimestamp(next_close_ts, tz=timezone.utc).strftime("%H:%M:%S UTC")
    close_local = datetime.fromtimestamp(next_close_ts).astimezone().strftime("%H:%M:%S")

    return {
        "timeframe": tf,
        "intervalSeconds": step,
        "candleOpenTs": int(current_open_ts * 1000),
        "candleCloseTs": int(next_close_ts * 1000),
        "remainingSeconds": remaining_sec,
        "remainingText": countdown,
        "validityPct": pct_remaining,
        "elapsedPct": pct_elapsed,
        "closeTimeUtc": close_utc,
        "closeTimeLocal": close_local,
    }


def ema_series(values: list[float], period: int) -> list[float]:
    if not values or period <= 1:
        return list(values)
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1.0 - k))
    return out


def compute_rsi(closes: list[float], period: int = 14) -> float:
    p = max(2, int(period))
    if len(closes) <= p:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(0.0, diff))
        losses.append(max(0.0, -diff))

    avg_gain = sum(gains[:p]) / p
    avg_loss = sum(losses[:p]) / p
    for i in range(p, len(gains)):
        avg_gain = (avg_gain * (p - 1) + gains[i]) / p
        avg_loss = (avg_loss * (p - 1) + losses[i]) / p

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def compute_macd(closes: list[float], fast: int, slow: int, signal: int) -> dict:
    if len(closes) < slow + signal:
        return {"macd": 0.0, "signal": 0.0, "hist": 0.0, "status": "NEUTRAL"}
    ema_f = ema_series(closes, int(fast))
    ema_s = ema_series(closes, int(slow))
    macd_line = [f - s for f, s in zip(ema_f, ema_s)]
    signal_line = ema_series(macd_line, int(signal))
    hist = macd_line[-1] - signal_line[-1]

    if hist > 0 and macd_line[-1] > signal_line[-1]:
        status = "BULLISH"
    elif hist < 0 and macd_line[-1] < signal_line[-1]:
        status = "BEARISH"
    else:
        status = "NEUTRAL"

    return {
        "macd": round(macd_line[-1], 5),
        "signal": round(signal_line[-1], 5),
        "hist": round(hist, 5),
        "status": status,
    }


def compute_atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    p = max(2, int(period))
    if len(closes) < 2 or not (len(highs) == len(lows) == len(closes)):
        return 0.0
    trs = [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(1, len(closes))
    ]
    window = trs[-p:] if len(trs) >= p else trs
    return round(sum(window) / len(window), 5) if window else 0.0


def compute_adx(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    p = max(2, int(period))
    if len(closes) < p + 2:
        return 0.0
    plus_dm, minus_dm, tr_list = [], [], []
    for i in range(1, len(closes)):
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        plus_dm.append(up if (up > dn and up > 0) else 0.0)
        minus_dm.append(dn if (dn > up and dn > 0) else 0.0)
        tr_list.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    atr_v = sum(tr_list[-p:]) / p or 1e-9
    p_di = 100.0 * (sum(plus_dm[-p:]) / p) / atr_v
    m_di = 100.0 * (sum(minus_dm[-p:]) / p) / atr_v
    dx = 100.0 * abs(p_di - m_di) / max(p_di + m_di, 1e-9)
    return round(dx, 2)


def compute_zscore(closes: list[float], period: int = 20) -> float:
    p = max(5, int(period))
    if len(closes) < p:
        return 0.0
    w = closes[-p:]
    mean = sum(w) / p
    std = math.sqrt(sum((x - mean) ** 2 for x in w) / p)
    return round((closes[-1] - mean) / std, 2) if std > 0 else 0.0


def detect_anti_fake_filters(opens, highs, lows, closes, volumes, atr, cfg, spread_ratio=0.0) -> dict:
    if not cfg.get("anti_fake_enabled", True) and not cfg.get("anti_breakout_enabled", True):
        return {"enabled": False, "fakeBreakout": "NONE", "isFalseSignal": False, "penaltyScore": 0.0, "reasons": []}

    lookback = max(5, int(cfg.get("fakeout_lookback", 20)))
    atr_mult = float(cfg.get("atr_spike_mult", 2.5))
    wick_limit = float(cfg.get("wick_ratio_limit", 0.55))
    vol_mult = float(cfg.get("min_breakout_vol_mult", 0.70))

    n = len(closes)
    if n < lookback + 2 or not (len(opens) == len(highs) == len(lows) == n):
        return {"enabled": True, "fakeBreakout": "NONE", "isFalseSignal": False, "penaltyScore": 0.0, "reasons": []}

    reasons = []
    penalty = 0.0
    fake_breakout = "NONE"

    prev_high = max(highs[-(lookback + 1):-1])
    prev_low = min(lows[-(lookback + 1):-1])
    o_last, h_last, l_last, c_last = opens[-1], highs[-1], lows[-1], closes[-1]
    candle_range = max(0.0, h_last - l_last)
    upper_wick = max(0.0, h_last - max(o_last, c_last))
    lower_wick = max(0.0, min(o_last, c_last) - l_last)

    if cfg.get("anti_fake_enabled", True):
        if h_last > prev_high and c_last < prev_high:
            fake_breakout = "BULL_TRAP"
            penalty += 35.0
            reasons.append(f"Falsa rottura rialzista (max {prev_high}, chiusura {c_last})")
        elif l_last < prev_low and c_last > prev_low:
            fake_breakout = "BEAR_TRAP"
            penalty += 35.0
            reasons.append(f"Falsa rottura ribassista (min {prev_low}, chiusura {c_last})")

        if candle_range > 0:
            if upper_wick / candle_range > wick_limit and c_last < o_last:
                penalty += 20.0
                reasons.append("Shadow superiore dominante (possibile fakeout/rifiuto)")
            if lower_wick / candle_range > wick_limit and c_last > o_last:
                penalty += 20.0
                reasons.append("Shadow inferiore dominante (possibile sweep)")

    if cfg.get("anti_breakout_enabled", True):
        if atr > 0 and candle_range > atr * atr_mult:
            penalty += 25.0
            reasons.append(f"Spike/range anomalo ({round(candle_range, 5)} > {atr_mult}x ATR)")
        if spread_ratio > 0.0015:
            penalty += 15.0
            reasons.append("Spread elevato / tenuta debole")
        if len(volumes) >= lookback + 1:
            avg_vol = sum(volumes[-(lookback + 1):-1]) / lookback
            if avg_vol > 0 and (h_last > prev_high or l_last < prev_low) and volumes[-1] < avg_vol * vol_mult:
                penalty += 15.0
                reasons.append(f"Rottura con volume debole (< {vol_mult}x media)")

    is_false_signal = fake_breakout != "NONE" or penalty >= 35.0
    return {
        "enabled": True,
        "fakeBreakout": fake_breakout,
        "isFalseSignal": is_false_signal,
        "penaltyScore": round(min(100.0, penalty), 1),
        "reasons": reasons,
    }


def fetch_bingx_klines(symbol: str, interval: str, limit: int = 210):
    resp = requests.get(
        f"{BINGX_BASE_URL}/openApi/swap/v2/quote/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=8,
    )
    resp.raise_for_status()
    rows = resp.json().get("data", [])
    opens, highs, lows, closes, volumes = [], [], [], [], []
    for r in rows:
        if isinstance(r, dict):
            opens.append(float(r.get("open", 0.0)))
            highs.append(float(r.get("high", 0.0)))
            lows.append(float(r.get("low", 0.0)))
            closes.append(float(r.get("close", 0.0)))
            volumes.append(float(r.get("volume", 0.0)))
        elif isinstance(r, (list, tuple)) and len(r) >= 6:
            opens.append(float(r[1]))
            highs.append(float(r[2]))
            lows.append(float(r[3]))
            closes.append(float(r[4]))
            volumes.append(float(r[5]))
    last_price = closes[-1] if closes else 0.0
    return last_price, opens, highs, lows, closes, volumes


def fetch_bingx_ticker(symbol: str) -> dict:
    resp = requests.get(
        f"{BINGX_BASE_URL}/openApi/swap/v2/quote/ticker",
        params={"symbol": symbol},
        timeout=6,
    )
    resp.raise_for_status()
    return resp.json().get("data") or {}


def fetch_bingx_depth(symbol: str, limit: int = 10) -> dict:
    resp = requests.get(
        f"{BINGX_BASE_URL}/openApi/swap/v2/quote/depth",
        params={"symbol": symbol, "limit": limit},
        timeout=6,
    )
    resp.raise_for_status()
    return resp.json().get("data") or {}


def sign_query(params: dict, secret: str) -> str:
    ordered = sorted((k, v) for k, v in params.items() if v is not None)
    qs = urlencode(ordered)
    sig = hmac.new(secret.encode("utf-8"), qs.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{qs}&signature={sig}"


def bingx_signed_request(method: str, path: str, params: dict | None = None) -> dict:
    api_k = (RUNTIME_CONFIG.get("api_key") or "").strip()
    sec_k = (RUNTIME_CONFIG.get("secret_key") or "").strip()
    if not api_k or not sec_k:
        raise ValueError("API Key o Secret Key BingX non configurate.")
    payload = dict(params or {})
    payload["timestamp"] = int(time.time() * 1000)
    payload["recvWindow"] = 5000
    signed_qs = sign_query(payload, sec_k)
    headers = {"X-BX-APIKEY": api_k}
    url = f"{BINGX_BASE_URL}{path}?{signed_qs}"
    r = requests.get(url, headers=headers, timeout=10) if method.upper() == "GET" else requests.post(url, headers=headers, timeout=10)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and data.get("code") not in (0, None):
        raise RuntimeError(data.get("msg") or f"BingX code {data.get('code')}")
    return data


def dynamic_leverage(equity_usdt: float, category: str) -> int:
    if equity_usdt <= 2.00:
        return 20 if category in ("Crypto", "Meme/Alt") else 10
    if category == "Forex":
        return 10
    if category == "Materie Prime":
        return 8
    if category == "Azioni":
        return 5
    return 10 if equity_usdt <= 25.00 else 5


def analyze_symbol_real(symbol: str, timeframe: str) -> dict:
    sym = normalize_symbol(symbol)
    meta = ASSET_CATALOG.get(sym, {"label": sym, "category": "Crypto", "price_decimals": 2, "win_rate": 72})
    dec = meta["price_decimals"]
    close_meta = candle_close_info(timeframe)

    last_price, opens, highs, lows, closes, volumes = 0.0, [], [], [], [], []
    change_24h_pct = 0.0
    funding_rate = "--"
    ticker_err = None

    try:
        last_price, opens, highs, lows, closes, volumes = fetch_bingx_klines(sym, timeframe, limit=210)
        tdata = fetch_bingx_ticker(sym)
        if tdata.get("lastPrice"):
            last_price = float(tdata["lastPrice"])
        raw_chg = str(tdata.get("priceChangePercent", "0")).replace("%", "").strip()
        change_24h_pct = float(raw_chg or 0.0)
        if tdata.get("lastFundingRate") is not None:
            funding_rate = f"{float(tdata['lastFundingRate']) * 100:.4f}%"
    except Exception as exc:
        ticker_err = str(exc)

    rsi_val = compute_rsi(closes, RUNTIME_CONFIG["rsi_period"])
    macd_info = compute_macd(closes, RUNTIME_CONFIG["macd_fast"], RUNTIME_CONFIG["macd_slow"], RUNTIME_CONFIG["macd_signal"])
    atr_val = compute_atr(highs, lows, closes, RUNTIME_CONFIG["atr_period"])
    adx_val = compute_adx(highs, lows, closes, RUNTIME_CONFIG["atr_period"])
    z_val = compute_zscore(closes, 20)

    ema_f_val = ema_series(closes, RUNTIME_CONFIG["ema_fast"])[-1] if closes else 0.0
    ema_m_val = ema_series(closes, RUNTIME_CONFIG["ema_mid"])[-1] if closes else 0.0
    ema_t_val = ema_series(closes, RUNTIME_CONFIG["ema_trend"])[-1] if closes else 0.0
    ema_s_val = ema_series(closes, RUNTIME_CONFIG["ema_slow"])[-1] if closes else 0.0

    anti_fake = detect_anti_fake_filters(opens, highs, lows, closes, volumes, atr_val, RUNTIME_CONFIG)

    if rsi_val <= RUNTIME_CONFIG["rsi_oversold"]:
        rsi_score = 75.0
    elif rsi_val >= RUNTIME_CONFIG["rsi_overbought"]:
        rsi_score = -75.0
    else:
        rsi_score = (rsi_val - 50.0) * 1.5

    macd_score = 70.0 if macd_info["status"] == "BULLISH" else (-70.0 if macd_info["status"] == "BEARISH" else 0.0)
    ema_score = 65.0 if ema_f_val > ema_m_val else (-65.0 if ema_f_val < ema_m_val else 0.0)

    raw_signed = (
        rsi_score * RUNTIME_CONFIG["w_rsi"]
        + macd_score * RUNTIME_CONFIG["w_macd"]
        + ema_score * RUNTIME_CONFIG["w_ema"]
    )
    raw_abs = min(100.0, abs(raw_signed))
    final_score = round(max(0.0, raw_abs - anti_fake["penaltyScore"]), 1)
    min_score = float(RUNTIME_CONFIG["min_signal_score"])
    certain_min = float(RUNTIME_CONFIG["certain_min_pct"])

    if final_score < min_score or anti_fake["isFalseSignal"]:
        signal_side = "NEUTRAL"
    else:
        signal_side = "BUY" if raw_signed > 0 else "SELL"

    reliability_pct = int(max(35, min(98, round(45 + final_score * 0.5))))
    is_certain = (signal_side in ("BUY", "SELL")) and (not anti_fake["isFalseSignal"]) and (reliability_pct >= certain_min) and (last_price > 0)

    if ticker_err and last_price <= 0:
        entry_decision = "NON ENTRARE"
        entry_reason = f"Errore dati BingX: {ticker_err}"
    elif anti_fake["isFalseSignal"]:
        entry_decision = "NON ENTRARE"
        entry_reason = " / ".join(anti_fake["reasons"]) or "Filtro anti-falsi segnali / falsa rottura attivo"
    elif signal_side == "NEUTRAL":
        entry_decision = "NON ENTRARE"
        entry_reason = f"Score {final_score} sotto soglia {min_score} o mercato neutrale"
    elif reliability_pct < certain_min:
        entry_decision = "NON ENTRARE"
        entry_reason = f"Affidabilità {reliability_pct}% sotto soglia segnale certo ({certain_min}%)"
    else:
        entry_decision = "ENTRA"
        entry_reason = f"Confluenza {signal_side} reale validata (Score {final_score}, Affidabilità {reliability_pct}%, ADX {adx_val})"

    sl_dist = atr_val * 1.5 if atr_val > 0 else last_price * 0.005
    tp_pct = round((sl_dist * 1.8 / max(last_price, 1e-9)) * 100.0, 2)
    sl_pct = round((sl_dist / max(last_price, 1e-9)) * 100.0, 2)

    if signal_side == "BUY" and last_price > 0:
        sl_p = round(max(0.0, last_price - sl_dist), dec)
        tp1 = round(last_price + sl_dist * 1.0, dec)
        tp2 = round(last_price + sl_dist * 1.8, dec)
        tp3 = round(last_price + sl_dist * 2.5, dec)
        tp4 = round(last_price + sl_dist * 3.5, dec)
    elif signal_side == "SELL" and last_price > 0:
        sl_p = round(last_price + sl_dist, dec)
        tp1 = round(max(0.0, last_price - sl_dist * 1.0), dec)
        tp2 = round(max(0.0, last_price - sl_dist * 1.8), dec)
        tp3 = round(max(0.0, last_price - sl_dist * 2.5), dec)
        tp4 = round(max(0.0, last_price - sl_dist * 3.5), dec)
    else:
        sl_p = tp1 = tp2 = tp3 = tp4 = None

    vol_last = volumes[-1] if volumes else 0.0
    vol_avg = (sum(volumes[-20:]) / len(volumes[-20:])) if volumes else 1.0
    vol_ratio = round(vol_last / max(vol_avg, 1e-9), 2)

    whale_pct = int(max(10, min(90, round(50 + raw_signed * 0.4))))
    bear_pct = 100 - whale_pct
    trend_health = int(max(10, min(100, round(50 + abs(ema_f_val - ema_m_val) / max(last_price, 1e-9) * 10000))))

    signed_display = round(final_score if signal_side == "BUY" else (-final_score if signal_side == "SELL" else raw_signed), 1)

    return {
        "symbol": sym,
        "label": meta["label"],
        "category": meta["category"],
        "priceDecimals": dec,
        "winRate": meta["win_rate"],
        "lastPrice": round(last_price, dec),
        "change24hPct": round(change_24h_pct, 2),
        "fundingRate": funding_rate,
        "signalSide": signal_side,
        "signedScore": signed_display,
        "signalScore": final_score,
        "minSignalScore": min_score,
        "reliabilityPct": reliability_pct,
        "certainMinPct": certain_min,
        "isCertainSignal": is_certain,
        "entryDecision": entry_decision,
        "entryReason": entry_reason,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "tp4": tp4,
        "suggestedSL": sl_p,
        "suggestedTP": tp2,
        "tpPct": tp_pct,
        "slPct": sl_pct,
        "trendHealth": trend_health,
        "whalePct": whale_pct,
        "bearPct": bear_pct,
        "indicators": {
            "rsi": rsi_val,
            "rsiPeriod": RUNTIME_CONFIG["rsi_period"],
            "macd": macd_info["macd"],
            "macdSignal": macd_info["signal"],
            "macdHist": macd_info["hist"],
            "macdStatus": macd_info["status"],
            "emaFast": round(ema_f_val, dec),
            "emaMid": round(ema_m_val, dec),
            "emaTrend": round(ema_t_val, dec),
            "emaSlow": round(ema_s_val, dec),
            "emaFastP": RUNTIME_CONFIG["ema_fast"],
            "emaMidP": RUNTIME_CONFIG["ema_mid"],
            "emaTrendP": RUNTIME_CONFIG["ema_trend"],
            "emaSlowP": RUNTIME_CONFIG["ema_slow"],
            "atr": atr_val,
            "adx": adx_val,
            "zScore": z_val,
            "volRatio": vol_ratio,
        },
        "antiFake": anti_fake,
        "highRiskWarning": entry_decision != "ENTRA",
        "riskReason": entry_reason,
        **close_meta,
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/desk-state")
def desk_state():
    sym = normalize_symbol(request.args.get("symbol", "BTC-USDT"))
    tf = request.args.get("timeframe", "15m")
    cat = request.args.get("category", "Materie Prime")

    market = analyze_symbol_real(sym, tf)

    cat_syms = [s for s, m in ASSET_CATALOG.items() if cat == "Tutti" or m["category"].lower() == cat.lower()]
    scanner = []
    for s in cat_syms[:10]:
        a = analyze_symbol_real(s, tf)
        scanner.append({
            "symbol": a["symbol"],
            "label": a["label"],
            "category": a["category"],
            "lastPrice": a["lastPrice"],
            "priceDecimals": a["priceDecimals"],
            "change24hPct": a["change24hPct"],
            "signalSide": a["signalSide"],
            "signalScore": a["signalScore"],
            "isCertainSignal": a["isCertainSignal"],
        })

    bids, asks, mid_p = [], [], market["lastPrice"]
    try:
        ob = fetch_bingx_depth(sym, limit=10)
        bids = [[float(x[0]), float(x[1])] for x in (ob.get("bids") or [])[:8]]
        asks = [[float(x[0]), float(x[1])] for x in (ob.get("asks") or [])[:8]]
        if bids and asks:
            mid_p = round((bids[0][0] + asks[0][0]) / 2.0, market["priceDecimals"])
    except Exception:
        pass

    equity_real = None
    positions = []
    income_rows = []
    api_connected = bool(RUNTIME_CONFIG.get("api_key") and RUNTIME_CONFIG.get("secret_key"))
    account_err = None

    if api_connected:
        try:
            bal = bingx_signed_request("GET", "/openApi/swap/v2/user/balance")
            bdata = bal.get("data", {})
            if isinstance(bdata, dict) and bdata.get("balance", {}).get("equity") is not None:
                equity_real = round(float(bdata["balance"]["equity"]), 4)
        except Exception as exc:
            account_err = str(exc)

        try:
            pr = bingx_signed_request("GET", "/openApi/swap/v2/user/positions", {"symbol": sym})
            for p in (pr.get("data") or []):
                amt = float(p.get("positionAmt", 0.0))
                if abs(amt) > 0:
                    entry = float(p.get("avgPrice", 0.0))
                    upnl = float(p.get("unrealizedProfit", 0.0))
                    pnl_pct = ((market["lastPrice"] - entry) / entry * 100.0) if entry > 0 else 0.0
                    if amt < 0:
                        pnl_pct = -pnl_pct
                    positions.append({
                        "symbol": p.get("symbol", sym),
                        "side": "LONG" if amt > 0 else "SHORT",
                        "size": abs(amt),
                        "entry": round(entry, market["priceDecimals"]),
                        "pnl": round(upnl, 4),
                        "pnlPct": round(pnl_pct, 2),
                    })
        except Exception:
            pass

        try:
            ir = bingx_signed_request("GET", "/openApi/swap/v2/user/income", {"symbol": sym, "limit": 20})
            for item in (ir.get("data") or [])[:12]:
                income_rows.append({
                    "type": item.get("incomeType", "REALIZED_PNL"),
                    "symbol": item.get("symbol", sym),
                    "income": float(item.get("income", 0.0)),
                })
        except Exception:
            pass

    lev_real = dynamic_leverage(equity_real if equity_real is not None else 2.0, market["category"])
    active_lock = LOCKED_SIGNALS[0] if LOCKED_SIGNALS else None

    return jsonify({
        "apiConnected": api_connected,
        "apiMasked": mask_key(RUNTIME_CONFIG.get("api_key", "")),
        "accountError": account_err,
        "equityReal": equity_real,
        "leverageReal": lev_real,
        "lockStatus": "BLOCCATO" if active_lock else "LIBERO",
        "activeLock": active_lock,
        "lockedSignals": LOCKED_SIGNALS,
        "logs": ROOM_LOGS,
        "income": income_rows,
        "positions": positions,
        "orderBook": {"bids": bids, "asks": asks, "midPrice": mid_p},
        "scanner": scanner,
        "market": market,
        "config": {k: v for k, v in RUNTIME_CONFIG.items() if k not in ("api_key", "secret_key")},
    })


@app.route("/api/config", methods=["GET", "POST"])
def manage_config():
    if request.method == "POST":
        data = request.get_json(force=True) or {}
        if "api_key" in data and str(data["api_key"]).strip():
            RUNTIME_CONFIG["api_key"] = str(data["api_key"]).strip()
        if "secret_key" in data and str(data["secret_key"]).strip():
            RUNTIME_CONFIG["secret_key"] = str(data["secret_key"]).strip()

        int_keys = ("rsi_period", "macd_fast", "macd_slow", "macd_signal", "ema_fast", "ema_mid", "ema_trend", "ema_slow", "atr_period", "fakeout_lookback", "default_leverage")
        float_keys = ("min_signal_score", "certain_min_pct", "default_qty", "rsi_oversold", "rsi_overbought", "atr_spike_mult", "wick_ratio_limit", "min_breakout_vol_mult")
        bool_keys = ("auto_trade", "close_at_candle_end", "anti_fake_enabled", "anti_breakout_enabled", "allow_manual_under_threshold", "sound_enabled", "voice_it_enabled")
        str_keys = ("mode", "scan_mode", "order_type")

        for k in int_keys:
            if k in data:
                RUNTIME_CONFIG[k] = int(data[k])
        for k in float_keys:
            if k in data:
                RUNTIME_CONFIG[k] = float(data[k])
        for k in bool_keys:
            if k in data:
                RUNTIME_CONFIG[k] = bool(data[k])
        for k in str_keys:
            if k in data:
                RUNTIME_CONFIG[k] = str(data[k])

        RUNTIME_CONFIG["min_signal_score"] = max(0.0, min(100.0, RUNTIME_CONFIG["min_signal_score"]))
        save_json(CONFIG_PATH, RUNTIME_CONFIG)

        api_valid = False
        api_msg = "Parametri salvati."
        if data.get("test_api") and RUNTIME_CONFIG.get("api_key") and RUNTIME_CONFIG.get("secret_key"):
            try:
                bingx_signed_request("GET", "/openApi/swap/v2/user/balance")
                api_valid = True
                api_msg = "API attive e valide su BingX"
            except Exception as exc:
                api_msg = f"Errore API BingX: {exc}"

        return jsonify({
            "status": "ok",
            "apiValid": api_valid,
            "apiMessage": api_msg,
            "apiMasked": mask_key(RUNTIME_CONFIG.get("api_key", "")),
        })

    return jsonify({k: v for k, v in RUNTIME_CONFIG.items() if k not in ("api_key", "secret_key")})


@app.route("/api/lock-signal", methods=["POST"])
def lock_signal():
    global LOCKED_SIGNALS
    body = request.get_json(force=True) or {}
    action = body.get("action", "lock")
    sym = normalize_symbol(body.get("symbol", "BTC-USDT"))
    tf = body.get("timeframe", "15m")

    if action == "unlock":
        LOCKED_SIGNALS = []
        save_json(LOCKS_PATH, LOCKED_SIGNALS)
        append_log({
            "time": datetime.now().astimezone().strftime("%H:%M:%S"),
            "pair": sym,
            "tf": tf,
            "tag": "SBLOCCATO",
            "tpSl": "-/-/-",
            "outcome": "LIBERO",
        })
        return jsonify({"status": "unlocked", "locks": LOCKED_SIGNALS})

    a = analyze_symbol_real(sym, tf)
    if not a["isCertainSignal"] and not bool(body.get("force", False)):
        return jsonify({"status": "rejected", "reason": a["entryReason"]}), 200

    side = a["signalSide"] if a["signalSide"] in ("BUY", "SELL") else "BUY"
    rec = {
        "id": f"{sym}_{tf}_{int(time.time())}",
        "symbol": sym,
        "timeframe": tf,
        "side": side,
        "score": a["signalScore"],
        "reliabilityPct": a["reliabilityPct"],
        "entry": a["lastPrice"],
        "tp1": a["tp1"],
        "tp2": a["tp2"],
        "tp3": a["tp3"],
        "sl": a["suggestedSL"],
        "lockedAt": datetime.now().astimezone().strftime("%H:%M:%S"),
    }
    LOCKED_SIGNALS.insert(0, rec)
    LOCKED_SIGNALS = LOCKED_SIGNALS[:25]
    save_json(LOCKS_PATH, LOCKED_SIGNALS)

    append_log({
        "time": rec["lockedAt"],
        "pair": sym,
        "tf": tf,
        "tag": f"{side} · BLOCCATO · {a['reliabilityPct']}%",
        "tpSl": f"{a['tp1']} / {a['tp2']} / {a['suggestedSL']}",
        "outcome": "SEGNALE BLOCCATO",
    })
    return jsonify({"status": "locked", "signal": rec, "locks": LOCKED_SIGNALS})


@app.route("/api/execute-signal", methods=["POST"])
def execute_signal():
    body = request.get_json(force=True) or {}
    action = body.get("action", "buy")
    sym = normalize_symbol(body.get("symbol", "BTC-USDT"))
    tf = body.get("timeframe", "15m")
    a = analyze_symbol_real(sym, tf)

    if action == "close_all":
        res = bingx_signed_request("POST", "/openApi/swap/v2/trade/closeAllPositions")
        append_log({
            "time": datetime.now().astimezone().strftime("%H:%M:%S"),
            "pair": sym,
            "tf": tf,
            "tag": "CLOSE_ALL",
            "tpSl": "-/-/-",
            "outcome": "CHIUSO SU BINGX",
        })
        return jsonify({"status": "closed_all", "bingx": res})

    side = "BUY" if action == "buy" else "SELL"
    allow_under = bool(RUNTIME_CONFIG.get("allow_manual_under_threshold", False))
    if a["entryDecision"] != "ENTRA" and not allow_under:
        return jsonify({"status": "blocked", "reason": a["entryReason"]}), 200

    qty = float(body.get("qty", RUNTIME_CONFIG.get("default_qty", 0.0005)))
    lev = int(body.get("leverage", RUNTIME_CONFIG.get("default_leverage", 20)))
    tp_val = body.get("tp", a["suggestedTP"])
    sl_val = body.get("sl", a["suggestedSL"])

    bingx_signed_request("POST", "/openApi/swap/v2/trade/leverage", {
        "symbol": sym,
        "side": "LONG" if side == "BUY" else "SHORT",
        "leverage": lev,
    })
    order_res = bingx_signed_request("POST", "/openApi/swap/v2/trade/order", {
        "symbol": sym,
        "side": side,
        "positionSide": "LONG" if side == "BUY" else "SHORT",
        "type": body.get("orderType", RUNTIME_CONFIG.get("order_type", "MARKET")),
        "quantity": qty,
    })

    append_log({
        "time": datetime.now().astimezone().strftime("%H:%M:%S"),
        "pair": sym,
        "tf": tf,
        "tag": f"{side} · {a['reliabilityPct']}%",
        "tpSl": f"{tp_val} / {sl_val}",
        "outcome": "APERTO SU BINGX",
    })
    return jsonify({"status": "executed", "bingx": order_res, "side": side, "tp": tp_val, "sl": sl_val})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
