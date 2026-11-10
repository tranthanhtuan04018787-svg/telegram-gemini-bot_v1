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

# 2. KHAI BÁO BIẾN MÔI TRƯỜNG
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_CHAT_ID = str(os.getenv("ADMIN_CHAT_ID", "")).strip()
GITHUB_REPO = os.getenv("GITHUB_REPO", "tranthanhtuan04018787-svg/Brain-2").strip()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
SHEET_WEBHOOK_URL = os.getenv("SHEET_WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8080))

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
    """Tự động quét và chọn model Gemini khả dụng nhất từ Google API."""
    global CURRENT_MODEL
    if CURRENT_MODEL:
        return CURRENT_MODEL

    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_API_KEY}"
    try:
        session = await get_http_session()
        async with session.get(url, timeout=15) as resp:
            if resp.status == 200:
                data = await resp.json()
                # Lọc các model hỗ trợ tạo văn bản
                valid_models = [m["name"] for m in data.get("models", []) if "generateContent" in m.get("supportedGenerationMethods", [])]
                
                # Ưu tiên các dòng flash hoặc flash-lite nhẹ và nhanh
                flash_models = [m for m in valid_models if "flash" in m.lower()]
                
                if flash_models:
                    # Ưu tiên bản lite-preview nếu có để tránh kẹt mạng
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
    url = f"https://api.github.com/repos/{repo}/contents/{file_path}"
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

async def append_to_github_file(file_path: str, entry_text: str) -> bool:
    token = str(GITHUB_TOKEN or "").strip()
    repo = str(GITHUB_REPO or "").strip()
    session = await get_http_session()
    url = f"https://api.github.com/repos/{repo}/contents/{file_path}"
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

    url = f"https://api.github.com/repos/{repo}/contents/attachments/{file_name}"
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
    user_histories[chat_id].append({"role": "user", "parts": [{"text": user_text}]})
    if len(user_histories[chat_id]) > MAX_HISTORY: user_histories[chat_id] = user_histories[chat_id][-MAX_HISTORY:]

    model_name = await get_active_gemini_model()
    url = f"https://generativelanguage.googleapis.com/v1beta/{model_name}:generateContent?key={GEMINI_API_KEY}"
    
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
            elif resp.status == 503:
                CURRENT_MODEL = None # Xóa model hiện tại để lần sau quét lại
                return "⚠️ AI hiện đang quá tải (Lỗi 503). Vui lòng thử lại sau ít phút!"
            else:
                err = await resp.text()
                user_histories[chat_id].pop()
                return f"⚠️ Lỗi AI ({resp.status}): {err[:200]}"
    except Exception as e:
        user_histories[chat_id].pop()
        return f"❌ Lỗi mạng: {str(e)}"

# 7. CÁC LỆNH (COMMANDS)
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id).strip()
    user_histories[chat_id] = []
    
    if chat_id == ADMIN_CHAT_ID:
        msg = (
            "Chào anh Tuấn! Trợ lý Obsidian đã sẵn sàng:\n\n"
            "📌 **Các cách lưu dữ liệu:**\n"
            "• `/note <nội dung>`: Lưu nhanh ghi chú vào INPUT_BOT.md\n"
            "• `/report <số ngày>`: AI tổng hợp và phân loại ghi chú\n"
            "• **Gửi ảnh trực tiếp**: Tự lưu vào attachments/\n"
            "• **Gửi video/tệp (< 15MB)**: Tự động lưu vào GitHub Vault\n"
            "• **Gửi file lớn (> 15MB)**: Tự đẩy lên Google Drive"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    else:
        await update.message.reply_text("Xin chào! Mình là trợ lý của anh Tuấn. Mình có thể giúp gì cho bạn?")

async def note_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id).strip()
    if chat_id != ADMIN_CHAT_ID: return

    note_text = " ".join(context.args).strip()
    if not note_text:
        await update.message.reply_text("⚠️ Vui lòng nhập nội dung! Ví dụ: `/note Ý tưởng mới`", parse_mode="Markdown")
        return

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    timestamp = get_vn_time()
    formatted_entry = f"\n- **[{timestamp}]**: {note_text}\n"

    ok = await append_to_github_file("INPUT_BOT.md", formatted_entry)
    if ok:
        await update.message.reply_text(f"✅ **Đã lưu vào INPUT_BOT.md:**\n_{note_text}_", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ **Lỗi lưu file lên GitHub**", parse_mode="Markdown")

async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CURRENT_MODEL
    chat_id = str(update.effective_chat.id).strip()
    if chat_id != ADMIN_CHAT_ID: return

    days = 1
    if context.args and context.args[0].isdigit():
        days = int(context.args[0])

    await update.message.reply_text(f"⏳ AI đang đọc và phân tích dữ liệu {days} ngày qua. Vui lòng đợi...", parse_mode="Markdown")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    # 1. Đọc file
    raw_content = await read_github_file("INPUT_BOT.md")
    if not raw_content:
        await update.message.reply_text("❌ Không thể đọc file INPUT_BOT.md hoặc file trống.")
        return

# 2. Lọc ghi chú (Khắc phục triệt để lỗi lệch múi giờ Python)
    # Lấy giờ VN hiện tại, xóa bỏ hệ tham chiếu múi giờ để Python không tự trừ đi 7 tiếng
    now_vn = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=7)
    
    # Lùi lại số ngày và đưa về 00:00:00 để quét trọn vẹn tất cả ghi chú của ngày hôm đó
    cutoff_date = (now_vn - timedelta(days=days)).replace(hour=0, minute=0, second=0)

    # Chẻ chuỗi và "bắt" luôn cả Ngày lẫn Giờ (thay vì chỉ bắt ngày như trước)
    parts = re.split(r'\[(\d{2}/\d{2}/\d{4}\s\d{2}:\d{2}:\d{2})\]', raw_content)

    filtered_notes = []
    for i in range(1, len(parts), 2):
        date_str = parts[i]
        content = parts[i+1]
        try:
            # So sánh chính xác thời gian (không bị ảnh hưởng bởi múi giờ)
            note_date = datetime.strptime(date_str, "%d/%m/%Y %H:%M:%S")
            if note_date >= cutoff_date:
                # Dọn sạch các ký tự rác (*, :, -, chấm tròn, khoảng trắng)
                clean_content = re.sub(r"^[\*\s:\-•]+", "", content).strip()
                if clean_content:
                    filtered_notes.append(f"- {clean_content}")
        except Exception:
            continue

    if not filtered_notes:
        await update.message.reply_text(f"⚠️ Không tìm thấy ghi chú nào từ {cutoff_date.strftime('%d/%m/%Y')}.")
        return

    notes_text = "\n".join(filtered_notes)-
    # 3. Gửi cho Gemini xử lý
    ai_prompt = f"""
    Bạn là trợ lý ảo quản lý dữ liệu. Dưới đây là các ghi chú tôi thu thập được trong {days} ngày qua:
    {notes_text}

    Hãy làm 2 việc:
    1. Viết 1 đoạn tóm tắt tổng quan về những nội dung này.
    2. Phân loại các ghi chú vào các file: Cong_Viec.md, Y_Tuong.md, Kien_Thuc.md, Tai_Chinh.md, Khac.md.

    BẮT BUỘC TRẢ VỀ CHUẨN JSON, KHÔNG BỌC TRONG THẺ ```json:
    {{
      "summary": "Nội dung tóm tắt...",
      "categories": {{
        "Cong_Viec.md": ["ghi chú 1", "ghi chú 2"],
        "Y_Tuong.md": ["ghi chú 3"]
      }}
    }}
    """
    
    model_name = await get_active_gemini_model()
    url = f"[https://generativelanguage.googleapis.com/v1beta/](https://generativelanguage.googleapis.com/v1beta/){model_name}:generateContent?key={GEMINI_API_KEY}"
    
    payload = {
        "contents": [{"role": "user", "parts": [{"text": ai_prompt}]}],
        "generationConfig": {"temperature": 0.2}
    }

    try:
        session = await get_http_session()
        async with session.post(url, json=payload, timeout=45) as resp:
            if resp.status == 200:
                data = await resp.json()
                ai_response = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                
                if ai_response.startswith("```json"): ai_response = ai_response[7:]
                if ai_response.startswith("```"): ai_response = ai_response[3:]
                if ai_response.endswith("```"): ai_response = ai_response[:-3]

                parsed_json = json.loads(ai_response.strip())
                summary = parsed_json.get("summary", "Không có tóm tắt.")
                categories = parsed_json.get("categories", {})

               # 4. Lưu Báo cáo tổng hợp
                report_text = f"\n\n## Báo cáo {days} ngày qua (Tạo ngày {get_vn_time()})\n**Tóm tắt:** {summary}\n"
                await append_to_github_file("Bao_Cao_Tong_Hop.md", report_text)

                # 5. Phân phối
                distribution_msg = f"Đã phân tích Báo cáo {days} ngày!\n\nTóm tắt: {summary}\n\nPhân bổ:\n"
                for cat_file, items in categories.items():
                    if items:
                        formatted_items = "\n".join([f"- [ ] {item}" for item in items])
                        await append_to_github_file(cat_file, f"\n\n### Từ INPUT_BOT ngày {get_vn_time()}:\n{formatted_items}\n")
                        distribution_msg += f"- {cat_file}: {len(items)} mục\n"

                # GỬI TIN NHẮN KHÔNG CÓ PARSE_MODE ĐỂ TRÁNH LỖI "Can't parse entities"
                await update.message.reply_text(distribution_msg) 
            elif resp.status == 503:
                CURRENT_MODEL = None
                await update.message.reply_text("⚠️ API đang quá tải (503). Vui lòng thử lại sau.")
            else:
                err = await resp.text()
                await update.message.reply_text(f"❌ Lỗi xử lý AI: {err[:150]}")
    except json.JSONDecodeError:
         await update.message.reply_text("❌ Lỗi: AI không trả về đúng form JSON.")
    except Exception as e:
        await update.message.reply_text(f"❌ Đã xảy ra lỗi: {str(e)}")

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id).strip()
    if chat_id != ADMIN_CHAT_ID: return

    caption = update.message.caption or ""
    timestamp = get_vn_time()
    time_id = get_timestamp_id()
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)

    if update.message.photo:
        photo = update.message.photo[-1]
        tg_file = await context.bot.get_file(photo.file_id)
        file_bytes = await tg_file.download_as_bytearray()
        
        file_name = f"image_{time_id}.jpg"
        ok, res = await upload_file_to_github_attachments(file_bytes, file_name)
        if ok:
            cap_text = f": {caption}" if caption else ""
            entry = f"\n- **[{timestamp}]**{cap_text}\n  ![[{file_name}]]\n"
            await append_to_github_file("INPUT_BOT.md", entry)
            await update.message.reply_text(f"✅ **Đã lưu ảnh vào attachments:**\n`![[{file_name}]]`", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"❌ **Lỗi lưu ảnh:** `{res}`", parse_mode="Markdown")
        return

    media = update.message.video or update.message.document
    if media:
        orig_name = getattr(media, "file_name", f"file_{time_id}.dat")
        file_name = f"{time_id}_{orig_name}"
        size_mb = media.file_size / (1024 * 1024)

        tg_file = await context.bot.get_file(media.file_id)
        file_bytes = await tg_file.download_as_bytearray()
        cap_text = f": {caption}" if caption else ""

        if size_mb <= 15:
            ok, res = await upload_file_to_github_attachments(file_bytes, file_name)
            if ok:
                entry = f"\n- **[{timestamp}]**{cap_text} (File: {orig_name})\n  [[{file_name}]]\n"
                await append_to_github_file("INPUT_BOT.md", entry)
                await update.message.reply_text(f"✅ **Đã lưu file ({size_mb:.1f}MB):**\n`[[{file_name}]]`", parse_mode="Markdown")
        else:
            ok, link_or_err = await upload_to_drive_webhook(file_bytes, file_name)
            if ok:
                entry = f"\n- **[{timestamp}]**{cap_text}\n  - 🔗 [Xem file trên Drive: {orig_name}]({link_or_err})\n"
                await append_to_github_file("INPUT_BOT.md", entry)
                await update.message.reply_text(f"☁️ **Đã lưu file lớn ({size_mb:.1f}MB) lên Drive:**\n[Mở Drive]({link_or_err})", parse_mode="Markdown")

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

# 8. MÁY CHỦ ẢO CHO RENDER VÀ HÀM CHÍNH
async def handle_ping(request): return web.Response(text="Bot is live!")

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
    telegram_app.add_handler(CommandHandler("report", report_cmd)) # Lệnh Report bằng AI
    
    media_filters = filters.PHOTO | filters.VIDEO | filters.Document.ALL
    telegram_app.add_handler(MessageHandler(media_filters, handle_media))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling(drop_pending_updates=True)

    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
