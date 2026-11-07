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
    api_key = GEMINI_API_KEY
    base_url = "https://generativelanguage.googleapis.com/v1beta"
    headers = {'Content-Type': 'application/json'}

    try:
        async with aiohttp.ClientSession() as session:
            # BƯỚC 1: HỎI GOOGLE XEM KEY NÀY ĐƯỢC PHÉP DÙNG NHỮNG MODEL NÀO
            list_url = f"{base_url}/models?key={api_key}"
            async with session.get(list_url, timeout=10) as list_resp:
                if list_resp.status != 200:
                    return f"Lỗi xác thực Key (Mã {list_resp.status}): {await list_resp.text()}"

                data = await list_resp.json()
                
                # Lọc ra các model có hỗ trợ chức năng trả lời tin nhắn (generateContent)
                valid_models = [
                    m['name'] for m in data.get('models', [])
                    if 'generateContent' in m.get('supportedGenerationMethods', [])
                ]

                if not valid_models:
                    return "API Key hợp lệ nhưng tài khoản Google của bạn không có quyền dùng bất kỳ model AI nào."

                # Thuật toán chọn model tốt nhất trong danh sách Google vừa trả về
                chosen_model = valid_models[0] # Lấy model đầu tiên làm mặc định
                for m in valid_models:
                    if 'gemini-1.5-flash' in m:
                        chosen_model = m
                        break
                    elif 'gemini-1.5-pro' in m:
                        chosen_model = m
                        break
                    elif 'gemini-pro' in m:
                        chosen_model = m

            # BƯỚC 2: GỬI CÂU HỎI VÀO CHÍNH MODEL VỪA TÌM ĐƯỢC
            gen_url = f"{base_url}/{chosen_model}:generateContent?key={api_key}"
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 800, "temperature": 0.7}
            }

            async with session.post(gen_url, headers=headers, json=payload, timeout=20) as gen_resp:
                if gen_resp.status == 200:
                    gen_data = await gen_resp.json()
                    text = gen_data['candidates'][0]['content']['parts'][0]['text']
                    
                    # Báo cáo nhẹ cho bạn biết bot đã tự động tìm thấy model nào
                    model_short = chosen_model.replace("models/", "")
                    return f"[Đã tự động cấp cứu bằng: {model_short}]\n\n{text}"
                else:
                    error_text = await gen_resp.text()
                    return f"Lỗi khi gọi model {chosen_model}: {error_text}"

    except Exception as e:
        return f"Lỗi kết nối mạng: {str(e)}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot AI đã sẵn sàng hoạt động bằng cơ chế dò model tự động!")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    chat_id = update.effective_chat.id

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    try:
        reply_content = await asyncio.wait_for(call_gemini(user_text), timeout=35.0)
        await update.message.reply_text(reply_content)
    except asyncio.TimeoutError:
        await update.message.reply_text("Máy chủ phản hồi chậm (hơn 35s). Bạn gửi lại nhé!")
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
