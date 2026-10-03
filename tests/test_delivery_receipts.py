import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import database
from sync_worker.runtime import state


class DeliveryReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        task_temp = Path(__file__).resolve().parents[1] / "temp"
        task_temp.mkdir(exist_ok=True)
        self.temp_dir = tempfile.TemporaryDirectory(dir=task_temp)
        self.db_path = Path(self.temp_dir.name) / "receipts.db"
        self.original_db_file = database.DB_FILE
        self.original_ensure_dirs = database.ensure_runtime_dirs
        self.session_patch = patch.object(database, "pyrogram_user_session_base", return_value=Path(self.temp_dir.name) / "fake-session", create=True)
        self.session_patch.start()
        database.clear_saved_message_account()
        await database.close_db()
        database.DB_FILE = str(self.db_path)
        database.ensure_runtime_dirs = lambda: self.db_path.parent.mkdir(exist_ok=True)
        await database.init_db()

    async def asyncTearDown(self):
        await database.close_db()
        database.DB_FILE = self.original_db_file
        database.ensure_runtime_dirs = self.original_ensure_dirs
        database.clear_saved_message_account()
        self.session_patch.stop()
        self.temp_dir.cleanup()

    async def test_send_intent_survives_connection_restart(self):
        await database.prepare_message_delivery(-1001, -1002, [10, 11])
        await database.close_db()
        await database.init_db()
        self.assertEqual((await database.get_message_delivery(-1001, 10, -1002))["state"], "sending")
        self.assertEqual((await database.get_message_delivery(-1001, 11, -1002))["state"], "sending")

    async def test_unconfirmed_result_blocks_another_send(self):
        await database.prepare_message_delivery(-1001, -1002, [10])
        await database.mark_message_delivery_unconfirmed(-1001, -1002, [10], "topics parse error")
        row = await database.get_message_delivery(-1001, 10, -1002)
        self.assertEqual((row["state"], row["reason"]), ("unconfirmed", "topics parse error"))
        with self.assertRaises(database.MessageDeliveryPendingError):
            await database.prepare_message_delivery(-1001, -1002, [10])

    async def test_prepare_checks_entire_group_before_writing(self):
        await database.prepare_message_delivery(-1001, -1002, [10])
        with self.assertRaises(database.MessageDeliveryPendingError):
            await database.prepare_message_delivery(-1001, -1002, [11, 10])
        self.assertIsNone(await database.get_message_delivery(-1001, 11, -1002))

    async def test_confirm_commits_mapping_and_removes_intent(self):
        await database.prepare_message_delivery(-1001, -1002, [10])
        await database.save_msg_mapping(-1001, 10, -1002, 900)
        self.assertTrue(await database.is_message_synced(-1001, 10, -1002))
        self.assertIsNone(await database.get_message_delivery(-1001, 10, -1002))

    async def test_confirmation_commit_failure_rolls_back_both_changes(self):
        await database.prepare_message_delivery(-1001, -1002, [10])
        conn = await database._get_connection()
        with patch.object(conn, "commit", AsyncMock(side_effect=sqlite3.OperationalError("disk I/O error"))):
            with self.assertRaises(sqlite3.OperationalError):
                await database.save_msg_mapping(-1001, 10, -1002, 900)
        self.assertFalse(await database.is_message_synced(-1001, 10, -1002))
        self.assertEqual((await database.get_message_delivery(-1001, 10, -1002))["state"], "sending")

    async def test_saved_receipts_are_isolated_by_owner(self):
        await database.prepare_message_delivery(-1001, -1, [10], owner_user_id=111)
        await database.mark_message_delivery_unconfirmed(-1001, -1, [10], "first owner", owner_user_id=111)
        await database.prepare_message_delivery(-1001, -1, [10], owner_user_id=222)
        self.assertEqual((await database.get_message_delivery(-1001, 10, -1, owner_user_id=111))["state"], "unconfirmed")
        self.assertEqual((await database.get_message_delivery(-1001, 10, -1, owner_user_id=222))["state"], "sending")
        await database.release_message_delivery(-1001, -1, [10], owner_user_id=222)
        self.assertIsNone(await database.get_message_delivery(-1001, 10, -1, owner_user_id=222))
        self.assertIsNotNone(await database.get_message_delivery(-1001, 10, -1, owner_user_id=111))

    async def test_force_override_replaces_pending_reason(self):
        await database.prepare_message_delivery(-1001, -1002, [10])
        await database.mark_message_delivery_unconfirmed(-1001, -1002, [10], "unknown response")
        await database.prepare_message_delivery(-1001, -1002, [10], force=True)
        row = await database.get_message_delivery(-1001, 10, -1002)
        self.assertEqual((row["state"], row["reason"]), ("sending", ""))

    async def test_failed_intent_commit_does_not_leave_a_receipt(self):
        conn = await database._get_connection()
        with patch.object(conn, "commit", AsyncMock(side_effect=sqlite3.OperationalError("disk I/O error"))):
            with self.assertRaises(sqlite3.OperationalError):
                await database.prepare_message_delivery(-1001, -1002, [10, 11])
        self.assertIsNone(await database.get_message_delivery(-1001, 10, -1002))
        self.assertIsNone(await database.get_message_delivery(-1001, 11, -1002))

    async def test_concurrent_preparations_only_reserve_once(self):
        results = await asyncio.gather(
            database.prepare_message_delivery(-1001, -1002, [10]),
            database.prepare_message_delivery(-1001, -1002, [10]),
            return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(result, database.MessageDeliveryPendingError) for result in results), 1)
        self.assertEqual(sum(result is None for result in results), 1)

    async def test_confirmation_removes_only_matching_owner_receipt(self):
        await database.prepare_message_delivery(-1001, -1, [10], owner_user_id=111)
        await database.prepare_message_delivery(-1001, -1, [10], owner_user_id=222)
        await database.save_msg_mapping(-1001, 10, -1, 900, owner_user_id=111)
        self.assertIsNone(await database.get_message_delivery(-1001, 10, -1, owner_user_id=111))
        self.assertIsNotNone(await database.get_message_delivery(-1001, 10, -1, owner_user_id=222))

    async def test_reinitialization_preserves_existing_mappings_and_receipts(self):
        await database.save_msg_mapping(-1001, 10, -1002, 900)
        await database.init_db()
        self.assertEqual(await database.get_target_msg_id(-1001, 10, -1002), 900)
        await database.prepare_message_delivery(-1001, -1002, [11])
        await database.close_db()
        await database.init_db()
        self.assertEqual(await database.get_target_msg_id(-1001, 10, -1002), 900)
        self.assertIsNotNone(await database.get_message_delivery(-1001, 11, -1002))

    async def test_guard_prepares_once_for_in_operation_retries(self):
        guard = state.DeliveryGuard(-1001, -1002, [10])
        action = AsyncMock(side_effect=[RuntimeError("temporary send failure"), "sent"])
        with patch.object(database, "prepare_message_delivery", wraps=database.prepare_message_delivery) as prepare:
            with self.assertRaises(RuntimeError):
                await guard.send(action)
            self.assertEqual(await guard.send(action), "sent")
        self.assertEqual(prepare.await_count, 1)
        self.assertEqual(action.await_count, 2)

    async def test_wrapped_client_prepares_once_and_reuses_successful_send(self):
        async def check_receipt(*args, **kwargs):
            self.assertIsNotNone(await database.get_message_delivery(-1001, 10, -1002))
            return "sent"

        client = SimpleNamespace(send_message=AsyncMock(side_effect=check_receipt), copy_message=AsyncMock(side_effect=check_receipt))
        guard = state.DeliveryGuard(-1001, -1002, [10])
        wrapped = guard.wrap_client(client)
        with patch.object(database, "prepare_message_delivery", wraps=database.prepare_message_delivery) as prepare:
            self.assertEqual(await wrapped.send_message(-1002, "text"), "sent")
            self.assertEqual(await wrapped.copy_message(-1002, -1001, 10), "sent")
        self.assertEqual(prepare.await_count, 1)
        client.send_message.assert_awaited_once_with(-1002, "text")
        client.copy_message.assert_not_awaited()

    async def test_guard_reuses_successful_result_until_released(self):
        first_result = object()
        second_result = object()
        guard = state.DeliveryGuard(-1001, -1002, [10])
        action = AsyncMock(return_value=first_result)
        retry_action = AsyncMock(return_value=second_result)
        self.assertIs(await guard.send(action), first_result)
        self.assertIs(await guard.send(retry_action), first_result)
        action.assert_awaited_once()
        retry_action.assert_not_awaited()
        await guard.release()
        self.assertIs(await guard.send(retry_action), second_result)
        retry_action.assert_awaited_once()

    async def test_guard_also_caches_a_none_result(self):
        guard = state.DeliveryGuard(-1001, -1002, [10])
        action = AsyncMock(return_value=None)
        self.assertIsNone(await guard.send(action))
        self.assertIsNone(await guard.send(action))
        action.assert_awaited_once()

    async def test_wrapped_copy_checks_pending_before_invocation(self):
        await database.prepare_message_delivery(-1001, -1002, [10])
        client = SimpleNamespace(copy_message=AsyncMock())
        wrapped = state.DeliveryGuard(-1001, -1002, [10]).wrap_client(client)
        with self.assertRaises(state.SyncDeliveryPendingError):
            await wrapped.copy_message(-1002, -1001, 10)
        client.copy_message.assert_not_awaited()

    async def test_wrapped_client_does_not_reserve_for_reads_or_authentication(self):
        client = SimpleNamespace(me=SimpleNamespace(id=1), get_messages=AsyncMock(), send_code=AsyncMock(), send_recovery_code=AsyncMock(), invoke=AsyncMock())
        wrapped = state.DeliveryGuard(-1001, -1002, [10]).wrap_client(client)
        self.assertIs(wrapped.me, client.me)
        await wrapped.get_messages(-1001, [10])
        await wrapped.send_code("123")
        await wrapped.send_recovery_code()
        await wrapped.invoke(type("GetHistory", (), {})())
        self.assertIsNone(await database.get_message_delivery(-1001, 10, -1002))

    async def test_wrapped_raw_send_checks_pending_before_invocation(self):
        await database.prepare_message_delivery(-1001, -1002, [10])
        client = SimpleNamespace(invoke=AsyncMock())
        wrapped = state.DeliveryGuard(-1001, -1002, [10]).wrap_client(client)
        with self.assertRaises(state.SyncDeliveryPendingError):
            await wrapped.invoke(type("SendMultiMedia", (), {})())
        client.invoke.assert_not_awaited()

    async def test_guard_never_calls_sender_if_intent_commit_fails(self):
        conn = await database._get_connection()
        action = AsyncMock()
        with patch.object(conn, "commit", AsyncMock(side_effect=sqlite3.OperationalError("disk I/O error"))):
            with self.assertRaises(sqlite3.OperationalError):
                await state.DeliveryGuard(-1001, -1002, [10]).send(action)
        action.assert_not_awaited()
        self.assertIsNone(await database.get_message_delivery(-1001, 10, -1002))

    async def test_guard_unconfirmed_result_blocks_reuse_until_release(self):
        guard = state.DeliveryGuard(-1001, -1002, [10])
        await guard.send(AsyncMock(return_value="sent"))
        await guard.mark_unconfirmed("unknown result")
        action = AsyncMock()
        with self.assertRaises(state.SyncDeliveryPendingError):
            await guard.send(action)
        action.assert_not_awaited()
        self.assertEqual((await database.get_message_delivery(-1001, 10, -1002))["reason"], "unknown result")
        await guard.release()
        self.assertIsNone(await database.get_message_delivery(-1001, 10, -1002))

    async def test_force_guard_cannot_resend_after_becoming_unconfirmed(self):
        guard = state.DeliveryGuard(-1001, -1002, [10, 11], force_send=True)
        await guard.send(AsyncMock(return_value=[SimpleNamespace(id=900)]))
        await guard.mark_unconfirmed("short group response")
        retry = AsyncMock()
        with self.assertRaises(state.SyncDeliveryPendingError) as caught:
            await guard.send(retry)
        self.assertEqual(caught.exception.msg_ids, [10, 11])
        retry.assert_not_awaited()
        self.assertEqual((await database.get_message_delivery(-1001, 10, -1002))["state"], "unconfirmed")

        replacement = state.DeliveryGuard(-1001, -1002, [10, 11], force_send=True)
        fresh_send = AsyncMock(return_value="explicit new delivery")
        self.assertEqual(await replacement.send(fresh_send), "explicit new delivery")
        fresh_send.assert_awaited_once()
        self.assertEqual((await database.get_message_delivery(-1001, 10, -1002))["state"], "sending")

    async def test_failed_unconfirmed_commit_still_blocks_force_guard_reuse(self):
        guard = state.DeliveryGuard(-1001, -1002, [10], force_send=True)
        await guard.send(AsyncMock(return_value=SimpleNamespace(id=900)))
        conn = await database._get_connection()
        with patch.object(conn, "commit", AsyncMock(side_effect=sqlite3.OperationalError("disk I/O error"))):
            with self.assertRaises(sqlite3.OperationalError):
                await guard.mark_unconfirmed("response missing member")
        self.assertEqual((await database.get_message_delivery(-1001, 10, -1002))["state"], "sending")
        retry = AsyncMock()
        with self.assertRaises(state.SyncDeliveryPendingError):
            await guard.send(retry)
        retry.assert_not_awaited()

    async def test_failed_release_does_not_remove_local_terminal_state(self):
        guard = state.DeliveryGuard(-1001, -1002, [10], force_send=True)
        await guard.send(AsyncMock(return_value=SimpleNamespace(id=900)))
        await guard.mark_unconfirmed("response missing member")
        conn = await database._get_connection()
        with patch.object(conn, "commit", AsyncMock(side_effect=sqlite3.OperationalError("disk I/O error"))):
            with self.assertRaises(sqlite3.OperationalError):
                await guard.release()
        retry = AsyncMock()
        with self.assertRaises(state.SyncDeliveryPendingError):
            await guard.send(retry)
        retry.assert_not_awaited()

    async def test_guard_pending_receipt_never_calls_sender(self):
        await database.prepare_message_delivery(-1001, -1002, [10])
        action = AsyncMock()
        with self.assertRaises(state.SyncDeliveryPendingError) as caught:
            await state.DeliveryGuard(-1001, -1002, [10, 11]).send(action)
        self.assertEqual(caught.exception.msg_ids, [10])
        action.assert_not_awaited()
        self.assertIsNone(await database.get_message_delivery(-1001, 11, -1002))

    async def test_guard_uses_owner_snapshot_during_account_change(self):
        with patch.object(database, "get_mapping_owner_id", return_value=111) as get_owner:
            guard = state.DeliveryGuard(-1001, -1, [10])
            get_owner.return_value = 222
            await guard.send(AsyncMock(return_value="sent"))
        self.assertEqual(guard.owner_user_id, 111)
        self.assertIsNotNone(await database.get_message_delivery(-1001, 10, -1, owner_user_id=111))
        self.assertIsNone(await database.get_message_delivery(-1001, 10, -1, owner_user_id=222))

    async def test_pending_is_not_logged_as_an_ordinary_duplicate(self):
        await database.prepare_message_delivery(-1001, -1002, [10])
        with patch.dict(state.sync_state, {"current": 0, "skipped": 0}), \
             patch.object(database, "is_message_synced", AsyncMock()) as check_mapping, \
             patch.object(database, "add_msg_log", AsyncMock()) as add_log:
            with self.assertRaises(state.SyncDeliveryPendingError):
                await state.update_state_and_check_skip(-1001, -1002, 10, "message")
            self.assertEqual(state.sync_state["skipped"], 0)
        check_mapping.assert_not_awaited()
        add_log.assert_not_awaited()

    async def test_mapping_failure_retries_only_database_and_keeps_intent(self):
        await database.prepare_message_delivery(-1001, -1002, [10])
        with patch.object(database, "save_msg_mapping", AsyncMock(side_effect=sqlite3.OperationalError("database is locked"))) as save, \
             patch.object(state.asyncio, "sleep", AsyncMock()):
            with self.assertRaises(state.SyncMappingPersistenceError):
                await state.record_success(-1001, -1002, 10, 900)
        self.assertEqual(save.await_count, 3)
        self.assertIsNotNone(await database.get_message_delivery(-1001, 10, -1002))

    async def test_mapping_storage_retry_can_recover(self):
        with patch.object(database, "save_msg_mapping", AsyncMock(side_effect=[sqlite3.OperationalError("database is locked"), None])) as save, \
             patch.object(state.asyncio, "sleep", AsyncMock()):
            await state.record_success(-1001, -1002, 10, 900)
        self.assertEqual(save.await_count, 2)

    async def test_non_database_failure_does_not_retry(self):
        with patch.object(database, "save_msg_mapping", AsyncMock(side_effect=ValueError("invalid mapping"))) as save:
            with self.assertRaises(ValueError):
                await state.record_success(-1001, -1002, 10, 900)
        self.assertEqual(save.await_count, 1)
