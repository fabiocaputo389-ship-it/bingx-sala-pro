import os
import json
import time
import hmac
import hashlib
import threading
import urllib.parse
import requests
import numpy as np
from flask import Flask, render_template, jsonify, request
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

KEYS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".bingx_keys.json")
BASE_URL = "https://open-api.bingx.com"


def load_saved_keys():
    api_key = os.getenv("BINGX_API_KEY", "")
    secret_key = os.getenv("BINGX_SECRET_KEY", "")
    if os.path.exists(KEYS_FILE):
        try:
            with open(KEYS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data.get("api_key"):
                    api_key = data["api_key"].strip()
                if data.get("secret_key"):
                    secret_key = data["secret_key"].strip()
        except Exception as e:
            print(f"[Keys Load Error] {e}")
    return api_key, secret_key


def save_keys_to_disk(api_key: str, secret_key: str):
    with open(KEYS_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "api_key": api_key.strip(),
            "secret_key": secret_key.strip(),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")
        }, f, indent=2)
    try:
        os.chmod(KEYS_FILE, 0o600)
    except Exception:
        pass


BINGX_API_KEY, BINGX_SECRET_KEY = load_saved_keys()

BRAIN_WEIGHTS = {
    "ema_weight": 30,
    "momentum_weight": 25,
    "volatility_weight": 20,
    "sentiment_weight": 25,
    "min_confidence": 75,
    "auto_scan_interval": 25,
    "auto_monitor_enabled": True,
    "auto_trade_enabled": False,
    "auto_trade_cooldown_sec": 300,
    "breakeven_at_tp_pct": 50.0,
    "max_daily_loss_usdt": 100.0,
    "account_capital": 1000.0,
    "risk_per_trade_pct": 1.5
}

# Mappa simboli API BingX -> nome esatto visualizzato in app BingX
API_TO_BINGX_NAME = {
    "NCCOGOLD2USD-USDT": "GOLD(XAU)",
    "GOLD-USDT": "GOLD(XAU)",
    "XAU-USDT": "GOLD(XAU)",
    "XAUT-USDT": "XAUT(GOLD)USDT",
    "PAXG-USDT": "PAXG(GOLD)USDT",
    "XAUXPT-USDT": "XAUXPT",
    "XAUEUR-USDT": "XAUEUR",
    "XAUZAR-USDT": "XAUZAR",
    "XAUJPY-USDT": "XAUJPY",
    "XAUAUD-USDT": "XAUAUD",
    "XAUKRW-USDT": "XAUKRW",
    "XAG-USDT": "SILVER(XAG)",
    "SILVER-USDT": "SILVER(XAG)",
    "WTI-USDT": "Oil WTI",
    "OILWTI-USDT": "Oil WTI",
    "BRENT-USDT": "Oil Brent",
    "OILBRENT-USDT": "Oil Brent",
    "NDX-USDT": "NASDAQ100",
    "NAS100-USDT": "NASDAQ100",
    "NASDAQ100-USDT": "NASDAQ100",
    "SOXL-USDT": "SOXL",
    "GS-USDT": "GS",
    "EUR-USDT": "EURUSDT",
    "EURUSD-USDT": "EURUSDT",
    "BTC-USDT": "BTCUSDT",
    "ETH-USDT": "ETHUSDT",
    "SOL-USDT": "SOLUSDT",
    "XRP-USDT": "XRPUSDT",
    "BNB-USDT": "BNBUSDT",
    "HYPE-USDT": "HYPEUSDT",
    "ZEC-USDT": "ZECUSDT",
    "JTO-USDT": "JTOUSDT",
    "PEOPLE-USDT": "PEOPLEUSDT",
    "SUI-USDT": "SUIUSDT",
    "PENDLE-USDT": "PENDLEUSDT"
}

BINGX_NAME_TO_API = {
    "GOLD(XAU)": ["NCCOGOLD2USD-USDT", "XAUT-USDT", "PAXG-USDT", "GOLD-USDT", "XAU-USDT"],
    "XAUT(GOLD)USDT": ["XAUT-USDT"],
    "PAXG(GOLD)USDT": ["PAXG-USDT"],
    "XAUXPT": ["XAUXPT-USDT", "XAUT-USDT", "PAXG-USDT"],
    "XAUEUR": ["XAUEUR-USDT", "XAUT-USDT", "PAXG-USDT"],
    "XAUZAR": ["XAUZAR-USDT", "XAUT-USDT"],
    "XAUJPY": ["XAUJPY-USDT", "XAUT-USDT"],
    "XAUAUD": ["XAUAUD-USDT", "XAUT-USDT"],
    "XAUKRW": ["XAUKRW-USDT", "XAUT-USDT"],
    "SILVER(XAG)": ["XAG-USDT", "SILVER-USDT"],
    "OIL WTI": ["WTI-USDT", "OILWTI-USDT"],
    "OIL BRENT": ["BRENT-USDT", "OILBRENT-USDT"],
    "NASDAQ100": ["NDX-USDT", "NAS100-USDT", "NASDAQ100-USDT"],
    "SOXL": ["SOXL-USDT"],
    "GS": ["GS-USDT"],
    "EURUSDT": ["EUR-USDT", "EURUSD-USDT"],
    "BTCUSDT": ["BTC-USDT"],
    "ETHUSDT": ["ETH-USDT"],
    "SOLUSDT": ["SOL-USDT"],
    "XRPUSDT": ["XRP-USDT"],
    "BNBUSDT": ["BNB-USDT"],
    "HYPEUSDT": ["HYPE-USDT"],
    "ZECUSDT": ["ZEC-USDT"],
    "JTOUSDT": ["JTO-USDT"],
    "PEOPLEUSDT": ["PEOPLE-USDT"],
    "SUIUSDT": ["SUI-USDT"],
    "PENDLEUSDT": ["PENDLE-USDT"]
}

CONTRACTS_CACHE = {"timestamp": 0, "data": [], "symbol_set": set(), "max_lev_map": {}}
AUTO_SCAN_STATE = {
    "running": False,
    "last_run": None,
    "interval": "15m",
    "signals": {},
    "last_auto_trade": {},
    "breakeven_moved": set(),
    "trade_logs": [],
    "open_positions": {}
}


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


def bingx_private_get(endpoint: str, params: dict = None):
    if not BINGX_API_KEY or not BINGX_SECRET_KEY:
        return None
    p = dict(params or {})
    p["timestamp"] = int(time.time() * 1000)
    signed_qs = sign_request(p)
    url = f"{BASE_URL}{endpoint}?{signed_qs}"
    headers = {"X-BX-APIKEY": BINGX_API_KEY}
    try:
        resp = requests.get(url, headers=headers, timeout=8)
        data = resp.json()
        if data.get("code") == 0:
            return data.get("data")
    except Exception as e:
        print(f"[BingX Private GET Error] {endpoint}: {e}")
    return None


def bingx_private_post(endpoint: str, params: dict):
    if not BINGX_API_KEY or not BINGX_SECRET_KEY:
        return {"code": -1, "msg": "API Key / Secret Key BingX mancanti."}
    params["timestamp"] = int(time.time() * 1000)
    signed_qs = sign_request(params)
    url = f"{BASE_URL}{endpoint}?{signed_qs}"
    headers = {"X-BX-APIKEY": BINGX_API_KEY}
    try:
        resp = requests.post(url, headers=headers, timeout=8)
        return resp.json()
    except Exception as e:
        return {"code": -1, "msg": str(e)}


def bingx_exact_name(api_symbol: str) -> str:
    s = api_symbol.upper()
    if s in API_TO_BINGX_NAME:
        return API_TO_BINGX_NAME[s]
    return s.replace("-", "")


def categorize_symbol(bingx_name: str, api_symbol: str) -> str:
    s = f"{bingx_name} {api_symbol}".upper()
    if any(k in s for k in ["GOLD", "XAU", "SILVER", "XAG", "WTI", "BRENT", "OIL", "NATGAS", "COPPER"]):
        return "METALLI & COMMODITIES"
    if any(k in s for k in ["EURUSDT", "EUR-", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD"]):
        return "FOREX & VALUTE"
    if any(k in s for k in ["NASDAQ100", "NDX", "NAS100", "SPX", "US30", "SOXL", "GS", "TSLA", "NVDA", "AAPL"]):
        return "INDICI & AZIONI"
    if any(k in s for k in ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "HYPEUSDT", "ZECUSDT", "JTOUSDT", "PEOPLEUSDT", "SUIUSDT", "PENDLEUSDT"]):
        return "CRYPTO MAJOR"
    return "ALTCOIN & DEFI"


def get_all_bingx_contracts(force: bool = False):
    now = time.time()
    if not force and CONTRACTS_CACHE["data"] and (now - CONTRACTS_CACHE["timestamp"] < 120):
        return CONTRACTS_CACHE["data"]

    raw_contracts = bingx_public_get("/openApi/swap/v2/quote/contracts")
    tickers = bingx_public_get("/openApi/swap/v2/quote/ticker")
    ticker_map = {t["symbol"]: t for t in (tickers or [])}

    results = []
    symbol_set = set()
    max_lev_map = {}
    if raw_contracts:
        for c in raw_contracts:
            if c.get("status") != 1:
                continue
            sym = c.get("symbol")
            max_lev = int(c.get("maxLeverage", 50) or 50)
            symbol_set.add(sym)
            max_lev_map[sym] = max_lev
            tk = ticker_map.get(sym, {})
            vol_24h = float(tk.get("quoteVolume", 0) or 0)
            exact_name = bingx_exact_name(sym)
            results.append({
                "symbol": sym,
                "bingxName": exact_name,
                "displayName": exact_name,
                "category": categorize_symbol(exact_name, sym),
                "maxLeverage": max_lev,
                "pricePrecision": c.get("pricePrecision", 4),
                "lastPrice": float(tk.get("lastPrice", 0) or 0),
                "priceChangePercent": float(tk.get("priceChangePercent", 0) or 0),
                "volume24h": vol_24h,
                "high24h": float(tk.get("highPrice", 0) or 0),
                "low24h": float(tk.get("lowPrice", 0) or 0),
            })
        results.sort(key=lambda x: x["volume24h"], reverse=True)
        CONTRACTS_CACHE["data"] = results
        CONTRACTS_CACHE["symbol_set"] = symbol_set
        CONTRACTS_CACHE["max_lev_map"] = max_lev_map
        CONTRACTS_CACHE["timestamp"] = now
    return results


def resolve_bingx_symbol(query_symbol: str) -> str:
    clean = query_symbol.strip().upper().replace("/", "-")
    if clean in CONTRACTS_CACHE["symbol_set"]:
        return clean
    if clean in BINGX_NAME_TO_API:
        for cand in BINGX_NAME_TO_API[clean]:
            if cand in CONTRACTS_CACHE["symbol_set"]:
                return cand
        return BINGX_NAME_TO_API[clean][0]
    if "-" not in clean and clean.endswith("USDT"):
        return clean[:-4] + "-USDT"
    return clean


def fetch_open_bingx_positions(signal_map: dict = None):
    raw_pos = bingx_private_get("/openApi/swap/v2/user/positions")
    pos_dict = {}
    if not isinstance(raw_pos, list):
        return pos_dict

    for p in raw_pos:
        amt = float(p.get("positionAmt", 0) or 0)
        if abs(amt) <= 0:
            continue
        sym = p.get("symbol")
        side = (p.get("positionSide") or ("LONG" if amt > 0 else "SHORT")).upper()
        entry_px = float(p.get("avgPrice", 0) or 0)
        mark_px = float(p.get("markPrice", 0) or entry_px)
        pnl = float(p.get("unrealizedProfit", 0) or 0)
        lev = float(p.get("leverage", 1) or 1)

        sig_info = (signal_map or {}).get(sym, {})
        tp_target = float(sig_info.get("levels", {}).get("tp2", 0) or 0)
        sl_target = float(sig_info.get("levels", {}).get("sl", 0) or 0)

        if tp_target <= 0 and entry_px > 0:
            tp_target = entry_px * 1.025 if side == "LONG" else entry_px * 0.975

        progress_pct = 0.0
        if entry_px > 0 and tp_target != entry_px:
            if side == "LONG":
                progress_pct = ((mark_px - entry_px) / (tp_target - entry_px)) * 100.0
            else:
                progress_pct = ((entry_px - mark_px) / (entry_px - tp_target)) * 100.0

        progress_clamped = round(max(0.0, min(100.0, progress_pct)), 1)

        be_threshold = float(BRAIN_WEIGHTS.get("breakeven_at_tp_pct", 50.0))
        be_key = f"{sym}_{side}"
        if progress_clamped >= be_threshold and be_key not in AUTO_SCAN_STATE["breakeven_moved"] and entry_px > 0:
            be_price = round(entry_px * (1.001 if side == "LONG" else 0.999), 6)
            bingx_private_post("/openApi/swap/v2/trade/order", {
                "symbol": sym,
                "side": "SELL" if side == "LONG" else "BUY",
                "positionSide": side,
                "type": "STOP_MARKET",
                "stopPrice": be_price,
                "workingType": "MARK_PRICE",
                "quantity": abs(amt)
            })
            AUTO_SCAN_STATE["breakeven_moved"].add(be_key)

        pos_dict[sym] = {
            "symbol": sym,
            "bingx_name": bingx_exact_name(sym),
            "side": side,
            "size": abs(amt),
            "entry_price": round(entry_px, 6),
            "mark_price": round(mark_px, 6),
            "tp_price": round(tp_target, 6),
            "sl_price": round(sl_target, 6),
            "tp_progress_pct": progress_clamped,
            "unrealized_pnl": round(pnl, 2),
            "leverage": lev
        }
    return pos_dict


def set_bingx_leverage(symbol: str, position_side: str, leverage: int):
    return bingx_private_post("/openApi/swap/v2/trade/leverage", {
        "symbol": symbol,
        "side": position_side,
        "leverage": int(leverage)
    })


def place_bingx_real_order(symbol: str, side: str, quantity, sl, tp, leverage: int = None):
    real_symbol = resolve_bingx_symbol(symbol)
    position_side = "LONG" if side == "BUY" else "SHORT"
    if leverage:
        set_bingx_leverage(real_symbol, position_side, leverage)

    order_params = {
        "symbol": real_symbol,
        "side": side,
        "positionSide": position_side,
        "type": "MARKET",
        "quantity": quantity,
        "stopLoss": f'{{"type": "STOP_MARKET", "stopPrice": {sl}, "workingType": "MARK_PRICE"}}',
        "takeProfit": f'{{"type": "TAKE_PROFIT_MARKET", "stopPrice": {tp}, "workingType": "MARK_PRICE"}}'
    }
    res = bingx_private_post("/openApi/swap/v2/trade/order", order_params)
    AUTO_SCAN_STATE["trade_logs"].insert(0, {
        "time": time.strftime("%H:%M:%S"),
        "symbol": bingx_exact_name(real_symbol),
        "side": side,
        "qty": quantity,
        "sl": sl,
        "tp": tp,
        "response": res
    })
    AUTO_SCAN_STATE["trade_logs"] = AUTO_SCAN_STATE["trade_logs"][:30]
    return res


def close_bingx_position(symbol: str, side: str, quantity):
    real_symbol = resolve_bingx_symbol(symbol)
    close_side = "SELL" if side.upper() == "LONG" else "BUY"
    res = bingx_private_post("/openApi/swap/v2/trade/order", {
        "symbol": real_symbol,
        "side": close_side,
        "positionSide": side.upper(),
        "type": "MARKET",
        "quantity": quantity
    })
    AUTO_SCAN_STATE["trade_logs"].insert(0, {
        "time": time.strftime("%H:%M:%S"),
        "symbol": bingx_exact_name(real_symbol),
        "side": f"CLOSE_{side.upper()}",
        "qty": quantity,
        "sl": "-",
        "tp": "-",
        "response": res
    })
    AUTO_SCAN_STATE["trade_logs"] = AUTO_SCAN_STATE["trade_logs"][:30]
    return res


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
    atr_s = np.convolve(tr, np.ones(period) / period, mode='valid')
    p_di = 100 * (np.convolve(plus_dm, np.ones(period) / period, mode='valid') / (atr_s + 1e-9))
    m_di = 100 * (np.convolve(minus_dm, np.ones(period) / period, mode='valid') / (atr_s + 1e-9))
    dx = 100 * np.abs(p_di - m_di) / (p_di + m_di + 1e-9)
    return float(np.mean(dx[-period:]))


def compute_tp_timeframe_plan(interval: str, adx: float):
    tf_map = {
        "1m":  {"tp_tf": "5m",  "candles": 4 if adx >= 25 else 7, "minutes_per_candle": 5},
        "5m":  {"tp_tf": "15m", "candles": 3 if adx >= 25 else 6, "minutes_per_candle": 15},
        "15m": {"tp_tf": "1h",  "candles": 2 if adx >= 25 else 4, "minutes_per_candle": 60},
        "1h":  {"tp_tf": "4h",  "candles": 2 if adx >= 25 else 3, "minutes_per_candle": 240},
        "4h":  {"tp_tf": "1d",  "candles": 2 if adx >= 25 else 3, "minutes_per_candle": 1440},
    }
    cfg = tf_map.get(interval, {"tp_tf": "1h", "candles": 3, "minutes_per_candle": 60})
    est_hours = round((cfg["candles"] * cfg["minutes_per_candle"]) / 60.0, 1)
    return {
        "tp_timeframe": cfg["tp_tf"],
        "tp_candles": cfg["candles"],
        "est_duration_h": est_hours,
        "label": f"{cfg['tp_tf']} ({cfg['candles']} candele • ~{est_hours}h)"
    }


def calc_recommended_leverage(atr_pct: float, max_contract_lev: int, capital: float) -> int:
    if atr_pct <= 0.15:
        base_lev = 25
    elif atr_pct <= 0.35:
        base_lev = 15
    elif atr_pct <= 0.75:
        base_lev = 10
    elif atr_pct <= 1.50:
        base_lev = 6
    else:
        base_lev = 3

    if capital <= 2.0:
        base_lev = min(base_lev, 20)
    return max(1, min(base_lev, max_contract_lev))


def evaluate_profit_math_and_entry(
    signal, confidence, rr_ratio, adx, r_squared, z_score, rsi,
    min_conf, capital, risk_pct, stop_dist, price, rec_leverage
):
    p_win = float(np.clip(0.45 + (confidence - 50.0) * 0.0045 + (r_squared - 0.3) * 0.12, 0.35, 0.76))
    p_loss = 1.0 - p_win
    reward_r = max(rr_ratio, 0.5)
    expectancy_r = round((p_win * reward_r) - (p_loss * 1.0), 2)

    kelly_raw = p_win - (p_loss / max(reward_r, 0.1))
    half_kelly_pct = round(max(0.0, min(kelly_raw * 0.5 * 100.0, 5.0)), 2)

    risk_usd = capital * (risk_pct / 100.0)
    suggested_qty = (risk_usd / stop_dist) if stop_dist > 0 else 0.0
    qty_fmt = round(suggested_qty, 4) if suggested_qty < 1 else (round(suggested_qty, 2) if suggested_qty < 100 else round(suggested_qty, 1))
    notional_usd = suggested_qty * price
    margin_needed = round(notional_usd / max(rec_leverage, 1), 2)

    if expectancy_r <= 0:
        entry_rule = {"action": "NO ENTRY", "badge": "🚫 NON ENTRARE", "reason": f"Expectancy {expectancy_r:+.2f}R non favorevole."}
    elif signal == "NEUTRAL / WAIT":
        entry_rule = {"action": "NO ENTRY", "badge": "🚫 NON ENTRARE", "reason": "Setup neutro: attendere direzione chiara."}
    elif abs(z_score) > 2.45:
        entry_rule = {"action": "NO ENTRY", "badge": "🚫 NON ENTRARE", "reason": f"Estensione eccessiva (Z={z_score:+.2f}σ)."}
    elif r_squared < 0.28 and adx < 22:
        entry_rule = {"action": "NO ENTRY", "badge": "🚫 NON ENTRARE", "reason": f"Mercato laterale (ADX {adx:.1f}, R² {r_squared:.2f})."}
    elif "LONG" in signal and rsi > 78:
        entry_rule = {"action": "WAIT PULLBACK", "badge": "⏳ ATTENDI PULLBACK", "reason": f"RSI {rsi:.1f} in ipercomprato."}
    elif "SHORT" in signal and rsi < 22:
        entry_rule = {"action": "WAIT PULLBACK", "badge": "⏳ ATTENDI PULLBACK", "reason": f"RSI {rsi:.1f} in ipervenduto."}
    elif confidence >= min_conf and expectancy_r > 0.15:
        entry_rule = {"action": "ENTER NOW", "badge": "✅ ENTRA ORA", "reason": f"Exp {expectancy_r:+.2f}R • Conf {confidence}% • Kelly {half_kelly_pct}%."}
    else:
        entry_rule = {"action": "WAIT CONFIRMATION", "badge": "⏳ CONFERMA PARZIALE", "reason": f"Confidenza {confidence}% sotto soglia {min_conf}%."}

    recommended_params = {
        "leverage": rec_leverage,
        "qty": qty_fmt,
        "margin_usdt": margin_needed,
        "risk_usd": round(risk_usd, 2),
        "p_win_pct": round(p_win * 100, 1),
        "expectancy_r": expectancy_r,
        "half_kelly_pct": half_kelly_pct,
        "summary": f"Lev {rec_leverage}x • Size {qty_fmt} • Exp {expectancy_r:+.2f}R • Kelly {half_kelly_pct}%"
    }
    return entry_rule, recommended_params


def analyze_symbol_math(symbol: str, interval: str = "15m", custom_sentiment: float = None):
    real_symbol = resolve_bingx_symbol(symbol)
    klines = bingx_public_get("/openApi/swap/v3/quote/klines", {
        "symbol": real_symbol,
        "interval": interval,
        "limit": 220
    })
    depth = bingx_public_get("/openApi/swap/v2/quote/depth", {"symbol": real_symbol, "limit": 20})
    premium = bingx_public_get("/openApi/swap/v2/quote/premiumIndex", {"symbol": real_symbol})

    if not klines or len(klines) < 50:
        return None

    klines = sorted(klines, key=lambda x: int(x["time"]))
    closes = np.array([float(k["close"]) for k in klines], dtype=float)
    highs = np.array([float(k["high"]) for k in klines], dtype=float)
    lows = np.array([float(k["low"]) for k in klines], dtype=float)
    volumes = np.array([float(k["volume"]) for k in klines], dtype=float)

    price = closes[-1]
    ema9 = calc_ema(closes, 9)
    ema21 = calc_ema(closes, 21)
    ema50 = calc_ema(closes, 50)
    ema200 = calc_ema(closes, 200 if len(closes) >= 200 else len(closes) - 1)

    e9, e21, e50, e200 = ema9[-1], ema21[-1], ema50[-1], ema200[-1]
    ema_slope = ((ema21[-1] - ema21[-5]) / (ema21[-5] + 1e-9)) * 100

    ema_dir = 0
    if e9 > e21: ema_dir += 30
    else: ema_dir -= 30
    if e21 > e50: ema_dir += 30
    else: ema_dir -= 30
    if price > e200: ema_dir += 25
    else: ema_dir -= 25
    ema_dir += np.clip(ema_slope * 20, -15, 15)

    rsi = calc_rsi(closes, 14)
    ema12 = calc_ema(closes, 12)
    ema26 = calc_ema(closes, 26)
    macd_line = ema12 - ema26
    signal_line = calc_ema(macd_line, 9)
    macd_hist = macd_line[-1] - signal_line[-1]
    adx = calc_adx(highs, lows, closes, 14)

    mom_dir = 0
    if 52 < rsi < 74: mom_dir += 40
    elif rsi >= 74: mom_dir -= 20
    elif 26 < rsi < 48: mom_dir -= 40
    elif rsi <= 26: mom_dir += 20

    if macd_hist > 0 and macd_line[-1] > signal_line[-1]:
        mom_dir += 40
    elif macd_hist < 0 and macd_line[-1] < signal_line[-1]:
        mom_dir -= 40

    adx_multiplier = 1.2 if adx >= 25 else 0.85
    mom_dir = np.clip(mom_dir * adx_multiplier, -100, 100)

    sma20 = np.mean(closes[-20:])
    std20 = np.std(closes[-20:]) + 1e-9
    bb_upper = sma20 + 2 * std20
    bb_lower = sma20 - 2 * std20
    bb_percent = ((price - bb_lower) / (bb_upper - bb_lower + 1e-9)) * 100
    z_score = (price - sma20) / std20

    x_idx = np.arange(20)
    corr_matrix = np.corrcoef(x_idx, closes[-20:])
    r_squared = float(corr_matrix[0, 1] ** 2) if not np.isnan(corr_matrix[0, 1]) else 0.0

    vol_dir = 0
    if 0.3 < z_score < 2.1: vol_dir += 50 * r_squared + 30
    elif -2.1 < z_score < -0.3: vol_dir -= 50 * r_squared + 30
    vol_dir = np.clip(vol_dir, -100, 100)

    bid_vol = sum(float(b[1]) for b in (depth.get("bids", []) if depth else []))
    ask_vol = sum(float(a[1]) for a in (depth.get("asks", []) if depth else []))
    ob_imbalance = ((bid_vol - ask_vol) / (bid_vol + ask_vol + 1e-9)) * 100

    funding_rate = float(premium.get("lastFundingRate", 0) or 0) * 100 if isinstance(premium, dict) else 0.0
    vol_ratio = volumes[-1] / (np.mean(volumes[-20:]) + 1e-9)

    calc_sentiment = np.clip(
        ob_imbalance * 0.7 + (20 if vol_ratio > 1.3 and closes[-1] > closes[-2] else (-20 if vol_ratio > 1.3 else 0)),
        -100,
        100
    )
    final_sentiment = float(custom_sentiment) if custom_sentiment is not None else float(calc_sentiment)

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
    ) / total_w

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

    atr = calc_atr(highs, lows, closes, 14)
    atr_pct = (atr / (price + 1e-9)) * 100
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

    stop_dist = max(abs(entry - sl), 1e-9)
    rr_ratio = round(abs(tp2 - entry) / stop_dist, 2)
    tp_plan = compute_tp_timeframe_plan(interval, adx)

    max_lev = CONTRACTS_CACHE["max_lev_map"].get(real_symbol, 50)
    capital = float(BRAIN_WEIGHTS.get("account_capital", 1000.0))
    risk_pct = float(BRAIN_WEIGHTS.get("risk_per_trade_pct", 1.5))
    rec_lev = calc_recommended_leverage(atr_pct, max_lev, capital)

    entry_rule, recommended_params = evaluate_profit_math_and_entry(
        signal=signal, confidence=confidence, rr_ratio=rr_ratio,
        adx=adx, r_squared=r_squared, z_score=z_score, rsi=rsi,
        min_conf=min_conf, capital=capital, risk_pct=risk_pct,
        stop_dist=stop_dist, price=price, rec_leverage=rec_lev
    )

    exact_name = bingx_exact_name(real_symbol)
    return {
        "symbol": real_symbol,
        "bingx_name": exact_name,
        "display_name": exact_name,
        "price": price,
        "signal": signal,
        "entry_rule": entry_rule,
        "recommended_params": recommended_params,
        "tp_plan": tp_plan,
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


def maybe_auto_execute_trade(analysis: dict):
    if not BRAIN_WEIGHTS.get("auto_trade_enabled", False):
        return
    if not BINGX_API_KEY or not BINGX_SECRET_KEY:
        return

    total_open_pnl = sum(p.get("unrealized_pnl", 0.0) for p in AUTO_SCAN_STATE["open_positions"].values())
    if total_open_pnl <= -abs(float(BRAIN_WEIGHTS.get("max_daily_loss_usdt", 100.0))):
        return

    sig = analysis.get("signal", "")
    action = analysis.get("entry_rule", {}).get("action", "")
    if action != "ENTER NOW" or sig not in ("STRONG LONG", "STRONG SHORT"):
        return

    sym = analysis["symbol"]
    if sym in AUTO_SCAN_STATE["open_positions"]:
        return

    now = time.time()
    cooldown = int(BRAIN_WEIGHTS.get("auto_trade_cooldown_sec", 300))
    last_ts = AUTO_SCAN_STATE["last_auto_trade"].get(sym, 0)
    if now - last_ts < cooldown:
        return

    rp = analysis.get("recommended_params", {})
    qty = rp.get("qty", 0)
    if not qty or float(qty) <= 0:
        return

    side = "BUY" if "LONG" in sig else "SELL"
    sl = analysis["levels"]["sl"]
    tp = analysis["levels"]["tp2"]
    lev = rp.get("leverage", 10)

    AUTO_SCAN_STATE["last_auto_trade"][sym] = now
    place_bingx_real_order(sym, side, qty, sl, tp, leverage=lev)


def auto_market_monitor_loop():
    AUTO_SCAN_STATE["running"] = True
    while True:
        try:
            if BRAIN_WEIGHTS.get("auto_monitor_enabled", True):
                contracts = get_all_bingx_contracts()
                top_symbols = [c["symbol"] for c in contracts[:25]]
                interval = AUTO_SCAN_STATE.get("interval", "15m")
                updated = dict(AUTO_SCAN_STATE["signals"])
                for sym in top_symbols:
                    res = analyze_symbol_math(sym, interval)
                    if res:
                        updated[sym] = res
                        maybe_auto_execute_trade(res)
                    time.sleep(0.15)

                AUTO_SCAN_STATE["signals"] = updated
                AUTO_SCAN_STATE["open_positions"] = fetch_open_bingx_positions(updated)
                AUTO_SCAN_STATE["last_run"] = time.strftime("%H:%M:%S")
        except Exception as e:
            print(f"[AutoMonitor Error] {e}")

        sleep_sec = max(10, int(BRAIN_WEIGHTS.get("auto_scan_interval", 25)))
        time.sleep(sleep_sec)


monitor_thread = threading.Thread(target=auto_market_monitor_loop, daemon=True)
monitor_thread.start()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/keys", methods=["GET", "POST"])
def api_keys():
    global BINGX_API_KEY, BINGX_SECRET_KEY
    if request.method == "POST":
        data = request.json or {}
        ak = (data.get("api_key") or "").strip()
        sk = (data.get("secret_key") or "").strip()
        if not ak or not sk:
            return jsonify({"status": "error", "msg": "Inserisci sia API Key che Secret Key"}), 400
        BINGX_API_KEY = ak
        BINGX_SECRET_KEY = sk
        save_keys_to_disk(ak, sk)
        AUTO_SCAN_STATE["open_positions"] = fetch_open_bingx_positions(AUTO_SCAN_STATE["signals"])
        return jsonify({"status": "ok", "msg": "Chiavi API BingX salvate e attivate!"})

    masked_key = f"{BINGX_API_KEY[:6]}...{BINGX_API_KEY[-4:]}" if len(BINGX_API_KEY) > 10 else ("Configurata" if BINGX_API_KEY else "Non impostata")
    masked_sec = f"{BINGX_SECRET_KEY[:4]}...{BINGX_SECRET_KEY[-4:]}" if len(BINGX_SECRET_KEY) > 8 else ("Configurata" if BINGX_SECRET_KEY else "Non impostata")
    return jsonify({
        "status": "ok",
        "has_keys": bool(BINGX_API_KEY and BINGX_SECRET_KEY),
        "masked_api_key": masked_key,
        "masked_secret_key": masked_sec
    })


@app.route("/api/markets")
def api_markets():
    force = request.args.get("force", "0") == "1"
    contracts = get_all_bingx_contracts(force=force)
    return jsonify({"status": "ok", "count": len(contracts), "markets": contracts})


@app.route("/api/close_position", methods=["POST"])
def api_close_position():
    data = request.json or {}
    sym = data.get("symbol")
    side = data.get("side")
    qty = data.get("quantity")
    if not all([sym, side, qty]):
        return jsonify({"status": "error", "msg": "Dati chiusura mancanti"}), 400
    res = close_bingx_position(sym, side, qty)
    AUTO_SCAN_STATE["open_positions"] = fetch_open_bingx_positions(AUTO_SCAN_STATE["signals"])
    return jsonify(res)


@app.route("/api/auto_monitor_status", methods=["GET", "POST"])
def api_auto_monitor_status():
    if request.method == "POST":
        data = request.json or {}
        if "enabled" in data:
            BRAIN_WEIGHTS["auto_monitor_enabled"] = bool(data["enabled"])
        if "auto_trade_enabled" in data:
            BRAIN_WEIGHTS["auto_trade_enabled"] = bool(data["auto_trade_enabled"])
        if "interval" in data:
            AUTO_SCAN_STATE["interval"] = data["interval"]
        if "scan_interval_sec" in data:
            BRAIN_WEIGHTS["auto_scan_interval"] = max(10, int(data["scan_interval_sec"]))

    positions = fetch_open_bingx_positions(AUTO_SCAN_STATE["signals"])
    AUTO_SCAN_STATE["open_positions"] = positions

    return jsonify({
        "status": "ok",
        "running": AUTO_SCAN_STATE["running"],
        "enabled": BRAIN_WEIGHTS.get("auto_monitor_enabled", True),
        "auto_trade_enabled": BRAIN_WEIGHTS.get("auto_trade_enabled", False),
        "last_run": AUTO_SCAN_STATE["last_run"],
        "interval": AUTO_SCAN_STATE["interval"],
        "scan_interval_sec": BRAIN_WEIGHTS.get("auto_scan_interval", 25),
        "signals": AUTO_SCAN_STATE["signals"],
        "open_positions": positions,
        "trade_logs": AUTO_SCAN_STATE["trade_logs"]
    })


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
        return jsonify({"status": "error", "msg": "Contratto non trovato o dati insufficienti su BingX"}), 400
    return jsonify({"status": "ok", "data": res})


@app.route("/api/scan_top", methods=["POST"])
def api_scan_top():
    data = request.json or {}
    symbols = data.get("symbols", [])
    interval = data.get("interval", "15m")
    overrides = data.get("overrides", {})

    results = []
    for sym in symbols[:30]:
        ov = overrides.get(sym, {})
        custom_sent = ov.get("sentiment", None)
        analysis = analyze_symbol_math(sym, interval, custom_sent)
        if analysis:
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
                if k in ("auto_monitor_enabled", "auto_trade_enabled"):
                    BRAIN_WEIGHTS[k] = bool(data[k])
                else:
                    BRAIN_WEIGHTS[k] = float(data[k])
    return jsonify({"status": "ok", "weights": BRAIN_WEIGHTS})


@app.route("/api/account_balance")
def api_account_balance():
    bal = bingx_private_get("/openApi/swap/v3/user/balance")
    if not bal:
        return jsonify({"status": "demo", "balance": BRAIN_WEIGHTS.get("account_capital", 1000.0)})
    usdt_bal = 0.0
    if isinstance(bal, list):
        for item in bal:
            if item.get("asset") == "USDT":
                usdt_bal = float(item.get("balance", 0) or 0)
    elif isinstance(bal, dict):
        data_obj = bal.get("balance", bal)
        usdt_bal = float(data_obj.get("balance", 0) or 0)
    BRAIN_WEIGHTS["account_capital"] = round(usdt_bal, 2)
    return jsonify({"status": "ok", "balance": round(usdt_bal, 2)})


@app.route("/api/execute_order", methods=["POST"])
def api_execute_order():
    data = request.json or {}
    symbol = data.get("symbol", "")
    side = data.get("side")
    quantity = data.get("quantity")
    sl = data.get("sl")
    tp = data.get("tp")
    leverage = data.get("leverage")

    if not all([symbol, side, quantity]):
        return jsonify({"status": "error", "msg": "Parametri ordine incompleti"}), 400

    res = place_bingx_real_order(symbol, side, quantity, sl, tp, leverage=leverage)
    AUTO_SCAN_STATE["open_positions"] = fetch_open_bingx_positions(AUTO_SCAN_STATE["signals"])
    return jsonify(res)


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
