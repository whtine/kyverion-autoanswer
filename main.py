import os, asyncio, asyncpg, httpx, logging
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Update, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.filters import Command
from contextlib import asynccontextmanager

# Настройка логов (информативно, но без лишнего мусора)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("MY_AUTO_BOT")

TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("RENDER_EXTERNAL_URL")
DB_URL = "postgresql://autoanswer_cfg_user:2UpBtzof467gxNdjkxwC12bRPlaor5y9@dpg-d7utdenlk1mc73aovmfg-a.ohio-postgres.render.com/autoanswer_cfg"
MY_ID = 6956377285 # Твой ID

bot = Bot(token=TOKEN)
dp = Dispatcher()
app = FastAPI()

class Form(StatesGroup):
    waiting_for_text = State()

async def get_conn():
    return await asyncpg.connect(DB_URL)

# --- ГЛАВНАЯ ЛОГИКА АВТООТВЕТА ---
@dp.business_message()
async def business_handler(message: types.Message):
    # Если пишет кто-то другой (не ты сам себе)
    if message.from_user.id != MY_ID:
        conn = await get_conn()
        # Берем настройки именно ТВОЕГО аккаунта
        config = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", MY_ID)
        await conn.close()

        if config and config['is_active']:
            try:
                await message.answer(config['reply_text'])
                logger.info(f"✅ ОТВЕТИЛ: Пользователю {message.from_user.id} | Текст: {config['reply_text']}")
            except Exception as e:
                logger.error(f"❌ ОШИБКА: {e}")
    else:
        # Логируем, когда ты сам пишешь (чтобы видеть, что бот живой)
        logger.info(f"ℹ️ Твоё исходящее сообщение в чат {message.chat.id}. Бот молчит.")

# --- УПРАВЛЕНИЕ (ТОЛЬКО ДЛЯ ТЕБЯ) ---

@dp.message(Command("start"), F.from_user.id == MY_ID)
async def start_cmd(message: types.Message):
    conn = await get_conn()
    user = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", MY_ID)
    if not user:
        await conn.execute("INSERT INTO users (user_id, reply_text, is_active) VALUES ($1, $2, $3)", 
                           MY_ID, "Привет! Я сейчас занят.", False)
        user = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", MY_ID)
    await conn.close()

    status_emoji = "✅ ВКЛ" if user['is_active'] else "❌ ВЫКЛ"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Изменить текст", callback_data="edit")],
        [InlineKeyboardButton(text=f"Статус: {status_emoji}", callback_data="switch")],
        [InlineKeyboardButton(text="👤 Профиль / База", callback_data="me")]
    ])
    await message.answer(f"🤖 **Твой автоответчик**\n\nСтатус: {status_emoji}\nТекст: `{user['reply_text']}`", 
                         reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data == "switch", F.from_user.id == MY_ID)
async def handle_switch(callback: types.CallbackQuery):
    conn = await get_conn()
    row = await conn.fetchrow("SELECT is_active FROM users WHERE user_id = $1", MY_ID)
    new_status = not row['is_active']
    await conn.execute("UPDATE users SET is_active = $1 WHERE user_id = $2", new_status, MY_ID)
    await conn.close()
    await callback.answer(f"Статус: {'ВКЛ' if new_status else 'ВЫКЛ'}")
    await start_cmd(callback.message)
    await callback.message.delete()

@dp.callback_query(F.data == "me", F.from_user.id == MY_ID)
async def handle_profile(callback: types.CallbackQuery):
    conn = await get_conn()
    rows = await conn.fetch("SELECT * FROM users")
    await conn.close()
    
    # Список всех (для контроля)
    db_list = "\n".join([f"{r['user_id']} | {r['is_active']}" for r in rows])
    text = (f"👤 **Твой профиль**\nID: `{MY_ID}`\n\n**База данных:**\n`{db_list}`")
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data == "back")
async def back_to_start(callback: types.CallbackQuery):
    await start_cmd(callback.message)
    await callback.message.delete()

@dp.callback_query(F.data == "edit")
async def edit_text(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("⌨️ Введи новый текст:")
    await state.set_state(Form.waiting_for_text)

@dp.message(Form.waiting_for_text, F.from_user.id == MY_ID)
async def save_text(message: types.Message, state: FSMContext):
    conn = await get_conn()
    await conn.execute("UPDATE users SET reply_text = $1 WHERE user_id = $2", message.text, MY_ID)
    await conn.close()
    await state.clear()
    await message.answer("✅ Сохранено!")
    await start_cmd(message)

# --- ПИНГЕР И ВЕБХУК ---
async def ping_self():
    async with httpx.AsyncClient() as client:
        while True:
            await asyncio.sleep(600)
            try: await client.get(APP_URL)
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
    update = Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}

@app.get("/")
async def root(): return {"status": "running"}
