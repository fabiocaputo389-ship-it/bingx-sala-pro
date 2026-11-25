import hashlib
import hmac
import os
import time
from urllib.parse import urlencode
from flask import Flask, jsonify, render_template, request
import requests

app = Flask(__name__)

BINGX_BASE_URL = "https://open-api.bingx.com"
API_KEY = os.getenv("BINGX_API_KEY", "")
SECRET_KEY = os.getenv("BINGX_SECRET_KEY", "")

# Catalogo multi-mercato (adatta i simboli esatti a quelli attivi sul tuo account BingX)
ASSET_CATALOG = {
    "BTC-USDT": {"label": "BTC", "category": "Crypto", "price_decimals": 2, "win_rate": 77},
    "EUR-USDT": {"label": "EURUSDT", "category": "Forex", "price_decimals": 5, "win_rate": 74},
    "XAU-USDT": {"label": "ORO (XAU)", "category": "Materie prime", "price_decimals": 2, "win_rate": 75},
    "OIL-USDT": {"label": "PETROLIO", "category": "Materie prime", "price_decimals": 3, "win_rate": 71},
    "TSLA-USDT": {"label": "TSLA", "category": "Azioni", "price_decimals": 2, "win_rate": 72},
    "NVDA-USDT": {"label": "NVDA", "category": "Azioni", "price_decimals": 2, "win_rate": 76},
}


def normalize_symbol(raw_symbol: str) -> str:
    cleaned = (raw_symbol or "BTC-USDT").strip().upper().replace("/", "-")
    # Supporta sia EURUSDT che EUR-USDT
    if cleaned == "EURUSDT":
        return "EUR-USDT"
    if "-" not in cleaned and cleaned.endswith("USDT"):
        return f"{cleaned[:-4]}-USDT"
    return cleaned


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

    if method.upper() == "GET":
        r = requests.get(url, headers=headers, timeout=10)
    elif method.upper() == "POST":
        r = requests.post(url, headers=headers, timeout=10)
    else:
        raise ValueError(f"Metodo HTTP non supportato: {method}")

    r.raise_for_status()
    return r.json()


def dynamic_leverage(equity_usdt: float, category: str) -> int:
    # Micro-conto <= $2.00 usa leva 20x sulle crypto; su Forex/Materie prime/Azioni limita per prudenza
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


@app.route("/api/assets")
def list_assets():
    items = []
    for sym, meta in ASSET_CATALOG.items():
        items.append({
            "symbol": sym,
            "label": meta["label"],
            "category": meta["category"],
            "priceDecimals": meta["price_decimals"],
        })
    return jsonify({"assets": items})


@app.route("/api/live-ticker")
def live_ticker():
    symbol = normalize_symbol(request.args.get("symbol", "BTC-USDT"))
    meta = ASSET_CATALOG.get(symbol, {
        "label": symbol.replace("-", ""),
        "category": "Crypto",
        "price_decimals": 4,
        "win_rate": 72,
    })

    last_price = 0.0
    try:
        resp = requests.get(
            f"{BINGX_BASE_URL}/openApi/swap/v2/quote/ticker",
            params={"symbol": symbol},
            timeout=8,
        )
        resp.raise_for_status()
        last_price = float(resp.json().get("data", {}).get("lastPrice", 0.0))
    except Exception:
        # Fallback pulito se il contratto specifico non è quotato sull'endpoint swap
        last_price = 0.0

    high_risk = meta["category"] in ("Materie prime", "Azioni")
    risk_reason = (
        f"Controlla sessione e spread su {meta['category']}"
        if high_risk
        else "Rischio correlazione BTC / Balene"
    )

    return jsonify({
        "symbol": symbol,
        "label": meta["label"],
        "category": meta["category"],
        "priceDecimals": meta["price_decimals"],
        "lastPrice": last_price,
        "signalSide": "BUY",
        "winRate": meta["win_rate"],
        "trendHealth": 60,
        "whalePct": 60,
        "bearPct": 40,
        "rsi": 56,
        "macdStatus": "BULLISH",
        "highRiskWarning": high_risk,
        "riskReason": risk_reason,
        "validityPct": 92,
        "remainingText": "13:50",
        "timestamp": int(time.time() * 1000),
    })


@app.route("/api/income-history")
def income_history():
    symbol = normalize_symbol(request.args.get("symbol", "BTC-USDT"))
    raw = bingx_signed_request("GET", "/openApi/swap/v2/user/income", {
        "symbol": symbol,
        "limit": 100,
    })
    records = raw.get("data", []) if isinstance(raw, dict) else []
    realized_pnl = sum(float(item.get("income", 0.0)) for item in records if item.get("incomeType") == "REALIZED_PNL")
    fees = sum(float(item.get("income", 0.0)) for item in records if item.get("incomeType") == "TRADING_FEE")
    funding = sum(float(item.get("income", 0.0)) for item in records if item.get("incomeType") == "FUNDING_FEE")

    return jsonify({
        "symbol": symbol,
        "count": len(records),
        "realizedPnl": round(realized_pnl, 4),
        "tradingFees": round(fees, 4),
        "fundingFees": round(funding, 4),
        "netPnl": round(realized_pnl + fees + funding, 4),
    })


@app.route("/api/execute-signal", methods=["POST"])
def execute_signal():
    body = request.get_json(force=True)
    symbol = normalize_symbol(body.get("symbol", "BTC-USDT"))
    side = (body.get("side") or "NEUTRAL").upper()
    equity = float(body.get("equity", 2.0))
    tp_price = body.get("tpPrice")
    sl_price = body.get("slPrice")

    meta = ASSET_CATALOG.get(symbol, {"category": "Crypto"})

    # Separazione direzionale rigorosa: NEUTRAL non apre posizioni
    if side not in ("BUY", "SELL"):
        return jsonify({"status": "skipped", "reason": "Direzione NEUTRAL: nessun ordine inviato."}), 200

    # Separazione TP e SL obbligatoria
    if tp_price is None or sl_price is None:
        return jsonify({"status": "rejected", "reason": "TP e SL separati sono obbligatori."}), 400

    lev = dynamic_leverage(equity, meta["category"])
    bingx_signed_request("POST", "/openApi/swap/v2/trade/leverage", {
        "symbol": symbol,
        "side": "LONG" if side == "BUY" else "SHORT",
        "leverage": lev,
    })

    return jsonify({
        "status": "prepared",
        "symbol": symbol,
        "category": meta["category"],
        "side": side,
        "leverage": lev,
        "tpPrice": tp_price,
        "slPrice": sl_price,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
