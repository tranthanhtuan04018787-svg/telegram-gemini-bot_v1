import os
import asyncio
import logging
import base64
import aiohttp
import json
import re
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

# 1. CẤU HÌNH LOGGING
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)

# 2. KHAI BÁO BIẾN MÔI TRƯỜNG & KHẮC PHỤC LỖI TỰ ĐỘNG CHÈN LINK
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_CHAT_ID = str(os.getenv("ADMIN_CHAT_ID", "")).strip()
GITHUB_REPO = os.getenv("GITHUB_REPO", "tranthanhtuan04018787-svg/Brain-2").strip()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
SHEET_WEBHOOK_URL = os.getenv("SHEET_WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8080))

# Tách chuỗi URL để chống giao diện chat tự động chèn markdown gây lỗi cú pháp
GITHUB_API_BASE = "https" + "://api.github.com/repos"
GEMINI_API_BASE = "https" + "://generativelanguage.googleapis.com/v1beta"

if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY:
    raise ValueError("Thiếu biến môi trường TELEGRAM_BOT_TOKEN hoặc GEMINI_API_KEY!")

# 3. BIẾN TOÀN CỤC & PROMPT
user_histories = {}
MAX_HISTORY = 4
http_session: aiohttp.ClientSession = None
CURRENT_MODEL = None  # Biến lưu trữ model đang hoạt động

SYSTEM_PROMPT = """
Bạn là trợ lý ảo cá nhân của anh Tuấn. 
Nhiệm vụ của bạn là tiếp đón người thân, đối tác hoặc bạn bè của anh Tuấn một cách lịch sự, thân thiện. 
Khi giao tiếp, hãy xưng là "mình" hoặc "trợ lý của anh Tuấn" và gọi người chat là "bạn" hoặc "anh/chị". 
Nếu họ hỏi Tuấn đâu, hãy nói rằng anh Tuấn đang bận chút việc, bạn đã ghi nhận thông tin và anh Tuấn sẽ phản hồi lại sau.
Tuyệt đối không nhận mình là AI do Google tạo ra.
"""

# System Instruction chuyên biệt dành riêng cho việc xử lý / report Second Brain
SECOND_BRAIN_SYSTEM_INSTRUCTION = """
Bạn là một chuyên gia quản lý dữ liệu cá nhân (Second Brain) chuyên nghiệp trên Obsidian.
Nhiệm vụ của bạn là đọc các ghi chú thô, tóm tắt tổng quan và phân loại chúng vào các tệp mục tiêu (Cong_Viec.md, Y_Tuong.md, Kien_Thuc.md, Tai_Chinh.md, Khac.md).

QUY TẮC BỌC LINK [[ ]] NGHIÊM NGẶT:
- CHỈ BỌC LINK CHO: Tên người cụ thể, Tên phần mềm/công cụ (Obsidian, Python, Telegram...), Tên dự án, hoặc Khái niệm chuyên môn cốt lõi (AI, Regex, Automation...).
- TUYỆT ĐỐI KHÔNG BỌC LINK: Các đại từ, danh từ chung chung, hành động hàng ngày (ví dụ: công việc, hôm nay, ý tưởng, đi ăn, mua sắm, báo cáo).
- VÍ DỤ ĐÚNG: "Sửa lỗi parse entities cho [[bot Telegram]]"
- VÍ DỤ SAI (CẦN TRÁNH): "Hoàn thành [[công việc]] [[hôm nay]]" (Rác liên kết).

BẮT BUỘC TRẢ VỀ CHUẨN JSON, KHÔNG BỌC TRONG THẺ MARKDOWN ```json:
{
  "summary": "Nội dung tóm tắt ngắn gọn...",
  "categories": {
    "Cong_Viec.md": ["ghi chú 1 có chứa [[từ khóa]]", "ghi chú 2 bình thường"],
    "Y_Tuong.md": ["ghi chú 3 về [[chủ đề A]]"]
  }
}
"""

# 4. CÁC HÀM TIỆN ÍCH & TỰ ĐỘNG CHỌN MODEL
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

async def get_active_gemini_model() -> str:
    global CURRENT_MODEL
    if CURRENT_MODEL:
        return CURRENT_MODEL

    url = f"{GEMINI_API_BASE}/models?key={GEMINI_API_KEY}"
    try:
        session = await get_http_session()
        async with session.get(url, timeout=15) as resp:
            if resp.status == 200:
                data = await resp.json()
                valid_models = [m["name"] for m in data.get("models", []) if "generateContent" in m.get("supportedGenerationMethods", [])]
                flash_models = [m for m in valid_models if "flash" in m.lower()]
                
                if flash_models:
                    preferred = [m for m in flash_models if "3.1-flash-lite" in m]
                    CURRENT_MODEL = preferred[0] if preferred else flash_models[0]
                elif valid_models:
                    CURRENT_MODEL = valid_models[0]
                else:
                    CURRENT_MODEL = "models/gemini-1.5-flash"
                
                logging.info(f"✅ Đã tự động chọn Model: {CURRENT_MODEL}")
                return CURRENT_MODEL
            else:
                return "models/gemini-1.5-flash"
    except Exception as e:
        logging.error(f"Lỗi lấy danh sách model: {e}")
        return "models/gemini-1.5-flash"

# 5. CÁC HÀM XỬ LÝ GITHUB VÀ GOOGLE DRIVE
async def read_github_file(file_path: str) -> str:
    token = str(GITHUB_TOKEN or "").strip()
    repo = str(GITHUB_REPO or "").strip()
    session = await get_http_session()
    url = f"{GITHUB_API_BASE}/{repo}/contents/{file_path}"
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    
    try:
        async with session.get(url, headers=headers, timeout=15) as resp:
            if resp.status == 200:
                data = await resp.json()
                return base64.b64decode(data.get("content", "")).decode("utf-8", errors="ignore")
            return ""
    except Exception as e:
        logging.error(f"Lỗi đọc file {file_path}: {e}")
        return ""

async def overwrite_github_file(file_path: str, new_content: str, commit_msg: str) -> bool:
    token = str(GITHUB_TOKEN or "").strip()
    repo = str(GITHUB_REPO or "").strip()
    session = await get_http_session()
    url = f"{GITHUB_API_BASE}/{repo}/contents/{file_path}"
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}

    try:
        sha = None
        async with session.get(url, headers=headers, timeout=15) as resp:
            if resp.status == 200:
                data = await resp.json()
                sha = data.get("sha")

        payload = {
            "message": commit_msg,
            "content": base64.b64encode(new_content.encode("utf-8")).decode("utf-8")
        }
        if sha: payload["sha"] = sha

        async with session.put(url, headers=headers, json=payload, timeout=20) as put_resp:
            return put_resp.status in [200, 201]
    except Exception:
        return False

async def append_to_github_file(file_path: str, entry_text: str) -> bool:
    token = str(GITHUB_TOKEN or "").strip()
    repo = str(GITHUB_REPO or "").strip()
    session = await get_http_session()
    url = f"{GITHUB_API_BASE}/{repo}/contents/{file_path}"
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}

    try:
        existing_text = ""
        sha = None
        async with session.get(url, headers=headers, timeout=15) as resp:
            if resp.status == 200:
                data = await resp.json()
                sha = data.get("sha")
                existing_text = base64.b64decode(data.get("content", "")).decode("utf-8", errors="ignore")
            elif resp.status == 404:
                existing_text = f"# 📂 {file_path.replace('.md', '')}\n\n"

        new_text = existing_text + entry_text
        payload = {
            "message": f"update {file_path}: {get_vn_time()}",
            "content": base64.b64encode(new_text.encode("utf-8")).decode("utf-8")
        }
        if sha: payload["sha"] = sha

        async with session.put(url, headers=headers, json=payload, timeout=20) as put_resp:
            return put_resp.status in [200, 201]
    except Exception:
        return False

async def upload_file_to_github_attachments(file_bytes: bytes, file_name: str) -> tuple[bool, str]:
    token = str(GITHUB_TOKEN or "").strip()
    repo = str(GITHUB_REPO or "").strip()
    session = await get_http_session()
    url = f"{GITHUB_API_BASE}/{repo}/contents/attachments/{file_name}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Telegram-Obsidian-Bot",
    }
    encoded_content = base64.b64encode(file_bytes).decode("utf-8")
    payload = {"message": f"upload attachment: {file_name}", "content": encoded_content}

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
    if not SHEET_WEBHOOK_URL:
        return False, "Chưa cấu hình biến SHEET_WEBHOOK_URL trên Render!"

    session = await get_http_session()
    base64_data = base64.b64encode(file_bytes).decode("utf-8")
    payload = {
        "action": "upload_drive", "file_name": file_name,
        "file_data": base64_data, "time": get_vn_time(),
    }

    try:
        async with session.post(SHEET_WEBHOOK_URL, json=payload, timeout=60) as resp:
            if resp.status == 200:
                data = await resp.json()
                drive_link = data.get("file_url") or data.get("url", "")
                if drive_link: return True, drive_link
                return False, "Webhook không trả về URL file"
            else:
                return False, f"Webhook Drive báo lỗi ({resp.status})"
    except Exception as e:
        return False, f"Lỗi ngoại lệ Drive: {str(e)}"

# 6. CÁC HÀM XỬ LÝ GEMINI VÀ LOG
async def log_to_sheet(chat_id: str, sender_name: str, user_text: str, bot_reply: str):
    if not SHEET_WEBHOOK_URL: return
    payload = {
        "time": get_vn_time(), "chat_id": chat_id, "sender_name": sender_name,
        "user_text": user_text, "bot_reply": bot_reply,
    }
    try:
        session = await get_http_session()
        async with session.post(SHEET_WEBHOOK_URL, json=payload, timeout=15) as resp:
            await resp.text()
    except Exception as e:
        logging.error(f"Lỗi ghi Google Sheet: {e}")

async def call_gemini(chat_id: str, user_text: str) -> str:
    global CURRENT_MODEL
    if chat_id not in user_histories: user_histories[chat_id] = []
    user_histories[chat_id].append({"role": "user
