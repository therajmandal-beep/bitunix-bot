"""
╔══════════════════════════════════════════════════════════════╗
║   BITUNIX AI TRADING BOT + TELEGRAM — RAILWAY VERSION       ║
║   Runs 24/7 on Railway cloud — no PC needed!                ║
╚══════════════════════════════════════════════════════════════╝
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
TELEGRAM_BOT_TOKEN = os.environ.get("8219770240:AAG0l89QA39RPlilYxyJCVQtsGmR5ZoF5Jc", "")
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
    if not TELEGRAM_BOT_TOKEN: return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=5
        )
    except Exception as e:
        log.error(f"Telegram error: {e}")

def send_trade_alert(action, symbol, price, qty, tp, sl, balance):
    emoji = "🟢 BUY" if action == "buy" else "🔴 SELL"
    send_telegram(
        f"{emoji} <b>{symbol}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"💰 Price:   <b>${price:,.2f}</b>\n"
        f"📦 Qty:     <b>{qty:.4f}</b>\n"
        f"🎯 TP:      <b>${tp:,.2f}</b> (+{SL_PERC*RR*100:.2f}%)\n"
        f"🛑 SL:      <b>${sl:,.2f}</b> (-{SL_PERC*100:.1f}%)\n"
        f"💼 Balance: <b>${balance:,.2f} USDT</b>\n"
        f"⏰ Time:    {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
    )

# ─── AI AGENT ────────────────────────────────────────────────────────────────
def ai_should_trade(action, symbol):
    score, reasons = 0, []
    try:
        balance = get_balance()
        if balance >= 50:
            score += 1; reasons.append(f"Balance OK (${balance:.2f})")
        else:
            return False, f"Balance too low (${balance:.2f})"
        if BOT_ACTIVE:
            score += 1; reasons.append("Bot active ✓")
        else:
            return False, "Bot is stopped"
        q = f"symbols={symbol}"
        r = requests.get(f"{BASE_URL}/api/v1/futures/market/tickers?{q}",
                         headers=_headers(query=q), timeout=5)
        if r.ok:
            chg = float(r.json().get("data", [{}])[0].get("priceChangePercent", 0))
            if (action == "buy" and chg > -5) or (action == "sell" and chg < 5):
                score += 1; reasons.append(f"24h change OK ({chg:+.1f}%)")
        return (score >= 2), " | ".join(reasons)
    except Exception as e:
        return True, f"AI check error: {e}"

# ─── BITUNIX API ──────────────────────────────────────────────────────────────
def _sign(nonce, ts, query="", body=""):
    digest = hashlib.sha256((nonce + ts + BITUNIX_API_KEY + query + body).encode()).hexdigest()
    return hmac.new(BITUNIX_SECRET_KEY.encode(), digest.encode(), hashlib.sha256).hexdigest()

def _headers(query="", body=""):
    nonce = uuid.uuid4().hex
    ts    = str(int(time.time() * 1000))
    return {
        "api-key": BITUNIX_API_KEY, "nonce": nonce, "timestamp": ts,
        "sign": _sign(nonce, ts, query, body), "Content-Type": "application/json"
    }

def get_balance():
    r = requests.get(f"{BASE_URL}/api/v1/futures/account", headers=_headers())
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
    r = requests.post(f"{BASE_URL}/api/v1/futures/order/place_order",
                      headers=_headers(body=body), data=body)
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
    ok, reason = ai_should_trade(action, symbol)
    log.info(f"AI: {reason}")
    if not ok:
        send_telegram(f"⚠️ <b>Trade BLOCKED</b>\n{symbol} {action.upper()}\n{reason}")
        return {"status": "blocked", "reason": reason}
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

# ─── TELEGRAM COMMANDS ────────────────────────────────────────────────────────
def handle_telegram_commands():
    global BOT_ACTIVE
    if not TELEGRAM_BOT_TOKEN: return
    offset = 0
    log.info("Telegram listening...")
    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                params={"offset": offset, "timeout": 10}, timeout=15
            )
            for update in r.json().get("result", []):
                offset = update["update_id"] + 1
                msg    = update.get("message", {})
                text   = msg.get("text", "").strip().lower()
                chat   = str(msg.get("chat", {}).get("id", ""))
                if chat != str(TELEGRAM_CHAT_ID): continue
                if text == "/status":
                    bal = get_balance()
                    send_telegram(
                        f"🤖 <b>Bot Status</b>\n"
                        f"Status: {'🟢 RUNNING' if BOT_ACTIVE else '🔴 STOPPED'}\n"
                        f"Balance: <b>${bal:,.2f} USDT</b>\n"
                        f"Leverage: {LEVERAGE}x | Risk: {RISK_PERC*100:.0f}%"
                    )
                elif text == "/balance":
                    send_telegram(f"💼 Balance: <b>${get_balance():,.2f} USDT</b>")
                elif text == "/price":
                    send_telegram(f"₿ BTC: <b>${get_price('BTCUSDT'):,.2f}</b>")
                elif text == "/stop":
                    BOT_ACTIVE = False
                    send_telegram("🔴 <b>Bot STOPPED</b> — send /start to resume")
                elif text == "/start":
                    BOT_ACTIVE = True
                    send_telegram("🟢 <b>Bot STARTED</b> — ready to trade!")
                elif text == "/help":
                    send_telegram(
                        "🤖 <b>Commands</b>\n"
                        "/status  — Bot status\n"
                        "/balance — USDT balance\n"
                        "/price   — BTC price\n"
                        "/stop    — Stop bot\n"
                        "/start   — Start bot\n"
                        "/help    — This menu"
                    )
        except Exception as e:
            log.error(f"TG error: {e}")
        time.sleep(3)

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
        send_telegram(f"❌ <b>Error</b>\n{str(e)[:200]}")
        return jsonify({"error": str(e)}), 500

@app.route("/", methods=["GET"])
def home():
    return jsonify({"status": "Bitunix Bot Running 🚀", "active": BOT_ACTIVE})

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "active": BOT_ACTIVE})

# ─── MAIN ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("BITUNIX BOT STARTING ON RAILWAY 🚀")
    send_telegram(
        "🚀 <b>Bitunix Bot Started on Railway!</b>\n"
        f"SL: {SL_PERC*100}% | TP: {SL_PERC*RR*100:.2f}%\n"
        f"Leverage: {LEVERAGE}x | Risk: {RISK_PERC*100:.0f}%/trade\n"
        "Send /help for commands"
    )
    threading.Thread(target=handle_telegram_commands, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
  
