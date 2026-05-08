import os, asyncio, asyncpg, httpx, logging
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
logger = logging.getLogger("AI_PRO_V4.5")

# Конфиг
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
    waiting_for_note = State()
    waiting_for_signal = State()
    waiting_for_limit = State()
    waiting_for_delay = State()

async def get_conn():
    return await asyncpg.connect(DB_URL)

# --- ЛОГИКА ИИ ---

async def ask_gemini(user_text, zone_context, remaining_msgs, notes, signals, mode="chat"):
    if not GEMINI_KEY: return "Ошибка: отсутствует API ключ.", 1
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3-flash-preview:generateContent?key={GEMINI_KEY}"
    
    # Формируем базу знаний из БД
    knowledge = "\n".join([f"- {n['content']}" for n in notes]) if notes else "Нет данных."
    rules = "\n".join([f"- {s['content']}" for s in signals]) if signals else "Просто будь вежлив."

    if mode == "analyze":
        prompt = f"Я обновил данные обучения.\nЗАМЕТКИ:\n{knowledge}\nСЦЕНАРИИ:\n{rules}\nКратко подтверди, как ты это понял."
    else:
        prompt = (
            f"ТЫ: ИИ-ассистент Влада. Твой характер: спокойный, умный.\n"
            f"ТВОЙ БЭКГРАУНД (используй только если уместно): \n{knowledge}\n\n"
            f"ИНСТРУКЦИИ И СЦЕНАРИИ: \n{rules}\n\n"
            f"Если текущее сообщение не подходит под конкретный сценарий, отвечай просто от себя, учитывая время ({zone_context}).\n"
            f"Никогда не говори 'согласно моим инструкциям'. Будь живым человеком.\n"
            f"СООБЩЕНИЕ ПОЛЬЗОВАТЕЛЯ: {user_text}\n"
            f"В конце на языке пользователя добавь: (ИИ: {remaining_msgs}). Оцени важность 1-3 в формате [P:X]."
        )
    
    payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.8}}
    
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(url, json=payload, timeout=25.0)
            
            if resp.status_code == 429:
                return "⚠️ Лимит. Подождите минуту.", 1
            
            if resp.status_code != 200:
                logger.error(f"API Error: {resp.text}")
                return "Я немного задумался, отвечу чуть позже.", 1

            data = resp.json()
            text = data['candidates'][0]['content']['parts'][0]['text']
            
            priority = 1
            if "[P:" in text:
                try:
                    priority = int(text.split("[P:")[1][0])
                    text = text.split("[P:")[0].strip()
                except: pass
            return text, priority
        except Exception as e:
            logger.error(f"AI Error: {e}")
            return "...", 1

# --- ОБРАБОТКА ---

@dp.business_message()
async def business_handler(message: types.Message):
    chat_id = message.chat.id
    
    # Игнорим всех, кроме владельца в управлении, но в бизнесе отвечаем другим
    if message.from_user.id == MY_ID:
        if chat_id in active_waits:
            active_waits[chat_id].cancel()
        replied_once.pop(chat_id, None)
        return

    conn = await get_conn()
    u = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", MY_ID)
    if not u or not u['is_active']:
        await conn.close()
        return

    # Проверка лимитов для конкретного чата
    limit_data = await conn.fetchrow("SELECT msg_count FROM chat_limits WHERE chat_id = $1", chat_id)
    count = limit_data['msg_count'] if limit_data else 0
    if count >= u['max_limit']:
        await conn.close()
        return

    # Загружаем обучение
    notes = await conn.fetch("SELECT content FROM ai_notes WHERE user_id = $1", MY_ID)
    signals = await conn.fetch("SELECT content FROM ai_signals WHERE user_id = $1", MY_ID)

    h = (datetime.utcnow().hour + 2) % 24
    zone = "ночное время" if (h >= 23 or h < 8) else "рабочее время"

    ai_reply, priority = await ask_gemini(message.text, zone, u['max_limit'] - count, notes, signals)

    # Логика мгновенного ответа
    if replied_once.get(chat_id):
        await message.answer(ai_reply)
    else:
        replied_once[chat_id] = True
        # Если есть задержка — ждем
        if u['delay_sec'] > 0:
            task = asyncio.create_task(delayed_reply(message, ai_reply, u['delay_sec']))
            active_waits[chat_id] = task
        else:
            await message.answer(ai_reply)

    # Обновляем счетчик
    if count == 0:
        await conn.execute("INSERT INTO chat_limits (chat_id, msg_count) VALUES ($1, 1)", chat_id)
    else:
        await conn.execute("UPDATE chat_limits SET msg_count = msg_count + 1 WHERE chat_id = $1", chat_id)
    await conn.close()

async def delayed_reply(message, text, delay):
    try:
        await asyncio.sleep(delay)
        await message.answer(text)
    except: pass
    finally: active_waits.pop(message.chat.id, None)

# --- ПАНЕЛЬ (ТОЛЬКО ДЛЯ ТЕБЯ) ---

@dp.message(Command("start"), F.from_user.id == MY_ID)
async def start_cmd(message: types.Message):
    conn = await get_conn()
    u = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", MY_ID)
    if not u:
        await conn.execute("INSERT INTO users (user_id) VALUES ($1)", MY_ID)
        u = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", MY_ID)
    await conn.close()
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Заметки", callback_data="list_notes"), 
         InlineKeyboardButton(text="🚨 Сценарии", callback_data="list_signals")],
        [InlineKeyboardButton(text=f"📊 Лимит: {u['max_limit']}", callback_data="set_limit"),
         InlineKeyboardButton(text=f"⏱ КД: {u['delay_sec']}с", callback_data="set_delay")],
        [InlineKeyboardButton(text="🧹 Сброс", callback_data="clear"), 
         InlineKeyboardButton(text=("✅ ВКЛ" if u['is_active'] else "❌ ВЫКЛ"), callback_data="switch")]
    ])
    await message.answer("🦾 **AI Boss Panel**\nНастрой свои знания и сценарии ниже:", reply_markup=kb)

@dp.message(lambda m: m.from_user.id != MY_ID)
async def access_denied(message: types.Message):
    await message.answer("❌ Доступ закрыт.")

# --- УПРАВЛЕНИЕ ЗАМЕТКАМИ/СЦЕНАРИЯМИ ---

@dp.callback_query(F.data == "list_notes", F.from_user.id == MY_ID)
async def list_notes(cb: types.CallbackQuery):
    conn = await get_conn()
    rows = await conn.fetch("SELECT id, content FROM ai_notes WHERE user_id = $1", MY_ID)
    await conn.close()
    text = "📝 **Твои заметки:**\n\n" + "\n".join([f"• {r['content']} [/del_n_{r['id']}]" for r in rows])
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ Добавить", callback_data="add_note")]])
    await cb.message.answer(text if rows else "Заметок нет.", reply_markup=kb)

@dp.callback_query(F.data == "add_note")
async def add_n_st(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.answer("Отправь факт о себе:")
    await state.set_state(Form.waiting_for_note)

@dp.message(Form.waiting_for_note, F.from_user.id == MY_ID)
async def save_n(message: types.Message, state: FSMContext):
    conn = await get_conn()
    await conn.execute("INSERT INTO ai_notes (user_id, content) VALUES ($1, $2)", MY_ID, message.text)
    notes = await conn.fetch("SELECT content FROM ai_notes WHERE user_id = $1", MY_ID)
    signals = await conn.fetch("SELECT content FROM ai_signals WHERE user_id = $1", MY_ID)
    await conn.close()
    
    res, _ = await ask_gemini("", "", 0, notes, signals, mode="analyze")
    await message.answer(f"✅ Сохранено. ИИ: {res}")
    await state.clear()
    await start_cmd(message)

@dp.callback_query(F.data == "list_signals", F.from_user.id == MY_ID)
async def list_signals(cb: types.CallbackQuery):
    conn = await get_conn()
    rows = await conn.fetch("SELECT id, content FROM ai_signals WHERE user_id = $1", MY_ID)
    await conn.close()
    text = "🚨 **Сценарии:**\n\n" + "\n".join([f"• {r['content']} [/del_s_{r['id']}]" for r in rows])
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ Добавить", callback_data="add_signal")]])
    await cb.message.answer(text if rows else "Сценариев нет.", reply_markup=kb)

@dp.callback_query(F.data == "add_signal")
async def add_s_st(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.answer("Отправь правило (Если... то...):")
    await state.set_state(Form.waiting_for_signal)

@dp.message(Form.waiting_for_signal, F.from_user.id == MY_ID)
async def save_s(message: types.Message, state: FSMContext):
    conn = await get_conn()
    await conn.execute("INSERT INTO ai_signals (user_id, content) VALUES ($1, $2)", MY_ID, message.text)
    await conn.close()
    await state.clear()
    await start_cmd(message)

@dp.message(F.text.startswith("/del_"), F.from_user.id == MY_ID)
async def del_item(message: types.Message):
    table = "ai_notes" if "_n_" in message.text else "ai_signals"
    item_id = int(message.text.split("_")[-1])
    conn = await get_conn()
    await conn.execute(f"DELETE FROM {table} WHERE id = $1 AND user_id = $2", item_id, MY_ID)
    await conn.close()
    await message.answer("🗑 Удалено.")
    await start_cmd(message)

# --- SWITCHERS ---

@dp.callback_query(F.data == "switch")
async def toggle(cb: types.CallbackQuery):
    conn = await get_conn()
    await conn.execute("UPDATE users SET is_active = NOT is_active WHERE user_id = $1", MY_ID)
    await conn.close()
    await start_cmd(cb.message)

@dp.callback_query(F.data == "clear")
async def clear(cb: types.CallbackQuery):
    conn = await get_conn()
    await conn.execute("DELETE FROM chat_limits")
    await conn.close()
    replied_once.clear()
    await cb.answer("Лимиты очищены")

@dp.callback_query(F.data == "set_limit")
async def st_lim(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.answer("Новый лимит:")
    await state.set_state(Form.waiting_for_limit)

@dp.message(Form.waiting_for_limit)
async def sv_lim(message: types.Message, state: FSMContext):
    if message.text.isdigit():
        conn = await get_conn()
        await conn.execute("UPDATE users SET max_limit = $1 WHERE user_id = $2", int(message.text), MY_ID)
        await conn.close()
    await state.clear()
    await start_cmd(message)

@dp.callback_query(F.data == "set_delay")
async def st_del(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.answer("Задержка (сек):")
    await state.set_state(Form.waiting_for_delay)

@dp.message(Form.waiting_for_delay)
async def sv_del(message: types.Message, state: FSMContext):
    if message.text.isdigit():
        conn = await get_conn()
        await conn.execute("UPDATE users SET delay_sec = $1 WHERE user_id = $2", int(message.text), MY_ID)
        await conn.close()
    await state.clear()
    await start_cmd(message)

# --- INIT ---

async def init_db():
    conn = await get_conn()
    await conn.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY, is_active BOOLEAN DEFAULT TRUE, 
        delay_sec INTEGER DEFAULT 30, max_limit INTEGER DEFAULT 10)''')
    await conn.execute('CREATE TABLE IF NOT EXISTS ai_notes (id SERIAL PRIMARY KEY, user_id BIGINT, content TEXT)')
    await conn.execute('CREATE TABLE IF NOT EXISTS ai_signals (id SERIAL PRIMARY KEY, user_id BIGINT, content TEXT)')
    await conn.execute('CREATE TABLE IF NOT EXISTS chat_limits (chat_id BIGINT PRIMARY KEY, msg_count INTEGER DEFAULT 0)')
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
async def root(): return {"status": "V4.5 Live"}
