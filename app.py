import os
import hmac
import hashlib
import time
import json
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
    "enabled": False,
    "symbol": "BTC-USDT",
    "tf": "5m",
    "qty": 0.001,
    "leverage": 20,
    "close_at_candle_end": True,
    "min_confidence": 75.0,
    "last_signal_key": None,
    "last_check_ts": 0,
    "last_action": "In attesa",
}

AUTO_CLOSE_WATCHLIST = {}
SIGNAL_HISTORY = []
SEEN_SIGNAL_KEYS = set()
ACTIVE_TPSL_REGISTRY = {}


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
    if requested_leverage is not None and to_float(requested_leverage, 0) > 0:
        lev = int(to_float(requested_leverage, 20))
    else:
        lev = 20
    if cap > 0 and cap <= 2.0:
        return max(lev, 20)
    return max(1, min(lev, 125))


def tf_to_seconds(tf: str) -> int:
    mapping = {
        "1m": 60,
        "3m": 180,
        "5m": 300,
        "15m": 900,
        "30m": 1800,
        "1h": 3600,
        "2h": 7200,
        "4h": 14400,
        "1d": 86400,
    }
    return mapping.get((tf or "5m").lower(), 300)


def candle_timing_info(tf: str) -> dict:
    sec = tf_to_seconds(tf)
    now_ts = int(time.time())
    candle_start_ts = (now_ts // sec) * sec
    candle_end_ts = candle_start_ts + sec
    remaining_sec = max(0, candle_end_ts - now_ts)
    return {
        "tf": tf,
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
        sorted_items.append((str(k), fmt_num_str(v) if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool) else str(v)))
    query_string = urlencode(sorted_items)
    signature = hmac.new(
        BINGX_SECRET_KEY.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    return f"{query_string}&signature={signature}"


def bingx_req(method: str, path: str, params: dict = None, signed: bool = False):
    p = dict(params or {})
    headers = {}
    if BINGX_API_KEY:
        headers["X-BX-APIKEY"] = BINGX_API_KEY

    if signed:
        if not BINGX_API_KEY or not BINGX_SECRET_KEY:
            return {"code": -100, "msg": "Chiavi API BingX non configurate (BINGX_API_KEY / BINGX_SECRET_KEY)"}
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
            resp = requests.get(url, headers=headers, timeout=12)
        elif m == "POST":
            resp = requests.post(url, headers=headers, timeout=12)
        elif m == "DELETE":
            resp = requests.delete(url, headers=headers, timeout=12)
        else:
            resp = requests.request(m, url, headers=headers, timeout=12)
        data = resp.json()
        if isinstance(data, dict):
            return data
        return {"code": resp.status_code, "msg": "Risposta non JSON da BingX", "raw": data}
    except Exception as exc:
        return {"code": -200, "msg": f"Errore rete BingX: {exc}"}


def fetch_klines(symbol: str, tf: str = "5m", limit: int = 80):
    interval_map = {
        "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m",
        "30m": "30m", "1h": "1h", "2h": "2h", "4h": "4h", "1d": "1d"
    }
    interval = interval_map.get((tf or "5m").lower(), "5m")
    res = bingx_req("GET", "/openApi/swap/v3/quote/klines", {
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    })
    raw_list = res.get("data")
    if not isinstance(raw_list, list) or not raw_list:
        res_v2 = bingx_req("GET", "/openApi/swap/v2/quote/klines", {
            "symbol": symbol,
            "interval": interval,
            "limit": limit
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
    k = 2.0 / (period + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1.0 - k))
    return out


def compute_quant_brain(symbol: str, tf: str = "5m", forced_side: str = None) -> dict:
    candles = fetch_klines(symbol, tf=tf, limit=80)
    ticker = bingx_req("GET", "/openApi/swap/v2/quote/ticker", {"symbol": symbol})
    t_data = ticker.get("data") if isinstance(ticker.get("data"), dict) else {}
    live_price = to_float(t_data.get("lastPrice"), 0.0)

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

    atr_pct = (atr / live_price) if live_price > 0 else 0.004
    step_dist = max(live_price * max(atr_pct, 0.0025), live_price * 0.002)

    if forced_side in ("BUY", "SELL"):
        signal = forced_side
    else:
        if ema_fast > ema_slow and momentum_pct >= -0.05:
            signal = "BUY"
        elif ema_fast < ema_slow and momentum_pct <= 0.05:
            signal = "SELL"
        else:
            signal = "NEUTRAL"

    raw_conf = 62.0 + min(28.0, abs(momentum_pct) * 18.0 + abs(ema_fast - ema_slow) / max(live_price, 1e-9) * 4500.0)
    confidence = round(min(94.5, max(52.0, raw_conf)), 1)

    if signal == "BUY":
        tp1 = format_price_smart(live_price + step_dist * 0.8, live_price)
        tp2 = format_price_smart(live_price + step_dist * 1.5, live_price)
        sl = format_price_smart(max(live_price * 0.0001, live_price - step_dist * 0.9), live_price)
        label = "SEGNALE CERTO LONG"
    elif signal == "SELL":
        tp1 = format_price_smart(max(live_price * 0.0001, live_price - step_dist * 0.8), live_price)
        tp2 = format_price_smart(max(live_price * 0.0001, live_price - step_dist * 1.5), live_price)
        sl = format_price_smart(live_price + step_dist * 0.9, live_price)
        label = "SEGNALE CERTO SHORT"
    else:
        tp1 = format_price_smart(live_price + step_dist * 0.8, live_price)
        tp2 = format_price_smart(live_price + step_dist * 1.5, live_price)
        sl = format_price_smart(max(live_price * 0.0001, live_price - step_dist * 0.9), live_price)
        label = "NEUTRALE / LATERALE"

    return {
        "symbol": symbol,
        "tf": tf,
        "price": format_price_smart(live_price, live_price),
        "signal": signal,
        "confidence": confidence,
        "label": label,
        "tp1": tp1,
        "tp2": tp2,
        "tp": tp2,
        "sl": sl,
        "ema_fast": format_price_smart(ema_fast, live_price),
        "ema_slow": format_price_smart(ema_slow, live_price),
        "atr": format_price_smart(atr, live_price),
        "candles": candles[-40:],
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

    clean_tp = None
    clean_sl = None

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
        tp_params = {
            "symbol": symbol,
            "side": close_side,
            "positionSide": pos_side,
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": fmt_num_str(clean_tp),
            "quantity": fmt_num_str(qty_val),
            "workingType": "MARK_PRICE",
        }
        attached["tp_order"] = bingx_req("POST", "/openApi/swap/v2/trade/order", tp_params, signed=True)

    if clean_sl is not None and to_float(clean_sl, 0.0) > 0:
        sl_params = {
            "symbol": symbol,
            "side": close_side,
            "positionSide": pos_side,
            "type": "STOP_MARKET",
            "stopPrice": fmt_num_str(clean_sl),
            "quantity": fmt_num_str(qty_val),
            "workingType": "MARK_PRICE",
        }
        attached["sl_order"] = bingx_req("POST", "/openApi/swap/v2/trade/order", sl_params, signed=True)

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
    quant: dict = None
):
    pos_side = "LONG" if side == "BUY" else "SHORT"
    qty_val = to_float(qty, 0.0)
    lev_val = int(to_float(leverage, 20))

    if qty_val <= 0:
        return {"code": -10, "msg": "Quantita' ordine non valida (deve essere > 0)"}

    bingx_req("POST", "/openApi/swap/v2/trade/leverage", {
        "symbol": symbol,
        "side": pos_side,
        "leverage": lev_val
    }, signed=True)

    q_ref = quant if isinstance(quant, dict) and quant.get("price") else compute_quant_brain(symbol, tf, side)

    ticker = bingx_req("GET", "/openApi/swap/v2/quote/ticker", {"symbol": symbol})
    t_data = ticker.get("data") if isinstance(ticker.get("data"), dict) else {}
    mark_ref = to_float(t_data.get("lastPrice", 0), 0.0)
    if mark_ref <= 0:
        mark_ref = to_float(q_ref.get("price", 0), 0.0)

    eff_tp = tp if tp not in (None, "", 0, 0.0) else (q_ref.get("tp") or q_ref.get("tp2") or q_ref.get("tp1"))
    eff_sl = sl if sl not in (None, "", 0, 0.0) else q_ref.get("sl")

    clean_tp, clean_sl = sanitize_tp_sl_for_side(side, mark_ref, eff_tp, eff_sl)

    order_params = {
        "symbol": symbol,
        "side": side,
        "positionSide": pos_side,
        "type": order_type,
        "quantity": fmt_num_str(qty_val)
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

        res = {
            **res,
            "tp_applied": clean_tp if tp_ok else None,
            "sl_applied": clean_sl if sl_ok else None,
            "tp_sl_orders": tp_sl_status,
            "tpsl_summary": f"{tp_info} | {sl_info}",
        }

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
            outcome=f"APERTO SU BINGX ({res['tpsl_summary']})",
        )
        return res

    bx_msg = res.get("msg") or res.get("message") or "Ordine rifiutato da BingX"
    return {
        **res,
        "code": res.get("code", -1),
        "msg": f"{bx_msg} (simbolo={symbol}, lato={pos_side}, qty={fmt_num_str(qty_val)}, leva={lev_val}x, tp={fmt_num_str(clean_tp) if clean_tp else '-'}, sl={fmt_num_str(clean_sl) if clean_sl else '-'})"
    }


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


def fetch_balance_summary():
    res = bingx_req("GET", "/openApi/swap/v2/user/balance", signed=True)
    if res.get("code") != 0 or not isinstance(res.get("data"), dict):
        return {"balance": 0.0, "available": 0.0, "unrealized": 0.0, "equity": 0.0, "raw_code": res.get("code")}
    bal = res["data"].get("balance") or {}
    total_bal = to_float(bal.get("balance"), 0.0)
    avail = to_float(bal.get("availableMargin") or bal.get("free"), total_bal)
    unreal = to_float(bal.get("unrealizedProfit"), 0.0)
    return {
        "balance": total_bal,
        "available": avail,
        "unrealized": unreal,
        "equity": total_bal + unreal,
        "raw_code": 0,
    }


def fetch_income_history(limit: int = 30):
    res = bingx_req("GET", "/openApi/swap/v2/user/income", {"limit": limit}, signed=True)
    if res.get("code") != 0 or not isinstance(res.get("data"), list):
        return []
    rows = []
    for item in res["data"][:limit]:
        rows.append({
            "type": item.get("incomeType", "REALIZED_PNL"),
            "symbol": item.get("symbol", "-"),
            "income": to_float(item.get("income"), 0.0),
            "asset": item.get("asset", "USDT"),
            "time": int(to_float(item.get("time"), 0)),
        })
    return rows


def process_auto_close_expired():
    now_ts = int(time.time())
    with STATE_LOCK:
        due_keys = [k for k, v in AUTO_CLOSE_WATCHLIST.items() if now_ts >= int(v.get("close_at", 0))]
        due_items = [(k, dict(AUTO_CLOSE_WATCHLIST[k])) for k in due_keys]
        for k in due_keys:
            del AUTO_CLOSE_WATCHLIST[k]

    closed_events = []
    for _, item in due_items:
        sym = item["symbol"]
        p_side = item["side"]
        tf = item.get("tf", "5m")
        close_res = bingx_req("POST", "/openApi/swap/v2/trade/closeAllPositions", {"symbol": sym}, signed=True)
        closed_events.append({"symbol": sym, "side": p_side, "res": close_res})
        _record_signal_log(
            symbol=sym,
            tf=tf,
            signal="CLOSE_TF",
            confidence=100.0,
            label="CHIUSURA AUTOMATICA FINE CANDELA",
            tp1="-",
            tp2="-",
            sl="-",
            outcome="CHIUSO",
        )
    return closed_events


def process_auto_trade_tick():
    with STATE_LOCK:
        if not AUTO_TRADE_STATE.get("enabled"):
            return None
        sym = AUTO_TRADE_STATE.get("symbol", "BTC-USDT")
        tf = AUTO_TRADE_STATE.get("tf", "5m")
        qty = to_float(AUTO_TRADE_STATE.get("qty"), 0.001)
        lev = int(to_float(AUTO_TRADE_STATE.get("leverage"), 20))
        close_at_end = bool(AUTO_TRADE_STATE.get("close_at_candle_end", True))
        min_conf = to_float(AUTO_TRADE_STATE.get("min_confidence"), 75.0)
        AUTO_TRADE_STATE["last_check_ts"] = int(time.time())

    timing = candle_timing_info(tf)
    quant = compute_quant_brain(sym, tf)
    sig = quant.get("signal", "NEUTRAL")
    conf = to_float(quant.get("confidence"), 0.0)
    dedup_key = f"{sym}_{tf}_{timing['candle_start_ts']}_{sig}"

    if sig not in ("BUY", "SELL"):
        _record_signal_log(
            symbol=sym, tf=tf, signal=sig, confidence=conf,
            label=quant.get("label", "NEUTRALE"),
            tp1=quant.get("tp1"), tp2=quant.get("tp2"), sl=quant.get("sl"),
            outcome="SEGNALE BLOCCATO", dedup_key=dedup_key
        )
        return None

    if conf < min_conf:
        _record_signal_log(
            symbol=sym, tf=tf, signal=f"{sig} • BLOCCATO", confidence=conf,
            label=quant.get("label", "CONFIDENZA BASSA"),
            tp1=quant.get("tp1"), tp2=quant.get("tp2"), sl=quant.get("sl"),
            outcome="SEGNALE BLOCCATO", dedup_key=dedup_key
        )
        return None

    with STATE_LOCK:
        if AUTO_TRADE_STATE.get("last_signal_key") == dedup_key:
            return None
        AUTO_TRADE_STATE["last_signal_key"] = dedup_key

    bal = fetch_balance_summary()
    eff_lev = get_dynamic_leverage(bal.get("available", 0.0), lev)
    order_res = submit_bingx_order(
        symbol=sym,
        side=sig,
        order_type="MARKET",
        tf=tf,
        qty=qty,
        leverage=eff_lev,
        tp=quant.get("tp"),
        sl=quant.get("sl"),
        close_at_candle_end=close_at_end,
        quant=quant,
    )
    with STATE_LOCK:
        AUTO_TRADE_STATE["last_action"] = f"{sig} {sym} -> {order_res.get('tpsl_summary', order_res.get('msg', 'OK'))}"
    return order_res


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/live-ticker")
def api_live_ticker():
    symbol = (request.args.get("symbol") or "BTC-USDT").strip().upper()
    tf = (request.args.get("tf") or "5m").strip().lower()

    process_auto_close_expired()
    process_auto_trade_tick()

    quant = compute_quant_brain(symbol, tf)
    balance = fetch_balance_summary()
    positions = fetch_positions_raw()
    income = fetch_income_history(20)
    timing = candle_timing_info(tf)

    with STATE_LOCK:
        logs = list(SIGNAL_HISTORY[:40])
        auto_state = dict(AUTO_TRADE_STATE)

    return jsonify({
        "ok": True,
        "quant": quant,
        "balance": balance,
        "positions": positions,
        "income": income,
        "timing": timing,
        "logs": logs,
        "auto_trade": auto_state,
    })


@app.route("/api/order", methods=["POST"])
def api_order():
    data = request.get_json(silent=True) or {}
    symbol = (data.get("symbol") or "BTC-USDT").strip().upper()
    side = (data.get("side") or "BUY").strip().upper()
    if side not in ("BUY", "SELL"):
        side = "BUY"
    order_type = (data.get("order_type") or "MARKET").strip().upper()
    tf = (data.get("tf") or "5m").strip().lower()
    qty = to_float(data.get("qty"), 0.001)

    bal = fetch_balance_summary()
    req_lev = int(to_float(data.get("leverage"), 20))
    lev = get_dynamic_leverage(bal.get("available", 0.0), req_lev)

    price = data.get("price")
    tp = data.get("tp")
    sl = data.get("sl")
    close_at_candle_end = bool(data.get("close_at_candle_end", True))

    result = submit_bingx_order(
        symbol=symbol,
        side=side,
        order_type=order_type,
        tf=tf,
        qty=qty,
        leverage=lev,
        price=price,
        tp=tp,
        sl=sl,
        close_at_candle_end=close_at_candle_end,
    )
    return jsonify({
        "ok": result.get("code") == 0,
        "result": result,
        "positions": fetch_positions_raw(),
        "balance": fetch_balance_summary(),
        "logs": list(SIGNAL_HISTORY[:40]),
    })


@app.route("/api/close-position", methods=["POST"])
def api_close_position():
    data = request.get_json(silent=True) or {}
    symbol = (data.get("symbol") or "BTC-USDT").strip().upper()
    res = bingx_req("POST", "/openApi/swap/v2/trade/closeAllPositions", {"symbol": symbol}, signed=True)
    return jsonify({
        "ok": res.get("code") == 0,
        "result": res,
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
            if "symbol" in data and data["symbol"]:
                AUTO_TRADE_STATE["symbol"] = str(data["symbol"]).strip().upper()
            if "tf" in data and data["tf"]:
                AUTO_TRADE_STATE["tf"] = str(data["tf"]).strip().lower()
            if "qty" in data:
                AUTO_TRADE_STATE["qty"] = max(0.0001, to_float(data["qty"], 0.001))
            if "leverage" in data:
                AUTO_TRADE_STATE["leverage"] = max(1, min(125, int(to_float(data["leverage"], 20))))
            if "close_at_candle_end" in data:
                AUTO_TRADE_STATE["close_at_candle_end"] = bool(data["close_at_candle_end"])
            if "min_confidence" in data:
                AUTO_TRADE_STATE["min_confidence"] = max(50.0, min(99.0, to_float(data["min_confidence"], 75.0)))
            AUTO_TRADE_STATE["last_action"] = "Attivo" if AUTO_TRADE_STATE["enabled"] else "Disattivato"

    with STATE_LOCK:
        st = dict(AUTO_TRADE_STATE)
    return jsonify({"ok": True, "auto_trade": st})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
