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
    # BỔ SUNG: Thêm model Pro và luân chuyển giữa các cụm máy chủ khác nhau
    models_to_try = [
        "gemini-2.5-pro",
        "gemini-1.5-pro",
        "gemini-flash-latest",
        "gemini-3.8-flash",
    ]
    config = types.GenerateContentConfig(
        max_output_tokens=800,
        temperature=0.7,
    )

    last_error = ""
    for model_name in models_to_try:
        try:
            response = await client.aio.models.generate_content(
                model=model_name,
                contents=prompt,
                config=config,
            )
            if response.text:
                return response.text
        except Exception as e:
            last_error = repr(e)
            # BỔ SUNG: Ngăn chặn spam bằng cách nghỉ 1s trước khi đổi model
            await asyncio.sleep(1.0)
            continue

    return f"Google từ chối phản hồi: {last_error}"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot AI đã sẵn sàng hoạt động!")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    chat_id = update.effective_chat.id

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    try:
        reply_content = await asyncio.wait_for(call_gemini(user_text), timeout=35.0)
        await update.message.reply_text(reply_content)
    except asyncio.TimeoutError:
        await update.message.reply_text("Hết thời gian chờ phản hồi (35s). Bạn hãy gửi lại nhé!")
    except Exception as e:
        err_msg = repr(e) if not str(e) else str(e)
        await update.message.reply_text(f"Lỗi: {err_msg}")


async def handle_ping(request):
    return web.Response(text="Bot is live!")


async def run_dummy_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()


async def main():
    await run_dummy_server()

    telegram_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling(drop_pending_updates=True)

    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
