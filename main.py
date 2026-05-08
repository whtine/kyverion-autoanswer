import os
import asyncio
import asyncpg
import httpx
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Update, InlineKeyboardMarkup, InlineKeyboardButton
from contextlib import asynccontextmanager

# --- КОНФИГУРАЦИЯ ---
TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("RENDER_EXTERNAL_URL")
# Твоя ссылка на БД
DB_URL = "postgresql://autoanswer_cfg_user:2UpBtzof467gxNdjkxwC12bRPlaor5y9@dpg-d7utdenlk1mc73aovmfg-a.ohio-postgres.render.com/autoanswer_cfg"

bot = Bot(token=TOKEN)
dp = Dispatcher()
app = FastAPI()

class Form(StatesGroup):
    waiting_for_text = State()

# --- ФУНКЦИИ БАЗЫ ДАННЫХ ---
async def init_db():
    conn = await asyncpg.connect(DB_URL)
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            reply_text TEXT DEFAULT 'Я сейчас занят, отвечу позже.',
            is_active BOOLEAN DEFAULT FALSE
        )
    ''')
    await conn.close()

async def get_user_data(user_id):
    conn = await asyncpg.connect(DB_URL)
    row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
    await conn.close()
    return row

async def update_user_data(user_id, text=None, status=None):
    conn = await asyncpg.connect(DB_URL)
    # Создаем запись, если её нет
    await conn.execute('''
        INSERT INTO users (user_id) VALUES ($1)
        ON CONFLICT (user_id) DO NOTHING
    ''', user_id)
    
    if text is not None:
        await conn.execute("UPDATE users SET reply_text = $1 WHERE user_id = $2", text, user_id)
    if status is not None:
        await conn.execute("UPDATE users SET is_active = $1 WHERE user_id = $2", status, user_id)
    await conn.close()

# --- ОБРАБОТКА КОМАНД ---

@dp.message(F.text == "/start")
async def start_cmd(message: types.Message):
    user = await get_user_data(message.from_user.id)
    if not user:
        await update_user_data(message.from_user.id)
        user = await get_user_data(message.from_user.id)

    status_emoji = "✅ ВКЛ" if user['is_active'] else "❌ ВЫКЛ"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Изменить текст", callback_data="edit")],
        [InlineKeyboardButton(text=f"Статус: {status_emoji}", callback_data="switch")],
        [InlineKeyboardButton(text="👤 Мой профиль", callback_data="me")]
    ])
    
    await message.answer(
        f"🤖 **Управление автоответчиком**\n\n"
        f"Твой текущий статус: {status_emoji}\n"
        f"Текст ответа: _{user['reply_text']}_",
        reply_markup=kb,
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "switch")
async def toggle_status(callback: types.CallbackQuery):
    user = await get_user_data(callback.from_user.id)
    new_status = not user['is_active']
    await update_user_data(callback.from_user.id, status=new_status)
    await callback.answer("Статус обновлен")
    await start_cmd(callback.message)

@dp.callback_query(F.data == "edit")
async def edit_text(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите новый текст для автоответа:")
    await state.set_state(Form.waiting_for_text)
    await callback.answer()

@dp.message(Form.waiting_for_text)
async def save_text(message: types.Message, state: FSMContext):
    await update_user_data(message.from_user.id, text=message.text)
    await state.clear()
    await message.answer("✅ Текст сохранен!")
    await start_cmd(message)

# --- ГЛАВНАЯ ЛОГИКА АВТООТВЕТА ---
@dp.business_message()
async def business_handler(message: types.Message):
    # В бизнес-сообщениях chat.id — это ID владельца аккаунта
    user_id = message.chat.id 
    user_config = await get_user_data(user_id)
    
    if user_config and user_config['is_active']:
        # Отвечаем, если пишет КТО-ТО ДРУГОЙ (не сам владелец)
        if message.from_user.id != user_id:
            await message.answer(user_config['reply_text'])

# --- WEBHOOK & LIFESPAN ---
async def ping_self():
    async with httpx.AsyncClient() as client:
        while True:
            await asyncio.sleep(600) # 10 минут
            try: await client.get(APP_URL)
            except: pass

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await bot.set_webhook(
        url=f"{APP_URL}/webhook",
        allowed_updates=["business_message", "message", "callback_query"]
    )
    task = asyncio.create_task(ping_self())
    yield
    task.cancel()

app.router.lifespan_context = lifespan

@app.post("/webhook")
async def webhook(request: Request):
    update = Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)

@app.get("/")
async def root(): return {"status": "working", "db": "connected"}
