import logging
import urllib.request
from pyrogram import Client, filters
from pyrogram.handlers import MessageHandler, EditedMessageHandler
import database as db

# ================= 配置区 =================
API_ID =
API_HASH = ""
BOT_TOKEN = ""
# ==========================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

bot_app = None  # 机器人实例 (负责监听和干活)
user_app = None # 用户实例 (专门负责查历史记录)

# ================= 核心处理函数 (由机器人执行) =================
async def debug_all_messages(client, message):
    logging.info(f"📩 [收到信号] 来源ID: {message.chat.id} | 类型: {message.chat.type}")

async def handle_new_post(client, message):
    source_id = message.chat.id
    target_id = await db.get_target_channel(source_id)
    if not target_id: return

    try:
        copied = await message.copy(target_id)
        await db.save_msg_mapping(source_id, message.id, copied.id)
        await db.add_log("SUCCESS", f"[实时同步] 成功: {source_id} -> {target_id}")
    except Exception as e:
        await db.add_log("ERROR", f"[实时同步] 失败 ID {message.id}: {e}")

async def handle_edited_post(client, message):
    if not message.chat: return
    source_id = message.chat.id
    target_id = await db.get_target_channel(source_id)
    if not target_id: return

    target_msg_id = await db.get_target_msg_id(source_id, message.id)
    if not target_msg_id: return

    try:
        if message.text:
            await client.edit_message_text(target_id, target_msg_id, text=message.text, entities=message.entities)
        elif message.caption is not None:
            await client.edit_message_caption(target_id, target_msg_id, caption=message.caption, caption_entities=message.caption_entities)
        await db.add_log("INFO", f"[修改同步] 已更新: {source_id} -> {target_id}")
    except Exception as e:
        await db.add_log("ERROR", f"[修改同步] 失败 ID {message.id}: {e}")

# ================= 初始化双核引擎 =================
def init_clients():
    global bot_app, user_app

    # 1. 清除幽灵 Webhook
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook", timeout=3).read()
    except Exception:
        pass

    # 2. 实例化机器人
    bot_app = Client(
        "sync_bot_session",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        ipv6=False
    )

    # 3. 实例化真实用户账号
    user_app = Client(
        "sync_user_session",
        api_id=API_ID,
        api_hash=API_HASH,
        ipv6=False
    )

    # 4. 监听器只挂载给机器人，用户账号只作为工具人
    bot_app.add_handler(MessageHandler(debug_all_messages, filters.all), group=0)
    bot_app.add_handler(MessageHandler(handle_new_post, filters.channel), group=1)
    bot_app.add_handler(EditedMessageHandler(handle_edited_post, filters.channel), group=1)

    return bot_app, user_app