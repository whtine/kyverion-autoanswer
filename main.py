import os, asyncio, asyncpg, httpx, logging, json
from datetime import datetime
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Update, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.filters import Command
from contextlib import asynccontextmanager

# Логи в консоль для дебага
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger("PRO_BOT")

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

async def get_conn():
    return await asyncpg.connect(DB_URL)

async def add_log(event_type, details):
    """Записывает событие в таблицу логов в БД"""
    try:
        conn = await get_conn()
        await conn.execute("INSERT INTO logs (event_time, event_type, details) VALUES ($1, $2, $3)", 
                           datetime.now(), event_type, details)
        await conn.close()
    except Exception as e:
        logger.error(f"Ошибка записи лога в БД: {e}")

# --- ЛОГИКА АВТООТВЕТА ---

@dp.business_message()
async def business_handler(message: types.Message):
    chat_id = message.chat.id
    sender_id = message.from_user.id

    # Если ты сам отвечаешь - отменяем таймер
    if sender_id == MY_ID:
        if chat_id in active_waits:
            active_waits[chat_id].cancel()
            del active_waits[chat_id]
            await add_log("CANCEL", f"Владелец ответил в чат {chat_id}. Бот отменен.")
        return

    conn = await get_conn()
    config = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", MY_ID)
    await conn.close()

    if config and config['is_active']:
        # Проверка времени (Часы работы)
        current_hour = datetime.now().hour
        start_h = config.get('work_start', 0)
        end_h = config.get('work_end', 24)

        if not (start_h <= current_hour < end_h):
            await add_log("SKIP", f"Вне рабочих часов ({current_hour}:00). Ожидалось {start_h}-{end_h}")
            return

        if chat_id in active_waits: return

        await add_log("RECEIVE", f"Сообщение от {sender_id}. Ждем 30с.")
        task = asyncio.create_task(delayed_reply(message, config))
        active_waits[chat_id] = task

async def delayed_reply(message, config):
    chat_id = message.chat.id
    try:
        await asyncio.sleep(30) # Задержка 30 секунд
        reply_text = config['reply_text']
        await message.answer(reply_text)
        await add_log("SENT", f"Ответил пользователю {message.from_user.id}: {reply_text}")
    except asyncio.CancelledError:
        pass
    finally:
        if chat_id in active_waits: del active_waits[chat_id]

# --- АДМИН ПАНЕЛЬ ---

@dp.message(Command("start"), F.from_user.id == MY_ID)
async def start_cmd(message: types.Message):
    conn = await get_conn()
    user = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", MY_ID)
    if not user:
        await conn.execute("INSERT INTO users (user_id, reply_text, is_active, work_start, work_end) VALUES ($1, $2, $3, $4, $5)", 
                           MY_ID, "Привет!", False, 0, 24)
        user = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", MY_ID)
    await conn.close()

    status = "✅ ВКЛ" if user['is_active'] else "❌ ВЫКЛ"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Изменить текст", callback_data="edit_text")],
        [InlineKeyboardButton(text="⏰ Часы работы", callback_data="edit_hours")],
        [InlineKeyboardButton(text=f"Бот: {status}", callback_data="switch")],
        [InlineKeyboardButton(text="📊 Выгрузить Логи БД", callback_data="get_logs")]
    ])
    
    await message.answer(
        f"🚀 **Управление ботом**\n\n"
        f"Статус: {status}\n"
        f"Текст: `{user['reply_text']}`\n"
        f"Часы работы: `{user['work_start']}:00 - {user['work_end']}:00` (UTC)\n\n"
        f"Бот ответит через 30с, если ты промолчишь.",
        reply_markup=kb, parse_mode="Markdown"
    )

@dp.callback_query(F.data == "edit_hours")
async def hours_req(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите часы работы в формате `9-21` (где 9 - начало, 21 - конец работы)")
    await state.set_state(Form.waiting_for_hours)

@dp.message(Form.waiting_for_hours, F.from_user.id == MY_ID)
async def hours_save(message: types.Message, state: FSMContext):
    try:
        start_h, end_h = map(int, message.text.split("-"))
        conn = await get_conn()
        await conn.execute("UPDATE users SET work_start=$1, work_end=$2 WHERE user_id=$3", start_h, end_h, MY_ID)
        await conn.close()
        await message.answer(f"✅ Часы работы установлены: {start_h}:00 - {end_h}:00")
        await state.clear()
        await start_cmd(message)
    except:
        await message.answer("❌ Ошибка! Введите например `8-22`")

@dp.callback_query(F.data == "get_logs")
async def send_logs(callback: types.CallbackQuery):
    conn = await get_conn()
    rows = await conn.fetch("SELECT * FROM logs ORDER BY event_time DESC LIMIT 50")
    await conn.close()
    
    if not rows:
        await callback.answer("Логи пока пусты")
        return

    log_text = "ПОСЛЕДНИЕ 50 СОБЫТИЙ:\n" + "-"*30 + "\n"
    for r in rows:
        log_text += f"[{r['event_time'].strftime('%H:%M:%S')}] {r['event_type']}: {r['details']}\n"
    
    file = BufferedInputFile(log_text.encode(), filename="logs.txt")
    await callback.message.answer_document(file, caption="Логи из БД")
    await callback.answer()

@dp.callback_query(F.data == "switch")
async def toggle(callback: types.CallbackQuery):
    conn = await get_conn()
    await conn.execute("UPDATE users SET is_active = NOT is_active WHERE user_id = $1", MY_ID)
    await conn.close()
    await start_cmd(callback.message)
    await callback.message.delete()

@dp.callback_query(F.data == "edit_text")
async def text_req(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Пришли новый текст автоответа:")
    await state.set_state(Form.waiting_for_text)

@dp.message(Form.waiting_for_text)
async def text_save(message: types.Message, state: FSMContext):
    conn = await get_conn()
    await conn.execute("UPDATE users SET reply_text=$1 WHERE user_id=$2", message.text, MY_ID)
    await conn.close()
    await state.clear()
    await start_cmd(message)

# --- СИСТЕМА ЖИЗНЕОБЕСПЕЧЕНИЯ ---

async def pinger():
    """Пингует сам себя каждые 5 минут, чтобы Render не спал"""
    async with httpx.AsyncClient() as client:
        while True:
            await asyncio.sleep(300) # 5 минут
            try:
                resp = await client.get(APP_URL)
                logger.info(f"Self-ping: {resp.status_code}")
            except Exception as e:
                logger.error(f"Ping error: {e}")

async def init_db():
    conn = await get_conn()
    # Таблица юзеров
    await conn.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY, 
        reply_text TEXT, 
        is_active BOOLEAN DEFAULT FALSE,
        work_start INTEGER DEFAULT 0,
        work_end INTEGER DEFAULT 24
    )''')
    # Таблица логов
    await conn.execute('''CREATE TABLE IF NOT EXISTS logs (
        id SERIAL PRIMARY KEY,
        event_time TIMESTAMP,
        event_type TEXT,
        details TEXT
    )''')
    await conn.close()

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await bot.set_webhook(url=f"{APP_URL}/webhook", allowed_updates=["business_message", "message", "callback_query"])
    asyncio.create_task(pinger()) # Запуск пингера
    yield

app.router.lifespan_context = lifespan

@app.post("/webhook")
async def webhook(request: Request):
    update = Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}

@app.get("/")
async def root(): return {"status": "I am alive"}
