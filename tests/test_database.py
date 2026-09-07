import sqlite3
import tempfile
import unittest
from pathlib import Path

import database
from services import sync_services


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "data.db"
        self.original_db_file = database.DB_FILE
        self.original_ensure_dirs = database.ensure_runtime_dirs
        self.original_get_config = database.get_config
        database.DB_FILE = str(self.db_path)
        database.ensure_runtime_dirs = lambda: self.db_path.parent.mkdir(parents=True, exist_ok=True)
        database.get_config = lambda: {"sync": {"system_log_retention_limit": 1000, "message_log_retention_limit": 5000}}
        await database.close_db()

    async def asyncTearDown(self):
        await database.close_db()
        database.DB_FILE = self.original_db_file
        database.ensure_runtime_dirs = self.original_ensure_dirs
        database.get_config = self.original_get_config
        self.temp_dir.cleanup()

    async def test_apply_message_filters_drops_text_or_file_name_matches(self):
        await database.init_db()
        await database.add_filter_rule("drop", "blocked")
        await database.add_filter_rule("skip_media", r"\.zip$")

        should_skip_text, text = await database.apply_message_filters("blocked message", False, "safe.txt")
        should_skip_file, _ = await database.apply_message_filters("allowed message", False, "archive.zip")

        self.assertTrue(should_skip_text)
        self.assertEqual(text, "blocked message")
        self.assertTrue(should_skip_file)

    async def test_apply_message_filters_replaces_text_in_rule_order(self):
        await database.init_db()
        await database.add_filter_rule("replace", "hello", "hi")
        await database.add_filter_rule("replace_text", "hi world", "done")

        should_skip, text = await database.apply_message_filters("hello world", False, "")

        self.assertFalse(should_skip)
        self.assertEqual(text, "done")

    async def test_apply_message_filters_skips_invalid_regex_and_continues(self):
        await database.init_db()
        await database.add_filter_rule("drop", "[")
        await database.add_filter_rule("replace", "hello", "hi")

        should_skip, text = await database.apply_message_filters("hello", False, "")

        self.assertFalse(should_skip)
        self.assertEqual(text, "hi")

    async def test_init_db_migrates_old_tables(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE channel_mappings (id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL UNIQUE, target_id INTEGER NOT NULL)")
        conn.execute("INSERT INTO channel_mappings (source_id, target_id) VALUES (1001, 2001)")
        conn.execute("CREATE TABLE message_mappings (id INTEGER PRIMARY KEY AUTOINCREMENT, source_channel_id INTEGER NOT NULL, source_msg_id INTEGER NOT NULL, target_msg_id INTEGER NOT NULL)")
        conn.execute("INSERT INTO message_mappings (source_channel_id, source_msg_id, target_msg_id) VALUES (1001, 11, 99)")
        conn.commit()
        conn.close()

        await database.init_db()

        self.assertEqual(await database.get_target_channels(1001), [2001])
        [mapping] = await database.get_target_channel_mappings(1001)
        self.assertEqual(mapping["target_id"], 2001)
        self.assertEqual(mapping["realtime_sender"], "bot")
        self.assertTrue(mapping["realtime_fallback_to_user"])
        self.assertFalse(mapping["realtime_hash_perturb"])
        self.assertEqual(mapping["source_mode"], "bot")
        self.assertEqual(mapping["source_ref"], "")
        # 迁移新增的 target_type 列对既有行回填默认值 "channel"，保证向后兼容。
        self.assertEqual(mapping["target_type"], "channel")
        self.assertEqual(await database.get_target_msg_id(1001, 11, 2001), 99)

    async def test_system_log_retention_uses_configured_limit(self):
        await database.init_db()
        database.get_config = lambda: {"sync": {"system_log_retention_limit": 120, "message_log_retention_limit": 5000}}
        for index in range(125):
            await database.add_sys_log("INFO", f"log-{index}")

        rows = await database.get_all_sys_logs()
        self.assertEqual(len(rows), 120)
        self.assertEqual(rows[0][3], "log-5")
        self.assertEqual(rows[-1][3], "log-124")

    async def test_message_log_retention_uses_independent_configured_limit(self):
        await database.init_db()
        database.get_config = lambda: {"sync": {"system_log_retention_limit": 1000, "message_log_retention_limit": 150}}
        for index in range(160):
            await database.add_msg_log("SEND", f"log-{index}")

        rows = await database.get_all_msg_logs()
        self.assertEqual(len(rows), 150)
        self.assertEqual(rows[0][3], "log-10")
        self.assertEqual(rows[-1][3], "log-159")

    async def test_multi_target_message_mapping_isolated(self):
        await database.init_db()
        await database.save_msg_mapping(1, 10, 100, 500)
        await database.save_msg_mapping(1, 10, 200, 600)

        self.assertEqual(await database.get_target_msg_id(1, 10, 100), 500)
        self.assertEqual(await database.get_target_msg_id(1, 10, 200), 600)

    async def test_channel_mapping_realtime_options_are_per_mapping(self):
        await database.init_db()
        await database.add_channel_mapping(
            1,
            100,
            realtime_sender="user",
            realtime_fallback_to_user=False,
            realtime_hash_perturb=True,
        )
        await database.add_channel_mapping(1, 200)

        mappings = await database.get_target_channel_mappings(1)

        self.assertEqual(
            mappings,
            [
                {
                    "target_id": 100,
                    "realtime_sender": "user",
                    "realtime_fallback_to_user": False,
                    "realtime_hash_perturb": True,
                    "source_mode": "bot",
                    "source_ref": "",
                    "target_type": "channel",
                },
                {
                    "target_id": 200,
                    "realtime_sender": "bot",
                    "realtime_fallback_to_user": True,
                    "realtime_hash_perturb": False,
                    "source_mode": "bot",
                    "source_ref": "",
                    "target_type": "channel",
                },
            ],
        )

    async def test_public_user_channel_mapping_groups(self):
        await database.init_db()
        await database.add_channel_mapping(
            -1001,
            -2001,
            realtime_sender="user",
            source_mode="public_user",
            source_ref="public_source",
            last_polled_message_id=10,
        )
        await database.add_channel_mapping(
            -1001,
            -2002,
            realtime_sender="user",
            source_mode="public_user",
            source_ref="public_source",
            last_polled_message_id=12,
        )

        [group] = await database.get_public_user_mapping_groups()

        self.assertEqual(group["source_id"], -1001)
        self.assertEqual(group["source_ref"], "public_source")
        self.assertEqual(group["last_polled_message_id"], 12)
        self.assertEqual([item["target_id"] for item in group["mappings"]], [-2002, -2001])

    async def test_channel_mapping_duplicate_and_cycle_detection(self):
        await database.init_db()
        await database.add_channel_mapping(1, 2)
        await database.add_channel_mapping(2, 3)

        self.assertTrue(await database.has_channel_mapping(1, 2))
        self.assertTrue(await database.would_create_channel_mapping_cycle(1, 1))
        self.assertTrue(await database.would_create_channel_mapping_cycle(3, 1))
        self.assertFalse(await database.would_create_channel_mapping_cycle(3, 4))

    async def test_channel_mapping_saved_target_type_stored_and_read_back(self):
        await database.init_db()
        await database.add_channel_mapping(
            -100123,
            sync_services.SAVED_MESSAGES_TARGET_ID,
            realtime_sender="user",
            source_mode="public_user",
            source_ref="public_source",
            target_type="saved",
        )

        mappings = await database.get_target_channel_mappings(-100123)
        [mapping] = [m for m in mappings if m["target_type"] == "saved"]
        self.assertEqual(mapping["target_id"], -1)
        self.assertEqual(mapping["target_type"], "saved")
        self.assertEqual(mapping["realtime_sender"], "user")

        [group] = await database.get_public_user_mapping_groups()
        [item] = [m for m in group["mappings"] if m["target_type"] == "saved"]
        self.assertEqual(item["target_id"], -1)
        self.assertEqual(item["target_type"], "saved")

        await database.save_msg_mapping(-100123, 10, sync_services.SAVED_MESSAGES_TARGET_ID, 20)
        self.assertEqual(
            await database.get_all_target_msg_mappings(-100123, 10),
            [(sync_services.SAVED_MESSAGES_TARGET_ID, 20, "saved")],
        )

    async def test_delete_message_mappings_for_target_preserves_other_targets(self):
        await database.init_db()
        await database.save_msg_mapping(-100123, 10, sync_services.SAVED_MESSAGES_TARGET_ID, 20)
        await database.save_msg_mapping(-100123, 10, -100456, 30)

        await database.delete_message_mappings_for_target(sync_services.SAVED_MESSAGES_TARGET_ID)

        self.assertIsNone(
            await database.get_target_msg_id(-100123, 10, sync_services.SAVED_MESSAGES_TARGET_ID)
        )
        self.assertEqual(await database.get_target_msg_id(-100123, 10, -100456), 30)
