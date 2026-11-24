import os
import time
import math
import hmac
import hashlib
from urllib.parse import urlencode
import requests
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

API_KEY = os.getenv("BINGX_API_KEY", "")
API_SECRET = os.getenv("BINGX_SECRET_KEY", "")
BASE_URL = "https://open-api.bingx.com"

TF_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
}

AUTO_CLOSE_WATCHLIST = {}


def sign_params(params: dict) -> str:
    query = urlencode(sorted(params.items()))
    return hmac.new(
        API_SECRET.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()


def bingx_req(method: str, path: str, params: dict = None, signed: bool = False):
    params = dict(params or {})
    headers = {"X-BX-APIKEY": API_KEY}
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        sig = sign_params(params)
        query = urlencode(sorted(params.items()))
        url = f"{BASE_URL}{path}?{query}&signature={sig}"
    else:
        query = urlencode(sorted(params.items()))
        url = f"{BASE_URL}{path}" + (f"?{query}" if query else "")

    try:
        r = requests.request(method, url, headers=headers, timeout=10)
        return r.json()
    except Exception as e:
        return {"code": -1, "msg": str(e)}


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
        h = float(candles[i].get("high", 0) or 0)
        l = float(candles[i].get("low", 0) or 0)
        prev_c = float(candles[i - 1].get("close", h) or h)
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
    use_trs = trs[-period:] if len(trs) >= period else trs
    return sum(use_trs) / len(use_trs) if use_trs else 0.0


def calc_adx_and_zscore(candles: list, closes: list, period: int = 14):
    """Calcola ADX (forza del trend) e Z-Score statistico sul close."""
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
        h = float(candles[i].get("high", 0) or 0)
        l = float(candles[i].get("low", 0) or 0)
        prev_h = float(candles[i - 1].get("high", h) or h)
        prev_l = float(candles[i - 1].get("low", l) or l)
        prev_c = float(candles[i - 1].get("close", h) or h)

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


def compute_quant_brain(symbol: str, tf: str, side: str = "BUY"):
    """Cervello matematico: Score quantitativo, TP/SL con R:R >= 1.8, Expectancy e tempo stimato al TP."""
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
            c = float(k.get("close", 0) or 0)
            v = float(k.get("volume", 0) or 0)
            if c > 0:
                closes.append(c)
                volumes.append(v)
        except Exception:
            continue

    if not closes:
        return {
            "score": 0,
            "signal": "NEUTRAL",
            "win_prob": 0.50,
            "rr_ratio": 1.80,
            "expectancy_r": 0.0,
            "profit_ready": False,
            "price": 0.0,
            "tp": 0.0,
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
        abs(float(k.get("high", price)) - float(k.get("low", price)))
        for k in raw[-21:-1]
    ]
    avg_range = sum(ranges) / len(ranges) if ranges else (atr_val or price * 0.004)
    avg_vol = (sum(volumes[-21:-1]) / max(len(volumes[-21:-1]), 1)) if len(volumes) > 2 else (volumes[-1] or 1.0)
    vol_ratio = round(volumes[-1] / avg_vol, 2) if avg_vol > 0 else 1.0

    # 1. Trend & EMA Score (-100..+100)
    ema_pts = 0.0
    if ema9 is not None and ema21 is not None:
        ema_pts += 35.0 if ema9 > ema21 else -35.0
        ema_pts += 20.0 if price > ema9 else -20.0
    if ema50 is not None:
        ema_pts += 25.0 if price > ema50 else -25.0
    if ema200 is not None:
        ema_pts += 20.0 if price > ema200 else -20.0
    ema_pts = max(-100.0, min(100.0, ema_pts))

    # 2. Momentum Score (MACD + RSI)
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

    # 3. Fattore statistico Z-Score + forza ADX
    adx_mult = min(1.25, max(0.65, adx_val / 25.0))
    z_penalty = -20.0 if abs(z_score) > 2.2 else 0.0

    raw_score = ((ema_pts * 0.45) + (macd_pts * 0.30) + (rsi_pts * 0.25)) * adx_mult + z_penalty
    score = int(round(max(-100, min(100, raw_score))))

    # Soglia alta precisione: richiede |score| >= 40 e ADX >= 18 per segnale direzionale
    if score >= 40 and adx_val >= 18:
        signal = "BUY"
    elif score <= -40 and adx_val >= 18:
        signal = "SELL"
    else:
        signal = "NEUTRAL"

    # Probabilita' stimata e R:R orientato al profitto (minimo 1.85R)
    target_side = side.upper() if side.upper() in ("BUY", "SELL") else ("SELL" if score < 0 else "BUY")
    aligned = (target_side == "BUY" and score > 0) or (target_side == "SELL" and score < 0)
    conf_strength = min(0.95, max(0.0, abs(score) / 100.0))
    win_prob = round(min(0.74, max(0.38, (0.48 + 0.22 * conf_strength) if aligned else (0.46 - 0.10 * conf_strength))), 2)

    # SL basato su ATR (1.1 * ATR), TP basato su R:R >= 1.85 (2.05 * ATR)
    unit_vol = atr_val if atr_val > 0 else max(avg_range, price * 0.002)
    sl_dist = max(unit_vol * 1.10, price * 0.0012)
    rr_ratio = 2.00 if abs(score) >= 55 else 1.85
    tp_dist = sl_dist * rr_ratio

    if target_side == "SELL":
        tp_price = price - tp_dist
        sl_price = price + sl_dist
    else:
        tp_price = price + tp_dist
        sl_price = price - sl_dist

    # Expectancy in unita' di rischio (R): E = p * RR - (1 - p) * 1
    expectancy_r = round((win_prob * rr_ratio) - ((1.0 - win_prob) * 1.0), 2)
    profit_ready = (expectancy_r > 0.15) and (adx_val >= 18) and (abs(z_score) <= 2.3)

    tf_sec = TF_SECONDS.get(tf, 300)
    speed_per_sec = max(avg_range / tf_sec, 1e-9)
    eta_tp_sec = int(min(tp_dist / speed_per_sec, tf_sec * 5))

    def fmt_num(val):
        if val is None:
            return None
        if abs(val) >= 100:
            return round(val, 2)
        if abs(val) >= 1:
            return round(val, 4)
        return round(val, 7)

    tp_pct = round((tp_dist / price) * 100, 2) if price > 0 else 0.0
    sl_pct = round((sl_dist / price) * 100, 2) if price > 0 else 0.0

    return {
        "score": score,
        "signal": signal,
        "win_prob": win_prob,
        "rr_ratio": rr_ratio,
        "expectancy_r": expectancy_r,
        "profit_ready": profit_ready,
        "price": fmt_num(price),
        "tp": fmt_num(tp_price),
        "sl": fmt_num(sl_price),
        "tp_pct": tp_pct,
        "sl_pct": sl_pct,
        "eta_tp_sec": eta_tp_sec,
        "avg_range": fmt_num(avg_range),
        "adx": adx_val,
        "z_score": z_score,
        "ema9": fmt_num(ema9),
        "ema21": fmt_num(ema21),
        "ema50": fmt_num(ema50),
        "ema200": fmt_num(ema200),
        "rsi14": rsi14,
        "macd_hist": fmt_num(hist_val),
        "atr": fmt_num(atr_val),
        "vol_ratio": vol_ratio
    }


def categorize_symbol(symbol: str) -> str:
    s = symbol.upper()
    commodities = ("XAU", "XAG", "GOLD", "SILVER", "OIL", "WTI", "BRENT", "COPPER", "NATGAS", "CORN", "WHEAT")
    stocks = ("AAPL", "TSLA", "NVDA", "MSFT", "AMZN", "META", "GOOGL", "COIN", "MSTR", "SPX", "NDX", "US30", "NAS100")
    memes = ("PEPE", "SHIB", "DOGE", "FLOKI", "BONK", "WIF", "MEME", "BOME", "TURBO", "1000PEPE", "1000BONK", "1000SATS", "PUSSY")

    if any(k in s for k in commodities):
        return "Materie Prime"
    if any(k in s for k in stocks):
        return "Azioni & Indici"
    if any(k in s for k in memes):
        return "Meme & Altcoin"
    return "Crypto Major"


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


def check_anti_fakeout_and_breakout(symbol: str, tf: str, side: str, anti_fakeout: bool, anti_breakout: bool, quant: dict = None):
    if quant and not quant.get("profit_ready", True):
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
    o = float(last_closed.get("open", 0))
    h = float(last_closed.get("high", 0))
    l = float(last_closed.get("low", 0))
    c = float(last_closed.get("close", 0))
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
            best_ask = float(asks[0][0])
            best_bid = float(bids[0][0])
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
    return closed_log


def calc_dynamic_leverage(equity: float) -> int:
    if equity <= 2.00:
        return 20
    if equity <= 50.00:
        return 15
    if equity <= 500.00:
        return 10
    return 5


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/api/markets")
def api_markets():
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
                "lastPrice": float(tk.get("lastPrice", 0) or 0),
                "priceChangePercent": float(tk.get("priceChangePercent", 0) or 0),
                "volume": float(tk.get("volume", 0) or 0),
            })

    out.sort(key=lambda x: x["volume"], reverse=True)
    return jsonify({"code": 0, "count": len(out), "markets": out})


@app.route("/api/tf-plan")
def api_tf_plan():
    symbol = request.args.get("symbol", "BTC-USDT")
    tf = request.args.get("tf", "5m")
    side = request.args.get("side", "BUY").upper()
    timing = candle_timing_info(tf)
    quant = compute_quant_brain(symbol, tf, side)
    return jsonify({"code": 0, "timing": timing, "quant": quant})


@app.route("/api/live-ticker")
def api_live_ticker():
    symbol = request.args.get("symbol", "BTC-USDT")
    tf = request.args.get("tf", "5m")
    auto_closed = process_auto_close_expired()

    ticker = bingx_req("GET", "/openApi/swap/v2/quote/ticker", {"symbol": symbol})
    depth = bingx_req("GET", "/openApi/swap/v2/quote/depth", {"symbol": symbol, "limit": 10})
    funding = bingx_req("GET", "/openApi/swap/v2/quote/premiumIndex", {"symbol": symbol})
    quant = compute_quant_brain(symbol, tf)

    return jsonify({
        "ticker": ticker.get("data", {}),
        "depth": depth.get("data", {}),
        "funding": funding.get("data", {}),
        "quant": quant,
        "timing": candle_timing_info(tf),
        "auto_closed": auto_closed,
        "active_auto_closes": list(AUTO_CLOSE_WATCHLIST.values())
    })


@app.route("/api/account")
def api_account():
    balance = bingx_req("GET", "/openApi/swap/v2/user/balance", signed=True)
    positions = bingx_req("GET", "/openApi/swap/v2/user/positions", signed=True)
    income = bingx_req("GET", "/openApi/swap/v2/user/income", {"limit": 20}, signed=True)

    equity = 0.0
    available = 0.0
    if balance.get("code") == 0 and isinstance(balance.get("data"), dict):
        b = balance["data"].get("balance", {})
        equity = float(b.get("equity", 0) or 0)
        available = float(b.get("availableMargin", 0) or 0)

    return jsonify({
        "equity": equity,
        "available": available,
        "recommended_leverage": calc_dynamic_leverage(equity),
        "positions": positions.get("data", []) if positions.get("code") == 0 else [],
        "income_history": income.get("data", []) if income.get("code") == 0 else []
    })


@app.route("/api/order", methods=["POST"])
def api_order():
    data = request.get_json(force=True) or {}
    symbol = data.get("symbol", "BTC-USDT")
    side = str(data.get("side", "NEUTRAL")).upper()
    order_type = str(data.get("type", "MARKET")).upper()
    tf = str(data.get("tf", "5m"))
    qty = float(data.get("quantity", 0) or 0)
    leverage = int(data.get("leverage", 20) or 20)
    price = data.get("price")
    tp = data.get("tp")
    sl = data.get("sl")
    close_at_candle_end = bool(data.get("close_at_candle_end", False))
    anti_fakeout = bool(data.get("anti_fakeout", True))
    anti_breakout = bool(data.get("anti_breakout", True))

    if side not in ("BUY", "SELL"):
        return jsonify({"code": -1, "msg": "Stato NEUTRAL: nessun ordine inviato"}), 400
    if qty <= 0:
        return jsonify({"code": -1, "msg": "Quantita' non valida"}), 400

    quant = compute_quant_brain(symbol, tf, side)
    ok_filter, reason = check_anti_fakeout_and_breakout(symbol, tf, side, anti_fakeout, anti_breakout, quant)
    if not ok_filter:
        return jsonify({"code": -10, "msg": reason, "status": "NEUTRAL"}), 400

    pos_side = "LONG" if side == "BUY" else "SHORT"

    bingx_req("POST", "/openApi/swap/v2/trade/leverage", {
        "symbol": symbol,
        "side": pos_side,
        "leverage": leverage
    }, signed=True)

    order_params = {
        "symbol": symbol,
        "side": side,
        "positionSide": pos_side,
        "type": order_type,
        "quantity": qty
    }
    if order_type == "LIMIT" and price:
        order_params["price"] = float(price)
    if tp:
        order_params["takeProfit"] = f'{{"type":"TAKE_PROFIT_MARKET","stopPrice":{float(tp)},"price":{float(tp)},"workingType":"MARK_PRICE"}}'
    if sl:
        order_params["stopLoss"] = f'{{"type":"STOP_MARKET","stopPrice":{float(sl)},"price":{float(sl)},"workingType":"MARK_PRICE"}}'

    res = bingx_req("POST", "/openApi/swap/v2/trade/order", order_params, signed=True)

    if res.get("code") == 0 and close_at_candle_end:
        timing = candle_timing_info(tf)
        watch_key = f"{symbol}_{pos_side}_{timing['candle_end_ts']}"
        AUTO_CLOSE_WATCHLIST[watch_key] = {
            "symbol": symbol,
            "side": pos_side,
            "qty": qty,
            "tf": tf,
            "close_at": timing["candle_end_ts"]
        }

    return jsonify(res)


@app.route("/api/close-all", methods=["POST"])
def api_close_all():
    data = request.get_json(silent=True) or {}
    params = {"symbol": data["symbol"]} if data.get("symbol") else {}
    res = bingx_req("POST", "/openApi/swap/v2/trade/closeAllPositions", params, signed=True)
    return jsonify(res)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
