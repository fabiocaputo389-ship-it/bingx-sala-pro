import os
import json
import time
import hmac
import hashlib
import threading
import urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
import numpy as np
from flask import Flask, render_template, jsonify, request
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

KEYS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".bingx_keys.json")
BASE_URL = "https://open-api.bingx.com"
ROME_TZ = ZoneInfo("Europe/Rome")


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

INTERVAL_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400
}

TIMEFRAME_PROFILES = {
    "1m": {
        "label": "1m Scalping HFT",
        "ema_weight": 20,
        "momentum_weight": 35,
        "volatility_weight": 25,
        "sentiment_weight": 20,
        "min_confidence": 72,
        "sl_mult": 1.4,
        "tp1_mult": 2.0,
        "tp2_mult": 3.0,
        "tp3_mult": 4.2,
        "breakeven_at_tp_pct": 45.0,
        "risk_per_trade_pct": 1.0,
        "auto_scan_interval": 8
    },
    "5m": {
        "label": "5m Intraday Fast",
        "ema_weight": 25,
        "momentum_weight": 35,
        "volatility_weight": 22,
        "sentiment_weight": 18,
        "min_confidence": 74,
        "sl_mult": 1.5,
        "tp1_mult": 2.2,
        "tp2_mult": 3.2,
        "tp3_mult": 4.6,
        "breakeven_at_tp_pct": 50.0,
        "risk_per_trade_pct": 1.25,
        "auto_scan_interval": 10
    },
    "15m": {
        "label": "15m Istituzionale",
        "ema_weight": 35,
        "momentum_weight": 30,
        "volatility_weight": 20,
        "sentiment_weight": 15,
        "min_confidence": 75,
        "sl_mult": 1.5,
        "tp1_mult": 2.2,
        "tp2_mult": 3.5,
        "tp3_mult": 5.0,
        "breakeven_at_tp_pct": 50.0,
        "risk_per_trade_pct": 1.5,
        "auto_scan_interval": 12
    },
    "1h": {
        "label": "1H Swing Trend",
        "ema_weight": 38,
        "momentum_weight": 28,
        "volatility_weight": 20,
        "sentiment_weight": 14,
        "min_confidence": 77,
        "sl_mult": 1.6,
        "tp1_mult": 2.4,
        "tp2_mult": 3.8,
        "tp3_mult": 5.5,
        "breakeven_at_tp_pct": 55.0,
        "risk_per_trade_pct": 1.75,
        "auto_scan_interval": 15
    },
    "4h": {
        "label": "4H Macro Trend",
        "ema_weight": 42,
        "momentum_weight": 25,
        "volatility_weight": 20,
        "sentiment_weight": 13,
        "min_confidence": 78,
        "sl_mult": 1.8,
        "tp1_mult": 2.6,
        "tp2_mult": 4.2,
        "tp3_mult": 6.0,
        "breakeven_at_tp_pct": 55.0,
        "risk_per_trade_pct": 2.0,
        "auto_scan_interval": 20
    }
}

MARKET_PROFILES = {
    "CRYPTO MAJOR": {
        "label": "Crypto Major 24/7",
        "ema_delta": 0,
        "momentum_delta": 5,
        "volatility_delta": -5,
        "sentiment_delta": 0,
        "conf_delta": 0,
        "sl_mult": 1.5,
        "tp1_mult": 2.2,
        "tp2_mult": 3.6,
        "tp3_mult": 5.2,
        "breakeven_at_tp_pct": 50.0,
        "risk_per_trade_pct": 1.5
    },
    "ALTCOIN & DEFI": {
        "label": "Altcoin & DeFi Alta Beta",
        "ema_delta": -5,
        "momentum_delta": 5,
        "volatility_delta": -5,
        "sentiment_delta": 5,
        "conf_delta": 2,
        "sl_mult": 1.7,
        "tp1_mult": 2.5,
        "tp2_mult": 4.0,
        "tp3_mult": 5.8,
        "breakeven_at_tp_pct": 45.0,
        "risk_per_trade_pct": 1.0
    },
    "METALLI & COMMODITIES": {
        "label": "Metalli & Commodities (Gold/Silver/Oil)",
        "ema_delta": 5,
        "momentum_delta": 0,
        "volatility_delta": 5,
        "sentiment_delta": -10,
        "conf_delta": 1,
        "sl_mult": 1.5,
        "tp1_mult": 2.2,
        "tp2_mult": 3.5,
        "tp3_mult": 5.0,
        "breakeven_at_tp_pct": 50.0,
        "risk_per_trade_pct": 1.5
    },
    "FOREX & VALUTE": {
        "label": "Forex & Valute",
        "ema_delta": 5,
        "momentum_delta": -5,
        "volatility_delta": 5,
        "sentiment_delta": -5,
        "conf_delta": 2,
        "sl_mult": 1.4,
        "tp1_mult": 2.0,
        "tp2_mult": 3.2,
        "tp3_mult": 4.5,
        "breakeven_at_tp_pct": 50.0,
        "risk_per_trade_pct": 1.25
    },
    "INDICI & AZIONI": {
        "label": "Indici & Azioni US",
        "ema_delta": 5,
        "momentum_delta": 0,
        "volatility_delta": 0,
        "sentiment_delta": -5,
        "conf_delta": 1,
        "sl_mult": 1.5,
        "tp1_mult": 2.2,
        "tp2_mult": 3.5,
        "tp3_mult": 5.0,
        "breakeven_at_tp_pct": 50.0,
        "risk_per_trade_pct": 1.5
    }
}

BRAIN_WEIGHTS = {
    "ema_weight": 35,
    "momentum_weight": 30,
    "volatility_weight": 20,
    "sentiment_weight": 15,
    "min_confidence": 70,
    "sl_mult": 1.5,
    "tp1_mult": 2.2,
    "tp2_mult": 3.5,
    "tp3_mult": 5.0,
    "auto_scan_interval": 12,
    "auto_monitor_enabled": True,
    "auto_trade_enabled": False,
    "auto_close_on_tp": True,
    "auto_close_on_candle_end": True,
    "auto_close_on_candle_signal": True,
    "auto_trade_cooldown_sec": 60,
    "breakeven_at_tp_pct": 50.0,
    "max_daily_loss_usdt": 100.0,
    "account_capital": 0.0,
    "risk_per_trade_pct": 1.5,
    "max_entry_atr_stretch": 1.55,
    "max_entry_z_abs": 2.60
}

BALANCE_STATE = {
    "balance_usdt": 0.0,
    "available_usdt": 0.0,
    "unrealized_pnl_usdt": 0.0,
    "equity_usdt": 0.0,
    "source": "init",
    "last_sync": None,
    "last_error": None
}

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

CONTRACTS_CACHE = {
    "timestamp": 0,
    "data": [],
    "symbol_set": set(),
    "max_lev_map": {},
    "contract_meta": {}
}

AUTO_SCAN_STATE = {
    "running": False,
    "last_run": None,
    "interval": "15m",
    "active_category": "ALL",
    "only_certain_and_early": False,
    "signals": {},
    "last_auto_trade": {},
    "managed_trades": {},
    "breakeven_moved": set(),
    "trade_logs": [],
    "open_positions": {},
    "audio_alerts": [],
    "last_seen_candle_index_by_tf": {},
    "latest_new_candle_alert": None,
    "new_candle_alerts": []
}


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


def compute_candle_timing(interval: str, klines: list = None, now_ts: float = None):
    if now_ts is None:
        now_ts = time.time()
    sec_per_candle = INTERVAL_SECONDS.get(interval, 900)

    latest_open_ms = None
    if klines and len(klines) > 0:
        try:
            latest_open_ms = int(klines[-1]["time"])
        except Exception:
            latest_open_ms = None

    if latest_open_ms and latest_open_ms > 0:
        candle_start_sec = latest_open_ms / 1000.0
        elapsed = max(0.0, now_ts - candle_start_sec)
        if elapsed > sec_per_candle * 1.5:
            candle_start_sec = (int(now_ts) // sec_per_candle) * sec_per_candle
            elapsed = max(0.0, now_ts - candle_start_sec)
    else:
        candle_start_sec = (int(now_ts) // sec_per_candle) * sec_per_candle
        elapsed = max(0.0, now_ts - candle_start_sec)

    remaining = max(0.0, sec_per_candle - elapsed)
    rem_int = int(round(remaining))
    mins, secs = divmod(rem_int, 60)
    hrs, mins = divmod(mins, 60)
    if hrs > 0:
        countdown_str = f"{hrs:02d}:{mins:02d}:{secs:02d}"
    else:
        countdown_str = f"{mins:02d}:{secs:02d}"

    pre_close_window_sec = min(45.0, max(15.0, sec_per_candle * 0.25))
    before_next_candle = remaining <= pre_close_window_sec
    candle_index = int(candle_start_sec // sec_per_candle)
    open_clock_rome = datetime.fromtimestamp(candle_start_sec, tz=ROME_TZ).strftime("%H:%M:%S")
    next_open_clock_rome = datetime.fromtimestamp(candle_start_sec + sec_per_candle, tz=ROME_TZ).strftime("%H:%M:%S")

    return {
        "timeframe": interval,
        "candle_duration_sec": sec_per_candle,
        "candle_index": candle_index,
        "candle_start_ts": int(candle_start_sec),
        "next_candle_ts": int(candle_start_sec + sec_per_candle),
        "open_clock_rome": open_clock_rome,
        "next_open_clock_rome": next_open_clock_rome,
        "elapsed_sec": int(elapsed),
        "seconds_to_next_candle": rem_int,
        "countdown_str": countdown_str,
        "pre_close_window_sec": int(pre_close_window_sec),
        "before_next_candle": bool(before_next_candle),
        "phase_label": "⏱ PRE-CANDELA (APERTURA PROSSIMA)" if before_next_candle else f"CANDELA {interval} IN CORSO"
    }


def format_signal_full_spec(item: dict) -> str:
    lv = item.get("levels") or {}
    rp = item.get("recommended_params") or {}
    tp_plan = item.get("tp_plan") or {}
    side_label = (
        item.get("certain_signal")
        or item.get("signal")
        or ((item.get("early_setup") or {}).get("direction"))
        or "LONG"
    )
    entry_px = lv.get("entry", item.get("price", "--"))
    sl_px = lv.get("sl", "--")
    tp1_px = lv.get("tp1", "--")
    tp2_px = lv.get("tp2", "--")
    tp3_px = lv.get("tp3", "--")
    lev_val = rp.get("leverage", "--")
    qty_val = rp.get("qty", 0)
    plan_lbl = tp_plan.get("label", "")

    spec = (
        f"{item.get('bingx_name') or item.get('symbol')} {side_label} | "
        f"Entry: {entry_px} | TP1: {tp1_px} | TP2: {tp2_px} | TP3: {tp3_px} | "
        f"SL: {sl_px} | Lev: {lev_val}x | Qty: {qty_val}"
    )
    if plan_lbl:
        spec += f" ({plan_lbl})"
    return spec


def record_new_candle_written_alert(interval: str, candle_timing: dict, analyses_map: dict):
    """Registra un avviso scritto di nuova candela includendo SOLO gli asset liberi da blocchi con Entry, TP1/TP2/TP3, SL e Leva."""
    if not candle_timing:
        return None

    tf_key = f"{interval}_{candle_timing.get('candle_index')}"
    prev_idx = AUTO_SCAN_STATE["last_seen_candle_index_by_tf"].get(interval)
    curr_idx = candle_timing.get("candle_index")

    if prev_idx is None:
        AUTO_SCAN_STATE["last_seen_candle_index_by_tf"][interval] = curr_idx
        return AUTO_SCAN_STATE.get("latest_new_candle_alert")

    if curr_idx is None or curr_idx <= prev_idx:
        return AUTO_SCAN_STATE.get("latest_new_candle_alert")

    AUTO_SCAN_STATE["last_seen_candle_index_by_tf"][interval] = curr_idx
    open_clock = candle_timing.get("open_clock_rome") or time.strftime("%H:%M:%S")

    free_candidates = []
    for sym, item in (analyses_map or {}).items():
        if not item:
            continue
        ew = item.get("entry_window") or {}
        sn = item.get("sn_rejection") or {}
        ms = item.get("manual_sign") or {}
        sess = item.get("session") or {}

        is_blocked = (
            bool(ew.get("exceeded"))
            or bool(sn.get("rejected"))
            or ms.get("status") in ("CANCELLED_REVERSAL", "REJECTED_SN", "OVER_THRESHOLD")
            or not bool(sess.get("session_ok", True))
        )
        if is_blocked:
            continue

        dir_str = str(item.get("certain_signal") or (item.get("early_setup") or {}).get("direction") or item.get("signal") or "")
        if "LONG" in dir_str or "SHORT" in dir_str:
            free_candidates.append(item)

    free_candidates.sort(
        key=lambda x: (
            1 if x.get("certain_signal") else 0,
            float(x.get("confidence") or 0.0)
        ),
        reverse=True
    )

    if free_candidates:
        top_items = free_candidates[:3]
        parts = [format_signal_full_spec(c) for c in top_items]

        best = top_items[0]
        lv0 = best.get("levels") or {}
        rp0 = best.get("recommended_params") or {}
        side0 = best.get("certain_signal") or best.get("signal") or ((best.get("early_setup") or {}).get("direction"))

        alert_obj = {
            "id": tf_key,
            "time": open_clock,
            "timeframe": interval,
            "blocked": False,
            "free_count": len(free_candidates),
            "symbol": best.get("bingx_name") or best.get("symbol"),
            "direction": side0,
            "entry": lv0.get("entry", best.get("price")),
            "tp1": lv0.get("tp1", "--"),
            "tp2": lv0.get("tp2", "--"),
            "tp3": lv0.get("tp3", "--"),
            "sl": lv0.get("sl", "--"),
            "leverage": rp0.get("leverage", "--"),
            "qty": rp0.get("qty", 0),
            "text": f"Nuova candela {interval} aperta alle {open_clock} — LIBERI ({len(free_candidates)}): " + " • ".join(parts)
        }
    else:
        sn_count = sum(
            1 for v in (analyses_map or {}).values()
            if v and (v.get("sn_rejection") or {}).get("rejected")
        )
        over_count = sum(
            1 for v in (analyses_map or {}).values()
            if v and (v.get("entry_window") or {}).get("exceeded")
        )
        reason_parts = []
        if sn_count:
            reason_parts.append(f"{sn_count} rifiutati per SN candela alta opposta")
        if over_count:
            reason_parts.append(f"{over_count} oltre soglia")
        reason = ", ".join(reason_parts) if reason_parts else "nessun asset libero da blocchi"

        alert_obj = {
            "id": tf_key,
            "time": open_clock,
            "timeframe": interval,
            "blocked": True,
            "free_count": 0,
            "reason": reason,
            "text": f"Nuova candela {interval} aperta alle {open_clock} — NESSUN ASSET LIBERO ({reason})."
        }

    AUTO_SCAN_STATE["latest_new_candle_alert"] = alert_obj
    AUTO_SCAN_STATE["new_candle_alerts"].insert(0, alert_obj)
    AUTO_SCAN_STATE["new_candle_alerts"] = AUTO_SCAN_STATE["new_candle_alerts"][:30]
    return alert_obj


def auto_tune_brain_for_category_and_tf(interval: str, category: str):
    clean_cat = category if category in MARKET_PROFILES else "CRYPTO MAJOR"
    tf = TIMEFRAME_PROFILES.get(interval, TIMEFRAME_PROFILES["15m"])
    mk = MARKET_PROFILES[clean_cat]

    w_ema = max(10, min(70, int(tf["ema_weight"] + mk["ema_delta"])))
    w_mom = max(10, min(70, int(tf["momentum_weight"] + mk["momentum_delta"])))
    w_vol = max(10, min(60, int(tf["volatility_weight"] + mk["volatility_delta"])))
    w_sent = max(5, min(50, int(tf["sentiment_weight"] + mk["sentiment_delta"])))

    auto_min_conf = max(60, min(82, int(tf["min_confidence"] + mk["conf_delta"] - 6)))
    sl_mult = round((tf["sl_mult"] + mk["sl_mult"]) / 2.0, 2)
    tp1_mult = round((tf["tp1_mult"] + mk["tp1_mult"]) / 2.0, 2)
    tp2_mult = round((tf["tp2_mult"] + mk["tp2_mult"]) / 2.0, 2)
    tp3_mult = round((tf["tp3_mult"] + mk["tp3_mult"]) / 2.0, 2)
    be_pct = round((tf["breakeven_at_tp_pct"] + mk["breakeven_at_tp_pct"]) / 2.0, 1)
    risk_pct = round((tf["risk_per_trade_pct"] + mk["risk_per_trade_pct"]) / 2.0, 2)

    BRAIN_WEIGHTS["ema_weight"] = w_ema
    BRAIN_WEIGHTS["momentum_weight"] = w_mom
    BRAIN_WEIGHTS["volatility_weight"] = w_vol
    BRAIN_WEIGHTS["sentiment_weight"] = w_sent
    BRAIN_WEIGHTS["min_confidence"] = auto_min_conf
    BRAIN_WEIGHTS["sl_mult"] = sl_mult
    BRAIN_WEIGHTS["tp1_mult"] = tp1_mult
    BRAIN_WEIGHTS["tp2_mult"] = tp2_mult
    BRAIN_WEIGHTS["tp3_mult"] = tp3_mult
    BRAIN_WEIGHTS["breakeven_at_tp_pct"] = be_pct
    BRAIN_WEIGHTS["risk_per_trade_pct"] = risk_pct
    BRAIN_WEIGHTS["auto_scan_interval"] = tf["auto_scan_interval"]

    return {
        "profile_name": f"AUTO • {mk['label']} • {tf['label']}",
        "category": clean_cat,
        "timeframe": interval,
        "min_confidence": auto_min_conf,
        "weights": dict(BRAIN_WEIGHTS)
    }


def get_market_session_info(category: str, bingx_name: str = "", api_symbol: str = ""):
    now_rome = datetime.now(ROME_TZ)
    weekday = now_rome.weekday()
    minutes = now_rome.hour * 60 + now_rome.minute
    time_str = now_rome.strftime("%H:%M CET")
    combined = f"{category} {bingx_name} {api_symbol}".upper()

    if category in ("CRYPTO MAJOR", "ALTCOIN & DEFI") or any(k in combined for k in ["BTC", "ETH", "SOL", "XRP", "BNB", "HYPE", "ZEC", "JTO", "PEOPLE", "SUI", "PENDLE"]):
        peak = (8 * 60) <= minutes <= (23 * 60)
        return {
            "market_type": "CRYPTO 24/7",
            "session_name": "Sessione Attiva (Liquidità Alta)" if peak else "Sessione Notturna 24/7",
            "hours_window": "00:00–24:00 (24/7)",
            "rome_time": time_str,
            "status": "OPEN",
            "session_ok": True,
            "badge": "🟢 24/7 APERTO" if peak else "🟡 24/7 NOTTE"
        }

    if weekday >= 5:
        return {
            "market_type": category,
            "session_name": "Chiusura Weekend",
            "hours_window": "Lun–Ven",
            "rome_time": time_str,
            "status": "CLOSED",
            "session_ok": False,
            "badge": "🔴 CHIUSO (WKND)"
        }

    if category == "INDICI & AZIONI" or any(k in combined for k in ["NASDAQ100", "NDX", "NAS100", "SOXL", "GS"]):
        if (15 * 60 + 30) <= minutes <= (22 * 60):
            return {
                "market_type": "INDICI & AZIONI US",
                "session_name": "Wall Street Cash Session",
                "hours_window": "15:30–22:00 CET",
                "rome_time": time_str,
                "status": "OPEN",
                "session_ok": True,
                "badge": "🟢 WS APERTO"
            }
        elif (14 * 60) <= minutes <= (23 * 60):
            return {
                "market_type": "INDICI & AZIONI US",
                "session_name": "Pre/Post Market US",
                "hours_window": "14:00–23:00 CET",
                "rome_time": time_str,
                "status": "PRE_POST",
                "session_ok": True,
                "badge": "🟡 PRE/POST WS"
            }
        else:
            return {
                "market_type": "INDICI & AZIONI US",
                "session_name": "Fuori Sessione US",
                "hours_window": "15:30–22:00 CET",
                "rome_time": time_str,
                "status": "CLOSED",
                "session_ok": False,
                "badge": "🔴 FUORI ORARIO"
            }

    if category == "FOREX & VALUTE" or "EUR" in combined:
        if (8 * 60) <= minutes <= (22 * 60):
            overlap = (13 * 60 + 30) <= minutes <= (17 * 60 + 30)
            return {
                "market_type": "FOREX FX",
                "session_name": "Overlap Londra + NY" if overlap else "Sessione EU / US",
                "hours_window": "08:00–22:00 CET",
                "rome_time": time_str,
                "status": "OPEN",
                "session_ok": True,
                "badge": "🟢 FX OVERLAP" if overlap else "🟢 FX APERTO"
            }
        return {
            "market_type": "FOREX FX",
            "session_name": "Sessione Asiatica / Bassa Volatilità",
            "hours_window": "08:00–22:00 CET",
            "rome_time": time_str,
            "status": "LOW_VOL",
            "session_ok": False,
            "badge": "🟡 FX CALMO"
        }

    if (8 * 60) <= minutes <= (23 * 60):
        us_open = (14 * 60 + 30) <= minutes <= (22 * 60)
        return {
            "market_type": "METALLI & COMMODITIES",
            "session_name": "Sessione US + London" if us_open else "Sessione Europea",
            "hours_window": "08:00–23:00 CET",
            "rome_time": time_str,
            "status": "OPEN",
            "session_ok": True,
            "badge": "🟢 COMM APERTO"
        }

    return {
        "market_type": "METALLI & COMMODITIES",
        "session_name": "Pausa Notturna / Bassa Liquidità",
        "hours_window": "08:00–23:00 CET",
        "rome_time": time_str,
        "status": "CLOSED",
        "session_ok": False,
        "badge": "🔴 NOTTE COMM"
    }


def log_trade_event(symbol_label: str, side: str, qty, sl, tp, response):
    AUTO_SCAN_STATE["trade_logs"].insert(0, {
        "time": time.strftime("%H:%M:%S"),
        "symbol": symbol_label,
        "side": side,
        "qty": qty,
        "sl": sl,
        "tp": tp,
        "response": response
    })
    AUTO_SCAN_STATE["trade_logs"] = AUTO_SCAN_STATE["trade_logs"][:50]


def sign_request(params: dict) -> str:
    clean_params = {k: v for k, v in params.items() if v is not None and v != ""}
    sorted_items = sorted(clean_params.items(), key=lambda x: str(x[0]))
    query_string = urllib.parse.urlencode(sorted_items, quote_via=urllib.parse.quote)
    signature = hmac.new(
        BINGX_SECRET_KEY.strip().encode("utf-8"),
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
        print(f"[BingX Public GET Error] {endpoint}: {e}")
    return None


def bingx_private_get(endpoint: str, params: dict = None):
    if not BINGX_API_KEY or not BINGX_SECRET_KEY:
        BALANCE_STATE["last_error"] = "Chiavi API / Secret Key BingX mancanti"
        return None
    p = dict(params or {})
    p.setdefault("recvWindow", 5000)
    p["timestamp"] = int(time.time() * 1000)
    signed_qs = sign_request(p)
    url = f"{BASE_URL}{endpoint}?{signed_qs}"
    headers = {"X-BX-APIKEY": BINGX_API_KEY.strip()}
    try:
        resp = requests.get(url, headers=headers, timeout=8)
        data = resp.json()
        if data.get("code") == 0:
            BALANCE_STATE["last_error"] = None
            return data.get("data")
        BALANCE_STATE["last_error"] = f"code {data.get('code')}: {data.get('msg')}"
    except Exception as e:
        BALANCE_STATE["last_error"] = str(e)
    return None


def bingx_private_post(endpoint: str, params: dict):
    if not BINGX_API_KEY or not BINGX_SECRET_KEY:
        return {"code": -1, "msg": "API Key / Secret Key BingX mancanti."}
    p = dict(params or {})
    p.setdefault("recvWindow", 5000)
    p["timestamp"] = int(time.time() * 1000)
    signed_qs = sign_request(p)
    url = f"{BASE_URL}{endpoint}?{signed_qs}"
    headers = {"X-BX-APIKEY": BINGX_API_KEY.strip()}
    try:
        resp = requests.post(url, headers=headers, timeout=8)
        return resp.json()
    except Exception as e:
        return {"code": -1, "msg": str(e)}


def sync_bingx_balance_realtime() -> dict:
    now_str = time.strftime("%H:%M:%S")
    raw_bal = bingx_private_get("/openApi/swap/v3/user/balance")
    if raw_bal is None:
        raw_bal = bingx_private_get("/openApi/swap/v2/user/balance")

    if raw_bal is None:
        BALANCE_STATE["last_sync"] = now_str
        BALANCE_STATE["source"] = "offline"
        return dict(BALANCE_STATE)

    balance_usdt = 0.0
    available_usdt = 0.0
    unrealized_pnl = 0.0
    equity_usdt = 0.0

    items = []
    if isinstance(raw_bal, list):
        items = raw_bal
    elif isinstance(raw_bal, dict):
        if isinstance(raw_bal.get("balance"), list):
            items = raw_bal["balance"]
        elif isinstance(raw_bal.get("data"), list):
            items = raw_bal["data"]
        elif isinstance(raw_bal.get("balance"), dict):
            items = [raw_bal["balance"]]
        else:
            items = [raw_bal]

    for item in items:
        if not isinstance(item, dict):
            continue
        asset = str(item.get("asset", "USDT")).upper()
        if len(items) > 1 and asset != "USDT":
            continue
        balance_usdt = float(item.get("balance") or item.get("walletBalance") or item.get("equity") or 0.0)
        available_usdt = float(item.get("availableMargin") or item.get("availableBalance") or item.get("free") or balance_usdt)
        unrealized_pnl = float(item.get("unrealizedProfit") or item.get("crossUnPnl") or 0.0)
        equity_usdt = float(item.get("equity") or (balance_usdt + unrealized_pnl))
        break

    balance_usdt = round(max(0.0, balance_usdt), 4)
    available_usdt = round(max(0.0, available_usdt), 4)
    unrealized_pnl = round(unrealized_pnl, 4)
    equity_usdt = round(max(0.0, equity_usdt), 4)

    BALANCE_STATE.update({
        "balance_usdt": balance_usdt,
        "available_usdt": available_usdt,
        "unrealized_pnl_usdt": unrealized_pnl,
        "equity_usdt": equity_usdt,
        "source": "bingx_live",
        "last_sync": now_str,
        "last_error": None
    })
    BRAIN_WEIGHTS["account_capital"] = balance_usdt
    return dict(BALANCE_STATE)


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
    contract_meta = {}

    if raw_contracts:
        for c in raw_contracts:
            if c.get("status") != 1:
                continue
            sym = c.get("symbol")
            max_lev = int(c.get("maxLeverage", 50) or 50)
            min_qty = float(c.get("tradeMinQuantity") or c.get("minTradeQuantity") or 0.0)
            step_size = float(c.get("stepSize") or c.get("quantityPrecision") or 0.0)
            qty_precision = int(c.get("quantityPrecision", 3) or 3)
            price_precision = int(c.get("pricePrecision", 4) or 4)

            symbol_set.add(sym)
            max_lev_map[sym] = max_lev
            contract_meta[sym] = {
                "min_qty": min_qty,
                "step_size": step_size,
                "qty_precision": qty_precision,
                "price_precision": price_precision
            }

            tk = ticker_map.get(sym, {})
            vol_24h = float(tk.get("quoteVolume", 0) or 0)
            exact_name = bingx_exact_name(sym)
            cat = categorize_symbol(exact_name, sym)
            sess = get_market_session_info(cat, exact_name, sym)
            results.append({
                "symbol": sym,
                "bingxName": exact_name,
                "displayName": exact_name,
                "category": cat,
                "session": sess,
                "maxLeverage": max_lev,
                "minQty": min_qty,
                "pricePrecision": price_precision,
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
        CONTRACTS_CACHE["contract_meta"] = contract_meta
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


def set_bingx_leverage(symbol: str, position_side: str, leverage: int):
    return bingx_private_post("/openApi/swap/v2/trade/leverage", {
        "symbol": symbol,
        "side": position_side,
        "leverage": int(leverage)
    })


def normalize_order_qty(symbol: str, raw_qty: float, price: float, leverage: int, capital: float) -> float:
    meta = CONTRACTS_CACHE["contract_meta"].get(symbol, {})
    min_qty = float(meta.get("min_qty", 0.0) or 0.0)
    precision = int(meta.get("qty_precision", 3) or 3)
    precision = max(0, min(precision, 6))

    qty = max(0.0, float(raw_qty or 0.0))
    if qty > 0:
        qty = round(qty, precision)

    if qty > 0 and price > 0 and capital > 0:
        max_notional = capital * max(leverage, 1) * 0.90
        max_affordable_qty = round(max_notional / price, precision)
        if max_affordable_qty > 0 and qty > max_affordable_qty:
            qty = max_affordable_qty

    if min_qty > 0 and qty > 0 and qty < min_qty:
        min_notional = min_qty * price
        if capital > 0 and min_notional <= capital * max(leverage, 1) * 0.95:
            qty = round(min_qty, precision)
    return qty


def place_bingx_real_order(symbol: str, side: str, quantity, sl, tp, leverage: int = None):
    real_symbol = resolve_bingx_symbol(symbol)
    position_side = "LONG" if side == "BUY" else "SHORT"
    exact_label = bingx_exact_name(real_symbol)

    if float(quantity or 0) <= 0:
        err = {"code": -2, "msg": "Quantità ordine nulla o insufficiente per il saldo corrente."}
        log_trade_event(exact_label, side, quantity, sl, tp, err)
        return err

    if leverage:
        set_bingx_leverage(real_symbol, position_side, leverage)

    order_params = {
        "symbol": real_symbol,
        "side": side,
        "positionSide": position_side,
        "type": "MARKET",
        "quantity": str(quantity)
    }
    if sl is not None and float(sl) > 0:
        order_params["stopLoss"] = json.dumps({
            "type": "STOP_MARKET",
            "stopPrice": float(sl),
            "workingType": "MARK_PRICE"
        }, separators=(",", ":"))
    if tp is not None and float(tp) > 0:
        order_params["takeProfit"] = json.dumps({
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": float(tp),
            "workingType": "MARK_PRICE"
        }, separators=(",", ":"))

    res = bingx_private_post("/openApi/swap/v2/trade/order", order_params)
    log_trade_event(exact_label, side, quantity, sl, tp, res)
    sync_bingx_balance_realtime()
    return res


def close_bingx_position(symbol: str, side: str, quantity, reason: str = "MANUAL"):
    real_symbol = resolve_bingx_symbol(symbol)
    exact_label = bingx_exact_name(real_symbol)
    close_side = "SELL" if side.upper() == "LONG" else "BUY"
    res = bingx_private_post("/openApi/swap/v2/trade/order", {
        "symbol": real_symbol,
        "side": close_side,
        "positionSide": side.upper(),
        "type": "MARKET",
        "quantity": str(quantity)
    })
    log_trade_event(exact_label, f"CLOSE_{reason}_{side.upper()}", quantity, "-", "-", res)
    AUTO_SCAN_STATE["managed_trades"].pop(f"{real_symbol}_{side.upper()}", None)
    sync_bingx_balance_realtime()
    return res


def fetch_open_bingx_positions(signal_map: dict = None):
    raw_pos = bingx_private_get("/openApi/swap/v2/user/positions")
    pos_dict = {}
    if not isinstance(raw_pos, list):
        return pos_dict

    active_keys = set()
    for p in raw_pos:
        amt = float(p.get("positionAmt", 0) or 0)
        if abs(amt) <= 0:
            continue
        sym = p.get("symbol")
        side = (p.get("positionSide") or ("LONG" if amt > 0 else "SHORT")).upper()
        m_key = f"{sym}_{side}"
        active_keys.add(m_key)

        entry_px = float(p.get("avgPrice", 0) or 0)
        mark_px = float(p.get("markPrice", 0) or entry_px)
        pnl = float(p.get("unrealizedProfit", 0) or 0)
        lev = float(p.get("leverage", 1) or 1)

        sig_info = (signal_map or {}).get(sym, {})
        managed = AUTO_SCAN_STATE["managed_trades"].get(m_key, {})

        tp_target = float(managed.get("tp2") or sig_info.get("levels", {}).get("tp2", 0) or 0)
        sl_target = float(managed.get("sl") or sig_info.get("levels", {}).get("sl", 0) or 0)

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
        if progress_clamped >= be_threshold and m_key not in AUTO_SCAN_STATE["breakeven_moved"] and entry_px > 0:
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
            AUTO_SCAN_STATE["breakeven_moved"].add(m_key)

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
            "leverage": lev,
            "opened_candle_id": managed.get("opened_candle_id"),
            "opened_candle_index": managed.get("opened_candle_index"),
            "close_mode": managed.get("close_mode", "TP_OR_CANDLE_END")
        }

    for stale_key in list(AUTO_SCAN_STATE["managed_trades"].keys()):
        if stale_key not in active_keys:
            AUTO_SCAN_STATE["managed_trades"].pop(stale_key, None)

    return pos_dict


def monitor_and_manage_auto_positions(latest_analyses: dict, open_positions: dict):
    if not BINGX_API_KEY or not BINGX_SECRET_KEY:
        return
    if not open_positions:
        return

    auto_tp_close = bool(BRAIN_WEIGHTS.get("auto_close_on_tp", True))
    auto_candle_end_close = bool(BRAIN_WEIGHTS.get("auto_close_on_candle_end", True))
    auto_candle_close = bool(BRAIN_WEIGHTS.get("auto_close_on_candle_signal", True))

    for sym, pos in list(open_positions.items()):
        side = pos.get("side", "LONG")
        size = float(pos.get("size", 0) or 0)
        mark_px = float(pos.get("mark_price", 0) or 0)
        if size <= 0 or mark_px <= 0:
            continue

        m_key = f"{sym}_{side}"
        managed = AUTO_SCAN_STATE["managed_trades"].get(m_key, {})
        analysis = latest_analyses.get(sym)

        tp1 = float(managed.get("tp1") or (analysis or {}).get("levels", {}).get("tp1") or 0)
        tp2 = float(managed.get("tp2") or pos.get("tp_price") or 0)
        tp3 = float(managed.get("tp3") or (analysis or {}).get("levels", {}).get("tp3") or 0)
        active_tp = tp2 if tp2 > 0 else (tp1 if tp1 > 0 else tp3)

        if auto_tp_close and active_tp > 0:
            tp_reached = (mark_px >= active_tp) if side == "LONG" else (mark_px <= active_tp)
            if tp_reached:
                close_bingx_position(sym, side, size, reason=f"TP_HIT_{active_tp}")
                continue

        if analysis:
            timing = analysis.get("candle_timing") or {}
            curr_candle = int((analysis.get("early_setup") or {}).get("candle_id", 0) or 0)
            curr_candle_idx = int(timing.get("candle_index", 0) or 0)
            opened_candle = managed.get("opened_candle_id")
            opened_candle_idx = managed.get("opened_candle_index")

            new_sig = str(analysis.get("signal") or "")
            new_certain = str(analysis.get("certain_signal") or "")
            new_early_dir = str((analysis.get("early_setup") or {}).get("direction") or "")
            conf = float(analysis.get("confidence") or 0)
            min_conf = float(BRAIN_WEIGHTS.get("min_confidence", 70))

            opposite_signal = False
            if side == "LONG":
                opposite_signal = (
                    ("SHORT" in new_sig and conf >= min_conf)
                    or ("SHORT" in new_certain)
                    or (new_early_dir == "SHORT" and conf >= min_conf)
                )
            elif side == "SHORT":
                opposite_signal = (
                    ("LONG" in new_sig and conf >= min_conf)
                    or ("LONG" in new_certain)
                    or (new_early_dir == "LONG" and conf >= min_conf)
                )

            candle_changed = (
                (opened_candle is not None and curr_candle > int(opened_candle))
                or (opened_candle_idx is not None and curr_candle_idx > int(opened_candle_idx))
            )

            if auto_candle_close and opposite_signal and candle_changed:
                close_bingx_position(sym, side, size, reason=f"CANDLE_SIGNAL_{new_certain or new_sig}")
                continue

            if auto_candle_end_close and candle_changed:
                close_bingx_position(sym, side, size, reason="CANDLE_END")
                continue


def calc_ema(prices: np.ndarray, period: int) -> np.ndarray:
    alpha = 2.0 / (period + 1.0)
    ema = np.zeros_like(prices)
    ema[0] = prices[0]
    for i in range(1, len(prices)):
        ema[i] = alpha * prices[i] + (1.0 - alpha) * ema[i - 1]
    return ema


def calc_ema_series(values: np.ndarray, period: int) -> np.ndarray:
    alpha = 2.0 / (period + 1.0)
    out = np.zeros_like(values, dtype=float)
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = alpha * values[i] + (1.0 - alpha) * out[i - 1]
    return out


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


def calc_atr_series(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> np.ndarray:
    if len(closes) < 2:
        return np.array([float(highs[-1] - lows[-1])])
    tr = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(np.abs(highs[1:] - closes[:-1]), np.abs(lows[1:] - closes[:-1]))
    )
    atr_s = calc_ema_series(tr, min(period, len(tr)))
    return np.concatenate([[atr_s[0]], atr_s])


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


def detect_opposite_sn_high_candle_rejection(direction: str, current_price: float, atr: float, opens, highs, lows, closes):
    if not direction or len(closes) < 10 or atr <= 0:
        return {
            "rejected": False,
            "sn_level": None,
            "max_candle_atr": 0.0,
            "badge": "✅ SN LATO OPPOSTO LIBERO",
            "reason": "Nessun livello SN con candela alta sul lato opposto."
        }

    ranges = highs[:-1] - lows[:-1]
    avg_range = float(np.mean(ranges[-25:])) if len(ranges) >= 25 else float(np.mean(ranges))
    high_candle_threshold = max(1.65 * atr, 1.65 * avg_range)

    if direction == "LONG":
        sn_candidates = []
        for i in range(2, len(closes) - 1):
            c_range = highs[i] - lows[i]
            c_body = abs(closes[i] - opens[i])
            upper_wick = highs[i] - max(opens[i], closes[i])
            is_high_candle = (c_range >= high_candle_threshold) or (c_body >= 1.40 * atr) or (upper_wick >= 1.05 * atr)
            if is_high_candle and highs[i] > current_price:
                sn_candidates.append((float(highs[i]), float(c_range / (atr + 1e-9)), float(upper_wick / (atr + 1e-9))))

        if sn_candidates:
            nearest_sn, c_atr, w_atr = min(sn_candidates, key=lambda x: x[0] - current_price)
            dist_atr = (nearest_sn - current_price) / (atr + 1e-9)
            if dist_atr <= 1.35 and (c_atr >= 1.65 or w_atr >= 1.05):
                return {
                    "rejected": True,
                    "sn_level": round(nearest_sn, 6),
                    "max_candle_atr": round(max(c_atr, w_atr), 2),
                    "badge": "🛑 RIFIUTO: SN CANDELA ALTA OPPOSTA",
                    "reason": (
                        f"Rifiuto LONG: resistenza SN a {round(nearest_sn, 6)} (+{dist_atr:.2f} ATR) "
                        f"con candela alta/rifiuto ({max(c_atr, w_atr):.2f}x ATR) sul lato opposto."
                    )
                }
        return {
            "rejected": False,
            "sn_level": round(sn_candidates[0][0], 6) if sn_candidates else None,
            "max_candle_atr": round(max([x[1] for x in sn_candidates], default=0.0), 2),
            "badge": "✅ SN LATO OPPOSTO LIBERO",
            "reason": "Nessuna resistenza SN con candela alta ravvicinata sul lato opposto."
        }

    sn_candidates = []
    for i in range(2, len(closes) - 1):
        c_range = highs[i] - lows[i]
        c_body = abs(closes[i] - opens[i])
        lower_wick = min(opens[i], closes[i]) - lows[i]
        is_high_candle = (c_range >= high_candle_threshold) or (c_body >= 1.40 * atr) or (lower_wick >= 1.05 * atr)
        if is_high_candle and lows[i] < current_price:
            sn_candidates.append((float(lows[i]), float(c_range / (atr + 1e-9)), float(lower_wick / (atr + 1e-9))))

    if sn_candidates:
        nearest_sn, c_atr, w_atr = min(sn_candidates, key=lambda x: current_price - x[0])
        dist_atr = (current_price - nearest_sn) / (atr + 1e-9)
        if dist_atr <= 1.35 and (c_atr >= 1.65 or w_atr >= 1.05):
            return {
                "rejected": True,
                "sn_level": round(nearest_sn, 6),
                "max_candle_atr": round(max(c_atr, w_atr), 2),
                "badge": "🛑 RIFIUTO: SN CANDELA ALTA OPPOSTA",
                "reason": (
                    f"Rifiuto SHORT: supporto SN a {round(nearest_sn, 6)} (-{dist_atr:.2f} ATR) "
                    f"con candela alta/rifiuto ({max(c_atr, w_atr):.2f}x ATR) sul lato opposto."
                )
            }

    return {
        "rejected": False,
        "sn_level": round(sn_candidates[0][0], 6) if sn_candidates else None,
        "max_candle_atr": round(max([x[1] for x in sn_candidates], default=0.0), 2),
        "badge": "✅ SN LATO OPPOSTO LIBERO",
        "reason": "Nessun supporto SN con candela alta ravvicinata sul lato opposto."
    }


def evaluate_entry_window(
    direction: str,
    current_price: float,
    ref_price: float,
    atr: float,
    z_score: float,
    max_atr_stretch: float = None,
    max_z_abs: float = None
):
    if max_atr_stretch is None:
        max_atr_stretch = float(BRAIN_WEIGHTS.get("max_entry_atr_stretch", 1.55))
    if max_z_abs is None:
        max_z_abs = float(BRAIN_WEIGHTS.get("max_entry_z_abs", 2.60))

    if not direction or ref_price <= 0 or atr <= 0:
        return {
            "in_window": True,
            "exceeded": False,
            "threshold_price": None,
            "status_badge": "⏳ IN ATTESA",
            "reason": "In attesa di una direzione o di un livello di riferimento valido."
        }

    if direction == "LONG":
        threshold_price = round(ref_price + max_atr_stretch * atr, 6)
        exceeded = (current_price > threshold_price) or (z_score > max_z_abs)
        if exceeded:
            return {
                "in_window": False,
                "exceeded": True,
                "threshold_price": threshold_price,
                "status_badge": "🛑 OLTRE SOGLIA — NON ENTRARE",
                "reason": (
                    f"Prezzo {current_price} oltre soglia max {threshold_price} "
                    f"(rif. {ref_price} + {max_atr_stretch}x ATR, Z={z_score:+.2f}σ). Non inseguire."
                )
            }
        return {
            "in_window": True,
            "exceeded": False,
            "threshold_price": threshold_price,
            "status_badge": "✅ FINESTRA INGRESSO LONG",
            "reason": (
                f"Prezzo {current_price} entro la soglia max {threshold_price} "
                f"(rif. {ref_price}, Z={z_score:+.2f}σ). Momento valido per valutare l'ingresso."
            )
        }

    threshold_price = round(ref_price - max_atr_stretch * atr, 6)
    exceeded = (current_price < threshold_price) or (z_score < -max_z_abs)
    if exceeded:
        return {
            "in_window": False,
            "exceeded": True,
            "threshold_price": threshold_price,
            "status_badge": "🛑 OLTRE SOGLIA — NON ENTRARE",
            "reason": (
                f"Prezzo {current_price} sceso oltre soglia max {threshold_price} "
                f"(rif. {ref_price} - {max_atr_stretch}x ATR, Z={z_score:+.2f}σ). Non inseguire."
            )
        }
    return {
        "in_window": True,
        "exceeded": False,
        "threshold_price": threshold_price,
        "status_badge": "✅ FINESTRA INGRESSO SHORT",
        "reason": (
            f"Prezzo {current_price} entro la soglia max {threshold_price} "
            f"(rif. {ref_price}, Z={z_score:+.2f}σ). Momento valido per valutare l'ingresso."
        )
    }


def detect_early_entry_setup(opens, highs, lows, closes, volumes, ema20_arr, ema50_arr, ema200_val, atr_series, rsi, z_score):
    price = closes[-1]
    prev_high, prev_low = highs[-2], lows[-2]
    curr_open, curr_high, curr_low, curr_close = opens[-1], highs[-1], lows[-1], closes[-1]

    atr_sma20 = float(np.mean(atr_series[-20:])) if len(atr_series) >= 20 else float(atr_series[-1])
    is_compressed = atr_series[-1] < (atr_sma20 * 0.96)

    candle_body_pct = abs(curr_close - curr_open) / (atr_series[-1] + 1e-9)
    not_chasing = candle_body_pct < 1.85 and abs(z_score) <= 2.75

    trend_bias = "BULL" if price >= ema200_val else "BEAR"

    e20 = ema20_arr[-1]
    e50 = ema50_arr[-1]
    dist_e20_atr = abs(price - e20) / (atr_series[-1] + 1e-9)
    dist_e50_atr = abs(price - e50) / (atr_series[-1] + 1e-9)
    pullback_bull = (trend_bias == "BULL") and (min(dist_e20_atr, dist_e50_atr) <= 1.05) and (rsi < 65)
    pullback_bear = (trend_bias == "BEAR") and (min(dist_e20_atr, dist_e50_atr) <= 1.05) and (rsi > 35)

    sweep_bull = (curr_low < prev_low) and (curr_close > prev_low) and (curr_close > curr_open)
    sweep_bear = (curr_high > prev_high) and (curr_close < prev_high) and (curr_close < curr_open)

    vol_avg20 = float(np.mean(volumes[-20:])) if len(volumes) >= 20 else float(volumes[-1])
    vol_calm_or_sweep = (volumes[-1] <= vol_avg20 * 1.50) or sweep_bull or sweep_bear

    setup_type = "STRUTTURA / TREND ATTIVO"
    direction = "LONG" if trend_bias == "BULL" else "SHORT"
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
    elif pullback_bull and not_chasing and rsi < 58:
        setup_type = "PULLBACK SU EMA 20/50 LONG"
        direction = "LONG"
        order_type = "BUY LIMIT / PULLBACK"
        trigger_sound = True
    elif pullback_bear and not_chasing and rsi > 42:
        setup_type = "PULLBACK SU EMA 20/50 SHORT"
        direction = "SHORT"
        order_type = "SELL LIMIT / PULLBACK"
        trigger_sound = True
    elif is_compressed and not_chasing:
        setup_type = "FASE ACCUMULAZIONE / SQUEEZE"
        direction = "LONG" if trend_bias == "BULL" else "SHORT"
        order_type = "LIMIT IN ATTESA ROTTURA"
        trigger_sound = True

    return {
        "setup_type": setup_type,
        "direction": direction,
        "is_compressed": bool(is_compressed),
        "not_chasing": bool(not_chasing),
        "sweep_detected": bool(sweep_bull or sweep_bear),
        "pullback_detected": bool(pullback_bull or pullback_bear),
        "order_type": order_type,
        "limit_price": limit_price,
        "trigger_sound": bool(trigger_sound),
        "candle_id": int(len(closes))
    }


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

    if 0 < capital <= 2.0:
        base_lev = min(base_lev, 20)
    return max(1, min(base_lev, max_contract_lev))


def evaluate_profit_math_and_entry(
    symbol, signal, confidence, rr_ratio, adx, r_squared, z_score, rsi,
    min_conf, capital, risk_pct, stop_dist, price, rec_leverage,
    ema_dir, mom_dir, vol_dir, final_sentiment, session_info, early_setup,
    entry_window, candle_timing, sn_rejection
):
    p_win = float(np.clip(0.48 + (confidence - 50.0) * 0.0045 + (r_squared - 0.25) * 0.14, 0.38, 0.78))
    p_loss = 1.0 - p_win
    reward_r = max(rr_ratio, 0.5)
    expectancy_r = round((p_win * reward_r) - (p_loss * 1.0), 2)

    kelly_raw = p_win - (p_loss / max(reward_r, 0.1))
    half_kelly_pct = round(max(0.0, min(kelly_raw * 0.5 * 100.0, 5.0)), 2)

    effective_capital = capital if capital > 0 else 0.0
    risk_usd = effective_capital * (risk_pct / 100.0)
    raw_qty = (risk_usd / stop_dist) if (stop_dist > 0 and risk_usd > 0) else 0.0
    qty_fmt = normalize_order_qty(symbol, raw_qty, price, rec_leverage, effective_capital)
    notional_usd = qty_fmt * price
    margin_needed = round(notional_usd / max(rec_leverage, 1), 2)

    session_ok = bool(session_info.get("session_ok", True))
    early_dir = early_setup.get("direction")
    is_early_active = bool(early_setup.get("trigger_sound") and early_dir)
    window_exceeded = bool((entry_window or {}).get("exceeded", False))
    sn_rejected = bool((sn_rejection or {}).get("rejected", False))
    before_next_candle = bool((candle_timing or {}).get("before_next_candle", False))
    rem_sec = int((candle_timing or {}).get("seconds_to_next_candle", 0) or 0)

    bull_aligned = (ema_dir > 0) and (mom_dir > -10) and (vol_dir >= -20)
    bear_aligned = (ema_dir < 0) and (mom_dir < 10) and (vol_dir <= 20)

    reversed_against_dir = False
    if early_dir == "LONG" and (ema_dir < -25 or mom_dir < -30):
        reversed_against_dir = True
    elif early_dir == "SHORT" and (ema_dir > 25 or mom_dir > 30):
        reversed_against_dir = True

    certain_long = (
        not reversed_against_dir
        and early_dir == "LONG"
        and bull_aligned
        and confidence >= min_conf
        and expectancy_r > 0.0
    )
    certain_short = (
        not reversed_against_dir
        and early_dir == "SHORT"
        and bear_aligned
        and confidence >= min_conf
        and expectancy_r > 0.0
    )

    certain_signal = None
    if certain_long:
        certain_signal = "SEGNALE CERTO ANTICIPO LONG"
    elif certain_short:
        certain_signal = "SEGNALE CERTO ANTICIPO SHORT"

    manual_sign = {
        "show_enter_now": False,
        "status": "ACTIVE_SIGNAL",
        "badge": f"📊 SEGNALE ATTIVO {signal}",
        "reason": f"Segnale {signal} ({confidence}%) • Prossima candela tra {candle_timing.get('countdown_str')}."
    }

    if sn_rejected:
        manual_sign = {
            "show_enter_now": False,
            "status": "REJECTED_SN",
            "badge": sn_rejection.get("badge", "🛑 RIFIUTO: SN CANDELA ALTA OPPOSTA"),
            "reason": sn_rejection.get("reason", "Rifiuto per livello SN con candela alta sul lato opposto.")
        }
        entry_rule = {
            "action": "REJECTED",
            "badge": manual_sign["badge"],
            "reason": manual_sign["reason"]
        }
    elif window_exceeded:
        manual_sign = {
            "show_enter_now": False,
            "status": "OVER_THRESHOLD",
            "badge": entry_window.get("status_badge", "🛑 OLTRE SOGLIA — NON ENTRARE"),
            "reason": entry_window.get("reason", "Soglia massima di ingresso superata: non entrare.")
        }
        entry_rule = {
            "action": "NO ENTRY",
            "badge": manual_sign["badge"],
            "reason": manual_sign["reason"]
        }
    elif reversed_against_dir:
        manual_sign = {
            "show_enter_now": False,
            "status": "CANCELLED_REVERSAL",
            "badge": "❌ SEGNALE ANNULLATO (INVERSIONE)",
            "reason": f"Direzione {early_dir} annullata per inversione del momentum/struttura."
        }
        entry_rule = {
            "action": "CANCELLED",
            "badge": manual_sign["badge"],
            "reason": manual_sign["reason"]
        }
    elif certain_signal and before_next_candle:
        manual_sign = {
            "show_enter_now": True,
            "status": "ENTER_NOW_PRE_CANDLE",
            "badge": f"🟢 ENTRA ORA • {early_dir} (PRE-CANDELA -{rem_sec}s)",
            "reason": (
                f"{certain_signal}: mancano {rem_sec}s all'inizio della prossima candela {candle_timing.get('timeframe')}, "
                f"entro soglia {entry_window.get('threshold_price')} e SN libero."
            )
        }
        entry_rule = {
            "action": "ENTER NOW",
            "badge": manual_sign["badge"],
            "reason": manual_sign["reason"]
        }
    elif certain_signal and not before_next_candle:
        manual_sign = {
            "show_enter_now": False,
            "status": "ARMED_WAIT_PRE_CANDLE",
            "badge": f"⏳ SEGNALE CERTO {early_dir} • ATTESA PRE-CANDELA ({candle_timing.get('countdown_str')})",
            "reason": (
                f"{certain_signal} attivo nel verso {early_dir}: il cartello ENTRA ORA scatterà negli ultimi "
                f"{candle_timing.get('pre_close_window_sec')}s prima della nuova candela (tra {candle_timing.get('countdown_str')})."
            )
        }
        entry_rule = {
            "action": "SIGNAL ACTIVE",
            "badge": manual_sign["badge"],
            "reason": manual_sign["reason"]
        }
    elif is_early_active:
        manual_sign = {
            "show_enter_now": False,
            "status": "EARLY_SETUP",
            "badge": f"🎯 ANTICIPO {early_dir} ({early_setup.get('setup_type')})",
            "reason": f"{early_setup.get('setup_type')} • Prossima candela tra {candle_timing.get('countdown_str')}."
        }
        entry_rule = {
            "action": "EARLY ACTIVE",
            "badge": manual_sign["badge"],
            "reason": manual_sign["reason"]
        }
    elif not session_ok:
        entry_rule = {
            "action": "OUT OF SESSION",
            "badge": "🟡 FUORI ORARIO",
            "reason": f"Segnale {signal} attivo • Mercato {session_info.get('session_name')} ({session_info.get('hours_window')})."
        }
    else:
        entry_rule = {
            "action": "SIGNAL ACTIVE",
            "badge": f"📊 {signal}",
            "reason": f"Segnale {signal} ({confidence}%) • Prossima candela tra {candle_timing.get('countdown_str')}."
        }

    recommended_params = {
        "leverage": rec_leverage,
        "qty": qty_fmt,
        "margin_usdt": margin_needed,
        "risk_usd": round(risk_usd, 4),
        "p_win_pct": round(p_win * 100, 1),
        "expectancy_r": expectancy_r,
        "half_kelly_pct": half_kelly_pct,
        "certain_signal": certain_signal,
        "summary": f"Lev {rec_leverage}x • Size {qty_fmt} • Exp {expectancy_r:+.2f}R • Kelly {half_kelly_pct}%"
    }
    return entry_rule, recommended_params, certain_signal, manual_sign


def is_certain_or_early_signal(analysis: dict) -> bool:
    if not analysis:
        return False
    early = analysis.get("early_setup", {})
    sig = str(analysis.get("signal") or "")
    return bool(
        analysis.get("certain_signal")
        or early.get("trigger_sound")
        or early.get("direction")
        or ("LONG" in sig or "SHORT" in sig)
    )


def register_sound_alert_if_exact_moment(analysis: dict):
    early = analysis.get("early_setup", {})
    certain = analysis.get("certain_signal")
    ew = analysis.get("entry_window", {})
    sn_rej = analysis.get("sn_rejection", {})
    if ew.get("exceeded") or sn_rej.get("rejected"):
        return
    if not (early.get("trigger_sound") or certain):
        return
    if not analysis.get("session", {}).get("session_ok", True):
        return

    sym = analysis["symbol"]
    side = certain or f"ANTICIPO_{early.get('direction')}_{early.get('setup_type')}"
    candle_id = early.get("candle_id", 0)
    alert_key = f"{sym}_{side}_{candle_id}"

    existing_keys = {a["key"] for a in AUTO_SCAN_STATE["audio_alerts"][:25]}
    if alert_key in existing_keys:
        return

    AUTO_SCAN_STATE["audio_alerts"].insert(0, {
        "id": int(time.time() * 1000),
        "key": alert_key,
        "time": time.strftime("%H:%M:%S"),
        "symbol": analysis["bingx_name"],
        "title": certain or early.get("setup_type"),
        "order_type": early.get("order_type"),
        "price": analysis["price"],
        "limit_price": early.get("limit_price"),
        "sl": analysis["levels"]["sl"],
        "tp1": analysis["levels"]["tp1"],
        "tp2": analysis["levels"]["tp2"],
        "tp3": analysis["levels"]["tp3"]
    })
    AUTO_SCAN_STATE["audio_alerts"] = AUTO_SCAN_STATE["audio_alerts"][:25]


def analyze_symbol_math(symbol: str, interval: str = "15m", custom_sentiment: float = None, apply_auto_profile: bool = True):
    real_symbol = resolve_bingx_symbol(symbol)
    exact_name = bingx_exact_name(real_symbol)
    category = categorize_symbol(exact_name, real_symbol)

    applied_profile = None
    if apply_auto_profile:
        applied_profile = auto_tune_brain_for_category_and_tf(interval, category)

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
    candle_timing = compute_candle_timing(interval, klines=klines)

    opens = np.array([float(k.get("open", k["close"])) for k in klines], dtype=float)
    closes = np.array([float(k["close"]) for k in klines], dtype=float)
    highs = np.array([float(k["high"]) for k in klines], dtype=float)
    lows = np.array([float(k["low"]) for k in klines], dtype=float)
    volumes = np.array([float(k["volume"]) for k in klines], dtype=float)

    price = closes[-1]
    ema9 = calc_ema(closes, 9)
    ema20 = calc_ema(closes, 20)
    ema21 = calc_ema(closes, 21)
    ema50 = calc_ema(closes, 50)
    ema200 = calc_ema(closes, 200 if len(closes) >= 200 else len(closes) - 1)

    e9, e21, e50, e200 = ema9[-1], ema21[-1], ema50[-1], ema200[-1]
    ema_slope = ((ema21[-1] - ema21[-5]) / (ema21[-5] + 1e-9)) * 100

    ema_dir = 0.0
    if e9 > e21: ema_dir += 30
    else: ema_dir -= 30
    if e21 > e50: ema_dir += 30
    else: ema_dir -= 30
    if price > e200: ema_dir += 25
    else: ema_dir -= 25
    ema_dir = float(np.clip(ema_dir + np.clip(ema_slope * 20, -15, 15), -100, 100))

    rsi = calc_rsi(closes, 14)
    ema12 = calc_ema(closes, 12)
    ema26 = calc_ema(closes, 26)
    macd_line = ema12 - ema26
    signal_line = calc_ema(macd_line, 9)
    macd_hist = macd_line[-1] - signal_line[-1]
    adx = calc_adx(highs, lows, closes, 14)

    mom_dir = 0.0
    if 52 < rsi < 74: mom_dir += 40
    elif rsi >= 74: mom_dir -= 20
    elif 26 < rsi < 48: mom_dir -= 40
    elif rsi <= 26: mom_dir += 20

    if macd_hist > 0 and macd_line[-1] > signal_line[-1]:
        mom_dir += 40
    elif macd_hist < 0 and macd_line[-1] < signal_line[-1]:
        mom_dir -= 40

    adx_multiplier = 1.2 if adx >= 25 else 0.85
    mom_dir = float(np.clip(mom_dir * adx_multiplier, -100, 100))

    sma20 = np.mean(closes[-20:])
    std20 = np.std(closes[-20:]) + 1e-9
    bb_upper = sma20 + 2 * std20
    bb_lower = sma20 - 2 * std20
    bb_percent = ((price - bb_lower) / (bb_upper - bb_lower + 1e-9)) * 100
    z_score = (price - sma20) / std20

    x_idx = np.arange(20)
    corr_matrix = np.corrcoef(x_idx, closes[-20:])
    r_squared = float(corr_matrix[0, 1] ** 2) if not np.isnan(corr_matrix[0, 1]) else 0.0

    vol_dir = 0.0
    if 0.3 < z_score < 2.1: vol_dir += 50 * r_squared + 30
    elif -2.1 < z_score < -0.3: vol_dir -= 50 * r_squared + 30
    vol_dir = float(np.clip(vol_dir, -100, 100))

    bid_vol = sum(float(b[1]) for b in (depth.get("bids", []) if depth else []))
    ask_vol = sum(float(a[1]) for a in (depth.get("asks", []) if depth else []))
    ob_imbalance = ((bid_vol - ask_vol) / (bid_vol + ask_vol + 1e-9)) * 100

    funding_rate = float(premium.get("lastFundingRate", 0) or 0) * 100 if isinstance(premium, dict) else 0.0
    vol_ratio = volumes[-1] / (np.mean(volumes[-20:]) + 1e-9)

    calc_sentiment = float(np.clip(
        ob_imbalance * 0.7 + (20 if vol_ratio > 1.3 and closes[-1] > closes[-2] else (-20 if vol_ratio > 1.3 else 0)),
        -100,
        100
    ))
    final_sentiment = float(custom_sentiment) if custom_sentiment is not None else calc_sentiment

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

    atr_series = calc_atr_series(highs, lows, closes, 14)
    atr = float(atr_series[-1])
    atr_pct = (atr / (price + 1e-9)) * 100
    decimals = len(str(price).split(".")[1]) if "." in str(price) else 2
    decimals = min(max(decimals, 2), 6)

    early_setup = detect_early_entry_setup(
        opens, highs, lows, closes, volumes, ema20, ema50, e200, atr_series, rsi, z_score
    )

    if early_setup.get("trigger_sound") and early_setup.get("direction"):
        if early_setup["direction"] == "LONG" and composite_score < 22:
            composite_score = max(abs(composite_score), 26.0)
        elif early_setup["direction"] == "SHORT" and composite_score > -22:
            composite_score = -max(abs(composite_score), 26.0)

    raw_conf = min(abs(composite_score) * 1.15 + (r_squared * 12) + (min(adx, 40) * 0.25), 99.4)
    if early_setup.get("trigger_sound"):
        raw_conf = max(raw_conf, float(BRAIN_WEIGHTS["min_confidence"]) + 2.0)
    confidence = round(float(min(raw_conf, 99.4)), 1)

    min_conf = BRAIN_WEIGHTS["min_confidence"]
    if composite_score > 16 and confidence >= min_conf:
        signal = "STRONG LONG"
    elif composite_score > 6:
        signal = "LONG"
    elif composite_score < -16 and confidence >= min_conf:
        signal = "STRONG SHORT"
    elif composite_score < -6:
        signal = "SHORT"
    else:
        signal = "LONG" if composite_score >= 0 else "SHORT"

    sl_m = float(BRAIN_WEIGHTS.get("sl_mult", 1.5))
    tp1_m = float(BRAIN_WEIGHTS.get("tp1_mult", 2.2))
    tp2_m = float(BRAIN_WEIGHTS.get("tp2_mult", 3.5))
    tp3_m = float(BRAIN_WEIGHTS.get("tp3_mult", 5.0))

    ref_dir = early_setup["direction"] if early_setup.get("direction") else ("LONG" if composite_score >= 0 else "SHORT")
    if ref_dir == "LONG":
        entry = round(price, decimals)
        sl = round(price - sl_m * atr, decimals)
        tp1 = round(price + tp1_m * atr, decimals)
        tp2 = round(price + tp2_m * atr, decimals)
        tp3 = round(price + tp3_m * atr, decimals)
    else:
        entry = round(price, decimals)
        sl = round(price + sl_m * atr, decimals)
        tp1 = round(price - tp1_m * atr, decimals)
        tp2 = round(price - tp2_m * atr, decimals)
        tp3 = round(price - tp3_m * atr, decimals)

    ref_entry_price = float(early_setup.get("limit_price") or entry)
    entry_window = evaluate_entry_window(
        direction=ref_dir,
        current_price=float(price),
        ref_price=ref_entry_price,
        atr=atr,
        z_score=float(z_score)
    )

    sn_rejection = detect_opposite_sn_high_candle_rejection(
        direction=ref_dir,
        current_price=float(price),
        atr=atr,
        opens=opens,
        highs=highs,
        lows=lows,
        closes=closes
    )

    stop_dist = max(abs(entry - sl), 1e-9)
    rr_ratio = round(abs(tp2 - entry) / stop_dist, 2)
    tp_plan = compute_tp_timeframe_plan(interval, adx)
    session_info = get_market_session_info(category, exact_name, real_symbol)

    max_lev = CONTRACTS_CACHE["max_lev_map"].get(real_symbol, 50)
    capital = float(BALANCE_STATE.get("balance_usdt", BRAIN_WEIGHTS.get("account_capital", 0.0)) or 0.0)
    risk_pct = float(BRAIN_WEIGHTS.get("risk_per_trade_pct", 1.5))
    rec_lev = calc_recommended_leverage(atr_pct, max_lev, capital)

    entry_rule, recommended_params, certain_signal, manual_sign = evaluate_profit_math_and_entry(
        symbol=real_symbol, signal=signal, confidence=confidence, rr_ratio=rr_ratio,
        adx=adx, r_squared=r_squared, z_score=z_score, rsi=rsi,
        min_conf=min_conf, capital=capital, risk_pct=risk_pct,
        stop_dist=stop_dist, price=price, rec_leverage=rec_lev,
        ema_dir=ema_dir, mom_dir=mom_dir, vol_dir=vol_dir, final_sentiment=final_sentiment,
        session_info=session_info, early_setup=early_setup, entry_window=entry_window,
        candle_timing=candle_timing, sn_rejection=sn_rejection
    )

    result_obj = {
        "symbol": real_symbol,
        "bingx_name": exact_name,
        "display_name": exact_name,
        "category": category,
        "session": session_info,
        "candle_timing": candle_timing,
        "early_setup": early_setup,
        "entry_window": entry_window,
        "sn_rejection": sn_rejection,
        "manual_sign": manual_sign,
        "timeframe": interval,
        "applied_profile": applied_profile,
        "weights": dict(BRAIN_WEIGHTS),
        "price": price,
        "signal": signal,
        "certain_signal": certain_signal,
        "entry_rule": entry_rule,
        "recommended_params": recommended_params,
        "tp_plan": tp_plan,
        "composite_score": round(float(composite_score), 1),
        "confidence": confidence,
        "sentiment": round(final_sentiment, 1),
        "account_balance": capital,
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
    register_sound_alert_if_exact_moment(result_obj)
    return result_obj


def maybe_auto_execute_trade(analysis: dict):
    if not BRAIN_WEIGHTS.get("auto_trade_enabled", False):
        return
    if not BINGX_API_KEY or not BINGX_SECRET_KEY:
        return

    if analysis.get("entry_window", {}).get("exceeded"):
        return
    if analysis.get("sn_rejection", {}).get("rejected"):
        return

    bal = float(BALANCE_STATE.get("balance_usdt", 0.0) or 0.0)
    if bal <= 0:
        return

    if not analysis.get("session", {}).get("session_ok", True):
        return

    total_open_pnl = sum(p.get("unrealized_pnl", 0.0) for p in AUTO_SCAN_STATE["open_positions"].values())
    if total_open_pnl <= -abs(float(BRAIN_WEIGHTS.get("max_daily_loss_usdt", 100.0))):
        return

    certain = analysis.get("certain_signal")
    early = analysis.get("early_setup", {})
    sig = analysis.get("signal", "")
    action = analysis.get("entry_rule", {}).get("action", "")

    is_valid_entry = (
        (certain is not None)
        or (early.get("trigger_sound") and action == "ENTER NOW")
        or (action == "ENTER NOW" and sig in ("STRONG LONG", "STRONG SHORT"))
    )
    if not is_valid_entry:
        return

    sym = analysis["symbol"]
    if sym in AUTO_SCAN_STATE["open_positions"]:
        return

    now = time.time()
    cooldown = int(BRAIN_WEIGHTS.get("auto_trade_cooldown_sec", 60))
    last_ts = AUTO_SCAN_STATE["last_auto_trade"].get(sym, 0)
    if now - last_ts < cooldown:
        return

    rp = analysis.get("recommended_params", {})
    qty = float(rp.get("qty", 0) or 0)
    if qty <= 0:
        return

    chosen_dir = certain or early.get("direction") or sig
    side = "BUY" if "LONG" in chosen_dir else "SELL"
    pos_side = "LONG" if side == "BUY" else "SHORT"
    levels = analysis.get("levels", {})
    sl = levels.get("sl")
    tp = levels.get("tp2")
    lev = rp.get("leverage", 10)
    timing = analysis.get("candle_timing") or {}

    AUTO_SCAN_STATE["last_auto_trade"][sym] = now
    order_res = place_bingx_real_order(sym, side, qty, sl, tp, leverage=lev)
    if isinstance(order_res, dict) and order_res.get("code") == 0:
        AUTO_SCAN_STATE["managed_trades"][f"{sym}_{pos_side}"] = {
            "symbol": sym,
            "side": pos_side,
            "entry": levels.get("entry", analysis.get("price")),
            "sl": sl,
            "tp1": levels.get("tp1"),
            "tp2": tp,
            "tp3": levels.get("tp3"),
            "opened_candle_id": int(early.get("candle_id", 0) or 0),
            "opened_candle_index": int(timing.get("candle_index", 0) or 0),
            "close_mode": "TP_OR_CANDLE_END",
            "opened_at": time.strftime("%H:%M:%S")
        }


def auto_market_monitor_loop():
    AUTO_SCAN_STATE["running"] = True
    while True:
        try:
            sync_bingx_balance_realtime()
            if BRAIN_WEIGHTS.get("auto_monitor_enabled", True):
                contracts = get_all_bingx_contracts()
                active_cat = AUTO_SCAN_STATE.get("active_category", "ALL")
                interval = AUTO_SCAN_STATE.get("interval", "15m")

                cat_for_tune = active_cat if active_cat not in ("ALL", "CERTAIN") else "CRYPTO MAJOR"
                auto_tune_brain_for_category_and_tf(interval, cat_for_tune)

                if active_cat and active_cat not in ("ALL", "CERTAIN"):
                    cat_contracts = [c for c in contracts if c.get("category") == active_cat]
                else:
                    cat_contracts = contracts

                target_symbols = [c["symbol"] for c in cat_contracts[:30]]
                for pos_sym in AUTO_SCAN_STATE["open_positions"].keys():
                    if pos_sym not in target_symbols:
                        target_symbols.append(pos_sym)

                updated = {}
                all_latest = {}
                ref_timing = None

                for sym in target_symbols:
                    res = analyze_symbol_math(sym, interval, apply_auto_profile=False)
                    if res:
                        if ref_timing is None and res.get("candle_timing"):
                            ref_timing = res["candle_timing"]
                        all_latest[sym] = res
                        if is_certain_or_early_signal(res):
                            updated[sym] = res
                        maybe_auto_execute_trade(res)
                    time.sleep(0.10)

                if ref_timing is None:
                    ref_timing = compute_candle_timing(interval)
                record_new_candle_written_alert(interval, ref_timing, all_latest)

                AUTO_SCAN_STATE["signals"] = updated
                positions = fetch_open_bingx_positions(all_latest)
                monitor_and_manage_auto_positions(all_latest, positions)
                AUTO_SCAN_STATE["open_positions"] = fetch_open_bingx_positions(all_latest)
                AUTO_SCAN_STATE["last_run"] = time.strftime("%H:%M:%S")
        except Exception as e:
            print(f"[AutoMonitor Error] {e}")

        sleep_sec = max(6, int(BRAIN_WEIGHTS.get("auto_scan_interval", 12)))
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
        bal_info = sync_bingx_balance_realtime()
        AUTO_SCAN_STATE["open_positions"] = fetch_open_bingx_positions(AUTO_SCAN_STATE["signals"])
        return jsonify({
            "status": "ok",
            "msg": f"Chiavi API BingX salvate! Saldo attuale: {bal_info['balance_usdt']:.2f} USDT"
        })

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
    for c in contracts:
        c["session"] = get_market_session_info(c["category"], c["bingxName"], c["symbol"])
    return jsonify({"status": "ok", "count": len(contracts), "markets": contracts})


@app.route("/api/close_position", methods=["POST"])
def api_close_position():
    data = request.json or {}
    sym = data.get("symbol")
    side = data.get("side")
    qty = data.get("quantity")
    if not all([sym, side, qty]):
        return jsonify({"status": "error", "msg": "Dati chiusura mancanti"}), 400
    res = close_bingx_position(sym, side, qty, reason="MANUAL")
    AUTO_SCAN_STATE["open_positions"] = fetch_open_bingx_positions(AUTO_SCAN_STATE["signals"])
    return jsonify(res)


@app.route("/api/auto_monitor_status", methods=["GET", "POST"])
def api_auto_monitor_status():
    profile = None
    if request.method == "POST":
        data = request.json or {}
        if "enabled" in data:
            BRAIN_WEIGHTS["auto_monitor_enabled"] = bool(data["enabled"])
        if "auto_trade_enabled" in data:
            BRAIN_WEIGHTS["auto_trade_enabled"] = bool(data["auto_trade_enabled"])
        if "auto_close_on_tp" in data:
            BRAIN_WEIGHTS["auto_close_on_tp"] = bool(data["auto_close_on_tp"])
        if "auto_close_on_candle_end" in data:
            BRAIN_WEIGHTS["auto_close_on_candle_end"] = bool(data["auto_close_on_candle_end"])
        if "auto_close_on_candle_signal" in data:
            BRAIN_WEIGHTS["auto_close_on_candle_signal"] = bool(data["auto_close_on_candle_signal"])
        if "active_category" in data:
            AUTO_SCAN_STATE["active_category"] = data["active_category"] or "ALL"
        if "only_certain_and_early" in data:
            AUTO_SCAN_STATE["only_certain_and_early"] = bool(data["only_certain_and_early"])
        if "interval" in data:
            AUTO_SCAN_STATE["interval"] = data["interval"]
        if any(k in data for k in ("interval", "active_category")) and data.get("apply_profile", True):
            cat_for_prof = AUTO_SCAN_STATE.get("active_category", "CRYPTO MAJOR")
            if cat_for_prof in ("ALL", "CERTAIN"):
                cat_for_prof = "CRYPTO MAJOR"
            profile = auto_tune_brain_for_category_and_tf(AUTO_SCAN_STATE.get("interval", "15m"), cat_for_prof)
        if "scan_interval_sec" in data:
            BRAIN_WEIGHTS["auto_scan_interval"] = max(6, int(data["scan_interval_sec"]))

    bal_info = sync_bingx_balance_realtime()
    positions = fetch_open_bingx_positions(AUTO_SCAN_STATE["signals"])
    monitor_and_manage_auto_positions(AUTO_SCAN_STATE["signals"], positions)
    positions = fetch_open_bingx_positions(AUTO_SCAN_STATE["signals"])
    AUTO_SCAN_STATE["open_positions"] = positions

    return jsonify({
        "status": "ok",
        "running": AUTO_SCAN_STATE["running"],
        "enabled": BRAIN_WEIGHTS.get("auto_monitor_enabled", True),
        "auto_trade_enabled": BRAIN_WEIGHTS.get("auto_trade_enabled", False),
        "auto_close_on_tp": BRAIN_WEIGHTS.get("auto_close_on_tp", True),
        "auto_close_on_candle_end": BRAIN_WEIGHTS.get("auto_close_on_candle_end", True),
        "auto_close_on_candle_signal": BRAIN_WEIGHTS.get("auto_close_on_candle_signal", True),
        "active_category": AUTO_SCAN_STATE.get("active_category", "ALL"),
        "only_certain_and_early": AUTO_SCAN_STATE.get("only_certain_and_early", False),
        "last_run": AUTO_SCAN_STATE["last_run"],
        "interval": AUTO_SCAN_STATE["interval"],
        "profile": profile or {"profile_name": f"AUTO • {AUTO_SCAN_STATE.get('active_category', 'ALL')} • {TIMEFRAME_PROFILES.get(AUTO_SCAN_STATE['interval'], {}).get('label', AUTO_SCAN_STATE['interval'])}"},
        "weights": BRAIN_WEIGHTS,
        "scan_interval_sec": BRAIN_WEIGHTS.get("auto_scan_interval", 12),
        "balance_info": bal_info,
        "signals": AUTO_SCAN_STATE["signals"],
        "open_positions": positions,
        "managed_trades": AUTO_SCAN_STATE["managed_trades"],
        "trade_logs": AUTO_SCAN_STATE["trade_logs"],
        "audio_alerts": AUTO_SCAN_STATE["audio_alerts"],
        "latest_new_candle_alert": AUTO_SCAN_STATE.get("latest_new_candle_alert"),
        "new_candle_alerts": AUTO_SCAN_STATE.get("new_candle_alerts", [])
    })


@app.route("/api/analyze", methods=["GET", "POST"])
def api_analyze():
    if request.method == "POST":
        data = request.json or {}
        symbol = data.get("symbol", "BTC-USDT")
        interval = data.get("interval", "15m")
        custom_sentiment = data.get("custom_sentiment", None)
        apply_auto_profile = bool(data.get("apply_auto_profile", True))
    else:
        symbol = request.args.get("symbol", "BTC-USDT")
        interval = request.args.get("interval", "15m")
        custom_sentiment = None
        apply_auto_profile = True

    res = analyze_symbol_math(symbol, interval, custom_sentiment, apply_auto_profile=apply_auto_profile)
    if not res:
        return jsonify({"status": "error", "msg": "Contratto non trovato o dati insufficienti su BingX"}), 400
    record_new_candle_written_alert(interval, res.get("candle_timing"), {res["symbol"]: res})
    maybe_auto_execute_trade(res)
    return jsonify({
        "status": "ok",
        "data": res,
        "audio_alerts": AUTO_SCAN_STATE["audio_alerts"],
        "latest_new_candle_alert": AUTO_SCAN_STATE.get("latest_new_candle_alert")
    })


@app.route("/api/scan_top", methods=["POST"])
def api_scan_top():
    data = request.json or {}
    symbols = data.get("symbols", [])
    interval = data.get("interval", "15m")
    overrides = data.get("overrides", {})
    only_ce = bool(data.get("only_certain_and_early", False))

    results = []
    batch_map = {}
    ref_timing = None

    for sym in symbols[:30]:
        ov = overrides.get(sym, {})
        custom_sent = ov.get("sentiment", None)
        analysis = analyze_symbol_math(sym, interval, custom_sent, apply_auto_profile=False)
        if analysis:
            if ref_timing is None and analysis.get("candle_timing"):
                ref_timing = analysis["candle_timing"]
            if "score_override" in ov and ov["score_override"] is not None:
                analysis["composite_score"] = float(ov["score_override"])
                analysis["confidence"] = min(round(abs(analysis["composite_score"]) * 1.15, 1), 99.9)
            batch_map[analysis["symbol"]] = analysis
            maybe_auto_execute_trade(analysis)
            if not only_ce or is_certain_or_early_signal(analysis):
                results.append(analysis)

    if ref_timing:
        record_new_candle_written_alert(interval, ref_timing, batch_map)

    return jsonify({
        "status": "ok",
        "signals": results,
        "audio_alerts": AUTO_SCAN_STATE["audio_alerts"],
        "latest_new_candle_alert": AUTO_SCAN_STATE.get("latest_new_candle_alert")
    })


@app.route("/api/weights", methods=["GET", "POST"])
def api_weights():
    if request.method == "POST":
        data = request.json or {}
        for k in BRAIN_WEIGHTS:
            if k in data:
                if k in ("auto_monitor_enabled", "auto_trade_enabled", "auto_close_on_tp", "auto_close_on_candle_end", "auto_close_on_candle_signal"):
                    BRAIN_WEIGHTS[k] = bool(data[k])
                else:
                    BRAIN_WEIGHTS[k] = float(data[k])
    return jsonify({"status": "ok", "weights": BRAIN_WEIGHTS})


@app.route("/api/account_balance")
def api_account_balance():
    info = sync_bingx_balance_realtime()
    return jsonify({
        "status": "ok" if info.get("source") == "bingx_live" else "error",
        "balance": info["balance_usdt"],
        "available": info["available_usdt"],
        "unrealized_pnl": info["unrealized_pnl_usdt"],
        "equity": info["equity_usdt"],
        "last_sync": info["last_sync"],
        "error": info.get("last_error")
    })


@app.route("/api/execute_order", methods=["POST"])
def api_execute_order():
    data = request.json or {}
    symbol = data.get("symbol", "")
    side = data.get("side")
    quantity = data.get("quantity")
    sl = data.get("sl")
    tp = data.get("tp")
    leverage = data.get("leverage")
    candle_id = data.get("candle_id")
    candle_index = data.get("candle_index")
    tp1 = data.get("tp1")
    tp3 = data.get("tp3")
    entry_px = data.get("entry")

    if not all([symbol, side, quantity]):
        return jsonify({"status": "error", "msg": "Parametri ordine incompleti"}), 400

    res = place_bingx_real_order(symbol, side, quantity, sl, tp, leverage=leverage)
    if isinstance(res, dict) and res.get("code") == 0:
        real_sym = resolve_bingx_symbol(symbol)
        pos_side = "LONG" if side == "BUY" else "SHORT"
        AUTO_SCAN_STATE["managed_trades"][f"{real_sym}_{pos_side}"] = {
            "symbol": real_sym,
            "side": pos_side,
            "entry": entry_px,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp,
            "tp3": tp3,
            "opened_candle_id": int(candle_id) if candle_id is not None else None,
            "opened_candle_index": int(candle_index) if candle_index is not None else None,
            "close_mode": "TP_OR_CANDLE_END",
            "opened_at": time.strftime("%H:%M:%S")
        }
    AUTO_SCAN_STATE["open_positions"] = fetch_open_bingx_positions(AUTO_SCAN_STATE["signals"])
    return jsonify(res)


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
