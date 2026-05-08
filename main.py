import os, asyncio, asyncpg, httpx, logging
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Update, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.filters import Command
from contextlib import asynccontextmanager

# Настройка логов
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("AUTO_ANSWER")

TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("RENDER_EXTERNAL_URL")
DB_URL = "postgresql://autoanswer_cfg_user:2UpBtzof467gxNdjkxwC12bRPlaor5y9@dpg-d7utdenlk1mc73aovmfg-a.ohio-postgres.render.com/autoanswer_cfg"
ADMIN_ID = 6956377285

bot = Bot(token=TOKEN)
dp = Dispatcher()
app = FastAPI()

class Form(StatesGroup):
    waiting_for_text = State()

async def get_conn():
    return await asyncpg.connect(DB_URL)

# --- БИЗНЕС ЛОГИКА (ОСНОВНОЙ ФИКС) ---
@dp.business_message()
async def business_handler(message: types.Message):
    # Тот, кому принадлежит аккаунт (на кого настроен бот)
    owner_id = message.chat.id 
    # Тот, кто прислал сообщение в личку
    sender_id = message.from_user.id 

    logger.info(f"--- [NEW MESSAGE] ---")
    logger.info(f"Владелец (кому пишут): {owner_id}")
    logger.info(f"Отправитель (клиент): {sender_id}")

    # Ищем настройки ТОЛЬКО для владельца аккаунта
    conn = await get_conn()
    owner_config = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", owner_id)
    await conn.close()

    if not owner_config:
        logger.info(f"Результат: Владелец {owner_id} не настраивал бота. Пропускаю.")
        return

    is_on = owner_config['is_active']
    text_to_send = owner_config['reply_text']

    logger.info(f"Настройки владельца: Активен={is_on}, Текст='{text_to_send}'")

    if is_on:
        # Если пишет НЕ сам владелец, а кто-то другой
        if sender_id != owner_id:
            try:
                await message.answer(text_to_send)
                logger.info(f"✅ ОТВЕТ ОТПРАВЛЕН: Кому: {sender_id} | От имени: {owner_id} | Текст: {text_to_send}")
            except Exception as e:
                logger.error(f"❌ ОШИБКА ОТПРАВКИ: {e}")
        else:
            logger.info("Результат: Владелец пишет сам, ответ не нужен.")
    else:
        logger.info(f"Результат: Автоответчик для {owner_id} выключен кнопкой в боте.")

# --- УПРАВЛЕНИЕ БОТОМ (Для тебя и других юзеров) ---

@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    conn = await get_conn()
    user = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", message.from_user.id)
    if not user:
        await conn.execute("INSERT INTO users (user_id, reply_text, is_active) VALUES ($1, $2, $3)", 
                           message.from_user.id, "Я сейчас занят, отвечу позже.", False)
        user = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", message.from_user.id)
    await conn.close()

    status_emoji = "✅ ВКЛ" if user['is_active'] else "❌ ВЫКЛ"
    buttons = [
        [InlineKeyboardButton(text="📝 Изменить текст", callback_data="edit")],
        [InlineKeyboardButton(text=f"Статус: {status_emoji}", callback_data="switch")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="me")]
    ]
    if message.from_user.id == ADMIN_ID:
        buttons.append([InlineKeyboardButton(text="📁 База данных (ADM)", callback_data="admin_db")])
    
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer(f"🤖 **Настройки твоего автоответчика**\n\nТекст: `{user['reply_text']}`\nСтатус: {status_emoji}", 
                         reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data == "switch")
async def handle_switch(callback: types.CallbackQuery):
    conn = await get_conn()
    user = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", callback.from_user.id)
    new_status = not user['is_active']
    await conn.execute("UPDATE users SET is_active = $1 WHERE user_id = $2", new_status, callback.from_user.id)
    await conn.close()
    await callback.answer(f"Статус изменен на {'ВКЛ' if new_status else 'ВЫКЛ'}")
    await start_cmd(callback.message)
    await callback.message.delete()

@dp.callback_query(F.data == "me")
async def handle_profile(callback: types.CallbackQuery):
    conn = await get_conn()
    user = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", callback.from_user.id)
    await conn.close()
    status_text = "Работает" if user['is_active'] else "Выключен"
    text = (f"👤 **Твой профиль**\n\n"
            f"Твой ID: `{user['user_id']}`\n"
            f"Статус: {status_text}\n"
            f"Текст ответа: {user['reply_text']}")
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data == "back")
async def back_to_start(callback: types.CallbackQuery):
    await start_cmd(callback.message)
    await callback.message.delete()

@dp.callback_query(F.data == "edit")
async def edit_text(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Пришли новый текст ответа:")
    await state.set_state(Form.waiting_for_text)

@dp.message(Form.waiting_for_text)
async def save_text(message: types.Message, state: FSMContext):
    conn = await get_conn()
    await conn.execute("UPDATE users SET reply_text = $1 WHERE user_id = $2", message.text, message.from_user.id)
    await conn.close()
    await state.clear()
    await message.answer("✅ Текст сохранен!")
    await start_cmd(message)

# --- АДМИН-ФУНКЦИИ ---
@dp.callback_query(F.data == "admin_db", F.from_user.id == ADMIN_ID)
async def admin_db(callback: types.CallbackQuery):
    conn = await get_conn()
    rows = await conn.fetch("SELECT * FROM users")
    await conn.close()
    report = "DB EXPORT:\n" + "\n".join([f"{r['user_id']} | {r['is_active']} | {r['reply_text'][:15]}" for r in rows])
    file = BufferedInputFile(report.encode(), filename="database.txt")
    await callback.message.answer_document(file, caption="Актуальная база")
    await callback.answer()

@dp.message(Command("db_clear"), F.from_user.id == ADMIN_ID)
async def db_clear(message: types.Message):
    conn = await get_conn()
    await conn.execute("DELETE FROM users WHERE user_id != $1", ADMIN_ID)
    await conn.close()
    await message.answer("🧹 База очищена!")

# --- СЛУЖЕБНОЕ ---
async def ping_self():
    async with httpx.AsyncClient() as client:
        while True:
            await asyncio.sleep(600)
            try: await client.get(APP_URL)
            except: pass

@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = await get_conn()
    await conn.execute('CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, reply_text TEXT, is_active BOOLEAN DEFAULT FALSE)')
    await conn.close()
    await bot.set_webhook(url=f"{APP_URL}/webhook", allowed_updates=["business_message", "message", "callback_query"])
    asyncio.create_task(ping_self())
    yield

app.router.lifespan_context = lifespan

@app.post("/webhook")
async def webhook(request: Request):
    update = Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}

@app.get("/")
async def root(): return {"status": "ok"}
