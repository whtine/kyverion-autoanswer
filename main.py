import os, asyncio, asyncpg, httpx, logging
from datetime import datetime
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Update, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from contextlib import asynccontextmanager

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("AI_ULTRA_BOT")

TOKEN = os.getenv("BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
APP_URL = os.getenv("RENDER_EXTERNAL_URL")
DB_URL = "postgresql://autoanswer_cfg_user:2UpBtzof467gxNdjkxwC12bRPlaor5y9@dpg-d7utdenlk1mc73aovmfg-a.ohio-postgres.render.com/autoanswer_cfg"
MY_ID = 6956377285

bot = Bot(token=TOKEN)
dp = Dispatcher()
app = FastAPI()

# Храним историю: кто уже получил первый ответ
# {chat_id: True}
replied_once = {} 
active_waits = {}

class Form(StatesGroup):
    waiting_for_delay = State()
    waiting_for_limit = State()
    waiting_for_instructions = State()

async def get_conn():
    return await asyncpg.connect(DB_URL)

# --- ЛОГИКА ИИ ---

async def ask_gemini(user_text, zone_context, remaining_msgs, instructions):
    if not GEMINI_KEY: return "Error key", 1
        
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3-flash-preview:generateContent?key={GEMINI_KEY}"
    
    prompt = (
        f"INSTRUCTION: {instructions}\n"
        f"OWNER CONTEXT: {zone_context}\n"
        f"USER MESSAGE: {user_text}\n\n"
        f"TASK: Answer politely in the USER'S LANGUAGE. "
        f"At the very end, add a phrase in the USER'S LANGUAGE that means: "
        f"'(AI Assistant. Remaining messages: {remaining_msgs})'. "
        f"Rate importance 1-3 and add [P:X] at the end."
    )
    
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.8, "maxOutputTokens": 800}
    }
    
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(url, json=payload, timeout=20.0)
            data = resp.json()
            full_text = data['candidates'][0]['content']['parts'][0]['text']
            
            priority = 1
            if "[P:" in full_text:
                try:
                    priority = int(full_text.split("[P:")[1][0])
                    full_text = full_text.split("[P:")[0].strip()
                except: pass
            return full_text, priority
        except Exception as e:
            logger.error(f"AI Error: {e}")
            return "...", 1

# --- ОБРАБОТКА БИЗНЕС-СООБЩЕНИЙ ---

@dp.business_message()
async def business_handler(message: types.Message):
    chat_id = message.chat.id
    
    # Если пишет владелец — сбрасываем всё
    if message.from_user.id == MY_ID:
        if chat_id in active_waits:
            active_waits[chat_id].cancel()
            active_waits.pop(chat_id, None)
        replied_once.pop(chat_id, None) # Сброс instant-ответа
        return

    conn = await get_conn()
    u = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", MY_ID)
    if not u or not u['is_active']:
        await conn.close()
        return

    # Проверка лимитов
    limit_data = await conn.fetchrow("SELECT msg_count FROM chat_limits WHERE chat_id = $1", chat_id)
    count = limit_data['msg_count'] if limit_data else 0
    if count >= u['max_limit']:
        await conn.close()
        return

    # Определяем время (Чехия UTC+2)
    h = (datetime.utcnow().hour + 2) % 24
    zone = "обычное время"
    if 8 <= h < 14: zone = "на занятиях"
    elif h >= 23 or h < 8: zone = "спит"

    # Запрос к ИИ
    ai_reply, priority = await ask_gemini(message.text, zone, u['max_limit'] - count, u['ai_instructions'])

    # Логика КД
    # Если мы уже отвечали в этот чат (replied_once), шлем сразу. Иначе — задержка.
    is_instant = replied_once.get(chat_id, False)
    
    if is_instant:
        await message.answer(ai_reply)
    else:
        # Ставим флаг, что первый ответ пошел (в процессе ожидания)
        replied_once[chat_id] = True
        task = asyncio.create_task(delayed_send(message, ai_reply, u['delay_sec']))
        active_waits[chat_id] = task

    # Обновляем БД
    if count == 0:
        await conn.execute("INSERT INTO chat_limits (chat_id, msg_count) VALUES ($1, 1)", chat_id)
    else:
        await conn.execute("UPDATE chat_limits SET msg_count = msg_count + 1 WHERE chat_id = $1", chat_id)
    await conn.close()

async def delayed_send(message, text, delay):
    try:
        await asyncio.sleep(delay)
        await message.answer(text)
    except asyncio.CancelledError:
        pass
    finally:
        active_waits.pop(message.chat.id, None)

# --- МЕНЮ УПРАВЛЕНИЯ ---

@dp.message(Command("start"), F.from_user.id == MY_ID)
async def start_cmd(message: types.Message):
    conn = await get_conn()
    u = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", MY_ID)
    if not u:
        await conn.execute("INSERT INTO users (user_id) VALUES ($1)", MY_ID)
        u = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", MY_ID)
    await conn.close()
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧠 Обучение", callback_data="edit_instr")],
        [InlineKeyboardButton(text=f"📊 Лимит: {u['max_limit']}", callback_data="set_limit")],
        [InlineKeyboardButton(text=f"⏱ КД: {u['delay_sec']}с", callback_data="set_delay")],
        [InlineKeyboardButton(text="🧹 Сброс лимитов", callback_data="clear")],
        [InlineKeyboardButton(text="Toggle: " + ("ON" if u['is_active'] else "OFF"), callback_data="switch")]
    ])
    
    await message.answer(f"🤖 **Настройки Gemini 3 Flash**\n\nИнструкция:\n_{u['ai_instructions']}_", reply_markup=kb)

@dp.callback_query(F.data == "edit_instr")
async def cb_instr(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.answer("Как мне себя вести? Напиши подробно:")
    await state.set_state(Form.waiting_for_instructions)

@dp.message(Form.waiting_for_instructions)
async def save_instr(message: types.Message, state: FSMContext):
    conn = await get_conn()
    await conn.execute("UPDATE users SET ai_instructions = $1 WHERE user_id = $2", message.text, MY_ID)
    await conn.close()
    await state.clear()
    await start_cmd(message)

@dp.callback_query(F.data == "set_limit")
async def cb_limit(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.answer("Новый лимит сообщений на человека:")
    await state.set_state(Form.waiting_for_limit)

@dp.message(Form.waiting_for_limit)
async def save_limit(message: types.Message, state: FSMContext):
    if message.text.isdigit():
        conn = await get_conn()
        await conn.execute("UPDATE users SET max_limit = $1 WHERE user_id = $2", int(message.text), MY_ID)
        await conn.close()
    await state.clear()
    await start_cmd(message)

@dp.callback_query(F.data == "switch")
async def toggle(cb: types.CallbackQuery):
    conn = await get_conn()
    await conn.execute("UPDATE users SET is_active = NOT is_active WHERE user_id = $1", MY_ID)
    await conn.close()
    await start_cmd(cb.message)
    await cb.message.delete()

@dp.callback_query(F.data == "clear")
async def clear_data(cb: types.CallbackQuery):
    conn = await get_conn()
    await conn.execute("DELETE FROM chat_limits")
    await conn.close()
    replied_once.clear()
    await cb.answer("Все лимиты и флаги ответов сброшены!")

@dp.callback_query(F.data == "set_delay")
async def cb_delay(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.answer("Задержка (сек):")
    await state.set_state(Form.waiting_for_delay)

@dp.message(Form.waiting_for_delay)
async def save_delay(message: types.Message, state: FSMContext):
    if message.text.isdigit():
        conn = await get_conn()
        await conn.execute("UPDATE users SET delay_sec = $1 WHERE user_id = $2", int(message.text), MY_ID)
        await conn.close()
    await state.clear()
    await start_cmd(message)

# --- ИНИЦИАЛИЗАЦИЯ ---

async def init_db():
    conn = await get_conn()
    await conn.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY, 
        is_active BOOLEAN DEFAULT TRUE, 
        delay_sec INTEGER DEFAULT 30,
        max_limit INTEGER DEFAULT 10,
        ai_instructions TEXT DEFAULT 'Be a helpful assistant.'
    )''')
    await conn.execute('CREATE TABLE IF NOT EXISTS chat_limits (chat_id BIGINT PRIMARY KEY, msg_count INTEGER DEFAULT 0)')
    # Проверка структуры (для существующих БД)
    cols = await conn.fetch("SELECT column_name FROM information_schema.columns WHERE table_name = 'users'")
    col_names = [c['column_name'] for c in cols]
    if 'max_limit' not in col_names: await conn.execute("ALTER TABLE users ADD COLUMN max_limit INTEGER DEFAULT 10")
    if 'ai_instructions' not in col_names: await conn.execute("ALTER TABLE users ADD COLUMN ai_instructions TEXT DEFAULT 'Assistant'")
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
async def root(): return {"status": "AI Live"}
