import logging
import os
import asyncio
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import Message
from aiogram.exceptions import TelegramRetryAfter
from pyrogram import Client
import database as db

load_dotenv()

API_ID = int(os.getenv("API_ID", "0").strip() or 0)
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
if not BOT_TOKEN: raise ValueError("❌ 错误：未在 .env 中找到 BOT_TOKEN。")

session = AiohttpSession(timeout=3600)
aiogram_bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher()
media_group_cache = {}

def get_chat_name(chat): return f"@{chat.username}" if chat.username else (chat.title or str(chat.id))

# ================= 核心：数据驱动解耦 =================
MSG_TYPES = ['photo', 'video', 'animation', 'audio', 'voice', 'sticker', 'document']
def get_msg_type(msg: Message) -> str:
    return next((t for t in MSG_TYPES if getattr(msg, t, None)), 'text')

async def is_type_allowed(msg_type: str) -> bool:
    settings = await db.get_all_settings()
    key_map = {t: f'sync_{t}' for t in MSG_TYPES}
    key_map['animation'] = 'sync_gif'
    return settings.get(key_map.get(msg_type, 'sync_text'), "1") == "1"

@dp.channel_post()
async def handle_new_post(message: Message):
    source_id = message.chat.id
    target_id = await db.get_target_channel(source_id)
    if not target_id: return

    chat_name = get_chat_name(message.chat)
    msg_type = get_msg_type(message)

    if not await is_type_allowed(msg_type):
        await db.add_msg_log("DROP_TYPE", f"源: [{chat_name}] ID:{message.message_id} | 拦截类型: {msg_type.upper()}")
        return

    if message.media_group_id:
        mg_id = message.media_group_id
        if mg_id not in media_group_cache:
            media_group_cache[mg_id] = [message]
            await asyncio.sleep(2)
            if mg_id in media_group_cache:
                group = sorted(media_group_cache.pop(mg_id), key=lambda m: m.message_id)
                for m in group:
                    t_html = m.html_text if m.text or m.caption else ""
                    f_name = m.document.file_name if m.document else (m.video.file_name if m.video else "")
                    s_skip, _ = await db.apply_message_filters(t_html, True, f_name or "")
                    if s_skip:
                        await db.add_msg_log("DROP_REGEX", f"源: [{chat_name}] 组IDs:{[m.message_id for m in group]} | 命中正则")
                        return

                msg_ids = [m.message_id for m in group]
                await db.add_msg_log("RECV_GROUP", f"源: [{chat_name}] 组IDs:{msg_ids} | 接收相册")
                try:
                    copied_ids = await aiogram_bot.copy_messages(chat_id=target_id, from_chat_id=source_id, message_ids=msg_ids)
                    for orig_m, new_m in zip(group, copied_ids): await db.save_msg_mapping(source_id, orig_m.message_id, new_m.message_id)
                    await db.add_msg_log("SEND_GROUP", f"目标: [{target_id}] | 相册转发成功")
                except Exception as e:
                    await db.add_msg_log("WARN", f"相册转发失败，降级单条拆散")
                    for m in group:
                        try:
                            copied = await aiogram_bot.copy_message(chat_id=target_id, from_chat_id=source_id, message_id=m.message_id)
                            await db.save_msg_mapping(source_id, m.message_id, copied.message_id)
                            await asyncio.sleep(1)
                        except: pass
        else: media_group_cache[mg_id].append(message)
        return

    has_media = msg_type != 'text'
    file_name = getattr(getattr(message, msg_type, None), 'file_name', "") if msg_type in ['document', 'video'] else ""
    text_html = message.html_text if message.text or message.caption else ""
    
    await db.add_msg_log("RECV", f"源: [{chat_name}] ID:{message.message_id} | 类型:{msg_type.upper()}")

    should_skip, new_html = await db.apply_message_filters(text_html, has_media, file_name)
    if should_skip or (not has_media and not new_html.strip()):
        await db.add_msg_log("DROP_REGEX", f"源: [{chat_name}] ID:{message.message_id} | 被正则或空文本拦截")
        return

    try:
        if new_html != text_html:
            kwargs = {"chat_id": target_id, "parse_mode": "HTML"}
            if not has_media: kwargs["text"] = new_html
            else: kwargs.update({"from_chat_id": source_id, "message_id": message.message_id, "caption": new_html})
            copied = await (aiogram_bot.send_message(**kwargs) if not has_media else aiogram_bot.copy_message(**kwargs))
        else:
            copied = await aiogram_bot.copy_message(chat_id=target_id, from_chat_id=source_id, message_id=message.message_id)
        
        await db.save_msg_mapping(source_id, message.message_id, copied.message_id)
        await db.add_msg_log("SEND", f"目标: [{target_id}] 新ID:{copied.message_id} | 转发成功")
    except Exception as e:
        await db.add_msg_log("ERROR", f"发送失败 ID:{message.message_id} | {e}")

@dp.edited_channel_post()
async def handle_edited_post(message: Message):
    source_id, msg_id = message.chat.id, message.message_id
    target_id = await db.get_target_channel(source_id)
    target_msg_id = await db.get_target_msg_id(source_id, msg_id) if target_id else None
    if not target_msg_id: return

    has_media = get_msg_type(message) != 'text'
    should_skip, new_html = await db.apply_message_filters(message.html_text if message.text or message.caption else "", has_media, "")
    if should_skip: return 

    try:
        kwargs = {"chat_id": target_id, "message_id": target_msg_id, "parse_mode": "HTML"}
        if message.text: await aiogram_bot.edit_message_text(text=new_html, **kwargs)
        else: await aiogram_bot.edit_message_caption(caption=new_html, **kwargs)
        await db.add_msg_log("EDIT", f"同步修改 源ID:{msg_id} -> 目标ID:{target_msg_id}")
    except Exception: pass

pyro_user_app = None
def init_user_client():
    global pyro_user_app
    if API_ID and API_HASH: pyro_user_app = Client("sync_user_session", api_id=API_ID, api_hash=API_HASH, ipv6=False)