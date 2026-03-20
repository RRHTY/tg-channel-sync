import asyncio
import json
import os
import html # 新增：用于处理富文本的 HTML 转义
from contextlib import asynccontextmanager
from fastapi import FastAPI, BackgroundTasks, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import uvicorn
from aiogram.types import FSInputFile
from aiogram.exceptions import TelegramRetryAfter, TelegramBadRequest

import database as db
import bot_engine

app_info_cache = {"bot": {"name": "", "username": ""}, "user": {"name": "", "status": "未配置"}}
sync_state = {
    "is_syncing": False, "mode": "", "total": 0, "current": 0,
    "current_text": "", "current_link": "", "skipped": 0,
    "stop_requested": False
}

@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()

    # 1. 启动 Aiogram (纯 Bot 模式)
    try:
        me = await bot_engine.aiogram_bot.get_me()
        app_info_cache["bot"] = {"name": me.first_name, "username": me.username}
        await db.add_log("INFO", f"🚀 [Aiogram 纯Bot模式] 已就绪: {me.first_name}")
        print(f"✅ Bot 已上线: {me.first_name} (@{me.username})")
        # 挂载后台轮询监听
        asyncio.create_task(bot_engine.dp.start_polling(bot_engine.aiogram_bot))
    except Exception as e:
        await db.add_log("ERROR", f"Bot启动失败: {e}")

    # 2. 尝试启动 Pyrofork (API 辅助账号)
    bot_engine.init_user_client()
    if bot_engine.pyro_user_app:
        try:
            await bot_engine.pyro_user_app.start()
            user_me = await bot_engine.pyro_user_app.get_me()
            app_info_cache["user"] = {"name": user_me.first_name, "status": "已登录"}
            await db.add_log("INFO", f"👤 [API 模式解锁] 辅助账号登录成功: {user_me.first_name}")
            print(f"✅ 用户辅助账号已连接")
        except Exception as e:
            await db.add_log("ERROR", f"辅助账号登录异常: {e}")
    else:
        await db.add_log("WARNING", "⚠️ 未填写 API_ID，当前系统运行在 [Aiogram 纯Bot模式]。API拉取功能暂时不可用。")

    yield

    print("⏳ 正在安全关闭系统...")
    try:
        if bot_engine.pyro_user_app and bot_engine.pyro_user_app.is_initialized:
            await bot_engine.pyro_user_app.stop(block=False)
    except Exception:
        pass

    try:
        await bot_engine.aiogram_bot.session.close()
        print("✅ Bot 会话已关闭")
    except Exception as e:
        print(f"❌ 关闭 Bot 会话时出错: {e}")

    print("👋 系统已安全退出")

app = FastAPI(title="杏铃同步台", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def serve_index(): return FileResponse("static/index.html")

@app.get("/api/app_info")
async def get_app_info(): return app_info_cache

@app.get("/api/mappings")
async def get_mappings(): return [{"source_id": m[0], "target_id": m[1]} for m in await db.get_all_channel_mappings()]

@app.post("/api/mappings")
async def add_mapping(source_id: int = Form(...), target_id: int = Form(...)):
    await db.add_channel_mapping(source_id, target_id)
    return {"status": "success", "message": "规则添加成功"}

@app.delete("/api/mappings/{source_id}")
async def delete_mapping(source_id: int):
    await db.delete_channel_mapping(source_id)
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
        background_tasks: BackgroundTasks, mode: str = Form(...), source_id: int = Form(...),
        target_id: int = Form(...), delay: float = Form(...), start_id: int = Form(0),
        end_id: int = Form(0), json_path: str = Form("")
):
    if sync_state["is_syncing"]: return {"status": "error", "message": "任务运行中！"}
    
    if mode == "api" and not bot_engine.pyro_user_app:
        error_msg = "❌ API模式受限：您未在代码中填写 API_ID。当前处于纯Bot模式，请使用 JSON 或 盲猜 功能。"
        asyncio.create_task(db.add_log("ERROR", error_msg))
        return {"status": "error", "message": "API信息未配置，请查看系统日志"}

    if mode == "json" and not os.path.exists(json_path): return {"status": "error", "message": "找不到 JSON 文件！"}

    background_tasks.add_task(process_master_sync, mode, source_id, target_id, delay, start_id, end_id, json_path)
    return {"status": "success", "message": f"已启动 {mode.upper()} 任务"}


# ================= 新增：JSON 富文本解析器 =================
def parse_tg_json_text(text_list):
    """将 TG 导出的 JSON 文本实体解析为安全的 HTML 格式"""
    if isinstance(text_list, str): 
        return html.escape(text_list) # 防止注入
    
    html_text = ""
    for t in text_list:
        if isinstance(t, str):
            html_text += html.escape(t)
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
            else: 
                html_text += inner
    return html_text
# =========================================================


async def process_master_sync(mode: str, source_id: int, target_id: int, delay: float, start_id: int, end_id: int, json_path: str):
    global sync_state
    safe_delay = max(0.5, float(delay))
    sync_state.update({"is_syncing": True, "mode": mode.upper(), "current": 0, "skipped": 0, "total": 0, "stop_requested": False})
    bot = bot_engine.aiogram_bot 

    try:
        if mode == "api":
            # 优化：动态获取结束 ID 防止全量拉取
            if not start_id: start_id = 1
            if not end_id:
                async for msg in bot_engine.pyro_user_app.get_chat_history(source_id, limit=1):
                    end_id = msg.id
            if not end_id: end_id = 1
            
            sync_state["total"] = end_id - start_id + 1
            
            # 核心优化：100条分块拉取，彻底解决内存爆炸，天然从旧到新排序
            chunk_size = 100
            for chunk_start in range(start_id, end_id + 1, chunk_size):
                if sync_state["stop_requested"]: break
                chunk_end = min(chunk_start + chunk_size - 1, end_id)
                ids_to_fetch = list(range(chunk_start, chunk_end + 1))
                
                try:
                    msgs = await bot_engine.pyro_user_app.get_messages(source_id, ids_to_fetch)
                except Exception as e:
                    await db.add_log("ERROR", f"批量获取历史失败: {e}")
                    continue
                
                for msg in msgs:
                    if sync_state["stop_requested"]: break
                    if msg is None or msg.empty: continue # 跳过已被删除的消息
                    
                    if await update_state_and_check_skip(source_id, msg.id, msg.text or "[媒体]"): continue
                    try:
                        copied = await bot.copy_message(chat_id=target_id, from_chat_id=source_id, message_id=msg.id)
                        await record_success(source_id, msg.id, copied.message_id)
                    except TelegramRetryAfter as e:
                        await handle_floodwait(e.retry_after)
                    except Exception as e:
                        await db.add_log("ERROR", f"API同步失败 ID {msg.id}: {e}")
                    await asyncio.sleep(safe_delay)

        elif mode == "blind":
            if start_id == 0 or end_id == 0: raise ValueError("必须填写起止ID！")
            sync_state["total"] = end_id - start_id + 1
            
            consecutive_fails = 0
            max_fails = 10 # 熔断阈值
            
            for msg_id in range(start_id, end_id + 1):
                if sync_state["stop_requested"]: break
                
                if await update_state_and_check_skip(source_id, msg_id, "盲猜尝试中..."): 
                    consecutive_fails = 0
                    continue
                try:
                    copied = await bot.copy_message(chat_id=target_id, from_chat_id=source_id, message_id=msg_id)
                    await record_success(source_id, msg_id, copied.message_id)
                    sync_state["current_text"] = "✅ 同步成功"
                    consecutive_fails = 0
                except TelegramRetryAfter as e:
                    await handle_floodwait(e.retry_after)
                except TelegramBadRequest:
                    sync_state["current_text"] = "❌ 消息不存在"
                    consecutive_fails += 1
                    if consecutive_fails >= max_fails:
                        await db.add_log("ERROR", f"🛑 触发熔断！连续 {max_fails} 个 ID 不存在，任务强制终止！")
                        sync_state["stop_requested"] = True
                        break
                await asyncio.sleep(safe_delay)

        elif mode == "json":
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            base_dir = os.path.dirname(os.path.abspath(json_path))
            msgs = [m for m in data.get('messages', []) if m.get('type') == 'message']
            if start_id and end_id: msgs = [m for m in msgs if start_id <= m.get('id', 0) <= end_id]
            sync_state["total"] = len(msgs)

            for m in msgs:
                if sync_state["stop_requested"]: break
                msg_id = m.get('id')
                
                # 核心优化：调用富文本解析器
                text = parse_tg_json_text(m.get('text', []))
                
                if await update_state_and_check_skip(source_id, msg_id, text[:50] or "[媒体]"): continue

                media_path = m.get('photo') or m.get('file')
                abs_media_path = os.path.join(base_dir, media_path) if media_path else None

                try:
                    sent = None
                    if abs_media_path and os.path.exists(abs_media_path):
                        media_file = FSInputFile(abs_media_path)
                        # 加入 parse_mode="HTML" 支持富文本
                        if m.get('photo'): sent = await bot.send_photo(chat_id=target_id, photo=media_file, caption=text, parse_mode="HTML")
                        elif m.get('media_type') == 'video_file': sent = await bot.send_video(chat_id=target_id, video=media_file, caption=text, parse_mode="HTML")
                        else: sent = await bot.send_document(chat_id=target_id, document=media_file, caption=text, parse_mode="HTML")
                    elif text.strip():
                        sent = await bot.send_message(chat_id=target_id, text=text, parse_mode="HTML")

                    if sent: await record_success(source_id, msg_id, sent.message_id)
                except TelegramRetryAfter as e:
                    await handle_floodwait(e.retry_after)
                except Exception as e:
                    await db.add_log("ERROR", f"发送失败 ID {msg_id}: {e}")
                await asyncio.sleep(safe_delay)

    except asyncio.CancelledError:
        pass
    except Exception as e:
        await db.add_log("ERROR", f"同步中断: {e}")
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
    uvicorn.run(app, host="0.0.0.0", port=8011)