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

async def get_http_session() -> aiohttp.ClientSession:
    global http_session
    if http_session is None or http_session.closed:
        connector = aiohttp.TCPConnector(limit=50, keepalive_timeout=60)
        http_session = aiohttp.ClientSession(connector=connector)
    return http_session

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
    """Lưu ghi chú trực tiếp vào file Inbox.md của Vault Obsidian."""
    token = str(GITHUB_TOKEN or "").strip()
    repo = str(GITHUB_REPO or "").strip()

    if not token:
        return False, "Thiếu biến GITHUB_TOKEN trên Render!"
    if not repo:
        return False, "Thiếu biến GITHUB_REPO trên Render!"

    session = await get_http_session()
    file_path = "Inbox.md"
    url = f"https://api.github.com/repos/{repo}/contents/{file_path}"
    
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Telegram-Obsidian-Bot",
    }

    timestamp = get_vn_time()
    formatted_entry = f"\n- **[{timestamp}]**: {note_content}\n"

    try:
        sha = None
        existing_text = ""
        
        async with session.get(url, headers=headers, timeout=15) as resp:
            if resp.status == 200:
                file_info = await resp.json()
                sha = file_info.get("sha")
                raw_bytes = base64.b64decode(file_info.get("content", ""))
                existing_text = raw_bytes.decode("utf-8", errors="ignore")
            elif resp.status == 404:
                existing_text = "# 📥 Hộp thư đến (Inbox)\n\n"
            else:
                err_body = await resp.text()
                return False, f"Lỗi GET ({resp.status}): {err_body[:150]}"

        new_text = existing_text + formatted_entry
        encoded_content = base64.b64encode(new_text.encode("utf-8")).decode("utf-8")

        payload = {
            "message": f"note: {timestamp}",
            "content": encoded_content,
        }
        if sha:
            payload["sha"] = sha

        async with session.put(url, headers=headers, json=payload, timeout=20) as put_resp:
            if put_resp.status in [200, 201]:
                return True, "Thành công"
            else:
                err_body = await put_resp.text()
                return False, f"Lỗi PUT ({put_resp.status}): {err_body[:150]}"
    except Exception as e:
        return False, f"Ngoại lệ kết nối: {str(e)}"

async def call_gemini(chat_id: str, user_text: str) -> str:
    if chat_id not in user_histories:
        user_histories[chat_id] = []

    user_histories[chat_id].append({"role": "user", "parts": [{"text": user_text}]})
    if len(user_histories[chat_id]) > MAX_HISTORY:
        user_histories[chat_id] = user_histories[chat_id][-MAX_HISTORY:]

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    dynamic_prompt = SYSTEM_PROMPT + f"\n\n[Thời gian]: {get_vn_time()} (Giờ VN)."

    payload = {
        "systemInstruction": {"parts": [{"text": dynamic_prompt}]},
        "contents": user_histories[chat_id],
        "generationConfig": {"maxOutputTokens": 600, "temperature": 0.7},
    }

    try:
        session = await get_http_session()
        async with session.post(url, headers=headers, json=payload, timeout=30) as resp:
            if resp.status == 200:
                data = await resp.json()
                bot_reply = data["candidates"][0]["content"]["parts"][0]["text"]
                user_histories[chat_id].append({"role": "model", "parts": [{"text": bot_reply}]})
                return bot_reply
            else:
                err = await resp.text()
                user_histories[chat_id].pop()
                return f"⚠️ Lỗi AI ({resp.status}): {err[:200]}"
    except Exception as e:
        user_histories[chat_id].pop()
        return f"❌ Lỗi mạng: {str(e)}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id).strip()
    user_histories[chat_id] = []
    
    if chat_id == ADMIN_CHAT_ID:
        msg = (
            "Chào anh Tuấn! Em đã sẵn sàng kết nối Obsidian.\n\n"
            "📌 **Lệnh sử dụng:**\n"
            "• `/note <nội dung>`: Lưu ý tưởng ngay vào kho Obsidian (Inbox)\n"
            "• Nhắn tin trực tiếp để trò chuyện cùng trợ lý AI."
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    else:
        await update.message.reply_text("Xin chào! Mình là trợ lý của anh Tuấn. Mình có thể giúp gì cho bạn?")

async def note_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id).strip()
    
    if chat_id != ADMIN_CHAT_ID:
        await update.message.reply_text("Bạn không có quyền sử dụng tính năng lưu ghi chú này.")
        return

    note_text = " ".join(context.args).strip()
    if not note_text:
        await update.message.reply_text("⚠️ Nhập nội dung sau lệnh! Ví dụ: `/note Ý tưởng mới`", parse_mode="Markdown")
        return

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    ok, debug_info = await save_note_to_github(note_text)

    if ok:
        await update.message.reply_text(f"✅ **Đã lưu vào Obsidian:**\n_{note_text}_", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ **Lỗi chi tiết từ GitHub:**\n`{debug_info}`", parse_mode="Markdown")

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
    except Exception as e:
        await update.message.reply_text(f"Đã có lỗi xảy ra: {str(e)}")

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
    telegram_app.add_handler(CommandHandler("note", note_cmd))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling(drop_pending_updates=True)

    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
