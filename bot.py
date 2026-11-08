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
from datetime import datetime, timedelta

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_CHAT_ID = str(os.getenv("ADMIN_CHAT_ID", "")).strip()
SHEET_WEBHOOK_URL = os.getenv("SHEET_WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8080))

if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY:
    raise ValueError("Thiếu biến môi trường TELEGRAM_BOT_TOKEN hoặc GEMINI_API_KEY!")

user_histories = {}
MAX_HISTORY = 4 

SYSTEM_PROMPT = """
Bạn là trợ lý ảo cá nhân của anh Tuấn. 
Nhiệm vụ của bạn là tiếp đón người thân, đối tác hoặc bạn bè của anh Tuấn một cách lịch sự, thân thiện. 
Khi giao tiếp, hãy xưng là "mình" hoặc "trợ lý của anh Tuấn" và gọi người chat là "bạn" hoặc "anh/chị". 
Nếu họ hỏi Tuấn đâu, hãy nói rằng anh Tuấn đang bận chút việc, bạn đã ghi nhận thông tin và anh Tuấn sẽ phản hồi lại sau.
Tuyệt đối không nhận mình là AI do Google tạo ra.
"""

async def log_to_sheet(chat_id: str, sender_name: str, user_text: str, bot_reply: str):
    if not SHEET_WEBHOOK_URL:
        return "❌ TRỐNG LINK: Render chưa có biến SHEET_WEBHOOK_URL"
    
    now = datetime.utcnow() + timedelta(hours=7)
    payload = {
        "time": now.strftime("%d/%m/%Y %H:%M:%S"),
        "chat_id": chat_id,
        "sender_name": sender_name,
        "user_text": user_text,
        "bot_reply": bot_reply
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(SHEET_WEBHOOK_URL, json=payload, timeout=15) as resp:
                resp_text = await resp.text()
                if resp.status in [200, 201, 302]:
                    return f"✅ Phản hồi từ Google: {resp_text}"
                else:
                    return f"❌ Lỗi HTTP {resp.status}: {resp_text[:100]}"
    except Exception as e:
        return f"❌ Lỗi kết nối mạng từ Render: {str(e)}"

async def call_gemini(chat_id: str, user_text: str) -> str:
    if chat_id not in user_histories:
        user_histories[chat_id] = []
        
    user_histories[chat_id].append({"role": "user", "parts": [{"text": user_text}]})
    
    if len(user_histories[chat_id]) > MAX_HISTORY:
        user_histories[chat_id] = user_histories[chat_id][-MAX_HISTORY:]

    models_to_try = ["gemini-3.1-flash", "gemini-3-flash-preview"]
    base_url = "https://generativelanguage.googleapis.com/v1beta"
    headers = {'Content-Type': 'application/json'}
    
    now = datetime.utcnow() + timedelta(hours=7)
    current_time = now.strftime("%d/%m/%Y %H:%M:%S")
    dynamic_system_prompt = SYSTEM_PROMPT + f"\n\n[THÔNG TIN BẮT BUỘC]: Thời gian hiện tại là {current_time} (Giờ Việt Nam)."

    payload = {
        "systemInstruction": {
            "parts": [{"text": dynamic_system_prompt}]
        },
        "contents": user_histories[chat_id],
        "generationConfig": {"maxOutputTokens": 800, "temperature": 0.7}
    }

    try:
        async with aiohttp.ClientSession() as session:
            for chosen_model in models_to_try:
                gen_url = f"{base_url}/models/{chosen_model}:generateContent?key={GEMINI_API_KEY}"
                
                async with session.post(gen_url, headers=headers, json=payload, timeout=45) as gen_resp:
                    if gen_resp.status == 200:
                        gen_data = await gen_resp.json()
                        try:
                            bot_reply = gen_data['candidates'][0]['content']['parts'][0]['text']
                            user_histories[chat_id].append({"role": "model", "parts": [{"text": bot_reply}]})
                            return bot_reply
                        except (KeyError, IndexError):
                            return f"⚠️ Lỗi trích xuất dữ liệu: {gen_data}"
                    elif gen_resp.status in [429, 503]:
                        await asyncio.sleep(1.0)
                        continue
                    else:
                        error_text = await gen_resp.text()
                        user_histories[chat_id].pop()
                        return f"⚠️ LỖI GOOGLE API (Mã {gen_resp.status}):\n{error_text[:500]}"

            user_histories[chat_id].pop()
            return "Máy chủ AI hiện đang bận (Đã thử hết các model nhưng không phản hồi)."
            
    except Exception as e:
        user_histories[chat_id].pop()
        return f"❌ LỖI HỆ THỐNG/MẠNG:\n{str(e)}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id).strip()
    user_histories[chat_id] = []
    await update.message.reply_text("Xin chào! Mình là trợ lý của anh Tuấn. Mình có thể giúp gì cho bạn?")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    chat_id = str(update.effective_chat.id).strip()
    sender_name = update.message.from_user.full_name

    if ADMIN_CHAT_ID and chat_id != ADMIN_CHAT_ID:
        try:
            report_msg = f"🔔 **Có tin nhắn mới tới Bot!**\n👤 Từ: {sender_name}\n💬 Nội dung: {user_text}"
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=report_msg)
        except Exception as e:
            logging.error(f"Lỗi gửi báo cáo: {e}")

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    try:
        reply_content = await asyncio.wait_for(call_gemini(chat_id, user_text), timeout=60.0)
        await update.message.reply_text(reply_content)
        
        asyncio.create_task(log_to_sheet(chat_id, sender_name, user_text, reply_content))
        
    except asyncio.TimeoutError:
        await update.message.reply_text("⏳ Quá thời gian chờ (Timeout). Kết nối quá chậm.")
    except Exception as e:
        await update.message.reply_text(f"Rất tiếc, đã có lỗi xảy ra: {str(e)}")

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
