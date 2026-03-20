import logging
import os
import asyncio
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher
from aiogram.types import Message
from aiogram.exceptions import TelegramRetryAfter
from pyrogram import Client
import database as db

load_dotenv()

api_id_raw = os.getenv("API_ID", "0").strip()
API_ID = int(api_id_raw) if api_id_raw.isdigit() else 0
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

if not BOT_TOKEN:
    raise ValueError("❌ 错误：未在 .env 中找到 BOT_TOKEN，请检查配置。")

aiogram_bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

@dp.channel_post()
async def handle_new_post(message: Message):
    source_id = message.chat.id
    target_id = await db.get_target_channel(source_id)
    if not target_id: return

    # --- 启动正则过滤系统 ---
    has_media = message.content_type in ['photo', 'video', 'document', 'audio', 'animation']
    file_name = ""
    if message.document: file_name = message.document.file_name or ""
    elif message.video: file_name = message.video.file_name or ""
    elif message.audio: file_name = message.audio.file_name or ""

    text_html = message.html_text if message.text or message.caption else ""
    should_skip, new_html = await db.apply_message_filters(text_html, has_media, file_name)
    
    if should_skip:
        await db.add_log("INFO", f"[过滤拦截] 触发媒体跳过规则，已丢弃消息 ID {message.message_id}")
        return

    # 若正则把纯文本全删光了，直接不发
    if not has_media and not new_html.strip():
        await db.add_log("INFO", f"[过滤拦截] 纯文本替换后为空，已丢弃消息 ID {message.message_id}")
        return

    max_retries = 3
    for attempt in range(max_retries):
        try:
            if new_html != text_html:
                # 文本被正则修改过，使用发送模式(保留HTML富文本格式)
                if not has_media:
                    copied = await aiogram_bot.send_message(target_id, text=new_html, parse_mode="HTML")
                else:
                    copied = await aiogram_bot.copy_message(chat_id=target_id, from_chat_id=source_id, message_id=message.message_id, caption=new_html, parse_mode="HTML")
            else:
                # 文本未变，完美硬拷贝
                copied = await aiogram_bot.copy_message(chat_id=target_id, from_chat_id=source_id, message_id=message.message_id)
            
            await db.save_msg_mapping(source_id, message.message_id, copied.message_id)
            await db.add_log("SUCCESS", f"[实时同步] 成功: {source_id} -> {target_id}")
            break
        except TelegramRetryAfter as e:
            await db.add_log("WARNING", f"[实时同步] 触发风控，等待 {e.retry_after} 秒后重试 (第 {attempt+1} 次)")
            await asyncio.sleep(e.retry_after)
        except Exception as e:
            await db.add_log("ERROR", f"[实时同步] 失败 ID {message.message_id}: {e}")
            break

@dp.edited_channel_post()
async def handle_edited_post(message: Message):
    source_id = message.chat.id
    target_id = await db.get_target_channel(source_id)
    if not target_id: return

    target_msg_id = await db.get_target_msg_id(source_id, message.message_id)
    if not target_msg_id: return

    # 修改动作同样受制于文本替换
    text_html = message.html_text if message.text or message.caption else ""
    has_media = message.content_type in ['photo', 'video', 'document', 'audio', 'animation']
    should_skip, new_html = await db.apply_message_filters(text_html, has_media, "")
    
    if should_skip: return # 如果被修改的内容触发了屏蔽词，不再进行修改

    try:
        if message.text:
            await aiogram_bot.edit_message_text(chat_id=target_id, message_id=target_msg_id, text=new_html, parse_mode="HTML")
        elif message.caption is not None:
            await aiogram_bot.edit_message_caption(chat_id=target_id, message_id=target_msg_id, caption=new_html, parse_mode="HTML")
        await db.add_log("INFO", f"[修改同步] 已更新: {source_id} -> {target_id}")
    except Exception:
        pass

pyro_user_app = None
def init_user_client():
    global pyro_user_app
    if API_ID and API_HASH:
        pyro_user_app = Client("sync_user_session", api_id=API_ID, api_hash=API_HASH, ipv6=False)
        return pyro_user_app
    return None