import os
import time
import hmac
import hashlib
import requests
from urllib.parse import urlencode
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

API_KEY = os.getenv("BINGX_API_KEY", "")
API_SECRET = os.getenv("BINGX_SECRET_KEY", "")
BASE_URL = "https://open-api.bingx.com"


def generate_signature(params_str: str) -> str:
    return hmac.new(
        API_SECRET.encode("utf-8"),
        params_str.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()


def bingx_request(method: str, path: str, params: dict = None, signed: bool = False):
    if params is None:
        params = {}

    headers = {"X-BX-APIKEY": API_KEY}
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        sorted_params = sorted(params.items())
        query_string = urlencode(sorted_params)
        signature = generate_signature(query_string)
        url = f"{BASE_URL}{path}?{query_string}&signature={signature}"
    else:
        query_string = urlencode(params)
        url = f"{BASE_URL}{path}?{query_string}" if query_string else f"{BASE_URL}{path}"

    try:
        if method == "GET":
            resp = requests.get(url, headers=headers, timeout=10)
        elif method == "POST":
            resp = requests.post(url, headers=headers, timeout=10)
        elif method == "DELETE":
            resp = requests.delete(url, headers=headers, timeout=10)
        else:
            return {"code": -1, "msg": "Metodo non supportato"}
        return resp.json()
    except Exception as e:
        return {"code": -1, "msg": str(e)}


def calculate_dynamic_leverage(equity: float) -> int:
    """Regola istituzionale di scalatura leva basata sul capitale."""
    if equity <= 2.00:
        return 20
    elif equity <= 50.00:
        return 15
    elif equity <= 500.00:
        return 10
    return 5


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/markets", methods=["GET"])
def get_all_markets():
    """Scarica l'intero mercato Perpetual Swap BingX + Ticker 24h."""
    contracts = bingx_request("GET", "/openApi/swap/v2/quote/contracts")
    tickers = bingx_request("GET", "/openApi/swap/v2/quote/ticker")

    ticker_map = {}
    if tickers.get("code") == 0:
        for t in tickers.get("data", []):
            ticker_map[t["symbol"]] = t

    markets = []
    if contracts.get("code") == 0:
        for c in contracts.get("data", []):
            sym = c["symbol"]
            t_info = ticker_map.get(sym, {})
            markets.append({
                "symbol": sym,
                "asset": c.get("asset"),
                "currency": c.get("currency"),
                "maxLeverage": c.get("maxLeverage", 20),
                "minQty": c.get("tradeMinQuantity", 0.001),
                "lastPrice": float(t_info.get("lastPrice", 0)),
                "priceChangePercent": float(t_info.get("priceChangePercent", 0)),
                "volume": float(t_info.get("volume", 0)),
                "highPrice": float(t_info.get("highPrice", 0)),
                "lowPrice": float(t_info.get("lowPrice", 0)),
            })

    # Ordina per volume 24h decrescente
    markets.sort(key=lambda x: x["volume"], reverse=True)
    return jsonify({"status": "ok", "count": len(markets), "markets": markets})


@app.route("/api/live-ticker", methods=["GET"])
def live_ticker():
    """Endpoint ad alta frequenza per prezzo, order book e funding rate."""
    symbol = request.args.get("symbol", "BTC-USDT")
    ticker = bingx_request("GET", "/openApi/swap/v2/quote/ticker", {"symbol": symbol})
    depth = bingx_request("GET", "/openApi/swap/v2/quote/depth", {"symbol": symbol, "limit": 10})
    funding = bingx_request("GET", "/openApi/swap/v2/quote/premiumIndex", {"symbol": symbol})

    return jsonify({
        "ticker": ticker.get("data", {}),
        "depth": depth.get("data", {}),
        "funding": funding.get("data", {})
    })


@app.route("/api/account", methods=["GET"])
def get_account():
    """Recupera saldo reale, posizioni aperte e storico PnL chiuso."""
    balance = bingx_request("GET", "/openApi/swap/v2/user/balance", signed=True)
    positions = bingx_request("GET", "/openApi/swap/v2/user/positions", signed=True)
    income = bingx_request("GET", "/openApi/swap/v2/user/income", {"limit": 20}, signed=True)

    equity = 0.0
    available = 0.0
    if balance.get("code") == 0 and "balance" in balance.get("data", {}):
        b = balance["data"]["balance"]
        equity = float(b.get("equity", 0))
        available = float(b.get("availableMargin", 0))

    rec_leverage = calculate_dynamic_leverage(equity)

    return jsonify({
        "equity": equity,
        "available": available,
        "recommended_leverage": rec_leverage,
        "positions": positions.get("data", []),
        "income_history": income.get("data", [])
    })


@app.route("/api/order", methods=["POST"])
def execute_order():
    """Esecuzione ordine professionale con separazione stretta BUY/SELL e TP/SL."""
    data = request.json
    symbol = data.get("symbol", "BTC-USDT")
    side = data.get("side", "NEUTRAL").upper()
    order_type = data.get("type", "MARKET").upper()
    quantity = float(data.get("quantity", 0))
    price = data.get("price")
    tp_price = data.get("tp")
    sl_price = data.get("sl")
    leverage = int(data.get("leverage", 20))

    if side not in ["BUY", "SELL"]:
        return jsonify({"code": -1, "msg": "Stato NEUTRAL o direzione non valida. Nessun ordine inviato."}), 400

    position_side = "LONG" if side == "BUY" else "SHORT"

    # 1. Imposta la leva prima dell'ordine
    bingx_request("POST", "/openApi/swap/v2/trade/leverage", {
        "symbol": symbol,
        "side": position_side,
        "leverage": leverage
    }, signed=True)

    # 2. Costruisci parametri ordine principale
    order_params = {
        "symbol": symbol,
        "side": side,
        "positionSide": position_side,
        "type": order_type,
        "quantity": quantity
    }
    if order_type == "LIMIT" and price:
        order_params["price"] = float(price)

    if tp_price:
        order_params["takeProfit"] = f'{{"type": "TAKE_PROFIT_MARKET", "stopPrice": {float(tp_price)}, "price": {float(tp_price)}, "workingType": "MARK_PRICE"}}'
    if sl_price:
        order_params["stopLoss"] = f'{{"type": "STOP_MARKET", "stopPrice": {float(sl_price)}, "price": {float(sl_price)}, "workingType": "MARK_PRICE"}}'

    result = bingx_request("POST", "/openApi/swap/v2/trade/order", order_params, signed=True)
    return jsonify(result)


@app.route("/api/close-all", methods=["POST"])
def panic_close_all():
    """Pulsante di emergenza istituzionale: chiude tutte le posizioni a mercato."""
    symbol = request.json.get("symbol")
    params = {"symbol": symbol} if symbol else {}
    res = bingx_request("POST", "/openApi/swap/v2/trade/closeAllPositions", params, signed=True)
    return jsonify(res)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
