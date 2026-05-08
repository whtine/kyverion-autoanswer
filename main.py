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
logger = logging.getLogger("AUTO_BOT")

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
                           datetime.now(), event_type, str(details))
        await conn.close()
    except Exception as e:
        logger.error(f"Ошибка записи лога: {e}")

# --- БИЗНЕС ЛОГИКА ---

@dp.business_message()
async def business_handler(message: types.Message):
    chat_id = message.chat.id
    sender_id = message.from_user.id

    # Если отвечаешь ТЫ - отменяем таймер бота
    if sender_id == MY_ID:
        if chat_id in active_waits:
            active_waits[chat_id].cancel()
            del active_waits[chat_id]
            await add_log("CANCEL", f"Владелец ответил сам в чат {chat_id}")
        return

    # Если пишет КЛИЕНТ
    conn = await get_conn()
    config = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", MY_ID)
    await conn.close()

    if config and config['is_active']:
        # Проверка времени
        current_hour = datetime.now().hour
        start_h = config.get('work_start', 0)
        end_h = config.get('work_end', 24)

        if not (start_h <= current_hour < end_h):
            # Вне рабочего времени - не отвечаем
            return

        # Если уже ждем ответа в этом чате - не плодим задачи
        if chat_id in active_waits:
            return

        await add_log("RECEIVE", f"Новое от {sender_id}. Ждем 30с.")
        task = asyncio.create_task(delayed_reply(message, config))
        active_waits[chat_id] = task

async def delayed_reply(message, config):
    chat_id = message.chat.id
    try:
        await asyncio.sleep(30) # Задержка перед автоответом
        reply_text = config['reply_text']
        await message.answer(reply_text)
        await add_log("SENT", f"Бот ответил пользователю {message.from_user.id}")
    except asyncio.CancelledError:
        # Сюда попадем, если ты ответил сам в течение 30 секунд
        pass
    finally:
        if chat_id in active_waits:
            del active_waits[chat_id]

# --- КОМАНДЫ И УПРАВЛЕНИЕ ---

@dp.message(Command("start"), F.from_user.id == MY_ID)
async def start_cmd(message: types.Message):
    conn = await get_conn()
    user = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", MY_ID)
    
    if not user:
        await conn.execute("INSERT INTO users (user_id, reply_text, is_active) VALUES ($1, $2, $3)", 
                           MY_ID, "Привет! Я сейчас занят.", False)
        user = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", MY_ID)
    await conn.close()

    # Защита от KeyError: берем данные безопасно
    is_active = user.get('is_active', False)
    w_start = user.get('work_start', 0)
    w_end = user.get('work_end', 24)
    txt = user.get('reply_text', "Текст не установлен")

    status_emoji = "✅ ВКЛ" if is_active else "❌ ВЫКЛ"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Текст ответа", callback_data="edit_text")],
        [InlineKeyboardButton(text="⏰ Часы работы", callback_data="edit_hours")],
        [InlineKeyboardButton(text=f"Статус: {status_emoji}", callback_data="switch")],
        [InlineKeyboardButton(text="📊 Логи БД", callback_data="get_logs")]
    ])
    
    await message.answer(
        f"🛠 **Настройки автоответчика**\n\n"
        f"Статус: {status_emoji}\n"
        f"Текст: `{txt}`\n"
        f"Часы: `{w_start}:00 - {w_end}:00` (UTC)\n\n"
        f"Задержка: 30 секунд.",
        reply_markup=kb, parse_mode="Markdown"
    )

@dp.callback_query(F.data == "switch", F.from_user.id == MY_ID)
async def handle_switch(callback: types.CallbackQuery):
    conn = await get_conn()
    await conn.execute("UPDATE users SET is_active = NOT is_active WHERE user_id = $1", MY_ID)
    await conn.close()
    await callback.answer("Статус изменен")
    await start_cmd(callback.message)
    await callback.message.delete()

@dp.callback_query(F.data == "edit_hours", F.from_user.id == MY_ID)
async def edit_hours_req(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите диапазон часов, когда бот должен работать (например `9-21`):")
    await state.set_state(Form.waiting_for_hours)

@dp.message(Form.waiting_for_hours, F.from_user.id == MY_ID)
async def save_hours(message: types.Message, state: FSMContext):
    try:
        h_range = message.text.replace(" ", "").split("-")
        start_h, end_h = int(h_range[0]), int(h_range[1])
        conn = await get_conn()
        await conn.execute("UPDATE users SET work_start=$1, work_end=$2 WHERE user_id=$3", start_h, end_h, MY_ID)
        await conn.close()
        await message.answer(f"✅ Время работы: {start_h}:00 - {end_h}:00")
        await state.clear()
        await start_cmd(message)
    except Exception:
        await message.answer("❌ Ошибка! Введите формат `начало-конец`, например `10-20`")

@dp.callback_query(F.data == "edit_text", F.from_user.id == MY_ID)
async def edit_text_req(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Пришли новый текст для автоответа:")
    await state.set_state(Form.waiting_for_text)

@dp.message(Form.waiting_for_text, F.from_user.id == MY_ID)
async def save_text(message: types.Message, state: FSMContext):
    conn = await get_conn()
    await conn.execute("UPDATE users SET reply_text=$1 WHERE user_id=$2", message.text, MY_ID)
    await conn.close()
    await state.clear()
    await message.answer("✅ Текст обновлен")
    await start_cmd(message)

@dp.callback_query(F.data == "get_logs", F.from_user.id == MY_ID)
async def get_logs_btn(callback: types.CallbackQuery):
    conn = await get_conn()
    rows = await conn.fetch("SELECT * FROM logs ORDER BY event_time DESC LIMIT 50")
    await conn.close()
    
    if not rows:
        await callback.answer("Логов пока нет")
        return

    output = "ПОСЛЕДНИЕ СОБЫТИЯ:\n" + "="*20 + "\n"
    for r in rows:
        output += f"[{r['event_time'].strftime('%H:%M:%S')}] {r['event_type']}: {r['details']}\n"
    
    file = BufferedInputFile(output.encode(), filename="logs.txt")
    await callback.message.answer_document(file, caption="История действий бота")
    await callback.answer()

# --- СЛУЖЕБНОЕ: ПИНГЕР И БД ---

async def pinger():
    """Не дает Render усыпить бота"""
    async with httpx.AsyncClient() as client:
        while True:
            await asyncio.sleep(300) # 5 минут
            try:
                r = await client.get(APP_URL)
                logger.info(f"Ping: {r.status_code}")
            except: pass

async def init_db():
    conn = await get_conn()
    # Создаем таблицы
    await conn.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY, 
        reply_text TEXT DEFAULT 'Я занят', 
        is_active BOOLEAN DEFAULT FALSE,
        work_start INTEGER DEFAULT 0,
        work_end INTEGER DEFAULT 24
    )''')
    await conn.execute('''CREATE TABLE IF NOT EXISTS logs (
        id SERIAL PRIMARY KEY,
        event_time TIMESTAMP,
        event_type TEXT,
        details TEXT
    )''')
    # ПРИНУДИТЕЛЬНОЕ добавление колонок, если таблица была создана ранее без них
    try:
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS work_start INTEGER DEFAULT 0")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS work_end INTEGER DEFAULT 24")
    except: pass
    await conn.close()
    logger.info("БД готова.")

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await bot.set_webhook(url=f"{APP_URL}/webhook", allowed_updates=["business_message", "message", "callback_query"])
    asyncio.create_task(pinger())
    yield

app.router.lifespan_context = lifespan

@app.post("/webhook")
async def webhook(request: Request):
    try:
        body = await request.json()
        update = Update.model_validate(body, context={"bot": bot})
        await dp.feed_update(bot, update)
    except Exception as e:
        logger.error(f"Webhook error: {e}")
    return {"ok": True}

@app.get("/")
async def root(): return {"status": "alive"}
