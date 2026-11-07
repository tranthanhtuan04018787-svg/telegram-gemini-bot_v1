import os
import asyncio
import logging
import aiohttp
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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PORT = int(os.getenv("PORT", 8080))

if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY:
    raise ValueError("Thiếu biến môi trường TELEGRAM_BOT_TOKEN hoặc GEMINI_API_KEY!")

async def call_gemini(prompt: str) -> str:
    # Gọi trực tiếp REST API chuẩn v1beta, bỏ qua thư viện SDK bị lỗi của Google
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    headers = {'Content-Type': 'application/json'}
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": 800,
            "temperature": 0.7
        }
    }

    # Cơ chế tự động thử lại 3 lần nếu dính lỗi 503 (quá tải cục bộ)
    for attempt in range(3):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=payload, timeout=20) as response:
                    if response.status == 200:
                        data = await response.json()
                        try:
                            return data['candidates'][0]['content']['parts'][0]['text']
                        except (KeyError, IndexError):
                            return "AI trả về dữ liệu trống."
                    elif response.status in [503, 429]: 
                        # Nếu máy chủ bận, nghỉ 2 giây rồi gọi lại
                        await asyncio.sleep(2.0)
                        continue
                    else:
                        error_data = await response.text()
                        return f"Lỗi máy chủ Google (Mã {response.status}): {error_data}"
        except Exception as e:
            return f"Lỗi kết nối mạng: {str(e)}"
    
    return "Máy chủ Google hiện đang quá tải sau nhiều lần thử nghiệm. Vui lòng nhắn lại sau 1-2 phút nhé!"

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
        await update.message.reply_text(f"Lỗi hệ thống: {str(e)}")

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
