import asyncio
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import bot_engine
from services import sync_services


def fake_user_client(account_id=101, *, authorized=True):
    return SimpleNamespace(
        is_initialized=False,
        is_connected=True,
        me=None,
        connect=AsyncMock(return_value=authorized),
        invoke=AsyncMock(),
        get_me=AsyncMock(return_value=SimpleNamespace(id=account_id, first_name="User", username="user")),
        initialize=AsyncMock(),
        disconnect=AsyncMock(),
        stop=AsyncMock(),
        stop_transmission=Mock(),
        send_code=AsyncMock(return_value=SimpleNamespace(phone_code_hash="code-hash")),
        sign_in=AsyncMock(return_value=SimpleNamespace(id=999)),
        check_password=AsyncMock(return_value=SimpleNamespace(id=999)),
    )


class IsolatedSavedAccountTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original_client = bot_engine.pyro_user_app
        self.original_auth_state = dict(bot_engine.user_auth_state)
        bot_engine.pyro_user_app = None
        bot_engine._clear_user_auth_state()
        self.stack = ExitStack()
        self.temp_dir = self.stack.enter_context(tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / "temp"))
        self.real_factory = bot_engine.init_user_client
        self.stack.enter_context(patch.object(bot_engine, "init_user_client", Mock(side_effect=AssertionError("Supply an isolated fake user client"))))
        self.stack.enter_context(patch.object(bot_engine, "Client", Mock(side_effect=AssertionError("Real Telegram clients are disabled"))))
        self.stack.enter_context(patch.object(bot_engine, "pyrogram_user_session_base", return_value=Path(self.temp_dir) / "session"))
        self.stack.enter_context(patch.object(bot_engine.db, "_get_connection", AsyncMock(side_effect=AssertionError("Real database access is disabled"))))
        self.bind_owner = self.stack.enter_context(patch.object(bot_engine.db, "bind_saved_message_account", AsyncMock(), create=True))
        self.clear_owner = self.stack.enter_context(patch.object(bot_engine.db, "clear_saved_message_account", Mock(), create=True))
        self.stack.enter_context(patch.object(bot_engine, "sync_state", {"is_syncing": False}))
        self.stack.enter_context(patch.object(sync_services, "_saved_messages_state_lock", asyncio.Lock()))
        self.stack.enter_context(patch.object(sync_services, "_saved_messages_active_operations", 0))
        self.stack.enter_context(patch.object(sync_services, "_saved_messages_account_switching", False))

    def tearDown(self):
        self.stack.close()
        bot_engine.pyro_user_app = self.original_client
        bot_engine.user_auth_state.update(self.original_auth_state)


class SavedAccountLifecycleTests(IsolatedSavedAccountTests):
    def test_client_factory_does_not_publish_unverified_client(self):
        existing_client = object()
        bot_engine.pyro_user_app = existing_client
        candidate = fake_user_client()
        with patch.object(bot_engine, "_telegram_config", return_value={"api_id": 1, "api_hash": "hash"}), \
             patch.object(bot_engine, "_proxy_config", return_value={}), \
             patch.object(bot_engine, "Client", return_value=candidate):
            self.assertIs(self.real_factory(), candidate)
        self.assertIs(bot_engine.pyro_user_app, existing_client)

    async def test_finalize_binds_verified_owner_before_initialize_and_publish(self):
        client = fake_user_client(202)
        events = []

        async def get_me():
            events.append("get_me")
            return SimpleNamespace(id=202)

        async def bind_owner(account_id):
            self.assertEqual(account_id, 202)
            self.assertIsNone(bot_engine.pyro_user_app)
            events.append("bind")

        async def initialize():
            self.assertIsNone(bot_engine.pyro_user_app)
            events.append("initialize")

        client.get_me.side_effect = get_me
        client.initialize.side_effect = initialize
        self.bind_owner.side_effect = bind_owner
        me = await bot_engine._finalize_user_client(client)
        self.assertEqual(events, ["get_me", "bind", "initialize"])
        self.assertEqual(me.id, 202)
        self.assertIs(bot_engine.pyro_user_app, client)

    async def test_finalize_failure_unbinds_and_disposes_candidate(self):
        client = fake_user_client()
        client.initialize.side_effect = RuntimeError("initialize failed")
        bot_engine.user_auth_state["client"] = client
        with self.assertRaisesRegex(RuntimeError, "initialize failed"):
            await bot_engine._finalize_user_client(client)
        self.bind_owner.assert_awaited_once_with(101)
        self.clear_owner.assert_called_once()
        self.assertIsNone(bot_engine.pyro_user_app)
        self.assertIsNone(bot_engine.user_auth_state["client"])
        client.disconnect.assert_awaited_once()

    async def test_startup_binds_existing_authorized_session(self):
        client = fake_user_client(303)
        with patch.object(bot_engine, "init_user_client", return_value=client):
            me = await bot_engine.start_user_client_if_authorized()
        self.assertEqual(me.id, 303)
        self.bind_owner.assert_awaited_once_with(303)
        self.assertIs(bot_engine.pyro_user_app, client)

    async def test_startup_connect_failure_clears_owner_and_disposes(self):
        client = fake_user_client()
        client.connect.side_effect = RuntimeError("connect failed")
        with patch.object(bot_engine, "init_user_client", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "connect failed"):
                await bot_engine.start_user_client_if_authorized()
        self.clear_owner.assert_called_once()
        client.disconnect.assert_awaited_once()
        self.assertIsNone(bot_engine.pyro_user_app)

    async def test_close_unpublishes_owner_and_client_before_disposal(self):
        client = fake_user_client()
        bot_engine.pyro_user_app = client

        async def dispose(candidate):
            if candidate is None:
                return
            self.assertIs(candidate, client)
            self.assertIsNone(bot_engine.pyro_user_app)
            self.clear_owner.assert_called_once()

        with patch.object(bot_engine, "_dispose_client", AsyncMock(side_effect=dispose)):
            await bot_engine.close_user_client()

    async def test_begin_auth_is_rejected_while_sync_is_running(self):
        with patch.object(bot_engine, "has_user_api_credentials", return_value=True), \
             patch.object(bot_engine, "sync_state", {"is_syncing": True}), \
             patch.object(bot_engine, "init_user_client", return_value=fake_user_client()), \
             patch.object(bot_engine, "close_user_client", AsyncMock()) as close:
            with self.assertRaisesRegex(ValueError, "先中断当前同步任务"):
                await bot_engine.begin_user_auth("123")
        close.assert_not_awaited()

    async def test_begin_auth_is_rejected_during_saved_message_operation(self):
        operation = await sync_services.begin_saved_messages_operation("saved", -1)
        try:
            with patch.object(bot_engine, "has_user_api_credentials", return_value=True), \
                 patch.object(bot_engine, "close_user_client", AsyncMock()) as close, \
                 patch.object(bot_engine, "init_user_client", return_value=fake_user_client()):
                with self.assertRaisesRegex(ValueError, "收藏夹消息仍在处理中"):
                    await bot_engine.begin_user_auth("123")
            close.assert_not_awaited()
        finally:
            await sync_services.finish_saved_messages_operation(operation)

    async def test_send_code_holds_account_switch_guard(self):
        client = fake_user_client(authorized=False)

        async def send_code(phone):
            operation = False
            try:
                with self.assertRaisesRegex(ValueError, "辅助账号正在切换"):
                    operation = await sync_services.begin_saved_messages_operation("saved", -1)
            finally:
                await sync_services.finish_saved_messages_operation(operation)
            return SimpleNamespace(phone_code_hash="code-hash")

        client.send_code.side_effect = send_code
        with patch.object(bot_engine, "has_user_api_credentials", return_value=True), \
             patch.object(bot_engine, "init_user_client", return_value=client):
            result = await bot_engine.begin_user_auth("123")
        self.assertEqual(result["status"], "code_sent")
        self.assertIsNone(bot_engine.pyro_user_app)
        self.bind_owner.assert_not_awaited()

    async def test_send_code_failure_disposes_unpublished_client(self):
        client = fake_user_client(authorized=False)
        client.send_code.side_effect = RuntimeError("send code failed")
        with patch.object(bot_engine, "has_user_api_credentials", return_value=True), \
             patch.object(bot_engine, "init_user_client", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "send code failed"):
                await bot_engine.begin_user_auth("123")
        client.disconnect.assert_awaited_once()
        self.assertIsNone(bot_engine.pyro_user_app)
        self.assertIsNone(bot_engine.user_auth_state["client"])

    async def test_code_login_uses_verified_me_for_owner(self):
        client = fake_user_client(404)
        bot_engine.user_auth_state.update(client=client, phone_number="123", phone_code_hash="hash", awaiting_code=True)
        result = await bot_engine.complete_user_auth("12345")
        self.assertEqual(result["user"]["id"], 404)
        self.bind_owner.assert_awaited_once_with(404)

    async def test_password_login_uses_verified_me_for_owner(self):
        client = fake_user_client(505)
        bot_engine.user_auth_state.update(client=client, awaiting_password=True)
        result = await bot_engine.complete_user_password("password")
        self.assertEqual(result["user"]["id"], 505)
        self.bind_owner.assert_awaited_once_with(505)

    async def test_invalid_code_keeps_pending_client_for_retry(self):
        client = fake_user_client()
        client.sign_in.side_effect = ValueError("invalid code")
        bot_engine.user_auth_state.update(client=client, phone_number="123", phone_code_hash="hash", awaiting_code=True)
        with self.assertRaisesRegex(ValueError, "invalid code"):
            await bot_engine.complete_user_auth("wrong")
        self.assertIs(bot_engine.user_auth_state["client"], client)
        self.assertTrue(bot_engine.user_auth_state["awaiting_code"])
        self.bind_owner.assert_not_awaited()
        client.disconnect.assert_not_awaited()
        self.assertFalse(sync_services._saved_messages_account_switching)

    async def test_invalid_password_keeps_pending_client_for_retry(self):
        client = fake_user_client()
        client.check_password.side_effect = ValueError("invalid password")
        bot_engine.user_auth_state.update(client=client, awaiting_password=True)
        with self.assertRaisesRegex(ValueError, "invalid password"):
            await bot_engine.complete_user_password("wrong")
        self.assertIs(bot_engine.user_auth_state["client"], client)
        self.assertTrue(bot_engine.user_auth_state["awaiting_password"])
        self.bind_owner.assert_not_awaited()
        client.disconnect.assert_not_awaited()
        self.assertFalse(sync_services._saved_messages_account_switching)

    async def test_cancel_cannot_dispose_client_while_login_is_finalizing(self):
        client = fake_user_client()
        initializing = asyncio.Event()
        allow_initialize = asyncio.Event()

        async def initialize():
            initializing.set()
            await allow_initialize.wait()

        client.initialize.side_effect = initialize
        bot_engine.user_auth_state.update(client=client, phone_number="123", phone_code_hash="hash", awaiting_code=True)
        login = asyncio.create_task(bot_engine.complete_user_auth("12345"))
        try:
            await asyncio.wait_for(initializing.wait(), timeout=1)
            with self.assertRaisesRegex(ValueError, "辅助账号正在切换"):
                await bot_engine.cancel_user_auth()
            client.disconnect.assert_not_awaited()
        finally:
            allow_initialize.set()
            await login

    async def test_switch_preserves_account_message_mappings(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / "temp") as temp_dir, \
             patch.object(bot_engine, "pyrogram_user_session_base", return_value=Path(temp_dir) / "session"), \
             patch.object(bot_engine.db, "delete_message_mappings_for_target", AsyncMock()) as delete:
            await bot_engine.switch_user_account()
        delete.assert_not_awaited()


class SavedAccountEditedPostTests(IsolatedSavedAccountTests):
    async def test_edit_rechecks_mapping_after_account_guard(self):
        await self._check_edit_after_account_change(88)

    async def test_edit_skips_mapping_not_owned_by_current_account(self):
        await self._check_edit_after_account_change(None)

    async def _check_edit_after_account_change(self, current_target_message_id):
        message = SimpleNamespace(chat=SimpleNamespace(id=-1001), message_id=10, text="new", caption=None, html_text="new")
        first_user = SimpleNamespace(is_initialized=True, me=SimpleNamespace(id=101), edit_message_text=AsyncMock())
        current_user = SimpleNamespace(is_initialized=True, me=SimpleNamespace(id=202), edit_message_text=AsyncMock())
        original_begin = bot_engine.begin_saved_messages_operation
        guard_acquired = False

        async def acquire_guard(target_type, target_id):
            nonlocal guard_acquired
            bot_engine.pyro_user_app = current_user
            guard_acquired = await original_begin(target_type, target_id)
            return guard_acquired

        async def lookup_mapping(source_id, message_id, target_id):
            self.assertTrue(guard_acquired)
            self.assertIs(bot_engine.pyro_user_app, current_user)
            return current_target_message_id

        with patch.object(bot_engine, "aiogram_bot", SimpleNamespace(edit_message_text=AsyncMock())), \
             patch.object(bot_engine, "pyro_user_app", first_user), \
             patch.object(bot_engine, "begin_saved_messages_operation", AsyncMock(side_effect=acquire_guard)), \
             patch.object(bot_engine.db, "get_all_target_msg_mappings", AsyncMock(return_value=[(-1, 20, "saved")])), \
             patch.object(bot_engine.db, "get_target_msg_id", AsyncMock(side_effect=lookup_mapping)) as lookup, \
             patch.object(bot_engine.db, "apply_message_filters", AsyncMock(return_value=(False, "new"))), \
             patch.object(bot_engine, "build_link_rewrite_context", AsyncMock(return_value=None)), \
             patch.object(bot_engine, "rewrite_message_links", AsyncMock(return_value=("new", 0))), \
             patch.object(bot_engine.db, "add_msg_log", AsyncMock()):
            await bot_engine.handle_edited_post(message)
        lookup.assert_awaited_once_with(-1001, 10, -1)
        first_user.edit_message_text.assert_not_awaited()
        if current_target_message_id is None:
            current_user.edit_message_text.assert_not_awaited()
        else:
            current_user.edit_message_text.assert_awaited_once_with(
                chat_id=202, message_id=88, text="new", parse_mode=bot_engine.ParseMode.HTML
            )
