"""
BITUNIX AI TRADING BOT + TELEGRAM - FIXED VERSION
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

# ─── KEYS FROM RAILWAY ENVIRONMENT VARIABLES ─────────────────────────────────
BITUNIX_API_KEY    = os.environ.get("4305043dfc9bf65857f86d215b3cbd1c",    "")
BITUNIX_SECRET_KEY = os.environ.get("136027f1d25dad59f3350426c9965d68", "")
TELEGRAM_BOT_TOKEN = os.environ.get("8712205632:AAGM2HstEIuz_ttIHBaMwkPILER5uNAf2l0", "")
TELEGRAM_CHAT_ID   = os.environ.get("1256115118",   "")

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

# ─── TELEGRAM ────────────────────────────────────────────────────────────────
def send_telegram(message: str):
    """Send message to Telegram."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat  = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        log.warning(f"Telegram not configured! TOKEN={bool(token)} CHAT={bool(chat)}")
        return False
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        r   = requests.post(url, json={
            "chat_id"    : chat,
            "text"       : message,
            "parse_mode" : "HTML"
        }, timeout=10)
        if r.ok:
            log.info("✅ Telegram message sent!")
            return True
        else:
            log.error(f"Telegram failed: {r.text}")
            return False
    except Exception as e:
        log.error(f"Telegram error: {e}")
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

# ─── BITUNIX API ──────────────────────────────────────────────────────────────
def _sign(nonce, ts, query="", body=""):
    secret = os.environ.get("BITUNIX_SECRET_KEY", "")
    key    = os.environ.get("BITUNIX_API_KEY", "")
    raw    = nonce + ts + key + query + body
    digest = hashlib.sha256(raw.encode()).hexdigest()
    sign   = hmac.new(
        secret.encode(),
        digest.encode(),
        hashlib.sha256
    ).hexdigest()
    log.info(f"Sign input: nonce={nonce} ts={ts} query={query} body={body}")
    log.info(f"Digest: {digest}")
    log.info(f"Sign: {sign}")
    return sign

def get_balance():
    r = requests.get(f"{BASE_URL}/api/v1/futures/account", headers=_headers())
    log.info(f"Balance response: {r.status_code} {r.text}")
    r.raise_for_status()
    for a in r.json().get("data", {}).get("assets", []):
        if a.get("currency", "").upper() == "USDT":
            return float(a.get("available", 0))
    return 0.0
def get_price(symbol):
    q = f"symbols={symbol}"
    r = requests.get(f"{BASE_URL}/api/v1/futures/market/tickers?{q}", headers=_headers(query=q))
    r.raise_for_status()
    tickers = r.json().get("data", [])
    if tickers: return float(tickers[0]["lastPrice"])
    raise ValueError(f"No price for {symbol}")

def set_leverage(symbol, leverage):
    body = json.dumps({"symbol": symbol, "leverage": leverage}, separators=(",", ":"))
    requests.post(f"{BASE_URL}/api/v1/futures/leverage", headers=_headers(body=body), data=body)

def place_order(symbol, side, qty, tp, sl):
    body = json.dumps({
        "symbol": symbol, "side": side.upper(), "orderType": "MARKET",
        "qty": str(round(qty, 6)), "effect": "GTC",
        "tpPrice": str(round(tp, 2)), "slPrice": str(round(sl, 2)),
        "tpStopType": "MARK_PRICE", "slStopType": "MARK_PRICE",
        "clientId": uuid.uuid4().hex,
    }, separators=(",", ":"))
    headers = _headers(body=body)
    r = requests.post(
        f"{BASE_URL}/api/v1/futures/order/place_order",
        headers=headers,
        data=body
    )
    log.info(f"Order response: {r.status_code} {r.text}")
    r.raise_for_status()
    return r.json()

# ─── DUPLICATE GUARD ──────────────────────────────────────────────────────────
last_signals = {}
def is_duplicate(symbol, action):
    key = f"{symbol}_{action}"
    now = time.time()
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
    qty     = round((balance * RISK_PERC * LEVERAGE) / price, 4)

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
    log.info(f"✅ Trade: {side} {qty} {symbol} @ {price}")
    return result

# ─── TELEGRAM COMMAND HANDLER ─────────────────────────────────────────────────
def telegram_polling():
    """Poll Telegram for commands."""
    global BOT_ACTIVE
    offset = 0
    log.info("🤖 Telegram polling STARTED!")

    # Send startup message
    send_telegram(
        "🚀 <b>Bitunix Bot is ONLINE!</b>\n"
        f"SL: {SL_PERC*100}% | TP: {SL_PERC*RR*100:.2f}%\n"
        f"Leverage: {LEVERAGE}x | Risk: {RISK_PERC*100:.0f}%\n"
        "Send /help to see commands"
    )

    while True:
        try:
            token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
            chat  = os.environ.get("TELEGRAM_CHAT_ID", "")
            if not token:
                log.error("❌ TELEGRAM_BOT_TOKEN is empty!")
                time.sleep(10)
                continue

            url = f"https://api.telegram.org/bot{token}/getUpdates"
            r   = requests.get(url, params={"offset": offset, "timeout": 10}, timeout=15)

            if not r.ok:
                log.error(f"Telegram getUpdates failed: {r.status_code}")
                time.sleep(5)
                continue

            updates = r.json().get("result", [])
            for update in updates:
                offset = update["update_id"] + 1
                msg    = update.get("message", {})
                text   = msg.get("text", "").strip().lower()
                from_chat = str(msg.get("chat", {}).get("id", ""))

                log.info(f"TG message from {from_chat}: {text}")

                # Only respond to your chat
                if from_chat != str(chat):
                    log.warning(f"Ignored message from unknown chat: {from_chat}")
                    continue

                if text in ("/start", "start"):
                    BOT_ACTIVE = True
                    send_telegram("🟢 <b>Bot STARTED!</b> Ready to trade.\nSend /help for commands.")

                elif text == "/help":
                    send_telegram(
                        "🤖 <b>Bitunix Bot Commands</b>\n"
                        "━━━━━━━━━━━━━━━━\n"
                        "/status  — Bot status\n"
                        "/balance — USDT balance\n"
                        "/price   — BTC price\n"
                        "/stop    — Stop trading\n"
                        "/start   — Start trading\n"
                        "/help    — This menu"
                    )

                elif text == "/status":
                    try:
                        bal = get_balance()
                        send_telegram(
                            f"🤖 <b>Bot Status</b>\n"
                            f"Status:   {'🟢 RUNNING' if BOT_ACTIVE else '🔴 STOPPED'}\n"
                            f"Balance:  <b>${bal:,.2f} USDT</b>\n"
                            f"Leverage: {LEVERAGE}x\n"
                            f"Risk:     {RISK_PERC*100:.0f}% per trade"
                        )
                    except Exception as e:
                        send_telegram(f"⚠️ Status error: {e}")

                elif text == "/balance":
                    try:
                        bal = get_balance()
                        send_telegram(f"💼 Balance: <b>${bal:,.2f} USDT</b>")
                    except Exception as e:
                        send_telegram(f"⚠️ Balance error: {e}")

                elif text == "/price":
                    try:
                        p = get_price("BTCUSDT")
                        send_telegram(f"₿ BTC/USDT: <b>${p:,.2f}</b>")
                    except Exception as e:
                        send_telegram(f"⚠️ Price error: {e}")

                elif text == "/stop":
                    BOT_ACTIVE = False
                    send_telegram("🔴 <b>Bot STOPPED</b>\nNo new trades will be placed.\nSend /start to resume.")

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
        price  = data.get("price", "?")
        log.info(f"📡 Signal: {action.upper()} {symbol} @ {price}")

        if action not in ("buy", "sell"):
            return jsonify({"error": "unknown action"}), 400
        if is_duplicate(symbol, action):
            return jsonify({"status": "duplicate"}), 200

        result = execute_trade(symbol, action)
        return jsonify({"status": "ok", "result": result}), 200

    except requests.HTTPError as e:
        err = e.response.text
        send_telegram(f"❌ <b>Trade Failed</b>\n{err[:200]}")
        return jsonify({"error": err}), 500
    except Exception as e:
        log.error(f"Webhook error: {e}", exc_info=True)
        send_telegram(f"❌ <b>Error</b>\n{str(e)[:200]}")
        return jsonify({"error": str(e)}), 500

@app.route("/", methods=["GET"])
def home():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat  = os.environ.get("TELEGRAM_CHAT_ID", "")
    return jsonify({
        "status"         : "running",
        "bot_active"     : BOT_ACTIVE,
        "telegram_token" : "SET" if token else "MISSING",
        "telegram_chat"  : "SET" if chat else "MISSING",
        "bitunix_key"    : "SET" if os.environ.get("BITUNIX_API_KEY") else "MISSING"
    })

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "active": BOT_ACTIVE})

@app.route("/test_telegram", methods=["GET"])
def test_telegram():
    """Test endpoint to check Telegram is working."""
    result = send_telegram("🧪 Test message from Bitunix Bot!")
    return jsonify({"sent": result})

# ─── MAIN ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Log all env vars status (not values)
    log.info("=" * 50)
    log.info("BITUNIX BOT STARTING ON RAILWAY 🚀")
    log.info(f"BITUNIX_API_KEY:    {'SET ✅' if os.environ.get('BITUNIX_API_KEY') else 'MISSING ❌'}")
    log.info(f"BITUNIX_SECRET_KEY: {'SET ✅' if os.environ.get('BITUNIX_SECRET_KEY') else 'MISSING ❌'}")
    log.info(f"TELEGRAM_BOT_TOKEN: {'SET ✅' if os.environ.get('TELEGRAM_BOT_TOKEN') else 'MISSING ❌'}")
    log.info(f"TELEGRAM_CHAT_ID:   {'SET ✅' if os.environ.get('TELEGRAM_CHAT_ID') else 'MISSING ❌'}")
    log.info("=" * 50)

    # Start Telegram polling in background thread
    t = threading.Thread(target=telegram_polling, daemon=True)
    t.start()
    log.info("Telegram thread started!")

    # Start Flask
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
    
