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
    # Ưu tiên dòng 3.1 và 3.0 flash (chính là model vừa chạy thành công)
    models_to_try = [
        "gemini-3.1-flash",
        "gemini-3-flash-preview"
    ]
    
    base_url = "https://generativelanguage.googleapis.com/v1beta"
    headers = {'Content-Type': 'application/json'}
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 800, "temperature": 0.7}
    }

    try:
        async with aiohttp.ClientSession() as session:
            for chosen_model in models_to_try:
                gen_url = f"{base_url}/models/{chosen_model}:generateContent?key={GEMINI_API_KEY}"
                
                async with session.post(gen_url, headers=headers, json=payload, timeout=20) as gen_resp:
                    if gen_resp.status == 200:
                        gen_data = await gen_resp.json()
                        try:
                            # Lấy nội dung trả lời (không chèn thêm các dòng chữ báo cáo kỹ thuật)
                            return gen_data['candidates'][0]['content']['parts'][0]['text']
                        except (KeyError, IndexError):
                            continue # Thử model dự phòng nếu dữ liệu trống
                    elif gen_resp.status in [429, 503]:
                        # Nếu model chính bận/hết hạn mức, nghỉ 1s rồi chuyển sang model dự phòng
                        await asyncio.sleep(1.0)
                        continue

            return "Máy chủ AI hiện đang bận, bạn vui lòng gửi lại sau vài giây nhé!"
            
    except Exception:
        return "Xin lỗi, kết nối mạng đang gián đoạn. Bạn thử lại nhé!"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Chào bạn! Tôi là trợ lý AI (Gemini 3.1). Tôi có thể giúp gì cho bạn hôm nay?")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    chat_id = update.effective_chat.id

    # Hiện trạng thái "Bot đang gõ..."
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    try:
        reply_content = await asyncio.wait_for(call_gemini(user_text), timeout=30.0)
        await update.message.reply_text(reply_content)
    except asyncio.TimeoutError:
        await update.message.reply_text("Hệ thống xử lý hơi lâu. Bạn vui lòng gửi lại câu hỏi nhé!")
    except Exception:
        await update.message.reply_text("Rất tiếc, đã có lỗi nhỏ xảy ra. Hãy thử lại!")

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
