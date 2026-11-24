import os
import time
import math
import hmac
import hashlib
import json
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlencode
import requests
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

if ENV_PATH.exists():
    raw_env = ENV_PATH.read_text(encoding="utf-8", errors="ignore").replace("\r\n", "\n").replace("\r", "")
    ENV_PATH.write_text(raw_env, encoding="utf-8")
    load_dotenv(dotenv_path=ENV_PATH, override=True)
else:
    load_dotenv(override=True)

app = Flask(__name__)


def to_float(val, default=0.0):
    if val is None or val == "":
        return float(default)
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace(",", ".")
    try:
        return float(s)
    except Exception:
        return float(default)


def clean_secret(val: str) -> str:
    if not val:
        return ""
    s = str(val).strip()
    if len(s) >= 2 and ((s[0] == '"' and s[-1] == '"') or (s[0] == "'" and s[-1] == "'")):
        s = s[1:-1].strip()
    return "".join(ch for ch in s if ch.isprintable() and not ch.isspace())


API_KEY = clean_secret(os.getenv("BINGX_API_KEY", ""))
API_SECRET = clean_secret(os.getenv("BINGX_SECRET_KEY", ""))
BASE_URL = "https://open-api.bingx.com"


def apply_api_credentials(api_key: str, api_secret: str, persist_env: bool = True):
    global API_KEY, API_SECRET
    k = clean_secret(api_key)
    s = clean_secret(api_secret)
    API_KEY = k
    API_SECRET = s
    os.environ["BINGX_API_KEY"] = k
    os.environ["BINGX_SECRET_KEY"] = s

    if persist_env:
        existing_lines = []
        if ENV_PATH.exists():
            raw = ENV_PATH.read_text(encoding="utf-8", errors="ignore").replace("\r\n", "\n").replace("\r", "")
            for line in raw.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    if stripped:
                        existing_lines.append(line)
                    continue
                if stripped.startswith("BINGX_API_KEY=") or stripped.startswith("BINGX_SECRET_KEY="):
                    continue
                existing_lines.append(line)
        existing_lines.append(f"BINGX_API_KEY={k}")
        existing_lines.append(f"BINGX_SECRET_KEY={s}")
        ENV_PATH.write_text("\n".join(existing_lines) + "\n", encoding="utf-8")


TF_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
}

AUTO_CLOSE_WATCHLIST = {}
SIGNAL_LOG = []
LAST_AUTO_ORDER_TS = {}
MARKET_CACHE = {"ts": 0.0, "markets": []}
SCAN_CURSOR = {}
SIGNAL_LOCK = {
    "locked": False,
    "symbol": None,
    "tf": None,
    "side": None,
    "certainty_pct": 0,
    "certainty_label": "",
    "locked_at": 0.0,
    "expires_at": 0.0,
    "quant": None
}


def sign_params(params: dict) -> str:
    clean_params = {k: v for k, v in params.items() if v is not None and v != ""}
    query = urlencode(sorted(clean_params.items()))
    return hmac.new(
        API_SECRET.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()


def bingx_req(method: str, path: str, params: dict = None, signed: bool = False):
    clean_params = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
    headers = {"X-BX-APIKEY": API_KEY}

    if signed:
        if not API_KEY or not API_SECRET:
            return {"code": -2, "msg": "Chiavi API BingX non impostate"}
        clean_params["timestamp"] = int(time.time() * 1000)
        sig = sign_params(clean_params)
        query = urlencode(sorted(clean_params.items()))
        url = f"{BASE_URL}{path}?{query}&signature={sig}"
    else:
        query = urlencode(sorted(clean_params.items()))
        url = f"{BASE_URL}{path}" + (f"?{query}" if query else "")

    try:
        r = requests.request(method, url, headers=headers, timeout=10)
        return r.json()
    except Exception as e:
        return {"code": -1, "msg": str(e)}


def record_signal(entry: dict):
    entry = dict(entry)
    now_ts = time.time()
    entry["ts"] = time.strftime("%H:%M:%S")
    entry["_ts"] = now_ts

    sym_e = str(entry.get("symbol", "")).upper()
    side_e = str(entry.get("side", "")).upper()
    tf_e = str(entry.get("tf", "")).lower()
    out_e = str(entry.get("outcome", "")).upper()
    cert_lbl = str(entry.get("certainty_label", ""))

    is_execution_event = (
        "APERTO SU BINGX" in out_e
        or "CHIUSO" in out_e
        or side_e == "CLOSE_TF"
        or (out_e not in ("SEGNALE BLOCCATO", "CALCOLATO", ""))
    )

    if not is_execution_event and SIGNAL_LOG:
        for prev in SIGNAL_LOG[:8]:
            if (
                str(prev.get("symbol", "")).upper() == sym_e
                and str(prev.get("side", "")).upper() == side_e
                and str(prev.get("tf", "")).lower() == tf_e
                and str(prev.get("outcome", "")).upper() == out_e
                and (now_ts - to_float(prev.get("_ts", 0.0), 0.0)) < 120
            ):
                prev["ts"] = entry["ts"]
                prev["_ts"] = now_ts
                prev["certainty_label"] = cert_lbl
                prev["tp1"] = entry.get("tp1", prev.get("tp1"))
                prev["tp2"] = entry.get("tp2", prev.get("tp2"))
                prev["sl"] = entry.get("sl", prev.get("sl"))
                return

    SIGNAL_LOG.insert(0, entry)
    del SIGNAL_LOG[40:]


def is_exact_certain_signal(quant: dict, min_certainty: int = 70) -> bool:
    if not isinstance(quant, dict):
        return False
    sig = str(quant.get("signal", "NEUTRAL")).upper()
    cert = int(quant.get("certainty_pct", 0) or 0)
    label = str(quant.get("certainty_label", "")).upper()
    profit_ready = bool(quant.get("profit_ready", False))
    exp_r = to_float(quant.get("expectancy_r", 0.0), 0.0)
    return (
        sig in ("BUY", "SELL")
        and cert >= max(70, int(min_certainty or 70))
        and "SEGNALE CERTO" in label
        and profit_ready
        and exp_r > 0
    )


def get_active_signal_lock():
    now = time.time()
    if SIGNAL_LOCK.get("locked") and now >= to_float(SIGNAL_LOCK.get("expires_at", 0.0), 0.0):
        SIGNAL_LOCK["locked"] = False
    return {
        **SIGNAL_LOCK,
        "remaining_sec": max(0, int(to_float(SIGNAL_LOCK.get("expires_at", 0.0), 0.0) - now)) if SIGNAL_LOCK.get("locked") else 0
    }


def engage_signal_lock(symbol: str, tf: str, quant: dict, ttl_sec: int = None):
    if not is_exact_certain_signal(quant):
        return get_active_signal_lock()
    now = time.time()
    duration = int(ttl_sec or TF_SECONDS.get(tf, 300))
    same_setup = (
        SIGNAL_LOCK.get("locked")
        and SIGNAL_LOCK.get("symbol") == symbol
        and SIGNAL_LOCK.get("tf") == tf
        and SIGNAL_LOCK.get("side") == quant.get("signal")
        and now < to_float(SIGNAL_LOCK.get("expires_at", 0.0), 0.0)
    )
    if not same_setup:
        SIGNAL_LOCK.update({
            "locked": True,
            "symbol": symbol,
            "tf": tf,
            "side": quant.get("signal"),
            "certainty_pct": int(quant.get("certainty_pct", 0) or 0),
            "certainty_label": quant.get("certainty_label", ""),
            "locked_at": now,
            "expires_at": now + duration,
            "quant": quant
        })
        record_signal({
            "symbol": symbol,
            "side": quant.get("signal"),
            "tf": tf,
            "certainty_label": f"BLOCCATO • {quant.get('certainty_label')}",
            "tp1": quant.get("tp1"),
            "tp2": quant.get("tp2"),
            "tp3": quant.get("tp3"),
            "sl": quant.get("sl"),
            "outcome": "SEGNALE BLOCCATO"
        })
    else:
        SIGNAL_LOCK["quant"] = quant
        SIGNAL_LOCK["certainty_pct"] = int(quant.get("certainty_pct", 0) or 0)
        SIGNAL_LOCK["certainty_label"] = quant.get("certainty_label", "")
    return get_active_signal_lock()


def release_signal_lock():
    SIGNAL_LOCK.update({
        "locked": False,
        "symbol": None,
        "tf": None,
        "side": None,
        "certainty_pct": 0,
        "certainty_label": "",
        "locked_at": 0.0,
        "expires_at": 0.0,
        "quant": None
    })
    return get_active_signal_lock()


def calc_ema(values: list, period: int):
    if not values or len(values) < period:
        return None
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = (v * k) + (ema * (1 - k))
    return ema


def calc_ema_series(values: list, period: int):
    if not values or len(values) < period:
        return []
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    series = [ema]
    for v in values[period:]:
        ema = (v * k) + (ema * (1 - k))
        series.append(ema)
    return series


def calc_rsi(closes: list, period: int = 14):
    if len(closes) <= period:
        return 50.0
    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def calc_macd(closes: list):
    if len(closes) < 26:
        return 0.0, 0.0, 0.0
    ema12_s = calc_ema_series(closes, 12)
    ema26_s = calc_ema_series(closes, 26)
    min_len = min(len(ema12_s), len(ema26_s))
    macd_line = [ema12_s[-min_len + i] - ema26_s[-min_len + i] for i in range(min_len)]
    signal_s = calc_ema_series(macd_line, 9)
    macd_val = macd_line[-1] if macd_line else 0.0
    signal_val = signal_s[-1] if signal_s else 0.0
    hist_val = macd_val - signal_val
    return macd_val, signal_val, hist_val


def calc_atr(candles: list, period: int = 14):
    if len(candles) < 2:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h = to_float(candles[i].get("high", 0), 0.0)
        l = to_float(candles[i].get("low", 0), 0.0)
        prev_c = to_float(candles[i - 1].get("close", h), h)
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
    use_trs = trs[-period:] if len(trs) >= period else trs
    return sum(use_trs) / len(use_trs) if use_trs else 0.0


def calc_adx_and_zscore(candles: list, closes: list, period: int = 14):
    z_score = 0.0
    if len(closes) >= 20:
        win = closes[-20:]
        mean_c = sum(win) / len(win)
        var_c = sum((x - mean_c) ** 2 for x in win) / len(win)
        std_c = math.sqrt(var_c)
        if std_c > 1e-12:
            z_score = (closes[-1] - mean_c) / std_c

    if len(candles) < period + 2:
        return 20.0, round(z_score, 2)

    plus_dm_list = []
    minus_dm_list = []
    tr_list = []

    for i in range(1, len(candles)):
        h = to_float(candles[i].get("high", 0), 0.0)
        l = to_float(candles[i].get("low", 0), 0.0)
        prev_h = to_float(candles[i - 1].get("high", h), h)
        prev_l = to_float(candles[i - 1].get("low", l), l)
        prev_c = to_float(candles[i - 1].get("close", h), h)

        up_move = h - prev_h
        down_move = prev_l - l
        plus_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm = down_move if (down_move > up_move and down_move > 0) else 0.0
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))

        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)
        tr_list.append(tr)

    dx_list = []
    for idx in range(period - 1, len(tr_list)):
        tr_sum = sum(tr_list[idx - period + 1:idx + 1])
        if tr_sum <= 1e-12:
            continue
        p_di = 100.0 * (sum(plus_dm_list[idx - period + 1:idx + 1]) / tr_sum)
        m_di = 100.0 * (sum(minus_dm_list[idx - period + 1:idx + 1]) / tr_sum)
        di_sum = p_di + m_di
        if di_sum > 1e-12:
            dx_list.append(100.0 * abs(p_di - m_di) / di_sum)

    adx_val = sum(dx_list[-period:]) / min(len(dx_list), period) if dx_list else 20.0
    return round(adx_val, 2), round(z_score, 2)


def format_price_smart(val, ref_price=None):
    if val is None:
        return None
    v = float(val)
    ref = abs(float(ref_price)) if ref_price not in (None, 0) else abs(v)
    if ref >= 1000:
        return round(v, 2)
    if ref >= 10:
        return round(v, 3)
    if ref >= 0.1:
        return round(v, 5)
    if ref >= 0.001:
        return round(v, 6)
    return round(v, 8)


def compute_quant_brain(symbol: str, tf: str, side: str = None):
    klines = bingx_req("GET", "/openApi/swap/v2/quote/klines", {
        "symbol": symbol,
        "interval": tf,
        "limit": 220
    })
    raw = klines.get("data") if klines.get("code") == 0 and isinstance(klines.get("data"), list) else []

    closes = []
    volumes = []
    for k in raw:
        try:
            c = to_float(k.get("close", 0), 0.0)
            v = to_float(k.get("volume", 0), 0.0)
            if c > 0:
                closes.append(c)
                volumes.append(v)
        except Exception:
            continue

    if not closes:
        return {
            "score": 0,
            "signal": "NEUTRAL",
            "certainty_pct": 0,
            "certainty_label": "ATTESA / NESSUN SEGNALE CERTO",
            "setup_status": "IN ATTESA DATI",
            "win_prob": 0.50,
            "rr_ratio": 2.0,
            "expectancy_r": 0.0,
            "profit_ready": False,
            "price": 0.0,
            "entry_low": 0.0,
            "entry_high": 0.0,
            "tp": 0.0,
            "tp1": 0.0,
            "tp2": 0.0,
            "tp3": 0.0,
            "sl": 0.0,
            "tp_pct": 0.0,
            "sl_pct": 0.0,
            "eta_tp_sec": 0,
            "avg_range": 0.0,
            "adx": 0.0,
            "z_score": 0.0,
            "ema9": None,
            "ema21": None,
            "ema50": None,
            "ema200": None,
            "rsi14": 50.0,
            "macd_hist": 0.0,
            "atr": 0.0,
            "vol_ratio": 1.0
        }

    price = closes[-1]
    ema9 = calc_ema(closes, 9)
    ema21 = calc_ema(closes, 21)
    ema50 = calc_ema(closes, 50)
    ema200 = calc_ema(closes, 200)
    rsi14 = calc_rsi(closes, 14)
    macd_val, sig_val, hist_val = calc_macd(closes)
    atr_val = calc_atr(raw, 14)
    adx_val, z_score = calc_adx_and_zscore(raw, closes, 14)

    ranges = [
        abs(to_float(k.get("high", price), price) - to_float(k.get("low", price), price))
        for k in raw[-21:-1]
    ]
    avg_range = sum(ranges) / len(ranges) if ranges else (atr_val or price * 0.004)
    avg_vol = (sum(volumes[-21:-1]) / max(len(volumes[-21:-1]), 1)) if len(volumes) > 2 else (volumes[-1] or 1.0)
    vol_ratio = round(volumes[-1] / avg_vol, 2) if avg_vol > 0 else 1.0

    ema_pts = 0.0
    if ema9 is not None and ema21 is not None:
        ema_pts += 35.0 if ema9 > ema21 else -35.0
        ema_pts += 20.0 if price > ema9 else -20.0
    if ema50 is not None:
        ema_pts += 25.0 if price > ema50 else -25.0
    if ema200 is not None:
        ema_pts += 20.0 if price > ema200 else -20.0
    ema_pts = max(-100.0, min(100.0, ema_pts))

    macd_pts = 0.0
    if macd_val > sig_val:
        macd_pts += 55.0
    elif macd_val < sig_val:
        macd_pts -= 55.0
    if hist_val > 0:
        macd_pts += 45.0
    elif hist_val < 0:
        macd_pts -= 45.0
    macd_pts = max(-100.0, min(100.0, macd_pts))

    if rsi14 >= 65:
        rsi_pts = 65.0 if rsi14 < 75 else 25.0
    elif rsi14 <= 35:
        rsi_pts = -65.0 if rsi14 > 25 else -25.0
    else:
        rsi_pts = (rsi14 - 50.0) * 2.0

    adx_mult = min(1.25, max(0.65, adx_val / 25.0))
    z_penalty = -20.0 if abs(z_score) > 2.2 else 0.0

    raw_score = ((ema_pts * 0.45) + (macd_pts * 0.30) + (rsi_pts * 0.25)) * adx_mult + z_penalty
    score = int(round(max(-100, min(100, raw_score))))

    if score >= 40 and adx_val >= 18:
        signal = "BUY"
    elif score <= -40 and adx_val >= 18:
        signal = "SELL"
    else:
        signal = "NEUTRAL"

    strength_component = min(45.0, (abs(score) / 100.0) * 45.0)
    adx_component = min(20.0, max(0.0, (adx_val / 40.0) * 20.0))
    vol_component = min(15.0, max(0.0, (vol_ratio / 2.0) * 15.0))
    z_bonus = 10.0 if abs(z_score) <= 1.8 else (-10.0 if abs(z_score) > 2.2 else 0.0)
    base_certainty = 15.0 + strength_component + adx_component + vol_component + z_bonus
    certainty_pct = int(round(max(5, min(95, base_certainty))))

    if signal == "BUY" and certainty_pct >= 70:
        certainty_label = f"{certainty_pct}% SEGNALE CERTO LONG"
        setup_status = "ZONA INGRESSO VALIDA • LONG"
    elif signal == "SELL" and certainty_pct >= 70:
        certainty_label = f"{certainty_pct}% SEGNALE CERTO SHORT"
        setup_status = "ZONA INGRESSO VALIDA • SHORT"
    elif signal in ("BUY", "SELL"):
        dir_txt = "LONG" if signal == "BUY" else "SHORT"
        certainty_label = f"{certainty_pct}% SEGNALE {dir_txt} MODERATO"
        setup_status = f"CONFERMA PARZIALE • {dir_txt}"
    else:
        certainty_label = f"{certainty_pct}% ATTESA • NO TRADE CERTO"
        setup_status = "MERCATO LATERALE / FILTRI ATTIVI"

    if side and str(side).upper() in ("BUY", "SELL"):
        target_side = str(side).upper()
    elif signal in ("BUY", "SELL"):
        target_side = signal
    else:
        target_side = "SELL" if score < 0 else "BUY"

    aligned = (target_side == "BUY" and score > 0) or (target_side == "SELL" and score < 0)
    win_prob = round(min(0.76, max(0.38, (0.48 + 0.24 * (abs(score) / 100.0)) if aligned else 0.42)), 2)

    unit_vol = atr_val if atr_val > 0 else max(avg_range, price * 0.0025)
    sl_dist = max(unit_vol * 1.15, price * 0.0015)
    rr_ratio = 2.20 if abs(score) >= 55 else 1.85
    tp2_dist = sl_dist * rr_ratio
    tp1_dist = tp2_dist * 0.55
    tp3_dist = tp2_dist * 1.45

    zone_half = max(unit_vol * 0.18, price * 0.0005)
    entry_low = price - zone_half
    entry_high = price + zone_half

    if target_side == "SELL":
        sl_raw = price + sl_dist
        tp1_raw = max(price * 0.000001, price - tp1_dist)
        tp2_raw = max(price * 0.000001, price - tp2_dist)
        tp3_raw = max(price * 0.000001, price - tp3_dist)
    else:
        sl_raw = max(price * 0.000001, price - sl_dist)
        tp1_raw = price + tp1_dist
        tp2_raw = price + tp2_dist
        tp3_raw = price + tp3_dist

    tp2_clean = format_price_smart(tp2_raw, price)
    sl_clean = format_price_smart(sl_raw, price)

    tp_pct = round(abs(tp2_clean - price) / price * 100, 2) if price > 0 else 0.0
    sl_pct = round(abs(sl_clean - price) / price * 100, 2) if price > 0 else 0.0

    expectancy_r = round((win_prob * rr_ratio) - ((1.0 - win_prob) * 1.0), 2)
    profit_ready = (expectancy_r > 0.15) and (adx_val >= 18) and (abs(z_score) <= 2.3)

    tf_sec = TF_SECONDS.get(tf, 300)
    speed_per_sec = max(avg_range / tf_sec, 1e-9)
    eta_tp_sec = int(min(tp2_dist / speed_per_sec, tf_sec * 5))

    return {
        "score": score,
        "signal": signal,
        "target_side": target_side,
        "certainty_pct": certainty_pct,
        "certainty_label": certainty_label,
        "setup_status": setup_status,
        "win_prob": win_prob,
        "rr_ratio": rr_ratio,
        "expectancy_r": expectancy_r,
        "profit_ready": profit_ready,
        "price": format_price_smart(price, price),
        "entry_low": format_price_smart(entry_low, price),
        "entry_high": format_price_smart(entry_high, price),
        "tp": tp2_clean,
        "tp1": format_price_smart(tp1_raw, price),
        "tp2": tp2_clean,
        "tp3": format_price_smart(tp3_raw, price),
        "sl": sl_clean,
        "tp_pct": tp_pct,
        "sl_pct": sl_pct,
        "eta_tp_sec": eta_tp_sec,
        "avg_range": format_price_smart(avg_range, price),
        "adx": adx_val,
        "z_score": z_score,
        "ema9": format_price_smart(ema9, price),
        "ema21": format_price_smart(ema21, price),
        "ema50": format_price_smart(ema50, price),
        "ema200": format_price_smart(ema200, price),
        "rsi14": rsi14,
        "macd_hist": format_price_smart(hist_val, price),
        "atr": format_price_smart(atr_val, price),
        "vol_ratio": vol_ratio
    }


def sanitize_tp_sl_for_side(side: str, mark_price: float, tp_val, sl_val):
    if mark_price <= 0:
        return None, None
    clean_tp = None
    clean_sl = None
    min_gap = max(mark_price * 0.002, 1e-8)

    if tp_val not in (None, ""):
        t = to_float(tp_val, 0.0)
        if t > 0:
            if side == "BUY":
                clean_tp = format_price_smart(max(t, mark_price + min_gap), mark_price)
            else:
                clean_tp = format_price_smart(max(1e-8, min(t, mark_price - min_gap)), mark_price)

    if sl_val not in (None, ""):
        s = to_float(sl_val, 0.0)
        if s > 0:
            if side == "BUY":
                clean_sl = format_price_smart(max(1e-8, min(s, mark_price - min_gap)), mark_price)
            else:
                clean_sl = format_price_smart(max(s, mark_price + min_gap), mark_price)

    return clean_tp, clean_sl


def categorize_symbol(symbol: str) -> str:
    s = symbol.upper()
    commodities = (
        "XAUT-USDT", "PAXG-USDT", "XAG-USDT", "WTI-USDT", "BRENT-USDT", "XAU-USDT"
    )
    stocks = (
        "NCSK", "AAPL", "TSLA", "NVDA", "MSFT", "AMZN", "META",
        "GOOGL", "GOOG", "COIN", "MSTR"
    )
    majors = (
        "BTC-USDT", "ETH-USDT", "SOL-USDT", "BNB-USDT", "XRP-USDT",
        "ADA-USDT", "AVAX-USDT", "LINK-USDT", "DOT-USDT", "LTC-USDT",
        "BCH-USDT", "TRX-USDT", "TON-USDT", "SUI-USDT", "NEAR-USDT"
    )

    if s in commodities or any(s.startswith(p) for p in ("XAUT", "PAXG", "XAG", "XAU")):
        return "Materie Prime"
    if any(k in s for k in stocks):
        return "Azioni & Indici"
    if s in majors:
        return "Crypto Major"
    return "Meme & Altcoin"


def get_cached_markets(force_refresh: bool = False):
    now = time.time()
    if not force_refresh and MARKET_CACHE["markets"] and (now - MARKET_CACHE["ts"] < 15.0):
        return MARKET_CACHE["markets"]

    contracts = bingx_req("GET", "/openApi/swap/v2/quote/contracts")
    tickers = bingx_req("GET", "/openApi/swap/v2/quote/ticker")

    t_map = {}
    if tickers.get("code") == 0:
        for item in tickers.get("data", []):
            t_map[item.get("symbol")] = item

    out = []
    if contracts.get("code") == 0:
        for c in contracts.get("data", []):
            sym = c.get("symbol")
            if not sym:
                continue
            tk = t_map.get(sym, {})
            out.append({
                "symbol": sym,
                "category": categorize_symbol(sym),
                "maxLeverage": c.get("maxLeverage", 20),
                "lastPrice": to_float(tk.get("lastPrice", 0), 0.0),
                "priceChangePercent": to_float(tk.get("priceChangePercent", 0), 0.0),
                "volume": to_float(tk.get("volume", 0), 0.0),
            })

    out.sort(key=lambda x: x["volume"], reverse=True)
    if out:
        MARKET_CACHE["ts"] = now
        MARKET_CACHE["markets"] = out
    return out


def select_category_candidates(category: str = "ALL", query: str = "", current_symbol: str = "BTC-USDT", max_items: int = 8):
    all_mkts = get_cached_markets(force_refresh=False)
    q = (query or "").strip().upper()
    cat = (category or "ALL").strip()

    def matches_category(m_cat: str) -> bool:
        c_up = cat.upper()
        m_up = (m_cat or "Crypto Major").upper()
        if c_up == "ALL":
            return True
        if m_up == c_up:
            return True
        if c_up in ("CRYPTO", "CRYPTO MAJOR") and m_up == "CRYPTO MAJOR":
            return True
        if c_up in ("MEME", "MEME/ALT", "MEME & ALTCOIN") and m_up == "MEME & ALTCOIN":
            return True
        if c_up in ("AZIONI", "AZIONI & INDICI") and m_up == "AZIONI & INDICI":
            return True
        if c_up in ("MATERIE PRIME", "COMMODITIES") and m_up == "MATERIE PRIME":
            return True
        return False

    cat_filtered = [m for m in all_mkts if matches_category(m.get("category", "Crypto Major"))]
    if not cat_filtered:
        cat_filtered = all_mkts

    if q:
        q_filtered = [m for m in cat_filtered if q in m.get("symbol", "").upper() or q in m.get("category", "").upper()]
        filtered = q_filtered if q_filtered else cat_filtered
    else:
        filtered = cat_filtered

    if not filtered:
        return []

    cursor_key = f"{cat.upper()}|{q}"
    start_idx = SCAN_CURSOR.get(cursor_key, 0) % len(filtered)
    rotated = filtered[start_idx:] + filtered[:start_idx]
    step = min(len(filtered), max_items)
    SCAN_CURSOR[cursor_key] = (start_idx + step) % len(filtered)

    return rotated[:step]


def scan_category_markets(category: str = "ALL", query: str = "", tf: str = "5m", current_symbol: str = "BTC-USDT", max_items: int = 8):
    lock_info = get_active_signal_lock()
    if lock_info.get("locked") and lock_info.get("symbol"):
        l_sym = lock_info["symbol"]
        l_cat = categorize_symbol(l_sym)
        candidates_check = select_category_candidates(category, query, current_symbol, max_items=200)
        allowed_syms = {c.get("symbol") for c in candidates_check}

        if category.upper() != "ALL" and l_sym not in allowed_syms and l_cat.upper() != category.upper():
            release_signal_lock()
            lock_info = get_active_signal_lock()
        else:
            l_tf = lock_info.get("tf") or tf
            l_side = lock_info.get("side") or "BUY"
            q_locked = compute_quant_brain(l_sym, l_tf, l_side)
            if is_exact_certain_signal(q_locked):
                engage_signal_lock(l_sym, l_tf, q_locked)
                best_locked = {
                    "symbol": l_sym,
                    "category": l_cat,
                    "score": int(q_locked.get("score", 0) or 0),
                    "abs_score": abs(int(q_locked.get("score", 0) or 0)),
                    "signal": q_locked.get("signal", l_side),
                    "certainty_pct": q_locked.get("certainty_pct", 0),
                    "certainty_label": q_locked.get("certainty_label", ""),
                    "expectancy_r": to_float(q_locked.get("expectancy_r", 0.0), 0.0),
                    "profit_ready": q_locked.get("profit_ready", False),
                    "tp": q_locked.get("tp"),
                    "tp1": q_locked.get("tp1"),
                    "tp2": q_locked.get("tp2"),
                    "tp3": q_locked.get("tp3"),
                    "sl": q_locked.get("sl"),
                    "quant": q_locked
                }
                return {
                    "category": category,
                    "query": query,
                    "scanned_count": 1,
                    "locked_found": True,
                    "best": best_locked,
                    "results": [best_locked],
                    "signal_lock": get_active_signal_lock()
                }
            else:
                release_signal_lock()
                lock_info = get_active_signal_lock()

    candidates = select_category_candidates(category, query, current_symbol, max_items)
    results = []
    best = None
    best_abs_score = -1
    locked_found = False

    for m in candidates:
        s = m["symbol"]
        q_data = compute_quant_brain(s, tf)
        sc = int(q_data.get("score", 0) or 0)
        exp_r = to_float(q_data.get("expectancy_r", 0.0), 0.0)
        item = {
            "symbol": s,
            "category": m.get("category", "Crypto Major"),
            "score": sc,
            "abs_score": abs(sc),
            "signal": q_data.get("signal", "NEUTRAL"),
            "certainty_pct": q_data.get("certainty_pct", 0),
            "certainty_label": q_data.get("certainty_label", ""),
            "expectancy_r": exp_r,
            "profit_ready": q_data.get("profit_ready", False),
            "tp": q_data.get("tp"),
            "tp1": q_data.get("tp1"),
            "tp2": q_data.get("tp2"),
            "tp3": q_data.get("tp3"),
            "sl": q_data.get("sl"),
            "quant": q_data
        }
        results.append(item)

        if is_exact_certain_signal(q_data):
            engage_signal_lock(s, tf, q_data)
            best = item
            locked_found = True
            break

        rank_val = abs(sc) + (15 if exp_r > 0 else -20)
        if rank_val > best_abs_score:
            best_abs_score = rank_val
            best = item

    results.sort(key=lambda x: (x["expectancy_r"] > 0, x["abs_score"], x["certainty_pct"]), reverse=True)
    return {
        "category": category,
        "query": query,
        "scanned_count": len(results),
        "locked_found": locked_found,
        "best": best,
        "results": results,
        "signal_lock": get_active_signal_lock()
    }


def candle_timing_info(tf: str) -> dict:
    sec = TF_SECONDS.get(tf, 300)
    now = time.time()
    elapsed = now % sec
    remaining = max(0, int(sec - elapsed))
    return {
        "timeframe": tf,
        "duration_sec": sec,
        "elapsed_sec": int(elapsed),
        "remaining_sec": remaining,
        "candle_end_ts": int(now + remaining)
    }


def check_anti_fakeout_and_breakout(symbol: str, tf: str, side: str, anti_fakeout: bool, anti_breakout: bool, quant: dict = None, min_certainty: int = 70, allow_override: bool = False):
    if allow_override:
        return True, "OVERRIDE ATTIVO"

    if quant:
        cert = int(quant.get("certainty_pct", 0) or 0)
        if cert < min_certainty:
            return False, f"BLOCCATO: {quant.get('certainty_label')} sotto soglia minima ({min_certainty}%)"
        if not quant.get("profit_ready", True):
            return False, f"CERVELLO QUANT: aspettativa non ottimale (Exp {quant.get('expectancy_r')}R, ADX {quant.get('adx')})"

    if not anti_fakeout and not anti_breakout:
        return True, "Filtri disattivati"

    klines = bingx_req("GET", "/openApi/swap/v2/quote/klines", {
        "symbol": symbol,
        "interval": tf,
        "limit": 5
    })
    depth = bingx_req("GET", "/openApi/swap/v2/quote/depth", {
        "symbol": symbol,
        "limit": 5
    })

    if klines.get("code") != 0 or not klines.get("data"):
        return False, "BLOCCATO: dati candela non disponibili per verifica anti-falso segnale"

    candles = klines["data"]
    last_closed = candles[-2] if len(candles) >= 2 else candles[-1]
    o = to_float(last_closed.get("open", 0), 0.0)
    h = to_float(last_closed.get("high", 0), 0.0)
    l = to_float(last_closed.get("low", 0), 0.0)
    c = to_float(last_closed.get("close", 0), 0.0)
    total_range = max(h - l, 1e-9)
    body = abs(c - o)

    if anti_fakeout:
        body_ratio = body / total_range
        if body_ratio < 0.35:
            return False, f"BLOCCATO FALSO SEGNALE: corpo candela debole ({body_ratio*100:.0f}% del range)"
        if side == "BUY" and c < o:
            return False, "BLOCCATO FALSO SEGNALE: ultima candela chiusa ribassista per ingresso BUY"
        if side == "SELL" and c > o:
            return False, "BLOCCATO FALSO SEGNALE: ultima candela chiusa rialzista per ingresso SELL"

    if anti_breakout and depth.get("code") == 0:
        d = depth.get("data") or {}
        asks = d.get("asks") or []
        bids = d.get("bids") or []
        if asks and bids:
            best_ask = to_float(asks[0][0], 0.0)
            best_bid = to_float(bids[0][0], 0.0)
            mid = (best_ask + best_bid) / 2 if (best_ask + best_bid) > 0 else 1
            spread_pct = ((best_ask - best_bid) / mid) * 100
            if spread_pct > 0.15:
                return False, f"ANTI-ROTTURA ATTIVO: spread elevato ({spread_pct:.3f}%), rischio falsa rottura"

    return True, "VALIDATO"


def process_auto_close_expired():
    now = time.time()
    expired_keys = [k for k, v in AUTO_CLOSE_WATCHLIST.items() if now >= v["close_at"]]
    closed_log = []
    for k in expired_keys:
        item = AUTO_CLOSE_WATCHLIST.pop(k)
        res = bingx_req("POST", "/openApi/swap/v2/trade/closeAllPositions", {
            "symbol": item["symbol"]
        }, signed=True)
        closed_log.append({"symbol": item["symbol"], "result": res})
        record_signal({
            "symbol": item["symbol"],
            "side": "CLOSE_TF",
            "tf": item.get("tf", "5m"),
            "certainty_label": "CHIUSURA AUTOMATICA FINE CANDELA",
            "tp1": "-",
            "tp2": "-",
            "sl": "-",
            "outcome": "CHIUSO" if res.get("code") == 0 else "ERRORE"
        })
    return closed_log


def calc_dynamic_leverage(equity: float) -> int:
    if equity <= 2.00:
        return 20
    if equity <= 50.00:
        return 15
    if equity <= 500.00:
        return 10
    return 5


def fetch_income_window(start_ms: int, end_ms: int = None, max_windows: int = 8) -> list:
    all_rows = []
    seen_ids = set()
    cursor_end = int(end_ms or (time.time() * 1000))
    window_ms = 7 * 24 * 60 * 60 * 1000

    for _ in range(max_windows):
        if cursor_end < start_ms:
            break
        cursor_start = max(int(start_ms), cursor_end - window_ms + 1)

        resp = bingx_req("GET", "/openApi/swap/v2/user/income", {
            "startTime": cursor_start,
            "endTime": cursor_end,
            "limit": 1000
        }, signed=True)

        if resp.get("code") == 0 and isinstance(resp.get("data"), list):
            for item in resp.get("data") or []:
                ts_raw = item.get("time") or item.get("timestamp") or item.get("updateTime") or 0
                ts_ms = int(to_float(ts_raw, 0.0))
                if 0 < ts_ms < 10**11:
                    ts_ms *= 1000
                uniq = "|".join([
                    str(item.get("tranId") or item.get("id") or item.get("orderId") or ""),
                    str(ts_ms),
                    str(item.get("incomeType") or ""),
                    str(item.get("symbol") or ""),
                    str(item.get("income") or ""),
                ])
                if uniq not in seen_ids:
                    seen_ids.add(uniq)
                    all_rows.append({**item, "_ts_ms": ts_ms})

        if cursor_start <= start_ms:
            break
        cursor_end = cursor_start - 1

    all_rows.sort(key=lambda x: x.get("_ts_ms", 0), reverse=True)
    return all_rows


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/api/credentials", methods=["GET", "POST"])
def api_credentials():
    if request.method == "POST":
        data = request.get_json(force=True) or {}
        new_key = clean_secret(data.get("api_key", ""))
        new_secret = clean_secret(data.get("api_secret", ""))

        if not new_key or not new_secret:
            return jsonify({
                "code": -1,
                "valid": False,
                "msg": "Inserisci sia API Key che Secret Key"
            }), 400

        apply_api_credentials(new_key, new_secret, persist_env=True)
        bal = bingx_req("GET", "/openApi/swap/v2/user/balance", signed=True)
        is_ok = bal.get("code") == 0

        return jsonify({
            "code": 0 if is_ok else bal.get("code", -1),
            "valid": is_ok,
            "configured": True,
            "api_key_preview": f"{API_KEY[:4]}...{API_KEY[-4:]}" if len(API_KEY) >= 8 else "OK",
            "msg": "API salvate e verificate con successo su BingX" if is_ok else (bal.get("msg") or "Errore verifica firma BingX")
        })

    bal = bingx_req("GET", "/openApi/swap/v2/user/balance", signed=True) if (API_KEY and API_SECRET) else {"code": -2, "msg": "Chiavi non impostate"}
    return jsonify({
        "code": 0,
        "configured": bool(API_KEY and API_SECRET),
        "valid": bal.get("code") == 0,
        "api_key_preview": f"{API_KEY[:4]}...{API_KEY[-4:]}" if len(API_KEY) >= 8 else "",
        "msg": "API attive e valide" if bal.get("code") == 0 else (bal.get("msg") or "Chiavi non impostate")
    })


@app.route("/api/markets")
def api_markets():
    out = get_cached_markets(force_refresh=False)
    return jsonify({"code": 0, "count": len(out), "markets": out})


@app.route("/api/signal-lock", methods=["GET", "POST"])
def api_signal_lock():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        action = str(data.get("action", "")).upper()
        if action in ("UNLOCK", "RELEASE", "CLEAR"):
            return jsonify({"code": 0, "signal_lock": release_signal_lock()})
        sym_req = data.get("symbol", "BTC-USDT")
        tf_req = data.get("tf", "5m")
        q_req = compute_quant_brain(sym_req, tf_req)
        if is_exact_certain_signal(q_req):
            return jsonify({"code": 0, "locked": True, "signal_lock": engage_signal_lock(sym_req, tf_req, q_req)})
        return jsonify({"code": -1, "locked": False, "msg": "Segnale non ancora certo", "signal_lock": get_active_signal_lock()})
    return jsonify({"code": 0, "signal_lock": get_active_signal_lock()})


@app.route("/api/scan-category")
def api_scan_category():
    category = request.args.get("category", "ALL")
    query = request.args.get("query", "")
    tf = request.args.get("tf", "5m")
    current_symbol = request.args.get("symbol", "BTC-USDT")
    scan = scan_category_markets(category, query, tf, current_symbol, max_items=8)
    best = scan.get("best") or {}
    return jsonify({
        "code": 0,
        "scan": scan,
        "best_symbol": best.get("symbol", current_symbol),
        "quant": best.get("quant"),
        "signal_lock": get_active_signal_lock(),
        "timing": candle_timing_info(tf),
        "signals_log": SIGNAL_LOG
    })


@app.route("/api/auto-scan")
def api_auto_scan():
    category = request.args.get("category", "ALL")
    query = request.args.get("query", "")
    tf = request.args.get("tf", "5m")
    current_symbol = request.args.get("symbol", "BTC-USDT")
    scan = scan_category_markets(category, query, tf, current_symbol, max_items=8)
    best = scan.get("best") or {}
    lock_info = get_active_signal_lock()
    return jsonify({
        "code": 0,
        "scan": scan,
        "locked_found": bool(scan.get("locked_found") or lock_info.get("locked")),
        "best_symbol": lock_info.get("symbol") if lock_info.get("locked") else best.get("symbol", current_symbol),
        "quant": lock_info.get("quant") if lock_info.get("locked") else best.get("quant"),
        "signal_lock": lock_info,
        "timing": candle_timing_info(tf),
        "signals_log": SIGNAL_LOG
    })


@app.route("/api/tf-plan")
def api_tf_plan():
    symbol = request.args.get("symbol", "BTC-USDT")
    tf = request.args.get("tf", "5m")
    side = str(request.args.get("side", "BUY")).upper()
    if side not in ("BUY", "SELL"):
        side = "BUY"
    timing = candle_timing_info(tf)
    quant = compute_quant_brain(symbol, tf, side)
    if is_exact_certain_signal(quant):
        engage_signal_lock(symbol, tf, quant)
    return jsonify({"code": 0, "timing": timing, "quant": quant, "signal_lock": get_active_signal_lock(), "signals_log": SIGNAL_LOG})


@app.route("/api/live-ticker")
def api_live_ticker():
    symbol = request.args.get("symbol", "BTC-USDT")
    tf = request.args.get("tf", "5m")
    auto_closed = process_auto_close_expired()

    ticker = bingx_req("GET", "/openApi/swap/v2/quote/ticker", {"symbol": symbol})
    depth = bingx_req("GET", "/openApi/swap/v2/quote/depth", {"symbol": symbol, "limit": 10})
    funding = bingx_req("GET", "/openApi/swap/v2/quote/premiumIndex", {"symbol": symbol})
    quant = compute_quant_brain(symbol, tf)
    if is_exact_certain_signal(quant):
        engage_signal_lock(symbol, tf, quant)

    return jsonify({
        "ticker": ticker.get("data", {}),
        "depth": depth.get("data", {}),
        "funding": funding.get("data", {}),
        "quant": quant,
        "signal_lock": get_active_signal_lock(),
        "timing": candle_timing_info(tf),
        "auto_closed": auto_closed,
        "active_auto_closes": list(AUTO_CLOSE_WATCHLIST.values()),
        "signals_log": SIGNAL_LOG
    })


@app.route("/api/account")
def api_account():
    now_dt = datetime.now(timezone.utc)
    now_ms = int(time.time() * 1000)
    month_start_ms = int(datetime(now_dt.year, now_dt.month, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)

    balance = bingx_req("GET", "/openApi/swap/v2/user/balance", signed=True)
    positions = bingx_req("GET", "/openApi/swap/v2/user/positions", signed=True)
    month_income = fetch_income_window(month_start_ms, now_ms, max_windows=6)

    equity = 0.0
    available = 0.0
    if balance.get("code") == 0 and isinstance(balance.get("data"), dict):
        b = balance["data"].get("balance", {})
        equity = to_float(b.get("equity", 0), 0.0)
        available = to_float(b.get("availableMargin", 0), 0.0)

    raw_pos = positions.get("data", []) if positions.get("code") == 0 and isinstance(positions.get("data"), list) else []

    active_positions = []
    for p in raw_pos:
        amt = abs(to_float(p.get("positionAmt", 0), 0.0))
        if amt <= 0:
            continue
        entry = to_float(p.get("avgPrice", 0), 0.0)
        mark = to_float(p.get("markPrice", entry), entry)
        pnl = to_float(p.get("unrealizedProfit", 0), 0.0)
        lev = to_float(p.get("leverage", calc_dynamic_leverage(equity)), 1.0)
        notional = amt * entry
        margin_used = notional / lev if lev > 0 else notional
        pnl_pct = round((pnl / margin_used) * 100, 2) if margin_used > 0 else 0.0
        active_positions.append({
            **p,
            "positionAmt": amt,
            "avgPrice": entry,
            "markPrice": mark,
            "unrealizedProfit": pnl,
            "pnlPct": pnl_pct
        })

    return jsonify({
        "equity": equity,
        "available": available,
        "recommended_leverage": calc_dynamic_leverage(equity),
        "positions": active_positions,
        "income_history": month_income,
        "signal_lock": get_active_signal_lock(),
        "signals_log": SIGNAL_LOG
    })


def submit_bingx_order(symbol: str, side: str, order_type: str, tf: str, qty: float, leverage: int, price=None, tp=None, sl=None, close_at_candle_end: bool = False):
    pos_side = "LONG" if side == "BUY" else "SHORT"
    qty_val = to_float(qty, 0.0)
    lev_val = int(to_float(leverage, 20))

    bingx_req("POST", "/openApi/swap/v2/trade/leverage", {
        "symbol": symbol,
        "side": pos_side,
        "leverage": lev_val
    }, signed=True)

    ticker = bingx_req("GET", "/openApi/swap/v2/quote/ticker", {"symbol": symbol})
    t_data = ticker.get("data") if isinstance(ticker.get("data"), dict) else {}
    mark_ref = to_float(t_data.get("lastPrice", 0), 0.0)
    if mark_ref <= 0:
        q_tmp = compute_quant_brain(symbol, tf, side)
        mark_ref = to_float(q_tmp.get("price", 0), 0.0)

    clean_tp, clean_sl = sanitize_tp_sl_for_side(side, mark_ref, tp, sl)

    order_params = {
        "symbol": symbol,
        "side": side,
        "positionSide": pos_side,
        "type": order_type,
        "quantity": qty_val
    }
    if order_type == "LIMIT" and price not in (None, ""):
        order_params["price"] = format_price_smart(to_float(price, mark_ref), mark_ref)
    if clean_tp not in (None, 0, 0.0):
        order_params["takeProfit"] = json.dumps({
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": float(clean_tp),
            "workingType": "MARK_PRICE"
        }, separators=(",", ":"))
    if clean_sl not in (None, 0, 0.0):
        order_params["stopLoss"] = json.dumps({
            "type": "STOP_MARKET",
            "stopPrice": float(clean_sl),
            "workingType": "MARK_PRICE"
        }, separators=(",", ":"))

    res = bingx_req("POST", "/openApi/swap/v2/trade/order", order_params, signed=True)

    if res.get("code") != 0 and ("takeProfit" in order_params or "stopLoss" in order_params):
        base_params = {
            "symbol": symbol,
            "side": side,
            "positionSide": pos_side,
            "type": order_type,
            "quantity": qty_val
        }
        if "price" in order_params:
            base_params["price"] = order_params["price"]
        base_res = bingx_req("POST", "/openApi/swap/v2/trade/order", base_params, signed=True)
        if base_res.get("code") == 0:
            order_id = ((base_res.get("data") or {}).get("order") or {}).get("orderId")
            if clean_tp:
                bingx_req("POST", "/openApi/swap/v2/trade/order", {
                    "symbol": symbol,
                    "side": "SELL" if side == "BUY" else "BUY",
                    "positionSide": pos_side,
                    "type": "TAKE_PROFIT_MARKET",
                    "stopPrice": float(clean_tp),
                    "quantity": qty_val,
                    "workingType": "MARK_PRICE"
                }, signed=True)
            if clean_sl:
                bingx_req("POST", "/openApi/swap/v2/trade/order", {
                    "symbol": symbol,
                    "side": "SELL" if side == "BUY" else "BUY",
                    "positionSide": pos_side,
                    "type": "STOP_MARKET",
                    "stopPrice": float(clean_sl),
                    "quantity": qty_val,
                    "workingType": "MARK_PRICE"
                }, signed=True)
            res = {**base_res, "orderId": order_id, "tp_applied": clean_tp, "sl_applied": clean_sl}

    if res.get("code") == 0 and close_at_candle_end:
        timing = candle_timing_info(tf)
        watch_key = f"{symbol}_{pos_side}_{timing['candle_end_ts']}"
        AUTO_CLOSE_WATCHLIST[watch_key] = {
            "symbol": symbol,
            "side": pos_side,
            "qty": qty_val,
            "tf": tf,
            "close_at": timing["candle_end_ts"]
        }

    if res.get("code") != 0:
        bx_msg = res.get("msg") or res.get("message") or "Ordine rifiutato da BingX"
        return {
            **res,
            "code": res.get("code", -1),
            "msg": f"{bx_msg} (simbolo={symbol}, lato={pos_side}, qty={qty_val}, leva={lev_val}x)"
        }

    return res


@app.route("/api/order", methods=["POST"])
def api_order():
    data = request.get_json(force=True) or {}
    lock_info = get_active_signal_lock()
    from_lock = bool(data.get("from_lock", False))

    symbol = lock_info.get("symbol") if (lock_info.get("locked") and lock_info.get("symbol")) else data.get("symbol", "BTC-USDT")
    side = str(data.get("side") or (lock_info.get("side") if lock_info.get("locked") else "NEUTRAL")).upper()
    order_type = str(data.get("type", "MARKET")).upper()
    tf = lock_info.get("tf") if (lock_info.get("locked") and lock_info.get("tf")) else str(data.get("tf", "5m"))
    qty = to_float(data.get("quantity", 0), 0.0)
    leverage = int(to_float(data.get("leverage", 20), 20))
    price = data.get("price")
    tp = data.get("tp")
    sl = data.get("sl")
    close_at_candle_end = bool(data.get("close_at_candle_end", False))
    anti_fakeout = bool(data.get("anti_fakeout", True))
    anti_breakout = bool(data.get("anti_breakout", True))
    min_certainty = int(to_float(data.get("min_certainty", 70), 70))
    allow_override = bool(data.get("allow_override", False))

    if side not in ("BUY", "SELL"):
        return jsonify({"code": -1, "msg": "Stato NEUTRAL: nessun ordine inviato"}), 400
    if qty <= 0:
        return jsonify({"code": -1, "msg": "Quantita' non valida"}), 400

    quant = compute_quant_brain(symbol, tf, side)
    if is_exact_certain_signal(quant):
        engage_signal_lock(symbol, tf, quant)

    if tp in (None, "") and quant.get("tp"):
        tp = quant.get("tp")
    if sl in (None, "") and quant.get("sl"):
        sl = quant.get("sl")

    effective_override = allow_override or from_lock
    ok_filter, reason = check_anti_fakeout_and_breakout(
        symbol, tf, side, anti_fakeout, anti_breakout, quant, min_certainty, effective_override
    )
    if not ok_filter:
        record_signal({
            "symbol": symbol,
            "side": side,
            "tf": tf,
            "certainty_label": quant.get("certainty_label"),
            "tp1": quant.get("tp1"),
            "tp2": quant.get("tp2"),
            "tp3": quant.get("tp3"),
            "sl": quant.get("sl"),
            "outcome": "BLOCCATO"
        })
        return jsonify({"code": -10, "msg": reason, "status": "NEUTRAL", "signal_lock": get_active_signal_lock(), "signals_log": SIGNAL_LOG}), 400

    res = submit_bingx_order(symbol, side, order_type, tf, qty, leverage, price, tp, sl, close_at_candle_end)

    record_signal({
        "symbol": symbol,
        "side": side,
        "tf": tf,
        "certainty_label": quant.get("certainty_label"),
        "tp1": quant.get("tp1"),
        "tp2": tp or quant.get("tp2"),
        "tp3": quant.get("tp3"),
        "sl": sl or quant.get("sl"),
        "outcome": "APERTO SU BINGX" if res.get("code") == 0 else (res.get("msg") or "ERRORE_API")
    })

    status_code = 200 if res.get("code") == 0 else 400
    return jsonify({**res, "signal_lock": get_active_signal_lock(), "signals_log": SIGNAL_LOG}), status_code


@app.route("/api/auto-trade", methods=["POST"])
def api_auto_trade():
    data = request.get_json(force=True) or {}
    category = str(data.get("category", "ALL"))
    query = str(data.get("query", ""))
    scan_all_cat = bool(data.get("scan_category", True))
    auto_trade_enabled = bool(data.get("auto_trade_enabled", True))
    current_symbol = data.get("symbol", "BTC-USDT")
    tf = str(data.get("tf", "5m"))
    qty = to_float(data.get("quantity", 0), 0.0)
    leverage = int(to_float(data.get("leverage", 20), 20))
    score_threshold = int(to_float(data.get("score_threshold", 60), 60))
    close_at_candle_end = bool(data.get("close_at_candle_end", True))
    anti_fakeout = bool(data.get("anti_fakeout", True))
    anti_breakout = bool(data.get("anti_breakout", True))

    if not auto_trade_enabled:
        scan = scan_category_markets(category, query, tf, current_symbol, max_items=8)
        best = scan.get("best") or {}
        lock_info = get_active_signal_lock()
        msg = (
            f"SEGNALE CERTO BLOCCATO {lock_info.get('side')} {lock_info.get('symbol')} — Modalita' MANUALE (apri tu)"
            if lock_info.get("locked")
            else f"SCANSIONE AUTONOMA {category} ({scan.get('scanned_count', 0)} coppie): in attesa di segnale certo (MANUALE)"
        )
        return jsonify({
            "code": 0,
            "triggered": False,
            "selected_symbol": lock_info.get("symbol") if lock_info.get("locked") else best.get("symbol", current_symbol),
            "msg": msg,
            "quant": lock_info.get("quant") if lock_info.get("locked") else best.get("quant"),
            "signal_lock": lock_info,
            "signals_log": SIGNAL_LOG
        })

    if qty <= 0:
        return jsonify({"code": -1, "triggered": False, "msg": "AUTO-TRADE: quantita' non valida", "signal_lock": get_active_signal_lock(), "signals_log": SIGNAL_LOG})

    lock_info = get_active_signal_lock()
    if lock_info.get("locked") and lock_info.get("symbol") and lock_info.get("side"):
        c_sym = lock_info["symbol"]
        side = lock_info["side"]
        tf_use = lock_info.get("tf") or tf
        quant_side = compute_quant_brain(c_sym, tf_use, side)
        if is_exact_certain_signal(quant_side):
            engage_signal_lock(c_sym, tf_use, quant_side)
            cooldown_key = f"{c_sym}_{tf_use}_{side}"
            now_ts = time.time()
            tf_sec = TF_SECONDS.get(tf_use, 300)
            if now_ts - LAST_AUTO_ORDER_TS.get(cooldown_key, 0) < tf_sec:
                return jsonify({
                    "code": 0,
                    "triggered": False,
                    "selected_symbol": c_sym,
                    "msg": f"SEGNALE CERTO BLOCCATO {side} {c_sym} ({lock_info.get('remaining_sec')}s residui)",
                    "quant": quant_side,
                    "signal_lock": get_active_signal_lock(),
                    "signals_log": SIGNAL_LOG
                })

            res = submit_bingx_order(
                symbol=c_sym,
                side=side,
                order_type="MARKET",
                tf=tf_use,
                qty=qty,
                leverage=leverage,
                tp=quant_side.get("tp"),
                sl=quant_side.get("sl"),
                close_at_candle_end=close_at_candle_end
            )
            if res.get("code") == 0:
                LAST_AUTO_ORDER_TS[cooldown_key] = now_ts
                record_signal({
                    "symbol": c_sym,
                    "side": side,
                    "tf": tf_use,
                    "certainty_label": f"AUTO-TRADE BLOCCATO • {quant_side.get('certainty_label')}",
                    "tp1": quant_side.get("tp1"),
                    "tp2": quant_side.get("tp2"),
                    "tp3": quant_side.get("tp3"),
                    "sl": quant_side.get("sl"),
                    "outcome": "APERTO SU BINGX"
                })
                return jsonify({
                    "code": 0,
                    "triggered": True,
                    "side": side,
                    "selected_symbol": c_sym,
                    "msg": f"POSIZIONE APERTA SU BINGX: AUTO {side} {c_sym} (SEGNALE CERTO BLOCCATO)",
                    "quant": quant_side,
                    "order_res": res,
                    "signal_lock": get_active_signal_lock(),
                    "signals_log": SIGNAL_LOG
                })
            else:
                return jsonify({
                    "code": res.get("code", -1),
                    "triggered": False,
                    "selected_symbol": c_sym,
                    "msg": res.get("msg") or "Ordine rifiutato da BingX",
                    "quant": quant_side,
                    "signal_lock": get_active_signal_lock(),
                    "signals_log": SIGNAL_LOG
                })
        else:
            release_signal_lock()

    if scan_all_cat:
        scan = scan_category_markets(category, query, tf, current_symbol, max_items=8)
        candidates = scan.get("results") or []
    else:
        q0 = compute_quant_brain(current_symbol, tf)
        candidates = [{"symbol": current_symbol, "category": categorize_symbol(current_symbol), "quant": q0}]

    last_status_msg = f"AUTO-TRADE IN ATTESA ({category}): nessun segnale certo sopra ±{score_threshold}"
    display_quant = candidates[0].get("quant") if candidates else compute_quant_brain(current_symbol, tf)
    display_symbol = candidates[0].get("symbol") if candidates else current_symbol

    for cand in candidates:
        c_sym = cand["symbol"]
        q_base = cand.get("quant") or compute_quant_brain(c_sym, tf)
        score = int(q_base.get("score", 0) or 0)
        exp_r = to_float(q_base.get("expectancy_r", 0.0), 0.0)
        profit_ready = bool(q_base.get("profit_ready", False))

        if abs(score) >= abs(display_quant.get("score", 0)):
            display_quant = q_base
            display_symbol = c_sym

        if is_exact_certain_signal(q_base):
            engage_signal_lock(c_sym, tf, q_base)
            side = q_base["signal"]
        elif score >= score_threshold:
            side = "BUY"
        elif score <= -abs(score_threshold):
            side = "SELL"
        else:
            last_status_msg = f"SCAN {category} ({c_sym}): Score {score} < ±{score_threshold}"
            continue

        if exp_r <= 0 or not profit_ready:
            last_status_msg = f"SCAN {category} ({c_sym}): Exp {exp_r}R non positiva"
            continue

        quant_side = compute_quant_brain(c_sym, tf, side)
        if is_exact_certain_signal(quant_side):
            engage_signal_lock(c_sym, tf, quant_side)

        ok_filter, reason = check_anti_fakeout_and_breakout(
            c_sym, tf, side, anti_fakeout, anti_breakout, quant_side, min_certainty=50, allow_override=False
        )
        if not ok_filter:
            last_status_msg = f"SCAN {category} ({c_sym}): {reason}"
            continue

        cooldown_key = f"{c_sym}_{tf}_{side}"
        now_ts = time.time()
        tf_sec = TF_SECONDS.get(tf, 300)
        if now_ts - LAST_AUTO_ORDER_TS.get(cooldown_key, 0) < tf_sec:
            last_status_msg = f"COOLDOWN {c_sym} ({tf}): ordine gia' inviato in questa candela"
            continue

        res = submit_bingx_order(
            symbol=c_sym,
            side=side,
            order_type="MARKET",
            tf=tf,
            qty=qty,
            leverage=leverage,
            tp=quant_side.get("tp"),
            sl=quant_side.get("sl"),
            close_at_candle_end=close_at_candle_end
        )

        if res.get("code") == 0:
            LAST_AUTO_ORDER_TS[cooldown_key] = now_ts
            record_signal({
                "symbol": c_sym,
                "side": side,
                "tf": tf,
                "certainty_label": f"AUTO-TRADE [{category}] • {quant_side.get('certainty_label')}",
                "tp1": quant_side.get("tp1"),
                "tp2": quant_side.get("tp2"),
                "tp3": quant_side.get("tp3"),
                "sl": quant_side.get("sl"),
                "outcome": "APERTO SU BINGX"
            })
            return jsonify({
                "code": 0,
                "triggered": True,
                "side": side,
                "selected_symbol": c_sym,
                "msg": f"POSIZIONE APERTA SU BINGX: AUTO {side} {c_sym} [{category}]",
                "quant": quant_side,
                "order_res": res,
                "signal_lock": get_active_signal_lock(),
                "signals_log": SIGNAL_LOG
            })

        last_status_msg = res.get("msg") or f"Errore ordine su {c_sym}"

    return jsonify({
        "code": 0,
        "triggered": False,
        "selected_symbol": display_symbol,
        "msg": last_status_msg,
        "quant": display_quant,
        "signal_lock": get_active_signal_lock(),
        "signals_log": SIGNAL_LOG
    })


@app.route("/api/close-all", methods=["POST"])
def api_close_all():
    data = request.get_json(silent=True) or {}
    params = {"symbol": data["symbol"]} if data.get("symbol") else {}
    res = bingx_req("POST", "/openApi/swap/v2/trade/closeAllPositions", params, signed=True)
    return jsonify(res)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
