import os
import asyncio
import asyncpg
import httpx
import logging
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Update, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.filters import Command
from contextlib import asynccontextmanager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("RENDER_EXTERNAL_URL")
DB_URL = "postgresql://autoanswer_cfg_user:2UpBtzof467gxNdjkxwC12bRPlaor5y9@dpg-d7utdenlk1mc73aovmfg-a.ohio-postgres.render.com/autoanswer_cfg"
ADMIN_ID = 6956377285

bot = Bot(token=TOKEN)
dp = Dispatcher()
app = FastAPI()

class Form(StatesGroup):
    waiting_for_text = State()

# --- СИСТЕМНЫЕ ФУНКЦИИ БД ---
async def get_conn():
    return await asyncpg.connect(DB_URL)

async def get_user_data(user_id):
    conn = await get_conn()
    row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
    await conn.close()
    return row

# --- МЕНЮ ---
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
    user = await get_user_data(message.from_user.id)
    if not user:
        conn = await get_conn()
        await conn.execute("INSERT INTO users (user_id, reply_text, is_active) VALUES ($1, $2, $3)", 
                           message.from_user.id, "Я сейчас занят.", False)
        await conn.close()
        user = await get_user_data(message.from_user.id)

    await message.answer(
        f"🤖 **Настройки автоответчика**\n\n"
        f"Статус: {'✅ Работает' if user['is_active'] else '❌ Выключен'}\n"
        f"Текст: `{user['reply_text']}`",
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
    
    await callback.answer(f"Статус: {'ВКЛ' if new_status else 'ВЫКЛ'}")
    # Обновляем это же сообщение
    await callback.message.edit_text(
        f"🤖 **Настройки автоответчика**\n\n"
        f"Статус: {'✅ Работает' if new_status else '❌ Выключен'}\n"
        f"Текст: `{user['reply_text']}`",
        reply_markup=get_main_kb(callback.from_user.id, new_status),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "me")
async def handle_profile(callback: types.CallbackQuery):
    user = await get_user_data(callback.from_user.id)
    status_str = "Активен" if user['is_active'] else "Отключен"
    text = (f"👤 **Профиль пользователя**\n\n"
            f"🆔 ID: `{user['user_id']}`\n"
            f"⚙️ Состояние: {status_str}\n"
            f"💬 Текст: {user['reply_text']}")
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")]])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data == "back_to_menu")
async def back_menu(callback: types.CallbackQuery):
    user = await get_user_data(callback.from_user.id)
    await callback.message.edit_text(
        f"🤖 **Настройки автоответчика**\n\n"
        f"Статус: {'✅ Работает' if user['is_active'] else '❌ Выключен'}\n"
        f"Текст: `{user['reply_text']}`",
        reply_markup=get_main_kb(callback.from_user.id, user['is_active']),
        parse_mode="Markdown"
    )

# --- АДМИН КОМАНДЫ ---

@dp.callback_query(F.data == "admin_db", F.from_user.id == ADMIN_ID)
@dp.message(Command("db_view"), F.from_user.id == ADMIN_ID)
async def db_view(event: types.Message | types.CallbackQuery):
    conn = await get_conn()
    rows = await conn.fetch("SELECT * FROM users")
    await conn.close()
    
    res = "USER_ID | ACTIVE | TEXT\n" + "-"*40 + "\n"
    for r in rows:
        res += f"{r['user_id']} | {r['is_active']} | {r['reply_text'][:15]}...\n"
    
    file = BufferedInputFile(res.encode(), filename="users_db.txt")
    
    if isinstance(event, types.CallbackQuery):
        await event.message.answer_document(file, caption="📂 Текущая база пользователей")
        await event.answer()
    else:
        await event.answer_document(file)

@dp.message(Command("db_clear"), F.from_user.id == ADMIN_ID)
async def db_clear(message: types.Message):
    conn = await get_conn()
    await conn.execute("DELETE FROM users WHERE user_id != $1", ADMIN_ID)
    await conn.close()
    await message.answer("⚠ База данных очищена (кроме вашего аккаунта).")

# --- ИЗМЕНЕНИЕ ТЕКСТА ---
@dp.callback_query(F.data == "edit")
async def edit_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("⌨ Введите новый текст ответа:")
    await state.set_state(Form.waiting_for_text)
    await callback.answer()

@dp.message(Form.waiting_for_text)
async def edit_finish(message: types.Message, state: FSMContext):
    conn = await get_conn()
    await conn.execute("UPDATE users SET reply_text = $1 WHERE user_id = $2", message.text, message.from_user.id)
    await conn.close()
    await state.clear()
    await message.answer("✅ Текст обновлен успешно!")
    await start_cmd(message)

# --- ЛОГИКА БИЗНЕСА (ФИКС) ---
@dp.business_message()
async def business_handler(message: types.Message):
    # ВАЖНО: В бизнес-сообщении chat.id — это ВСЕГДА владелец аккаунта
    owner_id = message.chat.id
    user_config = await get_user_data(owner_id)
    
    if user_config and user_config['is_active']:
        # Отвечаем, только если пишет НЕ владелец
        if message.from_user.id != owner_id:
            try:
                await message.answer(user_config['reply_text'])
                logger.info(f"Sent reply for {owner_id} to {message.from_user.id}")
            except Exception as e:
                logger.error(f"Error sending business reply: {e}")

# --- ИНИЦИАЛИЗАЦИЯ И ВЕБХУК ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = await get_conn()
    await conn.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY, 
        reply_text TEXT, 
        is_active BOOLEAN DEFAULT FALSE)''')
    await conn.close()
    await bot.set_webhook(url=f"{APP_URL}/webhook", allowed_updates=["business_message", "message", "callback_query"])
    asyncio.create_task(ping_self())
    yield

async def ping_self():
    async with httpx.AsyncClient() as client:
        while True:
            await asyncio.sleep(600)
            try: await client.get(APP_URL)
            except: pass

app.router.lifespan_context = lifespan

@app.post("/webhook")
async def webhook(request: Request):
    update = Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}

@app.get("/")
async def root(): return {"status": "ok"}
