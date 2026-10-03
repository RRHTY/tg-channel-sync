from __future__ import annotations

import asyncio
import os
import sqlite3
from collections.abc import Awaitable, Callable

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


class SyncDeliveryPendingError(db.MessageDeliveryPendingError):
    pass


class SyncMappingPersistenceError(RuntimeError):
    def __init__(self, source_id, target_id, msg_id, target_msg_id):
        self.source_id = source_id
        self.target_id = target_id
        self.msg_id = msg_id
        self.target_msg_id = target_msg_id
        super().__init__(f"消息已发送，但映射保存失败: 源 {source_id}/{msg_id}，目标 {target_id}/{target_msg_id}")


class DeliveryClient:
    def __init__(self, client, guard):
        self._client = client
        self._guard = guard

    def __getattr__(self, name):
        method = getattr(self._client, name)
        if name == "invoke":
            async def invoke(*args, **kwargs):
                query = args[0] if args else kwargs.get("query")
                if type(query).__name__.startswith("Send"):
                    return await self._guard.send(lambda: method(*args, **kwargs))
                return await method(*args, **kwargs)

            return invoke
        if name.startswith(("send_", "copy_")) and name not in {"send_code", "send_recovery_code", "send_chat_action"}:
            async def send(*args, **kwargs):
                return await self._guard.send(lambda: method(*args, **kwargs))

            return send
        return method


class DeliveryGuard:
    def __init__(self, source_id, target_id, msg_ids, force_send=False, owner_user_id=None):
        self.source_id = source_id
        self.target_id = target_id
        self.msg_ids = list(dict.fromkeys(int(msg_id) for msg_id in msg_ids))
        self.force_send = force_send
        self.owner_user_id = db.get_mapping_owner_id(target_id) if owner_user_id is None else int(owner_user_id)
        self._prepared = False
        self._has_result = False
        self._result = None
        self._unconfirmed = False

    def wrap_client(self, client):
        if isinstance(client, DeliveryClient):
            if client._guard is self:
                return client
            client = client._client
        return DeliveryClient(client, self)

    async def send(self, action: Callable[[], Awaitable[object]]):
        if self._unconfirmed:
            raise SyncDeliveryPendingError(self.source_id, self.target_id, self.msg_ids)
        if self._has_result:
            return self._result
        if not self._prepared:
            try:
                await db.prepare_message_delivery(
                    self.source_id, self.target_id, self.msg_ids,
                    force=self.force_send, owner_user_id=self.owner_user_id,
                )
            except db.MessageDeliveryPendingError as exc:
                raise SyncDeliveryPendingError(exc.source_id, exc.target_id, exc.msg_ids) from exc
            self._prepared = True
        self._result = await action()
        self._has_result = True
        return self._result

    async def mark_unconfirmed(self, reason: str) -> None:
        # The same operation must remain blocked even if persistence fails or it began with force.
        self._unconfirmed = True
        self._prepared = False
        self._has_result = False
        self._result = None
        await db.mark_message_delivery_unconfirmed(
            self.source_id, self.target_id, self.msg_ids, reason, owner_user_id=self.owner_user_id,
        )

    async def release(self) -> None:
        await db.release_message_delivery(
            self.source_id, self.target_id, self.msg_ids, owner_user_id=self.owner_user_id,
        )
        self._unconfirmed = False
        self._prepared = False
        self._has_result = False
        self._result = None


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


async def update_state_and_check_skip(source_id, target_id, msg_id, text, force_send=False, owner_user_id=None):
    sync_state["current"] += 1
    sync_state["current_link"] = f"t.me/c/{str(source_id).replace('-100', '')}/{msg_id}" if source_id else ""
    sync_state["current_text"] = text
    if not force_send:
        if await db.get_message_delivery(source_id, msg_id, target_id, owner_user_id=owner_user_id):
            raise SyncDeliveryPendingError(source_id, target_id, [msg_id])
        if await db.is_message_synced(source_id, msg_id, target_id):
            sync_state["skipped"] += 1
            mode_label = sync_state.get("mode", "SYNC") or "SYNC"
            source_label = source_id if source_id else "JSON"
            await db.add_msg_log(f"{mode_label}_SKIP_DUP", f"源:[{source_label}] 消息ID:{msg_id} | 已命中重复检查，跳过发送")
            return True
    return False


async def record_success(source_id, target_id, msg_id, target_msg_id, force_send=False, owner_user_id=None):
    owner_id = db.get_mapping_owner_id(target_id) if owner_user_id is None else int(owner_user_id)
    for attempt in range(3):
        try:
            await db.save_msg_mapping(
                source_id, msg_id, target_id, target_msg_id,
                overwrite=force_send, owner_user_id=owner_id,
            )
            return
        except sqlite3.Error as exc:
            if attempt == 2:
                raise SyncMappingPersistenceError(source_id, target_id, msg_id, target_msg_id) from exc
            await asyncio.sleep(0.1 * (attempt + 1))


def count_unmapped_group() -> None:
    """累计"已发送但拿不到新消息 ID"的媒体组，供任务结束汇总提示重复发送风险。"""
    sync_state["unmapped"] = int(sync_state.get("unmapped", 0) or 0) + 1


async def clear_temp_dir_files() -> None:
    for name in os.listdir(TEMP_DIR):
        try:
            os.remove(os.path.join(TEMP_DIR, name))
        except Exception:
            pass
