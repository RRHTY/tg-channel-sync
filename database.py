import aiosqlite
import logging
import re

DB_FILE = "data.db"

async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS channel_mappings (id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL UNIQUE, target_id INTEGER NOT NULL)")
        await db.execute("CREATE TABLE IF NOT EXISTS message_mappings (id INTEGER PRIMARY KEY AUTOINCREMENT, source_channel_id INTEGER NOT NULL, source_msg_id INTEGER NOT NULL, target_msg_id INTEGER NOT NULL, UNIQUE(source_channel_id, source_msg_id))")
        await db.execute("CREATE TABLE IF NOT EXISTS system_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, level TEXT NOT NULL, message TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        await db.execute("CREATE TABLE IF NOT EXISTS filter_rules (id INTEGER PRIMARY KEY AUTOINCREMENT, rule_type TEXT NOT NULL, pattern TEXT NOT NULL, replacement TEXT, is_case_sensitive INTEGER DEFAULT 0)")
        await db.execute("CREATE TABLE IF NOT EXISTS message_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL, detail TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        await db.execute("CREATE TABLE IF NOT EXISTS global_settings (setting_key TEXT PRIMARY KEY, setting_value TEXT NOT NULL)")
        try: await db.execute("ALTER TABLE filter_rules ADD COLUMN is_case_sensitive INTEGER DEFAULT 0")
        except Exception: pass
        await db.execute("CREATE INDEX IF NOT EXISTS idx_source_msg ON message_mappings(source_channel_id, source_msg_id)")
        await db.commit()

        default_settings = {"sync_text": "1", "sync_photo": "1", "sync_video": "1", "sync_document": "1", "sync_sticker": "1", "sync_gif": "1", "sync_audio": "1", "sync_voice": "1"}
        for k, v in default_settings.items(): await db.execute("INSERT OR IGNORE INTO global_settings (setting_key, setting_value) VALUES (?, ?)", (k, v))
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

# ================= 增量日志查询 (专供 SSE 流使用) =================
async def add_log(level: str, message: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("INSERT INTO system_logs (level, message) VALUES (?, ?)", (level, message))
        await db.commit()

async def get_sys_logs_after(last_id: int) -> list:
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("SELECT id, datetime(created_at, 'localtime'), level, message FROM system_logs WHERE id > ? ORDER BY id DESC LIMIT 50", (last_id,))
        return await cursor.fetchall()

async def add_msg_log(action: str, detail: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("INSERT INTO message_logs (action, detail) VALUES (?, ?)", (action, detail))
        await db.commit()

async def get_msg_logs_after(last_id: int) -> list:
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("SELECT id, datetime(created_at, 'localtime'), action, detail FROM message_logs WHERE id > ? ORDER BY id DESC LIMIT 50", (last_id,))
        return await cursor.fetchall()

async def get_all_settings() -> dict:
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("SELECT setting_key, setting_value FROM global_settings")
        return {k: v for k, v in await cursor.fetchall()}

async def update_settings(settings: dict):
    async with aiosqlite.connect(DB_FILE) as db:
        for k, v in settings.items(): await db.execute("INSERT OR REPLACE INTO global_settings (setting_key, setting_value) VALUES (?, ?)", (k, str(v)))
        await db.commit()

async def add_filter_rule(rule_type: str, pattern: str, replacement: str = "", is_case_sensitive: int = 0):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("INSERT INTO filter_rules (rule_type, pattern, replacement, is_case_sensitive) VALUES (?, ?, ?, ?)", (rule_type, pattern, replacement, is_case_sensitive))
        await db.commit()

async def get_all_filter_rules() -> list:
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("SELECT id, rule_type, pattern, replacement, is_case_sensitive FROM filter_rules")
        return await cursor.fetchall()

async def delete_filter_rule(rule_id: int):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM filter_rules WHERE id = ?", (rule_id,))
        await db.commit()

async def apply_message_filters(text_html: str, has_media: bool, file_name: str) -> tuple[bool, str]:
    rules = await get_all_filter_rules()
    should_skip = False
    new_text = text_html or ""
    for r in rules:
        _, r_type, pattern, replacement, is_case_sensitive = r
        flags = 0 if is_case_sensitive else re.IGNORECASE
        try:
            regex = re.compile(pattern, flags)
            if r_type in ['drop', 'skip_media']:
                if regex.search(new_text) or (file_name and regex.search(file_name)):
                    should_skip = True; break 
            elif r_type in ['replace', 'replace_text']:
                if new_text: new_text = regex.sub(replacement or "", new_text)
        except re.error: continue
    return should_skip, new_text