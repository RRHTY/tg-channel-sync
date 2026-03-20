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

# 媒体组缓冲池
media_group_cache = {}

@dp.channel_post()
async def handle_new_post(message: Message):
    source_id = message.chat.id
    target_id = await db.get_target_channel(source_id)
    if not target_id: return

    # ================= 1. 媒体组 (Album) 打包处理逻辑 =================
    if message.media_group_id:
        mg_id = message.media_group_id
        if mg_id not in media_group_cache:
            media_group_cache[mg_id] = [message]
            await asyncio.sleep(2)  # 缓冲 2 秒，等待同组的其他图片/视频到达
            
            if mg_id in media_group_cache:
                group = media_group_cache.pop(mg_id)
                group.sort(key=lambda m: m.message_id) 
                
                should_skip_group = False
                for m in group:
                    text_html = m.html_text if m.text or m.caption else ""
                    file_name = m.document.file_name if m.document else (m.video.file_name if m.video else "")
                    should_skip, _ = await db.apply_message_filters(text_html, True, file_name or "")
                    if should_skip:
                        should_skip_group = True
                        break
                
                if should_skip_group:
                    await db.add_log("INFO", f"⏭️ [过滤拦截] 实时媒体组命中屏蔽规则，整组丢弃 ID: {[m.message_id for m in group]}")
                    return

                msg_ids = [m.message_id for m in group]
                success = False
                
                # 尝试批量转发相册
                for attempt in range(3):
                    try:
                        copied_ids = await aiogram_bot.copy_messages(chat_id=target_id, from_chat_id=source_id, message_ids=msg_ids)
                        for orig_m, new_m in zip(group, copied_ids):
                            await db.save_msg_mapping(source_id, orig_m.message_id, new_m.message_id)
                        await db.add_log("SUCCESS", f"[实时同步] 媒体组成功: {source_id} -> {target_id} (共 {len(msg_ids)} 项)")
                        success = True
                        break
                    except TelegramRetryAfter as e:
                        await db.add_log("WARNING", f"⚠️ [实时同步] 触发风控，等待 {e.retry_after} 秒重试")
                        await asyncio.sleep(e.retry_after)
                    except Exception as e:
                        await db.add_log("ERROR", f"❌ [实时同步] 媒体组批量转发失败 IDs {msg_ids}: {e}")
                        break # 跳出重试，执行单条降级
                        
                # 安全降级方案
                if not success:
                    await db.add_log("WARNING", f"🔄 [实时降级] 媒体组批量失败，降级为逐个单条发送: {msg_ids}")
                    for m in group:
                        try:
                            copied = await aiogram_bot.copy_message(chat_id=target_id, from_chat_id=source_id, message_id=m.message_id)
                            await db.save_msg_mapping(source_id, m.message_id, copied.message_id)
                            await asyncio.sleep(1) # 单条发送加1秒缓冲
                        except Exception as ex:
                            await db.add_log("ERROR", f"❌ [实时降级] 单条失败 ID {m.message_id}: {ex}")
        else:
            media_group_cache[mg_id].append(message)
        return

    # ================= 2. 普通单条消息处理逻辑 =================
    has_media = message.content_type in ['photo', 'video', 'document', 'audio', 'animation']
    file_name = message.document.file_name if message.document else (message.video.file_name if message.video else "")
    text_html = message.html_text if message.text or message.caption else ""
    
    should_skip, new_html = await db.apply_message_filters(text_html, has_media, file_name or "")
    
    if should_skip:
        await db.add_log("INFO", f"⏭️ [过滤拦截] 触发跳过规则，已丢弃单条消息 ID {message.message_id}")
        return

    if not has_media and not new_html.strip():
        return

    for attempt in range(3):
        try:
            if new_html != text_html:
                if not has_media:
                    copied = await aiogram_bot.send_message(target_id, text=new_html, parse_mode="HTML")
                else:
                    copied = await aiogram_bot.copy_message(chat_id=target_id, from_chat_id=source_id, message_id=message.message_id, caption=new_html, parse_mode="HTML")
            else:
                copied = await aiogram_bot.copy_message(chat_id=target_id, from_chat_id=source_id, message_id=message.message_id)
            
            await db.save_msg_mapping(source_id, message.message_id, copied.message_id)
            await db.add_log("SUCCESS", f"[实时同步] 成功: {source_id} -> {target_id}")
            break
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except Exception as e:
            await db.add_log("ERROR", f"❌ [实时同步] 失败 ID {message.message_id}: {e}")
            break

@dp.edited_channel_post()
async def handle_edited_post(message: Message):
    source_id = message.chat.id
    target_id = await db.get_target_channel(source_id)
    if not target_id: return

    target_msg_id = await db.get_target_msg_id(source_id, message.message_id)
    if not target_msg_id: return

    text_html = message.html_text if message.text or message.caption else ""
    has_media = message.content_type in ['photo', 'video', 'document', 'audio', 'animation']
    should_skip, new_html = await db.apply_message_filters(text_html, has_media, "")
    
    if should_skip: return 

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