import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import AsyncMock, patch

import database


class SavedAccountDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        task_temp = Path(__file__).resolve().parents[1] / "temp"
        task_temp.mkdir(exist_ok=True)
        self.stack = ExitStack()
        self.temp_dir = self.stack.enter_context(tempfile.TemporaryDirectory(dir=task_temp))
        self.db_path = Path(self.temp_dir) / "account-data.db"
        self.session_base = Path(self.temp_dir) / "fake-user-session"
        await database.close_db()
        self.stack.enter_context(patch.object(database, "DB_FILE", str(self.db_path)))
        self.stack.enter_context(patch.object(database, "ensure_runtime_dirs", lambda: None))
        self.stack.enter_context(patch.object(database, "get_config", return_value={"sync": {}}))
        self.stack.enter_context(patch.object(database, "pyrogram_user_session_base", return_value=self.session_base, create=True))
        if hasattr(database, "clear_saved_message_account"):
            database.clear_saved_message_account()

    async def asyncTearDown(self):
        await database.close_db()
        if hasattr(database, "clear_saved_message_account"):
            database.clear_saved_message_account()
        self.stack.close()

    async def bind(self, owner):
        self.assertTrue(hasattr(database, "bind_saved_message_account"), "Verified Saved Messages account binding is required")
        await database.bind_saved_message_account(owner)

    async def add_saved_rule(self, source=-1001, cursor=0):
        await database.add_channel_mapping(source, -1, source_mode="public_user", source_ref=f"source{abs(source)}", target_type="saved", last_polled_message_id=cursor)

    async def saved_cursor(self, source=-1001):
        groups = await database.get_public_user_mapping_groups()
        mappings = [mapping for group in groups if group["source_id"] == source for mapping in group["mappings"] if mapping["target_id"] == -1]
        return mappings[0]["last_polled_message_id"]

    def create_legacy_data(self, owner=None, *, is_bot=0, cursor=10):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("CREATE TABLE channel_mappings (id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL, target_id INTEGER NOT NULL, source_mode TEXT NOT NULL, source_ref TEXT NOT NULL, last_polled_message_id INTEGER NOT NULL, target_type TEXT NOT NULL, UNIQUE(source_id,target_id))")
            conn.execute("INSERT INTO channel_mappings VALUES (1, -1001, -1, 'public_user', 'source1001', ?, 'saved')", (cursor,))
            conn.execute("CREATE TABLE message_mappings (id INTEGER PRIMARY KEY, source_channel_id INTEGER NOT NULL, source_msg_id INTEGER NOT NULL, target_channel_id INTEGER NOT NULL, target_msg_id INTEGER NOT NULL, UNIQUE(source_channel_id,source_msg_id,target_channel_id))")
            conn.execute("INSERT INTO message_mappings VALUES (1, -1001, 10, -1, 20)")
        if owner is not None:
            with closing(sqlite3.connect(self.session_base.with_suffix(".session"))) as conn, conn:
                conn.execute("CREATE TABLE sessions (user_id INTEGER, is_bot INTEGER)")
                conn.execute("INSERT INTO sessions VALUES (?, ?)", (owner, is_bot))

    async def test_unbound_saved_reads_are_empty_and_writes_rejected(self):
        await database.init_db()
        self.assertEqual(database.get_mapping_owner_id(-1), 0)
        self.assertIsNone(await database.get_target_msg_id(-1001, 10, -1))
        with self.assertRaisesRegex(ValueError, "账号|登录"):
            await database.save_msg_mapping(-1001, 10, -1, 20)
        with self.assertRaisesRegex(ValueError, "账号|登录"):
            await database.prepare_message_delivery(-1001, -1, [10])

    async def test_saved_rule_with_explicit_start_requires_verified_owner(self):
        await database.init_db()
        with self.assertRaisesRegex(ValueError, "账号|登录"):
            await self.add_saved_rule(cursor=15)
        self.assertFalse(await database.has_channel_mapping(-1001, -1))

    async def test_account_a_b_a_preserves_independent_saved_mappings(self):
        await database.init_db()
        await self.bind(111)
        await database.save_msg_mapping(-1001, 10, -1, 20)
        await database.save_msg_mapping(-1001, 10, -1002, 30)
        await self.bind(222)
        self.assertFalse(await database.is_message_synced(-1001, 10, -1))
        self.assertEqual(await database.get_all_target_msg_mappings(-1001, 10), [(-1002, 30, "channel")])
        await database.save_msg_mapping(-1001, 10, -1, 40)
        await self.bind(111)
        self.assertEqual(await database.get_target_msg_id(-1001, 10, -1), 20)
        self.assertEqual(await database.get_all_target_msg_mappings(-1001, 10), [(-1002, 30, "channel"), (-1, 20, "saved")])
        self.assertEqual(await database.get_target_msg_id(-1001, 10, -1002), 30)

    async def test_saved_mapping_and_receipt_survive_database_restart(self):
        await database.init_db()
        await self.bind(111)
        await database.save_msg_mapping(-1001, 10, -1, 20)
        await database.prepare_message_delivery(-1001, -1, [11])
        database.clear_saved_message_account()
        await database.close_db()
        await database.init_db()
        await self.bind(222)
        self.assertIsNone(await database.get_target_msg_id(-1001, 10, -1))
        self.assertIsNone(await database.get_message_delivery(-1001, 11, -1))
        await self.bind(111)
        self.assertEqual(await database.get_target_msg_id(-1001, 10, -1), 20)
        self.assertEqual((await database.get_message_delivery(-1001, 11, -1))["state"], "sending")

    async def test_saved_confirmation_uses_captured_owner_and_rolls_back_atomically(self):
        await database.init_db()
        await self.bind(111)
        await database.prepare_message_delivery(-1001, -1, [10])
        await self.bind(222)
        await database.prepare_message_delivery(-1001, -1, [10])
        conn = await database._get_connection()
        with patch.object(conn, "commit", AsyncMock(side_effect=sqlite3.OperationalError("disk I/O error"))):
            with self.assertRaises(sqlite3.OperationalError):
                await database.save_msg_mapping(-1001, 10, -1, 20, owner_user_id=111)
        self.assertIsNotNone(await database.get_message_delivery(-1001, 10, -1, owner_user_id=111))
        await database.save_msg_mapping(-1001, 10, -1, 20, owner_user_id=111)
        self.assertIsNone(await database.get_message_delivery(-1001, 10, -1, owner_user_id=111))
        self.assertIsNotNone(await database.get_message_delivery(-1001, 10, -1))
        self.assertIsNone(await database.get_target_msg_id(-1001, 10, -1))
        await self.bind(111)
        self.assertEqual(await database.get_target_msg_id(-1001, 10, -1), 20)

    async def test_saved_reset_only_clears_active_account_and_preserves_legacy_archive(self):
        self.create_legacy_data(111)
        await database.init_db()
        await self.bind(111)
        await self.bind(222)
        await database.save_msg_mapping(-1001, 10, -1, 40)
        await database.save_msg_mapping(-1001, 10, -1002, 30)
        await database.delete_message_mappings_for_target(-1)
        self.assertIsNone(await database.get_target_msg_id(-1001, 10, -1))
        self.assertEqual(await database.get_target_msg_id(-1001, 10, -1002), 30)
        await self.bind(111)
        self.assertEqual(await database.get_target_msg_id(-1001, 10, -1), 20)
        await database.delete_message_mappings_for_target(-1)
        await self.bind(111)
        self.assertIsNone(await database.get_target_msg_id(-1001, 10, -1))
        self.assertEqual(await database._fetchall("SELECT target_msg_id FROM message_mappings WHERE target_channel_id = -1"), [(20,)])

    async def test_new_saved_rule_explicit_start_only_initializes_creating_account(self):
        await database.init_db()
        await self.bind(111)
        await self.add_saved_rule(cursor=15)
        self.assertEqual(await self.saved_cursor(), 15)
        await database.update_public_user_poll_position(-1001, "source1001", 30, target_id=-1)
        await self.add_saved_rule(cursor=90)
        self.assertEqual(await self.saved_cursor(), 30)
        await self.bind(222)
        self.assertEqual(await self.saved_cursor(), 0)
        await self.bind(111)
        self.assertEqual(await self.saved_cursor(), 30)

    async def test_recreated_saved_rule_uses_explicit_start_only_for_active_account(self):
        await database.init_db()
        await self.bind(111)
        await self.add_saved_rule(cursor=15)
        await database.update_public_user_poll_position(-1001, "source1001", 30, target_id=-1)
        await self.bind(222)
        await database.update_public_user_poll_position(-1001, "source1001", 40, target_id=-1)
        await self.bind(111)
        await database.delete_channel_mapping(-1001, -1)
        await self.add_saved_rule(cursor=90)
        self.assertEqual(await self.saved_cursor(), 90)
        await self.add_saved_rule(cursor=120)
        self.assertEqual(await self.saved_cursor(), 90)
        await self.bind(222)
        self.assertEqual(await self.saved_cursor(), 40)

    async def test_bulk_poll_update_routes_saved_and_ordinary_targets_separately(self):
        await database.init_db()
        await self.bind(111)
        await self.add_saved_rule(cursor=5)
        await database.add_channel_mapping(-1001, -1002, source_mode="public_user", source_ref="source1001", last_polled_message_id=7)
        await database.update_public_user_poll_position(-1001, "source1001", 20)
        self.assertEqual(await self.saved_cursor(), 20)
        await self.bind(222)
        self.assertEqual(await self.saved_cursor(), 0)
        await database.update_public_user_poll_position(-1001, "source1001", 30)
        self.assertEqual(await self.saved_cursor(), 30)
        await self.bind(111)
        self.assertEqual(await self.saved_cursor(), 20)
        groups = await database.get_public_user_mapping_groups()
        ordinary = [m for g in groups for m in g["mappings"] if m["target_id"] == -1002]
        self.assertEqual(ordinary[0]["last_polled_message_id"], 30)
        archived_saved_cursor = await database._fetchone("SELECT last_polled_message_id FROM channel_mappings WHERE target_id = -1")
        self.assertEqual(archived_saved_cursor[0], 0)

    async def test_legacy_only_migrates_to_frozen_verified_session_owner(self):
        self.create_legacy_data(111, cursor=15)
        session = self.session_base.with_suffix(".session")
        original_bytes = session.read_bytes()
        connect = sqlite3.connect
        reads = []

        def connect_read_only(path, *args, **kwargs):
            if str(session).replace("\\", "/") in str(path).replace("\\", "/"):
                self.assertIn("mode=ro", str(path))
                self.assertTrue(kwargs.get("uri"))
                reads.append(path)
            return connect(path, *args, **kwargs)

        with patch.object(sqlite3, "connect", side_effect=connect_read_only):
            await database.init_db()
        self.assertEqual(len(reads), 1)
        self.assertEqual(session.read_bytes(), original_bytes)
        await self.bind(222)
        self.assertIsNone(await database.get_target_msg_id(-1001, 10, -1))
        self.assertEqual(await self.saved_cursor(), 0)
        await self.bind(111)
        self.assertEqual(await database.get_target_msg_id(-1001, 10, -1), 20)
        self.assertEqual(await self.saved_cursor(), 15)
        self.assertEqual(await database._fetchall("SELECT target_msg_id FROM message_mappings WHERE target_channel_id = -1"), [(20,)])

    async def test_legacy_owner_snapshot_is_not_replaced_after_account_switch(self):
        self.create_legacy_data(111)
        await database.init_db()
        with closing(sqlite3.connect(self.session_base.with_suffix(".session"))) as conn, conn:
            conn.execute("UPDATE sessions SET user_id = 222")
        connect = sqlite3.connect

        def no_session_reread(path, *args, **kwargs):
            self.assertNotIn("fake-user-session", str(path), "Session must be inspected only once")
            return connect(path, *args, **kwargs)

        with patch.object(sqlite3, "connect", side_effect=no_session_reread):
            await database.init_db()
        await self.bind(222)
        self.assertIsNone(await database.get_target_msg_id(-1001, 10, -1))
        await self.bind(111)
        self.assertEqual(await database.get_target_msg_id(-1001, 10, -1), 20)

    async def test_missing_legacy_owner_is_frozen_unknown_and_warned_once(self):
        self.create_legacy_data()
        with patch.object(database.logging, "getLogger") as logger:
            await database.init_db()
            await database.init_db()
            logger.return_value.warning.assert_called_once()
        self.assertEqual(len(await database.get_all_sys_logs()), 1)
        self.assertIn("归属无法确认", (await database.get_all_sys_logs())[0][3])
        with closing(sqlite3.connect(self.session_base.with_suffix(".session"))) as conn, conn:
            conn.execute("CREATE TABLE sessions (user_id INTEGER, is_bot INTEGER)")
            conn.execute("INSERT INTO sessions VALUES (111, 0)")
        await database.init_db()
        await self.bind(111)
        self.assertIsNone(await database.get_target_msg_id(-1001, 10, -1))
        self.assertEqual(await self.saved_cursor(), 0)
        self.assertEqual(await database._fetchall("SELECT target_msg_id FROM message_mappings WHERE target_channel_id = -1"), [(20,)])

    async def test_bot_session_cannot_claim_legacy_saved_data(self):
        self.create_legacy_data(111, is_bot=1)
        await database.init_db()
        await self.bind(111)
        self.assertIsNone(await database.get_target_msg_id(-1001, 10, -1))

    async def test_fractional_session_owner_is_frozen_unknown(self):
        await self._check_malformed_session_owner(111.5)

    async def test_infinite_session_owner_is_frozen_unknown(self):
        await self._check_malformed_session_owner(float("inf"))

    async def _check_malformed_session_owner(self, malformed_owner):
        self.create_legacy_data(malformed_owner)
        await database.init_db()
        self.assertEqual((await database.get_all_settings())["saved_messages_legacy_owner_id"], "0")
        await self.bind(111)
        self.assertIsNone(await database.get_target_msg_id(-1001, 10, -1))
        self.assertEqual(await self.saved_cursor(), 0)
        with closing(sqlite3.connect(self.session_base.with_suffix(".session"))) as conn, conn:
            conn.execute("UPDATE sessions SET user_id = 111")
        await database.init_db()
        await self.bind(111)
        self.assertIsNone(await database.get_target_msg_id(-1001, 10, -1))
        self.assertEqual((await database.get_all_settings())["saved_messages_legacy_owner_id"], "0")
        self.assertEqual(len(await database.get_all_sys_logs()), 1)

    async def test_legacy_migration_failure_rolls_back_and_does_not_publish_owner(self):
        self.create_legacy_data(111)
        await database.init_db()
        conn = await database._get_connection()
        with patch.object(conn, "commit", AsyncMock(side_effect=sqlite3.OperationalError("disk I/O error"))):
            with self.assertRaises(sqlite3.OperationalError):
                await self.bind(111)
        self.assertEqual(database.get_mapping_owner_id(-1), 0)
        self.assertEqual(await database._fetchall("SELECT target_msg_id FROM saved_message_mappings"), [])
        await self.bind(111)
        self.assertEqual(await database.get_target_msg_id(-1001, 10, -1), 20)

    async def test_failed_initialization_rolls_back_additive_schema_and_snapshot(self):
        self.create_legacy_data(111)
        conn = await database._get_connection()
        with patch.object(conn, "commit", AsyncMock(side_effect=sqlite3.OperationalError("disk I/O error"))):
            with self.assertRaises(sqlite3.OperationalError):
                await database.init_db()
        self.assertEqual(await database._fetchall("SELECT name FROM sqlite_master WHERE name IN ('saved_message_mappings', 'saved_poll_positions', 'global_settings')"), [])
        self.assertEqual(await database._fetchall("SELECT target_msg_id FROM message_mappings"), [(20,)])
        await database.init_db()
        await self.bind(111)
        self.assertEqual(await database.get_target_msg_id(-1001, 10, -1), 20)

    async def test_corrupt_or_ambiguous_session_cannot_claim_legacy_data(self):
        self.create_legacy_data(111)
        with closing(sqlite3.connect(self.session_base.with_suffix(".session"))) as conn, conn:
            conn.execute("INSERT INTO sessions VALUES (222, 0)")
        await database.init_db()
        await self.bind(111)
        self.assertIsNone(await database.get_target_msg_id(-1001, 10, -1))
        # A repaired session cannot replace the persisted unknown snapshot.
        self.session_base.with_suffix(".session").write_bytes(b"not a sqlite database")
        await database.init_db()
        await self.bind(222)
        self.assertIsNone(await database.get_target_msg_id(-1001, 10, -1))

    async def test_paused_saved_rule_preserves_account_cursor_and_excludes_edits(self):
        await database.init_db()
        await self.bind(111)
        await self.add_saved_rule(cursor=5)
        await database.save_msg_mapping(-1001, 10, -1, 20)
        await database.update_channel_mapping(-1001, -1, enabled=False)
        await database.update_public_user_poll_position(-1001, "source1001", 30)
        self.assertEqual(await database.get_public_user_mapping_groups(), [])
        self.assertEqual(await database.get_all_target_msg_mappings(-1001, 10), [])
        await database.update_channel_mapping(-1001, -1, enabled=True)
        self.assertEqual(await self.saved_cursor(), 5)

    async def test_bulk_checkpoint_failure_rolls_back_saved_and_ordinary_together(self):
        await database.init_db()
        await self.bind(111)
        await self.add_saved_rule(cursor=5)
        await database.add_channel_mapping(-1001, -1002, source_mode="public_user", source_ref="source1001", last_polled_message_id=7)
        conn = await database._get_connection()
        with patch.object(conn, "commit", AsyncMock(side_effect=sqlite3.OperationalError("disk I/O error"))):
            with self.assertRaises(sqlite3.OperationalError):
                await database.update_public_user_poll_position(-1001, "source1001", 30)
        self.assertEqual(await self.saved_cursor(), 5)
        mappings = (await database.get_public_user_mapping_groups())[0]["mappings"]
        self.assertEqual([m["last_polled_message_id"] for m in mappings if m["target_id"] == -1002], [7])
