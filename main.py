"""
PolyBot - Railway Backend
Runs 24/7, places real orders, updates dashboard
"""

import os
import json
import time
import hmac
import hashlib
import base64
import logging
from datetime import datetime
from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
import httpx
from groq import Groq
from eth_account import Account
from eth_account.messages import encode_defunct

# ── Config ────────────────────────────────────────────────────────────────────
GROQ_KEY             = os.environ.get("GROQ_KEY")
PRIVATE_KEY          = os.environ.get("WALLET_PRIVATE_KEY")
POLY_API_KEY         = os.environ.get("POLY_API_KEY")
POLY_API_ADDRESS     = os.environ.get("POLY_API_ADDRESS")
USDC_BUDGET          = float(os.environ.get("USDC_BUDGET", 5.92))
BET_SIZE             = float(os.environ.get("BET_SIZE", 0.50))
CONFIDENCE_THRESHOLD = float(os.environ.get("CONFIDENCE_THRESHOLD", 0.80))

# ── Proxy Config ──────────────────────────────────────────────────────────────
PROXY_USER = os.environ.get("PROXY_USER", "azucytnw")
PROXY_PASS = os.environ.get("PROXY_PASS", "uzwv5plwkyop")
PROXY_HOST = os.environ.get("PROXY_HOST", "45.38.107.97")
PROXY_PORT = os.environ.get("PROXY_PORT", "6014")

PROXY_URL = f"http://{PROXY_USER}:{PROXY_PASS}@{PROXY_HOST}:{PROXY_PORT}"

PROXIES = {
    "http":  PROXY_URL,
    "https": PROXY_URL
}

# ── Patch httpx globally so py_clob_client uses the proxy ────────────────────
_original_httpx_client       = httpx.Client
_original_httpx_async_client = httpx.AsyncClient

class _PatchedHttpxClient(_original_httpx_client):
    def __init__(self, *args, **kwargs):
        if "transport" not in kwargs and "mounts" not in kwargs:
            kwargs["transport"] = httpx.HTTPTransport(proxy=PROXY_URL)
        super().__init__(*args, **kwargs)

class _PatchedHttpxAsyncClient(_original_httpx_async_client):
    def __init__(self, *args, **kwargs):
        if "transport" not in kwargs and "mounts" not in kwargs:
            kwargs["transport"] = httpx.AsyncHTTPTransport(proxy=PROXY_URL)
        super().__init__(*args, **kwargs)

httpx.Client      = _PatchedHttpxClient
httpx.AsyncClient = _PatchedHttpxAsyncClient

client_groq = Groq(api_key=GROQ_KEY)
app         = Flask(__name__)
CORS(app)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

CATEGORIES = {
    "Crypto":      ["bitcoin","btc","ethereum","eth","crypto","price","ath","solana","xrp"],
    "Elections":   ["election","president","vote","poll","senate","governor","primary","ballot"],
    "Geopolitics": ["war","ceasefire","sanctions","treaty","nato","un","conflict","military"],
    "AI/Tech":     ["openai","anthropic","gpt","gemini","release","launch","model","ai","microsoft"],
    "Sports":      ["world cup","champions league","nba","nfl","premier league","tournament","fifa"]
}

POLYMARKET_API = "https://gamma-api.polymarket.com"
CLOB_API       = "https://clob.polymarket.com"

trades     = []
bot_logs   = []
total_spent= 0.0
bot_status = "idle"

# ── Helpers ───────────────────────────────────────────────────────────────────
def add_log(msg, level="info"):
    entry = {"time": datetime.now().strftime("%H:%M:%S"), "msg": msg, "level": level}
    bot_logs.append(entry)
    if len(bot_logs) > 200:
        bot_logs.pop(0)
    log.info(msg)

def detect_category(question):
    q = question.lower()
    for cat, keywords in CATEGORIES.items():
        if any(kw in q for kw in keywords):
            return cat
    return "General"

def get_seen_ids():
    return set(t.get("market_id") for t in trades if t.get("market_id"))

# ── Polymarket Auth ───────────────────────────────────────────────────────────
def get_l1_headers():
    timestamp = str(int(time.time()))
    nonce     = "0"
    message   = f"polymarket\n\n{timestamp}\n{nonce}"
    msg       = encode_defunct(text=message)
    signed    = Account.sign_message(msg, private_key=PRIVATE_KEY)
    signature = signed.signature.hex()
    if not signature.startswith("0x"):
        signature = "0x" + signature
    return {
        "POLY_ADDRESS":   POLY_API_ADDRESS,
        "POLY_SIGNATURE": signature,
        "POLY_TIMESTAMP": timestamp,
        "POLY_NONCE":     nonce,
        "Content-Type":   "application/json"
    }

def derive_api_key():
    global POLY_API_KEY
    try:
        r = requests.get(
            f"{CLOB_API}/auth/derive-api-key",
            headers=get_l1_headers(),
            proxies=PROXIES,
            timeout=15
        )
        if r.status_code == 200:
            data = r.json()
            POLY_API_KEY = data.get("apiKey", POLY_API_KEY)
            add_log(f"✅ API key derived: {POLY_API_KEY[:8]}...", "success")
            return data
        else:
            add_log(f"Derive returned {r.status_code}: {r.text[:100]}", "error")
    except Exception as e:
        add_log(f"Derive failed: {e}", "error")
    return None

# ── Order placement ───────────────────────────────────────────────────────────
def place_order(market, outcome, amount_usdc):
    global total_spent
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.constants import POLYGON

        clob = ClobClient(
            host           = CLOB_API,
            chain_id       = POLYGON,
            key            = PRIVATE_KEY,
            signature_type = 0
        )

        try:
            creds = clob.derive_api_key()
            from py_clob_client.clob_types import ApiCreds
            clob.set_api_creds(ApiCreds(
                api_key        = creds.api_key,
                api_secret     = creds.api_secret,
                api_passphrase = creds.api_passphrase
            ))
        except Exception as e:
            add_log(f"Cred derive: {e}", "warn")

        condition_id = market.get("conditionId")
        if not condition_id:
            return False, None

        r = requests.get(
            f"{CLOB_API}/markets/{condition_id}",
            proxies=PROXIES,
            timeout=10
        )
        if r.status_code != 200:
            return False, None

        clob_market = r.json()
        tokens      = clob_market.get("tokens", [])
        token_id    = None
        price       = 0.5

        for token in tokens:
            if token.get("outcome","").lower() == outcome.lower():
                token_id = token.get("token_id")
                price    = float(token.get("price", 0.5))
                break

        if not token_id:
            add_log(f"No token for {outcome}", "error")
            return False, None

        size = round(amount_usdc / max(price, 0.01), 4)
        add_log(f"Placing: {outcome} | Price:{price} | Size:{size} | ${amount_usdc}", "info")

        order_args   = OrderArgs(token_id=token_id, price=round(price,4), size=size, side="BUY")
        signed_order = clob.create_order(order_args)
        result       = clob.post_order(signed_order, OrderType.FOK)

        add_log(f"Order result: {result}", "info")

        order_id = None
        success  = False
        if isinstance(result, dict):
            order_id = result.get("orderID") or result.get("id")
            success  = result.get("success", False) or bool(order_id)
        elif hasattr(result, 'orderID'):
            order_id = result.orderID
            success  = True

        if success:
            total_spent += amount_usdc
            add_log(f"✅ LIVE BET! {outcome} ${amount_usdc} | ID:{order_id}", "success")
            return True, str(order_id)
        else:
            add_log(f"❌ Order failed: {result}", "error")
            return False, None

    except Exception as e:
        add_log(f"Order error: {e}", "error")
        return False, None

# ── Bot logic ─────────────────────────────────────────────────────────────────
def fetch_markets():
    r = requests.get(
        f"{POLYMARKET_API}/markets",
        params={"closed":"false","limit":50,"order":"volume","ascending":"false"},
        timeout=15
    )
    r.raise_for_status()
    return r.json()

def filter_markets(markets):
    all_kw = [kw for kws in CATEGORIES.values() for kw in kws]
    return [m for m in markets if any(kw in m.get("question","").lower() for kw in all_kw)]

def analyze_market(market):
    question  = market.get("question","Unknown")
    outcomes  = market.get("outcomes","[]")
    end_date  = market.get("endDate","Unknown")
    volume    = market.get("volume",0)
    liquidity = market.get("liquidity",0)

    prompt = f"""You are an expert prediction market analyst.
Analyze this Polymarket market and give a confidence score.
Market Question: {question}
Outcomes: {outcomes}
End Date: {end_date}
Volume: ${volume}
Liquidity: ${liquidity}

Respond ONLY with raw JSON:
{{"question":"{question}","recommended_outcome":"Yes or No","confidence":0.85,"reasoning":"Brief reason","risk_level":"low","bet":true}}
Rules: confidence 0.0-1.0. bet=true only if confidence>=0.80 AND risk_level low or medium. No markdown."""

    response = client_groq.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role":"user","content":prompt}],
        temperature=0.3,
        max_tokens=300
    )
    text = response.choices[0].message.content.strip()
    text = text.replace("```json","").replace("```","").strip()
    import re
    m = re.search(r'\{[\s\S]*\}', text)
    if m: text = m.group(0)
    return json.loads(text)

def run_bot_cycle():
    global bot_status, total_spent
    bot_status = "running"
    cycle      = 0

    add_log("🤖 PolyBot started on Railway!", "success")
    add_log(f"Budget: ${USDC_BUDGET} | Bet: ${BET_SIZE} | Min conf: {CONFIDENCE_THRESHOLD*100:.0f}%", "info")
    add_log(f"🌐 Proxy: {PROXY_HOST}:{PROXY_PORT} (UK residential, httpx transport patched)", "info")

    derive_api_key()

    while True:
        cycle += 1
        add_log(f"=== Cycle #{cycle} | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===", "info")

        remaining = USDC_BUDGET - total_spent
        if remaining < BET_SIZE:
            add_log(f"Budget exhausted (${total_spent:.2f} spent)", "warn")
            bot_status = "stopped"
            break

        try:
            markets     = fetch_markets()
            relevant    = filter_markets(markets)
            seen_ids    = get_seen_ids()
            new_markets = [m for m in relevant if str(m.get("id")) not in seen_ids]
            add_log(f"Markets: {len(markets)} total → {len(relevant)} relevant → {len(new_markets)} new", "info")

            signals = 0
            for market in new_markets[:10]:
                if (USDC_BUDGET - total_spent) < BET_SIZE:
                    break
                try:
                    analysis   = analyze_market(market)
                    confidence = analysis.get("confidence", 0)
                    should_bet = analysis.get("bet", False)
                    outcome    = analysis.get("recommended_outcome","Yes")

                    add_log(f"  {market.get('question','')[:55]}... | {confidence*100:.0f}% | Bet:{should_bet}", "info")

                    if should_bet and confidence >= CONFIDENCE_THRESHOLD:
                        order_placed, order_id = place_order(market, outcome, BET_SIZE)
                        trade = {
                            "timestamp":       datetime.now().isoformat(),
                            "question":        analysis.get("question", market.get("question","")),
                            "outcome":         outcome,
                            "confidence":      int(round(confidence * 100)),
                            "reasoning":       analysis.get("reasoning",""),
                            "risk":            analysis.get("risk_level","medium"),
                            "bet_amount_usdc": BET_SIZE,
                            "market_id":       str(market.get("id")),
                            "category":        detect_category(market.get("question","")),
                            "status":          "pending",
                            "live":            order_placed,
                            "order_id":        order_id
                        }
                        trades.append(trade)
                        signals += 1

                    time.sleep(2)
                except Exception as e:
                    add_log(f"  Error: {e}", "error")

            add_log(f"Cycle #{cycle} done | {signals} signals | ${total_spent:.2f} spent", "success")

        except Exception as e:
            add_log(f"Cycle error: {e}", "error")

        add_log("Sleeping 15 minutes...", "info")
        time.sleep(900)

# ── API Routes ────────────────────────────────────────────────────────────────
@app.route('/')
def home():
    return jsonify({"status": "PolyBot Railway API running", "version": "1.0"})

@app.route('/trades')
def get_trades():
    return jsonify(trades)

@app.route('/logs')
def get_logs():
    return jsonify(bot_logs[-50:])

@app.route('/stats')
def get_stats():
    total   = len(trades)
    wins    = sum(1 for t in trades if t.get("status")=="win")
    losses  = sum(1 for t in trades if t.get("status")=="loss")
    pending = sum(1 for t in trades if t.get("status")=="pending")
    staked  = sum(t.get("bet_amount_usdc",0) for t in trades)
    avg_conf= round(sum(t.get("confidence",0) for t in trades)/max(total,1))
    return jsonify({
        "total": total, "wins": wins, "losses": losses,
        "pending": pending, "staked": staked, "avg_confidence": avg_conf,
        "total_spent": total_spent, "status": bot_status
    })

@app.route('/update_trade', methods=['POST'])
def update_trade():
    data     = request.json
    market_id= data.get("market_id")
    status   = data.get("status")
    for t in trades:
        if t.get("market_id") == market_id:
            t["status"] = status
            return jsonify({"success": True})
    return jsonify({"success": False})

# ── Start ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import threading
    bot_thread = threading.Thread(target=run_bot_cycle, daemon=True)
    bot_thread.start()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
