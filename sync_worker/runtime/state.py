from __future__ import annotations

import os

import database as db
from app_paths import temp_dir


TEMP_DIR = str(temp_dir())
sync_state = {
    "is_syncing": False,
    "mode": "",
    "total": 0,
    "current": 0,
    "current_text": "",
    "current_link": "",
    "skipped": 0,
    "unmapped": 0,
    "stop_requested": False,
    "source_id_raw": "",
    "target_id_raw": "",
    "delay": 5,
    "start_id": "",
    "end_id": "",
    "json_path": "",
    "json_source_username": "",
    "sender": "bot",
    "force_send": False,
    "hash_perturb": False,
    "clone_fallback_to_user": True,
}


def start_sync_session(
    mode: str,
    sender: str,
    source_id_raw: str,
    target_id_raw: str,
    delay: float,
    start_id: int,
    end_id: int,
    json_path: str,
    force_send: bool,
    json_source_username: str,
    hash_perturb: bool = False,
    clone_fallback_to_user: bool = True,
) -> None:
    sync_state.update(
        {
            "is_syncing": True,
            "mode": mode.upper(),
            "source_id_raw": source_id_raw,
            "target_id_raw": target_id_raw,
            "delay": delay,
            "start_id": start_id,
            "end_id": end_id,
            "json_path": json_path,
            "json_source_username": json_source_username,
            "sender": sender,
            "current": 0,
            "skipped": 0,
            "unmapped": 0,
            "total": 0,
            "stop_requested": False,
            "result": None,
            "force_send": force_send,
            "hash_perturb": hash_perturb,
            "clone_fallback_to_user": clone_fallback_to_user,
        }
    )


def finish_sync_session() -> None:
    sync_state["is_syncing"] = False
    sync_state["stop_requested"] = False


async def update_state_and_check_skip(source_id, target_id, msg_id, text, force_send=False):
    sync_state["current"] += 1
    sync_state["current_link"] = f"t.me/c/{str(source_id).replace('-100', '')}/{msg_id}" if source_id else ""
    sync_state["current_text"] = text
    if not force_send and await db.is_message_synced(source_id, msg_id, target_id):
        sync_state["skipped"] += 1
        mode_label = sync_state.get("mode", "SYNC") or "SYNC"
        source_label = source_id if source_id else "JSON"
        await db.add_msg_log(f"{mode_label}_SKIP_DUP", f"源:[{source_label}] 消息ID:{msg_id} | 已命中重复检查，跳过发送")
        return True
    return False


async def record_success(source_id, target_id, msg_id, target_msg_id, force_send=False):
    await db.save_msg_mapping(source_id, msg_id, target_id, target_msg_id, overwrite=force_send)


def count_unmapped_group() -> None:
    """累计"已发送但拿不到新消息 ID"的媒体组，供任务结束汇总提示重复发送风险。"""
    sync_state["unmapped"] = int(sync_state.get("unmapped", 0) or 0) + 1


async def clear_temp_dir_files() -> None:
    for name in os.listdir(TEMP_DIR):
        try:
            os.remove(os.path.join(TEMP_DIR, name))
        except Exception:
            pass
