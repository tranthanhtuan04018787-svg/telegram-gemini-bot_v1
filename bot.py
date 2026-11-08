import os
import time
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
from datetime import datetime, timedelta, timezone

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# Ẩn log httpx để không bị lộ TELEGRAM_BOT_TOKEN trên màn hình Render
logging.getLogger("httpx").setLevel(logging.WARNING)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_CHAT_ID = str(os.getenv("ADMIN_CHAT_ID", "")).strip()
SHEET_WEBHOOK_URL = os.getenv("SHEET_WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8080))

if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY:
    raise ValueError("Thiếu biến môi trường TELEGRAM_BOT_TOKEN hoặc GEMINI_API_KEY!")

user_histories = {}
MAX_HISTORY = 4

# Bộ nhớ đệm lưu danh sách model khả dụng của API Key
cached_models = []
last_model_fetch = 0
active_model_name = "Đang tự động dò tìm..."

SYSTEM_PROMPT = """
Bạn là trợ lý ảo cá nhân của anh Tuấn. 
Nhiệm vụ của bạn là tiếp đón người thân, đối tác hoặc bạn bè của anh Tuấn một cách lịch sự, thân thiện. 
Khi giao tiếp, hãy xưng là "mình" hoặc "trợ lý của anh Tuấn" và gọi người chat là "bạn" hoặc "anh/chị". 
Nếu họ hỏi Tuấn đâu, hãy nói rằng anh Tuấn đang bận chút việc, bạn đã ghi nhận thông tin và anh Tuấn sẽ phản hồi lại sau.
Tuyệt đối không nhận mình là AI do Google tạo ra.
"""

def get_vn_time() -> str:
    now = datetime.now(timezone.utc) + timedelta(hours=7)
    return now.strftime("%d/%m/%Y %H:%M:%S")

def score_model(model_id: str) -> tuple:
    """Chấm điểm ưu tiên: 3.1-flash -> các dòng flash mới nhất -> các dòng pro."""
    m = model_id.lower()
    is_3_1_flash = 1 if ("3.1" in m and "flash" in m) else 0
    is_flash = 1 if "flash" in m else 0
    not_lite = 0 if ("lite" in m or "8b" in m) else 1

    ver_score = 0
    for idx, ver in enumerate(["1.5", "2.0", "2.5", "3.0", "3.1", "3.5", "3.8"]):
        if ver in m:
            ver_score = idx + 1

    return (is_3_1_flash, is_flash, ver_score, not_lite)

async def get_available_models(session: aiohttp.ClientSession) -> list:
    """Gọi trực tiếp ListModels của Google để lấy đúng tên model mà API Key hỗ trợ."""
    global cached_models, last_model_fetch
    if cached_models and (time.time() - last_model_fetch < 3600):
        return cached_models

    list_url = f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_API_KEY}"
    try:
        async with session.get(list_url, timeout=15) as resp:
            if resp.status == 200:
                data = await resp.json()
                valid_models = []
                excluded_keywords = [
                    "tts", "image", "imagen", "veo", "embedding",
                    "aqa", "audio", "robotics", "computer-use", "learnlm"
                ]
                for item in data.get("models", []):
                    name = item.get("name", "") # Dạng: models/gemini-...
                    methods = item.get("supportedGenerationMethods", [])
                    if "generateContent" in methods and "gemini" in name.lower():
                        if not any(kw in name.lower() for kw in excluded_keywords):
                            valid_models.append(name)

                if valid_models:
                    valid_models.sort(key=score_model, reverse=True)
                    cached_models = valid_models
                    last_model_fetch = time.time()
                    logging.info(f"Danh sách model khả dụng: {cached_models}")
                    return cached_models
            else:
                err = await resp.text()
                logging.error(f"Lỗi khi gọi ListModels ({resp.status}): {err}")
    except Exception as e:
        logging.error(f"Lỗi kết nối ListModels: {e}")

    # Danh sách dự phòng nếu mạng chập chờn không gọi được ListModels
    return [
        "models/gemini-2.5-flash",
        "models/gemini-2.0-flash",
        "models/gemini-1.5-flash-latest",
    ]

async def log_to_sheet(chat_id: str, sender_name: str, user_text: str, bot_reply: str):
    if not SHEET_WEBHOOK_URL:
        return
    payload = {
        "time": get_vn_time(),
        "chat_id": chat_id,
        "sender_name": sender_name,
        "user_text": user_text,
        "bot_reply": bot_reply,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(SHEET_WEBHOOK_URL, json=payload, timeout=15) as resp:
                await resp.text()
    except Exception as e:
        logging.error(f"Lỗi ghi Google Sheet: {e}")

async def call_gemini(chat_id: str, user_text: str) -> str:
    global active_model_name
    if chat_id not in user_histories:
        user_histories[chat_id] = []

    user_histories[chat_id].append({"role": "user", "parts": [{"text": user_text}]})
    if len(user_histories[chat_id]) > MAX_HISTORY:
        user_histories[chat_id] = user_histories[chat_id][-MAX_HISTORY:]

    base_url = "https://generativelanguage.googleapis.com/v1beta"
    headers = {"Content-Type": "application/json"}
    dynamic_system_prompt = (
        SYSTEM_PROMPT
        + f"\n\n[THÔNG TIN BẮT BUỘC]: Thời gian hiện tại là {get_vn_time()} (Giờ Việt Nam)."
    )

    payload = {
        "systemInstruction": {"parts": [{"text": dynamic_system_prompt}]},
        "contents": user_histories[chat_id],
        "generationConfig": {"maxOutputTokens": 800, "temperature": 0.7},
    }

    try:
        async with aiohttp.ClientSession() as session:
            models_to_try = await get_available_models(session)
            last_error = ""

            for chosen_model in models_to_try:
                # Đảm bảo định dạng chuẩn models/ten-model
                model_path = chosen_model if chosen_model.startswith("models/") else f"models/{chosen_model}"
                gen_url = f"{base_url}/{model_path}:generateContent?key={GEMINI_API_KEY}"

                async with session.post(gen_url, headers=headers, json=payload, timeout=45) as gen_resp:
                    if gen_resp.status == 200:
                        gen_data = await gen_resp.json()
                        try:
                            bot_reply = gen_data["candidates"][0]["content"]["parts"][0]["text"]
                            user_histories[chat_id].append({"role": "model", "parts": [{"text": bot_reply}]})
                            active_model_name = model_path
                            return bot_reply
                        except (KeyError, IndexError):
                            continue
                    elif gen_resp.status in [429, 503]:
                        # Quá tải hoặc hết quota ở model này -> tự thử model tiếp theo
                        await asyncio.sleep(0.5)
                        continue
                    else:
                        last_error = f"Mã {gen_resp.status} ({model_path}): {await gen_resp.text()}"
                        logging.warning(f"Bỏ qua model {model_path} do lỗi: {last_error[:200]}")
                        continue

            user_histories[chat_id].pop()
            return f"⚠️ Không thể kết nối AI. Chi tiết lỗi cuối: {last_error[:300]}"

    except Exception as e:
        user_histories[chat_id].pop()
        return f"❌ LỖI HỆ THỐNG/MẠNG:\n{str(e)}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id).strip()
    user_histories[chat_id] = []
    await update.message.reply_text("Xin chào! Mình là trợ lý của anh Tuấn. Mình có thể giúp gì cho bạn?")

async def check_model_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lệnh /model giúp kiểm tra danh sách model thực tế mà API Key đang sở hữu."""
    async with aiohttp.ClientSession() as session:
        models = await get_available_models(session)
    top_models = "\n".join([f"• `{m}`" for m in models[:8]])
    msg = (
        f"🤖 **Model đang dùng:** `{active_model_name}`\n\n"
        f"📋 **Top model khả dụng trong API Key của bạn:**\n{top_models}"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

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
    telegram_app.add_handler(CommandHandler("model", check_model_cmd))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling(drop_pending_updates=True)

    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
