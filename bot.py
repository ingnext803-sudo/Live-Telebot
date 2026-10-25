#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LIVE JOIN MANAGER BOT  (Owner / Admin / Client + Key system)
- Control bot : aiogram 3  (inline panels, coloured buttons, premium emoji)
- Accounts    : Pyrogram (kurigram) sessions + py-tgcalls (voice chat join)
Run:  python bot.py
"""
import asyncio, os, re, time, random, sqlite3, wave, logging, html, secrets, string
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet
import phonenumbers
from phonenumbers import geocoder

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
                           ReplyKeyboardMarkup, KeyboardButton)

from pyrogram import Client
from pyrogram import errors as perr
from pyrogram.raw import functions, types as rtypes
from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream

# ============================== CONFIG ==============================
# >>>>>>>>>>>>>>>>>>>>  YAHAN APNI DETAILS DAALO  <<<<<<<<<<<<<<<<<<<<
BOT_TOKEN = "8911012535:AAGHVSwv3KqH9LUUcvyt7rmOOQ_hwEt7zrE"          # @BotFather se mila token (quotes ke andar)
API_ID = 30954067                                # my.telegram.org se API ID (sirf number, quotes nahi)
API_HASH = "14b365d5ed5bc10b0379a4c9a31be351"            # my.telegram.org se API Hash (quotes ke andar)
OWNER_IDS = [8390645989]                   # Aapka Chat ID (@userinfobot se mil jata hai). Multiple: [111, 222]
# >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>

TZ_HOURS = 6                 # Bangladesh = UTC+6
DEFAULT_DAILY_LIMIT = 20     # client ke liye din mein max Live ON (0 = unlimited)
JOIN_DELAY = (1.5, 3.5)      # har account join ke beech random gap (sec) - FloodWait se bachne ke liye
AUTO_POLL = 15               # auto mode check interval (sec)
KEEPALIVE = 480              # silence stream refresh (sec)
DB_FILE = "livebot.db"
SILENCE = "silence.wav"
# ====================================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
LOG = logging.getLogger("livebot")

DUR = {"1h": ("1 Hour", 3600), "1d": ("1 Day", 86400), "7d": ("7 Days", 7 * 86400),
       "30d": ("1 Month", 30 * 86400), "365d": ("1 Year", 365 * 86400)}

# ------------------------------ helpers ------------------------------
def now(): return int(time.time())
def h(s): return html.escape(str(s if s is not None else ""))

def fmt_ts(ts):
    if not ts: return "-"
    return (datetime.fromtimestamp(ts, timezone.utc) + timedelta(hours=TZ_HOURS)).strftime("%d %b %Y, %I:%M %p")

def fmt_left(sec):
    if sec <= 0: return "Expired"
    d, r = divmod(int(sec), 86400); hh, r = divmod(r, 3600); mm = r // 60
    if d: return f"{d}d {hh}h"
    if hh: return f"{hh}h {mm}m"
    return f"{mm}m"

def today_start():
    lt = datetime.now(timezone.utc) + timedelta(hours=TZ_HOURS)
    mid = lt.replace(hour=0, minute=0, second=0, microsecond=0)
    return int((mid - timedelta(hours=TZ_HOURS)).timestamp())

def flag(code):
    if not code or len(code) != 2: return "🌍"
    return "".join(chr(0x1F1E6 + ord(c) - 65) for c in code.upper())

# ------------------------------ database ------------------------------
db = sqlite3.connect(DB_FILE, check_same_thread=False)
db.row_factory = sqlite3.Row
def q(sql, *a): return db.execute(sql, a).fetchall()
def q1(sql, *a): return db.execute(sql, a).fetchone()
def ex(sql, *a):
    cur = db.execute(sql, a); db.commit(); return cur

def init_db():
    db.executescript("""
    create table if not exists users(uid integer primary key, name text, username text, role text default 'client',
        first_seen integer, last_seen integer, paused integer default 0, daily_limit integer default 20);
    create table if not exists keys(id integer primary key autoincrement, key text unique, kind text, chat_id integer,
        ch_limit integer, acc_limit integer, dur integer, created_by integer, created_at integer,
        redeemed_by integer, redeemed_at integer, expires_at integer, status text default 'unused');
    create table if not exists accounts(id integer primary key autoincrement, owner_uid integer, phone text, country text,
        session text, name text, status text default 'ok', added_by integer, added_at integer);
    create table if not exists channels(id integer primary key autoincrement, owner_uid integer, ref text, title text,
        auto integer default 0, added_at integer);
    create table if not exists activity(id integer primary key autoincrement, uid integer, kind text, text text,
        n integer default 0, ts integer);
    create table if not exists settings(k text primary key, v text);
    create table if not exists emoji(ch text primary key, eid text);
    """)
    db.commit()

def setting(k, d=""):
    r = q1("select v from settings where k=?", k); return r["v"] if r else d
def set_setting(k, v): ex("insert or replace into settings(k,v) values(?,?)", k, str(v))
def log_act(uid, kind, text, n=0): ex("insert into activity(uid,kind,text,n,ts) values(?,?,?,?,?)", uid, kind, text, n, now())

# session encryption
if not os.path.exists("secret.key"):
    with open("secret.key", "wb") as f: f.write(Fernet.generate_key())
FER = Fernet(open("secret.key", "rb").read())
def enc(s): return FER.encrypt(s.encode()).decode()
def dec(s): return FER.decrypt(s.encode()).decode()

# ------------------------------ premium emoji ------------------------------
PE = {}            # normalized emoji char -> custom emoji id
EMOJI_RE = None

def norm(e): return e.replace("\ufe0f", "")

def load_pe():
    global EMOJI_RE
    PE.clear()
    for r in q("select ch,eid from emoji"): PE[r["ch"]] = r["eid"]
    keys = sorted(PE, key=len, reverse=True)
    EMOJI_RE = re.compile("(" + "|".join(re.escape(k) for k in keys) + ")\ufe0f?") if keys else None

def pe(text, plain=0):
    if plain >= 1 or not EMOJI_RE: return text
    return EMOJI_RE.sub(lambda m: f'<tg-emoji emoji-id="{PE[m.group(1)]}">{m.group(1)}</tg-emoji>', text)

# ------------------------------ ui helpers ------------------------------
def btn(text, data=None, style=None, url=None): return {"text": text, "data": data, "style": style, "url": url}

def build_kb(rows, plain=0):
    out = []
    for row in rows:
        r = []
        for b in row:
            text, extra = b["text"], {}
            if plain == 0 and EMOJI_RE:
                m = EMOJI_RE.match(text)
                if m:
                    extra["icon_custom_emoji_id"] = PE[m.group(1)]
                    text = text[m.end():].strip() or text
            if plain <= 1 and b.get("style"): extra["style"] = b["style"]
            if b.get("url"): r.append(InlineKeyboardButton(text=text, url=b["url"], **extra))
            else: r.append(InlineKeyboardButton(text=text, callback_data=b["data"], **extra))
        out.append(r)
    return InlineKeyboardMarkup(inline_keyboard=out)

bot: Bot = None
dp = Dispatcher()

async def show(ev, text, rows=None, new=False):
    is_cb = isinstance(ev, CallbackQuery)
    msg = ev.message if is_cb else ev
    attempts = [(0, True), (1, True), (2, True), (2, False)] if (is_cb and not new) else [(0, False), (1, False), (2, False)]
    for plain, edit in attempts:
        t = pe(text, plain)
        mk = build_kb(rows, plain) if rows is not None else None
        try:
            if edit: await msg.edit_text(t, reply_markup=mk)
            else: await msg.answer(t, reply_markup=mk)
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e).lower(): return
            LOG.warning("show retry (%s): %s", plain, e)
    LOG.error("show failed completely")

def reply_kb(uid, plain=0):
    def kb_(text, emo, style):
        extra = {}
        if plain == 0 and norm(emo) in PE: extra["icon_custom_emoji_id"] = PE[norm(emo)]
        if plain <= 1: extra["style"] = style
        return KeyboardButton(text=text, **extra)
    row = [kb_("User Panel", "👤", "primary")]
    if is_staff(uid): row.append(kb_("Admin Panel", "👑", "danger"))
    return ReplyKeyboardMarkup(keyboard=[row], resize_keyboard=True)

async def send_rk(m, text):
    for plain in (0, 1, 2):
        try:
            await m.answer(pe(text, plain), reply_markup=reply_kb(m.from_user.id, plain)); return
        except TelegramBadRequest as e:
            LOG.warning("rk retry: %s", e)

async def notify(uids, text):
    for u in uids:
        try: await bot.send_message(u, pe(text))
        except Exception: pass

# ------------------------------ roles & access ------------------------------
def role_of(uid):
    if uid in OWNER_IDS: return "owner"
    r = q1("select role from users where uid=?", uid)
    return r["role"] if r else "client"

def is_staff(uid): return role_of(uid) in ("owner", "admin")
def is_owner(uid): return uid in OWNER_IDS

def license_of(uid):
    return q1("select * from keys where redeemed_by=? and kind='client' and expires_at>? order by expires_at desc limit 1", uid, now())

def can_use(uid):
    if is_staff(uid): return True, ""
    lic = license_of(uid)
    if not lic:
        return False, ("expired" if q1("select 1 from keys where redeemed_by=?", uid) else "nokey")
    if setting("global_off") == "1": return False, "off"
    u = q1("select paused from users where uid=?", uid)
    if u and u["paused"]: return False, "paused"
    return True, ""

def limits(uid):
    if is_staff(uid): return None, None
    lic = license_of(uid)
    return (lic["ch_limit"], lic["acc_limit"]) if lic else (0, 0)

def daily_limit(uid):
    if is_staff(uid): return 0
    u = q1("select daily_limit from users where uid=?", uid)
    return u["daily_limit"] if u else DEFAULT_DAILY_LIMIT

def allowed(viewer, target): return viewer == target or is_staff(viewer)

def touch(u):
    if q1("select 1 from users where uid=?", u.id):
        ex("update users set name=?, username=?, last_seen=? where uid=?", u.full_name, u.username, now(), u.id)
    else:
        ex("insert into users(uid,name,username,first_seen,last_seen,role,daily_limit) values(?,?,?,?,?,?,?)",
           u.id, u.full_name, u.username, now(), now(), "client", DEFAULT_DAILY_LIMIT)

def n_acc(uid, ok_only=False):
    return q1("select count(*) c from accounts where owner_uid=?" + (" and status='ok'" if ok_only else ""), uid)["c"]
def n_ch(uid): return q1("select count(*) c from channels where owner_uid=?", uid)["c"]
def lives_today(uid):
    r = q1("select count(*) c, coalesce(sum(n),0) j from activity where uid=? and kind='live_on' and ts>=?", uid, today_start())
    return r["c"], r["j"]

def gen_key():
    while True:
        k = "LV-" + "-".join("".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(4)) for _ in range(3))
        if not q1("select 1 from keys where key=?", k): return k

# ------------------------------ userbot runtime ------------------------------
RT = {}                                  # account id -> {"c": Client, "call": PyTgCalls}
RT_LOCKS = defaultdict(asyncio.Lock)
LIVE_LOCKS = defaultdict(asyncio.Lock)
LIVE = {}                                # channel id -> {"since","accs","cid","by"}
CHAT_CACHE = {}
AUTO_SUPPRESS = set()
ST = {}                                  # per-user step state
PEND = {}                                # pending login clients

def make_silence():
    if os.path.exists(SILENCE): return
    with wave.open(SILENCE, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
        chunk = b"\x00\x00" * 8000
        for _ in range(600): w.writeframes(chunk)

def silence_stream():
    return MediaStream(SILENCE, video_flags=MediaStream.Flags.IGNORE)

async def get_rt(acc):
    aid = acc["id"]
    if aid in RT: return RT[aid]
    async with RT_LOCKS[aid]:
        if aid in RT: return RT[aid]
        c = Client(f"acc_{aid}", api_id=API_ID, api_hash=API_HASH, session_string=dec(acc["session"]), in_memory=True)
        await c.start()
        call = PyTgCalls(c)
        await call.start()
        RT[aid] = {"c": c, "call": call}
        return RT[aid]

async def stop_rt(aid):
    rt = RT.pop(aid, None)
    if rt:
        try: await rt["c"].stop()
        except Exception: pass

def mark_dead(aid):
    ex("update accounts set status='dead' where id=?", aid)
    RT.pop(aid, None)

async def resolve_chat(c, acc_id, ch):
    key = (acc_id, ch["id"])
    if key in CHAT_CACHE: return CHAT_CACHE[key]
    ref = ch["ref"]
    target = int(ref) if re.fullmatch(r"-?\d+", ref) else ref
    chat = None
    if isinstance(target, str):
        try: chat = await c.join_chat(target)
        except perr.UserAlreadyParticipant: pass
        except perr.FloodWait: raise
        except Exception as e: LOG.warning("join_chat %s: %s", target, e)
    if chat is None: chat = await c.get_chat(target)
    CHAT_CACHE[key] = chat.id
    if getattr(chat, "title", None) and chat.title != ch["title"]:
        ex("update channels set title=? where id=?", chat.title, ch["id"])
    return chat.id

async def call_active(c, cid):
    peer = await c.resolve_peer(cid)
    if isinstance(peer, rtypes.InputPeerChannel):
        full = await c.invoke(functions.channels.GetFullChannel(
            channel=rtypes.InputChannel(channel_id=peer.channel_id, access_hash=peer.access_hash)))
    else:
        full = await c.invoke(functions.messages.GetFullChat(chat_id=peer.chat_id))
    return bool(getattr(full.full_chat, "call", None))

async def create_call(c, cid):
    peer = await c.resolve_peer(cid)
    await c.invoke(functions.phone.CreateGroupCall(peer=peer, random_id=random.randint(1, 2 ** 31 - 1)))

async def join_one(acc, ch, cid=None):
    try:
        rt = await get_rt(acc)
        cid = cid or await resolve_chat(rt["c"], acc["id"], ch)
        await rt["call"].play(cid, silence_stream())
        return True, ""
    except perr.Unauthorized:
        mark_dead(acc["id"]); return False, "session dead"
    except perr.FloodWait as e:
        return False, f"FloodWait {e.value}s"
    except Exception as e:
        return False, type(e).__name__

async def probe(ch, accs):
    """koi ek working account se (chat_id, call_active) nikalo"""
    err = "no account"
    for acc in accs:
        try:
            rt = await get_rt(acc)
            cid = await resolve_chat(rt["c"], acc["id"], ch)
            return acc, cid, await call_active(rt["c"], cid), ""
        except perr.Unauthorized:
            mark_dead(acc["id"]); err = "session dead"
        except Exception as e:
            err = type(e).__name__
    return None, None, False, err

async def live_on(ch, actor, auto=False):
    chid, t = ch["id"], ch["owner_uid"]
    async with LIVE_LOCKS[chid]:
        if chid in LIVE: return "ℹ️ Live pehle se ON hai."
        accs = q("select * from accounts where owner_uid=? and status='ok' order by id", t)
        if not accs: return "❌ Koi working account nahi hai. Pehle account add karo."
        acc0, cid, active, err = await probe(ch, accs)
        if acc0 is None: return f"❌ Channel/Group access nahi mila ({err}). Account channel ka member hona chahiye."
        if not active:
            created = False
            for acc in accs:
                try:
                    rt = await get_rt(acc)
                    c2 = await resolve_chat(rt["c"], acc["id"], ch)
                    await create_call(rt["c"], c2); created = True; await asyncio.sleep(2); break
                except Exception as e:
                    LOG.info("create_call fail acc %s: %s", acc["id"], type(e).__name__)
            if not created:
                return ("⚠️ Voice chat/Live active nahi hai aur bot start nahi kar paya "
                        "(account ko admin + Manage Live Streams right chahiye). Manual start karke phir ON dabao.")
        joined, fails = set(), []
        for i, acc in enumerate(accs):
            ok, e = await join_one(acc, ch, cid)
            if ok: joined.add(acc["id"])
            else: fails.append(f"{acc['phone']}: {e}")
            if i < len(accs) - 1: await asyncio.sleep(random.uniform(*JOIN_DELAY))
        LIVE[chid] = {"since": now(), "accs": joined, "cid": cid, "by": actor}
        log_act(t, "live_on", f"{ch['title']}: {len(joined)}/{len(accs)} joined" + (" (auto)" if auto else ""), len(joined))
        if t not in OWNER_IDS:
            u = q1("select name,username from users where uid=?", t)
            await notify(OWNER_IDS, f"🔴 <b>Live ON</b>{' (auto)' if auto else ''}\n👤 {h(u['name']) if u else t} · <code>{t}</code>\n"
                                    f"📢 {h(ch['title'])}\n✅ {len(joined)}/{len(accs)} joined")
        msg = f"✅ <b>{len(joined)}/{len(accs)}</b> accounts live mein join ho gaye."
        if fails: msg += "\n⚠️ Fail:\n" + "\n".join(f"• <code>{h(x)}</code>" for x in fails[:5])
        return msg

async def live_off(chid, reason=""):
    st = LIVE.pop(chid, None)
    if not st: return 0
    n = 0
    for aid in list(st["accs"]):
        rt = RT.get(aid)
        if rt:
            try: await rt["call"].leave_call(st["cid"]); n += 1
            except Exception: pass
            await asyncio.sleep(0.3)
    ch = q1("select * from channels where id=?", chid)
    if ch: log_act(ch["owner_uid"], "live_off", f"{ch['title']}: {n} left {reason}", n)
    return n

async def stop_user_lives(uid):
    for ch in q("select id from channels where owner_uid=?", uid):
        if ch["id"] in LIVE: await live_off(ch["id"], "(stopped by admin)")

async def auto_loop():
    while True:
        await asyncio.sleep(AUTO_POLL)
        try:
            for ch in q("select * from channels where auto=1"):
                t, chid = ch["owner_uid"], ch["id"]
                ok, _ = can_use(t)
                if not ok:
                    if chid in LIVE: await live_off(chid, "(access off)")
                    continue
                accs = q("select * from accounts where owner_uid=? and status='ok' order by id", t)
                if not accs: continue
                acc0, cid, active, err = await probe(ch, accs[:3])
                if acc0 is None: continue
                if active and chid not in LIVE and chid not in AUTO_SUPPRESS:
                    lim = daily_limit(t)
                    if lim and lives_today(t)[0] >= lim: continue
                    await live_on(ch, t, auto=True)
                elif not active:
                    AUTO_SUPPRESS.discard(chid)
                    if chid in LIVE: await live_off(chid, "(live ended)")
        except Exception:
            LOG.exception("auto_loop")

async def keepalive_loop():
    while True:
        await asyncio.sleep(KEEPALIVE)
        for chid, st in list(LIVE.items()):
            for aid in list(st["accs"]):
                rt = RT.get(aid)
                if rt:
                    try: await rt["call"].play(st["cid"], silence_stream())
                    except Exception: pass
                    await asyncio.sleep(0.5)

# ============================== SCREENS ==============================
DENY = {
    "nokey": "🔒 <b>Access nahi hai</b>\n\nApna <b>Key</b> yahan bhejo (format: <code>LV-XXXX-XXXX-XXXX</code>).\n"
             "Key lene ke liye admin ko apna Chat ID do:\n🆔 <code>{uid}</code>",
    "expired": "⏰ <b>Aapki key expire ho gayi.</b>\nNayi key ke liye admin se contact karo, phir key yahan bhejo.\n🆔 <code>{uid}</code>",
    "off": "🌙 <b>Bot abhi OFF hai.</b>\nThodi der baad dubara try karo.",
    "paused": "⏸ <b>Aapka access abhi band hai</b> (key safe hai).\nAdmin ke on karne ke baad chalega.",
}

async def deny(ev, why, uid):
    await show(ev, DENY.get(why, "❌ Access nahi hai.").format(uid=uid), [[btn("🔄 Refresh", "u:home")]])

def cancel_flow(uid):
    ST.pop(uid, None)
    p = PEND.pop(uid, None)
    if p:
        asyncio.create_task(_safe_disc(p["c"]))

async def _safe_disc(c):
    try: await c.disconnect()
    except Exception: pass

def back_for(viewer, t): return "u:home" if viewer == t else f"a:c:{t}"
CANCEL = [[btn("❌ Cancel", "x:cancel", "danger")]]

async def u_home(ev):
    uid = ev.from_user.id
    ok, why = can_use(uid)
    if not ok: return await deny(ev, why, uid)
    role = role_of(uid)
    cl, al = limits(uid)
    lives, joins = lives_today(uid)
    dl = daily_limit(uid)
    L = ["🚀 <b>Live Join Manager</b>", "", f"👤 {h(ev.from_user.full_name)} · <code>{uid}</code>"]
    if role == "owner": L.append("👑 <b>Owner</b> · unlimited")
    elif role == "admin": L.append("👑 <b>Admin</b> · unlimited")
    else:
        lic = license_of(uid)
        L.append(f"⏳ Key valid: <b>{fmt_left(lic['expires_at'] - now())}</b> ({fmt_ts(lic['expires_at'])})")
    L.append("")
    L.append(f"📱 Accounts: <b>{n_acc(uid)}</b>" + (f" / {al}" if al is not None else ""))
    L.append(f"📢 Channels: <b>{n_ch(uid)}</b>" + (f" / {cl}" if cl is not None else ""))
    L.append(f"📊 Aaj Live ON: <b>{lives}</b>" + (f" / {dl}" if dl else "") + f" · joins: {joins}")
    rows = [[btn("📱 My Accounts", f"w:acc:{uid}", "primary"), btn("📢 Channels & Live", f"w:ch:{uid}", "success")],
            [btn("🔄 Refresh", "u:home")]]
    if is_staff(uid): rows.append([btn("👑 Admin Dashboard", "a:home", "danger")])
    await show(ev, "\n".join(L), rows)

# ------------------------------ accounts ------------------------------
async def w_acc(ev, v, t):
    if not allowed(v, t): return
    accs = q("select * from accounts where owner_uid=? order by id", t)
    L = [f"📱 <b>Accounts</b> ({len(accs)})", ""]
    rows, row = [], []
    for a in accs:
        ic = "🟢" if a["status"] == "ok" else "🔴"
        L.append(f"{ic} <code>{h(a['phone'])}</code> · {h(a['country'])}")
        row.append(btn(f"{ic} {a['phone']}", f"w:av:{a['id']}"))
        if len(row) == 2: rows.append(row); row = []
    if row: rows.append(row)
    if not accs: L.append("Abhi koi account add nahi hai.")
    rows.append([btn("➕ Add Account", f"w:aadd:{t}", "success")])
    rows.append([btn("⬅️ Back", back_for(v, t))])
    await show(ev, "\n".join(L), rows)

async def w_av(ev, v, aid):
    a = q1("select * from accounts where id=?", aid)
    if not a or not allowed(v, a["owner_uid"]): return
    adder = q1("select name from users where uid=?", a["added_by"])
    L = ["📱 <b>Account Details</b>", "",
         f"☎️ Number: <code>{h(a['phone'])}</code>", f"🌍 Country: {h(a['country'])}",
         f"👤 Name: {h(a['name'])}", f"📶 Status: {'🟢 Working' if a['status']=='ok' else '🔴 Dead / logout'}",
         f"📅 Added: {fmt_ts(a['added_at'])}", f"➕ Added by: {h(adder['name']) if adder else a['added_by']}"]
    rows = [[btn("🗑 Delete Account", f"w:ad:{aid}", "danger")], [btn("⬅️ Back", f"w:acc:{a['owner_uid']}")]]
    await show(ev, "\n".join(L), rows)

async def w_ad(ev, v, aid, confirm):
    a = q1("select * from accounts where id=?", aid)
    if not a or not allowed(v, a["owner_uid"]): return
    t = a["owner_uid"]
    if not confirm:
        return await show(ev, f"⚠️ <code>{h(a['phone'])}</code> delete karna hai?",
                          [[btn("✅ Haan, delete", f"w:ado:{aid}", "danger"), btn("❌ Nahi", f"w:av:{aid}")]])
    for chid, st in list(LIVE.items()):
        if aid in st["accs"]:
            rt = RT.get(aid)
            if rt:
                try: await rt["call"].leave_call(st["cid"])
                except Exception: pass
            st["accs"].discard(aid)
    await stop_rt(aid)
    ex("delete from accounts where id=?", aid)
    log_act(t, "acc_del", a["phone"])
    await w_acc(ev, v, t)

async def w_aadd(ev, v, t):
    if not allowed(v, t): return
    cl, al = limits(t)
    if al is not None and not is_staff(v) and n_acc(t) >= al:
        return await ev.answer(f"❌ Account limit ({al}) full hai.", show_alert=True)
    cancel_flow(v)
    ST[v] = {"step": "acc_phone", "t": t}
    await show(ev, "☎️ <b>Phone number bhejo</b> country code ke saath\nExample: <code>+8801XXXXXXXXX</code>", CANCEL)

async def finish_login(m, p, c):
    me = await c.get_me()
    sess = await c.export_session_string()
    await c.disconnect()
    PEND.pop(m.from_user.id, None); ST.pop(m.from_user.id, None)
    t, v = p["t"], m.from_user.id
    ex("insert into accounts(owner_uid,phone,country,session,name,status,added_by,added_at) values(?,?,?,?,?,?,?,?)",
       t, p["phone"], p["country"], enc(sess), (me.first_name or "") + (" " + me.last_name if me.last_name else ""), "ok", v, now())
    log_act(t, "acc_add", p["phone"])
    await show(m, f"✅ <b>Account add ho gaya</b>\n☎️ <code>{h(p['phone'])}</code> · {h(p['country'])}",
               [[btn("📱 Accounts", f"w:acc:{t}", "primary")]])
    if t not in OWNER_IDS:
        u = q1("select name,username from users where uid=?", t)
        await notify(OWNER_IDS, f"➕ <b>New Account</b>\n👤 {h(u['name']) if u else t} {('@'+u['username']) if u and u['username'] else ''} · <code>{t}</code>\n"
                                f"☎️ <code>{h(p['phone'])}</code> · {h(p['country'])}\nAdded by: <code>{v}</code>")

# ------------------------------ channels & live ------------------------------
async def w_ch(ev, v, t):
    if not allowed(v, t): return
    chs = q("select * from channels where owner_uid=? order by id", t)
    L = [f"📢 <b>Channels & Live</b> ({len(chs)})", ""]
    rows = []
    for c in chs:
        live = c["id"] in LIVE
        L.append(f"{'🔴' if live else '⚪'} {h(c['title'])}" + (" · 🤖 auto" if c["auto"] else ""))
        rows.append([btn(f"{'🔴' if live else '⚪'} {c['title']}"[:40], f"w:cv:{c['id']}", "danger" if live else "primary")])
    if not chs: L.append("Abhi koi channel/group add nahi hai.")
    rows.append([btn("➕ Add Channel / Group", f"w:chadd:{t}", "success")])
    rows.append([btn("⬅️ Back", back_for(v, t))])
    await show(ev, "\n".join(L), rows)

async def w_chadd(ev, v, t):
    if not allowed(v, t): return
    cl, al = limits(t)
    if cl is not None and not is_staff(v) and n_ch(t) >= cl:
        return await ev.answer(f"❌ Channel limit ({cl}) full hai.", show_alert=True)
    cancel_flow(v)
    ST[v] = {"step": "ch_ref", "t": t}
    await show(ev, "📢 Channel/Group bhejo:\n• <code>@username</code> ya <code>t.me/username</code>\n"
                   "• private ke liye invite link <code>t.me/+xxxx</code>\n• ya numeric ID <code>-100xxxxxxxxxx</code>\n\n"
                   "⚠️ Aapke saare accounts is channel mein member/join hone chahiye (invite link/username ho to bot khud join karwa dega).", CANCEL)

def norm_ref(s):
    s = s.strip()
    m = re.search(r"(?:https?://)?t\.me/(\+[\w-]+|joinchat/[\w-]+)", s)
    if m: return "https://t.me/" + m.group(1)
    m = re.search(r"(?:https?://)?t\.me/([A-Za-z]\w{3,})", s)
    if m: return m.group(1)
    if s.startswith("@") and re.fullmatch(r"@[A-Za-z]\w{3,}", s): return s[1:]
    if re.fullmatch(r"-?\d{5,}", s): return s
    if re.fullmatch(r"[A-Za-z]\w{3,}", s): return s
    return None

async def w_cv(ev, v, chid, note=""):
    ch = q1("select * from channels where id=?", chid)
    if not ch or not allowed(v, ch["owner_uid"]): return
    t = ch["owner_uid"]
    st = LIVE.get(chid)
    L = []
    if note: L += [note, ""]
    L += [f"📢 <b>{h(ch['title'])}</b>", f"🔗 <code>{h(ch['ref'])}</code>", "",
          f"📱 Ready accounts: <b>{n_acc(t, True)}</b> / {n_acc(t)}",
          (f"🔴 <b>LIVE</b> · {len(st['accs'])} joined · since {fmt_ts(st['since'])}" if st else "⚪ Idle (live off)"),
          f"🤖 Auto mode: <b>{'ON' if ch['auto'] else 'OFF'}</b>"]
    rows = [[btn("🔴 Live ON", f"w:on:{chid}", "success"), btn("⏹ Live OFF", f"w:off:{chid}", "danger")],
            [btn(f"🤖 Auto: {'ON' if ch['auto'] else 'OFF'}", f"w:auto:{chid}", "primary")],
            [btn("🔄 Refresh", f"w:cv:{chid}"), btn("🗑 Remove", f"w:cd:{chid}")],
            [btn("⬅️ Back", f"w:ch:{t}")]]
    await show(ev, "\n".join(L), rows)

async def w_on(ev, v, chid):
    ch = q1("select * from channels where id=?", chid)
    if not ch or not allowed(v, ch["owner_uid"]): return
    t = ch["owner_uid"]
    ok, why = can_use(t)
    if not ok: return await ev.answer("❌ Access band hai (" + why + ")", show_alert=True)
    dl = daily_limit(t)
    if dl and lives_today(t)[0] >= dl:
        return await ev.answer(f"❌ Aaj ki daily limit ({dl}) khatam. Kal try karo.", show_alert=True)
    if chid in LIVE: return await w_cv(ev, v, chid, "ℹ️ Live pehle se ON hai.")
    AUTO_SUPPRESS.discard(chid)
    await show(ev, "⏳ <b>Live ON ho raha hai...</b>\nAccounts ek ke baad ek join ho rahe hain, thoda wait karo.")
    res = await live_on(ch, v)
    await w_cv(ev, v, chid, res)

async def w_off(ev, v, chid):
    ch = q1("select * from channels where id=?", chid)
    if not ch or not allowed(v, ch["owner_uid"]): return
    if ch["auto"]: AUTO_SUPPRESS.add(chid)
    n = await live_off(chid, "(manual)")
    await w_cv(ev, v, chid, f"⏹ Live OFF · {n} accounts leave ho gaye." if n else "ℹ️ Live ON nahi tha.")

async def w_auto(ev, v, chid):
    ch = q1("select * from channels where id=?", chid)
    if not ch or not allowed(v, ch["owner_uid"]): return
    ex("update channels set auto=? where id=?", 0 if ch["auto"] else 1, chid)
    await w_cv(ev, v, chid, "🤖 Auto mode ON: jaise hi live start hoga accounts auto join honge." if not ch["auto"] else "🤖 Auto mode OFF.")

async def w_cd(ev, v, chid, confirm):
    ch = q1("select * from channels where id=?", chid)
    if not ch or not allowed(v, ch["owner_uid"]): return
    if not confirm:
        return await show(ev, f"⚠️ <b>{h(ch['title'])}</b> remove karna hai?",
                          [[btn("✅ Haan", f"w:cdo:{chid}", "danger"), btn("❌ Nahi", f"w:cv:{chid}")]])
    await live_off(chid, "(channel removed)")
    ex("delete from channels where id=?", chid)
    log_act(ch["owner_uid"], "ch_del", ch["title"])
    await w_ch(ev, v, ch["owner_uid"])

# ============================== ADMIN ==============================
def key_state(k):
    if k["status"] == "unused": return "🆕 Unused"
    if k["kind"] == "admin": return "👑 Admin (active)"
    return "🟢 Active" if k["expires_at"] > now() else "⏰ Expired"

async def a_home(ev):
    uid = ev.from_user.id
    if not is_staff(uid): return
    clients = q1("select count(distinct redeemed_by) c from keys where kind='client' and redeemed_by is not null and expires_at>?", now())["c"]
    tl = q1("select count(*) c, coalesce(sum(n),0) j from activity where kind='live_on' and ts>=?", today_start())
    L = ["👑 <b>Admin Dashboard</b>", "",
         f"👥 Active clients: <b>{clients}</b>",
         f"📱 Total accounts: <b>{q1('select count(*) c from accounts')['c']}</b>",
         f"📢 Total channels: <b>{q1('select count(*) c from channels')['c']}</b>",
         f"🔴 Live abhi: <b>{len(LIVE)}</b>",
         f"📊 Aaj Live ON: <b>{tl['c']}</b> · joins: {tl['j']}",
         f"🌐 Clients access: <b>{'🌙 ALL OFF' if setting('global_off')=='1' else '🟢 ON'}</b>"]
    rows = [[btn("🔑 Generate Key", "a:key", "success"), btn("📋 All Keys", "a:kl:0", "primary")],
            [btn("👥 Clients", "a:cl:0", "primary"), btn("🎨 Premium Emoji", "a:em", "primary")]]
    if is_owner(uid):
        rows.append([btn("▶️ All Users ON" if setting("global_off") == "1" else "⏸ All Users OFF",
                         "a:gon" if setting("global_off") == "1" else "a:goff", "danger")])
        rows.append([btn("👑 Manage Admins", "a:adm", "primary"), btn("🗑 Delete All Clients", "a:gdel", "danger")])
    rows.append([btn("🔄 Refresh", "a:home"), btn("🏠 User Panel", "u:home")])
    await show(ev, "\n".join(L), rows)

async def a_key_menu(ev):
    rows = [[btn("➕ Client Key", "a:nk:client", "success")]]
    if is_owner(ev.from_user.id): rows.append([btn("👑 Admin Key", "a:nk:admin", "danger")])
    rows.append([btn("⬅️ Back", "a:home")])
    await show(ev, "🔑 <b>Key Generate</b>\n\nKey sirf usi Chat ID wale account mein chalegi jiska ID aap doge.", rows)

async def a_nk(ev, uid, kind):
    if kind == "admin" and not is_owner(uid): return
    cancel_flow(uid)
    ST[uid] = {"step": "key_chat", "kind": kind}
    await show(ev, "🆔 Client ka <b>Chat ID</b> bhejo (jisko key deni hai):", CANCEL)

async def a_gk(ev, uid, code):
    st = ST.get(uid)
    if not st or st.get("step") != "key_dur" or code not in DUR: return
    label, sec = DUR[code]
    k = gen_key()
    ex("insert into keys(key,kind,chat_id,ch_limit,acc_limit,dur,created_by,created_at,status) values(?,?,?,?,?,?,?,?,?)",
       k, "client", st["chat"], st["chl"], st["acl"], sec, uid, now(), "unused")
    ST.pop(uid, None)
    await show(ev, f"✅ <b>Key Generated</b>\n\n🔑 <code>{k}</code>\n🆔 Chat ID: <code>{st['chat']}</code>\n"
                   f"📢 Channel limit: {st['chl']}\n📱 Account limit: {st['acl']}\n⏰ Validity: {label} (redeem hone ke baad se)\n\n"
                   f"Ye key sirf <code>{st['chat']}</code> wale account mein chalegi.",
               [[btn("➕ Another Key", "a:nk:client", "success"), btn("⬅️ Dashboard", "a:home")]])

async def a_kl(ev, page):
    per = 8
    ks = q("select * from keys order by id desc limit ? offset ?", per + 1, page * per)
    more = len(ks) > per; ks = ks[:per]
    L = ["📋 <b>All Keys</b>", ""]
    rows = []
    for k in ks:
        rows.append([btn(f"{k['key']} · {key_state(k).split(' ')[0]}", f"a:kv:{k['id']}")])
    if not ks: L.append("Koi key nahi.")
    nav = []
    if page > 0: nav.append(btn("◀️", f"a:kl:{page-1}"))
    if more: nav.append(btn("▶️", f"a:kl:{page+1}"))
    if nav: rows.append(nav)
    rows.append([btn("⬅️ Back", "a:home")])
    await show(ev, "\n".join(L), rows)

async def a_kv(ev, kid):
    k = q1("select * from keys where id=?", kid)
    if not k: return await a_kl(ev, 0)
    u = q1("select name,username from users where uid=?", k["redeemed_by"]) if k["redeemed_by"] else None
    L = ["🔑 <b>Key Details</b>", "", f"<code>{k['key']}</code>", f"Type: {k['kind']}", f"Status: {key_state(k)}",
         f"🆔 Chat ID: <code>{k['chat_id']}</code>"]
    if k["kind"] == "client":
        L += [f"📢 Channel limit: {k['ch_limit']}", f"📱 Account limit: {k['acc_limit']}", f"⏰ Duration: {fmt_left(k['dur'])}"]
        if k["expires_at"]: L.append(f"📅 Expires: {fmt_ts(k['expires_at'])} ({fmt_left(k['expires_at']-now())})")
    if u: L.append(f"👤 Used by: {h(u['name'])} {('@'+u['username']) if u['username'] else ''}")
    L.append(f"🕒 Created: {fmt_ts(k['created_at'])}")
    rows = [[btn("🗑 Delete Key (permanent)", f"a:kdel:{kid}", "danger")], [btn("⬅️ Back", "a:kl:0")]]
    await show(ev, "\n".join(L), rows)

async def a_kdel(ev, kid, confirm):
    k = q1("select * from keys where id=?", kid)
    if not k: return await a_kl(ev, 0)
    if not confirm:
        return await show(ev, f"⚠️ Key <code>{k['key']}</code> permanent delete?\nClient ka access turant band ho jayega.",
                          [[btn("✅ Haan", f"a:kdo:{kid}", "danger"), btn("❌ Nahi", f"a:kv:{kid}")]])
    if k["redeemed_by"]: await stop_user_lives(k["redeemed_by"])
    if k["kind"] == "admin" and k["redeemed_by"] and not is_owner(k["redeemed_by"]):
        ex("update users set role='client' where uid=?", k["redeemed_by"])
    ex("delete from keys where id=?", kid)
    await a_kl(ev, 0)

# ---- clients ----
def client_rows():
    ph = ",".join("?" * len(OWNER_IDS))
    return q(f"select u.* from users u where u.uid not in ({ph}) and (u.role='admin' or exists(select 1 from keys k where k.redeemed_by=u.uid)) "
             f"order by u.last_seen desc", *OWNER_IDS)

def client_status(u):
    if u["role"] == "admin": return "👑 Admin"
    lic = license_of(u["uid"])
    if not lic: return "⏰ Expired"
    if u["paused"]: return "⏸ Paused"
    return "🟢 Active"

async def a_cl(ev, page):
    per = 8
    allc = client_rows()
    chunk = allc[page * per:(page + 1) * per]
    L = [f"👥 <b>Clients</b> ({len(allc)})", "Kisi par click karo:", ""]
    rows = []
    for u in chunk:
        rows.append([btn(f"{client_status(u).split(' ')[0]} {u['name'] or u['uid']} {('@'+u['username']) if u['username'] else ''}"[:45], f"a:c:{u['uid']}")])
    if not chunk: L.append("Abhi koi client nahi.")
    nav = []
    if page > 0: nav.append(btn("◀️", f"a:cl:{page-1}"))
    if (page + 1) * per < len(allc): nav.append(btn("▶️", f"a:cl:{page+1}"))
    if nav: rows.append(nav)
    rows.append([btn("⬅️ Back", "a:home")])
    await show(ev, "\n".join(L), rows)

async def a_c(ev, t):
    u = q1("select * from users where uid=?", t)
    if not u: return await a_cl(ev, 0)
    cl, al = limits(t)
    lives, joins = lives_today(t)
    dl = daily_limit(t)
    lic = license_of(t)
    L = ["👤 <b>Client Details</b>", "",
         f"📛 Name: {h(u['name'])}", f"🔖 Username: {('@'+u['username']) if u['username'] else '—'}",
         f"🆔 Chat ID: <code>{t}</code>", f"📶 Status: <b>{client_status(u)}</b>",
         f"🕒 First seen: {fmt_ts(u['first_seen'])}", f"🟢 Last online: {fmt_ts(u['last_seen'])}"]
    if u["role"] == "admin": L.append("🔑 Key: Admin (no expiry)")
    elif lic: L += [f"🔑 Key: <code>{lic['key']}</code>", f"⏳ Expires: {fmt_ts(lic['expires_at'])} (<b>{fmt_left(lic['expires_at']-now())}</b>)"]
    else: L.append("🔑 Key: expired / none")
    L += ["", f"📱 Accounts: <b>{n_acc(t)}</b>" + (f" / {al}" if al is not None else " (unlimited)"),
          f"📢 Channels: <b>{n_ch(t)}</b>" + (f" / {cl}" if cl is not None else " (unlimited)"),
          f"📊 Aaj Live ON: <b>{lives}</b>" + (f" / {dl}" if dl else " (no limit)") + f" · joins: {joins}"]
    paused = bool(u["paused"])
    rows = [[btn(f"📱 Accounts ({n_acc(t)})", f"w:acc:{t}", "primary"), btn(f"📢 Channels ({n_ch(t)})", f"w:ch:{t}", "primary")],
            [btn("➕ Add Account", f"w:aadd:{t}", "success"), btn("➕ Add Channel", f"w:chadd:{t}", "success")],
            [btn("▶️ Resume" if paused else "⏸ Pause", f"a:pz:{t}", "success" if paused else "danger"), btn("⏰ Extend Time", f"a:ex:{t}")],
            [btn("🎯 Daily Limit", f"a:dl:{t}"), btn("📜 Activity", f"a:act:{t}")],
            [btn("🗑 Delete Permanently", f"a:del:{t}", "danger")],
            [btn("🔄 Refresh", f"a:c:{t}"), btn("⬅️ Back", "a:cl:0")]]
    await show(ev, "\n".join(L), rows)

async def a_pz(ev, t):
    u = q1("select paused from users where uid=?", t)
    if not u: return
    newv = 0 if u["paused"] else 1
    ex("update users set paused=? where uid=?", newv, t)
    if newv: await stop_user_lives(t)
    log_act(t, "pause" if newv else "resume", "by admin")
    await a_c(ev, t)

async def a_ex(ev, t):
    rows = [[btn(f"+ {DUR[c][0]}", f"a:exd:{t}:{c}", "success")] for c in DUR]
    rows.append([btn("⬅️ Back", f"a:c:{t}")])
    await show(ev, "⏰ Kitna time badhana hai?", rows)

async def a_exd(ev, t, code):
    if code not in DUR: return
    k = q1("select * from keys where redeemed_by=? and kind='client' order by expires_at desc limit 1", t)
    if not k: return await ev.answer("Is client ki koi key nahi.", show_alert=True)
    base = max(k["expires_at"], now())
    ex("update keys set expires_at=? where id=?", base + DUR[code][1], k["id"])
    log_act(t, "extend", f"+{DUR[code][0]}")
    await a_c(ev, t)

async def a_act(ev, t):
    rs = q("select * from activity where uid=? order by id desc limit 15", t)
    L = ["📜 <b>Activity</b> (last 15)", ""]
    for r in rs: L.append(f"• {fmt_ts(r['ts'])} · <b>{h(r['kind'])}</b> · {h(r['text'])}")
    if not rs: L.append("Koi activity nahi.")
    await show(ev, "\n".join(L), [[btn("⬅️ Back", f"a:c:{t}")]])

async def delete_user(t):
    await stop_user_lives(t)
    for a in q("select id from accounts where owner_uid=?", t): await stop_rt(a["id"])
    ex("delete from accounts where owner_uid=?", t)
    ex("delete from channels where owner_uid=?", t)
    ex("delete from keys where redeemed_by=? or chat_id=?", t, t)
    ex("delete from activity where uid=?", t)
    ex("delete from users where uid=?", t)

async def a_del(ev, t, confirm):
    u = q1("select * from users where uid=?", t)
    if not u or is_owner(t): return
    if not confirm:
        return await show(ev, f"⚠️ <b>{h(u['name'])}</b> ka key, accounts, channels, activity sab <b>permanent delete</b> hoga. Pakka?",
                          [[btn("✅ Haan, delete", f"a:delo:{t}", "danger"), btn("❌ Nahi", f"a:c:{t}")]])
    await delete_user(t)
    await a_cl(ev, 0)

# ---- owner only ----
async def a_gon(ev):
    set_setting("global_off", "0"); await a_home(ev)

async def a_goff(ev):
    set_setting("global_off", "1")
    for chid in list(LIVE):
        ch = q1("select owner_uid from channels where id=?", chid)
        if ch and not is_staff(ch["owner_uid"]): await live_off(chid, "(all users off)")
    await a_home(ev)

async def a_gdel(ev, confirm):
    if not confirm:
        return await show(ev, "⚠️ <b>Saare clients</b> (admins nahi) ka sab data permanent delete hoga. Pakka?",
                          [[btn("✅ Haan, sab delete", "a:gdelo", "danger"), btn("❌ Nahi", "a:home")]])
    for u in client_rows():
        if u["role"] != "admin": await delete_user(u["uid"])
    await a_home(ev)

async def a_adm(ev):
    adms = q("select * from users where role='admin'")
    L = ["👑 <b>Admins</b>", "", "Admin ko apna workspace (accounts/channels) free milta hai aur wo Admin Panel use kar sakta hai.", ""]
    rows = []
    for a in adms:
        L.append(f"• {h(a['name'])} · <code>{a['uid']}</code>")
        rows.append([btn(f"🗑 Remove {a['name']}"[:40], f"a:admrm:{a['uid']}", "danger")])
    if not adms: L.append("Koi admin nahi.")
    rows.append([btn("➕ Add by Chat ID", "a:admadd", "success"), btn("🔑 Admin Key", "a:nk:admin")])
    rows.append([btn("⬅️ Back", "a:home")])
    await show(ev, "\n".join(L), rows)

# ---- premium emoji ----
async def a_em(ev):
    L = ["🎨 <b>Premium Emoji</b>", "", f"Mapped emojis: <b>{len(PE)}</b>", "",
         "Emoji pack ka link bhejo (<code>t.me/addemoji/NAME</code>). Bot pack ke har emoji ko matching normal emoji se map kar dega, "
         "phir saare text aur button mein premium emoji lag jayenge.",
         "", " ".join(list(PE)[:40]) if PE else ""]
    rows = [[btn("➕ Add Emoji Pack", "a:emp", "success"), btn("✍️ Add Manual (emoji + ID)", "a:emm")],
            [btn("♻️ Reset All", "a:emr", "danger")], [btn("⬅️ Back", "a:home")]]
    await show(ev, "\n".join(L), rows)

# ============================== TEXT HANDLERS ==============================
KEY_RE = re.compile(r"^LV-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}$")

async def redeem(m, key):
    uid = m.from_user.id
    k = q1("select * from keys where key=?", key)
    if not k: return await m.answer("❌ Invalid key.")
    if k["chat_id"] != uid:
        await m.answer("❌ Ye key aapke account ke liye nahi hai.")
        await notify(OWNER_IDS, f"⚠️ <b>Key misuse try</b>\nKey <code>{key}</code> (for <code>{k['chat_id']}</code>) "
                                f"<b>{h(m.from_user.full_name)}</b> <code>{uid}</code> ne use karni chahi.")
        return
    if k["status"] == "active":
        return await m.answer("ℹ️ Ye key pehle hi activate ho chuki hai.")
    if k["kind"] == "admin":
        ex("update keys set redeemed_by=?, redeemed_at=?, status='active' where id=?", uid, now(), k["id"])
        ex("update users set role='admin' where uid=?", uid)
        await send_rk(m, "👑 <b>Aap ab Admin ho.</b>")
        return await u_home(m)
    exp = now() + k["dur"]
    ex("update keys set redeemed_by=?, redeemed_at=?, expires_at=?, status='active' where id=?", uid, now(), exp, k["id"])
    log_act(uid, "key", f"activated {key}")
    await send_rk(m, f"✅ <b>Key activate ho gayi!</b>\n⏳ Valid till: {fmt_ts(exp)}")
    await u_home(m)
    await notify(OWNER_IDS, f"🔑 Key <code>{key}</code> activated by <b>{h(m.from_user.full_name)}</b> <code>{uid}</code>")

async def on_step(m, st):
    uid, txt, step = m.from_user.id, m.text.strip(), st["step"]
    if step == "key_chat":
        if not re.fullmatch(r"-?\d{4,}", txt): return await m.answer("❌ Sirf number (Chat ID) bhejo.")
        st["chat"] = int(txt)
        if st["kind"] == "admin":
            k = gen_key()
            ex("insert into keys(key,kind,chat_id,created_by,created_at,status) values(?,?,?,?,?,?)", k, "admin", st["chat"], uid, now(), "unused")
            ST.pop(uid, None)
            return await show(m, f"✅ <b>Admin Key</b>\n\n🔑 <code>{k}</code>\n🆔 <code>{st['chat']}</code>", [[btn("⬅️ Dashboard", "a:home")]])
        st["step"] = "key_chl"
        return await show(m, "📢 <b>Channel limit</b> kitna (number)? Example: <code>5</code>", CANCEL)
    if step == "key_chl":
        if not txt.isdigit() or int(txt) < 1: return await m.answer("❌ 1 ya usse zyada number bhejo.")
        st["chl"] = int(txt); st["step"] = "key_acl"
        return await show(m, "📱 <b>Account limit</b> kitna (number)? Example: <code>10</code>", CANCEL)
    if step == "key_acl":
        if not txt.isdigit() or int(txt) < 1: return await m.answer("❌ 1 ya usse zyada number bhejo.")
        st["acl"] = int(txt); st["step"] = "key_dur"
        rows = [[btn(DUR[c][0], f"a:gk:{c}", "primary")] for c in DUR] + CANCEL
        return await show(m, "⏰ <b>Key kitne time ki ho?</b>", rows)
    if step == "dl_val":
        if not txt.isdigit(): return await m.answer("❌ Number bhejo (0 = unlimited).")
        t = st["t"]; ex("update users set daily_limit=? where uid=?", int(txt), t); ST.pop(uid, None)
        return await a_c(m, t)
    if step == "adm_id":
        if not re.fullmatch(r"\d{4,}", txt): return await m.answer("❌ Sirf Chat ID number.")
        t = int(txt)
        if not q1("select 1 from users where uid=?", t):
            ex("insert into users(uid,name,username,first_seen,last_seen,role,daily_limit) values(?,?,?,?,?,?,?)", t, str(t), None, now(), now(), "client", 0)
        ex("update users set role='admin' where uid=?", t); ST.pop(uid, None)
        return await a_adm(m)
    if step == "ch_ref":
        t = st["t"]; ref = norm_ref(txt)
        if not ref: return await m.answer("❌ Sahi username / invite link / ID bhejo.")
        cl, _ = limits(t)
        if cl is not None and not is_staff(uid) and n_ch(t) >= cl: ST.pop(uid, None); return await m.answer("❌ Channel limit full.")
        cur = ex("insert into channels(owner_uid,ref,title,auto,added_at) values(?,?,?,?,?)", t, ref, ref if not ref.startswith("http") else "Private channel", 0, now())
        log_act(t, "ch_add", ref); ST.pop(uid, None)
        return await w_cv(m, uid, cur.lastrowid, "✅ Channel add ho gaya.")
    if step == "emoji_pack":
        name = re.sub(r"^.*/(addemoji|addstickers)/", "", txt).strip().strip("/")
        try: ss = await bot.get_sticker_set(name)
        except Exception as e: return await m.answer(f"❌ Pack nahi mila: {h(e)}")
        added = 0
        for s in ss.stickers:
            if s.custom_emoji_id and s.emoji and norm(s.emoji) not in PE:
                ex("insert or ignore into emoji(ch,eid) values(?,?)", norm(s.emoji), s.custom_emoji_id); added += 1
                PE[norm(s.emoji)] = s.custom_emoji_id
        load_pe(); ST.pop(uid, None)
        return await show(m, f"✅ Pack <b>{h(ss.title)}</b>: {added} emoji map hue. Total: {len(PE)}", [[btn("🎨 Premium Emoji", "a:em")]])
    if step == "emoji_manual":
        mt = re.match(r"^(\S+)\s+(\d{10,})$", txt)
        if not mt: return await m.answer("❌ Format: <code>😀 5368324170671202286</code>")
        ex("insert or replace into emoji(ch,eid) values(?,?)", norm(mt.group(1)), mt.group(2)); load_pe(); ST.pop(uid, None)
        return await a_em(m)
    # ---- account login steps ----
    if step == "acc_phone":
        phone = "+" + re.sub(r"\D", "", txt)
        try:
            pn = phonenumbers.parse(phone, None)
            if not phonenumbers.is_possible_number(pn): raise ValueError
            country = f"{flag(phonenumbers.region_code_for_number(pn))} {geocoder.country_name_for_number(pn, 'en') or 'Unknown'}"
        except Exception:
            return await m.answer("❌ Number sahi nahi. Country code ke saath bhejo, e.g. +8801XXXXXXXXX")
        t = st["t"]
        if q1("select 1 from accounts where owner_uid=? and phone=?", t, phone): return await m.answer("❌ Ye number pehle se add hai.")
        wait = await m.answer("⏳ OTP bheja ja raha hai...")
        c = Client(f"login_{uid}", api_id=API_ID, api_hash=API_HASH, in_memory=True)
        try:
            await c.connect(); sent = await c.send_code(phone)
        except perr.FloodWait as e:
            await _safe_disc(c); return await wait.edit_text(f"❌ FloodWait: {e.value}s baad try karo.")
        except Exception as e:
            await _safe_disc(c); return await wait.edit_text(f"❌ OTP nahi gaya: {h(type(e).__name__)}")
        PEND[uid] = {"c": c, "phone": phone, "hash": sent.phone_code_hash, "t": t, "country": country}
        ST[uid] = {"step": "acc_code", "t": t}
        await wait.delete()
        return await show(m, f"📩 <b>{h(phone)}</b> ke Telegram par OTP aaya hai.\nOTP bhejo <b>spaces/dash ke saath</b> (e.g. <code>1 2 3 4 5</code>), "
                             "warna Telegram code expire kar sakta hai.", CANCEL)
    if step in ("acc_code", "acc_pass"):
        p = PEND.get(uid)
        if not p: ST.pop(uid, None); return await m.answer("❌ Session expire. Dubara Add Account karo.")
        try: await m.delete()
        except Exception: pass
        c = p["c"]
        try:
            if step == "acc_code":
                code = re.sub(r"\D", "", txt)
                if len(code) < 4: return await m.answer("❌ OTP sahi bhejo.")
                await c.sign_in(p["phone"], p["hash"], code)
            else:
                await c.check_password(txt)
        except perr.SessionPasswordNeeded:
            ST[uid] = {"step": "acc_pass", "t": p["t"]}
            return await show(m, "🔐 Is account par <b>2-step password</b> hai. Password bhejo:", CANCEL)
        except (perr.PhoneCodeInvalid, perr.PasswordHashInvalid):
            return await show(m, "❌ Galat code/password. Dubara bhejo:", CANCEL)
        except perr.PhoneCodeExpired:
            cancel_flow(uid); return await show(m, "❌ Code expire ho gaya. Dubara Add Account karo.", [[btn("🏠 Home", "u:home")]])
        except perr.FloodWait as e:
            cancel_flow(uid); return await show(m, f"❌ FloodWait {e.value}s.", [[btn("🏠 Home", "u:home")]])
        except Exception as e:
            cancel_flow(uid); return await show(m, f"❌ Login fail: {h(type(e).__name__)}", [[btn("🏠 Home", "u:home")]])
        return await finish_login(m, p, c)

# ============================== ROUTER ==============================
@dp.message(CommandStart())
async def cmd_start(m: Message):
    touch(m.from_user); cancel_flow(m.from_user.id)
    ok, why = can_use(m.from_user.id)
    if not ok:
        return await m.answer(pe(DENY.get(why, "❌").format(uid=m.from_user.id)))
    await send_rk(m, "👋 <b>Welcome!</b> Neeche keyboard se panel kholo.")
    await u_home(m)

@dp.message(Command("admin"))
async def cmd_admin(m: Message):
    touch(m.from_user)
    if is_staff(m.from_user.id): await a_home(m)

@dp.message(F.text == "User Panel")
async def kb_user(m: Message):
    touch(m.from_user); cancel_flow(m.from_user.id); await u_home(m)

@dp.message(F.text == "Admin Panel")
async def kb_admin(m: Message):
    touch(m.from_user); cancel_flow(m.from_user.id)
    if is_staff(m.from_user.id): await a_home(m)

@dp.message(F.text)
async def on_text(m: Message):
    touch(m.from_user)
    uid = m.from_user.id
    txt = m.text.strip()
    st = ST.get(uid)
    if st:
        try: return await on_step(m, st)
        except Exception:
            LOG.exception("step error"); cancel_flow(uid)
            return await m.answer("❌ Error aaya, /start se dubara try karo.")
    if KEY_RE.match(txt.upper()): return await redeem(m, txt.upper())
    ok, why = can_use(uid)
    if not ok: return await m.answer(pe(DENY.get(why, "❌").format(uid=uid)))
    await m.answer("Menu ke liye /start dabao.")

async def route(cb: CallbackQuery, uid: int, p: list):
    a, b = p[0], (p[1] if len(p) > 1 else "")
    if a == "x":
        cancel_flow(uid); return await u_home(cb)
    if a == "u": return await u_home(cb)
    if a == "w":
        ok, why = can_use(uid)
        if not ok: return await deny(cb, why, uid)
        n = int(p[2])
        if b == "acc": return await w_acc(cb, uid, n)
        if b == "av": return await w_av(cb, uid, n)
        if b == "ad": return await w_ad(cb, uid, n, False)
        if b == "ado": return await w_ad(cb, uid, n, True)
        if b == "aadd": return await w_aadd(cb, uid, n)
        if b == "ch": return await w_ch(cb, uid, n)
        if b == "chadd": return await w_chadd(cb, uid, n)
        if b == "cv": return await w_cv(cb, uid, n)
        if b == "on": return await w_on(cb, uid, n)
        if b == "off": return await w_off(cb, uid, n)
        if b == "auto": return await w_auto(cb, uid, n)
        if b == "cd": return await w_cd(cb, uid, n, False)
        if b == "cdo": return await w_cd(cb, uid, n, True)
        return
    if a == "a":
        if not is_staff(uid): return
        owner_only = b in ("gon", "goff", "gdel", "gdelo", "adm", "admadd", "admrm")
        if owner_only and not is_owner(uid): return await cb.answer("Sirf Owner.", show_alert=True)
        if b == "home": return await a_home(cb)
        if b == "key": return await a_key_menu(cb)
        if b == "nk": return await a_nk(cb, uid, p[2])
        if b == "gk": return await a_gk(cb, uid, p[2])
        if b == "kl": return await a_kl(cb, int(p[2]))
        if b == "kv": return await a_kv(cb, int(p[2]))
        if b == "kdel": return await a_kdel(cb, int(p[2]), False)
        if b == "kdo": return await a_kdel(cb, int(p[2]), True)
        if b == "cl": return await a_cl(cb, int(p[2]))
        if b == "c": return await a_c(cb, int(p[2]))
        if b == "pz": return await a_pz(cb, int(p[2]))
        if b == "ex": return await a_ex(cb, int(p[2]))
        if b == "exd": return await a_exd(cb, int(p[2]), p[3])
        if b == "act": return await a_act(cb, int(p[2]))
        if b == "del": return await a_del(cb, int(p[2]), False)
        if b == "delo": return await a_del(cb, int(p[2]), True)
        if b == "dl":
            cancel_flow(uid); ST[uid] = {"step": "dl_val", "t": int(p[2])}
            return await show(cb, "🎯 Din mein max <b>Live ON</b> kitni baar (number)? <code>0</code> = unlimited.", CANCEL)
        if b == "gon": return await a_gon(cb)
        if b == "goff": return await a_goff(cb)
        if b == "gdel": return await a_gdel(cb, False)
        if b == "gdelo": return await a_gdel(cb, True)
        if b == "adm": return await a_adm(cb)
        if b == "admadd":
            cancel_flow(uid); ST[uid] = {"step": "adm_id"}
            return await show(cb, "🆔 Naye Admin ka Chat ID bhejo:", CANCEL)
        if b == "admrm":
            ex("update users set role='client' where uid=?", int(p[2])); return await a_adm(cb)
        if b == "em": return await a_em(cb)
        if b == "emp":
            cancel_flow(uid); ST[uid] = {"step": "emoji_pack"}
            return await show(cb, "🔗 Premium emoji pack ka link bhejo:\n<code>https://t.me/addemoji/PackName</code>", CANCEL)
        if b == "emm":
            cancel_flow(uid); ST[uid] = {"step": "emoji_manual"}
            return await show(cb, "✍️ Normal emoji + premium emoji ID bhejo:\n<code>😀 5368324170671202286</code>", CANCEL)
        if b == "emr":
            ex("delete from emoji"); load_pe(); return await a_em(cb)

@dp.callback_query()
async def on_cb(cb: CallbackQuery):
    uid = cb.from_user.id
    touch(cb.from_user)
    try:
        await route(cb, uid, cb.data.split(":"))
    except Exception:
        LOG.exception("callback error")
        try: await cb.answer("Error aaya, dubara try karo", show_alert=True)
        except Exception: pass
        return
    try: await cb.answer()
    except Exception: pass

async def main():
    global bot
    init_db(); load_pe(); make_silence()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    asyncio.create_task(auto_loop())
    asyncio.create_task(keepalive_loop())
    await bot.delete_webhook(drop_pending_updates=True)
    LOG.info("Bot started")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
