import os
import asyncio
import logging
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from google import genai
from google.genai import types
from google.genai.errors import APIError

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

# Đọc cấu hình từ biến môi trường của Render
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY:
    raise ValueError("Thiếu biến môi trường TELEGRAM_BOT_TOKEN hoặc GEMINI_API_KEY!")

client = genai.Client(api_key=GEMINI_API_KEY)


async def call_gemini(prompt: str) -> str:
    """Gọi Gemini từ máy chủ Cloud với độ trễ thấp."""
    models_to_try = ["gemini-3.8-flash", "gemini-2.5-flash"]
    config = types.GenerateContentConfig(
        max_output_tokens=1000,
        temperature=0.7,
    )

    for model_name in models_to_try:
        for attempt in range(2):
            try:
                response = await client.aio.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=config,
                )
                if response.text:
                    return response.text
            except APIError as e:
                if e.code in [503, 404] or "503" in str(e) or "404" in str(e):
                    await asyncio.sleep(1.5)
                    continue
                raise e
            except Exception:
                await asyncio.sleep(1.0)
                continue

    return "Máy chủ AI hiện đang bận, bạn vui lòng gửi lại sau ít giây nhé!"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name
    await update.message.reply_text(f"Xin chào {user_name}! Bot chạy trên Cloud đã sẵn sàng.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    chat_id = update.effective_chat.id

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    try:
        # Trên Cloud tốc độ mạng rất nhanh nên chỉ cần timeout 25s
        reply_content = await asyncio.wait_for(
            call_gemini(user_text),
            timeout=25.0,
        )
        await update.message.reply_text(reply_content)
    except asyncio.TimeoutError:
        await update.message.reply_text("Thời gian xử lý quá 25s, bạn hãy gửi lại nhé!")
    except Exception as e:
        await update.message.reply_text(f"Lỗi: {e}")


if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print(">>> BOT ĐANG HOẠT ĐỘNG TRÊN CLOUD RENDER...")
    app.run_polling(drop_pending_updates=True)
