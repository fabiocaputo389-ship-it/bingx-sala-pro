import os
import time
import json
import hmac
import hashlib
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

import numpy as np
import requests
from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__, static_folder=".", static_url_path="")

BINGX_BASE_URL = "https://open-api.bingx.com"
ROME_TZ = ZoneInfo("Europe/Rome")
KEYS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bingx_keys.json")

STATE_LOCK = threading.Lock()

API_KEYS = {
    "api_key": os.environ.get("BINGX_API_KEY", "").strip(),
    "secret_key": os.environ.get("BINGX_SECRET_KEY", "").strip(),
}

BRAIN_WEIGHTS = {
    "ema_weight": 35.0,
    "momentum_weight": 30.0,
    "volatility_weight": 20.0,
    "sentiment_weight": 15.0,
    "min_confidence": 70.0,
    "account_capital": 0.0,
    "risk_per_trade_pct": 1.5,
    "max_daily_loss_usdt": 1.0,
    "sl_mult": 1.4,
    "tp1_mult": 1.8,
    "tp2_mult": 3.2,
    "tp3_mult": 4.8,
    "breakeven_at_tp_pct": 50.0,
    "auto_profile_enabled": True,
}

AUTO_MONITOR = {
    "enabled": True,
    "auto_trade_enabled": False,
    "interval": "15m",
    "active_category": "ALL",
    "last_run": None,
    "signals": {},
    "trade_logs": [],
    "audio_alerts": [],
    "open_positions": {},
    "breakeven_done": set(),
    "auto_traded_keys": set(),
    "balance_info": {
        "balance_usdt": 0.0,
        "available_usdt": 0.0,
        "unrealized_pnl_usdt": 0.0,
        "last_sync": None,
        "source": "init",
    },
}

MARKETS_CACHE = {
    "data": [],
    "by_symbol": {},
    "last_fetch": 0.0,
}

KLINES_CACHE = {}
KLINES_TTL = 8.0


def _mask_key(val: str) -> str:
    if not val:
        return ""
    if len(val) <= 8:
        return "*" * len(val)
    return f"{val[:4]}...{val[-4:]}"


def load_saved_keys_on_startup():
    if os.path.exists(KEYS_FILE):
        try:
            with open(KEYS_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            ak = str(saved.get("api_key", "")).strip()
            sk = str(saved.get("secret_key", "")).strip()
            if ak and sk:
                with STATE_LOCK:
                    API_KEYS["api_key"] = ak
                    API_KEYS["secret_key"] = sk
        except Exception as e:
            print("Errore caricamento chiavi salvate:", e)


def persist_keys_to_disk(ak: str, sk: str):
    with open(KEYS_FILE, "w", encoding="utf-8") as f:
        json.dump({"api_key": ak, "secret_key": sk}, f, indent=2)


def classify_market(symbol: str) -> str:
    s = symbol.upper()
    majors = {
        "BTC-USDT", "ETH-USDT", "BNB-USDT", "SOL-USDT", "XRP-USDT",
        "ADA-USDT", "DOGE-USDT", "AVAX-USDT", "LINK-USDT", "TON-USDT", "TRX-USDT"
    }
    metals_comm = {
        "XAU-USDT", "GOLD-USDT", "XAG-USDT", "SILVER-USDT",
        "WTI-USDT", "OIL-USDT", "BRENT-USDT", "NATGAS-USDT", "XPT-USDT", "XPD-USDT", "COPPER-USDT"
    }
    forex = {
        "EUR-USDT", "GBP-USDT", "USD-JPY-USDT", "AUD-USDT", "CAD-USDT", "CHF-USDT",
        "EURUSD-USDT", "GBPUSD-USDT", "USDJPY-USDT"
    }
    indices_stocks = {
        "NAS100-USDT", "NASDAQ100-USDT", "US30-USDT", "SP500-USDT", "GER40-USDT",
        "NVDA-USDT", "TSLA-USDT", "AAPL-USDT", "MSFT-USDT", "META-USDT", "AMZN-USDT",
        "GOOGL-USDT", "SOXL-USDT", "GS-USDT", "COIN-USDT", "MSTR-USDT"
    }
    if s in majors:
        return "CRYPTO MAJOR"
    if s in metals_comm or any(k in s for k in ["XAU", "GOLD", "XAG", "SILVER", "OIL", "WTI", "BRENT"]):
        return "METALLI & COMMODITIES"
    if s in forex:
        return "FOREX & VALUTE"
    if s in indices_stocks or any(k in s for k in ["NAS100", "NASDAQ", "US30", "SP500", "SOXL", "NVDA", "TSLA"]):
        return "INDICI & AZIONI"
    return "ALTCOIN & DEFI"


def format_bingx_display_name(symbol: str, category: str) -> str:
    clean = symbol.replace("-USDT", "").replace("-", "")
    mapping = {
        "XAU": "GOLD (XAU)",
        "GOLD": "GOLD (XAU)",
        "XAG": "SILVER (XAG)",
        "SILVER": "SILVER (XAG)",
        "WTI": "OIL WTI",
        "OIL": "OIL WTI",
        "BRENT": "OIL BRENT",
        "NASDAQ100": "NASDAQ100",
        "NAS100": "NASDAQ100",
    }
    base = mapping.get(clean, clean)
    return f"{base}USDT"


def get_market_session_info(symbol: str, category: str) -> dict:
    now_rome = datetime.now(ROME_TZ)
    hour_float = now_rome.hour + now_rome.minute / 60.0
    weekday = now_rome.weekday()
    rome_str = now_rome.strftime("%H:%M CET")

    if category in ("CRYPTO MAJOR", "ALTCOIN & DEFI"):
        sess_name = "Notte/Asia" if hour_float < 8 else ("Sessione Europea" if hour_float < 14.5 else ("Overlap US/EU" if hour_float < 17 else "Sessione USA"))
        return {
            "market_type": "Crypto 24/7",
            "session_name": sess_name,
            "hours_window": "00:00-24:00 (24/7)",
            "is_open": True,
            "badge": f"🟢 24/7 • {sess_name.upper()}",
            "rome_time": rome_str,
        }

    is_weekend = weekday >= 5
    if category == "METALLI & COMMODITIES":
        open_now = (not is_weekend) and not (weekday == 4 and hour_float >= 23.0) and not (weekday == 6 and hour_float < 0.0)
        return {
            "market_type": "Commodities / Metalli",
            "session_name": "Sessione Globale / NY-LDN",
            "hours_window": "00:00-23:00 (Lun-Ven)",
            "is_open": open_now,
            "badge": "🟢 APERTO" if open_now else "🔴 CHIUSO / PAUSA",
            "rome_time": rome_str,
        }

    if category == "FOREX & VALUTE":
        open_now = (not is_weekend) and not (weekday == 4 and hour_float >= 23.0)
        return {
            "market_type": "Forex",
            "session_name": "Mercato FX 24/5",
            "hours_window": "00:00-23:00 (Lun-Ven)",
            "is_open": open_now,
            "badge": "🟢 FX ATTIVO" if open_now else "🔴 WEEKEND FX",
            "rome_time": rome_str,
        }

    open_now = (not is_weekend) and (15.5 <= hour_float <= 22.0)
    return {
        "market_type": "Indici & Azioni",
        "session_name": "Wall Street Cash" if open_now else "Pre/Post Market",
        "hours_window": "15:30-22:00 CET",
        "is_open": open_now,
        "badge": "🟢 WALL STREET" if open_now else "🟡 FUORI ORARIO",
        "rome_time": rome_str,
    }


def get_auto_profile(category: str, interval: str) -> dict:
    tf_map = {
        "1m": {"sl_mult": 1.15, "tp1_mult": 1.5, "tp2_mult": 2.5, "tp3_mult": 3.8, "min_conf": 68.0, "tf_label": "1m (2-4 candele)"},
        "5m": {"sl_mult": 1.30, "tp1_mult": 1.7, "tp2_mult": 3.0, "tp3_mult": 4.5, "min_conf": 69.0, "tf_label": "5m (2-3 candele)"},
        "15m": {"sl_mult": 1.40, "tp1_mult": 1.8, "tp2_mult": 3.2, "tp3_mult": 4.8, "min_conf": 70.0, "tf_label": "15m (2-3 candele)"},
        "1h": {"sl_mult": 1.55, "tp1_mult": 2.0, "tp2_mult": 3.5, "tp3_mult": 5.2, "min_conf": 71.0, "tf_label": "1h (2 candele)"},
        "4h": {"sl_mult": 1.75, "tp1_mult": 2.2, "tp2_mult": 3.8, "tp3_mult": 5.8, "min_conf": 72.0, "tf_label": "4h (1-2 candele)"},
    }
    base = tf_map.get(interval, tf_map["15m"]).copy()

    cat_adj = {
        "CRYPTO MAJOR": (35.0, 30.0, 20.0, 15.0, 0.0, "Crypto Major"),
        "ALTCOIN & DEFI": (30.0, 32.0, 22.0, 16.0, 0.15, "Altcoin & DeFi"),
        "METALLI & COMMODITIES": (38.0, 28.0, 22.0, 12.0, 0.10, "Metalli & Commodities"),
        "FOREX & VALUTE": (40.0, 30.0, 18.0, 12.0, -0.10, "Forex & Valute"),
        "INDICI & AZIONI": (38.0, 32.0, 20.0, 10.0, 0.05, "Indici & Azioni"),
    }
    w_ema, w_mom, w_vol, w_sent, mult_adj, cat_label = cat_adj.get(
        category, (35.0, 30.0, 20.0, 15.0, 0.0, category)
    )
    return {
        "profile_name": f"{interval} {cat_label}",
        "ema_weight": w_ema,
        "momentum_weight": w_mom,
        "volatility_weight": w_vol,
        "sentiment_weight": w_sent,
        "min_confidence": base["min_conf"],
        "sl_mult": round(base["sl_mult"] + mult_adj, 2),
        "tp1_mult": round(base["tp1_mult"] + mult_adj, 2),
        "tp2_mult": round(base["tp2_mult"] + mult_adj * 1.5, 2),
        "tp3_mult": round(base["tp3_mult"] + mult_adj * 2.0, 2),
        "tp_label": base["tf_label"],
    }


def _sign_bingx_query(params: dict, secret_key: str) -> str:
    sorted_items = sorted((k, v) for k, v in params.items() if v is not None and v != "")
    query_str = urlencode(sorted_items)
    signature = hmac.new(secret_key.encode("utf-8"), query_str.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{query_str}&signature={signature}"


def bingx_signed_request(method: str, path: str, params: dict = None):
    with STATE_LOCK:
        api_key = API_KEYS["api_key"]
        secret_key = API_KEYS["secret_key"]
    if not api_key or not secret_key:
        return {"code": -1, "msg": "Chiavi API BingX non configurate"}

    req_params = dict(params or {})
    req_params["timestamp"] = int(time.time() * 1000)
    req_params.setdefault("recvWindow", 10000)
    signed_qs = _sign_bingx_query(req_params, secret_key)
    url = f"{BINGX_BASE_URL}{path}?{signed_qs}"
    headers = {"X-BX-APIKEY": api_key}
    try:
        if method.upper() == "GET":
            resp = requests.get(url, headers=headers, timeout=10)
        elif method.upper() == "DELETE":
            resp = requests.delete(url, headers=headers, timeout=10)
        else:
            resp = requests.post(url, headers=headers, timeout=10)
        return resp.json()
    except Exception as e:
        return {"code": -1, "msg": str(e)}


def fetch_bingx_real_balance() -> dict:
    with STATE_LOCK:
        has_keys = bool(API_KEYS["api_key"] and API_KEYS["secret_key"])
        prev = dict(AUTO_MONITOR["balance_info"])

    now_str = datetime.now(ROME_TZ).strftime("%H:%M:%S")
    if not has_keys:
        return {
            "ok": False,
            "balance_usdt": 0.0,
            "available_usdt": 0.0,
            "unrealized_pnl_usdt": 0.0,
            "last_sync": now_str,
            "msg": "Chiavi API non impostate",
        }

    res = bingx_signed_request("GET", "/openApi/swap/v2/user/balance")
    if isinstance(res, dict) and res.get("code") == 0:
        data = res.get("data", {})
        bal_obj = data.get("balance", data) if isinstance(data, dict) else {}
        if isinstance(bal_obj, list):
            usdt_item = next((x for x in bal_obj if str(x.get("asset", "")).upper() == "USDT"), {})
        else:
            usdt_item = bal_obj if isinstance(bal_obj, dict) else {}

        eq = float(usdt_item.get("equity", usdt_item.get("balance", 0.0)) or 0.0)
        avail = float(usdt_item.get("availableMargin", usdt_item.get("availableBalance", eq)) or 0.0)
        upnl = float(usdt_item.get("unrealizedProfit", 0.0) or 0.0)

        info = {
            "ok": True,
            "balance_usdt": round(eq, 4),
            "available_usdt": round(avail, 4),
            "unrealized_pnl_usdt": round(upnl, 4),
            "last_sync": now_str,
            "msg": "OK",
        }
        with STATE_LOCK:
            AUTO_MONITOR["balance_info"] = info
            if eq > 0:
                BRAIN_WEIGHTS["account_capital"] = round(eq, 4)
        return info

    prev["ok"] = False
    prev["last_sync"] = now_str
    prev["msg"] = res.get("msg", "Errore saldo BingX") if isinstance(res, dict) else "Errore saldo BingX"
    return prev


def fetch_bingx_open_positions() -> dict:
    with STATE_LOCK:
        has_keys = bool(API_KEYS["api_key"] and API_KEYS["secret_key"])
        signals_snapshot = dict(AUTO_MONITOR["signals"])
        be_done = set(AUTO_MONITOR["breakeven_done"])

    if not has_keys:
        return {}

    res = bingx_signed_request("GET", "/openApi/swap/v2/user/positions")
    if not isinstance(res, dict) or res.get("code") != 0:
        return {}

    raw_list = res.get("data", [])
    if isinstance(raw_list, dict):
        raw_list = raw_list.get("positions", [])
    if not isinstance(raw_list, list):
        return {}

    positions = {}
    for p in raw_list:
        amt = float(p.get("positionAmt", 0.0) or 0.0)
        if abs(amt) <= 0:
            continue
        sym = str(p.get("symbol", "")).upper()
        side_raw = str(p.get("positionSide", "")).upper()
        if side_raw in ("LONG", "SHORT"):
            side = side_raw
        else:
            side = "LONG" if amt > 0 else "SHORT"
        size = abs(amt)
        entry_price = float(p.get("avgPrice", p.get("entryPrice", 0.0)) or 0.0)
        mark_price = float(p.get("markPrice", entry_price) or entry_price)
        upnl = float(p.get("unrealizedProfit", 0.0) or 0.0)
        lev = int(float(p.get("leverage", 10) or 10))

        sig_data = signals_snapshot.get(sym, {})
        tp2 = float(sig_data.get("levels", {}).get("tp2", 0.0) or 0.0)
        sl = float(sig_data.get("levels", {}).get("sl", 0.0) or 0.0)

        tp_pct = 0.0
        if tp2 > 0 and entry_price > 0 and abs(tp2 - entry_price) > 1e-12:
            if side == "LONG":
                tp_pct = ((mark_price - entry_price) / (tp2 - entry_price)) * 100.0
            else:
                tp_pct = ((entry_price - mark_price) / (entry_price - tp2)) * 100.0
        tp_pct = round(max(0.0, min(100.0, tp_pct)), 1)

        with STATE_LOCK:
            be_target = float(BRAIN_WEIGHTS.get("breakeven_at_tp_pct", 50.0))
        pos_key = f"{sym}_{side}"
        if tp_pct >= be_target and pos_key not in be_done and entry_price > 0:
            be_res = bingx_signed_request("POST", "/openApi/swap/v2/trade/order", {
                "symbol": sym,
                "side": "SELL" if side == "LONG" else "BUY",
                "positionSide": side,
                "type": "STOP_MARKET",
                "stopPrice": round(entry_price, 6),
                "closePosition": "true",
            })
            if isinstance(be_res, dict) and be_res.get("code") == 0:
                with STATE_LOCK:
                    AUTO_MONITOR["breakeven_done"].add(pos_key)
                    AUTO_MONITOR["trade_logs"].insert(0, {
                        "time": datetime.now(ROME_TZ).strftime("%H:%M:%S"),
                        "symbol": sym,
                        "side": f"BE-{side}",
                        "qty": size,
                        "tp": tp2,
                        "sl": round(entry_price, 6),
                        "response": be_res,
                    })

        positions[sym] = {
            "symbol": sym,
            "side": side,
            "size": size,
            "entry_price": round(entry_price, 6),
            "mark_price": round(mark_price, 6),
            "unrealized_pnl": round(upnl, 4),
            "leverage": lev,
            "tp2": tp2,
            "sl": sl,
            "tp_progress_pct": tp_pct,
        }

    with STATE_LOCK:
        AUTO_MONITOR["open_positions"] = positions
    return positions


def load_markets_from_bingx(force: bool = False) -> list:
    now = time.time()
    with STATE_LOCK:
        if not force and MARKETS_CACHE["data"] and (now - MARKETS_CACHE["last_fetch"] < 120):
            return MARKETS_CACHE["data"]

    markets = []
    try:
        resp = requests.get(f"{BINGX_BASE_URL}/openApi/swap/v2/quote/contracts", timeout=10)
        raw = resp.json()
        contracts = raw.get("data", []) if isinstance(raw, dict) else []
    except Exception:
        contracts = []

    tickers_map = {}
    try:
        t_resp = requests.get(f"{BINGX_BASE_URL}/openApi/swap/v2/quote/ticker", timeout=10)
        t_raw = t_resp.json()
        t_list = t_raw.get("data", []) if isinstance(t_raw, dict) else []
        if isinstance(t_list, list):
            for item in t_list:
                sym = str(item.get("symbol", "")).upper()
                if sym:
                    tickers_map[sym] = item
    except Exception:
        pass

    for c in contracts:
        sym = str(c.get("symbol", "")).upper()
        if not sym or not sym.endswith("-USDT"):
            continue
        if str(c.get("status", "1")) not in ("1", "ONLINE", "TRADING", ""):
            continue
        cat = classify_market(sym)
        b_name = format_bingx_display_name(sym, cat)
        t_info = tickers_map.get(sym, {})
        last_p = float(t_info.get("lastPrice", c.get("lastPrice", 0.0)) or 0.0)
        chg_pct = float(t_info.get("priceChangePercent", 0.0) or 0.0)
        vol_24h = float(t_info.get("quoteVolume", t_info.get("volume", 0.0)) or 0.0)
        max_lev = int(float(c.get("maxLongLeverage", c.get("maxLeverage", 50)) or 50))
        sess = get_market_session_info(sym, cat)

        markets.append({
            "symbol": sym,
            "bingxName": b_name,
            "category": cat,
            "lastPrice": last_p,
            "priceChangePercent": chg_pct,
            "quoteVolume": vol_24h,
            "maxLeverage": max_lev,
            "session": sess,
        })

    if not markets:
        fallback_syms = [
            ("BTC-USDT", "CRYPTO MAJOR", 92000.0, 50),
            ("ETH-USDT", "CRYPTO MAJOR", 3150.0, 50),
            ("SOL-USDT", "CRYPTO MAJOR", 185.0, 50),
            ("XRP-USDT", "CRYPTO MAJOR", 1.45, 50),
            ("INJ-USDT", "ALTCOIN & DEFI", 5.95, 50),
            ("ADA-USDT", "ALTCOIN & DEFI", 0.145, 50),
            ("XAU-USDT", "METALLI & COMMODITIES", 2750.0, 50),
            ("XAG-USDT", "METALLI & COMMODITIES", 31.5, 50),
            ("EUR-USDT", "FOREX & VALUTE", 1.085, 50),
            ("NAS100-USDT", "INDICI & AZIONI", 20500.0, 50),
        ]
        for sym, cat, px, lev in fallback_syms:
            markets.append({
                "symbol": sym,
                "bingxName": format_bingx_display_name(sym, cat),
                "category": cat,
                "lastPrice": px,
                "priceChangePercent": 0.0,
                "quoteVolume": 1000000.0,
                "maxLeverage": lev,
                "session": get_market_session_info(sym, cat),
            })

    priority_order = {"CRYPTO MAJOR": 0, "METALLI & COMMODITIES": 1, "INDICI & AZIONI": 2, "FOREX & VALUTE": 3, "ALTCOIN & DEFI": 4}
    markets.sort(key=lambda x: (priority_order.get(x["category"], 9), -x["quoteVolume"]))

    by_sym = {m["symbol"]: m for m in markets}
    with STATE_LOCK:
        MARKETS_CACHE["data"] = markets
        MARKETS_CACHE["by_symbol"] = by_sym
        MARKETS_CACHE["last_fetch"] = now
    return markets


def _generate_synthetic_klines(base_price: float, count: int = 240):
    rng = np.random.default_rng(42)
    rets = rng.normal(0.0, 0.004, size=count)
    closes = base_price * np.cumprod(1.0 + rets)
    opens = np.roll(closes, 1)
    opens[0] = closes[0] * 0.999
    highs = np.maximum(opens, closes) * (1.0 + np.abs(rng.normal(0.0015, 0.001, size=count)))
    lows = np.minimum(opens, closes) * (1.0 - np.abs(rng.normal(0.0015, 0.001, size=count)))
    volumes = rng.uniform(50000, 250000, size=count)
    return opens, highs, lows, closes, volumes


def fetch_klines(symbol: str, interval: str = "15m", limit: int = 240):
    cache_key = f"{symbol}_{interval}"
    now = time.time()
    with STATE_LOCK:
        cached = KLINES_CACHE.get(cache_key)
        if cached and (now - cached["ts"] < KLINES_TTL):
            return cached["arrays"]

    tf_map = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h"}
    bx_interval = tf_map.get(interval, "15m")

    try:
        resp = requests.get(
            f"{BINGX_BASE_URL}/openApi/swap/v3/quote/klines",
            params={"symbol": symbol, "interval": bx_interval, "limit": limit},
            timeout=8,
        )
        raw = resp.json()
        rows = raw.get("data", []) if isinstance(raw, dict) else []
        if isinstance(rows, list) and len(rows) >= 60:
            opens, highs, lows, closes, volumes = [], [], [], [], []
            for r in rows:
                if isinstance(r, dict):
                    o = float(r.get("open", 0))
                    h = float(r.get("high", 0))
                    l = float(r.get("low", 0))
                    c = float(r.get("close", 0))
                    v = float(r.get("volume", 0))
                else:
                    o, h, l, c, v = float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])
                if c > 0:
                    opens.append(o)
                    highs.append(h)
                    lows.append(l)
                    closes.append(c)
                    volumes.append(max(v, 1.0))
            if len(closes) >= 60:
                arrs = (
                    np.array(opens, dtype=float),
                    np.array(highs, dtype=float),
                    np.array(lows, dtype=float),
                    np.array(closes, dtype=float),
                    np.array(volumes, dtype=float),
                )
                with STATE_LOCK:
                    KLINES_CACHE[cache_key] = {"ts": now, "arrays": arrs}
                return arrs
    except Exception:
        pass

    with STATE_LOCK:
        m_info = MARKETS_CACHE["by_symbol"].get(symbol, {})
    base_px = float(m_info.get("lastPrice", 100.0) or 100.0)
    arrs = _generate_synthetic_klines(base_px, limit)
    with STATE_LOCK:
        KLINES_CACHE[cache_key] = {"ts": now, "arrays": arrs}
    return arrs


def _ema_series(arr: np.ndarray, period: int) -> np.ndarray:
    alpha = 2.0 / (period + 1.0)
    out = np.empty_like(arr, dtype=float)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = alpha * arr[i] + (1.0 - alpha) * out[i - 1]
    return out


def compute_indicators(opens, highs, lows, closes, volumes):
    ema9 = _ema_series(closes, 9)
    ema21 = _ema_series(closes, 21)
    ema50 = _ema_series(closes, 50)
    ema200 = _ema_series(closes, min(200, len(closes)))

    # RSI (14) Wilder
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    period = 14
    avg_gain = np.mean(gains[:period]) if len(gains) >= period else 0.001
    avg_loss = np.mean(losses[:period]) if len(losses) >= period else 0.001
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    rs = avg_gain / max(avg_loss, 1e-12)
    rsi = 100.0 - (100.0 / (1.0 + rs))

    # MACD (12, 26, 9)
    ema12 = _ema_series(closes, 12)
    ema26 = _ema_series(closes, 26)
    macd_line = ema12 - ema26
    signal_line = _ema_series(macd_line, 9)
    macd_hist = float(macd_line[-1] - signal_line[-1])

    # ATR (14)
    prev_close = np.roll(closes, 1)
    prev_close[0] = closes[0]
    tr = np.maximum(highs - lows, np.maximum(np.abs(highs - prev_close), np.abs(lows - prev_close)))
    atr_series = _ema_series(tr, 14)
    atr = float(atr_series[-1])

    # ADX (14)
    up_move = highs[1:] - highs[:-1]
    down_move = lows[:-1] - lows[1:]
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr_14 = _ema_series(tr[1:], 14) + 1e-12
    plus_di = 100.0 * _ema_series(plus_dm, 14) / tr_14
    minus_di = 100.0 * _ema_series(minus_dm, 14) / tr_14
    dx = 100.0 * np.abs(plus_di - minus_di) / np.maximum(plus_di + minus_di, 1e-12)
    adx_series = _ema_series(dx, 14)
    adx = float(adx_series[-1])

    # Z-score e R² su ultimi 30 periodi
    win = closes[-30:] if len(closes) >= 30 else closes
    mean_w = float(np.mean(win))
    std_w = float(np.std(win)) + 1e-12
    z_score = float((closes[-1] - mean_w) / std_w)

    x_idx = np.arange(len(win), dtype=float)
    slope, intercept = np.polyfit(x_idx, win, 1)
    y_pred = slope * x_idx + intercept
    ss_res = float(np.sum((win - y_pred) ** 2))
    ss_tot = float(np.sum((win - mean_w) ** 2)) + 1e-12
    r_squared = max(0.0, min(1.0, 1.0 - (ss_res / ss_tot)))

    bb_mid = float(np.mean(closes[-20:]))
    bb_std = float(np.std(closes[-20:])) + 1e-12
    bb_upper = bb_mid + 2.0 * bb_std
    bb_lower = bb_mid - 2.0 * bb_std
    bb_percent = round(max(0.0, min(100.0, ((closes[-1] - bb_lower) / max(bb_upper - bb_lower, 1e-12)) * 100.0)), 1)

    return {
        "ema9": round(float(ema9[-1]), 6),
        "ema21": round(float(ema21[-1]), 6),
        "ema50": round(float(ema50[-1]), 6),
        "ema200": round(float(ema200[-1]), 6),
        "ema21_series": ema21,
        "ema50_series": ema50,
        "atr_series": atr_series,
        "rsi": round(float(rsi), 2),
        "macd_hist": round(macd_hist, 6),
        "atr": round(atr, 6),
        "adx": round(adx, 2),
        "z_score": round(z_score, 3),
        "r_squared": round(r_squared, 3),
        "bb_percent": bb_percent,
        "funding_rate": 0.010,
    }


def detect_early_entry_setup(opens, highs, lows, closes, volumes, ema20_arr, ema50_arr, ema200_val, atr_series, rsi, z_score):
    price = float(closes[-1])
    prev_high, prev_low = float(highs[-2]), float(lows[-2])
    curr_open, curr_high, curr_low, curr_close = float(opens[-1]), float(highs[-1]), float(lows[-1]), float(closes[-1])

    atr_val = float(atr_series[-1]) + 1e-9
    atr_sma20 = float(np.mean(atr_series[-20:])) if len(atr_series) >= 20 else float(atr_series[-1])
    is_compressed = bool(atr_series[-1] < (atr_sma20 * 0.92))

    candle_body = curr_close - curr_open
    candle_body_pct = abs(candle_body) / atr_val
    not_chasing = bool(candle_body_pct < 1.35 and abs(z_score) <= 2.25)

    e20 = float(ema20_arr[-1])
    e50 = float(ema50_arr[-1])

    # Blocchi anti-controtendenza severi:
    # Evita SHORT quando la candela spinge verso l'alto o il prezzo è sopra EMA20/EMA50 con RSI > 52
    bullish_push_against_short = bool(
        (candle_body > 0 and candle_body_pct > 0.30)
        or (price > e20 and price > e50 and rsi > 52.0)
        or (curr_close > prev_high and candle_body > 0)
    )
    # Evita LONG quando la candela spinge verso il basso o il prezzo è sotto EMA20/EMA50 con RSI < 48
    bearish_push_against_long = bool(
        (candle_body < 0 and candle_body_pct > 0.30)
        or (price < e20 and price < e50 and rsi < 48.0)
        or (curr_close < prev_low and candle_body < 0)
    )

    trend_bias = "BULL" if price >= float(ema200_val) else "BEAR"

    dist_e20_atr = abs(price - e20) / atr_val
    dist_e50_atr = abs(price - e50) / atr_val
    pullback_bull = bool((trend_bias == "BULL") and (min(dist_e20_atr, dist_e50_atr) <= 0.75) and (rsi < 60.0) and not bearish_push_against_long)
    pullback_bear = bool((trend_bias == "BEAR") and (min(dist_e20_atr, dist_e50_atr) <= 0.75) and (rsi > 40.0) and not bullish_push_against_short)

    sweep_bull = bool((curr_low < prev_low) and (curr_close > prev_low) and (curr_close > curr_open) and (curr_close >= e20 * 0.995) and not bearish_push_against_long)
    sweep_bear = bool((curr_high > prev_high) and (curr_close < prev_high) and (curr_close < curr_open) and (curr_close <= e20 * 1.005) and not bullish_push_against_short)

    vol_avg20 = float(np.mean(volumes[-20:])) if len(volumes) >= 20 else float(volumes[-1])
    vol_calm_or_sweep = bool((volumes[-1] <= vol_avg20 * 1.35) or sweep_bull or sweep_bear)

    setup_type = "NESSUN SETUP ANTICIPATO"
    direction = None
    limit_price = round(float(min(e20, e50) if trend_bias == "BULL" else max(e20, e50)), 6)
    order_type = "MARKET / CONFERMA"
    trigger_sound = False

    if sweep_bull and not_chasing:
        setup_type = "SWEEP RIALZISTA (CACCIA STOP SHORT)"
        direction = "LONG"
        limit_price = round(float(min(curr_close, e20)), 6)
        order_type = "BUY LIMIT / CONFERMA WICK"
        trigger_sound = True
    elif sweep_bear and not_chasing:
        setup_type = "SWEEP RIBASSISTA (CACCIA STOP LONG)"
        direction = "SHORT"
        limit_price = round(float(max(curr_close, e20)), 6)
        order_type = "SELL LIMIT / CONFERMA WICK"
        trigger_sound = True
    elif is_compressed and pullback_bull and vol_calm_or_sweep and not_chasing:
        setup_type = "COMPRESSIONE + PULLBACK LONG"
        direction = "LONG"
        order_type = "BUY LIMIT SU ZONA VALORE"
        trigger_sound = True
    elif is_compressed and pullback_bear and vol_calm_or_sweep and not_chasing:
        setup_type = "COMPRESSIONE + PULLBACK SHORT"
        direction = "SHORT"
        order_type = "SELL LIMIT SU ZONA VALORE"
        trigger_sound = True
    elif pullback_bull and not_chasing and rsi < 50.0:
        setup_type = "PULLBACK SU EMA 20/50 LONG"
        direction = "LONG"
        order_type = "BUY LIMIT / PULLBACK"
        trigger_sound = True
    elif pullback_bear and not_chasing and rsi > 50.0:
        setup_type = "PULLBACK SU EMA 20/50 SHORT"
        direction = "SHORT"
        order_type = "SELL LIMIT / PULLBACK"
        trigger_sound = True

    return {
        "setup_type": setup_type,
        "direction": direction,
        "is_compressed": is_compressed,
        "not_chasing": not_chasing,
        "sweep_detected": bool(sweep_bull or sweep_bear),
        "pullback_detected": bool(pullback_bull or pullback_bear),
        "order_type": order_type,
        "limit_price": limit_price,
        "trigger_sound": trigger_sound,
        "candle_id": int(len(closes)),
    }


def compute_entry_pre_alert(price: float, early_setup: dict, certain_signal: str, interval: str) -> dict:
    direction = early_setup.get("direction")
    if not direction and certain_signal:
        direction = "LONG" if "LONG" in certain_signal.upper() else ("SHORT" if "SHORT" in certain_signal.upper() else None)

    limit_px = float(early_setup.get("limit_price") or 0.0)
    not_chasing = bool(early_setup.get("not_chasing", True))
    is_certain = bool(certain_signal)

    tf_sec_map = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400}
    tf_sec = tf_sec_map.get(interval, 900)
    now_sec = int(time.time())
    rem = tf_sec - (now_sec % tf_sec)
    sec_to_next = 0 if rem == tf_sec else rem

    if not direction:
        return {
            "status": "WAIT",
            "direction": None,
            "limit_price": limit_px,
            "dist_pct": None,
            "sec_to_next_candle": sec_to_next,
            "enter_now": False,
            "pre_alert": False,
            "do_not_enter": False,
            "label": "IN ATTESA",
        }

    max_over_pct = 0.25
    past_threshold = False
    over_pct = None
    if limit_px > 0 and price > 0:
        if direction == "LONG" and price > limit_px * (1.0 + max_over_pct / 100.0):
            past_threshold = True
            over_pct = round(((price - limit_px) / limit_px) * 100.0, 2)
        elif direction == "SHORT" and price < limit_px * (1.0 - max_over_pct / 100.0):
            past_threshold = True
            over_pct = round(((limit_px - price) / limit_px) * 100.0, 2)

    dist_pct = round(abs(price - limit_px) / limit_px * 100.0, 2) if (limit_px > 0 and price > 0) else None
    near_trigger = bool((not past_threshold) and dist_pct is not None and dist_pct <= 0.50)
    candle_imminent = bool((not past_threshold) and sec_to_next <= 25)
    certain_ready = bool(is_certain and not_chasing and (not past_threshold))

    do_not_enter = bool(past_threshold or (not not_chasing))
    enter_now = bool((not do_not_enter) and (near_trigger or candle_imminent or certain_ready))
    pre_alert = bool((not do_not_enter) and (not enter_now))

    if do_not_enter:
        status = "DO_NOT_ENTER"
        label = f"NON ENTRARE (+{over_pct}%)" if (past_threshold and over_pct is not None) else "NON ENTRARE"
    elif enter_now:
        status = "ENTER_NOW"
        label = f"ENTRA ORA ({dist_pct}%)" if dist_pct is not None else "ENTRA ORA"
    else:
        status = "PRE_ALERT"
        label = f"PRE-AVVISO ({dist_pct}%)" if dist_pct is not None else "PRE-AVVISO"

    return {
        "status": status,
        "direction": direction,
        "limit_price": limit_px,
        "dist_pct": dist_pct,
        "over_pct": over_pct,
        "sec_to_next_candle": sec_to_next,
        "enter_now": enter_now,
        "pre_alert": pre_alert,
        "do_not_enter": do_not_enter,
        "label": label,
    }


def evaluate_profit_math_and_entry(price: float, ind: dict, weights: dict, early_setup: dict, interval: str, opens=None, closes=None) -> dict:
    w_ema = float(weights.get("ema_weight", 35.0))
    w_mom = float(weights.get("momentum_weight", 30.0))
    w_vol = float(weights.get("volatility_weight", 20.0))
    w_sent = float(weights.get("sentiment_weight", 15.0))
    w_sum = max(w_ema + w_mom + w_vol + w_sent, 1.0)

    ema_bias = 0.0
    if price > ind["ema9"] > ind["ema21"] > ind["ema50"]:
        ema_bias = 75.0
    elif price < ind["ema9"] < ind["ema21"] < ind["ema50"]:
        ema_bias = -75.0
    elif price > ind["ema50"]:
        ema_bias = 35.0
    elif price < ind["ema50"]:
        ema_bias = -35.0

    mom_bias = 0.0
    if ind["rsi"] > 55 and ind["macd_hist"] > 0:
        mom_bias += 45.0
    elif ind["rsi"] < 45 and ind["macd_hist"] < 0:
        mom_bias -= 45.0
    if ind["adx"] >= 22:
        mom_bias = mom_bias * 1.25

    vol_bias = -np.clip(ind["z_score"], -2.5, 2.5) * 18.0
    sent_bias = float(weights.get("custom_sentiment") or 0.0)

    raw_score = (ema_bias * w_ema + mom_bias * w_mom + vol_bias * w_vol + sent_bias * w_sent) / w_sum
    composite_score = round(float(np.clip(raw_score, -100.0, 100.0)), 1)

    # Verifica impulso candela corrente per evitare SHORT contro candela verde rialzista
    curr_green_push = False
    curr_red_push = False
    if opens is not None and closes is not None and len(opens) > 0 and len(closes) > 0:
        atr_v = max(float(ind.get("atr", 0.0)), 1e-9)
        body = float(closes[-1]) - float(opens[-1])
        if body > 0 and (body / atr_v) > 0.30:
            curr_green_push = True
        elif body < 0 and (abs(body) / atr_v) > 0.30:
            curr_red_push = True

    if curr_green_push and composite_score < 0:
        composite_score = round(min(composite_score * 0.35, -3.0), 1)
    if curr_red_push and composite_score > 0:
        composite_score = round(max(composite_score * 0.35, 3.0), 1)

    confidence = round(float(min(99.9, max(5.0, (abs(composite_score) * 1.1) + (ind["r_squared"] * 25.0) + (min(ind["adx"], 50.0) * 0.4)))), 1)

    min_conf = float(weights.get("min_confidence", 70.0))
    certain_signal = None
    if confidence >= min_conf and composite_score >= 18.0 and not curr_red_push:
        certain_signal = "CERTA LONG"
    elif confidence >= min_conf and composite_score <= -18.0 and not curr_green_push and price <= ind["ema21"] * 1.003:
        certain_signal = "CERTA SHORT"

    if certain_signal:
        signal = "STRONG LONG" if "LONG" in certain_signal else "STRONG SHORT"
    elif early_setup.get("direction") == "LONG":
        signal = "LONG"
    elif early_setup.get("direction") == "SHORT":
        signal = "SHORT"
    elif composite_score >= 20:
        signal = "STRONG LONG"
    elif composite_score > 5:
        signal = "LONG"
    elif composite_score <= -20:
        signal = "STRONG SHORT"
    elif composite_score < -5:
        signal = "SHORT"
    else:
        signal = "NEUTRAL / WAIT"

    atr = max(float(ind["atr"]), price * 0.002)
    sl_mult = float(weights.get("sl_mult", 1.4))
    tp1_mult = float(weights.get("tp1_mult", 1.8))
    tp2_mult = float(weights.get("tp2_mult", 3.2))
    tp3_mult = float(weights.get("tp3_mult", 4.8))

    is_short = ("SHORT" in signal) or (early_setup.get("direction") == "SHORT")
    if is_short:
        sl = round(price + atr * sl_mult, 6)
        tp1 = round(price - atr * tp1_mult, 6)
        tp2 = round(price - atr * tp2_mult, 6)
        tp3 = round(price - atr * tp3_mult, 6)
    else:
        sl = round(price - atr * sl_mult, 6)
        tp1 = round(price + atr * tp1_mult, 6)
        tp2 = round(price + atr * tp2_mult, 6)
        tp3 = round(price + atr * tp3_mult, 6)

    rr = round(tp2_mult / max(sl_mult, 0.01), 2)

    cap = float(weights.get("account_capital", 0.0) or 0.0)
    risk_pct = float(weights.get("risk_per_trade_pct", 1.5) or 1.5)
    max_loss = float(weights.get("max_daily_loss_usdt", 1.0) or 1.0)
    risk_budget = min(cap * (risk_pct / 100.0), max_loss) if cap > 0 else max_loss
    sl_dist = max(abs(price - sl), price * 0.002)
    qty = round(max(0.001, risk_budget / sl_dist), 3)
    lev = 10
    margin_usdt = round((qty * price) / lev, 2)
    p_win_pct = round(min(85.0, max(45.0, 50.0 + confidence * 0.25)), 1)

    entry_pre_alert = compute_entry_pre_alert(price, early_setup, certain_signal, interval)

    if entry_pre_alert["do_not_enter"]:
        entry_badge = "🚫 NON ENTRARE"
        entry_reason = f"Prezzo oltre soglia ({entry_pre_alert['over_pct']}%) o candela già estesa."
    elif entry_pre_alert["enter_now"]:
        entry_badge = "🚨 ENTRA ORA"
        entry_reason = f"Setup {early_setup.get('setup_type')} vicino al livello {early_setup.get('limit_price')}."
    elif entry_pre_alert["pre_alert"]:
        entry_badge = "⚡ ANTICIPO / PREP."
        entry_reason = f"In attesa di avvicinamento a {early_setup.get('limit_price')} ({early_setup.get('order_type')})."
    else:
        entry_badge = "⏳ IN ATTESA"
        entry_reason = "Nessun setup direzionale pulito senza contro-impulso."

    profile = get_auto_profile(weights.get("_category", "CRYPTO MAJOR"), interval)

    return {
        "composite_score": composite_score,
        "confidence": confidence,
        "sentiment": sent_bias,
        "signal": signal,
        "certain_signal": certain_signal,
        "levels": {"entry": round(price, 6), "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3, "rr": rr},
        "recommended_params": {
            "leverage": lev,
            "qty": qty,
            "margin_usdt": margin_usdt,
            "p_win_pct": p_win_pct,
            "summary": f"Lev {lev}x • Size {qty} • R:R 1:{rr}",
        },
        "tp_plan": {"label": profile["tp_label"]},
        "entry_rule": {"badge": entry_badge, "reason": entry_reason},
        "entry_pre_alert": entry_pre_alert,
    }


def analyze_single_symbol(symbol: str, interval: str = "15m", custom_sentiment=None, apply_auto_profile: bool = True) -> dict:
    with STATE_LOCK:
        m_info = MARKETS_CACHE["by_symbol"].get(symbol, {
            "symbol": symbol,
            "bingxName": symbol.replace("-USDT", "USDT"),
            "category": classify_market(symbol),
            "lastPrice": 0.0,
        })
        base_weights = dict(BRAIN_WEIGHTS)
        bal_usdt = float(AUTO_MONITOR["balance_info"].get("balance_usdt", 0.0) or 0.0)

    cat = m_info.get("category", classify_market(symbol))
    profile = get_auto_profile(cat, interval)
    if apply_auto_profile and base_weights.get("auto_profile_enabled", True):
        base_weights.update({
            "ema_weight": profile["ema_weight"],
            "momentum_weight": profile["momentum_weight"],
            "volatility_weight": profile["volatility_weight"],
            "sentiment_weight": profile["sentiment_weight"],
            "min_confidence": profile["min_confidence"],
            "sl_mult": profile["sl_mult"],
            "tp1_mult": profile["tp1_mult"],
            "tp2_mult": profile["tp2_mult"],
            "tp3_mult": profile["tp3_mult"],
        })
    if custom_sentiment is not None:
        base_weights["custom_sentiment"] = float(custom_sentiment)
    base_weights["_category"] = cat
    if bal_usdt > 0:
        base_weights["account_capital"] = bal_usdt

    opens, highs, lows, closes, volumes = fetch_klines(symbol, interval, limit=240)
    price = float(closes[-1]) if len(closes) else float(m_info.get("lastPrice", 0.0) or 0.0)
    ind = compute_indicators(opens, highs, lows, closes, volumes)
    early_setup = detect_early_entry_setup(
        opens, highs, lows, closes, volumes,
        ind["ema21_series"], ind["ema50_series"], ind["ema200"],
        ind["atr_series"], ind["rsi"], ind["z_score"]
    )
    ev = evaluate_profit_math_and_entry(price, ind, base_weights, early_setup, interval, opens=opens, closes=closes)

    clean_ind = {k: v for k, v in ind.items() if not isinstance(v, np.ndarray)}
    session_info = get_market_session_info(symbol, cat)

    return {
        "symbol": symbol,
        "bingx_name": m_info.get("bingxName", symbol.replace("-USDT", "USDT")),
        "category": cat,
        "interval": interval,
        "price": round(price, 6),
        "session": session_info,
        "indicators": clean_ind,
        "early_setup": early_setup,
        "composite_score": ev["composite_score"],
        "confidence": ev["confidence"],
        "sentiment": ev["sentiment"],
        "signal": ev["signal"],
        "certain_signal": ev["certain_signal"],
        "levels": ev["levels"],
        "recommended_params": ev["recommended_params"],
        "tp_plan": ev["tp_plan"],
        "entry_rule": ev["entry_rule"],
        "entry_pre_alert": ev["entry_pre_alert"],
        "weights": base_weights,
        "applied_profile": profile,
        "account_balance": bal_usdt,
    }


def _record_alerts_and_maybe_autotrades(analyses: list):
    now_str = datetime.now(ROME_TZ).strftime("%H:%M:%S")
    new_alerts = []
    for a in analyses:
        early = a.get("early_setup", {})
        cert = a.get("certain_signal")
        epa = a.get("entry_pre_alert", {})
        if (early.get("trigger_sound") or cert) and not epa.get("do_not_enter"):
            title = f"{'CERTO ' + cert if cert else 'ANTICIPO ' + str(early.get('direction', ''))}"
            alert_id = f"{a['symbol']}_{a['interval']}_{early.get('candle_id')}_{title}"
            new_alerts.append({
                "id": alert_id,
                "time": now_str,
                "symbol": a["bingx_name"],
                "title": title,
                "order_type": early.get("order_type", "MARKET"),
                "limit_price": early.get("limit_price", a["price"]),
                "sl": a["levels"]["sl"],
                "tp2": a["levels"]["tp2"],
            })

    with STATE_LOCK:
        existing_ids = {x["id"] for x in AUTO_MONITOR["audio_alerts"]}
        for al in new_alerts:
            if al["id"] not in existing_ids:
                AUTO_MONITOR["audio_alerts"].insert(0, al)
        AUTO_MONITOR["audio_alerts"] = AUTO_MONITOR["audio_alerts"][:50]
        auto_trade_on = bool(AUTO_MONITOR["auto_trade_enabled"])
        traded_set = set(AUTO_MONITOR["auto_traded_keys"])

    if not auto_trade_on:
        return

    for a in analyses:
        epa = a.get("entry_pre_alert", {})
        if not epa.get("enter_now") or epa.get("do_not_enter"):
            continue
        direction = epa.get("direction")
        if direction not in ("LONG", "SHORT"):
            continue
        trade_key = f"{a['symbol']}_{a['interval']}_{direction}_{a.get('early_setup', {}).get('candle_id')}"
        if trade_key in traded_set:
            continue
        qty = float(a.get("recommended_params", {}).get("qty", 0.0) or 0.0)
        lev = int(a.get("recommended_params", {}).get("leverage", 10) or 10)
        if qty <= 0:
            continue
        side = "BUY" if direction == "LONG" else "SELL"
        res = bingx_signed_request("POST", "/openApi/swap/v2/trade/order", {
            "symbol": a["symbol"],
            "side": side,
            "positionSide": direction,
            "type": "MARKET",
            "quantity": qty,
            "leverage": lev,
            "stopLoss": json.dumps({"type": "STOP_MARKET", "stopPrice": a["levels"]["sl"], "workingType": "MARK_PRICE"}),
            "takeProfit": json.dumps({"type": "TAKE_PROFIT_MARKET", "stopPrice": a["levels"]["tp2"], "workingType": "MARK_PRICE"}),
        })
        with STATE_LOCK:
            AUTO_MONITOR["auto_traded_keys"].add(trade_key)
            AUTO_MONITOR["trade_logs"].insert(0, {
                "time": now_str,
                "symbol": a["symbol"],
                "side": side,
                "qty": qty,
                "tp": a["levels"]["tp2"],
                "sl": a["levels"]["sl"],
                "response": res,
            })


def background_monitor_loop():
    while True:
        try:
            with STATE_LOCK:
                enabled = bool(AUTO_MONITOR["enabled"])
                interval = str(AUTO_MONITOR["interval"] or "15m")
                cat = str(AUTO_MONITOR["active_category"] or "ALL")
            if enabled:
                markets = load_markets_from_bingx(force=False)
                pool = [m["symbol"] for m in markets if cat in ("ALL", "CERTAIN") or m["category"] == cat][:20]
                results = {}
                list_res = []
                for sym in pool:
                    an = analyze_single_symbol(sym, interval=interval, apply_auto_profile=True)
                    results[sym] = an
                    list_res.append(an)
                _record_alerts_and_maybe_autotrades(list_res)
                fetch_bingx_real_balance()
                fetch_bingx_open_positions()
                with STATE_LOCK:
                    AUTO_MONITOR["signals"].update(results)
                    AUTO_MONITOR["last_run"] = datetime.now(ROME_TZ).strftime("%H:%M:%S")
        except Exception as e:
            print("Background monitor error:", e)
        time.sleep(5)


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/api/keys", methods=["GET", "POST"])
def api_keys():
    if request.method == "GET":
        with STATE_LOCK:
            ak = API_KEYS["api_key"]
            sk = API_KEYS["secret_key"]
        return jsonify({
            "status": "ok",
            "has_keys": bool(ak and sk),
            "masked_api_key": _mask_key(ak),
            "masked_secret_key": _mask_key(sk),
        })

    body = request.get_json(silent=True) or {}
    ak = str(body.get("api_key", "")).strip()
    sk = str(body.get("secret_key", "")).strip()
    if not ak or not sk:
        return jsonify({"status": "error", "msg": "Inserisci sia API Key che Secret Key."}), 400
    with STATE_LOCK:
        API_KEYS["api_key"] = ak
        API_KEYS["secret_key"] = sk
    persist_keys_to_disk(ak, sk)
    return jsonify({"status": "ok", "msg": "Chiavi API BingX salvate con successo."})


@app.route("/api/markets")
def api_markets():
    force = request.args.get("force", "0") == "1"
    data = load_markets_from_bingx(force=force)
    return jsonify({"status": "ok", "markets": data})


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    body = request.get_json(silent=True) or {}
    symbol = str(body.get("symbol", "BTC-USDT")).upper()
    interval = str(body.get("interval", "15m"))
    custom_sent = body.get("custom_sentiment")
    apply_prof = bool(body.get("apply_auto_profile", True))
    an = analyze_single_symbol(symbol, interval=interval, custom_sentiment=custom_sent, apply_auto_profile=apply_prof)
    _record_alerts_and_maybe_autotrades([an])
    with STATE_LOCK:
        AUTO_MONITOR["signals"][symbol] = an
        alerts = list(AUTO_MONITOR["audio_alerts"])
    return jsonify({"status": "ok", "data": an, "audio_alerts": alerts})


@app.route("/api/scan_top", methods=["POST"])
def api_scan_top():
    body = request.get_json(silent=True) or {}
    symbols = body.get("symbols") or []
    interval = str(body.get("interval", "15m"))
    overrides = body.get("overrides") or {}
    out = []
    for sym in symbols[:30]:
        s_up = str(sym).upper()
        ov = overrides.get(s_up, {}) if isinstance(overrides, dict) else {}
        c_sent = ov.get("sentiment") if ov.get("sentiment") not in (None, 0, "0") else None
        out.append(analyze_single_symbol(s_up, interval=interval, custom_sentiment=c_sent, apply_auto_profile=True))
    _record_alerts_and_maybe_autotrades(out)
    with STATE_LOCK:
        for item in out:
            AUTO_MONITOR["signals"][item["symbol"]] = item
        alerts = list(AUTO_MONITOR["audio_alerts"])
    return jsonify({"status": "ok", "signals": out, "audio_alerts": alerts})


@app.route("/api/weights", methods=["POST"])
def api_weights():
    body = request.get_json(silent=True) or {}
    with STATE_LOCK:
        for k in ("ema_weight", "momentum_weight", "volatility_weight", "sentiment_weight", "min_confidence", "account_capital", "risk_per_trade_pct", "max_daily_loss_usdt"):
            if k in body and body[k] is not None:
                try:
                    BRAIN_WEIGHTS[k] = float(body[k])
                except Exception:
                    pass
        w_copy = dict(BRAIN_WEIGHTS)
    return jsonify({"status": "ok", "weights": w_copy})


@app.route("/api/account_balance")
def api_account_balance():
    info = fetch_bingx_real_balance()
    return jsonify({
        "status": "ok",
        "balance": info.get("balance_usdt", 0.0),
        "available": info.get("available_usdt", 0.0),
        "unrealized_pnl": info.get("unrealized_pnl_usdt", 0.0),
        "last_sync": info.get("last_sync"),
        "msg": info.get("msg", ""),
    })


@app.route("/api/auto_monitor_status", methods=["GET", "POST"])
def api_auto_monitor_status():
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        with STATE_LOCK:
            if "enabled" in body:
                AUTO_MONITOR["enabled"] = bool(body["enabled"])
            if "auto_trade_enabled" in body:
                AUTO_MONITOR["auto_trade_enabled"] = bool(body["auto_trade_enabled"])
            if "interval" in body and body["interval"]:
                AUTO_MONITOR["interval"] = str(body["interval"])
            if "active_category" in body and body["active_category"]:
                AUTO_MONITOR["active_category"] = str(body["active_category"])
            cur_tf = AUTO_MONITOR["interval"]
            cur_cat = AUTO_MONITOR["active_category"]
            prof = get_auto_profile(cur_cat if cur_cat not in ("ALL", "CERTAIN") else "CRYPTO MAJOR", cur_tf)
            if body.get("apply_profile", True):
                BRAIN_WEIGHTS.update({
                    "ema_weight": prof["ema_weight"],
                    "momentum_weight": prof["momentum_weight"],
                    "volatility_weight": prof["volatility_weight"],
                    "sentiment_weight": prof["sentiment_weight"],
                    "min_confidence": prof["min_confidence"],
                    "sl_mult": prof["sl_mult"],
                    "tp1_mult": prof["tp1_mult"],
                    "tp2_mult": prof["tp2_mult"],
                    "tp3_mult": prof["tp3_mult"],
                })
            w_snap = dict(BRAIN_WEIGHTS)

        return jsonify({"status": "ok", "weights": w_snap, "profile": prof})

    with STATE_LOCK:
        snap = {
            "status": "ok",
            "enabled": AUTO_MONITOR["enabled"],
            "auto_trade_enabled": AUTO_MONITOR["auto_trade_enabled"],
            "interval": AUTO_MONITOR["interval"],
            "active_category": AUTO_MONITOR["active_category"],
            "last_run": AUTO_MONITOR["last_run"],
            "signals": dict(AUTO_MONITOR["signals"]),
            "trade_logs": list(AUTO_MONITOR["trade_logs"]),
            "audio_alerts": list(AUTO_MONITOR["audio_alerts"]),
            "open_positions": dict(AUTO_MONITOR["open_positions"]),
            "balance_info": dict(AUTO_MONITOR["balance_info"]),
            "weights": dict(BRAIN_WEIGHTS),
        }
    return jsonify(snap)


@app.route("/api/execute_order", methods=["POST"])
def api_execute_order():
    body = request.get_json(silent=True) or {}
    symbol = str(body.get("symbol", "")).upper()
    side = str(body.get("side", "BUY")).upper()
    qty = float(body.get("quantity", 0.0) or 0.0)
    lev = int(float(body.get("leverage", 10) or 10))
    sl = body.get("sl")
    tp = body.get("tp")

    if not symbol or qty <= 0:
        return jsonify({"status": "error", "msg": "Parametri ordine non validi"}), 400

    pos_side = "LONG" if side == "BUY" else "SHORT"
    params = {
        "symbol": symbol,
        "side": side,
        "positionSide": pos_side,
        "type": "MARKET",
        "quantity": qty,
        "leverage": lev,
    }
    if sl is not None:
        params["stopLoss"] = json.dumps({"type": "STOP_MARKET", "stopPrice": sl, "workingType": "MARK_PRICE"})
    if tp is not None:
        params["takeProfit"] = json.dumps({"type": "TAKE_PROFIT_MARKET", "stopPrice": tp, "workingType": "MARK_PRICE"})

    res = bingx_signed_request("POST", "/openApi/swap/v2/trade/order", params)
    with STATE_LOCK:
        AUTO_MONITOR["trade_logs"].insert(0, {
            "time": datetime.now(ROME_TZ).strftime("%H:%M:%S"),
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "tp": tp,
            "sl": sl,
            "response": res,
        })
    return jsonify({"status": "ok", "bingx_response": res})


@app.route("/api/close_position", methods=["POST"])
def api_close_position():
    body = request.get_json(silent=True) or {}
    symbol = str(body.get("symbol", "")).upper()
    side = str(body.get("side", "LONG")).upper()
    qty = float(body.get("quantity", 0.0) or 0.0)
    if not symbol or qty <= 0:
        return jsonify({"status": "error", "msg": "Parametri chiusura non validi"}), 400

    close_side = "SELL" if side == "LONG" else "BUY"
    res = bingx_signed_request("POST", "/openApi/swap/v2/trade/order", {
        "symbol": symbol,
        "side": close_side,
        "positionSide": side,
        "type": "MARKET",
        "quantity": qty,
    })
    with STATE_LOCK:
        AUTO_MONITOR["trade_logs"].insert(0, {
            "time": datetime.now(ROME_TZ).strftime("%H:%M:%S"),
            "symbol": symbol,
            "side": f"CLOSE-{side}",
            "qty": qty,
            "tp": "--",
            "sl": "--",
            "response": res,
        })
    return jsonify({"status": "ok", "bingx_response": res})


load_saved_keys_on_startup()
threading.Thread(target=background_monitor_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
