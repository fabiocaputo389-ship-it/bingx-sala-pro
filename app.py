import os
import time
import hmac
import hashlib
from urllib.parse import urlencode
import requests
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

API_KEY = os.getenv("BINGX_API_KEY", "")
API_SECRET = os.getenv("BINGX_SECRET_KEY", "")
BASE_URL = "https://open-api.bingx.com"


def sign_params(params: dict) -> str:
    query = urlencode(sorted(params.items()))
    return hmac.new(
        API_SECRET.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()


def bingx_req(method: str, path: str, params: dict = None, signed: bool = False):
    params = dict(params or {})
    headers = {"X-BX-APIKEY": API_KEY}
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        sig = sign_params(params)
        query = urlencode(sorted(params.items()))
        url = f"{BASE_URL}{path}?{query}&signature={sig}"
    else:
        query = urlencode(sorted(params.items()))
        url = f"{BASE_URL}{path}" + (f"?{query}" if query else "")

    try:
        r = requests.request(method, url, headers=headers, timeout=10)
        return r.json()
    except Exception as e:
        return {"code": -1, "msg": str(e)}


def calc_dynamic_leverage(equity: float) -> int:
    if equity <= 2.00:
        return 20
    if equity <= 50.00:
        return 15
    if equity <= 500.00:
        return 10
    return 5


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/api/markets")
def api_markets():
    contracts = bingx_req("GET", "/openApi/swap/v2/quote/contracts")
    tickers = bingx_req("GET", "/openApi/swap/v2/quote/ticker")

    t_map = {}
    if tickers.get("code") == 0:
        for item in tickers.get("data", []):
            t_map[item.get("symbol")] = item

    out = []
    if contracts.get("code") == 0:
        for c in contracts.get("data", []):
            sym = c.get("symbol")
            tk = t_map.get(sym, {})
            out.append({
                "symbol": sym,
                "maxLeverage": c.get("maxLeverage", 20),
                "lastPrice": float(tk.get("lastPrice", 0) or 0),
                "priceChangePercent": float(tk.get("priceChangePercent", 0) or 0),
                "volume": float(tk.get("volume", 0) or 0),
            })

    out.sort(key=lambda x: x["volume"], reverse=True)
    return jsonify({"code": 0, "count": len(out), "markets": out})


@app.route("/api/live-ticker")
def api_live_ticker():
    symbol = request.args.get("symbol", "BTC-USDT")
    ticker = bingx_req("GET", "/openApi/swap/v2/quote/ticker", {"symbol": symbol})
    depth = bingx_req("GET", "/openApi/swap/v2/quote/depth", {"symbol": symbol, "limit": 10})
    funding = bingx_req("GET", "/openApi/swap/v2/quote/premiumIndex", {"symbol": symbol})
    return jsonify({
        "ticker": ticker.get("data", {}),
        "depth": depth.get("data", {}),
        "funding": funding.get("data", {})
    })


@app.route("/api/account")
def api_account():
    balance = bingx_req("GET", "/openApi/swap/v2/user/balance", signed=True)
    positions = bingx_req("GET", "/openApi/swap/v2/user/positions", signed=True)
    income = bingx_req("GET", "/openApi/swap/v2/user/income", {"limit": 20}, signed=True)

    equity = 0.0
    available = 0.0
    if balance.get("code") == 0 and isinstance(balance.get("data"), dict):
        b = balance["data"].get("balance", {})
        equity = float(b.get("equity", 0) or 0)
        available = float(b.get("availableMargin", 0) or 0)

    return jsonify({
        "equity": equity,
        "available": available,
        "recommended_leverage": calc_dynamic_leverage(equity),
        "positions": positions.get("data", []) if positions.get("code") == 0 else [],
        "income_history": income.get("data", []) if income.get("code") == 0 else []
    })


@app.route("/api/order", methods=["POST"])
def api_order():
    data = request.get_json(force=True) or {}
    symbol = data.get("symbol", "BTC-USDT")
    side = str(data.get("side", "NEUTRAL")).upper()
    order_type = str(data.get("type", "MARKET")).upper()
    qty = float(data.get("quantity", 0) or 0)
    leverage = int(data.get("leverage", 20) or 20)
    price = data.get("price")
    tp = data.get("tp")
    sl = data.get("sl")

    if side not in ("BUY", "SELL"):
        return jsonify({"code": -1, "msg": "Stato NEUTRAL: nessun ordine inviato"}), 400
    if qty <= 0:
        return jsonify({"code": -1, "msg": "Quantità non valida"}), 400

    pos_side = "LONG" if side == "BUY" else "SHORT"

    bingx_req("POST", "/openApi/swap/v2/trade/leverage", {
        "symbol": symbol,
        "side": pos_side,
        "leverage": leverage
    }, signed=True)

    order_params = {
        "symbol": symbol,
        "side": side,
        "positionSide": pos_side,
        "type": order_type,
        "quantity": qty
    }
    if order_type == "LIMIT" and price:
        order_params["price"] = float(price)
    if tp:
        order_params["takeProfit"] = f'{{"type":"TAKE_PROFIT_MARKET","stopPrice":{float(tp)},"price":{float(tp)},"workingType":"MARK_PRICE"}}'
    if sl:
        order_params["stopLoss"] = f'{{"type":"STOP_MARKET","stopPrice":{float(sl)},"price":{float(sl)},"workingType":"MARK_PRICE"}}'

    res = bingx_req("POST", "/openApi/swap/v2/trade/order", order_params, signed=True)
    return jsonify(res)


@app.route("/api/close-all", methods=["POST"])
def api_close_all():
    data = request.get_json(silent=True) or {}
    params = {"symbol": data["symbol"]} if data.get("symbol") else {}
    res = bingx_req("POST", "/openApi/swap/v2/trade/closeAllPositions", params, signed=True)
    return jsonify(res)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
