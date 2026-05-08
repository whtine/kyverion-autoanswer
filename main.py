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
logger = logging.getLogger("PRO_AUTO_BOT")

TOKEN = os.getenv("BOT_TOKEN")
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

async def add_log(event_type, details):
    try:
        conn = await get_conn()
        await conn.execute("INSERT INTO logs (event_time, event_type, details) VALUES ($1, $2, $3)", 
                           datetime.now(), event_type, str(details))
        await conn.close()
    except: pass

# --- ВСПОМОГАТЕЛЬНАЯ ЛОГИКА ---

def is_hour_in_range(range_str, h):
    try:
        start, end = map(int, range_str.split("-"))
        if start == 0 and end == 0: return False
        if start < end: return start <= h < end
        return h >= start or h < end # Переход через полночь (например, 22-7)
    except: return False

# --- БИЗНЕС ЛОГИКА ---

@dp.business_message()
async def business_handler(message: types.Message):
    chat_id = message.chat.id
    if message.from_user.id == MY_ID:
        if chat_id in active_waits:
            active_waits[chat_id].cancel()
            active_waits.pop(chat_id, None)
            await add_log("CANCEL", f"Ты ответил сам в чат {chat_id}")
        return

    conn = await get_conn()
    u = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", MY_ID)
    await conn.close()

    if u and u['is_active']:
        if chat_id in active_waits: return
        
        h = (datetime.utcnow().hour + 2) % 24
        # Проверяем зоны по приоритету: Ночь -> Утро -> День
        if is_hour_in_range(u.get('range_night', '0-0'), h):
            reply_text, zone = u.get('text_night'), "НОЧЬ"
        elif is_hour_in_range(u.get('range_morning', '0-0'), h):
            reply_text, zone = u.get('text_morning'), "УТРО"
        else:
            reply_text, zone = u.get('reply_text'), "ДЕНЬ"

        if not reply_text: reply_text = u['reply_text'] # Фолбэк на основной

        delay = u.get('delay_sec', 30)
        await add_log("RECEIVE", f"Входное ({zone}). Ждем {delay}с.")
        task = asyncio.create_task(delayed_reply(message, reply_text, delay))
        active_waits[chat_id] = task

async def delayed_reply(message, text, delay):
    try:
        await asyncio.sleep(delay)
        await message.answer(text)
        await add_log("SENT", f"Ответил: {text[:20]}...")
    except asyncio.CancelledError: pass
    finally: active_waits.pop(message.chat.id, None)

# --- МЕНЮ ---

@dp.message(Command("start"), F.from_user.id == MY_ID)
async def start_cmd(message: types.Message):
    conn = await get_conn()
    u = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", MY_ID)
    if not u:
        await conn.execute("INSERT INTO users (user_id) VALUES ($1)", MY_ID)
        u = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", MY_ID)
    await conn.close()
    
    status = "✅ ВКЛ" if u['is_active'] else "❌ ВЫКЛ"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌅 Утро", callback_data="menu_morning"),
         InlineKeyboardButton(text="☀️ День", callback_data="menu_day"),
         InlineKeyboardButton(text="🌃 Ночь", callback_data="menu_night")],
        [InlineKeyboardButton(text=f"⏱ КД: {u['delay_sec']}с", callback_data="set_delay")],
        [InlineKeyboardButton(text=f"Статус: {status}", callback_data="switch")],
        [InlineKeyboardButton(text="📊 Логи", callback_data="get_logs")]
    ])
    
    text = (f"⚙️ **Настройки бота (UTC)**\n\n"
            f"🌅 **Утро ({u['range_morning']}):** `{u['text_morning']}`\n"
            f"☀️ **День (Остальное):** `{u['reply_text']}`\n"
            f"🌃 **Ночь ({u['range_night']}):** `{u['text_night']}`\n\n"
            f"КД: {u['delay_sec']} секунд")
    
    await message.answer(text, reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("menu_"))
async def zone_menu(callback: types.CallbackQuery):
    zone = callback.data.split("_")[1]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Изменить текст", callback_data=f"edit_text_{zone}")],
        [InlineKeyboardButton(text="⏰ Изменить часы", callback_data=f"edit_hours_{zone}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]
    ])
    await callback.message.edit_text(f"Настройка зоны: **{zone.upper()}**", reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data == "back")
async def back_to_start(callback: types.CallbackQuery):
    await start_cmd(callback.message)
    await callback.message.delete()

# --- ФУНКЦИИ РЕДАКТИРОВАНИЯ ---

@dp.callback_query(F.data.startswith("edit_"))
async def edit_router(callback: types.CallbackQuery, state: FSMContext):
    _, target, zone = callback.data.split("_")
    await state.update_data(target=target, zone=zone)
    if target == "text":
        await callback.message.answer(f"Введите текст для {zone.upper()}:")
        await state.set_state(Form.waiting_for_text)
    else:
        await callback.message.answer(f"Введите часы (напр. `23-7`):")
        await state.set_state(Form.waiting_for_hours)

@dp.message(Form.waiting_for_text)
async def save_text(message: types.Message, state: FSMContext):
    data = await state.get_data()
    zone, col = data['zone'], "reply_text" if data['zone'] == "day" else f"text_{data['zone']}"
    conn = await get_conn()
    await conn.execute(f"UPDATE users SET {col} = $1 WHERE user_id = $2", message.text, MY_ID)
    await conn.close()
    await state.clear()
    await start_cmd(message)

@dp.message(Form.waiting_for_hours)
async def save_hours(message: types.Message, state: FSMContext):
    data = await state.get_data()
    if data['zone'] == "day": return
    conn = await get_conn()
    await conn.execute(f"UPDATE users SET range_{data['zone']} = $1 WHERE user_id = $2", message.text.replace(" ",""), MY_ID)
    await conn.close()
    await state.clear()
    await start_cmd(message)

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

@dp.callback_query(F.data == "switch")
async def toggle(callback: types.CallbackQuery):
    conn = await get_conn()
    await conn.execute("UPDATE users SET is_active = NOT is_active WHERE user_id = $1", MY_ID)
    await conn.close()
    await start_cmd(callback.message)
    await callback.message.delete()

@dp.callback_query(F.data == "get_logs")
async def get_logs_btn(callback: types.CallbackQuery):
    conn = await get_conn()
    rows = await conn.fetch("SELECT * FROM logs ORDER BY event_time DESC LIMIT 40")
    await conn.close()
    res = "LOGS:\n" + "\n".join([f"[{r['event_time'].strftime('%H:%M')}] {r['event_type']}: {r['details']}" for r in rows])
    await callback.message.answer_document(BufferedInputFile(res.encode(), filename="logs.txt"))

# --- ИНИЦИАЛИЗАЦИЯ И ЗАПУСК ---

async def init_db():
    conn = await get_conn()
    await conn.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY, 
        reply_text TEXT DEFAULT 'Занят', 
        is_active BOOLEAN DEFAULT FALSE,
        delay_sec INTEGER DEFAULT 30,
        text_morning TEXT DEFAULT 'Доброе утро',
        range_morning TEXT DEFAULT '0-0',
        text_night TEXT DEFAULT 'Сплю',
        range_night TEXT DEFAULT '0-0'
    )''')
    for c, t in [("delay_sec","INT DEFAULT 30"), ("text_morning","TEXT"), ("range_morning","TEXT DEFAULT '0-0'"), ("text_night","TEXT"), ("range_night","TEXT DEFAULT '0-0'")]:
        try: await conn.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {c} {t}")
        except: pass
    await conn.execute('CREATE TABLE IF NOT EXISTS logs (id SERIAL PRIMARY KEY, event_time TIMESTAMP, event_type TEXT, details TEXT)')
    await conn.close()

async def pinger():
    async with httpx.AsyncClient() as client:
        while True:
            await asyncio.sleep(300)
            try: await client.get(APP_URL)
            except: pass

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await bot.set_webhook(url=f"{APP_URL}/webhook", allowed_updates=["business_message", "message", "callback_query"])
    asyncio.create_task(pinger())
    yield

app.router.lifespan_context = lifespan

@app.post("/webhook")
async def webhook(request: Request):
    update = Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}

@app.get("/")
async def root(): return {"status": "ok"}
