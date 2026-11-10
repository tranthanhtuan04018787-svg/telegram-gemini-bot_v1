import os
import asyncio
import logging
import base64
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
logging.getLogger("httpx").setLevel(logging.WARNING)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_CHAT_ID = str(os.getenv("ADMIN_CHAT_ID", "")).strip()
GITHUB_REPO = os.getenv("GITHUB_REPO", "tranthantuan04018787-svg/Brain-2").strip()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
SHEET_WEBHOOK_URL = os.getenv("SHEET_WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8080))

if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY:
    raise ValueError("Thiếu biến môi trường TELEGRAM_BOT_TOKEN hoặc GEMINI_API_KEY!")

user_histories = {}
MAX_HISTORY = 4

http_session: aiohttp.ClientSession = None

cached_models = [
    "models/gemini-3.1-flash-lite",
    "models/gemini-3.1-flash-lite-preview",
    "models/gemini-3.8-flash",
    "models/gemini-3.5-flash",
    "models/gemini-2.5-flash",
]
active_model_name = cached_models[0]

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

def get_vn_date() -> str:
    now = datetime.now(timezone.utc) + timedelta(hours=7)
    return now.strftime("%Y-%m-%d")

async def get_http_session() -> aiohttp.ClientSession:
    global http_session
    if http_session is None or http_session.closed:
        connector = aiohttp.TCPConnector(limit=50, keepalive_timeout=60)
        http_session = aiohttp.ClientSession(connector=connector)
    return http_session

def score_model(model_id: str) -> tuple:
    m = model_id.lower()
    is_3_1_flash = 1 if ("3.1" in m and "flash" in m) else 0
    not_preview = 0 if ("preview" in m or "exp" in m) else 1
    is_flash = 1 if "flash" in m else 0
    not_lite = 0 if ("lite" in m or "8b" in m) else 1

    ver_score = 0
    for idx, ver in enumerate(["1.5", "2.0", "2.5", "3.0", "3.1", "3.5", "3.6", "3.7", "3.8"]):
        if ver in m:
            ver_score = idx + 1

    return (is_3_1_flash, not_preview, is_flash, ver_score, not_lite)

async def refresh_models_from_google() -> list:
    global cached_models, active_model_name
    session = await get_http_session()
    list_url = f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_API_KEY}"
    try:
        async with session.get(list_url, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                valid_models = []
                excluded = [
                    "tts", "image", "imagen", "veo", "embedding",
                    "aqa", "audio", "robotics", "computer-use", "learnlm", "transcribe"
                ]
                for item in data.get("models", []):
                    name = item.get("name", "")
                    methods = item.get("supportedGenerationMethods", [])
                    if "generateContent" in methods and "gemini" in name.lower():
                        if not any(kw in name.lower() for kw in excluded):
                            valid_models.append(name)

                if valid_models:
                    valid_models.sort(key=score_model, reverse=True)
                    cached_models = valid_models
                    active_model_name = cached_models[0]
                    logging.info(f"Đã đồng bộ model ưu tiên số 1: {active_model_name}")
    except Exception as e:
        logging.error(f"Lỗi đồng bộ ListModels: {e}")
    return cached_models

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
        session = await get_http_session()
        async with session.post(SHEET_WEBHOOK_URL, json=payload, timeout=15) as resp:
            await resp.text()
    except Exception as e:
        logging.error(f"Lỗi ghi Google Sheet: {e}")

async def save_note_to_github(note_content: str) -> tuple[bool, str]:
    """Lưu ghi chú và trả về chi tiết phản hồi để gỡ lỗi."""
    if not GITHUB_TOKEN:
        return False, "Thiếu biến GITHUB_TOKEN trên Render!"
    if not GITHUB_REPO:
        return False, "Thiếu biến GITHUB_REPO trên Render!"

    session = await get_http_session()
    # Lưu trực tiếp vào Inbox.md ở thư mục gốc để loại trừ lỗi đường dẫn thư mục
    file_path = "Inbox.md"
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{file_path}"
    
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    timestamp = get_vn_time()
    formatted_entry = f"\n- **[{timestamp}]**: {note_content}\n"

    try:
        sha = None
        existing_text = ""
        
        # 1. Kiểm tra file cũ trên GitHub
        async with session.get(f"{url}?ref=main", headers=headers, timeout=15) as resp:
            if resp.status == 200:
                file_info = await resp.json()
                sha = file_info.get("sha")
                raw_bytes = base64.b64decode(file_info.get("content", ""))
                existing_text = raw_bytes.decode("utf-8", errors="ignore")
            elif resp.status == 404:
                existing_text = "# 📥 Hộp thư đến (Inbox)\n\n"
            else:
                err_body = await resp.text()
                return False, f"Lỗi GET ({resp.status}): {err_body[:180]}"

        # 2. Chuẩn bị nội dung mã hóa Base64
        new_text = existing_text + formatted_entry
        encoded_content = base64.b64encode(new_text.encode("utf-8")).decode("utf-8")

        payload = {
            "message": f"note: {timestamp}",
            "content": encoded_content,
            "branch": "main",
        }
        if sha:
            payload["sha"] = sha

        # 3. Ghi file lên nhánh main
        async with session.put(url, headers=headers, json=payload, timeout=20) as put_resp:
            if put_resp.status in [200, 201]:
                return True, "Thành công"
            else:
                err_body = await put_resp.text()
                return False, f"Lỗi PUT ({put_resp.status}): {err_body[:180]}"
    except Exception as e:
        return False, f"Ngoại lệ kết nối: {str(e)}"

async def note_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id).strip()
    if chat_id != ADMIN_CHAT_ID:
        await update.message.reply_text("Bạn không có quyền thực hiện lệnh này.")
        return

    note_text = " ".join(context.args).strip()
    if not note_text:
        await update.message.reply_text("⚠️ Nhập nội dung sau lệnh! Ví dụ: `/note Kiểm tra`", parse_mode="Markdown")
        return

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    ok, debug_info = await save_note_to_github(note_text)

    if ok:
        await update.message.reply_text(f"✅ **Đã lưu vào Obsidian:**\n_{note_text}_", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ **Lỗi chi tiết từ GitHub:**\n`{debug_info}`", parse_mode="Markdown")

async def call_gemini(chat_id: str, user_text: str) -> str:
    global active_model_name, cached_models
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
        "generationConfig": {"maxOutputTokens": 600, "temperature": 0.7},
    }

    try:
        session = await get_http_session()
        last_error = ""

        for idx, chosen_model in enumerate(list(cached_models)):
            model_path = chosen_model if chosen_model.startswith("models/") else f"models/{chosen_model}"
            gen_url = f"{base_url}/{model_path}:generateContent?key={GEMINI_API_KEY}"

            async with session.post(gen_url, headers=headers, json=payload, timeout=30) as gen_resp:
                if gen_resp.status == 200:
                    gen_data = await gen_resp.json()
                    try:
                        bot_reply = gen_data["candidates"][0]["content"]["parts"][0]["text"]
                        user_histories[chat_id].append({"role": "model", "parts": [{"text": bot_reply}]})
                        active_model_name = model_path
                        if idx > 0 and chosen_model in cached_models:
                            cached_models.remove(chosen_model)
                            cached_models.insert(0, chosen_model)
                        return bot_reply
                    except (KeyError, IndexError):
                        continue
                elif gen_resp.status == 404:
                    asyncio.create_task(refresh_models_from_google())
                    continue
                elif gen_resp.status in [429, 503]:
                    continue
                else:
                    last_error = f"Mã {gen_resp.status} ({model_path}): {await gen_resp.text()}"
                    continue

        user_histories[chat_id].pop()
        return f"⚠️ Không thể kết nối AI. Chi tiết: {last_error[:300]}"

    except Exception as e:
        user_histories[chat_id].pop()
        return f"❌ LỖI HỆ THỐNG/MẠNG:\n{str(e)}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id).strip()
    user_histories[chat_id] = []
    
    if chat_id == ADMIN_CHAT_ID:
        msg = (
            "Chào anh Tuấn! Em đã sẵn sàng kết nối Obsidian.\n\n"
            "📌 **Các lệnh dành riêng cho anh:**\n"
            "• `/note <nội dung>`: Lưu ý tưởng ngay vào kho Obsidian (Inbox)\n"
            "• `/model`: Xem model AI đang hoạt động\n"
            "• Hoặc nhắn trực tiếp để trò chuyện với trợ lý AI."
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    else:
        await update.message.reply_text("Xin chào! Mình là trợ lý của anh Tuấn. Mình có thể giúp gì cho bạn?")

async def note_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id).strip()
    
    # Kiểm tra phân quyền: Chỉ có Admin mới được lưu note vào Obsidian
    if chat_id != ADMIN_CHAT_ID:
        await update.message.reply_text("Bạn không có quyền sử dụng tính năng lưu ghi chú này.")
        return

    note_text = " ".join(context.args).strip()
    if not note_text:
        await update.message.reply_text("⚠️ Vui lòng nhập nội dung sau lệnh! Ví dụ:\n`/note Hôm nay học xong Git`", parse_mode="Markdown")
        return

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    success = await save_note_to_github(note_text)

    if success:
        await update.message.reply_text(f"✅ **Đã lưu vào Obsidian:**\n_{note_text}_", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Lưu vào GitHub thất bại. Vui lòng kiểm tra lại GITHUB_TOKEN hoặc quyền ghi repo trên Render.")

async def check_model_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    top_models = "\n".join([f"• `{m}`" for m in cached_models[:6]])
    msg = (
        f"⚡ **Model đang chạy cố định:** `{active_model_name}`\n\n"
        f"📋 **Danh sách dự phòng tức thì:**\n{top_models}"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    chat_id = str(update.effective_chat.id).strip()
    sender_name = update.message.from_user.full_name

    if ADMIN_CHAT_ID and chat_id != ADMIN_CHAT_ID:
        report_msg = f"🔔 **Có tin nhắn mới tới Bot!**\n👤 Từ: {sender_name}\n💬 Nội dung: {user_text}"
        asyncio.create_task(context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=report_msg))

    asyncio.create_task(context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING))

    try:
        reply_content = await asyncio.wait_for(call_gemini(chat_id, user_text), timeout=45.0)
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
    await refresh_models_from_google()

    telegram_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("note", note_cmd))
    telegram_app.add_handler(CommandHandler("model", check_model_cmd))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling(drop_pending_updates=True)

    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
