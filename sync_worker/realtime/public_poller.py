from __future__ import annotations

import asyncio

import database as db
from app_config import get_config
from services.sync_services import (
    begin_saved_messages_operation,
    finish_saved_messages_operation,
    resolve_destination_chat_id,
)

PROCESSED_SYNC_RESULTS = {"sent_mapped", "sent_unmapped", "skipped"}


def public_channel_peer(source_ref: str):
    clean_ref = str(source_ref or "").strip()
    if clean_ref.lstrip("-").isdigit():
        return int(clean_ref)
    return f"@{clean_ref.lstrip('@')}"


def public_channel_poll_interval() -> float:
    sync_cfg = get_config().get("sync", {})
    raw_value = sync_cfg.get("public_channel_poll_interval_seconds", sync_cfg.get("default_delay", 15))
    try:
        return max(5.0, float(raw_value or 15))
    except (TypeError, ValueError):
        return 15.0


async def get_public_channel_last_message_id(user_app, source_ref: str) -> int:
    if not getattr(user_app, "is_initialized", False):
        return 0
    async for message in user_app.get_chat_history(public_channel_peer(source_ref), limit=1):
        return int(getattr(message, "id", 0) or 0)
    return 0


async def load_public_channel_new_messages(user_app, source_ref: str, last_message_id: int):
    messages = []
    # Pyrogram paginates this iterator. Stop at our checkpoint, not at the
    # newest page: otherwise advancing the cursor would discard older backlog.
    async for message in user_app.get_chat_history(public_channel_peer(source_ref), limit=0):
        msg_id = int(getattr(message, "id", 0) or 0)
        if msg_id <= last_message_id:
            break
        messages.append(message)
    messages.sort(key=lambda item: int(getattr(item, "id", 0) or 0))
    return messages


async def _process_public_target(
    bot,
    user_app,
    source_id: int,
    target_mapping: dict,
    message_group: list,
    include_external_source_header: bool,
    source_username_override: str,
):
    from sync_worker.clone.process import sync_media_group, sync_single_message

    target_id = int(target_mapping["target_id"])
    target_type = str(target_mapping.get("target_type", "") or "channel")
    saved_operation = await begin_saved_messages_operation(target_type, target_id)
    try:
        # 收藏夹目标：chat_id 为当前辅助账号 own id（即时解析），target_id 仍是 DB 键（sentinel）。
        chat_id = await resolve_destination_chat_id(target_type, target_id, user_app)
        common_kwargs = {
            "hash_perturb": bool(target_mapping.get("realtime_hash_perturb", False)),
            "clone_fallback_to_user": bool(target_mapping.get("realtime_fallback_to_user", True)),
            "include_external_source_header": include_external_source_header,
            "source_username_override": source_username_override,
        }
        if len(message_group) == 1:
            return await sync_single_message(
                "api", "user", user_app, bot, source_id, target_id, message_group[0], 0.5, False, **common_kwargs,
                chat_id=chat_id,
            )
        return await sync_media_group(
            "api", "user", user_app, bot, source_id, target_id, message_group, 0.5, False, **common_kwargs,
            chat_id=chat_id,
        )
    finally:
        await finish_saved_messages_operation(saved_operation)


async def process_public_channel_mapping_group(bot, user_app, group: dict):
    from sync_worker.clone.process import group_messages
    from sync_worker.runtime import sync_state

    if sync_state.get("is_syncing"):
        return
    source_id = int(group["source_id"])
    source_ref = str(group["source_ref"] or "").lstrip("@")
    last_message_id = int(group.get("last_polled_message_id", 0) or 0)
    messages = await load_public_channel_new_messages(user_app, source_ref, last_message_id)
    if not messages:
        return

    sync_cfg = get_config().get("sync", {})
    include_external_source_header = bool(sync_cfg.get("add_external_source_header", False))
    source_username_override = "" if source_ref.lstrip("-").isdigit() else source_ref
    previous_mode = sync_state.get("mode", "")
    sync_state["mode"] = "PUBLIC"
    completed_max_seen_id = last_message_id
    try:
        for message_group in group_messages(messages):
            if sync_state.get("is_syncing") or not message_group:
                return
            group_max_id = max(int(getattr(item, "id", 0) or 0) for item in message_group)
            for target_mapping in group["mappings"]:
                target_id = int(target_mapping["target_id"])
                result = await _process_public_target(
                    bot,
                    user_app,
                    source_id,
                    target_mapping,
                    message_group,
                    include_external_source_header,
                    source_username_override,
                )
                if result not in PROCESSED_SYNC_RESULTS:
                    raise RuntimeError(
                        f"公开频道消息处理未完成: source={source_id} target={target_id} group_max_id={group_max_id} result={result}"
                    )
            completed_max_seen_id = max(completed_max_seen_id, group_max_id)
        if completed_max_seen_id > last_message_id:
            await db.update_public_user_poll_position(source_id, source_ref, completed_max_seen_id)
    finally:
        sync_state["mode"] = previous_mode


async def poll_public_user_channel_mappings(bot_factory, user_factory):
    while True:
        interval = public_channel_poll_interval()
        try:
            user_app = user_factory()
            if not getattr(user_app, "is_initialized", False):
                await asyncio.sleep(interval)
                continue
            for group in await db.get_public_user_mapping_groups():
                try:
                    await process_public_channel_mapping_group(bot_factory(), user_app, group)
                except Exception as exc:
                    await db.add_sys_log("WARNING", f"公开频道轮询失败 @{group.get('source_ref', '')}: {exc}")
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await db.add_sys_log("WARNING", f"公开频道轮询任务异常: {exc}")
            await asyncio.sleep(interval)
