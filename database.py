import aiosqlite
import logging
import re

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
        # ================= 新增：正则过滤规则表 =================
        await db.execute("""
            CREATE TABLE IF NOT EXISTS filter_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_type TEXT NOT NULL,
                pattern TEXT NOT NULL,
                replacement TEXT
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

# ================= 新增：过滤规则数据库操作与核心正则算法 =================
async def add_filter_rule(rule_type: str, pattern: str, replacement: str = ""):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("INSERT INTO filter_rules (rule_type, pattern, replacement) VALUES (?, ?, ?)", (rule_type, pattern, replacement))
        await db.commit()

async def get_all_filter_rules() -> list:
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("SELECT id, rule_type, pattern, replacement FROM filter_rules")
        return await cursor.fetchall()

async def delete_filter_rule(rule_id: int):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM filter_rules WHERE id = ?", (rule_id,))
        await db.commit()

async def apply_message_filters(text_html: str, has_media: bool, file_name: str) -> tuple[bool, str]:
    """全局过滤引擎：根据正则规则判定是否跳过，并返回处理后的 HTML 文本"""
    rules = await get_all_filter_rules()
    should_skip = False
    new_text = text_html or ""

    for r in rules:
        r_id, r_type, pattern, replacement = r
        try:
            regex = re.compile(pattern, re.IGNORECASE)
            if r_type == 'skip_media' and has_media:
                # 媒体跳过规则：如果文本内容或文件名命中了正则，整条消息直接屏蔽
                if regex.search(new_text) or (file_name and regex.search(file_name)):
                    should_skip = True
                    break
            elif r_type == 'replace_text':
                # 文本替换规则：执行正则替换
                if new_text:
                    new_text = regex.sub(replacement or "", new_text)
        except re.error:
            continue # 遇到用户填写的错误正则时忽略，防止系统崩溃

    return should_skip, new_text