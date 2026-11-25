import os
import json
import time
import hmac
import hashlib
import requests
import numpy as np
from urllib.parse import urlencode
from flask import Flask, render_template, jsonify, request
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

BINGX_BASE = "https://open-api.bingx.com"
SCANNER_SYMBOLS = [
    "BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT",
    "DOGE-USDT", "BNB-USDT", "AVAX-USDT", "LINK-USDT",
    "SUI-USDT", "ADA-USDT", "TON-USDT", "PEPE-USDT"
]

SIGNAL_HISTORY = []
_SEEN_SIGNAL_KEYS = set()


def _creds():
    return (
        os.getenv("BINGX_API_KEY", "").strip(),
        os.getenv("BINGX_SECRET_KEY", "").strip()
    )


def _bx_sign(params: dict, secret: str) -> str:
    query = urlencode(sorted(params.items()))
    sig = hmac.new(secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{query}&signature={sig}"


def _bx_req(method: str, path: str, params: dict = None, api_key: str = "", secret: str = "", signed: bool = False):
    params = dict(params or {})
    headers = {"X-BX-APIKEY": api_key} if api_key else {}
    if signed:
        if not api_key or not secret:
            return {"code": -1, "msg": "API keys mancanti"}
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 5000
        url = f"{BINGX_BASE}{path}?{_bx_sign(params, secret)}"
    else:
        qs = urlencode(params)
        url = f"{BINGX_BASE}{path}?{qs}" if qs else f"{BINGX_BASE}{path}"

    try:
        if method == "POST":
            r = requests.post(url, headers=headers, timeout=8)
        elif method == "DELETE":
            r = requests.delete(url, headers=headers, timeout=8)
        else:
            r = requests.get(url, headers=headers, timeout=8)
        return r.json()
    except Exception as exc:
        return {"code": -500, "msg": str(exc)}


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
        k = _bx_req("GET", "/openApi/swap/v3/quote/klines", {"symbol": sym, "interval": interval, "limit": 90})
        if k.get("code") != 0 or not k.get("data"):
            continue
        candles = sorted(k["data"], key=lambda x: int(x["time"]))
        if len(candles) < 60:
            continue

        opens = np.array([float(c["open"]) for c in candles])
        highs = np.array([float(c["high"]) for c in candles])
        lows = np.array([float(c["low"]) for c in candles])
        closes = np.array([float(c["close"]) for c in candles])
        vols = np.array([float(c["volume"]) for c in candles])

        price = float(closes[-1])
        chg_24h = ((price - float(opens[0])) / float(opens[0])) * 100.0 if opens[0] else 0.0
        ema9, ema21, ema55 = _ema(closes, 9), _ema(closes, 21), _ema(closes, 55)
        rsi_val = _rsi(closes, 14)
        atr_val = _atr(highs, lows, closes, 14)

        tp_price = (highs + lows + closes) / 3.0
        vwap = np.sum(tp_price[-30:] * vols[-30:]) / (np.sum(vols[-30:]) + 1e-9)
        std_val = np.std(closes[-30:]) + 1e-9
        z_score = float((price - vwap) / std_val)
        vol_ratio = float(vols[-1] / (np.mean(vols[-20:-1]) + 1e-9))

        depth = _bx_req("GET", "/openApi/swap/v2/quote/depth", {"symbol": sym, "limit": 10})
        obi = 0.0
        if depth.get("code") == 0 and depth.get("data"):
            b_vol = sum(float(x[1]) for x in depth["data"].get("bids", []))
            a_vol = sum(float(x[1]) for x in depth["data"].get("asks", []))
            if (b_vol + a_vol) > 0:
                obi = (b_vol - a_vol) / (b_vol + a_vol)

        score = 0.0
        trend_state = "RANGE"
        if ema9[-1] > ema21[-1] > ema55[-1]:
            score += 35
            trend_state = "BULLISH"
        elif ema9[-1] < ema21[-1] < ema55[-1]:
            score -= 35
            trend_state = "BEARISH"
        elif ema9[-1] > ema21[-1]:
            score += 15
            trend_state = "WEAK BULL"
        elif ema9[-1] < ema21[-1]:
            score -= 15
            trend_state = "WEAK BEAR"

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
        if vol_ratio > 1.5:
            score *= 1.1
        score = max(-100.0, min(100.0, round(score, 1)))

        if score >= 60:
            signal, tp, sl, rr = "STRONG BUY", price + 2.8 * atr_val, price - 1.4 * atr_val, 2.0
        elif score >= 35:
            signal, tp, sl, rr = "BUY", price + 2.2 * atr_val, price - 1.4 * atr_val, 1.57
        elif score <= -60:
            signal, tp, sl, rr = "STRONG SELL", price - 2.8 * atr_val, price + 1.4 * atr_val, 2.0
        elif score <= -35:
            signal, tp, sl, rr = "SELL", price - 2.2 * atr_val, price + 1.4 * atr_val, 1.57
        else:
            signal, tp, sl, rr = "NEUTRAL", 0.0, 0.0, 0.0

        spark = [round(float(x), 6) for x in closes[-35:]]

        rows.append({
            "symbol": sym,
            "price": price,
            "change_pct": round(chg_24h, 2),
            "signal": signal,
            "score": score,
            "confidence": round(abs(score), 1),
            "trend": trend_state,
            "rsi": round(rsi_val, 1),
            "z_score": round(z_score, 2),
            "obi": round(obi * 100, 1),
            "vol_ratio": round(vol_ratio, 2),
            "atr": round(atr_val, 6),
            "rr": rr,
            "entry": price if tp > 0 else 0.0,
            "tp": round(tp, 6) if tp > 0 else 0.0,
            "sl": round(sl, 6) if sl > 0 else 0.0,
            "sparkline": spark
        })

    rows.sort(key=lambda r: r["confidence"], reverse=True)
    return rows


def _evaluate_signal_status(sig: dict, current_price: float) -> str:
    side = sig["side"]
    tp1, tp2, tp3, sl = sig["tp1"], sig["tp2"], sig["tp3"], sig["sl"]
    if side == "LONG":
        if current_price <= sl:
            return "SL HIT"
        if current_price >= tp3:
            return "TP3 HIT"
        if current_price >= tp2:
            return "TP2 HIT"
        if current_price >= tp1:
            return "TP1 HIT"
        return "ACTIVE"
    else:
        if current_price >= sl:
            return "SL HIT"
        if current_price <= tp3:
            return "TP3 HIT"
        if current_price <= tp2:
            return "TP2 HIT"
        if current_price <= tp1:
            return "TP1 HIT"
        return "ACTIVE"


def build_signal_room(rows: list, interval: str, leverage: int) -> list:
    now_ts = int(time.time())
    now_str = time.strftime("%H:%M:%S")

    for r in rows:
        price = float(r["price"])
        atr = float(r["atr"])
        sig_type = r["signal"]
        conf = float(r["confidence"])

        if sig_type in ("STRONG BUY", "STRONG SELL") or (sig_type in ("BUY", "SELL") and conf >= 55.0):
            is_long = "BUY" in sig_type
            side = "LONG" if is_long else "SHORT"
            bx_side = "BUY" if is_long else "SELL"

            if is_long:
                sl = round(price - 1.4 * atr, 6)
                tp1 = round(price + 1.4 * atr, 6)
                tp2 = round(price + 2.4 * atr, 6)
                tp3 = round(price + 3.5 * atr, 6)
            else:
                sl = round(price + 1.4 * atr, 6)
                tp1 = round(price - 1.4 * atr, 6)
                tp2 = round(price - 2.4 * atr, 6)
                tp3 = round(price - 3.5 * atr, 6)

            risk_dist = abs(price - sl)
            risk_pct = round((risk_dist / price) * 100.0, 2) if price > 0 else 0.0
            rr_tp3 = round(abs(tp3 - price) / risk_dist, 2) if risk_dist > 0 else 0.0

            key = f"{r['symbol']}-{interval}-{side}-{round(price, 4)}"
            if key not in _SEEN_SIGNAL_KEYS:
                _SEEN_SIGNAL_KEYS.add(key)
                SIGNAL_HISTORY.insert(0, {
                    "id": key,
                    "time": now_str,
                    "ts": now_ts,
                    "symbol": r["symbol"],
                    "tf": interval,
                    "signal": sig_type,
                    "side": side,
                    "bx_side": bx_side,
                    "leverage": leverage,
                    "confidence": conf,
                    "score": r["score"],
                    "rsi": r["rsi"],
                    "z_score": r["z_score"],
                    "obi": r["obi"],
                    "entry": price,
                    "sl": sl,
                    "tp1": tp1,
                    "tp2": tp2,
                    "tp3": tp3,
                    "risk_pct": risk_pct,
                    "rr": rr_tp3,
                    "status": "ACTIVE"
                })
                SIGNAL_HISTORY[:] = SIGNAL_HISTORY[:40]

    price_map = {r["symbol"]: float(r["price"]) for r in rows}
    for item in SIGNAL_HISTORY:
        live_p = price_map.get(item["symbol"])
        if live_p:
            item["current_price"] = live_p
            item["status"] = _evaluate_signal_status(item, live_p)

    return SIGNAL_HISTORY


def fetch_account_and_positions(api_key: str, secret: str):
    account = {
        "connected": False,
        "balance": 0.0,
        "unrealized_pnl": 0.0,
        "leverage": 20,
        "realized_24h": 0.0
    }
    positions = []

    if not api_key or not secret:
        return account, positions

    bal = _bx_req("GET", "/openApi/swap/v2/user/balance", api_key=api_key, secret=secret, signed=True)
    if bal.get("code") == 0 and bal.get("data"):
        b_info = bal["data"].get("balance", {})
        cap = float(b_info.get("availableMargin", 0.0))
        upnl = float(b_info.get("unrealizedProfit", 0.0))

        inc = _bx_req(
            "GET",
            "/openApi/swap/v2/user/income",
            {"incomeType": "REALIZED_PNL", "startTime": int((time.time() - 86400) * 1000), "limit": 100},
            api_key=api_key,
            secret=secret,
            signed=True
        )
        rpnl = sum(float(i.get("income", 0.0)) for i in inc.get("data", [])) if inc.get("code") == 0 else 0.0

        pos_res = _bx_req("GET", "/openApi/swap/v2/user/positions", api_key=api_key, secret=secret, signed=True)
        if pos_res.get("code") == 0 and pos_res.get("data"):
            for p in pos_res["data"]:
                amt = float(p.get("positionAmt", 0.0))
                if amt != 0.0:
                    positions.append({
                        "symbol": p.get("symbol"),
                        "side": p.get("positionSide"),
                        "size": abs(amt),
                        "entry": round(float(p.get("avgPrice", 0.0)), 6),
                        "mark": round(float(p.get("markPrice", 0.0)), 6),
                        "pnl": round(float(p.get("unrealizedProfit", 0.0)), 4),
                        "leverage": p.get("leverage")
                    })

        account = {
            "connected": True,
            "balance": round(cap, 4),
            "unrealized_pnl": round(upnl, 4),
            "leverage": dynamic_leverage(cap),
            "realized_24h": round(rpnl, 4)
        }

    return account, positions


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/vip-scan")
def api_vip_scan():
    tf = request.args.get("tf", "5m")
    api_key, secret = _creds()
    account, positions = fetch_account_and_positions(api_key, secret)
    signals = scan_bingx_market(tf)
    room_signals = build_signal_room(signals, tf, account.get("leverage", 20))
    return jsonify({
        "account": account,
        "positions": positions,
        "signals": signals,
        "signal_room": room_signals,
        "ts": int(time.time())
    })


@app.route("/api/order", methods=["POST"])
def api_order():
    api_key, secret = _creds()
    data = request.json or {}
    symbol = data.get("symbol", "").strip()
    side = data.get("side", "").upper()
    margin_usdt = float(data.get("margin_usdt", 0.0))
    price = float(data.get("price", 0.0))
    tp = float(data.get("tp", 0.0))
    sl = float(data.get("sl", 0.0))

    if not symbol or side not in ("BUY", "SELL") or margin_usdt <= 0 or price <= 0:
        return jsonify({"code": -1, "msg": "Parametri ordine non validi"}), 400

    account, _ = fetch_account_and_positions(api_key, secret)
    lev = int(data.get("leverage") or account.get("leverage", 20))
    pos_side = "LONG" if side == "BUY" else "SHORT"

    _bx_req("POST", "/openApi/swap/v2/trade/leverage", {
        "symbol": symbol,
        "side": pos_side,
        "leverage": lev
    }, api_key=api_key, secret=secret, signed=True)

    qty = round((margin_usdt * lev) / price, 4)
    if qty <= 0:
        return jsonify({"code": -1, "msg": "Quantita troppo piccola"}), 400

    order_params = {
        "symbol": symbol,
        "side": side,
        "positionSide": pos_side,
        "type": "MARKET",
        "quantity": qty
    }
    if tp > 0:
        order_params["takeProfit"] = json.dumps({
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": tp,
            "workingType": "MARK_PRICE"
        })
    if sl > 0:
        order_params["stopLoss"] = json.dumps({
            "type": "STOP_MARKET",
            "stopPrice": sl,
            "workingType": "MARK_PRICE"
        })

    res = _bx_req("POST", "/openApi/swap/v2/trade/order", order_params, api_key=api_key, secret=secret, signed=True)
    return jsonify(res)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
