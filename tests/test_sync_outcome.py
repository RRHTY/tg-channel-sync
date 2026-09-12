import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sync_worker.clone import process as history
from sync_worker.json_import import process as importer


class SyncOutcomeTests(unittest.IsolatedAsyncioTestCase):
    async def run_history(self, results, *, fetch_error=None, settings_error=None):
        messages = [SimpleNamespace(id=i, empty=False, media_group_id=None) for i in (1, 2)]
        with ExitStack() as stack:
            stack.enter_context(patch.dict(history.sync_state, {"is_syncing": False, "stop_requested": False}))
            stack.enter_context(patch.object(history.db, "get_all_settings", AsyncMock(return_value={}, side_effect=settings_error)))
            stack.enter_context(patch.object(history.db, "add_log", AsyncMock()))
            stack.enter_context(patch.object(history, "get_config", return_value={}))
            stack.enter_context(patch.object(history, "resolve_chat_id", AsyncMock(return_value=-100123)))
            stack.enter_context(patch.object(history, "_safe_get_messages", AsyncMock(return_value=messages, side_effect=fetch_error)))
            stack.enter_context(patch.object(history, "get_msg_meta", return_value=("text", "sync_text")))
            stack.enter_context(patch.object(history, "sync_single_message", AsyncMock(side_effect=results)))
            await history.process_master_sync("api", "user", "source", "target", .5, 1, 2, "")
            self.assertFalse(history.sync_state["is_syncing"])
            return history.sync_state.get("result", {})

    async def test_one_failed_message_is_partial_failure(self):
        result = await self.run_history(["sent_mapped", "failed"])
        self.assertEqual(result.get("status"), "partial_failed")
        self.assertEqual((result["sent"], result["failed"]), (1, 1))

    async def test_all_failed_messages_are_failure(self):
        self.assertEqual((await self.run_history(["failed", "failed"])).get("status"), "failed")

    async def test_success_and_duplicate_are_completed(self):
        result = await self.run_history(["sent_mapped", "skipped"])
        self.assertEqual(result.get("status"), "completed")
        self.assertEqual((result["sent"], result["skipped"]), (1, 1))

    async def test_failed_fetch_is_not_completed(self):
        result = await self.run_history([], fetch_error=RuntimeError("fetch failed"))
        self.assertEqual(result.get("status"), "failed")
        self.assertEqual(result["failed_batches"], 1)

    async def test_initialization_failure_releases_state(self):
        result = await self.run_history([], settings_error=RuntimeError("database unavailable"))
        self.assertEqual(result.get("status"), "failed")

    async def test_missing_json_is_failed_at_master_boundary(self):
        with patch.object(history.db, "get_all_settings", AsyncMock(return_value={})), \
             patch.object(history.db, "add_log", AsyncMock()), \
             patch.object(history, "get_config", return_value={}), \
             patch.object(history, "resolve_chat_id", AsyncMock(return_value=-100123)), \
             patch.object(importer.os.path, "exists", return_value=False), \
             patch.dict(history.sync_state, {"is_syncing": False}):
            await history.process_master_sync("json", "bot", "", "target", .5, 0, 0, "missing.json")
            self.assertEqual(history.sync_state.get("result", {}).get("status"), "failed")

    async def test_cancelled_task_is_stopped(self):
        import asyncio
        result = await self.run_history([asyncio.CancelledError()])
        self.assertEqual(result["status"], "stopped")

    async def test_unmapped_send_is_not_reported_as_success(self):
        result = await self.run_history(["sent_unmapped", "skipped"])
        self.assertEqual(result["status"], "partial_failed")
        self.assertEqual((result["sent"], result["unmapped"]), (0, 1))

    async def test_invalid_json_raises_instead_of_returning_success(self):
        from unittest.mock import mock_open
        for content in ("{", '[]', '{"messages": {}}', '{"messages": [null]}'):
            with self.subTest(content=content), \
                 patch('services.sync_validation.Path.is_file', return_value=True), \
                 patch('services.sync_validation.Path.open', mock_open(read_data=content)):
                with self.assertRaises(importer.JsonSyncFatalError):
                    await importer.process_json_sync("bot", "target", "invalid.json", .5, False)

    async def test_json_partial_failure_counts_sent_and_failed(self):
        import json
        from unittest.mock import mock_open
        data = {"id": 123, "type": "private_channel", "messages": [
            {"id": 1, "type": "message", "text": "hello"},
            {"id": 2, "type": "message", "text": "world"},
        ]}
        with ExitStack() as stack:
            stack.enter_context(patch.dict(importer.sync_state, {"stop_requested": False}))
            stack.enter_context(patch.object(importer, "load_json_export", return_value=data))
            for name, value in (("resolve_chat_id", -100456), ("build_link_rewrite_context", {}),
                                ("resolve_reply_target", None), ("update_state_and_check_skip", False),
                                ("record_success", None), ("log_sync_error", None),
                                ("_select_json_upload_target", {"sender": "user"})):
                stack.enter_context(patch.object(importer, name, AsyncMock(return_value=value)))
            stack.enter_context(patch.object(importer, "get_config", return_value={}))
            stack.enter_context(patch.object(importer.db, "get_all_settings", AsyncMock(return_value={})))
            stack.enter_context(patch.object(importer.db, "add_msg_log", AsyncMock()))
            stack.enter_context(patch.object(importer.db, "apply_message_filters", AsyncMock(side_effect=lambda text, *_: (False, text))))
            stack.enter_context(patch.object(importer, "rewrite_message_links", AsyncMock(side_effect=lambda text, *_: (text, 0))))
            stack.enter_context(patch.object(importer, "_send_json_text_via_user", AsyncMock(side_effect=[SimpleNamespace(id=101), RuntimeError("send failed")])))
            outcome = await importer.process_json_sync("user", "target", "result.json", 0, False)
        self.assertEqual((outcome.sent, outcome.failed), (1, 1))
        self.assertEqual(outcome.snapshot()["status"], "partial_failed")

    async def test_incomplete_long_text_does_not_record_success(self):
        import json
        from unittest.mock import mock_open
        data = {"messages": [{"id": 1, "type": "message", "text": "x" * 5000}]}
        bot = SimpleNamespace(send_message=AsyncMock())
        target = {"sender": "bot", "client": bot}
        with ExitStack() as stack:
            stack.enter_context(patch.dict(importer.sync_state, {"stop_requested": False}))
            stack.enter_context(patch.object(importer, "load_json_export", return_value=data))
            for name, value in (("resolve_chat_id", -100456), ("build_link_rewrite_context", {}),
                                ("resolve_reply_target", None), ("update_state_and_check_skip", False),
                                ("log_sync_error", None), ("_select_json_upload_target", target)):
                stack.enter_context(patch.object(importer, name, AsyncMock(return_value=value)))
            record = stack.enter_context(patch.object(importer, "record_success", AsyncMock()))
            stack.enter_context(patch.object(importer, "get_config", return_value={}))
            stack.enter_context(patch.object(importer.db, "get_all_settings", AsyncMock(return_value={})))
            stack.enter_context(patch.object(importer.db, "add_msg_log", AsyncMock()))
            stack.enter_context(patch.object(importer.db, "apply_message_filters", AsyncMock(side_effect=lambda text, *_: (False, text))))
            stack.enter_context(patch.object(importer, "rewrite_message_links", AsyncMock(side_effect=lambda text, *_: (text, 0))))
            stack.enter_context(patch.object(importer.bot_engine, "mark_upload_bot_cooldown", AsyncMock()))
            stack.enter_context(patch.object(importer, "execute_with_network_retry", AsyncMock(side_effect=[
                SimpleNamespace(message_id=101), RuntimeError("retry after 1"),
                RuntimeError("retry after 1"), RuntimeError("retry after 1"),
            ])))
            outcome = await importer.process_json_sync("bot", "target", "result.json", 0, False, clone_fallback_to_user=False)
        record.assert_not_awaited()
        self.assertEqual((outcome.sent, outcome.failed), (0, 1))
        self.assertEqual(outcome.snapshot()["status"], "failed")
