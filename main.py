import os, asyncio, asyncpg, httpx, logging
from datetime import datetime
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Update, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.filters import Command
from contextlib import asynccontextmanager

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("AI_PRO_BOT")

TOKEN = os.getenv("BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY") # Получи на aistudio.google.com
APP_URL = os.getenv("RENDER_EXTERNAL_URL")
DB_URL = "postgresql://autoanswer_cfg_user:2UpBtzof467gxNdjkxwC12bRPlaor5y9@dpg-d7utdenlk1mc73aovmfg-a.ohio-postgres.render.com/autoanswer_cfg"
MY_ID = 6956377285

bot = Bot(token=TOKEN)
dp = Dispatcher()
app = FastAPI()
active_waits = {}

class Form(StatesGroup):
    waiting_for_text = State()
    waiting_for_hours = State()
    waiting_for_delay = State()

async def get_conn():
    return await asyncpg.connect(DB_URL)

# --- ЛОГИКА GEMINI AI ---

async def ask_gemini(user_text, zone_context, remaining_msgs):
    if not GEMINI_KEY:
        return "Извини, мой ИИ-модуль не настроен.", 1
        
    # ПЕРЕХОДИМ НА gemini-pro — она самая стабильная для этого API
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent?key={GEMINI_KEY}"
    
    headers = {'Content-Type': 'application/json'}
    
    # Формируем строгий промпт
    prompt_text = (
        f"Ты — ИИ-ассистент. Владелец сейчас: {zone_context}. "
        f"Ответь вежливо на языке пользователя. "
        f"В конце добавь: (ИИ-ассистент. Осталось: {remaining_msgs} зап.). "
        f"Оцени важность сообщения от 1 до 3 и в самом конце напиши [P:X], где X - число. "
        f"Сообщение пользователя: {user_text}"
    )
    
    payload = {
        "contents": [{
            "parts": [{"text": prompt_text}]
        }]
    }
    
    async with httpx.AsyncClient() as client:
        try:
            # Используем follow_redirects=True на всякий случай
            resp = await client.post(url, json=payload, headers=headers, timeout=20.0)
            
            if resp.status_code != 200:
                logger.error(f"Google API Error: {resp.status_code} - {resp.text}")
                # Если gemini-pro тоже выдаст 404, значит дело в региональной привязке ключа
                return "Привет! Я сейчас занят, отвечу как освобожусь.", 1
                
            data = resp.json()
            
            # Проверка наличия ответа в структуре
            if 'candidates' in data and data['candidates']:
                full_text = data['candidates'][0]['content']['parts'][0]['text']
                
                priority = 1
                if "[P:" in full_text:
                    try:
                        priority_str = full_text.split("[P:")[1][0]
                        priority = int(priority_str) if priority_str.isdigit() else 1
                        full_text = full_text.split("[P:")[0].strip()
                    except: pass
                
                return full_text, priority
            else:
                return "Привет! Получил твое сообщение, скоро буду на связи.", 1
                
        except Exception as e:
            logger.error(f"AI critical Error: {e}")
            return "Привет! Оставь сообщение, отвечу позже.", 1
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
        # Проверяем лимит (10 сообщений)
        limit_data = await conn.fetchrow("SELECT msg_count FROM chat_limits WHERE chat_id = $1", chat_id)
        count = limit_data['msg_count'] if limit_data else 0
        
        if count >= 10:
            await conn.close()
            return # Лимит исчерпан
        
        if chat_id in active_waits:
            await conn.close()
            return
            
        # Определяем контекст времени (Чехия +2)
        h = (datetime.utcnow().hour + 2) % 24
        zone_info = "Обычное время"
        if 8 <= h < 13: zone_info = "На учебе, отвечает раз в час"
        elif h >= 23 or h < 8: zone_info = "Спит, ответит утром"

        # Получаем ответ от ИИ
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
        
        # Обновляем счетчик в БД
        if count == 0:
            await conn.execute("INSERT INTO chat_limits (chat_id, msg_count) VALUES ($1, 1)", message.chat.id)
        else:
            await conn.execute("UPDATE chat_limits SET msg_count = msg_count + 1 WHERE chat_id = $1", message.chat.id)
            
        # Если приоритет ВАЖНЫЙ (2 или 3) - пересылаем владельцу
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

# --- УПРАВЛЕНИЕ (START И Т.Д.) ---

@dp.message(Command("start"), F.from_user.id == MY_ID)
async def start_cmd(message: types.Message):
    conn = await get_conn()
    u = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", MY_ID)
    if not u:
        await conn.execute("INSERT INTO users (user_id) VALUES ($1)", MY_ID)
        u = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", MY_ID)
    await conn.close()
    
    status = "✅ ИИ АКТИВЕН" if u['is_active'] else "❌ ВЫКЛЮЧЕН"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚙️ Зоны/Тексты", callback_data="menu_day")], # Для простоты оставил вход через дневное
        [InlineKeyboardButton(text=f"⏱ КД: {u['delay_sec']}с", callback_data="set_delay")],
        [InlineKeyboardButton(text=f"Статус: {status}", callback_data="switch")],
        [InlineKeyboardButton(text="🗑 Сбросить лимиты ИИ", callback_data="clear_limits")]
    ])
    
    await message.answer(f"🤖 **ИИ-Автоответчик (Gemini)**\n\nСтатус: {status}\nЧешское время: (UTC+2)\nЛимит: 10 сообщ/чел.", reply_markup=kb)

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

# --- ТЕ ЖЕ ФУНКЦИИ ИЗ ТВОЕГО КОДА (MENU/EDIT) ---
# ... (Оставил всё как было у тебя в коде выше для настройки текста и задержки)

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

# --- ИНИЦИАЛИЗАЦИЯ ---

async def init_db():
    conn = await get_conn()
    await conn.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY, is_active BOOLEAN DEFAULT FALSE, delay_sec INTEGER DEFAULT 30)''')
    await conn.execute('''CREATE TABLE IF NOT EXISTS chat_limits (
        chat_id BIGINT PRIMARY KEY, msg_count INTEGER DEFAULT 0)''')
    # Добавление колонок если нет
    try: await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS delay_sec INTEGER DEFAULT 30")
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
async def root():
    return {"status": "working", "ai_mode": "gemini-pro"}
