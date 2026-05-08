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
logger = logging.getLogger("AI_ULTRA_V4")

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
    if not GEMINI_KEY: return "Error key", 1
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3-flash-preview:generateContent?key={GEMINI_KEY}"
    
    notes_str = "\n".join([f"- {n['content']}" for n in notes])
    signals_str = "\n".join([f"- {s['content']}" for s in signals])

    if mode == "analyze":
        prompt = f"Я обновил настройки. Мои заметки:\n{notes_str}\n\nМои сценарии:\n{signals_str}\n\nНапиши коротко, как ты понял свою роль и стиль общения."
    else:
        prompt = (
            f"ТЫ: ИИ-ассистент Влада. \n"
            f"ТВОИ ЗНАНИЯ (используй только если уместно): \n{notes_str}\n\n"
            f"ТВОИ ПРАВИЛА/СЦЕНАРИИ: \n{signals_str}\n\n"
            f"КОНТЕКСТ: Сейчас {zone_context}. Владелец может быть занят.\n"
            f"ЗАДАЧА: Ответь пользователю на его языке. Не упоминай работу или заметки, если тебя не спросили прямо. Будь лаконичным.\n"
            f"СООБЩЕНИЕ: {user_text}\n"
            f"В конце на языке пользователя: (AI: осталось {remaining_msgs} отв.). Оцени важность 1-3: [P:X]."
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

# --- БИЗНЕС ЛОГИКА ---

@dp.business_message()
async def business_handler(message: types.Message):
    # Приватность: бот работает только для владельца
    if message.from_user.id == MY_ID:
        replied_once.pop(message.chat.id, None)
        return

    conn = await get_conn()
    u = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", MY_ID)
    if not u or not u['is_active']:
        await conn.close()
        return

    # Загружаем всё обучение
    notes = await conn.fetch("SELECT content FROM ai_notes WHERE user_id = $1", MY_ID)
    signals = await conn.fetch("SELECT content FROM ai_signals WHERE user_id = $1", MY_ID)

    limit_data = await conn.fetchrow("SELECT msg_count FROM chat_limits WHERE chat_id = $1", message.chat.id)
    count = limit_data['msg_count'] if limit_data else 0
    if count >= u['max_limit']:
        await conn.close()
        return

    h = (datetime.utcnow().hour + 2) % 24
    zone = "ночь" if (h >= 23 or h < 8) else "день"

    ai_reply, priority = await ask_gemini(message.text, zone, u['max_limit'] - count, notes, signals)

    if replied_once.get(message.chat.id):
        await message.answer(ai_reply)
    else:
        replied_once[message.chat.id] = True
        await asyncio.sleep(u['delay_sec'])
        await message.answer(ai_reply)

    if count == 0:
        await conn.execute("INSERT INTO chat_limits (chat_id, msg_count) VALUES ($1, 1)", message.chat.id)
    else:
        await conn.execute("UPDATE chat_limits SET msg_count = msg_count + 1 WHERE chat_id = $1", message.chat.id)
    await conn.close()

# --- ПАНЕЛЬ УПРАВЛЕНИЯ ---

@dp.message(Command("start"), F.from_user.id == MY_ID)
async def start_cmd(message: types.Message):
    conn = await get_conn()
    u = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", MY_ID)
    if not u:
        await conn.execute("INSERT INTO users (user_id) VALUES ($1)", MY_ID)
        u = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", MY_ID)
    await conn.close()
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Заметки (Факты)", callback_data="list_notes"), 
         InlineKeyboardButton(text="🚨 Сигналы (Сценарии)", callback_data="list_signals")],
        [InlineKeyboardButton(text=f"📊 Лимит: {u['max_limit']}", callback_data="set_limit"),
         InlineKeyboardButton(text=f"⏱ КД: {u['delay_sec']}с", callback_data="set_delay")],
        [InlineKeyboardButton(text="🧹 Сброс", callback_data="clear"), 
         InlineKeyboardButton(text=("✅ ВКЛ" if u['is_active'] else "❌ ВЫКЛ"), callback_data="switch")]
    ])
    await message.answer("🛠 **Управление ИИ-Ассистентом**\n\nБот доступен только тебе.", reply_markup=kb)

# Фильтр для всех остальных
@dp.message(lambda m: m.from_user.id != MY_ID)
async def access_denied(message: types.Message):
    await message.answer("🚫 Доступ запрещен.")

# --- УПРАВЛЕНИЕ ЗАМЕТКАМИ И СИГНАЛАМИ ---

@dp.callback_query(F.data == "list_notes")
async def list_notes(cb: types.CallbackQuery):
    conn = await get_conn()
    rows = await conn.fetch("SELECT id, content FROM ai_notes WHERE user_id = $1", MY_ID)
    await conn.close()
    
    text = "📝 **Твои заметки для ИИ:**\n\n" + "\n".join([f"{i+1}. {r['content']} (/del_n_{r['id']})" for i, r in enumerate(rows)])
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ Добавить заметку", callback_data="add_note")]])
    await cb.message.answer(text or "Заметок пока нет.", reply_markup=kb)

@dp.callback_query(F.data == "add_note")
async def add_note_st(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.answer("Введите факт о себе/работе для ИИ:")
    await state.set_state(Form.waiting_for_note)

@dp.message(Form.waiting_for_note)
async def save_note(message: types.Message, state: FSMContext):
    conn = await get_conn()
    await conn.execute("INSERT INTO ai_notes (user_id, content) VALUES ($1, $2)", MY_ID, message.text)
    notes = await conn.fetch("SELECT content FROM ai_notes WHERE user_id = $1", MY_ID)
    signals = await conn.fetch("SELECT content FROM ai_signals WHERE user_id = $1", MY_ID)
    await conn.close()
    
    # ИИ подтверждает понимание
    analysis, _ = await ask_gemini("", "", 0, notes, signals, mode="analyze")
    await message.answer(f"✅ Заметка сохранена!\n\n**ИИ:** {analysis}")
    await state.clear()
    await start_cmd(message)

# Удаление заметок (через команду)
@dp.message(F.text.startswith("/del_n_"))
async def del_note(message: types.Message):
    note_id = int(message.text.split("_")[-1])
    conn = await get_conn()
    await conn.execute("DELETE FROM ai_notes WHERE id = $1 AND user_id = $2", note_id, MY_ID)
    await conn.close()
    await message.answer("🗑 Удалено.")
    await start_cmd(message)

# --- АНАЛОГИЧНО ДЛЯ СИГНАЛОВ (СЦЕНАРИЕВ) ---

@dp.callback_query(F.data == "list_signals")
async def list_signals(cb: types.CallbackQuery):
    conn = await get_conn()
    rows = await conn.fetch("SELECT id, content FROM ai_signals WHERE user_id = $1", MY_ID)
    await conn.close()
    text = "🚨 **Сценарии поведения:**\n\n" + "\n".join([f"{i+1}. {r['content']} (/del_s_{r['id']})" for i, r in enumerate(rows)])
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ Добавить сценарий", callback_data="add_signal")]])
    await cb.message.answer(text or "Сценариев пока нет.", reply_markup=kb)

@dp.callback_query(F.data == "add_signal")
async def add_signal_st(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.answer("Введите правило (например: 'Если просят скидку, отвечай что цена фиксированная'):")
    await state.set_state(Form.waiting_for_signal)

@dp.message(Form.waiting_for_signal)
async def save_signal(message: types.Message, state: FSMContext):
    conn = await get_conn()
    await conn.execute("INSERT INTO ai_signals (user_id, content) VALUES ($1, $2)", MY_ID, message.text)
    await conn.close()
    await state.clear()
    await start_cmd(message)

@dp.message(F.text.startswith("/del_s_"))
async def del_signal(message: types.Message):
    sid = int(message.text.split("_")[-1])
    conn = await get_conn()
    await conn.execute("DELETE FROM ai_signals WHERE id = $1 AND user_id = $2", sid, MY_ID)
    await conn.close()
    await message.answer("🗑 Сценарий удален.")
    await start_cmd(message)

# --- ПРОЧИЕ НАСТРОЙКИ ---

@dp.callback_query(F.data == "switch")
async def toggle_active(cb: types.CallbackQuery):
    conn = await get_conn()
    await conn.execute("UPDATE users SET is_active = NOT is_active WHERE user_id = $1", MY_ID)
    await conn.close()
    await start_cmd(cb.message)

@dp.callback_query(F.data == "clear")
async def clear_limits(cb: types.CallbackQuery):
    conn = await get_conn()
    await conn.execute("DELETE FROM chat_limits")
    await conn.close()
    replied_once.clear()
    await cb.answer("Лимиты сброшены!")

@dp.callback_query(F.data == "set_limit")
async def set_limit_st(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.answer("Введите макс. кол-во ответов одному человеку:")
    await state.set_state(Form.waiting_for_limit)

@dp.message(Form.waiting_for_limit)
async def save_limit(message: types.Message, state: FSMContext):
    if message.text.isdigit():
        conn = await get_conn()
        await conn.execute("UPDATE users SET max_limit = $1 WHERE user_id = $2", int(message.text), MY_ID)
        await conn.close()
    await state.clear()
    await start_cmd(message)

@dp.callback_query(F.data == "set_delay")
async def set_delay_st(cb: types.CallbackQuery, state: FSMContext):
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

# --- ИНИЦИАЛИЗАЦИЯ БД ---

async def init_db():
    conn = await get_conn()
    await conn.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY, 
        is_active BOOLEAN DEFAULT TRUE, 
        delay_sec INTEGER DEFAULT 30,
        max_limit INTEGER DEFAULT 10
    )''')
    await conn.execute('''CREATE TABLE IF NOT EXISTS ai_notes (
        id SERIAL PRIMARY KEY, user_id BIGINT, content TEXT
    )''')
    await conn.execute('''CREATE TABLE IF NOT EXISTS ai_signals (
        id SERIAL PRIMARY KEY, user_id BIGINT, content TEXT
    )''')
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
async def root(): return {"status": "V4 Active"}
