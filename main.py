import asyncio
import time
import random
from typing import Optional

import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command

# ======================
# CONFIG
# ======================
TOKEN = "8161107014:AAGBWEYVxie7-pB4-2FoGCPjCv_sl0yHogc"
ADMIN_IDS = {5815294733}

DB = "casino.db"

# 2 ta kanal (username yoki -100... id bo'lishi mumkin)
REQUIRED_CHATS = ["@bypass_bypasss", "@kino_olami_kinolar"]

# Bonuslar
DAILY_BONUS_AMOUNT = 3000          # kunlik bonus (bonus_balance)
SUBSCRIBE_BONUS_AMOUNT = 5000      # 2 kanalga obuna bo'lganda 1 martalik bonus

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

  last_daily_at INTEGER NOT NULL DEFAULT 0,
  sub_bonus_claimed INTEGER NOT NULL DEFAULT 0
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
"""

async def db_init():
    async with aiosqlite.connect(DB) as db:
        await db.executescript(CREATE_SQL)
        await db.commit()

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

async def ensure_user(uid: int, ref_by: Optional[int] = None):
    now = int(time.time())
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT user_id, ref_by FROM users WHERE user_id=?", (uid,))
        row = await cur.fetchone()
        if row is None:
            # yangi user
            ref = ref_by if (ref_by and ref_by != uid) else None
            await db.execute(
                "INSERT INTO users(user_id, real_balance, bonus_balance, deposit_verified, ref_by, ref_count, last_daily_at, sub_bonus_claimed) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (uid, 0, 0, 0, ref, 0, 0, 0)
            )
            # ref_count +1 (faqat ref user mavjud bo'lsa)
            if ref:
                await db.execute("UPDATE users SET ref_count = ref_count + 1 WHERE user_id=?", (ref,))
        await db.commit()

async def get_user(uid: int):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "SELECT real_balance, bonus_balance, deposit_verified, ref_by, ref_count, last_daily_at, sub_bonus_claimed "
            "FROM users WHERE user_id=?",
            (uid,)
        )
        row = await cur.fetchone()
        return row if row else (0, 0, 0, None, 0, 0, 0)

async def add_bonus(uid: int, amount: int):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE users SET bonus_balance = bonus_balance + ? WHERE user_id=?", (amount, uid))
        await db.commit()

async def set_last_daily(uid: int, ts: int):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE users SET last_daily_at=? WHERE user_id=?", (ts, uid))
        await db.commit()

async def set_sub_claimed(uid: int):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE users SET sub_bonus_claimed=1 WHERE user_id=?", (uid,))
        await db.commit()

# ======================
# REPLY KEYBOARD (pastda)
# ======================
def menu_kb(admin=False):
    rows = [
        [types.KeyboardButton(text="💣 Mines"), types.KeyboardButton(text="✈️ Aviator")],
        [types.KeyboardButton(text="➕ Hisob to‘ldirish"), types.KeyboardButton(text="📤 Pul yechish")],
        [types.KeyboardButton(text="💰 Balans"), types.KeyboardButton(text="🎁 Promo code")],
        [types.KeyboardButton(text="🎁 Kunlik bonus"), types.KeyboardButton(text="🤝 Referal")],
        [types.KeyboardButton(text="✅ Obuna tekshirish"), types.KeyboardButton(text="ℹ️ Yordam")]
    ]
    if admin:
        rows.append([types.KeyboardButton(text="🛠 Admin panel")])
    return types.ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

# ======================
# STEPS (text input)
# ======================
step = {}  # uid -> {"mode": "promo_enter"}

# ======================
# CHANNEL CHECK
# ======================
async def is_subscribed(bot: Bot, user_id: int) -> bool:
    # user REQUIRED_CHATS ning barchasida member bo'lishi kerak
    for chat in REQUIRED_CHATS:
        try:
            member = await bot.get_chat_member(chat_id=chat, user_id=user_id)
            if member.status in ("left", "kicked"):
                return False
        except Exception:
            # bot kanalga admin emas / chat topilmadi / username xato
            return False
    return True

def channels_text() -> str:
    return "\n".join([f"• {c}" for c in REQUIRED_CHATS])

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
    await m.answer("Menyu 👇", reply_markup=menu_kb(is_admin(uid)))

# ======================
# BALANS
# ======================
@dp.message(F.text == "💰 Balans")
async def balans(m: types.Message):
    uid = m.from_user.id
    await ensure_user(uid)
    real, bonus, dep, ref_by, ref_count, last_daily, sub_claimed = await get_user(uid)
    await m.answer(
        f"💰 Balans\n"
        f"✅ Real: {real}\n"
        f"🎁 Bonus: {bonus}\n"
        f"📌 Deposit: {'✅ Tasdiqlangan' if dep else '❌ Yo‘q'}\n"
        f"👥 Referal: {ref_count}"
    )

# ======================
# PROMO CODE
# ======================
@dp.message(F.text == "🎁 Promo code")
async def promo_enter(m: types.Message):
    uid = m.from_user.id
    await ensure_user(uid)
    step[uid] = {"mode": "promo_enter"}
    await m.answer("🎁 Promo codeni yuboring (masalan: BONUS10)")

@dp.message(F.text)
async def step_text(m: types.Message):
    uid = m.from_user.id
    await ensure_user(uid)
    st = step.get(uid)
    if not st:
        return

    if st["mode"] == "promo_enter":
        code = (m.text or "").strip().upper()
        now = int(time.time())

        async with aiosqlite.connect(DB) as db:
            # oldin ishlatilganmi?
            cur = await db.execute("SELECT 1 FROM promo_uses WHERE user_id=? AND code=?", (uid, code))
            if await cur.fetchone():
                step.pop(uid, None)
                return await m.answer("❌ Bu promo sizda ishlatilgan.")

            # promo mavjudmi?
            cur = await db.execute("SELECT amount, max_uses, used_count FROM promo_codes WHERE code=?", (code,))
            row = await cur.fetchone()
            if not row:
                step.pop(uid, None)
                return await m.answer("❌ Promo topilmadi.")

            amount, max_uses, used_count = int(row[0]), int(row[1]), int(row[2])
            if used_count >= max_uses:
                step.pop(uid, None)
                return await m.answer("❌ Promo limiti tugagan.")

            # apply
            await db.execute("INSERT INTO promo_uses(user_id, code, used_at) VALUES(?,?,?)", (uid, code, now))
            await db.execute("UPDATE promo_codes SET used_count = used_count + 1 WHERE code=?", (code,))
            await db.execute("UPDATE users SET bonus_balance = bonus_balance + ? WHERE user_id=?", (amount, uid))
            await db.commit()

        step.pop(uid, None)
        await m.answer(f"✅ Promo qabul qilindi: +{amount} (BONUS balans)")

# Admin promo yaratish
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

    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT OR REPLACE INTO promo_codes(code, amount, max_uses, used_count, created_at) VALUES(?,?,?,?,?)",
            (code, amount, maxuses, 0, int(time.time()))
        )
        await db.commit()
    await m.answer(f"✅ Promo yaratildi: {code} | +{amount} | max={maxuses}")

# ======================
# OBUNA TEKSHIRISH + BONUS (1 marta)
# ======================
@dp.message(F.text == "✅ Obuna tekshirish")
async def sub_check(m: types.Message):
    uid = m.from_user.id
    await ensure_user(uid)

    ok = await is_subscribed(m.bot, uid)
    if not ok:
        return await m.answer(
            "❌ Bonus olish uchun 2 ta kanalga obuna bo‘ling:\n"
            f"{channels_text()}\n\n"
            "Obuna bo‘lgach, yana '✅ Obuna tekshirish' bosing."
        )

    real, bonus, dep, ref_by, ref_count, last_daily, sub_claimed = await get_user(uid)
    if sub_claimed:
        return await m.answer("✅ Obuna tekshirildi. (1 martalik bonus allaqachon berilgan)")

    await add_bonus(uid, SUBSCRIBE_BONUS_AMOUNT)
    await set_sub_claimed(uid)
    await m.answer(f"✅ Obuna tasdiqlandi! +{SUBSCRIBE_BONUS_AMOUNT} (BONUS balans)")

# ======================
# KUNLIK BONUS (24 soat)
# ======================
@dp.message(F.text == "🎁 Kunlik bonus")
async def daily_bonus(m: types.Message):
    uid = m.from_user.id
    await ensure_user(uid)

    ok = await is_subscribed(m.bot, uid)
    if not ok:
        return await m.answer(
            "❌ Kunlik bonus uchun 2 ta kanalga obuna bo‘ling:\n"
            f"{channels_text()}\n\n"
            "Obuna bo‘lgach, '🎁 Kunlik bonus' ni bosing."
        )

    real, bonus, dep, ref_by, ref_count, last_daily, sub_claimed = await get_user(uid)
    now = int(time.time())
    if now - int(last_daily) < 86400:
        left = 86400 - (now - int(last_daily))
        h = left // 3600
        mi = (left % 3600) // 60
        return await m.answer(f"⏳ Kunlik bonus hali tayyor emas.\nQolgan vaqt: {h} soat {mi} daqiqa")

    await add_bonus(uid, DAILY_BONUS_AMOUNT)
    await set_last_daily(uid, now)
    await m.answer(f"✅ Kunlik bonus: +{DAILY_BONUS_AMOUNT} (BONUS balans)")

# ======================
# REFERAL
# ======================
@dp.message(F.text == "🤝 Referal")
async def referral(m: types.Message):
    uid = m.from_user.id
    await ensure_user(uid)
    me = await m.bot.get_me()
    _, _, _, _, ref_count, _, _ = await get_user(uid)
    link = f"https://t.me/{me.username}?start={uid}"
    await m.answer(f"🤝 Referal\nLink: {link}\n👥 Taklif qilganlar: {ref_count}")

# ======================
# HELP
# ======================
@dp.message(F.text == "ℹ️ Yordam")
async def help_(m: types.Message):
    await m.answer(
        "ℹ️ Yordam\n"
        "• Promo code: kod kiritib bonus oling\n"
        "• Kunlik bonus: 2 kanalga obuna bo‘lsa ishlaydi\n"
        "• Bonus balans: hozircha yechilmaydi (deposit bo‘lsa real balans ochiladi)"
    )

# ======================
# PLACEHOLDERS (Mines/Aviator/Deposit/Withdraw keyin qo'shamiz)
# ======================
@dp.message(F.text.in_(["💣 Mines", "✈️ Aviator", "➕ Hisob to‘ldirish", "📤 Pul yechish", "🛠 Admin panel"]))
async def placeholder(m: types.Message):
    if m.text == "🛠 Admin panel" and not is_admin(m.from_user.id):
        return
    await m.answer("Bu bo‘limni keyingi qadamda to‘liq ulab beraman ✅")

# ======================
# RUN
# ======================
async def main():
    await db_init()
    bot = Bot(TOKEN)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
