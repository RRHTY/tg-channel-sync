import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import database
from sync_worker.clone import process as history


class HistoryDeliveryGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / "temp")
        self.original_db = database.DB_FILE
        self.original_dirs = database.ensure_runtime_dirs
        self.session_patch = patch.object(database, "pyrogram_user_session_base", return_value=Path(self.temp.name) / "fake-session", create=True)
        self.session_patch.start()
        database.clear_saved_message_account()
        await database.close_db()
        database.DB_FILE = str(Path(self.temp.name) / "data.db")
        database.ensure_runtime_dirs = lambda: None
        await database.init_db()
        self.stack = ExitStack()
        self.stack.enter_context(patch.dict(history.sync_state, {"current": 0, "skipped": 0,
            "stop_requested": False, "mode": "API"}))
        self.stack.enter_context(patch.object(history, "build_link_rewrite_context", AsyncMock(return_value={})))
        self.stack.enter_context(patch.object(history, "rewrite_message_links", AsyncMock(side_effect=lambda text, *_: (text, 0))))
        self.stack.enter_context(patch.object(history, "resolve_reply_target", AsyncMock(return_value=None)))
        self.stack.enter_context(patch.object(history, "rewrite_media_group_captions", AsyncMock(
            side_effect=lambda source, target, group, **_: (["" for _ in group], False, 0))))
        self.stack.enter_context(patch.object(history.asyncio, "sleep", AsyncMock()))
        async def perform(action, **kwargs):
            return await action()
        self.stack.enter_context(patch.object(history, "execute_with_network_retry", perform))
        self.stack.enter_context(patch.object(history, "_execute_with_clone_retry_interruptibly", perform))
        async def selected(client, group, **kwargs):
            return await client.send_media_group(chat_id=kwargs["chat_id"], media=group)
        self.stack.enter_context(patch.object(history, "_copy_selected_api_media_group", selected))

    async def asyncTearDown(self):
        self.stack.close()
        await database.close_db()
        database.DB_FILE = self.original_db
        database.ensure_runtime_dirs = self.original_dirs
        database.clear_saved_message_account()
        self.session_patch.stop()
        self.temp.cleanup()

    def messages(self):
        return [SimpleNamespace(id=i, text=None, caption=None, photo=SimpleNamespace(file_id=f"photo-{i}"))
                for i in (1, 2)]

    async def test_single_topics_keeps_receipt_and_second_run_does_not_send(self):
        app = SimpleNamespace(copy_message=AsyncMock(side_effect=TypeError("Messages.__init__ missing topics")))
        msg = self.messages()[0]
        result = await history.sync_single_message("api", "user", app, app, 10, 20, msg, 0, False)
        self.assertEqual(result, "sent_unmapped")
        self.assertEqual((await database.get_message_delivery(10, 1, 20))["state"], "unconfirmed")
        again = await history.sync_single_message("api", "user", app, app, 10, 20, msg, 0, False)
        self.assertEqual(again, "sent_unmapped")
        self.assertEqual(app.copy_message.await_count, 1)
        self.assertFalse(await database.is_message_synced(10, 1, 20))

    async def test_group_partial_response_does_not_guess_mapping_and_blocks_resend(self):
        app = SimpleNamespace(send_media_group=AsyncMock(return_value=[SimpleNamespace(id=901)]))
        group = self.messages()
        result = await history.sync_media_group("api", "user", app, app, 10, 20, group, 0, False)
        self.assertEqual(result, "sent_unmapped")
        self.assertFalse(await database.is_message_synced(10, 1, 20))
        self.assertEqual((await database.get_message_delivery(10, 2, 20))["state"], "unconfirmed")
        again = await history.sync_media_group("api", "user", app, app, 10, 20, group, 0, False)
        self.assertEqual(again, "sent_unmapped")
        self.assertEqual(app.send_media_group.await_count, 1)

    async def test_clone_mapping_failure_never_retries_telegram_send(self):
        app = SimpleNamespace(send_media_group=AsyncMock(return_value=[SimpleNamespace(id=901), SimpleNamespace(id=902)]))
        group = self.messages()
        paths = []
        for msg in group:
            path = Path(self.temp.name) / f"{msg.id}.jpg"
            path.write_bytes(b"fake photo")
            paths.append(str(path))
        async def directly(coro, state):
            return await coro
        with patch.object(history, "_download_clone_media_item", AsyncMock(side_effect=paths)), \
             patch.object(history, "safe_execute", directly), \
             patch.object(history, "resolve_upload_target", AsyncMock(return_value={
                 "sender": "user", "client": app, "label": "test", "parse_mode": history.ParseMode.HTML})), \
             patch.object(database, "save_msg_mapping", AsyncMock(side_effect=sqlite3.OperationalError("disk I/O error"))):
            with self.assertRaises(history.SyncMappingPersistenceError):
                await history.sync_media_group("clone", "user", app, app, 10, 20, group, 0, False)
        self.assertEqual(app.send_media_group.await_count, 1)
        self.assertIsNotNone(await database.get_message_delivery(10, 1, 20))

    async def test_success_clears_receipt_and_force_is_explicit_override(self):
        await database.prepare_message_delivery(10, 20, [1])
        app = SimpleNamespace(copy_message=AsyncMock(return_value=SimpleNamespace(id=901)))
        result = await history.sync_single_message("api", "user", app, app, 10, 20, self.messages()[0], 0, True)
        self.assertEqual(result, "sent_mapped")
        self.assertEqual(await database.get_target_msg_id(10, 1, 20), 901)
        self.assertIsNone(await database.get_message_delivery(10, 1, 20))

    async def test_single_missing_id_is_persisted_as_unconfirmed(self):
        app = SimpleNamespace(copy_message=AsyncMock(return_value=None))
        result = await history.sync_single_message("api", "user", app, app, 10, 20, self.messages()[0], 0, False)
        self.assertEqual(result, "sent_unmapped")
        self.assertEqual((await database.get_message_delivery(10, 1, 20))["state"], "unconfirmed")
        self.assertEqual(app.copy_message.await_count, 1)

    async def test_clone_single_topics_never_retries_send(self):
        msg = SimpleNamespace(id=1, text=SimpleNamespace(html="text"), caption=None)
        app = SimpleNamespace(send_message=AsyncMock(side_effect=TypeError("Messages.__init__ missing topics")))
        result = await history.sync_single_message("clone", "user", app, app, 10, 20, msg, 0, False)
        self.assertEqual(result, "sent_unmapped")
        self.assertEqual(app.send_message.await_count, 1)
        self.assertEqual((await database.get_message_delivery(10, 1, 20))["state"], "unconfirmed")

    async def test_repeated_target_ids_do_not_create_false_mappings(self):
        app = SimpleNamespace(send_media_group=AsyncMock(return_value=[SimpleNamespace(id=901), SimpleNamespace(id=901)]))
        result = await history.sync_media_group("api", "user", app, app, 10, 20, self.messages(), 0, False)
        self.assertEqual(result, "sent_unmapped")
        self.assertFalse(await database.is_message_synced(10, 1, 20))
        self.assertEqual((await database.get_message_delivery(10, 1, 20))["state"], "unconfirmed")

    async def test_force_partial_group_and_log_failure_do_not_resend(self):
        app = SimpleNamespace(send_media_group=AsyncMock(return_value=[SimpleNamespace(id=901)]))
        with patch.object(database, "add_msg_log", AsyncMock(side_effect=sqlite3.OperationalError("log I/O error"))):
            with self.assertRaises(history.SyncDeliveryPendingError):
                await history.sync_media_group("api", "user", app, app, 10, 20, self.messages(), 0, True)
        self.assertEqual(app.send_media_group.await_count, 1)
        self.assertEqual((await database.get_message_delivery(10, 1, 20))["state"], "unconfirmed")


if __name__ == "__main__":
    unittest.main()
