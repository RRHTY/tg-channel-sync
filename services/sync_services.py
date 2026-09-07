from __future__ import annotations

import asyncio
import hashlib
import re
import time
from collections.abc import Awaitable, Callable

import database as db


MESSAGE_LINK_RE = re.compile(r"https?://t\.me/(?:c/)?[^/\s]+/\d+")
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{2,}$")
NETWORK_RETRY_WAIT_SECONDS = 15
NETWORK_RETRY_MAX_RETRIES = 2
TEMPORARY_NETWORK_ERROR_PATTERNS = (
    "serverdisconnectederror",
    "server disconnected",
    "clientconnectorerror",
    "clientoserror",
    "connection reset by peer",
    "connection reset",
    "connection aborted",
    "connection refused",
    "cannot connect to host",
    "network is unreachable",
    "name or service not known",
    "temporarily unavailable",
    "timeouterror",
    "timed out",
)


class SyncNetworkRetryExhaustedError(RuntimeError):
    pass


def normalize_channel_username(channel_ref: str) -> str:
    ref = str(channel_ref or "").strip()
    if not ref:
        return ""

    if ref.startswith("@"):
        candidate = ref[1:]
    elif "t.me/" in ref.lower():
        normalized = ref.replace("\\", "/")
        candidate = normalized.split("t.me/", 1)[-1].split("?", 1)[0].split("#", 1)[0].split("/", 1)[0]
    else:
        candidate = ref

    candidate = candidate.strip().lstrip("@")
    if USERNAME_RE.fullmatch(candidate):
        return candidate
    return ""


def build_json_source_scope_id(source_username: str) -> int:
    normalized = normalize_channel_username(source_username)
    if not normalized:
        return 0
    digest = hashlib.sha1(normalized.lower().encode("utf-8")).hexdigest()[:15]
    return -int(digest, 16)


# 收藏夹（Saved Messages）目标在 channel_mappings.target_id 中存储的占位值。
# -1 永远不是真实 Telegram id；与 build_json_source_scope_id 派生的大负数 source_scope_id 不冲突。
# 存占位值而非真实 own id，是为了跨辅助账号重登录保持稳定（避免 stale-id 产生重复/失效映射）。
SAVED_MESSAGES_TARGET_ID = -1
_saved_messages_state_lock = asyncio.Lock()
_saved_messages_active_operations = 0
_saved_messages_account_switching = False


def is_saved_messages_target(target_type, target_id) -> bool:
    """判断该目标是否为收藏夹（Saved Messages）。target_type 优先，sentinel 作为兼容信号。"""
    if str(target_type or "channel") == "saved":
        return True
    try:
        return int(target_id or 0) == SAVED_MESSAGES_TARGET_ID
    except (TypeError, ValueError):
        return False


async def begin_saved_messages_operation(target_type, target_id) -> bool:
    """为一次收藏夹投递/编辑登记生命周期占用；普通频道返回 False。"""
    if not is_saved_messages_target(target_type, target_id):
        return False

    global _saved_messages_active_operations
    async with _saved_messages_state_lock:
        if _saved_messages_account_switching:
            raise ValueError("辅助账号正在切换，请稍后再处理收藏夹消息")
        _saved_messages_active_operations += 1
    return True


async def finish_saved_messages_operation(acquired: bool) -> None:
    if not acquired:
        return

    global _saved_messages_active_operations
    async with _saved_messages_state_lock:
        _saved_messages_active_operations = max(0, _saved_messages_active_operations - 1)


async def begin_saved_messages_account_switch() -> None:
    """占用账号切换窗口；存在未结束的收藏夹操作时立即拒绝。"""
    global _saved_messages_account_switching
    async with _saved_messages_state_lock:
        if _saved_messages_active_operations:
            raise ValueError("收藏夹消息仍在处理中，请待处理完成或停止后再切换辅助账号")
        if _saved_messages_account_switching:
            raise ValueError("辅助账号正在切换，请稍后重试")
        _saved_messages_account_switching = True


async def finish_saved_messages_account_switch() -> None:
    global _saved_messages_account_switching
    async with _saved_messages_state_lock:
        _saved_messages_account_switching = False


async def resolve_destination_chat_id(target_type, stored_target_id, user_app) -> int:
    """将存储的 target_id 映射为实际发送 chat_id。

    收藏夹目标返回当前辅助账号的 own id（每次即时解析，避免 stale-id 静默发错目标）；
    普通频道目标原样返回 stored_target_id。DB 键仍用 stored_target_id（收藏夹为 sentinel）。
    """
    if is_saved_messages_target(target_type, stored_target_id):
        if user_app is None or not getattr(user_app, "is_initialized", False):
            raise ValueError("收藏夹目标需要先完成辅助账号登录")
        me = getattr(user_app, "me", None)
        if me is None or not getattr(me, "id", None):
            me = await user_app.get_me()
        return int(me.id)
    return int(stored_target_id)


async def resolve_chat_id(bot, chat_ref: str) -> int:
    if bot is None:
        raise ValueError("BOT 未配置或未连接，无法解析频道")

    chat_ref = str(chat_ref or "").strip()
    if not chat_ref:
        raise ValueError("频道引用为空")
    if chat_ref.lstrip("-").isdigit():
        return int(chat_ref)
    if "t.me/" in chat_ref:
        username = chat_ref.split("t.me/")[-1].split("/")[0].split("?")[0]
        chat_ref = f"@{username.lstrip('@')}"
    if not chat_ref.startswith("@"):
        chat_ref = f"@{chat_ref}"
    try:
        return int((await bot.get_chat(chat_ref)).id)
    except Exception as exc:
        raise ValueError(f"无法解析频道 {chat_ref}: {exc}") from exc


def format_channel_check_error(exc: Exception, *, subject: str = "频道信息") -> str:
    detail = str(exc or "").strip()
    suffix = f" 原始错误：{detail}" if detail else ""
    if is_temporary_network_error(exc):
        return f"{subject}检查失败：当前网络连接异常，暂时无法向 Telegram 校验频道，请稍后重试。{suffix}"
    return f"{subject}检查失败，请确认频道 ID、@用户名或 t.me 链接是否正确，并确认 Bot/辅助账号有访问权限。{suffix}"


def get_quote_payload(message):
    quote = getattr(message, "quote", None)
    if not quote or not getattr(quote, "text", None):
        return None
    return {
        "text": quote.text,
        "position": getattr(quote, "position", None),
        "entities": getattr(quote, "entities", None),
    }


async def resolve_reply_target(
    source_id: int,
    target_id: int,
    reply_source_msg_id: int | None,
    mode_label: str,
    current_msg_id: int,
):
    if not reply_source_msg_id:
        return None
    target_reply_id = await db.get_target_msg_id(source_id, reply_source_msg_id, target_id)
    if target_reply_id:
        await db.add_msg_log(
            f"{mode_label}_REPLY_MAP",
            f"消息ID:{current_msg_id} | 回复源消息:{reply_source_msg_id} -> 目标消息:{target_reply_id}",
        )
    else:
        await db.add_msg_log(
            f"{mode_label}_REPLY_FALLBACK",
            f"消息ID:{current_msg_id} | 被回复消息:{reply_source_msg_id} 未同步，按普通消息发送",
        )
    return target_reply_id


async def resolve_reply_for_forward(
    source_id: int,
    target_id: int,
    current_msg_id: int,
    reply_source_msg_id: int | None,
):
    if not reply_source_msg_id:
        return None
    target_reply_id = await db.get_target_msg_id(source_id, reply_source_msg_id, target_id)
    if target_reply_id:
        await db.add_msg_log(
            "REPLY_MAP",
            f"源频道:{source_id} | 目标频道:{target_id} | 消息ID:{current_msg_id} | 已映射回复目标:{target_reply_id}",
        )
    else:
        await db.add_msg_log(
            "REPLY_FALLBACK",
            f"源频道:{source_id} | 目标频道:{target_id} | 消息ID:{current_msg_id} | 被回复消息ID:{reply_source_msg_id} 尚未同步，按普通消息发送",
        )
    return target_reply_id


async def build_link_rewrite_context(bot, source_id, target_id, source_username_override=None):
    if bot is None:
        return None

    # 收藏夹目标没有可改写的目标频道用户名，且 target_id 为 sentinel；
    # 返回 None 让 rewrite_message_links 原样返回文本，避免产生 t.me/c/1/* 失效链接。
    if is_saved_messages_target(None, target_id):
        return None

    context = {"source_id": source_id, "target_id": target_id}
    try:
        source_chat = await bot.get_chat(source_id) if source_id != 0 else None
        target_chat = await bot.get_chat(target_id)
    except Exception:
        source_chat = None
        target_chat = None

    source_username = source_username_override or (getattr(source_chat, "username", None) if source_chat else None)
    target_username = getattr(target_chat, "username", None) if target_chat else None
    context["source_username"] = str(source_username).lstrip("@") if source_username else None
    context["target_username"] = str(target_username).lstrip("@") if target_username else None
    return context


def _replace_msg_link(match, target_channel_ref: str, mapped_msg_id: int) -> str:
    suffix = match.group("suffix") or ""
    return f"{target_channel_ref}/{mapped_msg_id}{suffix}"


async def rewrite_message_links(text_html, source_id, link_context):
    if not text_html or not link_context:
        return text_html, 0

    updated_html = text_html
    rewrite_count = 0
    target_internal_id = str(abs(link_context["target_id"])).removeprefix("100")

    def has_internal_channel_id(chat_id: int) -> bool:
        return str(abs(int(chat_id or 0))).startswith("100")

    async def replace_pattern(pattern, target_channel_ref):
        nonlocal updated_html, rewrite_count
        parts = []
        cursor = 0
        changed = False
        for match in pattern.finditer(updated_html):
            source_msg_id = int(match.group("msg_id"))
            target_msg_id = await db.get_target_msg_id(source_id, source_msg_id, link_context["target_id"])
            original = match.group(0)
            replaced = _replace_msg_link(match, target_channel_ref, target_msg_id) if target_msg_id else original
            parts.append(updated_html[cursor:match.start()])
            parts.append(replaced)
            cursor = match.end()
            if original != replaced:
                rewrite_count += 1
                changed = True
        if changed:
            parts.append(updated_html[cursor:])
            updated_html = "".join(parts)

    if source_id != 0 and has_internal_channel_id(source_id):
        source_internal_id = str(abs(source_id)).removeprefix("100")
        await replace_pattern(
            re.compile(rf"(?P<prefix>https?://t\.me/c/{re.escape(source_internal_id)}/)(?P<msg_id>\d+)(?P<suffix>\b)"),
            f"https://t.me/c/{target_internal_id}",
        )

    if link_context.get("source_username"):
        target_channel_ref = (
            f"https://t.me/{link_context['target_username']}"
            if link_context.get("target_username")
            else f"https://t.me/c/{target_internal_id}"
        )
        await replace_pattern(
            re.compile(rf"(?P<prefix>https?://t\.me/{re.escape(link_context['source_username'])}/)(?P<msg_id>\d+)(?P<suffix>\b)"),
            target_channel_ref,
        )

    return updated_html, rewrite_count


def compute_progress_speed(downloaded: int, previous_downloaded: int, elapsed_seconds: float) -> float:
    if elapsed_seconds <= 0:
        return 0.0
    delta = max(0, downloaded - previous_downloaded)
    return delta / elapsed_seconds / (1024 * 1024)


def create_progress_callback(action_name: str, sync_state: dict):
    last_state = {"time": 0.0, "downloaded": 0}

    def progress(downloaded, total_bytes):
        if sync_state.get("stop_requested"):
            raise Exception("STOP_REQUESTED")
        now = time.time()
        elapsed = now - last_state["time"]
        if elapsed > 0.5 or downloaded == total_bytes:
            speed_mb = compute_progress_speed(downloaded, last_state["downloaded"], elapsed if last_state["time"] else 0)
            if total_bytes > 0:
                percent = downloaded / total_bytes * 100
                sync_state["current_text"] = f"{action_name} {percent:.1f}% ({speed_mb:.1f} MB/s)"
            else:
                sync_state["current_text"] = action_name
            last_state["time"] = now
            last_state["downloaded"] = downloaded

    return progress


def is_temporary_network_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(pattern in text for pattern in TEMPORARY_NETWORK_ERROR_PATTERNS)


async def safe_execute(coro, sync_state: dict):
    task = asyncio.create_task(coro)
    while not task.done():
        if sync_state.get("stop_requested"):
            task.cancel()
            raise Exception("STOP_REQUESTED")
        await asyncio.sleep(0.2)
    try:
        return await task
    except asyncio.CancelledError as exc:
        raise Exception("STOP_REQUESTED") from exc


async def execute_with_network_retry(
    coro_factory,
    *,
    action_label: str,
    sync_state: dict | None = None,
    log_tag: str = "NETWORK_RETRY",
    use_msg_log: bool = True,
    wait_seconds: int = NETWORK_RETRY_WAIT_SECONDS,
    max_retries: int = NETWORK_RETRY_MAX_RETRIES,
):
    attempt = 0
    while True:
        try:
            if sync_state is None:
                return await coro_factory()
            return await safe_execute(coro_factory(), sync_state)
        except Exception as exc:
            if sync_state and sync_state.get("stop_requested"):
                raise
            if not is_temporary_network_error(exc):
                raise
            if attempt >= max_retries:
                raise SyncNetworkRetryExhaustedError(
                    f"{action_label} 连续重试 {max_retries} 次后仍无法连接: {exc}"
                ) from exc
            attempt += 1
            if sync_state is not None:
                sync_state["current_text"] = f"网络中断，{wait_seconds} 秒后重试\n{action_label}"
            message = f"{action_label} | 检测到临时断网，{wait_seconds} 秒后重试 ({attempt}/{max_retries}) | {exc}"
            if use_msg_log:
                await db.add_msg_log(log_tag, message)
            else:
                await db.add_log("WARNING", message)
            await asyncio.sleep(wait_seconds)


async def log_sync_error(prefix: str, exc: Exception):
    await db.add_log("ERROR", f"{prefix}: {exc}")
