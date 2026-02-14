import asyncio
import math
import random
import time
from dataclasses import dataclass
from typing import Dict, Optional, Set, Tuple, List

import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

# =======================
# CONFIG
# =======================
TOKEN = "8161107014:AAGBWEYVxie7-pB4-2FoGCPjCv_sl0yHogc"
ADMIN_IDS = {5815294733}   # o'zingni admin ID
DB_PATH = "casino.db"

# Games config
MINES_SIZE = 5
MINES_BOMBS = 3

# Aviator config (demo, fair-ish)
AVIATOR_TICK_SEC = 0.7
AVIATOR_GROWTH = 0.06  # multiplier growth per tick


dp = Dispatcher()

# =======================
# DB
# =======================
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS users (
  user_id INTEGER PRIMARY KEY,
  balance INTEGER NOT NULL DEFAULT 0,
  ref_by INTEGER,
  ref_count INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS promo_codes (
  code TEXT PRIMARY KEY,
  amount INTEGER NOT NULL,
  max_uses INTEGER NOT NULL,
  used_count INTEGER NOT NULL DEFAULT 0,
  created_by INTEGER NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS promo_uses (
  user_id INTEGER NOT NULL,
  code TEXT NOT NULL,
  used_at INTEGER NOT NULL,
  PRIMARY KEY(user_id, code)
);

CREATE TABLE IF NOT EXISTS deposit_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  amount INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', -- pending/approved/rejected
  created_at INTEGER NOT NULL,
  handled_by INTEGER,
  handled_at INTEGER
);

CREATE TABLE IF NOT EXISTS withdraw_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  amount INTEGER NOT NULL,
  wallet TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at INTEGER NOT NULL,
  handled_by INTEGER,
  handled_at INTEGER
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "ref_bonus_percent": "3",   # referral bonus % (deposit approved bo'lganda ishlaydi)
}

async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(CREATE_SQL)
        for k, v in DEFAULT_SETTINGS.items():
            await db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
        await db.commit()

async def get_setting(key: str) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else ""

async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        await db.commit()

async def ensure_user(user_id: int, ref_by: Optional[int] = None):
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id, ref_by FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        if row is None:
            await db.execute(
                "INSERT INTO users(user_id, balance, ref_by, ref_count, created_at) VALUES(?,?,?,?,?)",
                (user_id, 0, ref_by if ref_by and ref_by != user_id else None, 0, now)
            )
            # ref count increment
            if ref_by and ref_by != user_id:
                await db.execute("UPDATE users SET ref_count = ref_count + 1 WHERE user_id=?", (ref_by,))
        await db.commit()

async def get_user(user_id: int) -> Tuple[int, Optional[int], int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT balance, ref_by, ref_count FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        if not row:
            return 0, None, 0
        return int(row[0]), row[1], int(row[2])

async def add_balance(user_id: int, amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (amount, user_id))
        await db.commit()

async def take_balance(user_id: int, amount: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        if not row or int(row[0]) < amount:
            return False
        await db.execute("UPDATE users SET balance = balance - ? WHERE user_id=?", (amount, user_id))
        await db.commit()
        return True

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

# =======================
# UI helpers
# =======================
def main_menu_kb(is_admin_user: bool) -> types.InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="💣 Mines", callback_data="go:mines")
    kb.button(text="✈️ Aviator", callback_data="go:aviator")
    kb.button(text="💰 Balans", callback_data="go:balance")
    kb.button(text="➕ Deposit", callback_data="go:deposit")
    kb.button(text="➖ Withdraw", callback_data="go:withdraw")
    kb.button(text="🎁 Promo code", callback_data="go:promo")
    kb.button(text="🤝 Referal", callback_data="go:ref")
    if is_admin_user:
        kb.button(text="🛠 Admin panel", callback_data="admin:panel")
    kb.adjust(2,2,2,2,1)
    return kb.as_markup()

# =======================
# States (in-memory)
# =======================
@dataclass
class MinesSession:
    bet: int
    bombs: Set[int]
    opened: Set[int]
    active: bool = True

@dataclass
class AviatorSession:
    bet: int
    mult: float
    crashed: bool
    cashed_out: bool

mines_sessions: Dict[int, MinesSession] = {}
aviator_sessions: Dict[int, AviatorSession] = {}

# for simple step flows
awaiting: Dict[int, Dict[str, str]] = {}  # user_id -> {"mode":"deposit_amount"/"withdraw_amount"/"withdraw_wallet"/"mines_bet"/"aviator_bet"/"promo_enter"}

# =======================
# GAME LOGIC
# =======================
def mines_multiplier(opened_count: int) -> float:
    # past multipliers: early small to prevent instant cashout
    table = [1.00, 1.10, 1.20, 1.35, 1.50, 1.80, 2.10, 2.50, 3.00, 3.70, 4.60]
    if opened_count < len(table):
        return table[opened_count]
    # after table, grow slowly
    return round(table[-1] + (opened_count - (len(table)-1)) * 0.9, 2)

def cell_emoji(idx: int, session: MinesSession) -> str:
    if idx in session.opened:
        return "✅"
    return "⬜️"

def mines_kb(uid: int) -> types.InlineKeyboardMarkup:
    s = mines_sessions[uid]
    kb = InlineKeyboardBuilder()
    for r in range(MINES_SIZE):
        for c in range(MINES_SIZE):
            idx = r * MINES_SIZE + c
            kb.button(text=cell_emoji(idx, s), callback_data=f"mines:open:{idx}")
        kb.adjust(MINES_SIZE)
    # bottom actions
    opened_count = len(s.opened)
    kb.button(text=f"💵 Cashout x{mines_multiplier(opened_count):.2f}", callback_data="mines:cashout")
    kb.button(text="❌ Stop", callback_data="mines:stop")
    kb.adjust(MINES_SIZE, 2)
    return kb.as_markup()

def gen_bombs(exclude_idx: int) -> Set[int]:
    cells = list(range(MINES_SIZE * MINES_SIZE))
    cells.remove(exclude_idx)
    return set(random.sample(cells, MINES_BOMBS))

async def aviator_loop(bot: Bot, uid: int):
    # runs until crash or cashout
    s = aviator_sessions.get(uid)
    if not s:
        return
    while True:
        await asyncio.sleep(AVIATOR_TICK_SEC)
        s = aviator_sessions.get(uid)
        if not s or s.cashed_out or s.crashed:
            return

        # update multiplier
        s.mult = round(s.mult * (1.0 + AVIATOR_GROWTH), 2)

        # crash chance increases with multiplier (simple demo)
        # NOTE: demo/fair-ish; do NOT manipulate per-user
        crash_prob = min(0.02 + (s.mult - 1.0) * 0.015, 0.45)
        if random.random() < crash_prob:
            s.crashed = True
            aviator_sessions[uid] = s
            await bot.send_message(uid, f"💥 Crash! x{s.mult:.2f}\n❌ Yutqazding.", reply_markup=main_menu_kb(is_admin(uid)))
            return

        # send small updates sometimes (avoid spam)
        if int(s.mult * 100) % 20 == 0:
            await bot.send_message(uid, f"✈️ Koef: x{s.mult:.2f}\n/stop — cashout", disable_web_page_preview=True)

# =======================
# HANDLERS
# =======================
@dp.message(CommandStart())
async def start(m: types.Message):
    uid = m.from_user.id
    # parse referral: /start 12345
    ref_by = None
    parts = (m.text or "").split()
    if len(parts) == 2 and parts[1].isdigit():
        ref_by = int(parts[1])
    await ensure_user(uid, ref_by=ref_by)
    await m.answer(
        "Salom! 👋\nBu demo casino bot (virtual balans).\nMenyu:",
        reply_markup=main_menu_kb(is_admin(uid))
    )

@dp.callback_query(F.data.startswith("go:"))
async def go_router(c: types.CallbackQuery):
    uid = c.from_user.id
    await ensure_user(uid)

    page = c.data.split(":")[1]
    if page == "balance":
        bal, _, refc = await get_user(uid)
        await c.message.edit_text(f"💰 Balans: {bal}\n👥 Referal: {refc}", reply_markup=main_menu_kb(is_admin(uid)))

    elif page == "deposit":
        awaiting[uid] = {"mode": "deposit_amount"}
        await c.message.edit_text("➕ Deposit so‘rovi\nSummani yozing (masalan 5000):")

    elif page == "withdraw":
        awaiting[uid] = {"mode": "withdraw_amount"}
        await c.message.edit_text("➖ Withdraw so‘rovi\nSummani yozing (masalan 5000):")

    elif page == "promo":
        awaiting[uid] = {"mode": "promo_enter"}
        await c.message.edit_text("🎁 Promo code kiriting:")

    elif page == "ref":
        bot_username = (await c.bot.get_me()).username
        link = f"https://t.me/{bot_username}?start={uid}"
        bal, _, refc = await get_user(uid)
        ref_percent = await get_setting("ref_bonus_percent")
        await c.message.edit_text(
            f"🤝 Referal tizim\n"
            f"Link: {link}\n"
            f"👥 Taklif qilganlar: {refc}\n"
            f"🎯 Bonus: {ref_percent}% (deposit tasdiqlansa)\n",
            reply_markup=main_menu_kb(is_admin(uid))
        )

    elif page == "mines":
        awaiting[uid] = {"mode": "mines_bet"}
        await c.message.edit_text("💣 Mines\nStavka (bet) yozing:")

    elif page == "aviator":
        awaiting[uid] = {"mode": "aviator_bet"}
        await c.message.edit_text("✈️ Aviator\nStavka (bet) yozing:")

    await c.answer()

@dp.message(Command("stop"))
async def stop_cmd(m: types.Message):
    uid = m.from_user.id
    s = aviator_sessions.get(uid)
    if not s or s.crashed or s.cashed_out:
        return await m.answer("Aviator o‘yini yo‘q yoki allaqachon tugagan.")
    s.cashed_out = True
    aviator_sessions[uid] = s
    win = int(round(s.bet * s.mult))
    await add_balance(uid, win)
    await m.answer(f"✅ Cashout: x{s.mult:.2f}\n💵 Yutuq: {win}", reply_markup=main_menu_kb(is_admin(uid)))

@dp.callback_query(F.data.startswith("mines:"))
async def mines_router(c: types.CallbackQuery):
    uid = c.from_user.id
    if uid not in mines_sessions:
        await c.answer("Mines sessiya yo‘q.")
        return
    s = mines_sessions[uid]
    if not s.active:
        await c.answer("O‘yin tugagan.")
        return

    action = c.data.split(":")[1]
    if action == "open":
        idx = int(c.data.split(":")[2])

        # first click -> generate bombs excluding clicked cell
        if not s.bombs:
            s.bombs = gen_bombs(idx)

        if idx in s.opened:
            await c.answer("Ochilgan.")
            return

        # if bomb -> lose
        if idx in s.bombs:
            s.active = False
            mines_sessions[uid] = s
            await c.message.edit_text(f"💥 Bomb! Yutqazding.\nBet: {s.bet}", reply_markup=main_menu_kb(is_admin(uid)))
            await c.answer()
            return

        s.opened.add(idx)
        mines_sessions[uid] = s
        opened_count = len(s.opened)
        await c.message.edit_text(
            f"💣 Mines\nBet: {s.bet}\nOchilgan: {opened_count}\nKoef: x{mines_multiplier(opened_count):.2f}",
            reply_markup=mines_kb(uid)
        )
        await c.answer()

    elif action == "cashout":
        opened_count = len(s.opened)
        if opened_count == 0:
            await c.answer("Avval bitta katak och!")
            return
        s.active = False
        mines_sessions[uid] = s
        mult = mines_multiplier(opened_count)
        win = int(round(s.bet * mult))
        await add_balance(uid, win)
        await c.message.edit_text(f"✅ Cashout!\nKoef: x{mult:.2f}\n💵 Yutuq: {win}", reply_markup=main_menu_kb(is_admin(uid)))
        await c.answer()

    elif action == "stop":
        s.active = False
        mines_sessions[uid] = s
        await c.message.edit_text("❌ Mines to‘xtatildi.", reply_markup=main_menu_kb(is_admin(uid)))
        await c.answer()


@dp.callback_query(F.data.startswith("admin:"))
async def admin_router(c: types.CallbackQuery):
    uid = c.from_user.id
    if not is_admin(uid):
        return await c.answer("Ruxsat yo‘q.")

    cmd = c.data.split(":")[1]
    if cmd == "panel":
        kb = InlineKeyboardBuilder()
        kb.button(text="📥 Deposit so‘rovlar", callback_data="admin:deps")
        kb.button(text="📤 Withdraw so‘rovlar", callback_data="admin:wds")
        kb.button(text="➕ /addbal", callback_data="admin:how_addbal")
        kb.button(text="🎁 Promo yaratish", callback_data="admin:promo_help")
        kb.button(text="⚙️ Ref % sozlash", callback_data="admin:ref_help")
        kb.adjust(2,2,1)
        await c.message.edit_text("🛠 Admin panel", reply_markup=kb.as_markup())
        await c.answer()

    elif cmd == "how_addbal":
        await c.message.edit_text("✅ /addbal USER_ID AMOUNT\nMasalan: /addbal 123456 5000")
        await c.answer()

    elif cmd == "promo_help":
        await c.message.edit_text("🎁 /mkpromo CODE AMOUNT MAXUSES\nMasalan: /mkpromo BONUS10 10000 50")
        await c.answer()

    elif cmd == "ref_help":
        await c.message.edit_text("⚙️ /setref 3   (foiz)\nMasalan: /setref 5")
        await c.answer()

    elif cmd == "deps":
        # show latest 10 pending
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT id,user_id,amount,created_at FROM deposit_requests WHERE status='pending' ORDER BY id DESC LIMIT 10"
            )
            rows = await cur.fetchall()
        if not rows:
            await c.message.edit_text("Pending deposit yo‘q.", reply_markup=main_menu_kb(True))
            return await c.answer()
        text = "📥 Pending deposit:\n"
        kb = InlineKeyboardBuilder()
        for rid, user_id, amount, _ in rows:
            text += f"\n#{rid} | {user_id} | {amount}"
            kb.button(text=f"✅ #{rid}", callback_data=f"admin:dep_ok:{rid}")
            kb.button(text=f"❌ #{rid}", callback_data=f"admin:dep_no:{rid}")
        kb.adjust(2)
        await c.message.edit_text(text, reply_markup=kb.as_markup())
        await c.answer()

    elif cmd == "wds":
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT id,user_id,amount,wallet,created_at FROM withdraw_requests WHERE status='pending' ORDER BY id DESC LIMIT 10"
            )
            rows = await cur.fetchall()
        if not rows:
            await c.message.edit_text("Pending withdraw yo‘q.", reply_markup=main_menu_kb(True))
            return await c.answer()
        text = "📤 Pending withdraw:\n"
        kb = InlineKeyboardBuilder()
        for rid, user_id, amount, wallet, _ in rows:
            text += f"\n#{rid} | {user_id} | {amount} | {wallet or '-'}"
            kb.button(text=f"✅ #{rid}", callback_data=f"admin:wd_ok:{rid}")
            kb.button(text=f"❌ #{rid}", callback_data=f"admin:wd_no:{rid}")
        kb.adjust(2)
        await c.message.edit_text(text, reply_markup=kb.as_markup())
        await c.answer()

    elif cmd in ("dep_ok", "dep_no", "wd_ok", "wd_no"):
        # handled in separate callback patterns below
        await c.answer()

@dp.callback_query(F.data.startswith("admin:dep_ok:"))
async def admin_dep_ok(c: types.CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer("No.")
    rid = int(c.data.split(":")[2])
    admin_id = c.from_user.id
    now = int(time.time())

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id, amount, status FROM deposit_requests WHERE id=?", (rid,))
        row = await cur.fetchone()
        if not row or row[2] != "pending":
            return await c.answer("Topilmadi/pending emas.")
        user_id, amount = int(row[0]), int(row[1])

        await db.execute("UPDATE deposit_requests SET status='approved', handled_by=?, handled_at=? WHERE id=?",
                         (admin_id, now, rid))
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (amount, user_id))

        # referral bonus if user has ref_by
        cur2 = await db.execute("SELECT ref_by FROM users WHERE user_id=?", (user_id,))
        r2 = await cur2.fetchone()
        ref_by = r2[0] if r2 else None
        if ref_by:
            ref_percent = int(await get_setting("ref_bonus_percent") or "0")
            bonus = int(amount * ref_percent / 100)
            if bonus > 0:
                await db.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (bonus, int(ref_by)))
        await db.commit()

    await c.bot.send_message(user_id, f"✅ Deposit tasdiqlandi: {amount}")
    await c.message.edit_text("✅ Deposit tasdiqlandi.")
    await c.answer()

@dp.callback_query(F.data.startswith("admin:dep_no:"))
async def admin_dep_no(c: types.CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer("No.")
    rid = int(c.data.split(":")[2])
    admin_id = c.from_user.id
    now = int(time.time())

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id, amount, status FROM deposit_requests WHERE id=?", (rid,))
        row = await cur.fetchone()
        if not row or row[2] != "pending":
            return await c.answer("Topilmadi/pending emas.")
        user_id, amount = int(row[0]), int(row[1])
        await db.execute("UPDATE deposit_requests SET status='rejected', handled_by=?, handled_at=? WHERE id=?",
                         (admin_id, now, rid))
        await db.commit()

    await c.bot.send_message(user_id, f"❌ Deposit rad etildi: {amount}")
    await c.message.edit_text("❌ Deposit rad etildi.")
    await c.answer()

@dp.callback_query(F.data.startswith("admin:wd_ok:"))
async def admin_wd_ok(c: types.CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer("No.")
    rid = int(c.data.split(":")[2])
    admin_id = c.from_user.id
    now = int(time.time())

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id, amount, status FROM withdraw_requests WHERE id=?", (rid,))
        row = await cur.fetchone()
        if not row or row[2] != "pending":
            return await c.answer("Topilmadi/pending emas.")
        user_id, amount = int(row[0]), int(row[1])

        # withdraw approved -> just mark approved (admin actually pays outside)
        await db.execute("UPDATE withdraw_requests SET status='approved', handled_by=?, handled_at=? WHERE id=?",
                         (admin_id, now, rid))
        await db.commit()

    await c.bot.send_message(user_id, f"✅ Withdraw tasdiqlandi: {amount}\nAdmin to‘lovni tashqarida amalga oshiradi.")
    await c.message.edit_text("✅ Withdraw tasdiqlandi.")
    await c.answer()

@dp.callback_query(F.data.startswith("admin:wd_no:"))
async def admin_wd_no(c: types.CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer("No.")
    rid = int(c.data.split(":")[2])
    admin_id = c.from_user.id
    now = int(time.time())

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id, amount, status FROM withdraw_requests WHERE id=?", (rid,))
        row = await cur.fetchone()
        if not row or row[2] != "pending":
            return await c.answer("Topilmadi/pending emas.")
        user_id, amount = int(row[0]), int(row[1])

        # if rejected -> return money to user
        await db.execute("UPDATE withdraw_requests SET status='rejected', handled_by=?, handled_at=? WHERE id=?",
                         (admin_id, now, rid))
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (amount, user_id))
        await db.commit()

    await c.bot.send_message(user_id, f"❌ Withdraw rad etildi, balansga qaytarildi: {amount}")
    await c.message.edit_text("❌ Withdraw rad etildi.")
    await c.answer()

# =======================
# ADMIN COMMANDS
# =======================
@dp.message(Command("addbal"))
async def cmd_addbal(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    parts = (m.text or "").split()
    if len(parts) != 3 or (not parts[1].isdigit()) or (not parts[2].lstrip("-").isdigit()):
        return await m.answer("Format: /addbal USER_ID AMOUNT")
    uid = int(parts[1]); amt = int(parts[2])
    await ensure_user(uid)
    await add_balance(uid, amt)
    await m.answer(f"✅ Balans o‘zgardi: {uid} -> {amt}")
    await m.bot.send_message(uid, f"💰 Balansingiz yangilandi: {amt:+d}")

@dp.message(Command("mkpromo"))
async def cmd_mkpromo(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    parts = (m.text or "").split()
    if len(parts) != 4:
        return await m.answer("Format: /mkpromo CODE AMOUNT MAXUSES")
    code = parts[1].upper()
    if not parts[2].isdigit() or not parts[3].isdigit():
        return await m.answer("AMOUNT va MAXUSES son bo‘lsin.")
    amount = int(parts[2]); maxuses = int(parts[3])
    now = int(time.time())

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO promo_codes(code,amount,max_uses,used_count,created_by,created_at) VALUES(?,?,?,?,?,?)",
            (code, amount, maxuses, 0, m.from_user.id, now)
        )
        await db.commit()
    await m.answer(f"🎁 Promo yaratildi: {code} | +{amount} | max {maxuses}")

@dp.message(Command("setref"))
async def cmd_setref(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    parts = (m.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        return await m.answer("Format: /setref 3")
    val = int(parts[1])
    if val < 0 or val > 50:
        return await m.answer("0..50 oralig‘ida kiriting.")
    await set_setting("ref_bonus_percent", str(val))
    await m.answer(f"✅ Ref bonus: {val}%")

# =======================
# TEXT INPUT FLOW
# =======================
@dp.message(F.text)
async def text_flow(m: types.Message):
    uid = m.from_user.id
    await ensure_user(uid)

    st = awaiting.get(uid)
    if not st:
        return  # ignore

    mode = st.get("mode", "")
    txt = (m.text or "").strip()

    if mode == "deposit_amount":
        if not txt.isdigit():
            return await m.answer("Summani son bilan yozing.")
        amount = int(txt)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO deposit_requests(user_id,amount,status,created_at) VALUES(?,?, 'pending', ?)",
                (uid, amount, int(time.time()))
            )
            await db.commit()
        awaiting.pop(uid, None)
        # notify admins
        for a in ADMIN_IDS:
            await m.bot.send_message(a, f"📥 Deposit so‘rovi\nUser: {uid}\nAmount: {amount}\n(admin panel -> Deposit so‘rovlar)")
        await m.answer("✅ Deposit so‘rovi yuborildi. Admin tasdiqlasa balans tushadi.", reply_markup=main_menu_kb(is_admin(uid)))

    elif mode == "withdraw_amount":
        if not txt.isdigit():
            return await m.answer("Summani son bilan yozing.")
        amount = int(txt)
        bal, _, _ = await get_user(uid)
        if bal < amount:
            awaiting.pop(uid, None)
            return await m.answer("❌ Balans yetarli emas.", reply_markup=main_menu_kb(is_admin(uid)))
        # take immediately, then if rejected admin returns
        ok = await take_balance(uid, amount)
        if not ok:
            awaiting.pop(uid, None)
            return await m.answer("❌ Balans yetarli emas.", reply_markup=main_menu_kb(is_admin(uid)))
        awaiting[uid] = {"mode": "withdraw_wallet", "amount": str(amount)}
        await m.answer("Kartangiz/hamyon rekvizitini yozing (masalan: 9860.... yoki TRC20 adres):")

    elif mode == "withdraw_wallet":
        amount = int(st.get("amount", "0"))
        wallet = txt[:200]
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO withdraw_requests(user_id,amount,wallet,status,created_at) VALUES(?,?,?, 'pending', ?)",
                (uid, amount, wallet, int(time.time()))
            )
            await db.commit()
        awaiting.pop(uid, None)
        for a in ADMIN_IDS:
            await m.bot.send_message(a, f"📤 Withdraw so‘rovi\nUser: {uid}\nAmount: {amount}\nWallet: {wallet}\n(admin panel -> Withdraw so‘rovlar)")
        await m.answer("✅ Withdraw so‘rovi yuborildi. Admin ko‘rib chiqadi.", reply_markup=main_menu_kb(is_admin(uid)))

    elif mode == "promo_enter":
        code = txt.upper()
        now = int(time.time())
        async with aiosqlite.connect(DB_PATH) as db:
            # already used?
            cur = await db.execute("SELECT 1 FROM promo_uses WHERE user_id=? AND code=?", (uid, code))
            if await cur.fetchone():
                awaiting.pop(uid, None)
                return await m.answer("❌ Bu promo sizda ishlatilgan.", reply_markup=main_menu_kb(is_admin(uid)))

            cur = await db.execute("SELECT amount,max_uses,used_count FROM promo_codes WHERE code=?", (code,))
            row = await cur.fetchone()
            if not row:
                awaiting.pop(uid, None)
                return await m.answer("❌ Promo topilmadi.", reply_markup=main_menu_kb(is_admin(uid)))

            amount, max_uses, used_count = int(row[0]), int(row[1]), int(row[2])
            if used_count >= max_uses:
                awaiting.pop(uid, None)
                return await m.answer("❌ Promo limiti tugagan.", reply_markup=main_menu_kb(is_admin(uid)))

            await db.execute("INSERT INTO promo_uses(user_id,code,used_at) VALUES(?,?,?)", (uid, code, now))
            await db.execute("UPDATE promo_codes SET used_count = used_count + 1 WHERE code=?", (code,))
            await db.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (amount, uid))
            await db.commit()

        awaiting.pop(uid, None)
        await m.answer(f"✅ Promo qabul qilindi: +{amount}", reply_markup=main_menu_kb(is_admin(uid)))

    elif mode == "mines_bet":
        if not txt.isdigit():
            return await m.answer("Bet summasini son bilan yozing.")
        bet = int(txt)
        bal, _, _ = await get_user(uid)
        if bal < bet or bet <= 0:
            awaiting.pop(uid, None)
            return await m.answer("❌ Balans yetarli emas.", reply_markup=main_menu_kb(is_admin(uid)))

        ok = await take_balance(uid, bet)
        if not ok:
            awaiting.pop(uid, None)
            return await m.answer("❌ Balans yetarli emas.", reply_markup=main_menu_kb(is_admin(uid)))

        mines_sessions[uid] = MinesSession(bet=bet, bombs=set(), opened=set(), active=True)
        awaiting.pop(uid, None)
        await m.answer("💣 Mines boshlandi! Katak tanlang:", reply_markup=mines_kb(uid))

    elif mode == "aviator_bet":
        if not txt.isdigit():
            return await m.answer("Bet summasini son bilan yozing.")
        bet = int(txt)
        bal, _, _ = await get_user(uid)
        if bal < bet or bet <= 0:
            awaiting.pop(uid, None)
            return await m.answer("❌ Balans yetarli emas.", reply_markup=main_menu_kb(is_admin(uid)))

        ok = await take_balance(uid, bet)
        if not ok:
            awaiting.pop(uid, None)
            return await m.answer("❌ Balans yetarli emas.", reply_markup=main_menu_kb(is_admin(uid)))

        awaiting.pop(uid, None)
        aviator_sessions[uid] = AviatorSession(bet=bet, mult=1.00, crashed=False, cashed_out=False)
        await m.answer("✈️ Aviator boshlandi!\nKoef oshadi. /stop bosib cashout qiling.", reply_markup=main_menu_kb(is_admin(uid)))
        asyncio.create_task(aviator_loop(m.bot, uid))

# =======================
# RUN
# =======================
async def main():
    await db_init()
    bot = Bot(TOKEN)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
