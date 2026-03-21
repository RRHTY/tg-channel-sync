import asyncio
import json
import os
import shutil
import sys
import signal
import html
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, BackgroundTasks, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import uvicorn
from aiogram.types import FSInputFile
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import InputMediaPhoto as AioInputMediaPhoto, InputMediaVideo as AioInputMediaVideo, InputMediaDocument as AioInputMediaDocument, InputMediaAudio as AioInputMediaAudio
from pyrogram.errors import FloodWait as PyroFloodWait
from pyrogram.enums import ParseMode
from pyrogram.types import InputMediaPhoto, InputMediaVideo, InputMediaDocument, InputMediaAudio
from dotenv import load_dotenv

import database as db
import bot_engine

load_dotenv()
PORT = int(os.getenv("PORT", 8011))

app_info_cache = {"bot": {"name": "", "username": ""}, "user": {"name": "", "status": "未配置"}}
sync_state = {
    "is_syncing": False, "mode": "", "total": 0, "current": 0,
    "current_text": "", "current_link": "", "skipped": 0,
    "stop_requested": False,
    "source_id_raw": "", "target_id_raw": "", "delay": 5,
    "start_id": "", "end_id": "", "json_path": ""
}

polling_task = None
TEMP_DIR = "temp"

@asynccontextmanager
async def lifespan(app: FastAPI):
    global polling_task
    await db.init_db()
    
    # 【智能缓存管理】: 仅在 Python 启动时彻底清空 temp，运行中途停止绝不删除，完美实现底层断点续传！
    if os.path.exists(TEMP_DIR):
        shutil.rmtree(TEMP_DIR, ignore_errors=True)
    os.makedirs(TEMP_DIR, exist_ok=True)
    
    try:
        me = await bot_engine.aiogram_bot.get_me()
        app_info_cache["bot"] = {"name": me.first_name, "username": me.username}
        await db.add_log("INFO", f"🚀 [Aiogram 实时监听] 已就绪: {me.first_name}")
        print(f"✅ Bot 已上线: {me.first_name} (@{me.username})")
        polling_task = asyncio.create_task(bot_engine.dp.start_polling(bot_engine.aiogram_bot))
    except Exception as e:
        await db.add_log("ERROR", f"Bot启动失败: {e}")

    bot_engine.init_user_client()
    if bot_engine.pyro_user_app:
        try:
            await bot_engine.pyro_user_app.start()
            user_me = await bot_engine.pyro_user_app.get_me()
            app_info_cache["user"] = {"name": user_me.first_name, "status": "已登录"}
            await db.add_log("INFO", f"👤 [历史数据引擎] 辅助账号登录成功: {user_me.first_name}")
        except Exception as e:
            await db.add_log("ERROR", f"辅助账号登录异常: {e}")
    yield
    
    print("⏳ 正在安全关闭系统...")
    if polling_task:
        polling_task.cancel()
        try: await polling_task
        except asyncio.CancelledError: pass
    await asyncio.sleep(0.5)
            
    try:
        if bot_engine.pyro_user_app and bot_engine.pyro_user_app.is_initialized:
            await bot_engine.pyro_user_app.stop(block=False)
    except Exception: pass
    try: 
        await bot_engine.aiogram_bot.session.close()
        print("✅ Bot 会话已安全关闭")
    except Exception: pass
    print("👋 系统已完全退出")

app = FastAPI(title="杏铃同步台", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def serve_index(): return FileResponse("static/index.html")

@app.get("/api/app_info")
async def get_app_info(): return app_info_cache

@app.post("/api/server/stop")
async def stop_server():
    async def shutdown():
        await asyncio.sleep(1)
        os.kill(os.getpid(), signal.SIGINT)
    asyncio.create_task(shutdown())
    return {"status": "success", "message": "服务端正在关闭，请稍候关闭此页面..."}

@app.post("/api/server/restart")
async def restart_server():
    async def restart():
        await asyncio.sleep(1)
        os.execv(sys.executable, [sys.executable] + sys.argv)
    asyncio.create_task(restart())
    return {"status": "success", "message": "服务端正在重启，页面即将刷新..."}

async def resolve_chat_id(chat_ref: str) -> int:
    chat_ref = str(chat_ref).strip()
    if not chat_ref: raise ValueError("频道标识为空")
    if chat_ref.lstrip('-').isdigit(): return int(chat_ref)
    if "t.me/" in chat_ref:
        chat_ref = "@" + chat_ref.split("/")[-1]
        if "?single" in chat_ref: chat_ref = chat_ref.split("?")[0]
    if not chat_ref.startswith("@") and not chat_ref.lstrip('-').isdigit():
        chat_ref = "@" + chat_ref
    try:
        chat = await bot_engine.aiogram_bot.get_chat(chat_ref)
        return chat.id
    except Exception as e: raise ValueError(f"无法解析频道 {chat_ref}: {e}")

@app.get("/api/mappings")
async def get_mappings(): return [{"source_id": m[0], "target_id": m[1]} for m in await db.get_all_channel_mappings()]

@app.post("/api/mappings")
async def add_mapping(source_id: str = Form(...), target_id: str = Form(...)):
    try:
        s_id = await resolve_chat_id(source_id)
        t_id = await resolve_chat_id(target_id)
        await db.add_channel_mapping(s_id, t_id)
        return {"status": "success", "message": f"规则添加成功 ({s_id} -> {t_id})"}
    except Exception as e: return {"status": "error", "message": str(e)}

@app.delete("/api/mappings/{source_id}")
async def delete_mapping(source_id: int):
    await db.delete_channel_mapping(source_id)
    return {"status": "success", "message": "规则已删除"}

@app.get("/api/filter_rules")
async def get_filter_rules():
    rules = await db.get_all_filter_rules()
    return [{"id": r[0], "rule_type": r[1], "pattern": r[2], "replacement": r[3], "is_case_sensitive": r[4]} for r in rules]

@app.post("/api/filter_rules")
async def add_filter_rule(rule_type: str = Form(...), pattern: str = Form(...), replacement: str = Form(""), is_case_sensitive: int = Form(0)):
    await db.add_filter_rule(rule_type, pattern, replacement, is_case_sensitive)
    return {"status": "success", "message": "过滤规则添加成功"}

@app.delete("/api/filter_rules/{rule_id}")
async def delete_filter_rule(rule_id: int):
    await db.delete_filter_rule(rule_id)
    return {"status": "success", "message": "规则已删除"}

@app.get("/api/global_settings")
async def get_global_settings(): return await db.get_all_settings()

@app.post("/api/global_settings")
async def update_global_settings(
    sync_text: str = Form("1"), sync_photo: str = Form("1"), sync_video: str = Form("1"),
    sync_document: str = Form("1"), sync_sticker: str = Form("1"), sync_gif: str = Form("1"),
    sync_audio: str = Form("1"), sync_voice: str = Form("1")
):
    settings = {
        "sync_text": sync_text, "sync_photo": sync_photo, "sync_video": sync_video,
        "sync_document": sync_document, "sync_sticker": sync_sticker, "sync_gif": sync_gif,
        "sync_audio": sync_audio, "sync_voice": sync_voice
    }
    await db.update_settings(settings)
    return {"status": "success", "message": "全局消息过滤配置已保存并对双端生效"}

@app.get("/api/sync_status")
async def get_sync_status(): return sync_state

@app.get("/api/logs/backend")
async def get_backend_logs(): return [{"time": l[0], "level": l[1], "msg": l[2]} for l in await db.get_recent_logs()]

@app.get("/api/logs/messages")
async def get_message_logs(): return [{"time": l[0], "action": l[1], "detail": l[2]} for l in await db.get_recent_msg_logs()]

@app.post("/api/stop_sync")
async def stop_sync():
    if sync_state["is_syncing"]:
        sync_state["stop_requested"] = True
        return {"status": "success", "message": "已下发硬核中断指令，网络连接将瞬间切断..."}
    return {"status": "error", "message": "没有运行中的任务"}

def is_allowed_msg_type(msg, mode, settings):
    if mode in ['api', 'clone']:
        if msg.photo: return settings.get('sync_photo') == '1'
        elif msg.video: return settings.get('sync_video') == '1'
        elif msg.animation: return settings.get('sync_gif') == '1'
        elif msg.audio: return settings.get('sync_audio') == '1'
        elif msg.voice: return settings.get('sync_voice') == '1'
        elif msg.document: return settings.get('sync_document') == '1'
        elif msg.sticker: return settings.get('sync_sticker') == '1'
        elif msg.text: return settings.get('sync_text') == '1'
        return True
    elif mode == 'json':
        if msg.get('photo'): return settings.get('sync_photo') == '1'
        media_type = msg.get('media_type')
        if media_type == 'video_file': return settings.get('sync_video') == '1'
        if media_type == 'animation': return settings.get('sync_gif') == '1'
        if media_type == 'audio_file': return settings.get('sync_audio') == '1'
        if media_type == 'voice_message': return settings.get('sync_voice') == '1'
        if media_type == 'sticker': return settings.get('sync_sticker') == '1'
        if 'file' in msg and media_type not in ['video_file', 'animation', 'sticker', 'audio_file', 'voice_message']: return settings.get('sync_document') == '1'
        if msg.get('text') and not msg.get('photo') and not 'file' in msg: return settings.get('sync_text') == '1'
        return True

@app.post("/api/start_sync")
async def start_sync(
        background_tasks: BackgroundTasks, mode: str = Form(...), sender: str = Form("bot"),
        source_id: str = Form(...), target_id: str = Form(...), delay: float = Form(...), 
        start_id: int = Form(0), end_id: int = Form(0), json_path: str = Form("")
):
    if sync_state["is_syncing"]: return {"status": "error", "message": "任务运行中！"}
    if mode in ["api", "clone"] and not bot_engine.pyro_user_app: return {"status": "error", "message": "该模式必须配置 API 账号"}
    if mode == "json" and not os.path.exists(json_path): return {"status": "error", "message": "找不到 JSON 文件！"}
    background_tasks.add_task(process_master_sync, mode, sender, source_id, target_id, delay, start_id, end_id, json_path)
    return {"status": "success", "message": f"已启动 {mode.upper()} 任务"}

def parse_tg_json_text(text_list):
    if isinstance(text_list, str): return html.escape(text_list)
    html_text = ""
    for t in text_list:
        if isinstance(t, str): html_text += html.escape(t)
        else:
            t_type = t.get('type')
            inner = html.escape(t.get('text', ''))
            if not inner: continue
            if t_type == 'bold': html_text += f"<b>{inner}</b>"
            elif t_type == 'italic': html_text += f"<i>{inner}</i>"
            elif t_type == 'code': html_text += f"<code>{inner}</code>"
            elif t_type == 'pre': html_text += f"<pre>{inner}</pre>"
            elif t_type == 'strikethrough': html_text += f"<s>{inner}</s>"
            elif t_type == 'underline': html_text += f"<u>{inner}</u>"
            elif t_type in ['text_link', 'link']: html_text += f"<a href='{t.get('href', inner)}'>{inner}</a>"
            else: html_text += inner
    return html_text

# ================= 核心修复：0 延迟“拔网线”级终止器 =================
async def safe_execute(coro):
    """包裹任意异步任务，实现按停瞬间斩断数据流"""
    task = asyncio.create_task(coro)
    while not task.done():
        if sync_state.get("stop_requested"):
            task.cancel()
            raise Exception("STOP_REQUESTED")
        await asyncio.sleep(0.2)
    try:
        return await task
    except asyncio.CancelledError:
        raise Exception("STOP_REQUESTED")

def create_progress_callback(action_name):
    start_t = time.time()
    last_upd = 0
    async def progress(current, total):
        nonlocal last_upd
        if sync_state.get("stop_requested"):
            raise Exception("STOP_REQUESTED")
        now = time.time()
        if now - last_upd > 0.5 or current == total:
            last_upd = now
            elapsed = now - start_t
            if elapsed > 0 and total > 0:
                spd_mb = (current / elapsed) / 1048576
                pct = current / total * 100
                sync_state["current_text"] = f"{action_name} {pct:.1f}% ({spd_mb:.1f} MB/s)"
            elif total > 0:
                pct = current / total * 100
                sync_state["current_text"] = f"{action_name} {pct:.1f}%"
    return progress

async def process_master_sync(mode: str, sender: str, source_id_raw: str, target_id_raw: str, delay: float, start_id: int, end_id: int, json_path: str):
    global sync_state
    safe_delay = max(0.5, float(delay))
    
    if mode == "api": sender = "user"
    elif mode == "json": sender = "bot"

    sync_state.update({
        "is_syncing": True, "mode": mode.upper(), 
        "source_id_raw": source_id_raw, "target_id_raw": target_id_raw,
        "delay": safe_delay, "start_id": start_id, "end_id": end_id, "json_path": json_path,
        "current": 0, "skipped": 0, "total": 0, "stop_requested": False
    })
    settings = await db.get_all_settings()

    try:
        source_id = await resolve_chat_id(source_id_raw)
        target_id = await resolve_chat_id(target_id_raw)
    except Exception as e:
        await db.add_log("ERROR", f"❌ 任务中止，频道信息有误: {e}")
        sync_state["is_syncing"] = False
        return

    try:
        if mode in ["api", "clone"]:
            app = bot_engine.pyro_user_app
            bot = bot_engine.aiogram_bot
            if not start_id: start_id = 1
            if not end_id:
                async for msg in app.get_chat_history(source_id, limit=1): end_id = msg.id
            if not end_id: end_id = 1
            sync_state["start_id"] = start_id
            sync_state["end_id"] = end_id
            sync_state["total"] = end_id - start_id + 1
            chunk_size = 100
            
            await db.add_log("INFO", f"🚀 [{mode.upper()}模式] 开始拉取 ID: {start_id} 到 {end_id} (执行引擎: {sender.upper()})")
            
            for chunk_start in range(start_id, end_id + 1, chunk_size):
                if sync_state["stop_requested"]: break
                chunk_end = min(chunk_start + chunk_size - 1, end_id)
                ids_to_fetch = list(range(chunk_start, chunk_end + 1))
                
                try: msgs = await app.get_messages(source_id, ids_to_fetch)
                except Exception: continue
                
                filtered_msgs = []
                for msg in msgs:
                    if msg is None or msg.empty: continue
                    if not is_allowed_msg_type(msg, mode, settings): 
                        continue
                    filtered_msgs.append(msg)
                
                grouped_msgs = []
                current_group = []
                for msg in filtered_msgs:
                    if msg.media_group_id:
                        if not current_group: current_group.append(msg)
                        elif current_group[0].media_group_id == msg.media_group_id: current_group.append(msg)
                        else:
                            grouped_msgs.append(current_group)
                            current_group = [msg]
                    else:
                        if current_group:
                            grouped_msgs.append(current_group)
                            current_group = []
                        grouped_msgs.append([msg])
                if current_group: grouped_msgs.append(current_group)

                for group in grouped_msgs:
                    if sync_state["stop_requested"]: break
                    
                    if len(group) == 1:
                        msg = group[0]
                        has_media = msg.media is not None
                        file_name = msg.document.file_name if msg.document else (msg.video.file_name if msg.video else "")
                        try: text_html = msg.text.html if msg.text else (msg.caption.html if msg.caption else "")
                        except: text_html = msg.text or msg.caption or ""
                        
                        await db.add_msg_log(f"{mode.upper()}_RECV", f"读取 ID:{msg.id} | 内容:{text_html[:20]}")

                        should_skip, new_html = await db.apply_message_filters(text_html, has_media, file_name or "")
                        if should_skip: continue
                        if not has_media and not new_html.strip(): continue 

                        if await update_state_and_check_skip(source_id, msg.id, new_html[:50] or "[单条内容]"): continue
                        
                        try:
                            if mode == "api":
                                if new_html != text_html:
                                    if not has_media: copied = await safe_execute(app.send_message(chat_id=target_id, text=new_html, parse_mode=ParseMode.HTML))
                                    else: copied = await safe_execute(app.copy_message(chat_id=target_id, from_chat_id=source_id, message_id=msg.id, caption=new_html, parse_mode=ParseMode.HTML))
                                else: copied = await safe_execute(app.copy_message(chat_id=target_id, from_chat_id=source_id, message_id=msg.id))
                                sent_id = copied.id
                                await record_success(source_id, msg.id, sent_id)
                                await db.add_msg_log(f"{mode.upper()}_SEND", f"目标:[{target_id}] 新ID:{sent_id} | 同步成功")
                                await asyncio.sleep(safe_delay)

                            elif mode == "clone":
                                if not has_media:
                                    if sender == 'bot': sent_id = (await safe_execute(bot.send_message(chat_id=target_id, text=new_html, parse_mode="HTML"))).message_id
                                    else: sent_id = (await safe_execute(app.send_message(chat_id=target_id, text=new_html, parse_mode=ParseMode.HTML))).id
                                    await record_success(source_id, msg.id, sent_id)
                                    await asyncio.sleep(safe_delay)
                                else:
                                    # ============ 深度克隆：加入断点续传与彻底重试保护 ============
                                    file_path = None
                                    for attempt in range(3):
                                        if sync_state["stop_requested"]: break
                                        try:
                                            # Pyrogram 底层会自动通过识别 temp_dir 的文件碎片实现断点续传！
                                            file_path = await safe_execute(app.download_media(msg, file_name=f"{TEMP_DIR}/", progress=create_progress_callback("⏬ 下载单文件")))
                                            if file_path: break
                                        except Exception as e:
                                            if "STOP_REQUESTED" in str(e): raise e
                                            await db.add_log("ERROR", f"⏬ 下载超时失败(重试 {attempt+1}/3) ID {msg.id}")
                                            await asyncio.sleep(2)
                                            
                                    if not file_path or sync_state["stop_requested"]: continue
                                    
                                    # 核心修复 2：Bot 限制 50MB 智能检测兜底
                                    file_size = os.path.getsize(file_path)
                                    actual_sender = sender
                                    if actual_sender == 'bot' and file_size > 50 * 1024 * 1024:
                                        await db.add_msg_log("WARN", f"文件达 {file_size/1048576:.1f}MB，Bot受限50M，自动切为用户账号上传")
                                        actual_sender = 'user'

                                    for attempt in range(3):
                                        if sync_state["stop_requested"]: break
                                        try:
                                            if actual_sender == 'bot':
                                                sync_state["current_text"] = "⏫ 机器人高速静默上传中..."
                                                media_file = FSInputFile(file_path)
                                                if msg.photo: sent = await safe_execute(bot.send_photo(chat_id=target_id, photo=media_file, caption=new_html, parse_mode="HTML"))
                                                elif msg.video: sent = await safe_execute(bot.send_video(chat_id=target_id, video=media_file, caption=new_html, parse_mode="HTML"))
                                                elif msg.audio: sent = await safe_execute(bot.send_audio(chat_id=target_id, audio=media_file, caption=new_html, parse_mode="HTML"))
                                                elif msg.voice: sent = await safe_execute(bot.send_voice(chat_id=target_id, voice=media_file, caption=new_html, parse_mode="HTML"))
                                                else: sent = await safe_execute(bot.send_document(chat_id=target_id, document=media_file, caption=new_html, parse_mode="HTML"))
                                                sent_id = sent.message_id
                                            else:
                                                up_cb = create_progress_callback("⏫ 辅助账号上传")
                                                if msg.photo: sent = await safe_execute(app.send_photo(chat_id=target_id, photo=file_path, caption=new_html, parse_mode=ParseMode.HTML, progress=up_cb))
                                                elif msg.video: sent = await safe_execute(app.send_video(chat_id=target_id, video=file_path, caption=new_html, parse_mode=ParseMode.HTML, progress=up_cb))
                                                elif msg.audio: sent = await safe_execute(app.send_audio(chat_id=target_id, audio=file_path, caption=new_html, parse_mode=ParseMode.HTML, progress=up_cb))
                                                elif msg.voice: sent = await safe_execute(app.send_voice(chat_id=target_id, voice=file_path, caption=new_html, parse_mode=ParseMode.HTML, progress=up_cb))
                                                else: sent = await safe_execute(app.send_document(chat_id=target_id, document=file_path, caption=new_html, parse_mode=ParseMode.HTML, progress=up_cb))
                                                sent_id = sent.id
                                                
                                            await record_success(source_id, msg.id, sent_id)
                                            await db.add_msg_log("CLONE_SEND", f"目标:[{target_id}] 新ID:{sent_id} | 同步成功")
                                            break
                                        except Exception as e:
                                            if "STOP_REQUESTED" in str(e): raise e
                                            await db.add_log("ERROR", f"⏫ 上传网络波动(重试 {attempt+1}/3) ID {msg.id}")
                                            await asyncio.sleep(2)
                                            
                                    try: os.remove(file_path) # 无论成功失败，重试完必清理当前文件
                                    except: pass
                                    await asyncio.sleep(safe_delay)

                        except Exception as e:
                            err_str = str(e)
                            if sync_state["stop_requested"] and ("STOP_REQUESTED" in err_str or "write" in err_str):
                                await db.add_log("WARNING", "⏹ 任务强行终止，已成功斩断底层网络流！")
                                break
                            await db.add_log("ERROR", f"❌ 单条同步抛出异常 ID {msg.id}: {e}")
                    
                    else:
                        # 媒体组处理
                        all_skipped = True
                        should_skip_group = False
                        for m in group:
                            try: t_html = m.text.html if m.text else (m.caption.html if m.caption else "")
                            except: t_html = m.text or m.caption or ""
                            f_name = m.document.file_name if m.document else (m.video.file_name if m.video else "")
                            s_skip, _ = await db.apply_message_filters(t_html, True, f_name or "")
                            if s_skip: should_skip_group = True; break 
                            sync_state["current"] += 1
                            if not await db.is_message_synced(source_id, m.id): all_skipped = False
                            else: sync_state["skipped"] += 1
                        msg_ids = [m.id for m in group]
                        await db.add_msg_log(f"{mode.upper()}_RECV_GRP", f"读取 组IDs:{msg_ids}")
                        if should_skip_group or all_skipped: continue

                        success = False
                        if mode == "api":
                            for attempt in range(3):
                                if sync_state["stop_requested"]: break
                                try:
                                    copied_msgs = await safe_execute(app.copy_media_group(chat_id=target_id, from_chat_id=source_id, message_id=msg_ids[0]))
                                    for orig_m, new_m in zip(group, copied_msgs): await record_success(source_id, orig_m.id, new_m.id)
                                    await db.add_msg_log("API_SEND_GRP", f"目标:[{target_id}] | 组复制成功")
                                    success = True; break
                                except TypeError as e:
                                    if "topics" in str(e) or "Messages.__init__" in str(e):
                                        for m in group: await record_success(source_id, m.id, 0)
                                        success = True; break
                                    else: break 
                                except Exception as e: 
                                    if "STOP_REQUESTED" in str(e): raise e

                        elif mode == "clone":
                            downloaded_files = []
                            dl_success = False
                            for attempt in range(3):
                                if sync_state["stop_requested"]: break
                                try:
                                    sem = asyncio.Semaphore(3)
                                    async def dl_album_item(m_item, idx, tot):
                                        async with sem:
                                            prog = create_progress_callback(f"⏬ 并发下载 [{idx}/{tot}]")
                                            return await safe_execute(app.download_media(m_item, file_name=f"{TEMP_DIR}/", progress=prog))
                                    
                                    tasks = [dl_album_item(m, i+1, len(group)) for i, m in enumerate(group)]
                                    results = await asyncio.gather(*tasks, return_exceptions=True)
                                    
                                    has_err = False
                                    for res in results:
                                        if isinstance(res, Exception):
                                            if "STOP_REQUESTED" in str(res): sync_state["stop_requested"] = True
                                            has_err = True
                                            break
                                    
                                    if not has_err and not sync_state["stop_requested"]:
                                        downloaded_files = [(m, p) for m, p in zip(group, results) if isinstance(p, str)]
                                        dl_success = True; break
                                    else:
                                        await asyncio.sleep(2)
                                except Exception as e:
                                    if "STOP_REQUESTED" in str(e): break
                                    
                            if not dl_success or sync_state["stop_requested"]:
                                for _, p in downloaded_files: 
                                    try: os.remove(p)
                                    except: pass
                                continue
                                
                            # 核心修复 3：相册的 50MB Bot限制智能检测兜底
                            actual_sender = sender
                            if actual_sender == 'bot':
                                for _, p in downloaded_files:
                                    if os.path.getsize(p) > 50 * 1024 * 1024:
                                        await db.add_msg_log("WARN", f"相册内含 > 50MB 巨物，强制切换整组为辅助账号上传")
                                        actual_sender = 'user'
                                        break

                            for attempt in range(3):
                                if sync_state["stop_requested"]: break
                                try:
                                    sync_state["current_text"] = f"⏫ 整体打包上传相册 ({len(group)}项)..."
                                    if actual_sender == 'bot':
                                        media_group = []
                                        for m, p in downloaded_files:
                                            try: t_html = m.text.html if m.text else (m.caption.html if m.caption else "")
                                            except: t_html = m.text or m.caption or ""
                                            _, cap = await db.apply_message_filters(t_html, True, "")
                                            f = FSInputFile(p)
                                            if m.photo: media_group.append(AioInputMediaPhoto(media=f, caption=cap, parse_mode="HTML"))
                                            elif m.video: media_group.append(AioInputMediaVideo(media=f, caption=cap, parse_mode="HTML"))
                                            elif m.audio: media_group.append(AioInputMediaAudio(media=f, caption=cap, parse_mode="HTML"))
                                            else: media_group.append(AioInputMediaDocument(media=f, caption=cap, parse_mode="HTML"))
                                        copied_msgs = await safe_execute(bot.send_media_group(chat_id=target_id, media=media_group))
                                        for orig_m, new_m in zip(group, copied_msgs): await record_success(source_id, orig_m.id, new_m.message_id)
                                    else:
                                        media_group = []
                                        for m, p in downloaded_files:
                                            try: t_html = m.text.html if m.text else (m.caption.html if m.caption else "")
                                            except: t_html = m.text or m.caption or ""
                                            _, cap = await db.apply_message_filters(t_html, True, "")
                                            if m.photo: media_group.append(InputMediaPhoto(p, caption=cap, parse_mode=ParseMode.HTML))
                                            elif m.video: media_group.append(InputMediaVideo(p, caption=cap, parse_mode=ParseMode.HTML))
                                            elif m.audio: media_group.append(InputMediaAudio(p, caption=cap, parse_mode=ParseMode.HTML))
                                            else: media_group.append(InputMediaDocument(p, caption=cap, parse_mode=ParseMode.HTML))
                                        copied_msgs = await safe_execute(app.send_media_group(chat_id=target_id, media=media_group))
                                        for orig_m, new_m in zip(group, copied_msgs): await record_success(source_id, orig_m.id, new_m.id)
                                    
                                    await db.add_msg_log("CLONE_SEND_GRP", f"目标:[{target_id}] | 组克隆上传成功")
                                    success = True; break
                                except TypeError as e:
                                    if "topics" in str(e) or "Messages.__init__" in str(e):
                                        for m in group: await record_success(source_id, m.id, 0)
                                        success = True; break
                                    else: break 
                                except Exception as e:
                                    if "STOP_REQUESTED" in str(e): raise e
                                    await asyncio.sleep(2)
                                
                            for _, p in downloaded_files:
                                try: os.remove(p)
                                except: pass

                        if not success and not sync_state["stop_requested"]:
                            for m in group:
                                if sync_state["stop_requested"]: break 
                                try:
                                    if sender == 'bot': copied = await safe_execute(bot.copy_message(chat_id=target_id, from_chat_id=source_id, message_id=m.id))
                                    else: copied = await safe_execute(app.copy_message(chat_id=target_id, from_chat_id=source_id, message_id=m.id))
                                    sent_id = copied.message_id if sender == 'bot' else copied.id
                                    await record_success(source_id, m.id, sent_id)
                                    await asyncio.sleep(safe_delay)
                                except Exception as e:
                                    if "STOP_REQUESTED" in str(e): break
                        elif success and not sync_state["stop_requested"]: await asyncio.sleep(safe_delay)

        elif mode == "json":
            bot = bot_engine.aiogram_bot
            with open(json_path, 'r', encoding='utf-8') as f: data = json.load(f)
            base_dir = os.path.dirname(os.path.abspath(json_path))
            raw_msgs = [m for m in data.get('messages', []) if m.get('type') == 'message']
            if start_id and end_id: raw_msgs = [m for m in raw_msgs if start_id <= m.get('id', 0) <= end_id]
            msgs = []
            for m in raw_msgs:
                if not is_allowed_msg_type(m, 'json', settings): continue
                msgs.append(m)
            sync_state["total"] = len(msgs)

            for m in msgs:
                if sync_state["stop_requested"]: break
                msg_id = m.get('id')
                text_html = parse_tg_json_text(m.get('text', []))
                has_media = 'photo' in m or 'file' in m or 'media_type' in m
                file_name = m.get('file', '') 
                
                await db.add_msg_log("JSON_READ", f"ID:{msg_id} | 读取本地项")
                should_skip, new_html = await db.apply_message_filters(text_html, has_media, file_name)
                if should_skip or (not has_media and not new_html.strip()): continue 
                if await update_state_and_check_skip(source_id, msg_id, new_html[:50] or "[媒体]"): continue
                media_path = m.get('photo') or m.get('file')
                abs_media_path = os.path.join(base_dir, media_path) if media_path else None

                try:
                    sent_id = None
                    if abs_media_path and os.path.exists(abs_media_path):
                        media_file = FSInputFile(abs_media_path)
                        if m.get('photo'): sent = await safe_execute(bot.send_photo(chat_id=target_id, photo=media_file, caption=new_html, parse_mode="HTML"))
                        elif m.get('media_type') == 'video_file': sent = await safe_execute(bot.send_video(chat_id=target_id, video=media_file, caption=new_html, parse_mode="HTML"))
                        elif m.get('media_type') == 'audio_file': sent = await safe_execute(bot.send_audio(chat_id=target_id, audio=media_file, caption=new_html, parse_mode="HTML"))
                        elif m.get('media_type') == 'voice_message': sent = await safe_execute(bot.send_voice(chat_id=target_id, voice=media_file, caption=new_html, parse_mode="HTML"))
                        else: sent = await safe_execute(bot.send_document(chat_id=target_id, document=media_file, caption=new_html, parse_mode="HTML"))
                        sent_id = sent.message_id
                    elif new_html.strip():
                        sent_id = (await safe_execute(bot.send_message(chat_id=target_id, text=new_html, parse_mode="HTML"))).message_id

                    if sent_id: 
                        await record_success(source_id, msg_id, sent_id)
                        await db.add_msg_log("JSON_SEND", f"ID:{msg_id} -> 新ID:{sent_id} | 本地上传成功")
                except Exception as e:
                    err_str = str(e)
                    if sync_state["stop_requested"] and "STOP_REQUESTED" in err_str:
                        await db.add_log("WARNING", "⏹ 任务强行终止，网络流已切断！")
                        break
                    await db.add_log("ERROR", f"❌ 发送失败 ID {msg_id}: {e}")
                await asyncio.sleep(safe_delay)

    except asyncio.CancelledError: pass
    except Exception as e: await db.add_log("ERROR", f"同步中断: {e}")
    finally:
        sync_state["is_syncing"] = False
        if sync_state["stop_requested"]: await db.add_log("WARNING", "⏹ 任务已被手动终止！")
        else: await db.add_log("INFO", "✅ 当前同步任务结束！")
        sync_state["stop_requested"] = False

async def update_state_and_check_skip(source_id, msg_id, text):
    sync_state["current"] += 1
    sync_state["current_link"] = f"t.me/c/{str(source_id).replace('-100', '')}/{msg_id}"
    sync_state["current_text"] = text
    if await db.is_message_synced(source_id, msg_id):
        sync_state["skipped"] += 1
        return True
    return False

async def record_success(source_id, msg_id, target_msg_id):
    await db.save_msg_mapping(source_id, msg_id, target_msg_id)

async def handle_floodwait(wait_time):
    await db.add_log("ERROR", f"触发速率限制，强制休眠 {wait_time} 秒...")
    await asyncio.sleep(wait_time)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)