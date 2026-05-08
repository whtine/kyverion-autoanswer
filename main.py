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
logger = logging.getLogger("AI_PRO_BOT")

TOKEN = os.getenv("BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
APP_URL = os.getenv("RENDER_EXTERNAL_URL")
DB_URL = "postgresql://autoanswer_cfg_user:2UpBtzof467gxNdjkxwC12bRPlaor5y9@dpg-d7utdenlk1mc73aovmfg-a.ohio-postgres.render.com/autoanswer_cfg"
MY_ID = 6956377285

bot = Bot(token=TOKEN)
dp = Dispatcher()
app = FastAPI()
active_waits = {}

class Form(StatesGroup):
    waiting_for_delay = State()
    waiting_for_limit = State()
    waiting_for_instructions = State()

async def get_conn():
    return await asyncpg.connect(DB_URL)

# --- ЛОГИКА ИИ ---

async def ask_gemini(user_text, zone_context, remaining_msgs, instructions):
    if not GEMINI_KEY:
        return "Привет! Я сейчас занят.", 1
        
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3-flash-preview:generateContent?key={GEMINI_KEY}"
    headers = {'Content-Type': 'application/json'}
    
    # Собираем промпт с учетом твоих инструкций
    prompt = (
        f"ИНСТРУКЦИЯ ДЛЯ ТЕБЯ: {instructions}\n"
        f"КОНТЕКСТ ВЛАДЕЛЬЦА: Сейчас {zone_context}.\n"
        f"ЗАДАЧА: Ответь коротко и вежливо. В конце добавь: (ИИ. Осталось: {remaining_msgs}). "
        f"Оцени важность от 1 до 3 и припиши [P:X].\n"
        f"СООБЩЕНИЕ ПОЛЬЗОВАТЕЛЯ: {user_text}"
    )
    
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 800}
    }
    
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(url, json=payload, headers=headers, timeout=20.0)
            if resp.status_code != 200: return "Дякую! Скоро відповім.", 1
            
            data = resp.json()
            full_text = data['candidates'][0]['content']['parts'][0]['text']
            
            priority = 1
            if "[P:" in full_text:
                try:
                    priority = int(full_text.split("[P:")[1][0])
                    full_text = full_text.split("[P:")[0].strip()
                except: pass
            return full_text, priority
        except:
            return "На зв'язку!", 1

# --- БИЗНЕС ЛОГИКА ---

@dp.business_message()
async def business_handler(message: types.Message):
    chat_id = message.chat.id
    if message.from_user.id == MY_ID:
        if chat_id in active_waits:
            active_waits[chat_id].cancel()
        return

    conn = await get_conn()
    u = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", MY_ID)
    if not u or not u['is_active']:
        await conn.close()
        return

    limit_data = await conn.fetchrow("SELECT msg_count FROM chat_limits WHERE chat_id = $1", chat_id)
    count = limit_data['msg_count'] if limit_data else 0
    
    # Используем настраиваемый лимит
    if count >= u['max_limit']:
        await conn.close()
        return

    # Чешское время
    h = (datetime.utcnow().hour + 2) % 24
    zone = "обычное время"
    if 8 <= h < 14: zone = "на учебе"
    elif h >= 23 or h < 8: zone = "спит"

    ai_reply, priority = await ask_gemini(message.text, zone, u['max_limit'] - count, u['ai_instructions'])

    # Сохраняем счетчик
    if count == 0:
        await conn.execute("INSERT INTO chat_limits (chat_id, msg_count) VALUES ($1, 1)", chat_id)
    else:
        await conn.execute("UPDATE chat_limits SET msg_count = msg_count + 1 WHERE chat_id = $1", chat_id)
    await conn.close()

    # Задержка и отправка
    await asyncio.sleep(u['delay_sec'])
    await message.answer(ai_reply)
    
    if priority >= 2:
        await bot.send_message(MY_ID, f"🔔 Важное от {message.from_user.full_name}:\n{message.text}")

# --- УПРАВЛЕНИЕ (МЕНЮ) ---

@dp.message(Command("start"), F.from_user.id == MY_ID)
async def start_cmd(message: types.Message):
    conn = await get_conn()
    u = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", MY_ID)
    if not u:
        await conn.execute("INSERT INTO users (user_id) VALUES ($1)", MY_ID)
        u = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", MY_ID)
    await conn.close()
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🤖 Обучение (Инструкции)", callback_data="edit_instr")],
        [InlineKeyboardButton(text=f"🔢 Лимит: {u['max_limit']} сообщ.", callback_data="set_limit")],
        [InlineKeyboardButton(text=f"⏱ КД: {u['delay_sec']}с", callback_data="set_delay")],
        [InlineKeyboardButton(text="🔄 Сбросить лимиты чатов", callback_data="clear")],
        [InlineKeyboardButton(text="💡 Статус: " + ("ВКЛ" if u['is_active'] else "ВЫКЛ"), callback_data="switch")]
    ])
    
    text = (f"⚙️ **Настройки ИИ**\n\n"
            f"📜 **Твоя инструкция:**\n{u['ai_instructions']}\n\n"
            f"Текущая модель: Gemini 3 Flash")
    await message.answer(text, reply_markup=kb)

# --- CALLBACKS ---

@dp.callback_query(F.data == "edit_instr")
async def edit_instr(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.answer("Опиши, как ИИ должен отвечать? (Например: 'Я студент, сейчас в Праге, занимаюсь боксом, отвечай дерзко')")
    await state.set_state(Form.waiting_for_instructions)

@dp.message(Form.waiting_for_instructions)
async def save_instr(message: types.Message, state: FSMContext):
    conn = await get_conn()
    await conn.execute("UPDATE users SET ai_instructions = $1 WHERE user_id = $2", message.text, MY_ID)
    await conn.close()
    await state.clear()
    await start_cmd(message)

@dp.callback_query(F.data == "set_limit")
async def edit_limit(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.answer("Сколько сообщений ИИ может отправить одному человеку?")
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
async def toggle_bot(cb: types.CallbackQuery):
    conn = await get_conn()
    await conn.execute("UPDATE users SET is_active = NOT is_active WHERE user_id = $1", MY_ID)
    await conn.close()
    await start_cmd(cb.message)
    await cb.message.delete()

@dp.callback_query(F.data == "clear")
async def clear_limits(cb: types.CallbackQuery):
    conn = await get_conn()
    await conn.execute("DELETE FROM chat_limits")
    await conn.close()
    await cb.answer("Лимиты всех чатов обнулены!")

@dp.callback_query(F.data == "set_delay")
async def set_delay(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.answer("Задержка перед ответом (сек):")
    await state.set_state(Form.waiting_for_delay)

@dp.message(Form.waiting_for_delay)
async def save_delay(message: types.Message, state: FSMContext):
    if message.text.isdigit():
        conn = await get_conn()
        await conn.execute("UPDATE users SET delay_sec = $1 WHERE user_id = $2", int(message.text), MY_ID)
        await conn.close()
    await state.clear()
    await start_cmd(message)

# --- DB & WEB ---

async def init_db():
    conn = await get_conn()
    await conn.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY, 
        is_active BOOLEAN DEFAULT TRUE, 
        delay_sec INTEGER DEFAULT 30,
        max_limit INTEGER DEFAULT 10,
        ai_instructions TEXT DEFAULT 'Ты вежливый помощник.'
    )''')
    await conn.execute('CREATE TABLE IF NOT EXISTS chat_limits (chat_id BIGINT PRIMARY KEY, msg_count INTEGER DEFAULT 0)')
    
    # Проверка новых колонок (если таблица уже была)
    try: await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS max_limit INTEGER DEFAULT 10")
    except: pass
    try: await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS ai_instructions TEXT DEFAULT 'Ты вежливый помощник.'")
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
async def root(): return {"status": "ok"}
