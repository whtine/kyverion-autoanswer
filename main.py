import os
import asyncio
import asyncpg
import httpx
import logging
import json
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Update, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.filters import Command
from contextlib import asynccontextmanager

# РАСШИРЕННОЕ ЛОГИРОВАНИЕ
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
)
logger = logging.getLogger("BusinessBot")

TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("RENDER_EXTERNAL_URL")
DB_URL = "postgresql://autoanswer_cfg_user:2UpBtzof467gxNdjkxwC12bRPlaor5y9@dpg-d7utdenlk1mc73aovmfg-a.ohio-postgres.render.com/autoanswer_cfg"
ADMIN_ID = 6956377285

bot = Bot(token=TOKEN)
dp = Dispatcher()
app = FastAPI()

class Form(StatesGroup):
    waiting_for_text = State()

async def get_conn():
    return await asyncpg.connect(DB_URL)

async def get_user_data(user_id):
    conn = await get_conn()
    row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
    await conn.close()
    logger.info(f"[DB] Запрос данных для ID {user_id}: Найдено -> {dict(row) if row else 'Ничего'}")
    return row

def get_main_kb(user_id, is_active):
    status_emoji = "✅ ВКЛ" if is_active else "❌ ВЫКЛ"
    buttons = [
        [InlineKeyboardButton(text="📝 Изменить текст", callback_data="edit")],
        [InlineKeyboardButton(text=f"Статус: {status_emoji}", callback_data="switch")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="me")]
    ]
    if user_id == ADMIN_ID:
        buttons.append([InlineKeyboardButton(text="📁 База данных (ADM)", callback_data="admin_db")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# --- ОБРАБОТЧИКИ ---

@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    logger.info(f"[CMD] /start от пользователя {message.from_user.id}")
    user = await get_user_data(message.from_user.id)
    if not user:
        conn = await get_conn()
        await conn.execute("INSERT INTO users (user_id, reply_text, is_active) VALUES ($1, $2, $3)", 
                           message.from_user.id, "Я сейчас занят.", False)
        await conn.close()
        user = await get_user_data(message.from_user.id)

    await message.answer(
        f"🤖 **Настройки автоответчика**\n\nСтатус: {'✅ Работает' if user['is_active'] else '❌ Выключен'}\nТекст: `{user['reply_text']}`",
        reply_markup=get_main_kb(message.from_user.id, user['is_active']),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "switch")
async def handle_switch(callback: types.CallbackQuery):
    user = await get_user_data(callback.from_user.id)
    new_status = not user['is_active']
    conn = await get_conn()
    await conn.execute("UPDATE users SET is_active = $1 WHERE user_id = $2", new_status, callback.from_user.id)
    await conn.close()
    
    logger.info(f"[ACT] Юзер {callback.from_user.id} сменил статус на {new_status}")
    await callback.answer(f"Статус: {'ВКЛ' if new_status else 'ВЫКЛ'}")
    
    # Обновляем интерфейс
    await callback.message.edit_text(
        f"🤖 **Настройки автоответчика**\n\nСтатус: {'✅ Работает' if new_status else '❌ Выключен'}\nТекст: `{user['reply_text']}`",
        reply_markup=get_main_kb(callback.from_user.id, new_status),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "me")
async def handle_profile(callback: types.CallbackQuery):
    user = await get_user_data(callback.from_user.id)
    status_str = "✅ Включен" if user['is_active'] else "❌ Отключен"
    text = (f"👤 **Ваш профиль**\n\n🆔 ID: `{user['user_id']}`\n⚙️ Статус: {status_str}\n💬 Текст: {user['reply_text']}")
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")]])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data == "back_to_menu")
async def back_menu(callback: types.CallbackQuery):
    user = await get_user_data(callback.from_user.id)
    await callback.message.edit_text(
        f"🤖 **Настройки автоответчика**\n\nСтатус: {'✅ Работает' if user['is_active'] else '❌ Выключен'}\nТекст: `{user['reply_text']}`",
        reply_markup=get_main_kb(callback.from_user.id, user['is_active']),
        parse_mode="Markdown"
    )

# --- БИЗНЕС ЛОГИКА (С ЛОГАМИ) ---
@dp.business_message()
async def business_handler(message: types.Message):
    owner_id = message.chat.id
    sender_id = message.from_user.id
    
    logger.info(f"[BIZ] Новое сообщение! Аккаунт владельца: {owner_id}, Отправитель: {sender_id}")
    
    user_config = await get_user_data(owner_id)
    
    if not user_config:
        logger.warning(f"[BIZ] Настройки для владельца {owner_id} не найдены в БД!")
        return

    logger.info(f"[BIZ] Проверка статуса владельца {owner_id}: is_active = {user_config['is_active']}")

    if user_config['is_active'] is True:
        if sender_id != owner_id:
            logger.info(f"[BIZ] Пытаюсь отправить ответ от имени {owner_id} пользователю {sender_id}")
            try:
                await message.answer(user_config['reply_text'])
                logger.info(f"[BIZ] ✅ Успешно ответил!")
            except Exception as e:
                logger.error(f"[BIZ] ❌ Ошибка отправки: {e}")
        else:
            logger.info(f"[BIZ] Владелец сам пишет в чате, бот молчит.")
    else:
        logger.info(f"[BIZ] Автоответчик для {owner_id} ВЫКЛЮЧЕН. Пропускаю.")

# --- АДМИН-ФУНКЦИИ ---
@dp.callback_query(F.data == "admin_db", F.from_user.id == ADMIN_ID)
async def admin_db_btn(callback: types.CallbackQuery):
    conn = await get_conn()
    rows = await conn.fetch("SELECT * FROM users")
    await conn.close()
    res = "ID | ACTIVE | TEXT\n" + "-"*30 + "\n"
    for r in rows: res += f"{r['user_id']} | {r['is_active']} | {r['reply_text'][:15]}\n"
    file = BufferedInputFile(res.encode(), filename="db.txt")
    await callback.message.answer_document(file, caption="База данных")
    await callback.answer()

@dp.message(Command("db_clear"), F.from_user.id == ADMIN_ID)
async def db_clear(message: types.Message):
    conn = await get_conn()
    await conn.execute("DELETE FROM users WHERE user_id != $1", ADMIN_ID)
    await conn.close()
    await message.answer("🧹 База очищена.")

# --- ОСТАЛЬНОЕ ---
@dp.callback_query(F.data == "edit")
async def edit_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("⌨ Введите новый текст:")
    await state.set_state(Form.waiting_for_text)

@dp.message(Form.waiting_for_text)
async def edit_finish(message: types.Message, state: FSMContext):
    conn = await get_conn()
    await conn.execute("UPDATE users SET reply_text = $1 WHERE user_id = $2", message.text, message.from_user.id)
    await conn.close()
    await state.clear()
    await message.answer("✅ Текст обновлен!")
    await start_cmd(message)

# --- LIFESPAN ---
async def ping_self():
    async with httpx.AsyncClient() as client:
        while True:
            await asyncio.sleep(600)
            try: 
                r = await client.get(APP_URL)
                logger.info(f"[PING] Self-ping: {r.status_code}")
            except: pass

@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = await get_conn()
    await conn.execute('CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, reply_text TEXT, is_active BOOLEAN DEFAULT FALSE)')
    await conn.close()
    await bot.set_webhook(url=f"{APP_URL}/webhook", allowed_updates=["business_message", "message", "callback_query"])
    asyncio.create_task(ping_self())
    yield

app.router.lifespan_context = lifespan

@app.post("/webhook")
async def webhook(request: Request):
    body = await request.json()
    # logger.info(f"[WEBHOOK] Входящие данные: {json.dumps(body)}") # Раскомментируй, если нужны совсем сырые данные
    update = Update.model_validate(body, context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}

@app.get("/")
async def root(): return {"status": "working"}
