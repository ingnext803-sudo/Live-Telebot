"""
Telegram Multi-Client Voice Chat Bot  (v2: activity log + admin user data)
=====================================
REQUIREMENTS (Wispbyte-এ requirements.txt ফাইলে এগুলো বসান):
    aiogram>=3.25
    kurigram
    tgcrypto
    py-tgcalls
    cryptography
    python-dotenv

RUN: python main.py   (Startup file = main.py)
"""
import asyncio, csv, html, io, logging, os, re, secrets, sqlite3, time
from cryptography.fernet import Fernet
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
                           InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile)
from aiogram.exceptions import TelegramRetryAfter
from pyrogram import Client, filters
from pyrogram.handlers import MessageHandler
from pyrogram.errors import (FloodWait, SessionPasswordNeeded, PhoneCodeInvalid,
                             PhoneCodeExpired, PasswordHashInvalid, UserAlreadyParticipant)
from pyrogram.raw.functions.messages import GetMessagesViews
from pytgcalls import PyTgCalls

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

# ====== আপনার কনফিগ (এখানেই বসানো আছে) ======
BOT_TOKEN = os.environ.get("BOT_TOKEN") or "8929246236:AAEqDfYPGFxe7i4k_AfXlM2Ev-V8Bg389q4"
API_ID = int(os.environ.get("API_ID") or 33680093)
API_HASH = os.environ.get("API_HASH") or "c6c802e0811240f1d1b46c730ba0a856"
OWNER_ID = int(os.environ.get("OWNER_ID") or 8815360015)
TZ_H = float(os.environ.get("TZ_OFFSET", "6"))   # display timezone (6 = Bangladesh)
START_TS = time.time()
CONTACT = os.environ.get("ADMIN_CONTACT", "")   # e.g. @yourusername  (shown to closed users)
BOT = None
DELAY = float(os.environ.get("DELAY", "1"))  # seconds between accounts (FloodWait protection)

# ====== Telegram Premium emoji (optional) ======
# Format:  "normal emoji": "custom_emoji_id"
# Every place the bot uses that emoji (messages + button icons) becomes a premium emoji.
# To get an ID: as admin, send any premium emoji to the bot -> it replies with the ID.
# Works only if the BOT OWNER account has Telegram Premium. Leave empty to use normal emoji.
PREMIUM = {
    # "📋": "5368324170671202286",
    # "🎧": "5310169226856644648",
}


def h(x):
    return html.escape(str(x))


def norm(e):
    return str(e).replace("\ufe0f", "")


# ---------- encryption + db ----------
KEY_FILE = "secret.key"
if os.environ.get("ENC_KEY"):
    KEY = os.environ["ENC_KEY"].encode()
elif os.path.exists(KEY_FILE):
    KEY = open(KEY_FILE, "rb").read()
else:
    KEY = Fernet.generate_key()
    open(KEY_FILE, "wb").write(KEY)
fernet = Fernet(KEY)

db = sqlite3.connect("data.db")
db.executescript("""
CREATE TABLE IF NOT EXISTS clients(id INTEGER PRIMARY KEY, name TEXT, phone TEXT, session BLOB, owner INTEGER);
CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, name TEXT, username TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS auto2(owner INTEGER, kind TEXT, chat_id TEXT, emoji TEXT, PRIMARY KEY(owner, kind, chat_id));
CREATE TABLE IF NOT EXISTS access_keys(key TEXT PRIMARY KEY, status TEXT, created INTEGER, used_by INTEGER, used_at INTEGER);
CREATE TABLE IF NOT EXISTS vip_users(user_id INTEGER PRIMARY KEY, key TEXT);
CREATE TABLE IF NOT EXISTS premium(emoji TEXT PRIMARY KEY, cid TEXT);
""")
try:
    db.execute("ALTER TABLE clients ADD COLUMN owner INTEGER")
except sqlite3.OperationalError:
    pass
db.execute("UPDATE clients SET owner=? WHERE owner IS NULL", (OWNER_ID,))
db.executescript("""
CREATE TABLE IF NOT EXISTS activity(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, kind TEXT, action TEXT, detail TEXT, result TEXT, ts INTEGER);
CREATE INDEX IF NOT EXISTS ix_act_user ON activity(user_id, id);
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
""")
for _col in ("joined INTEGER", "last_seen INTEGER"):
    try:
        db.execute("ALTER TABLE users ADD COLUMN " + _col)
    except sqlite3.OperationalError:
        pass
db.commit()
for _e, _c in db.execute("SELECT emoji, cid FROM premium").fetchall():   # premium emoji saved with /setemoji
    PREMIUM[_e] = _c
_t = {norm(k): v for k, v in PREMIUM.items()}
PREMIUM.clear(); PREMIUM.update(_t)

# ---------- runtime state ----------
RUNNING = {}     # tg_id -> (Client, PyTgCalls, owner_user_id)
PENDING = {}     # bot user id -> dict(app, phone, hash)
AUTO_REACT = {}  # (owner, chat_id) -> emoji
AUTO_VIEW = set()  # (owner, chat_id)
SEEN = {}        # (tg_id, chat, kind) -> set(message ids)


def load_auto():
    AUTO_REACT.clear(); AUTO_VIEW.clear()
    for owner, kind, chat, emoji in db.execute("SELECT owner, kind, chat_id, emoji FROM auto2"):
        if kind == "react":
            AUTO_REACT[(owner, int(chat))] = emoji
        else:
            AUTO_VIEW.add((owner, int(chat)))


def is_vip(uid):
    return bool(db.execute("SELECT 1 FROM vip_users WHERE user_id=?", (uid,)).fetchone())


def user_clients(uid):
    """Admin controls ALL accounts; VIP key holders control their own + the admin's accounts;
    everyone else only their own."""
    if uid == OWNER_ID:
        return list(RUNNING.items())
    if is_vip(uid):
        return [(tid, v) for tid, v in RUNNING.items() if v[2] in (uid, OWNER_ID)]
    return [(tid, v) for tid, v in RUNNING.items() if v[2] == uid]


def new_key():
    k = "VIP-" + secrets.token_hex(6).upper()
    db.execute("INSERT INTO access_keys(key,status,created) VALUES(?,?,?)", (k, "active", int(time.time())))
    db.commit()
    return k


def owner_name(oid):
    if oid == OWNER_ID:
        return "You"
    r = db.execute("SELECT name FROM users WHERE user_id=?", (oid,)).fetchone()
    return r[0] if r and r[0] else str(oid)


# ---------- settings / time / activity log ----------
def setting(k, d="0"):
    r = db.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    return r[0] if r else d


def set_setting(k, v):
    db.execute("INSERT OR REPLACE INTO settings(k,v) VALUES(?,?)", (k, str(v)))
    db.commit()


def fmt_ts(ts):
    return time.strftime("%d %b %H:%M", time.gmtime(ts + TZ_H * 3600))


def ago(ts):
    if not ts:
        return "never"
    d = int(time.time() - ts)
    if d < 60: return "just now"
    if d < 3600: return f"{d // 60}m ago"
    if d < 86400: return f"{d // 3600}h ago"
    return f"{d // 86400}d ago"


def fmt_row(r):
    kind, action, detail, result, ts = r
    s = f"<code>{fmt_ts(ts)}</code> {'⚙️' if kind == 'task' else '▸'} <b>{h(action)}</b>"
    if detail: s += f" — {h(detail)}"
    if result: s += f"  {h(result)}"
    return s


async def _feed(uid, action, detail, result):
    try:
        await BOT.send_message(OWNER_ID, f"📡 <b>{h(owner_name(uid))}</b> <code>{uid}</code>\n⚙️ {h(action)} {h(detail)} {h(result)}")
    except Exception:
        pass


def log_act(uid, kind, action, detail="", result=""):
    """kind: 'cmd' = menu/command used, 'task' = job executed (with result)."""
    try:
        db.execute("INSERT INTO activity(user_id,kind,action,detail,result,ts) VALUES(?,?,?,?,?,?)",
                   (uid, kind, action, str(detail)[:120], str(result)[:60], int(time.time())))
        db.commit()
        if kind == "task" and uid != OWNER_ID and BOT and setting("feed") == "1":
            asyncio.get_running_loop().create_task(_feed(uid, action, str(detail)[:80], str(result)))
    except Exception as e:
        log.debug("log_act: %r", e)


# ---------- helpers ----------
def parse_target(t):
    t = t.strip()
    m = re.search(r"t\.me/c/(\d+)", t)
    if m: return int("-100" + m.group(1))
    if re.fullmatch(r"-?\d+", t): return int(t)
    m = re.search(r"t\.me/(\+[\w-]+|joinchat/[\w-]+)", t)
    if m: return "https://t.me/" + m.group(1)
    m = re.search(r"(?:t\.me/|@)([A-Za-z0-9_]{4,})", t)
    if m: return m.group(1)
    return t


def parse_post(t):
    m = re.search(r"t\.me/c/(\d+)/(\d+)", t)
    if m: return int("-100" + m.group(1)), int(m.group(2))
    m = re.search(r"t\.me/([A-Za-z0-9_]+)/(\d+)", t)
    if m: return m.group(1), int(m.group(2))
    return None


async def for_all(uid, fn):
    ok = fail = 0
    t0 = time.time()
    for _, (app, calls, _o) in user_clients(uid):
        try:
            try:
                await fn(app, calls)
            except FloodWait as e:
                await asyncio.sleep(e.value + 1)
                await fn(app, calls)
            ok += 1
        except UserAlreadyParticipant:
            ok += 1
        except Exception as e:
            fail += 1
            log.warning("client failed: %r", e)
        await asyncio.sleep(DELAY)
    return ok, fail, max(1, int(time.time() - t0))


def report(ok, fail, secs, label="Done"):
    total = max(1, ok + fail)
    filled = round(10 * ok / total)
    _b = THEMES[THEME]["bar"]
    bar = _b[0] * filled + _b[1] * (10 - filled)
    return (hd("✅", f"{label} complete") +
            tree(f"✅ Success — <b>{ok}</b>", f"❌ Failed — <b>{fail}</b>", f"⏳ Time — <b>{secs}s</b>") +
            f"\n\n<code>{bar}</code> {round(100 * ok / total)}%")


def make_handler(owner):
    async def on_post(client, m):
        chat = m.chat.id
        emoji = AUTO_REACT.get((owner, chat)) or AUTO_REACT.get((OWNER_ID, chat))
        if emoji:
            try:
                await client.send_reaction(chat, m.id, emoji)
            except Exception as e:
                log.debug("react: %r", e)
        if (owner, chat) in AUTO_VIEW or (OWNER_ID, chat) in AUTO_VIEW:
            try:
                await client.invoke(GetMessagesViews(peer=await client.resolve_peer(chat), id=[m.id], increment=True))
            except Exception as e:
                log.debug("view: %r", e)
    return on_post


async def start_client(session_string, tg_id, owner):
    await stop_client(tg_id)
    app = Client(f"c{tg_id}", api_id=API_ID, api_hash=API_HASH,
                 session_string=session_string, in_memory=True)
    await app.start()
    try:
        async for _ in app.get_dialogs():   # fills peer cache so chat IDs resolve after restart
            pass
    except Exception as e:
        log.warning("dialogs %s: %r", tg_id, e)
    calls = PyTgCalls(app)
    await calls.start()
    app.add_handler(MessageHandler(make_handler(owner), filters.channel))
    RUNNING[tg_id] = (app, calls, owner)


async def stop_client(tg_id):
    pair = RUNNING.pop(tg_id, None)
    if pair:
        try: await pair[0].stop()
        except Exception: pass


async def recent_ids(app, chat, n=10):
    return [x.id async for x in app.get_chat_history(chat, limit=n)]


async def view_posts(app, chat, ids):
    await app.invoke(GetMessagesViews(peer=await app.resolve_peer(chat), id=ids, increment=True))


async def watcher():
    """Backup for Auto View / Auto React: checks recent posts every 10s so nothing is missed."""
    await asyncio.sleep(20)
    while True:
        try:
            for owner, chat in set(AUTO_VIEW) | set(AUTO_REACT):
                clients = user_clients(owner)
                if not clients:
                    continue
                try:
                    ids = await recent_ids(clients[0][1][0], chat)
                except Exception as e:
                    log.warning("watch %s: %r", chat, e)
                    continue
                for tg_id, (app, _c, _o) in clients:
                    if (owner, chat) in AUTO_VIEW:
                        vkey = (tg_id, chat, "v")
                        if vkey not in SEEN:
                            SEEN[vkey] = set(ids)   # baseline: old posts are not viewed
                        else:
                            new = [i for i in ids if i not in SEEN[vkey]]
                            if new:
                                try:
                                    await view_posts(app, chat, new)
                                    SEEN[vkey].update(new)
                                except FloodWait as e:
                                    await asyncio.sleep(e.value + 1)
                                except Exception as e:
                                    log.warning("view %s: %r", tg_id, e)
                    emoji = AUTO_REACT.get((owner, chat))
                    if emoji:
                        key = (tg_id, chat, "r")
                        if key not in SEEN:
                            SEEN[key] = set(ids)   # baseline: don't react to old posts
                        else:
                            for i in [i for i in ids if i not in SEEN[key]]:
                                try:
                                    await app.send_reaction(chat, i, emoji)
                                except Exception as e:
                                    log.debug("react %s: %r", tg_id, e)
                                SEEN[key].add(i)
                    await asyncio.sleep(0.5)
        except Exception as e:
            log.warning("watcher: %r", e)
        await asyncio.sleep(10)


# ---------- buttons: colors + premium emoji ----------
def split_icon(label):
    nl = norm(label)
    for e, cid in PREMIUM.items():
        if nl.startswith(e):
            return nl[len(e):].strip(), cid
    return label, None


def ibtn(label, data, style=None):
    text, icon = split_icon(label)
    return InlineKeyboardButton(text=text, callback_data=data, style=style, icon_custom_emoji_id=icon)


def ikb(*rows):
    """rows of (label, callback_data[, style]); style = primary | success | danger"""
    return InlineKeyboardMarkup(inline_keyboard=[[ibtn(*b) for b in r] for r in rows])


# Every emoji the design uses. /setemoji maps premium emoji onto these, in this order.
SLOTS = [("👑", "Admin"), ("💎", "VIP"), ("📱", "Accounts"), ("➕", "Add"), ("🎧", "Voice join"),
         ("🔇", "Voice leave"), ("🚀", "Join channel"), ("💫", "React"), ("⚡", "Auto react"),
         ("👀", "Auto view"), ("🔙", "Back"), ("✅", "Success"), ("❌", "Failed"), ("⏳", "Pending / time"),
         ("🟢", "Online"), ("🔴", "Offline"), ("🔑", "Keys"), ("👥", "Users"), ("📥", "Requests"),
         ("🚫", "Revoked"), ("🔐", "Lock"), ("🧹", "Remove"), ("🎉", "Celebrate")]

B_CLIENTS, B_ADD = "📱 Accounts", "➕ Add Account"
B_JVC, B_LVC = "🎧 Voice Join", "🔇 Voice Leave"
B_JOIN, B_REACT = "🚀 Join Channel", "💫 React"
B_AR, B_AV = "⚡ Auto React", "👀 Auto View"
B_ACC, B_ADMIN, B_BACK = "💎 Access", "👑 Admin", "🔙 Back"
B_LEAVE, B_ACT = "🚪 Leave Channel", "📜 My Activity"
BTN_NAMES = {}
for _l in (B_CLIENTS, B_ADD, B_JVC, B_LVC, B_JOIN, B_REACT, B_AR, B_AV, B_ACC, B_ADMIN, B_BACK, B_LEAVE, B_ACT):
    BTN_NAMES[_l] = BTN_NAMES[_l.split(" ", 1)[1]] = _l.split(" ", 1)[1]


def btext(label):
    """Premium emoji becomes the button icon, so the text keeps only the name."""
    e, name = label.split(" ", 1)
    return name if norm(e) in PREMIUM else label


def ANY(label):
    """Filter that matches a button with or without its emoji."""
    return F.text.in_({label, label.split(" ", 1)[1]})


def rbtn(label, style=None):
    return KeyboardButton(text=btext(label), style=style, icon_custom_emoji_id=PREMIUM.get(norm(label.split(" ", 1)[0])))


def _up(t):
    return t if "&" in t else t.upper()


# ---------- themes (switch live with /theme or Admin -> Theme) ----------
THEMES = {
    "aurora": {"name": "🌌 Aurora", "bar": "▰▱", "pre": "◈ ",
               "head": lambda i, t, s: f"⟪ {i} <b>{_up(t)}</b> ⟫\n" + (f"<i>{s}</i>\n" if s else "") + "▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n\n"},
    "clean": {"name": "🤍 Clean", "bar": "●○", "pre": "",
              "head": lambda i, t, s: f"<b>{i} {t}</b>\n" + (f"<i>{s}</i>\n" if s else "") + "\n"},
    "terminal": {"name": "🖥 Terminal", "bar": "█░", "pre": "❯ ",
                 "head": lambda i, t, s: f"<code>┌─[ {_up(t)} ]</code> {i}\n" + (f"<code>└─ {s}</code>\n" if s else "<code>└──</code>\n") + "\n"},
    "royal": {"name": "👑 Royal", "bar": "◆◇", "pre": "❖ ",
              "head": lambda i, t, s: f"✦ ━━ {i} <b>{_up(t)}</b> ━━ ✦\n" + (f"<i>{s}</i>\n" if s else "") + "\n"},
}
THEME = setting("theme", "aurora")
if THEME not in THEMES:
    THEME = "aurora"


def set_theme(k):
    global THEME
    if k in THEMES:
        THEME = k
        set_setting("theme", k)


def hd(icon, title, sub=""):
    """Screen header, drawn in the active theme."""
    return THEMES[THEME]["head"](icon, title, sub)


def tree(*lines, expand=False):
    """Info card (quote block), one item per row, bullet depends on the theme."""
    pre = THEMES[THEME]["pre"]
    lines = [pre + l for l in lines if l]
    tag = "blockquote expandable" if expand else "blockquote"
    return f"<{tag}>" + "\n".join(lines) + "</blockquote>"


def kb(uid):
    admin = uid == OWNER_ID
    rows = [
        [rbtn(B_JVC, "success"), rbtn(B_LVC, "danger")],
        [rbtn(B_JOIN, "success"), rbtn(B_LEAVE, "danger")],
        [rbtn(B_AR, "primary"), rbtn(B_AV, "primary")],
        [rbtn(B_REACT, "danger"), rbtn(B_CLIENTS, "primary")],
        [rbtn(B_ADD, "success"), rbtn(B_ACT, "primary")],
        [rbtn(B_ACC, "primary")] + ([rbtn(B_ADMIN, "danger")] if admin else []),
    ]
    return ReplyKeyboardMarkup(resize_keyboard=True, keyboard=rows)


def back_kb():
    return ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[[rbtn(B_BACK, "danger")]])


# ---------- closed (revoked / rejected) menu ----------
B_VIPKEY = "🔑 Enter VIP Key"


def strike(s):
    return "".join(c + "\u0336" for c in s)


def locked_kb():
    """Same layout as the real menu, but every button is struck through and locked."""
    def b(label):
        return KeyboardButton(text="🔒 " + strike(label.split(" ", 1)[1]), style="danger")
    rows = [[b(B_JVC), b(B_LVC)], [b(B_JOIN), b(B_LEAVE)], [b(B_AR), b(B_AV)],
            [b(B_REACT), b(B_CLIENTS)], [b(B_ADD), b(B_ACT)], [b(B_ACC)],
            [KeyboardButton(text=B_VIPKEY, style="primary")]]
    return ReplyKeyboardMarkup(resize_keyboard=True, keyboard=rows)


def closed_text():
    return ("🔒 <b>Closed</b>\n\nYour access is closed. Please contact the admin."
            + (f" {h(CONTACT)}" if CONTACT else "") + "\n\nHave a VIP key? Tap <b>🔑 Enter VIP Key</b> below.")


def menu_kb(uid):
    """Normal menu for allowed users, locked menu for closed ones."""
    if uid == OWNER_ID:
        return kb(uid)
    r = db.execute("SELECT status FROM users WHERE user_id=?", (uid,)).fetchone()
    return locked_kb() if r and r[0] in ("revoked", "rejected") else kb(uid)


class PremiumMW(BaseRequestMiddleware):
    """Turns plain emoji in outgoing messages into premium (custom) emoji."""
    async def __call__(self, make_request, bot, method):
        if PREMIUM and type(method).__name__ in ("SendMessage", "EditMessageText") and isinstance(method.text, str):
            pat = re.compile("(" + "|".join(re.escape(k) for k in sorted(PREMIUM, key=len, reverse=True)) + ")\ufe0f?")
            method.text = pat.sub(lambda mo: f'<tg-emoji emoji-id="{PREMIUM[mo.group(1)]}">{mo.group(1)}</tg-emoji>', method.text)
        return await make_request(bot, method)


class S(StatesGroup):
    phone = State(); otp = State(); pwd = State()
    join = State(); jvc = State(); lvc = State()
    react_link = State(); react_emoji = State()
    ar_chat = State(); ar_emoji = State(); av_chat = State()
    vip_key = State(); emoji_pack = State()
    leave = State(); bcast = State(); dm = State()


# ---------- access: request -> admin accepts ----------
ADMIN_ONLY_TEXT = {B_ADMIN, B_ADMIN.split(" ", 1)[1]}
VIP_BTN = ikb([("🔑 Enter VIP Access Key", "vip_enter", "primary")])


class Guard(BaseMiddleware):
    async def __call__(self, handler, event, data):
        u = event.from_user
        if not u:
            return
        is_msg = isinstance(event, Message)

        async def say(t, markup=None):
            if is_msg:
                await event.answer(t, reply_markup=markup)
            else:
                await event.answer(re.sub(r"<[^>]+>", "", t)[:190], show_alert=True)

        if u.id != OWNER_ID:
            db.execute("UPDATE users SET last_seen=? WHERE user_id=?", (int(time.time()), u.id))
            db.commit()
        if u.id == OWNER_ID:
            return await handler(event, data)
        if setting("maint") == "1":
            return await say("🛠 The bot is under maintenance right now.\n\nPlease try again a little later.")
        # key entry is allowed for everyone (so a key holder can get in)
        st_now = await data["state"].get_state() if data.get("state") else None
        if st_now == S.vip_key.state or (is_msg and event.text == B_VIPKEY) or (not is_msg and (event.data or "") == "vip_enter"):
            return await handler(event, data)
        row = db.execute("SELECT status FROM users WHERE user_id=?", (u.id,)).fetchone()
        st = row[0] if row else None
        if st == "approved":
            if is_msg and event.text in ADMIN_ONLY_TEXT:
                return await say("⛔ Admin only.")
            if not is_msg and (event.data or "").startswith("adm"):
                return await say("⛔ Admin only.")
            return await handler(event, data)
        if st == "rejected":
            return await say(closed_text(), locked_kb())
        if st == "revoked":
            return await say(closed_text(), locked_kb())
        if st == "pending":
            return await say("⏳ Your request is still waiting for the owner's approval.\n\nHave a VIP key? Enter it below.", VIP_BTN)
        # new user -> send request to admin
        db.execute("INSERT OR REPLACE INTO users(user_id,name,username,status,joined,last_seen) VALUES(?,?,?,?,?,?)",
                   (u.id, u.full_name, u.username or "", "pending", int(time.time()), int(time.time())))
        db.commit()
        await say("🔐 Bot authorization request sent!\n\nWaiting for the bot owner to approve you. You'll be notified here.\n\nHave a VIP key? Enter it below.", VIP_BTN)
        try:
            uname = f"@{h(u.username)}" if u.username else "—"
            await data["bot"].send_message(
                OWNER_ID,
                f"🔔 New access request\n\n👤 Name: {h(u.full_name)}\n🔗 Username: {uname}\n🆔 ID: {u.id}",
                reply_markup=ikb([("✅ Accept", f"adm:ap:{u.id}", "success"), ("❌ Reject", f"adm:rj:{u.id}", "danger")]))
        except Exception as e:
            log.warning("owner notify failed: %r", e)


class ActivityMW(BaseMiddleware):
    """Records every command and menu-button press. Inputs typed during a flow
    (phone, OTP, password, VIP key) are NEVER stored."""
    async def __call__(self, handler, event, data):
        u = event.from_user
        if u and isinstance(event, Message) and event.text:
            t = event.text.strip()
            name = None
            if t.startswith("/"):
                name = t.split()[0].split("@")[0]
            elif t in BTN_NAMES:
                name = BTN_NAMES[t]
            if name:
                log_act(u.id, "cmd", name)
        return await handler(event, data)


dp = Dispatcher(storage=MemoryStorage())
dp.message.outer_middleware(Guard())
dp.callback_query.outer_middleware(Guard())
dp.message.outer_middleware(ActivityMW())


# ---------- basic ----------
@dp.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    uid = m.from_user.id
    plan = "👑 Admin" if uid == OWNER_ID else ("💎 VIP" if is_vip(uid) else "🔐 Member")
    total, online = len(client_rows(uid)), len(user_clients(uid))
    today = count("SELECT COUNT(*) FROM activity WHERE user_id=? AND ts>=?", uid, midnight())
    up = int(time.time() - START_TS)
    await m.answer(hd("🎧", "Voice control", f"welcome, {h(m.from_user.first_name)}") +
                   tree(plan, f"📱 Accounts — <b>{total}</b>", f"🟢 Online — <b>{online}</b>",
                        f"⚡ Today — <b>{today}</b> commands", f"⏱ Uptime — <b>{up // 3600}h {up % 3600 // 60}m</b>") +
                   "\n\nChoose an action from the menu 👇", reply_markup=kb(uid))


@dp.message(Command("cancel"))
@dp.message(ANY(B_BACK))
async def cancel(m: Message, state: FSMContext):
    p = PENDING.pop(m.from_user.id, None)
    if p:
        try: await p["app"].disconnect()
        except Exception: pass
    await state.clear()
    await m.answer("❎ Cancelled.", reply_markup=menu_kb(m.from_user.id))


@dp.message(Command("setemoji"))
async def setemoji(m: Message, state: FSMContext):
    if m.from_user.id != OWNER_ID:
        return
    await state.set_state(S.emoji_pack)
    lst = "\n".join(f"{i}. {e}  {d}" for i, (e, d) in enumerate(SLOTS, 1))
    await m.answer(hd("💎", "Premium emoji setup", "owner account needs Telegram Premium") +
                   "Send ONE message with premium emoji in exactly this order (spaces between are fine):\n\n"
                   f"{lst}\n\nSend fewer to set only the first ones. /cancel to abort.", reply_markup=back_kb())


@dp.message(S.emoji_pack)
async def setemoji_do(m: Message, state: FSMContext):
    ents = [e for e in (m.entities or []) if e.type == "custom_emoji"]
    if not ents:
        return await m.answer("❌ No premium emoji found in that message. Send them again, or /cancel.")
    for e, (slot, _d) in zip(ents, SLOTS):
        PREMIUM[norm(slot)] = e.custom_emoji_id
        db.execute("INSERT OR REPLACE INTO premium(emoji,cid) VALUES(?,?)", (norm(slot), e.custom_emoji_id))
    db.commit()
    await state.clear()
    n = min(len(ents), len(SLOTS))
    await m.answer(hd("🎉", "Premium emoji saved", f"{n} of {len(SLOTS)} set") + "Menu and messages now use them.", reply_markup=kb(m.from_user.id))


@dp.message(Command("setpack"))
async def setpack(m: Message):
    if m.from_user.id != OWNER_ID:
        return
    args = (m.text or "").split()[1:]
    names = [re.sub(r".*/(?:addemoji|addstickers)/", "", a).strip("/ ") for a in args]
    if not names:
        return await m.answer(hd("💎", "Premium emoji pack", "one link = whole bot turns premium") +
                              "Send:\n<code>/setpack https://t.me/addemoji/PackName</code>\n\n"
                              "How to get a link: tap any premium emoji → tap its pack name → Share → Copy link.\n"
                              "You can give several packs, separated by a space.")
    by_emoji, pool, errs = {}, [], []
    for n in names:
        try:
            ss = await m.bot.get_sticker_set(n)
        except Exception as e:
            errs.append(f"{h(n)}: {h(e)}")
            continue
        for st in ss.stickers:
            cid = getattr(st, "custom_emoji_id", None)
            if cid:
                by_emoji.setdefault(norm(st.emoji or ""), cid)
                if cid not in pool:
                    pool.append(cid)
    if not pool:
        return await m.answer("❌ Could not read that pack (it must be a custom EMOJI pack).\n" + "\n".join(errs))
    chosen = {}
    for slot, _d in SLOTS:                       # 1) exact emoji match
        if norm(slot) in by_emoji:
            chosen[norm(slot)] = by_emoji[norm(slot)]
    exact = len(chosen)
    spare = [c for c in pool if c not in chosen.values()]
    for slot, _d in SLOTS:                       # 2) fill the rest from unused pack emoji
        if norm(slot) not in chosen and spare:
            chosen[norm(slot)] = spare.pop(0)
    for e, cid in by_emoji.items():              # 3) every other emoji the pack covers
        if e and e not in chosen:
            chosen[e] = cid
    for e, cid in chosen.items():
        PREMIUM[e] = cid
        db.execute("INSERT OR REPLACE INTO premium(emoji,cid) VALUES(?,?)", (e, cid))
    db.commit()
    await m.answer(hd("🎉", "Premium pack applied", f"{len(chosen)} emoji ready") +
                   f"💎 Exact matches — <b>{exact}</b>\n⚡ Filled from pack — <b>{len(chosen) - exact}</b>\n\n"
                   "Menu + all messages now use them.", reply_markup=kb(m.from_user.id))


@dp.message(Command("resetemoji"))
async def resetemoji(m: Message):
    if m.from_user.id != OWNER_ID:
        return
    PREMIUM.clear()
    db.execute("DELETE FROM premium"); db.commit()
    await m.answer("♻️ Premium emoji cleared.", reply_markup=kb(m.from_user.id))


@dp.message(Command("emojiid"))
async def emojiid_help(m: Message):
    if m.from_user.id == OWNER_ID:
        await m.answer("Send me any premium emoji and I'll reply with its ID.")


def has_custom_emoji(m: Message):
    return m.from_user.id == OWNER_ID and any(e.type == "custom_emoji" for e in (m.entities or []))


@dp.message(StateFilter(None), has_custom_emoji)
async def emoji_ids(m: Message):
    lines = []
    for e in m.entities:
        if e.type == "custom_emoji":
            ch = e.extract_from(m.text)
            lines.append(f'{h(ch)} → <code>{e.custom_emoji_id}</code>')
    await m.answer("\n".join(lines) + '\n\nPaste into PREMIUM in main.py like:\n<code>"📋": "ID"</code>')


# ---------- My Clients / Add Client ----------
def client_rows(uid):
    if uid == OWNER_ID:
        return db.execute("SELECT id, name, phone, owner FROM clients ORDER BY id").fetchall()
    if is_vip(uid):
        return db.execute("SELECT id, name, phone, owner FROM clients WHERE owner IN (?,?) ORDER BY id", (uid, OWNER_ID)).fetchall()
    return db.execute("SELECT id, name, phone, owner FROM clients WHERE owner=? ORDER BY id", (uid,)).fetchall()


@dp.message(ANY(B_CLIENTS))
async def my_clients(m: Message):
    uid = m.from_user.id
    rows = client_rows(uid)
    if not rows:
        return await m.answer(hd("📱", "Accounts", "nothing here yet") + "Tap <b>➕ Add Account</b> to log one in.")
    lines = []
    for i, (c, n, p, o) in enumerate(rows, 1):
        shared = uid != OWNER_ID and o == OWNER_ID   # admin's account seen by a VIP user
        l = f"{i}. {'🟢' if c in RUNNING else '🔴'} {h(n)} | 📞 {'hidden' if shared else h(p)} | 🆔 {c}"
        if uid == OWNER_ID:
            l += f" | 👥 {h(owner_name(o))}"
        elif shared:
            l += " | 🔒 VIP"
        lines.append(l)
    body = "\n".join(lines)
    if len(body) > 3500:
        body = body[:3500] + "\n…"
    on = sum(1 for r in rows if r[0] in RUNNING)
    await m.answer(hd("📱", f"Accounts · {len(rows)}", f"🟢 {on} online · 🔴 {len(rows) - on} offline") +
                   f"<blockquote expandable>{body}</blockquote>\n\n🧹 <code>/remove 3</code>")


@dp.message(ANY(B_ADD))
async def add_client_btn(m: Message):
    await m.answer(
        hd("➕", "Add account", "phone number + OTP login") + "Tap the button to start.",
        reply_markup=ikb([("📱 Add by Phone Number", "add_phone", "primary")]))


@dp.callback_query(F.data == "add_phone")
async def add_phone(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.phone)
    await c.message.answer(
        "📱 Add by Phone Number\n\nSend the Telegram account phone number with country code, e.g. +1234567890.\n\n"
        "Tap 🔙 Back to cancel, or send /cancel to abort.", reply_markup=back_kb())
    await c.answer()


@dp.message(Command("remove"))
async def remove_cmd(m: Message):
    rows = client_rows(m.from_user.id)
    nums = [int(x) for x in (m.text or "").split()[1:] if x.isdigit()]
    nums = [n for n in nums if 1 <= n <= len(rows)]
    if not nums:
        return await m.answer("Usage: /remove NUMBER\nUse the number from the 📱 Accounts list. Several at once: /remove 2 5 7")
    names = []
    for n in sorted(set(nums)):
        cid, name, _p, own = rows[n - 1]
        if m.from_user.id != OWNER_ID and own != m.from_user.id:
            continue   # VIP users can't remove the admin's accounts
        await stop_client(cid)
        db.execute("DELETE FROM clients WHERE id=?", (cid,))
        names.append(h(name))
    db.commit()
    log_act(m.from_user.id, "task", "Remove Account", ", ".join(names), f"{len(names)}")
    await m.answer("🗑 Removed: " + ", ".join(names))


@dp.message(S.phone)
async def got_phone(m: Message, state: FSMContext):
    phone = m.text.strip().replace(" ", "")
    app = Client(f"login{m.from_user.id}", api_id=API_ID, api_hash=API_HASH, in_memory=True)
    try:
        await app.connect()
        sent = await app.send_code(phone)
    except Exception as e:
        try: await app.disconnect()
        except Exception: pass
        return await m.answer(f"❌ Could not send code: {h(e)}\nTry again or tap 🔙 Back.")
    PENDING[m.from_user.id] = dict(app=app, phone=phone, hash=sent.phone_code_hash)
    await state.set_state(S.otp)
    await m.answer(
        f"🔑 OTP sent to {h(phone)}\n\nSend the login code. You can paste the full message — I'll extract the digits automatically.\n\n"
        "Tap 🔙 Back to cancel, or send /cancel to abort.")


async def finish_login(m: Message, state: FSMContext, p):
    app = p["app"]
    session = await app.export_session_string()
    me = await app.get_me()
    await app.disconnect()
    name = " ".join(x for x in [me.first_name, me.last_name] if x) or "NoName"
    db.execute("INSERT OR REPLACE INTO clients(id,name,phone,session,owner) VALUES(?,?,?,?,?)",
               (me.id, name, p["phone"], fernet.encrypt(session.encode()), m.from_user.id))
    db.commit()
    await start_client(session, me.id, m.from_user.id)
    PENDING.pop(m.from_user.id, None)
    await state.clear()
    log_act(m.from_user.id, "task", "Add Account", name, "✅")
    await m.answer(f"✅ Account added via Phone Number!\n\n👤 Name: {h(name)}\n📞 Phone: {h(p['phone'])}\n🆔 ID: {me.id}",
                   reply_markup=kb(m.from_user.id))


@dp.message(S.otp)
async def got_otp(m: Message, state: FSMContext):
    p = PENDING.get(m.from_user.id)
    code = "".join(re.findall(r"\d", m.text))
    if not p or len(code) < 5:
        return await m.answer("❌ Couldn't find a valid code. Send it again.")
    try:
        await p["app"].sign_in(p["phone"], p["hash"], code)
    except SessionPasswordNeeded:
        await state.set_state(S.pwd)
        return await m.answer("🔐 2-Step Verification is enabled.\n\nSend your account password.")
    except (PhoneCodeInvalid, PhoneCodeExpired) as e:
        return await m.answer(f"❌ {e.__class__.__name__}. Send the correct code, or /cancel and retry.")
    except Exception as e:
        return await m.answer(f"❌ Login failed: {h(e)}")
    await finish_login(m, state, p)


@dp.message(S.pwd)
async def got_pwd(m: Message, state: FSMContext):
    p = PENDING.get(m.from_user.id)
    try:
        await p["app"].check_password(m.text.strip())
    except PasswordHashInvalid:
        return await m.answer("❌ Wrong password. Try again.")
    except Exception as e:
        return await m.answer(f"❌ Login failed: {h(e)}")
    try: await m.delete()  # remove password from chat
    except Exception: pass
    await finish_login(m, state, p)


# ---------- Join Channel ----------
@dp.message(ANY(B_JOIN))
async def join_ask(m: Message, state: FSMContext):
    await state.set_state(S.join)
    await m.answer(
        hd("🚀", "Join channel") + "Send the username or invite link.\n\n"
        "• Public: @username or https://t.me/username\n• Private: https://t.me/+InviteHash\n\n"
        "All your clients will attempt to join.\n\nSend /cancel to abort.", reply_markup=back_kb())


@dp.message(S.join)
async def join_do(m: Message, state: FSMContext):
    target = parse_target(m.text)
    await state.clear()
    await m.answer("⏳ Joining...")
    ok, fail, secs = await for_all(m.from_user.id, lambda app, _: app.join_chat(target))
    log_act(m.from_user.id, "task", "Join Channel", str(target), f"✅{ok} ❌{fail}")
    await m.answer(report(ok, fail, secs, "Joined"), reply_markup=kb(m.from_user.id))


# ---------- Voice chat ----------
@dp.message(ANY(B_JVC))
async def jvc_ask(m: Message, state: FSMContext):
    await state.set_state(S.jvc)
    await m.answer(
        hd("🎧", "Voice join") + "Send the username or Chat ID.\n\n"
        "• Public: @username or https://t.me/username\n"
        "• Private: Chat ID (e.g. -1001234567890) or https://t.me/c/1234567890\n\n"
        "⚠️ For private groups/channels: use 🚀 Join Channel first, then come here with the Chat ID.\n"
        "⚠️ Invite links (t.me/+...) are not supported here — use the Chat ID instead.\n\nSend /cancel to abort.",
        reply_markup=back_kb())


@dp.message(S.jvc)
async def jvc_do(m: Message, state: FSMContext):
    chat = parse_target(m.text)
    if isinstance(chat, str) and chat.startswith("https://t.me/+"):
        return await m.answer("⚠️ Invite links aren't supported here. Send the Chat ID.")
    await state.clear()
    await m.answer("⏳ Joining voice chat...")

    async def fn(app, calls):
        await app.get_chat(chat)   # cache peer
        await calls.play(chat)     # no stream = silent listener

    ok, fail, secs = await for_all(m.from_user.id, fn)
    log_act(m.from_user.id, "task", "Voice Join", str(chat), f"✅{ok} ❌{fail}")
    await m.answer(report(ok, fail, secs, "Joined VC"), reply_markup=kb(m.from_user.id))


@dp.message(ANY(B_LVC))
async def lvc_ask(m: Message, state: FSMContext):
    await state.set_state(S.lvc)
    await m.answer(hd("🔇", "Voice leave") + "Send the username or Chat ID.\n\nSend /cancel to abort.", reply_markup=back_kb())


@dp.message(S.lvc)
async def lvc_do(m: Message, state: FSMContext):
    chat = parse_target(m.text)
    await state.clear()

    async def fn(app, calls):
        await calls.leave_call(chat)

    ok, fail, secs = await for_all(m.from_user.id, fn)
    log_act(m.from_user.id, "task", "Voice Leave", str(chat), f"✅{ok} ❌{fail}")
    await m.answer(report(ok, fail, secs, "Left VC"), reply_markup=kb(m.from_user.id))


# ---------- Send Reaction ----------
@dp.message(ANY(B_REACT))
async def react_ask(m: Message, state: FSMContext):
    await state.set_state(S.react_link)
    await m.answer(hd("💫", "React") + "Send the post link, e.g. https://t.me/channel/123\n\nSend /cancel to abort.", reply_markup=back_kb())


@dp.message(S.react_link)
async def react_link(m: Message, state: FSMContext):
    post = parse_post(m.text)
    if not post:
        return await m.answer("❌ Invalid post link. Try again.")
    await state.update_data(post=post)
    await state.set_state(S.react_emoji)
    await m.answer("Now send the emoji to react with (e.g. 👍 💫 🔥).")


@dp.message(S.react_emoji)
async def react_do(m: Message, state: FSMContext):
    chat, mid = (await state.get_data())["post"]
    emoji = m.text.strip()
    await state.clear()

    async def fn(app, _):
        await app.get_chat(chat)
        await app.send_reaction(chat, mid, emoji)

    ok, fail, secs = await for_all(m.from_user.id, fn)
    log_act(m.from_user.id, "task", "React", f"{chat}/{mid} {emoji}", f"✅{ok} ❌{fail}")
    await m.answer(report(ok, fail, secs, "Reacted"), reply_markup=kb(m.from_user.id))


# ---------- Auto React / Auto View ----------
def auto_list(uid):
    r = ", ".join(f"{c} {h(e)}" for (o, c), e in AUTO_REACT.items() if o == uid) or "none"
    v = ", ".join(str(c) for (o, c) in AUTO_VIEW if o == uid) or "none"
    return f"⚡ Auto React: {r}\n👀 Auto View: {v}"


async def resolve_chat_id(uid, target):
    if isinstance(target, int):
        return target
    for _, (app, _c, _o) in user_clients(uid):
        return (await app.get_chat(target)).id
    raise RuntimeError("No clients logged in")


@dp.message(ANY(B_AR))
async def ar_ask(m: Message, state: FSMContext):
    await state.set_state(S.ar_chat)
    await m.answer(hd("⚡", "Auto react") + f"{auto_list(m.from_user.id)}\n\nSend the channel username/Chat ID to enable.\n"
                   "Add <code>off</code> after it to disable, e.g. <code>@channel off</code>.\n\nSend /cancel to abort.",
                   reply_markup=back_kb())


@dp.message(S.ar_chat)
async def ar_chat(m: Message, state: FSMContext):
    uid = m.from_user.id
    off = m.text.strip().lower().endswith(" off")
    try:
        chat = await resolve_chat_id(uid, parse_target(m.text.rsplit(" ", 1)[0] if off else m.text))
    except Exception as e:
        return await m.answer(f"❌ {h(e)}")
    if off:
        AUTO_REACT.pop((uid, chat), None)
        db.execute("DELETE FROM auto2 WHERE owner=? AND kind='react' AND chat_id=?", (uid, str(chat))); db.commit()
        await state.clear()
        log_act(uid, "task", "Auto React", str(chat), "off")
        return await m.answer("⚡ Auto React disabled.", reply_markup=kb(uid))
    await state.update_data(chat=chat)
    await state.set_state(S.ar_emoji)
    await m.answer("Send the emoji to auto-react with.")


@dp.message(S.ar_emoji)
async def ar_emoji(m: Message, state: FSMContext):
    uid = m.from_user.id
    chat = (await state.get_data())["chat"]
    emoji = m.text.strip()
    AUTO_REACT[(uid, chat)] = emoji
    db.execute("INSERT OR REPLACE INTO auto2 VALUES(?,'react',?,?)", (uid, str(chat), emoji)); db.commit()
    await state.clear()
    log_act(uid, "task", "Auto React", f"{chat} {emoji}", "on")
    await m.answer(f"✅ Auto React enabled for {chat} with {h(emoji)}", reply_markup=kb(uid))


@dp.message(ANY(B_AV))
async def av_ask(m: Message, state: FSMContext):
    await state.set_state(S.av_chat)
    await m.answer(hd("👀", "Auto view") + f"{auto_list(m.from_user.id)}\n\nSend the channel username/Chat ID to enable.\n"
                   "Add <code>off</code> to disable, e.g. <code>@channel off</code>.\n\nSend /cancel to abort.", reply_markup=back_kb())


@dp.message(S.av_chat)
async def av_chat(m: Message, state: FSMContext):
    uid = m.from_user.id
    off = m.text.strip().lower().endswith(" off")
    try:
        chat = await resolve_chat_id(uid, parse_target(m.text.rsplit(" ", 1)[0] if off else m.text))
    except Exception as e:
        return await m.answer(f"❌ {h(e)}")
    if off:
        AUTO_VIEW.discard((uid, chat))
        db.execute("DELETE FROM auto2 WHERE owner=? AND kind='view' AND chat_id=?", (uid, str(chat)))
    else:
        AUTO_VIEW.add((uid, chat))
        db.execute("INSERT OR REPLACE INTO auto2 VALUES(?,'view',?,'')", (uid, str(chat)))
    db.commit()
    await state.clear()
    log_act(uid, "task", "Auto View", str(chat), "off" if off else "on")
    await m.answer(f"👀 Auto View {'disabled' if off else 'enabled'} for {chat}", reply_markup=kb(uid))
    if off:
        for k in [k for k in SEEN if k[1] == chat and k[2] == "v"]:
            SEEN.pop(k, None)
        return
    ok = fail = 0
    err = ""
    for tg_id, (app, _c, _o) in user_clients(uid):
        try:
            SEEN[(tg_id, chat, "v")] = set(await recent_ids(app, chat))   # baseline: only NEW posts get views
            ok += 1
        except Exception as e:
            fail += 1
            err = repr(e)
    await m.answer(f"👀 Ready for new posts\n✅ OK: {ok} client(s)\n❌ Failed: {fail} client(s)"
                   + (f"\n\nError: {h(err)}\nMake sure these accounts joined the channel first." if err else
                      "\n\nEvery new post from now on will be viewed by all your clients."))


# ---------- Leave Channel ----------
@dp.message(ANY(B_LEAVE))
async def leave_ask(m: Message, state: FSMContext):
    await state.set_state(S.leave)
    await m.answer(hd("🚪", "Leave channel") + "Send the @username or Chat ID.\n\nAll your accounts will leave it.\n\nSend /cancel to abort.",
                   reply_markup=back_kb())


@dp.message(S.leave)
async def leave_do(m: Message, state: FSMContext):
    target = parse_target(m.text or "")
    await state.clear()
    if isinstance(target, str) and target.startswith("https://t.me/+"):
        return await m.answer("⚠️ Invite links aren't supported here. Send @username or Chat ID.", reply_markup=kb(m.from_user.id))
    await m.answer("⏳ Leaving...")

    async def fn(app, _):
        await app.get_chat(target)
        await app.leave_chat(target)

    ok, fail, secs = await for_all(m.from_user.id, fn)
    log_act(m.from_user.id, "task", "Leave Channel", str(target), f"✅{ok} ❌{fail}")
    await m.answer(report(ok, fail, secs, "Left"), reply_markup=kb(m.from_user.id))


# ---------- My Activity ----------
@dp.message(ANY(B_ACT))
async def my_activity(m: Message):
    uid = m.from_user.id
    total = count("SELECT COUNT(*) FROM activity WHERE user_id=?", uid)
    today = count("SELECT COUNT(*) FROM activity WHERE user_id=? AND ts>=?", uid, midnight())
    rows = db.execute("SELECT kind,action,detail,result,ts FROM activity WHERE user_id=? ORDER BY id DESC LIMIT 12", (uid,)).fetchall()
    body = "\n".join(fmt_row(r) for r in rows)
    await m.answer(hd("📜", "My activity", f"{total} commands · {today} today") +
                   (f"<blockquote expandable>{body}</blockquote>" if rows else "Nothing here yet."))


# ---------- Access / Admin panel ----------
@dp.message(ANY(B_ACC))
async def access(m: Message):
    uid = m.from_user.id
    if uid == OWNER_ID:
        n = db.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
        return await m.answer(hd("👑", "Access", "full control") + tree("👑 Role — <b>Admin</b>", f"📱 Accounts — <b>{n}</b>"))
    own = db.execute("SELECT COUNT(*) FROM clients WHERE owner=?", (uid,)).fetchone()[0]
    if is_vip(uid):
        adm = db.execute("SELECT COUNT(*) FROM clients WHERE owner=?", (OWNER_ID,)).fetchone()[0]
        return await m.answer(hd("💎", "Access", "vip unlocked") + tree("💎 Plan — <b>VIP</b>", f"📱 Your accounts — <b>{own}</b>", f"👑 Admin accounts — <b>{adm}</b>"))
    await m.answer(hd("🔐", "Access", "standard member") + tree("🔐 Plan — <b>Member</b>", f"📱 Your accounts — <b>{own}</b>") + "\n\nHave a VIP key? Unlock all admin accounts.", reply_markup=VIP_BTN)


@dp.message(F.text == B_VIPKEY)
async def vip_enter_msg(m: Message, state: FSMContext):
    await state.set_state(S.vip_key)
    await m.answer(hd("🔑", "VIP key", "paste the key from the admin") + "/cancel to abort.", reply_markup=back_kb())


@dp.callback_query(F.data == "vip_enter")
async def vip_enter(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.vip_key)
    await c.message.answer(hd("🔑", "VIP key", "paste the key from the admin") + "/cancel to abort.", reply_markup=back_kb())
    await c.answer()


@dp.message(S.vip_key)
async def vip_redeem(m: Message, state: FSMContext):
    key = (m.text or "").strip().upper()
    row = db.execute("SELECT status FROM access_keys WHERE key=?", (key,)).fetchone()
    try: await m.delete()   # hide the key from chat
    except Exception: pass
    if not row or row[0] != "active":
        return await m.answer("❌ Invalid or already used key. Send a valid key, or /cancel.")
    u = m.from_user
    db.execute("UPDATE access_keys SET status='used', used_by=?, used_at=? WHERE key=?", (u.id, int(time.time()), key))
    db.execute("INSERT OR REPLACE INTO vip_users(user_id,key) VALUES(?,?)", (u.id, key))
    db.execute("INSERT INTO users(user_id,name,username,status,joined,last_seen) VALUES(?,?,?,'approved',?,?) "
               "ON CONFLICT(user_id) DO UPDATE SET status='approved'", (u.id, u.full_name, u.username or "", int(time.time()), int(time.time())))
    db.commit()
    await state.clear()
    n = db.execute("SELECT COUNT(*) FROM clients WHERE owner=?", (OWNER_ID,)).fetchone()[0]
    log_act(u.id, "task", "VIP Key", "", "activated")
    await m.answer(hd("🎉", "VIP activated") + tree("💎 Plan — <b>VIP</b>", f"👑 Admin accounts — <b>{n}</b>"), reply_markup=kb(u.id))
    try:
        await m.bot.send_message(OWNER_ID, f"💎 Key <code>{key}</code> used by {h(u.full_name)} | 🆔 {u.id}")
    except Exception:
        pass


def count(sql, *a):
    return db.execute(sql, a).fetchone()[0]


def midnight():
    now = time.time() + TZ_H * 3600
    return int(now - now % 86400 - TZ_H * 3600)


STATUS_ICON = {"approved": "✅", "pending": "⏳", "rejected": "❌", "revoked": "🚫"}
PAGE, LOG_PAGE = 8, 10
DEAD = set()


def u_icon(uid, status):
    return "💎" if status == "approved" and is_vip(uid) else STATUS_ICON.get(status, "•")


def nav_row(prefix, page, pages):
    row = []
    if page > 0:
        row.append(("◀ Prev", f"{prefix}:{page - 1}"))
    row.append((f"{page + 1}/{pages}", "adm:noop"))
    if page < pages - 1:
        row.append(("Next ▶", f"{prefix}:{page + 1}"))
    return row


def onoff(k):
    return "ON" if setting(k) == "1" else "OFF"


# ----- panel -----
def v_panel():
    pend = count("SELECT COUNT(*) FROM users WHERE status='pending'")
    appr = count("SELECT COUNT(*) FROM users WHERE status='approved'")
    vips = count("SELECT COUNT(*) FROM vip_users")
    today = count("SELECT COUNT(*) FROM activity WHERE ts>=?", midnight())
    txt = (hd("👑", "Admin console", "control everything from here") +
           tree(f"👥 Users — <b>{appr}</b>", f"💎 VIP — <b>{vips}</b>", f"⏳ Pending — <b>{pend}</b>",
                f"📱 Accounts — <b>{count('SELECT COUNT(*) FROM clients')}</b>",
                f"🟢 Online — <b>{len(RUNNING)}</b>", f"⚡ Commands today — <b>{today}</b>"))
    return txt, ikb(
        [(f"📥 Requests · {pend}", "adm:req", "primary"), ("🗂 User Data", "adm:ud:0", "primary")],
        [("🔑 VIP Keys", "adm:keys", "success"), ("📱 Accounts", "adm:acc")],
        [("📊 Stats", "adm:st"), ("📣 Broadcast", "adm:bc")],
        [("🩺 Health", "adm:hc"), (f"🛠 Maintenance: {onoff('maint')}", "adm:mt", "danger" if setting("maint") == "1" else None)],
        [(f"📡 Live feed: {onoff('feed')}", "adm:fd", "success" if setting("feed") == "1" else None), ("🎨 Theme", "adm:th")])


def v_theme():
    sample = (hd("🎨", "Theme preview", "this is how every screen looks") +
              tree("👥 Users — <b>10</b>", "🟢 Online — <b>22</b>", "⚡ Today — <b>5</b>") +
              "\n\nTap a theme to apply it everywhere.")
    rows = [[(("✅ " if k == THEME else "") + v["name"], f"adm:th:{k}", "success" if k == THEME else "primary")]
            for k, v in THEMES.items()]
    rows.append([("🔙 Back", "adm:home")])
    return sample, ikb(*rows)


def v_req():
    rows = db.execute("SELECT user_id, name, username FROM users WHERE status='pending'").fetchall()
    txt = hd("📥", "Requests", "waiting for approval") + ("\n".join(f"• {h(n or '—')} | 🆔 {i}" for i, n, _u in rows) if rows else "No pending requests.")
    btns = [[(f"✅ {(n or str(i))[:14]}", f"adm:ap:{i}:r", "success"), (f"❌ {(n or str(i))[:14]}", f"adm:rj:{i}:r", "danger")]
            for i, n, _u in rows[:30]]
    return txt, ikb(*btns, [("🔙 Back", "adm:home")])


# ----- User data -----
def v_users(page=0):
    total = count("SELECT COUNT(*) FROM users WHERE status!='pending'")
    pages = max(1, -(-total // PAGE))
    page = max(0, min(page, pages - 1))
    rows = db.execute("SELECT user_id,name,status FROM users WHERE status!='pending' "
                      "ORDER BY COALESCE(last_seen,0) DESC LIMIT ? OFFSET ?", (PAGE, page * PAGE)).fetchall()
    btns = []
    for i, n, s in rows:
        c = count("SELECT COUNT(*) FROM activity WHERE user_id=?", i)
        btns.append([(f"{u_icon(i, s)} {(n or str(i))[:18]} · {c}", f"adm:u:{i}")])
    btns.append(nav_row("adm:ud", page, pages))
    btns.append([("🔙 Back", "adm:home")])
    txt = hd("🗂", "User data", f"{total} users · most recent first") + (
        "Tap a user to open their profile.\nThe number is how many commands they used." if total else "No users yet.")
    return txt, ikb(*btns)


def v_profile(uid):
    r = db.execute("SELECT name,username,status,joined,last_seen FROM users WHERE user_id=?", (uid,)).fetchone()
    if not r:
        return hd("❌", "User not found"), ikb([("🔙 Users", "adm:ud:0")])
    n, un, st, jn, ls = r
    total = count("SELECT COUNT(*) FROM activity WHERE user_id=?", uid)
    tasks = count("SELECT COUNT(*) FROM activity WHERE user_id=? AND kind='task'", uid)
    today = count("SELECT COUNT(*) FROM activity WHERE user_id=? AND ts>=?", uid, midnight())
    own = count("SELECT COUNT(*) FROM clients WHERE owner=?", uid)
    online = sum(1 for v in RUNNING.values() if v[2] == uid)
    uname = ("@" + h(un)) if un else "—"
    txt = hd("👤", h(n or uid), f"{STATUS_ICON.get(st, '•')} {st}") + tree(
        f"🆔 ID — <code>{uid}</code>", f"🔗 Username — {uname}",
        "💎 Plan — <b>VIP</b>" if is_vip(uid) else "🔐 Plan — <b>Member</b>",
        f"📱 Accounts — <b>{own}</b> (🟢 {online})",
        f"⌨️ Commands — <b>{total}</b> · today <b>{today}</b>", f"⚙️ Tasks run — <b>{tasks}</b>",
        f"🕒 Last active — <b>{ago(ls)}</b>", f"📅 Joined — <b>{fmt_ts(jn) if jn else '—'}</b>")
    toggle = ("🚫 Revoke", f"adm:rv:{uid}:u", "danger") if st == "approved" else ("✅ Approve", f"adm:ap:{uid}:u", "success")
    return txt, ikb(
        [(f"📜 Commands · {total}", f"adm:ul:{uid}:0", "primary"), ("📊 Breakdown", f"adm:us:{uid}")],
        [("📤 Export CSV", f"adm:ux:{uid}"), ("✉️ Message", f"adm:dm:{uid}")],
        [toggle, ("🧹 Clear log", f"adm:uc:{uid}")],
        [("🔙 Users", "adm:ud:0")])


def v_log(uid, page=0):
    total = count("SELECT COUNT(*) FROM activity WHERE user_id=?", uid)
    pages = max(1, -(-total // LOG_PAGE))
    page = max(0, min(page, pages - 1))
    rows = db.execute("SELECT kind,action,detail,result,ts FROM activity WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
                      (uid, LOG_PAGE, page * LOG_PAGE)).fetchall()
    body = "\n".join(fmt_row(r) for r in rows)
    txt = hd("📜", "Command history", f"{h(owner_name(uid))} · {total} total") + (
        f"<blockquote expandable>{body}</blockquote>\n\n▸ command used   ⚙️ task result" if rows else "No activity yet.")
    return txt, ikb(nav_row(f"adm:ul:{uid}", page, pages), [("📊 Breakdown", f"adm:us:{uid}"), ("👤 Profile", f"adm:u:{uid}")])


def v_break(uid):
    rows = db.execute("SELECT action, COUNT(*) c FROM activity WHERE user_id=? GROUP BY action ORDER BY c DESC LIMIT 15", (uid,)).fetchall()
    mx = rows[0][1] if rows else 1
    body = "\n".join(f"<code>{'█' * max(1, round(10 * c / mx))}</code> {h(a)} — <b>{c}</b>" for a, c in rows)
    txt = hd("📊", "Breakdown", f"{h(owner_name(uid))} · most used first") + (f"<blockquote>{body}</blockquote>" if rows else "No activity yet.")
    return txt, ikb([("📜 Commands", f"adm:ul:{uid}:0"), ("👤 Profile", f"adm:u:{uid}")])


# ----- stats / accounts / keys -----
def v_stats():
    total = count("SELECT COUNT(*) FROM activity")
    today = count("SELECT COUNT(*) FROM activity WHERE ts>=?", midnight())
    tasks = count("SELECT COUNT(*) FROM activity WHERE kind='task'")
    acts = db.execute("SELECT action, COUNT(*) c FROM activity GROUP BY action ORDER BY c DESC LIMIT 6").fetchall()
    usrs = db.execute("SELECT user_id, COUNT(*) c FROM activity GROUP BY user_id ORDER BY c DESC LIMIT 5").fetchall()
    up = int(time.time() - START_TS)
    txt = (hd("📊", "Bot stats", "live numbers") +
           tree(f"⌨️ Commands — <b>{total}</b>", f"📅 Today — <b>{today}</b>", f"⚙️ Tasks run — <b>{tasks}</b>",
                f"⏱ Uptime — <b>{up // 3600}h {up % 3600 // 60}m</b>") +
           "\n\n🔥 <b>Top commands</b>\n" + (tree(*[f"{h(a)} — <b>{c}</b>" for a, c in acts]) if acts else "—") +
           "\n\n🏆 <b>Top users</b>\n" + (tree(*[f"{h(owner_name(i))} — <b>{c}</b>" for i, c in usrs]) if usrs else "—"))
    return txt, ikb([("🔙 Back", "adm:home")])


def v_acc():
    rows = db.execute("SELECT id, name, owner FROM clients ORDER BY owner").fetchall()
    groups = {}
    for cid, n, o in rows:
        groups.setdefault(o, []).append((cid, n))
    parts = []
    for o, items in groups.items():
        parts.append(f"👥 {h(owner_name(o))} ({len(items)})\n" + "\n".join(
            f"   {'🟢' if c in RUNNING else '🔴'} {h(n)}" for c, n in items))
    body = "\n\n".join(parts) or "No accounts yet."
    if len(body) > 3500:
        body = body[:3500] + "\n…"
    return hd("📱", f"All accounts · {len(rows)}") + f"<blockquote expandable>{body}</blockquote>", ikb([("🔙 Back", "adm:home")])


def v_keys(new=None):
    rows = db.execute("SELECT key, status, used_by FROM access_keys ORDER BY created DESC LIMIT 30").fetchall()
    lines = []
    for k, st, ub in rows:
        if st == "active":
            lines.append(f"🟢 <code>{k}</code> — unused")
        elif st == "used":
            lines.append(f"💎 <code>{k}</code> — {h(owner_name(ub))} ({ub})")
        else:
            lines.append(f"⛔ <code>{k}</code> — revoked")
    head = f"🎉 <b>New key</b>  · tap to copy\n<code>{new}</code>\n\n" if new else ""
    txt = head + hd("🔑", "VIP keys", "one key = one person · unlocks ALL admin accounts") + ("\n".join(lines) or "No keys yet.")
    btns = [[("➕ Generate New Key", "adm:kg", "success")]]
    for k, st, _ub in rows[:20]:
        if st != "revoked":
            btns.append([(f"🧹 Revoke {k}", f"adm:kr:{k}", "danger")])
    btns.append([("🔙 Back", "adm:home")])
    return txt, ikb(*btns)


# ----- health -----
async def health_view():
    DEAD.clear()
    alive = 0
    for tid, (app, _c, _o) in list(RUNNING.items()):
        try:
            await asyncio.wait_for(app.get_me(), 15)
            alive += 1
        except Exception:
            DEAD.add(tid)
    names = dict(db.execute("SELECT id, name FROM clients").fetchall())
    off = [t for t in names if t not in RUNNING]
    bad = [f"💀 {h(names.get(t, t))}" for t in DEAD] + [f"🔴 {h(names[t])}" for t in off]
    txt = hd("🩺", "Health check", "live ping of every account") + tree(
        f"🟢 Healthy — <b>{alive}</b>", f"💀 Not responding — <b>{len(DEAD)}</b>", f"🔴 Offline — <b>{len(off)}</b>")
    if bad:
        txt += f"\n\n<blockquote expandable>{chr(10).join(bad[:60])}</blockquote>"
    return txt, ikb([("♻️ Reconnect problem accounts", "adm:rc", "primary")], [("🔙 Back", "adm:home")])


async def reconnect_offline():
    ok = fail = 0
    for tg_id, session, owner in db.execute("SELECT id, session, owner FROM clients").fetchall():
        if tg_id in RUNNING and tg_id not in DEAD:
            continue
        try:
            await start_client(fernet.decrypt(session).decode(), tg_id, owner or OWNER_ID)
            ok += 1
        except Exception as e:
            fail += 1
            log.warning("reconnect %s: %r", tg_id, e)
    DEAD.clear()
    return ok, fail


async def show(c: CallbackQuery, view):
    text, markup = view
    try:
        await c.message.edit_text(text, reply_markup=markup)
    except Exception:
        await c.message.answer(text, reply_markup=markup)


@dp.message(ANY(B_ADMIN))
async def admin_panel(m: Message):
    if m.from_user.id != OWNER_ID:
        return await m.answer("⛔ Admin only.")
    txt, k = v_panel()
    await m.answer(txt, reply_markup=k)


@dp.message(Command("theme"))
async def theme_cmd(m: Message):
    if m.from_user.id != OWNER_ID:
        return
    t, k = v_theme()
    await m.answer(t, reply_markup=k)


@dp.callback_query(F.data.startswith("adm:"))
async def admin_cb(c: CallbackQuery, state: FSMContext):
    try:
        await _admin_cb(c, state)
    except Exception as e:
        log.exception("admin_cb failed")
        try:
            await c.answer(f"⚠️ {type(e).__name__}: {e}"[:190], show_alert=True)
        except Exception:
            pass


async def _admin_cb(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != OWNER_ID:
        return await c.answer("⛔ Admin only", show_alert=True)
    parts = c.data.split(":")
    act = parts[1]

    if act in ("ap", "rj", "rv"):
        uid = int(parts[2])
        back = parts[3] if len(parts) > 3 else ""
        status = {"ap": "approved", "rj": "rejected", "rv": "revoked"}[act]
        db.execute("UPDATE users SET status=? WHERE user_id=?", (status, uid))
        if act != "ap":
            db.execute("DELETE FROM vip_users WHERE user_id=?", (uid,))
        db.commit()
        msg = {"ap": "✅ Your access request has been approved!\n\nSend /start to begin.",
               "rj": "❌ Your access request was rejected by the bot owner.",
               "rv": "🚫 Your access has been revoked by the bot owner."}[act]
        try:
            await c.bot.send_message(uid, msg, reply_markup=kb(uid) if act == "ap" else locked_kb())
        except Exception:
            pass
        head = c.message.text or ""
        if back == "r":
            await show(c, v_req())
        elif back == "u":
            await show(c, v_profile(uid))
        elif head.startswith("🔔"):
            result = {"ap": "✅ Accepted", "rj": "❌ Rejected", "rv": "🚫 Revoked"}[act]
            await c.message.edit_text(f"{h(head)}\n\n{result}")
        else:
            await show(c, v_users())
        return await c.answer("Done")

    if act == "kg":
        await show(c, v_keys(new_key()))
        return await c.answer("Key created")
    if act == "kr":
        key = parts[2]
        r = db.execute("SELECT used_by FROM access_keys WHERE key=?", (key,)).fetchone()
        db.execute("UPDATE access_keys SET status='revoked' WHERE key=?", (key,))
        if r and r[0]:
            db.execute("DELETE FROM vip_users WHERE user_id=?", (r[0],))
            db.execute("UPDATE users SET status='revoked' WHERE user_id=?", (r[0],))
            try: await c.bot.send_message(r[0], "🚫 Your VIP access has been revoked by the bot owner.", reply_markup=locked_kb())
            except Exception: pass
        db.commit()
        await show(c, v_keys())
        return await c.answer("Key revoked")

    # ----- user data -----
    if act == "ud":
        await show(c, v_users(int(parts[2])))
    elif act == "u":
        await show(c, v_profile(int(parts[2])))
    elif act == "ul":
        await show(c, v_log(int(parts[2]), int(parts[3]) if len(parts) > 3 else 0))
    elif act == "us":
        await show(c, v_break(int(parts[2])))
    elif act == "ux":
        uid = int(parts[2])
        rows = db.execute("SELECT ts,kind,action,detail,result FROM activity WHERE user_id=? ORDER BY id", (uid,)).fetchall()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["time", "type", "command", "detail", "result"])
        for ts, k, a, d, r in rows:
            w.writerow([fmt_ts(ts), k, a, d, r])
        await c.message.answer_document(BufferedInputFile(buf.getvalue().encode("utf-8-sig"), filename=f"activity_{uid}.csv"),
                                        caption=f"📤 {h(owner_name(uid))} · {len(rows)} rows")
        return await c.answer("Exported")
    elif act == "uc":
        uid = int(parts[2])
        await show(c, (hd("🧹", "Clear log") + f"Delete the whole command history of <b>{h(owner_name(uid))}</b>?",
                       ikb([("✅ Yes, clear", f"adm:ucy:{uid}", "danger"), ("🔙 Cancel", f"adm:u:{uid}")])))
    elif act == "ucy":
        uid = int(parts[2])
        db.execute("DELETE FROM activity WHERE user_id=?", (uid,))
        db.commit()
        await show(c, v_profile(uid))
        return await c.answer("Cleared")
    elif act == "dm":
        uid = int(parts[2])
        await state.set_state(S.dm)
        await state.update_data(target=uid)
        await c.message.answer(hd("✉️", "Message user", h(owner_name(uid))) + "Send the message (text, photo, anything).\n\n/cancel to abort.",
                               reply_markup=back_kb())
        return await c.answer()

    # ----- tools -----
    elif act == "bc":
        n = count("SELECT COUNT(*) FROM users WHERE status='approved'")
        await state.set_state(S.bcast)
        await c.message.answer(hd("📣", "Broadcast", f"goes to {n} approved users") + "Send the message to broadcast (text, photo, anything).\n\n/cancel to abort.",
                               reply_markup=back_kb())
        return await c.answer()
    elif act == "th":
        if len(parts) > 2:
            if parts[2] == THEME:
                return await c.answer("Already active")
            set_theme(parts[2])
        await show(c, v_theme())
    elif act == "st":
        await show(c, v_stats())
    elif act == "hc":
        await c.answer("🩺 Checking accounts…")
        return await show(c, await health_view())
    elif act == "rc":
        await c.answer("♻️ Reconnecting…")
        ok, fail = await reconnect_offline()
        await show(c, await health_view())
        return await c.message.answer(f"♻️ Reconnected: ✅ {ok} · ❌ {fail}")
    elif act == "mt":
        set_setting("maint", "0" if setting("maint") == "1" else "1")
        await show(c, v_panel())
    elif act == "fd":
        set_setting("feed", "0" if setting("feed") == "1" else "1")
        await show(c, v_panel())
    else:
        views = {"keys": v_keys, "home": v_panel, "req": v_req, "acc": v_acc}
        if act in views:
            await show(c, views[act]())
    await c.answer()


# ---------- broadcast / direct message ----------
@dp.message(S.bcast)
async def bcast_do(m: Message, state: FSMContext):
    if m.from_user.id != OWNER_ID:
        return
    await state.clear()
    ids = [r[0] for r in db.execute("SELECT user_id FROM users WHERE status='approved'").fetchall()]
    await m.answer(f"📣 Sending to {len(ids)} users…")
    ok = fail = 0
    t0 = time.time()
    for i in ids:
        try:
            try:
                await m.copy_to(i)
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
                await m.copy_to(i)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.06)
    log_act(OWNER_ID, "task", "Broadcast", f"{ok}/{len(ids)}")
    await m.answer(report(ok, fail, max(1, int(time.time() - t0)), "Broadcast"), reply_markup=kb(OWNER_ID))


@dp.message(S.dm)
async def dm_do(m: Message, state: FSMContext):
    if m.from_user.id != OWNER_ID:
        return
    uid = (await state.get_data())["target"]
    await state.clear()
    try:
        await m.copy_to(uid)
        await m.answer("✅ Message sent.", reply_markup=kb(OWNER_ID))
    except Exception as e:
        await m.answer(f"❌ Could not send: {h(e)}", reply_markup=kb(OWNER_ID))


# ---------- boot ----------
async def main():
    global BOT
    load_auto()
    for tg_id, session, owner in db.execute("SELECT id, session, owner FROM clients").fetchall():
        try:
            await start_client(fernet.decrypt(session).decode(), tg_id, owner or OWNER_ID)
            log.info("client %s started", tg_id)
        except Exception as e:
            log.warning("client %s failed to start: %r", tg_id, e)
    asyncio.create_task(watcher())
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    BOT = bot
    bot.session.middleware(PremiumMW())   # premium emoji can be added live with /setemoji
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
