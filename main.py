import asyncio
import json
import os
import html
from contextlib import asynccontextmanager
from fastapi import FastAPI, BackgroundTasks, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import uvicorn
from aiogram.types import FSInputFile
from aiogram.exceptions import TelegramRetryAfter
from pyrogram.errors import FloodWait as PyroFloodWait, BadRequest as PyroBadRequest
from pyrogram.enums import ParseMode
from dotenv import load_dotenv

import database as db
import bot_engine

load_dotenv()
PORT = int(os.getenv("PORT", 8011))
MAX_FAILS = int(os.getenv("MAX_FAILS", 10))

app_info_cache = {"bot": {"name": "", "username": ""}, "user": {"name": "", "status": "未配置"}}
sync_state = {
    "is_syncing": False, "mode": "", "total": 0, "current": 0,
    "current_text": "", "current_link": "", "skipped": 0,
    "stop_requested": False
}

@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    try:
        me = await bot_engine.aiogram_bot.get_me()
        app_info_cache["bot"] = {"name": me.first_name, "username": me.username}
        await db.add_log("INFO", f"🚀 [Aiogram 实时监听] 已就绪: {me.first_name}")
        print(f"✅ Bot 已上线: {me.first_name} (@{me.username})")
        asyncio.create_task(bot_engine.dp.start_polling(bot_engine.aiogram_bot))
    except Exception as e:
        await db.add_log("ERROR", f"Bot启动失败: {e}")

    bot_engine.init_user_client()
    if bot_engine.pyro_user_app:
        try:
            await bot_engine.pyro_user_app.start()
            user_me = await bot_engine.pyro_user_app.get_me()
            app_info_cache["user"] = {"name": user_me.first_name, "status": "已登录"}
            await db.add_log("INFO", f"👤 [历史数据引擎] 辅助账号登录成功: {user_me.first_name}")
            print(f"✅ 用户辅助账号已连接")
        except Exception as e:
            await db.add_log("ERROR", f"辅助账号登录异常: {e}")
    else:
        await db.add_log("WARNING", "⚠️ 未填写 API_ID，API拉取等历史功能不可用。")

    yield
    print("⏳ 正在安全关闭系统...")
    try:
        if bot_engine.pyro_user_app and bot_engine.pyro_user_app.is_initialized:
            await bot_engine.pyro_user_app.stop(block=False)
    except Exception: pass

    try:
        await bot_engine.aiogram_bot.session.close()
        print("✅ Bot 会话已关闭")
    except Exception as e: print(f"❌ 关闭 Bot 会话时出错: {e}")
    print("👋 系统已安全退出")

app = FastAPI(title="杏铃同步台", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def serve_index(): return FileResponse("static/index.html")

@app.get("/api/app_info")
async def get_app_info(): return app_info_cache

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
    except Exception as e:
        raise ValueError(f"无法解析频道 {chat_ref}: {e}")

@app.get("/api/mappings")
async def get_mappings(): return [{"source_id": m[0], "target_id": m[1]} for m in await db.get_all_channel_mappings()]

@app.post("/api/mappings")
async def add_mapping(source_id: str = Form(...), target_id: str = Form(...)):
    try:
        s_id = await resolve_chat_id(source_id)
        t_id = await resolve_chat_id(target_id)
        await db.add_channel_mapping(s_id, t_id)
        return {"status": "success", "message": f"规则添加成功 ({s_id} -> {t_id})"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.delete("/api/mappings/{source_id}")
async def delete_mapping(source_id: int):
    await db.delete_channel_mapping(source_id)
    return {"status": "success", "message": "规则已删除"}

@app.get("/api/filter_rules")
async def get_filter_rules():
    rules = await db.get_all_filter_rules()
    return [{"id": r[0], "rule_type": r[1], "pattern": r[2], "replacement": r[3]} for r in rules]

@app.post("/api/filter_rules")
async def add_filter_rule(rule_type: str = Form(...), pattern: str = Form(...), replacement: str = Form("")):
    await db.add_filter_rule(rule_type, pattern, replacement)
    return {"status": "success", "message": "过滤规则添加成功"}

@app.delete("/api/filter_rules/{rule_id}")
async def delete_filter_rule(rule_id: int):
    await db.delete_filter_rule(rule_id)
    return {"status": "success", "message": "规则已删除"}

@app.get("/api/sync_status")
async def get_sync_status(): return sync_state

@app.get("/api/logs/backend")
async def get_backend_logs(): return [{"time": l[0], "level": l[1], "msg": l[2]} for l in await db.get_recent_logs()]

@app.post("/api/stop_sync")
async def stop_sync():
    if sync_state["is_syncing"]:
        sync_state["stop_requested"] = True
        return {"status": "success", "message": "已发送停止指令，正在安全退出..."}
    return {"status": "error", "message": "没有运行中的任务"}

@app.post("/api/start_sync")
async def start_sync(
        background_tasks: BackgroundTasks, mode: str = Form(...),
        source_id: str = Form(...), target_id: str = Form(...), delay: float = Form(...), 
        start_id: int = Form(0), end_id: int = Form(0), json_path: str = Form("")
):
    if sync_state["is_syncing"]: return {"status": "error", "message": "任务运行中！"}
    
    if mode in ["api", "blind"] and not bot_engine.pyro_user_app:
        asyncio.create_task(db.add_log("ERROR", "❌ 操作受限：API/盲猜模式必须配置 API 账号。"))
        return {"status": "error", "message": "API信息未配置"}
        
    if mode == "json" and not os.path.exists(json_path): return {"status": "error", "message": "找不到 JSON 文件！"}

    background_tasks.add_task(process_master_sync, mode, source_id, target_id, delay, start_id, end_id, json_path)
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
            elif t_type in ['text_link', 'link']:
                href = t.get('href', inner)
                html_text += f"<a href='{href}'>{inner}</a>"
            else: html_text += inner
    return html_text

async def process_master_sync(mode: str, source_id_raw: str, target_id_raw: str, delay: float, start_id: int, end_id: int, json_path: str):
    global sync_state
    safe_delay = max(0.5, float(delay))
    sync_state.update({"is_syncing": True, "mode": mode.upper(), "current": 0, "skipped": 0, "total": 0, "stop_requested": False})

    try:
        source_id = await resolve_chat_id(source_id_raw)
        target_id = await resolve_chat_id(target_id_raw)
    except Exception as e:
        await db.add_log("ERROR", f"❌ 任务中止，频道信息有误: {e}")
        sync_state["is_syncing"] = False
        return

    try:
        if mode == "api":
            app = bot_engine.pyro_user_app
            if not start_id: start_id = 1
            if not end_id:
                async for msg in app.get_chat_history(source_id, limit=1):
                    end_id = msg.id
            if not end_id: end_id = 1
            
            sync_state["total"] = end_id - start_id + 1
            chunk_size = 100
            
            await db.add_log("INFO", f"🚀 [API模式] 开始拉取 ID: {start_id} 到 {end_id} (使用辅助账号)")
            
            for chunk_start in range(start_id, end_id + 1, chunk_size):
                if sync_state["stop_requested"]: break
                chunk_end = min(chunk_start + chunk_size - 1, end_id)
                ids_to_fetch = list(range(chunk_start, chunk_end + 1))
                
                try:
                    msgs = await app.get_messages(source_id, ids_to_fetch)
                except Exception as e:
                    await db.add_log("ERROR", f"❌ 批量获取历史失败: {e}")
                    continue
                
                grouped_msgs = []
                current_group = []
                for msg in msgs:
                    if msg is None or msg.empty: continue
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

                        should_skip, new_html = await db.apply_message_filters(text_html, has_media, file_name or "")
                        if should_skip: continue
                        if not has_media and not new_html.strip(): continue 

                        if await update_state_and_check_skip(source_id, msg.id, new_html[:50] or "[单条媒体]"): continue
                        
                        try:
                            if new_html != text_html:
                                if not has_media: copied = await app.send_message(chat_id=target_id, text=new_html, parse_mode=ParseMode.HTML)
                                else: copied = await app.copy_message(chat_id=target_id, from_chat_id=source_id, message_id=msg.id, caption=new_html, parse_mode=ParseMode.HTML)
                            else:
                                copied = await app.copy_message(chat_id=target_id, from_chat_id=source_id, message_id=msg.id)
                            
                            await record_success(source_id, msg.id, copied.id)
                        except PyroFloodWait as e:
                            await handle_floodwait(e.value)
                        except Exception as e:
                            await db.add_log("ERROR", f"❌ 单条同步失败 ID {msg.id}: {e}")
                        
                        await asyncio.sleep(safe_delay)
                    
                    else:
                        # ===== 媒体组处理分支 =====
                        all_skipped = True
                        should_skip_group = False
                        
                        for m in group:
                            try: t_html = m.text.html if m.text else (m.caption.html if m.caption else "")
                            except: t_html = m.text or m.caption or ""
                            f_name = m.document.file_name if m.document else (m.video.file_name if m.video else "")
                                
                            s_skip, _ = await db.apply_message_filters(t_html, True, f_name or "")
                            if s_skip:
                                should_skip_group = True; break 
                                
                            sync_state["current"] += 1
                            sync_state["current_link"] = f"t.me/c/{str(source_id).replace('-100', '')}/{m.id}"
                            sync_state["current_text"] = f"[打包同步媒体组: {len(group)}张]"
                            if not await db.is_message_synced(source_id, m.id): all_skipped = False
                            else: sync_state["skipped"] += 1
                                
                        msg_ids = [m.id for m in group]
                        if should_skip_group or all_skipped: continue
                            
                        success = False
                        for attempt in range(3):
                            if sync_state["stop_requested"]: break
                            try:
                                copied_msgs = await app.copy_media_group(chat_id=target_id, from_chat_id=source_id, message_id=msg_ids[0])
                                for orig_m, new_m in zip(group, copied_msgs):
                                    await record_success(source_id, orig_m.id, new_m.id)
                                success = True; break
                            except PyroFloodWait as e:
                                await db.add_log("WARNING", f"⚠️ 触发风控，等待 {e.value} 秒重试...")
                                await asyncio.sleep(e.value)
                            except TypeError as e:
                                # 核心修复：识破 Pyrofork 库解析 Bug
                                if "topics" in str(e) or "Messages.__init__" in str(e):
                                    await db.add_log("SUCCESS", f"✅ [Bug规避] 媒体组实际已发送成功 IDs {msg_ids}")
                                    for m in group:
                                        await record_success(source_id, m.id, 0)
                                    success = True; break
                                else:
                                    await db.add_log("ERROR", f"❌ 批量相册转发解析失败 IDs {msg_ids}: {e}")
                                    break 
                            except Exception as e:
                                await db.add_log("ERROR", f"❌ 批量相册转发失败 IDs {msg_ids}: {e}")
                                break 
                        
                        if not success and not sync_state["stop_requested"]:
                            await db.add_log("WARNING", f"🔄 启动安全降级：将这 {len(msg_ids)} 张图拆散为单条逐个发送")
                            for m in group:
                                if sync_state["stop_requested"]: break # 修复：降级过程中允许即时打断
                                try:
                                    copied = await app.copy_message(chat_id=target_id, from_chat_id=source_id, message_id=m.id)
                                    await record_success(source_id, m.id, copied.id)
                                    await asyncio.sleep(safe_delay)
                                except PyroFloodWait as e:
                                    await handle_floodwait(e.value)
                                except Exception as ex:
                                    await db.add_log("ERROR", f"❌ 降级单条发送失败 ID {m.id}: {ex}")
                        elif success and not sync_state["stop_requested"]:
                            await asyncio.sleep(safe_delay)

        elif mode == "blind":
            app = bot_engine.pyro_user_app
            if start_id == 0 or end_id == 0: raise ValueError("必须填写起止ID！")
            sync_state["total"] = end_id - start_id + 1
            consecutive_fails = 0
            
            for msg_id in range(start_id, end_id + 1):
                if sync_state["stop_requested"]: break
                if await update_state_and_check_skip(source_id, msg_id, "盲猜尝试中..."): 
                    consecutive_fails = 0; continue
                try:
                    copied = await app.copy_message(chat_id=target_id, from_chat_id=source_id, message_id=msg_id)
                    await record_success(source_id, msg_id, copied.id)
                    sync_state["current_text"] = "✅ 同步成功"
                    consecutive_fails = 0
                except PyroFloodWait as e:
                    await handle_floodwait(e.value)
                except PyroBadRequest:
                    sync_state["current_text"] = "❌ 消息不存在"
                    consecutive_fails += 1
                    if consecutive_fails >= MAX_FAILS:
                        await db.add_log("ERROR", f"🛑 触发熔断！连续 {MAX_FAILS} 次失败，任务强制终止！")
                        sync_state["stop_requested"] = True
                        break
                except Exception as e:
                    pass
                await asyncio.sleep(safe_delay)

        elif mode == "json":
            bot = bot_engine.aiogram_bot
            with open(json_path, 'r', encoding='utf-8') as f: data = json.load(f)
            base_dir = os.path.dirname(os.path.abspath(json_path))
            msgs = [m for m in data.get('messages', []) if m.get('type') == 'message']
            if start_id and end_id: msgs = [m for m in msgs if start_id <= m.get('id', 0) <= end_id]
            sync_state["total"] = len(msgs)

            for m in msgs:
                if sync_state["stop_requested"]: break
                msg_id = m.get('id')
                
                text_html = parse_tg_json_text(m.get('text', []))
                has_media = 'photo' in m or 'file' in m or 'media_type' in m
                file_name = m.get('file', '') 
                
                should_skip, new_html = await db.apply_message_filters(text_html, has_media, file_name)
                if should_skip or (not has_media and not new_html.strip()): continue 

                if await update_state_and_check_skip(source_id, msg_id, new_html[:50] or "[媒体]"): continue
                media_path = m.get('photo') or m.get('file')
                abs_media_path = os.path.join(base_dir, media_path) if media_path else None

                try:
                    sent_id = None
                    if abs_media_path and os.path.exists(abs_media_path):
                        media_file = FSInputFile(abs_media_path)
                        if m.get('photo'): sent = await bot.send_photo(chat_id=target_id, photo=media_file, caption=new_html, parse_mode="HTML")
                        elif m.get('media_type') == 'video_file': sent = await bot.send_video(chat_id=target_id, video=media_file, caption=new_html, parse_mode="HTML")
                        else: sent = await bot.send_document(chat_id=target_id, document=media_file, caption=new_html, parse_mode="HTML")
                        sent_id = sent.message_id
                    elif new_html.strip():
                        sent_id = (await bot.send_message(chat_id=target_id, text=new_html, parse_mode="HTML")).message_id

                    if sent_id: await record_success(source_id, msg_id, sent_id)
                except TelegramRetryAfter as e:
                    await handle_floodwait(e.retry_after)
                except Exception as e:
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
    await db.add_log("SUCCESS", f"已成功同步: ID {msg_id}")

async def handle_floodwait(wait_time):
    await db.add_log("ERROR", f"触发速率限制，强制休眠 {wait_time} 秒...")
    await asyncio.sleep(wait_time)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)