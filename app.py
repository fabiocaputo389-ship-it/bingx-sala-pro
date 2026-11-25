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

SCORE_PARAMS = {
    "min_score": 65.0,
    "w_ema": 38.0,
    "w_rsi": 24.0,
    "w_zscore": 20.0,
    "w_obi": 18.0
}

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


def _macd(closes: np.ndarray):
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    macd_line = ema12 - ema26
    signal_line = _ema(macd_line, 9)
    hist = macd_line - signal_line
    if float(macd_line[-1]) > float(signal_line[-1]) and float(hist[-1]) >= 0:
        state = "RIALZISTA"
    elif float(macd_line[-1]) < float(signal_line[-1]) and float(hist[-1]) <= 0:
        state = "RIBASSISTA"
    else:
        state = "INVERSIONE"
    return round(float(macd_line[-1]), 6), round(float(hist[-1]), 6), state


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
    validity_pct = max(5.0, round(100.0 - (progress_pct * 0.65), 1))
    return {
        "timeframe": interval,
        "period_sec": period,
        "remaining_sec": rem,
        "candle_open_ts": candle_open_ts,
        "candle_close_ts": candle_close_ts,
        "progress_pct": progress_pct,
        "validity_pct": validity_pct
    }


def _detect_false_breakout(opens, highs, lows, closes, ema21, obi_pct, vol_ratio, score):
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
        if c_high > prev_high and c_close < prev_high:
            reasons.append("Falsa rottura massimo (Bull Trap)")
        if upper_wick > body * 1.8 and c_close < c_open:
            reasons.append("Respinta ombra superiore")
        if obi_pct < -20.0:
            reasons.append(f"Pressione Orsi contro LONG ({obi_pct}%)")
        if prev_close < float(ema21[-2]) and c_close < float(ema21[-1]):
            reasons.append("Chiusura sotto EMA21")
    elif score < 0:
        if c_low < prev_low and c_close > prev_low:
            reasons.append("Falsa rottura minimo (Bear Trap)")
        if lower_wick > body * 1.8 and c_close > c_open:
            reasons.append("Respinta ombra inferiore")
        if obi_pct > 20.0:
            reasons.append(f"Pressione Balene contro SHORT (+{obi_pct}%)")
        if prev_close > float(ema21[-2]) and c_close > float(ema21[-1]):
            reasons.append("Chiusura sopra EMA21")

    return {
        "is_fakeout": len(reasons) > 0,
        "reasons": reasons,
        "summary": " · ".join(reasons) if reasons else "PULITO (NESSUNA FALSA ROTTURA)"
    }


def scan_bingx_market(interval: str = "5m", params: dict = None) -> list:
    cfg = dict(SCORE_PARAMS)
    if params:
        cfg.update(params)

    min_score = float(cfg["min_score"])
    w_ema = float(cfg["w_ema"])
    w_rsi = float(cfg["w_rsi"])
    w_z = float(cfg["w_zscore"])
    w_obi = float(cfg["w_obi"])

    # Prima ricava trend/z-score BTC per valutare rischio correlazione sulle altcoin
    btc_z = 0.0
    btc_k = _bx_req("GET", "/openApi/swap/v3/quote/klines", {"symbol": "BTC-USDT", "interval": interval, "limit": 45})
    if btc_k.get("code") == 0 and btc_k.get("data"):
        bc = sorted(btc_k["data"], key=lambda x: int(x["time"]))
        if len(bc) >= 30:
            bh = np.array([float(c["high"]) for c in bc])
            bl = np.array([float(c["low"]) for c in bc])
            bcl = np.array([float(c["close"]) for c in bc])
            bv = np.array([float(c["volume"]) for c in bc])
            btp = (bh + bl + bcl) / 3.0
            bvwap = float(np.sum(btp[-30:] * bv[-30:]) / (np.sum(bv[-30:]) + 1e-9))
            bstd = float(np.std(bcl[-30:])) + 1e-9
            btc_z = float((float(bcl[-1]) - bvwap) / bstd)

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
        _, _, macd_state = _macd(closes)

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

        whales_pct = round(max(10.0, min(90.0, 50.0 + obi_raw * 40.0)), 0)
        bears_pct = round(100.0 - whales_pct, 0)
        obi_pct = round(obi_raw * 100.0, 1)

        factors_aligned = 0
        raw_score = 0.0
        trend_state = "LATERALE"

        if e9 > e21 > e55:
            raw_score += w_ema
            trend_state = "RIALZISTA"
            factors_aligned += 1
        elif e9 < e21 < e55:
            raw_score -= w_ema
            trend_state = "RIBASSISTA"
            factors_aligned += 1
        elif e9 > e21:
            raw_score += w_ema * 0.45
            trend_state = "DEBOLE LONG"
        elif e9 < e21:
            raw_score -= w_ema * 0.45
            trend_state = "DEBOLE SHORT"

        if rsi_val < 30:
            raw_score += w_rsi
            factors_aligned += 1
        elif rsi_val > 70:
            raw_score -= w_rsi
            factors_aligned += 1
        elif rsi_val > 52:
            raw_score += w_rsi * 0.6
            if raw_score > 0:
                factors_aligned += 1
        elif rsi_val < 48:
            raw_score -= w_rsi * 0.6
            if raw_score < 0:
                factors_aligned += 1

        if 0.35 < z_score < 2.1:
            raw_score += w_z
            if raw_score > 0:
                factors_aligned += 1
        elif -2.1 < z_score < -0.35:
            raw_score -= w_z
            if raw_score < 0:
                factors_aligned += 1

        raw_score += obi_raw * w_obi
        if (raw_score > 0 and obi_raw > 0.15) or (raw_score < 0 and obi_raw < -0.15):
            factors_aligned += 1

        if vol_ratio >= 1.25:
            raw_score *= 1.12
            factors_aligned += 1

        fakeout_info = _detect_false_breakout(opens, highs, lows, closes, ema21, obi_pct, vol_ratio, raw_score)
        if fakeout_info["is_fakeout"]:
            raw_score *= 0.55

        score = max(-100.0, min(100.0, round(raw_score, 1)))
        abs_score = round(abs(score), 1)

        # Salute del trend (0-100%)
        ema_spread = abs(e9 - e55) / (price + 1e-9) * 100.0
        trend_health = int(max(15, min(98, round(45 + min(35, ema_spread * 12) + factors_aligned * 4))))

        # Win Rate stimato matematicamente da confluenza, assenza fakeout e volumi
        win_rate = int(max(45, min(92, round(54 + factors_aligned * 6.5 - (14 if fakeout_info["is_fakeout"] else 0)))))

        # Certezza % reale
        certainty_pct = round(
            min(99.0, abs_score * (0.72 + 0.06 * min(factors_aligned, 5))) * (0.65 if fakeout_info["is_fakeout"] else 1.0),
            1
        )
        certainty_pct = max(0.0, min(99.0, certainty_pct))

        # Controllo rischio correlazione BTC sulle altcoin
        corr_risk = (sym != "BTC-USDT") and ((score > 0 and btc_z < -1.6) or (score < 0 and btc_z > 1.6))

        meets_score = abs_score >= min_score
        can_enter = meets_score and (not fakeout_info["is_fakeout"]) and (not corr_risk) and (certainty_pct >= 60.0)

        # Posizione lancetta tachimetro (0 = massimo rosso SELL, 50 = neutro, 100 = massimo verde BUY)
        gauge_pct = int(max(0, min(100, round(50.0 + (score / 2.0)))))

        if score >= 60 and not fakeout_info["is_fakeout"]:
            signal, gauge_label, tp, sl, rr = "FORTE ACQUISTO", "Acquisto Forte", price + 2.8 * atr_val, price - 1.4 * atr_val, 2.0
        elif score >= 35 and not fakeout_info["is_fakeout"]:
            signal, gauge_label, tp, sl, rr = "ACQUISTO", "Acquisto", price + 2.2 * atr_val, price - 1.4 * atr_val, 1.57
        elif score <= -60 and not fakeout_info["is_fakeout"]:
            signal, gauge_label, tp, sl, rr = "FORTE VENDITA", "Vendita Forte", price - 2.8 * atr_val, price + 1.4 * atr_val, 2.0
        elif score <= -35 and not fakeout_info["is_fakeout"]:
            signal, gauge_label, tp, sl, rr = "VENDITA", "Vendita", price - 2.2 * atr_val, price + 1.4 * atr_val, 1.57
        else:
            signal, gauge_label, tp, sl, rr = "NEUTRALE", "Neutrale", 0.0, 0.0, 0.0

        # Sentiment in italiano
        if abs_score >= min_score and not fakeout_info["is_fakeout"] and not corr_risk:
            sentiment = "RIALZISTA" if score > 0 else "RIBASSISTA"
        elif fakeout_info["is_fakeout"] or corr_risk:
            sentiment = "CAUTELA"
        else:
            sentiment = "ATTESA"

        # Avvisi operativi di Entrata, Rischio e Uscita
        if can_enter and score > 0:
            entry_alert = f"ENTRA LONG ORA @ {price} (Score {score} ≥ {min_score})"
            exit_alert = f"ESCI A TP {round(tp, 6)} O SL {round(sl, 6)}"
            risk_warning = None
        elif can_enter and score < 0:
            entry_alert = f"ENTRA SHORT ORA @ {price} (Score {score} ≤ -{min_score})"
            exit_alert = f"ESCI A TP {round(tp, 6)} O SL {round(sl, 6)}"
            risk_warning = None
        elif corr_risk:
            entry_alert = "NON ENTRARE — SEGNALE AD ALTO RISCHIO"
            exit_alert = "ATTENDI ALLINEAMENTO CON BTC"
            risk_warning = "Motivo: Rischio correlazione BTC / Balene"
        elif fakeout_info["is_fakeout"]:
            entry_alert = "NON ENTRARE — FALSA ROTTURA RILEVATA"
            exit_alert = "NON APRIRE — ATTENDI CONFERMA CANDELA"
            risk_warning = f"Motivo: {fakeout_info['summary']}"
        else:
            entry_alert = f"NO ENTRATA — SCORE {abs_score}/{min_score} (Certezza {certainty_pct}%)"
            exit_alert = "ATTENDI SCORE CERTO PRIMA DI ENTRARE"
            risk_warning = f"Motivo: Score attuale {abs_score} sotto soglia {min_score}"

        direction_txt = "LONG" if score >= 0 else "SHORT"
        recommendation = f"RACCOMANDAZIONE: {direction_txt}"
        rec_detail = f"Pressione: {int(whales_pct)}% Balene / {int(bears_pct)}% Orsi. MACD: {macd_state}."

        rows.append({
            "symbol": sym,
            "timeframe": interval,
            "price": price,
            "change_pct": round(chg_24h, 2),
            "signal": signal,
            "gauge_label": gauge_label,
            "gauge_pct": gauge_pct,
            "win_rate": win_rate,
            "top_asset": (abs_score >= max(65.0, min_score) and certainty_pct >= 75.0 and not fakeout_info["is_fakeout"]),
            "trend_health": trend_health,
            "whales_pct": int(whales_pct),
            "bears_pct": int(bears_pct),
            "sentiment": sentiment,
            "macd_state": macd_state,
            "recommendation": recommendation,
            "rec_detail": rec_detail,
            "risk_warning": risk_warning,
            "score": score,
            "abs_score": abs_score,
            "min_score": min_score,
            "can_enter": can_enter,
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
            "sparkline": [round(float(x), 6) for x in closes[-35:]]
        })

    rows.sort(key=lambda r: (1 if r["can_enter"] else 0, r["certainty_pct"]), reverse=True)
    return rows


def _evaluate_signal_status(sig: dict, current_price: float) -> tuple:
    side = sig["side"]
    tp1, tp2, tp3, sl = sig["tp1"], sig["tp2"], sig["tp3"], sig["sl"]
    if side == "LONG":
        if current_price <= sl:
            return "SL PRESO", f"ESCI SUBITO: STOP LOSS TOCCATO ({sl})"
        if current_price >= tp3:
            return "TP3 PRESO", f"ESCI / INCASSA COMPLETO A TP3 ({tp3})"
        if current_price >= tp2:
            return "TP2 PRESO", f"PARZIALIZZA / SPOSTA SL A TP1 ({tp1})"
        if current_price >= tp1:
            return "TP1 PRESO", f"PRENDI PARZIALE TP1 ({tp1}) — SL A BREAKEVEN"
        return "ATTIVO CERTIFICATO", f"MANTIENI LONG — TP1 {tp1} / SL {sl}"
    else:
        if current_price >= sl:
            return "SL PRESO", f"ESCI SUBITO: STOP LOSS TOCCATO ({sl})"
        if current_price <= tp3:
            return "TP3 PRESO", f"ESCI / INCASSA COMPLETO A TP3 ({tp3})"
        if current_price <= tp2:
            return "TP2 PRESO", f"PARZIALIZZA / SPOSTA SL A TP1 ({tp1})"
        if current_price <= tp1:
            return "TP1 PRESO", f"PRENDI PARZIALE TP1 ({tp1}) — SL A BREAKEVEN"
        return "ATTIVO CERTIFICATO", f"MANTIENI SHORT — TP1 {tp1} / SL {sl}"


def build_signal_room(rows: list, interval: str, leverage: int) -> list:
    now_ts = int(time.time())
    now_str = time.strftime("%H:%M:%S")
    timer_meta = candle_timer_info(interval, now_ts)

    for r in rows:
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
                "status": "ATTIVO CERTIFICATO"
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


@app.route("/api/score-params", methods=["POST"])
def api_score_params():
    data = request.json or {}
    for k in ("min_score", "w_ema", "w_rsi", "w_zscore", "w_obi"):
        if k in data:
            try:
                val = float(data[k])
                if k == "min_score":
                    SCORE_PARAMS[k] = max(10.0, min(95.0, val))
                else:
                    SCORE_PARAMS[k] = max(0.0, min(80.0, val))
            except (TypeError, ValueError):
                pass
    return jsonify({"status": "ok", "score_params": SCORE_PARAMS})


@app.route("/api/vip-scan")
def api_vip_scan():
    tf = request.args.get("tf", "5m")
    if tf not in TF_SECONDS:
        tf = "5m"

    req_cfg = dict(SCORE_PARAMS)
    for k in ("min_score", "w_ema", "w_rsi", "w_zscore", "w_obi"):
        if k in request.args:
            try:
                val = float(request.args.get(k))
                if k == "min_score":
                    SCORE_PARAMS[k] = max(10.0, min(95.0, val))
                else:
                    SCORE_PARAMS[k] = max(0.0, min(80.0, val))
                req_cfg[k] = SCORE_PARAMS[k]
            except (TypeError, ValueError):
                pass

    now_ts = int(time.time())
    api_key, secret = _creds()
    account, positions = fetch_account_and_positions(api_key, secret)
    signals = scan_bingx_market(tf, req_cfg)
    room_signals = build_signal_room(signals, tf, account.get("leverage", 20))
    return jsonify({
        "account": account,
        "positions": positions,
        "signals": signals,
        "signal_room": room_signals,
        "candle_timer": candle_timer_info(tf, now_ts),
        "score_params": req_cfg,
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

    tf_check = data.get("tf", "5m")
    live_rows = scan_bingx_market(tf_check if tf_check in TF_SECONDS else "5m", SCORE_PARAMS)
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
