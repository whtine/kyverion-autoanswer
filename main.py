import os
import asyncio
import asyncpg
import httpx
import logging  # Добавляем логирование
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Update, InlineKeyboardMarkup, InlineKeyboardButton
from contextlib import asynccontextmanager

# Настройка логов в консоль
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("RENDER_EXTERNAL_URL")
DB_URL = "postgresql://autoanswer_cfg_user:2UpBtzof467gxNdjkxwC12bRPlaor5y9@dpg-d7utdenlk1mc73aovmfg-a.ohio-postgres.render.com/autoanswer_cfg"

bot = Bot(token=TOKEN)
dp = Dispatcher()
app = FastAPI()

class Form(StatesGroup):
    waiting_for_text = State()

# --- ФУНКЦИИ БАЗЫ ДАННЫХ ---
async def init_db():
    logger.info("Initializing Database...")
    conn = await asyncpg.connect(DB_URL)
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            reply_text TEXT DEFAULT 'Я сейчас занят, отвечу позже.',
            is_active BOOLEAN DEFAULT FALSE
        )
    ''')
    await conn.close()
    logger.info("Database initialized successfully.")

async def get_user_data(user_id):
    conn = await asyncpg.connect(DB_URL)
    row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
    await conn.close()
    logger.info(f"Fetched data for user {user_id}: {row}")
    return row

async def update_user_data(user_id, text=None, status=None):
    logger.info(f"Updating user {user_id}: text={text}, status={status}")
    conn = await asyncpg.connect(DB_URL)
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

    # Исправляем отображение статуса
    status_emoji = "✅ ВКЛ" if user['is_active'] else "❌ ВЫКЛ"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Изменить текст", callback_data="edit")],
        [InlineKeyboardButton(text=f"Статус: {status_emoji}", callback_data="switch")],
        [InlineKeyboardButton(text="👤 Мой профиль", callback_data="me")]
    ])
    
    await message.answer(
        f"🤖 **Панель управления**\n\nСтатус: {status_emoji}\nТекст: {user['reply_text']}",
        reply_markup=kb,
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "switch")
async def toggle_status(callback: types.CallbackQuery):
    user = await get_user_data(callback.from_user.id)
    # Инвертируем текущий статус
    new_status = not user['is_active']
    await update_user_data(callback.from_user.id, status=new_status)
    
    logger.info(f"User {callback.from_user.id} changed status to {new_status}")
    await callback.answer(f"Статус: {'Включен' if new_status else 'Выключен'}")
    
    # Сразу вызываем обновление меню
    await start_cmd(callback.message)
    # Удаляем старое сообщение, чтобы не плодить копии
    await callback.message.delete()

@dp.callback_query(F.data == "me")
async def show_profile(callback: types.CallbackQuery):
    user = await get_user_data(callback.from_user.id)
    status_text = "Работает" if user['is_active'] else "Отключен"
    
    text = (f"👤 **Ваш профиль**\n\n"
            f"🆔 ID: `{user['user_id']}`\n"
            f"📢 Текст: {user['reply_text']}\n"
            f"⚙️ Состояние: {status_text}")
    
    # Добавим кнопку "Назад", чтобы вернуться в меню
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")]
    ])
    
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: types.CallbackQuery):
    await start_cmd(callback.message)
    await callback.message.delete()

# --- ОСТАЛЬНАЯ ЛОГИКА ---

@dp.callback_query(F.data == "edit")
async def edit_text(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите новый текст:")
    await state.set_state(Form.waiting_for_text)
    await callback.answer()

@dp.message(Form.waiting_for_text)
async def save_text(message: types.Message, state: FSMContext):
    await update_user_data(message.from_user.id, text=message.text)
    await state.clear()
    await message.answer("✅ Сохранено!")
    await start_cmd(message)

@dp.business_message()
async def business_handler(message: types.Message):
    user_id = message.chat.id 
    user_config = await get_user_data(user_id)
    
    if user_config and user_config['is_active']:
        if message.from_user.id != user_id:
            logger.info(f"Sending auto-reply to {message.from_user.id} in account of {user_id}")
            await message.answer(user_config['reply_text'])

# --- WEBHOOK & LIFESPAN ---
async def ping_self():
    async with httpx.AsyncClient() as client:
        while True:
            await asyncio.sleep(600)
            try:
                res = await client.get(APP_URL)
                logger.info(f"Self-ping status: {res.status_code}")
            except Exception as e:
                logger.error(f"Self-ping failed: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    webhook_url = f"{APP_URL}/webhook"
    await bot.set_webhook(url=webhook_url, allowed_updates=["business_message", "message", "callback_query"])
    logger.info(f"Webhook set to {webhook_url}")
    task = asyncio.create_task(ping_self())
    yield
    task.cancel()

app.router.lifespan_context = lifespan

@app.post("/webhook")
async def webhook(request: Request):
    try:
        body = await request.json()
        logger.info(f"Incoming update: {body}")
        update = Update.model_validate(body, context={"bot": bot})
        await dp.feed_update(bot, update)
    except Exception as e:
        logger.error(f"Error processing update: {e}")
    return {"ok": True}

@app.get("/")
async def root(): 
    return {"status": "active"}
