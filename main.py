import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder

# =======================
# SOZLAMALAR
# =======================
TOKEN = "8478058553:AAGR0eMotTJy5_zM-65bHGGsm2ImcOKKfeE"
ADMINS = {5815294733}

# Majburiy emas, faqat "Kinolar bo‘lim" uchun:
MOVIES_CHANNEL = "@kino_olami_kinolar"

DB_PATH = "kino.db"

dp = Dispatcher()

def is_admin(uid: int) -> bool:
    return uid in ADMINS


# =======================
# DB
# =======================
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS users (
  user_id INTEGER PRIMARY KEY,
  joined_at INTEGER
);

CREATE TABLE IF NOT EXISTS movies (
  code TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  file_id TEXT NOT NULL,
  added_by INTEGER,
  added_at INTEGER
);
"""

async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(CREATE_SQL)
        await db.commit()

async def db_add_user(uid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users(user_id, joined_at) VALUES(?,?)",
            (uid, int(time.time()))
        )
        await db.commit()

async def db_stats():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        (u_cnt,) = await cur.fetchone()
        cur = await db.execute("SELECT COUNT(*) FROM movies")
        (m_cnt,) = await cur.fetchone()
        return int(u_cnt), int(m_cnt)

async def db_add_movie(code: str, title: str, file_id: str, added_by: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO movies(code, title, file_id, added_by, added_at) VALUES(?,?,?,?,?)",
            (code, title, file_id, added_by, int(time.time()))
        )
        await db.commit()

async def db_get_movie(code: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT title, file_id FROM movies WHERE code=?", (code,))
        return await cur.fetchone()  # (title, file_id) or None

async def db_delete_movie(code: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM movies WHERE code=?", (code,))
        await db.commit()
        return cur.rowcount > 0

async def db_all_users():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM users")
        return await cur.fetchall()


# =======================
# KEYBOARDS
# =======================
def kb_channel_link():
    kb = InlineKeyboardBuilder()
    kb.button(text="🔗 Kinolar bo‘lim", url=f"https://t.me/{MOVIES_CHANNEL.replace('@','')}")
    return kb.as_markup()

def kb_user():
    kb = ReplyKeyboardBuilder()
    kb.button(text="🎬 Kino olish")
    kb.button(text="📢 Kinolar bo‘lim")
    kb.button(text="ℹ️ Yordam")
    kb.adjust(2, 1)
    return kb.as_markup(resize_keyboard=True)

def kb_admin():
    kb = ReplyKeyboardBuilder()
    kb.button(text="🎬 Kino olish")
    kb.button(text="📢 Kinolar bo‘lim")
    kb.button(text="➕ Kino qo‘shish")
    kb.button(text="❌ Kino o‘chirish")
    kb.button(text="📢 Broadcast")
    kb.button(text="📊 Statistika")
    kb.button(text="ℹ️ Yordam")
    kb.adjust(2, 2, 2, 1)
    return kb.as_markup(resize_keyboard=True)


# =======================
# ADMIN FLOWS
# =======================
@dataclass
class AddFlow:
    step: int = 0
    code: str = ""
    title: str = ""

ADD_FLOW: dict[int, AddFlow] = {}
DEL_FLOW: set[int] = set()

# Broadcast: admin /bc yuborgandan keyin keyingi xabar reklama bo'ladi
BC_WAIT: set[int] = set()


# =======================
# HANDLERS
# =======================
@dp.message(CommandStart())
async def start(message: types.Message):
    uid = message.from_user.id
    await db_add_user(uid)

    if is_admin(uid):
        await message.answer("✅ Admin panel", reply_markup=kb_admin())
    else:
        await message.answer("✅ Xush kelibsiz", reply_markup=kb_user())


@dp.message(F.text == "📢 Kinolar bo‘lim")
async def movies_channel(message: types.Message):
    if MOVIES_CHANNEL.startswith("@"):
        await message.answer("📢 Kinolar bo‘lim kanali:", reply_markup=kb_channel_link())
    else:
        await message.answer("⚠️ Kanal sozlanmagan (MOVIES_CHANNEL ni to‘g‘ri yozing).")


@dp.message(F.text == "ℹ️ Yordam")
async def help_msg(message: types.Message):
    if is_admin(message.from_user.id):
        await message.answer(
            "Admin:\n"
            "➕ Kino qo‘shish (kod → nom → video)\n"
            "❌ Kino o‘chirish (kod)\n"
            "📢 Broadcast: /bc (keyingi xabar reklama bo'ladi: matn/rasm/video)\n"
            "📊 Statistika\n\n"
            "User: kino kodini yuborsa video chiqadi."
        )
    else:
        await message.answer("🎬 Kino olish uchun kod yuboring. Masalan: 102 yoki 134")


@dp.message(F.text == "🎬 Kino olish")
async def ask_code(message: types.Message):
    await message.answer("🎬 Kino kodini yuboring.\nMasalan: 102 yoki 134")


@dp.message(F.text == "📊 Statistika")
async def stats(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    u_cnt, m_cnt = await db_stats()
    await message.answer(f"📊 Statistika:\n👤 Userlar: {u_cnt}\n🎬 Kinolar: {m_cnt}")


# -------- Broadcast (media ham)
@dp.message(F.text == "📢 Broadcast")
async def bc_hint(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "📢 Broadcast:\n"
        "1) `/bc` deb yubor\n"
        "2) Keyin reklama xabarini yubor (matn/rasm/video/audio/document)\n\n"
        "Bekor qilish: /cancel",
        parse_mode="Markdown"
    )

@dp.message(F.text == "/cancel")
async def cancel_any(message: types.Message):
    uid = message.from_user.id
    if uid in BC_WAIT:
        BC_WAIT.discard(uid)
        await message.answer("✅ Broadcast bekor qilindi.", reply_markup=kb_admin() if is_admin(uid) else kb_user())
        return
    if uid in ADD_FLOW:
        ADD_FLOW.pop(uid, None)
        await message.answer("✅ Kino qo‘shish bekor qilindi.", reply_markup=kb_admin())
        return
    if uid in DEL_FLOW:
        DEL_FLOW.discard(uid)
        await message.answer("✅ O‘chirish bekor qilindi.", reply_markup=kb_admin())
        return

@dp.message(F.text == "/bc")
async def bc_start(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    BC_WAIT.add(message.from_user.id)
    await message.answer("✅ OK. Endi reklama xabarini yubor (matn/rasm/video/audio/document).")

async def send_broadcast(bot: Bot, from_message: types.Message):
    users = await db_all_users()
    ok = fail = 0

    for (uid,) in users:
        try:
            # copy_message hamma turdagi mediaga ishlaydi
            await bot.copy_message(
                chat_id=uid,
                from_chat_id=from_message.chat.id,
                message_id=from_message.message_id
            )
            ok += 1
        except Exception:
            fail += 1
    return ok, fail


# -------- Admin: Add Movie
@dp.message(F.text == "➕ Kino qo‘shish")
async def add_start(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    ADD_FLOW[message.from_user.id] = AddFlow(step=1)
    await message.answer("1/3) Kino kodini yuboring (masalan: 102 yoki 134)")


# -------- Admin: Delete Movie
@dp.message(F.text == "❌ Kino o‘chirish")
async def del_start(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    DEL_FLOW.add(message.from_user.id)
    await message.answer("❌ O‘chirish uchun kino kodini yuboring.")


# -------- Universal handler
@dp.message()
async def universal(message: types.Message, bot: Bot):
    uid = message.from_user.id

    # 0) Broadcast kutilyapti (matn/rasm/video/etc)
    if is_admin(uid) and uid in BC_WAIT:
        BC_WAIT.discard(uid)
        await message.answer("⏳ Reklama tarqatilyapti...")
        ok, fail = await send_broadcast(bot, message)
        await message.answer(f"✅ Yuborildi: {ok}\n❌ Xato: {fail}", reply_markup=kb_admin())
        return

    # 1) Delete flow
    if is_admin(uid) and uid in DEL_FLOW and message.text:
        code = message.text.strip()
        DEL_FLOW.discard(uid)
        ok = await db_delete_movie(code)
        await message.answer("✅ O‘chirildi" if ok else "❌ Bunday kod topilmadi", reply_markup=kb_admin())
        return

    # 2) Add flow
    if is_admin(uid) and uid in ADD_FLOW:
        flow = ADD_FLOW[uid]

        if flow.step == 1:
            code = (message.text or "").strip()
            if not re.match(r"^[A-Za-z0-9_-]{1,20}$", code):
                await message.answer("❌ Kod noto‘g‘ri. Faqat harf/son. Qayta yuboring.", reply_markup=kb_admin())
                return
            flow.code = code
            flow.step = 2
            ADD_FLOW[uid] = flow
            await message.answer("2/3) Kino nomini yuboring.", reply_markup=kb_admin())
            return

        if flow.step == 2:
            title = (message.text or "").strip()
            if len(title) < 2:
                await message.answer("❌ Nom juda qisqa. Qayta yuboring.", reply_markup=kb_admin())
                return
            flow.title = title
            flow.step = 3
            ADD_FLOW[uid] = flow
            await message.answer("3/3) Endi kinoni VIDEO qilib yuboring.", reply_markup=kb_admin())
            return

        if flow.step == 3:
            if not message.video:
                await message.answer("❌ Video yuboring (Telegram video).", reply_markup=kb_admin())
                return
            await db_add_movie(flow.code, flow.title, message.video.file_id, uid)
            ADD_FLOW.pop(uid, None)
            await message.answer(
                f"✅ Kino qo‘shildi!\n🔑 Kod: {flow.code}\n🎬 Nomi: {flow.title}",
                reply_markup=kb_admin()
            )
            return

    # 3) User kino kod yuborsa
    if message.text:
        code = message.text.strip()

        # menyu tugmalarini o'tkazib yuborish
        if code in {
            "🎬 Kino olish", "📢 Kinolar bo‘lim", "ℹ️ Yordam",
            "➕ Kino qo‘shish", "❌ Kino o‘chirish", "📢 Broadcast", "📊 Statistika"
        }:
            return

        if len(code) > 25:
            return

        movie = await db_get_movie(code)
        if movie:
            title, file_id = movie
            await message.answer_video(video=file_id, caption=f"🎬 {title}\n🔑 Kod: {code}")
        else:
            # TOPILMAGANDA: kanalga yo'naltiramiz
            if MOVIES_CHANNEL.startswith("@"):
                await message.answer(
                    f"❌ Bu kod bo‘yicha kino topilmadi: {code}\n\n"
                    "📢 Kinolarni kanaldan topishingiz mumkin 👇",
                    reply_markup=kb_channel_link()
                )
            else:
                await message.answer(f"❌ Bu kod bo‘yicha kino topilmadi: {code}")


async def main():
    logging.basicConfig(level=logging.INFO)
    await db_init()
    bot = Bot(TOKEN)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
