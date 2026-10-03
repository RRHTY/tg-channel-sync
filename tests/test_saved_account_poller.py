import asyncio
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services import sync_services
from sync_worker.realtime import public_poller


class SavedAccountPollerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(public_poller.db, "_get_connection", AsyncMock(side_effect=AssertionError("Real database access is disabled"))))
        self.stack.enter_context(patch.object(public_poller, "get_config", return_value={"sync": {}}))
        self.stack.enter_context(patch.dict("sync_worker.runtime.sync_state", {"is_syncing": False, "mode": ""}))
        self.stack.enter_context(patch.object(sync_services, "_saved_messages_state_lock", asyncio.Lock()))
        self.stack.enter_context(patch.object(sync_services, "_saved_messages_active_operations", 0))
        self.stack.enter_context(patch.object(sync_services, "_saved_messages_account_switching", False))
        self.owner = self.stack.enter_context(patch.object(public_poller.db, "get_mapping_owner_id", return_value=111))
        self.send = self.stack.enter_context(patch("sync_worker.clone.process.sync_single_message", AsyncMock(return_value="sent_mapped")))
        self.checkpoint = self.stack.enter_context(patch.object(public_poller.db, "update_public_user_poll_position", AsyncMock()))
        self.enabled = self.stack.enter_context(patch.object(public_poller.db, "is_channel_mapping_enabled", AsyncMock(return_value=True)))
        self.stack.enter_context(patch.object(public_poller, "load_public_channel_new_messages", AsyncMock(return_value=[SimpleNamespace(id=10, media_group_id=None)])))
        self.user = SimpleNamespace(is_initialized=True, me=SimpleNamespace(id=111))
        self.group = {"source_id": -1001, "source_ref": "source", "last_polled_message_id": 0, "mappings": [{"target_id": -1, "target_type": "saved", "owner_user_id": 111, "last_polled_message_id": 0}]}

    def tearDown(self):
        self.stack.close()

    async def test_stale_user_client_is_rejected_before_send_and_checkpoint(self):
        self.owner.return_value = 222
        with self.assertRaisesRegex(ValueError, "账号|登录"):
            await public_poller.process_public_channel_mapping_group(None, self.user, self.group)
        self.send.assert_not_awaited()
        self.checkpoint.assert_not_awaited()
        self.enabled.assert_not_awaited()

    async def test_stale_account_cursor_snapshot_is_rejected(self):
        self.group["mappings"][0]["owner_user_id"] = 222
        with self.assertRaisesRegex(ValueError, "账号|登录"):
            await public_poller.process_public_channel_mapping_group(None, self.user, self.group)
        self.send.assert_not_awaited()
        self.checkpoint.assert_not_awaited()

    async def test_uninitialized_user_client_is_rejected(self):
        self.user.is_initialized = False
        with self.assertRaisesRegex(ValueError, "账号|登录"):
            await public_poller.process_public_channel_mapping_group(None, self.user, self.group)
        self.send.assert_not_awaited()
        self.checkpoint.assert_not_awaited()

    async def test_saved_mapping_without_owner_snapshot_is_rejected(self):
        del self.group["mappings"][0]["owner_user_id"]
        with self.assertRaisesRegex(ValueError, "账号|登录"):
            await public_poller.process_public_channel_mapping_group(None, self.user, self.group)
        self.send.assert_not_awaited()
        self.checkpoint.assert_not_awaited()

    async def test_stale_owner_is_checked_before_skipping_advanced_cursor(self):
        self.group["mappings"][0].update(owner_user_id=222, last_polled_message_id=20)
        with self.assertRaisesRegex(ValueError, "账号|登录"):
            await public_poller.process_public_channel_mapping_group(None, self.user, self.group)
        self.enabled.assert_not_awaited()
        self.send.assert_not_awaited()
        self.checkpoint.assert_not_awaited()

    async def test_switch_is_blocked_while_mapping_is_checked(self):
        async def enabled(*args, **kwargs):
            with self.assertRaisesRegex(ValueError, "收藏夹消息仍在处理中"):
                await sync_services.begin_saved_messages_account_switch()
            return True

        self.enabled.side_effect = enabled
        await public_poller.process_public_channel_mapping_group(None, self.user, self.group)
        self.send.assert_awaited_once()
        self.checkpoint.assert_awaited_once()
        self.assertEqual(sync_services._saved_messages_active_operations, 0)

    async def test_switch_is_blocked_until_saved_checkpoint_commit_finishes(self):
        checkpoint_started = asyncio.Event()
        allow_checkpoint = asyncio.Event()

        async def checkpoint(*args, **kwargs):
            checkpoint_started.set()
            await allow_checkpoint.wait()

        self.checkpoint.side_effect = checkpoint
        polling = asyncio.create_task(public_poller.process_public_channel_mapping_group(None, self.user, self.group))
        switch_acquired = False
        try:
            await asyncio.wait_for(checkpoint_started.wait(), timeout=1)
            with self.assertRaisesRegex(ValueError, "收藏夹消息仍在处理中"):
                await sync_services.begin_saved_messages_account_switch()
                switch_acquired = True
        finally:
            if switch_acquired:
                await sync_services.finish_saved_messages_account_switch()
            allow_checkpoint.set()
            await polling
        self.assertEqual(sync_services._saved_messages_active_operations, 0)

    async def test_failed_send_does_not_advance_saved_checkpoint_and_releases_guard(self):
        self.send.side_effect = RuntimeError("send failed")
        with self.assertRaisesRegex(RuntimeError, "send failed"):
            await public_poller.process_public_channel_mapping_group(None, self.user, self.group)
        self.checkpoint.assert_not_awaited()
        self.assertEqual(sync_services._saved_messages_active_operations, 0)

    async def test_cancellation_during_checkpoint_releases_guard(self):
        checkpoint_started = asyncio.Event()

        async def checkpoint(*args, **kwargs):
            checkpoint_started.set()
            await asyncio.Event().wait()

        self.checkpoint.side_effect = checkpoint
        polling = asyncio.create_task(public_poller.process_public_channel_mapping_group(None, self.user, self.group))
        try:
            await asyncio.wait_for(checkpoint_started.wait(), timeout=1)
        finally:
            polling.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await polling
        self.assertEqual(sync_services._saved_messages_active_operations, 0)
        await sync_services.begin_saved_messages_account_switch()
        await sync_services.finish_saved_messages_account_switch()

    async def test_channel_target_keeps_delivery_arguments_and_needs_no_saved_owner(self):
        self.group["mappings"] = [{"target_id": -1002}]
        bot = object()
        user = object()
        await public_poller.process_public_channel_mapping_group(bot, user, self.group)
        self.owner.assert_not_called()
        self.send.assert_awaited_once()
        self.assertEqual(self.send.await_args.args[0:4], ("api", "user", user, bot))
        self.assertEqual(self.send.await_args.args[4:6], (-1001, -1002))
        self.assertEqual(self.send.await_args.kwargs, {
            "hash_perturb": False,
            "clone_fallback_to_user": True,
            "include_external_source_header": False,
            "source_username_override": "source",
            "chat_id": -1002,
        })
        self.checkpoint.assert_awaited_once_with(-1001, "source", 10, target_id=-1002)
        self.assertEqual(sync_services._saved_messages_active_operations, 0)
