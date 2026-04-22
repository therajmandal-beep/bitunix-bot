"""
BITUNIX AI TRADING BOT + TELEGRAM - FULLY FIXED VERSION
All commands working: /status /balance /price /stop /start /help
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

# ─── TELEGRAM ────────────────────────────────────────────────────────────────
def send_telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat  = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        log.warning("Telegram not configured!")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": message, "parse_mode": "HTML"},
            timeout=10
        )
        if r.ok:
            log.info("✅ Telegram sent!")
            return True
        log.error(f"Telegram error: {r.text}")
        return False
    except Exception as e:
        log.error(f"Telegram exception: {e}")
        return False

# ─── BITUNIX API HELPERS ─────────────────────────────────────────────────────
def make_headers(query="", body=""):
    """Build signed Bitunix headers — always reads fresh from env."""
    api_key    = os.environ.get("BITUNIX_API_KEY", "")
    secret_key = os.environ.get("BITUNIX_SECRET_KEY", "")
    nonce      = uuid.uuid4().hex
    ts         = str(int(time.time() * 1000))
    raw        = nonce + ts + api_key + query + body
    digest     = hashlib.sha256(raw.encode()).hexdigest()
    signature  = hmac.new(secret_key.encode(), digest.encode(), hashlib.sha256).hexdigest()
    return {
        "api-key"      : api_key,
        "nonce"        : nonce,
        "timestamp"    : ts,
        "sign"         : signature,
        "Content-Type" : "application/json"
    }

def get_balance():
    r = requests.get(f"{BASE_URL}/api/v1/futures/account",
                     headers=make_headers(), timeout=10)
    r.raise_for_status()
    for asset in r.json().get("data", {}).get("assets", []):
        if asset.get("currency", "").upper() == "USDT":
            return float(asset.get("available", 0))
    return 0.0

def get_price(symbol):
    q = f"symbols={symbol}"
    r = requests.get(f"{BASE_URL}/api/v1/futures/market/tickers?{q}",
                     headers=make_headers(query=q), timeout=10)
    r.raise_for_status()
    data = r.json().get("data", [])
    if data:
        return float(data[0]["lastPrice"])
    raise ValueError(f"No ticker data for {symbol}")

def set_leverage(symbol, leverage):
    body = json.dumps({"symbol": symbol, "leverage": leverage}, separators=(",", ":"))
    requests.post(f"{BASE_URL}/api/v1/futures/leverage",
                  headers=make_headers(body=body), data=body, timeout=10)

def place_order(symbol, side, qty, tp, sl):
    body = json.dumps({
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
    }, separators=(",", ":"))
    r = requests.post(f"{BASE_URL}/api/v1/futures/order/place_order",
                      headers=make_headers(body=body), data=body, timeout=10)
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
        send_telegram(f"⚠️ Signal received but bot is STOPPED\n{action.upper()} {symbol}")
        return {"status": "blocked", "reason": "Bot stopped"}

    price   = get_price(symbol)
    balance = get_balance()
    qty     = round((balance * RISK_PERC * LEVERAGE) / price, 4)

    if qty <= 0:
        raise ValueError(f"Qty too small: {qty}")

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
    log.info(f"✅ Trade done: {side} {qty} {symbol} @ {price}")
    return result

# ─── TELEGRAM POLLING ─────────────────────────────────────────────────────────
def telegram_polling():
    global BOT_ACTIVE
    offset = 0
    log.info("🤖 Telegram polling started!")

    send_telegram(
        "🚀 <b>Bitunix Bot is ONLINE!</b>\n"
        f"SL: {SL_PERC*100}% | TP: {SL_PERC*RR*100:.2f}%\n"
        f"Leverage: {LEVERAGE}x | Risk: {RISK_PERC*100:.0f}%\n"
        "Send /help to see all commands"
    )

    while True:
        try:
            token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
            chat  = os.environ.get("TELEGRAM_CHAT_ID", "")

            if not token:
                log.error("❌ TELEGRAM_BOT_TOKEN missing!")
                time.sleep(10)
                continue

            r = requests.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params={"offset": offset, "timeout": 10},
                timeout=15
            )

            if not r.ok:
                time.sleep(5)
                continue

            for update in r.json().get("result", []):
                offset    = update["update_id"] + 1
                msg       = update.get("message", {})
                text      = msg.get("text", "").strip().lower()
                from_chat = str(msg.get("chat", {}).get("id", ""))

                log.info(f"TG from {from_chat}: {text}")

                if from_chat != str(chat):
                    continue

                if text in ("/start", "start"):
                    BOT_ACTIVE = True
                    send_telegram("🟢 <b>Bot STARTED!</b> Ready to trade.\nSend /help for commands.")

                elif text == "/stop":
                    BOT_ACTIVE = False
                    send_telegram("🔴 <b>Bot STOPPED</b>\nNo trades will be placed.\nSend /start to resume.")

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
                            f"━━━━━━━━━━━━\n"
                            f"Status:   {'🟢 RUNNING' if BOT_ACTIVE else '🔴 STOPPED'}\n"
                            f"Balance:  <b>${bal:,.2f} USDT</b>\n"
                            f"Leverage: {LEVERAGE}x\n"
                            f"Risk:     {RISK_PERC*100:.0f}%/trade\n"
                            f"SL: {SL_PERC*100}% | TP: {SL_PERC*RR*100:.2f}%"
                        )
                    except Exception as e:
                        send_telegram(f"⚠️ Status error: {str(e)[:100]}")

                elif text == "/balance":
                    try:
                        bal = get_balance()
                        send_telegram(f"💼 Balance: <b>${bal:,.2f} USDT</b>")
                    except Exception as e:
                        send_telegram(f"⚠️ Balance error: {str(e)[:100]}")

                elif text == "/price":
                    try:
                        p = get_price("BTCUSDT")
                        send_telegram(f"₿ BTC/USDT: <b>${p:,.2f}</b>")
                    except Exception as e:
                        send_telegram(f"⚠️ Price error: {str(e)[:100]}")

        except Exception as e:
            log.error(f"Polling error: {e}")

        time.sleep(2)

# ─── FLASK ────────────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data   = request.get_json(force=True)
        action = data.get("action", "").lower()
        symbol = data.get("symbol", "BTCUSDT").upper()
        log.info(f"📡 {action.upper()} {symbol}")
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
    return jsonify({
        "status"         : "Bitunix Bot Running 🚀",
        "bot_active"     : BOT_ACTIVE,
        "telegram_token" : "SET ✅" if os.environ.get("TELEGRAM_BOT_TOKEN") else "MISSING ❌",
        "telegram_chat"  : "SET ✅" if os.environ.get("TELEGRAM_CHAT_ID")   else "MISSING ❌",
        "bitunix_key"    : "SET ✅" if os.environ.get("BITUNIX_API_KEY")    else "MISSING ❌",
    })

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "active": BOT_ACTIVE})

@app.route("/test_telegram", methods=["GET"])
def test_tg():
    ok = send_telegram("🧪 Test from Bitunix Bot — working!")
    return jsonify({"sent": ok})

# ─── MAIN ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("=" * 50)
    log.info("BITUNIX BOT STARTING 🚀")
    log.info(f"BITUNIX_API_KEY:    {'SET ✅' if os.environ.get('BITUNIX_API_KEY')    else 'MISSING ❌'}")
    log.info(f"BITUNIX_SECRET_KEY: {'SET ✅' if os.environ.get('BITUNIX_SECRET_KEY') else 'MISSING ❌'}")
    log.info(f"TELEGRAM_BOT_TOKEN: {'SET ✅' if os.environ.get('TELEGRAM_BOT_TOKEN') else 'MISSING ❌'}")
    log.info(f"TELEGRAM_CHAT_ID:   {'SET ✅' if os.environ.get('TELEGRAM_CHAT_ID')   else 'MISSING ❌'}")
    log.info("=" * 50)

    threading.Thread(target=telegram_polling, daemon=True).start()
    log.info("Telegram thread started!")

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
        
