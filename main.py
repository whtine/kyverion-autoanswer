import os
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import Update
from fastapi import FastAPI, Request

# Конфигурация из переменных окружения (на Render настроишь позже)
TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_PATH = f"/webhook/{TOKEN}"
BASE_URL = os.getenv("RENDER_EXTERNAL_URL") # Автоматическая переменная на Render

bot = Bot(token=TOKEN)
dp = Dispatcher()
app = FastAPI()

# Обработчик бизнес-сообщений
@dp.business_message()
async def handle_business_message(message: types.Message):
    await message.answer("Привет! Я бизнес-помощник. Владелец скоро свяжется с вами.")

# Эндпоинт для приема обновлений от Telegram
@app.post(WEBHOOK_PATH)
async def bot_webhook(request: Request):
    update = Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)

# Настройка вебхука при запуске сервера
@app.on_event("startup")
async def on_startup():
    webhook_url = f"{BASE_URL}{WEBHOOK_PATH}"
    await bot.set_webhook(url=webhook_url, allowed_updates=["business_message"])

@app.get("/")
async def root():
    return {"status": "bot is running"}
