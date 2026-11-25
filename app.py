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

TF_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400
}

MIN_SCORE_THRESHOLD = 65.0
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


def candle_timer_info(interval: str, now_ts: int) -> dict:
    period = TF_SECONDS.get(interval, 300)
    candle_open_ts = (now_ts // period) * period
    candle_close_ts = candle_open_ts + period
    rem = max(0, candle_close_ts - now_ts)
    elapsed = period - rem
    progress_pct = round((elapsed / period) * 100.0, 1)
    return {
        "timeframe": interval,
        "period_sec": period,
        "remaining_sec": rem,
        "candle_open_ts": candle_open_ts,
        "candle_close_ts": candle_close_ts,
        "progress_pct": progress_pct
    }


def _detect_false_breakout(opens, highs, lows, closes, vols, ema21, vwap, obi, vol_ratio, score):
    """
    Rileva falsi segnali e false rotture (fakeout / stop-hunt):
    1. Wick eccessiva rispetto al corpo reale della candela precedente o corrente
    2. Rottura di un massimo/minimo recente seguita da rientro immediato
    3. Divergenza netta tra direzione del punteggio e Orderbook Imbalance (OBI)
    4. Volume troppo basso (assenza di partecipazione istituzionale)
    """
    reasons = []
    c_open = float(opens[-1])
    c_high = float(highs[-1])
    c_low = float(lows[-1])
    c_close = float(closes[-1])

    prev_high = float(np.max(highs[-10:-1]))
    prev_low = float(np.min(lows[-10:-1]))
    prev_close = float(closes[-2])

    body = abs(c_close - c_open) + 1e-9
    upper_wick = max(0.0, c_high - max(c_open, c_close))
    lower_wick = max(0.0, min(c_open, c_close) - c_low)

    if vol_ratio < 0.85:
        reasons.append(f"Volume debole ({vol_ratio}x)")

    if score > 0:
        # Falsa rottura rialzista: spinge sopra massimo recente ma chiude sotto o lascia upper wick lunga
        if c_high > prev_high and c_close < prev_high:
            reasons.append("Bull trap: rottura high rientrata")
        if upper_wick > body * 1.8 and c_close < c_open:
            reasons.append("Upper wick respinta")
        if obi < -20.0:
            reasons.append(f"Orderbook contro LONG ({obi}%)")
        if prev_close < float(ema21[-2]) and c_close < float(ema21[-1]):
            reasons.append("Chiusura sotto EMA21")
    elif score < 0:
        # Falsa rottura ribassista: buca minimo recente ma chiude sopra o lascia lower wick lunga
        if c_low < prev_low and c_close > prev_low:
            reasons.append("Bear trap: rottura low rientrata")
        if lower_wick > body * 1.8 and c_close > c_open:
            reasons.append("Lower wick respinta")
        if obi > 20.0:
            reasons.append(f"Orderbook contro SHORT ({obi}%)")
        if prev_close > float(ema21[-2]) and c_close > float(ema21[-1]):
            reasons.append("Chiusura sopra EMA21")

    return {
        "is_fakeout": len(reasons) > 0,
        "reasons": reasons,
        "summary": " · ".join(reasons) if reasons else "PULITO (NO FAKEOUT)"
    }


def scan_bingx_market(interval: str = "5m", min_score: float = 65.0) -> list:
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
        e9, e21, e55 = float(ema9[-1]), float(ema21[-1]), float(ema55[-1])

        rsi_val = _rsi(closes, 14)
        atr_val = _atr(highs, lows, closes, 14)

        tp_price = (highs + lows + closes) / 3.0
        vwap = float(np.sum(tp_price[-30:] * vols[-30:]) / (np.sum(vols[-30:]) + 1e-9))
        std_val = float(np.std(closes[-30:])) + 1e-9
        z_score = float((price - vwap) / std_val)
        vol_ratio = float(vols[-1] / (np.mean(vols[-20:-1]) + 1e-9))

        depth = _bx_req("GET", "/openApi/swap/v2/quote/depth", {"symbol": sym, "limit": 10})
        obi_raw = 0.0
        if depth.get("code") == 0 and depth.get("data"):
            b_vol = sum(float(x[1]) for x in depth["data"].get("bids", []))
            a_vol = sum(float(x[1]) for x in depth["data"].get("asks", []))
            if (b_vol + a_vol) > 0:
                obi_raw = (b_vol - a_vol) / (b_vol + a_vol)
        obi_pct = round(obi_raw * 100.0, 1)

        factors_aligned = 0
        raw_score = 0.0
        trend_state = "RANGE"

        if e9 > e21 > e55:
            raw_score += 38
            trend_state = "BULLISH"
            factors_aligned += 1
        elif e9 < e21 < e55:
            raw_score -= 38
            trend_state = "BEARISH"
            factors_aligned += 1
        elif e9 > e21:
            raw_score += 16
            trend_state = "WEAK BULL"
        elif e9 < e21:
            raw_score -= 16
            trend_state = "WEAK BEAR"

        if rsi_val < 30:
            raw_score += 24
            factors_aligned += 1
        elif rsi_val > 70:
            raw_score -= 24
            factors_aligned += 1
        elif rsi_val > 52:
            raw_score += 14
            if raw_score > 0:
                factors_aligned += 1
        elif rsi_val < 48:
            raw_score -= 14
            if raw_score < 0:
                factors_aligned += 1

        if 0.35 < z_score < 2.1:
            raw_score += 20
            if raw_score > 0:
                factors_aligned += 1
        elif -2.1 < z_score < -0.35:
            raw_score -= 20
            if raw_score < 0:
                factors_aligned += 1

        raw_score += obi_raw * 18.0
        if (raw_score > 0 and obi_raw > 0.15) or (raw_score < 0 and obi_raw < -0.15):
            factors_aligned += 1

        if vol_ratio >= 1.25:
            raw_score *= 1.12
            factors_aligned += 1

        fakeout_info = _detect_false_breakout(opens, highs, lows, closes, vols, ema21, vwap, obi_pct, vol_ratio, raw_score)
        if fakeout_info["is_fakeout"]:
            raw_score *= 0.55

        score = max(-100.0, min(100.0, round(raw_score, 1)))
        abs_score = round(abs(score), 1)

        certainty_pct = round(min(99.0, abs_score * (0.72 + 0.06 * min(factors_aligned, 5))) * (0.65 if fakeout_info["is_fakeout"] else 1.0), 1)
        certainty_pct = max(0.0, min(99.0, certainty_pct))

        meets_score = abs_score >= float(min_score)
        can_enter = meets_score and (not fakeout_info["is_fakeout"]) and (certainty_pct >= 60.0)

        if score >= 60 and not fakeout_info["is_fakeout"]:
            signal, tp, sl, rr = "STRONG BUY", price + 2.8 * atr_val, price - 1.4 * atr_val, 2.0
        elif score >= 35 and not fakeout_info["is_fakeout"]:
            signal, tp, sl, rr = "BUY", price + 2.2 * atr_val, price - 1.4 * atr_val, 1.57
        elif score <= -60 and not fakeout_info["is_fakeout"]:
            signal, tp, sl, rr = "STRONG SELL", price - 2.8 * atr_val, price + 1.4 * atr_val, 2.0
        elif score <= -35 and not fakeout_info["is_fakeout"]:
            signal, tp, sl, rr = "SELL", price - 2.2 * atr_val, price + 1.4 * atr_val, 1.57
        else:
            signal, tp, sl, rr = "NEUTRAL", 0.0, 0.0, 0.0

        # Avvisi operativi stretti: entrata consentita SOLO su segnale certo, score >= soglia e no fakeout
        if can_enter and score > 0:
            entry_alert = f"ENTRA LONG ORA @ {price} (Score {score} ≥ {min_score})"
            exit_alert = f"ESCI A TP {round(tp, 6)} O SL {round(sl, 6)}"
            entry_status = "CERTIFIED ENTRY"
        elif can_enter and score < 0:
            entry_alert = f"ENTRA SHORT ORA @ {price} (Score {score} ≤ -{min_score})"
            exit_alert = f"ESCI A TP {round(tp, 6)} O SL {round(sl, 6)}"
            entry_status = "CERTIFIED ENTRY"
        elif fakeout_info["is_fakeout"]:
            entry_alert = f"BLOCCATO (ANTI-FAKEOUT): {fakeout_info['summary']}"
            exit_alert = "NON ENTRARE — ATTENDI RIENTRO/CONFERMA"
            entry_status = "FAKEOUT BLOCK"
        else:
            entry_alert = f"NO ENTRY — SCORE {abs_score}/{min_score} (Certezza {certainty_pct}%)"
            exit_alert = "ATTENDI SCORE CERTO PRIMA DI APRIRE"
            entry_status = "LOW SCORE"

        spark = [round(float(x), 6) for x in closes[-35:]]

        rows.append({
            "symbol": sym,
            "timeframe": interval,
            "price": price,
            "change_pct": round(chg_24h, 2),
            "signal": signal,
            "score": score,
            "abs_score": abs_score,
            "min_score": float(min_score),
            "can_enter": can_enter,
            "entry_status": entry_status,
            "confidence": abs_score,
            "certainty_pct": certainty_pct,
            "factors_aligned": factors_aligned,
            "is_fakeout": fakeout_info["is_fakeout"],
            "fakeout_summary": fakeout_info["summary"],
            "trend": trend_state,
            "ema9": round(e9, 6),
            "ema21": round(e21, 6),
            "ema55": round(e55, 6),
            "rsi": round(rsi_val, 1),
            "z_score": round(z_score, 2),
            "obi": obi_pct,
            "vol_ratio": round(vol_ratio, 2),
            "atr": round(atr_val, 6),
            "rr": rr,
            "entry": price if can_enter else 0.0,
            "tp": round(tp, 6) if can_enter and tp > 0 else 0.0,
            "sl": round(sl, 6) if can_enter and sl > 0 else 0.0,
            "entry_alert": entry_alert,
            "exit_alert": exit_alert,
            "sparkline": spark
        })

    rows.sort(key=lambda r: (1 if r["can_enter"] else 0, r["certainty_pct"]), reverse=True)
    return rows


def _evaluate_signal_status(sig: dict, current_price: float) -> tuple:
    side = sig["side"]
    tp1, tp2, tp3, sl = sig["tp1"], sig["tp2"], sig["tp3"], sig["sl"]
    if side == "LONG":
        if current_price <= sl:
            return "SL HIT", f"ESCI SUBITO: STOP LOSS TOCCATO ({sl})"
        if current_price >= tp3:
            return "TP3 HIT", f"ESCI / INCASSA COMPLETO A TP3 ({tp3})"
        if current_price >= tp2:
            return "TP2 HIT", f"PARZIALIZZA / SPOSTA SL A TP1 ({tp1})"
        if current_price >= tp1:
            return "TP1 HIT", f"PRENDI PARZIALE TP1 ({tp1}) — SL A BREAKEVEN"
        return "CERTIFIED ACTIVE", f"MANTIENI LONG — TP1 {tp1} / SL {sl}"
    else:
        if current_price >= sl:
            return "SL HIT", f"ESCI SUBITO: STOP LOSS TOCCATO ({sl})"
        if current_price <= tp3:
            return "TP3 HIT", f"ESCI / INCASSA COMPLETO A TP3 ({tp3})"
        if current_price <= tp2:
            return "TP2 HIT", f"PARZIALIZZA / SPOSTA SL A TP1 ({tp1})"
        if current_price <= tp1:
            return "TP1 HIT", f"PRENDI PARZIALE TP1 ({tp1}) — SL A BREAKEVEN"
        return "CERTIFIED ACTIVE", f"MANTIENI SHORT — TP1 {tp1} / SL {sl}"


def build_signal_room(rows: list, interval: str, leverage: int) -> list:
    now_ts = int(time.time())
    now_str = time.strftime("%H:%M:%S")
    timer_meta = candle_timer_info(interval, now_ts)

    for r in rows:
        # Nella Sala Segnali entrano SOLO setup certi, privi di false rotture e con score >= soglia
        if not r.get("can_enter"):
            continue

        price = float(r["price"])
        atr = float(r["atr"])
        sig_type = r["signal"]
        cert = float(r["certainty_pct"])
        is_long = r["score"] > 0
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
                "candle_close_ts": timer_meta["candle_close_ts"],
                "signal": sig_type,
                "side": side,
                "bx_side": bx_side,
                "leverage": leverage,
                "certainty_pct": cert,
                "score": r["score"],
                "min_score": r["min_score"],
                "fakeout_summary": r["fakeout_summary"],
                "ema9": r["ema9"],
                "ema21": r["ema21"],
                "ema55": r["ema55"],
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
                "entry_advice": f"ENTRA {side} @ {price} (Score {r['score']} · Certezza {cert}%)",
                "exit_advice": f"ESCI A TP1 {tp1} / TP2 {tp2} / TP3 {tp3} — SL {sl}",
                "status": "CERTIFIED ACTIVE"
            })
            SIGNAL_HISTORY[:] = SIGNAL_HISTORY[:40]

    price_map = {r["symbol"]: float(r["price"]) for r in rows}
    for item in SIGNAL_HISTORY:
        live_p = price_map.get(item["symbol"])
        if live_p:
            item["current_price"] = live_p
            st, ex_adv = _evaluate_signal_status(item, live_p)
            item["status"] = st
            item["exit_advice"] = ex_adv

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
    global MIN_SCORE_THRESHOLD
    tf = request.args.get("tf", "5m")
    if tf not in TF_SECONDS:
        tf = "5m"

    try:
        req_score = float(request.args.get("min_score", MIN_SCORE_THRESHOLD))
        MIN_SCORE_THRESHOLD = max(10.0, min(95.0, req_score))
    except (TypeError, ValueError):
        pass

    now_ts = int(time.time())
    api_key, secret = _creds()
    account, positions = fetch_account_and_positions(api_key, secret)
    signals = scan_bingx_market(tf, MIN_SCORE_THRESHOLD)
    room_signals = build_signal_room(signals, tf, account.get("leverage", 20))
    return jsonify({
        "account": account,
        "positions": positions,
        "signals": signals,
        "signal_room": room_signals,
        "candle_timer": candle_timer_info(tf, now_ts),
        "min_score": MIN_SCORE_THRESHOLD,
        "ts": now_ts
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
    force_override = bool(data.get("force_override", False))

    if not symbol or side not in ("BUY", "SELL") or margin_usdt <= 0 or price <= 0:
        return jsonify({"code": -1, "msg": "Parametri ordine non validi"}), 400

    # Controllo di sicurezza anti-falso segnale / score minimo
    tf_check = data.get("tf", "5m")
    live_rows = scan_bingx_market(tf_check if tf_check in TF_SECONDS else "5m", MIN_SCORE_THRESHOLD)
    match_row = next((r for r in live_rows if r["symbol"] == symbol), None)
    if match_row and (not match_row["can_enter"]) and (not force_override):
        return jsonify({
            "code": -2,
            "msg": f"Entrata bloccata: {match_row['entry_alert']}"
        }), 400

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
