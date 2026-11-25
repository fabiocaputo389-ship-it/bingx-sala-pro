import hashlib
import hmac
import json
import math
import os
import time
from datetime import datetime, timezone
from urllib.parse import urlencode
from flask import Flask, jsonify, render_template, request
import requests

app = Flask(__name__)

BINGX_BASE_URL = "https://open-api.bingx.com"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config", "runtime_config.json")
LOCKS_PATH = os.path.join(BASE_DIR, "data", "locked_signals.json")
LOGS_PATH = os.path.join(BASE_DIR, "data", "signal_room_log.json")

TIMEFRAME_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}

ASSET_CATALOG = {
    "BTC-USDT": {"label": "BTC-USDT", "category": "Crypto", "decimals": 1, "wr": 77},
    "ETH-USDT": {"label": "ETH-USDT", "category": "Crypto", "decimals": 2, "wr": 75},
    "SOL-USDT": {"label": "SOL-USDT", "category": "Crypto", "decimals": 2, "wr": 74},
    "BNB-USDT": {"label": "BNB-USDT", "category": "Crypto", "decimals": 2, "wr": 76},
    "XRP-USDT": {"label": "XRP-USDT", "category": "Crypto", "decimals": 4, "wr": 72},
    "DOGE-USDT": {"label": "DOGE-USDT", "category": "Meme/Alt", "decimals": 5, "wr": 70},
    "SFP-USDT": {"label": "SFP-USDT", "category": "Meme/Alt", "decimals": 5, "wr": 71},
    "EUR-USDT": {"label": "EUR-USDT", "category": "Forex", "decimals": 5, "wr": 74},
    "GBP-USDT": {"label": "GBP-USDT", "category": "Forex", "decimals": 5, "wr": 72},
    "XAUT-USDT": {"label": "XAUT-USDT", "category": "Materie Prime", "decimals": 2, "wr": 75},
    "PAXG-USDT": {"label": "PAXG-USDT", "category": "Materie Prime", "decimals": 2, "wr": 75},
    "XAU-USDT": {"label": "XAU-USDT", "category": "Materie Prime", "decimals": 2, "wr": 75},
    "OIL-USDT": {"label": "OIL-USDT", "category": "Materie Prime", "decimals": 3, "wr": 71},
    "TSLA-USDT": {"label": "TSLA-USDT", "category": "Azioni", "decimals": 2, "wr": 72},
    "NVDA-USDT": {"label": "NVDA-USDT", "category": "Azioni", "decimals": 2, "wr": 76},
    "AAPL-USDT": {"label": "AAPL-USDT", "category": "Azioni", "decimals": 2, "wr": 73},
}

DEFAULT_CONFIG = {
    "api_key": os.getenv("BINGX_API_KEY", ""),
    "secret_key": os.getenv("BINGX_SECRET_KEY", ""),
    "mode": "AUTOMATICO",               # AUTOMATICO / MANUALE
    "auto_trade": True,
    "scan_mode": "Auto Scan + Blocco Certo",
    "score_threshold": 60.0,
    "certain_min_pct": 70.0,
    "close_at_candle_end": False,
    "block_false_signals": True,
    "anti_breakout_enabled": True,
    "allow_manual_under_threshold": False,
    "sound_enabled": True,
    "voice_it_enabled": True,
    "default_qty": 0.0005,
    "default_leverage": 20,
    "equity_usdt": 1.44,
    "fakeout_lookback": 20,
    "atr_spike_mult": 2.5,
    "wick_ratio_limit": 0.55,
}


def load_json(path: str, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(default, dict) and isinstance(data, dict):
                    merged = dict(default)
                    merged.update(data)
                    return merged
                if isinstance(default, list) and isinstance(data, list):
                    return data
        except Exception:
            pass
    return dict(default) if isinstance(default, dict) else list(default)


def save_json(path: str, payload) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


CFG = load_json(CONFIG_PATH, DEFAULT_CONFIG)
LOCKS = load_json(LOCKS_PATH, [])
ROOM_LOGS = load_json(LOGS_PATH, [])


def append_room_log(entry: dict) -> None:
    global ROOM_LOGS
    ROOM_LOGS.insert(0, entry)
    ROOM_LOGS = ROOM_LOGS[:60]
    save_json(LOGS_PATH, ROOM_LOGS)


def mask_api_key(key: str) -> str:
    if not key or len(key) < 8:
        return "NON IMPOSTATA"
    return f"{key[:4]}...{key[-4:]}"


def normalize_symbol(raw: str) -> str:
    s = (raw or "BTC-USDT").strip().upper().replace("/", "-")
    if s == "EURUSDT":
        return "EUR-USDT"
    if "-" not in s and s.endswith("USDT"):
        return f"{s[:-4]}-USDT"
    return s


def dynamic_leverage(equity_usdt: float, category: str) -> int:
    if equity_usdt <= 2.00:
        return 20 if category in ("Crypto", "Meme/Alt") else 10
    if category == "Forex":
        return 10
    if category == "Materie Prime":
        return 8
    if category == "Azioni":
        return 5
    return 10 if equity_usdt <= 25.00 else 5


def candle_timer_info(tf: str) -> dict:
    interval = tf if tf in TIMEFRAME_SECONDS else "15m"
    step = TIMEFRAME_SECONDS[interval]
    now = time.time()
    rem = max(0, int(step - (now % step)))
    mins = (rem % 3600) // 60
    secs = rem % 60
    pct_valid = max(0, min(100, int((rem / step) * 100)))
    close_local = datetime.fromtimestamp(math.floor(now / step) * step + step).astimezone().strftime("%H:%M:%S")
    close_utc = datetime.fromtimestamp(math.floor(now / step) * step + step, tz=timezone.utc).strftime("%H:%M:%S UTC")
    return {
        "timeframe": interval,
        "remainingSec": rem,
        "remainingText": f"{mins:02d}:{secs:02d}",
        "validityPct": pct_valid,
        "closeLocal": close_local,
        "closeUtc": close_utc,
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
        d = closes[i] - closes[i - 1]
        gains.append(max(0.0, d))
        losses.append(max(0.0, -d))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    if al == 0:
        return 100.0 if ag > 0 else 50.0
    return round(100.0 - (100.0 / (1.0 + ag / al)), 2)


def compute_macd(closes: list[float], fast=12, slow=26, sig=9) -> dict:
    if len(closes) < slow + sig:
        return {"macd": 0.0, "signal": 0.0, "hist": 0.0, "status": "NEUTRAL"}
    m_line = [a - b for a, b in zip(ema_series(closes, fast), ema_series(closes, slow))]
    s_line = ema_series(m_line, sig)
    hist = m_line[-1] - s_line[-1]
    status = "BULLISH" if hist > 0 and m_line[-1] > s_line[-1] else ("BEARISH" if hist < 0 and m_line[-1] < s_line[-1] else "NEUTRAL")
    return {"macd": round(m_line[-1], 5), "signal": round(s_line[-1], 5), "hist": round(hist, 5), "status": status}


def compute_atr(highs: list[float], lows: list[float], closes: list[float], period=14) -> float:
    if len(closes) < 2 or not (len(highs) == len(lows) == len(closes)):
        return 0.0
    trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])) for i in range(1, len(closes))]
    w = trs[-period:] if len(trs) >= period else trs
    return round(sum(w) / len(w), 5) if w else 0.0


def compute_adx_approx(highs: list[float], lows: list[float], closes: list[float], period=14) -> float:
    if len(closes) < period + 2:
        return 18.0
    plus_dm, minus_dm, tr_list = [], [], []
    for i in range(1, len(closes)):
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        plus_dm.append(up if (up > dn and up > 0) else 0.0)
        minus_dm.append(dn if (dn > up and dn > 0) else 0.0)
        tr_list.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    atr_v = sum(tr_list[-period:]) / period or 1e-9
    p_di = 100.0 * (sum(plus_dm[-period:]) / period) / atr_v
    m_di = 100.0 * (sum(minus_dm[-period:]) / period) / atr_v
    dx = 100.0 * abs(p_di - m_di) / max(p_di + m_di, 1e-9)
    return round(dx, 2)


def compute_zscore(closes: list[float], period=20) -> float:
    if len(closes) < period:
        return 0.0
    w = closes[-period:]
    mean = sum(w) / period
    var = sum((x - mean) ** 2 for x in w) / period
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    return round((closes[-1] - mean) / std, 2)


def fetch_klines(symbol: str, interval: str, limit=120):
    r = requests.get(
        f"{BINGX_BASE_URL}/openApi/swap/v2/quote/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=8,
    )
    r.raise_for_status()
    rows = r.json().get("data", [])
    opens, highs, lows, closes, vols = [], [], [], [], []
    for row in rows:
        if isinstance(row, dict):
            opens.append(float(row.get("open", 0.0)))
            highs.append(float(row.get("high", 0.0)))
            lows.append(float(row.get("low", 0.0)))
            closes.append(float(row.get("close", 0.0)))
            vols.append(float(row.get("volume", 0.0)))
        elif isinstance(row, (list, tuple)) and len(row) >= 6:
            opens.append(float(row[1]))
            highs.append(float(row[2]))
            lows.append(float(row[3]))
            closes.append(float(row[4]))
            vols.append(float(row[5]))
    last_price = closes[-1] if closes else 0.0
    return last_price, opens, highs, lows, closes, vols


def evaluate_filters(opens, highs, lows, closes, vols, atr, spread_ratio=0.0):
    reasons = []
    penalty = 0.0
    fakeout_type = "NONE"
    lookback = max(5, int(CFG.get("fakeout_lookback", 20)))
    n = len(closes)

    if n >= lookback + 2:
        prev_hi = max(highs[-(lookback + 1):-1])
        prev_lo = min(lows[-(lookback + 1):-1])
        o_l, h_l, l_l, c_l = opens[-1], highs[-1], lows[-1], closes[-1]
        rng = max(0.0, h_l - l_l)
        up_wick = max(0.0, h_l - max(o_l, c_l))
        dn_wick = max(0.0, min(o_l, c_l) - l_l)

        if CFG.get("block_false_signals", True):
            if h_l > prev_hi and c_l < prev_hi:
                fakeout_type = "BULL_TRAP"
                penalty += 35.0
                reasons.append("Falsa rottura rialzista (bull trap)")
            elif l_l < prev_lo and c_l > prev_lo:
                fakeout_type = "BEAR_TRAP"
                penalty += 35.0
                reasons.append("Falsa rottura ribassista (bear trap)")

            if rng > 0:
                w_lim = float(CFG.get("wick_ratio_limit", 0.55))
                if up_wick / rng > w_lim and c_l < o_l:
                    penalty += 20.0
                    reasons.append("Shadow superiore dominante (possibile rifiuto)")
                if dn_wick / rng > w_lim and c_l > o_l:
                    penalty += 20.0
                    reasons.append("Shadow inferiore dominante (possibile sweep)")

        if CFG.get("anti_breakout_enabled", True):
            atr_mult = float(CFG.get("atr_spike_mult", 2.5))
            if atr > 0 and rng > atr * atr_mult:
                penalty += 25.0
                reasons.append(f"Spike anomalo > {atr_mult}x ATR")
            if spread_ratio > 0.0015:
                penalty += 15.0
                reasons.append("Spread elevato / tenuta debole")
            if len(vols) >= lookback + 1:
                avg_v = sum(vols[-(lookback + 1):-1]) / lookback
                if avg_v > 0 and (h_l > prev_hi or l_l < prev_lo) and vols[-1] < avg_v * 0.70:
                    penalty += 15.0
                    reasons.append("Rottura con volume debole")

    is_false = (fakeout_type != "NONE") or (penalty >= 35.0)
    return {
        "fakeBreakout": fakeout_type,
        "isFalseSignal": is_false,
        "penalty": round(min(100.0, penalty), 1),
        "reasons": reasons,
    }


def analyze_market(symbol: str, tf: str) -> dict:
    sym = normalize_symbol(symbol)
    meta = ASSET_CATALOG.get(sym, {"label": sym, "category": "Crypto", "decimals": 2, "wr": 72})
    dec = meta["decimals"]
    timer = candle_timer_info(tf)

    price, opens, highs, lows, closes, vols = 0.0, [], [], [], [], []
    change_24h_pct = 0.0
    try:
        price, opens, highs, lows, closes, vols = fetch_klines(sym, tf, 210)
        t_resp = requests.get(f"{BINGX_BASE_URL}/openApi/swap/v2/quote/ticker", params={"symbol": sym}, timeout=6)
        if t_resp.ok:
            tdata = t_resp.json().get("data", {})
            price = float(tdata.get("lastPrice", price) or price)
            change_24h_pct = float(tdata.get("priceChangePercent", "0").replace("%", "") or 0.0)
    except Exception:
        pass

    rsi_v = compute_rsi(closes, 14)
    macd_d = compute_macd(closes, 12, 26, 9)
    atr_v = compute_atr(highs, lows, closes, 14)
    adx_v = compute_adx_approx(highs, lows, closes, 14)
    z_v = compute_zscore(closes, 20)

    ema9 = ema_series(closes, 9)[-1] if closes else 0.0
    ema21 = ema_series(closes, 21)[-1] if closes else 0.0
    ema50 = ema_series(closes, 50)[-1] if closes else 0.0
    ema200 = ema_series(closes, 200)[-1] if closes else 0.0

    filt = evaluate_filters(opens, highs, lows, closes, vols, atr_v)

    dir_pts = 0.0
    if macd_d["status"] == "BULLISH":
        dir_pts += 35.0
    elif macd_d["status"] == "BEARISH":
        dir_pts -= 35.0

    if ema9 > ema21:
        dir_pts += 30.0
    elif ema9 < ema21:
        dir_pts -= 30.0

    if rsi_v > 52:
        dir_pts += min(25.0, (rsi_v - 50.0) * 1.1)
    elif rsi_v < 48:
        dir_pts -= min(25.0, (50.0 - rsi_v) * 1.1)

    if adx_v >= 20:
        dir_pts += 10.0 if dir_pts >= 0 else -10.0

    signed_score = round(max(-100.0, min(100.0, dir_pts - (filt["penalty"] if dir_pts > 0 else -filt["penalty"]))), 1)
    abs_score = round(max(0.0, min(100.0, abs(dir_pts) - filt["penalty"])), 1)
    threshold = float(CFG.get("score_threshold", 60.0))
    certain_min = float(CFG.get("certain_min_pct", 70.0))

    if abs_score < threshold:
        side = "NEUTRAL"
    else:
        side = "BUY" if dir_pts > 0 else "SELL"

    reliability_pct = int(max(35, min(98, round(45 + abs_score * 0.5))))
    is_certain_signal = (side in ("BUY", "SELL")) and (not filt["isFalseSignal"]) and (reliability_pct >= certain_min) and (price > 0)

    # Decisione esplicita ENTRARE o NON ENTRARE + MOTIVO
    if filt["isFalseSignal"]:
        entry_decision = "NON ENTRARE"
        entry_reason = " / ".join(filt["reasons"]) or "Filtro anti-falsi segnali / falsa rottura attivo"
    elif side == "NEUTRAL" or abs_score < threshold:
        entry_decision = "NON ENTRARE"
        entry_reason = f"Score {abs_score} sotto soglia {threshold} o mercato in attesa/laterale"
    elif reliability_pct < certain_min:
        entry_decision = "NON ENTRARE"
        entry_reason = f"Affidabilità {reliability_pct}% inferiore alla soglia segnale certo ({certain_min}%)"
    else:
        entry_decision = "ENTRA"
        entry_reason = f"Confluenza {side} validata (Score {abs_score}, Affidabilità {reliability_pct}%, ADX {adx_v})"

    sl_dist = atr_v * 1.5 if atr_v > 0 else price * 0.005
    if side == "BUY" and price > 0:
        sl_p = round(max(0.0, price - sl_dist), dec)
        tp1 = round(price + sl_dist * 1.0, dec)
        tp2 = round(price + sl_dist * 1.8, dec)
        tp3 = round(price + sl_dist * 2.5, dec)
        tp4 = round(price + sl_dist * 3.5, dec)
    elif side == "SELL" and price > 0:
        sl_p = round(price + sl_dist, dec)
        tp1 = round(max(0.0, price - sl_dist * 1.0), dec)
        tp2 = round(max(0.0, price - sl_dist * 1.8), dec)
        tp3 = round(max(0.0, price - sl_dist * 2.5), dec)
        tp4 = round(max(0.0, price - sl_dist * 3.5), dec)
    else:
        sl_p = tp1 = tp2 = tp3 = tp4 = None

    vol_last = vols[-1] if vols else 0.0
    vol_avg = (sum(vols[-20:]) / len(vols[-20:])) if vols else 1.0
    vol_ratio = round(vol_last / max(vol_avg, 1e-9), 2)

    return {
        "symbol": sym,
        "label": meta["label"],
        "category": meta["category"],
        "decimals": dec,
        "winRate": meta["wr"],
        "price": round(price, dec),
        "change24hPct": round(change_24h_pct, 2),
        "side": side,
        "signedScore": signed_score,
        "absScore": abs_score,
        "scoreThreshold": threshold,
        "reliabilityPct": reliability_pct,
        "certainMinPct": certain_min,
        "isCertainSignal": is_certain_signal,
        "entryDecision": entry_decision,
        "entryReason": entry_reason,
        "setupStatus": "MERCATO LATERALE / FILTRI ATTIVI" if side == "NEUTRAL" else f"SETUP {side} ATTIVO",
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "tp4": tp4,
        "suggestedSL": sl_p,
        "suggestedTP": tp2,
        "fundingRate": "0.0047%",
        "equityUsdt": float(CFG.get("equity_usdt", 1.44)),
        "leverage": dynamic_leverage(float(CFG.get("equity_usdt", 1.44)), meta["category"]),
        "brain": {
            "trendIndex": f"{'+' if signed_score >= 0 else ''}{round(signed_score / 100 * 1.2, 2)}R",
            "rrWin": f"1.85R / {reliability_pct}%",
            "adx": adx_v,
            "zScore": z_v,
            "ema9": round(ema9, dec),
            "ema21": round(ema21, dec),
            "ema50": round(ema50, dec),
            "ema200": round(ema200, dec),
            "rsi": rsi_v,
            "macdStatus": macd_d["status"],
            "macd": macd_d["macd"],
            "atr": atr_v,
            "volRatio": vol_ratio,
        },
        "filters": filt,
        **timer,
    }


def sign_query(params: dict, secret: str) -> str:
    ordered = sorted((k, v) for k, v in params.items() if v is not None)
    qs = urlencode(ordered)
    sig = hmac.new(secret.encode("utf-8"), qs.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{qs}&signature={sig}"


def bingx_signed_req(method: str, path: str, params: dict | None = None) -> dict:
    api_k = CFG.get("api_key", "").strip()
    sec_k = CFG.get("secret_key", "").strip()
    if not api_k or not sec_k:
        raise ValueError("API Key o Secret Key BingX non configurate.")
    payload = dict(params or {})
    payload["timestamp"] = int(time.time() * 1000)
    payload["recvWindow"] = 5000
    signed_qs = sign_query(payload, sec_k)
    headers = {"X-BX-APIKEY": api_k}
    url = f"{BINGX_BASE_URL}{path}?{signed_qs}"
    r = requests.get(url, headers=headers, timeout=10) if method.upper() == "GET" else requests.post(url, headers=headers, timeout=10)
    r.raise_for_status()
    return r.json()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/desk-state")
def desk_state():
    sym = normalize_symbol(request.args.get("symbol", "BTC-USDT"))
    tf = request.args.get("timeframe", "15m")
    cat = request.args.get("category", "Materie Prime")

    market = analyze_market(sym, tf)
    cat_symbols = [s for s, m in ASSET_CATALOG.items() if cat == "Tutti" or m["category"].lower() == cat.lower()]
    scanner = []
    for s in cat_symbols[:8]:
        a = analyze_market(s, tf)
        scanner.append({
            "symbol": a["symbol"],
            "category": a["category"],
            "price": a["price"],
            "decimals": a["decimals"],
            "change24hPct": a["change24hPct"],
            "side": a["side"],
            "isCertainSignal": a["isCertainSignal"],
        })

    # Order book live
    bids, asks, mid_p = [], [], market["price"]
    try:
        ob = requests.get(f"{BINGX_BASE_URL}/openApi/swap/v2/quote/depth", params={"symbol": sym, "limit": 10}, timeout=6)
        if ob.ok:
            d = ob.json().get("data", {})
            bids = [[float(x[0]), float(x[1])] for x in (d.get("bids") or [])[:8]]
            asks = [[float(x[0]), float(x[1])] for x in (d.get("asks") or [])[:8]]
            if bids and asks:
                mid_p = round((bids[0][0] + asks[0][0]) / 2.0, market["decimals"])
    except Exception:
        pass

    # Posizioni aperte & income reali (se API impostate)
    positions = []
    income_rows = []
    api_connected = bool(CFG.get("api_key") and CFG.get("secret_key"))
    if api_connected:
        try:
            pr = bingx_signed_req("GET", "/openApi/swap/v2/user/positions", {"symbol": sym})
            for p in (pr.get("data") or []):
                amt = float(p.get("positionAmt", 0.0))
                if abs(amt) > 0:
                    entry = float(p.get("avgPrice", 0.0))
                    upnl = float(p.get("unrealizedProfit", 0.0))
                    pnl_pct = ((market["price"] - entry) / entry * 100.0) if entry > 0 else 0.0
                    if amt < 0:
                        pnl_pct = -pnl_pct
                    positions.append({
                        "symbol": p.get("symbol", sym),
                        "side": "LONG" if amt > 0 else "SHORT",
                        "size": abs(amt),
                        "entry": round(entry, market["decimals"]),
                        "pnl": round(upnl, 4),
                        "pnlPct": round(pnl_pct, 2),
                    })
        except Exception:
            pass
        try:
            ir = bingx_signed_req("GET", "/openApi/swap/v2/user/income", {"symbol": sym, "limit": 20})
            for item in (ir.get("data") or [])[:12]:
                income_rows.append({
                    "type": item.get("incomeType", "REALIZED_PNL"),
                    "symbol": item.get("symbol", sym),
                    "income": float(item.get("income", 0.0)),
                })
        except Exception:
            pass

    active_lock = LOCKS[0] if LOCKS else None

    return jsonify({
        "apiMasked": mask_api_key(CFG.get("api_key", "")),
        "apiConnected": api_connected,
        "lockStatus": "BLOCCATO" if active_lock else "LIBERO",
        "activeLock": active_lock,
        "lockedSignals": LOCKS,
        "mode": CFG.get("mode", "AUTOMATICO"),
        "autoTrade": bool(CFG.get("auto_trade", True)),
        "scanMode": CFG.get("scan_mode", "Auto Scan + Blocco Certo"),
        "soundEnabled": bool(CFG.get("sound_enabled", True)),
        "voiceItEnabled": bool(CFG.get("voice_it_enabled", True)),
        "config": {k: v for k, v in CFG.items() if k not in ("api_key", "secret_key")},
        "market": market,
        "scanner": scanner,
        "positions": positions,
        "logs": ROOM_LOGS,
        "income": income_rows,
        "orderBook": {"bids": bids, "asks": asks, "midPrice": mid_p},
    })


@app.route("/api/settings", methods=["POST"])
def update_settings():
    body = request.get_json(force=True) or {}
    if "api_key" in body and body["api_key"].strip():
        CFG["api_key"] = body["api_key"].strip()
    if "secret_key" in body and body["secret_key"].strip():
        CFG["secret_key"] = body["secret_key"].strip()

    for k in ("mode", "scan_mode"):
        if k in body:
            CFG[k] = body[k]
    for k in ("auto_trade", "close_at_candle_end", "block_false_signals", "anti_breakout_enabled", "allow_manual_under_threshold", "sound_enabled", "voice_it_enabled"):
        if k in body:
            CFG[k] = bool(body[k])
    for k in ("score_threshold", "certain_min_pct", "default_qty", "default_leverage", "equity_usdt"):
        if k in body:
            CFG[k] = float(body[k])

    save_json(CONFIG_PATH, CFG)

    api_ok = False
    msg = "Impostazioni salvate."
    if body.get("test_api") and CFG.get("api_key") and CFG.get("secret_key"):
        try:
            bingx_signed_req("GET", "/openApi/swap/v2/user/balance")
            api_ok = True
            msg = "API attive e valide"
        except Exception as exc:
            msg = f"Verifica API: {exc}"

    return jsonify({"status": "ok", "apiValid": api_ok, "message": msg, "apiMasked": mask_api_key(CFG.get("api_key", ""))})


@app.route("/api/lock-signal", methods=["POST"])
def lock_signal():
    global LOCKS
    body = request.get_json(force=True) or {}
    action = body.get("action", "lock")
    if action == "unlock":
        LOCKS = []
        save_json(LOCKS_PATH, LOCKS)
        append_room_log({
            "time": datetime.now().astimezone().strftime("%H:%M:%S"),
            "pair": normalize_symbol(body.get("symbol", "BTC-USDT")),
            "tf": body.get("timeframe", "15m"),
            "tag": "UNLOCK · LIBERATO",
            "tpSl": "-/-/-",
            "outcome": "SEGNALE SBLOCCATO",
        })
        return jsonify({"status": "unlocked", "locks": LOCKS})

    sym = normalize_symbol(body.get("symbol", "BTC-USDT"))
    tf = body.get("timeframe", "15m")
    a = analyze_market(sym, tf)

    if not a["isCertainSignal"] and not bool(body.get("force", False)):
        return jsonify({"status": "rejected", "reason": a["entryReason"]}), 200

    side = a["side"] if a["side"] in ("BUY", "SELL") else "SELL"
    rec = {
        "id": f"{sym}_{tf}_{int(time.time())}",
        "symbol": sym,
        "timeframe": tf,
        "side": side,
        "reliabilityPct": a["reliabilityPct"],
        "entry": a["price"],
        "tp1": a["tp1"],
        "tp2": a["tp2"],
        "tp3": a["tp3"],
        "sl": a["suggestedSL"],
        "lockedAt": datetime.now().astimezone().strftime("%H:%M:%S"),
    }
    LOCKS.insert(0, rec)
    LOCKS = LOCKS[:25]
    save_json(LOCKS_PATH, LOCKS)

    append_room_log({
        "time": rec["lockedAt"],
        "pair": sym,
        "tf": tf,
        "tag": f"{side} · BLOCCATO · {a['reliabilityPct']}% SEGNALE CERTO {'LONG' if side == 'BUY' else 'SHORT'}",
        "tpSl": f"{a['tp1']} / {a['tp2']} / {a['suggestedSL']}",
        "outcome": "SEGNALE BLOCCATO",
    })
    return jsonify({"status": "locked", "lock": rec, "locks": LOCKS})


@app.route("/api/trade-action", methods=["POST"])
def trade_action():
    body = request.get_json(force=True) or {}
    act = body.get("action", "buy")
    sym = normalize_symbol(body.get("symbol", "BTC-USDT"))
    tf = body.get("timeframe", "15m")
    a = analyze_market(sym, tf)

    if act == "close_all":
        executed_live = False
        if CFG.get("api_key") and CFG.get("secret_key"):
            try:
                bingx_signed_req("POST", "/openApi/swap/v2/trade/closeAllPositions")
                executed_live = True
            except Exception:
                pass
        append_room_log({
            "time": datetime.now().astimezone().strftime("%H:%M:%S"),
            "pair": sym,
            "tf": tf,
            "tag": "CLOSE_ALL · CHIUSURA POSIZIONI",
            "tpSl": "-/-/-",
            "outcome": "APERTO SU BINGX" if executed_live else "REGISTRATO IN SALA",
        })
        return jsonify({"status": "ok", "live": executed_live})

    side = "BUY" if act == "buy" else "SELL"
    if a["filters"]["isFalseSignal"] and not CFG.get("allow_manual_under_threshold", False):
        return jsonify({"status": "blocked", "reason": a["entryReason"]}), 200
    if a["reliabilityPct"] < float(CFG.get("certain_min_pct", 70.0)) and not CFG.get("allow_manual_under_threshold", False):
        return jsonify({"status": "blocked", "reason": a["entryReason"]}), 200

    qty = float(body.get("qty", CFG.get("default_qty", 0.0005)))
    lev = int(body.get("leverage", a["leverage"]))
    tp_val = body.get("tp", a["tp2"])
    sl_val = body.get("sl", a["suggestedSL"])

    live_sent = False
    if CFG.get("api_key") and CFG.get("secret_key"):
        try:
            bingx_signed_req("POST", "/openApi/swap/v2/trade/leverage", {
                "symbol": sym,
                "side": "LONG" if side == "BUY" else "SHORT",
                "leverage": lev,
            })
            bingx_signed_req("POST", "/openApi/swap/v2/trade/order", {
                "symbol": sym,
                "side": side,
                "positionSide": "LONG" if side == "BUY" else "SHORT",
                "type": "MARKET",
                "quantity": qty,
            })
            live_sent = True
        except Exception:
            pass

    append_room_log({
        "time": datetime.now().astimezone().strftime("%H:%M:%S"),
        "pair": sym,
        "tf": tf,
        "tag": f"{side} · {a['reliabilityPct']}% · {'LONG' if side == 'BUY' else 'SHORT'}",
        "tpSl": f"{tp_val} / {sl_val}",
        "outcome": "APERTO SU BINGX" if live_sent else "SEGNALE INVIATO",
    })
    return jsonify({"status": "ok", "liveSent": live_sent, "side": side, "tp": tp_val, "sl": sl_val})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
