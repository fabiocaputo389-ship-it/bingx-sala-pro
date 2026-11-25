import time
import hmac
import hashlib
import requests
import numpy as np
from urllib.parse import urlencode
from flask import jsonify, request

BINGX_BASE = "https://open-api.bingx.com"
SCANNER_SYMBOLS = [
    "BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT",
    "DOGE-USDT", "BNB-USDT", "AVAX-USDT", "LINK-USDT"
]


def _bx_sign(params: dict, secret: str) -> str:
    query = urlencode(sorted(params.items()))
    sig = hmac.new(secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{query}&signature={sig}"


def _bx_req(path: str, params: dict = None, api_key: str = "", secret: str = "", signed: bool = False):
    params = dict(params or {})
    headers = {"X-BX-APIKEY": api_key} if api_key else {}
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 5000
        url = f"{BINGX_BASE}{path}?{_bx_sign(params, secret)}"
    else:
        qs = urlencode(params)
        url = f"{BINGX_BASE}{path}?{qs}" if qs else f"{BINGX_BASE}{path}"
    r = requests.get(url, headers=headers, timeout=8)
    return r.json()


def _ema(arr: np.ndarray, period: int) -> np.ndarray:
    alpha = 2.0 / (period + 1.0)
    out = np.empty_like(arr, dtype=float)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = alpha * arr[i] + (1.0 - alpha) * out[i - 1]
    return out


def _rsi(closes: np.ndarray, period: int = 14) -> float:
    diff = np.diff(closes)
    gains = np.where(diff > 0, diff, 0.0)
    losses = np.where(diff < 0, -diff, 0.0)
    avg_g = np.mean(gains[:period])
    avg_l = np.mean(losses[:period])
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return float(100.0 - (100.0 / (1.0 + rs)))


def _atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> float:
    tr = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(np.abs(highs[1:] - closes[:-1]), np.abs(lows[1:] - closes[:-1]))
    )
    return float(np.mean(tr[-period:]))


def dynamic_leverage(capital: float) -> int:
    if capital <= 2.0:
        return 20
    if capital <= 10.0:
        return 15
    if capital <= 50.0:
        return 10
    return 5


def scan_bingx_market(interval: str = "5m") -> list:
    rows = []
    for sym in SCANNER_SYMBOLS:
        k = _bx_req("/openApi/swap/v3/quote/klines", {"symbol": sym, "interval": interval, "limit": 90})
        if k.get("code") != 0 or not k.get("data"):
            continue
        candles = sorted(k["data"], key=lambda x: int(x["time"]))
        if len(candles) < 60:
            continue

        highs = np.array([float(c["high"]) for c in candles])
        lows = np.array([float(c["low"]) for c in candles])
        closes = np.array([float(c["close"]) for c in candles])
        vols = np.array([float(c["volume"]) for c in candles])

        price = float(closes[-1])
        ema9, ema21, ema55 = _ema(closes, 9), _ema(closes, 21), _ema(closes, 55)
        rsi_val = _rsi(closes, 14)
        atr_val = _atr(highs, lows, closes, 14)

        tp_price = (highs + lows + closes) / 3.0
        vwap = np.sum(tp_price[-30:] * vols[-30:]) / (np.sum(vols[-30:]) + 1e-9)
        std_val = np.std(closes[-30:]) + 1e-9
        z_score = float((price - vwap) / std_val)

        depth = _bx_req("/openApi/swap/v2/quote/depth", {"symbol": sym, "limit": 10})
        obi = 0.0
        if depth.get("code") == 0 and depth.get("data"):
            b_vol = sum(float(x[1]) for x in depth["data"].get("bids", []))
            a_vol = sum(float(x[1]) for x in depth["data"].get("asks", []))
            if (b_vol + a_vol) > 0:
                obi = (b_vol - a_vol) / (b_vol + a_vol)

        score = 0.0
        if ema9[-1] > ema21[-1] > ema55[-1]:
            score += 35
        elif ema9[-1] < ema21[-1] < ema55[-1]:
            score -= 35

        if rsi_val < 30:
            score += 25
        elif rsi_val > 70:
            score -= 25
        elif rsi_val > 52:
            score += 15
        elif rsi_val < 48:
            score -= 15

        if 0.3 < z_score < 2.0:
            score += 20
        elif -2.0 < z_score < -0.3:
            score -= 20

        score += obi * 20.0
        score = max(-100.0, min(100.0, round(score, 1)))

        if score >= 60:
            signal, tp, sl = "STRONG BUY", price + 2.8 * atr_val, price - 1.4 * atr_val
        elif score >= 35:
            signal, tp, sl = "BUY", price + 2.2 * atr_val, price - 1.4 * atr_val
        elif score <= -60:
            signal, tp, sl = "STRONG SELL", price - 2.8 * atr_val, price + 1.4 * atr_val
        elif score <= -35:
            signal, tp, sl = "SELL", price - 2.2 * atr_val, price + 1.4 * atr_val
        else:
            signal, tp, sl = "NEUTRAL", 0.0, 0.0

        rows.append({
            "symbol": sym,
            "price": price,
            "signal": signal,
            "score": score,
            "confidence": round(abs(score), 1),
            "rsi": round(rsi_val, 1),
            "z_score": round(z_score, 2),
            "obi": round(obi * 100, 1),
            "entry": price if tp > 0 else 0.0,
            "tp": round(tp, 6) if tp > 0 else 0.0,
            "sl": round(sl, 6) if sl > 0 else 0.0
        })

    rows.sort(key=lambda r: r["confidence"], reverse=True)
    return rows


@app.route("/api/vip-scan")
def api_vip_scan():
    tf = request.args.get("tf", "5m")
    api_key = os.getenv("BINGX_API_KEY", "").strip()
    secret = os.getenv("BINGX_SECRET_KEY", "").strip()

    account = {"connected": False, "balance": 0.0, "leverage": 20, "realized_24h": 0.0}
    if api_key and secret:
        bal = _bx_req("/openApi/swap/v2/user/balance", api_key=api_key, secret=secret, signed=True)
        if bal.get("code") == 0 and bal.get("data"):
            cap = float(bal["data"].get("balance", {}).get("availableMargin", 0.0))
            inc = _bx_req(
                "/openApi/swap/v2/user/income",
                {"incomeType": "REALIZED_PNL", "startTime": int((time.time() - 86400) * 1000), "limit": 100},
                api_key=api_key,
                secret=secret,
                signed=True
            )
            rpnl = sum(float(i.get("income", 0.0)) for i in inc.get("data", [])) if inc.get("code") == 0 else 0.0
            account = {
                "connected": True,
                "balance": round(cap, 4),
                "leverage": dynamic_leverage(cap),
                "realized_24h": round(rpnl, 4)
            }

    return jsonify({"account": account, "signals": scan_bingx_market(tf)})
