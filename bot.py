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
GITHUB_REPO = os.getenv("GITHUB_REPO", "tranthanhtuan04018787-svg/Brain-2").strip()
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

def get_timestamp_id() -> str:
    now = datetime.now(timezone.utc) + timedelta(hours=7)
    return now.strftime("%Y%m%d_%H%M%S")

async def get_http_session() -> aiohttp.ClientSession:
    global http_session
    if http_session is None or http_session.closed:
        connector = aiohttp.TCPConnector(limit=50, keepalive_timeout=60)
        http_session = aiohttp.ClientSession(connector=connector)
    return http_session

async def append_to_input_md(entry_text: str) -> tuple[bool, str]:
    """Ghi nối tiếp văn bản vào file INPUT.md trên GitHub."""
    token = str(GITHUB_TOKEN or "").strip()
    repo = str(GITHUB_REPO or "").strip()
    session = await get_http_session()
    
    file_path = "INPUT.md"
    url = f"https://api.github.com/repos/{repo}/contents/{file_path}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Telegram-Obsidian-Bot",
    }

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
                existing_text = "# 📥 Hộp thư đến (INPUT)\n\n"
            else:
                err_body = await resp.text()
                return False, f"Lỗi GET INPUT.md ({resp.status}): {err_body[:120]}"

        new_text = existing_text + entry_text
        encoded_content = base64.b64encode(new_text.encode("utf-8")).decode("utf-8")

        payload = {
            "message": f"update input: {get_vn_time()}",
            "content": encoded_content,
        }
        if sha:
            payload["sha"] = sha

        async with session.put(url, headers=headers, json=payload, timeout=20) as put_resp:
            if put_resp.status in [200, 201]:
                return True, "Thành công"
            else:
                err_body = await put_resp.text()
                return False, f"Lỗi PUT INPUT.md ({put_resp.status}): {err_body[:120]}"
    except Exception as e:
        return False, f"Lỗi ngoại lệ INPUT.md: {str(e)}"

async def upload_file_to_github_attachments(file_bytes: bytes, file_name: str) -> tuple[bool, str]:
    """Tải file nhị phân (ảnh, tài liệu nhỏ) vào thư mục attachments/ trên GitHub."""
    token = str(GITHUB_TOKEN or "").strip()
    repo = str(GITHUB_REPO or "").strip()
    session = await get_http_session()

    url = f"https://api.github.com/repos/{repo}/contents/attachments/{file_name}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Telegram-Obsidian-Bot",
    }

    encoded_content = base64.b64encode(file_bytes).decode("utf-8")
    payload = {
        "message": f"upload attachment: {file_name}",
        "content": encoded_content,
    }

    try:
        async with session.put(url, headers=headers, json=payload, timeout=30) as resp:
            if resp.status in [200, 201]:
                return True, file_name
            else:
                err_body = await resp.text()
                return False, f"Lỗi tải attachments ({resp.status}): {err_body[:120]}"
    except Exception as e:
        return False, f"Lỗi ngoại lệ upload: {str(e)}"

async def upload_to_drive_webhook(file_bytes: bytes, file_name: str) -> tuple[bool, str]:
    """Gửi file lớn lên Google Drive qua Google Apps Script Webhook."""
    if not SHEET_WEBHOOK_URL:
        return False, "Chưa cấu hình biến SHEET_WEBHOOK_URL trên Render!"

    session = await get_http_session()
    base64_data = base64.b64encode(file_bytes).decode("utf-8")

    payload = {
        "action": "upload_drive",
        "file_name": file_name,
        "file_data": base64_data,
        "time": get_vn_time(),
    }

    try:
        async with session.post(SHEET_WEBHOOK_URL, json=payload, timeout=60) as resp:
            if resp.status == 200:
                data = await resp.json()
                drive_link = data.get("file_url") or data.get("url", "")
                if drive_link:
                    return True, drive_link
                return False, "Webhook không trả về URL file"
            else:
                return False, f"Webhook Drive báo lỗi ({resp.status})"
    except Exception as e:
        return False, f"Lỗi ngoại lệ Drive: {str(e)}"

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
            "Chào anh Tuấn! Trợ lý Obsidian đã sẵn sàng:\n\n"
            "📌 **Các cách lưu dữ liệu:**\n"
            "• `/note <nội dung>`: Lưu nhanh ghi chú văn bản vào INPUT.md\n"
            "• **Gửi ảnh trực tiếp**: Tự lưu vào attachments/ và nhúng thẻ hiển thị ảnh\n"
            "• **Gửi video/tệp (< 15MB)**: Tự động lưu vào GitHub Vault\n"
            "• **Gửi file lớn
