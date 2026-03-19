import aiosqlite
import logging

DB_FILE = "data.db"

async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS channel_mappings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL UNIQUE,
                target_id INTEGER NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS message_mappings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_channel_id INTEGER NOT NULL,
                source_msg_id INTEGER NOT NULL,
                target_msg_id INTEGER NOT NULL,
                UNIQUE(source_channel_id, source_msg_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS system_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_source_msg ON message_mappings(source_channel_id, source_msg_id)")
        await db.commit()

async def add_channel_mapping(source_id: int, target_id: int):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("INSERT OR REPLACE INTO channel_mappings (source_id, target_id) VALUES (?, ?)", (source_id, target_id))
        await db.commit()

async def delete_channel_mapping(source_id: int):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM channel_mappings WHERE source_id = ?", (source_id,))
        await db.commit()

async def get_target_channel(source_id: int) -> int:
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("SELECT target_id FROM channel_mappings WHERE source_id = ?", (source_id,))
        row = await cursor.fetchone()
        return row[0] if row else None

async def get_all_channel_mappings() -> list:
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("SELECT source_id, target_id FROM channel_mappings")
        return await cursor.fetchall()

async def save_msg_mapping(source_channel_id: int, source_msg_id: int, target_msg_id: int):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("INSERT OR IGNORE INTO message_mappings (source_channel_id, source_msg_id, target_msg_id) VALUES (?, ?, ?)",(source_channel_id, source_msg_id, target_msg_id))
        await db.commit()

async def get_target_msg_id(source_channel_id: int, source_msg_id: int) -> int:
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("SELECT target_msg_id FROM message_mappings WHERE source_channel_id = ? AND source_msg_id = ?",(source_channel_id, source_msg_id))
        row = await cursor.fetchone()
        return row[0] if row else None

async def is_message_synced(source_channel_id: int, source_msg_id: int) -> bool:
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("SELECT 1 FROM message_mappings WHERE source_channel_id = ? AND source_msg_id = ?", (source_channel_id, source_msg_id))
        row = await cursor.fetchone()
        return row is not None

async def add_log(level: str, message: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("INSERT INTO system_logs (level, message) VALUES (?, ?)", (level, message))
        await db.commit()

async def get_recent_logs(limit: int = 200) -> list:
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("""
            SELECT datetime(created_at, 'localtime'), level, message 
            FROM system_logs 
            WHERE created_at >= datetime('now', '-1 day') 
            ORDER BY created_at DESC LIMIT ?
        """, (limit,))
        return await cursor.fetchall()