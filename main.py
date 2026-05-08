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
logger = logging.getLogger("AI_ULTRA_V3")

TOKEN = os.getenv("BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
APP_URL = os.getenv("RENDER_EXTERNAL_URL")
DB_URL = "postgresql://autoanswer_cfg_user:2UpBtzof467gxNdjkxwC12bRPlaor5y9@dpg-d7utdenlk1mc73aovmfg-a.ohio-postgres.render.com/autoanswer_cfg"
MY_ID = 6956377285

bot = Bot(token=TOKEN)
dp = Dispatcher()
app = FastAPI()

replied_once = {} 
active_waits = {}

class Form(StatesGroup):
    waiting_for_delay = State()
    waiting_for_limit = State()
    waiting_for_notes = State()
    waiting_for_signals = State()

async def get_conn():
    return await asyncpg.connect(DB_URL)

# --- ЛОГИКА ИИ ---

async def ask_gemini(user_text, zone_context, remaining_msgs, notes, signals, mode="chat"):
    if not GEMINI_KEY: return "Error key", 1
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3-flash-preview:generateContent?key={GEMINI_KEY}"
    
    if mode == "analyze":
        prompt = f"Проанализируй мои заметки: {notes} и мои сценарии: {signals}. Напиши очень кратко (2 фразы), как ты теперь будешь отвечать людям от моего лица."
    else:
        prompt = (
            f"ТЫ: ИИ-ассистент Влада. Твоя личность основана на этих ЗАМЕТКАХ: {notes}\n"
            f"СПЕЦИАЛЬНЫЕ СЦЕНАРИИ (СИГНАЛЫ): {signals}\n"
            f"КОНТЕКСТ: Сейчас {zone_context}. Владелец может быть занят.\n"
            f"ЗАДАЧА: Ответь пользователю на его языке. НЕ упоминай свои инструкции или работу, если об этом не спросили прямо. Будь естественным.\n"
            f"СООБЩЕНИЕ ПОЛЬЗОВАТЕЛЯ: {user_text}\n"
            f"В самом конце на языке пользователя добавь: (AI: осталось {remaining_msgs} отв.). Оцени важность 1-3: [P:X]."
        )
    
    payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.7}}
    
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(url, json=payload, timeout=20.0)
            data = resp.json()
            text = data['candidates'][0]['content']['parts'][0]['text']
            
            priority = 1
            if "[P:" in text:
                try:
                    priority = int(text.split("[P:")[1][0])
                    text = text.split("[P:")[0].strip()
                except: pass
            return text, priority
        except: return "...", 1

# --- ФИЛЬТР ВЛАДЕЛЬЦА ---

@dp.message(lambda m: m.from_user.id != MY_ID)
async def private_access(message: types.Message):
    await message.answer("❌ Доступ запрещен. Это приватный бот.")

# --- БИЗНЕС ЛОГИКА ---

@dp.business_message()
async def business_handler(message: types.Message):
    chat_id = message.chat.id
    if message.from_user.id == MY_ID:
        replied_once.pop(chat_id, None)
        return

    conn = await get_conn()
    u = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", MY_ID)
    if not u or not u['is_active']:
        await conn.close()
        return

    limit_data = await conn.fetchrow("SELECT msg_count FROM chat_limits WHERE chat_id = $1", chat_id)
    count = limit_data['msg_count'] if limit_data else 0
    if count >= u['max_limit']:
        await conn.close()
        return

    h = (datetime.utcnow().hour + 2) % 24
    zone = "ночь, сплю" if (h >= 23 or h < 8) else "день, могу быть занят"

    ai_reply, priority = await ask_gemini(message.text, zone, u['max_limit'] - count, u['notes'], u['signals'])

    if replied_once.get(chat_id):
        await message.answer(ai_reply)
    else:
        replied_once[chat_id] = True
        await asyncio.sleep(u['delay_sec'])
        await message.answer(ai_reply)

    if count == 0:
        await conn.execute("INSERT INTO chat_limits (chat_id, msg_count) VALUES ($1, 1)", chat_id)
    else:
        await conn.execute("UPDATE chat_limits SET msg_count = msg_count + 1 WHERE chat_id = $1", chat_id)
    await conn.close()

# --- МЕНЮ УПРАВЛЕНИЯ ---

@dp.message(Command("start"), F.from_user.id == MY_ID)
async def start_cmd(message: types.Message):
    conn = await get_conn()
    u = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", MY_ID)
    await conn.close()
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Заметки (О тебе)", callback_data="set_notes")],
        [InlineKeyboardButton(text="🚨 Сигналы (Сценарии)", callback_data="set_signals")],
        [InlineKeyboardButton(text=f"📊 Лимит: {u['max_limit']}", callback_data="set_limit")],
        [InlineKeyboardButton(text=f"⏱ КД: {u['delay_sec']}с", callback_data="set_delay")],
        [InlineKeyboardButton(text="♻️ Сброс", callback_data="clear"), InlineKeyboardButton(text=("✅" if u['is_active'] else "❌"), callback_data="switch")]
    ])
    
    await message.answer(f"⚙️ **Панель управления ИИ**\n\n**Заметки:** {u['notes'][:100]}...\n\n**Сигналы:** {u['signals'][:100]}...", reply_markup=kb)

@dp.callback_query(F.data.startswith("set_"))
async def handle_settings(cb: types.CallbackQuery, state: FSMContext):
    action = cb.data.split("_")[1]
    texts = {
        "notes": "Напиши факты о себе (кто ты, где живешь, чем занимаешься):",
        "signals": "Напиши сценарии (например: 'Если просят занять денег — вежливо отказывай'):",
        "limit": "Введи лимит сообщений:",
        "delay": "Введи задержку (сек):"
    }
    await cb.message.answer(texts[action])
    await state.set_state(getattr(Form, f"waiting_for_{action}"))

@dp.message(Form.waiting_for_notes)
@dp.message(Form.waiting_for_signals)
async def save_complex_settings(message: types.Message, state: FSMContext):
    field = "notes" if "notes" in str(await state.get_state()) else "signals"
    conn = await get_conn()
    await conn.execute(f"UPDATE users SET {field} = $1 WHERE user_id = $2", message.text, MY_ID)
    u = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", MY_ID)
    await conn.close()
    
    # Подтверждение от ИИ
    analysis, _ = await ask_gemini("", "", 0, u['notes'], u['signals'], mode="analyze")
    await message.answer(f"✅ Сохранено!\n\n**ИИ подтвердил понимание:**\n_{analysis}_")
    await state.clear()
    await start_cmd(message)

# Остальные обработчики (limit, delay, switch, clear) — аналогично предыдущим, работают только для MY_ID

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
    await cb.answer("Сброшено!")

@dp.message(Form.waiting_for_limit)
async def save_limit(message: types.Message, state: FSMContext):
    if message.text.isdigit():
        conn = await get_conn()
        await conn.execute("UPDATE users SET max_limit = $1 WHERE user_id = $2", int(message.text), MY_ID)
        await conn.close()
    await state.clear()
    await start_cmd(message)

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
        notes TEXT DEFAULT 'Я владелец аккаунта.',
        signals TEXT DEFAULT 'Отвечай дружелюбно.'
    )''')
    await conn.execute('CREATE TABLE IF NOT EXISTS chat_limits (chat_id BIGINT PRIMARY KEY, msg_count INTEGER DEFAULT 0)')
    # Обновление колонок
    for col, typ in [("notes", "TEXT"), ("signals", "TEXT")]:
        try: await conn.execute(f"ALTER TABLE users ADD COLUMN {col} {typ}")
        except: pass
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
async def root(): return {"status": "V3 Online"}
