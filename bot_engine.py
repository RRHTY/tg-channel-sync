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

if not BOT_TOKEN: raise ValueError("❌ 错误：未在 .env 中找到 BOT_TOKEN，请检查配置。")

aiogram_bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
media_group_cache = {}

def get_chat_name(chat):
    if chat.username: return f"@{chat.username}"
    return chat.title or str(chat.id)

def get_msg_type(msg: Message) -> str:
    if msg.photo: return 'photo'
    if msg.video: return 'video'
    if msg.animation: return 'gif'
    if msg.audio: return 'audio'
    if msg.voice: return 'voice'
    if msg.sticker: return 'sticker'
    if msg.document: return 'document'
    return 'text'

async def is_type_allowed(msg_type: str) -> bool:
    settings = await db.get_all_settings()
    key_map = {'photo':'sync_photo', 'video':'sync_video', 'gif':'sync_gif', 
               'audio':'sync_audio', 'voice':'sync_voice', 'sticker':'sync_sticker', 
               'document':'sync_document', 'text':'sync_text'}
    return settings.get(key_map.get(msg_type, 'sync_text'), "1") == "1"

@dp.channel_post()
async def handle_new_post(message: Message):
    source_id = message.chat.id
    target_id = await db.get_target_channel(source_id)
    if not target_id: return

    chat_name = get_chat_name(message.chat)
    msg_type = get_msg_type(message)

    # 全局类型拦截
    if not await is_type_allowed(msg_type):
        await db.add_msg_log("DROP_TYPE", f"源: [{chat_name}] ID:{message.message_id} | 拦截类型: {msg_type.upper()}")
        return

    # ================= 媒体组处理 =================
    if message.media_group_id:
        mg_id = message.media_group_id
        if mg_id not in media_group_cache:
            media_group_cache[mg_id] = [message]
            await asyncio.sleep(2)
            if mg_id in media_group_cache:
                group = media_group_cache.pop(mg_id)
                group.sort(key=lambda m: m.message_id) 
                
                should_skip_group = False
                for m in group:
                    t_html = m.html_text if m.text or m.caption else ""
                    f_name = m.document.file_name if m.document else (m.video.file_name if m.video else "")
                    s_skip, _ = await db.apply_message_filters(t_html, True, f_name or "")
                    if s_skip:
                        should_skip_group = True; break
                
                msg_ids = [m.message_id for m in group]
                if should_skip_group:
                    await db.add_msg_log("DROP_REGEX", f"源: [{chat_name}] 组IDs:{msg_ids} | 命中正则屏蔽")
                    return

                await db.add_msg_log("RECV_GROUP", f"源: [{chat_name}] 组IDs:{msg_ids} | 接收相册打包")
                success = False
                for attempt in range(3):
                    try:
                        copied_ids = await aiogram_bot.copy_messages(chat_id=target_id, from_chat_id=source_id, message_ids=msg_ids)
                        for orig_m, new_m in zip(group, copied_ids): await db.save_msg_mapping(source_id, orig_m.message_id, new_m.message_id)
                        await db.add_msg_log("SEND_GROUP", f"目标: [{target_id}] 组IDs:{[m.message_id for m in copied_ids]} | 相册转发成功")
                        success = True; break
                    except TelegramRetryAfter as e: await asyncio.sleep(e.retry_after)
                    except Exception as e: break 
                        
                if not success:
                    await db.add_msg_log("WARN", f"源: [{chat_name}] | 相册转发失败，降级单条拆散")
                    for m in group:
                        try:
                            copied = await aiogram_bot.copy_message(chat_id=target_id, from_chat_id=source_id, message_id=m.message_id)
                            await db.save_msg_mapping(source_id, m.message_id, copied.message_id)
                            await asyncio.sleep(1)
                        except Exception: pass
        else: media_group_cache[mg_id].append(message)
        return

    # ================= 单条消息处理 =================
    has_media = message.content_type in ['photo', 'video', 'document', 'audio', 'voice', 'animation']
    file_name = message.document.file_name if message.document else (message.video.file_name if message.video else "")
    text_html = message.html_text if message.text or message.caption else ""
    
    await db.add_msg_log("RECV", f"源: [{chat_name}] ID:{message.message_id} | 类型:{msg_type.upper()} | 内容:{text_html[:30]}")

    should_skip, new_html = await db.apply_message_filters(text_html, has_media, file_name or "")
    if should_skip:
        await db.add_msg_log("DROP_REGEX", f"源: [{chat_name}] ID:{message.message_id} | 命中正则屏蔽")
        return
    if not has_media and not new_html.strip():
        await db.add_msg_log("DROP_EMPTY", f"源: [{chat_name}] ID:{message.message_id} | 文本替换后为空")
        return

    for attempt in range(3):
        try:
            if new_html != text_html:
                if not has_media: copied = await aiogram_bot.send_message(target_id, text=new_html, parse_mode="HTML")
                else: copied = await aiogram_bot.copy_message(chat_id=target_id, from_chat_id=source_id, message_id=message.message_id, caption=new_html, parse_mode="HTML")
            else:
                copied = await aiogram_bot.copy_message(chat_id=target_id, from_chat_id=source_id, message_id=message.message_id)
            
            await db.save_msg_mapping(source_id, message.message_id, copied.message_id)
            await db.add_msg_log("SEND", f"目标: [{target_id}] 新ID:{copied.message_id} | 转发成功")
            break
        except TelegramRetryAfter as e: await asyncio.sleep(e.retry_after)
        except Exception as e:
            await db.add_msg_log("ERROR", f"发送失败 ID:{message.message_id} | 报错:{e}")
            break

@dp.edited_channel_post()
async def handle_edited_post(message: Message):
    source_id = message.chat.id
    target_id = await db.get_target_channel(source_id)
    if not target_id: return
    target_msg_id = await db.get_target_msg_id(source_id, message.message_id)
    if not target_msg_id: return

    text_html = message.html_text if message.text or message.caption else ""
    has_media = message.content_type in ['photo', 'video', 'document', 'audio', 'voice', 'animation']
    should_skip, new_html = await db.apply_message_filters(text_html, has_media, "")
    if should_skip: return 

    try:
        if message.text: await aiogram_bot.edit_message_text(chat_id=target_id, message_id=target_msg_id, text=new_html, parse_mode="HTML")
        elif message.caption is not None: await aiogram_bot.edit_message_caption(chat_id=target_id, message_id=target_msg_id, caption=new_html, parse_mode="HTML")
        await db.add_msg_log("EDIT", f"已同步修改 源ID:{message.message_id} -> 目标ID:{target_msg_id}")
    except Exception: pass

pyro_user_app = None
def init_user_client():
    global pyro_user_app
    if API_ID and API_HASH:
        pyro_user_app = Client("sync_user_session", api_id=API_ID, api_hash=API_HASH, ipv6=False)
        return pyro_user_app
    return None