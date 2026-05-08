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

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
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

# --- БАЗА ДАННЫХ ---
async def get_user_data(user_id):
    conn = await asyncpg.connect(DB_URL)
    row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
    await conn.close()
    return row

async def toggle_user_status(user_id):
    conn = await asyncpg.connect(DB_URL)
    # Атомарный апдейт: если нет юзера - создаем со статусом True, если есть - инвертируем
    await conn.execute('''
        INSERT INTO users (user_id, is_active) VALUES ($1, TRUE)
        ON CONFLICT (user_id) DO UPDATE SET is_active = NOT users.is_active
    ''', user_id)
    await conn.close()

async def update_reply_text(user_id, text):
    conn = await asyncpg.connect(DB_URL)
    await conn.execute('''
        INSERT INTO users (user_id, reply_text) VALUES ($1, $2)
        ON CONFLICT (user_id) DO UPDATE SET reply_text = $2
    ''', user_id, text)
    await conn.close()

# --- АДМИНКА ---
@dp.message(Command("db"), F.from_user.id == ADMIN_ID)
async def export_db(message: types.Message):
    conn = await asyncpg.connect(DB_URL)
    rows = await conn.fetch("SELECT * FROM users")
    await conn.close()
    
    report = "ID | Status | Text\n" + "-"*30 + "\n"
    for r in rows:
        report += f"{r['user_id']} | {r['is_active']} | {r['reply_text'][:20]}...\n"
    
    file = BufferedInputFile(report.encode(), filename="database.txt")
    await message.answer_document(file, caption="Полная выгрузка базы данных.")

# --- КОМАНДЫ ПОЛЬЗОВАТЕЛЯ ---
@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    user = await get_user_data(message.from_user.id)
    # Если юзера нет, создаем дефолт
    if not user:
        await update_reply_text(message.from_user.id, "Я сейчас занят, отвечу позже.")
        user = await get_user_data(message.from_user.id)

    status_emoji = "✅ ВКЛ" if user['is_active'] else "❌ ВЫКЛ"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Изменить текст", callback_data="edit")],
        [InlineKeyboardButton(text=f"Статус: {status_emoji}", callback_data="switch")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="me")]
    ])
    await message.answer(f"🤖 **Настройки автоответчика**\n\nСтатус: {status_emoji}\nТекст: {user['reply_text']}", 
                         reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data == "switch")
async def handle_switch(callback: types.CallbackQuery):
    await toggle_user_status(callback.from_user.id)
    user = await get_user_data(callback.from_user.id)
    await callback.answer(f"Теперь: {'ВКЛ' if user['is_active'] else 'ВЫКЛ'}")
    
    # Обновляем сообщение (вместо удаления и пересылки, чтобы не мерцало)
    status_emoji = "✅ ВКЛ" if user['is_active'] else "❌ ВЫКЛ"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Изменить текст", callback_data="edit")],
        [InlineKeyboardButton(text=f"Статус: {status_emoji}", callback_data="switch")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="me")]
    ])
    await callback.message.edit_text(f"🤖 **Настройки автоответчика**\n\nСтатус: {status_emoji}\nТекст: {user['reply_text']}", 
                                     reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data == "me")
async def handle_profile(callback: types.CallbackQuery):
    user = await get_user_data(callback.from_user.id)
    text = f"👤 **Ваш профиль**\n\nID: `{user['user_id']}`\nСтатус: {user['is_active']}\nТекст: {user['reply_text']}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data == "back")
async def handle_back(callback: types.CallbackQuery):
    await start_cmd(callback.message)
    await callback.message.delete()

@dp.callback_query(F.data == "edit")
async def handle_edit(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Пришли новый текст:")
    await state.set_state(Form.waiting_for_text)

@dp.message(Form.waiting_for_text)
async def save_text(message: types.Message, state: FSMContext):
    await update_reply_text(message.from_user.id, message.text)
    await state.clear()
    await message.answer("✅ Текст обновлен!")
    await start_cmd(message)

# --- АВТООТВЕТ ---
@dp.business_message()
async def business_handler(message: types.Message):
    # Твой лог показал, что настройки ищутся по chat.id (это владелец)
    owner_id = message.chat.id
    user_config = await get_user_data(owner_id)
    
    if user_config and user_config['is_active']:
        # Если пишет НЕ владелец аккаунта
        if message.from_user.id != owner_id:
            logger.info(f"Auto-reply sent for owner {owner_id}")
            await message.answer(user_config['reply_text'])

# --- СЕРВЕР ---
async def ping_self():
    async with httpx.AsyncClient() as client:
        while True:
            await asyncio.sleep(600)
            try: await client.get(APP_URL)
            except: pass

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Создаем таблицу при старте, если вдруг она пропала
    conn = await asyncpg.connect(DB_URL)
    await conn.execute('CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, reply_text TEXT, is_active BOOLEAN DEFAULT FALSE)')
    await conn.close()
    
    await bot.set_webhook(url=f"{APP_URL}/webhook", allowed_updates=["business_message", "message", "callback_query"])
    asyncio.create_task(ping_self())
    yield

app.router.lifespan_context = lifespan

@app.post("/webhook")
async def webhook(request: Request):
    update = Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}

@app.get("/")
async def root(): return {"status": "online"}
