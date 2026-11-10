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
                        if not any(
