import asyncio
import logging
import os
import re
import time
from math import ceil

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.types import Message
from aiogram.types import ReplyParameters
from pyrogram import Client, raw
from pyrogram.enums import ParseMode
from pyrogram.errors import SessionPasswordNeeded

import database as db
from app_config import get_config
from app_paths import pyrogram_user_session_base
from services.sync_services import (
    SAVED_MESSAGES_TARGET_ID,
    begin_saved_messages_account_switch,
    begin_saved_messages_operation,
    build_link_rewrite_context,
    execute_with_network_retry,
    finish_saved_messages_account_switch,
    finish_saved_messages_operation,
    get_quote_payload,
    is_saved_messages_target,
    resolve_destination_chat_id,
    resolve_reply_for_forward,
    rewrite_message_links,
)
from sync_worker.core.media import has_media_spoiler
from sync_worker.core.progress import (
    ProgressFSInputFile,
    UploadProgressTracker,
    build_pyro_progress_callback,
    format_upload_label,
)
from sync_worker.core.text import prepend_source_header_html
from sync_worker.media import prepare_media_for_send
from sync_worker.runtime import TEMP_DIR, sync_state
from sync_worker.senders import (
    build_bot_media_group,
    build_user_media_group,
    dynamic_send,
    resolve_upload_target,
    should_fallback_to_user,
)


def _patch_pyrofork_messages_topics_default() -> None:
    """Pyrofork TL layer 220 起 raw.types.messages.Messages 构造函数要求 topics 参数，
    但其内部 copy_media_group / send_media_group / send_paid_media 发送成功后手动构造
    该类型时漏传 topics，导致回包解析抛 TypeError（消息实际已发送、映射无法落库）。
    这里给 topics 补默认值，兼容旧版库的全部手动构造点；read() 反序列化路径显式传值不受影响。"""
    messages_cls = raw.types.messages.Messages
    original_init = messages_cls.__init__

    def _init_with_topics_default(self, *, messages, chats, users, topics=None):
        original_init(
            self,
            messages=messages,
            chats=chats,
            users=users,
            topics=topics if topics is not None else [],
        )

    messages_cls.__init__ = _init_with_topics_default


_patch_pyrofork_messages_topics_default()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("tg-channel-sync.bot")

aiogram_bot = None
pyro_user_app = None
upload_bots = []
bot_api_mode = "cloud"
dp = Dispatcher()
MEDIA_GROUP_FLUSH_DELAY_SECONDS = 2.0
media_group_cache = {}
media_group_tasks = {}
user_auth_state = {
    "client": None,
    "phone_number": "",
    "phone_code_hash": "",
    "awaiting_code": False,
    "awaiting_password": False,
    "password_hint": "",
    "send_code_available_at": 0.0,
}
upload_bot_rr_index = 0
RETRY_AFTER_RE = re.compile(r"retry after\s+(?P<seconds>\d+)", re.IGNORECASE)

MSG_TYPES = ["photo", "video", "animation", "audio", "voice", "sticker", "document"]
REALTIME_REUPLOAD_TYPES = {"photo", "video", "animation", "audio", "voice", "sticker", "document"}


def _telegram_config():
    return get_config()["telegram"]


def _proxy_config():
    return get_config()["proxy"]


def _debug_terminal_logs_enabled() -> bool:
    return bool(get_config().get("app", {}).get("debug_terminal_logs", False))


def _message_preview(message: Message) -> str:
    return (getattr(message, "text", None) or getattr(message, "caption", None) or "")[:200]


def build_proxy_url():
    proxy = _proxy_config()
    if not proxy.get("enabled") or not proxy.get("host"):
        return None
    if proxy.get("username") and proxy.get("password"):
        return f"socks5://{proxy['username']}:{proxy['password']}@{proxy['host']}:{proxy['port']}"
    return f"socks5://{proxy['host']}:{proxy['port']}"


def has_bot_token():
    return bool(_telegram_config().get("bot_token"))


def has_local_bot_api_server():
    return bool(_telegram_config().get("bot_api_base_url"))


def has_user_api_credentials():
    telegram = _telegram_config()
    return bool(telegram.get("api_id") and telegram.get("api_hash"))


def _build_bot_client(bot_token: str, *, use_local_api: bool = False):
    session_kwargs = {"timeout": 3600, "proxy": build_proxy_url()}
    if use_local_api and _telegram_config().get("bot_api_base_url"):
        session_kwargs["api"] = TelegramAPIServer.from_base(_telegram_config()["bot_api_base_url"])
    session = AiohttpSession(**session_kwargs)
    return Bot(token=bot_token, session=session)


def _build_upload_bot_pool(primary_bot, primary_token: str, *, use_local_api: bool):
    telegram = _telegram_config()
    seen_tokens = set()
    pool = []

    def add_bot(client, token: str, label: str):
        clean_token = str(token or "").strip()
        if not clean_token or clean_token in seen_tokens:
            return
        seen_tokens.add(clean_token)
        pool.append(
            {
                "client": client,
                "token": clean_token,
                "label": label,
                "window_started_at": 0.0,
                "uploaded_bytes": 0,
                "cooldown_until": 0.0,
                "disabled": False,
                "disabled_reason": "",
            }
        )

    if primary_bot is not None:
        add_bot(primary_bot, primary_token, "主 Bot")

    for index, token in enumerate(telegram.get("extra_bot_tokens", []), start=2):
        try:
            add_bot(_build_bot_client(str(token).strip(), use_local_api=use_local_api), token, f"Bot#{index}")
        except Exception as exc:
            logging.warning("Init extra bot failed for Bot#%s: %s", index, exc)

    return pool


def init_bot_client(*, use_local_api: bool | None = None):
    global aiogram_bot, upload_bots, upload_bot_rr_index, bot_api_mode
    telegram = _telegram_config()
    bot_token = telegram.get("bot_token", "").strip()
    if not bot_token:
        aiogram_bot = None
        upload_bots = []
        return None

    if use_local_api is None:
        use_local_api = has_local_bot_api_server()
    aiogram_bot = _build_bot_client(bot_token, use_local_api=use_local_api)
    upload_bots = _build_upload_bot_pool(aiogram_bot, bot_token, use_local_api=use_local_api)
    upload_bot_rr_index = 0
    bot_api_mode = "local" if use_local_api else "cloud"
    return aiogram_bot


async def close_bot_client():
    global aiogram_bot, upload_bots, upload_bot_rr_index, bot_api_mode
    closed_clients = set()
    for item in upload_bots:
        client = item["client"]
        if client is not None and id(client) not in closed_clients:
            closed_clients.add(id(client))
            try:
                await client.session.close()
            except Exception:
                pass
    if aiogram_bot is not None and id(aiogram_bot) not in closed_clients:
        try:
            await aiogram_bot.session.close()
        except Exception:
            pass
    aiogram_bot = None
    upload_bots = []
    upload_bot_rr_index = 0
    bot_api_mode = "cloud"


def get_upload_bot_count():
    return len(upload_bots) or (1 if aiogram_bot is not None else 0)


def is_bot_client(client) -> bool:
    if client is None:
        return False
    if aiogram_bot is not None and client == aiogram_bot:
        return True
    return any(item["client"] == client for item in upload_bots)


def is_using_local_bot_api() -> bool:
    return bot_api_mode == "local" and has_local_bot_api_server()


def should_upload_via_bot(file_size: int) -> bool:
    if aiogram_bot is None:
        return False
    if is_using_local_bot_api():
        return True
    max_mb = float(get_config()["sync"].get("bot_upload_max_mb", 50) or 50)
    return file_size <= max_mb * 1024 * 1024


def _reset_window_if_needed(bot_state: dict, now: float, window_seconds: float):
    if bot_state["window_started_at"] <= 0:
        bot_state["window_started_at"] = now
        bot_state["uploaded_bytes"] = 0
        return
    if now - bot_state["window_started_at"] >= window_seconds:
        bot_state["window_started_at"] = now
        bot_state["uploaded_bytes"] = 0


def _window_reset_at(bot_state: dict, window_seconds: float) -> float:
    started_at = float(bot_state.get("window_started_at", 0.0) or 0.0)
    if started_at <= 0:
        return 0.0
    return started_at + window_seconds


def parse_retry_after_seconds(exc: Exception) -> int | None:
    match = RETRY_AFTER_RE.search(str(exc or ""))
    if not match:
        return None
    try:
        return int(match.group("seconds"))
    except (TypeError, ValueError):
        return None


def should_disable_upload_bot_for_error(exc: Exception) -> bool:
    text = str(exc or "").lower()
    return any(
        pattern in text
        for pattern in (
            "unauthorized",
            "invalid token",
            "bot token is invalid",
            "token is invalid",
        )
    )


def describe_user_client(client, fallback: str = "辅助账号") -> str:
    me = getattr(client, "me", None)
    if me is None:
        return fallback
    username = str(getattr(me, "username", "") or "").strip()
    if username:
        return f"{fallback} @{username}"
    first_name = str(getattr(me, "first_name", "") or "").strip()
    last_name = str(getattr(me, "last_name", "") or "").strip()
    full_name = " ".join(part for part in (first_name, last_name) if part).strip()
    if full_name:
        return f"{fallback} {full_name}"
    return fallback


def describe_upload_bot(client, fallback: str = "Bot") -> str:
    for bot_state in upload_bots:
        if bot_state["client"] == client:
            return bot_state["label"]
    if aiogram_bot is not None and client == aiogram_bot:
        return "主 Bot"
    return fallback


async def acquire_upload_bot(file_size: int, wait_if_unavailable: bool = True):
    global upload_bot_rr_index
    if not upload_bots:
        if aiogram_bot is None:
            raise RuntimeError("Bot 未配置或未连接，无法执行 Bot 上传")
        return {"client": aiogram_bot, "label": "主 Bot"}

    sync_cfg = get_config()["sync"]
    rate_limit_enabled = bool(sync_cfg.get("bot_rate_limit_enabled", False))
    threshold_bytes = int(float(sync_cfg.get("bot_rate_limit_gb", 10) or 10) * 1024 * 1024 * 1024)
    window_seconds = float(sync_cfg.get("bot_rate_limit_window_hours", 24) or 24) * 3600

    while True:
        now = time.time()
        earliest_ready_at = None
        total = len(upload_bots)
        disabled_count = 0
        blocked_labels = []

        for offset in range(total):
            idx = (upload_bot_rr_index + offset) % total
            bot_state = upload_bots[idx]
            if bot_state.get("disabled"):
                disabled_count += 1
                continue
            if rate_limit_enabled:
                _reset_window_if_needed(bot_state, now, window_seconds)
            if bot_state["cooldown_until"] > now:
                earliest_ready_at = bot_state["cooldown_until"] if earliest_ready_at is None else min(earliest_ready_at, bot_state["cooldown_until"])
                blocked_labels.append(f"{bot_state['label']}({max(1, int(bot_state['cooldown_until'] - now))}秒)")
                continue
            if rate_limit_enabled and threshold_bytes > 0 and bot_state["uploaded_bytes"] >= threshold_bytes:
                blocked_until = max(bot_state["cooldown_until"], _window_reset_at(bot_state, window_seconds))
                earliest_ready_at = blocked_until if earliest_ready_at is None else min(earliest_ready_at, blocked_until)
                blocked_labels.append(f"{bot_state['label']}({max(1, int(blocked_until - now))}秒)")
                continue
            upload_bot_rr_index = (idx + 1) % total
            return {"client": bot_state["client"], "label": bot_state["label"]}

        if disabled_count == total:
            raise RuntimeError("全部上传 Bot 均已失效，请检查 BOT_TOKEN / 额外 BOT_TOKEN 配置")
        if not wait_if_unavailable:
            return None
        wait_seconds = max(1, int((earliest_ready_at or (now + 5)) - now))
        detail = f"：{'、'.join(blocked_labels)}" if blocked_labels else ""
        await db.add_msg_log("BOT_WAIT", f"Bot 上传池暂时不可用{detail}，等待 {wait_seconds} 秒后继续；如需立即继续，可切换为辅助账号发送")
        await asyncio.sleep(wait_seconds)


async def note_upload_success(bot_client, file_size: int):
    if not upload_bots:
        return
    now = time.time()
    sync_cfg = get_config()["sync"]
    rate_limit_enabled = bool(sync_cfg.get("bot_rate_limit_enabled", False))
    threshold_bytes = int(float(sync_cfg.get("bot_rate_limit_gb", 10) or 10) * 1024 * 1024 * 1024)
    window_seconds = float(sync_cfg.get("bot_rate_limit_window_hours", 24) or 24) * 3600
    cooldown_seconds = float(sync_cfg.get("bot_rate_limit_cooldown_minutes", 300) or 300) * 60
    for bot_state in upload_bots:
        if bot_state["client"] == bot_client:
            _reset_window_if_needed(bot_state, now, window_seconds)
            previous_bytes = max(0, int(bot_state.get("uploaded_bytes", 0) or 0))
            bot_state["uploaded_bytes"] += max(0, int(file_size))
            if rate_limit_enabled and threshold_bytes > 0 and previous_bytes < threshold_bytes <= bot_state["uploaded_bytes"]:
                lock_until = max(now + cooldown_seconds, _window_reset_at(bot_state, window_seconds))
                bot_state["cooldown_until"] = max(float(bot_state.get("cooldown_until", 0.0) or 0.0), lock_until)
                wait_minutes = max(1, ceil(max(0.0, lock_until - now) / 60))
                await db.add_msg_log("BOT_ROTATE", f"{bot_state['label']} 达到上传阈值，暂停 {wait_minutes} 分钟并轮换下一个 Bot")
            break


async def mark_upload_bot_cooldown(bot_client, cooldown_seconds: int, reason: str = ""):
    if not upload_bots or bot_client is None:
        return None
    now = time.time()
    wait_seconds = max(1, int(cooldown_seconds or 0))
    for bot_state in upload_bots:
        if bot_state["client"] != bot_client:
            continue
        bot_state["cooldown_until"] = max(float(bot_state.get("cooldown_until", 0.0) or 0.0), now + wait_seconds)
        detail = f" | {reason}" if reason else ""
        await db.add_msg_log("BOT_ROTATE", f"{bot_state['label']} 遇到频控，冷却 {wait_seconds} 秒并轮换下一个 Bot{detail}")
        return bot_state["label"]
    return None


async def disable_upload_bot(bot_client, reason: str = ""):
    if not upload_bots or bot_client is None:
        return None
    detail = str(reason or "").strip()
    for bot_state in upload_bots:
        if bot_state["client"] != bot_client:
            continue
        if bot_state.get("disabled"):
            return bot_state["label"]
        bot_state["disabled"] = True
        bot_state["disabled_reason"] = detail
        suffix = f" | {detail}" if detail else ""
        await db.add_msg_log("BOT_DISABLE", f"{bot_state['label']} 因认证异常已退出轮换池{suffix}")
        return bot_state["label"]
    return None


def get_chat_name(chat):
    return f"@{chat.username}" if chat.username else (chat.title or str(chat.id))


def get_msg_type(msg: Message) -> str:
    return next((msg_type for msg_type in MSG_TYPES if getattr(msg, msg_type, None)), "text")


def _realtime_sync_options(mapping: dict | None = None) -> dict:
    sync_config = get_config().get("sync", {})
    mapping = mapping or {}
    return {
        "sender": "user" if mapping.get("realtime_sender", sync_config.get("realtime_sender")) == "user" else "bot",
        "fallback_to_user": bool(mapping.get("realtime_fallback_to_user", sync_config.get("realtime_fallback_to_user", True))),
        "hash_perturb": bool(mapping.get("realtime_hash_perturb", sync_config.get("realtime_hash_perturb", False))),
        "include_external_source_header": bool(sync_config.get("add_external_source_header", False)),
    }


def _realtime_needs_reupload(msg_type: str, options: dict) -> bool:
    return msg_type in REALTIME_REUPLOAD_TYPES and (
        options["sender"] == "user" or (options["hash_perturb"] and msg_type in {"photo", "video"})
    )


def _get_aiogram_media_object(message: Message, msg_type: str):
    media_obj = getattr(message, msg_type, None)
    if msg_type == "photo" and media_obj:
        return media_obj[-1]
    return media_obj


def _build_realtime_download_path(message: Message, msg_type: str) -> str:
    media_obj = _get_aiogram_media_object(message, msg_type)
    original_name = str(getattr(media_obj, "file_name", "") or "").strip()
    base_name, ext = os.path.splitext(original_name)
    if not base_name:
        default_ext = {
            "photo": ".jpg",
            "video": ".mp4",
            "animation": ".mp4",
            "audio": ".mp3",
            "voice": ".ogg",
            "sticker": ".webp",
            "document": "",
        }
        ext = ext or default_ext.get(msg_type, "")
        base_name = f"{msg_type}_{message.message_id}"
    safe_name = "".join("_" if char in '<>:"/\\|?*' else char for char in f"{base_name}{ext}")
    return os.path.join(TEMP_DIR, f"rt_{message.message_id}_{safe_name}")


async def _download_realtime_media(message: Message, msg_type: str) -> str | None:
    media_obj = _get_aiogram_media_object(message, msg_type)
    if media_obj is None:
        return None
    target_path = _build_realtime_download_path(message, msg_type)
    await execute_with_network_retry(
        lambda: aiogram_bot.download(media_obj, destination=target_path, timeout=3600),
        action_label=f"实时下载媒体 {message.message_id}",
        log_tag="REALTIME_NETWORK_RETRY",
    )
    return target_path if os.path.exists(target_path) else None


async def _send_realtime_text_with_identity(chat_id, text_html, reply_to_id, quote_data, options):
    sender = options["sender"]
    client = aiogram_bot if sender == "bot" else pyro_user_app
    if sender == "user" and not getattr(client, "is_initialized", False):
        raise RuntimeError("实时同步使用辅助账号发送前，请先完成辅助账号登录")
    sent = await execute_with_network_retry(
        lambda: dynamic_send(
            client,
            "text",
            chat_id,
            None,
            text_html,
            "HTML" if sender == "bot" else ParseMode.HTML,
            reply_to_message_id=reply_to_id,
            quote_data=quote_data if reply_to_id else None,
        ),
        action_label=f"实时文本发送 {chat_id}",
        log_tag="REALTIME_NETWORK_RETRY",
    )
    return sent.message_id if sender == "bot" else sent.id


def _realtime_should_fallback_to_user(options: dict) -> bool:
    return should_fallback_to_user(options["sender"], options["fallback_to_user"])


async def _resolve_realtime_upload_target(options: dict, file_sizes: list[int]):
    return await resolve_upload_target(
        options["sender"],
        pyro_user_app,
        file_sizes,
        allow_user_fallback=_realtime_should_fallback_to_user(options),
        wait_for_available_bot=not _realtime_should_fallback_to_user(options),
    )


async def _send_realtime_media_via_user(chat_id, msg_type, file_path, text_html, reply_to_id, quote_data, label_prefix, source_item=None):
    if not getattr(pyro_user_app, "is_initialized", False):
        raise RuntimeError("实时同步使用辅助账号发送前，请先完成辅助账号登录")
    file_size = os.path.getsize(file_path)
    file_label = format_upload_label(msg_type, file_path)
    tracker = UploadProgressTracker(label_prefix, file_size)
    sent = await execute_with_network_retry(
        lambda: dynamic_send(
            pyro_user_app,
            msg_type,
            chat_id,
            file_path,
            text_html,
            ParseMode.HTML,
            reply_to_message_id=reply_to_id,
            quote_data=quote_data if reply_to_id else None,
            progress=build_pyro_progress_callback(tracker, file_label, total_bytes=file_size, client=pyro_user_app),
            source_item=source_item,
        ),
        action_label=label_prefix,
        log_tag="REALTIME_NETWORK_RETRY",
    )
    return sent.id


async def _send_realtime_media_group_via_user(chat_id, downloaded_files, captions, reply_to_id, quote_data, action_label, spoiler_flags=None):
    if not getattr(pyro_user_app, "is_initialized", False):
        raise RuntimeError("实时同步使用辅助账号发送前，请先完成辅助账号登录")
    total_size = sum(os.path.getsize(path) for _, path, _ in downloaded_files)
    tracker = UploadProgressTracker("实时上传媒体组 [改用辅助账号]", total_size)
    media = build_user_media_group(downloaded_files, captions, {}, spoiler_flags=spoiler_flags)
    kwargs = {"chat_id": chat_id, "media": media}
    if reply_to_id:
        kwargs["reply_to_message_id"] = reply_to_id
    if quote_data and reply_to_id:
        kwargs["quote_text"] = quote_data["text"]
        if quote_data.get("entities"):
            kwargs["quote_entities"] = quote_data["entities"]
    kwargs["progress"] = build_pyro_progress_callback(
        tracker,
        f"实时上传媒体组: {len(downloaded_files)} 项",
        total_bytes=total_size,
        client=pyro_user_app,
    )
    sent_msgs = await execute_with_network_retry(
        lambda: pyro_user_app.send_media_group(**kwargs),
        action_label=action_label,
        log_tag="REALTIME_NETWORK_RETRY",
    )
    return [sent.id for sent in sent_msgs]


async def _send_realtime_media_upload(source_id, chat_id, message, msg_type, text_html, reply_to_id, quote_data, options):
    file_path = await _download_realtime_media(message, msg_type)
    if not file_path:
        return None
    try:
        file_path = await prepare_media_for_send(file_path, msg_type, message.message_id, options["hash_perturb"])
        file_size = os.path.getsize(file_path)
        upload_target = await _resolve_realtime_upload_target(options, [file_size])
        while True:
            actual_sender = upload_target["sender"]
            client = upload_target["client"]
            if actual_sender == "user":
                return await _send_realtime_media_via_user(
                    chat_id,
                    msg_type,
                    file_path,
                    text_html,
                    reply_to_id,
                    quote_data,
                    f"实时媒体辅助回退 {message.message_id}",
                    source_item=message,
                )

            file_label = format_upload_label(msg_type, file_path)
            tracker = UploadProgressTracker(f"实时上传 [{upload_target['label']}]", file_size)
            media_arg = ProgressFSInputFile(file_path, tracker, file_label)
            try:
                sent = await execute_with_network_retry(
                    lambda: dynamic_send(
                        client,
                        msg_type,
                        chat_id,
                        media_arg,
                        text_html,
                        upload_target["parse_mode"],
                        reply_to_message_id=reply_to_id,
                        quote_data=quote_data if reply_to_id else None,
                        source_item=message,
                    ),
                    action_label=f"实时媒体发送 {message.message_id}",
                    log_tag="REALTIME_NETWORK_RETRY",
                )
                await note_upload_success(client, file_size)
                return sent.message_id
            except Exception as exc:
                retry_after = parse_retry_after_seconds(exc)
                if retry_after is not None:
                    await mark_upload_bot_cooldown(client, retry_after + 1, f"REALTIME 消息ID:{message.message_id}")
                    upload_target = await _resolve_realtime_upload_target(options, [file_size])
                    if upload_target["sender"] == "user":
                        await db.add_msg_log("BOT_FALLBACK", f"实时同步 消息ID:{message.message_id} | Bot 频控，已切换辅助账号重传")
                    continue
                if should_disable_upload_bot_for_error(exc):
                    await disable_upload_bot(client, str(exc))
                    upload_target = await _resolve_realtime_upload_target(options, [file_size])
                    if upload_target["sender"] == "user":
                        await db.add_msg_log("BOT_FALLBACK", f"实时同步 消息ID:{message.message_id} | 当前 Bot 已失效，已切换辅助账号重传")
                    continue
                if _realtime_should_fallback_to_user(options) and getattr(pyro_user_app, "is_initialized", False):
                    await db.add_msg_log("BOT_FALLBACK", f"实时同步 消息ID:{message.message_id} | Bot 上传失败，已改用辅助账号重传")
                    return await _send_realtime_media_via_user(
                        chat_id,
                        msg_type,
                        file_path,
                        text_html,
                        reply_to_id,
                        quote_data,
                        f"实时媒体辅助回退 {message.message_id}",
                        source_item=message,
                    )
                raise
    finally:
        try:
            os.remove(file_path)
        except Exception:
            pass


async def _send_realtime_media_group_upload(source_id, chat_id, group, captions, reply_to_id, quote_data, options):
    downloaded_files = []
    try:
        for item in group:
            item_type = get_msg_type(item)
            path = await _download_realtime_media(item, item_type)
            if not path:
                return None
            path = await prepare_media_for_send(path, item_type, item.message_id, options["hash_perturb"])
            downloaded_files.append((item, path, "video" if item_type == "animation" else item_type))
        file_sizes = [os.path.getsize(path) for _, path, _ in downloaded_files]
        upload_target = await _resolve_realtime_upload_target(options, file_sizes)
        while True:
            actual_sender = upload_target["sender"]
            client = upload_target["client"]
            if actual_sender == "user":
                return await _send_realtime_media_group_via_user(
                    chat_id,
                    downloaded_files,
                    captions,
                    reply_to_id,
                    quote_data,
                    f"实时媒体组辅助回退 {group[0].message_id}",
                    spoiler_flags=[has_realtime_media_spoiler(item, get_msg_type(item)) for item in group],
                )

            _, media = build_bot_media_group(downloaded_files, captions, {}, sum(file_sizes), upload_target["label"])
            kwargs = {"chat_id": chat_id, "media": media}
            if reply_to_id:
                kwargs["reply_to_message_id"] = reply_to_id
            try:
                sent_msgs = await execute_with_network_retry(
                    lambda: client.send_media_group(**kwargs),
                    action_label=f"实时媒体组发送 {group[0].message_id}",
                    log_tag="REALTIME_NETWORK_RETRY",
                )
                await note_upload_success(client, sum(file_sizes))
                return [sent.message_id for sent in sent_msgs]
            except Exception as exc:
                retry_after = parse_retry_after_seconds(exc)
                if retry_after is not None:
                    await mark_upload_bot_cooldown(client, retry_after + 1, f"REALTIME 组首ID:{group[0].message_id}")
                    upload_target = await _resolve_realtime_upload_target(options, file_sizes)
                    if upload_target["sender"] == "user":
                        await db.add_msg_log("BOT_FALLBACK", f"实时同步 组首ID:{group[0].message_id} | Bot 频控，已切换辅助账号重传")
                    continue
                if should_disable_upload_bot_for_error(exc):
                    await disable_upload_bot(client, str(exc))
                    upload_target = await _resolve_realtime_upload_target(options, file_sizes)
                    if upload_target["sender"] == "user":
                        await db.add_msg_log("BOT_FALLBACK", f"实时同步 组首ID:{group[0].message_id} | 当前 Bot 已失效，已切换辅助账号重传")
                    continue
                if _realtime_should_fallback_to_user(options) and getattr(pyro_user_app, "is_initialized", False):
                    await db.add_msg_log("BOT_FALLBACK", f"实时同步 组首ID:{group[0].message_id} | Bot 上传失败，已改用辅助账号重传")
                    return await _send_realtime_media_group_via_user(
                        chat_id,
                        downloaded_files,
                        captions,
                        reply_to_id,
                        quote_data,
                        f"实时媒体组辅助回退 {group[0].message_id}",
                        spoiler_flags=[has_realtime_media_spoiler(item, get_msg_type(item)) for item in group],
                    )
                raise
    finally:
        for _, path, _ in downloaded_files:
            try:
                os.remove(path)
            except Exception:
                pass


async def is_type_allowed(msg_type: str) -> bool:
    settings = await db.get_all_settings()
    key_map = {msg_type: f"sync_{msg_type}" for msg_type in MSG_TYPES}
    key_map["animation"] = "sync_gif"
    return settings.get(key_map.get(msg_type, "sync_text"), "1") == "1"


def has_realtime_media_spoiler(message, msg_type):
    return has_media_spoiler(message, msg_type, "api")


async def _queue_realtime_media_group(message, source_id, target_mappings, chat_name):
    cache_key = (source_id, str(message.media_group_id))
    media_group_cache.setdefault(cache_key, []).append(message)

    previous_task = media_group_tasks.get(cache_key)
    if previous_task and not previous_task.done():
        previous_task.cancel()

    media_group_tasks[cache_key] = asyncio.create_task(
        _flush_realtime_media_group_after_delay(cache_key, source_id, target_mappings, chat_name)
    )


async def _flush_realtime_media_group_after_delay(cache_key, source_id, target_mappings, chat_name):
    try:
        await asyncio.sleep(MEDIA_GROUP_FLUSH_DELAY_SECONDS)
        await _process_realtime_media_group(cache_key, source_id, target_mappings, chat_name)
    except asyncio.CancelledError:
        return
    finally:
        current_task = asyncio.current_task()
        if media_group_tasks.get(cache_key) is current_task:
            media_group_tasks.pop(cache_key, None)


async def _process_realtime_media_group(cache_key, source_id, target_mappings, chat_name):
    group = sorted(media_group_cache.pop(cache_key, []), key=lambda item: item.message_id)
    if not group:
        return
    quote_data = get_quote_payload(group[0])

    include_external_source_header = _realtime_sync_options(target_mappings[0])["include_external_source_header"]

    for item in group:
        text_html = item.html_text if item.text or item.caption else ""
        file_name = item.document.file_name if item.document else (item.video.file_name if item.video else "")
        should_skip, _ = await db.apply_message_filters(text_html, True, file_name or "")
        if should_skip:
            await db.add_msg_log("DROP_REGEX", f"[{chat_name}] 媒体组消息ID:{[m.message_id for m in group]} | 已被正则过滤拦截")
            return

    msg_ids = [item.message_id for item in group]
    await db.add_msg_log("RECV_GROUP", f"[{chat_name}] 媒体组消息ID:{msg_ids}")
    for target_mapping in target_mappings:
        target_id = target_mapping["target_id"]
        target_type = str(target_mapping.get("target_type", "") or "channel")
        saved_operation = await begin_saved_messages_operation(target_type, target_id)
        try:
            # 收藏夹目标：chat_id 为当前辅助账号 own id（即时解析），target_id 仍是 DB 键（sentinel）。
            chat_id = await resolve_destination_chat_id(target_type, target_id, pyro_user_app)
            realtime_options = _realtime_sync_options(target_mapping)
            reply_to_id = await resolve_reply_for_forward(source_id, target_id, group[0].message_id, getattr(group[0], "reply_to_message_id", None))
            if any(_realtime_needs_reupload(get_msg_type(item), realtime_options) for item in group):
                captions = []
                link_context = await build_link_rewrite_context(aiogram_bot, source_id, target_id)
                for item in group:
                    item_text = item.html_text if item.text or item.caption else ""
                    if include_external_source_header:
                        item_text = prepend_source_header_html(item_text, item, enabled=True)
                    item_text, _ = await rewrite_message_links(item_text, source_id, link_context)
                    captions.append(item_text)
                sent_ids = await _send_realtime_media_group_upload(
                    source_id,
                    chat_id,
                    group,
                    captions,
                    reply_to_id,
                    quote_data,
                    realtime_options,
                )
                if not sent_ids:
                    continue
                for original, sent_id in zip(group, sent_ids):
                    await db.save_msg_mapping(source_id, original.message_id, target_id, sent_id)
                await db.add_msg_log("SEND_GROUP", f"源频道:{source_id} | 目标频道:{target_id} | 媒体组消息ID:{msg_ids} | 重传成功")
                continue
            # 如果启用外部来源前缀，需要逐条复制并添加前缀
            if include_external_source_header:
                await db.add_msg_log("WARN", f"目标频道:{target_id} | 媒体组启用外部来源前缀，使用逐条复制模式")
                for item in group:
                    try:
                        item_text = item.html_text if item.text or item.caption else ""
                        # 添加外部来源前缀
                        prefixed_text = prepend_source_header_html(item_text, item, enabled=True)

                        kwargs = {"chat_id": target_id, "from_chat_id": source_id, "message_id": item.message_id}
                        if prefixed_text != item_text:
                            kwargs["caption"] = prefixed_text
                            kwargs["parse_mode"] = "HTML"
                        if reply_to_id:
                            kwargs["reply_to_message_id"] = reply_to_id
                        if quote_data and reply_to_id:
                            kwargs["quote_text"] = quote_data["text"]
                            if quote_data.get("entities"):
                                kwargs["quote_entities"] = quote_data["entities"]
                        copied = await execute_with_network_retry(
                            lambda: aiogram_bot.copy_message(**kwargs),
                            action_label=f"实时逐条复制 {item.message_id}",
                            log_tag="REALTIME_NETWORK_RETRY",
                        )
                        await db.save_msg_mapping(source_id, item.message_id, target_id, copied.message_id)
                        await asyncio.sleep(1)
                    except Exception as exc:
                        await db.add_msg_log(
                            "ERROR",
                            f"源频道:{source_id} | 目标频道:{target_id} | 消息ID:{item.message_id} | 逐条发送失败: {exc}",
                        )
            else:
                # 正常的媒体组批量复制
                kwargs = {"chat_id": target_id, "from_chat_id": source_id, "message_ids": msg_ids}
                if reply_to_id:
                    kwargs["reply_to_message_id"] = reply_to_id
                if quote_data and reply_to_id:
                    kwargs["quote_text"] = quote_data["text"]
                    if quote_data.get("entities"):
                        kwargs["quote_entities"] = quote_data["entities"]
                copied_ids = await execute_with_network_retry(
                    lambda: aiogram_bot.copy_messages(**kwargs),
                    action_label=f"实时媒体组复制 {group[0].message_id}",
                    log_tag="REALTIME_NETWORK_RETRY",
                )
                for original, copied in zip(group, copied_ids):
                    await db.save_msg_mapping(source_id, original.message_id, target_id, copied.message_id)
                if quote_data and reply_to_id:
                    await db.add_msg_log("QUOTE_GROUP_SEND", f"源频道:{source_id} | 目标频道:{target_id} | 组首消息ID:{group[0].message_id} | 已保留引用回复")
                await db.add_msg_log("SEND_GROUP", f"源频道:{source_id} | 目标频道:{target_id} | 媒体组消息ID:{msg_ids} | 转发成功")
        except Exception as exc:
            await db.add_msg_log("WARN", f"目标频道:{target_id} | 媒体组整组复制失败，已改为逐条复制: {exc}")
            for item in group:
                try:
                    item_text = item.html_text if item.text or item.caption else ""
                    # 如果启用外部来源前缀，添加前缀
                    if include_external_source_header:
                        item_text = prepend_source_header_html(item_text, item, enabled=True)

                    kwargs = {"chat_id": target_id, "from_chat_id": source_id, "message_id": item.message_id}
                    if include_external_source_header and item_text != (item.html_text if item.text or item.caption else ""):
                        kwargs["caption"] = item_text
                        kwargs["parse_mode"] = "HTML"
                    if reply_to_id:
                        kwargs["reply_to_message_id"] = reply_to_id
                    if quote_data and reply_to_id:
                        kwargs["quote_text"] = quote_data["text"]
                        if quote_data.get("entities"):
                            kwargs["quote_entities"] = quote_data["entities"]
                    copied = await execute_with_network_retry(
                        lambda: aiogram_bot.copy_message(**kwargs),
                        action_label=f"实时媒体组回退逐条复制 {item.message_id}",
                        log_tag="REALTIME_NETWORK_RETRY",
                    )
                    await db.save_msg_mapping(source_id, item.message_id, target_id, copied.message_id)
                    await asyncio.sleep(1)
                except Exception as item_exc:
                    await db.add_msg_log(
                        "ERROR",
                        f"源频道:{source_id} | 目标频道:{target_id} | 消息ID:{item.message_id} | 媒体组回退逐条发送失败: {item_exc}",
                    )
        finally:
            await finish_saved_messages_operation(saved_operation)


@dp.channel_post()
async def handle_new_post(message: Message):
    if aiogram_bot is None:
        return

    source_id = message.chat.id
    if _debug_terminal_logs_enabled():
        logger.debug(
            "Bot 收到频道消息 | chat_id=%s chat=%s message_id=%s text=%s",
            source_id,
            get_chat_name(message.chat),
            message.message_id,
            _message_preview(message),
        )

    target_mappings = await db.get_target_channel_mappings(source_id, source_mode="bot")
    if not target_mappings:
        return

    chat_name = get_chat_name(message.chat)
    msg_type = get_msg_type(message)

    if not await is_type_allowed(msg_type):
        await db.add_msg_log("DROP_TYPE", f"[{chat_name}] 消息ID:{message.message_id} | 类型:{msg_type} | 已被类型过滤拦截")
        return

    if message.media_group_id:
        await _queue_realtime_media_group(message, source_id, target_mappings, chat_name)
        return
    has_media = msg_type != "text"
    file_name = getattr(getattr(message, msg_type, None), "file_name", "") if msg_type in ["document", "video"] else ""
    text_html = message.html_text if message.text or message.caption else ""
    quote_data = get_quote_payload(message)

    await db.add_msg_log("RECV", f"[{chat_name}] 消息ID:{message.message_id} | 类型:{msg_type}")

    should_skip, new_html = await db.apply_message_filters(text_html, has_media, file_name)
    if should_skip or (not has_media and not new_html.strip()):
        await db.add_msg_log("DROP_REGEX", f"[{chat_name}] 消息ID:{message.message_id} | 已被正则过滤拦截")
        return
    
    include_external_source_header = _realtime_sync_options(target_mappings[0])["include_external_source_header"]
    
    for target_mapping in target_mappings:
        target_id = target_mapping["target_id"]
        target_type = str(target_mapping.get("target_type", "") or "channel")
        saved_operation = await begin_saved_messages_operation(target_type, target_id)
        try:
            # 收藏夹目标：chat_id 为当前辅助账号 own id（即时解析），target_id 仍是 DB 键（sentinel）。
            chat_id = await resolve_destination_chat_id(target_type, target_id, pyro_user_app)
            realtime_options = _realtime_sync_options(target_mapping)
            link_context = await build_link_rewrite_context(aiogram_bot, source_id, target_id)
            target_html, rewrite_count = await rewrite_message_links(new_html, source_id, link_context)

            # 添加外部来源前缀（如果启用）
            if include_external_source_header:
                target_html = prepend_source_header_html(target_html, message, enabled=True)

            if rewrite_count:
                await db.add_msg_log("LINK_REWRITE", f"源频道:{source_id} | 目标频道:{target_id} | 消息链接改写:{rewrite_count}处")
            reply_to_id = await resolve_reply_for_forward(source_id, target_id, message.message_id, getattr(message, "reply_to_message_id", None))

            sent_id = None
            if not has_media and realtime_options["sender"] == "user":
                sent_id = await _send_realtime_text_with_identity(chat_id, target_html, reply_to_id, quote_data, realtime_options)
            elif has_media and _realtime_needs_reupload(msg_type, realtime_options):
                sent_id = await _send_realtime_media_upload(
                    source_id,
                    chat_id,
                    message,
                    msg_type,
                    target_html,
                    reply_to_id,
                    quote_data,
                    realtime_options,
                )
            elif target_html != text_html:
                kwargs = {"chat_id": target_id, "parse_mode": "HTML"}
                if not has_media:
                    kwargs["text"] = target_html
                else:
                    kwargs.update({"from_chat_id": source_id, "message_id": message.message_id, "caption": target_html})
                if reply_to_id:
                    kwargs["reply_to_message_id"] = reply_to_id
                if quote_data and reply_to_id and not has_media:
                    kwargs.pop("reply_to_message_id", None)
                    kwargs["reply_parameters"] = ReplyParameters(
                        message_id=reply_to_id,
                        quote=quote_data["text"],
                        quote_position=quote_data.get("position"),
                    )
                elif quote_data and reply_to_id:
                    kwargs["quote_text"] = quote_data["text"]
                    if quote_data.get("entities"):
                        kwargs["quote_entities"] = quote_data["entities"]
                copied = await execute_with_network_retry(
                    lambda: aiogram_bot.send_message(**kwargs) if not has_media else aiogram_bot.copy_message(**kwargs),
                    action_label=f"实时消息发送 {message.message_id}",
                    log_tag="REALTIME_NETWORK_RETRY",
                )
                sent_id = copied.message_id
            else:
                kwargs = {"chat_id": target_id, "from_chat_id": source_id, "message_id": message.message_id}
                if reply_to_id:
                    kwargs["reply_to_message_id"] = reply_to_id
                if quote_data and reply_to_id:
                    kwargs["quote_text"] = quote_data["text"]
                    if quote_data.get("entities"):
                        kwargs["quote_entities"] = quote_data["entities"]
                copied = await execute_with_network_retry(
                    lambda: aiogram_bot.copy_message(**kwargs),
                    action_label=f"实时消息复制 {message.message_id}",
                    log_tag="REALTIME_NETWORK_RETRY",
                )
                sent_id = copied.message_id

            if sent_id is None:
                continue
            await db.save_msg_mapping(source_id, message.message_id, target_id, sent_id)
            if quote_data and reply_to_id:
                await db.add_msg_log("QUOTE_SEND", f"源频道:{source_id} | 目标频道:{target_id} | 消息ID:{message.message_id} | 已保留引用回复")
            await db.add_msg_log("SEND", f"源频道:{source_id} | 消息ID:{message.message_id} | 目标频道:{target_id} | 新消息ID:{sent_id} | 转发成功")
        except Exception as exc:
            await db.add_msg_log("ERROR", f"消息ID:{message.message_id} | 目标频道:{target_id} | 发送失败: {exc}")
        finally:
            await finish_saved_messages_operation(saved_operation)


@dp.message()
async def debug_log_bot_message(message: Message):
    if not _debug_terminal_logs_enabled():
        return

    chat = getattr(message, "chat", None)
    from_user = getattr(message, "from_user", None)
    logger.debug(
        "Bot 收到普通消息 | chat_id=%s chat_type=%s chat=%s from_user=%s message_id=%s text=%s",
        getattr(chat, "id", None),
        getattr(chat, "type", None),
        get_chat_name(chat) if chat is not None else "",
        getattr(from_user, "id", None),
        getattr(message, "message_id", None),
        _message_preview(message),
    )


@dp.edited_channel_post()
async def handle_edited_post(message: Message):
    if aiogram_bot is None:
        return

    source_id = message.chat.id
    msg_id = message.message_id
    target_mappings = await db.get_all_target_msg_mappings(source_id, msg_id)
    if not target_mappings:
        return

    has_media = get_msg_type(message) != "text"
    should_skip, new_html = await db.apply_message_filters(message.html_text if message.text or message.caption else "", has_media, "")
    if should_skip:
        return

    for target_id, target_msg_id, target_type in target_mappings:
        saved_operation = await begin_saved_messages_operation(target_type, target_id)
        try:
            link_context = await build_link_rewrite_context(aiogram_bot, source_id, target_id)
            target_html, rewrite_count = await rewrite_message_links(new_html, source_id, link_context)
            chat_id = await resolve_destination_chat_id(target_type, target_id, pyro_user_app)
            kwargs = {"chat_id": chat_id, "message_id": target_msg_id}
            if is_saved_messages_target(target_type, target_id):
                kwargs["parse_mode"] = ParseMode.HTML
                if message.text:
                    await pyro_user_app.edit_message_text(text=target_html, **kwargs)
                else:
                    await pyro_user_app.edit_message_caption(caption=target_html, **kwargs)
            else:
                kwargs["parse_mode"] = "HTML"
                if message.text:
                    await aiogram_bot.edit_message_text(text=target_html, **kwargs)
                else:
                    await aiogram_bot.edit_message_caption(caption=target_html, **kwargs)
            if rewrite_count:
                await db.add_msg_log("LINK_REWRITE", f"源频道:{source_id} | 目标频道:{target_id} | 消息链接改写:{rewrite_count}处")
            await db.add_msg_log("EDIT", f"源消息ID:{msg_id} | 目标频道:{target_id} | 目标消息ID:{target_msg_id} | 编辑成功")
        except Exception as exc:
            await db.add_msg_log(
                "ERROR",
                f"源消息ID:{msg_id} | 目标频道:{target_id} | 目标消息ID:{target_msg_id} | 编辑失败: {exc}",
            )
        finally:
            await finish_saved_messages_operation(saved_operation)


def _public_channel_peer(source_ref: str):
    from sync_worker.realtime.public_poller import public_channel_peer

    return public_channel_peer(source_ref)


async def get_public_channel_last_message_id(source_ref: str) -> int:
    from sync_worker.realtime.public_poller import get_public_channel_last_message_id as get_last_message_id

    return await get_last_message_id(pyro_user_app, source_ref)


async def poll_public_user_channel_mappings():
    from sync_worker.realtime.public_poller import poll_public_user_channel_mappings as poll_mappings

    return await poll_mappings(lambda: aiogram_bot, lambda: pyro_user_app)


def init_user_client():
    global pyro_user_app
    telegram = _telegram_config()
    proxy = _proxy_config()
    if not telegram.get("api_id") or not telegram.get("api_hash"):
        pyro_user_app = None
        return None

    from pyrogram.connection.transport.tcp.tcp import TCP

    TCP.TIMEOUT = 60
    if proxy.get("enabled") and proxy.get("host"):
        async def _patched_connect(self, destination):
            from python_socks.async_.asyncio.v2 import Proxy
            if proxy.get("username") and proxy.get("password"):
                proxy_url = f"socks5://{proxy['username']}:{proxy['password']}@{proxy['host']}:{proxy['port']}"
            else:
                proxy_url = f"socks5://{proxy['host']}:{proxy['port']}"
            sock = await Proxy.from_url(proxy_url).connect(dest_host=destination[0], dest_port=destination[1])
            self.reader = sock.reader
            self.writer = sock.writer
        TCP._connect = _patched_connect

    pyro_user_app = Client(str(pyrogram_user_session_base()), api_id=telegram["api_id"], api_hash=telegram["api_hash"], ipv6=False)
    return pyro_user_app


async def _dispose_client(client):
    if not client:
        return
    try:
        if hasattr(client, "stop_transmission"):
            client.stop_transmission()
    except Exception:
        pass
    try:
        if getattr(client, "is_initialized", False):
            await client.stop()
        elif getattr(client, "is_connected", False):
            await client.disconnect()
    except Exception:
        pass


def _clear_user_auth_state():
    user_auth_state.update({
        "client": None,
        "phone_number": "",
        "phone_code_hash": "",
        "awaiting_code": False,
        "awaiting_password": False,
        "password_hint": "",
        "send_code_available_at": 0.0,
    })


def get_send_code_cooldown_seconds():
    remaining = int(user_auth_state["send_code_available_at"] - time.time())
    return max(0, remaining)


async def _finalize_user_client(client):
    global pyro_user_app
    await client.invoke(raw.functions.updates.GetState())
    client.me = await client.get_me()
    await client.initialize()
    pyro_user_app = client
    _clear_user_auth_state()
    return client.me


async def start_user_client_if_authorized():
    client = init_user_client()
    if client is None:
        _clear_user_auth_state()
        return None

    authorized = await client.connect()
    if not authorized:
        await _dispose_client(client)
        if pyro_user_app is client:
            globals()["pyro_user_app"] = None
        return None

    return await _finalize_user_client(client)


def get_user_auth_status():
    if pyro_user_app and getattr(pyro_user_app, "is_initialized", False):
        me = getattr(pyro_user_app, "me", None)
        return {
            "status": "authorized",
            "awaiting_code": False,
            "awaiting_password": False,
            "phone_number": "",
            "password_hint": "",
            "send_code_cooldown": get_send_code_cooldown_seconds(),
            "user": {
                "id": getattr(me, "id", None),
                "name": getattr(me, "first_name", "") or "",
                "username": getattr(me, "username", "") or "",
            },
        }
    return {
        "status": "awaiting_password" if user_auth_state["awaiting_password"] else "awaiting_code" if user_auth_state["awaiting_code"] else "idle",
        "awaiting_code": user_auth_state["awaiting_code"],
        "awaiting_password": user_auth_state["awaiting_password"],
        "phone_number": user_auth_state["phone_number"],
        "password_hint": user_auth_state["password_hint"],
        "send_code_cooldown": get_send_code_cooldown_seconds(),
        "user": None,
    }


async def begin_user_auth(phone_number: str):
    global pyro_user_app
    phone_number = (phone_number or "").strip()
    if not phone_number:
        raise ValueError("手机号不能为空")
    if not has_user_api_credentials():
        raise ValueError("请先配置 API_ID 和 API_HASH")

    cooldown = get_send_code_cooldown_seconds()
    if cooldown > 0:
        raise ValueError(f"请等待 {cooldown} 秒后再发送验证码")

    await close_user_client()
    client = init_user_client()
    if client is None:
        raise ValueError("辅助账号客户端初始化失败")

    authorized = await client.connect()
    if authorized:
        me = await _finalize_user_client(client)
        return {"status": "authorized", "message": "辅助账号已登录", "user": {"id": me.id, "name": me.first_name or "", "username": me.username or ""}}

    sent_code = await client.send_code(phone_number)
    pyro_user_app = None
    user_auth_state.update({
        "client": client,
        "phone_number": phone_number,
        "phone_code_hash": sent_code.phone_code_hash,
        "awaiting_code": True,
        "awaiting_password": False,
        "password_hint": "",
        "send_code_available_at": time.time() + 30,
    })
    return {"status": "code_sent", "message": "验证码已发送，请在页面中继续输入验证码", "send_code_cooldown": 30}


async def complete_user_auth(phone_code: str):
    client = user_auth_state["client"]
    phone_number = user_auth_state["phone_number"]
    phone_code_hash = user_auth_state["phone_code_hash"]
    if not client or not user_auth_state["awaiting_code"] or not phone_code_hash:
        raise ValueError("当前没有待确认的登录请求")

    try:
        me = await client.sign_in(phone_number, phone_code_hash, (phone_code or "").strip())
    except SessionPasswordNeeded:
        user_auth_state["awaiting_code"] = False
        user_auth_state["awaiting_password"] = True
        user_auth_state["password_hint"] = await client.get_password_hint()
        return {"status": "password_required", "message": "该账号已开启两步验证，请输入密码", "password_hint": user_auth_state["password_hint"]}

    me = me or await _finalize_user_client(client)
    if not getattr(pyro_user_app, "is_initialized", False):
        me = await _finalize_user_client(client)
    return {"status": "authorized", "message": "辅助账号登录成功", "user": {"id": me.id, "name": me.first_name or "", "username": me.username or ""}}


async def complete_user_password(password: str):
    client = user_auth_state["client"]
    if not client or not user_auth_state["awaiting_password"]:
        raise ValueError("当前没有待输入密码的登录请求")

    me = await client.check_password(password or "")
    if not getattr(pyro_user_app, "is_initialized", False):
        me = await _finalize_user_client(client)
    return {"status": "authorized", "message": "辅助账号登录成功", "user": {"id": me.id, "name": me.first_name or "", "username": me.username or ""}}


async def cancel_user_auth():
    client = user_auth_state["client"]
    _clear_user_auth_state()
    await _dispose_client(client)
    return {"status": "cancelled", "message": "已取消辅助账号登录流程"}


async def switch_user_account():
    if sync_state.get("is_syncing"):
        raise ValueError("请先中断当前同步任务，待任务停止后再切换辅助账号")
    await begin_saved_messages_account_switch()
    try:
        await close_user_client()
        await db.delete_message_mappings_for_target(SAVED_MESSAGES_TARGET_ID)
        session_base = pyrogram_user_session_base()
        for path in session_base.parent.glob(f"{session_base.name}*"):
            try:
                if path.is_file():
                    path.unlink()
            except Exception:
                pass
        return {"status": "success", "message": "已清除当前辅助账号会话，请重新登录新账号"}
    finally:
        await finish_saved_messages_account_switch()


async def close_user_client():
    global pyro_user_app
    pending_client = user_auth_state["client"]
    _clear_user_auth_state()
    await _dispose_client(pyro_user_app)
    if pending_client is not pyro_user_app:
        await _dispose_client(pending_client)
    pyro_user_app = None







