import os
import asyncio
import logging
from aiohttp import web
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from google import genai
from google.genai import types
from google.genai.errors import APIError

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PORT = int(os.getenv("PORT", 8080))

if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY:
    raise ValueError("Thiếu biến môi trường TELEGRAM_BOT_TOKEN hoặc GEMINI_API_KEY!")

client = genai.Client(api_key=GEMINI_API_KEY)


async def call_gemini(prompt: str) -> str:
    try:
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                max_output_tokens=1000,
                temperature=0.7,
            ),
        )
        if response.text:
            return response.text
        return "AI không trả về nội dung văn bản."
    except Exception as e:
        return f"Lỗi chi tiết từ Google: {e}"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot AI trên Cloud đã sẵn sàng hoạt động!")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    try:
        reply_content = await asyncio.wait_for(call_gemini(user_text), timeout=25.0)
        await update.message.reply_text(reply_content)
    except Exception as e:
        await update.message.reply_text(f"Lỗi: {e}")


# Tạo trang web giả lập để Render nhận diện Web Service Free
async def handle_ping(request):
    return web.Response(text="Bot is running perfectly 24/7!")


async def run_dummy_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()


async def main():
    # Chạy máy chủ web mini
    await run_dummy_server()

    # Khởi động Telegram Bot
    telegram_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling(drop_pending_updates=True)

    # Giữ tiến trình chạy liên tục
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
