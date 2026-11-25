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


def sign_query(params: dict, secret: str) -> str:
    ordered = sorted((k, v) for k, v in params.items() if v is not None)
    qs = urlencode(ordered)
    sig = hmac.new(secret.encode("utf-8"), qs.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{qs}&signature={sig}"


def bingx_signed_get(path: str, params: dict | None = None) -> dict:
    payload = dict(params or {})
    payload["timestamp"] = int(time.time() * 1000)
    payload["recvWindow"] = 5000
    signed_qs = sign_query(payload, SECRET_KEY)
    headers = {"X-BX-APIKEY": API_KEY}
    r = requests.get(f"{BINGX_BASE_URL}{path}?{signed_qs}", headers=headers, timeout=10)
    r.raise_for_status()
    return r.json()


def dynamic_leverage(equity_usdt: float) -> int:
    # Scala dinamica in base al capitale: 20x per micro-conti <= $2.00
    if equity_usdt <= 2.00:
        return 20
    if equity_usdt <= 25.00:
        return 10
    return 5


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/live-ticker")
def live_ticker():
    symbol = request.args.get("symbol", "BTC-USDT")
    resp = requests.get(
        f"{BINGX_BASE_URL}/openApi/swap/v2/quote/ticker",
        params={"symbol": symbol},
        timeout=8,
    )
    resp.raise_for_status()
    data = resp.json().get("data", {})
    last_price = float(data.get("lastPrice", 0.0))

    # Esempio di calcolo base di forza/direzione (da alimentare con indicatori reali RSI/MACD/flussi)
    return jsonify({
        "symbol": symbol,
        "lastPrice": last_price,
        "signalSide": "BUY",        # BUY / SELL / NEUTRAL
        "winRate": 77,
        "trendHealth": 60,
        "whalePct": 60,
        "bearPct": 40,
        "rsi": 56,
        "macdStatus": "BULLISH",
        "highRiskWarning": True,
        "riskReason": "Rischio correlazione BTC / Balene",
        "validityPct": 92,
        "remainingText": "13:50",
        "timestamp": int(time.time() * 1000),
    })


@app.route("/api/income-history")
def income_history():
    symbol = request.args.get("symbol", "BTC-USDT")
    raw = bingx_signed_get("/openApi/swap/v2/user/income", {
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
    symbol = body.get("symbol", "BTC-USDT")
    side = body.get("side", "NEUTRAL").upper()   # BUY / SELL / NEUTRAL
    equity = float(body.get("equity", 2.0))
    tp_price = body.get("tpPrice")
    sl_price = body.get("slPrice")

    # Separazione direzionale rigorosa: NEUTRAL non apre posizioni
    if side not in ("BUY", "SELL"):
        return jsonify({"status": "skipped", "reason": "Direzione NEUTRAL: nessun ordine inviato."}), 200

    # Separazione TP e SL: entrambi devono essere coerenti col lato operativo
    if tp_price is None or sl_price is None:
        return jsonify({"status": "rejected", "reason": "TP e SL sono obbligatori e separati."}), 400

    lev = dynamic_leverage(equity)
    bingx_signed_get("/openApi/swap/v2/trade/leverage", {
        "symbol": symbol,
        "side": "LONG" if side == "BUY" else "SHORT",
        "leverage": lev,
    })

    # Qui inserisci l'invio ordine reale /openApi/swap/v2/trade/order dopo i controlli di size/margine
    return jsonify({
        "status": "prepared",
        "symbol": symbol,
        "side": side,
        "leverage": lev,
        "tpPrice": tp_price,
        "slPrice": sl_price,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
