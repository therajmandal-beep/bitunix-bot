"""
BITUNIX AI TRADING BOT + TELEGRAM - COMPLETE FIXED VERSION
"""
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
import threading
from datetime import datetime
import requests
from flask import Flask, request, jsonify

# ─── BOT SETTINGS ────────────────────────────────────────────────────────────
BASE_URL   = "https://fapi.bitunix.com"
SL_PERC    = 0.007
RR         = 1.8
LEVERAGE   = 10
RISK_PERC  = 0.10
BOT_ACTIVE = True

# ─── LOGGING ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ─── HELPERS ─────────────────────────────────────────────────────────────────
def get_env(key):
    val = os.environ.get(key, "")
    if not val:
        log.warning(f"ENV VAR MISSING: {key}")
    return val

# ─── SIGNING ─────────────────────────────────────────────────────────────────
def _sign(nonce, ts, query="", body=""):
    api_key = get_env("BITUNIX_API_KEY")
    secret  = get_env("BITUNIX_SECRET_KEY")
    raw     = nonce + ts + api_key + query + body
    digest  = hashlib.sha256(raw.encode()).hexdigest()
    sign    = hmac.new(
        secret.encode(),
        digest.encode(),
        hashlib.sha256
    ).hexdigest()
    return sign

def _headers(query="", body=""):
    api_key = get_env("BITUNIX_API_KEY")
    nonce   = uuid.uuid4().hex
    ts      = str(int(time.time() * 1000))
    return {
        "api-key"      : api_key,
        "nonce"        : nonce,
        "timestamp"    : ts,
        "sign"         : _sign(nonce, ts, query, body),
        "Content-Type" : "application/json"
    }

# ─── TELEGRAM ────────────────────────────────────────────────────────────────
def send_telegram(message: str):
    token = get_env("TELEGRAM_BOT_TOKEN")
    chat  = get_env("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id"   : chat,
                "text"      : message,
                "parse_mode": "HTML"
            },
            timeout=10
        )
        if r.ok:
            log.info("✅ Telegram sent!")
            return True
        else:
            log.error(f"Telegram error: {r.text}")
            return False
    except Exception as e:
        log.error(f"Telegram exception: {e}")
        return False

def send_trade_alert(action, symbol, price, qty, tp, sl, balance):
    emoji = "🟢 BUY" if action == "buy" else "🔴 SELL"
    send_telegram(
        f"{emoji} <b>{symbol}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"💰 Price:   <b>${price:,.2f}</b>\n"
        f"📦 Qty:     <b>{qty:.4f}</b>\n"
        f"🎯 TP:      <b>${tp:,.2f}</b>\n"
        f"🛑 SL:      <b>${sl:,.2f}</b>\n"
        f"💼 Balance: <b>${balance:,.2f} USDT</b>\n"
        f"⏰ {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
    )

# ─── BITUNIX API ─────────────────────────────────────────────────────────────
def get_balance():
    try:
        r = requests.get(
            f"{BASE_URL}/api/v1/futures/account",
            headers=_headers(),
            timeout=10
        )
        log.info(f"Balance response: {r.status_code} {r.text}")
        data = r.json()
        if not data:
            log.error("Balance API returned empty!")
            return 0.0
        inner = data.get("data", {})
        if not inner:
            log.error(f"No data field in balance: {data}")
            return 0.0
        for a in inner.get("assets", []):
            if a.get("currency", "").upper() == "USDT":
                return float(a.get("available", 0))
        return 0.0
    except Exception as e:
        log.error(f"get_balance error: {e}")
        return 0.0

def get_price(symbol):
    try:
        q = f"symbols={symbol}"
        r = requests.get(
            f"{BASE_URL}/api/v1/futures/market/tickers?{q}",
            headers=_headers(query=q),
            timeout=10
        )
        log.info(f"Price response: {r.status_code} {r.text}")
        tickers = r.json().get("data", [])
        if tickers:
            return float(tickers[0]["lastPrice"])
        raise ValueError(f"No price data for {symbol}")
    except Exception as e:
        log.error(f"get_price error: {e}")
        raise

def set_leverage(symbol, leverage):
    try:
        body = json.dumps(
            {"symbol": symbol, "leverage": leverage},
            separators=(",", ":")
        )
        r = requests.post(
            f"{BASE_URL}/api/v1/futures/leverage",
            headers=_headers(body=body),
            data=body,
            timeout=10
        )
        log.info(f"Leverage response: {r.status_code} {r.text}")
    except Exception as e:
        log.error(f"set_leverage error: {e}")

def place_order(symbol, side, qty, tp, sl):
    payload = {
        "symbol"    : symbol,
        "side"      : side.upper(),
        "orderType" : "MARKET",
        "qty"       : str(round(qty, 6)),
        "effect"    : "GTC",
        "tpPrice"   : str(round(tp, 2)),
        "slPrice"   : str(round(sl, 2)),
        "tpStopType": "MARK_PRICE",
        "slStopType": "MARK_PRICE",
        "clientId"  : uuid.uuid4().hex,
    }
    body = json.dumps(payload, separators=(",", ":"))
    log.info(f"Placing order: {body}")
    headers = _headers(body=body)
    r = requests.post(
        f"{BASE_URL}/api/v1/futures/order/place_order",
        headers=headers,
        data=body,
        timeout=10
    )
    log.info(f"Order response: {r.status_code} {r.text}")
    r.raise_for_status()
    return r.json()

# ─── DUPLICATE GUARD ─────────────────────────────────────────────────────────
last_signals = {}
signals_lock = threading.Lock()

def is_duplicate(symbol, action):
    key = f"{symbol}_{action}"
    now = time.time()
    with signals_lock:
        if key in last_signals and (now - last_signals[key]) < 60:
            return True
        last_signals[key] = now
    return False

# ─── EXECUTE TRADE ────────────────────────────────────────────────────────────
def execute_trade(symbol, action):
    global BOT_ACTIVE
    symbol = symbol.upper()
    action = action.lower()

    if not BOT_ACTIVE:
        return {"status": "blocked", "reason": "Bot is stopped"}

    price   = get_price(symbol)
    balance = get_balance()

    log.info(f"Trading: {action} {symbol} price={price} balance={balance}")

    if balance < 1:
        raise ValueError(f"Balance too low: ${balance:.2f}")

    qty = round((balance * RISK_PERC * LEVERAGE) / price, 4)

    if qty <= 0:
        raise ValueError(f"Qty is zero! balance={balance} price={price}")

    if action == "buy":
        side = "BUY"
        sl   = round(price * (1 - SL_PERC), 2)
        tp   = round(price * (1 + SL_PERC * RR), 2)
    else:
        side = "SELL"
        sl   = round(price * (1 + SL_PERC), 2)
        tp   = round(price * (1 - SL_PERC * RR), 2)

    set_leverage(symbol, LEVERAGE)
    result = place_order(symbol, side, qty, tp, sl)
    send_trade_alert(action, symbol, price, qty, tp, sl, balance)
    log.info(f"✅ Trade done: {side} {qty} {symbol} @ {price}")
    return result

# ─── TELEGRAM POLLING ────────────────────────────────────────────────────────
def telegram_polling():
    global BOT_ACTIVE
    offset = 0
    log.info("🤖 Telegram polling started!")
    send_telegram(
        "🚀 <b>Bitunix Bot is ONLINE!</b>\n"
        f"SL: {SL_PERC*100}% | TP: {SL_PERC*RR*100:.2f}%\n"
        f"Leverage: {LEVERAGE}x | Risk: {RISK_PERC*100:.0f}%\n"
        "Send /help to see commands"
    )
    while True:
        try:
            token = get_env("TELEGRAM_BOT_TOKEN")
            chat  = get_env("TELEGRAM_CHAT_ID")
            if not token:
                time.sleep(10)
                continue
            r = requests.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params={"offset": offset, "timeout": 10},
                timeout=15
            )
            if not r.ok:
                log.error(f"getUpdates failed: {r.status_code} {r.text}")
                time.sleep(5)
                continue
            for update in r.json().get("result", []):
                offset    = update["update_id"] + 1
                msg       = update.get("message", {})
                text      = msg.get("text", "").strip().lower()
                from_chat = str(msg.get("chat", {}).get("id", ""))
                if from_chat != str(chat):
                    continue
                if text in ("/start", "start"):
                    BOT_ACTIVE = True
                    send_telegram("🟢 <b>Bot STARTED!</b> Ready to trade.")
                elif text == "/stop":
                    BOT_ACTIVE = False
                    send_telegram("🔴 <b>Bot STOPPED!</b> Send /start to resume.")
                elif text == "/status":
                    try:
                        bal = get_balance()
                        send_telegram(
                            f"🤖 <b>Bot Status</b>\n"
                            f"{'🟢 RUNNING' if BOT_ACTIVE else '🔴 STOPPED'}\n"
                            f"Balance:  <b>${bal:,.2f} USDT</b>\n"
                            f"Leverage: {LEVERAGE}x\n"
                            f"Risk:     {RISK_PERC*100:.0f}% per trade"
                        )
                    except Exception as e:
                        send_telegram(f"⚠️ Status error: {e}")
                elif text == "/balance":
                    try:
                        send_telegram(f"💼 Balance: <b>${get_balance():,.2f} USDT</b>")
                    except Exception as e:
                        send_telegram(f"⚠️ Balance error: {e}")
                elif text == "/price":
                    try:
                        send_telegram(f"₿ BTC: <b>${get_price('BTCUSDT'):,.2f}</b>")
                    except Exception as e:
                        send_telegram(f"⚠️ Price error: {e}")
                elif text == "/help":
                    send_telegram(
                        "🤖 <b>Commands</b>\n"
                        "━━━━━━━━━━━━━━\n"
                        "/status  — Bot status\n"
                        "/balance — USDT balance\n"
                        "/price   — BTC price\n"
                        "/stop    — Stop trading\n"
                        "/start   — Start trading\n"
                        "/help    — This menu"
                    )
        except Exception as e:
            log.error(f"Telegram polling error: {e}")
        time.sleep(2)

# ─── FLASK APP ────────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data   = request.get_json(force=True)
        action = data.get("action", "").lower()
        symbol = data.get("symbol", "BTCUSDT").upper()
        log.info(f"📡 Webhook: {action.upper()} {symbol}")

        if action not in ("buy", "sell"):
            return jsonify({"error": "unknown action"}), 400
        if is_duplicate(symbol, action):
            return jsonify({"status": "duplicate"}), 200

        result = execute_trade(symbol, action)
        return jsonify({"status": "ok", "result": result}), 200

    except requests.HTTPError as e:
        err = e.response.text
        log.error(f"HTTPError: {err}")
        send_telegram(f"❌ <b>Trade Failed</b>\n{err[:200]}")
        return jsonify({"error": err}), 500
    except Exception as e:
        log.error(f"Webhook error: {e}", exc_info=True)
        send_telegram(f"❌ <b>Error</b>\n{str(e)[:200]}")
        return jsonify({"error": str(e)}), 500

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status"      : "running",
        "bot_active"  : BOT_ACTIVE,
        "bitunix_key" : "SET" if get_env("BITUNIX_API_KEY") else "MISSING",
        "tg_token"    : "SET" if get_env("TELEGRAM_BOT_TOKEN") else "MISSING",
        "tg_chat"     : "SET" if get_env("TELEGRAM_CHAT_ID") else "MISSING",
    })

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "active": BOT_ACTIVE})

@app.route("/test_telegram", methods=["GET"])
def test_telegram():
    result = send_telegram("🧪 Test message from Bitunix Bot!")
    return jsonify({"sent": result})

@app.route("/test_balance", methods=["GET"])
def test_balance():
    bal = get_balance()
    return jsonify({"balance": bal})

@app.route("/test_price", methods=["GET"])
def test_price():
    try:
        price = get_price("BTCUSDT")
        return jsonify({"price": price})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─── MAIN ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("=" * 50)
    log.info("BITUNIX BOT STARTING 🚀")
    log.info(f"BITUNIX_API_KEY:    {'SET ✅' if get_env('BITUNIX_API_KEY') else 'MISSING ❌'}")
    log.info(f"BITUNIX_SECRET_KEY: {'SET ✅' if get_env('BITUNIX_SECRET_KEY') else 'MISSING ❌'}")
    log.info(f"TELEGRAM_BOT_TOKEN: {'SET ✅' if get_env('TELEGRAM_BOT_TOKEN') else 'MISSING ❌'}")
    log.info(f"TELEGRAM_CHAT_ID:   {'SET ✅' if get_env('TELEGRAM_CHAT_ID') else 'MISSING ❌'}")
    log.info("=" * 50)

    t = threading.Thread(target=telegram_polling, daemon=True)
    t.start()
    log.info("Telegram thread started!")

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
    
