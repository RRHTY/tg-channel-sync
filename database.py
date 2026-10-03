from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import closing

import aiosqlite

from app_config import get_config
from app_paths import database_file, ensure_runtime_dirs, pyrogram_user_session_base
from services.filter_rules import (
    validate_filter_rule,
    compile_filter_regex as _compile_filter_regex,
    apply_filter_rule as _apply_filter_rule,
)


DB_FILE = str(database_file())
LOG_RETENTION_LIMIT = 100
_db_conn: aiosqlite.Connection | None = None
_db_lock: asyncio.Lock | None = None
_db_loop: asyncio.AbstractEventLoop | None = None
_saved_message_account_id = 0


class MessageDeliveryPendingError(RuntimeError):
    def __init__(self, source_id: int, target_id: int, msg_ids: list[int]):
        self.source_id = source_id
        self.target_id = target_id
        self.msg_ids = msg_ids
        super().__init__(f"消息发送结果待确认: 源 {source_id}，目标 {target_id}，消息 {msg_ids}")


def get_mapping_owner_id(target_channel_id: int) -> int:
    return _saved_message_account_id if target_channel_id == -1 else 0


def clear_saved_message_account() -> None:
    global _saved_message_account_id
    _saved_message_account_id = 0


def _delivery_owner_id(target_channel_id: int, owner_user_id: int | None) -> int:
    if target_channel_id != -1:
        return 0
    return get_mapping_owner_id(target_channel_id) if owner_user_id is None else int(owner_user_id)


def _require_saved_owner(owner_id: int) -> int:
    if owner_id <= 0:
        raise ValueError("收藏夹目标需要先完成辅助账号登录并验证账号")
    return owner_id


async def _ensure_db_context() -> asyncio.Lock:
    global _db_lock, _db_loop, _db_conn
    current_loop = asyncio.get_running_loop()
    if _db_lock is None:
        _db_lock = asyncio.Lock()
        _db_loop = current_loop
        return _db_lock
    if _db_loop is not current_loop:
        old_conn = _db_conn
        _db_conn = None
        _db_lock = asyncio.Lock()
        _db_loop = current_loop
        if old_conn is not None:
            await old_conn.close()
    return _db_lock


async def _configure_connection(conn: aiosqlite.Connection) -> aiosqlite.Connection:
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA busy_timeout=5000")
    await conn.execute("PRAGMA synchronous=NORMAL")
    return conn


async def _get_connection() -> aiosqlite.Connection:
    global _db_conn
    ensure_runtime_dirs()
    async with await _ensure_db_context():
        if _db_conn is None:
            _db_conn = await aiosqlite.connect(DB_FILE)
            await _configure_connection(_db_conn)
    return _db_conn


async def close_db() -> None:
    global _db_conn
    async with await _ensure_db_context():
        if _db_conn is not None:
            await _db_conn.close()
            _db_conn = None


async def _run_in_db(
    action: Callable[[aiosqlite.Connection], Awaitable[None | object]],
    *,
    commit: bool = False,
):
    conn = await _get_connection()
    async with await _ensure_db_context():
        try:
            result = await action(conn)
            if commit:
                await conn.commit()
            return result
        except BaseException:
            if commit:
                await conn.rollback()
            raise


async def _fetchall(sql: str, params: tuple = ()) -> list:
    async def action(conn: aiosqlite.Connection):
        cursor = await conn.execute(sql, params)
        return await cursor.fetchall()

    return await _run_in_db(action)


async def _fetchone(sql: str, params: tuple = ()):
    async def action(conn: aiosqlite.Connection):
        cursor = await conn.execute(sql, params)
        return await cursor.fetchone()

    return await _run_in_db(action)


async def _execute(sql: str, params: tuple = (), *, commit: bool = False):
    async def action(conn: aiosqlite.Connection):
        await conn.execute(sql, params)

    return await _run_in_db(action, commit=commit)


async def _executemany(sql: str, rows: list[tuple], *, commit: bool = False):
    async def action(conn: aiosqlite.Connection):
        await conn.executemany(sql, rows)

    return await _run_in_db(action, commit=commit)


async def _migrate_channel_mappings(conn: aiosqlite.Connection) -> None:
    old_channel_rows = []
    old_channel_sql_row = await (
        await conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'channel_mappings'"
        )
    ).fetchone()
    if old_channel_sql_row and "source_id INTEGER NOT NULL UNIQUE" in (old_channel_sql_row[0] or ""):
        old_channel_rows = await (await conn.execute("SELECT source_id, target_id FROM channel_mappings")).fetchall()
        await conn.execute("ALTER TABLE channel_mappings RENAME TO channel_mappings_old")

    await conn.execute(
        "CREATE TABLE IF NOT EXISTS channel_mappings ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "source_id INTEGER NOT NULL, "
        "target_id INTEGER NOT NULL, "
        "UNIQUE(source_id, target_id))"
    )
    if old_channel_rows:
        await conn.executemany(
            "INSERT OR IGNORE INTO channel_mappings (source_id, target_id) VALUES (?, ?)",
            old_channel_rows,
        )
        await conn.execute("DROP TABLE channel_mappings_old")
    for column_name, column_sql in (
        ("realtime_sender", "ALTER TABLE channel_mappings ADD COLUMN realtime_sender TEXT NOT NULL DEFAULT 'bot'"),
        (
            "realtime_fallback_to_user",
            "ALTER TABLE channel_mappings ADD COLUMN realtime_fallback_to_user INTEGER NOT NULL DEFAULT 1",
        ),
        (
            "realtime_hash_perturb",
            "ALTER TABLE channel_mappings ADD COLUMN realtime_hash_perturb INTEGER NOT NULL DEFAULT 0",
        ),
        ("source_mode", "ALTER TABLE channel_mappings ADD COLUMN source_mode TEXT NOT NULL DEFAULT 'bot'"),
        ("source_ref", "ALTER TABLE channel_mappings ADD COLUMN source_ref TEXT NOT NULL DEFAULT ''"),
        (
            "last_polled_message_id",
            "ALTER TABLE channel_mappings ADD COLUMN last_polled_message_id INTEGER NOT NULL DEFAULT 0",
        ),
        # 目标类型判别列："channel"(默认/既有，普通频道/群组) | "saved"(收藏夹/Saved Messages)。
        # saved 目标在 target_id 中存 SAVED_MESSAGES_TARGET_ID(-1) 占位值，避免存真实 own id 产生 stale-id。
        ("target_type", "ALTER TABLE channel_mappings ADD COLUMN target_type TEXT NOT NULL DEFAULT 'channel'"),
        ("enabled", "ALTER TABLE channel_mappings ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"),
        ("source_title", "ALTER TABLE channel_mappings ADD COLUMN source_title TEXT NOT NULL DEFAULT ''"),
        ("target_title", "ALTER TABLE channel_mappings ADD COLUMN target_title TEXT NOT NULL DEFAULT ''"),
    ):
        try:
            await conn.execute(column_sql)
        except Exception:
            pass


async def _migrate_message_mappings(conn: aiosqlite.Connection) -> None:
    current_channel_rows = await (await conn.execute("SELECT source_id, target_id FROM channel_mappings")).fetchall()
    old_channel_map = {row[0]: row[1] for row in current_channel_rows}
    old_message_rows = []
    old_message_sql_row = await (
        await conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'message_mappings'"
        )
    ).fetchone()
    if old_message_sql_row and "target_channel_id" not in (old_message_sql_row[0] or ""):
        old_message_rows = await (
            await conn.execute("SELECT source_channel_id, source_msg_id, target_msg_id FROM message_mappings")
        ).fetchall()
        await conn.execute("ALTER TABLE message_mappings RENAME TO message_mappings_old")

    await conn.execute(
        "CREATE TABLE IF NOT EXISTS message_mappings ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "source_channel_id INTEGER NOT NULL, "
        "source_msg_id INTEGER NOT NULL, "
        "target_channel_id INTEGER NOT NULL, "
        "target_msg_id INTEGER NOT NULL, "
        "UNIQUE(source_channel_id, source_msg_id, target_channel_id))"
    )
    if old_message_rows:
        migrated_rows = [
            (source_channel_id, source_msg_id, old_channel_map[source_channel_id], target_msg_id)
            for source_channel_id, source_msg_id, target_msg_id in old_message_rows
            if source_channel_id in old_channel_map
        ]
        if migrated_rows:
            await conn.executemany(
                "INSERT OR IGNORE INTO message_mappings "
                "(source_channel_id, source_msg_id, target_channel_id, target_msg_id) VALUES (?, ?, ?, ?)",
                migrated_rows,
            )
        await conn.execute("DROP TABLE message_mappings_old")


async def _ensure_supporting_tables(conn: aiosqlite.Connection) -> None:
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS saved_message_mappings ("
        "owner_user_id INTEGER NOT NULL CHECK(owner_user_id > 0), "
        "source_channel_id INTEGER NOT NULL, source_msg_id INTEGER NOT NULL, "
        "target_msg_id INTEGER NOT NULL, "
        "PRIMARY KEY(owner_user_id, source_channel_id, source_msg_id))"
    )
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS saved_poll_positions ("
        "owner_user_id INTEGER NOT NULL CHECK(owner_user_id > 0), "
        "source_id INTEGER NOT NULL, source_ref TEXT NOT NULL, "
        "last_polled_message_id INTEGER NOT NULL DEFAULT 0, "
        "PRIMARY KEY(owner_user_id, source_id, source_ref))"
    )
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS message_sync_receipts ("
        "source_channel_id INTEGER NOT NULL, "
        "source_msg_id INTEGER NOT NULL, "
        "target_channel_id INTEGER NOT NULL, "
        "owner_user_id INTEGER NOT NULL DEFAULT 0, "
        "status TEXT NOT NULL CHECK(status IN ('sending', 'unconfirmed')), "
        "reason TEXT NOT NULL DEFAULT '', "
        "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
        "PRIMARY KEY(source_channel_id, source_msg_id, target_channel_id, owner_user_id))"
    )
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS system_logs ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "level TEXT NOT NULL, "
        "message TEXT NOT NULL, "
        "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS filter_rules ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "rule_type TEXT NOT NULL, "
        "pattern TEXT NOT NULL, "
        "replacement TEXT, "
        "is_case_sensitive INTEGER DEFAULT 0)"
    )
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS message_logs ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "action TEXT NOT NULL, "
        "detail TEXT NOT NULL, "
        "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS global_settings ("
        "setting_key TEXT PRIMARY KEY, "
        "setting_value TEXT NOT NULL)"
    )
    try:
        await conn.execute("ALTER TABLE filter_rules ADD COLUMN is_case_sensitive INTEGER DEFAULT 0")
    except Exception:
        pass
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_channel_source ON channel_mappings(source_id)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_channel_target ON channel_mappings(target_id)")
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_source_msg_target "
        "ON message_mappings(source_channel_id, source_msg_id, target_channel_id)"
    )


async def _seed_default_settings(conn: aiosqlite.Connection) -> None:
    default_settings = {
        "sync_text": "1",
        "sync_photo": "1",
        "sync_video": "1",
        "sync_document": "1",
        "sync_sticker": "1",
        "sync_gif": "1",
        "sync_audio": "1",
        "sync_voice": "1",
    }
    for key, value in default_settings.items():
        await conn.execute(
            "INSERT OR IGNORE INTO global_settings (setting_key, setting_value) VALUES (?, ?)",
            (key, value),
        )


def _read_legacy_saved_owner() -> int:
    session_path = pyrogram_user_session_base().with_suffix(".session")
    if not session_path.is_file():
        return 0
    try:
        with closing(sqlite3.connect(session_path.resolve().as_uri() + "?mode=ro", uri=True)) as session:
            rows = session.execute("SELECT user_id, is_bot FROM sessions LIMIT 2").fetchall()
        if len(rows) == 1 and rows[0][1] == 0 and type(rows[0][0]) is int and rows[0][0] > 0:
            return rows[0][0]
    except (sqlite3.Error, TypeError, ValueError, OSError):
        pass
    return 0


async def _freeze_legacy_saved_owner(conn: aiosqlite.Connection) -> bool:
    existing = await (await conn.execute(
        "SELECT setting_value FROM global_settings WHERE setting_key = 'saved_messages_legacy_owner_id'"
    )).fetchone()
    if existing:
        return False
    legacy = await (await conn.execute(
        "SELECT 1 FROM message_mappings WHERE target_channel_id = -1 "
        "UNION ALL SELECT 1 FROM channel_mappings WHERE target_id = -1 AND last_polled_message_id > 0 LIMIT 1"
    )).fetchone()
    owner_id = _read_legacy_saved_owner() if legacy else 0
    await conn.execute(
        "INSERT INTO global_settings (setting_key, setting_value) VALUES ('saved_messages_legacy_owner_id', ?)",
        (str(owner_id),),
    )
    return bool(legacy and owner_id == 0)


async def bind_saved_message_account(account_id: int) -> None:
    global _saved_message_account_id
    owner_id = _require_saved_owner(int(account_id))

    async def action(conn: aiosqlite.Connection):
        await conn.execute("BEGIN IMMEDIATE")
        frozen = await (await conn.execute(
            "SELECT setting_value FROM global_settings WHERE setting_key = 'saved_messages_legacy_owner_id'"
        )).fetchone()
        migrated = await (await conn.execute(
            "SELECT 1 FROM global_settings WHERE setting_key = 'saved_messages_legacy_migrated'"
        )).fetchone()
        if frozen and frozen[0] == str(owner_id) and not migrated:
            # Keep the original rows as an archive; only the verified original account receives a copy.
            await conn.execute(
                "INSERT OR IGNORE INTO saved_message_mappings "
                "(owner_user_id, source_channel_id, source_msg_id, target_msg_id) "
                "SELECT ?, source_channel_id, source_msg_id, target_msg_id FROM message_mappings WHERE target_channel_id = -1",
                (owner_id,),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO saved_poll_positions (owner_user_id, source_id, source_ref, last_polled_message_id) "
                "SELECT ?, source_id, source_ref, last_polled_message_id FROM channel_mappings "
                "WHERE target_id = -1 AND source_mode = 'public_user'",
                (owner_id,),
            )
            await conn.execute(
                "INSERT INTO global_settings (setting_key, setting_value) VALUES ('saved_messages_legacy_migrated', '1')"
            )

    await _run_in_db(action, commit=True)
    _saved_message_account_id = owner_id


async def init_db():
    warning = "旧收藏夹断点的账号归属无法确认，已隔离保留，当前登录账号不会自动继承。"
    async def action(conn: aiosqlite.Connection):
        await conn.execute("BEGIN IMMEDIATE")
        await _migrate_channel_mappings(conn)
        await _migrate_message_mappings(conn)
        await _ensure_supporting_tables(conn)
        await _seed_default_settings(conn)
        unknown_owner = await _freeze_legacy_saved_owner(conn)
        if unknown_owner:
            await conn.execute("INSERT INTO system_logs (level, message) VALUES ('WARNING', ?)", (warning,))
        return unknown_owner

    unknown_legacy_owner = await _run_in_db(action, commit=True)
    if unknown_legacy_owner:
        logging.getLogger("tg-channel-sync").warning(warning)


async def add_channel_mapping(
    source_id: int,
    target_id: int,
    realtime_sender: str = "bot",
    realtime_fallback_to_user: bool = True,
    realtime_hash_perturb: bool = False,
    source_mode: str = "bot",
    source_ref: str = "",
    last_polled_message_id: int = 0,
    target_type: str = "channel",
):
    realtime_sender = "user" if str(realtime_sender).strip() == "user" else "bot"
    source_mode = "public_user" if str(source_mode).strip() == "public_user" else "bot"
    source_ref = str(source_ref or "").strip().lstrip("@")
    target_type = "saved" if str(target_type or "").strip() == "saved" else "channel"
    saved_target = target_id == -1
    owner_id = _require_saved_owner(get_mapping_owner_id(-1)) if saved_target else 0
    cursor = max(0, int(last_polled_message_id or 0))
    sql = (
        "INSERT INTO channel_mappings "
        "(source_id, target_id, realtime_sender, realtime_fallback_to_user, realtime_hash_perturb, "
        "source_mode, source_ref, last_polled_message_id, target_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(source_id, target_id) DO UPDATE SET "
        "realtime_sender = excluded.realtime_sender, "
        "realtime_fallback_to_user = excluded.realtime_fallback_to_user, "
        "realtime_hash_perturb = excluded.realtime_hash_perturb, "
        "source_mode = excluded.source_mode, "
        "source_ref = excluded.source_ref, "
        "target_type = excluded.target_type"
    )
    params = (
        source_id,
        target_id,
        realtime_sender,
        1 if realtime_fallback_to_user else 0,
        1 if realtime_hash_perturb else 0,
        source_mode,
        source_ref,
        0 if saved_target else cursor,
        target_type,
    )

    async def action(conn: aiosqlite.Connection):
        existing = await (await conn.execute(
            "SELECT 1 FROM channel_mappings WHERE source_id = ? AND target_id = ?", (source_id, target_id)
        )).fetchone()
        await conn.execute(sql, params)
        if saved_target and source_mode == "public_user" and not existing:
            await conn.execute(
                "INSERT INTO saved_poll_positions (owner_user_id, source_id, source_ref, last_polled_message_id) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(owner_user_id, source_id, source_ref) "
                "DO UPDATE SET last_polled_message_id = excluded.last_polled_message_id",
                (owner_id, source_id, source_ref, cursor),
            )

    await _run_in_db(action, commit=True)


async def delete_channel_mapping(source_id: int, target_id: int | None = None):
    if target_id is None:
        await _execute("DELETE FROM channel_mappings WHERE source_id = ?", (source_id,), commit=True)
        return
    await _execute(
        "DELETE FROM channel_mappings WHERE source_id = ? AND target_id = ?",
        (source_id, target_id),
        commit=True,
    )


async def has_channel_mapping(source_id: int, target_id: int) -> bool:
    row = await _fetchone(
        "SELECT 1 FROM channel_mappings WHERE source_id = ? AND target_id = ?",
        (source_id, target_id),
    )
    return row is not None


async def is_channel_mapping_enabled(source_id: int, target_id: int) -> bool:
    return await _fetchone("SELECT 1 FROM channel_mappings WHERE source_id = ? AND target_id = ? AND enabled = 1", (source_id, target_id)) is not None


async def update_channel_mapping(source_id: int, target_id: int, **changes) -> None:
    allowed = {"enabled", "realtime_sender", "realtime_fallback_to_user", "realtime_hash_perturb", "source_title", "target_title"}
    fields = [(key, value) for key, value in changes.items() if key in allowed]
    if fields:
        await _execute("UPDATE channel_mappings SET " + ", ".join(f"{key} = ?" for key, _ in fields)
                       + " WHERE source_id = ? AND target_id = ?",
                       tuple(value for _, value in fields) + (source_id, target_id), commit=True)


async def would_create_channel_mapping_cycle(source_id: int, target_id: int) -> bool:
    if source_id == target_id:
        return True
    rows = await _fetchall("SELECT source_id, target_id FROM channel_mappings")
    adjacency: dict[int, set[int]] = {}
    for current_source, current_target in rows:
        adjacency.setdefault(current_source, set()).add(current_target)

    stack = [target_id]
    visited = set()
    while stack:
        current = stack.pop()
        if current == source_id:
            return True
        if current in visited:
            continue
        visited.add(current)
        stack.extend(adjacency.get(current, ()))
    return False


async def get_target_channels(source_id: int) -> list[int]:
    rows = await _fetchall("SELECT target_id FROM channel_mappings WHERE source_id = ? AND enabled = 1 ORDER BY target_id", (source_id,))
    return [row[0] for row in rows]


async def get_target_channel_mappings(source_id: int, source_mode: str | None = None) -> list[dict]:
    mode_clause = ""
    params: tuple = (source_id,)
    if source_mode:
        mode_clause = " AND source_mode = ?"
        params = (source_id, source_mode)
    rows = await _fetchall(
        "SELECT target_id, realtime_sender, realtime_fallback_to_user, realtime_hash_perturb, source_mode, source_ref, "
        "target_type "
        f"FROM channel_mappings WHERE source_id = ? AND enabled = 1{mode_clause} ORDER BY target_id",
        params,
    )
    return [
        {
            "target_id": row[0],
            "realtime_sender": row[1] or "bot",
            "realtime_fallback_to_user": bool(row[2]),
            "realtime_hash_perturb": bool(row[3]),
            "source_mode": row[4] or "bot",
            "source_ref": row[5] or "",
            "target_type": row[6] or "channel",
        }
        for row in rows
    ]


async def get_all_channel_mappings() -> list:
    return await _fetchall(
        "SELECT cm.source_id, cm.target_id, realtime_sender, realtime_fallback_to_user, realtime_hash_perturb, "
        "source_mode, cm.source_ref, CASE WHEN cm.target_id = -1 THEN COALESCE(sp.last_polled_message_id, 0) "
        "ELSE cm.last_polled_message_id END, target_type, enabled, source_title, target_title "
        "FROM channel_mappings cm LEFT JOIN saved_poll_positions sp "
        "ON cm.target_id = -1 AND sp.owner_user_id = ? AND sp.source_id = cm.source_id AND sp.source_ref = cm.source_ref "
        "ORDER BY cm.target_id, cm.source_id", (get_mapping_owner_id(-1),),
    )


async def get_public_user_mapping_groups() -> list[dict]:
    owner_id = get_mapping_owner_id(-1)
    rows = await _fetchall(
        "SELECT cm.source_id, cm.source_ref, target_id, realtime_sender, realtime_fallback_to_user, "
        "realtime_hash_perturb, CASE WHEN target_id = -1 THEN COALESCE(sp.last_polled_message_id, 0) "
        "ELSE cm.last_polled_message_id END, target_type "
        "FROM channel_mappings cm LEFT JOIN saved_poll_positions sp "
        "ON cm.target_id = -1 AND sp.owner_user_id = ? AND sp.source_id = cm.source_id AND sp.source_ref = cm.source_ref "
        "WHERE source_mode = 'public_user' AND cm.source_ref != '' AND enabled = 1 "
        "ORDER BY cm.source_id, target_id", (owner_id,),
    )
    groups: dict[tuple[int, str], dict] = {}
    for row in rows:
        source_id, source_ref = row[0], row[1] or ""
        group = groups.setdefault(
            (source_id, source_ref),
            {
                "source_id": source_id,
                "source_ref": source_ref,
                "last_polled_message_id": int(row[6] or 0),
                "mappings": [],
            },
        )
        group["last_polled_message_id"] = min(group["last_polled_message_id"], int(row[6] or 0))
        group["mappings"].append(
            {
                "target_id": row[2],
                "realtime_sender": row[3] or "user",
                "realtime_fallback_to_user": bool(row[4]),
                "realtime_hash_perturb": bool(row[5]),
                "source_mode": "public_user",
                "source_ref": source_ref,
                "target_type": row[7] or "channel",
                "last_polled_message_id": int(row[6] or 0),
                **({"owner_user_id": owner_id} if row[2] == -1 else {}),
            }
        )
    return list(groups.values())


async def update_public_user_poll_position(source_id: int, source_ref: str, message_id: int, target_id: int | None = None) -> None:
    owner_id = get_mapping_owner_id(-1)
    if target_id == -1:
        _require_saved_owner(owner_id)
    sql = (
        "UPDATE channel_mappings "
        "SET last_polled_message_id = CASE "
        "WHEN last_polled_message_id > ? THEN last_polled_message_id ELSE ? END "
        "WHERE source_mode = 'public_user' AND source_id = ? AND source_ref = ? AND enabled = 1 AND target_id != -1"
        + (" AND target_id = ?" if target_id is not None else "")
    )
    params = (message_id, message_id, source_id, source_ref) + ((target_id,) if target_id is not None else ())

    async def action(conn: aiosqlite.Connection):
        if target_id is None:
            saved_rule = await (await conn.execute(
                "SELECT 1 FROM channel_mappings WHERE source_mode = 'public_user' AND source_id = ? "
                "AND source_ref = ? AND enabled = 1 AND target_id = -1", (source_id, source_ref)
            )).fetchone()
            if saved_rule:
                _require_saved_owner(owner_id)
        if target_id != -1:
            await conn.execute(sql, params)
        if target_id in (None, -1) and owner_id > 0:
            await conn.execute(
                "INSERT INTO saved_poll_positions (owner_user_id, source_id, source_ref, last_polled_message_id) "
                "SELECT ?, source_id, source_ref, ? FROM channel_mappings WHERE source_id = ? AND source_ref = ? "
                "AND target_id = -1 AND source_mode = 'public_user' AND enabled = 1 "
                "ON CONFLICT(owner_user_id, source_id, source_ref) DO UPDATE SET "
                "last_polled_message_id = MAX(saved_poll_positions.last_polled_message_id, excluded.last_polled_message_id)",
                (owner_id, max(0, int(message_id)), source_id, source_ref),
            )

    await _run_in_db(action, commit=True)


async def save_msg_mapping(
    source_channel_id: int,
    source_msg_id: int,
    target_channel_id: int,
    target_msg_id: int,
    overwrite: bool = False,
    owner_user_id: int | None = None,
):
    owner_id = _delivery_owner_id(target_channel_id, owner_user_id)
    if target_channel_id == -1:
        _require_saved_owner(owner_id)
    sql = (
        "INSERT OR REPLACE INTO message_mappings "
        "(source_channel_id, source_msg_id, target_channel_id, target_msg_id) VALUES (?, ?, ?, ?)"
        if overwrite
        else
        "INSERT OR IGNORE INTO message_mappings "
        "(source_channel_id, source_msg_id, target_channel_id, target_msg_id) VALUES (?, ?, ?, ?)"
    )
    if target_channel_id == -1:
        sql = (
            ("INSERT OR REPLACE" if overwrite else "INSERT OR IGNORE")
            + " INTO saved_message_mappings (owner_user_id, source_channel_id, source_msg_id, target_msg_id) VALUES (?, ?, ?, ?)"
        )
        mapping_params = (owner_id, source_channel_id, source_msg_id, target_msg_id)
    else:
        mapping_params = (source_channel_id, source_msg_id, target_channel_id, target_msg_id)
    async def action(conn: aiosqlite.Connection):
        await conn.execute(sql, mapping_params)
        await conn.execute(
            "DELETE FROM message_sync_receipts "
            "WHERE source_channel_id = ? AND source_msg_id = ? AND target_channel_id = ? AND owner_user_id = ?",
            (source_channel_id, source_msg_id, target_channel_id, owner_id),
        )

    await _run_in_db(action, commit=True)


async def prepare_message_delivery(
    source_channel_id: int,
    target_channel_id: int,
    msg_ids: list[int],
    force: bool = False,
    owner_user_id: int | None = None,
) -> None:
    owner_id = _delivery_owner_id(target_channel_id, owner_user_id)
    if target_channel_id == -1:
        _require_saved_owner(owner_id)
    unique_ids = list(dict.fromkeys(int(msg_id) for msg_id in msg_ids))
    if not unique_ids:
        return

    async def action(conn: aiosqlite.Connection):
        # Reserve the entire group before any Telegram operation, including across connections.
        await conn.execute("BEGIN IMMEDIATE")
        if not force:
            pending_ids = []
            for msg_id in unique_ids:
                cursor = await conn.execute(
                    "SELECT 1 FROM message_sync_receipts "
                    "WHERE source_channel_id = ? AND source_msg_id = ? AND target_channel_id = ? AND owner_user_id = ?",
                    (source_channel_id, msg_id, target_channel_id, owner_id),
                )
                if await cursor.fetchone():
                    pending_ids.append(msg_id)
            if pending_ids:
                raise MessageDeliveryPendingError(source_channel_id, target_channel_id, pending_ids)
        await conn.executemany(
            "INSERT INTO message_sync_receipts "
            "(source_channel_id, source_msg_id, target_channel_id, owner_user_id, status, reason) "
            "VALUES (?, ?, ?, ?, 'sending', '') "
            "ON CONFLICT(source_channel_id, source_msg_id, target_channel_id, owner_user_id) "
            "DO UPDATE SET status = 'sending', reason = '', updated_at = CURRENT_TIMESTAMP",
            [(source_channel_id, msg_id, target_channel_id, owner_id) for msg_id in unique_ids],
        )

    await _run_in_db(action, commit=True)


async def get_message_delivery(
    source_channel_id: int,
    source_msg_id: int,
    target_channel_id: int,
    owner_user_id: int | None = None,
) -> dict | None:
    owner_id = _delivery_owner_id(target_channel_id, owner_user_id)
    if target_channel_id == -1 and owner_id <= 0:
        return None
    row = await _fetchone(
        "SELECT status, reason FROM message_sync_receipts "
        "WHERE source_channel_id = ? AND source_msg_id = ? AND target_channel_id = ? AND owner_user_id = ?",
        (source_channel_id, source_msg_id, target_channel_id, owner_id),
    )
    return {"state": row[0], "reason": row[1]} if row else None


async def mark_message_delivery_unconfirmed(
    source_channel_id: int,
    target_channel_id: int,
    msg_ids: list[int],
    reason: str,
    owner_user_id: int | None = None,
) -> None:
    owner_id = _delivery_owner_id(target_channel_id, owner_user_id)
    if target_channel_id == -1:
        _require_saved_owner(owner_id)
    await _executemany(
        "UPDATE message_sync_receipts SET status = 'unconfirmed', reason = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE source_channel_id = ? AND source_msg_id = ? AND target_channel_id = ? AND owner_user_id = ?",
        [(reason, source_channel_id, int(msg_id), target_channel_id, owner_id) for msg_id in msg_ids],
        commit=True,
    )


async def release_message_delivery(
    source_channel_id: int,
    target_channel_id: int,
    msg_ids: list[int],
    owner_user_id: int | None = None,
) -> None:
    owner_id = _delivery_owner_id(target_channel_id, owner_user_id)
    if target_channel_id == -1:
        _require_saved_owner(owner_id)
    await _executemany(
        "DELETE FROM message_sync_receipts "
        "WHERE source_channel_id = ? AND source_msg_id = ? AND target_channel_id = ? AND owner_user_id = ?",
        [(source_channel_id, int(msg_id), target_channel_id, owner_id) for msg_id in msg_ids],
        commit=True,
    )


async def get_target_msg_id(source_channel_id: int, source_msg_id: int, target_channel_id: int) -> int | None:
    if target_channel_id == -1:
        owner_id = get_mapping_owner_id(-1)
        if owner_id <= 0:
            return None
        row = await _fetchone(
            "SELECT target_msg_id FROM saved_message_mappings WHERE owner_user_id = ? AND source_channel_id = ? AND source_msg_id = ?",
            (owner_id, source_channel_id, source_msg_id),
        )
        return row[0] if row else None
    row = await _fetchone(
        "SELECT target_msg_id FROM message_mappings "
        "WHERE source_channel_id = ? AND source_msg_id = ? AND target_channel_id = ?",
        (source_channel_id, source_msg_id, target_channel_id),
    )
    return row[0] if row else None


async def get_all_target_msg_mappings(source_channel_id: int, source_msg_id: int) -> list[tuple[int, int, str]]:
    return await _fetchall(
        "SELECT mm.target_channel_id, mm.target_msg_id, COALESCE(cm.target_type, 'channel') "
        "FROM message_mappings AS mm "
        "LEFT JOIN channel_mappings AS cm "
        "ON cm.source_id = mm.source_channel_id AND cm.target_id = mm.target_channel_id "
        "WHERE mm.source_channel_id = ? AND mm.source_msg_id = ? AND mm.target_channel_id != -1 AND COALESCE(cm.enabled, 1) = 1 "
        "UNION ALL SELECT -1, sm.target_msg_id, 'saved' FROM saved_message_mappings sm "
        "LEFT JOIN channel_mappings cm ON cm.source_id = sm.source_channel_id AND cm.target_id = -1 "
        "WHERE sm.source_channel_id = ? AND sm.source_msg_id = ? AND sm.owner_user_id = ? AND COALESCE(cm.enabled, 1) = 1 ORDER BY 1",
        (source_channel_id, source_msg_id, source_channel_id, source_msg_id, get_mapping_owner_id(-1)),
    )


async def is_message_synced(source_channel_id: int, source_msg_id: int, target_channel_id: int) -> bool:
    if target_channel_id == -1:
        return await get_target_msg_id(source_channel_id, source_msg_id, target_channel_id) is not None
    row = await _fetchone(
        "SELECT 1 FROM message_mappings WHERE source_channel_id = ? AND source_msg_id = ? AND target_channel_id = ?",
        (source_channel_id, source_msg_id, target_channel_id),
    )
    return row is not None


async def delete_message_mappings_for_target(target_channel_id: int) -> None:
    if target_channel_id == -1:
        owner_id = _require_saved_owner(get_mapping_owner_id(-1))
        await _execute("DELETE FROM saved_message_mappings WHERE owner_user_id = ?", (owner_id,), commit=True)
        return
    await _execute(
        "DELETE FROM message_mappings WHERE target_channel_id = ?",
        (target_channel_id,),
        commit=True,
    )


async def _append_log(table: str, fields: tuple[str, str], values: tuple[str, str]) -> None:
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    retention_limit = get_log_retention_limit(table)

    async def action(conn: aiosqlite.Connection):
        await conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", values)
        await conn.execute(
            f"DELETE FROM {table} WHERE id NOT IN (SELECT id FROM {table} ORDER BY id DESC LIMIT ?)",
            (retention_limit,),
        )

    await _run_in_db(action, commit=True)
    _emit_terminal_log(table, values)


def _debug_terminal_logs_enabled() -> bool:
    return bool(get_config().get("app", {}).get("debug_terminal_logs", False))


def _terminal_log_level(label: str) -> int:
    text = str(label or "").upper()
    if "ERROR" in text or "FAIL" in text:
        return logging.ERROR
    if "WARN" in text or "DROP" in text:
        return logging.WARNING
    return logging.INFO


def _emit_terminal_log(table: str, values: tuple[str, str]) -> None:
    if not _debug_terminal_logs_enabled():
        return
    kind = "SYS" if table == "system_logs" else "MSG" if table == "message_logs" else table
    label, detail = values
    logging.getLogger("tg-channel-sync").log(_terminal_log_level(label), "[%s][%s] %s", kind, label, detail)


def get_log_retention_limit(table: str) -> int:
    sync_cfg = get_config().get("sync", {})
    if table == "system_logs":
        value = sync_cfg.get("system_log_retention_limit", 1000)
        default_value = 1000
    elif table == "message_logs":
        value = sync_cfg.get("message_log_retention_limit", 5000)
        default_value = 5000
    else:
        value = LOG_RETENTION_LIMIT
        default_value = LOG_RETENTION_LIMIT
    try:
        return max(100, int(value or default_value))
    except (TypeError, ValueError):
        return default_value


async def add_log(level: str, message: str):
    await _append_log("system_logs", ("level", "message"), (level, message))


async def add_sys_log(level: str, message: str):
    await add_log(level, message)


async def add_msg_log(action: str, detail: str):
    await _append_log("message_logs", ("action", "detail"), (action, detail))


async def get_sys_logs_after(last_id: int) -> list:
    return await _fetchall(
        "SELECT id, datetime(created_at, 'localtime'), level, message "
        "FROM system_logs WHERE id > ? ORDER BY id DESC LIMIT 50",
        (last_id,),
    )


async def get_recent_sys_logs(limit: int = LOG_RETENTION_LIMIT) -> list:
    return await _fetchall(
        "SELECT id, datetime(created_at, 'localtime'), level, message "
        "FROM system_logs ORDER BY id DESC LIMIT ?",
        (limit,),
    )


async def get_msg_logs_after(last_id: int) -> list:
    return await _fetchall(
        "SELECT id, datetime(created_at, 'localtime'), action, detail "
        "FROM message_logs WHERE id > ? ORDER BY id DESC LIMIT 50",
        (last_id,),
    )


async def get_recent_msg_logs(limit: int = LOG_RETENTION_LIMIT) -> list:
    return await _fetchall(
        "SELECT id, datetime(created_at, 'localtime'), action, detail "
        "FROM message_logs ORDER BY id DESC LIMIT ?",
        (limit,),
    )


async def get_all_sys_logs() -> list:
    return await _fetchall(
        "SELECT id, datetime(created_at, 'localtime'), level, message "
        "FROM system_logs ORDER BY id ASC"
    )


async def get_all_msg_logs() -> list:
    return await _fetchall(
        "SELECT id, datetime(created_at, 'localtime'), action, detail "
        "FROM message_logs ORDER BY id ASC"
    )


async def clear_sys_logs():
    await _execute("DELETE FROM system_logs", commit=True)


async def clear_msg_logs():
    await _execute("DELETE FROM message_logs", commit=True)


async def get_all_settings() -> dict:
    rows = await _fetchall("SELECT setting_key, setting_value FROM global_settings")
    return {key: value for key, value in rows}


async def update_settings(settings: dict):
    rows = [(key, str(value)) for key, value in settings.items()]
    await _executemany(
        "INSERT OR REPLACE INTO global_settings (setting_key, setting_value) VALUES (?, ?)",
        rows,
        commit=True,
    )


async def add_filter_rule(rule_type: str, pattern: str, replacement: str = "", is_case_sensitive: int = 0):
    validate_filter_rule(rule_type, pattern, replacement, is_case_sensitive)
    await _execute(
        "INSERT INTO filter_rules (rule_type, pattern, replacement, is_case_sensitive) VALUES (?, ?, ?, ?)",
        (rule_type, pattern, replacement, is_case_sensitive),
        commit=True,
    )


async def get_all_filter_rules() -> list:
    return await _fetchall("SELECT id, rule_type, pattern, replacement, is_case_sensitive FROM filter_rules")


async def delete_filter_rule(rule_id: int):
    await _execute("DELETE FROM filter_rules WHERE id = ?", (rule_id,), commit=True)




async def apply_message_filters(text_html: str, has_media: bool, file_name: str) -> tuple[bool, str]:
    del has_media
    rules = await get_all_filter_rules()
    should_skip = False
    new_text = text_html or ""
    for _, rule_type, pattern, replacement, is_case_sensitive in rules:
        try:
            regex = _compile_filter_regex(pattern, is_case_sensitive)
            should_skip, new_text = _apply_filter_rule(rule_type, regex, replacement, new_text, file_name)
            if should_skip:
                break
        except re.error:
            continue
    return should_skip, new_text
