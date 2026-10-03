import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import database
from sync_worker.json_import import process as importer
from sync_worker.runtime import state


SOURCE_ID = -1000000001001
TARGET_ID = -1002


class JsonDeliveryGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        task_temp = Path(__file__).resolve().parents[1] / "temp"
        task_temp.mkdir(exist_ok=True)
        self.temp_dir = tempfile.TemporaryDirectory(dir=task_temp)
        self.original_db = database.DB_FILE
        self.original_dirs = database.ensure_runtime_dirs
        await database.close_db()
        database.DB_FILE = str(Path(self.temp_dir.name) / "data.db")
        database.ensure_runtime_dirs = lambda: None
        await database.init_db()
        self.stack = ExitStack()
        self.stack.enter_context(patch.dict(importer.sync_state, {"current": 0, "skipped": 0, "unmapped": 0,
            "stop_requested": False, "mode": "JSON"}))
        self.stack.enter_context(patch.object(importer, "resolve_chat_id", AsyncMock(return_value=TARGET_ID)))
        self.stack.enter_context(patch.object(importer, "resolve_reply_target", AsyncMock(return_value=None)))
        self.stack.enter_context(patch.object(importer, "build_link_rewrite_context", AsyncMock(return_value={})))
        self.stack.enter_context(patch.object(importer, "rewrite_message_links", AsyncMock(side_effect=lambda text, *_: (text, 0))))
        self.stack.enter_context(patch.object(importer, "get_config", return_value={}))
        self.stack.enter_context(patch.object(importer.bot_engine, "note_upload_success", AsyncMock()))

        async def perform(action, **kwargs):
            return await action()

        self.stack.enter_context(patch.object(importer, "execute_with_network_retry", perform))
        self.stack.enter_context(patch.object(importer, "_execute_with_retry", perform))

    async def asyncTearDown(self):
        self.stack.close()
        await database.close_db()
        database.DB_FILE = self.original_db
        database.ensure_runtime_dirs = self.original_dirs
        self.temp_dir.cleanup()

    def group(self):
        items = []
        for msg_id in (1, 2):
            path = Path(self.temp_dir.name) / f"{msg_id}.jpg"
            path.write_bytes(b"photo")
            items.append({"id": msg_id, "type": "message", "photo": path.name, "text": ""})
        return items

    async def run_group(self, app, group, sender="user", force_send=False):
        with patch.object(importer.bot_engine, "pyro_user_app", app), \
             patch.object(importer, "_select_json_upload_target", AsyncMock(return_value={"sender": sender, "client": app, "label": "test"})):
            return await importer.send_json_media_group(group, TARGET_ID, self.temp_dir.name, SOURCE_ID,
                force_send, {}, sender, False)

    async def run_text(self, app, text="message"):
        data = {"type": "public_channel", "id": 1001, "messages": [{"id": 1, "type": "message", "text": text}]}
        with patch.object(importer, "load_json_export", return_value=data), \
             patch.object(importer.bot_engine, "pyro_user_app", app), \
             patch.object(importer, "_select_json_upload_target", AsyncMock(return_value={"sender": "user", "client": app, "label": "test"})):
            return await importer.process_json_sync("user", str(TARGET_ID), str(Path(self.temp_dir.name) / "export.json"), 0, False)

    async def test_single_missing_id_persists_and_blocks_second_run(self):
        app = SimpleNamespace(is_initialized=True, send_message=AsyncMock(return_value=SimpleNamespace()))
        outcome = await self.run_text(app)
        self.assertEqual(outcome.unmapped, 1)
        self.assertEqual((await database.get_message_delivery(SOURCE_ID, 1, TARGET_ID))["state"], "unconfirmed")
        await database.close_db()
        await database.init_db()
        again = await self.run_text(app)
        self.assertEqual(again.unmapped, 1)
        self.assertEqual(app.send_message.await_count, 1)
        self.assertFalse(await database.is_message_synced(SOURCE_ID, 1, TARGET_ID))

    async def test_mapping_failure_escapes_without_another_send(self):
        app = SimpleNamespace(is_initialized=True, send_message=AsyncMock(return_value=SimpleNamespace(id=901)))
        with patch.object(database, "save_msg_mapping", AsyncMock(side_effect=sqlite3.OperationalError("disk I/O error"))):
            with self.assertRaises(state.SyncMappingPersistenceError):
                await self.run_text(app)
        self.assertEqual(app.send_message.await_count, 1)
        self.assertIsNotNone(await database.get_message_delivery(SOURCE_ID, 1, TARGET_ID))

    async def test_partial_album_does_not_guess_mapping_and_blocks_resend(self):
        app = SimpleNamespace(is_initialized=True, send_media_group=AsyncMock(return_value=[SimpleNamespace(id=901)]))
        group = self.group()
        self.assertEqual(await self.run_group(app, group), importer.JSON_GROUP_SENT_UNMAPPED)
        self.assertFalse(await database.is_message_synced(SOURCE_ID, 1, TARGET_ID))
        self.assertEqual((await database.get_message_delivery(SOURCE_ID, 2, TARGET_ID))["state"], "unconfirmed")
        self.assertEqual(await self.run_group(app, group), importer.JSON_GROUP_SENT_UNMAPPED)
        self.assertEqual(app.send_media_group.await_count, 1)

    async def test_confirmed_leader_does_not_hide_pending_group_member(self):
        app = SimpleNamespace(is_initialized=True, send_media_group=AsyncMock(return_value=[SimpleNamespace(id=901), SimpleNamespace(id=902)]))
        group = self.group()
        save_mapping = database.save_msg_mapping

        async def fail_second_mapping(source_id, msg_id, target_id, target_msg_id, **kwargs):
            if msg_id == 2:
                raise sqlite3.OperationalError("disk I/O error")
            await save_mapping(source_id, msg_id, target_id, target_msg_id, **kwargs)

        with patch.object(database, "save_msg_mapping", side_effect=fail_second_mapping):
            with self.assertRaises(state.SyncMappingPersistenceError):
                await self.run_group(app, group)
        self.assertTrue(await database.is_message_synced(SOURCE_ID, 1, TARGET_ID))
        self.assertIsNone(await database.get_message_delivery(SOURCE_ID, 1, TARGET_ID))
        self.assertFalse(await database.is_message_synced(SOURCE_ID, 2, TARGET_ID))
        self.assertEqual((await database.get_message_delivery(SOURCE_ID, 2, TARGET_ID))["state"], "sending")
        await database.close_db()
        await database.init_db()

        self.assertEqual(await self.run_group(app, group), importer.JSON_GROUP_SENT_UNMAPPED)
        self.assertEqual(app.send_media_group.await_count, 1)
        self.assertEqual(importer.sync_state["skipped"], 0)

        self.assertEqual(await self.run_group(app, group, force_send=True), [901, 902])
        self.assertEqual(app.send_media_group.await_count, 2)
        self.assertEqual(await database.get_target_msg_id(SOURCE_ID, 2, TARGET_ID), 902)
        self.assertIsNone(await database.get_message_delivery(SOURCE_ID, 2, TARGET_ID))

    async def test_topics_album_persists_uncertainty(self):
        app = SimpleNamespace(is_initialized=True, send_media_group=AsyncMock(side_effect=TypeError("Messages.__init__ missing topics")))
        group = self.group()
        self.assertEqual(await self.run_group(app, group), importer.JSON_GROUP_SENT_UNMAPPED)
        self.assertEqual((await database.get_message_delivery(SOURCE_ID, 1, TARGET_ID))["state"], "unconfirmed")
        self.assertEqual(await self.run_group(app, group), importer.JSON_GROUP_SENT_UNMAPPED)
        self.assertEqual(app.send_media_group.await_count, 1)

    async def test_duplicate_target_ids_do_not_confirm_album_mapping(self):
        app = SimpleNamespace(is_initialized=True, send_media_group=AsyncMock(return_value=[SimpleNamespace(id=901), SimpleNamespace(id=901)]))
        self.assertEqual(await self.run_group(app, self.group()), importer.JSON_GROUP_SENT_UNMAPPED)
        for msg_id in (1, 2):
            self.assertFalse(await database.is_message_synced(SOURCE_ID, msg_id, TARGET_ID))
            self.assertEqual((await database.get_message_delivery(SOURCE_ID, msg_id, TARGET_ID))["state"], "unconfirmed")

    async def test_null_album_response_stays_unconfirmed(self):
        app = SimpleNamespace(is_initialized=True, send_media_group=AsyncMock(return_value=None))
        self.assertEqual(await self.run_group(app, self.group()), importer.JSON_GROUP_SENT_UNMAPPED)
        self.assertEqual((await database.get_message_delivery(SOURCE_ID, 1, TARGET_ID))["state"], "unconfirmed")

    async def test_single_topics_error_is_not_retried(self):
        app = SimpleNamespace(is_initialized=True, send_message=AsyncMock(side_effect=TypeError("Messages.__init__ missing topics")))
        outcome = await self.run_text(app)
        self.assertEqual(outcome.unmapped, 1)
        self.assertEqual((await database.get_message_delivery(SOURCE_ID, 1, TARGET_ID))["state"], "unconfirmed")
        self.assertEqual(app.send_message.await_count, 1)

    async def test_null_text_send_result_stays_unconfirmed(self):
        app = SimpleNamespace(is_initialized=True, send_message=AsyncMock(return_value=None))
        outcome = await self.run_text(app)
        self.assertEqual(outcome.unmapped, 1)
        self.assertEqual((await database.get_message_delivery(SOURCE_ID, 1, TARGET_ID))["state"], "unconfirmed")
        self.assertEqual(app.send_message.await_count, 1)

    async def test_group_intent_is_committed_before_rpc_and_cleared_on_success(self):
        async def sent(**kwargs):
            for msg_id in (1, 2):
                self.assertIsNotNone(await database.get_message_delivery(SOURCE_ID, msg_id, TARGET_ID))
            return [SimpleNamespace(id=901), SimpleNamespace(id=902)]

        app = SimpleNamespace(is_initialized=True, send_media_group=AsyncMock(side_effect=sent))
        self.assertEqual(await self.run_group(app, self.group()), [901, 902])
        for msg_id in (1, 2):
            self.assertIsNone(await database.get_message_delivery(SOURCE_ID, msg_id, TARGET_ID))
            self.assertEqual(await database.get_target_msg_id(SOURCE_ID, msg_id, TARGET_ID), msg_id + 900)

    async def test_preparation_failure_never_creates_intent(self):
        app = SimpleNamespace(is_initialized=True, send_media_group=AsyncMock())
        with patch.object(importer, "_prepare_json_media_group", AsyncMock(side_effect=OSError("prepare failed"))):
            with self.assertRaises(OSError):
                await self.run_group(app, self.group())
        app.send_media_group.assert_not_awaited()
        self.assertIsNone(await database.get_message_delivery(SOURCE_ID, 1, TARGET_ID))

    async def test_long_text_sends_all_parts_with_one_intent(self):
        app = SimpleNamespace(is_initialized=True, send_message=AsyncMock(side_effect=[SimpleNamespace(id=901), SimpleNamespace(id=902)]))
        with patch.object(database, "prepare_message_delivery", wraps=database.prepare_message_delivery) as prepare:
            outcome = await self.run_text(app, "x" * 5000)
        self.assertEqual(outcome.sent, 1)
        self.assertEqual(app.send_message.await_count, 2)
        self.assertEqual(prepare.await_count, 1)
        self.assertEqual(await database.get_target_msg_id(SOURCE_ID, 1, TARGET_ID), 901)

    async def test_post_send_pool_error_does_not_repeat_album_rpc(self):
        app = SimpleNamespace(send_media_group=AsyncMock(return_value=[SimpleNamespace(message_id=901), SimpleNamespace(message_id=902)]))
        with patch.object(importer.bot_engine, "note_upload_success", AsyncMock(side_effect=[RuntimeError("retry after 1"), None])), \
             patch.object(importer.bot_engine, "mark_upload_bot_cooldown", AsyncMock()):
            self.assertEqual(await self.run_group(app, self.group(), sender="bot"), [901, 902])
        self.assertEqual(app.send_media_group.await_count, 1)
