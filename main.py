import asyncio
import random
import time
from dataclasses import dataclass
from typing import Dict, Optional, Set, Tuple

import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command

# ======================
# CONFIG
# ======================
TOKEN = "8161107014:AAGBWEYVxie7-pB4-2FoGCPjCv_sl0yHogc"
ADMIN_IDS = {5815294733}
DB_PATH = "casino.db"

# Cards
CARD_HUMO = "9860 6067 5024 7151"
CARD_UZCARD = "8600 0000 0000 0000"

# Deposit limits
MIN_DEP = 20000
MAX_DEP = 2000000

# Daily bonus (BONUS balance)
DAILY_BONUS_AMOUNT = 3000

# Mines
MINES_SIZE = 5
MINES_BOMBS = 3

# Aviator
AVIATOR_TICK_SEC = 0.7
AVIATOR_GROWTH = 0.06  # multiplier growth per tick

dp = Dispatcher()

# ======================
# DB
# ======================
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS users(
  user_id INTEGER PRIMARY KEY,
  real_balance INTEGER NOT NULL DEFAULT 0,
  bonus_balance INTEGER NOT NULL DEFAULT 0,
  deposit_verified INTEGER NOT NULL DEFAULT 0,

  ref_by INTEGER,
  ref_count INTEGER NOT NULL DEFAULT 0,

  last_daily_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS promo_codes(
  code TEXT PRIMARY KEY,
  amount INTEGER NOT NULL,
  max_uses INTEGER NOT NULL,
  used_count INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS promo_uses(
  user_id INTEGER NOT NULL,
  code TEXT NOT NULL,
  used_at INTEGER NOT NULL,
  PRIMARY KEY(user_id, code)
);

CREATE TABLE IF NOT EXISTS deposit_requests(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  amount INTEGER NOT NULL,
  method TEXT NOT NULL,
  receipt_file_id TEXT,
  status TEXT NOT NULL DEFAULT 'pending', -- pending/approved/rejected
  created_at INTEGER NOT NULL,
  handled_by INTEGER,
  handled_at INTEGER
);

CREATE TABLE IF NOT EXISTS withdraw_requests(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  amount INTEGER NOT NULL,
  card TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at INTEGER NOT NULL,
  handled_by INTEGER,
  handled_at INTEGER
);
"""

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(CREATE_SQL)
        await db.commit()

async def ensure_user(uid: int, ref_by: Optional[int] = None):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM users WHERE user_id=?", (uid,))
        row = await cur.fetchone()
        if row is None:
            ref = ref_by if (ref_by and ref_by != uid) else None
            await db.execute(
                "INSERT INTO users(user_id, real_balance, bonus_balance, deposit_verified, ref_by, ref_count, last_daily_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (uid, 0, 0, 0, ref, 0, 0)
            )
            # referral count +1
            if ref:
                await db.execute("UPDATE users SET ref_count = ref_count + 1 WHERE user_id=?", (ref,))
        await db.commit()

async def get_user(uid: int) -> Tuple[int, int, int, Optional[int], int, int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT real_balance, bonus_balance, deposit_verified, ref_by, ref_count, last_daily_at "
            "FROM users WHERE user_id=?",
            (uid,)
        )
        row = await cur.fetchone()
        if not row:
            return 0, 0, 0, None, 0, 0
        return int(row[0]), int(row[1]), int(row[2]), row[3], int(row[4]), int(row[5])

async def add_real(uid: int, amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET real_balance = real_balance + ? WHERE user_id=?", (amount, uid))
        await db.commit()

async def add_bonus(uid: int, amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET bonus_balance = bonus_balance + ? WHERE user_id=?", (amount, uid))
        await db.commit()

async def take_real(uid: int, amount: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT real_balance FROM users WHERE user_id=?", (uid,))
        row = await cur.fetchone()
        if not row or int(row[0]) < amount:
            return False
        await db.execute("UPDATE users SET real_balance = real_balance - ? WHERE user_id=?", (amount, uid))
        await db.commit()
        return True

async def take_bonus(uid: int, amount: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT bonus_balance FROM users WHERE user_id=?", (uid,))
        row = await cur.fetchone()
        if not row or int(row[0]) < amount:
            return False
        await db.execute("UPDATE users SET bonus_balance = bonus_balance - ? WHERE user_id=?", (amount, uid))
        await db.commit()
        return True

async def set_last_daily(uid: int, ts: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET last_daily_at=? WHERE user_id=?", (ts, uid))
        await db.commit()

# ======================
# Reply Keyboards (pastda)
# ======================
def main_menu_kb(admin: bool) -> types.ReplyKeyboardMarkup:
    rows = [
        [types.KeyboardButton(text="💣 Mines"), types.KeyboardButton(text="✈️ Aviator")],
        [types.KeyboardButton(text="➕ Hisob to‘ldirish"), types.KeyboardButton(text="📤 Pul yechish")],
        [types.KeyboardButton(text="💰 Balans"), types.KeyboardButton(text="🎁 Promo code")],
        [types.KeyboardButton(text="🎁 Kunlik bonus"), types.KeyboardButton(text="🤝 Referal")],
        [types.KeyboardButton(text="ℹ️ Yordam"), types.KeyboardButton(text="❌ Bekor qilish")],
    ]
    if admin:
        rows.append([types.KeyboardButton(text="🛠 Admin panel")])
    return types.ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

def deposit_method_kb() -> types.ReplyKeyboardMarkup:
    rows = [
        [types.KeyboardButton(text="🟦 HUMO"), types.KeyboardButton(text="💳 UZCARD")],
        [types.KeyboardButton(text="🔙 Menyu"), types.KeyboardButton(text="❌ Bekor qilish")]
    ]
    return types.ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

def deposit_amount_quick_kb() -> types.ReplyKeyboardMarkup:
    rows = [
        [types.KeyboardButton(text="20000"), types.KeyboardButton(text="50000"), types.KeyboardButton(text="100000")],
        [types.KeyboardButton(text="200000"), types.KeyboardButton(text="500000"), types.KeyboardButton(text="Boshqa summa")],
        [types.KeyboardButton(text="🔙 Menyu"), types.KeyboardButton(text="❌ Bekor qilish")]
    ]
    return types.ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

def back_to_menu_kb(admin: bool) -> types.ReplyKeyboardMarkup:
    rows = [[types.KeyboardButton(text="🔙 Menyu")], [types.KeyboardButton(text="❌ Bekor qilish")]]
    if admin:
        rows.append([types.KeyboardButton(text="🛠 Admin panel")])
    return types.ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

# Mines grid keyboard (25 buttons)
def mines_grid_kb(session: "MinesSession") -> types.ReplyKeyboardMarkup:
    rows = []
    for r in range(MINES_SIZE):
        row = []
        for c in range(MINES_SIZE):
            idx = r * MINES_SIZE + c
            if idx in session.opened:
                row.append(types.KeyboardButton(text=f"✅{idx+1}"))
            else:
                row.append(types.KeyboardButton(text=f"⬜️{idx+1}"))
        rows.append(row)
    rows.append([types.KeyboardButton(text="💵 Cashout"), types.KeyboardButton(text="❌ Stop")])
    return types.ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

def aviator_kb() -> types.ReplyKeyboardMarkup:
    rows = [
        [types.KeyboardButton(text="💸 Cashout"), types.KeyboardButton(text="❌ Stop")],
        [types.KeyboardButton(text="🔙 Menyu")]
    ]
    return types.ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

# ======================
# Step states
# ======================
steps: Dict[int, Dict[str, str]] = {}  # uid -> {"mode": "...", ...}

# ======================
# Mines logic
# ======================
@dataclass
class MinesSession:
    bet: int
    wallet: str  # "real" or "bonus"
    bombs: Set[int]
    opened: Set[int]
    active: bool = True

mines_sessions: Dict[int, MinesSession] = {}

def mines_multiplier(opened_count: int) -> float:
    table = [1.00, 1.10, 1.20, 1.35, 1.55, 1.80, 2.10, 2.50, 3.00]
    if opened_count < len(table):
        return table[opened_count]
    return round(table[-1] + (opened_count - (len(table)-1)) * 0.8, 2)

def gen_bombs(exclude_idx: int) -> Set[int]:
    cells = list(range(MINES_SIZE * MINES_SIZE))
    cells.remove(exclude_idx)
    return set(random.sample(cells, MINES_BOMBS))

# ======================
# Aviator logic
# ======================
@dataclass
class AviatorSession:
    bet: int
    wallet: str  # "real" or "bonus"
    mult: float = 1.0
    crashed: bool = False
    cashed_out: bool = False

aviator_sessions: Dict[int, AviatorSession] = {}

async def aviator_loop(bot: Bot, uid: int):
    while True:
        await asyncio.sleep(AVIATOR_TICK_SEC)
        s = aviator_sessions.get(uid)
        if not s or s.cashed_out or s.crashed:
            return

        s.mult = round(s.mult * (1.0 + AVIATOR_GROWTH), 2)

        # Crash probability grows with multiplier (halol random, "azartli")
        crash_prob = min(0.02 + (s.mult - 1.0) * 0.015, 0.45)
        if random.random() < crash_prob:
            s.crashed = True
            aviator_sessions[uid] = s
            await bot.send_message(uid, f"💥 Crash! x{s.mult:.2f}\n😅 Keyingi safar omad keladi, yana urinib ko‘r!", reply_markup=back_to_menu_kb(is_admin(uid)))
            return

        aviator_sessions[uid] = s
        # vaqti-vaqti bilan update
        if int(s.mult * 100) % 25 == 0:
            await bot.send_message(uid, f"✈️ Koef: x{s.mult:.2f}", reply_markup=aviator_kb())

# ======================
# START
# ======================
@dp.message(CommandStart())
async def start(m: types.Message):
    uid = m.from_user.id
    parts = (m.text or "").split()
    ref_by = None
    if len(parts) == 2 and parts[1].isdigit():
        ref_by = int(parts[1])
    await ensure_user(uid, ref_by=ref_by)
    await m.answer("Menyu 👇", reply_markup=main_menu_kb(is_admin(uid)))

# ======================
# Cancel / Menu
# ======================
@dp.message(F.text == "❌ Bekor qilish")
async def cancel(m: types.Message):
    uid = m.from_user.id
    steps.pop(uid, None)
    mines_sessions.pop(uid, None)
    aviator_sessions.pop(uid, None)
    await m.answer("✅ Bekor qilindi.", reply_markup=main_menu_kb(is_admin(uid)))

@dp.message(F.text == "🔙 Menyu")
async def back_menu(m: types.Message):
    uid = m.from_user.id
    steps.pop(uid, None)
    mines_sessions.pop(uid, None)
    aviator_sessions.pop(uid, None)
    await m.answer("Menyu 👇", reply_markup=main_menu_kb(is_admin(uid)))

# ======================
# BALANCE
# ======================
@dp.message(F.text == "💰 Balans")
async def balance(m: types.Message):
    uid = m.from_user.id
    await ensure_user(uid)
    real, bonus, dep, _, refc, _ = await get_user(uid)
    await m.answer(
        f"💰 Balans:\n"
        f"✅ Real: {real}\n"
        f"🎁 Bonus: {bonus}\n"
        f"📌 Deposit: {'✅ Tasdiqlangan' if dep else '❌ Yo‘q'}\n"
        f"👥 Referal: {refc}",
        reply_markup=main_menu_kb(is_admin(uid))
    )

# ======================
# HELP
# ======================
@dp.message(F.text == "ℹ️ Yordam")
async def help_(m: types.Message):
    await m.answer(
        "ℹ️ Qoidalar:\n"
        "• Promo/Kunlik/Referal bonus -> BONUS balans (chiqmaydi)\n"
        "• Withdraw faqat DEPOSIT tasdiqlangan va REAL balansdan\n"
        "• Deposit: chek yuborasiz, admin tasdiqlaydi\n"
    )

# ======================
# REFERRAL
# ======================
@dp.message(F.text == "🤝 Referal")
async def referal(m: types.Message):
    uid = m.from_user.id
    await ensure_user(uid)
    me = await m.bot.get_me()
    _, _, _, _, refc, _ = await get_user(uid)
    link = f"https://t.me/{me.username}?start={uid}"
    await m.answer(f"🤝 Referal link:\n{link}\n👥 Taklif qilganlar: {refc}")

# ======================
# DAILY BONUS (no channels)
# ======================
@dp.message(F.text == "🎁 Kunlik bonus")
async def daily(m: types.Message):
    uid = m.from_user.id
    await ensure_user(uid)
    _, _, _, _, _, last_daily = await get_user(uid)
    now = int(time.time())

    if now - int(last_daily) < 86400:
        left = 86400 - (now - int(last_daily))
        h = left // 3600
        mi = (left % 3600) // 60
        return await m.answer(f"⏳ Kunlik bonus hali tayyor emas.\nQolgan: {h} soat {mi} daqiqa")

    await add_bonus(uid, DAILY_BONUS_AMOUNT)
    await set_last_daily(uid, now)
    await m.answer(f"✅ Kunlik bonus: +{DAILY_BONUS_AMOUNT} (BONUS balans)")

# ======================
# PROMO
# ======================
@dp.message(F.text == "🎁 Promo code")
async def promo_enter(m: types.Message):
    uid = m.from_user.id
    await ensure_user(uid)
    steps[uid] = {"mode": "promo_enter"}
    await m.answer("🎁 Promo kodni yuboring (masalan: BONUS10)")

@dp.message(Command("mkpromo"))
async def mkpromo(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    parts = (m.text or "").split()
    if len(parts) != 4:
        return await m.answer("Format: /mkpromo CODE AMOUNT MAXUSES\nMasalan: /mkpromo BONUS10 10000 50")
    code = parts[1].upper()
    if not parts[2].isdigit() or not parts[3].isdigit():
        return await m.answer("AMOUNT va MAXUSES son bo‘lsin.")
    amount = int(parts[2]); maxuses = int(parts[3])

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO promo_codes(code,amount,max_uses,used_count,created_at) VALUES(?,?,?,?,?)",
            (code, amount, maxuses, 0, int(time.time()))
        )
        await db.commit()
    await m.answer(f"✅ Promo yaratildi: {code} | +{amount} | max={maxuses}")

# ======================
# DEPOSIT
# ======================
@dp.message(F.text == "➕ Hisob to‘ldirish")
async def dep_start(m: types.Message):
    uid = m.from_user.id
    await ensure_user(uid)
    steps[uid] = {"mode": "dep_amount"}
    await m.answer(
        f"➕ Hisob to‘ldirish\nSummani tanlang yoki 'Boshqa summa' bosing.\nMin {MIN_DEP} / Max {MAX_DEP}",
        reply_markup=deposit_amount_quick_kb()
    )

# ======================
# WITHDRAW
# ======================
@dp.message(F.text == "📤 Pul yechish")
async def wd_start(m: types.Message):
    uid = m.from_user.id
    await ensure_user(uid)
    real, bonus, dep, *_ = await get_user(uid)

    if not dep:
        return await m.answer("❌ Pul yechish uchun avval deposit qiling va admin tasdiqlasin.")

    if real <= 0:
        return await m.answer("❌ Real balans 0. Bonus balans chiqmaydi.")

    steps[uid] = {"mode": "wd_amount"}
    await m.answer("📤 Pul yechish\nSummani yozing (faqat REAL balansdan).")

# ======================
# MINES
# ======================
@dp.message(F.text == "💣 Mines")
async def mines_enter(m: types.Message):
    uid = m.from_user.id
    await ensure_user(uid)
    real, bonus, dep, *_ = await get_user(uid)

    if real <= 0 and bonus <= 0:
        return await m.answer("Balans yo‘q. Avval deposit yoki bonus oling.")

    # foydalanuvchi qaysi balans bilan o'ynaydi: real bo'lsa real, bo'lmasa bonus
    wallet = "real" if real > 0 else "bonus"
    steps[uid] = {"mode": "mines_bet", "wallet": wallet}
    await m.answer(
        f"💣 Mines (5x5, 3 bomba)\nStavkani yozing.\nBalans turi: {wallet.upper()}",
        reply_markup=back_to_menu_kb(is_admin(uid))
    )

# ======================
# AVIATOR
# ======================
@dp.message(F.text == "✈️ Aviator")
async def aviator_enter(m: types.Message):
    uid = m.from_user.id
    await ensure_user(uid)
    real, bonus, dep, *_ = await get_user(uid)

    if real <= 0 and bonus <= 0:
        return await m.answer("Balans yo‘q. Avval deposit yoki bonus oling.")

    wallet = "real" if real > 0 else "bonus"
    steps[uid] = {"mode": "aviator_bet", "wallet": wallet}
    await m.answer(
        f"✈️ Aviator\nStavkani yozing.\nBalans turi: {wallet.upper()}",
        reply_markup=back_to_menu_kb(is_admin(uid))
    )

# ======================
# ADMIN PANEL (minimal via commands)
# ======================
@dp.message(F.text == "🛠 Admin panel")
async def admin_panel(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    await m.answer(
        "🛠 Admin buyruqlar:\n"
        "• /deps - pending depositlar\n"
        "• /dep_ok ID - deposit approve\n"
        "• /dep_no ID - deposit reject\n"
        "• /wds - pending withdrawlar\n"
        "• /wd_ok ID - withdraw approve\n"
        "• /wd_no ID - withdraw reject (pul qaytadi)\n"
        "• /mkpromo CODE AMOUNT MAXUSES\n"
    )

@dp.message(Command("deps"))
async def list_deps(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id,user_id,amount,method,status FROM deposit_requests ORDER BY id DESC LIMIT 10")
        rows = await cur.fetchall()
    if not rows:
        return await m.answer("Depositlar yo‘q.")
    txt = "📥 Depositlar (oxirgi 10):\n"
    for r in rows:
        txt += f"#{r[0]} | uid={r[1]} | {r[2]} | {r[3]} | {r[4]}\n"
    await m.answer(txt)

@dp.message(Command("wds"))
async def list_wds(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id,user_id,amount,card,status FROM withdraw_requests ORDER BY id DESC LIMIT 10")
        rows = await cur.fetchall()
    if not rows:
        return await m.answer("Withdrawlar yo‘q.")
    txt = "📤 Withdrawlar (oxirgi 10):\n"
    for r in rows:
        txt += f"#{r[0]} | uid={r[1]} | {r[2]} | {r[3]} | {r[4]}\n"
    await m.answer(txt)

@dp.message(Command("dep_ok"))
async def dep_ok(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    parts = (m.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        return await m.answer("Format: /dep_ok ID")
    rid = int(parts[1])

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id,amount,status FROM deposit_requests WHERE id=?", (rid,))
        row = await cur.fetchone()
        if not row or row[2] != "pending":
            return await m.answer("Topilmadi yoki pending emas.")
        uid, amount = int(row[0]), int(row[1])

        await db.execute("UPDATE deposit_requests SET status='approved', handled_by=?, handled_at=? WHERE id=?",
                         (m.from_user.id, int(time.time()), rid))
        await db.execute("UPDATE users SET real_balance=real_balance+?, deposit_verified=1 WHERE user_id=?",
                         (amount, uid))
        await db.commit()

    await m.answer(f"✅ Deposit tasdiqlandi #{rid}")
    try:
        await m.bot.send_message(uid, f"✅ Deposit tasdiqlandi: +{amount} (REAL balans)", reply_markup=main_menu_kb(is_admin(uid)))
    except:
        pass

@dp.message(Command("dep_no"))
async def dep_no(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    parts = (m.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        return await m.answer("Format: /dep_no ID")
    rid = int(parts[1])

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id,amount,status FROM deposit_requests WHERE id=?", (rid,))
        row = await cur.fetchone()
        if not row or row[2] != "pending":
            return await m.answer("Topilmadi yoki pending emas.")
        uid, amount = int(row[0]), int(row[1])

        await db.execute("UPDATE deposit_requests SET status='rejected', handled_by=?, handled_at=? WHERE id=?",
                         (m.from_user.id, int(time.time()), rid))
        await db.commit()

    await m.answer(f"❌ Deposit rad etildi #{rid}")
    try:
        await m.bot.send_message(uid, f"❌ Deposit rad etildi: {amount}", reply_markup=main_menu_kb(is_admin(uid)))
    except:
        pass

@dp.message(Command("wd_ok"))
async def wd_ok(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    parts = (m.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        return await m.answer("Format: /wd_ok ID")
    rid = int(parts[1])

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id,amount,card,status FROM withdraw_requests WHERE id=?", (rid,))
        row = await cur.fetchone()
        if not row or row[3] != "pending":
            return await m.answer("Topilmadi yoki pending emas.")
        uid, amount, card = int(row[0]), int(row[1]), row[2]

        await db.execute("UPDATE withdraw_requests SET status='approved', handled_by=?, handled_at=? WHERE id=?",
                         (m.from_user.id, int(time.time()), rid))
        await db.commit()

    await m.answer(f"✅ Withdraw tasdiqlandi #{rid} (admin tashqarida to‘laydi)")
    try:
        await m.bot.send_message(uid, f"✅ Withdraw tasdiqlandi: {amount}\nKarta: {card}", reply_markup=main_menu_kb(is_admin(uid)))
    except:
        pass

@dp.message(Command("wd_no"))
async def wd_no(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    parts = (m.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        return await m.answer("Format: /wd_no ID")
    rid = int(parts[1])

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id,amount,status FROM withdraw_requests WHERE id=?", (rid,))
        row = await cur.fetchone()
        if not row or row[2] != "pending":
            return await m.answer("Topilmadi yoki pending emas.")
        uid, amount = int(row[0]), int(row[1])

        # reject -> return money
        await db.execute("UPDATE withdraw_requests SET status='rejected', handled_by=?, handled_at=? WHERE id=?",
                         (m.from_user.id, int(time.time()), rid))
        await db.execute("UPDATE users SET real_balance=real_balance+? WHERE user_id=?", (amount, uid))
        await db.commit()

    await m.answer(f"❌ Withdraw rad etildi #{rid} (pul qaytarildi)")
    try:
        await m.bot.send_message(uid, f"❌ Withdraw rad etildi. Pul qaytarildi: {amount}", reply_markup=main_menu_kb(is_admin(uid)))
    except:
        pass

# ======================
# TEXT / PHOTO FLOW (steps)
# ======================
@dp.message(F.photo)
async def handle_photo(m: types.Message):
    uid = m.from_user.id
    await ensure_user(uid)

    st = steps.get(uid)
    if not st or st.get("mode") != "dep_wait_receipt":
        return

    file_id = m.photo[-1].file_id
    rid = int(st["rid"])

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE deposit_requests SET receipt_file_id=? WHERE id=?", (file_id, rid))
        await db.commit()

    steps.pop(uid, None)
    await m.answer("✅ Chek qabul qilindi. Admin tekshiradi.", reply_markup=main_menu_kb(is_admin(uid)))

    # notify admins with receipt
    for a in ADMIN_IDS:
        try:
            await m.bot.send_photo(
                a,
                photo=file_id,
                caption=f"📥 Deposit chek\nID: #{rid}\nUser: {uid}\nAmount: {st['amount']}\nMethod: {st['method']}\n\nTasdiq: /dep_ok {rid}\nRad: /dep_no {rid}"
            )
        except:
            pass

@dp.message(F.text)
async def handle_text(m: types.Message):
    uid = m.from_user.id
    await ensure_user(uid)
    text = (m.text or "").strip()

    # ----- steps -----
    st = steps.get(uid)

    # PROMO ENTER
    if st and st.get("mode") == "promo_enter":
        code = text.upper()
        now = int(time.time())
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT 1 FROM promo_uses WHERE user_id=? AND code=?", (uid, code))
            if await cur.fetchone():
                steps.pop(uid, None)
                return await m.answer("❌ Bu promo sizda ishlatilgan.", reply_markup=main_menu_kb(is_admin(uid)))

            cur = await db.execute("SELECT amount,max_uses,used_count FROM promo_codes WHERE code=?", (code,))
            row = await cur.fetchone()
            if not row:
                steps.pop(uid, None)
                return await m.answer("❌ Promo topilmadi.", reply_markup=main_menu_kb(is_admin(uid)))

            amount, max_uses, used_count = int(row[0]), int(row[1]), int(row[2])
            if used_count >= max_uses:
                steps.pop(uid, None)
                return await m.answer("❌ Promo limiti tugagan.", reply_markup=main_menu_kb(is_admin(uid)))

            await db.execute("INSERT INTO promo_uses(user_id,code,used_at) VALUES(?,?,?)", (uid, code, now))
            await db.execute("UPDATE promo_codes SET used_count=used_count+1 WHERE code=?", (code,))
            await db.execute("UPDATE users SET bonus_balance=bonus_balance+? WHERE user_id=?", (amount, uid))
            await db.commit()

        steps.pop(uid, None)
        return await m.answer(f"✅ Promo qabul qilindi: +{amount} (BONUS balans)", reply_markup=main_menu_kb(is_admin(uid)))

    # DEPOSIT AMOUNT
    if st and st.get("mode") == "dep_amount_custom":
        if not text.isdigit():
            return await m.answer("Summani son bilan yozing.")
        amt = int(text)
        if amt < MIN_DEP or amt > MAX_DEP:
            return await m.answer(f"Min {MIN_DEP} / Max {MAX_DEP}")
        st["amount"] = str(amt)
        st["mode"] = "dep_method"
        return await m.answer(f"Summa: {amt}\nTo‘lov turini tanlang:", reply_markup=deposit_method_kb())

    # WITHDRAW AMOUNT
    if st and st.get("mode") == "wd_amount":
        if not text.isdigit():
            return await m.answer("Summani son bilan yozing.")
        amt = int(text)

        real, bonus, dep, *_ = await get_user(uid)
        if not dep:
            steps.pop(uid, None)
            return await m.answer("❌ Deposit qilmasdan pul yechib bo‘lmaydi.", reply_markup=main_menu_kb(is_admin(uid)))

        if amt <= 0 or amt > real:
            return await m.answer("❌ Noto‘g‘ri summa yoki REAL balans yetarli emas.")

        st["amount"] = str(amt)
        st["mode"] = "wd_card"
        return await m.answer("Kartani yozing (HUMO/UZCARD):")

    # WITHDRAW CARD
    if st and st.get("mode") == "wd_card":
        card = text[:64]
        amt = int(st["amount"])

        # take immediately; if admin rejects -> returns
        ok = await take_real(uid, amt)
        if not ok:
            steps.pop(uid, None)
            return await m.answer("❌ Real balans yetarli emas.", reply_markup=main_menu_kb(is_admin(uid)))

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO withdraw_requests(user_id,amount,card,status,created_at) VALUES(?,?,?,?,?)",
                (uid, amt, card, "pending", int(time.time()))
            )
            await db.commit()

            cur = await db.execute("SELECT last_insert_rowid()")
            rid = (await cur.fetchone())[0]

        steps.pop(uid, None)
        await m.answer("✅ Withdraw so‘rovi yuborildi. Admin ko‘rib chiqadi.", reply_markup=main_menu_kb(is_admin(uid)))

        for a in ADMIN_IDS:
            try:
                await m.bot.send_message(
                    a,
                    f"📤 Withdraw so‘rovi\nID: #{rid}\nUser: {uid}\nAmount: {amt}\nCard: {card}\n\nTasdiq: /wd_ok {rid}\nRad: /wd_no {rid}"
                )
            except:
                pass
        return

    # MINES BET
    if st and st.get("mode") == "mines_bet":
        if not text.isdigit():
            return await m.answer("Stavkani son bilan yozing.")
        bet = int(text)
        if bet <= 0:
            return await m.answer("Stavka noto‘g‘ri.")

        wallet = st["wallet"]
        real, bonus, *_ = await get_user(uid)

        if wallet == "real":
            if bet > real:
                return await m.answer("❌ Real balans yetarli emas.")
            ok = await take_real(uid, bet)
            if not ok:
                return await m.answer("❌ Real balans yetarli emas.")
        else:
            if bet > bonus:
                return await m.answer("❌ Bonus balans yetarli emas.")
            ok = await take_bonus(uid, bet)
            if not ok:
                return await m.answer("❌ Bonus balans yetarli emas.")

        steps.pop(uid, None)
        mines_sessions[uid] = MinesSession(bet=bet, wallet=wallet, bombs=set(), opened=set(), active=True)
        return await m.answer(
            f"💣 Mines boshlandi! Bet: {bet} ({wallet.upper()})\nKatak tanlang:",
            reply_markup=mines_grid_kb(mines_sessions[uid])
        )

    # AVIATOR BET
    if st and st.get("mode") == "aviator_bet":
        if not text.isdigit():
            return await m.answer("Stavkani son bilan yozing.")
        bet = int(text)
        if bet <= 0:
            return await m.answer("Stavka noto‘g‘ri.")

        wallet = st["wallet"]
        real, bonus, *_ = await get_user(uid)

        if wallet == "real":
            if bet > real:
                return await m.answer("❌ Real balans yetarli emas.")
            ok = await take_real(uid, bet)
            if not ok:
                return await m.answer("❌ Real balans yetarli emas.")
        else:
            if bet > bonus:
                return await m.answer("❌ Bonus balans yetarli emas.")
            ok = await take_bonus(uid, bet)
            if not ok:
                return await m.answer("❌ Bonus balans yetarli emas.")

        steps.pop(uid, None)
        aviator_sessions[uid] = AviatorSession(bet=bet, wallet=wallet)
        await m.answer(
            f"✈️ Aviator boshlandi! Bet: {bet} ({wallet.upper()})\nKoef sekin oshadi. '💸 Cashout' bosing.",
            reply_markup=aviator_kb()
        )
        asyncio.create_task(aviator_loop(m.bot, uid))
        return

    # ----- non-step buttons -----
    if text == "Boshqa summa":
        steps[uid] = {"mode": "dep_amount_custom"}
        return await m.answer(f"Summani yozing. Min {MIN_DEP} / Max {MAX_DEP}")

    if text.isdigit() and st and st.get("mode") == "dep_amount":
        # if user typed amount directly in quick mode
        amt = int(text)
        if amt < MIN_DEP or amt > MAX_DEP:
            return await m.answer(f"Min {MIN_DEP} / Max {MAX_DEP}")
        st["amount"] = str(amt)
        st["mode"] = "dep_method"
        return await m.answer(f"Summa: {amt}\nTo‘lov turini tanlang:", reply_markup=deposit_method_kb())

    if text in ("20000", "50000", "100000", "200000", "500000"):
        # quick deposit amount when in deposit mode
        if st and st.get("mode") == "dep_amount":
            amt = int(text)
            st["amount"] = str(amt)
            st["mode"] = "dep_method"
            return await m.answer(f"Summa: {amt}\nTo‘lov turini tanlang:", reply_markup=deposit_method_kb())

    if text == "🟦 HUMO" and st and st.get("mode") == "dep_method":
        st["method"] = "HUMO"
        st["mode"] = "dep_pay"
        amt = int(st["amount"])
        return await m.answer(
            f"🟦 HUMO\nKarta: {CARD_HUMO}\nSumma: {amt}\n\nPul yuborgach 'To‘lov qildim' deb yozing.",
            reply_markup=types.ReplyKeyboardMarkup(
                keyboard=[[types.KeyboardButton(text="✅ To‘lov qildim")],
                          [types.KeyboardButton(text="🔙 Menyu"), types.KeyboardButton(text="❌ Bekor qilish")]],
                resize_keyboard=True
            )
        )

    if text == "💳 UZCARD" and st and st.get("mode") == "dep_method":
        st["method"] = "UZCARD"
        st["mode"] = "dep_pay"
        amt = int(st["amount"])
        return await m.answer(
            f"💳 UZCARD\nKarta: {CARD_UZCARD}\nSumma: {amt}\n\nPul yuborgach 'To‘lov qildim' deb yozing.",
            reply_markup=types.ReplyKeyboardMarkup(
                keyboard=[[types.KeyboardButton(text="✅ To‘lov qildim")],
                          [types.KeyboardButton(text="🔙 Menyu"), types.KeyboardButton(text="❌ Bekor qilish")]],
                resize_keyboard=True
            )
        )

    if text == "✅ To‘lov qildim" and st and st.get("mode") == "dep_pay":
        # create deposit request, then ask receipt photo
        amt = int(st["amount"])
        method = st["method"]
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO deposit_requests(user_id,amount,method,receipt_file_id,status,created_at) VALUES(?,?,?,?,?,?)",
                (uid, amt, method, None, "pending", int(time.time()))
            )
            await db.commit()
            cur = await db.execute("SELECT last_insert_rowid()")
            rid = (await cur.fetchone())[0]

        steps[uid] = {"mode": "dep_wait_receipt", "rid": str(rid), "amount": str(amt), "method": method}
        return await m.answer("📸 Chek rasmini yuboring (PHOTO).")

    # Deposit enter (set step dep_amount)
    if text == "➕ Hisob to‘ldirish":
        steps[uid] = {"mode": "dep_amount"}
        return await m.answer(
            f"➕ Hisob to‘ldirish\nSummani tanlang yoki 'Boshqa summa' bosing.\nMin {MIN_DEP} / Max {MAX_DEP}",
            reply_markup=deposit_amount_quick_kb()
        )

    # Withdraw enter
    if text == "📤 Pul yechish":
        real, bonus, dep, *_ = await get_user(uid)
        if not dep:
            return await m.answer("❌ Pul yechish uchun avval deposit qiling va admin tasdiqlasin.")
        if real <= 0:
            return await m.answer("❌ Real balans 0. Bonus balans chiqmaydi.")
        steps[uid] = {"mode": "wd_amount"}
        return await m.answer("📤 Summani yozing (REAL balansdan).")

    # Mines cell click via reply keyboard (⬜️12 / ✅12)
    if text.startswith("⬜️") or text.startswith("✅"):
        session = mines_sessions.get(uid)
        if not session or not session.active:
            return
        # parse index
        try:
            idx = int(text[2:]) - 1
        except:
            return
        if idx < 0 or idx >= MINES_SIZE * MINES_SIZE:
            return

        # first click -> bombs generate excluding clicked cell
        if not session.bombs:
            session.bombs = gen_bombs(idx)

        if idx in session.opened:
            return

        # bomb -> lose
        if idx in session.bombs:
            session.active = False
            mines_sessions[uid] = session
            return await m.answer(
                f"💥 Bomb! Yutqazding.\n😅 Keyingi safar omad keladi, yana urinib ko‘r!\nBet: {session.bet}",
                reply_markup=back_to_menu_kb(is_admin(uid))
            )

        session.opened.add(idx)
        mines_sessions[uid] = session
        opened = len(session.opened)
        mult = mines_multiplier(opened)
        return await m.answer(
            f"💣 Mines\nBet: {session.bet} ({session.wallet.upper()})\nOchilgan: {opened}\nKoef: x{mult:.2f}",
            reply_markup=mines_grid_kb(session)
        )

    # Mines cashout/stop
    if text == "💵 Cashout":
        session = mines_sessions.get(uid)
        if not session or not session.active:
            return
        opened = len(session.opened)
        if opened == 0:
            return await m.answer("Avval bitta katak oching.")
        session.active = False
        mult = mines_multiplier(opened)
        win = int(round(session.bet * mult))
        if session.wallet == "real":
            await add_real(uid, win)
        else:
            await add_bonus(uid, win)
        mines_sessions[uid] = session
        return await m.answer(
            f"✅ Cashout!\nKoef: x{mult:.2f}\nYutuq: {win} ({session.wallet.upper()})",
            reply_markup=main_menu_kb(is_admin(uid))
        )

    if text == "❌ Stop":
        # stop mines or aviator
        if uid in mines_sessions:
            mines_sessions.pop(uid, None)
            return await m.answer("Mines to‘xtatildi.", reply_markup=main_menu_kb(is_admin(uid)))
        if uid in aviator_sessions:
            aviator_sessions.pop(uid, None)
            return await m.answer("Aviator to‘xtatildi.", reply_markup=main_menu_kb(is_admin(uid)))

    # Aviator cashout
    if text == "💸 Cashout":
        s = aviator_sessions.get(uid)
        if not s or s.crashed or s.cashed_out:
            return
        s.cashed_out = True
        aviator_sessions[uid] = s
        win = int(round(s.bet * s.mult))
        if s.wallet == "real":
            await add_real(uid, win)
        else:
            await add_bonus(uid, win)
        return await m.answer(
            f"✅ Cashout: x{s.mult:.2f}\nYutuq: {win} ({s.wallet.upper()})",
            reply_markup=main_menu_kb(is_admin(uid))
        )

    # Deposit quick start (set step)
    if text in ("20000","50000","100000","200000","500000") and (not st):
        # ignore if not in deposit mode
        return

# ======================
# Deposit start (set initial step)
# ======================
@dp.message(F.text == "➕ Hisob to‘ldirish")
async def dep_entry(m: types.Message):
    uid = m.from_user.id
    await ensure_user(uid)
    steps[uid] = {"mode": "dep_amount"}
    await m.answer(
        f"➕ Hisob to‘ldirish\nSummani tanlang yoki 'Boshqa summa' bosing.\nMin {MIN_DEP} / Max {MAX_DEP}",
        reply_markup=deposit_amount_quick_kb()
    )

# ======================
# Promo start (set step)
# ======================
@dp.message(F.text == "🎁 Promo code")
async def promo_entry(m: types.Message):
    uid = m.from_user.id
    await ensure_user(uid)
    steps[uid] = {"mode": "promo_enter"}
    await m.answer("🎁 Promo kodni yuboring (masalan: BONUS10)")

# ======================
# Mines/Aviator entry handlers already above via text, kept here just in case
# ======================

async def main():
    await db_init()
    bot = Bot(TOKEN)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
