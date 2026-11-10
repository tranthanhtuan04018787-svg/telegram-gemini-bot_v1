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
