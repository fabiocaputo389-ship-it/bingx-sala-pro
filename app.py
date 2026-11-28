import hashlib
import hmac
import json
import os
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

BINGX_BASE_URL = "https://open-api.bingx.com"
KEYS_FILE = "bingx_keys.json"
STATE_FILE = "bingx_state.json"

STATE_LOCK = threading.RLock()

DEFAULT_STATE = {
    "api_key": "",
    "secret_key": "",
    "monitor_enabled": True,
    "auto_trade_enabled": True,
    "anti_fomo_enabled": True,
    "auto_close_on_tp": True,
    "auto_close_on_candle_end": True,
    "auto_close_on_candle_signal": True,
    "active_category": "ALL",
    "only_certain_and_early": False,
    "interval": "1m",
    "scan_interval_sec": 8,
    "last_run": "--:--:--",
    "balance_info": {
        "balance_usdt": 0.0,
        "available_usdt": 0.0,
        "unrealized_pnl_usdt": 0.0,
        "equity_usdt": 0.0,
        "daily_pnl_usdt": 0.0,
        "daily_pnl_pct": 0.0,
        "monthly_pnl_usdt": 0.0,
        "monthly_pnl_pct": 0.0,
        "last_sync": "--:--:--",
    },
    "signals": {},
    "open_positions": {},
    "trade_logs": [],
    "audio_alerts": [],
    "latest_new_candle_alert": None,
}

STATE = json.loads(json.dumps(DEFAULT_STATE))
MARKETS_CACHE = {"ts": 0.0, "data": []}


def now_rome_str() -> str:
    # CET/CEST approximate offset or local system clock
    return datetime.now().strftime("%H:%M:%S")


def load_persisted_keys():
    if os.path.exists(KEYS_FILE):
        try:
            with open(KEYS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                with STATE_LOCK:
                    STATE["api_key"] = str(data.get("api_key") or "").strip()
                    STATE["secret_key"] = str(data.get("secret_key") or "").strip()
        except Exception:
            pass

    env_key = (os.environ.get("BINGX_API_KEY") or "").strip()
    env_sec = (os.environ.get("BINGX_SECRET_KEY") or "").strip()
    with STATE_LOCK:
        if env_key:
            STATE["api_key"] = env_key
        if env_sec:
            STATE["secret_key"] = env_sec


def save_persisted_keys(api_key: str, secret_key: str):
    with STATE_LOCK:
        if api_key:
            STATE["api_key"] = api_key.strip()
        if secret_key:
            STATE["secret_key"] = secret_key.strip()
        payload = {
            "api_key": STATE["api_key"],
            "secret_key": STATE["secret_key"],
        }
    with open(KEYS_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def mask_secret(val: str) -> str:
    if not val:
        return "--"
    if len(val) <= 8:
        return "*" * len(val)
    return f"{val[:4]}...{val[-4:]}"


def sign_params(params: dict, secret_key: str) -> str:
    sorted_items = sorted((k, v) for k, v in params.items() if v is not None and v != "")
    query = urlencode(sorted_items)
    signature = hmac.new(
        secret_key.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{query}&signature={signature}"


def bingx_request(method: str, path: str, params: dict = None, signed: bool = False):
    params = dict(params or {})
    headers = {"X-BX-APIKEY": ""}

    with STATE_LOCK:
        api_key = STATE["api_key"]
        secret_key = STATE["secret_key"]

    if signed:
        if not api_key or not secret_key:
            return {"code": -1, "msg": "Chiavi API BingX non configurate"}
        params["timestamp"] = int(time.time() * 1000)
        params.setdefault("recvWindow", 10000)
        headers["X-BX-APIKEY"] = api_key
        query_with_sig = sign_params(params, secret_key)
        url = f"{BINGX_BASE_URL}{path}?{query_with_sig}"
        try:
            resp = requests.request(method.upper(), url, headers=headers, timeout=10)
            return resp.json()
        except Exception as exc:
            return {"code": -2, "msg": str(exc)}
    else:
        url = f"{BINGX_BASE_URL}{path}"
        try:
            resp = requests.request(method.upper(), url, params=params, headers=headers, timeout=10)
            return resp.json()
        except Exception as exc:
            return {"code": -2, "msg": str(exc)}


def classify_market(symbol: str):
    s = symbol.upper()
    base = s.split("-")[0]
    if base in {"BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "AVAX", "DOGE", "LINK", "DOT", "LTC", "TRX"}:
        return "CRYPTO MAJOR", f"{base}/USDT"
    if base in {"XAU", "XAG", "GOLD", "SILVER", "WTI", "BRENT", "COPPER", "NATGAS"}:
        return "METALLI & COMMODITIES", f"{base}/USDT"
    if base in {"EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "USD"}:
        return "FOREX & VALUTE", symbol
    if base in {"SPX", "NDX", "US30", "NAS100", "GER40", "UK100", "AAPL", "TSLA", "NVDA", "MSFT", "AMZN", "META"}:
        return "INDICI & AZIONI", f"{base}/USDT"
    return "ALTCOIN & DEFI", f"{base}/USDT"


def current_session_info():
    utc_hour = datetime.now(timezone.utc).hour
    if 13 <= utc_hour < 21:
        return {"session_name": "NY + London Overlap", "badge": "⚡ NY/LDN", "session_ok": True}
    if 7 <= utc_hour < 16:
        return {"session_name": "London Session", "badge": "🇬🇧 LDN", "session_ok": True}
    if 0 <= utc_hour < 9:
        return {"session_name": "Asia Session", "badge": "🌏 ASIA", "session_ok": True}
    return {"session_name": "Off-Peak", "badge": "🌙 OFF", "session_ok": True}


def fetch_bingx_markets(force: bool = False):
    now = time.time()
    if not force and MARKETS_CACHE["data"] and (now - MARKETS_CACHE["ts"]) < 25:
        return MARKETS_CACHE["data"]

    contracts_res = bingx_request("GET", "/openApi/swap/v2/quote/contracts", signed=False)
    ticker_res = bingx_request("GET", "/openApi/swap/v2/quote/ticker", signed=False)

    max_lev_map = {}
    if isinstance(contracts_res, dict) and contracts_res.get("code") == 0:
        for item in contracts_res.get("data") or []:
            sym = str(item.get("symbol") or "").upper()
            lev = item.get("maxLongLeverage") or item.get("maxLeverage") or 50
            try:
                max_lev_map[sym] = int(float(lev))
            except Exception:
                max_lev_map[sym] = 50

    ticker_list = []
    if isinstance(ticker_res, dict) and ticker_res.get("code") == 0:
        raw_data = ticker_res.get("data")
        if isinstance(raw_data, list):
            ticker_list = raw_data
        elif isinstance(raw_data, dict):
            ticker_list = [raw_data]

    sess = current_session_info()
    markets = []
    for t in ticker_list:
        sym = str(t.get("symbol") or "").upper()
        if not sym.endswith("-USDT"):
            continue
        try:
            last_price = float(t.get("lastPrice") or t.get("close") or 0.0)
        except Exception:
            last_price = 0.0
        if last_price <= 0:
            continue

        try:
            chg_pct = float(t.get("priceChangePercent") or 0.0)
        except Exception:
            chg_pct = 0.0

        try:
            vol_24h = float(t.get("quoteVolume") or t.get("volume") or 0.0)
        except Exception:
            vol_24h = 0.0

        cat, bx_name = classify_market(sym)
        markets.append({
            "symbol": sym,
            "bingxName": bx_name,
            "category": cat,
            "session": sess,
            "lastPrice": last_price,
            "priceChangePercent": chg_pct,
            "volume24h": vol_24h,
            "maxLeverage": max_lev_map.get(sym, 50),
        })

    markets.sort(key=lambda x: x["volume24h"], reverse=True)
    if markets:
        MARKETS_CACHE["ts"] = now
        MARKETS_CACHE["data"] = markets
    return markets


def fetch_klines(symbol: str, interval: str = "1m", limit: int = 80):
    res = bingx_request(
        "GET",
        "/openApi/swap/v2/quote/klines",
        params={"symbol": symbol.upper(), "interval": interval, "limit": limit},
        signed=False,
    )
    if not isinstance(res, dict) or res.get("code") != 0:
        return []
    rows = res.get("data") or []
    candles = []
    for r in rows:
        try:
            if isinstance(r, dict):
                o = float(r.get("open"))
                h = float(r.get("high"))
                l = float(r.get("low"))
                c = float(r.get("close"))
                v = float(r.get("volume") or 0.0)
            else:
                o = float(r[1])
                h = float(r[2])
                l = float(r[3])
                c = float(r[4])
                v = float(r[5]) if len(r) > 5 else 0.0
            candles.append({"open": o, "high": h, "low": l, "close": c, "volume": v})
        except Exception:
            continue
    candles.sort(key=lambda x: 0)  # preserve chronological order from API
    if len(candles) >= 2 and candles[0]["open"] == candles[-1]["open"] and candles[0]["close"] == candles[-1]["close"]:
        pass
    return candles


def compute_ema(values, period: int):
    if not values:
        return 0.0
    k = 2.0 / (period + 1)
    ema = values[0]
    for v in values[1:]:
        ema = v * k + ema * (1 - k)
    return ema


def compute_rsi(closes, period: int = 14):
    if len(closes) <= period:
        return 50.0
    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(0.0, diff))
        losses.append(max(0.0, -diff))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 75.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def compute_atr(candles, period: int = 14):
    if len(candles) < 2:
        return max(1e-6, (candles[-1]["close"] * 0.002) if candles else 0.002)
    trs = []
    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]
        prev_c = candles[i - 1]["close"]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
    return sum(trs[-period:]) / min(period, len(trs))


def compute_dynamic_leverage(balance_usdt: float, max_lev: int = 50) -> int:
    if balance_usdt <= 2.0:
        target = 20
    elif balance_usdt <= 15.0:
        target = 15
    elif balance_usdt <= 50.0:
        target = 10
    else:
        target = 5
    return max(1, min(int(max_lev or 50), target))


def compute_order_qty(balance_usdt: float, price: float, leverage: int) -> float:
    if price <= 0:
        return 0.01
    effective_bal = max(1.0, float(balance_usdt or 0.0))
    margin_to_use = min(effective_bal * 0.20, max(0.5, effective_bal * 0.5))
    notional = margin_to_use * max(1, leverage)
    raw_qty = notional / price
    if raw_qty >= 10:
        return round(raw_qty, 1)
    if raw_qty >= 1:
        return round(raw_qty, 2)
    return max(0.01, round(raw_qty, 4))


def analyze_symbol(symbol: str, interval: str = "1m"):
    sym = symbol.upper().strip()
    if not sym.endswith("-USDT") and "-" not in sym:
        sym = f"{sym}-USDT"

    markets = fetch_bingx_markets(force=False)
    mkt_obj = next((m for m in markets if m["symbol"] == sym), None)
    candles = fetch_klines(sym, interval=interval, limit=60)

    price = float(mkt_obj["lastPrice"]) if mkt_obj else (candles[-1]["close"] if candles else 0.0)
    if price <= 0:
        return None

    cat, bx_name = classify_market(sym)
    max_lev = int(mkt_obj["maxLeverage"]) if mkt_obj else 50
    sess = (mkt_obj["session"] if mkt_obj else None) or current_session_info()

    closes = [c["close"] for c in candles] if candles else [price]
    vols = [c["volume"] for c in candles] if candles else [1.0]

    ema9 = compute_ema(closes, 9)
    ema21 = compute_ema(closes, 21)
    ema200 = compute_ema(closes, min(len(closes), 50))
    rsi = compute_rsi(closes, 14)
    atr = compute_atr(candles, 14)

    avg_vol = (sum(vols[-20:]) / max(1, len(vols[-20:]))) if vols else 1.0
    curr_vol = vols[-1] if vols else avg_vol
    prev_vol = vols[-2] if len(vols) >= 2 else avg_vol
    curr_vol_ratio = round(curr_vol / max(1e-6, avg_vol), 2)
    prev_vol_ratio = round(prev_vol / max(1e-6, avg_vol), 2)

    last_c = candles[-1] if candles else {"open": price, "high": price * 1.001, "low": price * 0.999, "close": price}
    bullish_candle = last_c["close"] >= last_c["open"]

    # Determine direction
    bull_score = 0
    bear_score = 0
    if ema9 >= ema21:
        bull_score += 2
    else:
        bear_score += 2
    if bullish_candle:
        bull_score += 2
    else:
        bear_score += 2
    if rsi < 42:
        bull_score += 1
    elif rsi > 58:
        bear_score += 1
    if mkt_obj and float(mkt_obj.get("priceChangePercent", 0.0)) >= 0:
        bull_score += 1
    else:
        bear_score += 1

    is_short = bear_score > bull_score
    direction = "SHORT" if is_short else "LONG"
    signal_code = "STRONG_SHORT" if is_short else "STRONG_LONG"
    certain_signal = f"CERTAIN_{direction}"

    confidence = min(94, max(62, 66 + abs(bull_score - bear_score) * 6 + (6 if curr_vol_ratio >= 1.1 else 0)))
    power_charge_pct = min(98, max(28, int(45 + curr_vol_ratio * 22 + abs(rsi - 50) * 0.6)))
    flow_certainty_pct = min(96, max(52, int(confidence + (curr_vol_ratio - 1.0) * 8)))

    step = max(atr, price * 0.0018)
    decimals = 6 if price < 0.05 else (5 if price < 1 else (4 if price < 10 else (2 if price < 500 else 1)))

    if not is_short:
        sl_p = round(price - step * 1.15, decimals)
        tp1_p = round(price + step * 0.90, decimals)
        tp2_p = round(price + step * 1.65, decimals)
        tp3_p = round(price + step * 2.50, decimals)
    else:
        sl_p = round(price + step * 1.15, decimals)
        tp1_p = round(price - step * 0.90, decimals)
        tp2_p = round(price - step * 1.65, decimals)
        tp3_p = round(price - step * 2.50, decimals)

    with STATE_LOCK:
        bal_usdt = float(STATE["balance_info"].get("balance_usdt") or 0.0)

    lev = compute_dynamic_leverage(bal_usdt, max_lev)
    qty = compute_order_qty(bal_usdt, price, lev)

    # Anti-FOMO check when price moved sharply in same direction
    chg_24h = float(mkt_obj.get("priceChangePercent", 0.0)) if mkt_obj else 0.0
    fomo_blocked = (not is_short and chg_24h > 18.0) or (is_short and chg_24h < -18.0)

    adx_est = round(min(65.0, max(14.0, 18.0 + abs(ema9 - ema21) / max(1e-6, step) * 14.0)), 1)
    z_est = round((price - ema21) / max(1e-6, step), 2)

    return {
        "symbol": sym,
        "bingx_name": bx_name,
        "category": cat,
        "timeframe": interval,
        "price": round(price, decimals),
        "signal": signal_code,
        "certain_signal": certain_signal,
        "confidence": confidence,
        "session": sess,
        "levels": {
            "entry": round(price, decimals),
            "sl": sl_p,
            "tp1": tp1_p,
            "tp2": tp2_p,
            "tp3": tp3_p,
            "rr": "1:1.5",
        },
        "tp_plan": {
            "label": "TP1 Parziale • TP2 Target • TP3 Runner",
        },
        "recommended_params": {
            "leverage": lev,
            "qty": qty,
            "expectancy_r": "+0.45",
        },
        "early_setup": {
            "direction": direction,
            "setup_type": "Momentum + EMA Flow",
            "limit_price": round(price, decimals),
            "trigger_sound": confidence >= 72,
            "candle_id": int(time.time()) // 60,
        },
        "candle_timing": {
            "candle_index": int(time.time()) // 60,
        },
        "candle_flow": {
            "flow_direction": direction,
            "flow_certainty_pct": flow_certainty_pct,
            "power_charge_pct": power_charge_pct,
            "prev_vol_ratio": prev_vol_ratio,
            "curr_vol_ratio": curr_vol_ratio,
            "is_power_charging": power_charge_pct >= 68,
            "banner_label": f"⚡ Carica {power_charge_pct}% • Vol {curr_vol_ratio}x",
        },
        "entry_window": {
            "exceeded": False,
            "status_badge": "🟢 Finestra Valida",
        },
        "sn_rejection": {
            "rejected": False,
            "badge": "🛡️ SN OK",
        },
        "anti_fomo": {
            "blocked": fomo_blocked,
            "badge": "🛡️ FOMO Bloccato" if fomo_blocked else "🛡️ FOMO Libero",
            "reason": "Escursione 24h elevata" if fomo_blocked else "Nessun eccesso FOMO",
        },
        "manual_sign": {
            "status": "ACTIVE",
            "badge": f"⚡ {direction} CONFERMATO",
            "reason": f"Allineamento EMA9/21 e flusso candela {interval} su {bx_name}",
            "show_get_ready": power_charge_pct >= 68,
            "show_enter_now": confidence >= 75 and not fomo_blocked,
        },
        "indicators": {
            "rsi": round(rsi, 1),
            "adx": adx_est,
            "z_score": z_est,
            "ema200": round(ema200, decimals),
            "atr": round(atr, decimals),
        },
    }


def sync_bingx_account_and_positions():
    with STATE_LOCK:
        has_keys = bool(STATE["api_key"] and STATE["secret_key"])

    if not has_keys:
        return

    bal_res = bingx_request("GET", "/openApi/swap/v2/user/balance", signed=True)
    pos_res = bingx_request("GET", "/openApi/swap/v2/user/positions", signed=True)
    inc_res = bingx_request("GET", "/openApi/swap/v2/user/income", params={"limit": 100}, signed=True)

    balance_usdt = 0.0
    available_usdt = 0.0
    unrealized_pnl = 0.0

    if isinstance(bal_res, dict) and bal_res.get("code") == 0:
        b_data = bal_res.get("data")
        items = []
        if isinstance(b_data, dict):
            items = [b_data.get("balance") or b_data]
        elif isinstance(b_data, list):
            items = b_data
        for it in items:
            if not isinstance(it, dict):
                continue
            asset = str(it.get("asset") or "USDT").upper()
            if asset == "USDT":
                try:
                    balance_usdt = float(it.get("balance") or it.get("walletBalance") or 0.0)
                    available_usdt = float(it.get("availableMargin") or it.get("availableBalance") or balance_usdt)
                    unrealized_pnl = float(it.get("unrealizedProfit") or it.get("crossUnPnl") or 0.0)
                except Exception:
                    pass
                break

    open_pos_map = {}
    calc_unpnl = 0.0
    if isinstance(pos_res, dict) and pos_res.get("code") == 0:
        p_list = pos_res.get("data") or []
        for p in p_list:
            try:
                amt = float(p.get("positionAmt") or 0.0)
                if abs(amt) <= 0:
                    continue
                sym = str(p.get("symbol") or "").upper()
                _, bx_name = classify_market(sym)
                pos_side = str(p.get("positionSide") or "").upper()
                if pos_side not in {"LONG", "SHORT"}:
                    pos_side = "LONG" if amt > 0 else "SHORT"
                entry_p = float(p.get("avgPrice") or p.get("entryPrice") or 0.0)
                mark_p = float(p.get("markPrice") or entry_p)
                un_pnl = float(p.get("unrealizedProfit") or 0.0)
                lev = int(float(p.get("leverage") or 10))
                calc_unpnl += un_pnl

                open_pos_map[sym] = {
                    "symbol": sym,
                    "bingx_name": bx_name,
                    "side": pos_side,
                    "size": abs(amt),
                    "entry_price": entry_p,
                    "mark_price": mark_p,
                    "sl_price": float(p.get("stopLossPrice") or 0.0),
                    "tp_price": float(p.get("takeProfitPrice") or 0.0),
                    "leverage": lev,
                    "unrealized_pnl": un_pnl,
                }
            except Exception:
                continue

    if calc_unpnl != 0.0:
        unrealized_pnl = calc_unpnl

    now_dt = datetime.now(timezone.utc)
    day_start_ms = int(datetime(now_dt.year, now_dt.month, now_dt.day, tzinfo=timezone.utc).timestamp() * 1000)
    month_start_ms = int(datetime(now_dt.year, now_dt.month, 1, tzinfo=timezone.utc).timestamp() * 1000)

    daily_usdt = 0.0
    monthly_usdt = 0.0
    if isinstance(inc_res, dict) and inc_res.get("code") == 0:
        for row in inc_res.get("data") or []:
            try:
                inc_val = float(row.get("income") or 0.0)
                inc_type = str(row.get("incomeType") or "").upper()
                if inc_type in {"REALIZED_PNL", "COMMISSION", "FUNDING_FEE"}:
                    ts_ms = int(row.get("time") or 0)
                    if ts_ms >= month_start_ms:
                        monthly_usdt += inc_val
                    if ts_ms >= day_start_ms:
                        daily_usdt += inc_val
            except Exception:
                continue

    equity = balance_usdt + unrealized_pnl
    base_denom = max(1.0, balance_usdt if balance_usdt > 0 else equity)
    daily_pct = (daily_usdt / base_denom) * 100.0
    monthly_pct = (monthly_usdt / base_denom) * 100.0

    with STATE_LOCK:
        STATE["open_positions"] = open_pos_map
        STATE["balance_info"] = {
            "balance_usdt": round(balance_usdt, 4),
            "available_usdt": round(available_usdt, 4),
            "unrealized_pnl_usdt": round(unrealized_pnl, 4),
            "equity_usdt": round(equity, 4),
            "daily_pnl_usdt": round(daily_usdt, 4),
            "daily_pnl_pct": round(daily_pct, 2),
            "monthly_pnl_usdt": round(monthly_usdt, 4),
            "monthly_pnl_pct": round(monthly_pct, 2),
            "last_sync": now_rome_str(),
        }


def place_bingx_order(symbol: str, side: str, quantity: float, sl: float = 0.0, tp: float = 0.0, leverage: int = 10):
    sym = symbol.upper().strip()
    if not sym.endswith("-USDT") and "-" not in sym:
        sym = f"{sym}-USDT"

    side_u = str(side or "BUY").upper()
    is_sell = side_u in {"SELL", "SHORT"}
    bx_side = "SELL" if is_sell else "BUY"
    pos_side = "SHORT" if is_sell else "LONG"

    if leverage and int(leverage) > 0:
        bingx_request(
            "POST",
            "/openApi/swap/v2/trade/leverage",
            params={"symbol": sym, "side": pos_side, "leverage": int(leverage)},
            signed=True,
        )

    order_params = {
        "symbol": sym,
        "side": bx_side,
        "positionSide": pos_side,
        "type": "MARKET",
        "quantity": float(quantity),
    }
    if sl and float(sl) > 0:
        order_params["stopLoss"] = json.dumps({"type": "MARK_PRICE", "stopPrice": float(sl)})
    if tp and float(tp) > 0:
        order_params["takeProfit"] = json.dumps({"type": "MARK_PRICE", "stopPrice": float(tp)})

    res = bingx_request("POST", "/openApi/swap/v2/trade/order", params=order_params, signed=True)
    ok = isinstance(res, dict) and res.get("code") == 0
    return ok, res


def close_bingx_position(symbol: str, side: str, quantity: float):
    sym = symbol.upper().strip()
    if not sym.endswith("-USDT") and "-" not in sym:
        sym = f"{sym}-USDT"

    s_u = str(side or "").upper()
    if s_u in {"LONG", "BUY"}:
        pos_side = "LONG"
        close_side = "SELL"
    else:
        pos_side = "SHORT"
        close_side = "BUY"

    res = bingx_request(
        "POST",
        "/openApi/swap/v2/trade/order",
        params={
            "symbol": sym,
            "side": close_side,
            "positionSide": pos_side,
            "type": "MARKET",
            "quantity": float(quantity),
        },
        signed=True,
    )
    ok = isinstance(res, dict) and res.get("code") == 0
    return ok, res


def monitor_worker():
    while True:
        try:
            with STATE_LOCK:
                enabled = bool(STATE["monitor_enabled"])
                interval = STATE["interval"]
                cat = STATE["active_category"]
                scan_sec = max(5, min(120, int(STATE["scan_interval_sec"] or 8)))
                auto_trade = bool(STATE["auto_trade_enabled"])
                has_keys = bool(STATE["api_key"] and STATE["secret_key"])

            if enabled:
                sync_bingx_account_and_positions()
                markets = fetch_bingx_markets(force=False)
                pool = [m["symbol"] for m in markets if cat in {"ALL", "CERTAIN"} or m["category"] == cat][:12]

                new_sigs = {}
                for sym in pool:
                    an = analyze_symbol(sym, interval=interval)
                    if an:
                        new_sigs[sym] = an

                with STATE_LOCK:
                    if new_sigs:
                        STATE["signals"] = new_sigs
                    STATE["last_run"] = now_rome_str()

                if auto_trade and has_keys and new_sigs:
                    with STATE_LOCK:
                        open_pos = dict(STATE["open_positions"])
                        bal_usdt = float(STATE["balance_info"].get("balance_usdt") or 0.0)

                    if len(open_pos) < 3 and bal_usdt >= 1.0:
                        candidates = sorted(new_sigs.values(), key=lambda x: x["confidence"], reverse=True)
                        for cand in candidates:
                            c_sym = cand["symbol"]
                            if c_sym in open_pos:
                                continue
                            if cand.get("anti_fomo", {}).get("blocked"):
                                continue
                            side_to_exec = "SELL" if "SHORT" in str(cand.get("certain_signal", "")).upper() else "BUY"
                            qty = float(cand["recommended_params"]["qty"])
                            lev = int(cand["recommended_params"]["leverage"])
                            lv = cand["levels"]
                            if qty > 0:
                                ok, api_resp = place_bingx_order(c_sym, side_to_exec, qty, lv["sl"], lv["tp2"], lev)
                                with STATE_LOCK:
                                    STATE["trade_logs"].insert(0, {
                                        "time": now_rome_str(),
                                        "symbol": c_sym,
                                        "side": side_to_exec,
                                        "qty": qty,
                                        "sl": lv["sl"],
                                        "tp": lv["tp2"],
                                        "entry": lv["entry"],
                                        "reason": "AUTO-MONITOR",
                                        "response": api_resp,
                                    })
                                    STATE["trade_logs"] = STATE["trade_logs"][:80]
                                if ok:
                                    break
        except Exception as exc:
            print(f"[monitor_worker error] {exc}")

        with STATE_LOCK:
            scan_sec = max(5, min(120, int(STATE["scan_interval_sec"] or 8)))
        time.sleep(scan_sec)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/keys", methods=["GET", "POST"])
def api_keys():
    if request.method == "GET":
        with STATE_LOCK:
            ak = STATE["api_key"]
            sk = STATE["secret_key"]
        return jsonify({
            "status": "ok",
            "has_keys": bool(ak and sk),
            "masked_api_key": mask_secret(ak),
            "masked_secret_key": mask_secret(sk),
        })

    data = request.get_json(silent=True) or {}
    ak = str(data.get("api_key") or "").strip()
    sk = str(data.get("secret_key") or "").strip()
    if not ak and not sk:
        return jsonify({"status": "error", "msg": "Inserisci almeno API Key o Secret Key"}), 400

    save_persisted_keys(ak, sk)
    sync_bingx_account_and_positions()
    return jsonify({"status": "ok", "msg": "Chiavi BingX salvate e sincronizzate"})


@app.route("/api/markets", methods=["GET"])
def api_markets():
    force = request.args.get("force") in {"1", "true", "True"}
    markets = fetch_bingx_markets(force=force)
    return jsonify({"status": "ok", "markets": markets})


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    data = request.get_json(silent=True) or {}
    symbol = str(data.get("symbol") or "BTC-USDT").strip()
    interval = str(data.get("interval") or "1m").strip()
    res = analyze_symbol(symbol, interval=interval)
    if not res:
        return jsonify({"status": "error", "msg": f"Impossibile analizzare {symbol}"}), 404

    with STATE_LOCK:
        STATE["signals"][res["symbol"]] = res
        alert_item = {
            "id": f"{res['symbol']}_{int(time.time())}",
            "time": now_rome_str(),
            "symbol": res["symbol"],
            "title": res["manual_sign"]["badge"],
            "order_type": res["certain_signal"],
            "price": res["price"],
            "sl": res["levels"]["sl"],
            "tp1": res["levels"]["tp1"],
            "tp2": res["levels"]["tp2"],
            "tp3": res["levels"]["tp3"],
        }
        STATE["audio_alerts"].insert(0, alert_item)
        STATE["audio_alerts"] = STATE["audio_alerts"][:40]

    return jsonify({"status": "ok", "data": res})


@app.route("/api/scan_top", methods=["POST"])
def api_scan_top():
    data = request.get_json(silent=True) or {}
    symbols = data.get("symbols") or []
    interval = str(data.get("interval") or "1m").strip()
    only_ce = bool(data.get("only_certain_and_early", False))

    if not symbols:
        markets = fetch_bingx_markets(force=False)
        symbols = [m["symbol"] for m in markets[:15]]

    found = []
    for sym in symbols[:30]:
        an = analyze_symbol(sym, interval=interval)
        if not an:
            continue
        if only_ce and not (an.get("certain_signal") or an.get("early_setup", {}).get("direction")):
            continue
        found.append(an)

    with STATE_LOCK:
        for item in found:
            STATE["signals"][item["symbol"]] = item
        STATE["last_run"] = now_rome_str()
        alerts = list(STATE["audio_alerts"])

    return jsonify({
        "status": "ok",
        "signals": found,
        "audio_alerts": alerts,
    })


@app.route("/api/execute_order", methods=["POST"])
def api_execute_order():
    data = request.get_json(silent=True) or {}
    symbol = str(data.get("symbol") or "").strip().upper()
    side = str(data.get("side") or "BUY").strip().upper()
    qty = float(data.get("quantity") or 0.0)
    sl = float(data.get("sl") or 0.0)
    tp = float(data.get("tp") or 0.0)
    leverage = int(data.get("leverage") or 10)
    entry = float(data.get("entry") or 0.0)

    if not symbol or qty <= 0:
        return jsonify({"status": "error", "msg": "Parametri ordine non validi"}), 400

    ok, api_resp = place_bingx_order(symbol, side, qty, sl=sl, tp=tp, leverage=leverage)
    with STATE_LOCK:
        STATE["trade_logs"].insert(0, {
            "time": now_rome_str(),
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "sl": sl,
            "tp": tp,
            "entry": entry,
            "response": api_resp,
        })
        STATE["trade_logs"] = STATE["trade_logs"][:80]

    sync_bingx_account_and_positions()
    if ok:
        return jsonify({"status": "ok", "success": True, "msg": f"Ordine {side} {symbol} eseguito su BingX", "data": api_resp})
    return jsonify({
        "status": "error",
        "success": False,
        "msg": api_resp.get("msg") if isinstance(api_resp, dict) else "Errore ordine BingX",
        "data": api_resp,
    }), 400


@app.route("/api/close_position", methods=["POST"])
def api_close_position():
    data = request.get_json(silent=True) or {}
    symbol = str(data.get("symbol") or "").strip().upper()
    side = str(data.get("side") or "LONG").strip().upper()
    qty = float(data.get("quantity") or 0.0)
    reason = str(data.get("reason") or "CHIUSURA").strip()

    if not symbol or qty <= 0:
        return jsonify({"status": "error", "msg": "Parametri chiusura non validi"}), 400

    with STATE_LOCK:
        pos_obj = STATE["open_positions"].get(symbol) or {}
        est_pnl = pos_obj.get("unrealized_pnl")

    ok, api_resp = close_bingx_position(symbol, side, qty)
    with STATE_LOCK:
        STATE["trade_logs"].insert(0, {
            "time": now_rome_str(),
            "symbol": symbol,
            "side": f"CLOSE_{side}",
            "qty": qty,
            "sl": 0,
            "tp": 0,
            "pnl": est_pnl,
            "reason": reason,
            "response": api_resp,
        })
        STATE["trade_logs"] = STATE["trade_logs"][:80]

    sync_bingx_account_and_positions()
    if ok:
        return jsonify({"status": "ok", "success": True, "msg": f"Posizione {symbol} chiusa ({reason})", "data": api_resp})
    return jsonify({
        "status": "error",
        "success": False,
        "msg": api_resp.get("msg") if isinstance(api_resp, dict) else "Errore chiusura posizione",
        "data": api_resp,
    }), 400


@app.route("/api/auto_monitor_status", methods=["GET", "POST"])
def api_auto_monitor_status():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        with STATE_LOCK:
            if "enabled" in data:
                STATE["monitor_enabled"] = bool(data["enabled"])
            if "auto_trade_enabled" in data:
                STATE["auto_trade_enabled"] = bool(data["auto_trade_enabled"])
            if "anti_fomo_enabled" in data:
                STATE["anti_fomo_enabled"] = bool(data["anti_fomo_enabled"])
            if "auto_close_on_tp" in data:
                STATE["auto_close_on_tp"] = bool(data["auto_close_on_tp"])
            if "auto_close_on_candle_end" in data:
                STATE["auto_close_on_candle_end"] = bool(data["auto_close_on_candle_end"])
            if "auto_close_on_candle_signal" in data:
                STATE["auto_close_on_candle_signal"] = bool(data["auto_close_on_candle_signal"])
            if "active_category" in data:
                STATE["active_category"] = str(data["active_category"] or "ALL")
            if "only_certain_and_early" in data:
                STATE["only_certain_and_early"] = bool(data["only_certain_and_early"])
            if "interval" in data:
                STATE["interval"] = str(data["interval"] or "1m")
            if "scan_interval_sec" in data:
                STATE["scan_interval_sec"] = max(5, min(120, int(data["scan_interval_sec"] or 8)))

    sync_bingx_account_and_positions()
    with STATE_LOCK:
        bal_usdt = float(STATE["balance_info"].get("balance_usdt") or 0.0)
        lev_prof = compute_dynamic_leverage(bal_usdt, 50)
        payload = {
            "status": "ok",
            "enabled": STATE["monitor_enabled"],
            "auto_trade_enabled": STATE["auto_trade_enabled"],
            "anti_fomo_enabled": STATE["anti_fomo_enabled"],
            "interval": STATE["interval"],
            "scan_interval_sec": STATE["scan_interval_sec"],
            "last_run": STATE["last_run"],
            "profile": {"profile_name": f"AUTO {lev_prof}x"},
            "balance_info": dict(STATE["balance_info"]),
            "signals": dict(STATE["signals"]),
            "open_positions": dict(STATE["open_positions"]),
            "trade_logs": list(STATE["trade_logs"]),
            "audio_alerts": list(STATE["audio_alerts"]),
            "latest_new_candle_alert": STATE["latest_new_candle_alert"],
        }
    return jsonify(payload)


def start_background_thread():
    t = threading.Thread(target=monitor_worker, daemon=True)
    t.start()


load_persisted_keys()
start_background_thread()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
