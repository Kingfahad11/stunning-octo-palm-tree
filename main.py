import os
import re
import io
import ast
import sys
import json
import time
import html
import shutil
import psutil
import zipfile
import threading
import subprocess
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
import telebot
from telebot import types

import admin

# ======================== টেলিগ্রাম বট ইনিশিয়ালাইজেশন ========================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8907405779:AAHkNLRVZGwdA1X2cjknvitb0kK7ueSURo4")
# সুপারফাস্ট মাল্টি-থ্রেডিং ও নো-ল্যাগ হ্যান্ডলিং (15 Workers)
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", num_threads=15)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECTS_DIR = os.path.join(BASE_DIR, "user_projects")
os.makedirs(PROJECTS_DIR, exist_ok=True)

# ======================== 📡 Database Channel ========================
# ইউজার যে কোড/ফাইল আপলোড করে রান করে তার কপি আর অ্যাডমিনের বট-চ্যাটে যাবে না —
# সরাসরি এই চ্যানেলে জমা হবে। (বটকে অবশ্যই চ্যানেলের Admin বানাতে হবে — "Post Messages" পারমিশনসহ)
# চাইলে Render/হোস্টের ENV-এ DB_CHANNEL_ID দিয়ে চ্যানেল বদলানো যাবে।
DB_CHANNEL_ID = int(os.environ.get("DB_CHANNEL_ID", "-1002768250515"))

def send_code_copy_to_channel(data: bytes, file_name: str, caption: str) -> bool:
    """ইউজারের কোড/ফাইলের কপি Database চ্যানেলে পাঠায়। সফল হলে True।
    রেট-লিমিট (429) এলে একবার অপেক্ষা করে আবার চেষ্টা করে; ব্যর্থ হলে কনসোলে কারণ প্রিন্ট করে
    (ইউজারের চ্যাটে কিছুই দেখানো হয় না, এবং অ্যাডমিনের DM-এ ফলব্যাক পাঠানো হয় না)।"""
    for attempt in range(2):
        try:
            bio = io.BytesIO(data)
            bio.name = file_name
            bot.send_document(DB_CHANNEL_ID, bio, caption=caption, parse_mode="HTML")
            return True
        except Exception as e:
            err = str(e)
            if attempt == 0 and ("429" in err or "Too Many Requests" in err):
                m = re.search(r"retry after (\d+)", err)
                time.sleep(min(int(m.group(1)) if m else 3, 30) + 1)
                continue
            print(f"[DB-CHANNEL] কপি পাঠানো যায়নি ({DB_CHANNEL_ID}): {err[:200]}")
            return False
    return False

def free_hosting_blocked_message() -> str:
    """🆓 ফ্রি হোস্টিং অ্যাডমিন থেকে বন্ধ থাকলে ফ্রি-প্ল্যান ইউজারদের এই মেসেজ দেখানো হয়।
    পেইড প্ল্যান/24-7-কভার্ড স্লট এতে প্রভাবিত হয় না।"""
    hours = admin.SYSTEM_CONFIG.get("free_hosting_hours", 12)
    return (
        f"🔒 <b>ফ্রি হোস্টিং ({hours} ঘণ্টা) সাময়িকভাবে বন্ধ আছে!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "সার্ভার মেইনটেনেন্স/লোডের কারণে অ্যাডমিন ফ্রি সেশন সাময়িকভাবে বন্ধ রেখেছেন।\n\n"
        "👉 এখনই বট চালাতে <b>💎 Upgrade Plan</b> থেকে যেকোনো প্ল্যান নিন (24/7 আনলিমিটেড), "
        "অথবা কিছুক্ষণ পর আবার চেষ্টা করুন।"
    )

def queue_user_code_copy(from_user, project_id: int, file_name: str, data: bytes, source: str = "File Upload (.py)"):
    """ব্যাকগ্রাউন্ড থ্রেডে ইউজারের কোডের কপি + একটা বিস্তারিত অডিট কার্ড Database চ্যানেলে পাঠায়
    (ডেপ্লয়মেন্ট ধীর করে না)। বট টোকেন কোডে থাকলে অটো-ডিটেক্ট ও মাস্ক করে দেখায়, আর হালকা
    হিউরিস্টিক সিকিউরিটি স্ক্যানও চালায়।"""
    user_id = from_user.id
    username = getattr(from_user, "username", None)
    full_name = (getattr(from_user, "first_name", "") or "").strip()
    if getattr(from_user, "last_name", None):
        full_name = (full_name + " " + from_user.last_name).strip()
    full_name = full_name or "Unknown"

    def _worker():
        try:
            proj = admin.get_project_by_id(project_id)
            proj_name = proj["project_name"] if proj else f"Bot #{project_id}"
            slot_no = proj.get("slot_number") if proj else None
            slot_label = f"Slot #{slot_no}" if slot_no else "Unassigned"

            u_data = admin.get_user(user_id)
            plan = u_data.get("plan", "FREE").upper()
            if plan == "FREE":
                plan_status = f"{admin.SYSTEM_CONFIG.get('free_hosting_hours', 12)}h/Session"
            else:
                plan_status = "24/7"

            token = extract_bot_token_from_code(data)
            if token:
                token_line = f"<code>{html.escape(token)}</code>"
                is_valid, bot_uname_or_err = validate_bot_token(token)
                bot_user_line = f"{html.escape(bot_uname_or_err)} [🟢 Live]" if is_valid else "⚠️ Invalid / Offline"
            else:
                token_line = "❌ Not Found in Code"
                bot_user_line = "❔ Unknown"

            packages = _extract_packages_from_code(data.decode("utf-8", errors="ignore")) if file_name.endswith(".py") else []
            packages_line = ", ".join(packages) if packages else "None"
            security_line = scan_code_security(data)
            size_kb = len(data) / 1024
            time_str = datetime.now().strftime("%d-%b-%Y • %I:%M %p")

            caption = (
                "╭━━━[ 📥 𝗨𝗦𝗘𝗥 𝗖𝗢𝗗𝗘 𝗔𝗨𝗗𝗜𝗧 ]━━━╮\n"
                f"┃ 🚀 Source: {html.escape(source)}\n"
                "┣━━━[ 👤 𝗖𝗟𝗜𝗘𝗡𝗧 𝗢𝗩𝗘𝗥𝗩𝗜𝗘𝗪 ]━━━┫\n"
                f"┃ 🏷️ Name: {html.escape(full_name)}\n"
                f"┃ 🆔 ID: <code>{user_id}</code>\n"
                f"┃ 👤 User: @{html.escape(username or 'N/A')}\n"
                f"┃ 👑 Plan: {plan} ({plan_status})\n"
                "┣━━━[ 🤖 𝗕𝗢𝗧 & 𝗧𝗢𝗞𝗘𝗡 𝗜𝗡𝗙𝗢 ]━━━┫\n"
                f"┃ 📦 Project: {html.escape(proj_name)} ({slot_label})\n"
                f"┃ 🏷️ Bot User: {bot_user_line}\n"
                f"┃ 🔑 Token: {token_line}\n"
                "┣━━━[ 📄 𝗙𝗜𝗟𝗘 & 𝗦𝗘𝗖𝗨𝗥𝗜𝗧𝗬 ]━━━┫\n"
                f"┃ 📁 File: <code>{html.escape(file_name)}</code> ({size_kb:.1f} KB)\n"
                f"┃ 📦 Packages: {html.escape(packages_line)}\n"
                f"┃ 🛡️ Security: {security_line}\n"
                f"┃ 🕒 Time: {time_str} BST\n"
                "╰━━━━━━━━━━━━━━━━━━━━━━━╯"
            )
            send_code_copy_to_channel(data, file_name, caption)
        except Exception as e:
            print(f"[DB-CHANNEL] worker error: {str(e)[:200]}")
    threading.Thread(target=_worker, daemon=True).start()

# প্রসেস ও মেমোরি স্টেট
active_processes = {}
user_states = {}
installed_packages_cache = set()
upload_batches = {}   # user_id -> মাল্টি-ফাইল আপলোড ব্যাচ (একাধিক ফাইল একটাই কার্ডে দেখানোর জন্য)
upload_batch_lock = threading.RLock()
upload_target_hint = {}   # user_id -> {"pid", "ts"}: একসাথে/পরপর আসা ফাইলগুলো যেন একই বটে যায়
UPLOAD_EXPLICIT_TTL = 1800   # স্লট/Update Code চাপার পর ৩০ মিনিট পর্যন্ত সেই বটই টার্গেট থাকবে
UPLOAD_HINT_TTL = 45         # পরপর আসা ফাইলের মধ্যে সর্বোচ্চ গ্যাপ (সেকেন্ড)
UPLOAD_BATCH_WINDOW = 2   # সেকেন্ড — এই সময়ের মধ্যে নতুন ফাইল না এলে অটো ডিপ্লয় শুরু হবে
# ⭐ NEW: "🔴 Live Mode" লগ ভিউয়ের জন্য একটিভ সেশন ট্র্যাক করে (chat_id_message_id -> {"stop": bool})
live_log_sessions = {}

# ----------------- Rate Limiter (স্প্যাম প্রতিরোধক) -----------------
# প্রতি ইউজারের শেষ মেসেজের সময় ট্র্যাক করে — MAX_MSG_PER_SEC সেকেন্ডে ১টার বেশি
# মেসেজ এলে সাইলেন্টলি ইগনোর করা হয় (ইউজারকে কোনো এরর দেখানো হয় না)
_rate_limit_store: dict = {}   # {user_id: last_message_timestamp}
MAX_MSG_INTERVAL_SEC = 1.2     # ইউজার প্রতি মিনিমাম গ্যাপ (সেকেন্ড)

def check_rate_limit(user_id: int) -> bool:
    """True হলে মেসেজটি প্রসেস করা যাবে, False হলে রেটলিমিটেড — বাদ দেওয়া হবে।"""
    now = time.monotonic()
    last = _rate_limit_store.get(user_id, 0)
    if now - last < MAX_MSG_INTERVAL_SEC:
        return False
    _rate_limit_store[user_id] = now
    return True

# ----------------- বট টোকেন ভ্যালিডেটর -----------------
def validate_bot_token(token: str, timeout: int = 10) -> tuple:
    """Telegram getMe API-তে কল করে টোকেনটি সত্যিই কাজ করে কিনা যাচাই করে।
    রিটার্ন করে (is_valid: bool, bot_username: str | error_msg: str)।
    ডিপ্লয়ের আগে এটা করা হয় যাতে ভুল টোকেন দিয়ে অকারণে স্লট নষ্ট না হয়।
    timeout ছোট রাখলে (যেমন ডিপ্লয় সাকসেস কার্ডে বট হ্যান্ডল দেখানোর সময়) ধীর নেটওয়ার্কে
    ইউজারকে বেশিক্ষণ অপেক্ষা করাতে হয় না।"""
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/getMe",
            headers={"User-Agent": "PyHostCloud"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("ok"):
            uname = data["result"].get("username", "unknown")
            return True, f"@{uname}"
        return False, "Telegram API ভুল ফলাফল দিয়েছে।"
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, "টোকেনটি ভুল বা বাতিল হয়ে গেছে (401 Unauthorized)।"
        return False, f"HTTP {e.code} এরর।"
    except Exception as ex:
        return False, f"নেটওয়ার্ক সমস্যা: {str(ex)[:100]}"


PIP_ALIASES = {
    "telebot": "pyTelegramBotAPI",
    "bs4": "beautifulsoup4",
    "PIL": "Pillow",
    "cv2": "opencv-python",
    "dotenv": "python-dotenv",
    "telegram": "python-telegram-bot",
    "yaml": "PyYAML",
    "sklearn": "scikit-learn",
    "discord": "discord.py",
    "dateutil": "python-dateutil",
    "jose": "python-jose",
    "jwt": "PyJWT",
    "dns": "dnspython",
    "magic": "python-magic",
    "nmap": "python-nmap"
}

# ----------------- Render Health Web Server & Self Keep-Alive -----------------
class RenderHealthHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"PyHost Cloud 24/7 Multi-Bot Hosting Engine is Live & Healthy!")

    def log_message(self, format, *args):
        return

def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    try:
        server = HTTPServer(("0.0.0.0", port), RenderHealthHandler)
        server.serve_forever()
    except Exception:
        pass

threading.Thread(target=start_health_server, daemon=True).start()

# সেলফ-পিং কিপ অ্যালাইভ (সার্ভার স্লিপ মোড প্রতিরোধক)
def self_keep_alive_pinger():
    port = int(os.environ.get("PORT", 10000))
    url = f"http://127.0.0.1:{port}/"
    while True:
        time.sleep(240)
        try:
            urllib.request.urlopen(url, timeout=10)
        except Exception:
            pass

threading.Thread(target=self_keep_alive_pinger, daemon=True).start()

# ----------------- ফাস্ট ডিপেন্ডেন্সি স্ক্যানার ও সিঙ্ক ইনস্টলার -----------------
def _extract_packages_from_code(code: str) -> list:
    """কোড টেক্সট থেকে ইম্পোর্ট করা থার্ড-পার্টি প্যাকেজ বের করে (AST দিয়ে, ব্যর্থ হলে regex fallback)।
    scan_python_imports (ফাইল-ভিত্তিক) ও queue_user_code_copy (মেমোরি-ভিত্তিক) দুটোই এটা শেয়ার করে।"""
    detected = set()
    try:
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    detected.add(alias.name.split('.')[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    detected.add(node.module.split('.')[0])
    except Exception:
        raw_imports = re.findall(r"^(?:from|import)\s+([a-zA-Z0-9_]+)", code, re.MULTILINE)
        detected.update(raw_imports)

    stdlib = {
        'os', 'sys', 'time', 'json', 'sqlite3', 'math', 'random', 're', 'asyncio',
        'threading', 'subprocess', 'datetime', 'urllib', 'shutil', 'logging',
        'pathlib', 'itertools', 'functools', 'collections', 'hashlib', 'base64',
        'traceback', 'typing', 'tempfile', 'socket', 'signal', 'io', 'http'
    }

    packages = []
    for pkg in detected - stdlib:
        resolved = PIP_ALIASES.get(pkg, pkg)
        packages.append(resolved)

    return sorted(list(set(packages)))

def scan_python_imports(file_path):
    if not os.path.exists(file_path):
        return []
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        code = f.read()
    return _extract_packages_from_code(code)

def extract_bot_token_from_code(data: bytes):
    """আপলোড করা কোডের ভেতর থেকে বট টোকেন প্যাটার্ন (যদি সরাসরি কোডে লেখা থাকে) খুঁজে বের করে।"""
    try:
        text = data.decode("utf-8", errors="ignore")
    except Exception:
        return None
    m = re.search(r'\d{8,10}:[a-zA-Z0-9_\-]{35}', text)
    return m.group(0) if m else None

def mask_bot_token(token: str) -> str:
    """চ্যানেলে সম্পূর্ণ টোকেন না দেখিয়ে মাস্ক করে দেখায় (নিরাপত্তার জন্য)।"""
    if ":" not in token:
        return (token[:6] + "...") if len(token) > 6 else token
    bot_id, secret = token.split(":", 1)
    if len(secret) <= 6:
        return f"{bot_id}:{secret}"
    return f"{bot_id}:{secret[:3]}...{secret[-3:]}"

def scan_code_security(data: bytes) -> str:
    """হালকা হিউরিস্টিক স্ক্যান — সত্যিকারের ম্যালওয়্যার ডিটেকশন নয়, শুধু কিছু সন্দেহজনক প্যাটার্ন
    থাকলে অ্যাডমিনকে flag করে দেখায় যাতে ম্যানুয়ালি রিভিউ করা সহজ হয়।"""
    try:
        text = data.decode("utf-8", errors="ignore")
    except Exception:
        return "⚠️ Unreadable"
    suspicious_patterns = [
        r'os\.system\s*\(',
        r'subprocess\.(Popen|call|run)\s*\([^)]{0,120}shell\s*=\s*True',
        r'\beval\s*\(',
        r'\bexec\s*\(',
        r'rm\s+-rf\s+/',
        r'base64\.b64decode\s*\(',
    ]
    flagged = sum(1 for p in suspicious_patterns if re.search(p, text))
    if flagged:
        return f"⚠️ Flagged ({flagged} suspicious pattern{'s' if flagged > 1 else ''})"
    return "✅ Clean (No Threat)"

def sanitize_upload_filename(file_name: str) -> str:
    """
    টেলিগ্রাম থেকে আসা ফাইলের নাম স্যানিটাইজ করে — শুধু বেসনেইম রাখা হয় যাতে কেউ
    ফাইলের নামে "../../" জাতীয় কিছু ঢুকিয়ে প্রজেক্ট ফোল্ডারের বাইরে লিখতে না পারে
    (Path Traversal প্রতিরোধ)। ".env"-এর মতো ডটফাইল নাম ঠিক রেখেই এটা করা হয়।
    """
    raw = (file_name or "").replace("\\", "/")
    safe = os.path.basename(raw).strip()
    if not safe or safe in (".", ".."):
        safe = f"upload_{int(time.time())}.dat"
    return safe

def safe_extract_zip(zip_path: str, dest_dir: str):
    """
    ZIP ফাইল নিরাপদে এক্সট্রাক্ট করে — প্রতিটি এন্ট্রির চূড়ান্ত (resolved) পাথ dest_dir-এর
    ভেতরেই থাকছে কিনা যাচাই করা হয় (Zip Slip / Path Traversal অ্যাটাক ঠেকাতে; যেমন কোনো
    এন্ট্রির নাম "../../../something" হলে সেটা প্রজেক্ট ফোল্ডারের বাইরে গিয়ে ফাইল লিখে
    ফেলতে পারত)। সন্দেহজনক এন্ট্রি পেলে সেটা স্কিপ করে দেওয়া হয়, পুরো এক্সট্রাকশন আটকায় না।
    রিটার্ন করে স্কিপ করা এন্ট্রিগুলোর নামের লিস্ট।
    """
    dest_abs = os.path.abspath(dest_dir)
    skipped = []
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        infos = zip_ref.infolist()
        # ZIP-bomb সুরক্ষা: এক্সট্রাক্ট করলে মোট সাইজ/ফাইল সংখ্যা সীমা ছাড়ালে কিছুই এক্সট্রাক্ট করা হয় না
        if len(infos) > 3000 or sum(i.file_size for i in infos) > 200 * 1024 * 1024:
            return ["ZIP অনেক বড় (সর্বোচ্চ ২০০MB / ৩০০০ ফাইল)"]
        for member in infos:
            member_path = os.path.abspath(os.path.join(dest_abs, member.filename))
            if member_path != dest_abs and not member_path.startswith(dest_abs + os.sep):
                skipped.append(member.filename)
                continue
            try:
                zip_ref.extract(member, dest_abs)
            except Exception:
                skipped.append(member.filename)
    return skipped

def install_missing_packages_sync(pkg_list):
    """সব প্যাকেজ এক pip কমান্ডে একসাথে ইনস্টল করে (প্রতিটির জন্য আলাদা pip প্রসেস চালু
    করলে যে বাড়তি সময় লাগে সেটা এড়াতে) — ডিপ্লয় অনেক দ্রুত হয়।"""
    needed = [pkg for pkg in pkg_list if pkg not in installed_packages_cache]
    if not needed:
        return True, []

    try:
        res = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input", *needed],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90
        )
        if res.returncode == 0:
            for pkg in needed:
                installed_packages_cache.add(pkg)
            return True, needed
    except Exception:
        pass

    # ব্যাচ ইনস্টল ব্যর্থ হলে (যেমন একটা প্যাকেজের নাম ভুল) একটা একটা করে ফলব্যাক
    installed = []
    for pkg in needed:
        try:
            res = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input", pkg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=45)
            if res.returncode == 0:
                installed_packages_cache.add(pkg)
                installed.append(pkg)
        except Exception:
            pass
    return True, installed

# ----------------- অপ্টিমাইজড প্রসেস এক্সিকিউশন ও কাউন্টার ফিক্স -----------------
HOST_SECRET_ENV_KEYS = {"BOT_TOKEN", "DB_CHANNEL_ID", "ADMIN_ID", "ADMIN_IDS", "OWNER_ID"}

def _user_script_env() -> dict:
    """ইউজারের বট প্রসেসকে হোস্ট-বটের গোপন এনভায়রনমেন্ট ভ্যারিয়েবল (BOT_TOKEN ইত্যাদি) ছাড়া চালানো হয়,
    যাতে কেউ os.environ পড়ে হোস্ট বটের টোকেন নিয়ে যেতে না পারে।"""
    return {k: v for k, v in os.environ.items() if k.upper() not in HOST_SECRET_ENV_KEYS}

def pip_install_requirements(req_file: str, timeout: int = 240) -> bool:
    """requirements.txt ইনস্টল — টাইমআউটসহ, যাতে একটা আটকে যাওয়া pip পুরো ডিপ্লয় ঝুলিয়ে না রাখে।"""
    try:
        res = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", req_file, "--disable-pip-version-check", "--no-input"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)
        return res.returncode == 0
    except Exception:
        return False

def execute_project_script(user_id: int, project_id: int, file_name: str = None, preserve_restarts: bool = False, bypass_free_gate: bool = False):
    project = admin.get_project_by_id(project_id)
    if not project:
        return False, "Project not found!"

    # 🆓 ফ্রি হোস্টিং টগল — অ্যাডমিন বন্ধ রাখলে নতুন ফ্রি সেশন (Start/Restart/নতুন ডিপ্লয়) আটকানো হবে।
    # bypass_free_gate=True শুধু ইতিমধ্যে চলতে থাকা বটের ক্র্যাশ-রিকভারি অটো-রিস্টার্টে ব্যবহৃত হয়,
    # যাতে টগল অফ করলে চলমান ফ্রি বটগুলো মাঝপথে বিপর্যস্ত না হয়ে যায়।
    if not bypass_free_gate and not admin.SYSTEM_CONFIG.get("free_hosting_enabled", True):
        u_info_gate = admin.get_user(user_id)
        slot_status_gate = admin.get_effective_slot_status(u_info_gate, project)
        if not slot_status_gate["is_24_7"]:
            return False, free_hosting_blocked_message()

    p_key = f"{user_id}_{project_id}"
    p_dir = os.path.join(PROJECTS_DIR, f"{user_id}_{project_id}")
    os.makedirs(p_dir, exist_ok=True)

    if file_name:
        admin.update_project_main_file(project_id, file_name)
    else:
        file_name = project["main_file"]

    script_path = os.path.join(p_dir, file_name)
    log_path = os.path.join(p_dir, "app.log")

    if not os.path.exists(script_path):
        return False, f"<code>{file_name}</code> ফাইলটি পাওয়া যায়নি!"

    # বট রানের আগে DB থেকে env vars .env ফাইলে লেখা হচ্ছে
    try:
        admin.write_env_file(user_id, project_id)
    except Exception:
        pass

    old_restarts = 0
    if p_key in active_processes:
        try:
            if preserve_restarts:
                old_restarts = active_processes[p_key].get("restarts", 0)
            active_processes[p_key]["process"].terminate()
            if "log_handle" in active_processes[p_key]:
                active_processes[p_key]["log_handle"].close()
        except Exception:
            pass

    log_file = open(log_path, "a", encoding="utf-8")
    log_file.write(f"\n--- [Bot Started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ---\n")
    log_file.flush()

    proc = subprocess.Popen(
        [sys.executable, "-u", script_path],
        cwd=p_dir,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        env=_user_script_env()
    )

    active_processes[p_key] = {
        "process": proc,
        "start_time": time.time(),
        "file": file_name,
        "log_path": log_path,
        "log_handle": log_file,
        "user_id": user_id,
        "project_id": project_id,
        "project_name": project["project_name"],
        "restarts": old_restarts,
        "warning_sent": False
    }

    admin.update_project_status(project_id, "RUNNING")
    admin.increment_deploy_count(user_id)

    # প্রথম বট চালু করার অ্যাচিভমেন্ট ব্যাজ
    try:
        u_check = admin.get_user(user_id)
        if (u_check.get("total_deploys") or 0) <= 1:
            if admin.award_badge(user_id, "FIRST_BOT"):
                b = admin.ACHIEVEMENTS["FIRST_BOT"]
                bot.send_message(
                    user_id,
                    f"{b['emoji']} <b>নতুন ব্যাজ অর্জিত: {b['title']}!</b>\n{b['desc']} 🎉",
                    parse_mode="HTML"
                )
    except Exception:
        pass

    return True, "Success"

def stop_project_script(user_id: int, project_id: int):
    p_key = f"{user_id}_{project_id}"
    if p_key in active_processes:
        try:
            active_processes[p_key]["process"].terminate()
            active_processes[p_key]["log_handle"].close()
        except Exception:
            pass
        del active_processes[p_key]
    admin.update_project_status(project_id, "STOPPED")
    return True

def restart_all_active_bots():
    count = 0
    for p_key, data in list(active_processes.items()):
        uid = data["user_id"]
        pid = data["project_id"]
        fn = data["file"]
        execute_project_script(uid, pid, fn, preserve_restarts=False, bypass_free_gate=True)
        count += 1
    return count

def is_slot_locked(u_data: dict, project: dict) -> bool:
    """অ্যাডমিন ফ্রি হোস্টিং বন্ধ রাখলে, যেসব স্লট প্ল্যান/এক্সটেনশন দিয়ে 24/7 কভার্ড নয়
    (অর্থাৎ সত্যিকারের ফ্রি স্লট), সেগুলো এই ফাংশন অনুযায়ী "লকড" ধরা হবে — Start/Stop/Restart/
    Upload/Rename/GitHub Sync ইত্যাদি সব অ্যাকশন বন্ধ থাকবে, শুধু Upgrade বাটন দেখাবে।"""
    if admin.SYSTEM_CONFIG.get("free_hosting_enabled", True):
        return False
    slot_status = admin.get_effective_slot_status(u_data, project)
    return not slot_status["is_24_7"]

def lock_all_free_bots() -> int:
    """অ্যাডমিন ফ্রি হোস্টিং OFF করলে এই মুহূর্তে চলতে থাকা সব ফ্রি (non-24/7) বট বন্ধ করে দেয়,
    যাতে টগল অফ করার সাথে সাথেই সব ফ্রি স্লট সত্যিকারভাবে লক হয়ে যায়। পেইড/24-7-কভার্ড বট
    এতে প্রভাবিত হয় না। রিটার্ন করে কতটি বট বন্ধ হলো।"""
    stopped = 0
    for p_key, data in list(active_processes.items()):
        uid = data["user_id"]
        pid = data["project_id"]
        project = admin.get_project_by_id(pid)
        if not project:
            continue
        u_data = admin.get_user(uid)
        slot_status = admin.get_effective_slot_status(u_data, project)
        if not slot_status["is_24_7"]:
            stop_project_script(uid, pid)
            stopped += 1
            try:
                bot.send_message(
                    uid,
                    f"🔒 <b>ফ্রি হোস্টিং সাময়িকভাবে বন্ধ করা হয়েছে!</b>\n"
                    f"🤖 <b>বট:</b> <code>{data['project_name']}</code>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    "আপনার বটটি সাময়িকভাবে বন্ধ রাখা হয়েছে। অ্যাডমিন আবার চালু করলে অথবা "
                    "💎 <b>Upgrade Plan</b> থেকে যেকোনো প্ল্যান নিলে আপনি আবার চালাতে পারবেন।",
                    parse_mode="HTML"
                )
            except Exception:
                pass
    return stopped

# ----------------- স্মার্ট ওয়াচডগ ও অটোমেটিক এক্সপায়ারি অ্যালার্ট -----------------

def _award_first_upgrade_badge(uid: int):
    try:
        if admin.award_badge(uid, "FIRST_UPGRADE"):
            b = admin.ACHIEVEMENTS["FIRST_UPGRADE"]
            bot.send_message(uid, f"{b['emoji']} <b>নতুন ব্যাজ অর্জিত: {b['title']}!</b>\n{b['desc']} 🎉", parse_mode="HTML")
    except Exception:
        pass

def hosting_watchdog_thread():
    while True:
        time.sleep(5)
        now = time.time()
        
        # ১. লাইভ প্রসেস মনিটরিং ও ক্র্যাশ কন্ট্রোল
        for p_key in list(active_processes.keys()):
            data = active_processes.get(p_key)
            if not data:
                continue

            uid = data["user_id"]
            pid = data["project_id"]
            proc = data["process"]
            start_t = data["start_time"]
            u_info = admin.get_user(uid)
            plan = u_info.get("plan", "FREE").upper()

            # স্লটের প্রকৃত (effective) স্ট্যাটাস — অ্যাকাউন্ট প্ল্যান + স্লটের নিজস্ব Extend
            # Validity ক্রয় উভয়ই একসাথে বিবেচনা করা হয়, যাতে একটা একটিভ প্ল্যান থাকা সত্ত্বেও
            # স্লট ভুলবশত বন্ধ না হয়ে যায়, বা স্লট আলাদাভাবে কেনা থাকলে সেটা কাজেই না লাগে।
            proj_info = admin.get_project_by_id(pid)
            slot_status = admin.get_effective_slot_status(u_info, proj_info) if proj_info else None

            # কোনো কভারেজই (প্ল্যান বা স্লট) না থাকলে এবং আগে কেনা মেয়াদ সত্যিই শেষ হয়ে গেলে বট বন্ধ
            if slot_status and slot_status["is_expired"]:
                try:
                    proc.terminate()
                    data["log_handle"].close()
                except Exception:
                    pass
                if p_key in active_processes:
                    del active_processes[p_key]
                admin.update_project_status(pid, "STOPPED")
                try:
                    bot.send_message(
                        uid,
                        f"⏳ <b>বট স্লটের মেয়াদ শেষ হয়েছে!</b>\n"
                        f"🤖 <b>বট:</b> <code>{data['project_name']}</code>\n"
                        "━━━━━━━━━━━━━━━━━━━━━━\n"
                        "👉 বট চালু রাখতে <b>⏳ Extend Validity</b> থেকে মেয়াদ বাড়িয়ে নিন।",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
                continue

            # যদি বট ১ মিনিটের বেশি স্বাভাবিক চলে, রিস্টার্ট কাউন্টার রিসেট হবে
            if proc.poll() is None and (now - start_t) > 60 and data.get("restarts", 0) > 0:
                data["restarts"] = 0

            # ১২ ঘণ্টার ফ্রি-সেশন ক্যাপ শুধুমাত্র সেই স্লটেই প্রযোজ্য যেটা না অ্যাকাউন্ট প্ল্যান
            # দ্বারা, না নিজস্ব Extend Validity ক্রয় দ্বারা কভার্ড — অর্থাৎ, সত্যিকারের ফ্রি স্লট।
            # (আগে এই চেক শুধু plan == "FREE" দেখেই চলত, ফলে কেউ শুধু এই স্লটের জন্য Extend
            # Validity কিনলেও অ্যাকাউন্ট প্ল্যান FREE থাকলে বটটি তবুও ১২ ঘণ্টা পর বন্ধ হয়ে যেত।)
            is_covered_24_7 = bool(slot_status and slot_status["is_24_7"])
            if not is_covered_24_7:
                elapsed = now - start_t
                if elapsed >= admin.get_free_warning_seconds() and not data.get("warning_sent", False):
                    data["warning_sent"] = True
                    try:
                        bot.send_message(
                            uid,
                            f"⏳ <b>হোস্টিং সেশন অ্যালার্ট (৩০ মিনিট বাকি)!</b> ⚠️\n"
                            "━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"🤖 <b>বটের নাম:</b> <code>{data['project_name']}</code>\n"
                            f"⏱️ আপনার ১২ ঘণ্টার ফ্রি সেশনের মেয়াদ আর মাত্র <b>৩০ মিনিট</b> পর শেষ হবে।\n\n"
                            "💡 <b>বট নিরবচ্ছিন্ন সচল রাখতে:</b>\n"
                            "• মেয়াদ শেষ হলে <b>🎛️ Control Panel</b> থেকে রিস্টার্ট করুন (ফ্রি)\n"
                            "• অথবা ২৪/৭ নন-স্টপ হোস্টিং পেতে <b>💎 Upgrade Plan</b> থেকে আপগ্রেড করুন!\n"
                            "━━━━━━━━━━━━━━━━━━━━━━",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass

                # ১২ ঘণ্টা পূর্ণ হলে ফ্রি সেশন বন্ধ
                if elapsed >= admin.get_free_session_seconds():
                    try:
                        proc.terminate()
                        data["log_handle"].close()
                    except Exception:
                        pass
                    del active_processes[p_key]
                    admin.update_project_status(pid, "STOPPED")
                    try:
                        bot.send_message(
                            uid,
                            f"⏳ <b>১২ ঘণ্টার ফ্রি হোস্টিং সেশন পূর্ণ হয়েছে!</b>\n"
                            f"🤖 <b>বট:</b> <code>{data['project_name']}</code>\n"
                            "━━━━━━━━━━━━━━━━━━━━━━\n"
                            "👉 পুনরায় চালু করতে <b>🎛️ Control Panel</b> থেকে Start চাপুন অথবা ২৪/৭ আনলিমিটেড চালাতে <b>💎 Upgrade Plan</b> থেকে আপগ্রেড করুন।",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
                    continue

            # ক্র্যাশ ডিটেকশন ও স্মার্ট রিস্টার্ট হ্যান্ডলার
            if proc.poll() is not None:
                current_restarts = data.get("restarts", 0) + 1
                data["restarts"] = current_restarts

                if current_restarts <= 3:
                    time.sleep(1)
                    execute_project_script(uid, pid, data["file"], preserve_restarts=True, bypass_free_gate=True)
                    try:
                        bot.send_message(
                            uid,
                            f"⚠️ <b>বট ক্র্যাশ করেছে:</b> <code>{data['project_name']}</code> স্বয়ংক্রিয়ভাবে রিস্টার্ট করা হয়েছে ({current_restarts}/3)।"
                        )
                    except Exception:
                        pass
                else:
                    last_err = "লগ ফাইলে কোনো বিস্তারিত পাওয়া যায়নি।"
                    try:
                        if os.path.exists(data["log_path"]):
                            with open(data["log_path"], "r", encoding="utf-8", errors="ignore") as lf:
                                lines = lf.readlines()
                                last_err = "".join(lines[-8:]) if lines else "Empty log"
                    except Exception:
                        pass

                    try:
                        bot.send_message(
                            uid,
                            f"🚨 <b>বট ক্র্যাশ অ্যালার্ট (Bot Stopped)!</b> 🔴\n"
                            "━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"🤖 <b>বটের নাম:</b> <code>{data['project_name']}</code>\n"
                            f"❌ কোডে মারাত্মক ভুলের কারণে ৩ বার রিস্টার্ট চেষ্টার পর বটটি বন্ধ করা হয়েছে।\n\n"
                            f"📜 <b>এরর বিবরণ:</b>\n<pre>{last_err}</pre>\n"
                            "━━━━━━━━━━━━━━━━━━━━━━\n"
                            "👉 কোড সংশোধন করে আবার <b>🎛️ Control Panel</b> থেকে Start দিন।",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
                    admin.update_project_status(pid, "STOPPED")
                    # অ্যাডমিনকে সাইলেন্ট ক্র্যাশ নোটিফিকেশন
                    u_info = admin.get_user(uid)
                    uname = u_info.get("username") or str(uid)
                    crash_notify = (
                        "🚨 <b>BOT PERMANENTLY CRASHED!</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🆔 <b>User:</b> @{uname} (<code>{uid}</code>)\n"
                        f"🤖 <b>Bot:</b> <code>{data['project_name']}</code>\n"
                        f"🔁 <b>Restarts:</b> 5 attempts failed\n"
                        f"🕒 <b>Time:</b> <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>\n"
                        "━━━━━━━━━━━━━━━━━━━━━━"
                    )
                    for aid in admin.ADMIN_IDS:
                        try:
                            bot.send_message(aid, crash_notify, parse_mode="HTML")
                        except Exception:
                            pass
                    if p_key in active_processes:
                        del active_processes[p_key]
            else:
                # বট স্বাভাবিক চলছে — কিউমুলেটিভ আপটাইম (Leaderboard), RAM অ্যালার্ট ও
                # আপটাইম মাইলস্টোন (৭/৩০ দিন) চেক করা হচ্ছে (প্রতি টিকে ৫ সেকেন্ড যোগ হয়)
                try:
                    admin.add_uptime_seconds(uid, 5)
                except Exception:
                    pass

                try:
                    mem_mb, _cpu = admin.get_bot_resource_usage(proc.pid)
                    ram_limit = admin.SYSTEM_CONFIG.get("max_ram_mb_per_process", 256)
                    threshold = ram_limit * 0.8
                    if mem_mb >= threshold and not data.get("ram_alert_sent", False):
                        data["ram_alert_sent"] = True
                        try:
                            bot.send_message(
                                uid,
                                "⚠️ <b>রিসোর্স অ্যালার্ট: হাই RAM ব্যবহার!</b>\n"
                                "━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"🤖 <b>বট:</b> <code>{data['project_name']}</code>\n"
                                f"💾 <b>RAM ব্যবহার:</b> <code>{mem_mb} MB</code> / <code>{ram_limit} MB</code> ({int(mem_mb*100/ram_limit)}%)\n\n"
                                "👉 বট অপ্টিমাইজ করুন অথবা কোডে মেমরি লিক আছে কিনা যাচাই করুন।",
                                parse_mode="HTML"
                            )
                        except Exception:
                            pass
                    elif mem_mb < threshold * 0.9:
                        data["ram_alert_sent"] = False
                except Exception:
                    pass

                try:
                    elapsed_run = now - start_t
                    for ms_days, ms_key in ((7, "UPTIME_7D"), (30, "UPTIME_30D")):
                        if elapsed_run >= ms_days * 86400 and not admin.has_milestone_sent(pid, ms_key):
                            admin.mark_milestone_sent(pid, ms_key)
                            newly = admin.award_badge(uid, ms_key)
                            badge_line = ""
                            if newly:
                                b = admin.ACHIEVEMENTS[ms_key]
                                badge_line = f"\n\n{b['emoji']} <b>নতুন ব্যাজ অর্জিত:</b> {b['title']}!"
                            try:
                                bot.send_message(
                                    uid,
                                    f"🎯 <b>আপটাইম মাইলস্টোন অর্জিত!</b>\n"
                                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                                    f"🤖 <b>বট:</b> <code>{data['project_name']}</code>\n"
                                    f"⏱️ একটানা <b>{ms_days} দিন</b> সচল আছে! 🔥{badge_line}",
                                    parse_mode="HTML"
                                )
                            except Exception:
                                pass
                except Exception:
                    pass

        # ২ক. শিডিউল্ড Start/Stop — প্রতিটি বটের নির্ধারিত সময় হলে স্বয়ংক্রিয়ভাবে চালু/বন্ধ
        try:
            now_hm = datetime.now().strftime("%H:%M")
            today_str = datetime.now().strftime("%Y-%m-%d")
            for sp in admin.get_scheduled_projects():
                s_uid = sp["user_id"]
                s_pid = sp["project_id"]
                s_key = f"{s_uid}_{s_pid}"
                start_tag = f"start_{today_str}_{sp.get('sched_start_time')}"
                stop_tag = f"stop_{today_str}_{sp.get('sched_stop_time')}"
                if sp.get("sched_start_time") == now_hm and sp.get("sched_last_action") != start_tag:
                    admin.mark_schedule_action(s_pid, start_tag)
                    if s_key not in active_processes:
                        ok, _ = execute_project_script(s_uid, s_pid)
                        if ok:
                            try:
                                bot.send_message(s_uid, f"⏰ <b>শিডিউল অনুযায়ী বট চালু হয়েছে:</b> <code>{sp['project_name']}</code>", parse_mode="HTML")
                            except Exception:
                                pass
                if sp.get("sched_stop_time") == now_hm and sp.get("sched_last_action") != stop_tag:
                    admin.mark_schedule_action(s_pid, stop_tag)
                    if s_key in active_processes:
                        stop_project_script(s_uid, s_pid)
                        try:
                            bot.send_message(s_uid, f"⏰ <b>শিডিউল অনুযায়ী বট বন্ধ হয়েছে:</b> <code>{sp['project_name']}</code>", parse_mode="HTML")
                        except Exception:
                            pass
        except Exception:
            pass

        # ২খ. অটো-ব্রডকাস্ট — এডমিনের সেট করা শিডিউল্ড মেসেজ সময়মতো পাঠানো
        try:
            due = admin.get_due_broadcasts()
            for b in due:
                sent_count = 0
                for u in admin.get_all_users():
                    try:
                        bot.send_message(u["user_id"], b["message"], parse_mode="HTML")
                        sent_count += 1
                    except Exception:
                        pass
                admin.mark_broadcast_sent(b["id"], sent_count)
        except Exception:
            pass


        # ২. পেইড সাবস্ক্রিপশন মেয়াদ অ্যালার্ট (২৪ ঘণ্টা ও ২ ঘণ্টা আগে)
        try:
            conn = admin.get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT user_id, username, plan, plan_expiry, expiry_alert_24h, expiry_alert_2h, expiry_alert_3d FROM users WHERE plan IN ('PRO', 'PREMIUM', 'BUSINESS', 'VIP')")
            paid_users = cur.fetchall()
            now_dt = datetime.now()

            for pu in paid_users:
                exp_str = pu["plan_expiry"]
                if exp_str and exp_str != "LIFETIME":
                    try:
                        exp_dt = datetime.strptime(exp_str, "%Y-%m-%d %H:%M:%S")
                        diff = exp_dt - now_dt
                        
                        # ৩ দিন আগে রিমাইন্ডার (প্রথম ও সবচেয়ে গুরুত্বপূর্ণ অ্যালার্ট)
                        if timedelta(days=0) < diff <= timedelta(days=3) and not pu["expiry_alert_3d"]:
                            cur.execute("UPDATE users SET expiry_alert_3d = 1 WHERE user_id = ?", (pu["user_id"],))
                            conn.commit()
                            days_left = max(1, diff.days)
                            bot.send_message(
                                pu["user_id"],
                                f"📅 <b>সাবস্ক্রিপশন রিমাইন্ডার ({days_left} দিন বাকি)!</b>\n"
                                "━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"👑 <b>প্ল্যান:</b> <code>{pu['plan']}</code>\n"
                                f"⏳ <b>মেয়াদ শেষ:</b> <code>{exp_str}</code>\n\n"
                                f"🔔 আপনার প্ল্যানের মেয়াদ মাত্র <b>{days_left} দিন</b> পরে শেষ হয়ে যাবে!\n"
                                "এখনই রিনিউ করলে মেয়াদের উপরে যোগ হবে (কোনো দিন নষ্ট হবে না)।\n\n"
                                "👉 <b>💎 Upgrade Plan</b> থেকে রিনিউ করুন।",
                                reply_markup=_renew_markup(), parse_mode="HTML"
                            )
                        
                        # ২৪ ঘণ্টা আগে রিমাইন্ডার
                        if timedelta(hours=0) < diff <= timedelta(hours=24) and not pu["expiry_alert_24h"]:
                            cur.execute("UPDATE users SET expiry_alert_24h = 1 WHERE user_id = ?", (pu["user_id"],))
                            conn.commit()
                            bot.send_message(
                                pu["user_id"],
                                f"🔔 <b>সাবস্ক্রিপশন রিনিউয়াল রিমাইন্ডার (২৪ ঘণ্টা বাকি)!</b>\n"
                                "━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"👤 <b>বর্তমান প্ল্যান:</b> <code>{pu['plan']}</code>\n"
                                f"⏳ আপনার প্ল্যানের মেয়াদ শেষ হবে: <code>{exp_str}</code>\n\n"
                                "👉 বট ২৪/৭ সচল রাখতে এখনই <b>💎 Upgrade Plan</b> থেকে রিনিউ করে নিন!",
                                reply_markup=_renew_markup(), parse_mode="HTML"
                            )

                        # ২ ঘণ্টা আগে রিমাইন্ডার
                        elif timedelta(hours=0) < diff <= timedelta(hours=2) and not pu["expiry_alert_2h"]:
                            cur.execute("UPDATE users SET expiry_alert_2h = 1 WHERE user_id = ?", (pu["user_id"],))
                            conn.commit()
                            bot.send_message(
                                pu["user_id"],
                                f"⚠️ <b>জরুরি নোটিশ: প্ল্যানের মেয়াদ আর মাত্র ২ ঘণ্টা বাকি!</b>\n"
                                "━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"👤 <b>প্ল্যান:</b> <code>{pu['plan']}</code>\n"
                                "👉 এখনই <b>💎 Upgrade Plan</b> ট্যাপ করে মেয়াদ বাড়িয়ে নিন।",
                                reply_markup=_renew_markup(), parse_mode="HTML"
                            )

                        # মেয়াদ সম্পূর্ণ শেষ হলে ফ্রি প্ল্যানে নেওয়া
                        elif diff <= timedelta(seconds=0):
                            cur.execute("UPDATE users SET plan = 'FREE', plan_expiry = 'LIFETIME', expiry_alert_24h = 0, expiry_alert_2h = 0, expiry_alert_3d = 0 WHERE user_id = ?", (pu["user_id"],))
                            conn.commit()
                            bot.send_message(
                                pu["user_id"],
                                "ℹ️ <b>আপনার প্রিমিয়াম সাবস্ক্রিপশনের মেয়াদ শেষ হয়েছে।</b>\nএকাউন্টটি ফ্রি প্ল্যানে (১২ ঘণ্টা/সেশন) ফিরিয়ে নেওয়া হয়েছে। ২৪/৭ চালাতে যেকোনো সময় রিনিউ করতে পারবেন।",
                                reply_markup=_renew_markup(), parse_mode="HTML"
                            )
                    except Exception:
                        pass
            conn.close()
        except Exception:
            pass

threading.Thread(target=hosting_watchdog_thread, daemon=True).start()

def daily_report_scheduler():
    """প্রতি মিনিটে চেক করে — ইউজারের সেট করা সময় হলে Daily Report পাঠায়।
    Bangladesh Time (UTC+6) ব্যবহার করা হচ্ছে।"""
    import time as _time
    sent_today = {}  # {user_id: date_str} — একই দিনে দুইবার পাঠানো এড়াতে

    while True:
        try:
            # UTC+6 = Bangladesh Time
            now_bst = datetime.utcnow()
            from datetime import timezone, timedelta as _td
            bst = now_bst + _td(hours=6)
            current_time = bst.strftime("%H:%M")
            current_date = bst.strftime("%Y-%m-%d")

            users_with_report = admin.get_users_with_daily_report()
            for u in users_with_report:
                uid = u["user_id"]
                report_time = u["daily_report_time"]
                if report_time != current_time:
                    continue
                if sent_today.get(uid) == current_date:
                    continue

                # রিপোর্ট তৈরি করা
                try:
                    projects = admin.get_user_projects(uid)
                    total = len(projects)
                    running = sum(1 for p in projects if f"{uid}_{p['project_id']}" in active_processes and active_processes[f"{uid}_{p['project_id']}"]['process'].poll() is None)
                    stopped = total - running

                    u_data = admin.get_user(uid)
                    pinfo = admin.get_plan_display_info(u_data)

                    report = (
                        f"📊 <b>Daily Report — {current_date}</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"⏰ <b>সময়:</b> {current_time} BST\n"
                        f"👑 <b>প্ল্যান:</b> {pinfo['icon']} {pinfo['display_name']}\n"
                        "━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🤖 <b>মোট বট:</b> {total}টি\n"
                        f"🟢 <b>চলছে:</b> {running}টি\n"
                        f"🔴 <b>বন্ধ:</b> {stopped}টি\n"
                        "━━━━━━━━━━━━━━━━━━━━━━\n"
                    )
                    if total > 0:
                        for p in projects:
                            p_key = f"{uid}_{p['project_id']}"
                            is_run = p_key in active_processes and active_processes[p_key]['process'].poll() is None
                            status = "🟢 চলছে" if is_run else "🔴 বন্ধ"
                            report += f"• {html.escape(p['project_name'])}: {status}\n"

                    markup = types.InlineKeyboardMarkup()
                    markup.add(make_inline_button("🎛️ Control Panel", callback_data="open_cp", style="primary"))
                    bot.send_message(uid, report, reply_markup=markup, parse_mode="HTML")
                    sent_today[uid] = current_date
                except Exception:
                    pass

            # পুরনো sent_today রেকর্ড পরিষ্কার (RAM বাঁচাতে)
            if len(sent_today) > 5000:
                sent_today.clear()

        except Exception:
            pass
        _time.sleep(60)  # প্রতি মিনিটে একবার চেক

threading.Thread(target=daily_report_scheduler, daemon=True).start()


# ----------------- বোল্ড ফন্ট (𝗕𝗼𝗹𝗱) — সব বাটনে স্টাইল ফন্ট -----------------
def bold_font(text):
    """ইংরেজি অক্ষর/সংখ্যাকে বোল্ড ইউনিকোড ফন্টে রূপান্তর করে (Git → 𝗚𝗶𝘁, Slot 1 → 𝗦𝗹𝗼𝘁 𝟭)।"""
    out = []
    for ch in str(text):
        o = ord(ch)
        if 65 <= o <= 90:
            out.append(chr(0x1D5D4 + o - 65))
        elif 97 <= o <= 122:
            out.append(chr(0x1D5EE + o - 97))
        elif 48 <= o <= 57:
            out.append(chr(0x1D7EC + o - 48))
        else:
            out.append(ch)
    return "".join(out)

def unbold_font(text):
    out = []
    for ch in str(text):
        o = ord(ch)
        if 0x1D5D4 <= o <= 0x1D5ED:
            out.append(chr(65 + o - 0x1D5D4))
        elif 0x1D5EE <= o <= 0x1D607:
            out.append(chr(97 + o - 0x1D5EE))
        elif 0x1D7EC <= o <= 0x1D7F5:
            out.append(chr(48 + o - 0x1D7EC))
        else:
            out.append(ch)
    return "".join(out)

def normalize_button_text(text):
    """বোল্ড ফন্টের রিপ্লাই-বাটন চাপলে যে টেক্সট আসে সেটাকে আসল ইংরেজি লেবেলে ফিরিয়ে আনে,
    যাতে আগের সব `text == "..."` চেক আগের মতোই কাজ করে।"""
    if not text or len(text) > 60:
        return text
    if any(0x1D5D4 <= ord(c) <= 0x1D7F5 for c in text) and not text[0].isalnum():
        return unbold_font(text)
    return text

def make_kb(text, **kwargs):
    return types.KeyboardButton(bold_font(text), **kwargs)

# ----------------- 🛒 মার্কেটপ্লেস টেমপ্লেট রেজিস্ট্রি -----------------
# নতুন টেমপ্লেট যোগ করতে শুধু এই ডিকশনারিতে একটা এন্ট্রি বসালেই লিস্ট, ডিটেইলস, ডেপ্লয় সব অটো কাজ করবে।
# কোডে __TOKEN__ জায়গায় ইউজারের বট টোকেন বসে যায়।
MARKETPLACE_TEMPLATES = {
    "group_automator": {
        "emoji": "👥", "name": "Premium Group Automator", "proj_name": "Group Automator",
        "category": '🛡️ Group Management', "level": '⭐⭐ Intermediate', "setup": '~30 sec',
        "features": ['নতুন মেম্বার ক্যাপচা বাটনে ভেরিফাই না করা পর্যন্ত মিউট (সময় পেরোলে অটো-কিক)', 'নন-অ্যাডমিনের লিংক/স্প্যাম মেসেজ অটো-ডিলিট', '৩ স্ট্রাইকে ১ ঘণ্টার অটো-মিউট', 'অ্যাডমিনরা রিপ্লাই করে warn/unwarn/mute/unmute/kick করতে পারবেন'],
        "commands": '/warn /unwarn /mute /unmute /kick',
        "about": "গ্রুপ ম্যানেজমেন্ট বট — ক্যাপচা ভেরিফিকেশন, লিংক/স্প্যাম অটো-ডিলিট আর /warn, /mute, /kick এর মতো মডারেশন কমান্ড সহ।",
        "steps": [
            "বট সাথে সাথে ডিপ্লয় ও লাইভ হবে",
            "বটটিকে গ্রুপে Admin (Restrict + Delete Messages পারমিশন সহ) যোগ করুন",
            "নতুন মেম্বার জয়েন করলে ক্যাপচা বাটনে ক্লিক না করা পর্যন্ত মিউট থাকবে",
            "নন-অ্যাডমিনদের পাঠানো লিংক/স্প্যাম মেসেজ অটো ডিলিট হবে, ৩ বার ওয়ার্নিং পেলে অটো-মিউট",
            "গ্রুপ অ্যাডমিনরা রিপ্লাই করে /warn /mute /unmute /kick ব্যবহার করতে পারবেন",
        ],
        "need": "শুধু বট টোকেন (@BotFather থেকে) + গ্রুপে Admin পারমিশন",
        "preview": "👤 Rahim joined\n🤖 👋 Welcome Rahim! ✅ বাটনে ক্লিক করে ভেরিফাই করুন\n👤 [লিংক স্প্যাম] → 🤖 ডিলিট + ⚠️ Warning 1/3",
        "code": r'''import telebot, json, os, time, threading
from telebot import types

BOT_TOKEN = '__TOKEN__'
bot = telebot.TeleBot(BOT_TOKEN, parse_mode='HTML')
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'group_data.json')
lock = threading.Lock()
PENDING_TTL = 180  # সেকেন্ড — এর মধ্যে ক্যাপচা ভেরিফাই না করলে কিক

def load():
    try:
        with open(DB, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {"warns": {}, "pending": {}}

def save(d):
    with open(DB, 'w', encoding='utf-8') as f:
        json.dump(d, f)

def is_admin(chat_id, user_id):
    try:
        m = bot.get_chat_member(chat_id, user_id)
        return m.status in ('administrator', 'creator')
    except Exception:
        return False

def key(chat_id, user_id):
    return f'{chat_id}_{user_id}'

@bot.message_handler(commands=['start'])
def start(m):
    bot.reply_to(m, '🛡️ <b>Premium Group Automator is Active!</b>\n\nবটকে গ্রুপে Admin বানান — নতুন মেম্বার ক্যাপচা, লিংক-স্প্যাম ফিল্টার ও মডারেশন কমান্ড (/warn /mute /unmute /kick) স্বয়ংক্রিয়ভাবে কাজ করবে।\n\n⚡ Powered by @PyHost_CloudBot')

@bot.message_handler(content_types=['new_chat_members'])
def welcome(m):
    d = load()
    for u in m.new_chat_members:
        if u.id == bot.get_me().id:
            continue
        try:
            bot.restrict_chat_member(m.chat.id, u.id, can_send_messages=False)
        except Exception:
            pass
        d['pending'][key(m.chat.id, u.id)] = time.time() + PENDING_TTL
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton('✅ আমি রোবট না', callback_data=f'verify_{m.chat.id}_{u.id}'))
        bot.send_message(m.chat.id, f'👋 Welcome <b>{u.first_name}</b>!\n\n⏳ {PENDING_TTL//60} মিনিটের মধ্যে নিচের বাটনে ক্লিক করে ভেরিফাই করুন, নাহলে গ্রুপ থেকে বের করে দেওয়া হবে।\n\n⚡ Powered by @PyHost_CloudBot', reply_markup=kb)
    save(d)

@bot.callback_query_handler(func=lambda c: c.data.startswith('verify_'))
def verify(c):
    _, chat_id, uid = c.data.split('_')
    chat_id, uid = int(chat_id), int(uid)
    if c.from_user.id != uid:
        bot.answer_callback_query(c.id, '⚠️ এই বাটন আপনার জন্য নয়!', show_alert=True)
        return
    d = load()
    d['pending'].pop(key(chat_id, uid), None)
    save(d)
    try:
        bot.restrict_chat_member(chat_id, uid, can_send_messages=True, can_send_media_messages=True, can_send_other_messages=True, can_add_web_page_previews=True)
    except Exception:
        pass
    bot.answer_callback_query(c.id, '✅ ভেরিফাই সফল! এখন মেসেজ পাঠাতে পারবেন।')
    try:
        bot.edit_message_text('✅ ভেরিফিকেশন সম্পন্ন — স্বাগতম!', chat_id, c.message.message_id)
    except Exception:
        pass

def captcha_watchdog():
    while True:
        time.sleep(20)
        try:
            d = load()
            now = time.time()
            expired = [k for k, ts in d['pending'].items() if ts <= now]
            for k in expired:
                chat_id, uid = k.split('_')
                try:
                    bot.kick_chat_member(int(chat_id), int(uid))
                    bot.unban_chat_member(int(chat_id), int(uid))
                except Exception:
                    pass
                d['pending'].pop(k, None)
            if expired:
                save(d)
        except Exception:
            pass

LINK_MARKERS = ('http://', 'https://', 't.me/', 'www.')

@bot.message_handler(func=lambda m: m.chat.type in ('group', 'supergroup') and bool(m.text), content_types=['text'])
def antispam(m):
    if is_admin(m.chat.id, m.from_user.id):
        return
    low = m.text.lower()
    if not any(mk in low for mk in LINK_MARKERS):
        return
    try:
        bot.delete_message(m.chat.id, m.message_id)
    except Exception:
        pass
    d = load()
    k = key(m.chat.id, m.from_user.id)
    d['warns'][k] = d['warns'].get(k, 0) + 1
    n = d['warns'][k]
    save(d)
    if n >= 3:
        try:
            bot.restrict_chat_member(m.chat.id, m.from_user.id, can_send_messages=False, until_date=int(time.time()) + 3600)
        except Exception:
            pass
        bot.send_message(m.chat.id, f'🔇 {m.from_user.first_name} ৩ বার ওয়ার্নিং পেয়ে ১ ঘণ্টার জন্য মিউট হয়েছে।')
        d['warns'][k] = 0
        save(d)
    else:
        bot.send_message(m.chat.id, f'⚠️ {m.from_user.first_name}, লিংক পাঠানো নিষেধ! Warning {n}/3')

def _target(m):
    if m.reply_to_message:
        return m.reply_to_message.from_user
    return None

@bot.message_handler(commands=['warn', 'unwarn', 'mute', 'unmute', 'kick'])
def mod_commands(m):
    if not is_admin(m.chat.id, m.from_user.id):
        bot.reply_to(m, '⚠️ শুধু গ্রুপ অ্যাডমিনরা এই কমান্ড ব্যবহার করতে পারবেন।')
        return
    target = _target(m)
    if not target:
        bot.reply_to(m, '⚠️ যাকে এই একশন দিতে চান তার মেসেজে রিপ্লাই করে কমান্ডটি দিন।')
        return
    cmd = m.text.split()[0][1:]
    d = load()
    k = key(m.chat.id, target.id)
    if cmd == 'warn':
        d['warns'][k] = d['warns'].get(k, 0) + 1
        save(d)
        bot.reply_to(m, f'⚠️ {target.first_name} — Warning {d["warns"][k]}/3')
    elif cmd == 'unwarn':
        d['warns'][k] = max(0, d['warns'].get(k, 0) - 1)
        save(d)
        bot.reply_to(m, f'✅ {target.first_name} — Warning {d["warns"][k]}/3')
    elif cmd == 'mute':
        try:
            bot.restrict_chat_member(m.chat.id, target.id, can_send_messages=False)
            bot.reply_to(m, f'🔇 {target.first_name} মিউট করা হয়েছে।')
        except Exception:
            bot.reply_to(m, '⚠️ মিউট করা যায়নি — বট অ্যাডমিন কিনা চেক করুন।')
    elif cmd == 'unmute':
        try:
            bot.restrict_chat_member(m.chat.id, target.id, can_send_messages=True, can_send_media_messages=True, can_send_other_messages=True, can_add_web_page_previews=True)
            bot.reply_to(m, f'🔊 {target.first_name} আনমিউট করা হয়েছে।')
        except Exception:
            bot.reply_to(m, '⚠️ আনমিউট করা যায়নি।')
    elif cmd == 'kick':
        try:
            bot.kick_chat_member(m.chat.id, target.id)
            bot.unban_chat_member(m.chat.id, target.id)
            bot.reply_to(m, f'👢 {target.first_name} গ্রুপ থেকে বের করে দেওয়া হয়েছে।')
        except Exception:
            bot.reply_to(m, '⚠️ কিক করা যায়নি — বট অ্যাডমিন কিনা চেক করুন।')

threading.Thread(target=captcha_watchdog, daemon=True).start()
bot.infinity_polling()
''',
    },

    "file_locker": {
        "emoji": "🔒", "name": "Advanced File Locker Bot", "proj_name": "File Locker Bot",
        "category": '🔐 Security & Storage', "level": '⭐⭐ Intermediate', "setup": '~30 sec',
        "features": ['ফাইল/ছবি/ভিডিও/অডিও/ভয়েস পাসওয়ার্ড দিয়ে লক (SHA-256 হ্যাশ করে সেভ)', '৬-অক্ষরের ইউনিক লক ID পাবেন', 'মিনিট দিয়ে অটো-এক্সপায়ার সেট করা যায়', 'ভিউ-লিমিট সেট করা যায় (কতবার খোলা যাবে)'],
        "commands": '/lock <পাসওয়ার্ড> [মিনিট] [ভিউ] · /unlock <ID> <পাসওয়ার্ড>',
        "about": "ফাইল/ছবি/ভিডিও পাসওয়ার্ড দিয়ে লক করে রাখে — এক্সপায়ারি টাইম ও ভিউ-লিমিট সেট করা যায়।",
        "steps": [
            "বট সাথে সাথে ডিপ্লয় ও লাইভ হবে",
            "ফাইল পাঠিয়ে ক্যাপশনে লিখুন: /lock পাসওয়ার্ড [মিনিট] [ভিউলিমিট] (দুটোই ঐচ্ছিক)",
            "বট একটা Lock ID দেবে",
            "/unlock ID পাসওয়ার্ড দিলে ফাইল ফেরত আসবে — এক্সপায়ার বা ভিউ-লিমিট শেষ হলে আর খোলা যাবে না",
        ],
        "need": "শুধু বট টোকেন",
        "preview": "👤 [photo] /lock 1234 30 2\n🤖 🔒 লক! ID: K7X２QA (৩০ মিনিট, ২ বার দেখা যাবে)\n👤 /unlock K7X2QA 1234\n🤖 [photo] ✅ আনলক সফল! (বাকি ১ বার)",
        "code": r'''import telebot, json, os, hashlib, random, string, time, threading

BOT_TOKEN = '__TOKEN__'
bot = telebot.TeleBot(BOT_TOKEN, parse_mode='HTML')
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'locks.json')
lock = threading.Lock()

def load():
    try:
        with open(DB, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}

def save(d):
    with open(DB, 'w', encoding='utf-8') as f:
        json.dump(d, f)

def h(p):
    return hashlib.sha256(p.encode()).hexdigest()

SENDERS = {'document': bot.send_document, 'photo': bot.send_photo, 'video': bot.send_video, 'audio': bot.send_audio, 'voice': bot.send_voice}

USAGE = '🔒 <b>Advanced File Locker</b>\n\n📥 লক: ফাইল/ছবি/ভিডিও পাঠিয়ে ক্যাপশনে লিখুন:\n<code>/lock পাসওয়ার্ড [মিনিট] [ভিউলিমিট]</code>\n(মিনিট ও ভিউলিমিট ঐচ্ছিক — না দিলে কখনো এক্সপায়ার হবে না)\n\n📤 আনলক: <code>/unlock ID পাসওয়ার্ড</code>\n\n⚡ Powered by @PyHost_CloudBot'

@bot.message_handler(commands=['start', 'help'])
def start(m):
    bot.reply_to(m, USAGE)

@bot.message_handler(content_types=['document', 'photo', 'video', 'audio', 'voice', 'text'])
def handle(m):
    txt = (m.caption or m.text or '').strip()
    if txt.startswith('/lock'):
        parts = txt.split()
        if len(parts) < 2 or m.content_type == 'text':
            bot.reply_to(m, '⚠️ ফাইল/ছবি/ভিডিও পাঠিয়ে ক্যাপশনে লিখুন: <code>/lock পাসওয়ার্ড [মিনিট] [ভিউলিমিট]</code>')
            return
        pw = parts[1]
        expiry_ts = None
        max_views = None
        try:
            if len(parts) >= 3:
                expiry_ts = time.time() + int(parts[2]) * 60
            if len(parts) >= 4:
                max_views = int(parts[3])
        except ValueError:
            bot.reply_to(m, '⚠️ মিনিট ও ভিউলিমিট সংখ্যা হতে হবে।')
            return
        if m.content_type == 'photo':
            fid = m.photo[-1].file_id
        else:
            fid = getattr(m, m.content_type).file_id
        lid = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        with lock:
            d = load()
            d[lid] = {'type': m.content_type, 'file_id': fid, 'pw': h(pw), 'owner': m.from_user.id, 'expiry_ts': expiry_ts, 'max_views': max_views, 'views': 0}
            save(d)
        try:
            bot.delete_message(m.chat.id, m.message_id)
        except Exception:
            pass
        extra = []
        if expiry_ts:
            extra.append(f'{parts[2]} মিনিট পর এক্সপায়ার')
        if max_views:
            extra.append(f'সর্বোচ্চ {max_views} বার দেখা যাবে')
        extra_line = (' (' + ', '.join(extra) + ')') if extra else ''
        bot.send_message(m.chat.id, f'🔒 <b>লক হয়েছে!</b>{extra_line}\n🆔 ID: <code>{lid}</code>\n\nখুলতে: <code>/unlock {lid} পাসওয়ার্ড</code>')
    elif txt.startswith('/unlock'):
        parts = txt.split()
        if len(parts) < 3:
            bot.reply_to(m, '⚠️ ব্যবহার: <code>/unlock ID পাসওয়ার্ড</code>')
            return
        lid = parts[1].upper()
        try:
            bot.delete_message(m.chat.id, m.message_id)
        except Exception:
            pass
        with lock:
            d = load()
            e = d.get(lid)
            if not e or e['pw'] != h(parts[2]):
                bot.send_message(m.chat.id, '❌ ID বা পাসওয়ার্ড ভুল।')
                return
            if e['expiry_ts'] and time.time() > e['expiry_ts']:
                d.pop(lid, None)
                save(d)
                bot.send_message(m.chat.id, '⌛ এই লকটির মেয়াদ শেষ হয়ে গেছে।')
                return
            e['views'] += 1
            remaining = None
            if e['max_views']:
                remaining = e['max_views'] - e['views']
                if remaining < 0:
                    d.pop(lid, None)
                    save(d)
                    bot.send_message(m.chat.id, '⚠️ এই লকের ভিউ-লিমিট শেষ হয়ে গেছে।')
                    return
                if remaining <= 0:
                    d.pop(lid, None)
                else:
                    d[lid] = e
            else:
                d[lid] = e
            save(d)
        cap = '✅ আনলক সফল!' + (f' (বাকি {remaining} বার)' if remaining is not None else '')
        SENDERS[e['type']](m.chat.id, e['file_id'], caption=cap)
    elif txt in ('/start', '/help'):
        bot.reply_to(m, USAGE)

def cleanup_worker():
    while True:
        time.sleep(30)
        try:
            with lock:
                d = load()
                now = time.time()
                dead = [k for k, e in d.items() if e.get('expiry_ts') and now > e['expiry_ts']]
                for k in dead:
                    d.pop(k, None)
                if dead:
                    save(d)
        except Exception:
            pass

threading.Thread(target=cleanup_worker, daemon=True).start()
bot.infinity_polling()
''',
    },

    "echo": {
        "emoji": "💬", "name": "FAQ & Support Bot", "proj_name": "FAQ Support Bot",
        "category": '💬 FAQ & Support', "level": '⭐ Beginner', "setup": '~20 sec',
        "features": ['প্রথম যিনি /start দেবেন তিনি অটো Owner হয়ে যাবেন', 'Owner চ্যাটেই FAQ যোগ/মোছা যায়', 'ইউজাররা ইনলাইন বাটনে প্রশ্ন বেছে সাথে সাথে উত্তর পায়', 'আপনি অফলাইন থাকলেও ২৪/৭ সাপোর্ট চলে'],
        "commands": '/faq /addfaq প্রশ্ন|উত্তর · /delfaq প্রশ্ন',
        "about": "প্রশ্ন-উত্তর (FAQ) সেভ করে রাখা যায়, ইউজার বাটনে ট্যাপ করেই উত্তর পাবে — না মিললে সাপোর্টে যোগাযোগের অপশন দেখাবে।",
        "steps": [
            "বট সাথে সাথে ডিপ্লয় ও লাইভ হবে",
            "সবার আগে আপনি /start দিন — আপনি অটোমেটিক Owner হয়ে যাবেন",
            "Owner হিসেবে /addfaq প্রশ্ন|উত্তর দিয়ে FAQ যোগ করুন",
            "ইউজার /faq দিলে সব প্রশ্ন বাটন আকারে দেখবে, ট্যাপ করলেই উত্তর পাবে",
            "কোনো FAQ না মিললে বট প্রশ্নটা echo করে সাপোর্ট বাটন দেখাবে",
        ],
        "need": "শুধু বট টোকেন",
        "preview": "👤 /addfaq রিফান্ড কীভাবে পাবো?|৭ দিনের মধ্যে সাপোর্টে জানান\n👤 /faq → [বাটন: রিফান্ড কীভাবে পাবো?]\n👤 [ট্যাপ] 🤖 ৭ দিনের মধ্যে সাপোর্টে জানান",
        "code": r'''import telebot, json, os
from telebot import types

BOT_TOKEN = '__TOKEN__'
bot = telebot.TeleBot(BOT_TOKEN, parse_mode='HTML')
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'faq_data.json')

def load():
    try:
        with open(DB, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {"owner": None, "faqs": {}}

def save(d):
    with open(DB, 'w', encoding='utf-8') as f:
        json.dump(d, f)

def is_owner(uid):
    d = load()
    return d.get('owner') == uid

@bot.message_handler(commands=['start'])
def start(m):
    d = load()
    if d.get('owner') is None:
        d['owner'] = m.from_user.id
        save(d)
        bot.reply_to(m, '👑 <b>আপনি এই বটের Owner হয়ে গেছেন!</b>\n\n/addfaq প্রশ্ন|উত্তর — নতুন FAQ যোগ করুন\n/delfaq প্রশ্ন — FAQ মুছুন\n/faq — সব প্রশ্ন দেখুন\n\n⚡ Powered by @PyHost_CloudBot')
    else:
        bot.reply_to(m, '💬 <b>FAQ & Support Bot চালু আছে!</b>\n\n/faq লিখে সাধারণ প্রশ্নগুলো দেখুন।\n\n⚡ Powered by @PyHost_CloudBot')

@bot.message_handler(commands=['addfaq'])
def addfaq(m):
    if not is_owner(m.from_user.id):
        bot.reply_to(m, '⚠️ শুধু Owner FAQ যোগ করতে পারবেন।')
        return
    parts = m.text.split(None, 1)
    if len(parts) < 2 or '|' not in parts[1]:
        bot.reply_to(m, '⚠️ ব্যবহার: <code>/addfaq প্রশ্ন|উত্তর</code>')
        return
    q, a = parts[1].split('|', 1)
    d = load()
    d['faqs'][q.strip()] = a.strip()
    save(d)
    bot.reply_to(m, f'✅ FAQ যোগ হয়েছে: <b>{q.strip()}</b>')

@bot.message_handler(commands=['delfaq'])
def delfaq(m):
    if not is_owner(m.from_user.id):
        bot.reply_to(m, '⚠️ শুধু Owner FAQ মুছতে পারবেন।')
        return
    parts = m.text.split(None, 1)
    if len(parts) < 2:
        bot.reply_to(m, '⚠️ ব্যবহার: <code>/delfaq প্রশ্ন</code>')
        return
    d = load()
    if d['faqs'].pop(parts[1].strip(), None) is not None:
        save(d)
        bot.reply_to(m, '🗑️ মুছে ফেলা হয়েছে।')
    else:
        bot.reply_to(m, '❌ এই নামে কোনো FAQ পাওয়া যায়নি।')

@bot.message_handler(commands=['faq'])
def faqlist(m):
    d = load()
    if not d['faqs']:
        bot.reply_to(m, 'এখনো কোনো FAQ যোগ করা হয়নি।')
        return
    kb = types.InlineKeyboardMarkup(row_width=1)
    for i, q in enumerate(d['faqs'].keys()):
        kb.add(types.InlineKeyboardButton(q, callback_data=f'faq_{i}'))
    bot.reply_to(m, '❓ <b>প্রশ্ন বেছে নিন:</b>', reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith('faq_'))
def faq_answer(c):
    d = load()
    keys = list(d['faqs'].keys())
    idx = int(c.data.replace('faq_', ''))
    if idx >= len(keys):
        bot.answer_callback_query(c.id, '❌ পাওয়া যায়নি।')
        return
    q = keys[idx]
    bot.answer_callback_query(c.id)
    bot.send_message(c.message.chat.id, f'❓ <b>{q}</b>\n\n{d["faqs"][q]}')

@bot.message_handler(func=lambda m: bool(m.text) and not m.text.startswith('/'))
def fallback(m):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton('❓ FAQ দেখুন', callback_data='show_faq_list'))
    bot.reply_to(m, f'{m.text}\n\nℹ️ এই প্রশ্নের সরাসরি উত্তর নেই। নিচের বাটনে FAQ দেখুন বা সাপোর্টে যোগাযোগ করুন।\n\n⚡ Powered by @PyHost_CloudBot', reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data == 'show_faq_list')
def show_faq_cb(c):
    bot.answer_callback_query(c.id)
    fake = type('M', (), {'chat': c.message.chat, 'message_id': c.message.message_id})
    faqlist(c.message)

bot.infinity_polling()
''',
    },

    "qr_generator": {
        "emoji": "📷", "name": "QR Code Generator Bot", "proj_name": "QR Generator Bot",
        "category": '🧰 Utility', "level": '⭐ Beginner', "setup": '~20 sec',
        "features": ['যেকোনো টেক্সট/লিংক পাঠালেই সাথে সাথে QR ইমেজ', 'WiFi QR — স্ক্যান করলেই কানেক্ট', 'Contact (vCard) QR — নাম, ফোন, ইমেইল', 'একাধিক লাইন পাঠালে প্রতিটার আলাদা QR (সর্বোচ্চ ৫টা)'],
        "commands": '/wifi SSID|পাস|WPA · /contact নাম|ফোন|ইমেইল',
        "about": "টেক্সট/লিংক ছাড়াও WiFi QR ও Contact (vCard) QR বানাতে পারে, একসাথে একাধিক QR-ও বানানো যায়।",
        "steps": [
            "বট সাথে সাথে ডিপ্লয় ও লাইভ হবে",
            "সাধারণ টেক্সট/লিংক পাঠালে সরাসরি QR পাবেন",
            "WiFi QR: /wifi SSID|পাসওয়ার্ড|WPA (WPA/WEP/nopass)",
            "Contact QR: /contact নাম|ফোন|ইমেইল",
            "একাধিক লাইনে টেক্সট পাঠালে প্রতিটার জন্য আলাদা QR (সর্বোচ্চ ৫টা)",
        ],
        "need": "শুধু বট টোকেন",
        "preview": "👤 /wifi HomeWifi|pass123|WPA\n🤖 [QR ছবি] 📶 WiFi QR রেডি — স্ক্যান করে কানেক্ট করুন!",
        "code": r'''import telebot
import urllib.parse

BOT_TOKEN = '__TOKEN__'
bot = telebot.TeleBot(BOT_TOKEN, parse_mode='HTML')

def qr_url(data):
    return 'https://api.qrserver.com/v1/create-qr-code/?size=500x500&margin=10&data=' + urllib.parse.quote(data[:900])

USAGE = ('📷 <b>QR Code Generator</b>\n\n'
         '✅ সাধারণ টেক্সট/লিংক পাঠান — সরাসরি QR\n'
         '📶 <code>/wifi SSID|পাসওয়ার্ড|WPA</code> (WPA/WEP/nopass)\n'
         '👤 <code>/contact নাম|ফোন|ইমেইল</code>\n'
         '📚 একাধিক লাইনে টেক্সট পাঠালে প্রতিটার জন্য আলাদা QR (সর্বোচ্চ ৫টা)\n\n'
         '⚡ Powered by @PyHost_CloudBot')

@bot.message_handler(commands=['start', 'help'])
def start(m):
    bot.reply_to(m, USAGE)

@bot.message_handler(commands=['wifi'])
def wifi(m):
    parts = m.text.split(None, 1)
    if len(parts) < 2 or parts[1].count('|') < 1:
        bot.reply_to(m, '⚠️ ব্যবহার: <code>/wifi SSID|পাসওয়ার্ড|WPA</code>')
        return
    bits = parts[1].split('|')
    ssid = bits[0].strip()
    pw = bits[1].strip() if len(bits) > 1 else ''
    sec = bits[2].strip().upper() if len(bits) > 2 else 'WPA'
    if sec not in ('WPA', 'WEP', 'NOPASS'):
        sec = 'WPA'
    payload = f'WIFI:T:{sec};S:{ssid};P:{pw};;'
    try:
        bot.send_photo(m.chat.id, qr_url(payload), caption=f'📶 <b>{ssid}</b> WiFi QR রেডি — ক্যামেরা দিয়ে স্ক্যান করে কানেক্ট করুন!\n\n⚡ Powered by @PyHost_CloudBot')
    except Exception:
        bot.reply_to(m, '⚠️ QR বানানো যায়নি।')

@bot.message_handler(commands=['contact'])
def contact(m):
    parts = m.text.split(None, 1)
    if len(parts) < 2 or parts[1].count('|') < 1:
        bot.reply_to(m, '⚠️ ব্যবহার: <code>/contact নাম|ফোন|ইমেইল</code>')
        return
    bits = parts[1].split('|')
    name = bits[0].strip()
    phone = bits[1].strip() if len(bits) > 1 else ''
    email = bits[2].strip() if len(bits) > 2 else ''
    vcard = f'BEGIN:VCARD\nVERSION:3.0\nFN:{name}\nTEL:{phone}\nEMAIL:{email}\nEND:VCARD'
    try:
        bot.send_photo(m.chat.id, qr_url(vcard), caption=f'👤 <b>{name}</b> কন্টাক্ট QR রেডি!\n\n⚡ Powered by @PyHost_CloudBot')
    except Exception:
        bot.reply_to(m, '⚠️ QR বানানো যায়নি।')

@bot.message_handler(func=lambda m: bool(m.text) and not m.text.startswith('/'))
def make_qr(m):
    lines = [l.strip() for l in m.text.splitlines() if l.strip()][:5]
    if not lines:
        return
    if len(lines) == 1:
        try:
            bot.send_photo(m.chat.id, qr_url(lines[0]), caption='✅ আপনার QR কোড রেডি!\n\n⚡ Powered by @PyHost_CloudBot')
        except Exception:
            bot.reply_to(m, '⚠️ QR বানানো যায়নি।')
        return
    for i, line in enumerate(lines, 1):
        try:
            bot.send_photo(m.chat.id, qr_url(line), caption=f'✅ QR {i}/{len(lines)}: {line[:50]}')
        except Exception:
            pass

bot.infinity_polling()
''',
    },

    "poll_bot": {
        "emoji": "📊", "name": "Quick Poll Bot", "proj_name": "Poll Bot",
        "category": '📊 Engagement', "level": '⭐⭐ Intermediate', "setup": '~20 sec',
        "features": ['Anonymous, Public ও Multiple-choice — তিন ধরনের পোল', '২–১০টা অপশন সাপোর্ট', 'নির্দিষ্ট সময়ে প্রতিদিন অটো পোল শিডিউল', 'গ্রুপ ও চ্যানেলে ব্যবহারযোগ্য'],
        "commands": '/poll /pollpublic /pollmulti /schedulepoll',
        "about": "সাধারণ, পাবলিক, মাল্টি-আনসার ও শিডিউলড — চার ধরনের পোল বানানো যায়।",
        "steps": [
            "বট সাথে সাথে ডিপ্লয় ও লাইভ হবে",
            "/poll প্রশ্ন | অপশন ১ | অপশন ২ — সাধারণ Anonymous পোল",
            "/pollpublic ... — সবার ভোট প্রকাশ্যে দেখা যাবে এমন পোল",
            "/pollmulti ... — একাধিক অপশনে ভোট দেওয়া যাবে এমন পোল",
            "/schedulepoll HH:MM | প্রশ্ন | অপশন ১ | অপশন ২ — নির্দিষ্ট সময়ে অটো পোল",
        ],
        "need": "শুধু বট টোকেন",
        "preview": "👤 /schedulepoll 20:00 | রাতে কী খাবো? | ভাত | বিরিয়ানি\n🤖 ✅ রাত ৮টায় অটো পোল চালু হবে!\n[৮টায়] 🤖 📊 রাতে কী খাবো?",
        "code": r'''import telebot, json, os, time, threading, datetime

BOT_TOKEN = '__TOKEN__'
bot = telebot.TeleBot(BOT_TOKEN, parse_mode='HTML')
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'scheduled_polls.json')
lock = threading.Lock()

def load():
    try:
        with open(DB, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return []

def save(items):
    with open(DB, 'w', encoding='utf-8') as f:
        json.dump(items, f)

USAGE = ('📊 <b>Quick Poll Bot</b>\n\n'
         '<code>/poll প্রশ্ন | অপশন ১ | অপশন ২</code> — Anonymous\n'
         '<code>/pollpublic ...</code> — Public (কে কাকে ভোট দিলো দেখা যাবে)\n'
         '<code>/pollmulti ...</code> — একাধিক অপশনে ভোট দেওয়া যাবে\n'
         '<code>/schedulepoll HH:MM | প্রশ্ন | অপশন ১ | অপশন ২</code> — নির্দিষ্ট সময়ে অটো পোল\n\n'
         '(২ থেকে ১০টা অপশন দেওয়া যাবে)\n\n⚡ Powered by @PyHost_CloudBot')

@bot.message_handler(commands=['start', 'help'])
def start(m):
    bot.reply_to(m, USAGE)

def parse_items(raw):
    items = [p.strip() for p in raw.split('|') if p.strip()]
    return items

def send_poll(chat_id, items, anonymous=True, multiple=False):
    if len(items) < 3 or len(items) > 11:
        return False
    try:
        bot.send_poll(chat_id, items[0][:300], [i[:100] for i in items[1:]], is_anonymous=anonymous, allows_multiple_answers=multiple)
        return True
    except Exception:
        return False

@bot.message_handler(commands=['poll'])
def poll_cmd(m):
    parts = m.text.split(None, 1)
    if len(parts) < 2 or not send_poll(m.chat.id, parse_items(parts[1])):
        bot.reply_to(m, '⚠️ একটা প্রশ্ন এবং ২-১০টা অপশন দিন।\n\n' + USAGE)

@bot.message_handler(commands=['pollpublic'])
def pollpublic_cmd(m):
    parts = m.text.split(None, 1)
    if len(parts) < 2 or not send_poll(m.chat.id, parse_items(parts[1]), anonymous=False):
        bot.reply_to(m, '⚠️ একটা প্রশ্ন এবং ২-১০টা অপশন দিন।\n\n' + USAGE)

@bot.message_handler(commands=['pollmulti'])
def pollmulti_cmd(m):
    parts = m.text.split(None, 1)
    if len(parts) < 2 or not send_poll(m.chat.id, parse_items(parts[1]), multiple=True):
        bot.reply_to(m, '⚠️ একটা প্রশ্ন এবং ২-১০টা অপশন দিন।\n\n' + USAGE)

@bot.message_handler(commands=['schedulepoll'])
def schedule_cmd(m):
    parts = m.text.split(None, 1)
    if len(parts) < 2 or '|' not in parts[1]:
        bot.reply_to(m, '⚠️ ব্যবহার: <code>/schedulepoll HH:MM | প্রশ্ন | অপশন ১ | অপশন ২</code>')
        return
    time_part, _, rest = parts[1].partition('|')
    time_part = time_part.strip()
    try:
        datetime.datetime.strptime(time_part, '%H:%M')
    except ValueError:
        bot.reply_to(m, '⚠️ সময় HH:MM ফরম্যাটে দিন (যেমন 20:00)')
        return
    items = parse_items(rest)
    if len(items) < 3:
        bot.reply_to(m, '⚠️ একটা প্রশ্ন এবং কমপক্ষে ২টা অপশন দিন।')
        return
    with lock:
        items_db = load()
        items_db.append({'chat_id': m.chat.id, 'time': time_part, 'items': items, 'fired_date': None})
        save(items_db)
    bot.reply_to(m, f'✅ <b>{time_part}</b>-এ অটো পোল শিডিউল করা হয়েছে!')

def scheduler_worker():
    while True:
        time.sleep(20)
        try:
            now = datetime.datetime.now()
            today = now.strftime('%Y-%m-%d')
            now_hm = now.strftime('%H:%M')
            with lock:
                items_db = load()
                changed = False
                for entry in items_db:
                    if entry['time'] == now_hm and entry.get('fired_date') != today:
                        if send_poll(entry['chat_id'], entry['items']):
                            entry['fired_date'] = today
                            changed = True
                if changed:
                    save(items_db)
        except Exception:
            pass

threading.Thread(target=scheduler_worker, daemon=True).start()
bot.infinity_polling()
''',
    },

    "weather_bot": {
        "emoji": "🌤️", "name": "Weather Info Bot", "proj_name": "Weather Bot",
        "category": '🌦️ Info & Lifestyle', "level": '⭐⭐ Intermediate', "setup": '~20 sec',
        "features": ['শহরের নাম লিখলেই লাইভ আবহাওয়া (তাপমাত্রা, আর্দ্রতা, বাতাস)', 'Location শেয়ার করলে সরাসরি সেই জায়গার আবহাওয়া', '৩ দিনের ফোরকাস্ট', 'প্রতিদিন সকাল ৮টায় অটো-আপডেট সাবস্ক্রিপশন'],
        "commands": '/subscribe <city> · /unsubscribe',
        "about": "শহরের নাম বা লোকেশন শেয়ার করলে আবহাওয়া জানায়, সাথে ৩ দিনের ফোরকাস্ট ও ডেইলি অটো-আপডেট সাবস্ক্রিপশন।",
        "steps": [
            "বট সাথে সাথে ডিপ্লয় ও লাইভ হবে",
            "শহরের নাম লিখুন (যেমন: Dhaka) — বর্তমান আবহাওয়া + ৩ দিনের ফোরকাস্ট পাবেন",
            "📎 → Location শেয়ার করলে সরাসরি নিজের জায়গার আবহাওয়া পাবেন",
            "/subscribe Dhaka — প্রতিদিন সকাল ৮টায় অটো আবহাওয়া পাবেন",
            "/unsubscribe — অটো-আপডেট বন্ধ করুন",
        ],
        "need": "শুধু বট টোকেন",
        "preview": "👤 Dhaka\n🤖 🌤️ Dhaka: এখন +৩১°C, আংশিক মেঘলা\n   📅 আগামী ৩ দিন: ৩২°/৩০°/২৯°",
        "code": r'''import telebot, requests, json, os, time, threading, datetime

BOT_TOKEN = '__TOKEN__'
bot = telebot.TeleBot(BOT_TOKEN, parse_mode='HTML')
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'weather_subs.json')
lock = threading.Lock()
SEND_HOUR = 8  # প্রতিদিন সকাল ৮টায় (সার্ভারের সময় অনুযায়ী)

def load():
    try:
        with open(DB, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}

def save(d):
    with open(DB, 'w', encoding='utf-8') as f:
        json.dump(d, f)

def fetch_weather(query):
    try:
        r = requests.get('https://wttr.in/' + requests.utils.quote(query), params={'format': 'j1'}, timeout=10, headers={'User-Agent': 'curl/8.0'})
        if r.status_code != 200:
            return None
        j = r.json()
        cur = j['current_condition'][0]
        line1 = f"🌤️ এখন: {cur['temp_C']}°C ({cur['weatherDesc'][0]['value']}), 💧 {cur['humidity']}%, 💨 {cur['windspeedKmph']}km/h"
        days = j.get('weather', [])[:3]
        fc = ' / '.join(d['maxtempC'] + '°' for d in days)
        line2 = f"📅 আগামী {len(days)} দিনের সর্বোচ্চ তাপমাত্রা: {fc}"
        area = j.get('nearest_area', [{}])[0]
        name = area.get('areaName', [{}])[0].get('value', query)
        return f'<b>{name}</b>\n{line1}\n{line2}'
    except Exception:
        return None

USAGE = ('🌤️ <b>Weather Info Bot</b>\n\n'
         'শহরের নাম লিখুন (ইংরেজিতে), যেমন: <code>Dhaka</code>\n'
         '📎 Location শেয়ার করলে সরাসরি সেই জায়গার আবহাওয়া\n'
         '<code>/subscribe শহর</code> — প্রতিদিন সকাল ৮টায় অটো-আপডেট\n'
         '<code>/unsubscribe</code> — বন্ধ করুন\n\n⚡ Powered by @PyHost_CloudBot')

@bot.message_handler(commands=['start', 'help'])
def start(m):
    bot.reply_to(m, USAGE)

@bot.message_handler(commands=['subscribe'])
def subscribe(m):
    parts = m.text.split(None, 1)
    if len(parts) < 2:
        bot.reply_to(m, '⚠️ ব্যবহার: <code>/subscribe Dhaka</code>')
        return
    city = parts[1].strip()
    with lock:
        d = load()
        d[str(m.chat.id)] = {'city': city, 'last_sent': None}
        save(d)
    bot.reply_to(m, f'✅ প্রতিদিন সকাল {SEND_HOUR}টায় <b>{city}</b>-এর আবহাওয়া পাবেন।')

@bot.message_handler(commands=['unsubscribe'])
def unsubscribe(m):
    with lock:
        d = load()
        if d.pop(str(m.chat.id), None) is not None:
            save(d)
            bot.reply_to(m, '✅ অটো-আপডেট বন্ধ করা হয়েছে।')
        else:
            bot.reply_to(m, 'আপনি কোনো সাবস্ক্রিপশনে নেই।')

@bot.message_handler(content_types=['location'])
def loc_weather(m):
    res = fetch_weather(f'{m.location.latitude},{m.location.longitude}')
    bot.reply_to(m, res if res else '⚠️ আবহাওয়ার তথ্য পাওয়া যায়নি।')

@bot.message_handler(func=lambda m: bool(m.text) and not m.text.startswith('/'))
def weather(m):
    city = m.text.strip()
    res = fetch_weather(city)
    bot.reply_to(m, res if res else '⚠️ আবহাওয়ার তথ্য পাওয়া যায়নি, শহরের নাম ঠিক আছে কিনা দেখুন।')

def daily_worker():
    while True:
        time.sleep(60)
        try:
            now = datetime.datetime.now()
            if now.hour != SEND_HOUR:
                continue
            today = now.strftime('%Y-%m-%d')
            with lock:
                d = load()
                changed = False
                for chat_id, info in d.items():
                    if info.get('last_sent') == today:
                        continue
                    res = fetch_weather(info['city'])
                    if res:
                        try:
                            bot.send_message(int(chat_id), '☀️ <b>আজকের আবহাওয়া</b>\n\n' + res)
                        except Exception:
                            pass
                    info['last_sent'] = today
                    changed = True
                if changed:
                    save(d)
        except Exception:
            pass

threading.Thread(target=daily_worker, daemon=True).start()
bot.infinity_polling()
''',
    },

    "reminder_bot": {
        "emoji": "⏰", "name": "Smart Reminder Bot", "proj_name": "Reminder Bot",
        "category": '⏰ Productivity', "level": '⭐⭐ Intermediate', "setup": '~30 sec',
        "features": ['সেকেন্ড/মিনিট/ঘণ্টা/দিন — যেকোনো হিসাবে রিমাইন্ডার', 'daily / weekly অটো-রিপিট', '/myreminders থেকে বাটনে ডিলিট', 'ব্যাকগ্রাউন্ড ওয়ার্কার সময় হলে নিজে থেকে মেসেজ পাঠায়'],
        "commands": '/remind 30m টেক্সট [daily|weekly] · /myreminders',
        "about": "একবারের বা প্রতিদিন/সপ্তাহে রিপিট হওয়া রিমাইন্ডার সেট করা যায়, লিস্ট দেখে ডিলিটও করা যায় — বট রিস্টার্ট হলেও হারায় না।",
        "steps": [
            "বট সাথে সাথে ডিপ্লয় ও লাইভ হবে",
            "/remind 30m চা খেতে হবে — একবারের রিমাইন্ডার (s/m/h/d)",
            "/remind 1d চা খেতে হবে daily — প্রতিদিন রিপিট হবে",
            "/remind 1d মিটিং weekly — প্রতি সপ্তাহে একই সময়ে রিপিট হবে",
            "/myreminders — সব রিমাইন্ডার দেখুন ও বাটনে চেপে ডিলিট করুন",
        ],
        "need": "শুধু বট টোকেন",
        "preview": "👤 /remind 1d ওষুধ খেতে হবে daily\n🤖 ✅ প্রতিদিন এই সময়ে মনে করিয়ে দেব!\n[পরদিন] 🤖 ⏰ রিমাইন্ডার! ওষুধ খেতে হবে",
        "code": r'''import telebot, json, os, time, threading, re
from telebot import types

BOT_TOKEN = '__TOKEN__'
bot = telebot.TeleBot(BOT_TOKEN, parse_mode='HTML')
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'reminders.json')
lock = threading.Lock()
UNITS = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
USAGE = ('⏰ <b>Smart Reminder Bot</b>\n\n'
         '<code>/remind 30m চা খেতে হবে</code> — একবারের রিমাইন্ডার\n'
         '<code>/remind 1d ওষুধ daily</code> — প্রতিদিন রিপিট\n'
         '<code>/remind 1d মিটিং weekly</code> — প্রতি সপ্তাহে রিপিট\n'
         '<code>/myreminders</code> — লিস্ট দেখুন ও ডিলিট করুন\n\n'
         's = সেকেন্ড, m = মিনিট, h = ঘণ্টা, d = দিন (সর্বোচ্চ ৩০ দিন গ্যাপ)\n\n⚡ Powered by @PyHost_CloudBot')

def load():
    try:
        with open(DB, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return []

def save(items):
    with open(DB, 'w', encoding='utf-8') as f:
        json.dump(items, f)

@bot.message_handler(commands=['start', 'help'])
def start(m):
    bot.reply_to(m, USAGE)

@bot.message_handler(commands=['remind'])
def remind(m):
    parts = m.text.split(None, 2)
    if len(parts) < 3:
        bot.reply_to(m, USAGE)
        return
    mt = re.fullmatch(r'(\d+)([smhd])', parts[1].lower())
    if not mt:
        bot.reply_to(m, USAGE)
        return
    secs = int(mt.group(1)) * UNITS[mt.group(2)]
    if secs < 5 or secs > 30 * 86400:
        bot.reply_to(m, '⚠️ সময় ৫ সেকেন্ড থেকে ৩০ দিনের মধ্যে হতে হবে।')
        return
    body = parts[2]
    repeat = None
    for tag in ('daily', 'weekly'):
        if body.lower().endswith(tag):
            repeat = tag
            body = body[: -len(tag)].strip()
            break
    interval = 86400 if repeat == 'daily' else (7 * 86400 if repeat == 'weekly' else None)
    with lock:
        items = load()
        rid = (max([i.get('id', 0) for i in items], default=0)) + 1
        items.append({'id': rid, 'chat': m.chat.id, 'ts': time.time() + secs, 'text': body[:500], 'repeat': repeat, 'interval': interval})
        save(items)
    tag_txt = f' ({repeat})' if repeat else ''
    bot.reply_to(m, f'✅ ঠিক আছে! {parts[1]} পরে মনে করিয়ে দেব{tag_txt}। (ID: {rid})')

@bot.message_handler(commands=['myreminders'])
def myreminders(m):
    items = [i for i in load() if i['chat'] == m.chat.id]
    if not items:
        bot.reply_to(m, 'আপনার কোনো সক্রিয় রিমাইন্ডার নেই।')
        return
    kb = types.InlineKeyboardMarkup(row_width=1)
    lines = []
    for i in items:
        when = time.strftime('%d %b, %H:%M', time.localtime(i['ts']))
        tag = f" ({i['repeat']})" if i.get('repeat') else ''
        lines.append(f"🆔 {i['id']} — {i['text'][:40]}{tag} — {when}")
        kb.add(types.InlineKeyboardButton(f"🗑️ Delete #{i['id']}", callback_data=f"delrem_{i['id']}"))
    bot.reply_to(m, '📋 <b>আপনার রিমাইন্ডার:</b>\n\n' + '\n'.join(lines), reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith('delrem_'))
def delrem(c):
    rid = int(c.data.replace('delrem_', ''))
    with lock:
        items = load()
        items = [i for i in items if not (i['id'] == rid and i['chat'] == c.message.chat.id)]
        save(items)
    bot.answer_callback_query(c.id, f'🗑️ রিমাইন্ডার #{rid} ডিলিট হয়েছে।')
    try:
        bot.edit_message_text('🗑️ ডিলিট করা হয়েছে।', c.message.chat.id, c.message.message_id)
    except Exception:
        pass

def worker():
    while True:
        time.sleep(3)
        try:
            with lock:
                items = load()
                now = time.time()
                due = [i for i in items if i['ts'] <= now]
                remaining = [i for i in items if i['ts'] > now]
                for i in due:
                    if i.get('repeat') and i.get('interval'):
                        i['ts'] = now + i['interval']
                        remaining.append(i)
                if due:
                    save(remaining)
            for i in due:
                try:
                    bot.send_message(i['chat'], '⏰ <b>রিমাইন্ডার!</b>\n\n' + i['text'])
                except Exception:
                    pass
        except Exception:
            pass

threading.Thread(target=worker, daemon=True).start()
bot.infinity_polling()
''',
    },

    "currency_converter": {
        "emoji": "💱", "name": "Currency Converter Bot", "proj_name": "Currency Converter",
        "category": '💱 Finance', "level": '⭐ Beginner', "setup": '~20 sec',
        "features": ['১৫০+ কারেন্সি লাইভ রেটে কনভার্ট', 'রেট দেখানো (1 USD = কত BDT) + ১০ মিনিট ক্যাশ — দ্রুত রেসপন্স', '🔁 Reverse বাটনে এক ট্যাপে উল্টো কনভার্ট', 'জনপ্রিয় কারেন্সির রেট-লিস্ট'],
        "commands": '/convert 100 USD BDT · /rates [কারেন্সি]',
        "about": "লাইভ এক্সচেঞ্জ রেটে যেকোনো দুই কারেন্সির মধ্যে কনভার্ট করে — ১৫০+ কারেন্সি, রেট-লিস্ট আর এক-ট্যাপে Reverse কনভার্ট সহ।",
        "steps": [
            "বট সাথে সাথে ডিপ্লয় ও লাইভ হবে",
            "লিখুন: /convert 100 USD BDT",
            "বট লাইভ এক্সচেঞ্জ রেট এনে হিসাব করে দেবে",
        ],
        "need": "শুধু বট টোকেন",
        "preview": "👤 /convert 100 USD BDT\n🤖 💱 100 USD = ১২,১৫০.০০ BDT",
        "code": r'''import telebot, requests, time
from telebot import types

BOT_TOKEN = '__TOKEN__'
bot = telebot.TeleBot(BOT_TOKEN, parse_mode='HTML')
POPULAR = ['USD', 'EUR', 'GBP', 'INR', 'SAR', 'AED', 'JPY', 'CNY']
_cache = {}  # base -> (timestamp, rates)

USAGE = (
    '💱 <b>Currency Converter Bot</b>\n'
    '━━━━━━━━━━━━━━━━━━\n'
    '🔹 <code>/convert 100 USD BDT</code> — কনভার্ট করুন\n'
    '🔹 <code>/rates</code> — 1 USD = কত? (জনপ্রিয় কারেন্সি)\n'
    '🔹 <code>/rates EUR</code> — যেকোনো বেস কারেন্সির রেট\n\n'
    '⚡ Powered by @PyHost_CloudBot'
)


def get_rates(base):
    base = base.upper()
    hit = _cache.get(base)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    r = requests.get('https://open.er-api.com/v6/latest/' + base, timeout=10)
    j = r.json()
    if j.get('result') != 'success':
        return None
    _cache[base] = (time.time(), j['rates'])
    return j['rates']


@bot.message_handler(commands=['start', 'help'])
def start(m):
    bot.reply_to(m, USAGE)


@bot.message_handler(commands=['convert'])
def convert(m):
    parts = m.text.split()
    if len(parts) != 4:
        bot.reply_to(m, USAGE)
        return
    try:
        amount = float(parts[1].replace(',', ''))
    except ValueError:
        bot.reply_to(m, '⚠️ পরিমাণ সংখ্যা হতে হবে, যেমন: <code>/convert 100 USD BDT</code>')
        return
    frm, to = parts[2].upper(), parts[3].upper()
    try:
        rates = get_rates(frm)
        if not rates or to not in rates:
            bot.reply_to(m, '⚠️ কারেন্সি কোড ঠিক আছে কিনা দেখুন (যেমন USD, BDT, EUR)।')
            return
        rate = rates[to]
        result = amount * rate
        mk = types.InlineKeyboardMarkup()
        mk.add(types.InlineKeyboardButton('🔁 Reverse', callback_data=f'rev|{amount:g}|{to}|{frm}'))
        bot.reply_to(
            m,
            f'💱 <b>{amount:,.2f} {frm}</b> = <b>{result:,.2f} {to}</b>\n'
            f'📈 Rate: 1 {frm} = {rate:,.4f} {to}\n'
            f'🕒 লাইভ রেট (ক্যাশ ১০ মিনিট)',
            reply_markup=mk,
        )
    except Exception:
        bot.reply_to(m, '⚠️ কনভার্ট করা যায়নি, একটু পরে চেষ্টা করুন।')


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith('rev|'))
def reverse(c):
    try:
        _, amt, frm, to = c.data.split('|')
        amount = float(amt)
        rates = get_rates(frm)
        result = amount * rates[to]
        bot.answer_callback_query(c.id)
        bot.send_message(c.message.chat.id, f'💱 <b>{amount:,.2f} {frm}</b> = <b>{result:,.2f} {to}</b>')
    except Exception:
        bot.answer_callback_query(c.id, '⚠️ কনভার্ট করা যায়নি', show_alert=True)


@bot.message_handler(commands=['rates'])
def rates_cmd(m):
    parts = m.text.split()
    base = parts[1].upper() if len(parts) > 1 else 'USD'
    try:
        rates = get_rates(base)
        if not rates:
            bot.reply_to(m, '⚠️ কারেন্সি কোড ঠিক নেই।')
            return
        lines = [f'📈 <b>1 {base} =</b>']
        for cur in ['BDT'] + POPULAR:
            if cur != base and cur in rates:
                lines.append(f'• {cur}: <b>{rates[cur]:,.4f}</b>')
        bot.reply_to(m, '\n'.join(lines))
    except Exception:
        bot.reply_to(m, '⚠️ রেট আনা যায়নি, একটু পরে চেষ্টা করুন।')


bot.infinity_polling()
''',
    },

    "quiz_bot": {
        "emoji": "🧠", "name": "Quiz Bot", "proj_name": "Quiz Bot",
        "category": '🎮 Fun & Learning', "level": '⭐ Beginner', "setup": '~20 sec',
        "features": ['টেলিগ্রামের নেটিভ Quiz Poll, সঠিক উত্তর অটো হাইলাইট', 'অপশন অটো এলোমেলো', 'এক মেসেজে একাধিক কুইজ (সর্বোচ্চ ১০টা)', 'ইউজার ভোট দিলে সাথে সাথে সঠিক/ভুল দেখায়'],
        "commands": '/quiz প্রশ্ন | সঠিক | ভুল ১ | ভুল ২',
        "about": "টেলিগ্রামের নিজস্ব Quiz Poll দিয়ে মাল্টিপল-চয়েস কুইজ তৈরি করে — সঠিক উত্তর অটো হাইলাইট হয়।",
        "steps": [
            "বট সাথে সাথে ডিপ্লয় ও লাইভ হবে",
            "লিখুন: /quiz প্রশ্ন | সঠিক উত্তর | ভুল ১ | ভুল ২",
            "বট একটা Quiz Poll পাঠাবে, অপশনগুলো এলোমেলো ক্রমে দেখাবে",
            "কেউ ভোট দিলে সাথে সাথে সঠিক/ভুল বুঝিয়ে দেবে",
        ],
        "need": "শুধু বট টোকেন",
        "preview": "👤 /quiz বাংলাদেশের রাজধানী কী? | ঢাকা | চট্টগ্রাম | রাজশাহী\n🤖 🧠 [Quiz Poll] বাংলাদেশের রাজধানী কী?",
        "code": r'''import telebot, random

BOT_TOKEN = '__TOKEN__'
bot = telebot.TeleBot(BOT_TOKEN, parse_mode='HTML')
USAGE = (
    '🧠 <b>Quiz Bot</b>\n'
    '━━━━━━━━━━━━━━━━━━\n'
    'ব্যবহার: <code>/quiz প্রশ্ন | সঠিক উত্তর | ভুল ১ | ভুল ২</code>\n\n'
    '💡 <b>টিপস:</b>\n'
    '• সর্বোচ্চ ৯টা ভুল অপশন দেওয়া যাবে\n'
    '• একসাথে একাধিক কুইজ: প্রতি লাইনে একটা করে /quiz লিখে এক মেসেজে পাঠান\n'
    '• অপশনগুলো অটো এলোমেলো হবে\n\n'
    '⚡ Powered by @PyHost_CloudBot'
)


def make_quiz(chat_id, body):
    bits = [b.strip() for b in body.split('|') if b.strip()]
    if len(bits) < 3:
        return False, 'ফরম্যাট ঠিক নেই (কমপক্ষে প্রশ্ন + সঠিক উত্তর + ১টা ভুল অপশন লাগবে)'
    question, correct, wrongs = bits[0], bits[1], bits[2:]
    options = [correct] + wrongs[:9]
    random.shuffle(options)
    correct_id = options.index(correct)
    try:
        bot.send_poll(chat_id, question[:300], [o[:100] for o in options], type='quiz',
                      correct_option_id=correct_id, is_anonymous=False)
        return True, ''
    except Exception as e:
        return False, str(e)[:150]


@bot.message_handler(commands=['start', 'help'])
def start(m):
    bot.reply_to(m, USAGE)


@bot.message_handler(commands=['quiz'])
def quiz(m):
    lines = [ln.strip() for ln in m.text.split('\n') if ln.strip()]
    bodies = []
    for ln in lines:
        if ln.startswith('/quiz'):
            bodies.append(ln.split(None, 1)[1] if len(ln.split(None, 1)) > 1 else '')
        elif bodies:
            bodies[-1] += ' ' + ln
    bodies = [b for b in bodies if b.count('|') >= 2]
    if not bodies:
        bot.reply_to(m, USAGE)
        return
    sent, errors = 0, []
    for b in bodies[:10]:
        ok, err = make_quiz(m.chat.id, b)
        if ok:
            sent += 1
        else:
            errors.append(err)
    if errors:
        bot.reply_to(m, f'✅ {sent}টা কুইজ পাঠানো হয়েছে।\n⚠️ {len(errors)}টা ব্যর্থ: ' + errors[0])


bot.infinity_polling()
''',
    },

    "url_shortener": {
        "emoji": "✂️", "name": "URL Shortener Bot", "proj_name": "URL Shortener",
        "category": '🧰 Utility', "level": '⭐ Beginner', "setup": '~20 sec',
        "features": ['লিংক পাঠালেই সাথে সাথে শর্ট লিংক', 'এক মেসেজে একাধিক লিংক (সর্বোচ্চ ৫টা)', 'কাস্টম শর্ট-নাম সাপোর্ট', 'ফেল করলে পরিষ্কার এরর মেসেজ'],
        "commands": '/short <url> [কাস্টম নাম]',
        "about": "লম্বা লিংক পাঠালেই সাথে সাথে ছোট শেয়ার-যোগ্য লিংক বানিয়ে দেয়।",
        "steps": [
            "বট সাথে সাথে ডিপ্লয় ও লাইভ হবে",
            "বটকে যেকোনো লম্বা লিংক পাঠান (http/https দিয়ে শুরু)",
            "বট সাথে সাথে ছোট লিংক ফেরত দেবে",
        ],
        "need": "শুধু বট টোকেন",
        "preview": "👤 https://example.com/a/very/long/path?x=1\n🤖 🔗 শর্ট লিংক: https://is.gd/abc123",
        "code": r'''import telebot, requests, re

BOT_TOKEN = '__TOKEN__'
bot = telebot.TeleBot(BOT_TOKEN, parse_mode='HTML')
URL_RE = re.compile(r'https?://\S+')
USAGE = (
    '✂️ <b>URL Shortener Bot</b>\n'
    '━━━━━━━━━━━━━━━━━━\n'
    '🔹 যেকোনো লম্বা লিংক পাঠান — সাথে সাথে শর্ট লিংক পাবেন\n'
    '🔹 একসাথে একাধিক লিংক দিলেও চলবে (সর্বোচ্চ ৫টা)\n'
    '🔹 কাস্টম নাম: <code>/short https://example.com mylink</code>\n\n'
    '⚡ Powered by @PyHost_CloudBot'
)


def shorten_url(url, alias=None):
    params = {'format': 'simple', 'url': url}
    if alias:
        params['shorturl'] = alias
    r = requests.get('https://is.gd/create.php', params=params, timeout=10)
    text = r.text.strip()
    if r.status_code == 200 and text.startswith('http'):
        return True, text
    return False, text[:150]


@bot.message_handler(commands=['start', 'help'])
def start(m):
    bot.reply_to(m, USAGE)


@bot.message_handler(commands=['short'])
def short_cmd(m):
    parts = m.text.split()
    if len(parts) < 2 or not parts[1].startswith(('http://', 'https://')):
        bot.reply_to(m, USAGE)
        return
    alias = parts[2] if len(parts) > 2 else None
    try:
        ok, res = shorten_url(parts[1], alias)
        if ok:
            bot.reply_to(m, f'🔗 <b>শর্ট লিংক:</b> {res}\n\n⚡ Powered by @PyHost_CloudBot')
        else:
            bot.reply_to(m, '⚠️ শর্ট করা যায়নি: ' + res)
    except Exception:
        bot.reply_to(m, '⚠️ শর্ট করা যায়নি, একটু পরে চেষ্টা করুন।')


@bot.message_handler(func=lambda m: bool(m.text) and bool(URL_RE.search(m.text)) and not m.text.startswith('/'))
def shorten(m):
    urls = URL_RE.findall(m.text)[:5]
    out = []
    for u in urls:
        try:
            ok, res = shorten_url(u)
            out.append(f'🔗 {res}' if ok else f'⚠️ {u[:40]} → {res}')
        except Exception:
            out.append(f'⚠️ {u[:40]} → ব্যর্থ')
    bot.reply_to(m, '\n'.join(out) + '\n\n⚡ Powered by @PyHost_CloudBot')


@bot.message_handler(func=lambda m: bool(m.text) and not m.text.startswith('/'))
def fallback(m):
    bot.reply_to(m, USAGE)


bot.infinity_polling()
''',
    },
}

MARKETPLACE_INTRO = (
    "🛒 <b>PyHost Bot Store</b>\n━━━━━━━━━━━━━━━━━━\n"
    "কোনো কোডিং ছাড়াই ১-ক্লিকে রেডিমেড বট ডেপ্লয় করুন!\n"
    "নিচের যেকোনো বট বেছে নিন:"
)

def build_marketplace_markup():
    markup = types.InlineKeyboardMarkup(row_width=2)
    buttons = [make_inline_button(f"{t['emoji']} {t['name']}", callback_data=f"mp_view_{key}", style="primary") for key, t in MARKETPLACE_TEMPLATES.items()]
    markup.add(*buttons)
    add_home_button(markup)
    return markup

def build_marketplace_card(key):
    t = MARKETPLACE_TEMPLATES[key]
    steps = "\n".join(f"{i}️⃣ {s}" for i, s in enumerate(t["steps"], 1))
    feats = "\n".join(f"  ✅ {f}" for f in t.get("features", []))
    msg = (
        f"{t['emoji']} <b>{t['name']}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🗂️ <b>Category:</b> {t.get('category', '🤖 Bot')}\n"
        f"🎚️ <b>Level:</b> {t.get('level', '⭐ Beginner')}   ⏱️ <b>Setup:</b> {t.get('setup', '~30 sec')}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 {t['about']}\n\n"
        + (f"✨ <b>Key Features:</b>\n{feats}\n\n" if feats else "")
        + (f"⌨️ <b>Commands:</b> <code>{html.escape(t['commands'])}</code>\n\n" if t.get("commands") else "")
        + f"🚀 <b>টোকেন দিলে যা হবে:</b>\n{steps}\n\n"
        f"🎬 <b>Demo Preview:</b>\n<pre>{html.escape(t['preview'])}</pre>\n"
        f"📌 <b>প্রয়োজন:</b> {t['need']}\n"
        "🔄 <i>ডিপ্লয়ের পর কোড আপনার File Manager থেকে এডিট করা যাবে।</i>"
    )
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(make_inline_button("🚀 Deploy Now", callback_data=f"deploy_tpl_{key}", style="success"))
    markup.add(
        make_inline_button("🔑 Get Bot Token", callback_data="faq_get_token", style="primary"),
        make_inline_button("◀️ Marketplace", callback_data="open_mp_menu", style="primary")
    )
    return msg, markup

def _renew_markup():
    m = types.InlineKeyboardMarkup(row_width=1)
    m.add(make_inline_button("💎 Renew Now", callback_data="open_upgrade_menu", style="success"))
    m.add(make_inline_button("🔙 Back to Home", callback_data="go_home", style="primary"))
    return m


# ----------------- বাটন হেল্পারস ও কিবোর্ড জেনারেটরস (All Buttons in English) -----------------
def make_reply_button(text, style="primary"):
    try:
        return make_kb(text, style=style)
    except Exception:
        return make_kb(text)

def make_inline_button(text, callback_data=None, url=None, style="primary"):
    text = bold_font(text)
    try:
        if url:
            return types.InlineKeyboardButton(text, url=url, style=style)
        return types.InlineKeyboardButton(text, callback_data=callback_data, style=style)
    except Exception:
        if url:
            return types.InlineKeyboardButton(text, url=url)
        return types.InlineKeyboardButton(text, callback_data=callback_data)

def add_home_button(markup, extra_text="🏠 Home Menu", extra_callback="go_home"):
    """যেকোনো ইনলাইন মেনুর নিচে একটা কুইক 'Home' বাটন জুড়ে দেয়, যাতে যেকোনো স্ক্রিন থেকে
    এক ট্যাপেই মূল হাব-এ ফিরে আসা যায় (নেভিগেশন সহজ করার জন্য, কোনো কিছু রিমুভ হয় না)।"""
    markup.add(make_inline_button(extra_text, callback_data=extra_callback, style="primary"))
    return markup

_BN_DIGIT_MAP = str.maketrans("0123456789", "০১২৩৪৫৬৭৮৯")
def to_bn_num(n) -> str:
    """সংখ্যাকে বাংলা সংখ্যায় (০-৯) রূপান্তর করে — যেমন 60 → ৬০।"""
    return str(n).translate(_BN_DIGIT_MAP)

def build_deploy_success_card(project_id: int, file_name: str, verify_token: bool = True):
    """ডিপ্লয়মেন্ট সফল কার্ড — আগে ৩ জায়গায় কপি-পেস্ট করা ছিল, এখন একটাই ফাংশনে।
    verify_token=False দিলে বট টোকেন Telegram-এ যাচাই করার নেটওয়ার্ক কলটা (যেটা কয়েক
    সেকেন্ড সময় নিতে পারে) স্কিপ করে সাথে সাথে কার্ড রিটার্ন করে — যাতে বট রান হওয়ার পর
    ইউজার সঙ্গে সঙ্গেই "Live" কার্ড দেখতে পায়। এরপর ব্যাকগ্রাউন্ডে যাচাই করে কার্ডটা
    আপডেট করে দেওয়া হয় (দেখুন refresh_deploy_card_async)।"""
    proj = admin.get_project_by_id(project_id)
    proj_name = proj["project_name"] if proj else file_name
    slot_no = proj.get("slot_number") if proj else None
    slot_label = f"Slot #{slot_no}" if slot_no else "Unassigned"

    # বট হ্যান্ডল (@username) — ডিপ্লয় করা ফাইলে টোকেন লেখা থাকলে দ্রুত (৪ সেকেন্ড টাইমআউট)
    # যাচাই করে দেখানো হয়, যাতে ধীর নেটওয়ার্কেও ইউজারকে বেশিক্ষণ অপেক্ষা করতে না হয়।
    bot_handle_line = "🔄 Connecting..."
    packages_line = "None Needed"
    session_line = "24/7 Unlimited Run"
    try:
        if proj:
            p_dir = os.path.join(PROJECTS_DIR, f"{proj['user_id']}_{project_id}")
            file_path = os.path.join(p_dir, file_name)
            if file_name.endswith(".py") and os.path.exists(file_path):
                if verify_token:
                    with open(file_path, "rb") as f:
                        code_bytes = f.read()
                    token = extract_bot_token_from_code(code_bytes)
                    if token:
                        is_valid, uname_or_err = validate_bot_token(token, timeout=4)
                        bot_handle_line = f"{uname_or_err} 🟢" if is_valid else "⚠️ Unknown (couldn't verify)"
                pkgs = scan_python_imports(file_path)
                packages_line = "Auto-Installed (OK)" if pkgs else "None Needed"

            u_data = admin.get_user(proj["user_id"])
            slot_status = admin.get_effective_slot_status(u_data, proj)
            if slot_status["is_24_7"]:
                session_line = "24/7 Unlimited Run"
            else:
                session_line = f"🆓 {admin.SYSTEM_CONFIG.get('free_hosting_hours', 12)} Hours Free Session"
    except Exception:
        pass

    ram_mb = admin.SYSTEM_CONFIG.get("max_ram_mb_per_process", 256)

    text = (
        "╭━━〔 🚀 DEPLOYMENT SUCCESS 〕━━╮\n"
        "│ ⚡ Status: 🟢 Live & Running\n"
        f"│ 🏷️ Bot Name: {html.escape(proj_name)}\n"
        f"│ 🔢 Active Slot: {slot_label}\n"
        f"│ 🤖 Bot Handle: {bot_handle_line}\n"
        f"│ 📄 Main File: <code>{html.escape(file_name)}</code>\n"
        f"│ 📦 Libraries: {packages_line}\n"
        f"│ 🧠 RAM Limit: {ram_mb} MB\n"
        f"│ ⏱️ Session: {session_line}\n"
        "│ 🛡️ Watchdog: Crash-Protection Active\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━━━╯\n"
        "💡 কোনো এরর হলে ➜ View Logs চেক করুন\n\n"
        "👇 বটটি পরিচালনা করতে নিচের\n"
        f"🎛️ {bold_font('Control Panel')} ব্যবহার করুন:"
    )
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        make_inline_button("🎛️ Control Panel", callback_data=f"manage_bot_{project_id}", style="primary"),
        make_inline_button("📜 View Logs", callback_data=f"bot_logs_{project_id}", style="primary")
    )
    markup.add(make_inline_button("⏹️ Stop Bot", callback_data=f"bot_stop_{project_id}", style="danger"))
    markup.add(make_inline_button("🔙 Back to Control Panel", callback_data="back_to_cp", style="primary"))
    return text, markup

def refresh_deploy_card_async(chat_id, message_id, project_id: int, file_name: str):
    """ব্যাকগ্রাউন্ড থ্রেডে বট টোকেন যাচাই করে ডিপ্লয়মেন্ট কার্ডের 'Bot Handle' লাইনটা
    পরে আপডেট করে দেয় — যাতে টোকেন যাচাইয়ের নেটওয়ার্ক কলের জন্য ইউজারকে বট রান হওয়ার পর
    বাড়তি সময় অপেক্ষা করতে না হয়।"""
    def _worker():
        try:
            text, markup = build_deploy_success_card(project_id, file_name, verify_token=True)
            bot.edit_message_text(text, chat_id, message_id, reply_markup=markup, parse_mode="HTML")
        except Exception:
            pass
    threading.Thread(target=_worker, daemon=True).start()

# ----------------- GitHub Deploy (VIP-exclusive) -----------------
def sync_github_repo(user_id: int, pid: int, chat_id, status_message_id, token: str = None):
    """ইতিমধ্যে GitHub থেকে ডিপ্লয় করা একটি প্রজেক্টকে রিপোর লেটেস্ট কমিট দিয়ে আপডেট করে।
    পুরনো ফাইলগুলো রাখা হয় না — রিপোর নতুন ভার্সন দিয়ে সম্পূর্ণ রিপ্লেস হয়।
    বট চলমান থাকলে প্রথমে বন্ধ করা হয়, sync শেষে আবার চালু করা হয়।"""

    def _edit(txt):
        try:
            bot.edit_message_text(txt, chat_id, status_message_id, parse_mode="HTML")
        except Exception:
            try:
                bot.send_message(chat_id, txt, parse_mode="HTML")
            except Exception:
                pass

    gh_info = admin.get_project_github_info(pid)
    if not gh_info:
        _edit("❌ <b>এই বটের GitHub তথ্য পাওয়া যায়নি!</b> শুধু GitHub Deploy দিয়ে তৈরি বটেই Sync কাজ করে।")
        return

    owner, repo = gh_info["owner"], gh_info["repo"]
    is_private = gh_info["is_private"]
    project = admin.get_project_by_id(pid)
    if not project:
        _edit("❌ <b>প্রজেক্ট খুঁজে পাওয়া যায়নি।</b>")
        return

    p_dir = os.path.join(PROJECTS_DIR, f"{user_id}_{pid}")
    p_key = f"{user_id}_{pid}"

    # চলমান বট বন্ধ করা
    if p_key in active_processes:
        try:
            active_processes[p_key]["process"].terminate()
            active_processes[p_key]["log_handle"].close()
        except Exception:
            pass
        del active_processes[p_key]
        admin.update_project_status(pid, "STOPPED")

    _edit(f"🔄 <b>GitHub Sync শুরু হচ্ছে...</b>\n📦 <code>{html.escape(owner)}/{html.escape(repo)}</code> থেকে লেটেস্ট ভার্সন নামানো হচ্ছে...")

    # GitHub API headers
    headers = {"User-Agent": "PyHostCloud-Bot", "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"token {token}"

    # Default branch বের করা
    try:
        req = urllib.request.Request(f"https://api.github.com/repos/{owner}/{repo}", headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            repo_info = json.loads(resp.read().decode("utf-8"))
        default_branch = repo_info.get("default_branch") or "main"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            _edit("❌ <b>GitHub অ্যাক্সেস প্রত্যাখ্যাত!</b> Private repo হলে নতুন Token দিয়ে আবার Sync করুন।")
        elif e.code == 404:
            _edit("❌ <b>রিপো পাওয়া যায়নি!</b> হয়তো রিপোটি ডিলিট/private করা হয়েছে।")
        else:
            _edit(f"❌ <b>GitHub API এরর:</b> HTTP {e.code}")
        return
    except Exception as ex:
        _edit(f"❌ <b>সংযোগ সমস্যা:</b> <code>{html.escape(str(ex)[:150])}</code>")
        return

    # Zipball ডাউনলোড
    try:
        zip_req = urllib.request.Request(
            f"https://api.github.com/repos/{owner}/{repo}/zipball/{default_branch}",
            headers=headers
        )
        with urllib.request.urlopen(zip_req, timeout=90) as resp:
            zip_bytes = resp.read()
    except Exception as ex:
        _edit(f"❌ <b>ডাউনলোড ব্যর্থ:</b> <code>{html.escape(str(ex)[:150])}</code>")
        return

    _edit("📦 <b>ডাউনলোড সম্পন্ন! পুরনো ফাইল রিপ্লেস হচ্ছে...</b>")

    # পুরনো ফাইল পরিষ্কার করা (requirements.txt ও .env রেখে বাকি সব রিপ্লেস)
    keep_files = {".env", ".env.local", "local_config.py"}
    saved = {}
    for kf in keep_files:
        kp = os.path.join(p_dir, kf)
        if os.path.exists(kp):
            with open(kp, "rb") as f:
                saved[kf] = f.read()

    # পুরনো ফোল্ডার পরিষ্কার
    if os.path.exists(p_dir):
        shutil.rmtree(p_dir, ignore_errors=True)
    os.makedirs(p_dir, exist_ok=True)

    # সংরক্ষিত ফাইল ফিরিয়ে দেওয়া
    for kf, kdata in saved.items():
        with open(os.path.join(p_dir, kf), "wb") as f:
            f.write(kdata)

    # নতুন ফাইল এক্সট্র্যাক্ট
    tmp_zip = os.path.join(p_dir, "__sync__.zip")
    try:
        with open(tmp_zip, "wb") as f:
            f.write(zip_bytes)
        safe_extract_zip(tmp_zip, p_dir)
    finally:
        if os.path.exists(tmp_zip):
            os.remove(tmp_zip)

    # GitHub-এর wrapper ফোল্ডার flatten করা
    entries = [e for e in os.listdir(p_dir) if e not in keep_files]
    if len(entries) == 1 and os.path.isdir(os.path.join(p_dir, entries[0])):
        wrapper_dir = os.path.join(p_dir, entries[0])
        for item in os.listdir(wrapper_dir):
            dest = os.path.join(p_dir, item)
            if os.path.exists(dest):
                if os.path.isdir(dest):
                    shutil.rmtree(dest)
                else:
                    os.remove(dest)
            shutil.move(os.path.join(wrapper_dir, item), dest)
        shutil.rmtree(wrapper_dir, ignore_errors=True)

    py_files = [f for f in os.listdir(p_dir) if f.endswith(".py")]
    if not py_files:
        _edit("❌ <b>রিপোতে কোনো .py ফাইল পাওয়া যায়নি!</b> Sync বাতিল — আগের ফাইল হারিয়ে গেছে। নতুন করে Deploy করুন।")
        return

    req_file = os.path.join(p_dir, "requirements.txt")
    if os.path.exists(req_file):
        _edit("📦 <b>requirements.txt থেকে নতুন লাইব্রেরি ইনস্টল হচ্ছে...</b>")
        pip_install_requirements(req_file)

    main_f = project["main_file"]
    if main_f not in py_files:
        main_f = "main.py" if "main.py" in py_files else ("bot.py" if "bot.py" in py_files else py_files[0])
        admin.update_project_main_file(pid, main_f)

    detected = scan_python_imports(os.path.join(p_dir, main_f))
    if detected:
        install_missing_packages_sync(detected)

    ok_run, run_msg = execute_project_script(user_id, pid, main_f)
    # GitHub তথ্য আপডেট (token পরিবর্তন হলে)
    admin.set_project_github_info(pid, owner, repo, is_private)

    if not ok_run:
        _edit(f"⚠️ <b>কোড সিঙ্ক হয়েছে কিন্তু বট চালু করা যায়নি!</b>\n━━━━━━━━━━━━━━━━━━━━━━\n{run_msg}")
        return

    _edit(
        f"✅ <b>GitHub Sync সফল হয়েছে!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 <b>Repo:</b> <code>{html.escape(owner)}/{html.escape(repo)}</code>\n"
        f"🌿 <b>Branch:</b> <code>{default_branch}</code>\n"
        f"📄 <b>Main File:</b> <code>{main_f}</code>\n"
        "🟢 <b>বট চালু হয়েছে!</b>"
    )


def deploy_github_repo(user_id: int, chat_id, status_message_id, owner: str, repo: str, token: str = None, is_private: bool = False):
    """ব্যাকগ্রাউন্ড থ্রেডে চলে — GitHub রিপো (Public/Private) ডাউনলোড, নিরাপদে এক্সট্র্যাক্ট ও
    ডিপ্লয় করে। কোনো ধাপে সমস্যা হলে ইউজারকে স্পষ্ট বাংলা মেসেজে জানানো হয় এবং আংশিক তৈরি হওয়া
    প্রজেক্ট থাকলে সেটা ক্লিনআপ করে দেওয়া হয় (যাতে "ভুতুড়ে" খালি স্লট না থেকে যায়)। সফল হলে
    সব অ্যাডমিনকে একটা সাইলেন্ট নোটিফিকেশন পাঠানো হয় (টোকেন ছাড়া)।"""

    def _edit(txt, markup=None):
        try:
            bot.edit_message_text(txt, chat_id, status_message_id, reply_markup=markup, parse_mode="HTML")
        except Exception:
            try:
                bot.send_message(chat_id, txt, reply_markup=markup, parse_mode="HTML")
            except Exception:
                pass

    headers = {"User-Agent": "PyHostCloud-Bot", "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"token {token}"

    # ১. রিপো ভেরিফাই ও ডিফল্ট ব্রাঞ্চ বের করা
    try:
        req = urllib.request.Request(f"https://api.github.com/repos/{owner}/{repo}", headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            repo_info = json.loads(resp.read().decode("utf-8"))
        default_branch = repo_info.get("default_branch") or "main"
    except urllib.error.HTTPError as e:
        if e.code == 404:
            _edit("❌ <b>রিপো পাওয়া যায়নি!</b>\nলিংকটা চেক করুন — অথবা এটা Private হলে সঠিক GitHub Token দিয়ে আবার চেষ্টা করুন।")
        elif e.code in (401, 403):
            _edit("❌ <b>অ্যাক্সেস প্রত্যাখ্যাত!</b>\nটোকেন ভুল/মেয়াদোত্তীর্ণ, অথবা GitHub rate-limit এ পড়েছে। একটু পর আবার চেষ্টা করুন।")
        else:
            _edit(f"❌ <b>GitHub এরর:</b> HTTP {e.code}")
        return
    except Exception as e:
        _edit(f"❌ <b>রিপো খুঁজে পেতে সমস্যা হয়েছে:</b> <code>{html.escape(str(e)[:200])}</code>")
        return

    # ২. জিপবল ডাউনলোড
    try:
        zip_req = urllib.request.Request(f"https://api.github.com/repos/{owner}/{repo}/zipball/{default_branch}", headers=headers)
        with urllib.request.urlopen(zip_req, timeout=90) as resp:
            zip_bytes = resp.read()
    except Exception as e:
        _edit(f"❌ <b>রিপো ডাউনলোড ব্যর্থ হয়েছে:</b> <code>{html.escape(str(e)[:200])}</code>")
        return

    _edit(f"📦 <b>'{html.escape(repo)}' ডাউনলোড সম্পন্ন! আনপ্যাক ও এনভায়রনমেন্ট সেটআপ হচ্ছে...</b>")

    # ৩. প্রজেক্ট স্লট রিজার্ভ ও এক্সট্র্যাক্ট (নিরাপদ zip-slip-protected এক্সট্র্যাক্টর রিইউজ করা হচ্ছে)
    pid = admin.create_user_project(user_id, repo[:30])
    p_dir = os.path.join(PROJECTS_DIR, f"{user_id}_{pid}")
    os.makedirs(p_dir, exist_ok=True)

    tmp_zip_path = os.path.join(p_dir, "__gh_download__.zip")
    try:
        with open(tmp_zip_path, "wb") as f:
            f.write(zip_bytes)
        safe_extract_zip(tmp_zip_path, p_dir)
    finally:
        if os.path.exists(tmp_zip_path):
            os.remove(tmp_zip_path)

    # GitHub zipball সবসময় একটা একক টপ-লেভেল ফোল্ডারে (owner-repo-<sha>) ফাইলগুলো দেয় —
    # সেটা ফ্ল্যাটেন করে প্রজেক্ট ফোল্ডারের রুটে নিয়ে আসা হচ্ছে
    entries = os.listdir(p_dir)
    if len(entries) == 1 and os.path.isdir(os.path.join(p_dir, entries[0])):
        wrapper_dir = os.path.join(p_dir, entries[0])
        for item in os.listdir(wrapper_dir):
            shutil.move(os.path.join(wrapper_dir, item), os.path.join(p_dir, item))
        shutil.rmtree(wrapper_dir, ignore_errors=True)

    # ৪. .py ফাইল না থাকলে ডিপ্লয় বাতিল ও ক্লিনআপ (ভুতুড়ে খালি স্লট এড়াতে)
    py_files = [f for f in os.listdir(p_dir) if f.endswith(".py")]
    if not py_files:
        admin.delete_user_project(pid)
        _edit("❌ <b>এই রিপোতে কোনো .py ফাইল পাওয়া যায়নি!</b> ডিপ্লয় বাতিল করা হয়েছে, কোনো স্লট নষ্ট হয়নি।")
        return

    req_file = os.path.join(p_dir, "requirements.txt")
    if os.path.exists(req_file):
        _edit("📦 <b>requirements.txt থেকে লাইব্রেরি ইনস্টল হচ্ছে...</b>")
        pip_install_requirements(req_file)

    main_f = "main.py" if "main.py" in py_files else ("bot.py" if "bot.py" in py_files else py_files[0])
    detected = scan_python_imports(os.path.join(p_dir, main_f))
    if detected:
        install_missing_packages_sync(detected)

    ok_run, run_msg = execute_project_script(user_id, pid, main_f)
    if not ok_run:
        admin.delete_user_project(pid)   # ফ্রি স্লট নষ্ট না হয় সেজন্য বাতিল করা হলো
        _edit(f"{run_msg}\n\n(কোনো স্লট নষ্ট হয়নি — রিপো ডাউনলোড হয়েছিল কিন্তু ডিপ্লয় বাতিল করা হয়েছে।)")
        return

    # GitHub রিপো তথ্য সেভ করা হচ্ছে — পরে "🔄 GitHub Sync" বাটনে কাজে লাগবে
    admin.set_project_github_info(pid, owner, repo, is_private)
    success_card, markup = build_deploy_success_card(pid, main_f, verify_token=False)
    _edit(success_card, markup)
    refresh_deploy_card_async(chat_id, status_message_id, pid, main_f)

    # ৫. অ্যাডমিনদের সাইলেন্ট নোটিফিকেশন (টোকেন কখনোই এতে থাকবে না)
    u_data = admin.get_user(user_id)
    notify_text = (
        "🐙 <b>New GitHub Deploy!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <b>User ID:</b> <code>{user_id}</code>\n"
        f"📦 <b>Repo:</b> <code>{html.escape(owner)}/{html.escape(repo)}</code> ({'🔒 Private' if is_private else '🔓 Public'})\n"
        f"🏷️ <b>Plan:</b> <code>{u_data.get('plan', 'FREE')}</code>\n"
        f"🕒 <b>Time:</b> <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )
    for aid in admin.ADMIN_IDS:
        try:
            bot.send_message(aid, notify_text, parse_mode="HTML")
        except Exception:
            pass

def get_user_keyboard(user_id=None):
    """মূল ইউজার মেনু — মূল বাটনগুলো (Control Panel, File Manager, My Account, Upgrade Plan,
    GitHub Deploy, Documentation, Support Hub) সামনে থাকবে। Marketplace এখন 🐙 GitHub Deploy
    স্ক্রিনের ভিতরে, আর Redeem Code এখন 💎 Upgrade Plan স্ক্রিনের ভিতরে (ও /redeem কমান্ডে)।"""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    b1 = make_reply_button("🎛️ Control Panel", style="success")
    b2 = make_reply_button("📁 File Manager", style="primary")
    b3 = make_reply_button("👤 My Account", style="primary")
    b4 = make_reply_button("💎 Upgrade Plan", style="primary")
    b6_gh = make_reply_button("🐙 GitHub Deploy", style="success")
    b6 = make_reply_button("📖 Documentation", style="primary")
    b7 = make_reply_button("📡 Support Hub", style="primary")

    # GitHub Deploy সবসময় নিচের সারির ডান পাশে থাকবে
    markup.row(b1, b2)
    markup.row(b3, b4)
    if user_id and admin.is_admin(user_id):
        b_admin = make_reply_button("👑 Admin Panel", style="danger")
        markup.row(b6, b7)
        markup.row(b_admin, b6_gh)
    else:
        markup.row(b7)
        markup.row(b6, b6_gh)
    return markup

def get_admin_reply_keyboard():
    """অ্যাডমিন মেইন কিবোর্ড — ৪টা ক্যাটাগরি বাটন, প্রতিটার ভেতরে সাব-মেনু (Analytics এখন মনিটরিং-এর ভেতরে)"""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        make_kb("📊 Monitoring"),
        make_kb("👥 User Manager"),
    )
    markup.add(
        make_kb("💰 Billing & Plans"),
        make_kb("⚙️ System"),
    )
    markup.add(make_kb("🔙 User Menu"))
    return markup

def get_admin_monitoring_keyboard():
    """মনিটরিং সাব-মেনু"""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        make_kb("📊 Live Telemetry"),
        make_kb("🤖 Process Tracker"),
    )
    markup.add(
        make_kb("🔄 Restart All Bots"),
        make_kb("⏹ Stop All Bots"),
    )
    markup.add(make_kb("📈 Analytics"))
    markup.add(make_kb("🔙 Admin Menu"))
    return markup

def get_admin_user_keyboard():
    """ইউজার ম্যানেজ সাব-মেনু"""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        make_kb("👥 User Directory"),
        make_kb("👤 User Management"),
    )
    markup.add(
        make_kb("📢 Global Broadcast"),
        make_kb("🚫 Ban / Security"),
    )
    markup.add(
        make_kb("🗓️ Schedule Broadcast"),
        make_kb("📋 Pending Requests"),
    )
    markup.add(make_kb("🎫 Support Tickets"))
    markup.add(make_kb("🔙 Admin Menu"))
    return markup

def get_admin_billing_keyboard():
    """বিলিং সাব-মেনু"""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        make_kb("💎 Grant VIP (Manual)"),
        make_kb("🎟️ Coupon Manager"),
    )
    markup.add(make_kb("🎁 Redeem Codes"))
    markup.add(make_kb("🔙 Admin Menu"))
    return markup

def get_admin_system_keyboard():
    """সিস্টেম সাব-মেনু"""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        make_kb("💾 Database Backup"),
        make_kb("🧹 Server Cleanup"),
    )
    markup.add(
        make_kb("⚙️ Maintenance Mode"),
        make_kb("📤 Bot Export"),
    )
    markup.add(make_kb("🆓 Free Hosting (12h)"))
    markup.add(make_kb("🔙 Admin Menu"))
    return markup


def get_multi_bot_control_panel(user_id: int):
    projects = admin.get_user_projects(user_id)
    u_data = admin.get_user(user_id)
    plan = u_data.get("plan", "FREE").upper()
    max_bots = admin.get_max_bot_limit_for_user(u_data)
    free_hosting_on = admin.SYSTEM_CONFIG.get("free_hosting_enabled", True)
    free_run_label = f"{admin.SYSTEM_CONFIG.get('free_hosting_hours', 12)} Hours/Run"

    slot_map = {}
    for p in projects:
        sn = p.get("slot_number")
        if sn:
            slot_map[sn] = p

    running_count = 0
    for p in projects:
        p_key = f"{user_id}_{p['project_id']}"
        if p_key in active_processes and active_processes[p_key]["process"].poll() is None:
            running_count += 1

    total_count = len(projects)
    stopped_count = total_count - running_count

    text = (
        "🎛️ <b>PYHOST MULTI-BOT CONTROL PANEL</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>প্ল্যান:</b> <code>{plan}</code> ({'24/7 Unlimited' if plan != 'FREE' else (free_run_label if free_hosting_on else '🔒 Locked (Off)')})\n"
        f"📦 <b>মোট বট স্লট:</b> <code>{total_count}/{max_bots}</code> টি\n"
        f"├ 🟢 <b>সক্রিয় বট:</b> <code>{running_count}</code> টি\n"
        f"└ 🔴 <b>বন্ধ বট:</b> <code>{stopped_count}</code> টি\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "👇 <b>যেকোনো স্লটে ট্যাপ করে ম্যানেজ, আপডেট অথবা মেয়াদ বৃদ্ধি করুন:</b>"
    )

    markup = types.InlineKeyboardMarkup(row_width=2)
    slot_buttons = []
    for slot_no in range(1, admin.MAX_BOT_SLOTS + 1):
        p = slot_map.get(slot_no)
        if p:
            pid = p["project_id"]
            p_key = f"{user_id}_{pid}"
            is_run = p_key in active_processes and active_processes[p_key]["process"].poll() is None
            slot_status = admin.get_effective_slot_status(u_data, p)
            is_expired = slot_status["is_expired"]
            is_covered = slot_status["is_24_7"]
            pinfo = admin.get_plan_display_info(u_data)

            if is_expired:
                icon = "⛔"
                state_label = "Expired"
            elif is_covered and slot_status["source"] == "PLAN":
                icon = pinfo["icon"]
                state_label = f"{pinfo['display_name']} • {'Running' if is_run else 'Stopped'}"
            elif is_covered:
                icon = "💎"
                state_label = "Extended • Running" if is_run else "Extended • Stopped"
            elif not free_hosting_on:
                icon = "🔒"
                state_label = "Locked (Free Off)"
            elif is_run:
                icon = "🟢"
                state_label = "Running"
            else:
                icon = "🔴"
                state_label = "Stopped"

            btn_text = f"{icon} Slot {slot_no} ({state_label})"
            slot_buttons.append(make_inline_button(btn_text, callback_data=f"manage_bot_{pid}", style="primary"))
        elif slot_no <= max_bots:
            if admin.is_vip_active(u_data):
                slot_buttons.append(make_inline_button(f"💎 Slot {slot_no} (VIP)", callback_data=f"slot_new_{slot_no}", style="success"))
            else:
                slot_buttons.append(make_inline_button(f"🆓 Slot {slot_no} (Free)", callback_data=f"slot_new_{slot_no}", style="success"))
        else:
            slot_buttons.append(make_inline_button(f"🔒 Slot {slot_no} (Locked)", callback_data="slot_locked", style="danger"))

    for i in range(0, len(slot_buttons), 2):
        pair = slot_buttons[i:i + 2]
        markup.add(*pair)

    add_home_button(markup)
    return text, markup

def get_single_bot_management_markup(user_id: int, project_id: int):
    project = admin.get_project_by_id(project_id)
    u_data = admin.get_user(user_id)
    if project and is_slot_locked(u_data, project):
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(make_inline_button("💎 Upgrade Plan (আনলক করুন)", callback_data="open_upgrade_menu", style="success"))
        markup.add(make_inline_button("🔙 Back to Control Panel", callback_data="back_to_cp", style="primary"))
        return markup

    p_key = f"{user_id}_{project_id}"
    is_running = p_key in active_processes and active_processes[p_key]["process"].poll() is None
    # GitHub Sync বাটন শুধু তখনই দেখাবে যখন বটটি GitHub থেকে ডিপ্লয় করা হয়েছে
    gh_info = admin.get_project_github_info(project_id)

    markup = types.InlineKeyboardMarkup(row_width=2)
    if is_running:
        markup.add(
            make_inline_button("⏹️ Stop Bot", callback_data=f"bot_stop_{project_id}", style="danger"),
            make_inline_button("🔄 Restart Bot", callback_data=f"bot_restart_{project_id}", style="primary")
        )
    else:
        markup.add(
            make_inline_button("▶️ Start Bot", callback_data=f"bot_start_{project_id}", style="success")
        )

    markup.add(
        make_inline_button("📜 View Logs", callback_data=f"bot_logs_{project_id}", style="primary"),
        make_inline_button("📤 Update / Upload File", callback_data=f"bot_update_{project_id}", style="primary")
    )
    markup.add(
        make_inline_button("✏️ Rename Main File", callback_data=f"bot_rename_{project_id}", style="primary")
    )
    # GitHub Sync — শুধু GitHub Deploy করা বটে দেখাবে
    if gh_info:
        markup.add(
            make_inline_button(
                f"🐙 GitHub Sync ({html.escape(gh_info['repo'])})",
                callback_data=f"gh_sync_{project_id}",
                style="success"
            )
        )
    markup.add(
        make_inline_button("🏥 Health Check", callback_data=f"bot_health_{project_id}", style="primary"),
        make_inline_button("⏳ Extend Validity", callback_data=f"bot_extend_{project_id}", style="success")
    )
    # ⭐ FIX: Download Backup, .env Manager, Version History, Clone Bot, Change Token ও
    # Scheduled Start/Stop — এই ৬টি বাটন আলাদা আলাদা না দেখিয়ে একটি "🧰 More Tools" বাটনের
    # ভিতরে একত্র করা হয়েছে (স্ক্রিনশটে গোল দাগ দেওয়া বাটনগুলো)। এতে মূল ড্যাশবোর্ড ছোট ও
    # পরিষ্কার থাকবে, ট্যাপ করলে সাব-মেনুতে সবগুলো অপশন দেখা যাবে।
    markup.add(
        make_inline_button("🧰 More Tools", callback_data=f"bot_more_tools_{project_id}", style="primary")
    )
    markup.add(
        make_inline_button("🗑️ Delete Bot", callback_data=f"bot_delete_confirm_{project_id}", style="danger")
    )
    markup.add(make_inline_button("🔙 Back to Control Panel", callback_data="back_to_cp", style="primary"))
    return markup

def get_bot_more_tools_markup(project_id: int):
    """⭐ FIX: Download Backup, .env Manager, Version History, Clone Bot, Change Token,
    Scheduled Start/Stop — এই ৬টি বাটনের সাব-মেনু (স্ক্রিনশটে গোল দাগ দেওয়া বাটনগুলো একত্রে)।"""
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        make_inline_button("📦 Download Backup", callback_data=f"fm_zip_{project_id}", style="primary"),
        make_inline_button("🔧 .env Manager", callback_data=f"env_mgr_{project_id}", style="primary")
    )
    markup.add(
        make_inline_button("📚 Version History", callback_data=f"ver_hist_{project_id}", style="primary"),
        make_inline_button("📋 Clone Bot", callback_data=f"bot_clone_{project_id}", style="success")
    )
    markup.add(
        make_inline_button("🔑 Change Token", callback_data=f"bot_token_{project_id}", style="primary"),
        make_inline_button("⏰ Scheduled Start/Stop", callback_data=f"bot_schedule_{project_id}", style="primary")
    )
    markup.add(make_inline_button("🔙 Bot Dashboard", callback_data=f"manage_bot_{project_id}", style="primary"))
    return markup

def get_file_manager_dashboard(user_id: int, project_id: int):
    project = admin.get_project_by_id(project_id)
    files, total_size = admin.get_project_files_info(user_id, project_id)
    
    file_lines = []
    if files:
        for f in files[:15]:
            sz_kb = max(1, f["size"] // 1024)
            icon = "⭐" if f["name"] == project.get("main_file", "main.py") else "📄"
            file_lines.append(f"{icon} <code>{f['name']}</code> ({sz_kb} KB)")
        if len(files) > 15:
            file_lines.append(f"<i>...এবং আরও {len(files)-15} টি ফাইল</i>")
        files_str = "\n".join(file_lines)
    else:
        files_str = "<i>কোনো ফাইল নেই (ফোল্ডার খালি)</i>"

    total_kb = total_size // 1024
    msg = (
        f"📁 <b>ফাইল ম্যানেজার: {project['project_name']}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📄 <b>মেইন স্ক্রিপ্ট:</b> <code>{project['main_file']}</code>\n"
        f"💾 <b>মোট ফাইল সাইজ:</b> <code>{total_kb} KB</code> ({len(files)} টি ফাইল)\n\n"
        f"📋 <b>ফাইল তালিকা:</b>\n{files_str}\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "👇 <b>ফাইল অ্যাকশন নির্বাচন করুন:</b>"
    )

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        make_inline_button("📤 Upload / Replace File", callback_data=f"bot_update_{project_id}", style="primary"),
        make_inline_button("📥 Download Backup (.zip)", callback_data=f"fm_zip_{project_id}", style="primary")
    )
    markup.add(
        make_inline_button("🗑️ Delete File", callback_data=f"fm_delfile_{project_id}", style="danger"),
        make_inline_button("🔙 Bot Dashboard", callback_data=f"manage_bot_{project_id}", style="primary")
    )
    return msg, markup

def get_my_account_card(user_id: int, user_obj):
    u_data = admin.get_user(user_id)
    projects = admin.get_user_projects(user_id)
    active_instances = 0
    for p in projects:
        p_key = f"{user_id}_{p['project_id']}"
        if p_key in active_processes and active_processes[p_key]["process"].poll() is None:
            active_instances += 1

    pinfo = admin.get_plan_display_info(u_data)
    plan = pinfo["plan_key"]
    max_bots = admin.get_max_bot_limit_for_user(u_data)
    time_rem = pinfo["expiry_str"] if pinfo["is_active"] else ("— (Free Tier)" if plan == "FREE" else pinfo["expiry_str"])
    plan_display = f"{pinfo['icon']} {pinfo['display_name']}"
    ref_link = f"https://t.me/{bot.get_me().username}?start=ref_{user_id}"

    card_text = (
        "╭━━━[ 👤 <b>ACCOUNT OVERVIEW</b> ]━━━╮\n"
        f"┣ 🏷 <b>Name:</b> {user_obj.first_name}\n"
        f"┣ 🆔 <b>User ID:</b> <code>{user_id}</code>\n"
        f"┣ 👑 <b>Plan:</b> <code>{plan_display}</code>\n"
        f"┣ 🕐 <b>Time Remaining:</b> <code>{time_rem}</code>\n"
        "┣━━━[ 💳 <b>BILLING & REWARDS</b> ]━━━━┫\n"
        f"┣ ☁️ <b>Credits:</b> <code>{u_data.get('credits', 0.0)}</code>\n"
        f"┣ 🎁 <b>Referral Credits:</b> <code>{u_data.get('referral_credits', 0.0)}</code>\n"
        f"┣ 👥 <b>Total Referrals:</b> <code>{u_data.get('referral_count', 0)}</code> Users\n"
        "┣━━━[ 📊 <b>RESOURCE USAGE</b> ]━━━━━┫\n"
        f"┣ 🚀 <b>Active Instances:</b> <code>{active_instances}/{max_bots}</code> Bots\n"
        f"┣ ⏱️ <b>Total Uptime:</b> <code>{format_uptime_seconds(u_data.get('total_uptime_seconds', 0))}</code>\n"
        f"┣ 🎖️ <b>Badges Earned:</b> <code>{len(admin.get_user_badges(user_id))}/{len(admin.ACHIEVEMENTS)}</code>\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "🔗 <b>আপনার রেফারেল লিংক:</b>\n"
        f"<code>{ref_link}</code>"
    )

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        make_inline_button("💎 Upgrade Plan", callback_data="open_upgrade_menu", style="success"),
        make_inline_button("📢 Share Referral", url=f"https://t.me/share/url?url={ref_link}&text=PyHost%20এ%20ফ্রিতে%20Python%20বট%20হোস্ট%20করুন!", style="primary")
    )
    markup.add(make_inline_button("🔄 Refresh Stats", callback_data="refresh_my_account", style="primary"))
    add_home_button(markup)
    return card_text, markup

def format_uptime_seconds(total_seconds) -> str:
    total_seconds = int(total_seconds or 0)
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    if days > 0:
        return f"{days}d {hours}h"
    minutes = (total_seconds % 3600) // 60
    return f"{hours}h {minutes}m"

# ⭐ NEW: "🔴 Live Mode" লগ ভিউয়ের কনফিগ ও হেল্পার — ⏳ FIX অংশে বলা "Live Terminal" ফিচারটা
# আসলে বাস্তবায়ন করে (আগে "View Logs" শুধু একবারের স্ন্যাপশট নিত, সত্যিকারের লাইভ ছিল না)।
LIVE_LOG_MAX_DURATION = 90   # সেকেন্ড — এত সময় পর অটো বন্ধ হয়ে যাবে (সার্ভার রিসোর্স বাঁচাতে)
LIVE_LOG_INTERVAL = 4        # সেকেন্ড — প্রতি এত সেকেন্ড পরপর মেসেজ রিফ্রেশ হবে

def render_log_view(user_id: int, pid: int, n: int = 25, live: bool = False, remaining: int = None) -> str:
    """একটি বটের শেষ n লাইনের লগ HTML-এস্কেপ করে রেন্ডার করে (আগে raw টেক্সট escape ছাড়াই
    <pre> ট্যাগে বসানো হতো — ট্রেসব্যাকে '<'/'>' থাকলে HTML পার্সিং ভেঙে যেতে পারত)।"""
    p_dir = os.path.join(PROJECTS_DIR, f"{user_id}_{pid}")
    log_path = os.path.join(p_dir, "app.log")
    if os.path.exists(log_path):
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            logs = f.readlines()
        last_logs = "".join(logs[-n:]) if logs else "লগ ফাইল খালি।"
    else:
        last_logs = "কোনো লগ পাওয়া যায়নি।"

    if live:
        ts = datetime.now().strftime("%H:%M:%S")
        header = f"🔴 <b>লাইভ টার্মিনাল লগ</b> (শেষ {n} লাইন) — আপডেট: <code>{ts}</code>"
        if remaining is not None:
            header += f"\n⏳ <i>{remaining} সেকেন্ড পর স্বয়ংক্রিয়ভাবে বন্ধ হবে</i>"
    else:
        header = f"📜 <b>টার্মিনাল লগ</b> (শেষ {n} লাইন):"

    return f"{header}\n<pre>{html.escape(last_logs)}</pre>"

def _live_log_loop(chat_id, message_id, user_id, pid, session_key):
    """ব্যাকগ্রাউন্ড থ্রেড — LIVE_LOG_INTERVAL সেকেন্ড পরপর একই মেসেজ এডিট করে লগ রিফ্রেশ করে,
    LIVE_LOG_MAX_DURATION সেকেন্ড পার হলে বা ইউজার '⏹ Stop' চাপলে থেমে যায়।"""
    start_time = time.time()
    last_text = None
    stopped_manually = False

    while time.time() - start_time < LIVE_LOG_MAX_DURATION:
        if session_key not in live_log_sessions:
            stopped_manually = True
            break
        remaining = max(0, int(LIVE_LOG_MAX_DURATION - (time.time() - start_time)))
        text = render_log_view(user_id, pid, live=True, remaining=remaining)
        if text != last_text:
            try:
                stop_markup = types.InlineKeyboardMarkup(row_width=1)
                stop_markup.add(make_inline_button("⏹ Live Mode বন্ধ করুন", callback_data=f"bot_logs_stoplive_{pid}", style="danger"))
                stop_markup.add(make_inline_button("🔙 Bot Dashboard", callback_data=f"manage_bot_{pid}", style="primary"))
                bot.edit_message_text(text, chat_id, message_id, reply_markup=stop_markup, parse_mode="HTML")
                last_text = text
            except Exception:
                pass
        time.sleep(LIVE_LOG_INTERVAL)

    if not stopped_manually and live_log_sessions.pop(session_key, None) is not None:
        try:
            static_markup = types.InlineKeyboardMarkup(row_width=1)
            static_markup.add(make_inline_button("🔴 Live Mode আবার চালু করুন", callback_data=f"bot_logs_live_{pid}", style="danger"))
            static_markup.add(make_inline_button("🔙 Bot Dashboard", callback_data=f"manage_bot_{pid}", style="primary"))
            bot.edit_message_text(
                render_log_view(user_id, pid) + "\n\n⏱️ <i>Live mode সময় শেষ হওয়ায় স্বয়ংক্রিয়ভাবে বন্ধ হয়েছে।</i>",
                chat_id, message_id, reply_markup=static_markup, parse_mode="HTML"
            )
        except Exception:
            pass

def render_bot_dashboard(chat_id, message_id, user_id: int, pid: int):
    """একক বটের ড্যাশবোর্ড মেসেজ রেন্ডার/রিফ্রেশ করে। আগে Start/Stop/Restart-এর পর
    একটা fake callback object বানিয়ে হ্যান্ডলার নিজেকে কল করত — এখন সরাসরি এই ফাংশন কল হবে,
    যা বেশি স্থিতিশীল ও রক্ষণাবেক্ষণযোগ্য।"""
    project = admin.get_project_by_id(pid)
    if not project:
        return

    p_key = f"{user_id}_{pid}"
    is_running = p_key in active_processes and active_processes[p_key]["process"].poll() is None
    status_str = "🟢 Running (Active)" if is_running else "🔴 Stopped (Inactive)"

    runtime_str = "0h 0m"
    mem_str = "0 MB"
    cpu_str = "0.0%"
    if is_running:
        elapsed = int(time.time() - active_processes[p_key]["start_time"])
        runtime_str = f"{elapsed // 3600}h {(elapsed % 3600) // 60}m"
        mem, cpu = admin.get_bot_resource_usage(active_processes[p_key]["process"].pid)
        mem_str = f"{mem} MB"
        cpu_str = f"{cpu}%"

    # প্ল্যান + স্লট এক্সটেনশন — দুটো সোর্স মিলিয়ে একটাই effective স্ট্যাটাস, Control Panel ও
    # Extend Validity স্ক্রিনের সাথে ঠিক একই তথ্য এখানেও দেখানো হবে
    u_data = admin.get_user(user_id)
    slot_status = admin.get_effective_slot_status(u_data, project)
    pinfo = admin.get_plan_display_info(u_data)
    is_expired = slot_status["is_expired"]
    slot_display = project.get("slot_number") or "-"
    # Badge — প্ল্যান কিনলে সেই প্ল্যানের সুন্দর নামটাই দেখাবে (BUSINESS → Business Boss)
    if slot_status["is_24_7"] and slot_status["source"] == "PLAN":
        vip_badge = f" • {pinfo['icon']} {pinfo['display_name']}"
    elif slot_status["is_24_7"] and slot_status["source"] == "SLOT":
        vip_badge = " • 💎 Extended"
    else:
        vip_badge = " • 🆓 Free"

    # তারিখ সুন্দরভাবে ফরম্যাট করা ("18 Sep 2026 • 19:00:44")
    try:
        created_dt = datetime.strptime(project['created_at'], "%Y-%m-%d %H:%M:%S")
        created_str = created_dt.strftime("%d %b %Y • %H:%M:%S")
    except Exception:
        created_str = project['created_at']

    # ⭐ NEW: প্ল্যান/মেয়াদ ব্লকটি এখন একটা আলাদা সুন্দর বক্সে দেখানো হয়, আর সার্ভিস স্ট্যাটাস
    # লাইনের নিচে বটটি এখন সক্রিয়/বন্ধ/মেয়াদোত্তীর্ণ কিনা তার একটা স্পষ্ট সামারি লাইন যোগ হয়েছে।
    free_hours_now = admin.SYSTEM_CONFIG.get("free_hosting_hours", 12)
    locked_now = is_slot_locked(u_data, project)
    if slot_status["expiry_dt"] is None:
        service_line = "⚡ সার্ভিস: 24/7 Active (Lifetime)" if slot_status["is_24_7"] else f"⚡ সার্ভিস: 🆓 Free ({free_hours_now}h/Session)"
        validity_lines = f"│ {service_line}"
    else:
        days_left = slot_status["days_left"]
        expiry_str = slot_status["expiry_dt"].strftime("%d %b %Y • %H:%M")
        service_line = "⚡ সার্ভিস: 24/7 Active" if slot_status["is_24_7"] else f"⚡ সার্ভিস: 🆓 Free ({free_hours_now}h/Session)"
        validity_lines = (
            f"│ {service_line}\n"
            f"│ 📆 মেয়াদ বাকি: {days_left} দিন\n"
            f"│ ⏳ মেয়াদ শেষ: {expiry_str}"
        )

    plan_box = (
        "╭─────────────────────\n"
        f"│ 👑 প্ল্যান: {pinfo['icon']} {pinfo['display_name']}\n"
        f"{validity_lines}\n"
        "╰─────────────────────"
    )

    if locked_now:
        footer_line = "🔒 <b>ফ্রি হোস্টিং অ্যাডমিন থেকে সাময়িক বন্ধ — এই স্লট লকড</b>"
    elif is_expired:
        footer_line = "⛔ <b>মেয়াদ শেষ — বট বন্ধ আছে</b>"
    elif is_running:
        footer_line = "🟢 <b>বট বর্তমানে সক্রিয় ও চলছে</b>"
    else:
        footer_line = "🔴 <b>বট বর্তমানে বন্ধ আছে</b>"

    msg = (
        "🤖 <b>বট ড্যাশবোর্ড</b>\n"
        f"{html.escape(project['project_name'])}{vip_badge}\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔢 <b>Slot:</b> <code>{slot_display}</code>\n"
        f"⚡ <b>স্ট্যাটাস:</b> <code>{status_str}</code>\n"
        f"📄 <b>মেইন ফাইল:</b> <code>{project['main_file']}</code>\n"
        f"🧠 <b>RAM Usage:</b> <code>{mem_str} / 256 MB</code>\n"
        f"💻 <b>CPU Load:</b> <code>{cpu_str}</code>\n"
        f"⏰ <b>রানিং টাইম:</b> <code>{runtime_str}</code>\n"
        f"📅 <b>তৈরি:</b> <code>{created_str}</code>\n\n"
        f"<pre>{plan_box}</pre>\n\n"
        f"{footer_line}"
    )
    try:
        bot.edit_message_text(
            msg, chat_id, message_id,
            reply_markup=get_single_bot_management_markup(user_id, pid),
            parse_mode="HTML"
        )
    except Exception:
        pass


def _doc_pages():
    """📖 Documentation-এর পাতাগুলো প্রতিবার ফ্রেশ বানানো হয় যাতে ফ্রি হোস্টিংয়ের ঘণ্টা সবসময় সঠিক থাকে।"""
    hours = admin.SYSTEM_CONFIG.get("free_hosting_hours", 12)
    return [
        ("🚀 নতুন বট তৈরি ও ডেপ্লয়",
         "├ 1️⃣ 🎛️ <b>Control Panel</b> থেকে <code>➕ Add New Bot</code> চাপুন।\n"
         "├ 2️⃣ আপনার বটের নাম দিন (যেমন: <i>My Support Bot</i>)।\n"
         "├ 3️⃣ <code>📤 Update / Upload File</code> থেকে <code>.py</code> বা <code>.zip</code> স্ক্রিপ্ট আপলোড করুন (বা সরাসরি কোড চ্যাটে পেস্ট করুন)।\n"
         "└ 4️⃣ ফাইল আপলোড হলে সিস্টেম স্বয়ংক্রিয়ভাবে প্যাকেজ ইনস্টল করে বট চালু করবে!"),
        ("🛒 নো-কোড মার্কেটপ্লেস (১-ক্লিক ডেপ্লয়)",
         "├ কোনো কোডিং ছাড়াই বট চালাতে 🤖 <b>Marketplace</b>-এ যান (🐙 GitHub Deploy স্ক্রিনের ভিতরে পাবেন)।\n"
         "├ পছন্দের বট সিলেক্ট করে @BotFather থেকে পাওয়া <code>BOT_TOKEN</code> দিন।\n"
         "└ সিস্টেম স্বয়ংক্রিয়ভাবে পুরো বট কনফিগার করে লাইভ করে দেবে।"),
        ("📁 ফাইল ও লাইব্রেরি রুলস",
         "├ 📄 <b>মেইন ফাইল:</b> আপনার প্রধান ফাইলের নাম <code>main.py</code> রাখা উত্তম।\n"
         "├ 📦 <b>জিপ ফাইল:</b> <code>.zip</code> আপলোড করলে তা নিজে থেকেই আনপ্যাক হয়ে যাবে।\n"
         "└ ⚙️ <b>ডিপেন্ডেন্সি:</b> কোড স্ক্যান করে সব লাইব্রেরি সিস্টেম অটো ইনস্টল করে নেয়।"),
        ("🎛️ ড্যাশবোর্ড ও ফাইল ম্যানেজার",
         "├ ▶️ <b>Start / Stop:</b> বট চালু বা বন্ধ রাখা।\n"
         "├ 🔄 <b>Restart:</b> কোড আপডেটের পর রিফ্রেশ করা।\n"
         "├ 📜 <b>View Logs:</b> কোনো এরর হলে লাইভ লগ চেক করা।\n"
         "└ 📁 <b>File Manager:</b> ফাইলের সাইজ দেখা, জিপ ব্যাকআপ ও নির্দিষ্ট ফাইল ডিলিট।"),
        ("⏳ প্ল্যান পলিসি ও প্যাকেজসমূহ",
         f"├ 🟢 <b>FREE Plan:</b> প্রতি রান {hours} ঘণ্টা ({hours} ঘণ্টা পর CP থেকে আবার চালু করা যাবে)।\n"
         "├ ⚡ <b>Pro Plan:</b> 24/7 লাইভ • 1টি বট • ফাস্ট বুট।\n"
         "├ 🌟 <b>Premium Plan:</b> 24/7 লাইভ • 3টি বট • অটো-রিস্টার্ট • ব্যাকআপ।\n"
         "└ 👑 <b>Business Boss:</b> 24/7 লাইভ • 10টি বট • ম্যাক্সিমাম RAM • VIP সাপোর্ট।\n"
         "🎁 <b>Redeem Code:</b> 💎 Upgrade Plan পেজ থেকে অথবা <code>/redeem CODE</code> দিয়ে সাথে সাথে প্ল্যান চালু করুন।"),
        ("🛠️ সাপোর্ট হাব (Support Hub)",
         "├ 💼 <b>Dev Studio:</b> কাস্টম বট বানাতে আমাদের ডেভেলপার টিমকে রিকোয়ারমেন্ট পাঠান।\n"
         "└ 👨‍💼 <b>Contact Admin:</b> পেমেন্ট বা টেকনিক্যাল সহায়তার জন্য সরাসরি যোগাযোগ করুন।"),
    ]

def build_doc_page(idx: int):
    pages = _doc_pages()
    idx = max(0, min(idx, len(pages) - 1))
    title, body = pages[idx]
    text = (
        f"📖 <b>PYHOST CLOUD গাইড</b> — পাতা {idx + 1}/{len(pages)}\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n\n"
        f"<b>{title}</b>\n"
        f"{body}\n\n"
        "〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )
    markup = types.InlineKeyboardMarkup(row_width=3)
    nav = []
    if idx > 0:
        nav.append(make_inline_button("◀️ Prev", callback_data=f"doc_page_{idx - 1}", style="primary"))
    nav.append(make_inline_button(f"{idx + 1}/{len(pages)}", callback_data="doc_noop", style="primary"))
    if idx < len(pages) - 1:
        nav.append(make_inline_button("Next ▶️", callback_data=f"doc_page_{idx + 1}", style="success"))
    markup.row(*nav)
    add_home_button(markup)
    return text, markup

def get_upload_guide_card():
    text = (
        "╭──────────────╮\n"
        "│ 📤 ফাইল / কোড পাঠান\n"
        "│\n"
        "│ • 🐍 \".py\" → main.py / bot.py\n"
        "│ • 📄 \".txt\" \".json\" \".env\"\n"
        "│ • 📦 \".zip\" → সব ফাইল\n"
        "│ • 💻 সরাসরি Python কোড\n"
        "╰──────────────╯\n"
        "╭━━━━━━━━━━━━━━━━━━╮\n"
        "┃ 📥 এখন ফাইল বা কোডটি সেন্ড করুন\n"
        "╰━━━━━━━━━━━━━━━━━━╯"
    )
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(make_inline_button("◀️ Back to Control Panel", callback_data="back_to_cp", style="primary"))
    return text, markup

# ৩-টিয়ার প্যাকেজ সিলেকশন ভিউ
def get_upgrade_plan_view(user_id: int):
    u_data = admin.get_user(user_id)
    pinfo = admin.get_plan_display_info(u_data)
    curr_tier_display = f"{pinfo['icon']} {pinfo['display_name']}"
    exp_display = pinfo["expiry_str"] if pinfo["is_active"] else "—"

    def _tag(tdata):
        t = " | Pro" if tdata.get("pro") else ""
        if tdata.get("sold") is not None:
            t += f" | Sold: {tdata['sold']}/{tdata.get('sold_total', tdata['sold'])}"
        return t

    pro = admin.TIER_PLANS["PRO"]
    prem = admin.TIER_PLANS["PREMIUM"]
    biz = admin.TIER_PLANS["BUSINESS"]
    biz6 = admin.TIER_PLANS["BUSINESS6M"]
    yr = admin.TIER_PLANS["YEARLY"]

    text = (
        "🚀 <b>PyHost Cloud — Premium Plans</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🟢 <b>Current Tier:</b> <code>{curr_tier_display}</code> ({exp_display})\n\n"
        "⚡ <b>Pro:</b> 24/7 • Ad-Free • 1 Bot • Auto-Renew\n"
        "🌟 <b>Premium:</b> 24/7 • 3 Bots • GitHub Sync • Backup • DDoS Shield\n"
        "👑🔥 <b>Business Boss:</b> 24/7 • 10 Bots • Live Terminal • Max RAM • VIP Support\n"
        "🏆 <b>Business Yearly:</b> Business-এর সব ফিচার + সারা বছরের জন্য বিশাল ছাড়\n\n"
        "👇 <b>Plan select:</b>"
    )

    markup = types.InlineKeyboardMarkup(row_width=1)
    b1 = make_inline_button(f"⚡ {pro['period_bn']} — {pro['price']}{_tag(pro)}", callback_data="select_tier_PRO", style="primary")
    b2 = make_inline_button(f"🌟 {prem['period_bn']} — {prem['price']}{_tag(prem)}", callback_data="select_tier_PREMIUM", style="success")
    b3 = make_inline_button(f"👑 {biz['period_bn']} — {biz['price']}{_tag(biz)}", callback_data="select_tier_BUSINESS", style="primary")
    b4 = make_inline_button(f"🔥 {biz6['period_bn']} — {biz6['price']}{_tag(biz6)}", callback_data="select_tier_BUSINESS6M", style="success")
    b5 = make_inline_button(f"🏆 {yr['period_bn']} — {yr['price']}{_tag(yr)}", callback_data="select_tier_YEARLY", style="success")
    markup.add(b1, b2, b3, b4, b5)
    markup.add(make_inline_button("🎁 Redeem Code আছে?", callback_data="user_redeem_start", style="success"))
    add_home_button(markup)
    return text, markup

# নতুন অর্ডার সামারি কার্ড ও পেমেন্ট গেটওয়ে বাটনস (স্ক্রিনশটের হুবহু ডিজাইন)
def get_order_summary_view(tier_key: str, discount_percent: int = 0):
    tdata = admin.TIER_PLANS.get(tier_key, admin.TIER_PLANS["PREMIUM"])
    
    price_val = tdata["price_num"]
    if discount_percent > 0:
        price_val = int(price_val * (1 - discount_percent / 100))
        price_text = f"<s>{tdata['price_num']} BDT</s> <b>{price_val} BDT</b> ({discount_percent}% OFF)"
    else:
        price_text = f"{price_val} BDT"

    text = (
        "🛒 <b>Order Summary</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 <b>{tdata['title']}</b> | ⏱️ <b>{tdata['days']} Days</b>" + (" | Pro" if tdata.get("pro") else "") + "\n"
        f"💰 <b>{price_text}</b>" + (f" | Sold: {tdata['sold']}/{tdata.get('sold_total', tdata['sold'])}" if tdata.get("sold") is not None else "") + "\n\n"
        f"✨ <b>Features:</b>\n{tdata['features']}\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "পেমেন্ট মেথড বেছে নিন:"
    )

    markup = types.InlineKeyboardMarkup(row_width=2)
    b1 = make_inline_button("💳 bKash", callback_data=f"paygw_{tier_key}_bkash_{price_val}", style="primary")
    b2 = make_inline_button("💳 Nagad", callback_data=f"paygw_{tier_key}_nagad_{price_val}", style="primary")
    b3 = make_inline_button("🪙 Crypto Pay (USDT/TRX)", callback_data=f"paygw_{tier_key}_crypto_{price_val}", style="primary")
    b4 = make_inline_button("🎁 Coupon Code", callback_data=f"apply_coupon_{tier_key}", style="success")
    b5 = make_inline_button("◀️ Back", callback_data="open_upgrade_menu", style="danger")
    
    markup.add(b1, b2)
    markup.add(b3)
    markup.add(b4)
    markup.add(b5)
    return text, markup

def get_support_hub_markup():
    markup = types.InlineKeyboardMarkup(row_width=2)
    b1 = make_inline_button("❓ FAQ", callback_data="sup_faq", style="primary")
    b2 = make_inline_button("📢 Official Channel ↗️", url="https://t.me/+6eODSVuNdqUxMTNl", style="primary")
    b3 = make_inline_button("👥 Community ↗️", url="https://t.me/+6eODSVuNdqUxMTNl", style="primary")
    b4 = make_inline_button("💼 Dev Studio", callback_data="sup_devstudio", style="primary")
    # ⭐ NEW: "👨‍💼 Contact Admin" আগে সরাসরি অ্যাডমিনের ব্যক্তিগত DM-এ URL লিংক ছিল — কোনো
    # ট্র্যাকিং ছিল না। এখন "🎫 My Tickets" দিয়ে বটের ভিতরেই একটা ট্র্যাকযোগ্য টিকিট
    # সিস্টেমে যাওয়া যায় (status + মেসেজ থ্রেড সহ)।
    b6 = make_inline_button("🎫 My Tickets", callback_data="my_tickets_page_0", style="success")
    b5 = make_inline_button("👨‍💼 Contact Admin ↗️", url="https://t.me/forhadkhandakar", style="success")
    markup.add(b1, b2, b3, b4)
    markup.add(b6)
    markup.add(b5)
    add_home_button(markup, "🔙 Back to Home")
    return markup

# ======================== 💼 Dev Studio (কাস্টম বট ডেভেলপমেন্ট রিকোয়েস্ট) ========================
def get_dev_studio_card():
    """Support Hub-এর 💼 Dev Studio বাটনের স্ক্রিন — রিকোয়ারমেন্ট পাঠালে সাপোর্ট টিকিট হিসেবে অ্যাডমিনের কাছে যায়।"""
    text = (
        "💼 <b>Dev Studio — কাস্টম বট ডেভেলপমেন্ট</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "নিজের আইডিয়া অনুযায়ী কাস্টম টেলিগ্রাম বট বানাতে চান? আমাদের ডেভেলপার টিমকে আপনার "
        "রিকোয়ারমেন্ট পাঠান — টিম রিভিউ করে সরাসরি আপনাকে রিপ্লাই দেবে।\n\n"
        "📝 <b>রিকোয়ারমেন্টে যা লিখলে দ্রুত কাজ শুরু হবে:</b>\n"
        "├ 1️⃣ বটের ধরন (যেমন: স্টোর, সাপোর্ট, গ্রুপ ম্যানেজার)\n"
        "├ 2️⃣ কী কী ফিচার চান\n"
        "├ 3️⃣ আপনার বাজেট\n"
        "└ 4️⃣ কবে নাগাদ দরকার (ডেডলাইন)\n\n"
        "🎫 রিকোয়ারমেন্ট <b>সাপোর্ট টিকিট</b> হিসেবে জমা হবে — <b>My Tickets</b> থেকে রিপ্লাই ও স্ট্যাটাস দেখতে পারবেন।\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        make_inline_button("📝 রিকোয়ারমেন্ট পাঠান", callback_data="devstudio_request", style="success"),
        make_inline_button("🎫 My Tickets", callback_data="my_tickets_page_0", style="primary"),
        make_inline_button("🔙 Back", callback_data="back_to_support", style="danger"),
    )
    return text, markup

# ======================== 🎁 Redeem Code — অ্যাডমিন ড্যাশবোর্ড + ইউজার ফ্লো ========================
def _edit_or_send(call, text, markup=None):
    """মেসেজ এডিট করে; কোনো কারণে না পারলে (মেসেজ পুরনো/মুছে গেছে) নতুন মেসেজ পাঠায়।"""
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
    except Exception as e:
        if "not modified" in str(e).lower():
            return
        bot.send_message(call.message.chat.id, text, reply_markup=markup, parse_mode="HTML")

def _rc_pack_label(tier_key: str, days: int = None) -> str:
    t = admin.TIER_PLANS.get(tier_key, {})
    d = days if days is not None else t.get("days", 0)
    return f"{t.get('icon', '🎁')} {t.get('title', tier_key)} • {d} দিন"

def _rc_status(c: dict) -> tuple:
    if not c["is_active"]:
        return "🔴", "বাতিল"
    if c["max_uses"] > 0 and c["used_count"] >= c["max_uses"]:
        return "✅", "ব্যবহৃত"
    return "🟢", "অ্যাভেইলেবল"

def _rc_uses_label(c: dict) -> str:
    return f"{c['used_count']}/{c['max_uses']}" if c["max_uses"] > 0 else f"{c['used_count']}/∞"

def build_redeem_dashboard():
    st = admin.get_redeem_stats()
    recent = admin.list_redeem_codes(limit=5)
    if recent:
        lines = [f"{_rc_status(c)[0]} <code>{c['code']}</code> — {_rc_pack_label(c['tier_key'], c['days'])} | {_rc_uses_label(c)}" for c in recent]
        recent_txt = "\n".join(lines)
    else:
        recent_txt = "<i>এখনো কোনো কোড তৈরি হয়নি।</i>"
    text = (
        "🎁 <b>Redeem Code Manager</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "প্যাক বেছে কোড বানান → ইউজারকে কোড দিন → ইউজার <b>🎁 Redeem Code</b> দিয়ে রিডিম করলেই "
        "প্ল্যান সরাসরি চালু হয়ে যাবে।\n\n"
        f"📦 মোট কোড: <b>{st['total']}</b>\n"
        f"🟢 ব্যবহারযোগ্য: <b>{st['available']}</b>\n"
        f"✅ মোট রিডিম: <b>{st['redeemed']}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🕒 <b>সর্বশেষ ৫টি কোড:</b>\n" + recent_txt
    )
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(make_inline_button("➕ Create New Code", callback_data="rc_new", style="success"))
    markup.add(
        make_inline_button("📋 All Codes", callback_data="rc_list_0", style="primary"),
        make_inline_button("🔄 Refresh", callback_data="rc_home", style="primary"),
    )
    return text, markup

def build_redeem_tier_picker():
    text = (
        "📦 <b>প্যাক বেছে নিন</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "কোন প্ল্যানের কোড বানাতে চান? (বট লিমিট প্ল্যান অনুযায়ী স্বয়ংক্রিয়ভাবে সেট হবে)"
    )
    markup = types.InlineKeyboardMarkup(row_width=1)
    for key, t in admin.TIER_PLANS.items():
        markup.add(make_inline_button(
            f"{t['icon']} {t['title']} • {t['period_bn'].replace('দিন','Days').replace('মাস','Months').replace('বছর','Year')} • {t['bot_limit']} Bots",
            callback_data=f"rc_tier_{key}", style="primary"))
    markup.add(make_inline_button("🔙 Back", callback_data="rc_home", style="danger"))
    return text, markup

def build_redeem_days_picker(tier_key: str):
    t = admin.TIER_PLANS[tier_key]
    text = (
        f"⏳ <b>মেয়াদ বেছে নিন</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 প্যাক: <b>{t['icon']} {t['title']}</b>\n"
        f"ডিফল্ট মেয়াদ: <b>{t['days']} দিন</b> — চাইলে অন্য মেয়াদও দিতে পারেন।"
    )
    markup = types.InlineKeyboardMarkup(row_width=4)
    markup.add(make_inline_button(f"✅ Default ({t['days']} Days)", callback_data=f"rc_days_{tier_key}_{t['days']}", style="success"))
    quick = [d for d in (7, 15, 30, 90) if d != t["days"]]
    markup.row(*[make_inline_button(f"{d} Days", callback_data=f"rc_days_{tier_key}_{d}", style="primary") for d in quick])
    markup.add(make_inline_button("✏️ Custom Days", callback_data=f"rc_daysc_{tier_key}", style="primary"))
    markup.add(make_inline_button("🔙 Back", callback_data="rc_new", style="danger"))
    return text, markup

def build_redeem_qty_picker(tier_key: str, days: int):
    text = (
        "🔢 <b>কতটি কোড বানাবেন?</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 প্যাক: <b>{_rc_pack_label(tier_key, days)}</b>\n"
        "প্রতিটি কোড শুধু <b>১ জন</b> ইউজার রিডিম করতে পারবে।\n"
        "নিজের পছন্দের নামের কোড বা একাধিক ইউজারের কোড চাইলে ✍️ কাস্টম কোড বেছে নিন।"
    )
    markup = types.InlineKeyboardMarkup(row_width=5)
    markup.row(*[make_inline_button(str(q), callback_data=f"rc_qty_{tier_key}_{days}_{q}", style="success") for q in (1, 5, 10, 25, 50)])
    markup.add(make_inline_button("✍️ Custom Code (Own Name / Multi-User)", callback_data=f"rc_custom_{tier_key}_{days}", style="primary"))
    markup.add(make_inline_button("🔙 Back", callback_data=f"rc_tier_{tier_key}", style="danger"))
    return text, markup

def build_redeem_codes_result(tier_key: str, days: int, codes: list, max_uses: int):
    uses_label = "আনলিমিটেড ইউজার" if max_uses == 0 else f"{max_uses} জন ইউজার"
    header = (
        f"✅ <b>{len(codes)}টি কোড তৈরি হয়েছে!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 প্যাক: <b>{_rc_pack_label(tier_key, days)}</b>\n"
        f"👥 প্রতি কোড: <b>{uses_label}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
    )
    footer = "\n━━━━━━━━━━━━━━━━━━━━━━\n👆 কোডে ট্যাপ করলে কপি হবে — ইউজারকে পাঠিয়ে দিন।"
    # লম্বা লিস্ট হলে ৪০৯৬ ক্যারেক্টার লিমিট এড়াতে ভেঙে পাঠানো হয়
    chunks, cur = [], []
    cur_len = len(header) + len(footer)
    for c in codes:
        line = f"<code>{c}</code>"
        if cur_len + len(line) + 1 > 3800:
            chunks.append(cur); cur = []; cur_len = len(footer) + 20
        cur.append(line); cur_len += len(line) + 1
    chunks.append(cur)
    messages = []
    for i, ch in enumerate(chunks):
        body = "\n".join(ch)
        messages.append((header if i == 0 else "🎁 <b>(পরের অংশ)</b>\n") + body + footer)
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        make_inline_button("➕ Create More", callback_data="rc_new", style="success"),
        make_inline_button("🎁 Redeem Home", callback_data="rc_home", style="primary"),
    )
    return messages, markup

def build_redeem_list_view(page: int = 0):
    codes = admin.list_redeem_codes(limit=200)
    markup = types.InlineKeyboardMarkup(row_width=1)
    if not codes:
        markup.add(make_inline_button("🔙 Back", callback_data="rc_home", style="danger"))
        return "📋 <b>কোড লিস্ট</b>\n━━━━━━━━━━━━━━━━━━━━━━\n<i>এখনো কোনো কোড তৈরি হয়নি।</i>", markup
    items, page, total_pages = admin.paginate(codes, page, per_page=8)
    text = (
        f"📋 <b>সব Redeem Code</b> — মোট <code>{len(codes)}</code>টি (পেজ {page+1}/{total_pages})\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n🟢 ব্যবহারযোগ্য  ✅ ব্যবহৃত  🔴 বাতিল\n👇 ডিটেইলস দেখতে কোডে ট্যাপ করুন:"
    )
    for c in items:
        icon, _ = _rc_status(c)
        markup.add(make_inline_button(f"{icon} {c['code']} • {c['days']}d • {_rc_uses_label(c)}", callback_data=f"rc_view_{c['code']}", style="primary"))
    nav = []
    if page > 0:
        nav.append(make_inline_button("◀️ Prev", callback_data=f"rc_list_{page-1}", style="primary"))
    if page < total_pages - 1:
        nav.append(make_inline_button("Next ▶️", callback_data=f"rc_list_{page+1}", style="primary"))
    if nav:
        markup.row(*nav)
    markup.add(make_inline_button("🔙 Back", callback_data="rc_home", style="danger"))
    return text, markup

def build_redeem_detail_view(code: str):
    c = admin.get_redeem_code(code)
    if not c:
        return None, None
    icon, label = _rc_status(c)
    uses = admin.get_redeem_uses(c["code"], limit=15)
    if uses:
        used_lines = "\n".join(f"• @{html.escape(u['username'] or 'Anonymous')} (<code>{u['user_id']}</code>) — {u['used_at']}" for u in uses)
    else:
        used_lines = "<i>এখনো কেউ রিডিম করেনি।</i>"
    text = (
        f"🎁 <b>Redeem Code</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔑 কোড: <code>{c['code']}</code>\n"
        f"📦 প্যাক: <b>{_rc_pack_label(c['tier_key'], c['days'])}</b>\n"
        f"📊 স্ট্যাটাস: {icon} <b>{label}</b>\n"
        f"👥 ব্যবহার: <b>{_rc_uses_label(c)}</b>\n"
        f"🕒 তৈরি: <code>{c['created_at']}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>রিডিম করেছেন:</b>\n{used_lines}"
    )
    markup = types.InlineKeyboardMarkup(row_width=1)
    if c["is_active"]:
        markup.add(make_inline_button("⛔ Revoke Code", callback_data=f"rc_off_{c['code']}", style="danger"))
    else:
        markup.add(make_inline_button("♻️ Re-Activate", callback_data=f"rc_on_{c['code']}", style="success"))
    markup.add(make_inline_button("🔙 Code List", callback_data="rc_list_0", style="primary"))
    return text, markup

def _handle_redeem_admin_callback(call):
    """অ্যাডমিন প্যানেলের সব rc_* কলব্যাক এখানে। (ডিসপ্যাচারে অ্যাডমিন চেক আগেই হয়ে গেছে)"""
    uid = call.from_user.id
    parts = call.data.split("_")
    action = parts[1] if len(parts) > 1 else ""
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    if action == "home":
        user_states.pop(uid, None)
        text, markup = build_redeem_dashboard()
        _edit_or_send(call, text, markup)

    elif action == "new":
        user_states.pop(uid, None)
        text, markup = build_redeem_tier_picker()
        _edit_or_send(call, text, markup)

    elif action == "tier" and len(parts) >= 3 and parts[2] in admin.TIER_PLANS:
        text, markup = build_redeem_days_picker(parts[2])
        _edit_or_send(call, text, markup)

    elif action == "daysc" and len(parts) >= 3 and parts[2] in admin.TIER_PLANS:
        user_states[uid] = {"state": "awaiting_redeem_days", "tier": parts[2]}
        bot.send_message(
            call.message.chat.id,
            "✏️ <b>কাস্টম মেয়াদ</b>\nকত দিনের প্যাক? শুধু সংখ্যা লিখে পাঠান (১ – ৩৬৫০)।\nউদাহরণ: <code>45</code>",
            parse_mode="HTML"
        )

    elif action == "days" and len(parts) >= 4 and parts[2] in admin.TIER_PLANS and parts[3].isdigit():
        text, markup = build_redeem_qty_picker(parts[2], int(parts[3]))
        _edit_or_send(call, text, markup)

    elif action == "qty" and len(parts) >= 5 and parts[2] in admin.TIER_PLANS and parts[3].isdigit() and parts[4].isdigit():
        tier_key, days, qty = parts[2], int(parts[3]), int(parts[4])
        ok, res = admin.create_redeem_codes(tier_key, days, qty, 1, uid)
        if not ok:
            bot.send_message(call.message.chat.id, f"❌ {html.escape(str(res))}", parse_mode="HTML")
            return
        messages, markup = build_redeem_codes_result(tier_key, days, res, 1)
        for i, m in enumerate(messages):
            bot.send_message(call.message.chat.id, m, reply_markup=markup if i == len(messages) - 1 else None, parse_mode="HTML")

    elif action == "custom" and len(parts) >= 4 and parts[2] in admin.TIER_PLANS and parts[3].isdigit():
        user_states[uid] = {"state": "awaiting_redeem_custom", "tier": parts[2], "days": int(parts[3])}
        bot.send_message(
            call.message.chat.id,
            "✍️ <b>কাস্টম কোড</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 প্যাক: <b>{_rc_pack_label(parts[2], int(parts[3]))}</b>\n\n"
            "এই ফরম্যাটে পাঠান:\n<code>CODE MAX_USES</code>\n\n"
            "উদাহরণ: <code>EID2026 50</code>\n→ 'EID2026' কোডটি সর্বোচ্চ ৫০ জন ইউজার রিডিম করতে পারবে\n\n"
            "• MAX_USES না দিলে ১ জন\n• MAX_USES = 0 দিলে আনলিমিটেড\n"
            "• কোডে শুধু ইংরেজি লেটার/সংখ্যা/ড্যাশ (৪-৩২ অক্ষর)",
            parse_mode="HTML"
        )

    elif action == "list":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        text, markup = build_redeem_list_view(page)
        _edit_or_send(call, text, markup)

    elif action in ("view", "off", "on") and len(parts) >= 3:
        code = "_".join(parts[2:])
        if action == "off":
            admin.set_redeem_code_active(code, False)
        elif action == "on":
            admin.set_redeem_code_active(code, True)
        text, markup = build_redeem_detail_view(code)
        if not text:
            bot.send_message(call.message.chat.id, "❌ কোডটি পাওয়া যায়নি।")
            return
        _edit_or_send(call, text, markup)

def process_user_redeem(chat_id: int, user_id: int, username: str, raw_code: str):
    """ইউজারের দেওয়া কোড রিডিম করে সাকসেস/এরর কার্ড পাঠায় (বাটন, /redeem কমান্ড ও টেক্সট — তিন পথেই এটা ব্যবহার হয়)।"""
    ok, res = admin.redeem_code(user_id, username, raw_code)
    if not ok:
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            make_inline_button("🔁 আবার চেষ্টা করুন", callback_data="user_redeem_start", style="success"),
            make_inline_button("🎫 সাপোর্টে যোগাযোগ", callback_data="back_to_support", style="primary"),
        )
        bot.send_message(chat_id, f"❌ <b>রিডিম ব্যর্থ!</b>\n{html.escape(str(res))}", reply_markup=markup, parse_mode="HTML")
        return

    _award_first_upgrade_badge(user_id)
    u_data = admin.get_user(user_id)
    try:
        pinfo = admin.get_plan_display_info(u_data)
        plan_line = f"👑 <b>বর্তমান প্ল্যান:</b> {pinfo['icon']} {html.escape(str(pinfo['display_name']))}\n⏳ <b>মেয়াদ শেষ:</b> <code>{pinfo['expiry_str']}</code>\n"
    except Exception:
        plan_line = ""
    try:
        slots_line = f"🤖 <b>বট লিমিট:</b> {admin.get_max_bot_limit_for_user(u_data)}টি\n"
    except Exception:
        slots_line = ""
    card = (
        "🎉 <b>রিডিম সফল!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎁 <b>কোড:</b> <code>{res['code']}</code>\n"
        f"📦 <b>প্যাক:</b> {_rc_pack_label(res['tier_key'], res['days'])}\n"
        f"{plan_line}{slots_line}"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🚀 এখন আপনার বট ২৪/৭ চালাতে পারবেন!"
    )
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        make_inline_button("🎛️ Control Panel", callback_data="back_to_cp", style="success"),
        make_inline_button("🏠 Home Menu", callback_data="go_home", style="primary"),
    )
    bot.send_message(chat_id, card, reply_markup=markup, parse_mode="HTML")

    admin_alert = (
        "🎁 <b>Redeem Code ব্যবহৃত হয়েছে!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 @{html.escape(username or 'Anonymous')} (ID: <code>{user_id}</code>)\n"
        f"🔑 <code>{res['code']}</code>\n"
        f"📦 {_rc_pack_label(res['tier_key'], res['days'])}"
    )
    for aid in admin.ADMIN_IDS:
        try:
            bot.send_message(aid, admin_alert, parse_mode="HTML")
        except Exception:
            pass

def send_redeem_prompt(chat_id: int, user_id: int):
    """ইউজারকে কোড লেখার প্রম্পট দেখায় এবং স্টেট সেট করে।"""
    user_states[user_id] = {"state": "awaiting_redeem_input"}
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(make_inline_button("❌ বাতিল", callback_data="user_redeem_cancel", style="danger"))
    bot.send_message(
        chat_id,
        "🎁 <b>Redeem Code</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "আপনার রিডিম কোডটি লিখে পাঠান — কোড সঠিক হলে প্ল্যান সাথে সাথে চালু হয়ে যাবে।\n\n"
        "উদাহরণ: <code>PYH-ABCD-EFGH-JKLM</code>\n"
        "💡 কোড না থাকলে <b>💎 Upgrade Plan</b> থেকে প্ল্যান কিনতে পারেন।",
        reply_markup=markup, parse_mode="HTML"
    )

# যে স্টেটগুলোতে মেনুর বাটন চাপলে ইনপুট-মোড বাতিল হয়ে বাটনটি স্বাভাবিকভাবে কাজ করবে
# (আগে এই অবস্থায় বাটনের লেখাটাই টিকিট/কোড হিসেবে ধরা হতো)
_NAV_CANCELLABLE_STATES = {
    "awaiting_redeem_input", "awaiting_devstudio_request", "awaiting_ticket_message",
    "awaiting_redeem_days", "awaiting_redeem_custom",
}
_NAV_BUTTON_TEXTS = {
    "🎛️ Control Panel", "📁 File Manager", "👤 My Account", "💎 Upgrade Plan", "🤖 Marketplace",
    "🐙 GitHub Deploy", "📖 Documentation", "📡 Support Hub", "🎁 Redeem Code", "👑 Admin Panel",
    "🔙 User Menu", "🔙 Admin Menu", "📊 Monitoring", "👥 User Manager", "💰 Billing & Plans",
    "⚙️ System", "📈 Analytics", "🎁 Redeem Codes", "🎟️ Coupon Manager", "💎 Grant VIP (Manual)",
    "🆓 Free Hosting (12h)",
}

# ⭐ NEW: সাপোর্ট টিকিট সিস্টেম — ভিউ বিল্ডার ফাংশনসমূহ
TICKET_STATUS_ICON = {"OPEN": "🟢", "REPLIED": "🟡", "CLOSED": "🔒"}
TICKET_STATUS_LABEL = {"OPEN": "Open", "REPLIED": "Replied", "CLOSED": "Closed"}

def build_user_tickets_view(user_id: int, page: int = 0):
    """ইউজারের নিজের সব টিকিটের লিস্ট — নতুন টিকিট খোলার বাটনসহ।"""
    tickets = admin.get_user_tickets(user_id)
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(make_inline_button("📝 নতুন টিকিট খুলুন", callback_data="new_ticket", style="success"))

    if not tickets:
        add_home_button(markup)
        return "🎫 <b>My Tickets</b>\n━━━━━━━━━━━━━━━━━━━━━━\n<i>আপনার কোনো টিকিট নেই।</i>", markup

    items, page, total_pages = admin.paginate(tickets, page, per_page=6)
    text = f"🎫 <b>My Tickets</b> — মোট <code>{len(tickets)}</code> টি (পেজ {page+1}/{total_pages})\n━━━━━━━━━━━━━━━━━━━━━━"
    for t in items:
        icon = TICKET_STATUS_ICON.get(t["status"], "⚪")
        label = f"{icon} #{t['ticket_id']} • {t['subject']} ({TICKET_STATUS_LABEL.get(t['status'], t['status'])})"
        markup.add(make_inline_button(label, callback_data=f"ticket_view_{t['ticket_id']}", style="primary"))

    nav_row = []
    if page > 0:
        nav_row.append(make_inline_button("◀️ Prev", callback_data=f"my_tickets_page_{page-1}", style="primary"))
    if page < total_pages - 1:
        nav_row.append(make_inline_button("Next ▶️", callback_data=f"my_tickets_page_{page+1}", style="primary"))
    if nav_row:
        markup.add(*nav_row)
    add_home_button(markup)
    return text, markup

def build_ticket_thread_view(ticket_id: int, is_admin: bool = False):
    """একটা টিকিটের পুরো মেসেজ থ্রেড রেন্ডার করে — ইউজার ও অ্যাডমিন দুই ভিউতেই ব্যবহার হয়,
    শুধু বাটন ও ব্যাক-নেভিগেশন আলাদা।"""
    ticket = admin.get_ticket(ticket_id)
    if not ticket:
        return "❌ টিকিটটি পাওয়া যায়নি।", None

    msgs = admin.get_ticket_messages(ticket_id)
    icon = TICKET_STATUS_ICON.get(ticket["status"], "⚪")
    header = (
        f"🎫 <b>Ticket #{ticket_id}</b> — {icon} {TICKET_STATUS_LABEL.get(ticket['status'], ticket['status'])}\n"
        f"👤 <code>{ticket['username'] or ticket['user_id']}</code> (ID: <code>{ticket['user_id']}</code>)\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )
    lines = [header]
    for m in msgs:
        sender_label = "👤 You" if (m["sender"] == "user" and not is_admin) else ("👨‍💼 Admin" if m["sender"] == "admin" else "👤 User")
        lines.append(f"<b>{sender_label}</b> <i>({m['created_at']})</i>\n{html.escape(m['message'])}")
    body = "\n\n".join(lines)

    markup = types.InlineKeyboardMarkup(row_width=1)
    if ticket["status"] != "CLOSED":
        if is_admin:
            markup.add(make_inline_button("💬 Reply", callback_data=f"adm_ticket_reply_{ticket_id}", style="primary"))
            markup.add(make_inline_button("🔒 Close Ticket", callback_data=f"adm_ticket_close_{ticket_id}", style="danger"))
        else:
            markup.add(make_inline_button("💬 রিপ্লাই দিন", callback_data=f"ticket_reply_{ticket_id}", style="primary"))
            markup.add(make_inline_button("🔒 টিকিট বন্ধ করুন", callback_data=f"ticket_close_{ticket_id}", style="danger"))

    if is_admin:
        markup.add(make_inline_button("🔙 Support Tickets", callback_data="adm_tickets_page_0", style="primary"))
    else:
        markup.add(make_inline_button("🔙 My Tickets", callback_data="my_tickets_page_0", style="primary"))

    return body, markup

def build_admin_tickets_view(page: int = 0):
    """অ্যাডমিনের সাপোর্ট টিকিট লিস্ট — OPEN/REPLIED (নজর দরকার) আগে, CLOSED পরে।"""
    open_tickets = admin.get_all_tickets(status="OPEN") + admin.get_all_tickets(status="REPLIED")
    closed_tickets = admin.get_all_tickets(status="CLOSED", limit=30)
    all_tickets = sorted(open_tickets, key=lambda t: t["updated_at"], reverse=True) + closed_tickets

    if not all_tickets:
        return "✅ <b>কোনো সাপোর্ট টিকিট নেই।</b>", None

    items, page, total_pages = admin.paginate(all_tickets, page, per_page=8)
    open_count = len(open_tickets)
    text = f"🎫 <b>Support Tickets</b> — <code>{open_count}</code> টি নজর দরকার (পেজ {page+1}/{total_pages})\n━━━━━━━━━━━━━━━━━━━━━━"
    markup = types.InlineKeyboardMarkup(row_width=1)
    for t in items:
        icon = TICKET_STATUS_ICON.get(t["status"], "⚪")
        label = f"{icon} #{t['ticket_id']} • @{t['username'] or 'N/A'} • {t['subject']}"
        markup.add(make_inline_button(label, callback_data=f"adm_ticket_view_{t['ticket_id']}", style="primary"))

    nav_row = []
    if page > 0:
        nav_row.append(make_inline_button("◀️ Prev", callback_data=f"adm_tickets_page_{page-1}", style="primary"))
    if page < total_pages - 1:
        nav_row.append(make_inline_button("Next ▶️", callback_data=f"adm_tickets_page_{page+1}", style="primary"))
    if nav_row:
        markup.add(*nav_row)
    return text, markup

# ----------------- অ্যাডমিন: পেজিনেটেড পেন্ডিং রিকোয়েস্ট ভিউ (স্প্যাম কমাতে ও সহজে ব্রাউজ করতে) -----------------
def build_pending_requests_view(page: int = 0):
    pending = admin.get_pending_vip_requests()
    if not pending:
        return "✅ <b>কোনো পেন্ডিং সাবস্ক্রিপশন রিকোয়েস্ট নেই।</b>", None

    items, page, total_pages = admin.paginate(pending, page, per_page=3)

    text_parts = [f"📋 <b>পেন্ডিং রিকোয়েস্ট</b> — মোট <code>{len(pending)}</code> টি (পেজ {page+1}/{total_pages})\n━━━━━━━━━━━━━━━━━━━━━━"]
    markup = types.InlineKeyboardMarkup(row_width=2)
    for req in items:
        req_id = req["request_id"]
        text_parts.append(
            f"📦 <b>Req #{req_id}</b> | UID: <code>{req['user_id']}</code> (@{req['username']})\n"
            f"Plan: <code>{req['plan_name']}</code> ({req.get('gateway','bKash')})\n"
            f"Price: <code>{req['price']}</code> | TrxID: <code>{req['trx_id']}</code>"
        )
        markup.add(
            make_inline_button(f"✅ Approve #{req_id}", callback_data=f"adm_app_vip_{req_id}", style="success"),
            make_inline_button(f"❌ Reject #{req_id}", callback_data=f"adm_rej_vip_{req_id}", style="danger")
        )

    nav_row = []
    if page > 0:
        nav_row.append(make_inline_button("◀️ Prev", callback_data=f"adm_reqs_page_{page-1}", style="primary"))
    if page < total_pages - 1:
        nav_row.append(make_inline_button("Next ▶️", callback_data=f"adm_reqs_page_{page+1}", style="primary"))
    if nav_row:
        markup.add(*nav_row)

    return "\n\n".join(text_parts), markup

# ----------------- অ্যাডমিন: পেজিনেটেড ও সার্চযোগ্য ইউজার ডাইরেক্টরি -----------------
def build_user_directory_view(page: int = 0, query: str = ""):
    users = admin.search_users(query) if query else admin.get_all_users()
    total_all = len(admin.get_all_users())

    if not users:
        return f"👥 <b>কোনো ইউজার পাওয়া যায়নি</b> (মোট রেজিস্টার্ড: <code>{total_all}</code>)।", None

    items, page, total_pages = admin.paginate(users, page, per_page=8)

    header = f"👥 <b>ইউজার ডাইরেক্টরি</b> — মোট <code>{total_all}</code> ক্লায়েন্ট"
    if query:
        header += f"\n🔎 সার্চ ফলাফল: <code>{query}</code> ({len(users)} মিলেছে)"
    header += f"\n(পেজ {page+1}/{total_pages})\n━━━━━━━━━━━━━━━━━━━━━━"

    markup = types.InlineKeyboardMarkup(row_width=1)
    for u in items:
        ban_icon = "🚫" if u.get("is_banned") else "🟢"
        label = f"{ban_icon} @{u.get('username') or 'N/A'} • {u.get('plan','FREE')} • {u['user_id']}"
        markup.add(make_inline_button(label, callback_data=f"adm_view_user_{u['user_id']}", style="primary"))

    nav_row = []
    page_prefix = f"adm_users_page_{{}}_" + (query or "-")
    if page > 0:
        nav_row.append(make_inline_button("◀️ Prev", callback_data=f"adm_users_page_{page-1}_{query or '-'}", style="primary"))
    if page < total_pages - 1:
        nav_row.append(make_inline_button("Next ▶️", callback_data=f"adm_users_page_{page+1}_{query or '-'}", style="primary"))
    if nav_row:
        markup.add(*nav_row)

    tip = "\n\n💡 নির্দিষ্ট ইউজার খুঁজতে: <code>/find username_or_id</code>"
    return header + tip, markup

def build_admin_user_profile_card(target_uid: int):
    """অ্যাডমিন ক্লায়েন্ট প্রোফাইল কার্ড — /user কমান্ড ও ইউজার ডাইরেক্টরি দুই জায়গা থেকেই ব্যবহার হয়।"""
    u = admin.get_user(target_uid)
    projects = admin.get_user_projects(target_uid)
    is_ban = bool(u.get("is_banned", 0))

    card = (
        "👤 <b>ADMIN CLIENT PROFILE CARD</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <b>User ID:</b> <code>{target_uid}</code>\n"
        f"👤 <b>Username:</b> @{u.get('username', 'None')}\n"
        f"💎 <b>Plan:</b> <code>{u.get('plan', 'FREE')}</code> (Exp: <code>{u.get('plan_expiry', 'LIFETIME')}</code>)\n"
        f"📦 <b>Total Bots:</b> <code>{len(projects)}</code>\n"
        f"👥 <b>Referrals:</b> <code>{u.get('referral_count', 0)}</code> Clients\n"
        f"🚫 <b>Status:</b> <code>{'🔴 BANNED' if is_ban else '🟢 ACTIVE'}</code>\n"
        f"📅 <b>Joined:</b> <code>{u.get('joined_date', 'Unknown')}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        make_inline_button("💎 Grant Pro (7d)", callback_data=f"adm_grant_pro_{target_uid}", style="success"),
        make_inline_button("💎 Grant Prem (30d)", callback_data=f"adm_grant_vip_{target_uid}", style="success")
    )
    markup.add(
        make_inline_button("🗂 Manage Plan", callback_data=f"adm_plan_menu_{target_uid}", style="primary"),
        make_inline_button("📦 User's Bots", callback_data=f"adm_userbots_{target_uid}", style="primary")
    )
    markup.add(
        make_inline_button("📜 Audit Log", callback_data=f"adm_auditlog_{target_uid}", style="primary"),
        make_inline_button("📩 Send DM", callback_data=f"adm_dm_{target_uid}", style="primary")
    )
    markup.add(
        make_inline_button("🚫 Unban" if is_ban else "🚫 Ban", callback_data=f"adm_ban_confirm_{target_uid}", style="danger")
    )
    markup.add(make_inline_button("🔙 User Directory", callback_data="adm_users_page_0_-", style="primary"))
    return card, markup

# ----------------- ইনস্ট্যান্ট স্টার্ট ও কমান্ড হ্যান্ডলারস (< 0.2s Response) -----------------

def send_onboarding_tutorial(chat_id, user_id):
    """নতুন ইউজারের জন্য ধাপে ধাপে ইন্টারেক্টিভ Tutorial — স্বয়ংক্রিয়ভাবে Welcome-এর পরে আসে।"""
    steps = [
        (
            "🎓 <b>PyHost Tutorial — ধাপ ১/৪</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🤖 <b>বট স্লট কী?</b>\n"
            "প্রতিটা বট স্লট = একটা আলাদা বট চালানোর জায়গা।\n"
            "Free প্ল্যানে ২টা স্লট পাবেন — প্রতিটা দিনে সর্বোচ্চ ১২ ঘণ্টা চলবে।\n\n"
            "👉 <b>পরবর্তী ধাপে যেতে নিচের বাটন চাপুন:</b>",
            "tut_step_2"
        ),
        (
            "🎓 <b>PyHost Tutorial — ধাপ ২/৪</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📤 <b>বট কীভাবে আপলোড করবেন?</b>\n"
            "① <b>🎛️ Control Panel</b> খুলুন\n"
            "② <b>➕ New Bot</b> বা <b>Slot (Free)</b> ট্যাপ করুন\n"
            "③ বটের একটা নাম দিন\n"
            "④ আপনার Python ফাইল (.py) বা .zip পাঠান\n"
            "⑤ বট অটো স্টার্ট হয়ে যাবে! 🚀\n\n"
            "💡 <b>Tip:</b> main.py বা bot.py নামের ফাইল অটো-ডিটেক্ট হয়।",
            "tut_step_3"
        ),
        (
            "🎓 <b>PyHost Tutorial — ধাপ ৩/৪</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🐙 <b>GitHub থেকে ডিপ্লয় (VIP)</b>\n"
            "পেইড প্ল্যানে GitHub repo লিংক দিলেই বট অটো-ডিপ্লয় হবে।\n"
            "পরে repo আপডেট করলে <b>GitHub Sync</b> বাটনে একক ক্লিকেই আপডেট!\n\n"
            "🛒 <b>Marketplace</b> — কোনো কোড ছাড়াই রেডিমেড বট ১ ক্লিকে চালু করুন।\n\n"
            "💎 <b>প্ল্যান আপগ্রেড করলে:</b>\n"
            "• 24/7 বট চালু থাকবে\n"
            "• বেশি স্লট পাবেন\n"
            "• GitHub Deploy ও আরো ফিচার",
            "tut_step_4"
        ),
        (
            "🎓 <b>PyHost Tutorial — ধাপ ৪/৪ (শেষ!)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📋 <b>আপনার করণীয় Checklist:</b>\n"
            "⬜ প্রথম বট তৈরি করুন (Control Panel → New Bot)\n"
            "⬜ একটি .py ফাইল আপলোড করুন\n"
            "⬜ বট রান হতে দেখুন (Bot Dashboard)\n"
            "⬜ বন্ধুকে রেফার করুন (My Account → Share Referral)\n\n"
            "❓ <b>সমস্যা হলে:</b> নিচের মেনু থেকে <b>📖 Documentation</b> বা <b>📡 Support Hub</b> ব্যবহার করুন।\n\n"
            "🎉 সব ঠিকঠাক! শুরু করুন →",
            "tut_done"
        ),
    ]
    # প্রথম ধাপ পাঠাই
    markup = types.InlineKeyboardMarkup()
    markup.add(make_inline_button("▶️ পরের ধাপ →", callback_data=steps[0][1], style="success"))
    markup.add(make_inline_button("⏭️ Tutorial Skip করুন", callback_data="tut_skip", style="primary"))
    bot.send_message(chat_id, steps[0][0], reply_markup=markup, parse_mode="HTML")

ONBOARDING_STEPS = [
    ("tut_step_2", 1),
    ("tut_step_3", 2),
    ("tut_step_4", 3),
    ("tut_done", None),
]

def get_onboarding_step_content(step_key):
    texts = {
        "tut_step_2": (
            "🎓 <b>PyHost Tutorial — ধাপ ২/৪</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📤 <b>বট কীভাবে আপলোড করবেন?</b>\n"
            "① <b>🎛️ Control Panel</b> খুলুন\n"
            "② <b>➕ New Bot</b> বা যেকোনো <b>Slot (Free)</b> ট্যাপ করুন\n"
            "③ বটের একটা নাম দিন\n"
            "④ আপনার Python ফাইল (.py) বা .zip পাঠান\n"
            "⑤ বট অটো স্টার্ট হয়ে যাবে! 🚀\n\n"
            "💡 main.py বা bot.py অটো-ডিটেক্ট হয়।"
        ),
        "tut_step_3": (
            "🎓 <b>PyHost Tutorial — ধাপ ৩/৪</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🐙 <b>GitHub Deploy (VIP)</b> — repo লিংক দিলেই ডিপ্লয়!\n"
            "🛒 <b>Marketplace</b> — কোড ছাড়াই রেডিমেড বট চালু!\n\n"
            "💎 <b>পেইড প্ল্যানে:</b> 24/7, বেশি স্লট, GitHub Sync"
        ),
        "tut_step_4": (
            "🎓 <b>PyHost Tutorial — ধাপ ৪/৪ (শেষ!)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📋 <b>আপনার Checklist:</b>\n"
            "⬜ Control Panel → New Bot → ফাইল আপলোড\n"
            "⬜ বট Dashboard থেকে লগ দেখুন\n"
            "⬜ বন্ধুকে Refer করুন (My Account)\n\n"
            "❓ সমস্যায় → 📖 Documentation / 📡 Support Hub\n"
            "🎉 শুরু করুন!"
        ),
    }
    next_map = {"tut_step_2": "tut_step_3", "tut_step_3": "tut_step_4", "tut_step_4": "tut_done"}
    return texts.get(step_key, ""), next_map.get(step_key)

@bot.message_handler(commands=['start'])

def cmd_start(message):
    user_id = message.from_user.id
    referrer_id = None
    
    args = message.text.split()
    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            referrer_id = int(args[1].replace("ref_", ""))
        except Exception:
            pass

    welcome_text = (
        "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
        "┃ ☁️ PyHost Multi-Bot Cloud 🚀\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "⚡ <b>মাল্টি-বট হোস্টিং</b>\n"
        "একই অ্যাকাউন্টে একাধিক বট চালান।\n\n"
        "📦 <b>স্মার্ট ফাইল আপডেট</b>\n"
        "ফাইল রিপ্লেস, ZIP Backup ও Realtime Logs।\n\n"
        "🛒 <b>বট মার্কেটপ্লেস</b>\n"
        "কোডিং ছাড়াই রেডিমেড বট চালু করুন।\n\n"
        "🎁 <b>রেফারেল বোনাস</b>\n"
        "৩ জন বন্ধুকে Invite করলে ৩ দিন VIP ফ্রি।\n\n"
        "🔐 <b>নিরাপদ হোস্টিং</b>\n"
        "আপনার তথ্য, কোড ও আপলোড করা ফাইল নিরাপদে সংরক্ষিত থাকবে।\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🚀 শুরু করতে নিচের বাটন ব্যবহার করুন।\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )
    bot.send_message(message.chat.id, welcome_text, reply_markup=get_user_keyboard(user_id), parse_mode="HTML")

    def _async_reg():
        is_new_user, bonus_ref_id = admin.register_user(user_id, message.from_user.username, referrer_id)

        # নতুন ইউজার জয়েন-নোটিফিকেশন — সম্পূর্ণ সাইলেন্ট, ইউজার কিছু বুঝবে না
        if is_new_user:
            uname = message.from_user.username
            full_name = " ".join(filter(None, [message.from_user.first_name, message.from_user.last_name])) or "-"
            join_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ref_line = f"🔗 <b>Referred by:</b> <code>{referrer_id}</code>\n" if referrer_id else ""
            notify_text = (
                "🆕 <b>NEW USER JOINED!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🆔 <b>User ID:</b> <code>{user_id}</code>\n"
                f"👤 <b>Username:</b> @{uname or 'Anonymous'}\n"
                f"📛 <b>Name:</b> {html.escape(full_name)}\n"
                f"🕒 <b>Joined:</b> <code>{join_time}</code>\n"
                f"{ref_line}"
                "━━━━━━━━━━━━━━━━━━━━━━"
            )
            for aid in admin.ADMIN_IDS:
                try:
                    bot.send_message(aid, notify_text, parse_mode="HTML")
                except Exception:
                    pass
            # Tutorial শুধু নতুন ইউজারদের জন্য — ৩ সেকেন্ড পর পাঠানো হয়
            import time as _t
            _t.sleep(3)
            try:
                send_onboarding_tutorial(message.chat.id, user_id)
            except Exception:
                pass

        if bonus_ref_id:
            try:
                bot.send_message(
                    bonus_ref_id,
                    f"🎉 <b>অভিনন্দন!</b> ৩ জন সফল রেফারে আপনি <b>{admin.SYSTEM_CONFIG['referral_bonus_days']} দিনের ফ্রি Premium VIP প্ল্যান</b> বোনাস পেয়েছেন! 🚀",
                    parse_mode="HTML"
                )
            except Exception:
                pass

        # রেফারেল অ্যাচিভমেন্ট ব্যাজ (৫ জন / ১০ জন)
        if referrer_id:
            try:
                ref_user = admin.get_user(referrer_id)
                rc = ref_user.get("referral_count") or 0
                for threshold, badge_key in ((5, "REFER_5"), (10, "REFER_10")):
                    if rc >= threshold and admin.award_badge(referrer_id, badge_key):
                        b = admin.ACHIEVEMENTS[badge_key]
                        bot.send_message(
                            referrer_id,
                            f"{b['emoji']} <b>নতুন ব্যাজ অর্জিত: {b['title']}!</b>\n{b['desc']} 🎉",
                            parse_mode="HTML"
                        )
            except Exception:
                pass
    threading.Thread(target=_async_reg, daemon=True).start()

@bot.message_handler(commands=['user'])
def cmd_user_admin_search(message):
    user_id = message.from_user.id
    if not admin.is_admin(user_id):
        return

    args = message.text.split()
    if len(args) < 2:
        bot.send_message(message.chat.id, "ℹ️ <b>Usage:</b> <code>/user &lt;user_id&gt;</code>")
        return

    try:
        target_uid = int(args[1].strip())
    except ValueError:
        bot.send_message(message.chat.id, "❌ Invalid User ID!")
        return

    card, markup = build_admin_user_profile_card(target_uid)
    bot.send_message(message.chat.id, card, reply_markup=markup, parse_mode="HTML")

@bot.message_handler(commands=['find'])
def cmd_find_users(message):
    """অ্যাডমিন ইউজারনেইম বা আইডি দিয়ে সার্চ করতে পারবে (পেজিনেশন সহ)।"""
    user_id = message.from_user.id
    if not admin.is_admin(user_id):
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        bot.send_message(message.chat.id, "ℹ️ <b>Usage:</b> <code>/find &lt;username বা user_idর অংশ&gt;</code>")
        return

    query = args[1].strip()
    text, markup = build_user_directory_view(page=0, query=query)
    bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="HTML")

@bot.message_handler(commands=['redeem'])
def cmd_redeem(message):
    """/redeem            -> কোড লেখার প্রম্পট
    /redeem PYH-XXXX-...  -> সরাসরি রিডিম"""
    user_id = message.from_user.id
    if not check_rate_limit(user_id):
        return
    if admin.get_user(user_id).get("is_banned"):
        bot.send_message(message.chat.id, "🚫 <b>আপনার একাউন্টটি ব্যান করা হয়েছে।</b>")
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        send_redeem_prompt(message.chat.id, user_id)
    else:
        user_states.pop(user_id, None)
        process_user_redeem(message.chat.id, user_id, message.from_user.username, args[1])

# ----------------- টেক্সট ও স্টেট হ্যান্ডলারস -----------------
@bot.message_handler(func=lambda msg: True)
def handle_all_text_navigation(message):
    user_id = message.from_user.id
    if not check_rate_limit(user_id):
        return   # সাইলেন্টলি ইগনোর — স্প্যাম প্রতিরোধ

    message.text = normalize_button_text(message.text)
    text = message.text

    u_data = admin.get_user(user_id)
    if u_data.get("is_banned"):
        bot.send_message(message.chat.id, "🚫 <b>আপনার একাউন্টটি ব্যান করা হয়েছে।</b>")
        return

    # স্টেট হ্যান্ডলারস
    if user_id in user_states:
        state_data = user_states[user_id]
        state_name = state_data if isinstance(state_data, str) else state_data.get("state")

        # ইনপুট-মোডে থাকা অবস্থায় মেনুর বাটন চাপলে ইনপুট-মোড বাতিল হয়ে বাটনটিই কাজ করবে
        if state_name in _NAV_CANCELLABLE_STATES and text in _NAV_BUTTON_TEXTS:
            del user_states[user_id]
            state_name = None

        # ১. অ্যাডমিন গ্লোবাল ব্রডকাস্ট
        if state_name == "awaiting_broadcast" and admin.is_admin(user_id):
            del user_states[user_id]
            users = admin.get_all_users()
            total = len(users)
            
            prog = bot.send_message(message.chat.id, f"📢 <b>Broadcast শুরু হচ্ছে ({total} ইউজার)...</b>", parse_mode="HTML")
            
            def _run_broadcast():
                success = 0
                failed = 0
                for u in users:
                    try:
                        bot.send_message(u["user_id"], f"📢 <b>PYHOST OFFICIAL ANNOUNCEMENT</b>\n━━━━━━━━━━━━━━━━━━━━━━\n{text}\n━━━━━━━━━━━━━━━━━━━━━━", parse_mode="HTML")
                        success += 1
                    except Exception:
                        failed += 1
                    time.sleep(0.04)
                bot.edit_message_text(f"✅ <b>Broadcast সম্পন্ন!</b>\nসফল: <code>{success}</code> | ব্যর্থ: <code>{failed}</code>", message.chat.id, prog.message_id, parse_mode="HTML")

            threading.Thread(target=_run_broadcast, daemon=True).start()
            return

        # ২. অ্যাডমিন ডাইরেক্ট ডিএম
        elif state_name == "awaiting_admin_dm" and admin.is_admin(user_id):
            target_uid = state_data.get("target_uid")
            del user_states[user_id]
            try:
                bot.send_message(target_uid, f"📩 <b>PyHost Support Message from Admin:</b>\n━━━━━━━━━━━━━━━━━━━━━━\n{text}\n━━━━━━━━━━━━━━━━━━━━━━", parse_mode="HTML")
                bot.send_message(message.chat.id, f"✅ ইউজার <code>{target_uid}</code>-কে মেসেজ পাঠানো হয়েছে!")
            except Exception as e:
                bot.send_message(message.chat.id, f"❌ মেসেজ পাঠানো যায়নি: {e}")
            return

        # ৩. অ্যাডমিন ম্যানুয়াল ভিআইপি আইডি
        elif state_name == "awaiting_vip_id" and admin.is_admin(user_id):
            del user_states[user_id]
            try:
                target_id = int(text.strip())
                admin.set_user_plan(target_id, "PREMIUM", 30)
                _award_first_upgrade_badge(target_id)
                bot.send_message(message.chat.id, f"✅ ইউজার <code>{target_id}</code>-কে Premium (30 Days) দেওয়া হয়েছে!")
                try:
                    bot.send_message(target_id, "🎉 <b>অভিনন্দন! আপনার অ্যাকাউন্টটি Premium প্ল্যানে আপগ্রেড করা হয়েছে।</b> (24/7 Unlimited Run)")
                except Exception:
                    pass
            except ValueError:
                bot.send_message(message.chat.id, "❌ অনুগ্রহ করে সঠিক Telegram User ID দিন।")
            return

        # ৪. অ্যাডমিন ব্যান আইডি
        elif state_name == "awaiting_ban_id" and admin.is_admin(user_id):
            del user_states[user_id]
            try:
                target_id = int(text.strip())
                u_info = admin.get_user(target_id)
                new_status = not bool(u_info.get("is_banned", 0))
                admin.set_user_ban(target_id, new_status)
                status_str = "BANNED" if new_status else "UNBANNED"
                bot.send_message(message.chat.id, f"✅ ইউজার <code>{target_id}</code> স্ট্যাটাস: <b>{status_str}</b>!")
            except ValueError:
                bot.send_message(message.chat.id, "❌ অনুগ্রহ করে সঠিক Telegram User ID দিন।")
            return

        elif state_name == "awaiting_new_coupon" and admin.is_admin(user_id):
            del user_states[user_id]
            parts = text.strip().split()
            if len(parts) != 3:
                bot.send_message(message.chat.id, "❌ ভুল ফরম্যাট! এভাবে দিন: <code>CODE DISCOUNT MAX_USES</code>\nউদাহরণ: <code>EID50 50 100</code>", parse_mode="HTML")
                return
            code_in, disc_in, uses_in = parts
            try:
                disc_int = int(disc_in)
                uses_int = int(uses_in)
            except ValueError:
                bot.send_message(message.chat.id, "❌ DISCOUNT ও MAX_USES অবশ্যই সংখ্যা হতে হবে।")
                return
            success, msg = admin.create_coupon(code_in, disc_int, uses_int, user_id)
            bot.send_message(message.chat.id, msg, parse_mode="HTML")
            return

        elif state_name == "awaiting_deactivate_coupon" and admin.is_admin(user_id):
            del user_states[user_id]
            code_in = text.strip().upper()
            if admin.deactivate_coupon(code_in):
                bot.send_message(message.chat.id, f"✅ কুপন <code>{html.escape(code_in)}</code> বাতিল করা হয়েছে।", parse_mode="HTML")
            else:
                bot.send_message(message.chat.id, f"❌ <code>{html.escape(code_in)}</code> নামে কোনো কুপন পাওয়া যায়নি।", parse_mode="HTML")
            return

        # ৫. কুপন কোড ইনপুট হ্যান্ডলার
        elif state_name == "awaiting_coupon_code":
            tier_key = state_data["tier_key"]
            coupon_input = text.strip().upper()
            del user_states[user_id]

            # DB-backed কুপন চেক — আগে hardcoded dict ছিল, এখন use_coupon() দিয়ে
            # একই কুপন একই ইউজার বারবার ব্যবহার করতে পারবে না, ম্যাক্স ইউজ লিমিটও চেক হয়
            success, result = admin.use_coupon(coupon_input, user_id)
            if success:
                disc = result  # disc = discount percentage (int)
                bot.send_message(
                    message.chat.id,
                    f"🎉 <b>কুপন সফল!</b> আপনি <b>{disc}%</b> ডিসকাউন্ট পেয়েছেন!",
                    parse_mode="HTML"
                )
                sum_text, sum_markup = get_order_summary_view(tier_key, discount_percent=disc)
                bot.send_message(message.chat.id, sum_text, reply_markup=sum_markup, parse_mode="HTML")
            else:
                bot.send_message(message.chat.id, f"❌ <b>কুপন ব্যর্থ:</b> {html.escape(str(result))}", parse_mode="HTML")
                sum_text, sum_markup = get_order_summary_view(tier_key, discount_percent=0)
                bot.send_message(message.chat.id, sum_text, reply_markup=sum_markup, parse_mode="HTML")
            return


        # ৬. নতুন বট তৈরির নাম
        elif state_name == "awaiting_bot_name":
            bot_name = text.strip()[:30]
            safe_bot_name = html.escape(bot_name)  # HTML পার্সিং ভাঙা এড়াতে ইউজার-দেওয়া নাম এসকেপ করা হচ্ছে
            target_slot = state_data.get("slot_number") if isinstance(state_data, dict) else None
            pid = admin.create_user_project(user_id, bot_name, slot_number=target_slot)
            user_states[user_id] = {"state": "awaiting_file_for_bot", "project_id": pid, "ts": time.time()}
            admin.set_checklist_flag(user_id, "bot_created")
            card_text, card_markup = get_upload_guide_card()
            created_box = (
                "╭━━━━━━━━━━━━━━━━━━╮\n"
                f"┃  ✅ \'<b>{safe_bot_name}</b>\' প্রজেক্ট তৈরি হয়েছে!\n"
                "╰━━━━━━━━━━━━━━━━━━╯\n"
            )
            bot.send_message(message.chat.id, created_box + card_text, reply_markup=card_markup, parse_mode="HTML")
            return

        # ৭. সরাসরি কোড পেস্ট করে ডেপ্লয়
        elif state_name == "awaiting_file_for_bot" and ("import " in text or "def " in text or "bot =" in text or "print(" in text) \
                and time.time() - state_data.get("ts", time.time()) <= UPLOAD_EXPLICIT_TTL \
                and (admin.get_project_by_id(state_data.get("project_id")) or {}).get("user_id") == user_id:
            pid = state_data.get("project_id")
            del user_states[user_id]
            p_dir = os.path.join(PROJECTS_DIR, f"{user_id}_{pid}")
            os.makedirs(p_dir, exist_ok=True)
            main_path = os.path.join(p_dir, "main.py")
            with open(main_path, "w", encoding="utf-8") as cf:
                cf.write(text)
            # পেস্ট করা কোডের কপিও Database চ্যানেলে
            queue_user_code_copy(message.from_user, pid, "main.py", text.encode("utf-8"), source="Pasted Code")

            prog_msg = bot.send_message(message.chat.id, render_upload_progress("main.py", pid, completed={"save"}, current="scan", elapsed=0), parse_mode="HTML")
            ticker = LiveDeployTicker(message.chat.id, prog_msg.message_id, "main.py", pid, completed={"save"}, current="scan")
            detected = scan_python_imports(main_path)
            if detected:
                ticker.update(completed={"save", "scan"}, current="install", note=f" ({', '.join(detected)})")
                install_missing_packages_sync(detected)

            ticker.update(completed={"save", "scan", "install"}, current="run")
            ok_run, run_msg = execute_project_script(user_id, pid, "main.py")
            ticker.stop()
            if not ok_run:
                _remember_upload_target(user_id, pid)   # কোড ঠিক করে আবার পেস্ট করলে একই বটে যাবে
                bot.edit_message_text(run_msg, message.chat.id, prog_msg.message_id, reply_markup=_back_cp_markup(), parse_mode="HTML")
                return

            success_card, markup = build_deploy_success_card(pid, "main.py", verify_token=False)
            bot.edit_message_text(success_card, message.chat.id, prog_msg.message_id, reply_markup=markup, parse_mode="HTML")
            refresh_deploy_card_async(message.chat.id, prog_msg.message_id, pid, "main.py")
            return

        # ৮. মার্কেটপ্লেস অটো-ডেপ্লয় টোকেন
        elif state_name == "awaiting_deploy_token":
            bot_token = text.strip()
            template_type = state_data["template"]
            del user_states[user_id]

            if not re.match(r"^\d{8,10}:[a-zA-Z0-9_-]{35}$", bot_token):
                bot.send_message(message.chat.id, "❌ <b>ভুল বট টোকেন ফরম্যাট!</b> @BotFather থেকে সঠিক টোকেন এনে আবার চেষ্টা করুন।", parse_mode="HTML")
                return

            # টোকেন ভ্যালিডেশন — Telegram API-তে চেক করে আসল/কাজের টোকেন কিনা
            # এটা না থাকলে ভুল টোকেন দিয়ে স্লট নষ্ট হয়ে যেতে পারে
            v_msg = bot.send_message(message.chat.id, "🔍 <b>টোকেন যাচাই করা হচ্ছে...</b>", parse_mode="HTML")
            is_valid, val_info = validate_bot_token(bot_token)
            try:
                bot.delete_message(message.chat.id, v_msg.message_id)
            except Exception:
                pass
            if not is_valid:
                bot.send_message(
                    message.chat.id,
                    f"❌ <b>টোকেন ভ্যালিড নয়!</b>\n{html.escape(val_info)}\n\n"
                    "@BotFather থেকে সঠিক টোকেন এনে আবার চেষ্টা করুন।",
                    parse_mode="HTML"
                )
                return


            # স্লট-লিমিট চেক (আগে এই চেকটা মার্কেটপ্লেস ফ্লো-তে ছিলই না — ফলে FREE ইউজারও
            # প্ল্যান-লিমিট বাইপাস করে ইচ্ছামতো বট ডিপ্লয় করে ফেলতে পারত)
            u_data = admin.get_user(user_id)
            plan = u_data.get("plan", "FREE").upper()
            max_bots = admin.get_max_bot_limit_for_user(u_data)
            projects = admin.get_user_projects(user_id)
            if len(projects) >= max_bots:
                bot.send_message(
                    message.chat.id,
                    "⚠️ <b>আপনার বটের স্লট লিমিট পূর্ণ হয়ে গেছে!</b>\n"
                    "মার্কেটপ্লেস থেকে বট ডিপ্লয় করতেও ফাঁকা স্লট লাগবে — 💎 <b>Upgrade Plan</b> থেকে "
                    "প্ল্যান আপগ্রেড করুন অথবা কোনো একটি বট ডিলিট করে জায়গা খালি করুন।",
                    parse_mode="HTML"
                )
                return
            # সব ১০টা স্লট (হার্ড ক্যাপ) পূর্ণ থাকলে slot_number=None দিয়ে "ভুতুড়ে" প্রজেক্ট
            # তৈরি হয়ে যেত, যা Control Panel-এর স্লট-বেজড UI-তে কখনো দেখাই যেত না
            if admin.get_next_free_slot(user_id) is None:
                bot.send_message(message.chat.id, "⚠️ <b>আপনার সবগুলো বট স্লট (সর্বোচ্চ ১০টি) পূর্ণ!</b> নতুন বট ডিপ্লয় করার আগে একটি ডিলিট করুন।", parse_mode="HTML")
                return

            status_msg = bot.send_message(message.chat.id, "⏳ <b>বটের ফাইল তৈরি ও লাইভ পরিবেশ সেটআপ হচ্ছে...</b>", parse_mode="HTML")

            tpl_def = MARKETPLACE_TEMPLATES.get(template_type) or MARKETPLACE_TEMPLATES["echo"]
            proj_name = tpl_def["proj_name"]
            bot_code = tpl_def["code"].replace("__TOKEN__", bot_token)

            pid = admin.create_user_project(user_id, proj_name)
            p_dir = os.path.join(PROJECTS_DIR, f"{user_id}_{pid}")
            main_path = os.path.join(p_dir, "main.py")
            with open(main_path, "w", encoding="utf-8") as f:
                f.write(bot_code)

            # ⭐ কিছু অ্যাডভান্স টেমপ্লেটে telebot ছাড়াও বাড়তি লাইব্রেরি লাগে (যেমন Weather Bot-এ
            # requests) — তাই রান করার আগে ইমপোর্ট স্ক্যান করে দরকারি প্যাকেজ ইনস্টল করে নেওয়া হয়।
            detected = scan_python_imports(main_path)
            if detected:
                bot.edit_message_text("📦 <b>দরকারি প্যাকেজ ইনস্টল হচ্ছে...</b>", message.chat.id, status_msg.message_id, parse_mode="HTML")
                install_missing_packages_sync(detected)

            ok_run, run_msg = execute_project_script(user_id, pid, "main.py")
            if not ok_run:
                admin.delete_user_project(pid)
                bot.edit_message_text(
                    f"{run_msg}\n\n(কোনো স্লট নষ্ট হয়নি।)",
                    message.chat.id, status_msg.message_id, parse_mode="HTML"
                )
                return

            bot.edit_message_text(
                f"🎉 <b>অভিনন্দন! আপনার '{proj_name}' সফলভাবে ডেপ্লয় হয়েছে!</b>\n\n"
                f"🚀 বটটি এখন লাইভ চলছে। 🎛️ <b>Control Panel</b> থেকে এটি পরিচালনা করতে পারবেন।",
                message.chat.id,
                status_msg.message_id,
                parse_mode="HTML"
            )
            return

        # ৯. পেমেন্ট TrxID সাবমিশন
        elif state_name == "awaiting_vip_trx":
            del user_states[user_id]
            trx_id = text.strip()
            plan_tier = state_data["plan_tier"]
            gateway = state_data.get("gateway", "bKash")
            price_text = state_data.get("price", "30 BDT")
            tier_info = admin.TIER_PLANS.get(plan_tier, admin.TIER_PLANS["PREMIUM"])
            days = tier_info["days"]
            plan_name = tier_info["name"]

            req_id = admin.create_vip_request(user_id, message.from_user.username, plan_name, days, price_text, gateway, trx_id, tier_key=plan_tier)

            bot.send_message(
                message.chat.id,
                "✅ <b>আপনার TrxID সফলভাবে জমা হয়েছে!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📦 <b>প্যাকেজ:</b> {plan_name}\n"
                f"💳 <b>মেথড:</b> {gateway}\n"
                f"💵 <b>মূল্য:</b> {price_text}\n"
                f"📝 <b>TrxID:</b> <code>{trx_id}</code>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "⏳ <i>অ্যাডমিন ভেরিফাই করে অ্যাপ্রুভ করলেই আপনার প্ল্যান সক্রিয় হয়ে যাবে।</i>",
                parse_mode="HTML"
            )

            admin_markup = types.InlineKeyboardMarkup(row_width=2)
            admin_markup.add(
                make_inline_button("✅ Approve", callback_data=f"adm_app_vip_{req_id}", style="success"),
                make_inline_button("❌ Reject", callback_data=f"adm_rej_vip_{req_id}", style="danger")
            )

            admin_alert = (
                f"🔔 <b>NEW SUBSCRIPTION ORDER!</b> (#REQ-{req_id})\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>Client:</b> @{message.from_user.username or 'Anonymous'} (ID: <code>{user_id}</code>)\n"
                f"📦 <b>Tier:</b> <code>{plan_name}</code> ({days} Days)\n"
                f"💳 <b>Gateway:</b> <code>{gateway}</code>\n"
                f"💵 <b>Amount:</b> <code>{price_text}</code>\n"
                f"📝 <b>TrxID:</b> <code>{trx_id}</code>\n"
                "━━━━━━━━━━━━━━━━━━━━━━"
            )

            for aid in admin.ADMIN_IDS:
                try:
                    bot.send_message(aid, admin_alert, reply_markup=admin_markup, parse_mode="HTML")
                except Exception:
                    pass
            return

        # স্লট মেয়াদ বৃদ্ধি (Extend Validity) পেমেন্ট TrxID সাবমিশন
        elif state_name == "awaiting_ext_trx":
            del user_states[user_id]
            trx_id = text.strip()
            pid = state_data["project_id"]
            plan_key = state_data["plan_key"]
            plan_d = admin.SLOT_EXTEND_PLANS.get(plan_key, admin.SLOT_EXTEND_PLANS["30D"])
            project = admin.get_project_by_id(pid)
            proj_name = project["project_name"] if project else f"Bot #{pid}"

            req_id = admin.create_renewal_request(user_id, message.from_user.username, pid, proj_name, plan_d["days"], plan_d["price"], "Manual", trx_id)

            bot.send_message(
                message.chat.id,
                "✅ <b>আপনার TrxID সফলভাবে জমা হয়েছে!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🤖 <b>বট:</b> {proj_name}\n"
                f"📦 <b>প্যাকেজ:</b> {plan_d['label']}\n"
                f"💵 <b>মূল্য:</b> {plan_d['price']}\n"
                f"📝 <b>TrxID:</b> <code>{trx_id}</code>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "⏳ <i>অ্যাডমিন ভেরিফাই করে অ্যাপ্রুভ করলেই মেয়াদ যোগ হয়ে যাবে।</i>",
                parse_mode="HTML"
            )

            admin_markup = types.InlineKeyboardMarkup(row_width=2)
            admin_markup.add(
                make_inline_button("✅ Approve", callback_data=f"adm_app_ext_{req_id}", style="success"),
                make_inline_button("❌ Reject", callback_data=f"adm_rej_ext_{req_id}", style="danger")
            )

            admin_alert = (
                f"🔔 <b>NEW SLOT RENEWAL REQUEST!</b> (#EXT-{req_id})\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>Client:</b> @{message.from_user.username or 'Anonymous'} (ID: <code>{user_id}</code>)\n"
                f"🤖 <b>Bot:</b> <code>{proj_name}</code> (PID: {pid})\n"
                f"📦 <b>Package:</b> <code>{plan_d['label']}</code>\n"
                f"💵 <b>Amount:</b> <code>{plan_d['price']}</code>\n"
                f"📝 <b>TrxID:</b> <code>{trx_id}</code>\n"
                "━━━━━━━━━━━━━━━━━━━━━━"
            )

            for aid in admin.ADMIN_IDS:
                try:
                    bot.send_message(aid, admin_alert, reply_markup=admin_markup, parse_mode="HTML")
                except Exception:
                    pass
            return

        elif state_name == "awaiting_github_url":
            raw_url = text.strip()
            # github.com/owner/repo বা https://github.com/owner/repo দুটো ফরম্যাটই সাপোর্ট করা হচ্ছে
            m = re.match(r"(?:https?://)?github\.com/([\w.\-]+)/([\w.\-]+?)(?:\.git|/.*)?$", raw_url)
            if not m:
                markup = types.InlineKeyboardMarkup(row_width=1)
                markup.add(make_inline_button("❌ বাতিল করুন", callback_data="gh_cancel", style="danger"))
                bot.send_message(
                    message.chat.id,
                    "❌ <b>ভুল ফরম্যাট!</b> GitHub রিপোর লিংক এভাবে দিন:\n"
                    "<code>https://github.com/owner/repo</code>\n\n"
                    "🔄 আবার সঠিক লিংক পাঠান বা নিচের বাটনে বাতিল করুন।",
                    reply_markup=markup, parse_mode="HTML"
                )
                # state মুছে দেওয়া হচ্ছে না — ইউজার retry করতে পারবে
                return
            owner, repo = m.group(1), m.group(2)
            user_states[user_id] = {"state": "awaiting_github_visibility", "owner": owner, "repo": repo}
            markup = types.InlineKeyboardMarkup(row_width=2)
            markup.add(
                make_inline_button("🔓 Public Repo", callback_data="gh_repo_public", style="success"),
                make_inline_button("🔒 Private Repo", callback_data="gh_repo_private", style="primary")
            )
            markup.add(make_inline_button("🔙 Back / Cancel", callback_data="gh_cancel", style="danger"))
            bot.send_message(
                message.chat.id,
                f"🐙 <b>{html.escape(owner)}/{html.escape(repo)}</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "রিপোটি Public নাকি Private?",
                reply_markup=markup, parse_mode="HTML"
            )
            return

        elif state_name == "awaiting_github_token":
            token = text.strip()
            owner, repo = state_data["owner"], state_data["repo"]
            del user_states[user_id]
            # টোকেন মেসেজটা সাথে সাথে ডিলিট করে দেওয়া হচ্ছে (নিরাপত্তার জন্য)
            try:
                bot.delete_message(message.chat.id, message.message_id)
            except Exception:
                pass
            status_msg = bot.send_message(
                message.chat.id,
                f"⏳ <b>GitHub থেকে '{html.escape(owner)}/{html.escape(repo)}' ডাউনলোড হচ্ছে...</b>",
                parse_mode="HTML"
            )
            threading.Thread(
                target=deploy_github_repo,
                args=(user_id, message.chat.id, status_msg.message_id, owner, repo, token, True),
                daemon=True
            ).start()
            return
        elif state_name == "awaiting_bot_notes":
            pid = state_data["pid"]
            del user_states[user_id]
            notes_text = text.strip()[:300]
            admin.set_bot_notes(pid, notes_text)
            admin.set_checklist_flag(user_id, "added_notes")
            bot.send_message(message.chat.id, f"📝 <b>নোট সেভ হয়েছে:</b> {html.escape(notes_text)}", parse_mode="HTML")
            return

        elif state_name == "awaiting_sync_token":
            token = text.strip()
            pid = state_data["pid"]
            del user_states[user_id]
            # টোকেন মেসেজ সাথে সাথে ডিলিট (নিরাপত্তা)
            try:
                bot.delete_message(message.chat.id, message.message_id)
            except Exception:
                pass
            project = admin.get_project_by_id(pid)
            proj_name = project["project_name"] if project else f"Bot #{pid}"
            status_msg = bot.send_message(
                message.chat.id,
                f"🔄 <b>GitHub Sync শুরু হচ্ছে...</b>\n📦 <code>{html.escape(proj_name)}</code>",
                parse_mode="HTML"
            )
            threading.Thread(
                target=sync_github_repo,
                args=(user_id, pid, message.chat.id, status_msg.message_id, token),
                daemon=True
            ).start()
            return


        elif state_name == "awaiting_env_var":
            pid = state_data["pid"]
            del user_states[user_id]
            # মেসেজ সাথে সাথে ডিলিট করা (value-তে sensitive info থাকতে পারে)
            try:
                bot.delete_message(message.chat.id, message.message_id)
            except Exception:
                pass
            raw = text.strip()
            if "=" not in raw:
                bot.send_message(message.chat.id, "❌ ভুল ফরম্যাট। <code>KEY=VALUE</code> আকারে পাঠান।", parse_mode="HTML")
                return
            key, _, value = raw.partition("=")
            key = key.strip().upper()
            if not key:
                bot.send_message(message.chat.id, "❌ KEY খালি হতে পারবে না।")
                return
            admin.set_env_var(pid, key, value.strip())
            admin.set_checklist_flag(user_id, "env_var_set")
            bot.send_message(
                message.chat.id,
                f"✅ <b>Env Variable সেভ হয়েছে!</b>\n"
                f"🔑 <code>{html.escape(key)}</code> = <code>{'*' * min(len(value), 8)}</code>\n"
                "বট পরের বার রান হওয়ার সময় এটা .env ফাইলে লেখা হবে।",
                parse_mode="HTML"
            )
            return

        elif state_name == "awaiting_env_delete":
            pid = state_data["pid"]
            del user_states[user_id]
            key = text.strip().upper()
            if admin.delete_env_var(pid, key):
                bot.send_message(message.chat.id, f"✅ <code>{html.escape(key)}</code> মুছে ফেলা হয়েছে।", parse_mode="HTML")
            else:
                bot.send_message(message.chat.id, f"❌ <code>{html.escape(key)}</code> নামে কোনো variable নেই।", parse_mode="HTML")
            return

        elif state_name == "awaiting_daily_report_time":
            del user_states[user_id]
            import re as _re
            time_input = text.strip()
            if not _re.match(r"^([01]?[0-9]|2[0-3]):[0-5][0-9]$", time_input):
                bot.send_message(message.chat.id, "❌ ভুল ফরম্যাট। HH:MM আকারে দিন, যেমন <code>08:00</code>", parse_mode="HTML")
                return
            # HH:MM নর্মালাইজ
            h, m = time_input.split(":")
            normalized = f"{int(h):02d}:{int(m):02d}"
            admin.set_daily_report_time(user_id, normalized)
            bot.send_message(
                message.chat.id,
                f"✅ <b>Daily Report সেট হয়েছে!</b>\n"
                f"⏰ প্রতিদিন <b>{normalized}</b> (Bangladesh Time)-এ রিপোর্ট পাবেন।",
                parse_mode="HTML"
            )
            return

        # ======================== ব্যাচ ৩ — Feedback Comment ========================
        elif state_name == "awaiting_feedback_comment":
            rating = state_data.get("rating", 5)
            star_str = "⭐" * rating
            del user_states[user_id]
            comment = "" if text.strip().lower() == "skip" else text.strip()[:300]
            admin.add_feedback(user_id, message.from_user.username or "Anonymous", rating, comment)
            bot.send_message(
                message.chat.id,
                f"{star_str} <b>\u09a7\u09a8\u09cd\u09af\u09ac\u09be\u09a6! \u0986\u09aa\u09a8\u09be\u09b0 \u09ab\u09bf\u09a1\u09ac\u09cd\u09af\u09be\u0995 \u09b8\u09c7\u09ad \u0995\u09b0\u09be \u09b9\u09df\u09c7\u099b\u09c7\u0964</b>",
                parse_mode="HTML"
            )
            for aid in admin.ADMIN_IDS:
                try:
                    bot.send_message(aid, f"\u2b50 <b>New Feedback</b> ({rating}/5) from <code>{user_id}</code>:\n{html.escape(comment) if comment else '<i>No comment</i>'}", parse_mode="HTML")
                except Exception:
                    pass
            return

        # ======================== \u09ac\u099f Token Change ========================
        elif state_name == "awaiting_new_token":
            pid = state_data.get("pid")
            del user_states[user_id]
            try:
                bot.delete_message(message.chat.id, message.message_id)
            except Exception:
                pass
            new_token = text.strip()
            status_msg = bot.send_message(message.chat.id, "\u23f3 \u099f\u09cb\u0995\u09c7\u09a8 \u09af\u09be\u099a\u09be\u0987 \u0995\u09b0\u09be \u09b9\u099a\u09cd\u099b\u09c7...")
            is_valid, info = validate_bot_token(new_token)
            if not is_valid:
                bot.edit_message_text(f"\u274c \u099f\u09cb\u0995\u09c7\u09a8 \u0985\u09ac\u09c8\u09a7: {info}", message.chat.id, status_msg.message_id)
                return
            admin.set_env_var(pid, "BOT_TOKEN", new_token)
            project = admin.get_project_by_id(pid)
            p_key = f"{user_id}_{pid}"
            was_running = p_key in active_processes
            restart_ok, restart_msg = (True, None)
            if was_running:
                restart_ok, restart_msg = execute_project_script(user_id, pid, preserve_restarts=True)
            if was_running and restart_ok:
                extra_line = "\U0001F504 \u09ac\u099f \u09b0\u09bf\u09b8\u09cd\u099f\u09be\u09b0\u09cd\u099f \u0995\u09b0\u09be \u09b9\u09df\u09c7\u099b\u09c7 \u09a8\u09a4\u09c1\u09a8 \u099f\u09cb\u0995\u09c7\u09a8 \u09b8\u09b9\u0964"
            elif was_running and not restart_ok:
                extra_line = f"\u26a0\ufe0f {restart_msg}"
            else:
                extra_line = "\u09a8\u09c7\u0995\u09cd\u09b8\u099f \u09ac\u09be\u09b0 \u09ac\u099f Start \u0995\u09b0\u09b2\u09c7 \u09a8\u09a4\u09c1\u09a8 \u099f\u09cb\u0995\u09c7\u09a8 \u09ac\u09cd\u09af\u09ac\u09b9\u09c3\u09a4 \u09b9\u09ac\u09c7\u0964"
            bot.edit_message_text(
                f"\u2705 <b>\u099f\u09cb\u0995\u09c7\u09a8 \u0986\u09aa\u09a1\u09c7\u099f \u09b9\u09df\u09c7\u099b\u09c7!</b>\n"
                f"\U0001F916 \u09ac\u099f: <code>{html.escape(project['project_name']) if project else pid}</code>\n"
                f"\u2705 \u09a8\u09a4\u09c1\u09a8 \u099f\u09cb\u0995\u09c7\u09a8: {info}\n"
                + extra_line,
                message.chat.id, status_msg.message_id, parse_mode="HTML"
            )
            return

        # ======================== Bot Clone \u2014 \u09a8\u09be\u09ae \u0987\u09a8\u09aa\u09c1\u099f ========================
        elif state_name == "awaiting_clone_name":
            source_pid = state_data.get("source_pid")
            del user_states[user_id]
            new_name = text.strip()[:40] or "Cloned Bot"
            new_pid, slot = admin.clone_project_record(user_id, source_pid, new_name)
            if not new_pid:
                bot.send_message(message.chat.id, "\u274c \u0995\u09cd\u09b2\u09cb\u09a8 \u09ac\u09cd\u09af\u09b0\u09cd\u09a5: \u0995\u09cb\u09a8\u09cb \u0996\u09be\u09b2\u09bf \u09b8\u09cd\u09b2\u099f \u09a8\u09c7\u0987 \u0985\u09a5\u09ac\u09be \u09ae\u09c2\u09b2 \u09ac\u099f\u099f\u09bf \u0996\u09c1\u0981\u099c\u09c7 \u09aa\u09be\u0993\u09df\u09be \u09af\u09be\u09df\u09a8\u09bf\u0964", parse_mode="HTML")
                return
            # সোর্স প্রজেক্টের ফাইল ফোল্ডার কপি
            src_dir = os.path.join(PROJECTS_DIR, f"{user_id}_{source_pid}")
            dst_dir = os.path.join(PROJECTS_DIR, f"{user_id}_{new_pid}")
            try:
                if os.path.exists(src_dir):
                    shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True)
                else:
                    os.makedirs(dst_dir, exist_ok=True)
            except Exception:
                pass
            bot.send_message(
                message.chat.id,
                f"\u2705 <b>\u09ac\u099f \u0995\u09cd\u09b2\u09cb\u09a8 \u09b8\u09ab\u09b2 \u09b9\u09df\u09c7\u099b\u09c7!</b>\n"
                f"\U0001F195 \u09a8\u09a4\u09c1\u09a8 \u09ac\u099f: <code>{html.escape(new_name)}</code> (Slot {slot})\n"
                "\U0001F4C1 \u09b8\u09ac \u09ab\u09be\u0987\u09b2 \u0993 env variable \u0995\u09aa\u09bf \u09b9\u09df\u09c7\u099b\u09c7\u0964 \U0001F3ae Control Panel \u09a5\u09c7\u0995\u09c7 \u0928\u09a4\u09c1\u09a8 \u09b8\u09cd\u09b2\u099f Start \u0995\u09b0\u09c1\u09a8\u0964",
                parse_mode="HTML"
            )
            return

        # ======================== Scheduled Start/Stop \u2014 \u09b8\u09ae\u09df \u0987\u09a8\u09aa\u09c1\u099f ========================
        elif state_name in ("awaiting_sched_start", "awaiting_sched_stop"):
            pid = state_data.get("pid")
            del user_states[user_id]
            import re as _re2
            time_input = text.strip()
            if not _re2.match(r"^([01]?[0-9]|2[0-3]):[0-5][0-9]$", time_input):
                bot.send_message(message.chat.id, "\u274c \u09ad\u09c1\u09b2 \u09ab\u09b0\u09ae\u09cd\u09af\u09be\u099f\u0964 HH:MM \u0986\u0995\u09be\u09b0\u09c7 \u09a6\u09bf\u09a8, \u09af\u09c7\u09ae\u09a8 <code>22:00</code>", parse_mode="HTML")
                return
            h, m = time_input.split(":")
            normalized = f"{int(h):02d}:{int(m):02d}"
            project = admin.get_project_by_id(pid)
            cur_start = project.get("sched_start_time") if project else None
            cur_stop = project.get("sched_stop_time") if project else None
            if state_name == "awaiting_sched_start":
                admin.set_bot_schedule(pid, normalized, cur_stop, enabled=True)
                label = "Start"
            else:
                admin.set_bot_schedule(pid, cur_start, normalized, enabled=True)
                label = "Stop"
            bot.send_message(
                message.chat.id,
                f"\u2705 <b>Scheduled {label} Time \u09b8\u09c7\u099f \u09b9\u09df\u09c7\u099b\u09c7!</b>\n\u23f0 \u09aa\u09cd\u09b0\u09a4\u09bf\u09a6\u09bf\u09a8 <b>{normalized}</b> (Bangladesh Time)-\u098f \u098f\u0987 \u0985\u09cd\u09af\u09be\u0995\u09b6\u09a8 \u09b9\u09ac\u09c7\u0964",
                parse_mode="HTML"
            )
            return

        # ======================== Auto-Broadcast \u09b6\u09bf\u09a1\u09bf\u0989\u09b2 \u2014 \u09ae\u09c7\u09b8\u09c7\u099c \u09a7\u09be\u09aa \u0993 \u09b8\u09ae\u09df ========================
        elif state_name == "awaiting_sched_broadcast_msg" and admin.is_admin(user_id):
            user_states[user_id] = {"state": "awaiting_sched_broadcast_time", "msg": text}
            bot.send_message(
                message.chat.id,
                "\u23f0 <b>\u0995\u0996\u09a8 \u09aa\u09be\u09a0\u09be\u09a4\u09c7 \u099a\u09be\u09a8?</b>\n\u098f\u0987 \u09ab\u09b0\u09ae\u09cd\u09af\u09be\u099f\u09c7 \u09a6\u09bf\u09a8: <code>YYYY-MM-DD HH:MM</code>\n\u0989\u09a6\u09be\u09b9\u09b0\u09a3: <code>2026-09-20 09:00</code>",
                parse_mode="HTML"
            )
            return

        elif state_name == "awaiting_sched_broadcast_time" and admin.is_admin(user_id):
            msg_text = state_data.get("msg", "")
            del user_states[user_id]
            send_at = text.strip()
            try:
                datetime.strptime(send_at, "%Y-%m-%d %H:%M")
            except Exception:
                bot.send_message(message.chat.id, "\u274c \u09ad\u09c1\u09b2 \u09ab\u09b0\u09ae\u09cd\u09af\u09be\u099f\u0964 \u0989\u09a6\u09be\u09b9\u09b0\u09a3: <code>2026-09-20 09:00</code>", parse_mode="HTML")
                return
            admin.create_scheduled_broadcast(msg_text, send_at, user_id)
            bot.send_message(
                message.chat.id,
                f"✅ <b>Broadcast শিডিউল হয়েছে!</b>\n⏰ পাঠানো হবে: <code>{send_at}</code>\n📝 মেসেজ: {html.escape(msg_text[:200])}",
                parse_mode="HTML"
            )
            return

        # 💼 Dev Studio — ইউজারের কাস্টম বট রিকোয়ারমেন্ট → সাপোর্ট টিকিট + অ্যাডমিন অ্যালার্ট
        elif state_name == "awaiting_devstudio_request":
            body = (text or "").strip()
            if len(body) < 15:
                bot.send_message(
                    message.chat.id,
                    "⚠️ <b>রিকোয়ারমেন্ট খুব ছোট!</b> বটের ধরন, ফিচার, বাজেট ও ডেডলাইন সহ একটু বিস্তারিত লিখে আবার পাঠান।",
                    parse_mode="HTML"
                )
                return
            del user_states[user_id]
            ticket_id = admin.create_support_ticket(user_id, message.from_user.username, "💼 [Dev Studio Request]\n" + body[:3500])
            ok_markup = types.InlineKeyboardMarkup(row_width=1)
            ok_markup.add(
                make_inline_button("🎫 My Tickets", callback_data="my_tickets_page_0", style="primary"),
                make_inline_button("🏠 Home Menu", callback_data="go_home", style="success"),
            )
            bot.send_message(
                message.chat.id,
                f"✅ <b>রিকোয়ারমেন্ট জমা হয়েছে!</b> (টিকিট #{ticket_id})\n━━━━━━━━━━━━━━━━━━━━━━\n"
                "💼 আমাদের ডেভেলপার টিম রিভিউ করে শীঘ্রই আপনাকে রিপ্লাই দেবে।\n"
                "রিপ্লাই ও স্ট্যাটাস দেখতে <b>🎫 My Tickets</b> চেক করুন।",
                reply_markup=ok_markup, parse_mode="HTML"
            )
            admin_alert = (
                f"💼 <b>নতুন Dev Studio রিকোয়ারমেন্ট!</b> (#{ticket_id})\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>ইউজার:</b> @{html.escape(message.from_user.username or 'Anonymous')} (ID: <code>{user_id}</code>)\n"
                f"📝 <b>রিকোয়ারমেন্ট:</b> {html.escape(body[:600])}"
            )
            admin_markup = types.InlineKeyboardMarkup()
            admin_markup.add(make_inline_button("💬 View / Reply", callback_data=f"adm_ticket_view_{ticket_id}", style="primary"))
            for aid in admin.ADMIN_IDS:
                try:
                    bot.send_message(aid, admin_alert, reply_markup=admin_markup, parse_mode="HTML")
                except Exception:
                    pass
            return

        # 🎁 ইউজার রিডিম কোড লিখেছে
        elif state_name == "awaiting_redeem_input":
            del user_states[user_id]
            process_user_redeem(message.chat.id, user_id, message.from_user.username, text or "")
            return

        # 🎁 অ্যাডমিন: কাস্টম মেয়াদ (দিন) লিখেছে
        elif state_name == "awaiting_redeem_days" and admin.is_admin(user_id):
            tier_key = state_data.get("tier")
            raw = (text or "").strip()
            if not raw.isdigit() or not (1 <= int(raw) <= 3650):
                bot.send_message(message.chat.id, "❌ ১ থেকে ৩৬৫০-এর মধ্যে একটি সংখ্যা লিখুন। উদাহরণ: <code>45</code>", parse_mode="HTML")
                return
            del user_states[user_id]
            q_text, q_markup = build_redeem_qty_picker(tier_key, int(raw))
            bot.send_message(message.chat.id, q_text, reply_markup=q_markup, parse_mode="HTML")
            return

        # 🎁 অ্যাডমিন: কাস্টম কোড (CODE MAX_USES)
        elif state_name == "awaiting_redeem_custom" and admin.is_admin(user_id):
            parts = (text or "").strip().split()
            if not parts or len(parts) > 2 or (len(parts) == 2 and not parts[1].isdigit()):
                bot.send_message(message.chat.id, "❌ ভুল ফরম্যাট! এভাবে দিন: <code>CODE MAX_USES</code>\nউদাহরণ: <code>EID2026 50</code>", parse_mode="HTML")
                return
            max_uses = int(parts[1]) if len(parts) == 2 else 1
            tier_key, days = state_data.get("tier"), state_data.get("days")
            ok, res = admin.create_redeem_codes(tier_key, days, 1, max_uses, user_id, custom_code=parts[0])
            if not ok:
                bot.send_message(message.chat.id, f"❌ {html.escape(str(res))}\nআবার চেষ্টা করুন বা অন্য কোড দিন।", parse_mode="HTML")
                return
            del user_states[user_id]
            messages, r_markup = build_redeem_codes_result(tier_key, days, res, max_uses)
            for i, m in enumerate(messages):
                bot.send_message(message.chat.id, m, reply_markup=r_markup if i == len(messages) - 1 else None, parse_mode="HTML")
            return

        # ⭐ NEW: সাপোর্ট টিকিট সিস্টেম — ইউজার নতুন টিকিট খুলছে
        elif state_name == "awaiting_ticket_message":
            del user_states[user_id]
            msg_body = text.strip()
            ticket_id = admin.create_support_ticket(user_id, message.from_user.username, msg_body)
            bot.send_message(
                message.chat.id,
                f"✅ <b>টিকিট #{ticket_id} তৈরি হয়েছে!</b>\nআমাদের টিম শীঘ্রই আপনাকে রিপ্লাই দেবে। "
                "আপডেট পেতে <b>🎫 My Tickets</b> চেক করুন।",
                parse_mode="HTML"
            )
            admin_alert = (
                f"🎫 <b>নতুন সাপোর্ট টিকিট!</b> (#{ticket_id})\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>ইউজার:</b> @{message.from_user.username or 'Anonymous'} (ID: <code>{user_id}</code>)\n"
                f"📝 <b>মেসেজ:</b> {html.escape(msg_body[:300])}"
            )
            admin_markup = types.InlineKeyboardMarkup()
            admin_markup.add(make_inline_button("💬 Reply", callback_data=f"adm_ticket_view_{ticket_id}", style="primary"))
            for aid in admin.ADMIN_IDS:
                try:
                    bot.send_message(aid, admin_alert, reply_markup=admin_markup, parse_mode="HTML")
                except Exception:
                    pass
            return

        # ⭐ NEW: ইউজার একটা ওপেন টিকিটে রিপ্লাই দিচ্ছে
        elif state_name == "awaiting_ticket_reply":
            del user_states[user_id]
            ticket_id = state_data["ticket_id"]
            ticket = admin.get_ticket(ticket_id)
            if not ticket or ticket["user_id"] != user_id:
                bot.send_message(message.chat.id, "❌ টিকিটটি পাওয়া যায়নি।")
                return
            admin.add_ticket_message(ticket_id, "user", text.strip())
            thread_text, thread_markup = build_ticket_thread_view(ticket_id, is_admin=False)
            bot.send_message(message.chat.id, thread_text, reply_markup=thread_markup, parse_mode="HTML")
            for aid in admin.ADMIN_IDS:
                try:
                    _mk = types.InlineKeyboardMarkup()
                    _mk.add(make_inline_button("💬 View / Reply", callback_data=f"adm_ticket_view_{ticket_id}", style="primary"))
                    bot.send_message(
                        aid,
                        f"🔔 <b>টিকিট #{ticket_id}-এ ইউজার রিপ্লাই দিয়েছে!</b>\n{html.escape(text.strip()[:300])}",
                        reply_markup=_mk,
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
            return

        # ⭐ NEW: অ্যাডমিন একটা টিকিটে রিপ্লাই দিচ্ছেন
        elif state_name == "awaiting_admin_ticket_reply" and admin.is_admin(user_id):
            del user_states[user_id]
            ticket_id = state_data["ticket_id"]
            ticket = admin.get_ticket(ticket_id)
            if not ticket:
                bot.send_message(message.chat.id, "❌ টিকিটটি পাওয়া যায়নি।")
                return
            admin.add_ticket_message(ticket_id, "admin", text.strip())
            thread_text, thread_markup = build_ticket_thread_view(ticket_id, is_admin=True)
            bot.send_message(message.chat.id, thread_text, reply_markup=thread_markup, parse_mode="HTML")
            try:
                _mk2 = types.InlineKeyboardMarkup()
                _mk2.add(make_inline_button("👁 টিকিট দেখুন", callback_data=f"ticket_view_{ticket_id}", style="primary"))
                bot.send_message(
                    ticket["user_id"],
                    f"💬 <b>সাপোর্ট থেকে রিপ্লাই এসেছে!</b> (টিকিট #{ticket_id})\n{html.escape(text.strip()[:500])}",
                    reply_markup=_mk2,
                    parse_mode="HTML"
                )
            except Exception:
                pass
            return

    # --- অ্যাডমিন বাটন অ্যাকশনস ---
    if text == "👑 Admin Panel" and admin.is_admin(user_id):
        stats = admin.get_comprehensive_server_stats(active_processes)
        bot.send_message(message.chat.id, stats + "\n\n👇 <b>Select an admin control action:</b>", reply_markup=get_admin_reply_keyboard(), parse_mode="HTML")

    elif text == "🔙 User Menu":
        bot.send_message(message.chat.id, "🔙 <b>ইউজার মেনুতে ফিরে আসা হয়েছে:</b>", reply_markup=get_user_keyboard(user_id), parse_mode="HTML")

    # ======================== অ্যাডমিন সাব-মেনু নেভিগেশন ========================
    elif text == "📊 Monitoring" and admin.is_admin(user_id):
        bot.send_message(message.chat.id, "📊 <b>মনিটরিং মেনু</b>\nসার্ভার ও বট স্ট্যাটাস দেখুন:", reply_markup=get_admin_monitoring_keyboard(), parse_mode="HTML")

    elif text == "👥 User Manager" and admin.is_admin(user_id):
        bot.send_message(message.chat.id, "👥 <b>ইউজার ম্যানেজমেন্ট</b>\nইউজার সংক্রান্ত সব কার্যক্রম:", reply_markup=get_admin_user_keyboard(), parse_mode="HTML")

    elif text == "💰 Billing & Plans" and admin.is_admin(user_id):
        bot.send_message(message.chat.id, "💰 <b>বিলিং ও প্ল্যান</b>\nVIP প্রদান, কুপন ও রিডিম কোড ম্যানেজ:", reply_markup=get_admin_billing_keyboard(), parse_mode="HTML")

    elif text == "⚙️ System" and admin.is_admin(user_id):
        bot.send_message(message.chat.id, "⚙️ <b>সিস্টেম</b>\nসার্ভার ম্যানেজমেন্ট:", reply_markup=get_admin_system_keyboard(), parse_mode="HTML")

    elif text == "📈 Analytics" and admin.is_admin(user_id):
        # Analytics Dashboard directly
        all_users = admin.get_all_users()
        total = len(all_users)
        free_c = sum(1 for u in all_users if u.get("plan","FREE")=="FREE")
        pro_c = sum(1 for u in all_users if u.get("plan")=="PRO")
        prem_c = sum(1 for u in all_users if u.get("plan")=="PREMIUM")
        biz_c = sum(1 for u in all_users if u.get("plan") in ("BUSINESS","VIP"))
        paid_total = pro_c+prem_c+biz_c
        today_str = datetime.now().strftime("%Y-%m-%d")
        today_new = sum(1 for u in all_users if str(u.get("created_at","")).startswith(today_str))
        active_bot_count = len([k for k,v in active_processes.items() if v["process"].poll() is None])
        conn = admin.get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM projects"); total_bots = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM vip_requests WHERE status='APPROVED'"); approved_vip = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM coupon_uses"); coupon_uses = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM coupons WHERE is_active=1"); active_coupons = cur.fetchone()[0]
        conn.close()
        conv = f"{(paid_total/total*100):.1f}%" if total>0 else "0%"
        dash = (
            "📈 <b>Analytics Dashboard</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👥 মোট ইউজার: <b>{total}</b> | আজ নতুন: <b>{today_new}</b>\n"
            f"💰 পেইড: <b>{paid_total}</b> | ফ্রি: <b>{free_c}</b> | Conversion: <b>{conv}</b>\n"
            f"⚡ Pro: <b>{pro_c}</b> | 🌟 Premium: <b>{prem_c}</b> | 👑 Business: <b>{biz_c}</b>\n"
            f"🤖 বট চলছে: <b>{active_bot_count}</b> | মোট: <b>{total_bots}</b>\n"
            f"✅ VIP Approved: <b>{approved_vip}</b>\n"
            f"🎟️ কুপন একটিভ: <b>{active_coupons}</b> | ব্যবহার: <b>{coupon_uses}</b>\n"
            f"🕒 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(make_inline_button("🔄 Refresh", callback_data="adm_refresh_analytics", style="primary"))
        markup.add(make_inline_button("🔙 Admin Menu", callback_data="adm_back_menu", style="primary"))
        bot.send_message(message.chat.id, dash, reply_markup=markup, parse_mode="HTML")

    elif text == "⏹ Stop All Bots" and admin.is_admin(user_id):
        stopped = 0
        for p_key in list(active_processes.keys()):
            try:
                uid, pid = map(int, p_key.split("_", 1))
                active_processes[p_key]["process"].terminate()
                active_processes[p_key]["log_handle"].close()
                del active_processes[p_key]
                admin.update_project_status(pid, "STOPPED")
                stopped += 1
            except Exception:
                pass
        bot.send_message(message.chat.id, f"⏹️ <b>{stopped}টি বট বন্ধ করা হয়েছে।</b>", parse_mode="HTML")

    elif text == "📤 Bot Export" and admin.is_admin(user_id):
        # সব ইউজারের বট সামারি CSV হিসেবে দেওয়া
        all_users = admin.get_all_users()
        lines_csv = ["User ID,Username,Plan,Total Bots,Active Bots"]
        for u in all_users:
            uid = u["user_id"]
            uname = u.get("username","")
            plan = u.get("plan","FREE")
            projs = admin.get_user_projects(uid)
            active_c = sum(1 for p in projs if f"{uid}_{p['project_id']}" in active_processes and active_processes[f"{uid}_{p['project_id']}"]['process'].poll() is None)
            lines_csv.append(f"{uid},{uname},{plan},{len(projs)},{active_c}")
        csv_data = "\n".join(lines_csv).encode("utf-8")
        import io as _io
        bio = _io.BytesIO(csv_data)
        bio.name = f"pyhost_export_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
        bot.send_document(message.chat.id, bio, caption="📤 <b>All Users Bot Export</b>", parse_mode="HTML")

    elif text == "🔙 Admin Menu" and admin.is_admin(user_id):
        stats = admin.get_comprehensive_server_stats(active_processes)
        bot.send_message(message.chat.id, stats + "\n\n👇 <b>অ্যাডমিন মেনু:</b>", reply_markup=get_admin_reply_keyboard(), parse_mode="HTML")

    elif text == "📊 Live Telemetry" and admin.is_admin(user_id):
        stats = admin.get_comprehensive_server_stats(active_processes)
        markup = types.InlineKeyboardMarkup()
        markup.add(make_inline_button("🔄 Refresh Telemetry", callback_data="adm_refresh_telem", style="primary"))
        markup.add(make_inline_button("🔙 Admin Menu", callback_data="adm_back_menu", style="primary"))
        bot.send_message(message.chat.id, stats, reply_markup=markup, parse_mode="HTML")

    elif text == "🤖 Process Tracker" and admin.is_admin(user_id):
        if not active_processes:
            bot.send_message(message.chat.id, "🤖 No active bot processes running.")
        else:
            lines = []
            for p_key, d in active_processes.items():
                elapsed = int(time.time() - d["start_time"])
                mem, cpu = admin.get_bot_resource_usage(d["process"].pid)
                lines.append(f"• <b>Bot:</b> <code>{d['project_name']}</code> (UID: <code>{d['user_id']}</code>)\n  └ 🧠 {mem} MB | 💻 {cpu}% | ⏱️ {elapsed//3600}h {(elapsed%3600)//60}m")
            bot.send_message(message.chat.id, "🤖 <b>Active Bots Process Directory:</b>\n━━━━━━━━━━━━━━━━━━━━━━\n" + "\n".join(lines), parse_mode="HTML")

    elif text == "📋 Pending Requests" and admin.is_admin(user_id):
        req_text, req_markup = build_pending_requests_view(page=0)
        bot.send_message(message.chat.id, req_text, reply_markup=req_markup, parse_mode="HTML")

    elif text == "🎫 Support Tickets" and admin.is_admin(user_id):
        t_text, t_markup = build_admin_tickets_view(page=0)
        bot.send_message(message.chat.id, t_text, reply_markup=t_markup, parse_mode="HTML")

    elif text == "🔄 Restart All Bots" and admin.is_admin(user_id):
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            make_inline_button("✅ Yes, Restart", callback_data="adm_confirm_restart_all", style="danger"),
            make_inline_button("❌ Cancel", callback_data="adm_cancel_action", style="primary")
        )
        bot.send_message(
            message.chat.id,
            f"⚠️ <b>নিশ্চিত করুন!</b>\nবর্তমানে সক্রিয় <code>{len(active_processes)}</code> টি বট রিস্টার্ট হবে। চালিয়ে যেতে চান?",
            reply_markup=markup, parse_mode="HTML"
        )

    elif text == "👥 User Directory" and admin.is_admin(user_id):
        dir_text, dir_markup = build_user_directory_view(page=0)
        bot.send_message(message.chat.id, dir_text, reply_markup=dir_markup, parse_mode="HTML")

    elif text == "👤 User Management" and admin.is_admin(user_id):
        dir_text, dir_markup = build_user_directory_view(page=0)
        intro = (
            "👤 <b>USER MANAGEMENT</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "নিচের লিস্ট থেকে ইউজার বেছে নিন — প্রোফাইলে ট্যাপ করলে প্ল্যান পরিবর্তন, "
            "ব্যান/আনব্যান, বট ম্যানেজমেন্ট ও অ্যাক্টিভিটি লগসহ সব কন্ট্রোল এক জায়গায় পাবেন।\n\n"
        )
        bot.send_message(message.chat.id, intro + dir_text, reply_markup=dir_markup, parse_mode="HTML")

    elif text == "📢 Global Broadcast" and admin.is_admin(user_id):
        user_states[user_id] = "awaiting_broadcast"
        bot.send_message(message.chat.id, "📢 <b>Broadcast Notice:</b> Type the announcement message to broadcast to all clients:")

    elif text == "🗓️ Schedule Broadcast" and admin.is_admin(user_id):
        user_states[user_id] = "awaiting_sched_broadcast_msg"
        bot.send_message(message.chat.id, "🗓️ <b>Schedule Broadcast:</b> যে মেসেজটি নির্দিষ্ট সময়ে সব ইউজারকে পাঠাতে চান সেটা লিখুন:", parse_mode="HTML")

    elif text == "💎 Grant VIP (Manual)" and admin.is_admin(user_id):
        user_states[user_id] = "awaiting_vip_id"
        bot.send_message(message.chat.id, "💎 <b>Grant VIP:</b> Enter the Telegram User ID to upgrade:")

    elif text == "🚫 Ban / Security" and admin.is_admin(user_id):
        user_states[user_id] = "awaiting_ban_id"
        bot.send_message(message.chat.id, "🚫 <b>Ban/Unban:</b> Enter the Telegram User ID to toggle ban status:")

    elif text == "💾 Database Backup" and admin.is_admin(user_id):
        bot.send_message(message.chat.id, "⏳ Generating system & database backup...")
        b_path = admin.generate_system_backup()
        with open(b_path, "rb") as bf:
            bot.send_document(message.chat.id, bf, caption="💾 <b>PyHost Full Database Backup</b>")

    elif text == "🧹 Server Cleanup" and admin.is_admin(user_id):
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            make_inline_button("✅ Yes, Clean Up", callback_data="adm_confirm_cleanup", style="danger"),
            make_inline_button("❌ Cancel", callback_data="adm_cancel_action", style="primary")
        )
        bot.send_message(message.chat.id, "⚠️ <b>নিশ্চিত করুন!</b>\nপুরনো লগ ও ক্যাশ পরিষ্কার করা হবে। চালিয়ে যেতে চান?", reply_markup=markup, parse_mode="HTML")

    elif text == "🆓 Free Hosting (12h)" and admin.is_admin(user_id):
        will_be = not admin.SYSTEM_CONFIG.get("free_hosting_enabled", True)
        current_hours = admin.SYSTEM_CONFIG.get("free_hosting_hours", 12)
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            make_inline_button("✅ Confirm" if will_be else "🔒 Disable (Confirm)", callback_data="adm_confirm_free_hosting", style="danger"),
            make_inline_button("❌ Cancel", callback_data="adm_cancel_action", style="primary")
        )
        markup.add(
            make_inline_button("3 Hours" + (" ✓" if current_hours == 3 else ""), callback_data="adm_set_free_hours_3", style="primary"),
            make_inline_button("6 Hours" + (" ✓" if current_hours == 6 else ""), callback_data="adm_set_free_hours_6", style="primary"),
        )
        markup.add(
            make_inline_button("9 Hours" + (" ✓" if current_hours == 9 else ""), callback_data="adm_set_free_hours_9", style="primary"),
            make_inline_button("12 Hours" + (" ✓" if current_hours == 12 else ""), callback_data="adm_set_free_hours_12", style="primary"),
        )
        target_label = "🟢 ON (ফ্রি ইউজাররা আগের মতোই বট চালাতে পারবে)" if will_be else "🔴 OFF (সব ফ্রি স্লট লক হয়ে যাবে, এখন চলতে থাকা ফ্রি বটও বন্ধ হয়ে যাবে)"
        current_label = "🟢 বর্তমানে ON আছে" if admin.SYSTEM_CONFIG.get("free_hosting_enabled", True) else "🔴 বর্তমানে OFF আছে"
        bot.send_message(
            message.chat.id,
            f"🆓 <b>ফ্রি হোস্টিং সিস্টেম</b>\n{current_label} • সেশন মেয়াদ: <b>{current_hours} Hours</b>\n\n"
            f"পরিবর্তন করে <b>{target_label}</b> করা হবে।\n"
            "💎 পেইড প্ল্যান (24/7) কোনোভাবেই প্রভাবিত হবে না — শুধু FREE প্ল্যান আটকাবে।\n\n"
            "⏱️ নিচ থেকে ফ্রি সেশনের মেয়াদ (ঘণ্টা) বদলাতে পারেন — এখনই এফেক্ট হবে, Control Panel-এও সাথে সাথে আপডেট হয়ে দেখাবে।",
            reply_markup=markup, parse_mode="HTML"
        )

    elif text == "⚙️ Maintenance Mode" and admin.is_admin(user_id):
        will_be = not admin.SYSTEM_CONFIG["maintenance_mode"]
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            make_inline_button("✅ Confirm", callback_data="adm_confirm_maintenance", style="danger"),
            make_inline_button("❌ Cancel", callback_data="adm_cancel_action", style="primary")
        )
        target_label = "🔴 ON (সব নতুন ডিপ্লয়মেন্ট বন্ধ থাকবে)" if will_be else "🟢 OFF"
        bot.send_message(
            message.chat.id,
            f"⚠️ <b>নিশ্চিত করুন!</b>\nMaintenance Mode <b>{target_label}</b> করা হবে — এটি সব ইউজারকে প্রভাবিত করবে। চালিয়ে যেতে চান?",
            reply_markup=markup, parse_mode="HTML"
        )


    elif text == "📈 Analytics Dashboard" and admin.is_admin(user_id):
        all_users = admin.get_all_users()
        total = len(all_users)
        free_c = sum(1 for u in all_users if u.get("plan", "FREE") == "FREE")
        pro_c = sum(1 for u in all_users if u.get("plan") == "PRO")
        prem_c = sum(1 for u in all_users if u.get("plan") == "PREMIUM")
        biz_c = sum(1 for u in all_users if u.get("plan") in ("BUSINESS", "VIP"))
        paid_total = pro_c + prem_c + biz_c
        banned_c = sum(1 for u in all_users if u.get("is_banned") == 1)
        # আজকের নতুন ইউজার
        today_str = datetime.now().strftime("%Y-%m-%d")
        today_new = sum(1 for u in all_users if str(u.get("created_at", "")).startswith(today_str))
        # Active bots
        active_bot_count = len([k for k, v in active_processes.items() if v["process"].poll() is None])
        # Total bots in DB
        import sqlite3
        conn = admin.get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM projects")
        total_bots = cur.fetchone()[0]
        # Revenue estimate (approved requests)
        cur.execute("SELECT COUNT(*) FROM vip_requests WHERE status = 'APPROVED'")
        approved_vip = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM renewal_requests WHERE status = 'APPROVED'")
        approved_ext = cur.fetchone()[0]
        # Coupon usage
        cur.execute("SELECT COUNT(*) FROM coupon_uses")
        total_coupon_uses = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM coupons WHERE is_active = 1")
        active_coupons = cur.fetchone()[0]
        conn.close()

        # Conversion rate
        conv_rate = f"{(paid_total/total*100):.1f}%" if total > 0 else "0%"

        dashboard = (
            "📈 <b>PyHost Analytics Dashboard</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "👥 <b>ইউজার সামারি:</b>\n"
            f"├ 📊 মোট রেজিস্টার্ড: <b>{total}</b> জন\n"
            f"├ 🆕 আজকের নতুন: <b>{today_new}</b> জন\n"
            f"├ 💰 পেইড সদস্য: <b>{paid_total}</b> জন\n"
            f"├ 🆓 ফ্রি ইউজার: <b>{free_c}</b> জন\n"
            f"└ 🚫 ব্যান্ড: <b>{banned_c}</b> জন\n\n"
            "📦 <b>প্ল্যান বিভাজন:</b>\n"
            f"├ ⚡ Pro: <b>{pro_c}</b> জন\n"
            f"├ 🌟 Premium: <b>{prem_c}</b> জন\n"
            f"├ 👑 Business/VIP: <b>{biz_c}</b> জন\n"
            f"└ 📈 Conversion Rate: <b>{conv_rate}</b>\n\n"
            "🤖 <b>বট স্ট্যাটাস:</b>\n"
            f"├ 🟢 এখন চলছে: <b>{active_bot_count}</b> টি\n"
            f"└ 📦 মোট তৈরি: <b>{total_bots}</b> টি\n\n"
            "💰 <b>পেমেন্ট সামারি:</b>\n"
            f"├ ✅ অ্যাপ্রুভড প্ল্যান রিকোয়েস্ট: <b>{approved_vip}</b>\n"
            f"└ ✅ অ্যাপ্রুভড রিনিউয়াল: <b>{approved_ext}</b>\n\n"
            "🎟️ <b>কুপন সামারি:</b>\n"
            f"├ 🟢 একটিভ কুপন: <b>{active_coupons}</b> টি\n"
            f"└ 📊 মোট ব্যবহার: <b>{total_coupon_uses}</b> বার\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🕒 <i>Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</i>"
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(make_inline_button("🔄 Refresh", callback_data="adm_refresh_analytics", style="primary"))
        markup.add(make_inline_button("🔙 Admin Menu", callback_data="adm_back_menu", style="primary"))
        bot.send_message(message.chat.id, dashboard, reply_markup=markup, parse_mode="HTML")

    elif text == "🎟️ Coupon Manager" and admin.is_admin(user_id):
        coupons = admin.list_coupons()
        if coupons:
            lines_text = []
            for c in coupons[:15]:
                status = "🟢" if c["is_active"] else "🔴"
                uses = f"{c['used_count']}/{c['max_uses']}" if c["max_uses"] > 0 else f"{c['used_count']}/∞"
                lines_text.append(f"{status} <code>{c['code']}</code> — {c['discount_percent']}% ছাড় | ব্যবহার: {uses}")
            coupon_list = "\n".join(lines_text)
        else:
            coupon_list = "<i>এখনো কোনো কুপন নেই।</i>"

        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            make_inline_button("➕ Create New Coupon", callback_data="adm_coupon_create", style="success"),
            make_inline_button("❌ Deactivate Coupon", callback_data="adm_coupon_deactivate", style="danger")
        )
        markup.add(make_inline_button("🔙 Admin Menu", callback_data="adm_back_menu", style="primary"))
        bot.send_message(
            message.chat.id,
            f"🎟️ <b>Coupon Manager</b>\n━━━━━━━━━━━━━━━━━━━━━━\n{coupon_list}\n━━━━━━━━━━━━━━━━━━━━━━",
            reply_markup=markup, parse_mode="HTML"
        )

    # 🎁 অ্যাডমিন: Redeem Code ম্যানেজার
    elif text == "🎁 Redeem Codes" and admin.is_admin(user_id):
        rc_text, rc_markup = build_redeem_dashboard()
        bot.send_message(message.chat.id, rc_text, reply_markup=rc_markup, parse_mode="HTML")

    # --- রেগুলার ইউজার বাটনস ---
    elif text == "🎁 Redeem Code":
        send_redeem_prompt(message.chat.id, user_id)

    elif text == "🎛️ Control Panel":
        cp_text, cp_markup = get_multi_bot_control_panel(user_id)
        bot.send_message(message.chat.id, cp_text, reply_markup=cp_markup, parse_mode="HTML")

    elif text == "📁 File Manager":
        projects = admin.get_user_projects(user_id)
        if not projects:
            markup = types.InlineKeyboardMarkup()
            markup.add(make_inline_button("➕ Create Bot & Upload File", callback_data="add_new_bot", style="success"))
            markup.add(make_inline_button("🔙 Back to Home", callback_data="go_home", style="primary"))
            bot.send_message(
                message.chat.id,
                "⚠️ <b>আপনার কোনো সক্রিয় বট প্রজেক্ট পাওয়া যায়নি!</b>\n\nফাইল আপলোড ও হোস্ট করতে নিচের বাটনে চাপ দিন:",
                reply_markup=markup,
                parse_mode="HTML"
            )
        else:
            markup = types.InlineKeyboardMarkup(row_width=1)
            for p in projects:
                markup.add(make_inline_button(f"📁 {p['project_name']}", callback_data=f"fm_view_{p['project_id']}", style="primary"))
            markup.add(make_inline_button("🔙 Back to Control Panel", callback_data="back_to_cp", style="primary"))
            bot.send_message(
                message.chat.id,
                "📂 <b>স্মার্ট ফাইল ম্যানেজার</b>\n━━━━━━━━━━━━━━━━━━━━━━\nযে বটের ফাইল দেখতে, ব্যাকআপ নিতে বা আপডেট করতে চান তা নিচে নির্বাচন করুন:",
                reply_markup=markup,
                parse_mode="HTML"
            )

    elif text == "👤 My Account":
        card_text, markup = get_my_account_card(user_id, message.from_user)
        bot.send_message(message.chat.id, card_text, reply_markup=markup, parse_mode="HTML")

    elif text == "💎 Upgrade Plan":
        plan_text, plan_markup = get_upgrade_plan_view(user_id)
        bot.send_message(message.chat.id, plan_text, reply_markup=plan_markup, parse_mode="HTML")

    elif text == "🐙 GitHub Deploy":
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            make_inline_button("🐙 GitHub Deploy", callback_data="gh_deploy_start", style="success"),
            make_inline_button("🤖 Marketplace", callback_data="open_mp_menu", style="primary")
        )
        add_home_button(markup)
        bot.send_message(
            message.chat.id,
            "🐙 <b>GitHub Deploy</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "GitHub রিপো থেকে সরাসরি বট ডিপ্লয় করতে নিচের <b>🐙 GitHub Deploy</b> বাটনে চাপুন (VIP প্ল্যান প্রয়োজন)।\n"
            "কোডিং জানা না থাকলে <b>🤖 Marketplace</b> থেকে রেডিমেড বট বেছে নিন।",
            reply_markup=markup, parse_mode="HTML"
        )

    elif text == "🤖 Marketplace":
        bot.send_message(message.chat.id, MARKETPLACE_INTRO, reply_markup=build_marketplace_markup(), parse_mode="HTML")

    elif text == "📡 Support Hub":
        msg = (
            "🛠️ <b>Support Centre</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "যেকোনো সহায়তার জন্য বা কাস্টম বট বানানোর জন্য নিচের অপশনগুলো বেছে নিন:"
        )
        support_markup = get_support_hub_markup()
        add_home_button(support_markup)
        bot.send_message(message.chat.id, msg, reply_markup=support_markup, parse_mode="HTML")

    elif text == "📖 Documentation":
        _doc_text0, _doc_markup0 = build_doc_page(0)
        bot.send_message(message.chat.id, _doc_text0, reply_markup=_doc_markup0, parse_mode="HTML")

# ----------------- ফাইল আপলোড ও লাইভ ডেপ্লয়মেন্ট হ্যান্ডলার -----------------
# ⭐ FIX: ফাইল আপলোডের সময় একটার পর একটা এলোমেলো এক-লাইনের টেক্সট দেখানোর বদলে ধাপে-ধাপে
# চেকলিস্ট দেখানো হবে (সম্পন্ন/চলমান/বাকি ধাপ) — একই মেসেজ এডিট হয়ে আপডেট হবে, বট সফলভাবে
# চালু হলে পুরো চেকলিস্ট মুছে গিয়ে (vanish) তার জায়গায় সাকসেস কার্ড বসবে।
UPLOAD_PROGRESS_STEPS = [
    ("save",    "ফাইল সংরক্ষণ"),
    ("scan",    "কোড ও সিকিউরিটি স্ক্যান"),
    ("install", "প্রয়োজনীয় প্যাকেজ ইনস্টল"),
    ("run",     "ব্যাকগ্রাউন্ডে বট বুট করা"),
]

def render_upload_progress(file_name: str, project_id: int, completed: set, current: str = None, note: str = "", elapsed: int = 0):
    """ডিপ্লয়ের সময় ইউজারকে দেখানো লাইভ প্রোগ্রেস কার্ড — প্রতিটা ধাপ ✅/⏳/⬜ দিয়ে
    স্পষ্টভাবে বোঝানো হয়, যাতে ইউজার বুঝতে পারে ঠিক কোন পর্যায়ে আছে এবং কেন অপেক্ষা করছে।
    'elapsed' সেকেন্ডটা প্রতি ১ সেকেন্ডে বাড়তে থাকে (LiveDeployTicker দেখুন) — তাই এখানে
    স্ট্যাটিক '৫-১০ সেকেন্ড অপেক্ষা করুন' না লিখে আসল সময় লাইভ দেখানো হয়।"""
    proj = admin.get_project_by_id(project_id)
    bot_name = proj["project_name"] if proj else "New Bot"
    slot_no = proj.get("slot_number") if proj else None
    slot_label = f"Slot #{slot_no}" if slot_no else "Assigning..."

    step_lines = []
    for idx, (key, label) in enumerate(UPLOAD_PROGRESS_STEPS, start=1):
        if key in completed:
            step_lines.append(f"┃ ✅ [{idx}/4] {label} সম্পন্ন")
        elif key == current:
            step_lines.append(f"┃ ⏳ [{idx}/4] {label} হচ্ছে...{note}")
        else:
            step_lines.append(f"┃ ⬜ [{idx}/4] {label}")

    return (
        "╭━━━[ ⚙️ 𝗕𝗢𝗧 𝗗𝗘𝗣𝗟𝗢𝗬𝗜𝗡𝗚... ]━━━╮\n"
        "┃ ⏳ Status: Building Environment\n"
        "┣━━━[ 🤖 𝗧𝗔𝗥𝗚𝗘𝗧 𝗜𝗡𝗙𝗢 ]━━━━━┫\n"
        f"┃ 🏷️ Bot: {html.escape(bot_name)}\n"
        f"┃ 🔢 Target Slot: {slot_label}\n"
        f"┃ 📁 Source File: {html.escape(file_name)}\n"
        "┣━━━[ 📊 𝗣𝗥𝗢𝗚𝗥𝗘𝗦𝗦 𝗦𝗧𝗔𝗚𝗘𝗦 ]━━━┫\n"
        + "\n".join(step_lines) + "\n"
        "┣━━━[ 💡 𝗡𝗢𝗧𝗜𝗖𝗘 ]━━━━━━━━━┫\n"
        "┃ ⏳ অনুগ্রহ করে ৫-১০ সেকেন্ড অপেক্ষা করুন...\n"
        "┃ 🚫 ডিপ্লয় শেষ না হওয়া পর্যন্ত মেসেজ পাঠাবেন না\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━╯"
    )

class LiveDeployTicker:
    """render_upload_progress কার্ডে স্ট্যাটিক '৫-১০ সেকেন্ড অপেক্ষা করুন' এর বদলে আসল
    সেকেন্ড লাইভ গুনে দেখায় — প্রতি ১ সেকেন্ডে ব্যাকগ্রাউন্ডে মেসেজ এডিট করে সময় আপডেট করে।
    মূল ফ্লো শুধু .update(completed, current, note) কল করবে ধাপ বদলাতে, বাকিটা এই থ্রেড
    নিজে থেকে সামলাবে। .stop() কল করলে টিকার বন্ধ হয়ে যায় (সাকসেস কার্ড বসানোর ঠিক আগে)।"""
    def __init__(self, chat_id, msg_id, file_name, project_id, completed=None, current=None, note=""):
        self.chat_id = chat_id
        self.msg_id = msg_id
        self.file_name = file_name
        self.project_id = project_id
        self.completed = set(completed or [])
        self.current = current
        self.note = note
        self.start_ts = time.time()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._last_text = None
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def update(self, completed=None, current=None, note=""):
        with self._lock:
            if completed is not None:
                self.completed = set(completed)
            self.current = current
            self.note = note

    def _loop(self):
        while not self._stop.is_set():
            with self._lock:
                completed, current, note, fname = set(self.completed), self.current, self.note, self.file_name
            elapsed = int(time.time() - self.start_ts)
            text = render_upload_progress(fname, self.project_id, completed, current, note, elapsed=elapsed)
            if text != self._last_text:
                try:
                    bot.edit_message_text(text, self.chat_id, self.msg_id, parse_mode="HTML")
                    self._last_text = text
                except Exception:
                    pass
            self._stop.wait(1)

    def set_source(self, file_name):
        with self._lock:
            self.file_name = file_name

    def stop(self):
        self._stop.set()
        # ⭐ FIX: লুপ থ্রেড এডিটের মাঝপথে থাকলে পুরনো প্রোগ্রেস ফ্রেম সাকসেস কার্ডের উপর বসে যেতে পারত —
        # তাই বন্ধ করার সময় থ্রেডটা শেষ হওয়া পর্যন্ত (সর্বোচ্চ ৩ সেকেন্ড) অপেক্ষা করা হয়।
        if threading.current_thread() is not self._thread:
            try:
                self._thread.join(timeout=3)
            except Exception:
                pass

def _project_is_empty(user_id: int, proj: dict) -> bool:
    """যে বটে এখনো কোনো .py ফাইল আপলোড হয়নি (এবং চলছে না) — সেটা \"ফাঁকা স্লট\" হিসেবে গণ্য।"""
    if str(proj.get("status", "")).upper() == "RUNNING":
        return False
    p_dir = os.path.join(PROJECTS_DIR, f"{user_id}_{proj['project_id']}")
    if not os.path.isdir(p_dir):
        return True
    for _root, _dirs, files in os.walk(p_dir):
        if any(f.endswith(".py") for f in files):
            return False
    return True

def _remember_upload_target(user_id: int, pid: int):
    """ডিপ্লয় ফেইল করলে ইউজার ফাইল ঠিক করে আবার পাঠালে যেন নতুন স্লট না খুলে সেই বটেই যায়।"""
    user_states[user_id] = {"state": "awaiting_file_for_bot", "project_id": pid, "ts": time.time()}

def _finish_upload_target(user_id: int, pid: int):
    """সফল ডিপ্লয়ের পর টার্গেট মুছে ফেলা হয় — পরের আপলোড অটো ফাঁকা স্লটে যাবে, পুরনো বট ওভাররাইট হবে না।"""
    with upload_batch_lock:
        upload_target_hint.pop(user_id, None)
        st = user_states.get(user_id)
        if isinstance(st, dict) and st.get("state") == "awaiting_file_for_bot" and st.get("project_id") == pid:
            user_states.pop(user_id, None)

def resolve_upload_target(user_id: int, u_data: dict):
    """ফাইল আপলোডের টার্গেট বট ঠিক করে। রিটার্ন: (project_id, error_kind)।
    অগ্রাধিকার:
      ১) ইউজার সম্প্রতি কোনো বটের 'Update Code' / স্লট চেপে থাকলে সেই বট
      ২) একই ব্যাচে পরপর আসা ফাইল — আগের ফাইলের বটেই
      ৩) এখনো ফাইল আপলোড হয়নি এমন ফাঁকা বট (সবচেয়ে ছোট স্লট নম্বর)
      ৪) নতুন বট — প্রথম খালি স্লটে (প্ল্যান-লিমিটের ভেতরে)
      ৫) সব স্লট পূর্ণ হলে কোনো বট ওভাররাইট না করে error_kind='full' রিটার্ন"""
    now = time.time()
    with upload_batch_lock:
        # ১) explicit state
        st = user_states.get(user_id)
        if isinstance(st, dict) and st.get("state") == "awaiting_file_for_bot" and st.get("project_id"):
            if now - st.get("ts", now) <= UPLOAD_EXPLICIT_TTL:
                proj = admin.get_project_by_id(st["project_id"])
                if proj and proj["user_id"] == user_id:
                    upload_target_hint[user_id] = {"pid": proj["project_id"], "ts": now}
                    return proj["project_id"], None

        # ২) চলমান ব্যাচ / সদ্য আসা ফাইলের টার্গেট
        batch = upload_batches.get(user_id)
        if batch:
            upload_target_hint[user_id] = {"pid": batch["target_pid"], "ts": now}
            return batch["target_pid"], None
        hint = upload_target_hint.get(user_id)
        if hint and now - hint["ts"] <= UPLOAD_HINT_TTL:
            proj = admin.get_project_by_id(hint["pid"])
            if proj and proj["user_id"] == user_id:
                hint["ts"] = now
                return proj["project_id"], None

        projects = admin.get_user_projects(user_id)

        # ৩) ফাঁকা (আপলোড হয়নি এমন) বট
        for proj in sorted(projects, key=lambda x: (x.get("slot_number") or 999, x["project_id"])):
            if _project_is_empty(user_id, proj):
                upload_target_hint[user_id] = {"pid": proj["project_id"], "ts": now}
                return proj["project_id"], None

        # ৪) নতুন বট প্রথম খালি স্লটে
        max_bots = admin.get_max_bot_limit_for_user(u_data)
        if len(projects) >= max_bots:
            return None, "full"
        slot = admin.get_next_free_slot(user_id)
        if slot is None:
            return None, "full"
        name = "My Bot" if slot == 1 else f"My Bot {slot}"
        pid = admin.create_user_project(user_id, name, slot_number=slot)
        upload_target_hint[user_id] = {"pid": pid, "ts": now}
        return pid, None

def _slots_full_notice(chat_id: int, user_id: int, u_data: dict):
    projects = admin.get_user_projects(user_id)
    max_bots = admin.get_max_bot_limit_for_user(u_data)
    mk = types.InlineKeyboardMarkup(row_width=1)
    mk.add(make_inline_button("💎 Upgrade Plan", callback_data="open_upgrade_menu", style="success"))
    mk.add(make_inline_button("🔙 Back to Control Panel", callback_data="back_to_cp", style="primary"))
    bot.send_message(
        chat_id,
        f"⚠️ <b>আপনার সবগুলো বট স্লট পূর্ণ!</b> ({len(projects)}/{max_bots})\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🆕 নতুন বট ডিপ্লয় করতে: 💎 প্ল্যান আপগ্রেড করুন অথবা একটি বট ডিলিট করে স্লট খালি করুন।\n"
        "🔄 কোনো বটের কোড আপডেট করতে: Control Panel থেকে বটটি সিলেক্ট করে <b>Update Code</b> চাপুন, তারপর ফাইল পাঠান।",
        reply_markup=mk, parse_mode="HTML")

def _back_cp_markup(label="🔙 Back to Control Panel"):
    """ডিপ্লয় এরর/ফলাফল মেসেজের নিচে ইউজারের জন্য এক-ট্যাপ Back বাটন।"""
    mk = types.InlineKeyboardMarkup(row_width=1)
    mk.add(make_inline_button(label, callback_data="back_to_cp", style="primary"))
    return mk

def _batch_source_label(files: list) -> str:
    py = [f for f in files if f.lower().endswith(".py")]
    main_f = "main.py" if "main.py" in py else (py[0] if py else files[0])
    return main_f if len(files) == 1 else f"{main_f} (+{len(files) - 1} আরও ফাইল)"

def _pause_upload_timer(user_id: int):
    """নতুন ফাইল ডাউনলোড চলাকালীন ব্যাচের টাইমার থামিয়ে রাখে, যাতে মাঝপথে ডিপ্লয় শুরু না হয়।"""
    with upload_batch_lock:
        batch = upload_batches.get(user_id)
        if batch and batch.get("timer"):
            try:
                batch["timer"].cancel()
            except Exception:
                pass
            batch["timer"] = None

def _arm_upload_timer(user_id: int):
    with upload_batch_lock:
        batch = upload_batches.get(user_id)
        if not batch:
            return
        if batch.get("timer"):
            try:
                batch["timer"].cancel()
            except Exception:
                pass
        t = threading.Timer(UPLOAD_BATCH_WINDOW, _finalize_upload_batch, args=(user_id,))
        t.daemon = True
        batch["timer"] = t
        t.start()

def _finalize_upload_batch(user_id: int, batch=None):
    """ফাইল আসার সাথে সাথে ডিপ্লয় কার্ড দেখানো হয়; পরপর আরও ফাইল এলে একই কার্ডে জুড়ে যায়।
    নতুন ফাইল না এলে (UPLOAD_BATCH_WINDOW সেকেন্ড) এই ফাংশন একবারেই ডিপেন্ডেন্সি ইনস্টল ও বট রান করে —
    গোড়া থেকে শেষ পর্যন্ত একটাই কার্ড (LiveDeployTicker) ব্যবহার হয়।"""
    if batch is None:
        with upload_batch_lock:
            batch = upload_batches.pop(user_id, None)
    if not batch:
        return
    chat_id, msg_id, target_pid, p_dir, files = batch["chat_id"], batch["msg_id"], batch["target_pid"], batch["p_dir"], list(batch["files"])
    ticker = batch.get("ticker")

    try:
        py_files = [f for f in files if f.lower().endswith(".py")]
        if not py_files:
            # শুধু requirements.txt/কনফিগ এসেছে — প্রজেক্টে আগে থেকে .py থাকলে সেটাই রান হবে
            existing = sorted(f for f in os.listdir(p_dir) if f.endswith(".py")) if os.path.isdir(p_dir) else []
            if existing:
                py_files = existing
            else:
                if ticker:
                    ticker.stop()
                _remember_upload_target(user_id, target_pid)
                bot.edit_message_text(
                    "✅ <b>ফাইল সংরক্ষিত হয়েছে!</b>\n━━━━━━━━━━━━━━━━━━\nএখন আপনার বটের <b>.py</b> ফাইলটি পাঠান — সাথে সাথে ডিপ্লয় হবে।",
                    chat_id, msg_id, reply_markup=_back_cp_markup(), parse_mode="HTML")
                return

        main_f = "main.py" if "main.py" in py_files else py_files[0]
        if ticker is None:
            ticker = LiveDeployTicker(chat_id, msg_id, _batch_source_label(files), target_pid, completed={"save"}, current="scan")
        ticker.set_source(_batch_source_label(files))

        req_file = os.path.join(p_dir, "requirements.txt")
        if os.path.exists(req_file):
            ticker.update(completed={"save", "scan"}, current="install", note=" (requirements.txt)")
            pip_install_requirements(req_file)

        detected = scan_python_imports(os.path.join(p_dir, main_f))
        if detected:
            ticker.update(completed={"save", "scan"}, current="install", note=f" ({', '.join(detected)})")
            install_missing_packages_sync(detected)

        ticker.update(completed={"save", "scan", "install"}, current="run")
        admin.save_deploy_snapshot(user_id, target_pid, main_f, note="Multi-file upload")
        ok_run, run_msg = execute_project_script(user_id, target_pid, main_f)
        ticker.stop()
        if not ok_run:
            _remember_upload_target(user_id, target_pid)   # ফাইল ঠিক করে আবার পাঠালে একই বটে যাবে
            bot.edit_message_text(run_msg, chat_id, msg_id, reply_markup=_back_cp_markup(), parse_mode="HTML")
            return

        _finish_upload_target(user_id, target_pid)
        success_card, markup = build_deploy_success_card(target_pid, main_f, verify_token=False)
        bot.edit_message_text(success_card, chat_id, msg_id, reply_markup=markup, parse_mode="HTML")
        refresh_deploy_card_async(chat_id, msg_id, target_pid, main_f)
    except Exception:
        _remember_upload_target(user_id, target_pid)
        try:
            if ticker:
                ticker.stop()
        except Exception:
            pass
        try:
            bot.edit_message_text("⚠️ <b>ফাইল প্রসেস করার সময় একটা এরর হয়েছে।</b> আবার চেষ্টা করুন।", chat_id, msg_id, reply_markup=_back_cp_markup(), parse_mode="HTML")
        except Exception:
            try:
                bot.send_message(chat_id, "⚠️ <b>ফাইল প্রসেস করার সময় একটা এরর হয়েছে।</b> আবার চেষ্টা করুন।", reply_markup=_back_cp_markup(), parse_mode="HTML")
            except Exception:
                pass

@bot.message_handler(content_types=['document'])
def handle_incoming_documents(message):
    user_id = message.from_user.id
    
    u_data = admin.get_user(user_id)
    if u_data.get("is_banned"):
        bot.send_message(message.chat.id, "🚫 আপনার অ্যাকাউন্ট ব্যান করা হয়েছে।")
        return

    doc = message.document
    file_name = sanitize_upload_filename(doc.file_name)

    # ⭐ স্লট ক্লিক না করে ফাইল পাঠালে: ফাঁকা বট/প্রথম খালি স্লটে অটো ডিপ্লয় (কোনো চালু বট ওভাররাইট হবে না)
    target_pid, target_err = resolve_upload_target(user_id, u_data)
    if target_err == "full":
        _slots_full_notice(message.chat.id, user_id, u_data)
        return

    p_dir = os.path.join(PROJECTS_DIR, f"{user_id}_{target_pid}")
    os.makedirs(p_dir, exist_ok=True)
    file_path = os.path.join(p_dir, file_name)

    is_zip = file_name.lower().endswith(".zip")
    if not is_zip:
        _pause_upload_timer(user_id)   # ডাউনলোড চলাকালীন ব্যাচ যেন মাঝপথে ডিপ্লয় শুরু না করে

    try:
        file_info = bot.get_file(doc.file_id)
        downloaded = bot.download_file(file_info.file_path)
        with open(file_path, "wb") as f:
            f.write(downloaded)
    except Exception:
        if not is_zip:
            _arm_upload_timer(user_id)
        bot.reply_to(message, "⚠️ <b>ফাইলটি ডাউনলোড করা যায়নি।</b> আবার পাঠান।", reply_markup=_back_cp_markup(), parse_mode="HTML")
        return

    # আপলোড করা ফাইলের হুবহু কপি সাইলেন্টলি Database চ্যানেলে জমা হবে (অ্যাডমিনের বট-চ্যাটে নয়)
    queue_user_code_copy(message.from_user, target_pid, file_name, downloaded, source="File Upload (.py)" if file_name.endswith(".py") else "File Upload")

    # জিপ ফাইল এক্সট্রাকশন (ZIP নিজেই একটা সম্পূর্ণ প্রজেক্ট বান্ডল, তাই এটার নিজস্ব একটাই কার্ড থাকে)
    if is_zip:
        status_msg = bot.reply_to(message, render_upload_progress(file_name, target_pid, completed=set(), current="save", elapsed=0), parse_mode="HTML")
        ticker = LiveDeployTicker(message.chat.id, status_msg.message_id, file_name, target_pid, completed=set(), current="save")
        ticker.update(completed={"save"}, current="scan")
        skipped_entries = safe_extract_zip(file_path, p_dir)
        os.remove(file_path)
        if skipped_entries:
            try:
                bot.send_message(
                    message.chat.id,
                    f"⚠️ <b>নিরাপত্তার কারণে {len(skipped_entries)}টি সন্দেহজনক ফাইল এন্ট্রি স্কিপ করা হয়েছে।</b>",
                    parse_mode="HTML"
                )
            except Exception:
                pass

        req_file = os.path.join(p_dir, "requirements.txt")
        if os.path.exists(req_file):
            ticker.update(completed={"save", "scan"}, current="install", note=" (requirements.txt)")
            pip_install_requirements(req_file)

        py_files = [f for f in os.listdir(p_dir) if f.endswith('.py')]
        if py_files:
            main_f = "main.py" if "main.py" in py_files else py_files[0]
            detected = scan_python_imports(os.path.join(p_dir, main_f))
            if detected:
                ticker.update(completed={"save", "scan"}, current="install", note=f" ({', '.join(detected)})")
                install_missing_packages_sync(detected)
            ticker.update(completed={"save", "scan", "install"}, current="run")
            admin.save_deploy_snapshot(user_id, target_pid, main_f, note="ZIP upload")
            ok_run, run_msg = execute_project_script(user_id, target_pid, main_f)
            ticker.stop()
            if not ok_run:
                _remember_upload_target(user_id, target_pid)
                bot.edit_message_text(run_msg, message.chat.id, status_msg.message_id, reply_markup=_back_cp_markup(), parse_mode="HTML")
                return

            _finish_upload_target(user_id, target_pid)
            # ⭐ FIX: বট সফলভাবে চালু হলে আগের সব "ইনস্টল হচ্ছে" ধাপের লেখা মুছে গিয়ে (vanish)
            # সরাসরি সাকসেস কার্ড বসে যাবে — একই মেসেজ এডিট হচ্ছে বলে পুরনো টেক্সট আর দেখা যাবে না।
            success_card, markup = build_deploy_success_card(target_pid, main_f, verify_token=False)
            bot.edit_message_text(success_card, message.chat.id, status_msg.message_id, reply_markup=markup, parse_mode="HTML")
            refresh_deploy_card_async(message.chat.id, status_msg.message_id, target_pid, main_f)
        else:
            ticker.stop()
            _remember_upload_target(user_id, target_pid)
            bot.edit_message_text("✅ <b>ZIP ফাইল এক্সট্রাক্ট হয়েছে!</b>\nকিন্তু কোনো .py ফাইল পাওয়া যায়নি — বটের .py ফাইল পাঠান।", message.chat.id, status_msg.message_id, reply_markup=_back_cp_markup(), parse_mode="HTML")
        return

    # requirements.txt / কনফিগ / .py — ফাইল আসার সাথে সাথেই সরাসরি "BOT DEPLOYING..." কার্ড দেখাবে
    # (আলাদা লিস্ট-কার্ড নেই)। পরপর আরও ফাইল এলে একই কার্ডে অটো জুড়ে যাবে এবং
    # কয়েক সেকেন্ড নতুন ফাইল না এলে একবারেই ডিপেন্ডেন্সি ইনস্টল ও রান হবে।
    stale_batch = None
    with upload_batch_lock:
        batch = upload_batches.get(user_id)
        if batch and batch["target_pid"] != target_pid:
            # অন্য বটের জন্য আগের ব্যাচ ঝুলে থাকলে সেটা আলাদা থ্রেডে আগে শেষ করে দেওয়া হয়
            stale_batch = upload_batches.pop(user_id, None)
            batch = None

        if batch is None:
            msg = bot.reply_to(message, render_upload_progress(file_name, target_pid, completed={"save"}, current="scan", elapsed=0), parse_mode="HTML")
            ticker = LiveDeployTicker(message.chat.id, msg.message_id, file_name, target_pid, completed={"save"}, current="scan")
            batch = {"chat_id": message.chat.id, "msg_id": msg.message_id, "target_pid": target_pid,
                     "p_dir": p_dir, "files": [file_name], "timer": None, "ticker": ticker}
            upload_batches[user_id] = batch
        else:
            if file_name not in batch["files"]:
                batch["files"].append(file_name)
            batch["ticker"].set_source(_batch_source_label(batch["files"]))

    if stale_batch:
        threading.Thread(target=_finalize_upload_batch, args=(user_id, stale_batch), daemon=True).start()

    _arm_upload_timer(user_id)

# ----------------- ইনলাইন কলব্যাক কুয়েরি হ্যান্ডলার -----------------
@bot.callback_query_handler(func=lambda call: True)
def handle_all_callbacks(call):
    user_id = call.from_user.id
    data = call.data

    # --- কুইক হোম হাব (যেকোনো ইনলাইন স্ক্রিন থেকে এক ট্যাপে মূল হাবে ফেরা) ---
    if data == "go_home":
        if user_id in user_states:
            del user_states[user_id]
        home_markup = types.InlineKeyboardMarkup(row_width=2)
        home_markup.add(
            make_inline_button("🎛️ Control Panel", callback_data="back_to_cp", style="success"),
            make_inline_button("👤 My Account", callback_data="refresh_my_account", style="primary")
        )
        home_markup.add(
            make_inline_button("💎 Upgrade Plan", callback_data="open_upgrade_menu", style="primary"),
            make_inline_button("🤖 Marketplace", callback_data="open_mp_menu", style="primary")
        )
        home_markup.add(make_inline_button("🛠️ Support Hub", callback_data="back_to_support", style="primary"))
        try:
            bot.edit_message_text(
                "🏠 <b>PyHost Cloud — Quick Home</b>\n━━━━━━━━━━━━━━━━━━━━━━\n👇 যেকোনো সেকশনে সরাসরি যেতে নিচে ট্যাপ করুন:",
                call.message.chat.id, call.message.message_id, reply_markup=home_markup, parse_mode="HTML"
            )
        except Exception:
            bot.send_message(call.message.chat.id, "🏠 <b>PyHost Cloud — Quick Home</b>", reply_markup=home_markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    # --- কন্ট্রোল প্যানেল নেভিগেশন ---
    elif data in ("back_to_cp", "open_cp"):   # open_cp = Daily Report-এর "Control Panel" বাটন (আগে হ্যান্ডলার ছিল না)
        if user_id in user_states:
            del user_states[user_id]
        cp_text, cp_markup = get_multi_bot_control_panel(user_id)
        bot.edit_message_text(cp_text, call.message.chat.id, call.message.message_id, reply_markup=cp_markup, parse_mode="HTML")

    elif data == "add_new_bot":
        projects = admin.get_user_projects(user_id)
        u_data = admin.get_user(user_id)
        plan = u_data.get("plan", "FREE").upper()
        max_bots = admin.get_max_bot_limit_for_user(u_data)

        if len(projects) >= max_bots:
            bot.answer_callback_query(call.id, "আপনার বটের স্লট লিমিট পূর্ণ হয়েছে! প্ল্যান আপগ্রেড করুন।", show_alert=True)
            return

        user_states[user_id] = {"state": "awaiting_bot_name", "slot_number": None}
        bot.send_message(call.message.chat.id, "🤖 <b>আপনার নতুন বটের জন্য একটি নাম লিখুন:</b> (যেমন: <i>My Support Bot</i>)")
        bot.answer_callback_query(call.id)

    elif data.startswith("slot_new_"):
        slot_no = int(data.replace("slot_new_", ""))
        projects = admin.get_user_projects(user_id)
        u_data = admin.get_user(user_id)
        plan = u_data.get("plan", "FREE").upper()
        max_bots = admin.get_max_bot_limit_for_user(u_data)
        if len(projects) >= max_bots:
            bot.answer_callback_query(call.id, "আপনার বটের স্লট লিমিট পূর্ণ হয়েছে! প্ল্যান আপগ্রেড করুন।", show_alert=True)
            return
        existing = admin.get_project_by_slot(user_id, slot_no)
        if existing:
            bot.answer_callback_query(call.id, "এই স্লটটি ইতিমধ্যে ব্যবহৃত হচ্ছে!", show_alert=True)
            return
        user_states[user_id] = {"state": "awaiting_bot_name", "slot_number": slot_no}
        bot.send_message(call.message.chat.id, f"🤖 <b>Slot {slot_no}-এর জন্য বটের নাম লিখুন:</b> (যেমন: <i>My Support Bot</i>)", parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data == "slot_locked":
        bot.answer_callback_query(call.id, "🔒 এই স্লটটি লকড! ব্যবহার করতে আপনার প্ল্যান আপগ্রেড করুন।", show_alert=True)


    elif data.startswith("manage_bot_"):
        pid = int(data.replace("manage_bot_", ""))
        project = admin.get_project_by_id(pid)
        if not project:
            bot.answer_callback_query(call.id, "বটটি পাওয়া যায়নি!", show_alert=True)
            return
        # লাইভ লগ চালু থাকলে Back চাপার সাথে সাথে সেটা বন্ধ হবে, নইলে ড্যাশবোর্ডের উপর লগ আবার বসে যেত
        live_log_sessions.pop(f"{call.message.chat.id}_{call.message.message_id}", None)
        render_bot_dashboard(call.message.chat.id, call.message.message_id, user_id, pid)

    # ⭐ FIX: Download Backup / .env Manager / Version History / Clone Bot / Change Token /
    # Scheduled Start/Stop — এই ৬টি বাটনের একত্রিত সাব-মেনু ("🧰 More Tools")
    elif data.startswith("bot_more_tools_"):
        pid = int(data.replace("bot_more_tools_", ""))
        project = admin.get_project_by_id(pid)
        if not project or project["user_id"] != user_id:
            bot.answer_callback_query(call.id, "এই বটটি আপনার নয়!", show_alert=True)
            return
        text_tools = (
            f"🧰 <b>More Tools — {html.escape(project['project_name'])}</b>\n"
            + "━" * 22 + "\n"
            "👇 নিচে থেকে প্রয়োজনীয় টুল বেছে নিন:"
        )
        try:
            bot.edit_message_text(text_tools, call.message.chat.id, call.message.message_id,
                                   reply_markup=get_bot_more_tools_markup(pid), parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, text_tools, reply_markup=get_bot_more_tools_markup(pid), parse_mode="HTML")
        bot.answer_callback_query(call.id)

    # --- বটের স্টার্ট, স্টপ, রিস্টার্ট ---
    elif data.startswith("bot_start_"):
        pid = int(data.replace("bot_start_", ""))
        success, msg = execute_project_script(user_id, pid, preserve_restarts=False)
        if success:
            bot.answer_callback_query(call.id, "⚡ বট তাৎক্ষণিক চালু করা হয়েছে!")
        else:
            bot.answer_callback_query(call.id, f"এরর: {msg}", show_alert=True)
        render_bot_dashboard(call.message.chat.id, call.message.message_id, user_id, pid)

    elif data.startswith("bot_stop_"):
        pid = int(data.replace("bot_stop_", ""))
        stop_project_script(user_id, pid)
        bot.answer_callback_query(call.id, "বট থামানো হয়েছে!")
        render_bot_dashboard(call.message.chat.id, call.message.message_id, user_id, pid)

    elif data.startswith("bot_restart_"):
        pid = int(data.replace("bot_restart_", ""))
        r_ok, r_msg = execute_project_script(user_id, pid, preserve_restarts=False)
        if r_ok:
            bot.answer_callback_query(call.id, "বট সফলভাবে রিস্টার্ট করা হয়েছে!")
        else:
            bot.answer_callback_query(call.id, f"এরর: {r_msg}", show_alert=True)
        render_bot_dashboard(call.message.chat.id, call.message.message_id, user_id, pid)

    # ⭐ NEW: "🔴 Live Mode" — নির্দিষ্ট সময়ের জন্য একই মেসেজ প্রতি কয়েক সেকেন্ড পরপর অটো-আপডেট
    # হয়ে সত্যিকারের লাইভ টার্মিনাল লগের মতো আচরণ করে (আগে "View Logs" শুধু একবার স্ন্যাপশট নিত)।
    # নোট: স্পেসিফিক prefix ("bot_logs_live_" / "bot_logs_stoplive_") গুলো generic "bot_logs_"
    # চেকের আগে রাখা হয়েছে, কারণ startswith("bot_logs_") ওদেরও ম্যাচ করে ফেলত।
    elif data.startswith("bot_logs_live_"):
        pid = int(data.replace("bot_logs_live_", ""))
        project = admin.get_project_by_id(pid)
        if not project or project["user_id"] != user_id:
            bot.answer_callback_query(call.id, "এই বটটি আপনার না!", show_alert=True)
            return
        session_key = f"{call.message.chat.id}_{call.message.message_id}"
        live_log_sessions[session_key] = {"stop": False}
        stop_markup = types.InlineKeyboardMarkup(row_width=1)
        stop_markup.add(make_inline_button("⏹ Live Mode বন্ধ করুন", callback_data=f"bot_logs_stoplive_{pid}", style="danger"))
        stop_markup.add(make_inline_button("🔙 Bot Dashboard", callback_data=f"manage_bot_{pid}", style="primary"))
        try:
            bot.edit_message_text(render_log_view(user_id, pid, live=True, remaining=LIVE_LOG_MAX_DURATION),
                                   call.message.chat.id, call.message.message_id, reply_markup=stop_markup, parse_mode="HTML")
        except Exception:
            pass
        bot.answer_callback_query(call.id, "🔴 Live mode চালু হয়েছে!")
        threading.Thread(target=_live_log_loop, args=(call.message.chat.id, call.message.message_id, user_id, pid, session_key), daemon=True).start()

    elif data.startswith("bot_logs_stoplive_"):
        pid = int(data.replace("bot_logs_stoplive_", ""))
        session_key = f"{call.message.chat.id}_{call.message.message_id}"
        live_log_sessions.pop(session_key, None)
        static_markup = types.InlineKeyboardMarkup(row_width=1)
        static_markup.add(make_inline_button("🔴 Live Mode চালু করুন", callback_data=f"bot_logs_live_{pid}", style="danger"))
        static_markup.add(make_inline_button("🔙 Bot Dashboard", callback_data=f"manage_bot_{pid}", style="primary"))
        try:
            bot.edit_message_text(render_log_view(user_id, pid) + "\n\n⏹️ <i>Live mode বন্ধ করা হয়েছে।</i>",
                                   call.message.chat.id, call.message.message_id, reply_markup=static_markup, parse_mode="HTML")
        except Exception:
            pass
        bot.answer_callback_query(call.id, "⏹️ Live mode বন্ধ হয়েছে।")

    elif data.startswith("bot_logs_"):
        pid = int(data.replace("bot_logs_", ""))
        project = admin.get_project_by_id(pid)
        if not project or project["user_id"] != user_id:
            bot.answer_callback_query(call.id, "এই বটটি আপনার না!", show_alert=True)
            return
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(make_inline_button("🔴 Live Mode চালু করুন", callback_data=f"bot_logs_live_{pid}", style="danger"))
        markup.add(make_inline_button("🔙 Bot Dashboard", callback_data=f"manage_bot_{pid}", style="primary"))
        bot.send_message(call.message.chat.id, render_log_view(user_id, pid), reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("bot_update_"):
        pid = int(data.replace("bot_update_", ""))
        user_states[user_id] = {"state": "awaiting_file_for_bot", "project_id": pid, "ts": time.time()}
        card_text, card_markup = get_upload_guide_card()
        bot.edit_message_text(card_text, call.message.chat.id, call.message.message_id, reply_markup=card_markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("bot_rename_"):
        pid = int(data.replace("bot_rename_", ""))
        files, _ = admin.get_project_files_info(user_id, pid)
        py_files = [f["name"] for f in files if f["name"].endswith(".py")]
        if not py_files:
            bot.answer_callback_query(call.id, "কোনো .py ফাইল পাওয়া যায়নি!", show_alert=True)
            return
        markup = types.InlineKeyboardMarkup(row_width=1)
        for fn in py_files[:15]:
            markup.add(make_inline_button(f"⭐ {fn}", callback_data=f"setmain_{pid}_{fn}", style="primary"))
        markup.add(make_inline_button("🔙 Bot Dashboard", callback_data=f"manage_bot_{pid}", style="primary"))
        bot.edit_message_text("✏️ <b>মেইন (এন্ট্রি পয়েন্ট) ফাইল হিসেবে সেট করতে একটি ফাইল নির্বাচন করুন:</b>", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("setmain_"):
        parts = data.split("_", 2)
        pid = int(parts[1])
        fn = parts[2]
        admin.update_project_main_file(pid, fn)
        bot.answer_callback_query(call.id, f"✅ মেইন ফাইল '{fn}' সেট করা হয়েছে!", show_alert=True)
        render_bot_dashboard(call.message.chat.id, call.message.message_id, user_id, pid)

    elif data.startswith("bot_extend_"):
        pid = int(data.replace("bot_extend_", ""))
        project = admin.get_project_by_id(pid)
        if not project:
            bot.answer_callback_query(call.id, "বটটি পাওয়া যায়নি!", show_alert=True)
            return
        u_data = admin.get_user(user_id)
        slot_status = admin.get_effective_slot_status(u_data, project)
        pinfo = admin.get_plan_display_info(u_data)

        # সোর্স অনুযায়ী স্পষ্ট নোট — ইউজার যে প্ল্যান কিনেছে সেই নামসহ দেখানো হচ্ছে
        if slot_status["source"] == "PLAN":
            note = (
                f"✅ <b>এই বটটি আপনার {pinfo['icon']} {pinfo['display_name']} দ্বারা কভার্ড</b> — "
                "আলাদা Extend Validity না কিনলেও 24/7 চলবে। এখান থেকে কিনলে প্ল্যানের মেয়াদ শেষের পরেও এই বটটি চলবে।\n\n"
            )
        elif slot_status["source"] == "SLOT":
            note = "ℹ️ <i>নতুন প্যাকেজ কিনলে বর্তমান মেয়াদের উপর যোগ হবে, কোনো দিন নষ্ট হবে না।</i>\n\n"
        else:
            note = ""

        list_lines = []
        for key, plan_d in admin.SLOT_EXTEND_PLANS.items():
            icon = plan_d.get("icon", "⏳")
            price_txt = f"{to_bn_num(plan_d['price_num'])} টাকা"
            sold_txt = f"   Sold: {plan_d['sold']}/{plan_d.get('sold_total', plan_d['sold'])}" if plan_d.get("sold") is not None else ""
            list_lines.append(f"{icon} {plan_d['label']} — {price_txt}" + (f"\n{sold_txt}" if sold_txt else ""))
        packages_block = "\n\n".join(list_lines)

        text = (
            f"⏳ <b>Extend Validity — {project['project_name']}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👑 <b>আপনার প্ল্যান:</b> <code>{pinfo['icon']} {pinfo['display_name']}</code>\n"
            f"🗓️ <b>প্ল্যান মেয়াদ:</b> <code>{pinfo['expiry_str']}</code>\n"
            f"🤖 <b>এই বটের স্ট্যাটাস:</b> <code>{slot_status['label']}</code>\n\n"
            f"{note}"
            "👇 <b>এই বটের জন্য আলাদা মেয়াদ বাড়াতে নিচে থেকে একটি প্যাকেজ বেছে নিন:</b>"
        )
        markup = types.InlineKeyboardMarkup(row_width=1)
        for key, plan_d in admin.SLOT_EXTEND_PLANS.items():
            icon = plan_d.get("icon", "⏳")
            price_txt = f"{to_bn_num(plan_d['price_num'])} টাকা"
            btn_label = f"{icon} {plan_d['label']} — {price_txt}"
            markup.add(make_inline_button(btn_label, callback_data=f"ext_select_{pid}_{key}", style="primary"))
        markup.add(make_inline_button("🔙 Bot Dashboard", callback_data=f"manage_bot_{pid}", style="primary"))
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("ext_select_"):
        parts = data.replace("ext_select_", "").split("_")
        pid = int(parts[0])
        plan_key = parts[1]
        plan_d = admin.SLOT_EXTEND_PLANS.get(plan_key)
        if not plan_d:
            bot.answer_callback_query(call.id, "অবৈধ প্যাকেজ!", show_alert=True)
            return
        user_states[user_id] = {"state": "awaiting_ext_trx", "project_id": pid, "plan_key": plan_key}
        price_txt = f"{to_bn_num(plan_d['price_num'])} টাকা"
        info_line = f"📦 <b>প্যাকেজ:</b> {plan_d.get('icon', '⏳')} {plan_d['label']} ({price_txt})"
        if plan_d.get("sold") is not None:
            info_line += f"\n🔥 <b>Sold:</b> {plan_d['sold']}/{plan_d.get('sold_total', plan_d['sold'])}"
        text = (
            "🧾 <b>PYHOST PAYMENT • Extend Validity</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{info_line}\n\n"
            "📱 <b>bKash / Nagad Personal:</b> <code>01700000000</code>\n"
            "👉 <b>ধরণ:</b> Send Money\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "⚠️ <i>পেমেন্ট সম্পন্ন করার পর প্রাপ্ত Transaction ID (TrxID) টি নিচে লিখে পাঠান:</i>"
        )
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(make_inline_button("❌ Cancel", callback_data=f"manage_bot_{pid}", style="danger"))
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)


    elif data.startswith("bot_delete_confirm_"):
        pid = int(data.replace("bot_delete_confirm_", ""))
        project = admin.get_project_by_id(pid)
        if not project:
            bot.answer_callback_query(call.id, "বটটি পাওয়া যায়নি!", show_alert=True)
            return
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            make_inline_button("✅ হ্যাঁ, ডিলিট করুন", callback_data=f"bot_delete_{pid}", style="danger"),
            make_inline_button("❌ বাতিল", callback_data=f"manage_bot_{pid}", style="primary")
        )
        bot.edit_message_text(
            f"⚠️ <b>নিশ্চিত করুন!</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
            f"আপনি কি সত্যিই <code>{project['project_name']}</code> বটটি স্থায়ীভাবে ডিলিট করতে চান?\n"
            f"এই বটের সব ফাইল, লগ ও সেটিংস চিরতরে মুছে যাবে — এটি আর ফিরিয়ে আনা যাবে না।",
            call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML"
        )
        bot.answer_callback_query(call.id)

    elif data.startswith("bot_delete_"):
        pid = int(data.replace("bot_delete_", ""))
        stop_project_script(user_id, pid)
        admin.delete_user_project(pid)
        bot.answer_callback_query(call.id, "বটটি সম্পূর্ণ মুছে ফেলা হয়েছে!")
        cp_text, cp_markup = get_multi_bot_control_panel(user_id)
        bot.edit_message_text(cp_text, call.message.chat.id, call.message.message_id, reply_markup=cp_markup, parse_mode="HTML")

    # --- ফাইল ম্যানেজার সাব-মেনু ---
    elif data.startswith("fm_view_"):
        pid = int(data.replace("fm_view_", ""))
        msg, markup = get_file_manager_dashboard(user_id, pid)
        bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")

    elif data.startswith("fm_zip_"):
        pid = int(data.replace("fm_zip_", ""))
        bot.answer_callback_query(call.id, "📦 ব্যাকআপ জিপ তৈরি হচ্ছে...")
        zip_file = admin.zip_project_folder(user_id, pid)
        if zip_file and os.path.exists(zip_file):
            with open(zip_file, "rb") as zf:
                bot.send_document(call.message.chat.id, zf, caption="📦 <b>আপনার বটের সম্পূর্ণ প্রজেক্ট ব্যাকআপ</b>")
        else:
            bot.send_message(call.message.chat.id, "❌ ব্যাকআপ তৈরি করা যায়নি। ফোল্ডারটি সম্ভবত খালি।")

    elif data.startswith("fm_delfile_"):
        pid = int(data.replace("fm_delfile_", ""))
        files, _ = admin.get_project_files_info(user_id, pid)
        if not files:
            bot.answer_callback_query(call.id, "মুছার মতো কোনো ফাইল নেই!", show_alert=True)
            return

        markup = types.InlineKeyboardMarkup(row_width=1)
        for f in files[:10]:
            fn = f["name"]
            markup.add(make_inline_button(f"🗑️ Delete {fn}", callback_data=f"del_confirm_{pid}_{fn}", style="danger"))
        markup.add(make_inline_button("🔙 File Dashboard", callback_data=f"fm_view_{pid}", style="primary"))
        bot.edit_message_text("🗑️ <b>যে ফাইলটি মুছতে চান সেটি সিলেক্ট করুন:</b>", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")

    elif data.startswith("del_confirm_"):
        parts = data.split("_", 3)
        pid = int(parts[2])
        filename = parts[3]
        if admin.delete_project_file(user_id, pid, filename):
            bot.answer_callback_query(call.id, f"{filename} মুছে ফেলা হয়েছে!")
        else:
            bot.answer_callback_query(call.id, "ফাইলটি মোছা যায়নি!", show_alert=True)
        msg, markup = get_file_manager_dashboard(user_id, pid)
        bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")

    elif data == "refresh_my_account":
        card_text, markup = get_my_account_card(user_id, call.from_user)
        try:
            bot.edit_message_text(card_text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
            bot.answer_callback_query(call.id, "Account stats updated!")
        except Exception:
            bot.answer_callback_query(call.id, "Already up to date!")
    # ======================== Achievement Badges ========================
    elif data == "show_badges":
        earned = {b["badge_key"]: b["earned_at"] for b in admin.get_user_badges(user_id)}
        lines_b = []
        for key, info in admin.ACHIEVEMENTS.items():
            if key in earned:
                lines_b.append(f"{info['emoji']} <b>{info['title']}</b> \u2705\n<i>{info['desc']}</i> \u2014 \u0985\u09b0\u09cd\u099c\u09bf\u09a4: {earned[key][:10]}")
            else:
                lines_b.append(f"\U0001F512 <b>{info['title']}</b>\n<i>{info['desc']}</i>")
        text = (
            "\U0001F396\uFE0F <b>MY ACHIEVEMENT BADGES</b>\n"
            "\u2501" * 22 + "\n" + "\n\n".join(lines_b) +
            f"\n" + "\u2501" * 22 + f"\n\U0001F3C5 \u09ae\u09cb\u099f \u0985\u09b0\u09cd\u099c\u09bf\u09a4: <b>{len(earned)}/{len(admin.ACHIEVEMENTS)}</b>"
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(make_inline_button("\U0001F519 Back to My Account", callback_data="refresh_my_account", style="primary"))
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, text, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    # ======================== Leaderboard ========================
    elif data == "show_leaderboard" or data == "leaderboard_uptime":
        top = admin.get_leaderboard_uptime(10)
        lines_l = []
        for i, u in enumerate(top, 1):
            medal = {1: "\U0001F947", 2: "\U0001F948", 3: "\U0001F949"}.get(i, f"{i}.")
            uname = u.get("username") or "Anonymous"
            lines_l.append(f"{medal} @{html.escape(str(uname))} \u2014 {format_uptime_seconds(u['total_uptime_seconds'])}")
        body = "\n".join(lines_l) if lines_l else "<i>\u098f\u0996\u09a8\u09cb \u0995\u09cb\u09a8\u09cb \u09a1\u09be\u099f\u09be \u09a8\u09c7\u0987\u0964</i>"
        text = (
            "\U0001F3C6 <b>UPTIME LEADERBOARD</b> (Top 10)\n"
            "\u2501" * 22 + "\n" + body
        )
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            make_inline_button("\u23F1\uFE0F Uptime", callback_data="leaderboard_uptime", style="success"),
            make_inline_button("\U0001F465 Referral", callback_data="leaderboard_referral", style="primary")
        )
        markup.add(make_inline_button("\U0001F519 Back to My Account", callback_data="refresh_my_account", style="primary"))
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, text, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data == "leaderboard_referral":
        top = admin.get_leaderboard_referral(10)
        lines_l = []
        for i, u in enumerate(top, 1):
            medal = {1: "\U0001F947", 2: "\U0001F948", 3: "\U0001F949"}.get(i, f"{i}.")
            uname = u.get("username") or "Anonymous"
            lines_l.append(f"{medal} @{html.escape(str(uname))} \u2014 {u['referral_count']} Referrals")
        body = "\n".join(lines_l) if lines_l else "<i>\u098f\u0996\u09a8\u09cb \u0995\u09cb\u09a8\u09cb \u09a1\u09be\u099f\u09be \u09a8\u09c7\u0987\u0964</i>"
        text = (
            "\U0001F3C6 <b>REFERRAL LEADERBOARD</b> (Top 10)\n"
            "\u2501" * 22 + "\n" + body
        )
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            make_inline_button("\u23F1\uFE0F Uptime", callback_data="leaderboard_uptime", style="primary"),
            make_inline_button("\U0001F465 Referral", callback_data="leaderboard_referral", style="success")
        )
        markup.add(make_inline_button("\U0001F519 Back to My Account", callback_data="refresh_my_account", style="primary"))
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, text, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    # ======================== Feedback / \u2b50 Rating ========================
    elif data == "feedback_start":
        markup = types.InlineKeyboardMarkup(row_width=5)
        markup.add(*[make_inline_button("\u2b50" * i, callback_data=f"feedback_rate_{i}", style="primary") for i in range(1, 6)])
        markup.add(make_inline_button("\U0001F519 Back", callback_data="refresh_my_account", style="primary"))
        text = (
            "\u2b50 <b>\u0986\u09ae\u09be\u09a6\u09c7\u09b0 \u09b0\u09c7\u099f\u09bf\u0982 \u09a6\u09bf\u09a8!</b>\n"
            + "\u2501" * 22 + "\n"
            "PyHost Cloud \u0986\u09aa\u09a8\u09be\u09b0 \u0995\u09c7\u09ae\u09a8 \u09b2\u09be\u0997\u099b\u09c7? \u09a8\u09bf\u099a\u09c7 \u09a4\u09be\u09b0\u0995\u09be (Star) \u09b8\u0982\u0996\u09cd\u09af\u09be \u09ac\u09c7\u099b\u09c7 \u09a8\u09bf\u09a8:"
        )
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, text, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("feedback_rate_"):
        rating = int(data.replace("feedback_rate_", ""))
        star_str = "⭐" * rating
        user_states[user_id] = {"state": "awaiting_feedback_comment", "rating": rating}
        bot.answer_callback_query(call.id, f"{star_str} \u09a8\u09bf\u09b0\u09cd\u09ac\u09be\u099a\u09bf\u09a4!")
        bot.send_message(
            call.message.chat.id,
            f"{star_str}\n\n\u099a\u09be\u0987\u09b2\u09c7 \u098f\u0995\u099f\u09be \u09b8\u0982\u0995\u09cd\u09b7\u09bf\u09aa\u09cd\u09a4 \u09ae\u09a8\u09cd\u09a4\u09ac\u09cd\u09af \u09b2\u09bf\u0996\u09c7 \u09aa\u09be\u09a0\u09be\u09a8 (\u0990\u099a\u09cd\u099b\u09bf\u0995), \u0985\u09a5\u09ac\u09be <b>Skip</b> \u09b2\u09bf\u0996\u09c1\u09a8\u0964",
            parse_mode="HTML"
        )

    # ======================== Language Toggle ========================
    elif data == "toggle_language":
        current = admin.get_user_language(user_id)
        new_lang = "en" if current == "bn" else "bn"
        admin.set_user_language(user_id, new_lang)
        msg = "\u2705 Language set to English! (\u09ac\u09c7\u09b6\u09bf\u09b0\u09ad\u09be\u0997 \u09ae\u09c7\u09b8\u09c7\u099c \u098f\u0996\u09a8\u09cb \u09ac\u09be\u0982\u09b2\u09be\u09df \u09a5\u09be\u0995\u09ac\u09c7 \u2014 \u09ab\u09c1\u09b2 \u0985\u09a8\u09c1\u09ac\u09be\u09a6 \u09b6\u09c0\u0998\u09cd\u09b0\u0987 \u0986\u09b8\u099b\u09c7)" if new_lang == "en" else "\u2705 \u09ad\u09be\u09b7\u09be \u09ac\u09be\u0982\u09b2\u09be \u0995\u09b0\u09be \u09b9\u09af\u09bc\u09c7\u099b\u09c7!"
        bot.answer_callback_query(call.id, msg, show_alert=True)


    # --- ৩-টিয়ার প্যাকেজ ও চেকআউট ফ্লো ---
    elif data == "open_upgrade_menu":
        p_text, p_markup = get_upgrade_plan_view(user_id)
        bot.edit_message_text(p_text, call.message.chat.id, call.message.message_id, reply_markup=p_markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("select_tier_"):
        tier = data.replace("select_tier_", "")
        sum_text, sum_markup = get_order_summary_view(tier, discount_percent=0)
        bot.edit_message_text(sum_text, call.message.chat.id, call.message.message_id, reply_markup=sum_markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("apply_coupon_"):
        tier = data.replace("apply_coupon_", "")
        user_states[user_id] = {"state": "awaiting_coupon_code", "tier_key": tier}
        bot.send_message(call.message.chat.id, "🎁 <b>আপনার ডিসকাউন্ট কুপন কোডটি লিখুন:</b> (যেমন: <code>PYHOST20</code>)")
        bot.answer_callback_query(call.id)

    elif data.startswith("paygw_"):
        parts = data.split("_")
        tier = parts[1]
        gateway = parts[2]
        price_val = parts[3]
        price_str = f"{price_val} BDT"

        user_states[user_id] = {
            "state": "awaiting_vip_trx",
            "plan_tier": tier,
            "gateway": gateway.upper(),
            "price": price_str
        }

        if gateway == "bkash":
            gw_info = (
                "📱 <b>bKash Personal:</b> <code>01700000000</code>\n"
                "👉 <b>ধরণ:</b> Send Money (ট্যাপ করলেই কপি হবে)\n"
                f"💵 <b>প্রদেয় পরিমাণ:</b> <code>{price_str}</code>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "⚠️ <i>টাকা পাঠানোর পর প্রাপ্ত Transaction ID (TrxID) টি নিচে লিখে পাঠান:</i>"
            )
        elif gateway == "nagad":
            gw_info = (
                "📱 <b>Nagad Personal:</b> <code>01700000000</code>\n"
                "👉 <b>ধরণ:</b> Send Money (ট্যাপ করলেই কপি হবে)\n"
                f"💵 <b>প্রদেয় পরিমাণ:</b> <code>{price_str}</code>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "⚠️ <i>টাকা পাঠানোর পর প্রাপ্ত Transaction ID (TrxID) টি নিচে লিখে পাঠান:</i>"
            )
        else:
            gw_info = (
                "🪙 <b>Binance Pay ID:</b> <code>123456789</code>\n"
                "🌐 <b>USDT (TRC20):</b> <code>TYDzsxd8Vp4...EXAMPLE...TRC20</code>\n"
                f"💵 <b>প্রদেয় পরিমাণ:</b> <code>{price_str} (~${int(price_val)/120:.2f})</code>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "⚠️ <i>পেমেন্ট সম্পন্ন করার পর প্রাপ্ত Order ID / TxHash টি নিচে লিখে পাঠান:</i>"
            )

        pay_msg = (
            f"🧾 <b>PYHOST PAYMENT GATEWAY • {gateway.upper()}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 <b>প্যাকেজ:</b> {admin.TIER_PLANS[tier]['name']}\n\n"
            f"{gw_info}"
        )
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(make_inline_button("❌ Cancel Order", callback_data="cancel_vip_order", style="danger"))
        bot.send_message(call.message.chat.id, pay_msg, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data == "cancel_vip_order":
        if user_id in user_states:
            del user_states[user_id]
        bot.edit_message_text("❌ <b>পেমেন্ট অর্ডারটি বাতিল করা হয়েছে।</b>", call.message.chat.id, call.message.message_id)
        bot.answer_callback_query(call.id, "Order cancelled")

    # --- অ্যাডমিন ১-ক্লিক ভিআইপি অ্যাপ্রুভাল ---
    elif data.startswith("adm_app_vip_") and admin.is_admin(user_id):
        req_id = int(data.replace("adm_app_vip_", ""))
        req = admin.get_vip_request(req_id)
        if req and req["status"] == "PENDING":
            target_uid = req["user_id"]
            days = req["days"]

            # ⭐ FIX: আগে শুধু plan_name স্ট্রিং-এ "Business" শব্দ খুঁজে assigned_plan ঠিক করা
            # হতো — এতে 2 মাস (BUSINESS) / 6 মাস (BUSINESS6M) / 12 মাস (YEARLY) তিনটা প্যাকেজই
            # একই "BUSINESS" ধরে নিয়ে একই (১০টি) বট-স্লট লিমিট পেত। এখন request-এ সেভ থাকা
            # আসল tier_key ব্যবহার করে সঠিক প্যাকেজ শনাক্ত করা হচ্ছে।
            tier_key = req.get("tier_key")
            plan_name_raw = req["plan_name"]

            if not tier_key:
                # পুরনো (মাইগ্রেশনের আগে তৈরি) রিকোয়েস্টের জন্য fallback — নাম মিলিয়ে অনুমান
                if "Pro" in plan_name_raw:
                    tier_key = "PRO"
                elif "Business Yearly" in plan_name_raw or "🏆" in plan_name_raw:
                    tier_key = "YEARLY"
                elif "6M" in plan_name_raw or "🔥" in plan_name_raw:
                    tier_key = "BUSINESS6M"
                elif "Business" in plan_name_raw:
                    tier_key = "BUSINESS"
                else:
                    tier_key = "PREMIUM"

            tier_key = tier_key.upper()
            if tier_key == "PRO":
                assigned_plan = "PRO"
            elif tier_key in ("BUSINESS", "BUSINESS6M", "YEARLY"):
                assigned_plan = "BUSINESS"
            else:
                assigned_plan = "PREMIUM"

            admin.set_user_plan(target_uid, assigned_plan, days, tier_key=tier_key)
            _award_first_upgrade_badge(target_uid)
            admin.update_vip_request_status(req_id, "APPROVED")
            
            bot.edit_message_text(
                f"{call.message.text}\n\n✅ <b>APPROVED by Admin @{call.from_user.username}</b>",
                call.message.chat.id,
                call.message.message_id
            )
            try:
                bot.send_message(
                    target_uid,
                    f"🎉 <b>অভিনন্দন! আপনার পেমেন্ট ভেরিফাই হয়েছে।</b>\n"
                    f"💎 <b>{req['plan_name']}</b> সফলভাবে সক্রিয় করা হয়েছে! (24/7 Unlimited Run)",
                    parse_mode="HTML"
                )
            except Exception:
                pass
        else:
            bot.answer_callback_query(call.id, "এই রিকোয়েস্টটি আগেই প্রসেস করা হয়েছে!", show_alert=True)

    elif data.startswith("adm_rej_vip_") and admin.is_admin(user_id):
        req_id = int(data.replace("adm_rej_vip_", ""))
        req = admin.get_vip_request(req_id)
        if req and req["status"] == "PENDING":
            admin.update_vip_request_status(req_id, "REJECTED")
            bot.edit_message_text(
                f"{call.message.text}\n\n❌ <b>REJECTED by Admin @{call.from_user.username}</b>",
                call.message.chat.id,
                call.message.message_id
            )
            try:
                bot.send_message(
                    req["user_id"],
                    "❌ <b>দুঃখিত! আপনার পেমেন্ট রিকোয়েস্টটি বাতিল করা হয়েছে।</b>\nসঠিক TrxID দিয়ে পুনরায় চেষ্টা করুন বা সাপোর্টে যোগাযোগ করুন।",
                    parse_mode="HTML"
                )
            except Exception:
                pass
        else:
            bot.answer_callback_query(call.id, "এই রিকোয়েস্টটি আগেই প্রসেস করা হয়েছে!", show_alert=True)

    elif data.startswith("adm_app_ext_") and admin.is_admin(user_id):
        req_id = int(data.replace("adm_app_ext_", ""))
        req = admin.get_renewal_request(req_id)
        if req and req["status"] == "PENDING":
            new_exp = admin.extend_project_expiry(req["project_id"], req["days"])
            admin.update_renewal_request_status(req_id, "APPROVED")
            bot.edit_message_text(
                f"{call.message.text}\n\n✅ <b>APPROVED by Admin @{call.from_user.username}</b>",
                call.message.chat.id,
                call.message.message_id
            )
            try:
                bot.send_message(
                    req["user_id"],
                    f"🎉 <b>মেয়াদ বৃদ্ধি সফল হয়েছে!</b>\n🤖 <b>বট:</b> {req['project_name']}\n🗓️ <b>নতুন মেয়াদ:</b> <code>{new_exp}</code>",
                    parse_mode="HTML"
                )
            except Exception:
                pass
        else:
            bot.answer_callback_query(call.id, "এই রিকোয়েস্টটি আগেই প্রসেস করা হয়েছে!", show_alert=True)

    elif data.startswith("adm_rej_ext_") and admin.is_admin(user_id):
        req_id = int(data.replace("adm_rej_ext_", ""))
        req = admin.get_renewal_request(req_id)
        if req and req["status"] == "PENDING":
            admin.update_renewal_request_status(req_id, "REJECTED")
            bot.edit_message_text(
                f"{call.message.text}\n\n❌ <b>REJECTED by Admin @{call.from_user.username}</b>",
                call.message.chat.id,
                call.message.message_id
            )
            try:
                bot.send_message(
                    req["user_id"],
                    "❌ <b>দুঃখিত! আপনার মেয়াদ বৃদ্ধি রিকোয়েস্টটি বাতিল করা হয়েছে।</b>\nসঠিক TrxID দিয়ে আবার চেষ্টা করুন বা সাপোর্টে যোগাযোগ করুন।",
                    parse_mode="HTML"
                )
            except Exception:
                pass
        else:
            bot.answer_callback_query(call.id, "এই রিকোয়েস্টটি আগেই প্রসেস করা হয়েছে!", show_alert=True)

    # --- অ্যাডমিন টেলিমেট্রি ও কন্ট্রোল ---
    elif data == "adm_back_menu" and admin.is_admin(user_id):
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        bot.send_message(call.message.chat.id, "👑 <b>Admin Panel</b>\n👇 অ্যাডমিন মেনু:", reply_markup=get_admin_reply_keyboard(), parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data == "adm_refresh_telem" and admin.is_admin(user_id):
        stats = admin.get_comprehensive_server_stats(active_processes)
        markup = types.InlineKeyboardMarkup()
        markup.add(make_inline_button("🔄 Refresh Telemetry", callback_data="adm_refresh_telem", style="primary"))
        markup.add(make_inline_button("🔙 Admin Menu", callback_data="adm_back_menu", style="primary"))
        try:
            bot.edit_message_text(stats, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
            bot.answer_callback_query(call.id, "Telemetry refreshed!")
        except Exception:
            bot.answer_callback_query(call.id, "Already refreshed!")

    elif data.startswith("adm_grant_pro_") and admin.is_admin(user_id):
        target_uid = int(data.replace("adm_grant_pro_", ""))
        admin.set_user_plan(target_uid, "PRO", 7)
        _award_first_upgrade_badge(target_uid)
        bot.answer_callback_query(call.id, f"Pro Plan (7d) granted to {target_uid}!", show_alert=True)
        try:
            bot.send_message(target_uid, "🎉 <b>অভিনন্দন! অ্যাডমিন আপনার একাউন্ট Pro Plan (7 Days) প্ল্যানে আপগ্রেড করেছে।</b>", parse_mode="HTML")
        except Exception:
            pass

    elif data.startswith("adm_grant_vip_") and admin.is_admin(user_id):
        target_uid = int(data.replace("adm_grant_vip_", ""))
        admin.set_user_plan(target_uid, "PREMIUM", 30)
        _award_first_upgrade_badge(target_uid)
        bot.answer_callback_query(call.id, f"Premium Plan (30d) granted to {target_uid}!", show_alert=True)
        try:
            bot.send_message(target_uid, "🎉 <b>অভিনন্দন! অ্যাডমিন আপনার একাউন্ট Premium Plan (30 Days) প্ল্যানে আপগ্রেড করেছে।</b>", parse_mode="HTML")
        except Exception:
            pass

    elif data.startswith("adm_ban_confirm_") and admin.is_admin(user_id):
        target_uid = int(data.replace("adm_ban_confirm_", ""))
        u_info = admin.get_user(target_uid)
        will_ban = not bool(u_info.get("is_banned", 0))
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            make_inline_button("✅ Confirm", callback_data=f"adm_toggle_ban_{target_uid}", style="danger"),
            make_inline_button("❌ Cancel", callback_data=f"adm_view_user_{target_uid}", style="primary")
        )
        action_label = "🚫 BAN" if will_ban else "✅ UNBAN"
        bot.send_message(
            call.message.chat.id,
            f"⚠️ <b>নিশ্চিত করুন!</b>\nইউজার <code>{target_uid}</code>-কে <b>{action_label}</b> করা হবে। চালিয়ে যেতে চান?",
            reply_markup=markup, parse_mode="HTML"
        )
        bot.answer_callback_query(call.id)

    elif data.startswith("adm_toggle_ban_") and admin.is_admin(user_id):
        target_uid = int(data.replace("adm_toggle_ban_", ""))
        u_info = admin.get_user(target_uid)
        new_status = not bool(u_info.get("is_banned", 0))
        admin.set_user_ban(target_uid, new_status)
        bot.answer_callback_query(call.id, f"User status updated: {'BANNED' if new_status else 'UNBANNED'}!", show_alert=True)
        card, markup = build_admin_user_profile_card(target_uid)
        try:
            bot.edit_message_text(card, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, card, reply_markup=markup, parse_mode="HTML")

    elif data.startswith("adm_dm_") and admin.is_admin(user_id):
        target_uid = int(data.replace("adm_dm_", ""))
        user_states[user_id] = {"state": "awaiting_admin_dm", "target_uid": target_uid}
        bot.send_message(call.message.chat.id, f"📩 <b>ইউজার <code>{target_uid}</code>-কে মেসেজ লিখুন:</b>")
        bot.answer_callback_query(call.id)

    # --- অ্যাডমিন: পেজিনেটেড রিকোয়েস্ট ও ইউজার ডাইরেক্টরি নেভিগেশন ---
    elif data.startswith("adm_reqs_page_") and admin.is_admin(user_id):
        page = int(data.replace("adm_reqs_page_", ""))
        req_text, req_markup = build_pending_requests_view(page=page)
        try:
            bot.edit_message_text(req_text, call.message.chat.id, call.message.message_id, reply_markup=req_markup, parse_mode="HTML")
        except Exception:
            pass
        bot.answer_callback_query(call.id)

    # ⭐ NEW: সাপোর্ট টিকিট সিস্টেম — ইউজার সাইড কলব্যাকসমূহ
    elif data.startswith("my_tickets_page_"):
        page = int(data.replace("my_tickets_page_", ""))
        t_text, t_markup = build_user_tickets_view(user_id, page=page)
        try:
            bot.edit_message_text(t_text, call.message.chat.id, call.message.message_id, reply_markup=t_markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, t_text, reply_markup=t_markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data == "new_ticket":
        user_states[user_id] = "awaiting_ticket_message"
        bot.send_message(
            call.message.chat.id,
            "📝 <b>নতুন টিকিট:</b>\nআপনার সমস্যাটি বিস্তারিত লিখে একটি মেসেজে পাঠান — আমাদের টিম শীঘ্রই রিপ্লাই দেবে।",
            parse_mode="HTML"
        )
        bot.answer_callback_query(call.id)

    elif data.startswith("ticket_view_"):
        tid = int(data.replace("ticket_view_", ""))
        ticket = admin.get_ticket(tid)
        if not ticket or ticket["user_id"] != user_id:
            bot.answer_callback_query(call.id, "এই টিকিটটি আপনার না!", show_alert=True)
            return
        t_text, t_markup = build_ticket_thread_view(tid, is_admin=False)
        try:
            bot.edit_message_text(t_text, call.message.chat.id, call.message.message_id, reply_markup=t_markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, t_text, reply_markup=t_markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("ticket_reply_"):
        tid = int(data.replace("ticket_reply_", ""))
        ticket = admin.get_ticket(tid)
        if not ticket or ticket["user_id"] != user_id:
            bot.answer_callback_query(call.id, "এই টিকিটটি আপনার না!", show_alert=True)
            return
        user_states[user_id] = {"state": "awaiting_ticket_reply", "ticket_id": tid}
        bot.send_message(call.message.chat.id, f"💬 <b>টিকিট #{tid}-এ আপনার রিপ্লাই লিখুন:</b>", parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("ticket_close_"):
        tid = int(data.replace("ticket_close_", ""))
        ticket = admin.get_ticket(tid)
        if not ticket or ticket["user_id"] != user_id:
            bot.answer_callback_query(call.id, "এই টিকিটটি আপনার না!", show_alert=True)
            return
        admin.update_ticket_status(tid, "CLOSED")
        t_text, t_markup = build_ticket_thread_view(tid, is_admin=False)
        try:
            bot.edit_message_text(t_text, call.message.chat.id, call.message.message_id, reply_markup=t_markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, t_text, reply_markup=t_markup, parse_mode="HTML")
        bot.answer_callback_query(call.id, "🔒 টিকিট বন্ধ করা হয়েছে।")

    # ⭐ NEW: সাপোর্ট টিকিট সিস্টেম — অ্যাডমিন সাইড কলব্যাকসমূহ
    elif data.startswith("adm_tickets_page_") and admin.is_admin(user_id):
        page = int(data.replace("adm_tickets_page_", ""))
        t_text, t_markup = build_admin_tickets_view(page=page)
        try:
            bot.edit_message_text(t_text, call.message.chat.id, call.message.message_id, reply_markup=t_markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, t_text, reply_markup=t_markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("adm_ticket_view_") and admin.is_admin(user_id):
        tid = int(data.replace("adm_ticket_view_", ""))
        t_text, t_markup = build_ticket_thread_view(tid, is_admin=True)
        try:
            bot.edit_message_text(t_text, call.message.chat.id, call.message.message_id, reply_markup=t_markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, t_text, reply_markup=t_markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("adm_ticket_reply_") and admin.is_admin(user_id):
        tid = int(data.replace("adm_ticket_reply_", ""))
        user_states[user_id] = {"state": "awaiting_admin_ticket_reply", "ticket_id": tid}
        bot.send_message(call.message.chat.id, f"💬 <b>টিকিট #{tid}-এ রিপ্লাই লিখুন:</b>", parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("adm_ticket_close_") and admin.is_admin(user_id):
        tid = int(data.replace("adm_ticket_close_", ""))
        ticket = admin.get_ticket(tid)
        admin.update_ticket_status(tid, "CLOSED")
        t_text, t_markup = build_ticket_thread_view(tid, is_admin=True)
        try:
            bot.edit_message_text(t_text, call.message.chat.id, call.message.message_id, reply_markup=t_markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, t_text, reply_markup=t_markup, parse_mode="HTML")
        if ticket:
            try:
                bot.send_message(ticket["user_id"], f"🔒 <b>আপনার টিকিট #{tid} বন্ধ করা হয়েছে।</b>", parse_mode="HTML")
            except Exception:
                pass
        bot.answer_callback_query(call.id, "🔒 টিকিট বন্ধ করা হয়েছে।")

    elif data.startswith("adm_users_page_") and admin.is_admin(user_id):
        rest = data.replace("adm_users_page_", "")
        page_str, _, query = rest.partition("_")
        page = int(page_str)
        query = "" if query == "-" else query
        dir_text, dir_markup = build_user_directory_view(page=page, query=query)
        try:
            bot.edit_message_text(dir_text, call.message.chat.id, call.message.message_id, reply_markup=dir_markup, parse_mode="HTML")
        except Exception:
            pass
        bot.answer_callback_query(call.id)

    elif data.startswith("adm_view_user_") and admin.is_admin(user_id):
        target_uid = int(data.replace("adm_view_user_", ""))
        card, markup = build_admin_user_profile_card(target_uid)
        try:
            bot.edit_message_text(card, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, card, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("adm_plan_menu_") and admin.is_admin(user_id):
        target_uid = int(data.replace("adm_plan_menu_", ""))
        text2 = f"🗂 <b>Manage Plan — User <code>{target_uid}</code></b>\n━━━━━━━━━━━━━━━━━━━━━━\n👇 নতুন প্ল্যান নির্বাচন করুন:"
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(make_inline_button("🆓 Set FREE", callback_data=f"adm_setplan_{target_uid}_FREE_0", style="danger"))
        markup.add(make_inline_button("⚡ Pro — 7 Days", callback_data=f"adm_setplan_{target_uid}_PRO_7", style="primary"))
        markup.add(make_inline_button("⚡ Pro — 30 Days", callback_data=f"adm_setplan_{target_uid}_PRO_30", style="primary"))
        markup.add(make_inline_button("🌟 Premium — 30 Days", callback_data=f"adm_setplan_{target_uid}_PREMIUM_30", style="success"))
        markup.add(make_inline_button("🌟 Premium — 90 Days", callback_data=f"adm_setplan_{target_uid}_PREMIUM_90", style="success"))
        markup.add(make_inline_button("👑 Business — 30 Days", callback_data=f"adm_setplan_{target_uid}_BUSINESS_30", style="success"))
        markup.add(make_inline_button("👑 Business — 365 Days", callback_data=f"adm_setplan_{target_uid}_BUSINESS_365", style="success"))
        markup.add(make_inline_button("🔙 Back to Profile", callback_data=f"adm_view_user_{target_uid}", style="primary"))
        bot.edit_message_text(text2, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("adm_setplan_") and admin.is_admin(user_id):
        parts = data.replace("adm_setplan_", "").split("_")
        target_uid = int(parts[0])
        plan_name = parts[1]
        days = int(parts[2])
        admin.set_user_plan(target_uid, plan_name, days)
        _award_first_upgrade_badge(target_uid)
        bot.answer_callback_query(call.id, f"✅ Plan updated to {plan_name} for {days} days!", show_alert=True)
        try:
            bot.send_message(target_uid, f"🎉 <b>অভিনন্দন! অ্যাডমিন আপনার একাউন্ট {plan_name} প্ল্যানে সেট করেছে।</b>", parse_mode="HTML")
        except Exception:
            pass
        card, markup = build_admin_user_profile_card(target_uid)
        try:
            bot.edit_message_text(card, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, card, reply_markup=markup, parse_mode="HTML")

    elif data.startswith("adm_userbots_") and admin.is_admin(user_id):
        target_uid = int(data.replace("adm_userbots_", ""))
        projects = admin.get_user_projects(target_uid)
        if not projects:
            bot.answer_callback_query(call.id, "এই ইউজারের কোনো বট নেই!", show_alert=True)
            return
        markup = types.InlineKeyboardMarkup(row_width=1)
        for p in projects:
            pid = p["project_id"]
            p_key = f"{target_uid}_{pid}"
            is_run = p_key in active_processes and active_processes[p_key]["process"].poll() is None
            icon = "🟢" if is_run else "🔴"
            slot_lbl = p.get("slot_number") or "-"
            markup.add(make_inline_button(f"{icon} Slot {slot_lbl} — {p['project_name']}", callback_data=f"adm_bot_view_{target_uid}_{pid}", style="primary"))
        markup.add(make_inline_button("🔙 Back to Profile", callback_data=f"adm_view_user_{target_uid}", style="primary"))
        bot.edit_message_text(f"📦 <b>User <code>{target_uid}</code> এর সব বট:</b>", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("adm_bot_view_") and admin.is_admin(user_id):
        parts = data.replace("adm_bot_view_", "").split("_")
        target_uid = int(parts[0])
        pid = int(parts[1])
        project = admin.get_project_by_id(pid)
        if not project:
            bot.answer_callback_query(call.id, "বটটি পাওয়া যায়নি!", show_alert=True)
            return
        owner_data = admin.get_user(target_uid)
        slot_status = admin.get_effective_slot_status(owner_data, project)
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(make_inline_button("🗑️ Force Delete Bot", callback_data=f"adm_bot_del_confirm_{target_uid}_{pid}", style="danger"))
        markup.add(make_inline_button("🔙 Back to Bots List", callback_data=f"adm_userbots_{target_uid}", style="primary"))
        info = (
            f"🤖 <b>{project['project_name']}</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔢 Slot: <code>{project.get('slot_number') or '-'}</code>\n"
            f"📄 Main File: <code>{project['main_file']}</code>\n"
            f"📅 Created: <code>{project['created_at']}</code>\n"
            f"⏳ Status: <code>{slot_status['label']}</code>"
        )
        bot.edit_message_text(info, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("adm_bot_del_confirm_") and admin.is_admin(user_id):
        parts = data.replace("adm_bot_del_confirm_", "").split("_")
        target_uid = int(parts[0])
        pid = int(parts[1])
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            make_inline_button("✅ Yes, Delete", callback_data=f"adm_bot_del_{target_uid}_{pid}", style="danger"),
            make_inline_button("❌ Cancel", callback_data=f"adm_bot_view_{target_uid}_{pid}", style="primary")
        )
        bot.edit_message_text("⚠️ <b>নিশ্চিত করুন! এই বটটি স্থায়ীভাবে ডিলিট করা হবে।</b>", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("adm_bot_del_") and admin.is_admin(user_id):
        parts = data.replace("adm_bot_del_", "").split("_")
        target_uid = int(parts[0])
        pid = int(parts[1])
        stop_project_script(target_uid, pid)
        admin.delete_user_project(pid)
        bot.answer_callback_query(call.id, "বটটি ফোর্স ডিলিট করা হয়েছে!", show_alert=True)
        try:
            bot.send_message(target_uid, "⚠️ <b>অ্যাডমিন আপনার একটি বট ডিলিট করেছে।</b>", parse_mode="HTML")
        except Exception:
            pass
        card, markup = build_admin_user_profile_card(target_uid)
        try:
            bot.edit_message_text(card, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, card, reply_markup=markup, parse_mode="HTML")

    elif data.startswith("adm_auditlog_") and admin.is_admin(user_id):
        target_uid = int(data.replace("adm_auditlog_", ""))
        logs = admin.get_user_audit_log(target_uid, limit=15)
        if not logs:
            bot.answer_callback_query(call.id, "কোনো অ্যাক্টিভিটি লগ পাওয়া যায়নি!", show_alert=True)
            return
        lines = [f"• <code>{l.get('timestamp', '')}</code> — <b>{l.get('action', '')}</b>\n  └ {l.get('details', '')}" for l in logs]
        text3 = f"📜 <b>Audit Log — User <code>{target_uid}</code></b>\n━━━━━━━━━━━━━━━━━━━━━━\n" + "\n".join(lines)
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(make_inline_button("🔙 Back to Profile", callback_data=f"adm_view_user_{target_uid}", style="primary"))
        bot.edit_message_text(text3[:4000], call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    # --- অ্যাডমিন: ঝুঁকিপূর্ণ অ্যাকশনের কনফার্মেশন ডায়ালগ ---
    elif data == "adm_coupon_create" and admin.is_admin(user_id):
        user_states[user_id] = {"state": "awaiting_new_coupon"}
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "🎟️ <b>নতুন কুপন তৈরি করুন</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "এই ফরম্যাটে পাঠান:\n"
            "<code>CODE DISCOUNT MAX_USES</code>\n\n"
            "উদাহরণ: <code>EID50 50 100</code>\n"
            "→ 'EID50' কোডে ৫০% ছাড়, সর্বোচ্চ ১০০ বার ব্যবহার\n\n"
            "⚠️ MAX_USES = 0 দিলে আনলিমিটেড।",
            parse_mode="HTML"
        )

    elif data == "adm_coupon_deactivate" and admin.is_admin(user_id):
        user_states[user_id] = {"state": "awaiting_deactivate_coupon"}
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "❌ <b>কুপন বাতিল করুন</b>\n"
            "বাতিল করতে চান এমন কুপনের CODE পাঠান।\n"
            "উদাহরণ: <code>EID50</code>",
            parse_mode="HTML"
        )

    elif data == "adm_refresh_analytics" and admin.is_admin(user_id):
        # Analytics refresh — same data, re-render
        all_users = admin.get_all_users()
        total = len(all_users)
        free_c = sum(1 for u in all_users if u.get("plan", "FREE") == "FREE")
        pro_c = sum(1 for u in all_users if u.get("plan") == "PRO")
        prem_c = sum(1 for u in all_users if u.get("plan") == "PREMIUM")
        biz_c = sum(1 for u in all_users if u.get("plan") in ("BUSINESS", "VIP"))
        paid_total = pro_c + prem_c + biz_c
        banned_c = sum(1 for u in all_users if u.get("is_banned") == 1)
        today_str = datetime.now().strftime("%Y-%m-%d")
        today_new = sum(1 for u in all_users if str(u.get("created_at", "")).startswith(today_str))
        active_bot_count = len([k for k, v in active_processes.items() if v["process"].poll() is None])
        conn = admin.get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM projects"); total_bots = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM vip_requests WHERE status = 'APPROVED'"); approved_vip = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM renewal_requests WHERE status = 'APPROVED'"); approved_ext = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM coupon_uses"); total_coupon_uses = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM coupons WHERE is_active = 1"); active_coupons = cur.fetchone()[0]
        conn.close()
        conv_rate = f"{(paid_total/total*100):.1f}%" if total > 0 else "0%"
        dashboard = (
            "📈 <b>PyHost Analytics Dashboard</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "👥 <b>ইউজার সামারি:</b>\n"
            f"├ 📊 মোট রেজিস্টার্ড: <b>{total}</b> জন\n"
            f"├ 🆕 আজকের নতুন: <b>{today_new}</b> জন\n"
            f"├ 💰 পেইড সদস্য: <b>{paid_total}</b> জন\n"
            f"├ 🆓 ফ্রি ইউজার: <b>{free_c}</b> জন\n"
            f"└ 🚫 ব্যান্ড: <b>{banned_c}</b> জন\n\n"
            "📦 <b>প্ল্যান বিভাজন:</b>\n"
            f"├ ⚡ Pro: <b>{pro_c}</b> জন\n"
            f"├ 🌟 Premium: <b>{prem_c}</b> জন\n"
            f"├ 👑 Business/VIP: <b>{biz_c}</b> জন\n"
            f"└ 📈 Conversion Rate: <b>{conv_rate}</b>\n\n"
            "🤖 <b>বট স্ট্যাটাস:</b>\n"
            f"├ 🟢 এখন চলছে: <b>{active_bot_count}</b> টি\n"
            f"└ 📦 মোট তৈরি: <b>{total_bots}</b> টি\n\n"
            "💰 <b>পেমেন্ট সামারি:</b>\n"
            f"├ ✅ অ্যাপ্রুভড প্ল্যান: <b>{approved_vip}</b>\n"
            f"└ ✅ অ্যাপ্রুভড রিনিউয়াল: <b>{approved_ext}</b>\n\n"
            "🎟️ <b>কুপন সামারি:</b>\n"
            f"├ 🟢 একটিভ কুপন: <b>{active_coupons}</b> টি\n"
            f"└ 📊 মোট ব্যবহার: <b>{total_coupon_uses}</b> বার\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🕒 <i>Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</i>"
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(make_inline_button("🔄 Refresh", callback_data="adm_refresh_analytics", style="primary"))
        markup.add(make_inline_button("🔙 Admin Menu", callback_data="adm_back_menu", style="primary"))
        try:
            bot.edit_message_text(dashboard, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        except Exception:
            pass
        bot.answer_callback_query(call.id, "✅ Refreshed!")

    elif data == "adm_cancel_action" and admin.is_admin(user_id):
        try:
            bot.edit_message_text("❌ <b>অ্যাকশনটি বাতিল করা হয়েছে।</b>", call.message.chat.id, call.message.message_id, parse_mode="HTML")
        except Exception:
            pass
        bot.answer_callback_query(call.id, "Cancelled")

    elif data == "adm_confirm_restart_all" and admin.is_admin(user_id):
        count = restart_all_active_bots()
        try:
            bot.edit_message_text(f"🔄 Successfully reloaded and restarted <b>{count}</b> active bot instances!", call.message.chat.id, call.message.message_id, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, f"🔄 Successfully reloaded and restarted <b>{count}</b> active bot instances!", parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data == "adm_confirm_cleanup" and admin.is_admin(user_id):
        kb = admin.execute_server_cleanup()
        try:
            bot.edit_message_text(f"🧹 Cleanup finished! <code>{kb} KB</code> of logs & cache purged.", call.message.chat.id, call.message.message_id, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, f"🧹 Cleanup finished! <code>{kb} KB</code> of logs & cache purged.", parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("doc_page_"):
        idx = int(data.replace("doc_page_", ""))
        d_text, d_markup = build_doc_page(idx)
        try:
            bot.edit_message_text(d_text, call.message.chat.id, call.message.message_id, reply_markup=d_markup, parse_mode="HTML")
        except Exception:
            pass
        bot.answer_callback_query(call.id)

    elif data == "doc_noop":
        bot.answer_callback_query(call.id)

    elif data == "adm_confirm_maintenance" and admin.is_admin(user_id):
        admin.SYSTEM_CONFIG["maintenance_mode"] = not admin.SYSTEM_CONFIG["maintenance_mode"]
        status = "🔴 ACTIVE (ON)" if admin.SYSTEM_CONFIG["maintenance_mode"] else "🟢 INACTIVE (OFF)"
        try:
            bot.edit_message_text(f"⚙️ Maintenance Mode is now: <b>{status}</b>.", call.message.chat.id, call.message.message_id, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, f"⚙️ Maintenance Mode is now: <b>{status}</b>.", parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data == "adm_confirm_free_hosting" and admin.is_admin(user_id):
        admin.set_system_setting("free_hosting_enabled", not admin.SYSTEM_CONFIG.get("free_hosting_enabled", True))
        status = "🟢 ON" if admin.SYSTEM_CONFIG["free_hosting_enabled"] else "🔴 OFF"
        result_text = f"🆓 ফ্রি হোস্টিং এখন: <b>{status}</b>।"
        if not admin.SYSTEM_CONFIG["free_hosting_enabled"]:
            stopped_n = lock_all_free_bots()
            result_text += f"\n🔒 <b>{stopped_n}</b>টি চলতে থাকা ফ্রি বট বন্ধ করে সব ফ্রি স্লট লক করা হয়েছে।"
        try:
            bot.edit_message_text(result_text, call.message.chat.id, call.message.message_id, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, result_text, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("adm_set_free_hours_") and admin.is_admin(user_id):
        hours = int(data.replace("adm_set_free_hours_", ""))
        admin.set_system_setting("free_hosting_hours", hours)
        result_text = f"🆓 ফ্রি হোস্টিং সেশনের মেয়াদ এখন: <b>{hours} Hours</b> per session। Control Panel-এও এখন থেকে এটাই দেখাবে।"
        try:
            bot.edit_message_text(result_text, call.message.chat.id, call.message.message_id, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, result_text, parse_mode="HTML")
        bot.answer_callback_query(call.id, f"সেট হয়েছে: {hours} Hours")

    # --- মার্কেটপ্লেস হ্যান্ডলারস ---
    elif data.startswith("mp_view_") and data[len("mp_view_"):] in MARKETPLACE_TEMPLATES:
        msg, markup = build_marketplace_card(data[len("mp_view_"):])
        bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data == "mp_view_github":
        u_data = admin.get_user(user_id)
        if not admin.is_vip_active(u_data):
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(
                make_inline_button("💎 Upgrade Plan", callback_data="open_upgrade_menu", style="success"),
                make_inline_button("◀️ Back to Marketplace", callback_data="open_mp_menu", style="primary")
            )
            bot.edit_message_text(
                "🔒 <b>GitHub Deploy — VIP Exclusive Feature</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "এই ফিচারটি শুধুমাত্র পেইড প্ল্যান (Pro / Premium / Business / Business Yearly) ইউজারদের জন্য উন্মুক্ত।\n\n"
                "👉 GitHub থেকে সরাসরি রিপো ডিপ্লয় করতে প্ল্যান আপগ্রেড করুন।",
                call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML"
            )
            bot.answer_callback_query(call.id)
            return
        max_bots = admin.get_max_bot_limit_for_user(u_data)
        if len(admin.get_user_projects(user_id)) >= max_bots:
            bot.answer_callback_query(call.id, "⚠️ স্লট লিমিট পূর্ণ! একটি বট ডিলিট করুন বা প্ল্যান আপগ্রেড করুন।", show_alert=True)
            return
        if admin.get_next_free_slot(user_id) is None:
            bot.answer_callback_query(call.id, "⚠️ সবগুলো স্লট পূর্ণ! নতুন ডিপ্লয়ের আগে একটি ডিলিট করুন।", show_alert=True)
            return
        user_states[user_id] = {"state": "awaiting_github_url"}
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(make_inline_button("◀️ Back to Marketplace", callback_data="open_mp_menu", style="primary"))
        bot.edit_message_text(
            "🐙 <b>Deploy from GitHub</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "আপনার GitHub রিপোর লিংক পাঠান।\n"
            "যেমন: <code>https://github.com/owner/repo</code>\n\n"
            "✅ requirements.txt থাকলে অটো-ইনস্টল হবে\n"
            "✅ main.py / bot.py অটো-ডিটেক্ট হয়ে রান হবে",
            call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML"
        )
        bot.answer_callback_query(call.id)

    elif data.startswith("gh_sync_"):
        pid = int(data.replace("gh_sync_", ""))
        project = admin.get_project_by_id(pid)
        if not project or project["user_id"] != user_id:
            bot.answer_callback_query(call.id, "এই বটটি আপনার না!", show_alert=True)
            return
        gh_info = admin.get_project_github_info(pid)
        if not gh_info:
            bot.answer_callback_query(call.id, "এই বটের কোনো GitHub তথ্য নেই!", show_alert=True)
            return
        bot.answer_callback_query(call.id)

        # Private repo হলে নতুন token চাওয়া হবে, Public হলে সরাসরি sync
        if gh_info["is_private"]:
            user_states[user_id] = {"state": "awaiting_sync_token", "pid": pid}
            bot.edit_message_text(
                f"🔒 <b>Private Repo Sync — Token দরকার</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📦 <b>Repo:</b> <code>{html.escape(gh_info['owner'])}/{html.escape(gh_info['repo'])}</code>\n\n"
                "আপনার GitHub Personal Access Token পাঠান।\n"
                "⚠️ <i>Token কোথাও সেভ হবে না — Sync শেষে ডিসকার্ড হবে। পাঠানো মেসেজও ডিলিট হবে।</i>",
                call.message.chat.id, call.message.message_id, parse_mode="HTML"
            )
        else:
            status_msg = bot.edit_message_text(
                f"🔄 <b>GitHub Sync শুরু হচ্ছে...</b>\n"
                f"📦 <code>{html.escape(gh_info['owner'])}/{html.escape(gh_info['repo'])}</code>",
                call.message.chat.id, call.message.message_id, parse_mode="HTML"
            )
            threading.Thread(
                target=sync_github_repo,
                args=(user_id, pid, call.message.chat.id, status_msg.message_id, None),
                daemon=True
            ).start()

    # ======================== Tutorial Callbacks ========================
    elif data in ("tut_step_2", "tut_step_3", "tut_step_4"):
        content, next_key = get_onboarding_step_content(data)
        markup = types.InlineKeyboardMarkup()
        if next_key and next_key != "tut_done":
            markup.add(make_inline_button("▶️ পরের ধাপ →", callback_data=next_key, style="success"))
        elif next_key == "tut_done":
            markup.add(make_inline_button("✅ শুরু করি!", callback_data="tut_done", style="success"))
        _prev = {"tut_step_3": "tut_step_2", "tut_step_4": "tut_step_3"}.get(data)
        if _prev:
            markup.add(make_inline_button("🔙 Back", callback_data=_prev, style="primary"))
        markup.add(make_inline_button("⏭️ Skip", callback_data="tut_skip", style="primary"))
        try:
            bot.edit_message_text(content, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, content, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data == "tut_done":
        admin.mark_onboarding_done(user_id)
        admin.set_checklist_flag(user_id, "tutorial_done")
        # ⭐ Tutorial শেষ হলে মেসেজটা চ্যাটে পড়ে না থেকে অটো-ভ্যানিশ (ডিলিট) হয়ে যাবে —
        # নিচের answer_callback_query টোস্টেই কনফার্মেশন দেখানো হচ্ছে, চ্যাটে বাড়তি লাইন থাকবে না।
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            try:
                bot.edit_message_text(
                    "✅ <b>Tutorial সম্পন্ন!</b>\n🎉 এখন Control Panel থেকে প্রথম বট তৈরি করুন!",
                    call.message.chat.id, call.message.message_id, parse_mode="HTML"
                )
            except Exception:
                pass
        bot.answer_callback_query(call.id, "টিউটোরিয়াল শেষ! শুরু করুন 🚀")

    elif data == "tut_skip":
        admin.mark_onboarding_done(user_id)
        # ⭐ Skip করলেও মেসেজটা অটো-ভ্যানিশ (ডিলিট) হয়ে যাবে, চ্যাটে পড়ে থাকবে না
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            try:
                bot.edit_message_text("⏭️ Tutorial skip করা হয়েছে। যেকোনো সময় Help থেকে দেখতে পারবেন।", call.message.chat.id, call.message.message_id, parse_mode="HTML")
            except Exception:
                pass
        bot.answer_callback_query(call.id, "Tutorial skip করা হয়েছে। Help থেকে যেকোনো সময় দেখতে পারবেন।")

    # ======================== Onboarding Checklist ========================
    elif data == "show_checklist":
        flags = admin.get_checklist_flags(user_id)
        projects = admin.get_user_projects(user_id)
        u_data = admin.get_user(user_id)
        pinfo = admin.get_plan_display_info(u_data)
        has_bot = len(projects) > 0
        has_run = any(f"{user_id}_{p['project_id']}" in active_processes for p in projects)
        has_upgrade = pinfo["is_active"]
        has_refer = (u_data.get("referral_count") or 0) > 0
        def chk(done): return "✅" if done else "⬜"
        checklist = (
            "📋 <b>Onboarding Checklist</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{chk(flags.get('tutorial_done'))} Tutorial দেখুন\n"
            f"{chk(has_bot)} প্রথম বট তৈরি করুন\n"
            f"{chk(has_run)} বট রান করুন\n"
            f"{chk(has_upgrade)} প্ল্যান আপগ্রেড করুন (24/7)\n"
            f"{chk(has_refer)} ১ জন বন্ধুকে Refer করুন\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
        )
        done_count = sum([bool(flags.get("tutorial_done")), has_bot, has_run, has_upgrade, has_refer])
        checklist += f"🏆 সম্পন্ন: <b>{done_count}/5</b>"
        markup = types.InlineKeyboardMarkup()
        markup.add(make_inline_button("🔄 Refresh", callback_data="show_checklist", style="primary"))
        markup.add(make_inline_button("🔙 Back to Home", callback_data="go_home", style="primary"))
        try:
            bot.edit_message_text(checklist, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, checklist, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    # ======================== Bot Health Check ========================
    elif data.startswith("bot_health_"):
        pid = int(data.replace("bot_health_", ""))
        project = admin.get_project_by_id(pid)
        if not project or project["user_id"] != user_id:
            bot.answer_callback_query(call.id, "এই বটটি আপনার না!", show_alert=True)
            return
        p_key = f"{user_id}_{pid}"
        is_running = p_key in active_processes and active_processes[p_key]["process"].poll() is None

        # লগ থেকে এরর ডিটেক্ট
        errors = admin.scan_logs_for_errors(user_id, pid, PROJECTS_DIR)

        if is_running:
            proc_data = active_processes.get(p_key, {})
            proc = proc_data.get("process")
            try:
                mem = psutil.Process(proc.pid).memory_info().rss // (1024*1024)
            except Exception:
                mem = 0
            status_icon = "🟢"
            status_text = f"চলছে ✅ | RAM: {mem}MB"
        else:
            status_icon = "🔴"
            status_text = "বন্ধ"

        health_text = (
            f"🏥 <b>Bot Health Check — {html.escape(project['project_name'])}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{status_icon} <b>স্ট্যাটাস:</b> {status_text}\n"
            f"📄 <b>Main File:</b> <code>{project['main_file']}</code>\n"
            f"🔢 <b>Slot:</b> {project.get('slot_number','-')}\n"
            f"📅 <b>Created:</b> {project.get('created_at','-')[:10]}\n"
        )
        if errors:
            health_text += "\n⚠️ <b>পাওয়া সমস্যা:</b>\n" + "\n".join(f"• {e}" for e in errors)
        else:
            health_text += "\n✅ <b>কোনো সমস্যা পাওয়া যায়নি!</b>"
        gh_info = admin.get_project_github_info(pid)
        if gh_info:
            health_text += f"\n\n🐙 <b>GitHub:</b> <code>{html.escape(gh_info['owner'])}/{html.escape(gh_info['repo'])}</code>"
        notes = admin.get_bot_notes(pid)
        if notes:
            health_text += f"\n\n📝 <b>নোট:</b> {html.escape(notes)}"

        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            make_inline_button("🔄 Refresh", callback_data=f"bot_health_{pid}", style="primary"),
            make_inline_button("📝 নোট এডিট", callback_data=f"bot_edit_notes_{pid}", style="primary")
        )
        markup.add(make_inline_button("🔙 Dashboard", callback_data=f"manage_bot_{pid}", style="primary"))
        try:
            bot.edit_message_text(health_text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, health_text, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("bot_edit_notes_"):
        pid = int(data.replace("bot_edit_notes_", ""))
        user_states[user_id] = {"state": "awaiting_bot_notes", "pid": pid}
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "📝 <b>বটের নোট লিখুন</b> (সর্বোচ্চ ৩০০ অক্ষর)\n"
            "যেমন: 'এটা আমার শপিং বট', 'admin: @username'\n"
            "বাতিল করতে /start দিন।",
            parse_mode="HTML"
        )

    # ======================== Quick Actions ========================
    # ======================== .env Manager ========================
    elif data.startswith("env_mgr_"):
        pid = int(data.replace("env_mgr_", ""))
        project = admin.get_project_by_id(pid)
        if not project or project["user_id"] != user_id:
            bot.answer_callback_query(call.id, "এই বটটি আপনার না!", show_alert=True)
            return
        env_vars = admin.get_env_vars(pid)
        if env_vars:
            lines_text = []
            for ev in env_vars:
                val_masked = ev["value"][:2] + "*" * max(0, len(ev["value"]) - 4) + ev["value"][-2:] if len(ev["value"]) > 4 else "****"
                lines_text.append(f"🔑 <code>{ev['key']}</code> = <code>{val_masked}</code>")
            env_text = "\n".join(lines_text)
        else:
            env_text = "<i>কোনো env variable নেই। যোগ করুন।</i>"
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            make_inline_button("➕ Variable যোগ করুন", callback_data=f"env_add_{pid}", style="success"),
            make_inline_button("🗑️ Variable মুছুন", callback_data=f"env_del_{pid}", style="danger"),
            make_inline_button("📄 .env ফাইল দেখুন (Full)", callback_data=f"env_show_full_{pid}", style="primary"),
            make_inline_button("🔙 Back", callback_data=f"bot_more_tools_{pid}", style="primary")
        )
        try:
            bot.edit_message_text(
                f"🔧 <b>.env Manager — {html.escape(project['project_name'])}</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"{env_text}\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "⚠️ <i>Values মাস্ক করে দেখানো হচ্ছে। বট রান হওয়ার সময় .env ফাইলে লেখা হয়।</i>",
                call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML"
            )
        except Exception:
            bot.send_message(call.message.chat.id,
                f"🔧 <b>.env Manager</b>\n{env_text}", reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    # ======================== Bot Clone ========================
    elif data.startswith("bot_clone_"):
        pid = int(data.replace("bot_clone_", ""))
        project = admin.get_project_by_id(pid)
        if not project or project["user_id"] != user_id:
            bot.answer_callback_query(call.id, "\u098f\u0987 \u09ac\u099f\u099f\u09bf \u0986\u09aa\u09a8\u09be\u09b0 \u09a8\u09be!", show_alert=True)
            return
        if admin.get_next_free_slot(user_id) is None:
            bot.answer_callback_query(call.id, "\u0995\u09cb\u09a8\u09cb \u0996\u09be\u09b2\u09bf \u09b8\u09cd\u09b2\u099f \u09a8\u09c7\u0987! \u09aa\u09cd\u09b0\u09a5\u09ae\u09c7 \u098f\u0995\u099f\u09be \u09ac\u099f \u09ae\u09c1\u099b\u09c1\u09a8 \u0985\u09a5\u09ac\u09be Upgrade Plan \u0995\u09b0\u09c1\u09a8\u0964", show_alert=True)
            return
        user_states[user_id] = {"state": "awaiting_clone_name", "source_pid": pid}
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            f"\U0001F4cb <b>Clone Bot \u2014 {html.escape(project['project_name'])}</b>\n"
            "\u2501" * 22 + "\n"
            "\u09a8\u09a4\u09c1\u09a8 \u0995\u09cd\u09b2\u09cb\u09a8 \u0995\u09b0\u09be \u09ac\u099f\u099f\u09bf\u09b0 \u09a8\u09be\u09ae \u09b2\u09bf\u0996\u09c1\u09a8:\n"
            "\U0001F4c1 \u09b8\u09ac \u09ab\u09be\u0987\u09b2 \u0993 env variable \u0985\u099f\u09cb\u09ae\u09c7\u099f\u09bf\u0995 \u0995\u09aa\u09bf \u09b9\u09ac\u09c7\u0964",
            parse_mode="HTML"
        )

    # ======================== Bot Token Change ========================
    elif data.startswith("bot_token_"):
        pid = int(data.replace("bot_token_", ""))
        project = admin.get_project_by_id(pid)
        if not project or project["user_id"] != user_id:
            bot.answer_callback_query(call.id, "\u098f\u0987 \u09ac\u099f\u099f\u09bf \u0986\u09aa\u09a8\u09be\u09b0 \u09a8\u09be!", show_alert=True)
            return
        user_states[user_id] = {"state": "awaiting_new_token", "pid": pid}
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            f"\U0001F511 <b>Change Bot Token \u2014 {html.escape(project['project_name'])}</b>\n"
            "\u2501" * 22 + "\n"
            "\u09a8\u09a4\u09c1\u09a8 Telegram Bot Token \u09aa\u09be\u09a0\u09be\u09a8:\n\n"
            "\u26a0\ufe0f \u099f\u09cb\u0995\u09c7\u09a8 \u09af\u09be\u099a\u09be\u0987 \u0995\u09b0\u09c7 BOT_TOKEN env variable \u0986\u09aa\u09a1\u09c7\u099f \u09b9\u09ac\u09c7 \u098f\u09ac\u0982 \u09ac\u099f\u099f\u09bf \u099a\u09b2\u09ae\u09be\u09a8 \u09a5\u09be\u0995\u09b2\u09c7 \u0985\u099f\u09cb-\u09b0\u09bf\u09b8\u09cd\u099f\u09be\u09b0\u09cd\u099f \u09b9\u09ac\u09c7\u0964\n"
            "\u09ae\u09c7\u09b8\u09c7\u099c\u099f\u09bf \u09aa\u09be\u09a0\u09be\u09a8\u09cb\u09b0 \u09aa\u09b0 \u09b8\u09be\u09a5\u09c7 \u09b8\u09be\u09a5\u09c7 \u09a1\u09bf\u09b2\u09bf\u099f \u09b9\u09df\u09c7 \u09af\u09be\u09ac\u09c7\u0964",
            parse_mode="HTML"
        )

    # ======================== Scheduled Start/Stop \u09ae\u09c7\u09a8\u09c1 ========================
    elif data.startswith("bot_schedule_"):
        pid = int(data.replace("bot_schedule_", ""))
        project = admin.get_project_by_id(pid)
        if not project or project["user_id"] != user_id:
            bot.answer_callback_query(call.id, "\u098f\u0987 \u09ac\u099f\u099f\u09bf \u0986\u09aa\u09a8\u09be\u09b0 \u09a8\u09be!", show_alert=True)
            return
        start_t = project.get("sched_start_time") or "\u09b8\u09c7\u099f \u0995\u09b0\u09be \u09a8\u09c7\u0987"
        stop_t = project.get("sched_stop_time") or "\u09b8\u09c7\u099f \u0995\u09b0\u09be \u09a8\u09c7\u0987"
        enabled = "\u2705 \u09b8\u099a\u09b2" if project.get("sched_enabled") else "\u274c \u09ac\u09a8\u09cd\u09a7"
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            make_inline_button("\u25b6\ufe0f Set Start Time", callback_data=f"sched_set_start_{pid}", style="success"),
            make_inline_button("\u23f9\ufe0f Set Stop Time", callback_data=f"sched_set_stop_{pid}", style="danger")
        )
        markup.add(make_inline_button("\U0001F515 Disable Schedule", callback_data=f"sched_disable_{pid}", style="primary"))
        markup.add(make_inline_button("\U0001F519 Back", callback_data=f"bot_more_tools_{pid}", style="primary"))
        text_sched = (
            f"\u23f0 <b>Scheduled Start/Stop \u2014 {html.escape(project['project_name'])}</b>\n"
            + "\u2501" * 22 + "\n"
            f"\u25b6\ufe0f <b>Start Time:</b> <code>{start_t}</code>\n"
            f"\u23f9\ufe0f <b>Stop Time:</b> <code>{stop_t}</code>\n"
            f"\U0001F4cc <b>Status:</b> {enabled}\n\n"
            "\u09b8\u09ae\u09df \u09b8\u09c7\u099f \u0995\u09b0\u09b2\u09c7 \u09aa\u09cd\u09b0\u09a4\u09bf\u09a6\u09bf\u09a8 \u0989\u0995\u09cd\u09a4 \u09b8\u09ae\u09df\u09c7 \u09ac\u099f\u099f\u09bf \u09b8\u09cd\u09ac\u09df\u0982\u0995\u09cd\u09b0\u09bf\u09df\u09ad\u09be\u09ac\u09c7 \u099a\u09be\u09b2\u09c1/\u09ac\u09a8\u09cd\u09a7 \u09b9\u09ac\u09c7 (Bangladesh Time)\u0964"
        )
        try:
            bot.edit_message_text(text_sched, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, text_sched, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("sched_set_start_"):
        pid = int(data.replace("sched_set_start_", ""))
        user_states[user_id] = {"state": "awaiting_sched_start", "pid": pid}
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, "\u25b6\ufe0f \u0995\u09a4 \u09b8\u09ae\u09df\u09c7 \u09ac\u099f\u099f\u09bf \u099a\u09be\u09b2\u09c1 \u09b9\u09ac\u09c7? HH:MM \u0986\u0995\u09be\u09b0\u09c7 \u09aa\u09be\u09a0\u09be\u09a8 (\u09af\u09c7\u09ae\u09a8 <code>08:00</code>):", parse_mode="HTML")

    elif data.startswith("sched_set_stop_"):
        pid = int(data.replace("sched_set_stop_", ""))
        user_states[user_id] = {"state": "awaiting_sched_stop", "pid": pid}
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, "\u23f9\ufe0f \u0995\u09a4 \u09b8\u09ae\u09df\u09c7 \u09ac\u099f\u099f\u09bf \u09ac\u09a8\u09cd\u09a7 \u09b9\u09ac\u09c7? HH:MM \u0986\u0995\u09be\u09b0\u09c7 \u09aa\u09be\u09a0\u09be\u09a8 (\u09af\u09c7\u09ae\u09a8 <code>00:00</code>):", parse_mode="HTML")

    elif data.startswith("sched_disable_"):
        pid = int(data.replace("sched_disable_", ""))
        admin.clear_bot_schedule(pid)
        bot.answer_callback_query(call.id, "\u2705 Schedule \u09ac\u09a8\u09cd\u09a7 \u0995\u09b0\u09be \u09b9\u09df\u09c7\u099b\u09c7!", show_alert=True)

    elif data.startswith("env_add_"):
        pid = int(data.replace("env_add_", ""))
        user_states[user_id] = {"state": "awaiting_env_var", "pid": pid}
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "🔧 <b>নতুন Env Variable যোগ করুন</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "এই ফরম্যাটে পাঠান:\n"
            "<code>KEY=VALUE</code>\n\n"
            "উদাহরণ:\n"
            "<code>BOT_TOKEN=123456:ABC...</code>\n"
            "<code>ADMIN_ID=987654321</code>\n\n"
            "⚠️ মেসেজটি পাঠানোর পর সাথে সাথে ডিলিট হয়ে যাবে।",
            parse_mode="HTML"
        )

    elif data.startswith("env_del_"):
        pid = int(data.replace("env_del_", ""))
        env_vars = admin.get_env_vars(pid)
        if not env_vars:
            bot.answer_callback_query(call.id, "কোনো variable নেই!", show_alert=True)
            return
        user_states[user_id] = {"state": "awaiting_env_delete", "pid": pid}
        bot.answer_callback_query(call.id)
        keys_text = "\n".join(f"• <code>{ev['key']}</code>" for ev in env_vars)
        bot.send_message(
            call.message.chat.id,
            f"🗑️ <b>কোন KEY মুছবেন?</b>\nKey-এর নাম পাঠান:\n{keys_text}",
            parse_mode="HTML"
        )

    elif data.startswith("env_show_full_"):
        pid = int(data.replace("env_show_full_", ""))
        project = admin.get_project_by_id(pid)
        if not project or project["user_id"] != user_id:
            bot.answer_callback_query(call.id, "এই বটটি আপনার না!", show_alert=True)
            return
        env_vars = admin.get_env_vars(pid)
        if not env_vars:
            bot.answer_callback_query(call.id, "কোনো env variable নেই!", show_alert=True)
            return
        lines_text = [f"{ev['key']}={ev['value']}" for ev in env_vars]
        env_content = "\n".join(lines_text)
        # ফাইল হিসেবে পাঠানো (নিরাপদ — শুধু ইউজার নিজে দেখবে)
        bio = io.BytesIO(env_content.encode("utf-8"))
        bio.name = ".env"
        bot.send_document(call.message.chat.id, bio, caption=f"🔧 <b>{html.escape(project['project_name'])}</b> — .env file", parse_mode="HTML")
        bot.answer_callback_query(call.id)

    # ======================== Version History ========================
    elif data.startswith("ver_hist_"):
        pid = int(data.replace("ver_hist_", ""))
        project = admin.get_project_by_id(pid)
        if not project or project["user_id"] != user_id:
            bot.answer_callback_query(call.id, "এই বটটি আপনার না!", show_alert=True)
            return
        history = admin.get_deploy_history(pid)
        if not history:
            bot.answer_callback_query(call.id, "এখনো কোনো ভার্সন হিস্ট্রি নেই!", show_alert=True)
            return
        markup = types.InlineKeyboardMarkup(row_width=1)
        for h in history[:10]:
            note = f" ({h['note']})" if h.get('note') and not h['note'].startswith('[') else ""
            label = f"v{h['version']} — {h['created_at'][:16]}{note}"
            markup.add(make_inline_button(f"↩️ {label}", callback_data=f"rollback_{pid}_{h['id']}", style="primary"))
        markup.add(make_inline_button("🔙 Back", callback_data=f"bot_more_tools_{pid}", style="primary"))
        try:
            bot.edit_message_text(
                f"📚 <b>Version History — {html.escape(project['project_name'])}</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "যেকোনো ভার্সনে Rollback করতে বাটন চাপুন:\n"
                "<i>(Rollback করার আগে বর্তমান ভার্সন অটো-সেভ হবে)</i>",
                call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML"
            )
        except Exception:
            bot.send_message(call.message.chat.id, "📚 <b>Version History</b>", reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("rollback_"):
        parts = data.replace("rollback_", "").split("_")
        pid, hist_id = int(parts[0]), int(parts[1])
        project = admin.get_project_by_id(pid)
        if not project or project["user_id"] != user_id:
            bot.answer_callback_query(call.id, "এই বটটি আপনার না!", show_alert=True)
            return
        bot.answer_callback_query(call.id, "⏳ Rollback হচ্ছে...")
        # চলমান বট বন্ধ
        p_key = f"{user_id}_{pid}"
        if p_key in active_processes:
            try:
                active_processes[p_key]["process"].terminate()
                active_processes[p_key]["log_handle"].close()
                del active_processes[p_key]
            except Exception:
                pass
        success, main_f_or_err = admin.restore_deploy_version(user_id, pid, hist_id)
        if success:
            ok_run, run_msg = execute_project_script(user_id, pid, main_f_or_err)
            rollback_line = ("🟢 বট আবার চালু হয়েছে।" if ok_run else f"⚠️ {run_msg}")

            try:
                bot.edit_message_text(
                    f"✅ <b>Rollback সফল হয়েছে!</b>\n"
                    f"📄 Main file: <code>{main_f_or_err}</code>\n"
                    + rollback_line,
                    call.message.chat.id, call.message.message_id, parse_mode="HTML"
                )
            except Exception:
                bot.send_message(call.message.chat.id, f"✅ Rollback সফল! বট রিস্টার্ট হয়েছে।")
        else:
            try:
                bot.edit_message_text(f"❌ <b>Rollback ব্যর্থ:</b> {html.escape(main_f_or_err)}", call.message.chat.id, call.message.message_id, parse_mode="HTML")
            except Exception:
                pass

    # ======================== Daily Report Settings ========================
    elif data == "daily_report_settings":
        u_data = admin.get_user(user_id)
        current_time = u_data.get("daily_report_time") or "বন্ধ"
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            make_inline_button("⏰ সময় সেট করুন", callback_data="daily_report_set", style="success"),
            make_inline_button("🔕 বন্ধ করুন", callback_data="daily_report_off", style="danger")
        )
        markup.add(make_inline_button("🔙 Back to Control Panel", callback_data="back_to_cp", style="primary"))
        try:
            bot.edit_message_text(
                f"📊 <b>Daily Report Settings</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⏰ <b>বর্তমান সময়:</b> <code>{current_time}</code> (Bangladesh Time)\n\n"
                "প্রতিদিন নির্দিষ্ট সময়ে আপনার বটের সারসংক্ষেপ পাবেন:\n"
                "• কতটা বট চলছে\n"
                "• কোনো ক্র্যাশ হয়েছে কিনা\n"
                "• মোট আপটাইম",
                call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML"
            )
        except Exception:
            bot.send_message(call.message.chat.id, "📊 Daily Report Settings", reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data == "daily_report_set":
        user_states[user_id] = {"state": "awaiting_daily_report_time"}
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "⏰ <b>Daily Report সময় সেট করুন</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "HH:MM ফরম্যাটে সময় পাঠান (Bangladesh Time):\n"
            "উদাহরণ: <code>08:00</code> বা <code>20:30</code>",
            parse_mode="HTML"
        )

    elif data == "daily_report_off":
        admin.set_daily_report_time(user_id, None)
        bot.answer_callback_query(call.id, "✅ Daily Report বন্ধ করা হয়েছে।")
        try:
            bot.edit_message_text("🔕 <b>Daily Report বন্ধ করা হয়েছে।</b>", call.message.chat.id, call.message.message_id, parse_mode="HTML")
        except Exception:
            pass

    elif data == "adm_quick_stop_all" and admin.is_admin(user_id):
        stopped = 0
        for p_key in list(active_processes.keys()):
            try:
                uid_p, pid_p = p_key.split("_", 1)
                active_processes[p_key]["process"].terminate()
                active_processes[p_key]["log_handle"].close()
                del active_processes[p_key]
                admin.update_project_status(int(pid_p), "STOPPED")
                stopped += 1
            except Exception:
                pass
        bot.answer_callback_query(call.id, f"✅ {stopped}টি বট বন্ধ করা হয়েছে!")
        try:
            bot.edit_message_text(f"⏹️ <b>{stopped}টি বট বন্ধ করা হয়েছে।</b>", call.message.chat.id, call.message.message_id, parse_mode="HTML")
        except Exception:
            pass

    elif data == "gh_deploy_start":
        u_data = admin.get_user(user_id)
        if not admin.is_vip_active(u_data):
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(make_inline_button("💎 Upgrade Plan", callback_data="open_upgrade_menu", style="success"))
            add_home_button(markup)
            lock_text = (
                "🔒 <b>GitHub Deploy — VIP Exclusive</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "এই ফিচারটি শুধুমাত্র পেইড প্ল্যান ইউজারদের জন্য।\n"
                "👉 ব্যবহার করতে নিচের বাটন থেকে যেকোনো পেইড প্ল্যান নিন।"
            )
            try:
                bot.edit_message_text(lock_text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
            except Exception:
                bot.send_message(call.message.chat.id, lock_text, reply_markup=markup, parse_mode="HTML")
            bot.answer_callback_query(call.id)
            return
        max_bots = admin.get_max_bot_limit_for_user(u_data)
        if len(admin.get_user_projects(user_id)) >= max_bots:
            bot.answer_callback_query(call.id, "⚠️ স্লট লিমিট পূর্ণ! একটি বট ডিলিট করুন বা প্ল্যান আপগ্রেড করুন।", show_alert=True)
            return
        if admin.get_next_free_slot(user_id) is None:
            bot.answer_callback_query(call.id, "⚠️ সবগুলো স্লট পূর্ণ! নতুন ডিপ্লয়ের আগে একটি ডিলিট করুন।", show_alert=True)
            return
        user_states[user_id] = {"state": "awaiting_github_url"}
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(make_inline_button("❌ বাতিল করুন", callback_data="gh_cancel", style="danger"))
        prompt_text = (
            "🐙 <b>Deploy from GitHub</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "আপনার GitHub রিপোর লিংক পাঠান।\n"
            "যেমন: <code>https://github.com/owner/repo</code>\n\n"
            "✅ requirements.txt থাকলে অটো-ইনস্টল হবে\n"
            "✅ main.py / bot.py অটো-ডিটেক্ট হয়ে রান হবে"
        )
        try:
            bot.edit_message_text(prompt_text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, prompt_text, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data == "gh_cancel":
        if user_id in user_states:
            del user_states[user_id]
        bot.answer_callback_query(call.id, "বাতিল করা হয়েছে।")
        try:
            bot.edit_message_text(
                "❌ <b>GitHub Deploy বাতিল করা হয়েছে।</b>\n"
                "মূল মেনুতে ফিরে যেতে /start দিন।",
                call.message.chat.id, call.message.message_id, parse_mode="HTML"
            )
        except Exception:
            bot.send_message(call.message.chat.id, "❌ বাতিল করা হয়েছে।")

    elif data == "gh_repo_public":
        st = user_states.get(user_id)
        if not st or not isinstance(st, dict) or st.get("state") != "awaiting_github_visibility":
            bot.answer_callback_query(call.id, "সেশনের মেয়াদ শেষ, আবার শুরু করুন।", show_alert=True)
            return
        owner, repo = st["owner"], st["repo"]
        del user_states[user_id]
        bot.answer_callback_query(call.id)
        bot.edit_message_text(
            f"⏳ <b>GitHub থেকে '{html.escape(owner)}/{html.escape(repo)}' ডাউনলোড হচ্ছে...</b>",
            call.message.chat.id, call.message.message_id, parse_mode="HTML"
        )
        threading.Thread(
            target=deploy_github_repo,
            args=(user_id, call.message.chat.id, call.message.message_id, owner, repo, None, False),
            daemon=True
        ).start()

    elif data == "gh_repo_private":
        st = user_states.get(user_id)
        if not st or not isinstance(st, dict) or st.get("state") != "awaiting_github_visibility":
            bot.answer_callback_query(call.id, "সেশনের মেয়াদ শেষ, আবার শুরু করুন।", show_alert=True)
            return
        user_states[user_id] = {"state": "awaiting_github_token", "owner": st["owner"], "repo": st["repo"]}
        bot.answer_callback_query(call.id)
        bot.edit_message_text(
            "🔒 <b>Private Repo — GitHub Token দিন</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "আপনার GitHub Personal Access Token (PAT) পাঠান।\n\n"
            "⚠️ <i>টোকেনটি শুধু এই ডাউনলোডের জন্য একবারই ব্যবহার হবে — কোথাও সংরক্ষিত করা হয় না।"
            " পাঠানো মেসেজটাও নিরাপত্তার জন্য সাথে সাথে ডিলিট হয়ে যাবে।</i>",
            call.message.chat.id, call.message.message_id, parse_mode="HTML"
        )

    elif data == "open_mp_menu":
        bot.edit_message_text(MARKETPLACE_INTRO, call.message.chat.id, call.message.message_id, reply_markup=build_marketplace_markup(), parse_mode="HTML")
        bot.answer_callback_query(call.id)

    # ⭐ FIX: এই ব্লকের "elif data.startswith("deploy_tpl_")" লাইনটি হারিয়ে গিয়ে কোডটি ভুলে open_mp_menu-এর
    # ভেতরে ঢুকে গিয়েছিল — ফলে Marketplace খুললেই টোকেন-ইনপুট মোড চালু হয়ে যেত আর "🚀 Deploy করুন"
    # বাটন কাজই করত না। এখন আলাদা ব্লকে ঠিক করা হয়েছে।
    elif data.startswith("deploy_tpl_"):
        tpl = data.replace("deploy_tpl_", "")
        user_states[user_id] = {"state": "awaiting_deploy_token", "template": tpl}
        bot.send_message(
            call.message.chat.id,
            "🤖 <b>আপনার বটের টোকেন দিন:</b>\n\n"
            "👉 @BotFather থেকে পাওয়া <code>BOT_TOKEN</code> টি মেসেজে সেন্ড করুন:"
        )
        bot.answer_callback_query(call.id)


    # 💼 Dev Studio (⭐ FIX: বাটনটির কোনো কলব্যাক হ্যান্ডলারই ছিল না, তাই কিছুই হতো না)
    elif data == "sup_devstudio":
        ds_text, ds_markup = get_dev_studio_card()
        try:
            bot.edit_message_text(ds_text, call.message.chat.id, call.message.message_id, reply_markup=ds_markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, ds_text, reply_markup=ds_markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data == "devstudio_request":
        user_states[user_id] = {"state": "awaiting_devstudio_request"}
        cancel_markup = types.InlineKeyboardMarkup()
        cancel_markup.add(make_inline_button("❌ বাতিল", callback_data="devstudio_cancel", style="danger"))
        bot.send_message(
            call.message.chat.id,
            "📝 <b>আপনার রিকোয়ারমেন্ট লিখুন</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
            "একটি মেসেজে লিখে পাঠান:\n"
            "1️⃣ বটের ধরন\n2️⃣ কী কী ফিচার চান\n3️⃣ বাজেট\n4️⃣ ডেডলাইন\n\n"
            "💡 যত বিস্তারিত লিখবেন, তত দ্রুত ও নির্ভুল কোটেশন পাবেন।",
            reply_markup=cancel_markup, parse_mode="HTML"
        )
        bot.answer_callback_query(call.id)

    elif data == "devstudio_cancel":
        user_states.pop(user_id, None)
        try:
            bot.edit_message_text("❌ <b>Dev Studio রিকোয়েস্ট বাতিল করা হয়েছে।</b>", call.message.chat.id, call.message.message_id, parse_mode="HTML")
        except Exception:
            pass
        bot.answer_callback_query(call.id, "বাতিল করা হয়েছে")

    # 🎁 ইউজার সাইড রিডিম
    elif data == "user_redeem_start":
        send_redeem_prompt(call.message.chat.id, user_id)
        bot.answer_callback_query(call.id)

    elif data == "user_redeem_cancel":
        user_states.pop(user_id, None)
        try:
            bot.edit_message_text("❌ <b>রিডিম বাতিল করা হয়েছে।</b>", call.message.chat.id, call.message.message_id, parse_mode="HTML")
        except Exception:
            pass
        bot.answer_callback_query(call.id, "বাতিল করা হয়েছে")

    # 🎁 অ্যাডমিন সাইড Redeem Code ম্যানেজার (rc_*)
    elif data.startswith("rc_"):
        if not admin.is_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ শুধু অ্যাডমিনদের জন্য।", show_alert=True)
            return
        _handle_redeem_admin_callback(call)

    elif data == "sup_faq":
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            make_inline_button("❓ বট চলছে না কেন?", callback_data="faq_bot_not_running", style="primary"),
            make_inline_button("🔑 টোকেন কোথায় পাবো?", callback_data="faq_get_token", style="primary"),
            make_inline_button("📦 requirements.txt কী?", callback_data="faq_requirements", style="primary"),
            make_inline_button("💎 প্ল্যান না কিনলে কী হয়?", callback_data="faq_free_plan", style="primary"),
            make_inline_button("🐙 GitHub Deploy কীভাবে?", callback_data="faq_github", style="primary"),
            make_inline_button("🔙 Back", callback_data="back_to_support", style="primary")
        )
        bot.edit_message_text(
            "❓ <b>FAQ — সাধারণ প্রশ্নোত্তর</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
            "নিচের যেকোনো প্রশ্নে ট্যাপ করুন:",
            call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML"
        )

    elif data in ("faq_bot_not_running","faq_get_token","faq_requirements","faq_free_plan","faq_github"):
        answers = {
            "faq_bot_not_running": (
                "❓ <b>বট চলছে না কেন?</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
                "১. বট টোকেন ভুল আছে কিনা চেক করুন (@BotFather → /mybots)\n"
                "২. কোডে SyntaxError আছে কিনা চেক করুন (View Logs)\n"
                "৩. requirements.txt-এ সব লাইব্রেরি আছে কিনা দেখুন\n"
                "৪. 🏥 Health Check বাটন চাপুন — স্বয়ংক্রিয়ভাবে সমস্যা ধরবে\n"
                "৫. Free প্ল্যানে বট দিনে ১২ ঘণ্টার বেশি চলে না"
            ),
            "faq_get_token": (
                "🔑 <b>বট টোকেন কোথায় পাবেন?</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
                "① Telegram-এ @BotFather-কে মেসেজ দিন\n"
                "② /newbot কমান্ড দিন\n"
                "③ বটের নাম ও username দিন (username শেষে Bot থাকতে হবে)\n"
                "④ টোকেন পাবেন — এটা কপি করে PyHost-এ দিন\n\n"
                "⚠️ টোকেন কখনো শেয়ার করবেন না!"
            ),
            "faq_requirements": (
                "📦 <b>requirements.txt কী?</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
                "আপনার Python বট যে লাইব্রেরিগুলো ব্যবহার করে, সেগুলোর লিস্ট।\n\n"
                "উদাহরণ:\n<code>pyTelegramBotAPI\nrequests\npython-dotenv</code>\n\n"
                "এই ফাইল থাকলে PyHost স্বয়ংক্রিয়ভাবে সব ইনস্টল করে নেয়।"
            ),
            "faq_free_plan": (
                "💎 <b>Free প্ল্যানে কী পাবেন?</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
                "✅ ২টি বট স্লট | ✅ দিনে ১২ ঘণ্টা | ✅ ফাইল আপলোড\n"
                "❌ 24/7 চলবে না | ❌ GitHub Deploy নেই\n\n"
                "💡 ৩ জনকে Refer করলে ৩ দিন ফ্রি VIP!"
            ),
            "faq_github": (
                "🐙 <b>GitHub Deploy কীভাবে?</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
                "① পেইড প্ল্যান থাকতে হবে\n"
                "② 🐙 GitHub Deploy বাটন চাপুন\n"
                "③ রিপো লিংক দিন: https://github.com/owner/repo\n"
                "④ Public/Private বেছে নিন → বট ডিপ্লয়!\n"
                "🔄 আপডেট হলে → Bot Dashboard → GitHub Sync"
            ),
        }
        answer = answers.get(data, "উত্তর পাওয়া যায়নি।")
        markup = types.InlineKeyboardMarkup()
        markup.add(make_inline_button("🔙 FAQ তালিকায় ফিরুন", callback_data="sup_faq", style="primary"))
        try:
            bot.edit_message_text(answer, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        except Exception:
            bot.send_message(call.message.chat.id, answer, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data == "back_to_support":
        msg = "🛠️ <b>Support Centre</b>\n━━━━━━━━━━━━━━━━━━\nযেকোনো সহায়তার জন্য বা কাস্টম বট বানানোর জন্য নিচের অপশনগুলো বেছে নিন:"
        bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, reply_markup=get_support_hub_markup(), parse_mode="HTML")

# ----------------- রিকানেকশন ও পোলিং লুপ -----------------
if __name__ == "__main__":
    print("PyHost Cloud 24/7 Multi-Bot Engine successfully initiated...")
    
    try:
        bot.remove_webhook()
        bot.delete_my_commands()
    except Exception:
        pass

    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=20, long_polling_timeout=20)
        except telebot.apihelper.ApiTelegramException as e:
            if e.error_code == 409:
                print("⚠️ Warning: Conflict Error (409)! Another instance is active. Retrying in 5s...")
                time.sleep(5)
            else:
                time.sleep(3)
        except Exception:
            time.sleep(3)
