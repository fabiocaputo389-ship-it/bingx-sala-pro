import hashlib
import hmac
import math
import os
import time
from datetime import datetime, timezone
from urllib.parse import urlencode
from flask import Flask, jsonify, render_template, request
import requests

app = Flask(__name__)

BINGX_BASE_URL = "https://open-api.bingx.com"
API_KEY = os.getenv("BINGX_API_KEY", "")
SECRET_KEY = os.getenv("BINGX_SECRET_KEY", "")

TIMEFRAME_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}

ASSET_CATALOG = {
    "BTC-USDT": {"label": "BTC", "category": "Crypto", "price_decimals": 2, "win_rate": 77},
    "EUR-USDT": {"label": "EURUSDT", "category": "Forex", "price_decimals": 5, "win_rate": 74},
    "XAU-USDT": {"label": "ORO (XAU)", "category": "Materie prime", "price_decimals": 2, "win_rate": 75},
    "OIL-USDT": {"label": "PETROLIO", "category": "Materie prime", "price_decimals": 3, "win_rate": 71},
    "TSLA-USDT": {"label": "TSLA", "category": "Azioni", "price_decimals": 2, "win_rate": 72},
    "NVDA-USDT": {"label": "NVDA", "category": "Azioni", "price_decimals": 2, "win_rate": 76},
}

# Configurazione modificabile a runtime (score soglia e parametri indicatori)
RUNTIME_CONFIG = {
    "min_signal_score": 65,     # Soglia modificabile (0 - 100)
    "rsi_period": 14,
    "rsi_oversold": 30.0,
    "rsi_overbought": 70.0,
    "macd_fast": 12,
    "macd_slow": 26,
    "macd_signal": 9,
    "ema_fast": 20,
    "ema_slow": 50,
    "atr_period": 14,
    "w_rsi": 0.30,
    "w_macd": 0.40,
    "w_ema": 0.30,
}


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

    close_iso_utc = datetime.fromtimestamp(next_close_ts, tz=timezone.utc).strftime("%H:%M:%S UTC")
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
        "closeTimeUtc": close_iso_utc,
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
    if len(closes) <= period:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(0.0, diff))
        losses.append(max(0.0, -diff))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def compute_macd(closes: list[float], fast: int, slow: int, signal: int) -> dict:
    if len(closes) < slow + signal:
        return {"macd": 0.0, "signal": 0.0, "hist": 0.0, "status": "NEUTRAL"}
    ema_f = ema_series(closes, fast)
    ema_s = ema_series(closes, slow)
    macd_line = [f - s for f, s in zip(ema_f, ema_s)]
    signal_line = ema_series(macd_line, signal)
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
    if len(closes) < 2 or len(highs) != len(closes) or len(lows) != len(closes):
        return 0.0
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    window = trs[-period:] if len(trs) >= period else trs
    return round(sum(window) / len(window), 5) if window else 0.0


def fetch_klines(symbol: str, interval: str, limit: int = 120) -> tuple[float, list[float], list[float], list[float]]:
    resp = requests.get(
        f"{BINGX_BASE_URL}/openApi/swap/v2/quote/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=10,
    )
    resp.raise_for_status()
    rows = resp.json().get("data", [])
    opens, highs, lows, closes = [], [], [], []
    for r in rows:
        # Supporta sia dict che list a seconda della risposta
        if isinstance(r, dict):
            highs.append(float(r.get("high", 0)))
            lows.append(float(r.get("low", 0)))
            closes.append(float(r.get("close", 0)))
        elif isinstance(r, (list, tuple)) and len(r) >= 5:
            highs.append(float(r[2]))
            lows.append(float(r[3]))
            closes.append(float(r[4]))
    last_price = closes[-1] if closes else 0.0
    return last_price, highs, lows, closes


def sign_query(params: dict, secret: str) -> str:
    ordered = sorted((k, v) for k, v in params.items() if v is not None)
    qs = urlencode(ordered)
    sig = hmac.new(secret.encode("utf-8"), qs.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{qs}&signature={sig}"


def bingx_signed_request(method: str, path: str, params: dict | None = None) -> dict:
    payload = dict(params or {})
    payload["timestamp"] = int(time.time() * 1000)
    payload["recvWindow"] = 5000
    signed_qs = sign_query(payload, SECRET_KEY)
    headers = {"X-BX-APIKEY": API_KEY}
    url = f"{BINGX_BASE_URL}{path}?{signed_qs}"
    r = requests.get(url, headers=headers, timeout=10) if method.upper() == "GET" else requests.post(url, headers=headers, timeout=10)
    r.raise_for_status()
    return r.json()


def dynamic_leverage(equity_usdt: float, category: str) -> int:
    if equity_usdt <= 2.00:
        return 20 if category == "Crypto" else 10
    if category == "Forex":
        return 10
    if category == "Materie prime":
        return 8
    if category == "Azioni":
        return 5
    return 10 if equity_usdt <= 25.00 else 5


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/config", methods=["GET", "POST"])
def manage_config():
    if request.method == "POST":
        data = request.get_json(force=True) or {}
        if "min_signal_score" in data:
            RUNTIME_CONFIG["min_signal_score"] = max(0, min(100, int(data["min_signal_score"])))
        if "rsi_period" in data:
            RUNTIME_CONFIG["rsi_period"] = max(2, min(100, int(data["rsi_period"])))
        if "rsi_oversold" in data:
            RUNTIME_CONFIG["rsi_oversold"] = float(data["rsi_oversold"])
        if "rsi_overbought" in data:
            RUNTIME_CONFIG["rsi_overbought"] = float(data["rsi_overbought"])
        if "macd_fast" in data:
            RUNTIME_CONFIG["macd_fast"] = max(2, int(data["macd_fast"]))
        if "macd_slow" in data:
            RUNTIME_CONFIG["macd_slow"] = max(3, int(data["macd_slow"]))
        if "macd_signal" in data:
            RUNTIME_CONFIG["macd_signal"] = max(2, int(data["macd_signal"]))
        if "ema_fast" in data:
            RUNTIME_CONFIG["ema_fast"] = max(2, int(data["ema_fast"]))
        if "ema_slow" in data:
            RUNTIME_CONFIG["ema_slow"] = max(3, int(data["ema_slow"]))
        if "atr_period" in data:
            RUNTIME_CONFIG["atr_period"] = max(2, int(data["atr_period"]))
    return jsonify(RUNTIME_CONFIG)


@app.route("/api/live-ticker")
def live_ticker():
    symbol = normalize_symbol(request.args.get("symbol", "BTC-USDT"))
    timeframe = request.args.get("timeframe", "15m")
    if timeframe not in TIMEFRAME_SECONDS:
        timeframe = "15m"

    meta = ASSET_CATALOG.get(symbol, {
        "label": symbol.replace("-", ""),
        "category": "Crypto",
        "price_decimals": 4,
        "win_rate": 72,
    })
    close_meta = candle_close_info(timeframe)

    last_price, highs, lows, closes = 0.0, [], [], []
    try:
        last_price, highs, lows, closes = fetch_klines(symbol, timeframe, limit=120)
    except Exception:
        pass

    rsi_val = compute_rsi(closes, RUNTIME_CONFIG["rsi_period"])
    macd_info = compute_macd(
        closes,
        RUNTIME_CONFIG["macd_fast"],
        RUNTIME_CONFIG["macd_slow"],
        RUNTIME_CONFIG["macd_signal"],
    )
    atr_val = compute_atr(highs, lows, closes, RUNTIME_CONFIG["atr_period"])
    ema_f_val = ema_series(closes, RUNTIME_CONFIG["ema_fast"])[-1] if closes else 0.0
    ema_s_val = ema_series(closes, RUNTIME_CONFIG["ema_slow"])[-1] if closes else 0.0

    # Calcolo score direzionale (-100 bearish .. 0 neutrale .. +100 bullish) e forza (0..100)
    rsi_score = 0.0
    if rsi_val <= RUNTIME_CONFIG["rsi_oversold"]:
        rsi_score = 75.0
    elif rsi_val >= RUNTIME_CONFIG["rsi_overbought"]:
        rsi_score = -75.0
    else:
        rsi_score = (rsi_val - 50.0) * 1.5

    macd_score = 70.0 if macd_info["status"] == "BULLISH" else (-70.0 if macd_info["status"] == "BEARISH" else 0.0)
    ema_score = 65.0 if ema_f_val > ema_s_val else (-65.0 if ema_f_val < ema_s_val else 0.0)

    raw_signed_score = (
        rsi_score * RUNTIME_CONFIG["w_rsi"]
        + macd_score * RUNTIME_CONFIG["w_macd"]
        + ema_score * RUNTIME_CONFIG["w_ema"]
    )
    signal_score = round(min(100.0, abs(raw_signed_score)), 1)
    min_score = float(RUNTIME_CONFIG["min_signal_score"])

    if signal_score < min_score:
        signal_side = "NEUTRAL"
    else:
        signal_side = "BUY" if raw_signed_score > 0 else "SELL"

    whale_pct = int(max(10, min(90, round(50 + raw_signed_score * 0.4))))
    bear_pct = 100 - whale_pct
    trend_health = int(max(10, min(100, round(50 + abs(ema_f_val - ema_s_val) / max(last_price, 1e-9) * 10000))))

    high_risk = signal_score < min_score or meta["category"] in ("Materie prime", "Azioni")
    risk_reason = (
        f"Score {signal_score} sotto soglia ({min_score})"
        if signal_score < min_score
        else f"Controlla sessione e ATR ({atr_val}) su {meta['category']}"
    )

    return jsonify({
        "symbol": symbol,
        "label": meta["label"],
        "category": meta["category"],
        "priceDecimals": meta["price_decimals"],
        "lastPrice": last_price,
        "signalSide": signal_side,
        "signalScore": signal_score,
        "minSignalScore": min_score,
        "winRate": meta["win_rate"],
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
            "emaFast": round(ema_f_val, meta["price_decimals"]),
            "emaSlow": round(ema_s_val, meta["price_decimals"]),
            "atr": atr_val,
        },
        "highRiskWarning": high_risk,
        "riskReason": risk_reason,
        **close_meta,
    })


@app.route("/api/execute-signal", methods=["POST"])
def execute_signal():
    body = request.get_json(force=True)
    symbol = normalize_symbol(body.get("symbol", "BTC-USDT"))
    side = (body.get("side") or "NEUTRAL").upper()
    score = float(body.get("signalScore", 0.0))
    min_score = float(RUNTIME_CONFIG["min_signal_score"])
    equity = float(body.get("equity", 2.0))
    tp_price = body.get("tpPrice")
    sl_price = body.get("slPrice")

    if side not in ("BUY", "SELL"):
        return jsonify({"status": "skipped", "reason": "Direzione NEUTRAL: nessun ordine inviato."}), 200
    if score < min_score:
        return jsonify({"status": "blocked", "reason": f"Score {score} inferiore alla soglia minima {min_score}."}), 200
    if tp_price is None or sl_price is None:
        return jsonify({"status": "rejected", "reason": "TP e SL separati sono obbligatori."}), 400

    meta = ASSET_CATALOG.get(symbol, {"category": "Crypto"})
    lev = dynamic_leverage(equity, meta["category"])
    bingx_signed_request("POST", "/openApi/swap/v2/trade/leverage", {
        "symbol": symbol,
        "side": "LONG" if side == "BUY" else "SHORT",
        "leverage": lev,
    })

    return jsonify({
        "status": "prepared",
        "symbol": symbol,
        "side": side,
        "signalScore": score,
        "leverage": lev,
        "tpPrice": tp_price,
        "slPrice": sl_price,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
