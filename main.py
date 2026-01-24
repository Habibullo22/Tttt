import asyncio
import random
import re
import time
from dataclasses import dataclass
from typing import Dict, Optional, List, Tuple

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import 
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

# ================== CONFIG ==================
TOKEN = "8161107014:AAGBWEYVxie7-pB4-2FoGCPjCv_sl0yHogc"
ADMIN_IDS = {5815294733}  # <-- o'zingizning user_id

DB = "casino_full.db"

MIN_BET = 1000
MIN_DEPOSIT = 20_000
MIN_WITHDRAW = 50_000

# ================== DB ==================
async def db_init():
    async with aiosqlite.connect(DB) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            phone TEXT,
            balance INTEGER NOT NULL DEFAULT 0,
            referred_by INTEGER,
            ref_count INTEGER NOT NULL DEFAULT 0,
            is_banned INTEGER NOT NULL DEFAULT 0
        )""")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS promo(
            code TEXT PRIMARY KEY,
            bonus INTEGER NOT NULL,
            max_uses INTEGER NOT NULL DEFAULT 1,
            uses INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1
        )""")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS promo_used(
            user_id INTEGER NOT NULL,
            code TEXT NOT NULL,
            UNIQUE(user_id, code)
        )""")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS withdraws(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING'
        )""")
        await db.commit()

async def get_user(uid: int):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE user_id=?", (uid,))
        return await cur.fetchone()

async def add_user_once(uid: int, ref: Optional[int]):
    u = await get_user(uid)
    if u:
        return
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT INTO users(user_id, referred_by) VALUES(?,?)",
            (uid, ref if ref and ref != uid else None)
        )
        # ref faqat yangi user bo'lsa qo'shiladi
        if ref and ref != uid:
            await db.execute("UPDATE users SET ref_count = ref_count + 1 WHERE user_id=?", (ref,))
        await db.commit()

async def set_phone(uid: int, phone: str):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE users SET phone=? WHERE user_id=?", (phone, uid))
        await db.commit()

async def is_verified(uid: int) -> bool:
    u = await get_user(uid)
    return bool(u and u["phone"])

async def is_banned(uid: int) -> bool:
    u = await get_user(uid)
    return bool(u and u["is_banned"] == 1)

async def get_balance(uid: int) -> int:
    u = await get_user(uid)
    return int(u["balance"]) if u else 0

async def add_balance(uid: int, amount: int):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (int(amount), uid))
        await db.commit()

async def take_balance(uid: int, amount: int):
    amount = int(amount)
    async with aiosqlite.connect(DB) as db:
        await db.execute("""
            UPDATE users
            SET balance = CASE WHEN balance - ? < 0 THEN 0 ELSE balance - ? END
            WHERE user_id=?
        """, (amount, amount, uid))
        await db.commit()

async def set_ban(uid: int, banned: bool):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE users SET is_banned=? WHERE user_id=?", (1 if banned else 0, uid))
        await db.commit()

# ================== KEYBOARDS ==================
def kb_contact():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📲 Kontakt ulashish", request_contact=True)]],
        resize_keyboard=True
    )

def kb_main(is_admin: bool):
    rows = [
        [
            InlineKeyboardButton(text="🎮 O‘yinlar", callback_data="menu:games"),
            InlineKeyboardButton(text="💰 Balans", callback_data="menu:balance"),
        ],
        [
            InlineKeyboardButton(text="➕ Hisob (Demo)", callback_data="menu:deposit"),
            InlineKeyboardButton(text="➖ Yechib olish", callback_data="menu:withdraw"),
        ],
        [
            InlineKeyboardButton(text="👥 Referal", callback_data="menu:ref"),
            InlineKeyboardButton(text="🏷 Promo", callback_data="menu:promo"),
        ],
        [
            InlineKeyboardButton(text="🆘 Yordam", callback_data="menu:help"),
            InlineKeyboardButton(text="🔄 Yangilash", callback_data="menu:balance"),
        ],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton(text="🛠 Admin", callback_data="admin:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_games():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💣 Mines", callback_data="game:mines"),
            InlineKeyboardButton(text="✈️ Aviator", callback_data="game:aviator"),
        ],
        [
            InlineKeyboardButton(text="🎯 Crash Mini", callback_data="game:crash"),
            InlineKeyboardButton(text="🪙 Coin Flip", callback_data="game:coinflip"),
        ],
        [
            InlineKeyboardButton(text="❌⭕ TicTacToe", callback_data="game:ttt"),
            InlineKeyboardButton(text="⬅️ Orqaga", callback_data="menu:back"),
        ],
    ])

def kb_admin():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ Balans", callback_data="admin:addbal"),
            InlineKeyboardButton(text="➖ Ayirish", callback_data="admin:takebal"),
        ],
        [
            InlineKeyboardButton(text="🏷 Promo+", callback_data="admin:promocreate"),
            InlineKeyboardButton(text="📥 Withdraw", callback_data="admin:wlist"),
        ],
        [
            InlineKeyboardButton(text="🚫 Ban", callback_data="admin:ban"),
            InlineKeyboardButton(text="✅ Unban", callback_data="admin:unban"),
        ],
        [
            InlineKeyboardButton(text="⬅️ Orqaga", callback_data="menu:back"),
        ]
    ])

# ================== GAME DATA ==================
@dataclass
class MinesSession:
    bet: int
    mines: set
    opened: set
    active: bool
    mine_count: int

@dataclass
class CoinFlipSession:
    bet: int
    choice: str  # "H" or "T"
    streak: int
    active: bool

@dataclass
class CrashSession:
    bet: int
    crash_at: float
    cashout_done: bool
    active: bool
    start_ts: float
    speed: float  # multiplier growth speed
    msg_id: int

@dataclass
class TTTSession:
    bet: int
    board: List[str]  # 9 cells: " ", "X", "O"
    user_mark: str
    bot_mark: str
    turn: str  # "USER" or "BOT"
    active: bool

MINES: Dict[int, MinesSession] = {}
COIN: Dict[int, CoinFlipSession] = {}
CRASH: Dict[int, CrashSession] = {}
AVIATOR: Dict[int, CrashSession] = {}  # aviator ham crash session

TTT: Dict[int, TTTSession] = {}

# ================== UTIL ==================
def parse_ref(text: str) -> Optional[int]:
    # /start ref_123
    if not text:
        return None
    m = re.search(r"ref_(\d+)", text)
    return int(m.group(1)) if m else None

def fmt(n: int) -> str:
    return f"{n:,}"

def ensure_min_bet(amount: int) -> bool:
    return amount >= MIN_BET

async def guard_user(uid: int) -> Optional[str]:
    if await is_banned(uid):
        return "🚫 Siz bloklangansiz."
    if not await is_verified(uid):
        return "📲 Avval kontakt ulashing."
    return None

# ================== FSM ==================
class DepositFSM(StatesGroup):
    amount = State()

class WithdrawFSM(StatesGroup):
    amount = State()

class PromoFSM(StatesGroup):
    code = State()

class BetFSM(StatesGroup):
    game = State()
    amount = State()

class AdminAddBalFSM(StatesGroup):
    uid = State()
    amount = State()

class AdminTakeBalFSM(StatesGroup):
    uid = State()
    amount = State()

class AdminPromoFSM(StatesGroup):
    code = State()
    bonus = State()
    max_uses = State()

class AdminBanFSM(StatesGroup):
    uid = State()

class AdminUnbanFSM(StatesGroup):
    uid = State()

# ================== BOT ==================
bot = Bot(TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ================== START / CONTACT ==================
@dp.message(CommandStart())
async def cmd_start(msg: Message):
    ref = parse_ref(msg.text or "")
    await add_user_once(msg.from_user.id, ref)
    if not await is_verified(msg.from_user.id):
        await msg.answer("👋 Botdan foydalanish uchun kontakt ulashing 👇", reply_markup=kb_contact())
        return
    await msg.answer("✅ Menyu", reply_markup=kb_main(msg.from_user.id in ADMIN_IDS))

@dp.message(F.contact)
async def on_contact(msg: Message):
    await add_user_once(msg.from_user.id, None)
    await set_phone(msg.from_user.id, msg.contact.phone_number)
    await msg.answer("✅ Kontakt saqlandi!", reply_markup=kb_main(msg.from_user.id in ADMIN_IDS))

# ================== MAIN MENU ==================
@dp.callback_query(F.data == "menu:back")
async def menu_back(c: CallbackQuery):
    await c.message.edit_text("✅ Menyu", reply_markup=kb_main(c.from_user.id in ADMIN_IDS))
    await c.answer()

@dp.callback_query(F.data == "menu:games")
async def menu_games(c: CallbackQuery):
    err = await guard_user(c.from_user.id)
    if err:
        await c.answer(err, show_alert=True)
        return
    await c.message.edit_text("🎮 O‘yinlar", reply_markup=kb_games())
    await c.answer()

@dp.callback_query(F.data == "menu:balance")
async def menu_balance(c: CallbackQuery):
    err = await guard_user(c.from_user.id)
    if err:
        await c.answer(err, show_alert=True)
        return
    bal = await get_balance(c.from_user.id)
    await c.message.edit_text(f"💰 Balans: {fmt(bal)} so‘m", reply_markup=kb_main(c.from_user.id in ADMIN_IDS))
    await c.answer()

@dp.callback_query(F.data == "menu:help")
async def menu_help(c: CallbackQuery):
    text = (
        "🆘 Yordam / Qoidalar\n\n"
        f"• Min stavka: {fmt(MIN_BET)}\n"
        f"• Deposit min: {fmt(MIN_DEPOSIT)}\n"
        f"• Withdraw min: {fmt(MIN_WITHDRAW)}\n\n"
        "Bu bot DEMO balans bilan ishlaydi.\n"
        "O‘yinlar random natija beradi. Cashout tugmalari mavjud."
    )
    await c.message.edit_text(text, reply_markup=kb_main(c.from_user.id in ADMIN_IDS))
    await c.answer()

# ================== REFERAL ==================
@dp.callback_query(F.data == "menu:ref")
async def menu_ref(c: CallbackQuery):
    err = await guard_user(c.from_user.id)
    if err:
        await c.answer(err, show_alert=True)
        return
    u = await get_user(c.from_user.id)
    ref_count = int(u["ref_count"])
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{c.from_user.id}"
    await c.message.edit_text(
        f"👥 Referal\n\n"
        f"🔗 Link: {link}\n"
        f"👤 Referallar: {ref_count}",
        reply_markup=kb_main(c.from_user.id in ADMIN_IDS)
    )
    await c.answer()

# ================== DEPOSIT ==================
@dp.callback_query(F.data == "menu:deposit")
async def menu_deposit(c: CallbackQuery, state: FSMContext):
    err = await guard_user(c.from_user.id)
    if err:
        await c.answer(err, show_alert=True)
        return
    await state.clear()
    await state.set_state(DepositFSM.amount)
    await c.message.edit_text(f"➕ Hisob to‘ldirish (DEMO)\nMinimal: {fmt(MIN_DEPOSIT)}\nSummani yuboring:")
    await c.answer()

@dp.message(DepositFSM.amount)
async def deposit_amount(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    err = await guard_user(uid)
    if err:
        await msg.answer(err, reply_markup=kb_main(uid in ADMIN_IDS))
        await state.clear()
        return
    raw = (msg.text or "").replace(" ", "").replace(",", "")
    if not raw.isdigit():
        await msg.answer("❌ Faqat son yuboring. Masalan: 20000")
        return
    amount = int(raw)
    if amount < MIN_DEPOSIT:
        await msg.answer(f"❌ Minimal: {fmt(MIN_DEPOSIT)}")
        return
    await add_balance(uid, amount)
    await state.clear()
    bal = await get_balance(uid)
    await msg.answer(f"✅ +{fmt(amount)} qo‘shildi.\n💰 Balans: {fmt(bal)}", reply_markup=kb_main(uid in ADMIN_IDS))

# ================== WITHDRAW ==================
@dp.callback_query(F.data == "menu:withdraw")
async def menu_withdraw(c: CallbackQuery, state: FSMContext):
    err = await guard_user(c.from_user.id)
    if err:
        await c.answer(err, show_alert=True)
        return
    await state.clear()
    await state.set_state(WithdrawFSM.amount)
    await c.message.edit_text(f"➖ Pul yechish (DEMO)\nMinimal: {fmt(MIN_WITHDRAW)}\nSummani yuboring:")
    await c.answer()

@dp.message(WithdrawFSM.amount)
async def withdraw_amount(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    err = await guard_user(uid)
    if err:
        await msg.answer(err, reply_markup=kb_main(uid in ADMIN_IDS))
        await state.clear()
        return
    raw = (msg.text or "").replace(" ", "").replace(",", "")
    if not raw.isdigit():
        await msg.answer("❌ Faqat son yuboring.")
        return
    amount = int(raw)
    if amount < MIN_WITHDRAW:
        await msg.answer(f"❌ Minimal: {fmt(MIN_WITHDRAW)}")
        return
    bal = await get_balance(uid)
    if bal < amount:
        await msg.answer("❌ Balans yetarli emas.")
        return

    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO withdraws(user_id, amount, status) VALUES(?,?, 'PENDING')", (uid, amount))
        await db.commit()

    await state.clear()
    await msg.answer("✅ So‘rov yuborildi. Admin ko‘rib chiqadi.", reply_markup=kb_main(uid in ADMIN_IDS))

# ================== PROMO ==================
@dp.callback_query(F.data == "menu:promo")
async def menu_promo(c: CallbackQuery, state: FSMContext):
    err = await guard_user(c.from_user.id)
    if err:
        await c.answer(err, show_alert=True)
        return
    await state.clear()
    await state.set_state(PromoFSM.code)
    await c.message.edit_text("🏷 Promo code kiriting (1 martalik):")
    await c.answer()

@dp.message(PromoFSM.code)
async def promo_use(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    err = await guard_user(uid)
    if err:
        await msg.answer(err, reply_markup=kb_main(uid in ADMIN_IDS))
        await state.clear()
        return

    code = (msg.text or "").strip().upper()
    if not code or len(code) < 3:
        await msg.answer("❌ Promo xato.")
        return

    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row

        cur = await db.execute("SELECT * FROM promo WHERE code=? AND active=1", (code,))
        p = await cur.fetchone()
        if not p:
            await msg.answer("❌ Promo topilmadi yoki aktiv emas.")
            return

        if int(p["uses"]) >= int(p["max_uses"]):
            await msg.answer("❌ Bu promo limitga yetgan.")
            return

        # user 1 marta ishlatadi
        try:
            await db.execute("INSERT INTO promo_used(user_id, code) VALUES(?,?)", (uid, code))
        except:
            await msg.answer("❌ Siz bu promoni avval ishlatgansiz.")
            return

        bonus = int(p["bonus"])
        await db.execute("UPDATE promo SET uses = uses + 1 WHERE code=?", (code,))
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (bonus, uid))
        await db.commit()

    await state.clear()
    bal = await get_balance(uid)
    await msg.answer(f"✅ Promo qabul qilindi: +{fmt(bonus)}\n💰 Balans: {fmt(bal)}", reply_markup=kb_main(uid in ADMIN_IDS))

# ================== BET FLOW (COMMON) ==================
@dp.callback_query(F.data.startswith("game:"))
async def game_select(c: CallbackQuery, state: FSMContext):
    err = await guard_user(c.from_user.id)
    if err:
        await c.answer(err, show_alert=True)
        return
    game = c.data.split(":", 1)[1]  # mines/aviator/crash/coinflip/ttt
    await state.clear()
    await state.set_state(BetFSM.amount)
    await state.update_data(game=game)
    await c.message.edit_text(f"💰 Stavka kiriting (min {fmt(MIN_BET)}):")
    await c.answer()

@dp.message(BetFSM.amount)
async def bet_amount(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    err = await guard_user(uid)
    if err:
        await msg.answer(err, reply_markup=kb_main(uid in ADMIN_IDS))
        await state.clear()
        return

    data = await state.get_data()
    game = data.get("game")
    raw = (msg.text or "").replace(" ", "").replace(",", "")
    if not raw.isdigit():
        await msg.answer("❌ Faqat son.")
        return
    bet = int(raw)
    if bet < MIN_BET:
        await msg.answer(f"❌ Minimal stavka: {fmt(MIN_BET)}")
        return
    bal = await get_balance(uid)
    if bal < bet:
        await msg.answer("❌ Balans yetarli emas.")
        return

    # betni yechib olamiz (stake lock)
    await take_balance(uid, bet)
    await state.clear()

    if game == "mines":
        await start_mines(msg, uid, bet)
    elif game == "coinflip":
        await start_coinflip(msg, uid, bet)
    elif game == "ttt":
        await start_ttt(msg, uid, bet)
    elif game == "crash":
        await start_crash(msg, uid, bet, mini=True)
    elif game == "aviator":
        await start_crash(msg, uid, bet, mini=False)
    else:
        await add_balance(uid, bet)
        await msg.answer("❌ O‘yin topilmadi.", reply_markup=kb_main(uid in ADMIN_IDS))

# ================== MINES ==================
def mines_multiplier(safe_picks: int, mine_count: int) -> float:
    # oddiy, tushunarli jadval (keyin admin sozlamaga chiqaramiz)
    base = 1.0
    # xavf mine_count bo‘yicha biroz oshadi
    risk = 1.0 + (mine_count - 3) * 0.07
    for i in range(safe_picks):
        base *= (1.12 * risk)  # har pick ~ +12% (riskga bog‘liq)
    return round(base, 2)

def kb_mines(uid: int) -> InlineKeyboardMarkup:
    s = MINES.get(uid)
    buttons = []
    for r in range(5):
        row = []
        for c in range(5):
            idx = r * 5 + c
            if idx in s.opened:
                row.append(InlineKeyboardButton(text="💎", callback_data=f"m:noop"))
            else:
                row.append(InlineKeyboardButton(text="❓", callback_data=f"m:{idx}"))
        buttons.append(row)
    buttons.append([
        InlineKeyboardButton(text="💸 Pul olish", callback_data="m:cashout"),
        InlineKeyboardButton(text="⬅️ Menyu", callback_data="m:exit"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def start_mines(msg: Message, uid: int, bet: int):
    mine_count = 3  # keyin tanlash qo‘shamiz
    # tasodifiy mina joylashuvi
    mines = set(random.sample(range(25), mine_count))
    MINES[uid] = MinesSession(bet=bet, mines=mines, opened=set(), active=True, mine_count=mine_count)
    await msg.answer(
        f"💣 Mines 5x5\nStavka: {fmt(bet)}\nMina: {mine_count} ta\n\nKatak tanlang 👇",
        reply_markup=kb_mines(uid)
    )

@dp.callback_query(F.data.startswith("m:"))
async def mines_click(c: CallbackQuery):
    uid = c.from_user.id
    s = MINES.get(uid)
    if not s or not s.active:
        await c.answer("❌ Mines sessiya yo‘q.", show_alert=True)
        return

    action = c.data.split(":", 1)[1]
    if action == "noop":
        await c.answer()
        return

    if action == "exit":
        # sessiyani yopamiz, pul qaytmaydi (stake allaqachon yechilgan)
        s.active = False
        MINES.pop(uid, None)
        await c.message.edit_text("✅ Menyu", reply_markup=kb_main(uid in ADMIN_IDS))
        await c.answer()
        return

    if action == "cashout":
        safe = len(s.opened)
        if safe == 0:
            await c.answer("❌ Avval kamida 1 ta oching.", show_alert=True)
            return
        mult = mines_multiplier(safe, s.mine_count)
        win = int(s.bet * mult)
        await add_balance(uid, win)
        s.active = False
        MINES.pop(uid, None)
        bal = await get_balance(uid)
        await c.message.edit_text(f"✅ Cashout!\nKoef: x{mult}\nYutuq: {fmt(win)}\n💰 Balans: {fmt(bal)}",
                                 reply_markup=kb_main(uid in ADMIN_IDS))
        await c.answer()
        return

    # cell click
    if not action.isdigit():
        await c.answer()
        return
    idx = int(action)
    if idx in s.opened:
        await c.answer()
        return

    # mina bo‘lsa yutqazdi
    if idx in s.mines:
        s.active = False
        MINES.pop(uid, None)
        await c.message.edit_text(
            f"💥 Bomba!\nStavka: {fmt(s.bet)} yo‘qotildi.",
            reply_markup=kb_main(uid in ADMIN_IDS)
        )
        await c.answer("💥 Bomba!", show_alert=True)
        return

    s.opened.add(idx)
    safe = len(s.opened)
    mult = mines_multiplier(safe, s.mine_count)
    await c.message.edit_text(
        f"💣 Mines 5x5\nStavka: {fmt(s.bet)}\nOchilgan: {safe}\nHozirgi koef: x{mult}\n\nDavom etasizmi yoki cashout?",
        reply_markup=kb_mines(uid)
    )
    await c.answer()

# ================== COIN FLIP (STREAK) ==================
STREAK_TABLE = {1: 1.9, 2: 3.6, 3: 6.5, 4: 12.0, 5: 22.0}

def kb_coin(uid: int) -> InlineKeyboardMarkup:
    s = COIN.get(uid)
    rows = [
        [
            InlineKeyboardButton(text="🟦 HEADS", callback_data="c:H"),
            InlineKeyboardButton(text="🟥 TAILS", callback_data="c:T"),
        ]
    ]
    if s and s.streak > 0:
        rows.append([InlineKeyboardButton(text="💸 Pul olish", callback_data="c:cashout")])
    rows.append([InlineKeyboardButton(text="⬅️ Menyu", callback_data="c:exit")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def start_coinflip(msg: Message, uid: int, bet: int):
    COIN[uid] = CoinFlipSession(bet=bet, choice="H", streak=0, active=True)
    await msg.answer(
        f"🪙 Coin Flip (Streak)\nStavka: {fmt(bet)}\n\nTanlang va o‘ynang 👇",
        reply_markup=kb_coin(uid)
    )

@dp.callback_query(F.data.startswith("c:"))
async def coin_click(c: CallbackQuery):
    uid = c.from_user.id
    s = COIN.get(uid)
    if not s or not s.active:
        await c.answer("❌ Sessiya yo‘q.", show_alert=True)
        return

    action = c.data.split(":", 1)[1]
    if action == "exit":
        # stake qaytmaydi (o‘yin tizimi), xohlasangiz exitda qaytarishni qo‘shamiz
        COIN.pop(uid, None)
        await c.message.edit_text("✅ Menyu", reply_markup=kb_main(uid in ADMIN_IDS))
        await c.answer()
        return

    if action == "cashout":
        if s.streak <= 0:
            await c.answer("❌ Hali yutuq yo‘q.", show_alert=True)
            return
        mult = STREAK_TABLE.get(s.streak, STREAK_TABLE[max(STREAK_TABLE)])
        win = int(s.bet * mult)
        await add_balance(uid, win)
        COIN.pop(uid, None)
        bal = await get_balance(uid)
        await c.message.edit_text(f"✅ Cashout!\nStreak: {s.streak}\nKoef: x{mult}\nYutuq: {fmt(win)}\n💰 Balans: {fmt(bal)}",
                                 reply_markup=kb_main(uid in ADMIN_IDS))
        await c.answer()
        return

    if action not in ("H", "T"):
        await c.answer()
        return

    s.choice = action
    flip = random.choice(["H", "T"])
    if flip == s.choice:
        s.streak += 1
        if s.streak > 5:
            s.streak = 5
        mult = STREAK_TABLE.get(s.streak, 1.9)
        await c.message.edit_text(
            f"✅ To‘g‘ri! Tushdi: {flip}\nStreak: {s.streak}\nHozirgi koef: x{mult}\n\nDavom etasizmi yoki cashout?",
            reply_markup=kb_coin(uid)
        )
        await c.answer("✅")
    else:
        COIN.pop(uid, None)
        await c.message.edit_text(
            f"❌ Xato! Tushdi: {flip}\nStavka {fmt(s.bet)} yo‘qotildi.",
            reply_markup=kb_main(uid in ADMIN_IDS)
        )
        await c.answer("❌", show_alert=True)

# ================== CRASH / AVIATOR (per-user) ==================
def crash_distribution(mini: bool) -> float:
    # Kichik koef ko‘p, yuqori juda kam (realistik)
    r = random.random()
    if mini:
        if r < 0.70:  # 70% 1.1 - 3.0
            return round(random.uniform(1.1, 3.0), 2)
        if r < 0.95:  # 25% 3 - 10
            return round(random.uniform(3.0, 10.0), 2)
        return round(random.uniform(10.0, 50.0), 2)
    else:
        if r < 0.65:
            return round(random.uniform(1.1, 4.0), 2)
        if r < 0.92:
            return round(random.uniform(4.0, 15.0), 2)
        if r < 0.995:
            return round(random.uniform(15.0, 120.0), 2)
        return round(random.uniform(120.0, 1000.0), 2)

def kb_crash(kind: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💸 Pul olish", callback_data=f"{kind}:cashout")],
        [InlineKeyboardButton(text="⬅️ Menyu", callback_data=f"{kind}:exit")]
    ])

def current_multiplier(start_ts: float, speed: float) -> float:
    t = max(0.0, time.time() - start_ts)
    # sekin ko'tarilish (smooth)
    mult = 1.0 + (t * speed)
    return round(mult, 2)

async def start_crash(msg: Message, uid: int, bet: int, mini: bool):
    crash_at = crash_distribution(mini)
    speed = 0.18 if mini else 0.12  # mini tezroq
    kind = "cr" if mini else "av"
    text = f"{'🎯 Crash Mini' if mini else '✈️ Aviator'}\nStavka: {fmt(bet)}\n\nx1.00"
    sent = await msg.answer(text, reply_markup=kb_crash(kind))
    sess = CrashSession(
        bet=bet, crash_at=crash_at, cashout_done=False, active=True,
        start_ts=time.time(), speed=speed, msg_id=sent.message_id
    )
    if mini:
        CRASH[uid] = sess
    else:
        AVIATOR[uid] = sess

    asyncio.create_task(run_crash_loop(uid, mini, sent.chat.id))

async def run_crash_loop(uid: int, mini: bool, chat_id: int):
    kind = "cr" if mini else "av"
    store = CRASH if mini else AVIATOR
    s = store.get(uid)
    if not s:
        return

    while True:
        s = store.get(uid)
        if not s or not s.active:
            return
        mult = current_multiplier(s.start_ts, s.speed)

        # crash bo'ldi
        if mult >= s.crash_at:
            if s.cashout_done:
                # allaqachon cashout bo'lgan, faqat sessiyani yopamiz
                store.pop(uid, None)
                return

            # yutqazdi
            s.active = False
            store.pop(uid, None)
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=s.msg_id,
                    text=f"{'🎯 Crash Mini' if mini else '✈️ Aviator'}\n💥 CRASH!\nCrash: x{s.crash_at}\n\nStavka {fmt(s.bet)} yo‘qotildi.",
                    reply_markup=kb_main(uid in ADMIN_IDS)
                )
            except:
                pass
            return

        # yangilash (faqat cashout qilmagan bo'lsa ham ko'rsatamiz)
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=s.msg_id,
                text=f"{'🎯 Crash Mini' if mini else '✈️ Aviator'}\nStavka: {fmt(s.bet)}\n\nx{mult}",
                reply_markup=kb_crash(kind) if not s.cashout_done else kb_main(uid in ADMIN_IDS)
            )
        except:
            pass

        await asyncio.sleep(0.7 if mini else 0.9)

@dp.callback_query(F.data.in_(["cr:cashout", "av:cashout"]))
async def crash_cashout(c: CallbackQuery):
    uid = c.from_user.id
    mini = c.data.startswith("cr:")
    store = CRASH if mini else AVIATOR
    s = store.get(uid)
    if not s or not s.active:
        await c.answer("❌ Sessiya yo‘q.", show_alert=True)
        return
    if s.cashout_done:
        await c.answer("✅ Olingan.", show_alert=True)
        return

    mult = current_multiplier(s.start_ts, s.speed)
    # crashga juda yaqin bo‘lsa ham, agar bosgan bo‘lsa shu mult bo‘yicha oladi
    win = int(s.bet * mult)
    await add_balance(uid, win)
    s.cashout_done = True

    bal = await get_balance(uid)
    await c.message.edit_text(
        f"✅ Pul olindi!\nKoef: x{mult}\nYutuq: {fmt(win)}\n💰 Balans: {fmt(bal)}\n\n(Round davom etadi, siz chiqdingiz)",
        reply_markup=kb_main(uid in ADMIN_IDS)
    )
    await c.answer()

@dp.callback_query(F.data.in_(["cr:exit", "av:exit"]))
async def crash_exit(c: CallbackQuery):
    uid = c.from_user.id
    mini = c.data.startswith("cr:")
    store = CRASH if mini else AVIATOR
    if uid in store:
        store.pop(uid, None)
    await c.message.edit_text("✅ Menyu", reply_markup=kb_main(uid in ADMIN_IDS))
    await c.answer()

# ================== TIC TAC TOE (UNBEATABLE) ==================
WIN_LINES = [
    (0,1,2),(3,4,5),(6,7,8),
    (0,3,6),(1,4,7),(2,5,8),
    (0,4,8),(2,4,6)
]

def ttt_winner(board: List[str]) -> Optional[str]:
    for a,b,c in WIN_LINES:
        if board[a] != " " and board[a] == board[b] == board[c]:
            return board[a]
    if " " not in board:
        return "D"  # draw
    return None

def minimax(board: List[str], bot_mark: str, user_mark: str, is_bot_turn: bool) -> Tuple[int, Optional[int]]:
    w = ttt_winner(board)
    if w == bot_mark:
        return (10, None)
    if w == user_mark:
        return (-10, None)
    if w == "D":
        return (0, None)

    best_move = None
    if is_bot_turn:
        best_score = -999
        for i in range(9):
            if board[i] == " ":
                board[i] = bot_mark
                score, _ = minimax(board, bot_mark, user_mark, False)
                board[i] = " "
                if score > best_score:
                    best_score = score
                    best_move = i
        return (best_score, best_move)
    else:
        best_score = 999
        for i in range(9):
            if board[i] == " ":
                board[i] = user_mark
                score, _ = minimax(board, bot_mark, user_mark, True)
                board[i] = " "
                if score < best_score:
                    best_score = score
                    best_move = i
        return (best_score, best_move)

def kb_ttt(uid: int) -> InlineKeyboardMarkup:
    s = TTT.get(uid)
    buttons = []
    for r in range(3):
        row = []
        for c in range(3):
            idx = r*3 + c
            cell = s.board[idx]
            txt = "⬜️" if cell == " " else ("❌" if cell == "X" else "⭕️")
            row.append(InlineKeyboardButton(text=txt, callback_data=f"t:{idx}"))
        buttons.append(row)
    buttons.append([
        InlineKeyboardButton(text="⬅️ Menyu", callback_data="t:exit"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def start_ttt(msg: Message, uid: int, bet: int):
    # user = X, bot = O
    TTT[uid] = TTTSession(
        bet=bet,
        board=[" "] * 9,
        user_mark="X",
        bot_mark="O",
        turn="USER",
        active=True
    )
    await msg.answer(f"❌⭕ TicTacToe (AI juda kuchli)\nStavka: {fmt(bet)}\n\nSiz ❌, Bot ⭕️", reply_markup=kb_ttt(uid))

@dp.callback_query(F.data.startswith("t:"))
async def ttt_click(c: CallbackQuery):
    uid = c.from_user.id
    s = TTT.get(uid)
    if not s or not s.active:
        await c.answer("❌ Sessiya yo‘q.", show_alert=True)
        return
    action = c.data.split(":",1)[1]
    if action == "exit":
        TTT.pop(uid, None)
        await c.message.edit_text("✅ Menyu", reply_markup=kb_main(uid in ADMIN_IDS))
        await c.answer()
        return
    if not action.isdigit():
        await c.answer()
        return
    idx = int(action)
    if s.board[idx] != " ":
        await c.answer("❌ Band.", show_alert=True)
        return

    # user move
    s.board[idx] = s.user_mark
    w = ttt_winner(s.board)
    if w:
        await finish_ttt(c, uid, w)
        return

    # bot move (minimax)
    _, move = minimax(s.board[:], s.bot_mark, s.user_mark, True)
    if move is not None and s.board[move] == " ":
        s.board[move] = s.bot_mark

    w = ttt_winner(s.board)
    if w:
        await finish_ttt(c, uid, w)
        return

    await c.message.edit_text(f"❌⭕ TicTacToe\nStavka: {fmt(s.bet)}", reply_markup=kb_ttt(uid))
    await c.answer()

async def finish_ttt(c: CallbackQuery, uid: int, result: str):
    s = TTT.get(uid)
    if not s:
        return
    s.active = False
    TTT.pop(uid, None)

    # payout: win x1.5, draw 0.5 qaytadi, lose 0
    if result == s.user_mark:
        win = int(s.bet * 1.5)
        await add_balance(uid, win)
        text = f"✅ Siz yutdingiz! (+{fmt(win)})"
    elif result == "D":
        back = int(s.bet * 0.5)
        await add_balance(uid, back)
        text = f"🤝 Durang! (qaytdi: {fmt(back)})"
    else:
        text = f"❌ Bot yutdi. Stavka {fmt(s.bet)} yo‘qotildi."

    bal = await get_balance(uid)
    await c.message.edit_text(f"{text}\n💰 Balans: {fmt(bal)}", reply_markup=kb_main(uid in ADMIN_IDS))
    await c.answer()

# ================== ADMIN ==================
@dp.callback_query(F.data == "admin:home")
async def admin_home(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        await c.answer("❌ Admin emassiz.", show_alert=True)
        return
    await c.message.edit_text("🛠 Admin panel", reply_markup=kb_admin())
    await c.answer()

@dp.callback_query(F.data == "admin:wlist")
async def admin_wlist(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        await c.answer("❌", show_alert=True)
        return
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM withdraws WHERE status='PENDING' ORDER BY id DESC LIMIT 10")
        rows = await cur.fetchall()

    if not rows:
        await c.message.edit_text("📥 Withdraw navbat bo‘sh.", reply_markup=kb_admin())
        await c.answer()
        return

    text = "📥 Withdraw navbat (oxirgi 10 ta):\n\n"
    kb_rows = []
    for r in rows:
        text += f"#{r['id']} | user:{r['user_id']} | {fmt(r['amount'])}\n"
        kb_rows.append([
            InlineKeyboardButton(text=f"✅ #{r['id']}", callback_data=f"admin:wok:{r['id']}"),
            InlineKeyboardButton(text=f"❌ #{r['id']}", callback_data=f"admin:wno:{r['id']}"),
        ])
    kb_rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="admin:home")])
    await c.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    await c.answer()

@dp.callback_query(F.data.startswith("admin:wok:"))
async def admin_w_ok(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        await c.answer("❌", show_alert=True)
        return
    wid = int(c.data.split(":")[-1])
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM withdraws WHERE id=?", (wid,))
        w = await cur.fetchone()
        if not w or w["status"] != "PENDING":
            await c.answer("❌ Topilmadi.", show_alert=True)
            return
        uid = int(w["user_id"])
        amount = int(w["amount"])
        bal = await get_balance(uid)
        if bal < amount:
            await db.execute("UPDATE withdraws SET status='REJECT' WHERE id=?", (wid,))
            await db.commit()
            await c.answer("Balans yetarli emas, rad etildi.", show_alert=True)
        else:
            await take_balance(uid, amount)
            await db.execute("UPDATE withdraws SET status='PAID' WHERE id=?", (wid,))
            await db.commit()
            try:
                await bot.send_message(uid, f"✅ Withdraw tasdiqlandi: {fmt(amount)} (DEMO)")
            except:
                pass
            await c.answer("✅ Tasdiqlandi.", show_alert=True)
    await admin_wlist(c)

@dp.callback_query(F.data.startswith("admin:wno:"))
async def admin_w_no(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        await c.answer("❌", show_alert=True)
        return
    wid = int(c.data.split(":")[-1])
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE withdraws SET status='REJECT' WHERE id=?", (wid,))
        await db.commit()
    await c.answer("❌ Rad etildi.", show_alert=True)
    await admin_wlist(c)

@dp.callback_query(F.data == "admin:addbal")
async def admin_addbal_start(c: CallbackQuery, state: FSMContext):
    if c.from_user.id not in ADMIN_IDS:
        await c.answer("❌", show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminAddBalFSM.uid)
    await c.message.edit_text("➕ Balans qo‘shish\nUser ID yuboring:")
    await c.answer()

@dp.message(AdminAddBalFSM.uid)
async def admin_addbal_uid(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMIN_IDS:
        await state.clear()
        return
    if not (msg.text or "").isdigit():
        await msg.answer("❌ Faqat user_id son.")
        return
    await state.update_data(uid=int(msg.text))
    await state.set_state(AdminAddBalFSM.amount)
    await msg.answer("Summani yuboring:")

@dp.message(AdminAddBalFSM.amount)
async def admin_addbal_amount(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMIN_IDS:
        await state.clear()
        return
    raw = (msg.text or "").replace(" ", "").replace(",", "")
    if not raw.isdigit():
        await msg.answer("❌ Faqat son.")
        return
    data = await state.get_data()
    uid = int(data["uid"])
    amount = int(raw)
    await add_balance(uid, amount)
    await state.clear()
    await msg.answer("✅ Qo‘shildi.", reply_markup=kb_admin())

@dp.callback_query(F.data == "admin:takebal")
async def admin_takebal_start(c: CallbackQuery, state: FSMContext):
    if c.from_user.id not in ADMIN_IDS:
        await c.answer("❌", show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminTakeBalFSM.uid)
    await c.message.edit_text("➖ Balans ayirish\nUser ID yuboring:")
    await c.answer()

@dp.message(AdminTakeBalFSM.uid)
async def admin_takebal_uid(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMIN_IDS:
        await state.clear()
        return
    if not (msg.text or "").isdigit():
        await msg.answer("❌ Faqat user_id son.")
        return
    await state.update_data(uid=int(msg.text))
    await state.set_state(AdminTakeBalFSM.amount)
    await msg.answer("Ayiriladigan summa:")

@dp.message(AdminTakeBalFSM.amount)
async def admin_takebal_amount(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMIN_IDS:
        await state.clear()
        return
    raw = (msg.text or "").replace(" ", "").replace(",", "")
    if not raw.isdigit():
        await msg.answer("❌ Faqat son.")
        return
    data = await state.get_data()
    uid = int(data["uid"])
    amount = int(raw)
    await take_balance(uid, amount)
    await state.clear()
    await msg.answer("✅ Ayirildi.", reply_markup=kb_admin())

@dp.callback_query(F.data == "admin:promocreate")
async def admin_promo_start(c: CallbackQuery, state: FSMContext):
    if c.from_user.id not in ADMIN_IDS:
        await c.answer("❌", show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminPromoFSM.code)
    await c.message.edit_text("🏷 Promo yaratish\nCode yuboring (masalan: WELCOME):")
    await c.answer()

@dp.message(AdminPromoFSM.code)
async def admin_promo_code(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMIN_IDS:
        await state.clear()
        return
    code = (msg.text or "").strip().upper()
    if not re.match(r"^[A-Z0-9_]{3,20}$", code):
        await msg.answer("❌ Code faqat A-Z 0-9 _ va 3-20 uzunlik.")
        return
    await state.update_data(code=code)
    await state.set_state(AdminPromoFSM.bonus)
    await msg.answer("Bonus summa (masalan 10000):")

@dp.message(AdminPromoFSM.bonus)
async def admin_promo_bonus(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMIN_IDS:
        await state.clear()
        return
    raw = (msg.text or "").replace(" ", "").replace(",", "")
    if not raw.isdigit():
        await msg.answer("❌ Faqat son.")
        return
    await state.update_data(bonus=int(raw))
    await state.set_state(AdminPromoFSM.max_uses)
    await msg.answer("Max uses (masalan 100):")

@dp.message(AdminPromoFSM.max_uses)
async def admin_promo_max(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMIN_IDS:
        await state.clear()
        return
    raw = (msg.text or "").replace(" ", "").replace(",", "")
    if not raw.isdigit():
        await msg.answer("❌ Faqat son.")
        return
    data = await state.get_data()
    code = data["code"]
    bonus = int(data["bonus"])
    max_uses = int(raw)

    async with aiosqlite.connect(DB) as db:
        await db.execute("""
            INSERT OR REPLACE INTO promo(code, bonus, max_uses, uses, active)
            VALUES(?, ?, ?, COALESCE((SELECT uses FROM promo WHERE code=?), 0), 1)
        """, (code, bonus, max_uses, code))
        await db.commit()

    await state.clear()
    await msg.answer(f"✅ Promo yaratildi: {code} | +{fmt(bonus)} | limit {max_uses}", reply_markup=kb_admin())

@dp.callback_query(F.data == "admin:ban")
async def admin_ban_start(c: CallbackQuery, state: FSMContext):
    if c.from_user.id not in ADMIN_IDS:
        await c.answer("❌", show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminBanFSM.uid)
    await c.message.edit_text("🚫 Ban\nUser ID yuboring:")
    await c.answer()

@dp.message(AdminBanFSM.uid)
async def admin_ban_do(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMIN_IDS:
        await state.clear()
        return
    if not (msg.text or "").isdigit():
        await msg.answer("❌ Faqat user_id son.")
        return
    uid = int(msg.text)
    await set_ban(uid, True)
    await state.clear()
    await msg.answer("✅ Ban qilindi.", reply_markup=kb_admin())

@dp.callback_query(F.data == "admin:unban")
async def admin_unban_start(c: CallbackQuery, state: FSMContext):
    if c.from_user.id not in ADMIN_IDS:
        await c.answer("❌", show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminUnbanFSM.uid)
    await c.message.edit_text("✅ Unban\nUser ID yuboring:")
    await c.answer()

@dp.message(AdminUnbanFSM.uid)
async def admin_unban_do(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMIN_IDS:
        await state.clear()
        return
    if not (msg.text or "").isdigit():
        await msg.answer("❌ Faqat user_id son.")
        return
    uid = int(msg.text)
    await set_ban(uid, False)
    await state.clear()
    await msg.answer("✅ Unban qilindi.", reply_markup=kb_admin())

# ================== RUN ==================
async def main():
    await db_init()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
