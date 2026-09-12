from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
import re

from aiogram.types import FSInputFile
from pyrogram.enums import ParseMode

import bot_engine
import database as db
from app_config import get_config
from services.sync_services import (
    MESSAGE_LINK_RE,
    SAVED_MESSAGES_TARGET_ID,
    SyncNetworkRetryExhaustedError,
    build_link_rewrite_context,
    build_json_source_scope_id,
    execute_with_network_retry,
    format_channel_check_error,
    log_sync_error,
    normalize_channel_username,
    resolve_chat_id,
    resolve_destination_chat_id,
    resolve_reply_target,
    rewrite_message_links,
    safe_execute,
)
from ..core import (
    UploadProgressTracker as SharedUploadProgressTracker,
    build_json_text,
    build_pyro_progress_callback,
    get_msg_meta,
    get_reply_source_msg_id,
    has_media_spoiler,
    resolve_json_media,
)
from ..media import prepare_json_media_for_send
from ..runtime import TEMP_DIR, count_unmapped_group, record_success, sync_state, update_state_and_check_skip
from ..runtime.result import SyncResult
from ..senders import build_bot_media_group, build_user_media_group
from .grouping import _json_group_family, group_json_messages
from .helpers import (
    JSON_MEDIA_GROUP_WINDOW_SECONDS,
    JsonSyncFatalError,
    ProgressFSInputFile,
    UploadProgressTracker,
    _format_media_label,
    _is_request_entity_too_large,
    _is_topics_parse_error,
    _json_should_fallback_to_user,
    _parse_retry_after_seconds,
    _select_json_upload_target,
    _execute_with_retry,
)


JSON_TEXT_MESSAGE_LIMIT = 4096
JSON_STANDARD_USER_UPLOAD_MAX_BYTES = 2000 * 1024 * 1024
HTML_TOKEN_RE = re.compile(r"<[^>]+>|[^<]+")
HTML_TAG_NAME_RE = re.compile(r"</?\s*([A-Za-z][A-Za-z0-9-]*)")
JSON_GROUP_SENT_UNMAPPED = "sent_unmapped"
JSON_GROUP_SKIPPED = "skipped"


def _pyro_file_ref(media_path: str) -> str:
    return media_path


def _get_sent_message_id(sent_msg):
    return getattr(sent_msg, "message_id", None) or getattr(sent_msg, "id", None)


def _format_bytes(size: int) -> str:
    size = max(0, int(size or 0))
    if size >= 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024 * 1024):.2f} GB"
    return f"{size / (1024 * 1024):.1f} MB"


def _scan_json_import_media_groups(groups: list[list[dict]], json_dir: str, sender: str) -> dict:
    stats = {
        "media_files": 0,
        "missing_files": 0,
        "media_groups": 0,
        "file_media_groups": 0,
        "large_group_count": 0,
        "largest_group_first_id": 0,
        "largest_group_bytes": 0,
        "largest_group_items": 0,
        "largest_file_msg_id": 0,
        "largest_file_bytes": 0,
        "over_standard_user_limit": 0,
        "over_bot_limit": 0,
    }

    for group in groups:
        group_file_sizes = []
        group_family = _json_group_family(group[0]) if group else None
        for item in group:
            media_path, _, _ = resolve_json_media(item, json_dir)
            if not media_path:
                continue
            if not os.path.exists(media_path):
                stats["missing_files"] += 1
                continue
            file_size = os.path.getsize(media_path)
            stats["media_files"] += 1
            group_file_sizes.append(file_size)
            if file_size > stats["largest_file_bytes"]:
                stats["largest_file_bytes"] = file_size
                stats["largest_file_msg_id"] = int(item.get("id") or 0)
            if file_size > JSON_STANDARD_USER_UPLOAD_MAX_BYTES:
                stats["over_standard_user_limit"] += 1
            if sender == "bot" and not bot_engine.should_upload_via_bot(file_size):
                stats["over_bot_limit"] += 1

        if len(group) > 1:
            stats["media_groups"] += 1
            if group_family == "document":
                stats["file_media_groups"] += 1
            group_bytes = sum(group_file_sizes)
            if group_bytes > stats["largest_group_bytes"]:
                stats["largest_group_bytes"] = group_bytes
                stats["largest_group_first_id"] = int(group[0].get("id") or 0)
                stats["largest_group_items"] = len(group)
            if group_bytes > JSON_STANDARD_USER_UPLOAD_MAX_BYTES:
                stats["large_group_count"] += 1

    return stats


async def _log_json_import_scan(stats: dict) -> None:
    if not stats["media_files"] and not stats["missing_files"] and not stats["media_groups"]:
        return

    await db.add_msg_log(
        "JSON_SCAN",
        (
            f"预扫描完成 | 媒体文件:{stats['media_files']} | 媒体组:{stats['media_groups']} "
            f"（文件媒体组:{stats['file_media_groups']}）| 最大媒体组:首ID {stats['largest_group_first_id']}，"
            f"{stats['largest_group_items']} 条，共 {_format_bytes(stats['largest_group_bytes'])} | "
            f"最大单文件:消息ID {stats['largest_file_msg_id']}，{_format_bytes(stats['largest_file_bytes'])}"
        ),
    )
    if stats["missing_files"]:
        await db.add_msg_log("JSON_WARN", f"预扫描发现 {stats['missing_files']} 个媒体文件不存在，发送到对应消息时会跳过或改为逐条处理")
    if stats["over_bot_limit"]:
        await db.add_msg_log("JSON_WARN", f"预扫描发现 {stats['over_bot_limit']} 个文件超过当前 Bot 单文件上限，发送时会按现有规则改用辅助账号或报错")
    if stats["over_standard_user_limit"]:
        await db.add_msg_log("JSON_WARN", f"预扫描发现 {stats['over_standard_user_limit']} 个文件超过 2GB 普通账号上传上限，可能需要 Premium 辅助账号或本地 Bot API 支持")
    if stats["large_group_count"]:
        await db.add_msg_log(
            "JSON_INFO",
            (
                f"预扫描发现 {stats['large_group_count']} 个媒体组总大小超过 2GB。"
                "Telegram 主要限制单文件大小和每组数量，本次不会按组总大小强制拆分；"
                "如本机资源紧张，建议分批导入。"
            ),
        )


def _html_tag_name(token: str) -> str:
    match = HTML_TAG_NAME_RE.match(token)
    return match.group(1).lower() if match else ""


def _is_html_closing_tag(token: str) -> bool:
    return token.startswith("</")


def _is_html_self_closing_tag(token: str) -> bool:
    return token.endswith("/>") or token.lower().startswith("<br")


def _close_tags(open_tags: list[tuple[str, str]]) -> str:
    return "".join(f"</{name}>" for name, _ in reversed(open_tags))


def _open_tags(open_tags: list[tuple[str, str]]) -> str:
    return "".join(raw for _, raw in open_tags)


def _track_html_tag(token: str, open_tags: list[tuple[str, str]]) -> None:
    tag_name = _html_tag_name(token)
    if not tag_name or _is_html_self_closing_tag(token):
        return
    if _is_html_closing_tag(token):
        for index in range(len(open_tags) - 1, -1, -1):
            if open_tags[index][0] == tag_name:
                del open_tags[index:]
                break
        return
    open_tags.append((tag_name, token))


def _split_json_text_for_send(text: str | None, limit: int = JSON_TEXT_MESSAGE_LIMIT) -> list[str]:
    rendered = str(text or "")
    if len(rendered) <= limit:
        return [rendered] if rendered else []

    parts: list[str] = []
    open_tags: list[tuple[str, str]] = []
    current = ""

    def flush_current() -> None:
        nonlocal current
        if current:
            parts.append(current + _close_tags(open_tags))
            current = _open_tags(open_tags)

    for token in HTML_TOKEN_RE.findall(rendered):
        if token.startswith("<"):
            suffix = _close_tags(open_tags)
            if current and len(current) + len(token) + len(suffix) > limit:
                flush_current()
            current += token
            _track_html_tag(token, open_tags)
            continue

        remaining = token
        while remaining:
            suffix = _close_tags(open_tags)
            available = limit - len(current) - len(suffix)
            if available <= 0:
                flush_current()
                suffix = _close_tags(open_tags)
                available = limit - len(current) - len(suffix)
            current += remaining[:available]
            remaining = remaining[available:]
            if remaining:
                flush_current()

    if current:
        parts.append(current + _close_tags(open_tags))
    return [part for part in parts if part]


async def _prepare_json_media_path(media_path: str, media_type: str, msg_id: int, hash_perturb: bool) -> tuple[str, bool]:
    return await prepare_json_media_for_send(media_path, media_type, msg_id, hash_perturb, temp_dir=TEMP_DIR)


async def _send_json_text_parts_via_user(chat_id, text_parts, reply_to_id):
    app = bot_engine.pyro_user_app
    if not getattr(app, "is_initialized", False):
        raise JsonSyncFatalError("Bot 发送失败，且辅助账号未登录，无法回退发送文本消息")

    first_sent = None
    for index, text_part in enumerate(text_parts):
        sent = await _execute_with_retry(
            lambda text_part=text_part, part_reply_to_id=reply_to_id if index == 0 else None: app.send_message(
                chat_id=chat_id,
                text=text_part,
                parse_mode=ParseMode.HTML,
                **({"reply_to_message_id": part_reply_to_id} if part_reply_to_id else {}),
            ),
            action_label=f"文本消息 -> 辅助账号发送 {index + 1}/{len(text_parts)}",
            stop_client=app,
        )
        if sent is None:
            raise RuntimeError("文本分片发送未完成，部分内容可能已发送，请核对目标频道后重跑")
        if first_sent is None:
            first_sent = sent
    return first_sent


async def _send_json_text_via_bot(upload_target, sender, clone_fallback_to_user, chat_id, text, reply_to_id, msg_id):
    text_parts = _split_json_text_for_send(text)
    first_sent = None
    index = 0
    while index < len(text_parts):
        text_part = text_parts[index]
        part_reply_to_id = reply_to_id if index == 0 else None
        sent = None
        for _ in range(3):
            if sync_state["stop_requested"]:
                break
            try:
                sent = await execute_with_network_retry(
                    lambda text_part=text_part, part_reply_to_id=part_reply_to_id: upload_target["client"].send_message(
                        chat_id,
                        text_part,
                        parse_mode="HTML",
                        reply_to_message_id=part_reply_to_id,
                    ),
                    action_label=f"JSON 文本发送 {msg_id} ({index + 1}/{len(text_parts)})",
                    sync_state=sync_state,
                    log_tag="JSON_NETWORK_RETRY",
                )
                break
            except Exception as exc:
                if sync_state["stop_requested"]:
                    raise
                if isinstance(exc, SyncNetworkRetryExhaustedError):
                    raise
                retry_after = _parse_retry_after_seconds(exc)
                if retry_after is not None:
                    await bot_engine.mark_upload_bot_cooldown(upload_target["client"], retry_after + 1, f"JSON 消息ID:{msg_id}")
                    upload_target = await _select_json_upload_target(
                        sender,
                        [],
                        clone_fallback_to_user=clone_fallback_to_user,
                        wait_for_available_bot=not _json_should_fallback_to_user(sender, clone_fallback_to_user),
                    )
                    if upload_target["sender"] == "user":
                        await db.add_msg_log("JSON_FALLBACK", f"消息ID:{msg_id} | Bot 频控，已切换辅助账号发送文本")
                        user_sent = await _send_json_text_parts_via_user(chat_id, text_parts[index:], part_reply_to_id)
                        return first_sent or user_sent
                    continue
                if bot_engine.should_disable_upload_bot_for_error(exc):
                    await bot_engine.disable_upload_bot(upload_target["client"], str(exc))
                    upload_target = await _select_json_upload_target(
                        sender,
                        [],
                        clone_fallback_to_user=clone_fallback_to_user,
                        wait_for_available_bot=not _json_should_fallback_to_user(sender, clone_fallback_to_user),
                    )
                    if upload_target["sender"] == "user":
                        await db.add_msg_log("JSON_FALLBACK", f"消息ID:{msg_id} | 当前 Bot 已失效，已切换辅助账号发送文本")
                        user_sent = await _send_json_text_parts_via_user(chat_id, text_parts[index:], part_reply_to_id)
                        return first_sent or user_sent
                    continue
                raise

        if sent is None and not sync_state["stop_requested"] and _json_should_fallback_to_user(sender, clone_fallback_to_user):
            await db.add_msg_log("JSON_FALLBACK", f"消息ID:{msg_id} | Bot 发送失败，已改用辅助账号发送文本")
            user_sent = await _send_json_text_parts_via_user(chat_id, text_parts[index:], part_reply_to_id)
            return first_sent or user_sent
        if sent is None:
            raise RuntimeError("文本分片发送未完成，部分内容可能已发送，请核对目标频道后重跑")
        if first_sent is None:
            first_sent = sent
        index += 1
    return first_sent


async def _send_json_single_via_user(chat_id, media_type, media_path, caption, reply_to_id, tracker, file_label, has_spoiler=False):
    app = bot_engine.pyro_user_app
    if not getattr(app, "is_initialized", False):
        raise JsonSyncFatalError("Bot 上传体积超限，且辅助账号未登录，无法回退重传")
    spoiler_kwargs = {"has_spoiler": True} if has_spoiler and media_type in {"photo", "video", "animation"} else {}
    caption_kwargs = {"caption": caption, "parse_mode": ParseMode.HTML} if caption else {}
    media_kwargs = {"sticker": _pyro_file_ref(media_path), "emoji": ""} if media_type == "sticker" else {
        media_type if hasattr(app, f"send_{media_type}") else "document": _pyro_file_ref(media_path)
    }
    return await _execute_with_retry(
        lambda: getattr(app, f"send_{media_type}", app.send_document)(
            chat_id=chat_id,
            **media_kwargs,
            **({} if media_type == "sticker" else caption_kwargs),
            **({"reply_to_message_id": reply_to_id} if reply_to_id else {}),
            **spoiler_kwargs,
            progress=build_pyro_progress_callback(tracker, file_label, total_bytes=os.path.getsize(media_path), client=app),
        ),
        action_label=f"消息 -> 辅助账号重传 [{os.path.basename(media_path)}]",
        stop_client=app,
    )   


async def _send_json_text_via_user(chat_id, text, reply_to_id):
    return await _send_json_text_parts_via_user(chat_id, _split_json_text_for_send(text), reply_to_id)


def _json_media_group_items(file_entries, rewritten_captions):
    normalized_captions = []
    group_items = []
    for (item, media_path, media_type), caption_html in zip(file_entries, rewritten_captions):
        normalized_captions.append(caption_html if caption_html else None)
        group_items.append((item, media_path, "video" if media_type == "animation" else media_type))
    return group_items, normalized_captions


async def _send_json_group_via_user(group, chat_id, rewritten_captions, file_entries, reply_to_id):
    app = bot_engine.pyro_user_app
    if not getattr(app, "is_initialized", False):
        raise JsonSyncFatalError("Bot 上传体积超限，且辅助账号未登录，无法回退发送媒体组")
    total_bytes = sum(os.path.getsize(path) for _, path, _ in file_entries)
    tracker = SharedUploadProgressTracker("上传媒体组 [改用辅助账号]", total_bytes)
    group_items, normalized_captions = _json_media_group_items(file_entries, rewritten_captions)
    spoiler_flags = [has_media_spoiler(item, media_type, "json") for item, _, media_type in file_entries]
    media = build_user_media_group(group_items, normalized_captions, {}, spoiler_flags)
    kwargs = {"chat_id": chat_id, "media": media}
    if reply_to_id:
        kwargs["reply_to_message_id"] = reply_to_id
    kwargs["progress"] = build_pyro_progress_callback(
        tracker,
        f"上传媒体组: {len(file_entries)} 项",
        total_bytes=total_bytes,
        client=app,
    )
    try:
        return await _execute_with_retry(
            lambda: app.send_media_group(**kwargs),
            action_label=f"媒体组 -> 辅助账号重传 [{len(file_entries)} 项]",
            retry_unknown_errors=False,
            stop_client=app,
        )
    except Exception as exc:
        if isinstance(exc, SyncNetworkRetryExhaustedError):
            raise
        if _is_topics_parse_error(exc):
            await db.add_msg_log(
                "JSON_TOPICS_COMPAT",
                f"组首消息ID:{int(group[0].get('id') or 0)} | 辅助账号发送后返回 topics 解析异常，已停止重试避免重复发送",
            )
            return JSON_GROUP_SENT_UNMAPPED
        raise

@dataclass
class _JsonMediaGroupPreparation:
    file_entries: list[tuple[dict, str, str]]
    spoiler_flags: list[bool]
    prepared_temp_paths: list[str]
    rewritten_captions: list[str]
    total_bytes: int


async def _prepare_json_media_group(group, json_dir, source_scope_id, link_context, hash_perturb, include_external_source_header, prepared_temp_paths):
    first_msg = group[0]
    group_family = _json_group_family(first_msg)
    file_entries = []
    spoiler_flags = []
    rewritten_captions = []
    total_bytes = 0
    raw_captions = [build_json_text(item, include_external_source_header=False) for item in group]
    visual_header_index = None
    if group_family == "visual" and include_external_source_header:
        for caption_index, caption_text in enumerate(raw_captions):
            if str(caption_text or "").strip():
                visual_header_index = caption_index
                break

    for index, item in enumerate(group):
        item_id = int(item.get("id") or 0)
        media_path, media_type, _ = resolve_json_media(item, json_dir)
        if not media_path or not os.path.exists(media_path):
            await db.add_msg_log("JSON_MEDIA_MISSING", f"消息ID:{item_id} | 媒体组文件不存在，已改为逐条发送")
            return None
        media_path, created_temp = await _prepare_json_media_path(media_path, media_type, item_id, hash_perturb)
        if created_temp:
            prepared_temp_paths.append(media_path)
        total_bytes += os.path.getsize(media_path)
        file_entries.append((item, media_path, media_type))
        spoiler_flags.append(has_media_spoiler(item, media_type, "json"))

        should_add_source_header = include_external_source_header and (
            group_family != "visual" or visual_header_index == index
        )
        caption_html = build_json_text(item, include_external_source_header=should_add_source_header)
        caption_html, rewrite_count = await rewrite_message_links(caption_html, source_scope_id, link_context)
        if rewrite_count:
            await db.add_msg_log("JSON_LINK_REWRITE", f"消息ID:{item_id} | 命中 {rewrite_count} 个链接改写")
        rewritten_captions.append(caption_html)

    return _JsonMediaGroupPreparation(file_entries, spoiler_flags, prepared_temp_paths, rewritten_captions, total_bytes)

async def send_json_media_group(
    group,
    target_id,
    json_dir,
    source_scope_id,
    force_send,
    link_context,
    sender: str,
    clone_fallback_to_user: bool,
    hash_perturb: bool = False,
    include_external_source_header: bool = False,
    chat_id: int | None = None,
):
    # chat_id 为实际发送目的地（收藏夹目标 = 当前辅助账号 own id），target_id 为 DB 键（sentinel）。
    if chat_id is None:
        chat_id = target_id
    group_ids = [int(item.get("id") or 0) for item in group]
    first_msg = group[0]
    first_id = group_ids[0] if group_ids else 0
    if await update_state_and_check_skip(source_scope_id, target_id, first_id, "[JSON媒体组]", force_send=force_send):
        return JSON_GROUP_SKIPPED

    for item in group:
        item_id = int(item.get("id") or 0)
        msg_type, _ = get_msg_meta(item, "json")
        text = build_json_text(item, include_external_source_header=False)
        media_path, _, _ = resolve_json_media(item, json_dir)
        file_name = os.path.basename(str(media_path or ""))
        should_skip, _ = await db.apply_message_filters(text, msg_type != "text", file_name)
        if should_skip:
            await db.add_msg_log(
                "JSON_DROP_REGEX",
                f"媒体组消息ID:{group_ids} | 命中消息ID:{item_id} | 已被正则过滤拦截",
            )
            return JSON_GROUP_SKIPPED

    sent_ids = []
    prepared_temp_paths = []

    try:
        preparation = await _prepare_json_media_group(
            group,
            json_dir,
            source_scope_id,
            link_context,
            hash_perturb,
            include_external_source_header,
            prepared_temp_paths,
        )
        if preparation is None:
            return None
        file_entries = preparation.file_entries
        spoiler_flags = preparation.spoiler_flags
        prepared_temp_paths = preparation.prepared_temp_paths
        rewritten_captions = preparation.rewritten_captions
        total_bytes = preparation.total_bytes

        reply_to_id = await resolve_reply_target(
            source_scope_id,
            target_id,
            get_reply_source_msg_id(first_msg, "json"),
            "JSON",
            first_id,
        )
        file_sizes = [os.path.getsize(path) for _, path, _ in file_entries]
        upload_target = await _select_json_upload_target(
            sender,
            file_sizes,
            clone_fallback_to_user=clone_fallback_to_user,
            wait_for_available_bot=not _json_should_fallback_to_user(sender, clone_fallback_to_user),
        )
        sent_group = None

        if upload_target["sender"] == "user":
            sent_group = await _send_json_group_via_user(group, chat_id, rewritten_captions, file_entries, reply_to_id)
        else:
            for _ in range(3):
                if sync_state["stop_requested"]:
                    break
                group_items, normalized_captions = _json_media_group_items(file_entries, rewritten_captions)
                tracker, media_list = build_bot_media_group(group_items, normalized_captions, {}, total_bytes, upload_target["label"], spoiler_flags)
                try:
                    sent_group = await execute_with_network_retry(
                        lambda: upload_target["client"].send_media_group(
                            chat_id,
                            media_list,
                            reply_to_message_id=reply_to_id,
                        ),
                        action_label=f"JSON 媒体组发送 {first_id}",
                        sync_state=sync_state,
                        log_tag="JSON_NETWORK_RETRY",
                    )
                    await bot_engine.note_upload_success(upload_target["client"], sum(file_sizes))
                    break
                except Exception as exc:
                    if sync_state["stop_requested"]:
                        raise
                    if isinstance(exc, SyncNetworkRetryExhaustedError):
                        raise
                    if _is_request_entity_too_large(exc):
                        if _json_should_fallback_to_user(sender, clone_fallback_to_user):
                            await db.add_msg_log("JSON_FALLBACK", f"组首消息ID:{first_id} | Bot 上传体积超限，已改用辅助账号发送媒体组")
                            sent_group = await _send_json_group_via_user(group, chat_id, rewritten_captions, file_entries, reply_to_id)
                            break
                        raise
                    retry_after = _parse_retry_after_seconds(exc)
                    if retry_after is not None:
                        await bot_engine.mark_upload_bot_cooldown(upload_target["client"], retry_after + 1, f"JSON 媒体组首ID:{first_id}")
                        upload_target = await _select_json_upload_target(
                            sender,
                            file_sizes,
                            clone_fallback_to_user=clone_fallback_to_user,
                            wait_for_available_bot=not _json_should_fallback_to_user(sender, clone_fallback_to_user),
                        )
                        if upload_target["sender"] == "user":
                            await db.add_msg_log("JSON_FALLBACK", f"组首消息ID:{first_id} | Bot 频控，已切换辅助账号发送媒体组")
                            sent_group = await _send_json_group_via_user(group, chat_id, rewritten_captions, file_entries, reply_to_id)
                            break
                        continue
                    if bot_engine.should_disable_upload_bot_for_error(exc):
                        await bot_engine.disable_upload_bot(upload_target["client"], str(exc))
                        upload_target = await _select_json_upload_target(
                            sender,
                            file_sizes,
                            clone_fallback_to_user=clone_fallback_to_user,
                            wait_for_available_bot=not _json_should_fallback_to_user(sender, clone_fallback_to_user),
                        )
                        if upload_target["sender"] == "user":
                            await db.add_msg_log("JSON_FALLBACK", f"组首消息ID:{first_id} | 当前 Bot 已失效，已切换辅助账号发送媒体组")
                            sent_group = await _send_json_group_via_user(group, chat_id, rewritten_captions, file_entries, reply_to_id)
                            break
                        continue
                    raise
            if sent_group is None and not sync_state["stop_requested"] and _json_should_fallback_to_user(sender, clone_fallback_to_user):
                await db.add_msg_log("JSON_FALLBACK", f"组首消息ID:{first_id} | Bot 发送失败，已改用辅助账号发送媒体组")
                sent_group = await _send_json_group_via_user(group, chat_id, rewritten_captions, file_entries, reply_to_id)

        if sent_group is None:
            return None
        if sent_group == JSON_GROUP_SENT_UNMAPPED:
            await db.add_msg_log(
                "JSON_GROUP_SEND_UNMAPPED",
                f"组首消息ID:{first_id} | 共 {len(group)} 条 | 目标:[{target_id}] | 可能已发送，回包解析失败，未记录映射",
            )
            count_unmapped_group()
            return JSON_GROUP_SENT_UNMAPPED
        for original_msg, sent_msg in zip(group, sent_group):
            new_id = _get_sent_message_id(sent_msg)
            sent_ids.append(new_id)
            if new_id > 0:
                await record_success(source_scope_id, target_id, int(original_msg.get("id") or 0), new_id, force_send=force_send)

        await db.add_msg_log(
            "JSON_GROUP_SEND",
            f"组首消息ID:{first_id} | 共 {len(group)} 条 | 目标:[{target_id}] | 已按媒体组发送",
        )
        return sent_ids
    finally:
        for temp_path in prepared_temp_paths:
            try:
                os.remove(temp_path)
            except Exception:
                pass


async def process_json_sync(
    sender,
    target_id_raw,
    json_path,
    safe_delay,
    force_send,
    json_source_username="",
    media_group_window_seconds: int = JSON_MEDIA_GROUP_WINDOW_SECONDS,
    clone_fallback_to_user: bool = True,
    hash_perturb: bool = False,
    target_type: str = "channel",
    outcome: SyncResult | None = None,
):
    outcome = outcome if outcome is not None else SyncResult()
    if not json_path or not os.path.exists(json_path):
        raise JsonSyncFatalError(f"JSON 文件不存在或路径无效: {json_path or ''}")

    try:
        with open(json_path, "r", encoding="utf-8") as file_obj:
            data = json.load(file_obj)
    except Exception as exc:
        raise JsonSyncFatalError(f"JSON 解析失败: {exc}") from exc

    if not isinstance(data, dict) or not isinstance(data.get("messages"), list) or any(
        not isinstance(message, dict) for message in data["messages"]
    ):
        raise JsonSyncFatalError("JSON 格式无效：需要包含 messages 数组的单个聊天导出文件")

    messages = data.get("messages", [])
    json_dir = os.path.dirname(os.path.abspath(json_path))
    try:
        if str(target_type or "").strip() == "saved":
            # 收藏夹目标：跳过 resolve_chat_id（sentinel 不是真实 Telegram id），
            # 发送 chat_id 即时解析为当前辅助账号 own id；sender 恒定 user。
            target_id = SAVED_MESSAGES_TARGET_ID
            chat_id = await resolve_destination_chat_id("saved", target_id, bot_engine.pyro_user_app)
        else:
            target_id = await resolve_chat_id(bot_engine.aiogram_bot, target_id_raw)
            chat_id = target_id
    except Exception as exc:
        raise JsonSyncFatalError(format_channel_check_error(exc, subject="目标频道信息")) from exc
    source_username = normalize_channel_username(json_source_username)
    from .source import export_source_id

    if source_username:
        source_scope_id = build_json_source_scope_id(source_username)
        try:
            source_scope_id = await resolve_chat_id(bot_engine.aiogram_bot, f"@{source_username}")
        except Exception:
            await db.add_msg_log(
                "JSON_WARN",
                f"JSON 源频道 @{source_username} 无法解析真实 ID，已使用稳定用户名作用域导入（链接改写仅支持 t.me/@用户名）",
            )
    else:
        source_scope_id, uses_file_identity = export_source_id(data)
        if json_source_username:
            await db.add_msg_log("JSON_WARN", "源频道用户名格式无效，请填写 @username 或 https://t.me/username")

    if not source_username:
        await db.add_msg_log(
            "JSON_INFO",
            "去重使用导出来源标识；旧版未填写用户名的公共去重记录不再使用，首次重跑请核对目标频道",
        )
        if uses_file_identity:
            await db.add_msg_log(
                "JSON_WARN",
                "导出文件缺少可识别的来源 ID，按文件内容去重（移动文件不受影响）；"
                "修改或重新导出内容后可能重复发送，建议填写稳定的源频道用户名",
            )

    link_context = await build_link_rewrite_context(
        bot_engine.aiogram_bot,
        source_scope_id,
        target_id,
        source_username_override=source_username,
    )
    sync_state["total"] = len(messages)
    warned_media_groups = False
    warned_link_rewrite = False
    settings = await db.get_all_settings()
    sync_config = get_config().get("sync", {})
    include_external_source_header = bool(sync_config.get("add_external_source_header", False))
    if not include_external_source_header:
        legacy_value = str(getattr(settings, "get", lambda *_: "")("add_external_source_header", "") or "").strip().lower()
        include_external_source_header = legacy_value in {"1", "true", "yes", "on"}

    media_group_window_seconds = max(1, int(media_group_window_seconds or JSON_MEDIA_GROUP_WINDOW_SECONDS))
    grouped_messages = group_json_messages(messages, media_group_window_seconds)
    await _log_json_import_scan(_scan_json_import_media_groups(grouped_messages, json_dir, sender))

    for group in grouped_messages:
        if sync_state["stop_requested"]:
            break
        if len(group) > 1:
            if any(item.get("media_group_id") or item.get("grouped_id") or item.get("media_group") for item in group):
                if not warned_media_groups:
                    warned_media_groups = True
                    await db.add_msg_log("JSON_INFO", "JSON 导入检测到显式媒体组标记，已按媒体组发送")
            result = await send_json_media_group(
                group,
                target_id,
                json_dir,
                source_scope_id,
                force_send,
                link_context,
                sender,
                clone_fallback_to_user,
                hash_perturb,
                include_external_source_header,
                chat_id=chat_id,
            )
            if result is not None:
                if result in (JSON_GROUP_SKIPPED, JSON_GROUP_SENT_UNMAPPED):
                    outcome.record(result, len(group))
                elif len(result) == len(group) and all(result):
                    outcome.record("sent_mapped", len(group))
                else:
                    outcome.record("sent_unmapped", len(group))
                sync_state["current"] += max(0, len(group) - 1)
                await asyncio.sleep(safe_delay)
                continue
        for msg in group:
            if sync_state["stop_requested"]:
                break
            if msg.get("type") != "message":
                continue

            msg_id = msg.get("id", 0)
            msg_type, sync_key = get_msg_meta(msg, "json")
            if settings.get(sync_key, "1") == "0":
                outcome.record("skipped")
                await db.add_msg_log("JSON_DROP_TYPE", f"消息ID:{msg_id} | 类型:{msg_type} | 已被类型过滤拦截")
                continue

            text = build_json_text(msg, include_external_source_header=include_external_source_header)
            if text and not warned_link_rewrite and MESSAGE_LINK_RE.search(text):
                warned_link_rewrite = True
                if source_username:
                    await db.add_msg_log("JSON_INFO", f"JSON 导入已启用链接改写，源频道用户名: @{source_username}")
                else:
                    await db.add_msg_log("JSON_WARN", "JSON 导入检测到消息链接引用；未填写源频道用户名，无法安全改写源频道链接")

            file_name = ""
            if msg.get("file"):
                file_name = os.path.basename(str(msg.get("file") or ""))
            elif msg.get("photo"):
                file_name = os.path.basename(str(msg.get("photo") or ""))
            elif msg.get("video"):
                file_name = os.path.basename(str(msg.get("video") or ""))
            elif msg.get("audio"):
                file_name = os.path.basename(str(msg.get("audio") or ""))
            elif msg.get("voice"):
                file_name = os.path.basename(str(msg.get("voice") or ""))

            should_skip, text = await db.apply_message_filters(text, msg_type != "text", file_name)
            if should_skip or (msg_type == "text" and not text.strip()):
                outcome.record("skipped")
                await db.add_msg_log("JSON_DROP_REGEX", f"消息ID:{msg_id} | 已被正则过滤拦截")
                continue

            if await update_state_and_check_skip(source_scope_id, target_id, msg_id, text[:50] or "[媒体]", force_send=force_send):
                outcome.record("skipped")
                continue
            text, rewrite_count = await rewrite_message_links(text, source_scope_id, link_context)
            if rewrite_count:
                await db.add_msg_log("JSON_LINK_REWRITE", f"消息ID:{msg_id} | 命中 {rewrite_count} 个链接改写")

            media_path, media_type, _ = resolve_json_media(msg, json_dir)
            reply_to_id = await resolve_reply_target(
                source_scope_id,
                target_id,
                get_reply_source_msg_id(msg, "json"),
                "JSON",
                msg_id,
            )

            try:
                if media_path and not os.path.exists(media_path):
                    raise FileNotFoundError(f"媒体文件不存在: {media_path}")
                if media_path and os.path.exists(media_path):
                    media_path, created_temp = await _prepare_json_media_path(media_path, media_type, msg_id, hash_perturb)
                    file_size = os.path.getsize(media_path)
                    caption = text if text else None
                    file_label = _format_media_label(media_type, media_path)
                    media_has_spoiler = has_media_spoiler(msg, media_type, "json")
                    try:
                        upload_target = await _select_json_upload_target(
                            sender,
                            [file_size],
                            clone_fallback_to_user=clone_fallback_to_user,
                            wait_for_available_bot=not _json_should_fallback_to_user(sender, clone_fallback_to_user),
                        )

                        if upload_target["sender"] == "user":
                            sent = await _send_json_single_via_user(
                                chat_id,
                                media_type,
                                media_path,
                                caption,
                                reply_to_id,
                                SharedUploadProgressTracker("上传中 [改用辅助账号]", file_size),
                                file_label,
                                media_has_spoiler,
                            )
                        else:
                            sent = None
                            for _ in range(3):
                                if sync_state["stop_requested"]:
                                    break
                                tracker = UploadProgressTracker(f"上传中 [{upload_target['label']}]", file_size)
                                file = ProgressFSInputFile(media_path, tracker, file_label)
                                try:
                                    if media_type == "photo":
                                        send_coro = lambda: upload_target["client"].send_photo(
                                            chat_id, file, caption=caption, parse_mode="HTML", reply_to_message_id=reply_to_id, has_spoiler=media_has_spoiler
                                        )
                                    elif media_type == "video":
                                        send_coro = lambda: upload_target["client"].send_video(
                                            chat_id, file, caption=caption, parse_mode="HTML", reply_to_message_id=reply_to_id, has_spoiler=media_has_spoiler
                                        )
                                    elif media_type == "animation":
                                        send_coro = lambda: upload_target["client"].send_animation(
                                            chat_id, file, caption=caption, parse_mode="HTML", reply_to_message_id=reply_to_id
                                        )
                                    elif media_type == "audio":
                                        send_coro = lambda: upload_target["client"].send_audio(
                                            chat_id, file, caption=caption, parse_mode="HTML", reply_to_message_id=reply_to_id
                                        )
                                    elif media_type == "voice":
                                        send_coro = lambda: upload_target["client"].send_voice(
                                            chat_id, file, caption=caption, parse_mode="HTML", reply_to_message_id=reply_to_id
                                        )
                                    elif media_type == "sticker":
                                        sent = await execute_with_network_retry(
                                            lambda: upload_target["client"].send_sticker(chat_id, file, reply_to_message_id=reply_to_id),
                                            action_label=f"JSON 贴纸发送 {msg_id}",
                                            sync_state=sync_state,
                                            log_tag="JSON_NETWORK_RETRY",
                                        )
                                        await bot_engine.note_upload_success(upload_target["client"], file_size)
                                        break
                                    else:
                                        send_coro = lambda: upload_target["client"].send_document(
                                            chat_id, file, caption=caption, parse_mode="HTML", reply_to_message_id=reply_to_id
                                        )
                                    if media_type != "sticker":
                                        sent = await execute_with_network_retry(
                                            send_coro,
                                            action_label=f"JSON 媒体发送 {msg_id}",
                                            sync_state=sync_state,
                                            log_tag="JSON_NETWORK_RETRY",
                                        )
                                        await bot_engine.note_upload_success(upload_target["client"], file_size)
                                        break
                                except Exception as exc:
                                    if sync_state["stop_requested"]:
                                        raise
                                    if isinstance(exc, SyncNetworkRetryExhaustedError):
                                        raise
                                    if media_type == "sticker":
                                        thumb = str(msg.get("thumbnail") or "")
                                        thumb_path = os.path.join(json_dir, thumb) if thumb else ""
                                        if _is_request_entity_too_large(exc):
                                            if _json_should_fallback_to_user(sender, clone_fallback_to_user):
                                                await db.add_msg_log("JSON_FALLBACK", f"消息ID:{msg_id} | Bot 上传体积超限，已改用辅助账号发送")
                                                sent = await _send_json_single_via_user(
                                                    chat_id,
                                                    media_type,
                                                    media_path,
                                                    caption,
                                                    reply_to_id,
                                                    SharedUploadProgressTracker("上传中 [改用辅助账号]", file_size),
                                                    file_label,
                                                    media_has_spoiler,
                                                )
                                                break
                                            raise
                                        retry_after = _parse_retry_after_seconds(exc)
                                        if retry_after is not None:
                                            await bot_engine.mark_upload_bot_cooldown(upload_target["client"], retry_after + 1, f"JSON 消息ID:{msg_id}")
                                            upload_target = await _select_json_upload_target(
                                                sender,
                                                [file_size],
                                                clone_fallback_to_user=clone_fallback_to_user,
                                                wait_for_available_bot=not _json_should_fallback_to_user(sender, clone_fallback_to_user),
                                            )
                                            if upload_target["sender"] == "user":
                                                await db.add_msg_log("JSON_FALLBACK", f"消息ID:{msg_id} | Bot 频控，已切换辅助账号发送")
                                                sent = await _send_json_single_via_user(
                                                    chat_id,
                                                    media_type,
                                                    media_path,
                                                    caption,
                                                    reply_to_id,
                                                    SharedUploadProgressTracker("上传中 [改用辅助账号]", file_size),
                                                    file_label,
                                                    media_has_spoiler,
                                                )
                                                break
                                            continue
                                        if bot_engine.should_disable_upload_bot_for_error(exc):
                                            await bot_engine.disable_upload_bot(upload_target["client"], str(exc))
                                            upload_target = await _select_json_upload_target(
                                                sender,
                                                [file_size],
                                                clone_fallback_to_user=clone_fallback_to_user,
                                                wait_for_available_bot=not _json_should_fallback_to_user(sender, clone_fallback_to_user),
                                            )
                                            if upload_target["sender"] == "user":
                                                await db.add_msg_log("JSON_FALLBACK", f"消息ID:{msg_id} | 当前 Bot 已失效，已切换辅助账号发送")
                                                sent = await _send_json_single_via_user(
                                                    chat_id,
                                                    media_type,
                                                    media_path,
                                                    caption,
                                                    reply_to_id,
                                                    SharedUploadProgressTracker("上传中 [改用辅助账号]", file_size),
                                                    file_label,
                                                    media_has_spoiler,
                                                )
                                                break
                                            continue
                                        if not thumb_path or not os.path.exists(thumb_path):
                                            raise
                                        await db.add_msg_log("JSON_STICKER_AS_IMAGE", f"消息ID:{msg_id} | 贴纸发送失败，已改为缩略图图片发送: {exc}")
                                        sent = await _execute_with_retry(
                                            lambda: bot_engine.aiogram_bot.send_photo(
                                                chat_id,
                                                FSInputFile(thumb_path),
                                                caption=caption,
                                                parse_mode="HTML",
                                                reply_to_message_id=reply_to_id,
                                            ),
                                            action_label=f"贴纸缩略图回退 [{msg_id}]",
                                        )
                                        break
                                    if _is_request_entity_too_large(exc):
                                        if _json_should_fallback_to_user(sender, clone_fallback_to_user):
                                            await db.add_msg_log("JSON_FALLBACK", f"消息ID:{msg_id} | Bot 上传体积超限，已改用辅助账号发送")
                                            sent = await _send_json_single_via_user(
                                                chat_id,
                                                media_type,
                                                media_path,
                                                caption,
                                                reply_to_id,
                                                SharedUploadProgressTracker("上传中 [改用辅助账号]", file_size),
                                                file_label,
                                                media_has_spoiler,
                                            )
                                            break
                                        raise
                                    retry_after = _parse_retry_after_seconds(exc)
                                    if retry_after is not None:
                                        await bot_engine.mark_upload_bot_cooldown(upload_target["client"], retry_after + 1, f"JSON 消息ID:{msg_id}")
                                        upload_target = await _select_json_upload_target(
                                            sender,
                                            [file_size],
                                            clone_fallback_to_user=clone_fallback_to_user,
                                            wait_for_available_bot=not _json_should_fallback_to_user(sender, clone_fallback_to_user),
                                        )
                                        if upload_target["sender"] == "user":
                                            await db.add_msg_log("JSON_FALLBACK", f"消息ID:{msg_id} | Bot 频控，已切换辅助账号发送")
                                            sent = await _send_json_single_via_user(
                                                chat_id,
                                                media_type,
                                                media_path,
                                                caption,
                                                reply_to_id,
                                                SharedUploadProgressTracker("上传中 [改用辅助账号]", file_size),
                                                file_label,
                                                media_has_spoiler,
                                            )
                                            break
                                        continue
                                    if bot_engine.should_disable_upload_bot_for_error(exc):
                                        await bot_engine.disable_upload_bot(upload_target["client"], str(exc))
                                        upload_target = await _select_json_upload_target(
                                            sender,
                                            [file_size],
                                            clone_fallback_to_user=clone_fallback_to_user,
                                            wait_for_available_bot=not _json_should_fallback_to_user(sender, clone_fallback_to_user),
                                        )
                                        if upload_target["sender"] == "user":
                                            await db.add_msg_log("JSON_FALLBACK", f"消息ID:{msg_id} | 当前 Bot 已失效，已切换辅助账号发送")
                                            sent = await _send_json_single_via_user(
                                                chat_id,
                                                media_type,
                                                media_path,
                                                caption,
                                                reply_to_id,
                                                SharedUploadProgressTracker("上传中 [改用辅助账号]", file_size),
                                                file_label,
                                                media_has_spoiler,
                                            )
                                            break
                                        continue
                                    raise
                            if sent is None and not sync_state["stop_requested"] and _json_should_fallback_to_user(sender, clone_fallback_to_user):
                                await db.add_msg_log("JSON_FALLBACK", f"消息ID:{msg_id} | Bot 发送失败，已改用辅助账号发送")
                                sent = await _send_json_single_via_user(
                                    chat_id,
                                    media_type,
                                    media_path,
                                    caption,
                                    reply_to_id,
                                    SharedUploadProgressTracker("上传中 [改用辅助账号]", file_size),
                                    file_label,
                                    media_has_spoiler,
                                )
                            if sent is None:
                                if sync_state["stop_requested"]:
                                    return outcome
                                raise RuntimeError("媒体发送未返回结果")
                        sent_id = _get_sent_message_id(sent)
                    finally:
                        if created_temp:
                            try:
                                os.remove(media_path)
                            except Exception:
                                pass
                elif text:
                    upload_target = await _select_json_upload_target(
                        sender,
                        [],
                        clone_fallback_to_user=clone_fallback_to_user,
                        wait_for_available_bot=not _json_should_fallback_to_user(sender, clone_fallback_to_user),
                    )
                    if upload_target["sender"] == "user":
                        sent = await _send_json_text_via_user(chat_id, text, reply_to_id)
                    else:
                        sent = await _send_json_text_via_bot(
                            upload_target,
                            sender,
                            clone_fallback_to_user,
                            chat_id,
                            text,
                            reply_to_id,
                            msg_id,
                        )
                    if sent is None:
                        if sync_state["stop_requested"]:
                            return outcome
                        raise RuntimeError("文本发送未返回结果")
                    sent_id = _get_sent_message_id(sent)
                else:
                    outcome.record("failed")
                    await db.add_msg_log("JSON_MEDIA_MISSING", f"消息ID:{msg_id} | 没有可发送的媒体或文本")
                    continue

                if not sent_id:
                    outcome.record("sent_unmapped")
                    await db.add_msg_log("JSON_SEND_UNMAPPED", f"消息ID:{msg_id} | 发送未返回消息 ID，请核对目标频道")
                    continue
                await record_success(source_scope_id, target_id, msg_id, sent_id, force_send=force_send)
                outcome.record("sent_mapped")
                await db.add_msg_log("JSON_SEND", f"消息ID:{msg_id} | 目标:[{target_id}] 新ID:{sent_id} | 上传成功")
            except JsonSyncFatalError as exc:
                outcome.record("failed")
                await log_sync_error(f"JSON 致命错误 ID {msg_id}", exc)
                raise
            except Exception as exc:
                if sync_state["stop_requested"]:
                    break
                outcome.record("failed")
                if isinstance(exc, SyncNetworkRetryExhaustedError):
                    raise
                await log_sync_error(f"JSON 消息上传失败 ID {msg_id}", exc)

            await asyncio.sleep(safe_delay)
    return outcome
