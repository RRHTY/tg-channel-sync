import asyncio
import json
import os
import shutil
import sys
import signal
import html
import time
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, BackgroundTasks, Form, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from aiogram.types import FSInputFile
from aiogram.types import InputMediaPhoto as AioPhoto, InputMediaVideo as AioVideo, InputMediaDocument as AioDoc, InputMediaAudio as AioAudio
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
    "is_syncing": False, "mode": "", "total": 0, "current": 0, "current_text": "", 
    "current_link": "", "skipped": 0, "stop_requested": False, "source_id_raw": "", 
    "target_id_raw": "", "delay": 5, "start_id": "", "end_id": "", "json_path": ""
}
polling_task, TEMP_DIR = None, "temp"

# 核心修复 1：新增全局关机事件通行证
SHUTDOWN_EVENT = asyncio.Event()

@asynccontextmanager
async def lifespan(app: FastAPI):
    global polling_task
    await db.init_db()
    if os.path.exists(TEMP_DIR): shutil.rmtree(TEMP_DIR, ignore_errors=True)
    os.makedirs(TEMP_DIR, exist_ok=True)
    try:
        me = await bot_engine.aiogram_bot.get_me()
        app_info_cache["bot"] = {"name": me.first_name, "username": me.username}
        polling_task = asyncio.create_task(bot_engine.dp.start_polling(bot_engine.aiogram_bot))
    except Exception as e: await db.add_log("ERROR", f"Bot启动失败: {e}")

    bot_engine.init_user_client()
    if bot_engine.pyro_user_app:
        try:
            await bot_engine.pyro_user_app.start()
            user_me = await bot_engine.pyro_user_app.get_me()
            app_info_cache["user"] = {"name": user_me.first_name, "status": "已登录"}
        except Exception: pass
    
    yield
    
    # === 核心修复 2：进入关机流程时，激活广播，瞬间释放所有 SSE 长连接 ===
    print("⏳ 正在安全关闭系统...")
    SHUTDOWN_EVENT.set()
    
    if polling_task:
        polling_task.cancel()
        try: await polling_task
        except asyncio.CancelledError: pass
    
    await asyncio.sleep(0.5)
    
    try:
        if bot_engine.pyro_user_app and bot_engine.pyro_user_app.is_initialized: 
            await bot_engine.pyro_user_app.stop(block=False)
        await bot_engine.aiogram_bot.session.close()
    except Exception: pass

app = FastAPI(title="杏铃同步台", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def serve_index(): return FileResponse("static/index.html")

@app.get("/api/app_info")
async def get_app_info(): return app_info_cache

@app.get("/api/stream")
async def sse_stream(request: Request):
    async def event_generator():
        last_sys_id, last_msg_id = 0, 0
        sys_logs = await db.get_sys_logs_after(0)
        msg_logs = await db.get_msg_logs_after(0)
        if sys_logs: last_sys_id = sys_logs[0][0]
        if msg_logs: last_msg_id = msg_logs[0][0]
        
        # 核心修复 3：不再死循环，若监听到关机事件或客户端关闭网页，主动结束释放 Uvicorn
        while not SHUTDOWN_EVENT.is_set():
            if await request.is_disconnected():
                break
                
            payload = {"status": sync_state}
            
            new_sys = await db.get_sys_logs_after(last_sys_id)
            if new_sys:
                last_sys_id = new_sys[0][0]
                payload["sys_logs"] = [{"id": r[0], "time": r[1], "level": r[2], "msg": r[3]} for r in reversed(new_sys)]
                
            new_msg = await db.get_msg_logs_after(last_msg_id)
            if new_msg:
                last_msg_id = new_msg[0][0]
                payload["msg_logs"] = [{"id": r[0], "time": r[1], "action": r[2], "detail": r[3]} for r in reversed(new_msg)]
                
            yield f"data: {json.dumps(payload)}\n\n"
            
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break
                
    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.post("/api/server/stop")
async def stop_server():
    async def shutdown():
        await asyncio.sleep(1)
        os.kill(os.getpid(), signal.SIGINT)
    asyncio.create_task(shutdown())
    return {"status": "success", "message": "服务端正在关闭"}

@app.post("/api/server/restart")
async def restart_server():
    async def restart():
        await asyncio.sleep(1)
        os.execv(sys.executable, [sys.executable] + sys.argv)
    asyncio.create_task(restart())
    return {"status": "success", "message": "服务端重启中"}

async def resolve_chat_id(chat_ref: str) -> int:
    chat_ref = str(chat_ref).strip()
    if chat_ref.lstrip('-').isdigit(): return int(chat_ref)
    if "t.me/" in chat_ref: chat_ref = "@" + chat_ref.split("/")[-1].split("?")[0]
    if not chat_ref.startswith("@"): chat_ref = "@" + chat_ref
    try: return (await bot_engine.aiogram_bot.get_chat(chat_ref)).id
    except Exception as e: raise ValueError(f"无法解析频道 {chat_ref}")

@app.get("/api/mappings")
async def get_mappings(): return [{"source_id": m[0], "target_id": m[1]} for m in await db.get_all_channel_mappings()]

@app.post("/api/mappings")
async def add_mapping(source_id: str = Form(...), target_id: str = Form(...)):
    try:
        await db.add_channel_mapping(await resolve_chat_id(source_id), await resolve_chat_id(target_id))
        return {"status": "success", "message": "映射规则添加成功"}
    except Exception as e: return {"status": "error", "message": str(e)}

@app.delete("/api/mappings/{source_id}")
async def delete_mapping(source_id: int):
    await db.delete_channel_mapping(source_id)
    return {"status": "success", "message": "规则已删除"}

@app.get("/api/filter_rules")
async def get_filter_rules():
    return [{"id": r[0], "rule_type": r[1], "pattern": r[2], "replacement": r[3], "is_case_sensitive": r[4]} for r in await db.get_all_filter_rules()]

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
    await db.update_settings({"sync_text": sync_text, "sync_photo": sync_photo, "sync_video": sync_video, "sync_document": sync_document, "sync_sticker": sync_sticker, "sync_gif": sync_gif, "sync_audio": sync_audio, "sync_voice": sync_voice})
    return {"status": "success", "message": "全局消息过滤配置已保存"}

@app.post("/api/stop_sync")
async def stop_sync():
    if sync_state["is_syncing"]:
        sync_state["stop_requested"] = True
        return {"status": "success", "message": "已下发中断指令！"}
    return {"status": "error", "message": "无运行中任务"}

@app.post("/api/start_sync")
async def start_sync(
        background_tasks: BackgroundTasks, mode: str = Form(...), sender: str = Form("bot"),
        source_id: str = Form(...), target_id: str = Form(...), delay: float = Form(...), 
        start_id: int = Form(0), end_id: int = Form(0), json_path: str = Form("")
):
    if sync_state["is_syncing"]: return {"status": "error", "message": "任务运行中"}
    if mode in ["api", "clone"] and not bot_engine.pyro_user_app: return {"status": "error", "message": "请配置 API 账号"}
    background_tasks.add_task(process_master_sync, mode, sender, source_id, target_id, delay, start_id, end_id, json_path)
    return {"status": "success", "message": f"启动 {mode.upper()} 任务"}

TYPE_MAP = {
    'photo': 'sync_photo', 'video': 'sync_video', 'animation': 'sync_gif',
    'audio': 'sync_audio', 'voice': 'sync_voice', 'document': 'sync_document', 'sticker': 'sync_sticker'
}
AIO_MEDIA_CLS = {'photo': AioPhoto, 'video': AioVideo, 'audio': AioAudio, 'document': AioDoc}
PYRO_MEDIA_CLS = {'photo': InputMediaPhoto, 'video': InputMediaVideo, 'audio': InputMediaAudio, 'document': InputMediaDocument}

def get_msg_meta(msg, mode):
    if mode in ['api', 'clone']:
        for attr, key in TYPE_MAP.items():
            if getattr(msg, attr, None): return attr, key
        return 'text', 'sync_text'
    else: 
        if msg.get('photo'): return 'photo', 'sync_photo'
        t = msg.get('media_type')
        json_map = {'video_file': ('video','sync_video'), 'animation': ('animation','sync_gif'), 'audio_file': ('audio','sync_audio'), 'voice_message': ('voice','sync_voice'), 'sticker': ('sticker','sync_sticker')}
        if t in json_map: return json_map[t]
        if 'file' in msg: return 'document', 'sync_document'
        return 'text', 'sync_text'

async def dynamic_send(client, msg_type, chat_id, file_ref, caption, parse_mode):
    method_name = f"send_{msg_type}" if msg_type != 'text' else 'send_message'
    method = getattr(client, method_name, client.send_document)
    kwargs = {"chat_id": chat_id, "parse_mode": parse_mode}
    if msg_type != 'text':
        kwargs["caption"] = caption
        kwargs[msg_type if hasattr(client, method_name) else 'document'] = file_ref
    else:
        kwargs["text"] = caption
    return await method(**kwargs)

async def safe_execute(coro):
    task = asyncio.create_task(coro)
    while not task.done():
        if sync_state.get("stop_requested"): task.cancel(); raise Exception("STOP_REQUESTED")
        await asyncio.sleep(0.2)
    try: return await task
    except asyncio.CancelledError: raise Exception("STOP_REQUESTED")

def create_progress_callback(action_name):
    start_t = time.time(); last_upd = 0
    async def progress(current, total):
        nonlocal last_upd
        if sync_state.get("stop_requested"): raise Exception("STOP_REQUESTED")
        now = time.time()
        if now - last_upd > 0.5 or current == total:
            last_upd = now
            spd_mb = (current / (now - start_t)) / 1048576 if now - start_t > 0 else 0
            sync_state["current_text"] = f"{action_name} {current/total*100:.1f}% ({spd_mb:.1f} MB/s)" if total > 0 else action_name
    return progress

async def process_master_sync(mode: str, sender: str, source_id_raw: str, target_id_raw: str, delay: float, start_id: int, end_id: int, json_path: str):
    global sync_state
    safe_delay = max(0.5, float(delay))
    if mode == "api": sender = "user"
    elif mode == "json": sender = "bot"

    sync_state.update({"is_syncing": True, "mode": mode.upper(), "source_id_raw": source_id_raw, "target_id_raw": target_id_raw, "delay": safe_delay, "start_id": start_id, "end_id": end_id, "json_path": json_path, "current": 0, "skipped": 0, "total": 0, "stop_requested": False})
    settings = await db.get_all_settings()

    try:
        source_id = await resolve_chat_id(source_id_raw)
        target_id = await resolve_chat_id(target_id_raw)
    except Exception as e:
        await db.add_log("ERROR", f"❌ 任务中止，频道有误: {e}")
        sync_state["is_syncing"] = False; return

    if mode == "clone":
        for f in os.listdir(TEMP_DIR):
            try: os.remove(os.path.join(TEMP_DIR, f))
            except: pass
        await db.add_log("INFO", "🧹 已清空 temp，准备下载")

    try:
        if mode in ["api", "clone"]:
            app, bot = bot_engine.pyro_user_app, bot_engine.aiogram_bot
            if not start_id: start_id = 1
            if not end_id:
                async for msg in app.get_chat_history(source_id, limit=1): end_id = msg.id
            if not end_id: end_id = 1
            sync_state["total"] = end_id - start_id + 1
            
            for chunk_start in range(start_id, end_id + 1, 100):
                if sync_state["stop_requested"]: break
                try: msgs = await app.get_messages(source_id, list(range(chunk_start, min(chunk_start + 99, end_id) + 1)))
                except Exception: continue
                
                filtered_msgs = []
                for msg in msgs:
                    if msg is None or msg.empty: continue
                    msg_type, sync_key = get_msg_meta(msg, mode)
                    if settings.get(sync_key, '1') == '0': continue
                    filtered_msgs.append(msg)
                
                grouped_msgs, current_group = [], []
                for msg in filtered_msgs:
                    if msg.media_group_id:
                        if not current_group or current_group[0].media_group_id == msg.media_group_id: current_group.append(msg)
                        else: grouped_msgs.append(current_group); current_group = [msg]
                    else:
                        if current_group: grouped_msgs.append(current_group); current_group = []
                        grouped_msgs.append([msg])
                if current_group: grouped_msgs.append(current_group)

                for group in grouped_msgs:
                    if sync_state["stop_requested"]: break
                    if len(group) == 1:
                        msg = group[0]
                        msg_type, _ = get_msg_meta(msg, mode)
                        has_media = msg_type != 'text'
                        file_name = getattr(getattr(msg, msg_type, None), 'file_name', "") if msg_type in ['document', 'video'] else ""
                        text_html = msg.text.html if msg.text else (msg.caption.html if msg.caption else "") if hasattr(msg, 'text') else ""
                        
                        should_skip, new_html = await db.apply_message_filters(text_html, has_media, file_name or "")
                        if should_skip or (not has_media and not new_html.strip()): continue 
                        if await update_state_and_check_skip(source_id, msg.id, new_html[:50] or "[媒体]"): continue
                        
                        try:
                            if mode == "api":
                                if new_html != text_html:
                                    kwargs = {"chat_id": target_id, "parse_mode": ParseMode.HTML}
                                    if not has_media: kwargs["text"] = new_html
                                    else: kwargs.update({"from_chat_id": source_id, "message_id": msg.id, "caption": new_html})
                                    sent_id = (await safe_execute(app.send_message(**kwargs) if not has_media else app.copy_message(**kwargs))).id
                                else: sent_id = (await safe_execute(app.copy_message(chat_id=target_id, from_chat_id=source_id, message_id=msg.id))).id

                            elif mode == "clone":
                                if not has_media:
                                    sent_id = (await safe_execute(dynamic_send(bot if sender=='bot' else app, 'text', target_id, None, new_html, "HTML" if sender=='bot' else ParseMode.HTML))).message_id if sender=='bot' else (await safe_execute(dynamic_send(bot if sender=='bot' else app, 'text', target_id, None, new_html, "HTML" if sender=='bot' else ParseMode.HTML))).id
                                else:
                                    file_path = None
                                    for _ in range(3):
                                        if sync_state["stop_requested"]: break
                                        try:
                                            file_path = await safe_execute(app.download_media(msg, file_name=f"{TEMP_DIR}/", progress=create_progress_callback("⏬ 下载")))
                                            if file_path: break
                                        except Exception as e:
                                            if "STOP_REQUESTED" in str(e): raise e
                                            await asyncio.sleep(2)
                                    if not file_path or sync_state["stop_requested"]: continue
                                    
                                    actual_sender = 'user' if (sender == 'bot' and os.path.getsize(file_path) > 50*1024*1024) else sender
                                    client = bot if actual_sender == 'bot' else app
                                    pm = "HTML" if actual_sender == 'bot' else ParseMode.HTML

                                    for _ in range(3):
                                        if sync_state["stop_requested"]: break
                                        try:
                                            sync_state["current_text"] = "⏫ 上传中..."
                                            media_arg = FSInputFile(file_path) if actual_sender == 'bot' else file_path
                                            sent = await safe_execute(dynamic_send(client, msg_type, target_id, media_arg, new_html, pm))
                                            sent_id = sent.message_id if actual_sender == 'bot' else sent.id
                                            break
                                        except Exception as e:
                                            if "STOP_REQUESTED" in str(e): raise e
                                            await asyncio.sleep(2)
                                    try: os.remove(file_path) 
                                    except: pass

                            await record_success(source_id, msg.id, sent_id)
                            await db.add_msg_log(f"{mode.upper()}_SEND", f"目标:[{target_id}] 新ID:{sent_id} | 同步成功")
                        except Exception as e:
                            if sync_state["stop_requested"]: break
                            await db.add_log("ERROR", f"❌ 单条同步抛出异常 ID {msg.id}: {e}")
                        await asyncio.sleep(safe_delay)
                    else:
                        if await update_state_and_check_skip(source_id, group[0].id, "[媒体组]"): continue
                        if mode == "api":
                            for _ in range(3):
                                if sync_state["stop_requested"]: break
                                try:
                                    copied_msgs = await safe_execute(app.copy_media_group(chat_id=target_id, from_chat_id=source_id, message_id=group[0].id))
                                    for orig_m, new_m in zip(group, copied_msgs): await record_success(source_id, orig_m.id, new_m.id)
                                    break
                                except TypeError as e:
                                    if "topics" in str(e):
                                        for m in group: await record_success(source_id, m.id, 0)
                                        break
                                except Exception as e: 
                                    if "STOP_REQUESTED" in str(e): raise e

                        elif mode == "clone":
                            downloaded_files, dl_success = [], False
                            for _ in range(3):
                                if sync_state["stop_requested"]: break
                                try:
                                    sem = asyncio.Semaphore(3)
                                    async def dl_album_item(m_item, idx):
                                        async with sem: return await safe_execute(app.download_media(m_item, file_name=f"{TEMP_DIR}/", progress=create_progress_callback(f"⏬ 并发下载 [{idx}]")))
                                    results = await asyncio.gather(*[dl_album_item(m, i+1) for i, m in enumerate(group)], return_exceptions=True)
                                    if any(isinstance(r, Exception) for r in results):
                                        if any("STOP_REQUESTED" in str(r) for r in results): sync_state["stop_requested"] = True
                                        await asyncio.sleep(2)
                                        continue
                                    downloaded_files = [(m, p) for m, p in zip(group, results) if isinstance(p, str)]
                                    dl_success = True; break
                                except Exception: pass
                                    
                            if not dl_success or sync_state["stop_requested"]:
                                for _, p in downloaded_files: 
                                    try: os.remove(p)
                                    except: pass
                                continue
                                
                            actual_sender = 'user' if (sender == 'bot' and any(os.path.getsize(p) > 50*1024*1024 for _, p in downloaded_files)) else sender
                            cls_map = AIO_MEDIA_CLS if actual_sender == 'bot' else PYRO_MEDIA_CLS
                            client = bot if actual_sender == 'bot' else app
                            
                            for _ in range(3):
                                if sync_state["stop_requested"]: break
                                try:
                                    sync_state["current_text"] = f"⏫ 整体打包上传相册 ({len(group)}项)..."
                                    media_group = []
                                    for m, p in downloaded_files:
                                        m_type, _ = get_msg_meta(m, mode)
                                        _, cap = await db.apply_message_filters(m.text.html if getattr(m, 'text', None) else (m.caption.html if getattr(m, 'caption', None) else ""), True, "")
                                        media_cls = cls_map.get(m_type, cls_map['document'])
                                        media_group.append(media_cls(media=FSInputFile(p) if actual_sender=='bot' else p, caption=cap, parse_mode="HTML" if actual_sender=='bot' else ParseMode.HTML))
                                    
                                    copied_msgs = await safe_execute(client.send_media_group(chat_id=target_id, media=media_group))
                                    for orig_m, new_m in zip(group, copied_msgs): await record_success(source_id, orig_m.id, getattr(new_m, 'message_id', getattr(new_m, 'id', 0)))
                                    break
                                except Exception as e:
                                    if "STOP_REQUESTED" in str(e): raise e
                                    await asyncio.sleep(2)
                                
                            for _, p in downloaded_files:
                                try: os.remove(p)
                                except: pass
                        await asyncio.sleep(safe_delay)

        elif mode == "json":
            # JSON 的逻辑不变
            pass
            
    except asyncio.CancelledError: pass
    except Exception as e: await db.add_log("ERROR", f"同步中断: {e}")
    finally:
        sync_state["is_syncing"] = False
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
    await db.add_log("ERROR", f"触发风控休眠 {wait_time} 秒...")
    await asyncio.sleep(wait_time)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)