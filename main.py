import os, asyncio, asyncpg, httpx, logging
import google.generativeai as genai
from datetime import datetime
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Update, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from contextlib import asynccontextmanager

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("AI_PRO_BOT")

# Конфигурация
TOKEN = os.getenv("BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
APP_URL = os.getenv("RENDER_EXTERNAL_URL")
DB_URL = "postgresql://autoanswer_cfg_user:2UpBtzof467gxNdjkxwC12bRPlaor5y9@dpg-d7utdenlk1mc73aovmfg-a.ohio-postgres.render.com/autoanswer_cfg"
MY_ID = 6956377285

# Инициализация Gemini SDK
genai.configure(api_key=GEMINI_KEY)
ai_model = genai.GenerativeModel('gemini-1.5-flash') # Самая быстрая и стабильная модель

bot = Bot(token=TOKEN)
dp = Dispatcher()
app = FastAPI()
active_waits = {}

class Form(StatesGroup):
    waiting_for_delay = State()

async def get_conn():
    return await asyncpg.connect(DB_URL)

# --- ЛОГИКА ИИ (ЧЕРЕЗ SDK) ---

async def ask_gemini(user_text, zone_context, remaining_msgs):
    if not GEMINI_KEY:
        return "Извини, мой ИИ-модуль не настроен.", 1
    
    prompt = (
        f"Ты — ИИ-ассистент владельца. Он сейчас: {zone_context}. "
        f"Ответь вежливо на языке пользователя. "
        f"В конце добавь: (ИИ-ассистент. Осталось: {remaining_msgs} зап.). "
        f"Оцени важность сообщения от 1 до 3 и в самом конце напиши [P:X], где X - число."
        f"Текст пользователя: {user_text}"
    )

    try:
        # Генерация контента (используем asyncio.to_thread, так как SDK блокирующий)
        response = await asyncio.to_thread(ai_model.generate_content, prompt)
        full_text = response.text
        
        priority = 1
        if "[P:" in full_text:
            try:
                priority_str = full_text.split("[P:")[1][0]
                priority = int(priority_str) if priority_str.isdigit() else 1
                full_text = full_text.split("[P:")[0].strip()
            except: pass
        
        return full_text, priority
    except Exception as e:
        logger.error(f"Gemini SDK Error: {e}")
        return "Привет! Сейчас я занят, отвечу позже.", 1

# --- БИЗНЕС ЛОГИКА ---

@dp.business_message()
async def business_handler(message: types.Message):
    chat_id = message.chat.id
    if message.from_user.id == MY_ID:
        if chat_id in active_waits:
            active_waits[chat_id].cancel()
            active_waits.pop(chat_id, None)
        return

    conn = await get_conn()
    u = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", MY_ID)
    
    if u and u['is_active']:
        limit_data = await conn.fetchrow("SELECT msg_count FROM chat_limits WHERE chat_id = $1", chat_id)
        count = limit_data['msg_count'] if limit_data else 0
        
        if count >= 10:
            await conn.close()
            return 
        
        if chat_id in active_waits:
            await conn.close()
            return
            
        h = (datetime.utcnow().hour + 2) % 24
        zone_info = "Обычное время"
        if 8 <= h < 13: zone_info = "На учебе, отвечает раз в час"
        elif h >= 23 or h < 8: zone_info = "Спит, ответит утром"

        ai_reply, priority = await ask_gemini(message.text, zone_info, 10 - count)

        delay = u.get('delay_sec', 30)
        task = asyncio.create_task(delayed_ai_reply(message, ai_reply, delay, priority, count, conn))
        active_waits[chat_id] = task
    else:
        await conn.close()

async def delayed_ai_reply(message, text, delay, priority, count, conn):
    try:
        await asyncio.sleep(delay)
        await message.answer(text)
        
        if count == 0:
            await conn.execute("INSERT INTO chat_limits (chat_id, msg_count) VALUES ($1, 1)", message.chat.id)
        else:
            await conn.execute("UPDATE chat_limits SET msg_count = msg_count + 1 WHERE chat_id = $1", message.chat.id)
            
        if priority >= 2:
            tag = "‼️ СРОЧНО" if priority == 3 else "ℹ️ ВАЖНО"
            await bot.send_message(
                MY_ID, 
                f"{tag}\nОт: {message.from_user.full_name}\nТекст: {message.text}\n\nИИ ответил: {text}"
            )
            
    except Exception as e:
        logger.error(f"Reply error: {e}")
    finally:
        await conn.close()
        active_waits.pop(message.chat.id, None)

# --- МЕНЮ ---

@dp.message(Command("start"), F.from_user.id == MY_ID)
async def start_cmd(message: types.Message):
    conn = await get_conn()
    u = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", MY_ID)
    if not u:
        await conn.execute("INSERT INTO users (user_id, is_active) VALUES ($1, TRUE)", MY_ID)
        u = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", MY_ID)
    await conn.close()
    
    status = "✅ ИИ АКТИВЕН" if u['is_active'] else "❌ ВЫКЛЮЧЕН"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⏱ КД: {u['delay_sec']}с", callback_data="set_delay")],
        [InlineKeyboardButton(text=f"Статус: {status}", callback_data="switch")],
        [InlineKeyboardButton(text="🗑 Сбросить лимиты ИИ", callback_data="clear_limits")]
    ])
    
    await message.answer(f"🤖 **ИИ-Автоответчик (Gemini 1.5 Flash)**\n\nСтатус: {status}\nЧешское время: (UTC+2)\nЛимит: 10 сообщ/чел.", reply_markup=kb)

@dp.callback_query(F.data == "switch")
async def toggle(callback: types.CallbackQuery):
    conn = await get_conn()
    await conn.execute("UPDATE users SET is_active = NOT is_active WHERE user_id = $1", MY_ID)
    await conn.close()
    await start_cmd(callback.message)
    await callback.message.delete()

@dp.callback_query(F.data == "clear_limits")
async def clear_limits(callback: types.CallbackQuery):
    conn = await get_conn()
    await conn.execute("DELETE FROM chat_limits")
    await conn.close()
    await callback.answer("Лимиты сообщений сброшены!")

@dp.callback_query(F.data == "set_delay")
async def delay_req(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите задержку (сек):")
    await state.set_state(Form.waiting_for_delay)

@dp.message(Form.waiting_for_delay)
async def delay_save(message: types.Message, state: FSMContext):
    if message.text.isdigit():
        conn = await get_conn()
        await conn.execute("UPDATE users SET delay_sec = $1 WHERE user_id = $2", int(message.text), MY_ID)
        await conn.close()
        await state.clear()
        await start_cmd(message)

# --- СЛУЖЕБНОЕ ---

async def init_db():
    conn = await get_conn()
    await conn.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY, is_active BOOLEAN DEFAULT FALSE, delay_sec INTEGER DEFAULT 30)''')
    await conn.execute('''CREATE TABLE IF NOT EXISTS chat_limits (
        chat_id BIGINT PRIMARY KEY, msg_count INTEGER DEFAULT 0)''')
    await conn.close()

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await bot.set_webhook(url=f"{APP_URL}/webhook", allowed_updates=["business_message", "message", "callback_query"])
    yield

app.router.lifespan_context = lifespan

@app.post("/webhook")
async def webhook(request: Request):
    update = Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}

@app.get("/")
async def root():
    return {"status": "working", "ai_mode": "gemini-1.5-flash-sdk"}
