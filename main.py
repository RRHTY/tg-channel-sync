import asyncio
import json
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, BackgroundTasks, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import uvicorn
from pyrogram.errors import FloodWait, MessageIdInvalid

import database as db
import bot_engine

bot_app = None
user_app = None
# 缓存双核身份信息
app_info_cache = {"bot": {"name": "", "username": ""}, "user": {"name": ""}}
sync_state = {
    "is_syncing": False, "mode": "", "total": 0, "current": 0,
    "current_text": "", "current_link": "", "skipped": 0,
    "stop_requested": False # 新增：停止信号标志
}

@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot_app, user_app
    await db.init_db()

    bot_app, user_app = bot_engine.init_clients()

    try:
        await bot_app.start()
        me = await bot_app.get_me()
        app_info_cache["bot"] = {"name": me.first_name, "username": me.username}
        await db.add_log("INFO", f"🚀 系统启动，Bot [{me.first_name}] 已上线")
        print(f"✅ Bot 已上线: {me.first_name} (@{me.username})")
    except Exception as e:
        await db.add_log("ERROR", f"机器人启动失败: {e}")

    try:
        await user_app.start()
        user_me = await user_app.get_me()
        app_info_cache["user"] = {"name": user_me.first_name or "辅助账号"}
        print(f"✅ 用户账号登录成功: {user_me.first_name}")
        await db.add_log("INFO", f"👤 辅助账号 [{user_me.first_name}] 登录成功，API 模式已解锁！")
    except Exception as e:
        await db.add_log("ERROR", f"用户账号登录失败: {e}")

    yield

    print("⏳ 正在安全关闭系统...")
    try:
        await bot_app.stop(block=False)
        await user_app.stop(block=False)
    except Exception:
        pass
    print("👋 系统已安全退出")

app = FastAPI(title="杏铃同步台", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def serve_index(): return FileResponse("static/index.html")

# 更改为返回双核信息
@app.get("/api/app_info")
async def get_app_info(): return app_info_cache

@app.get("/api/mappings")
async def get_mappings():
    return [{"source_id": m[0], "target_id": m[1]} for m in await db.get_all_channel_mappings()]

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
async def get_backend_logs():
    return [{"time": l[0], "level": l[1], "msg": l[2]} for l in await db.get_recent_logs()]

# 新增：停止任务的 API
@app.post("/api/stop_sync")
async def stop_sync():
    if sync_state["is_syncing"]:
        sync_state["stop_requested"] = True
        return {"status": "success", "message": "已发送停止指令，正在等待当前消息处理完毕..."}
    return {"status": "error", "message": "当前没有运行的任务"}

@app.post("/api/start_sync")
async def start_sync(
        background_tasks: BackgroundTasks,
        mode: str = Form(...),
        source_id: int = Form(...),
        target_id: int = Form(...),
        delay: float = Form(...),
        start_id: int = Form(0),
        end_id: int = Form(0),
        json_path: str = Form("")
):
    if sync_state["is_syncing"]: return {"status": "error", "message": "当前已有同步任务在运行！"}
    if mode == "json" and not os.path.exists(json_path): return {"status": "error", "message": "找不到指定的 JSON 文件！"}

    background_tasks.add_task(process_master_sync, mode, source_id, target_id, delay, start_id, end_id, json_path)
    return {"status": "success", "message": f"已启动 [{mode.upper()}] 模式同步任务"}

async def process_master_sync(mode: str, source_id: int, target_id: int, delay: float, start_id: int, end_id: int, json_path: str):
    global sync_state
    safe_delay = max(0.5, float(delay))
    # 初始化状态，清除之前的停止标志
    sync_state.update({"is_syncing": True, "mode": mode.upper(), "current": 0, "skipped": 0, "total": 0, "stop_requested": False})
    await db.add_log("INFO", f"🚀 启动历史同步，模式: {mode.upper()}")

    try:
        if mode == "api":
            history_gen = user_app.get_chat_history(source_id)
            messages_to_process = []
            async for msg in history_gen:
                # 检查是否按下停止键
                if sync_state["stop_requested"]: break
                if start_id and end_id and not (start_id <= msg.id <= end_id): continue
                if start_id and msg.id < start_id: continue
                if end_id and msg.id > end_id: continue
                messages_to_process.append(msg)

            if sync_state["stop_requested"]:
                await db.add_log("WARNING", "⏹ 数据拉取阶段已手动终止！")
            else:
                messages_to_process.reverse()
                sync_state["total"] = len(messages_to_process)

                for msg in messages_to_process:
                    if sync_state["stop_requested"]:
                        await db.add_log("WARNING", "⏹ 任务已被手动终止！")
                        break

                    if await update_state_and_check_skip(source_id, msg.id, msg.text or "[多媒体]"): continue
                    try:
                        copied = await bot_app.copy_message(target_id, source_id, msg.id)
                        await record_success(source_id, msg.id, copied.id)
                    except FloodWait as e:
                        await handle_floodwait(e)
                    except Exception as e:
                        await db.add_log("ERROR", f"API同步失败 ID {msg.id}: {e}")
                    await asyncio.sleep(safe_delay)

        elif mode == "blind":
            if start_id == 0 or end_id == 0 or start_id > end_id: raise ValueError("盲猜模式必须填起止ID！")
            sync_state["total"] = end_id - start_id + 1

            for msg_id in range(start_id, end_id + 1):
                if sync_state["stop_requested"]:
                    await db.add_log("WARNING", "⏹ 任务已被手动终止！")
                    break

                if await update_state_and_check_skip(source_id, msg_id, "盲猜尝试中..."): continue
                try:
                    copied = await bot_app.copy_message(target_id, source_id, msg_id)
                    await record_success(source_id, msg_id, copied.id)
                    sync_state["current_text"] = "✅ 同步成功"
                except FloodWait as e:
                    await handle_floodwait(e)
                except Exception:
                    await db.add_log("WARNING", f"消息不存在或删除，跳过 ID {msg_id}")
                    sync_state["current_text"] = "❌ 消息不存在"
                await asyncio.sleep(safe_delay)

        elif mode == "json":
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            base_dir = os.path.dirname(os.path.abspath(json_path))
            msgs = [m for m in data.get('messages', []) if m.get('type') == 'message']

            if start_id and end_id: msgs = [m for m in msgs if start_id <= m.get('id', 0) <= end_id]
            sync_state["total"] = len(msgs)

            for m in msgs:
                if sync_state["stop_requested"]:
                    await db.add_log("WARNING", "⏹ 任务已被手动终止！")
                    break

                msg_id = m.get('id')
                text = "".join([t if isinstance(t, str) else t.get('text', '') for t in m.get('text', [])])
                if await update_state_and_check_skip(source_id, msg_id, text[:50] or "[多媒体]"): continue

                media_path = m.get('photo') or m.get('file')
                abs_media_path = os.path.join(base_dir, media_path) if media_path else None

                try:
                    sent = None
                    if abs_media_path and os.path.exists(abs_media_path):
                        if m.get('photo'): sent = await bot_app.send_photo(target_id, photo=abs_media_path, caption=text)
                        elif m.get('media_type') == 'video_file': sent = await bot_app.send_video(target_id, video=abs_media_path, caption=text)
                        else: sent = await bot_app.send_document(target_id, document=abs_media_path, caption=text)
                    elif text.strip():
                        sent = await bot_app.send_message(target_id, text=text)

                    if sent: await record_success(source_id, msg_id, sent.id)
                except FloodWait as e:
                    await handle_floodwait(e)
                except Exception as e:
                    await db.add_log("ERROR", f"JSON发送失败 ID {msg_id}: {e}")

                await asyncio.sleep(safe_delay)

    except asyncio.CancelledError:
        # 核心：修复退出时的 KeyboardInterrupt / CancelledError 报错
        await db.add_log("WARNING", "进程被强制终止")
    except Exception as e:
        await db.add_log("ERROR", f"同步任务异常中断: {e}")
    finally:
        sync_state["is_syncing"] = False
        sync_state["stop_requested"] = False
        if not sync_state.get("stop_requested", False):
            await db.add_log("INFO", "✅ 当前同步任务执行/取消完毕！")

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

async def handle_floodwait(e):
    wait_time = e.value + 1
    await db.add_log("ERROR", f"触发速率限制，强制休眠 {wait_time} 秒...")
    await asyncio.sleep(wait_time)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)