import logging
import os
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher
from aiogram.types import Message
from pyrogram import Client
import database as db

# 加载 .env 环境变量
load_dotenv()

# ================= 配置区 =================
# 安全获取环境变量，防止 .env 中留空导致 int("") 报错
api_id_raw = os.getenv("API_ID", "0").strip()
API_ID = int(api_id_raw) if api_id_raw.isdigit() else 0
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
# ==========================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# === 1. Aiogram 纯 Bot 引擎 (默认开启，负责干活) ===
if not BOT_TOKEN:
    raise ValueError("❌ 错误：未在 .env 中找到 BOT_TOKEN，请检查配置。")

aiogram_bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

@dp.channel_post()
async def handle_new_post(message: Message):
    source_id = message.chat.id
    target_id = await db.get_target_channel(source_id)
    if not target_id: return
    try:
        copied = await aiogram_bot.copy_message(chat_id=target_id, from_chat_id=source_id, message_id=message.message_id)
        await db.save_msg_mapping(source_id, message.message_id, copied.message_id)
        await db.add_log("SUCCESS", f"[实时同步] 成功: {source_id} -> {target_id}")
    except Exception as e:
        await db.add_log("ERROR", f"[实时同步] 失败 ID {message.message_id}: {e}")

@dp.edited_channel_post()
async def handle_edited_post(message: Message):
    source_id = message.chat.id
    target_id = await db.get_target_channel(source_id)
    if not target_id: return

    target_msg_id = await db.get_target_msg_id(source_id, message.message_id)
    if not target_msg_id: return

    try:
        if message.text:
            await aiogram_bot.edit_message_text(chat_id=target_id, message_id=target_msg_id, text=message.text, entities=message.entities)
        elif message.caption is not None:
            await aiogram_bot.edit_message_caption(chat_id=target_id, message_id=target_msg_id, caption=message.caption, caption_entities=message.caption_entities)
        await db.add_log("INFO", f"[修改同步] 已更新: {source_id} -> {target_id}")
    except Exception:
        pass


# === 2. Pyrofork 用户账号引擎 (按需开启，仅用于 API 拉取) ===
pyro_user_app = None

def init_user_client():
    global pyro_user_app
    if API_ID and API_HASH:
        pyro_user_app = Client("sync_user_session", api_id=API_ID, api_hash=API_HASH, ipv6=False)
        return pyro_user_app
    return None