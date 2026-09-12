from __future__ import annotations

import asyncio
import os
from aiogram.types import FSInputFile
from aiogram.types import InputMediaVideo as AioVideo
from aiogram.types import ReplyParameters
from pyrogram.enums import ParseMode

import bot_engine
import database as db
from app_config import get_config
from services.sync_services import (
    SAVED_MESSAGES_TARGET_ID,
    SyncNetworkRetryExhaustedError,
    build_link_rewrite_context,
    create_progress_callback,
    execute_with_network_retry,
    format_channel_check_error,
    get_quote_payload,
    log_sync_error,
    resolve_chat_id,
    resolve_destination_chat_id,
    resolve_reply_target,
    rewrite_message_links,
    safe_execute,
)
from ..core import (
    ProgressFSInputFile,
    UploadProgressTracker,
    build_pyro_progress_callback,
    format_upload_label,
    get_media_reference,
    get_msg_meta,
    get_reply_source_msg_id,
    has_media_spoiler,
    has_text_spoiler,
    normalize_bot_html,
    normalize_pyro_html,
    prepend_source_header_html,
    rewrite_media_group_captions,
)
from ..media import describe_hash_perturb_reason, prepare_media_for_send
from ..runtime import (
    TEMP_DIR,
    clear_temp_dir_files,
    count_unmapped_group,
    finish_sync_session,
    record_success,
    start_sync_session,
    sync_state,
    update_state_and_check_skip,
)
from ..senders import (
    build_bot_media_group,
    build_user_media_group,
    dynamic_send,
    resolve_upload_target,
    should_fallback_to_user,
)
from .helpers import (
    _build_temp_download_path,
    _clone_should_fallback_to_user,
    _download_media_thumbnail,
    _execute_with_clone_retry,
    _execute_with_clone_retry_interruptibly,
    _is_chat_forwards_restricted,
    _is_request_entity_too_large,
    _is_topics_parse_error,
    _parse_retry_after_seconds,
)
from ..json_import import process_json_sync
from ..runtime.result import SyncResult

SYNC_RESULT_SENT_MAPPED = "sent_mapped"
SYNC_RESULT_SENT_UNMAPPED = "sent_unmapped"
SYNC_RESULT_SKIPPED = "skipped"
SYNC_RESULT_FAILED = "failed"


def _normalize_sync_html(mode: str, html_text: str | None) -> str:
    return normalize_pyro_html(html_text) if mode == "api" else normalize_bot_html(html_text)


def _base_api_copy_kwargs(chat_id, source_id, msg_id):
    return {"chat_id": chat_id, "from_chat_id": source_id, "message_id": msg_id}


def _add_reply_kwargs(kwargs: dict, reply_to_id):
    if reply_to_id:
        kwargs["reply_to_message_id"] = reply_to_id
    return kwargs


def _add_quote_kwargs(kwargs: dict, quote_data, reply_to_id):
    if quote_data and reply_to_id:
        kwargs["quote_text"] = quote_data["text"]
        if quote_data.get("entities"):
            kwargs["quote_entities"] = quote_data["entities"]
    return kwargs


def _set_group_download_status(first_id: int, completed: int, total: int, detail: str = ""):
    suffix = f"\n{detail}" if detail else ""
    sync_state["current_text"] = f"组下载 {completed}/{total} | 组首ID:{first_id}{suffix}"


def _download_actor_label(app) -> str:
    return bot_engine.describe_user_client(app, fallback="辅助账号")


async def _media_group_should_drop(group, mode: str) -> bool:
    """任一成员命中屏蔽规则时丢弃整个媒体组。"""
    for item in group:
        item_type, _ = get_msg_meta(item, mode)
        caption = getattr(item, "caption", None)
        text_html = str(getattr(caption, "html", caption) or "")
        media = getattr(item, item_type, None)
        file_name = str(getattr(media, "file_name", "") or "")
        should_skip, _ = await db.apply_message_filters(text_html, True, file_name)
        if should_skip:
            return True
    return False


def _build_api_media_group_copy_kwargs(
    chat_id,
    source_id,
    group_first_id,
    rewritten_captions,
    captions_changed,
    spoiler_flags,
    group_has_spoiler,
    reply_to_id,
    quote_data,
):
    kwargs = _base_api_copy_kwargs(chat_id, source_id, group_first_id)
    if captions_changed or group_has_spoiler:
        kwargs["parse_mode"] = ParseMode.HTML
        if captions_changed:
            kwargs["captions"] = _api_group_captions(rewritten_captions)
        if group_has_spoiler:
            kwargs["has_spoilers"] = spoiler_flags
    _add_reply_kwargs(kwargs, reply_to_id)
    _add_quote_kwargs(kwargs, quote_data, reply_to_id)
    return kwargs


def _clone_media_group_items(downloaded_files, mode):
    return [(item, path, get_msg_meta(item, mode)[0]) for item, path in downloaded_files]


def _build_clone_media_group_send_kwargs(
    actual_sender,
    chat_id,
    group_items,
    rewritten_captions,
    thumbnail_paths,
    spoiler_flags,
    upload_label,
    total_bytes,
    reply_to_id,
    quote_data,
    item_count,
    client,
):
    if actual_sender == "bot":
        tracker, media_list = build_bot_media_group(group_items, rewritten_captions, thumbnail_paths, total_bytes, upload_label, spoiler_flags)
    else:
        tracker = UploadProgressTracker(f"上传媒体组 [{upload_label}]", total_bytes)
        media_list = build_user_media_group(group_items, rewritten_captions, thumbnail_paths, spoiler_flags)
    send_kwargs = {"chat_id": chat_id, "media": media_list}
    if reply_to_id:
        send_kwargs["reply_to_message_id"] = reply_to_id
    if quote_data and reply_to_id:
        if actual_sender == "bot":
            send_kwargs.pop("reply_to_message_id", None)
            send_kwargs["reply_parameters"] = ReplyParameters(
                message_id=reply_to_id,
                quote=quote_data["text"],
                quote_position=quote_data.get("position"),
            )
        else:
            send_kwargs["quote_text"] = quote_data["text"]
            if quote_data.get("entities"):
                send_kwargs["quote_entities"] = quote_data["entities"]
    if actual_sender != "bot":
        send_kwargs["progress"] = build_pyro_progress_callback(
            tracker,
            f"上传媒体组: {item_count} 项",
            total_bytes=total_bytes,
            client=client,
        )
    return send_kwargs


async def _download_clone_media_item(
    app,
    msg,
    mode: str,
    *,
    progress_label: str,
    normal_download_semaphore=None,
):
    item_type, _ = get_msg_meta(msg, mode)
    download_target = _build_temp_download_path(msg, item_type)
    _set_group_download_status(msg.id, 0, 1, f"准备下载 {item_type} | 消息ID:{msg.id}")
    async def _download_normally():
        return await execute_with_network_retry(
            lambda: app.download_media(
                msg,
                file_name=download_target,
                progress=create_progress_callback(progress_label, sync_state),
            ),
            action_label=f"媒体下载 {msg.id}",
            sync_state=sync_state,
            log_tag="CLONE_NETWORK_RETRY",
        )

    if normal_download_semaphore is None:
        return await _download_normally()
    async with normal_download_semaphore:
        return await _download_normally()


async def _safe_get_messages(app, source_id, msg_ids):
    try:
        return await execute_with_network_retry(
            lambda: app.get_messages(source_id, msg_ids),
            action_label=f"批量拉取消息 {msg_ids[0]}-{msg_ids[-1]}",
            sync_state=sync_state,
            log_tag="SYNC_NETWORK_RETRY",
        )
    except Exception as exc:
        if "topics" not in str(exc).lower():
            raise
    result = []
    for msg_id in msg_ids:
        try:
            msg = await execute_with_network_retry(
                lambda msg_id=msg_id: app.get_messages(source_id, msg_id),
                action_label=f"逐条拉取消息 {msg_id}",
                sync_state=sync_state,
                log_tag="SYNC_NETWORK_RETRY",
            )
        except Exception:
            msg = None
        result.append(msg)
    return result


async def _send_api_media(
    app,
    msg_type,
    chat_id,
    media_ref,
    caption_html,
    reply_to_id=None,
    quote_data=None,
    has_spoiler=False,
):
    return await execute_with_network_retry(
        lambda: dynamic_send(
            app,
            msg_type,
            chat_id,
            media_ref,
            caption_html,
            ParseMode.HTML,
            reply_to_message_id=reply_to_id,
            quote_data=quote_data,
            has_spoiler=has_spoiler,
        ),
        action_label=f"API 媒体消息发送 {msg_type}",
        sync_state=sync_state,
        log_tag="SYNC_NETWORK_RETRY",
    )


def _api_group_captions(rewritten_captions):
    return [normalize_pyro_html(caption_html or "") for caption_html in rewritten_captions]


async def _fetch_last_message_id(app, source_id):
    async def _load_last():
        async for last_msg in app.get_chat_history(source_id, limit=1):
            return last_msg.id
        return 1

    return await execute_with_network_retry(
        _load_last,
        action_label=f"获取频道末尾消息 {source_id}",
        sync_state=sync_state,
        log_tag="SYNC_NETWORK_RETRY",
    )


async def sync_single_message(
    mode,
    sender,
    app,
    bot,
    source_id,
    target_id,
    msg,
    safe_delay,
    force_send,
    hash_perturb=False,
    clone_fallback_to_user=True,
    include_external_source_header: bool = False,
    source_username_override: str | None = None,
    chat_id: int | None = None,
):
    # chat_id 为实际发送目的地（收藏夹目标 = 当前辅助账号 own id），target_id 为 DB 键
    # （收藏夹目标 = sentinel -1）。普通频道目标二者相同，缺省时回退 target_id。
    if chat_id is None:
        chat_id = target_id
    msg_type, _ = get_msg_meta(msg, mode)
    has_media = msg_type != "text"
    file_name = getattr(getattr(msg, msg_type, None), "file_name", "") if msg_type in ["document", "video"] else ""
    text_html = msg.text.html if msg.text else (msg.caption.html if msg.caption else "") if hasattr(msg, "text") else ""
    text_html = prepend_source_header_html(text_html, msg, enabled=include_external_source_header)
    quote_data = get_quote_payload(msg)

    should_skip, new_html = await db.apply_message_filters(text_html, has_media, file_name or "")
    if should_skip or (not has_media and not new_html.strip()):
        return SYNC_RESULT_SKIPPED
    if await update_state_and_check_skip(source_id, target_id, msg.id, new_html[:50] or "[媒体]", force_send=force_send):
        return SYNC_RESULT_SKIPPED

    reply_to_id = await resolve_reply_target(source_id, target_id, get_reply_source_msg_id(msg, mode), mode.upper(), msg.id)
    link_context = await build_link_rewrite_context(
        bot_engine.aiogram_bot,
        source_id,
        target_id,
        source_username_override=source_username_override,
    )
    new_html, rewrite_count = await rewrite_message_links(new_html, source_id, link_context)
    new_html = _normalize_sync_html(mode, new_html)
    media_has_spoiler = has_media_spoiler(msg, msg_type, mode)
    text_has_spoiler = has_text_spoiler(msg, new_html)
    if rewrite_count:
        await db.add_msg_log(f"{mode.upper()}_LINK_REWRITE", f"原始:[{source_id}] 消息ID:{msg.id} | 命中 {rewrite_count} 个链接改写")

    try:
        if mode == "api":
            if quote_data and reply_to_id:
                if not has_media:
                    kwargs = {
                        "chat_id": chat_id,
                        "text": new_html,
                        "parse_mode": ParseMode.HTML,
                        "reply_to_message_id": reply_to_id,
                        "quote_text": quote_data["text"],
                    }
                    if quote_data.get("entities"):
                        kwargs["quote_entities"] = quote_data["entities"]
                    sent_id = (
                        await execute_with_network_retry(
                            lambda: app.send_message(**kwargs),
                            action_label=f"API 文本发送 {msg.id}",
                            sync_state=sync_state,
                            log_tag="SYNC_NETWORK_RETRY",
                        )
                    ).id
                else:
                    media_ref = get_media_reference(msg, msg_type)
                    if not media_ref:
                        raise ValueError(f"引用回复媒体缺少可复用 file_id: {msg.id}")
                    sent = await _send_api_media(
                        app,
                        msg_type,
                        chat_id,
                        media_ref,
                        new_html,
                        reply_to_id,
                        quote_data,
                        has_spoiler=media_has_spoiler,
                    )
                    sent_id = sent.id
                await db.add_msg_log("API_QUOTE_SEND", f"原始:[{source_id}] 消息ID:{msg.id} | 已按引用回复发送")
            elif new_html != text_html or include_external_source_header or text_has_spoiler or media_has_spoiler:
                # 如果文本发生变化，或者启用了外部来源前缀功能，使用 send_message/copy_message with caption
                # 这样可以确保转发信息被正确处理（避免 copy_message 的不一致行为）
                if not has_media:
                    kwargs = {"chat_id": chat_id, "text": new_html, "parse_mode": ParseMode.HTML}
                    if reply_to_id:
                        kwargs["reply_to_message_id"] = reply_to_id
                    sent_id = (
                        await execute_with_network_retry(
                            lambda: app.send_message(**kwargs),
                            action_label=f"API 文本发送 {msg.id}",
                            sync_state=sync_state,
                            log_tag="SYNC_NETWORK_RETRY",
                        )
                    ).id
                elif media_has_spoiler:
                    media_ref = get_media_reference(msg, msg_type)
                    if not media_ref:
                        raise ValueError(f"媒体遮罩消息缺少可复用 file_id: {msg.id}")
                    sent = await _send_api_media(app, msg_type, chat_id, media_ref, new_html, reply_to_id, has_spoiler=True)
                    sent_id = sent.id
                else:
                    kwargs = _add_reply_kwargs(_base_api_copy_kwargs(chat_id, source_id, msg.id), reply_to_id)
                    kwargs.update({"caption": new_html, "parse_mode": ParseMode.HTML})
                    sent_id = (
                        await execute_with_network_retry(
                            lambda: app.copy_message(**kwargs),
                            action_label=f"API 复制消息 {msg.id}",
                            sync_state=sync_state,
                            log_tag="SYNC_NETWORK_RETRY",
                        )
                    ).id
            else:
                kwargs = _base_api_copy_kwargs(chat_id, source_id, msg.id)
                _add_reply_kwargs(kwargs, reply_to_id)
                sent_id = (
                    await execute_with_network_retry(
                        lambda: app.copy_message(**kwargs),
                        action_label=f"API 复制消息 {msg.id}",
                        sync_state=sync_state,
                        log_tag="SYNC_NETWORK_RETRY",
                    )
                ).id
        else:
            if not has_media:
                sent = await _execute_with_clone_retry_interruptibly(
                    lambda: dynamic_send(
                        bot if sender == "bot" else app,
                        "text",
                        chat_id,
                        None,
                        new_html,
                        "HTML" if sender == "bot" else ParseMode.HTML,
                        reply_to_message_id=reply_to_id,
                        quote_data=quote_data if reply_to_id else None,
                    ),
                    action_label=f"单条消息 {msg.id}",
                    stop_client=app if sender != "bot" else None,
                )
                sent_id = sent.message_id if sender == "bot" else sent.id
            else:
                file_path = None
                for _ in range(3):
                    if sync_state["stop_requested"]:
                        break
                    try:
                        file_path = await _download_clone_media_item(
                            app,
                            msg,
                            mode,
                            progress_label="下载中",
                        )
                        if file_path:
                            break
                    except Exception as exc:
                        if "STOP_REQUESTED" in str(exc):
                            raise
                        if isinstance(exc, SyncNetworkRetryExhaustedError):
                            raise
                        retry_after = _parse_retry_after_seconds(exc)
                        if retry_after is not None:
                            wait_seconds = retry_after + 1
                            actor_label = _download_actor_label(app)
                            sync_state["current_text"] = f"等待重试\n{actor_label} 下载源媒体触发限流，需等待 {wait_seconds} 秒"
                            await db.add_msg_log(
                                "CLONE_DOWNLOAD_WAIT",
                                f"消息ID:{msg.id} | {actor_label} 下载源媒体时触发 Telegram 限流，需等待 {wait_seconds} 秒后重试",
                            )
                            await asyncio.sleep(wait_seconds)
                            continue
                        await asyncio.sleep(2)
                if not file_path or sync_state["stop_requested"]:
                    return SYNC_RESULT_FAILED

                file_path = await prepare_media_for_send(file_path, msg_type, msg.id, hash_perturb)
                file_size = os.path.getsize(file_path)
                upload_target = await safe_execute(
                    resolve_upload_target(
                        sender,
                        app,
                        [file_size],
                        allow_user_fallback=should_fallback_to_user(sender, clone_fallback_to_user),
                    ),
                    sync_state,
                )
                actual_sender = upload_target["sender"]
                client = upload_target["client"]
                parse_mode = upload_target["parse_mode"]
                sent_id = None
                bot_size_limit_hit = False
                thumbnail_path = await _download_media_thumbnail(app, msg, msg_type) if msg_type in {"video", "document"} else None

                for _ in range(3):
                    if sync_state["stop_requested"]:
                        break
                    try:
                        file_label = format_upload_label(msg_type, file_path)
                        tracker = UploadProgressTracker(f"上传中 [{upload_target['label']}]", file_size)
                        media_arg = ProgressFSInputFile(file_path, tracker, file_label) if actual_sender == "bot" else file_path
                        thumbnail_arg = FSInputFile(thumbnail_path) if actual_sender == "bot" and thumbnail_path and os.path.exists(thumbnail_path) else thumbnail_path
                        sent = await _execute_with_clone_retry_interruptibly(
                            lambda: dynamic_send(
                                client,
                                msg_type,
                                chat_id,
                                media_arg,
                                new_html,
                                parse_mode,
                                reply_to_message_id=reply_to_id,
                                quote_data=quote_data if reply_to_id else None,
                                progress=build_pyro_progress_callback(tracker, file_label, total_bytes=file_size, client=client) if actual_sender != "bot" else None,
                                thumbnail=thumbnail_arg,
                                source_item=msg,
                            ),
                            action_label=f"单条媒体 {msg.id}",
                            stop_client=client if actual_sender != "bot" else None,
                        )
                        sent_id = sent.message_id if actual_sender == "bot" else sent.id
                        if actual_sender == "bot":
                            await bot_engine.note_upload_success(client, file_size)
                        break
                    except Exception as exc:
                        if "STOP_REQUESTED" in str(exc):
                            raise
                        if isinstance(exc, SyncNetworkRetryExhaustedError):
                            raise
                        if actual_sender == "bot" and _is_request_entity_too_large(exc):
                            bot_size_limit_hit = True
                            break
                        retry_after = _parse_retry_after_seconds(exc)
                        if actual_sender == "bot" and retry_after is not None:
                            await bot_engine.mark_upload_bot_cooldown(client, retry_after + 1, f"CLONE 单条消息ID:{msg.id}")
                            upload_target = await safe_execute(
                                resolve_upload_target(
                                    sender,
                                    app,
                                    [file_size],
                                    allow_user_fallback=should_fallback_to_user(sender, clone_fallback_to_user),
                                    wait_for_available_bot=not should_fallback_to_user(sender, clone_fallback_to_user),
                                ),
                                sync_state,
                            )
                            actual_sender = upload_target["sender"]
                            client = upload_target["client"]
                            parse_mode = upload_target["parse_mode"]
                            if actual_sender == "user":
                                await db.add_msg_log("BOT_FALLBACK", f"原始:[{source_id}] 消息ID:{msg.id} | Bot 频控，已切换辅助账号继续发送")
                            continue
                        if actual_sender == "bot" and bot_engine.should_disable_upload_bot_for_error(exc):
                            await bot_engine.disable_upload_bot(client, str(exc))
                            upload_target = await safe_execute(
                                resolve_upload_target(
                                    sender,
                                    app,
                                    [file_size],
                                    allow_user_fallback=should_fallback_to_user(sender, clone_fallback_to_user),
                                    wait_for_available_bot=not should_fallback_to_user(sender, clone_fallback_to_user),
                                ),
                                sync_state,
                            )
                            actual_sender = upload_target["sender"]
                            client = upload_target["client"]
                            parse_mode = upload_target["parse_mode"]
                            if actual_sender == "user":
                                await db.add_msg_log("BOT_FALLBACK", f"原始:[{source_id}] 消息ID:{msg.id} | 当前 Bot 已失效，已切换辅助账号继续发送")
                            continue
                        await asyncio.sleep(2)

                if sent_id is None and _clone_should_fallback_to_user(sender, clone_fallback_to_user) and actual_sender == "bot":
                    fallback_reason = "Bot 上传体积超限，改用辅助账号重传" if bot_size_limit_hit else f"{upload_target['label']} 上传失败，改用辅助账号重传"
                    await db.add_msg_log("BOT_FALLBACK", f"原始:[{source_id}] 消息ID:{msg.id} | {fallback_reason}")
                    for _ in range(3):
                        if sync_state["stop_requested"]:
                            break
                        try:
                            file_label = format_upload_label(msg_type, file_path)
                            tracker = UploadProgressTracker("上传中 [改用辅助账号]", file_size)
                            sent = await _execute_with_clone_retry_interruptibly(
                                lambda: dynamic_send(
                                    app,
                                    msg_type,
                                    chat_id,
                                    file_path,
                                    new_html,
                                    ParseMode.HTML,
                                    reply_to_message_id=reply_to_id,
                                    quote_data=quote_data if reply_to_id else None,
                                    progress=build_pyro_progress_callback(tracker, file_label, total_bytes=file_size, client=app),
                                    thumbnail=thumbnail_path,
                                    source_item=msg,
                                ),
                                action_label=f"单条媒体辅助回退 {msg.id}",
                                stop_client=app,
                            )
                            sent_id = sent.id
                            break
                        except Exception as exc:
                            if "STOP_REQUESTED" in str(exc):
                                raise
                            if isinstance(exc, SyncNetworkRetryExhaustedError):
                                raise
                            await asyncio.sleep(2)

                try:
                    os.remove(file_path)
                except Exception:
                    pass
                if thumbnail_path:
                    try:
                        os.remove(thumbnail_path)
                    except Exception:
                        pass
                if sent_id is None:
                    return SYNC_RESULT_FAILED

        await record_success(source_id, target_id, msg.id, sent_id, force_send=force_send)
        if mode == "clone" and quote_data and reply_to_id:
            await db.add_msg_log("CLONE_QUOTE_SEND", f"原始:[{source_id}] 消息ID:{msg.id} | 已按引用回复发送")
        await db.add_msg_log(f"{mode.upper()}_SEND", f"原始:[{source_id}] 消息ID:{msg.id} | 目标:[{target_id}] 新ID:{sent_id} | 同步成功")
        result = SYNC_RESULT_SENT_MAPPED
    except Exception as exc:
        if mode == "api" and _is_chat_forwards_restricted(exc):
            raise RuntimeError("该频道不支持转发，请使用下载重传") from exc
        if isinstance(exc, SyncNetworkRetryExhaustedError):
            raise
        if sync_state["stop_requested"]:
            return SYNC_RESULT_FAILED
        await log_sync_error(f"单条同步异常 ID {msg.id}", exc)
        result = SYNC_RESULT_FAILED

    await asyncio.sleep(safe_delay)
    return result


async def sync_media_group(
    mode,
    sender,
    app,
    bot,
    source_id,
    target_id,
    group,
    safe_delay,
    force_send,
    hash_perturb=False,
    clone_fallback_to_user=True,
    include_external_source_header: bool = False,
    source_username_override: str | None = None,
    chat_id: int | None = None,
):
    if chat_id is None:
        chat_id = target_id
    if await update_state_and_check_skip(source_id, target_id, group[0].id, "[媒体组]", force_send=force_send):
        return SYNC_RESULT_SKIPPED
    if await _media_group_should_drop(group, mode):
        await db.add_msg_log(
            f"{mode.upper()}_DROP_REGEX",
            f"原始:[{source_id}] 媒体组消息ID:{[item.id for item in group]} | 已被正则过滤拦截",
        )
        return SYNC_RESULT_SKIPPED

    reply_to_id = await resolve_reply_target(source_id, target_id, get_reply_source_msg_id(group[0], mode), mode.upper(), group[0].id)
    quote_data = get_quote_payload(group[0])
    rewritten_captions, captions_changed, caption_rewrite_count = await rewrite_media_group_captions(
        source_id,
        target_id,
        group,
        source_username_override=source_username_override,
        include_external_source_header=include_external_source_header,
    )
    spoiler_flags = []
    for item in group:
        item_type, _ = get_msg_meta(item, mode)
        spoiler_flags.append(has_media_spoiler(item, item_type, mode))
    group_has_spoiler = any(spoiler_flags)
    result = SYNC_RESULT_FAILED

    if mode == "api":
        last_error = None
        for _ in range(3):
            if sync_state["stop_requested"]:
                break
            try:
                kwargs = _build_api_media_group_copy_kwargs(
                    chat_id,
                    source_id,
                    group[0].id,
                    rewritten_captions,
                    captions_changed,
                    spoiler_flags,
                    group_has_spoiler,
                    reply_to_id,
                    quote_data,
                )
                copied_msgs = list(await execute_with_network_retry(
                    lambda: app.copy_media_group(**kwargs),
                    action_label=f"API 媒体组复制 {group[0].id}",
                    sync_state=sync_state,
                    log_tag="SYNC_NETWORK_RETRY",
                ) or [])
                for orig_m, new_m in zip(group, copied_msgs):
                    await record_success(source_id, target_id, orig_m.id, new_m.id, force_send=force_send)
                if len(copied_msgs) != len(group):
                    await db.add_msg_log(
                        "API_GROUP_PARTIAL",
                        f"原始:[{source_id}] 组首ID:{group[0].id} | 源组 {len(group)} 项，回包 {len(copied_msgs)} 项 | 未能完整确认映射",
                    )
                    count_unmapped_group()
                if captions_changed:
                    await db.add_msg_log("API_GROUP_CAPTION_REWRITE", f"原始:[{source_id}] 组首ID:{group[0].id} | 命中 {caption_rewrite_count} 个 caption 链接改写")
                if quote_data and reply_to_id:
                    await db.add_msg_log("API_QUOTE_GROUP_SEND", f"原始:[{source_id}] 组首ID:{group[0].id} | 已按引用回复发送媒体组")
                result = (
                    SYNC_RESULT_SENT_MAPPED
                    if len(copied_msgs) == len(group)
                    else SYNC_RESULT_SENT_UNMAPPED
                )
                break
            except TypeError as exc:
                # 只有确认是 Pyrofork 回包 topics 解析异常才认定"已发送"，
                # 其余 TypeError（如参数构造错误）必须继续按失败处理，避免整组被静默丢弃。
                if _is_topics_parse_error(exc):
                    await db.add_msg_log(
                        "API_TOPICS_COMPAT",
                        f"原始:[{source_id}] 组首ID:{group[0].id} | 回包 topics 解析异常，可能已发送但未记录映射",
                    )
                    count_unmapped_group()
                    result = SYNC_RESULT_SENT_UNMAPPED
                    break
                last_error = exc
            except Exception as exc:
                if "STOP_REQUESTED" in str(exc):
                    raise
                if isinstance(exc, SyncNetworkRetryExhaustedError):
                    raise
                if _is_chat_forwards_restricted(exc):
                    raise RuntimeError("该频道不支持转发，请使用下载重传") from exc
                last_error = exc
                await asyncio.sleep(2)
        if result == SYNC_RESULT_FAILED and last_error is not None and not sync_state["stop_requested"]:
            await log_sync_error(f"API 媒体组复制失败 组首ID {group[0].id}", last_error)
    else:
        downloaded_files = []
        dl_success = False
        for attempt in range(1, 4):
            if sync_state["stop_requested"]:
                break
            try:
                sem = asyncio.Semaphore(4)
                normal_download_semaphore = asyncio.Semaphore(1)
                completed = 0
                completed_lock = asyncio.Lock()
                total_items = len(group)

                async def dl_album_item(m_item, idx):
                    nonlocal completed
                    async with sem:
                        item_type, _ = get_msg_meta(m_item, mode)
                        path = await _download_clone_media_item(
                            app,
                            m_item,
                            mode,
                            progress_label=f"组内下载 [{idx}/{total_items}]",
                            normal_download_semaphore=normal_download_semaphore,
                        )
                        async with completed_lock:
                            completed += 1
                            _set_group_download_status(group[0].id, completed, total_items, f"已完成 {item_type} | 消息ID:{m_item.id}")
                        return path

                results = await asyncio.gather(
                    *[dl_album_item(item, index + 1) for index, item in enumerate(group)],
                    return_exceptions=True,
                )
                if any(isinstance(result, Exception) for result in results):
                    retry_waits = []
                    failure_details = []
                    for item, result in zip(group, results):
                        if isinstance(result, Exception):
                            wait_seconds = _parse_retry_after_seconds(result)
                            if wait_seconds is not None:
                                retry_waits.append(wait_seconds)
                            failure_details.append(f"消息ID:{item.id} -> {result}")
                        elif isinstance(result, str):
                            try:
                                os.remove(result)
                            except Exception:
                                pass
                    if any("STOP_REQUESTED" in str(result) for result in results):
                        sync_state["stop_requested"] = True
                    elif retry_waits:
                        wait_seconds = max(retry_waits) + 1
                        actor_label = _download_actor_label(app)
                        sync_state["current_text"] = f"等待重试\n{actor_label} 下载源媒体组触发限流，需等待 {wait_seconds} 秒"
                        await db.add_msg_log(
                            "CLONE_GROUP_DOWNLOAD_WAIT",
                            f"组首ID:{group[0].id} | {actor_label} 下载源媒体组时触发 Telegram 限流，需等待 {wait_seconds} 秒后重试",
                        )
                        await asyncio.sleep(wait_seconds)
                    else:
                        await db.add_msg_log(
                            "CLONE_GROUP_DOWNLOAD_RETRY",
                            f"组首ID:{group[0].id} | 第 {attempt}/3 次下载失败 | " + " ; ".join(failure_details[:3]),
                        )
                        await asyncio.sleep(2)
                    continue
                downloaded_files = [(item, path) for item, path in zip(group, results) if isinstance(path, str)]
                dl_success = True
                break
            except Exception as exc:
                if isinstance(exc, SyncNetworkRetryExhaustedError):
                    raise
                await db.add_msg_log("CLONE_GROUP_DOWNLOAD_RETRY", f"组首ID:{group[0].id} | 第 {attempt}/3 次下载异常 | {exc}")
                await asyncio.sleep(2)

        if not dl_success or sync_state["stop_requested"]:
            if not sync_state["stop_requested"] and not dl_success:
                await log_sync_error(f"媒体组下载失败 组首ID {group[0].id}", RuntimeError("媒体组下载重试 3 次后仍失败"))
            for _, path in downloaded_files:
                try:
                    os.remove(path)
                except Exception:
                    pass
            return SYNC_RESULT_FAILED

        if hash_perturb:
            perturbed_files = []
            for item, path in downloaded_files:
                item_type, _ = get_msg_meta(item, mode)
                new_path = await prepare_media_for_send(path, item_type, item.id, True)
                perturbed_files.append((item, new_path))
            downloaded_files = perturbed_files
        else:
            for item, _ in downloaded_files:
                item_type, _ = get_msg_meta(item, mode)
                if item_type in {"photo", "video"}:
                    await db.add_msg_log("HASH_PERTURB_SKIP", f"消息ID:{item.id} | 类型:{item_type} | {describe_hash_perturb_reason('disabled')}")

        file_sizes = [os.path.getsize(path) for _, path in downloaded_files]
        upload_target = await safe_execute(
            resolve_upload_target(
                sender,
                app,
                file_sizes,
                allow_user_fallback=should_fallback_to_user(sender, clone_fallback_to_user),
            ),
            sync_state,
        )
        actual_sender = upload_target["sender"]
        client = upload_target["client"]
        parse_mode = upload_target["parse_mode"]
        sync_state["current_text"] = f"准备上传媒体组 [{upload_target['label']}]"
        thumbnail_paths = {}
        for item, _ in downloaded_files:
            item_type, _ = get_msg_meta(item, mode)
            if item_type in {"video", "document"}:
                thumbnail_paths[item.id] = await _download_media_thumbnail(app, item, item_type)

        sent_group_success = False
        bot_size_limit_hit = False
        for _ in range(3):
            if sync_state["stop_requested"]:
                break
            try:
                group_items = _clone_media_group_items(downloaded_files, mode)
                send_kwargs = _build_clone_media_group_send_kwargs(
                    actual_sender,
                    chat_id,
                    group_items,
                    rewritten_captions,
                    thumbnail_paths,
                    spoiler_flags,
                    upload_target["label"],
                    sum(file_sizes),
                    reply_to_id,
                    quote_data,
                    len(downloaded_files),
                    client,
                )
                sent_msgs = await _execute_with_clone_retry_interruptibly(
                    lambda: client.send_media_group(**send_kwargs),
                    action_label=f"媒体组 {group[0].id}",
                    stop_client=client if actual_sender != "bot" else None,
                )
                for orig_m, new_m in zip(group, sent_msgs):
                    await record_success(source_id, target_id, orig_m.id, new_m.message_id if actual_sender == "bot" else new_m.id, force_send=force_send)
                if actual_sender == "bot":
                    await bot_engine.note_upload_success(client, sum(file_sizes))
                sent_group_success = True
                if captions_changed:
                    await db.add_msg_log("CLONE_GROUP_CAPTION_REWRITE", f"原始:[{source_id}] 组首ID:{group[0].id} | 命中 {caption_rewrite_count} 个 caption 链接改写")
                if quote_data and reply_to_id:
                    await db.add_msg_log("CLONE_QUOTE_GROUP_SEND", f"原始:[{source_id}] 组首ID:{group[0].id} | 已按引用回复发送媒体组")
                await db.add_msg_log(
                    "CLONE_GROUP_SEND",
                    f"原始:[{source_id}] 组首ID:{group[0].id} | 目标:[{target_id}] 共 {len(group)} 项 | 同步成功",
                )
                result = SYNC_RESULT_SENT_MAPPED
                break
            except Exception as exc:
                if "STOP_REQUESTED" in str(exc):
                    raise
                if isinstance(exc, SyncNetworkRetryExhaustedError):
                    raise
                if actual_sender != "bot" and _is_topics_parse_error(exc):
                    await db.add_msg_log(
                        "CLONE_TOPICS_COMPAT",
                        f"原始:[{source_id}] 组首ID:{group[0].id} | 辅助账号发送后返回 topics 解析异常，已停止重试避免重复发送",
                    )
                    sent_group_success = True
                    await db.add_msg_log(
                        "CLONE_GROUP_SEND",
                        f"原始:[{source_id}] 组首ID:{group[0].id} | 目标:[{target_id}] 共 {len(group)} 项 | 可能已发送，回包解析失败，未记录映射",
                    )
                    count_unmapped_group()
                    result = SYNC_RESULT_SENT_UNMAPPED
                    break
                if actual_sender == "bot" and _is_request_entity_too_large(exc):
                    bot_size_limit_hit = True
                    break
                retry_after = _parse_retry_after_seconds(exc)
                if actual_sender == "bot" and retry_after is not None:
                    await bot_engine.mark_upload_bot_cooldown(client, retry_after + 1, f"CLONE 媒体组首ID:{group[0].id}")
                    upload_target = await safe_execute(
                        resolve_upload_target(
                            sender,
                            app,
                            file_sizes,
                            allow_user_fallback=should_fallback_to_user(sender, clone_fallback_to_user),
                            wait_for_available_bot=not should_fallback_to_user(sender, clone_fallback_to_user),
                        ),
                        sync_state,
                    )
                    actual_sender = upload_target["sender"]
                    client = upload_target["client"]
                    parse_mode = upload_target["parse_mode"]
                    if actual_sender == "user":
                        await db.add_msg_log("BOT_FALLBACK", f"原始:[{source_id}] 组首ID:{group[0].id} | Bot 频控，已切换辅助账号继续发送")
                    continue
                if actual_sender == "bot" and bot_engine.should_disable_upload_bot_for_error(exc):
                    await bot_engine.disable_upload_bot(client, str(exc))
                    upload_target = await safe_execute(
                        resolve_upload_target(
                            sender,
                            app,
                            file_sizes,
                            allow_user_fallback=should_fallback_to_user(sender, clone_fallback_to_user),
                            wait_for_available_bot=not should_fallback_to_user(sender, clone_fallback_to_user),
                        ),
                        sync_state,
                    )
                    actual_sender = upload_target["sender"]
                    client = upload_target["client"]
                    parse_mode = upload_target["parse_mode"]
                    if actual_sender == "user":
                        await db.add_msg_log("BOT_FALLBACK", f"原始:[{source_id}] 组首ID:{group[0].id} | 当前 Bot 已失效，已切换辅助账号继续发送")
                    continue
                await asyncio.sleep(2)

        if not sync_state["stop_requested"] and should_fallback_to_user(sender, clone_fallback_to_user) and actual_sender == "bot" and not sent_group_success:
            first_id = group[0].id if group else 0
            fallback_reason = "Bot 上传体积超限，改用辅助账号重传" if bot_size_limit_hit else f"{upload_target['label']} 上传失败，改用辅助账号重传"
            await db.add_msg_log("BOT_FALLBACK", f"原始:[{source_id}] 组首ID:{first_id} | {fallback_reason}")
            tracker = UploadProgressTracker("上传媒体组 [改用辅助账号]", sum(file_sizes))
            group_items = _clone_media_group_items(downloaded_files, mode)
            media_list = build_user_media_group(group_items, rewritten_captions, thumbnail_paths, spoiler_flags)
            send_kwargs = {"chat_id": chat_id, "media": media_list}
            if reply_to_id:
                send_kwargs["reply_to_message_id"] = reply_to_id
            if quote_data and reply_to_id:
                send_kwargs["quote_text"] = quote_data["text"]
                if quote_data.get("entities"):
                    send_kwargs["quote_entities"] = quote_data["entities"]
            send_kwargs["progress"] = build_pyro_progress_callback(
                tracker,
                f"上传媒体组: {len(downloaded_files)} 项",
                total_bytes=sum(file_sizes),
                client=app,
            )
            try:
                sent_msgs = await _execute_with_clone_retry_interruptibly(
                    lambda: app.send_media_group(**send_kwargs),
                    action_label=f"媒体组辅助回退 {first_id}",
                    stop_client=app,
                )
                for orig_m, new_m in zip(group, sent_msgs):
                    await record_success(source_id, target_id, orig_m.id, new_m.id, force_send=force_send)
                await db.add_msg_log(
                    "CLONE_GROUP_SEND",
                    f"原始:[{source_id}] 组首ID:{first_id} | 目标:[{target_id}] 共 {len(group)} 项 | 已改用辅助账号发送成功",
                )
                sent_group_success = True
                result = SYNC_RESULT_SENT_MAPPED
            except Exception as exc:
                if _is_topics_parse_error(exc):
                    await db.add_msg_log(
                        "CLONE_TOPICS_COMPAT",
                        f"原始:[{source_id}] 组首ID:{first_id} | 改用辅助账号发送后返回 topics 解析异常，已停止重试避免重复发送",
                    )
                    await db.add_msg_log(
                        "CLONE_GROUP_SEND",
                        f"原始:[{source_id}] 组首ID:{first_id} | 目标:[{target_id}] 共 {len(group)} 项 | 可能已发送，回包解析失败，未记录映射",
                    )
                    sent_group_success = True
                    count_unmapped_group()
                    result = SYNC_RESULT_SENT_UNMAPPED
                else:
                    raise

        for _, path in downloaded_files:
            try:
                os.remove(path)
            except Exception:
                pass
        for thumbnail_path in thumbnail_paths.values():
            if thumbnail_path:
                try:
                    os.remove(thumbnail_path)
                except Exception:
                    pass

    await asyncio.sleep(safe_delay)
    return result


def group_messages(messages):
    grouped_msgs = []
    current_group = []
    for msg in messages:
        if msg.media_group_id:
            if not current_group or current_group[0].media_group_id == msg.media_group_id:
                current_group.append(msg)
            else:
                grouped_msgs.append(current_group)
                current_group = [msg]
        else:
            if current_group:
                grouped_msgs.append(current_group)
                current_group = []
            grouped_msgs.append([msg])
    if current_group:
        grouped_msgs.append(current_group)
    return grouped_msgs


async def process_master_sync(
    mode: str,
    sender: str,
    source_id_raw: str,
    target_id_raw: str,
    delay: float,
    start_id: int,
    end_id: int,
    json_path: str,
    force_send: bool = False,
    json_source_username: str = "",
    json_media_group_window_seconds: int = 3,
    hash_perturb: bool = False,
    clone_fallback_to_user: bool = True,
    target_type: str = "channel",
):
    outcome = SyncResult()
    error = ""
    stopped = False
    try:
        await _run_master_sync(
            mode, sender, source_id_raw, target_id_raw, delay, start_id, end_id,
            json_path, force_send, json_source_username, json_media_group_window_seconds,
            hash_perturb, clone_fallback_to_user, target_type, outcome,
        )
    except asyncio.CancelledError:
        stopped = True
    except Exception as exc:
        error = str(exc) or type(exc).__name__
    finally:
        result = outcome.snapshot(error=error, stopped=stopped or bool(sync_state.get("stop_requested")))
        sync_state["result"] = result
        try:
            summary = (
                f"{result['label']}：{mode.upper()} | 成功 {outcome.sent} | 跳过 {outcome.skipped} | "
                f"失败 {outcome.failed} | 待核对 {outcome.unmapped} | 读取失败批次 {outcome.failed_batches}"
            )
            if error:
                summary += f" | {error}"
            await db.add_log("INFO" if result["status"] in {"completed", "stopped"} else "ERROR", summary)
        finally:
            finish_sync_session()


async def _run_master_sync(
    mode, sender, source_id_raw, target_id_raw, delay, start_id, end_id,
    json_path, force_send, json_source_username, json_media_group_window_seconds,
    hash_perturb, clone_fallback_to_user, target_type, outcome,
):
    safe_delay = max(0.5, float(delay))
    if mode == "api":
        sender = "user"

    start_sync_session(
        mode,
        sender,
        source_id_raw,
        target_id_raw,
        safe_delay,
        start_id,
        end_id,
        json_path,
        force_send,
        json_source_username,
        hash_perturb,
        clone_fallback_to_user,
    )
    settings = await db.get_all_settings()
    sync_config = get_config().get("sync", {})
    include_external_source_header = bool(sync_config.get("add_external_source_header", False))
    if not include_external_source_header:
        legacy_value = str(getattr(settings, "get", lambda *_: "")("add_external_source_header", "") or "").strip().lower()
        include_external_source_header = legacy_value in {"1", "true", "yes", "on"}

    try:
        source_id = 0 if mode == "json" else await resolve_chat_id(bot_engine.aiogram_bot, source_id_raw)
        if str(target_type or "").strip() == "saved":
            # 收藏夹目标：跳过 resolve_chat_id（sentinel 不是真实 Telegram id），
            # 发送 chat_id 即时解析为当前辅助账号 own id。
            target_id = SAVED_MESSAGES_TARGET_ID
            chat_id = await resolve_destination_chat_id("saved", target_id, bot_engine.pyro_user_app)
        else:
            target_id = await resolve_chat_id(bot_engine.aiogram_bot, target_id_raw)
            chat_id = None
    except Exception as exc:
        raise RuntimeError(format_channel_check_error(exc)) from exc

    if mode == "clone":
        await clear_temp_dir_files()
        await db.add_log("INFO", "已清空 temp，准备下载")

    try:
        if mode in ["api", "clone"]:
            app, bot = bot_engine.pyro_user_app, bot_engine.aiogram_bot
            if not start_id:
                start_id = 1
            if not end_id:
                end_id = await _fetch_last_message_id(app, source_id)
            if not end_id:
                end_id = 1
            sync_state["total"] = end_id - start_id + 1

            for chunk_start in range(start_id, end_id + 1, 100):
                if sync_state["stop_requested"]:
                    break
                try:
                    msgs = await _safe_get_messages(source_id=source_id, app=app, msg_ids=list(range(chunk_start, min(chunk_start + 99, end_id) + 1)))
                except SyncNetworkRetryExhaustedError:
                    raise
                except Exception as exc:
                    if sync_state.get("stop_requested"):
                        break
                    outcome.failed_batches += 1
                    await log_sync_error(f"读取消息失败 {chunk_start}-{min(chunk_start + 99, end_id)}", exc)
                    continue

                filtered_msgs = []
                for msg in msgs:
                    if msg is None or msg.empty:
                        continue
                    msg_type, sync_key = get_msg_meta(msg, mode)
                    if settings.get(sync_key, "1") == "0":
                        outcome.record("skipped")
                        continue
                    filtered_msgs.append(msg)

                for group in group_messages(filtered_msgs):
                    if sync_state["stop_requested"]:
                        break
                    if len(group) == 1:
                        result = await sync_single_message(
                            mode,
                            sender,
                            app,
                            bot,
                            source_id,
                            target_id,
                            group[0],
                            safe_delay,
                            force_send,
                            hash_perturb=hash_perturb,
                            clone_fallback_to_user=clone_fallback_to_user,
                            include_external_source_header=include_external_source_header,
                            chat_id=chat_id,
                        )
                    else:
                        result = await sync_media_group(
                            mode,
                            sender,
                            app,
                            bot,
                            source_id,
                            target_id,
                            group,
                            safe_delay,
                            force_send,
                            hash_perturb=hash_perturb,
                            clone_fallback_to_user=clone_fallback_to_user,
                            include_external_source_header=include_external_source_header,
                            chat_id=chat_id,
                        )
                    if result != SYNC_RESULT_FAILED or not sync_state.get("stop_requested"):
                        outcome.record(result, len(group))
        else:
            await process_json_sync(
                sender,
                target_id_raw,
                json_path,
                safe_delay,
                force_send,
                json_source_username=json_source_username,
                media_group_window_seconds=json_media_group_window_seconds,
                clone_fallback_to_user=clone_fallback_to_user,
                hash_perturb=hash_perturb,
                target_type=target_type,
                outcome=outcome,
            )

    except asyncio.CancelledError:
        raise
