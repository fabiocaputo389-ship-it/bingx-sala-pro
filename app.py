import os
import time
import hmac
import hashlib
import urllib.parse
import requests
import numpy as np
from flask import Flask, render_template, jsonify, request
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

BINGX_API_KEY = os.getenv("BINGX_API_KEY", "")
BINGX_SECRET_KEY = os.getenv("BINGX_SECRET_KEY", "")
BASE_URL = "https://open-api.bingx.com"

# Pesi predefiniti del Cervello Matematico (modificabili live dalla dashboard)
BRAIN_WEIGHTS = {
    "ema_weight": 30,        # Peso allineamento EMA (9/21/50/200)
    "momentum_weight": 25,   # Peso RSI + MACD + ADX
    "volatility_weight": 20, # Peso Bollinger + Z-Score + R^2
    "sentiment_weight": 25,  # Peso Orderbook Imbalance + Funding Rate
    "min_confidence": 78     # Soglia minima (%) per emettere segnale STRONG BUY/SELL
}

# Cache contratti BingX
CONTRACTS_CACHE = {"timestamp": 0, "data": []}


def sign_request(params: dict) -> str:
    query_string = urllib.parse.urlencode(sorted(params.items()))
    signature = hmac.new(
        BINGX_SECRET_KEY.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    return f"{query_string}&signature={signature}"


def bingx_public_get(endpoint: str, params: dict = None):
    url = f"{BASE_URL}{endpoint}"
    try:
        resp = requests.get(url, params=params, timeout=8)
        data = resp.json()
        if data.get("code") == 0:
            return data.get("data")
    except Exception as e:
        print(f"[BingX API Error] {endpoint}: {e}")
    return None


def bingx_private_post(endpoint: str, params: dict):
    if not BINGX_API_KEY or not BINGX_SECRET_KEY:
        return {"code": -1, "msg": "API Key BingX non configurate nel file .env"}
    params["timestamp"] = int(time.time() * 1000)
    signed_qs = sign_request(params)
    url = f"{BASE_URL}{endpoint}?{signed_qs}"
    headers = {"X-BX-APIKEY": BINGX_API_KEY}
    try:
        resp = requests.post(url, headers=headers, timeout=8)
        return resp.json()
    except Exception as e:
        return {"code": -1, "msg": str(e)}


def categorize_symbol(symbol: str) -> str:
    s = symbol.upper()
    # Commodities / Metalli / Energia
    if any(k in s for k in ["XAU", "XAG", "GOLD", "SILVER", "OIL", "WTI", "BRENT", "NATGAS", "COPPER", "PLATINUM"]):
        return "METALLI & COMMODITIES"
    # Forex / Valute Fiat
    if any(k in s for k in ["EUR-", "GBP-", "JPY", "AUD-", "CAD-", "CHF-", "NZD-", "USDJPY", "EURUSD", "GBPUSD"]):
        return "FOREX & VALUTE"
    # Indici & Azioni
    if any(k in s for k in ["SPX", "NAS100", "NDX", "US30", "DJI", "DAX", "UK100", "NIKKEI", "TSLA", "NVDA", "AAPL"]):
        return "INDICI & AZIONI"
    # Crypto Major
    if any(s.startswith(k) for k in ["BTC-", "ETH-", "SOL-", "BNB-", "XRP-", "ADA-", "AVAX-", "DOGE-", "DOT-", "LINK-", "TON-", "SUI-"]):
        return "CRYPTO MAJOR"
    return "ALTCOIN & DEFI"


def get_all_bingx_contracts():
    now = time.time()
    if CONTRACTS_CACHE["data"] and (now - CONTRACTS_CACHE["timestamp"] < 300):
        return CONTRACTS_CACHE["data"]

    raw_contracts = bingx_public_get("/openApi/swap/v2/quote/contracts")
    tickers = bingx_public_get("/openApi/swap/v2/quote/ticker")
    ticker_map = {t["symbol"]: t for t in (tickers or [])}

    results = []
    if raw_contracts:
        for c in raw_contracts:
            if c.get("status") != 1:
                continue
            sym = c.get("symbol")
            tk = ticker_map.get(sym, {})
            vol_24h = float(tk.get("quoteVolume", 0) or 0)
            results.append({
                "symbol": sym,
                "category": categorize_symbol(sym),
                "maxLeverage": c.get("maxLeverage", 50),
                "pricePrecision": c.get("pricePrecision", 4),
                "lastPrice": float(tk.get("lastPrice", 0) or 0),
                "priceChangePercent": float(tk.get("priceChangePercent", 0) or 0),
                "volume24h": vol_24h,
                "high24h": float(tk.get("highPrice", 0) or 0),
                "low24h": float(tk.get("lowPrice", 0) or 0),
            })
        results.sort(key=lambda x: x["volume24h"], reverse=True)
        CONTRACTS_CACHE["data"] = results
        CONTRACTS_CACHE["timestamp"] = now
    return results


# ==============================================================================
# CERVELLO MATEMATICO QUANTITATIVO (Motore Istituzionale)
# ==============================================================================

def calc_ema(prices: np.ndarray, period: int) -> np.ndarray:
    alpha = 2.0 / (period + 1.0)
    ema = np.zeros_like(prices)
    ema[0] = prices[0]
    for i in range(1, len(prices)):
        ema[i] = alpha * prices[i] + (1.0 - alpha) * ema[i - 1]
    return ema


def calc_rsi(prices: np.ndarray, period: int = 14) -> float:
    if len(prices) < period + 1:
        return 50.0
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100.0 - (100.0 / (1.0 + rs)))


def calc_atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> float:
    if len(closes) < period + 1:
        return float(np.mean(highs - lows))
    tr = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(np.abs(highs[1:] - closes[:-1]), np.abs(lows[1:] - closes[:-1]))
    )
    atr = np.mean(tr[:period])
    for i in range(period, len(tr)):
        atr = (atr * (period - 1) + tr[i]) / period
    return float(atr)


def calc_adx(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> float:
    if len(closes) < period * 2:
        return 20.0
    up_move = highs[1:] - highs[:-1]
    down_move = lows[:-1] - lows[1:]
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(np.abs(highs[1:] - closes[:-1]), np.abs(lows[1:] - closes[:-1]))
    )
    atr_s = np.convolve(tr, np.ones(period)/period, mode='valid')
    p_di = 100 * (np.convolve(plus_dm, np.ones(period)/period, mode='valid') / (atr_s + 1e-9))
    m_di = 100 * (np.convolve(minus_dm, np.ones(period)/period, mode='valid') / (atr_s + 1e-9))
    dx = 100 * np.abs(p_di - m_di) / (p_di + m_di + 1e-9)
    return float(np.mean(dx[-period:]))


def analyze_symbol_math(symbol: str, interval: str = "15m", custom_sentiment: float = None):
    klines = bingx_public_get("/openApi/swap/v3/quote/klines", {
        "symbol": symbol,
        "interval": interval,
        "limit": 220
    })
    depth = bingx_public_get("/openApi/swap/v2/quote/depth", {"symbol": symbol, "limit": 20})
    premium = bingx_public_get("/openApi/swap/v2/quote/premiumIndex", {"symbol": symbol})

    if not klines or len(klines) < 50:
        return None

    # BingX klines: ordine cronologico
    klines = sorted(klines, key=lambda x: int(x["time"]))
    closes = np.array([float(k["close"]) for k in klines], dtype=float)
    highs = np.array([float(k["high"]) for k in klines], dtype=float)
    lows = np.array([float(k["low"]) for k in klines], dtype=float)
    volumes = np.array([float(k["volume"]) for k in klines], dtype=float)

    price = closes[-1]

    # 1. Sistema Multi-EMA (9, 21, 50, 200)
    ema9 = calc_ema(closes, 9)
    ema21 = calc_ema(closes, 21)
    ema50 = calc_ema(closes, 50)
    ema200 = calc_ema(closes, 200 if len(closes) >= 200 else len(closes) - 1)

    e9, e21, e50, e200 = ema9[-1], ema21[-1], ema50[-1], ema200[-1]
    ema_slope = ((ema21[-1] - ema21[-5]) / (ema21[-5] + 1e-9)) * 100

    # Punteggio direzionale EMA [-100, +100]
    ema_dir = 0
    if e9 > e21: ema_dir += 30
    else: ema_dir -= 30
    if e21 > e50: ema_dir += 30
    else: ema_dir -= 30
    if price > e200: ema_dir += 25
    else: ema_dir -= 25
    ema_dir += np.clip(ema_slope * 20, -15, 15)

    # 2. Momentum: RSI(14), MACD(12,26,9), ADX(14)
    rsi = calc_rsi(closes, 14)
    ema12 = calc_ema(closes, 12)
    ema26 = calc_ema(closes, 26)
    macd_line = ema12 - ema26
    signal_line = calc_ema(macd_line, 9)
    macd_hist = macd_line[-1] - signal_line[-1]
    adx = calc_adx(highs, lows, closes, 14)

    mom_dir = 0
    if 52 < rsi < 74: mom_dir += 40
    elif rsi >= 74: mom_dir -= 20  # Ipercomprato estremo
    elif 26 < rsi < 48: mom_dir -= 40
    elif rsi <= 26: mom_dir += 20  # Ipervenduto estremo

    if macd_hist > 0 and macd_line[-1] > signal_line[-1]:
        mom_dir += 40
    elif macd_hist < 0 and macd_line[-1] < signal_line[-1]:
        mom_dir -= 40

    adx_multiplier = 1.2 if adx >= 25 else 0.85
    mom_dir = np.clip(mom_dir * adx_multiplier, -100, 100)

    # 3. Volatilità, Bande di Bollinger, Z-Score & R^2 Regressione Lineare
    sma20 = np.mean(closes[-20:])
    std20 = np.std(closes[-20:]) + 1e-9
    bb_upper = sma20 + 2 * std20
    bb_lower = sma20 - 2 * std20
    bb_percent = ((price - bb_lower) / (bb_upper - bb_lower + 1e-9)) * 100
    z_score = (price - sma20) / std20

    # R^2 su ultime 20 candele
    x_idx = np.arange(20)
    corr_matrix = np.corrcoef(x_idx, closes[-20:])
    r_squared = float(corr_matrix[0, 1] ** 2) if not np.isnan(corr_matrix[0, 1]) else 0.0

    vol_dir = 0
    if 0.3 < z_score < 2.1: vol_dir += 50 * r_squared + 30
    elif -2.1 < z_score < -0.3: vol_dir -= 50 * r_squared + 30
    vol_dir = np.clip(vol_dir, -100, 100)

    # 4. Sentiment Reale BingX (Orderbook Imbalance + Funding Rate + Volume Surge)
    bid_vol = sum(float(b[1]) for b in (depth.get("bids", []) if depth else []))
    ask_vol = sum(float(a[1]) for a in (depth.get("asks", []) if depth else []))
    ob_imbalance = ((bid_vol - ask_vol) / (bid_vol + ask_vol + 1e-9)) * 100  # [-100, +100]

    funding_rate = float(premium.get("lastFundingRate", 0) or 0) * 100 if isinstance(premium, dict) else 0.0
    vol_ratio = volumes[-1] / (np.mean(volumes[-20:]) + 1e-9)

    calc_sentiment = np.clip(ob_imbalance * 0.7 + (20 if vol_ratio > 1.3 and closes[-1] > closes[-2] else (-20 if vol_ratio > 1.3 else 0)), -100, 100)
    final_sentiment = float(custom_sentiment) if custom_sentiment is not None else float(calc_sentiment)

    # 5. Sintesi Cervello Matematico (Score Pesato)
    w_ema = BRAIN_WEIGHTS["ema_weight"]
    w_mom = BRAIN_WEIGHTS["momentum_weight"]
    w_vol = BRAIN_WEIGHTS["volatility_weight"]
    w_sent = BRAIN_WEIGHTS["sentiment_weight"]
    total_w = max(w_ema + w_mom + w_vol + w_sent, 1)

    composite_score = (
        ema_dir * w_ema +
        mom_dir * w_mom +
        vol_dir * w_vol +
        final_sentiment * w_sent
    ) / total_w  # Range [-100, +100]

    # Calcolo Certezza Matematica (Confidenza 0-100%)
    # Combina intensità dello score direzionale, coerenza R^2 e forza trend ADX
    raw_conf = min(abs(composite_score) * 1.15 + (r_squared * 12) + (min(adx, 40) * 0.25), 99.4)
    confidence = round(float(raw_conf), 1)

    min_conf = BRAIN_WEIGHTS["min_confidence"]
    if composite_score > 18 and confidence >= min_conf:
        signal = "STRONG LONG"
    elif composite_score > 10:
        signal = "LONG"
    elif composite_score < -18 and confidence >= min_conf:
        signal = "STRONG SHORT"
    elif composite_score < -10:
        signal = "SHORT"
    else:
        signal = "NEUTRAL / WAIT"

    # 6. Livelli Matematici Esatti (Entry, SL, TP1, TP2, TP3) basati su ATR(14)
    atr = calc_atr(highs, lows, closes, 14)
    decimals = len(str(price).split(".")[1]) if "." in str(price) else 2
    decimals = min(max(decimals, 2), 6)

    if composite_score >= 0:
        entry = round(price, decimals)
        sl = round(price - 1.5 * atr, decimals)
        tp1 = round(price + 2.0 * atr, decimals)
        tp2 = round(price + 3.5 * atr, decimals)
        tp3 = round(price + 5.0 * atr, decimals)
    else:
        entry = round(price, decimals)
        sl = round(price + 1.5 * atr, decimals)
        tp1 = round(price - 2.0 * atr, decimals)
        tp2 = round(price - 3.5 * atr, decimals)
        tp3 = round(price - 5.0 * atr, decimals)

    rr_ratio = round(abs(tp2 - entry) / (abs(entry - sl) + 1e-9), 2)

    return {
        "symbol": symbol,
        "price": price,
        "signal": signal,
        "composite_score": round(float(composite_score), 1),
        "confidence": confidence,
        "sentiment": round(final_sentiment, 1),
        "indicators": {
            "ema9": round(float(e9), decimals),
            "ema21": round(float(e21), decimals),
            "ema50": round(float(e50), decimals),
            "ema200": round(float(e200), decimals),
            "rsi": round(rsi, 2),
            "macd_hist": round(float(macd_hist), decimals + 2),
            "adx": round(adx, 2),
            "bb_percent": round(float(bb_percent), 1),
            "z_score": round(float(z_score), 2),
            "r_squared": round(r_squared, 3),
            "atr": round(atr, decimals),
            "ob_imbalance": round(float(ob_imbalance), 1),
            "funding_rate": round(funding_rate, 4)
        },
        "levels": {
            "entry": entry,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "rr": rr_ratio
        }
    }


# ==============================================================================
# ROTTE API FLASK
# ==============================================================================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/markets")
def api_markets():
    contracts = get_all_bingx_contracts()
    return jsonify({"status": "ok", "count": len(contracts), "markets": contracts})


@app.route("/api/analyze", methods=["GET", "POST"])
def api_analyze():
    if request.method == "POST":
        data = request.json or {}
        symbol = data.get("symbol", "BTC-USDT")
        interval = data.get("interval", "15m")
        custom_sentiment = data.get("custom_sentiment", None)
    else:
        symbol = request.args.get("symbol", "BTC-USDT")
        interval = request.args.get("interval", "15m")
        custom_sentiment = None

    res = analyze_symbol_math(symbol, interval, custom_sentiment)
    if not res:
        return jsonify({"status": "error", "msg": "Dati insufficienti da BingX"}), 400
    return jsonify({"status": "ok", "data": res})


@app.route("/api/scan_top", methods=["POST"])
def api_scan_top():
    """Scansiona i mercati principali per categoria e restituisce i segnali live."""
    data = request.json or {}
    symbols = data.get("symbols", [])
    interval = data.get("interval", "15m")
    overrides = data.get("overrides", {})  # { "BTC-USDT": {"sentiment": 80, "score_offset": 10} }

    results = []
    for sym in symbols[:25]:
        ov = overrides.get(sym, {})
        custom_sent = ov.get("sentiment", None)
        analysis = analyze_symbol_math(sym, interval, custom_sent)
        if analysis:
            # Applica eventuale override manuale allo score
            if "score_override" in ov and ov["score_override"] is not None:
                analysis["composite_score"] = float(ov["score_override"])
                analysis["confidence"] = min(round(abs(analysis["composite_score"]) * 1.15, 1), 99.9)
            results.append(analysis)
    return jsonify({"status": "ok", "signals": results})


@app.route("/api/weights", methods=["GET", "POST"])
def api_weights():
    if request.method == "POST":
        data = request.json or {}
        for k in BRAIN_WEIGHTS:
            if k in data:
                BRAIN_WEIGHTS[k] = float(data[k])
    return jsonify({"status": "ok", "weights": BRAIN_WEIGHTS})


@app.route("/api/execute_order", methods=["POST"])
def api_execute_order():
    """Invia ordine reale a BingX Swap v2 con SL e TP calcolati dal cervello matematico."""
    data = request.json or {}
    symbol = data.get("symbol")
    side = data.get("side")  # BUY o SELL
    position_side = "LONG" if side == "BUY" else "SHORT"
    quantity = data.get("quantity")
    sl = data.get("sl")
    tp = data.get("tp")

    if not all([symbol, side, quantity]):
        return jsonify({"status": "error", "msg": "Parametri ordine incompleti"}), 400

    order_params = {
        "symbol": symbol,
        "side": side,
        "positionSide": position_side,
        "type": "MARKET",
        "quantity": quantity,
        "stopLoss": f'{{"type": "STOP_MARKET", "stopPrice": {sl}, "workingType": "MARK_PRICE"}}',
        "takeProfit": f'{{"type": "TAKE_PROFIT_MARKET", "stopPrice": {tp}, "workingType": "MARK_PRICE"}}'
    }
    res = bingx_private_post("/openApi/swap/v2/trade/order", order_params)
    return jsonify(res)


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
