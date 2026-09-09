import inspect
import unittest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
import asyncio

import bot_engine
import database
from services import sync_services
from sync_worker.clone import process as history
from sync_worker.core import prepend_source_header_html


class FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class FakeNamedChat:
    def __init__(self, chat_id, title=None, username=None):
        self.id = chat_id
        self.title = title
        self.username = username


class FakeBot:
    async def get_chat(self, ref):
        return FakeChat(-100123 if str(ref).startswith("@source") else -100456)


class FakePyroApp:
    is_initialized = True

    async def get_chat(self, ref):
        return type("Chat", (), {"id": int(ref), "username": ""})()


class SyncServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "data.db"
        self.original_db_file = database.DB_FILE
        self.original_ensure_dirs = database.ensure_runtime_dirs
        database.DB_FILE = str(self.db_path)
        database.ensure_runtime_dirs = lambda: self.db_path.parent.mkdir(parents=True, exist_ok=True)
        await database.close_db()
        await database.init_db()

    async def asyncTearDown(self):
        await database.close_db()
        database.DB_FILE = self.original_db_file
        database.ensure_runtime_dirs = self.original_ensure_dirs
        self.temp_dir.cleanup()

    def test_format_channel_check_error_distinguishes_network_errors(self):
        message = sync_services.format_channel_check_error(RuntimeError("HTTP Client says - ClientOSError:"))

        self.assertIn("网络连接异常", message)
        self.assertNotIn("请确认频道 ID", message)

    def test_format_channel_check_error_for_parse_or_permission_errors(self):
        message = sync_services.format_channel_check_error(RuntimeError("chat not found"))

        self.assertIn("请确认频道 ID", message)

    async def test_resolve_chat_id_supports_username_and_url(self):
        bot = FakeBot()

        self.assertEqual(await sync_services.resolve_chat_id(bot, "@source"), -100123)
        self.assertEqual(await sync_services.resolve_chat_id(bot, "https://t.me/source/10"), -100123)
        self.assertEqual(await sync_services.resolve_chat_id(bot, "-100999"), -100999)

    async def test_rewrite_message_links_uses_target_mapping(self):
        context = {"target_id": -100456, "source_username": "source", "target_username": "target"}
        with patch("services.sync_services.db.get_target_msg_id", AsyncMock(return_value=88)):
            rewritten, count = await sync_services.rewrite_message_links(
                "see https://t.me/source/12 and https://t.me/c/123/12",
                -100123,
                context,
            )

        self.assertIn("https://t.me/target/88", rewritten)
        self.assertIn("https://t.me/c/456/88", rewritten)
        self.assertEqual(count, 2)

    async def test_rewrite_message_links_falls_back_to_internal_target_without_duplicate_prefix(self):
        context = {"target_id": -1003717669322, "source_username": "KOIUJHBFJB", "target_username": None}
        with patch("services.sync_services.db.get_target_msg_id", AsyncMock(return_value=301)):
            rewritten, count = await sync_services.rewrite_message_links(
                "https://t.me/KOIUJHBFJB/68",
                0,
                context,
            )

        self.assertEqual(rewritten, "https://t.me/c/3717669322/301")
        self.assertEqual(count, 1)

    async def test_rewrite_message_links_counts_each_duplicate_position(self):
        context = {"target_id": -100456, "source_username": "source", "target_username": "target"}

        async def map_message_id(source_id, source_msg_id, target_id):
            self.assertEqual(source_id, -100123)
            self.assertEqual(target_id, -100456)
            return {12: 88, 13: None}[source_msg_id]

        with patch("services.sync_services.db.get_target_msg_id", AsyncMock(side_effect=map_message_id)):
            rewritten, count = await sync_services.rewrite_message_links(
                "https://t.me/source/12 https://t.me/source/12 https://t.me/source/13",
                -100123,
                context,
            )

        self.assertEqual(rewritten, "https://t.me/target/88 https://t.me/target/88 https://t.me/source/13")
        self.assertEqual(count, 2)

    def test_prepend_source_header_html_supports_pyrogram_like_message(self):
        msg = type(
            "Msg",
            (),
            {
                "forward_from_chat": FakeNamedChat(-100123, title="原频道"),
                "forward_from_message_id": 321,
                "forward_from": None,
                "forward_sender_name": None,
                "external_reply": type(
                    "ExternalReply",
                    (),
                    {
                        "chat": FakeNamedChat(-100456, title="外部频道"),
                        "message_id": 789,
                    },
                )(),
            },
        )()

        rendered = prepend_source_header_html("正文", msg, enabled=True)

        self.assertIn('href="https://t.me/c/123/321"', rendered)
        self.assertIn("#转发自", rendered)
        self.assertIn(">原频道</a>", rendered)
        self.assertIn('href="https://t.me/c/456/789"', rendered)
        self.assertIn('#回复自 <a href="https://t.me/c/456/789">外部频道</a>', rendered)
        self.assertTrue(rendered.endswith("\n正文"))

    def test_prepend_source_header_html_external_reply_falls_back_without_name(self):
        msg = type(
            "Msg",
            (),
            {
                "external_reply": type(
                    "ExternalReply",
                    (),
                    {
                        "chat": FakeNamedChat(-100456),
                        "message_id": 789,
                    },
                )(),
            },
        )()

        rendered = prepend_source_header_html("正文", msg, enabled=True)

        self.assertIn('#回复自 <a href="https://t.me/c/456/789">外部消息</a>', rendered)

    def test_prepend_source_header_html_supports_json_forwarded_channel_peer(self):
        rendered = prepend_source_header_html(
            "正文",
            {
                "forwarded_from": "333频道",
                "forwarded_from_id": "channel3912522050",
                "forwarded_from_message_id": "66",
            },
            enabled=True,
        )

        self.assertIn('href="https://t.me/c/3912522050/66"', rendered)
        self.assertIn("#转发自", rendered)
        self.assertIn(">333频道</a>", rendered)

    def test_compute_progress_speed_uses_delta(self):
        speed = sync_services.compute_progress_speed(20 * 1024 * 1024, 10 * 1024 * 1024, 2)
        self.assertAlmostEqual(speed, 5.0)

    def test_is_temporary_network_error(self):
        self.assertTrue(sync_services.is_temporary_network_error(Exception("HTTP Client says - ServerDisconnectedError: Server disconnected")))
        self.assertTrue(sync_services.is_temporary_network_error(Exception("TimeoutError")))
        self.assertFalse(sync_services.is_temporary_network_error(Exception("retry after 10")))

    async def test_execute_with_network_retry_retries_twice_then_raises(self):
        calls = {"count": 0}

        async def failing():
            calls["count"] += 1
            raise Exception("HTTP Client says - ServerDisconnectedError: Server disconnected")

        with patch("services.sync_services.db.add_msg_log", AsyncMock()) as mock_add_msg_log, \
             patch("services.sync_services.asyncio.sleep", AsyncMock()):
            with self.assertRaises(sync_services.SyncNetworkRetryExhaustedError):
                await sync_services.execute_with_network_retry(
                    failing,
                    action_label="测试动作",
                    sync_state=None,
                    log_tag="TEST_NETWORK_RETRY",
                )

        self.assertEqual(calls["count"], 3)
        self.assertEqual(mock_add_msg_log.await_count, 2)

    async def test_process_master_sync_json_does_not_require_source_id(self):
        with patch("sync_worker.clone.process.db.get_all_settings", AsyncMock(return_value={})), \
             patch("sync_worker.clone.process.resolve_chat_id", AsyncMock(return_value=-100456)) as mock_resolve, \
             patch("sync_worker.clone.process.process_json_sync", AsyncMock()) as mock_process_json:
            await history.process_master_sync("json", "bot", "", "@target", 1, 0, 0, "fake.json", False, "", 3)

        mock_process_json.assert_awaited_once()
        self.assertEqual(mock_resolve.await_count, 1)
        self.assertEqual(mock_process_json.await_args.args[0], "bot")

    def test_normalize_channel_username_supports_at_and_link(self):
        self.assertEqual(sync_services.normalize_channel_username("@source_name"), "source_name")
        self.assertEqual(sync_services.normalize_channel_username("https://t.me/source_name/123"), "source_name")
        self.assertEqual(sync_services.normalize_channel_username("t.me/source_name"), "source_name")
        self.assertEqual(sync_services.normalize_channel_username(""), "")

    def test_build_json_source_scope_id_is_stable(self):
        scope_a = sync_services.build_json_source_scope_id("@source_name")
        scope_b = sync_services.build_json_source_scope_id("https://t.me/source_name/123")
        self.assertEqual(scope_a, scope_b)
        self.assertLess(scope_a, 0)

    def test_is_saved_messages_target_uses_target_type_then_sentinel(self):
        # target_type 优先，无论 target_id 是什么
        self.assertTrue(sync_services.is_saved_messages_target("saved", 0))
        self.assertTrue(sync_services.is_saved_messages_target("saved", -100456))
        # sentinel 占位值兼容识别（旧路径未带 target_type 时）
        self.assertTrue(sync_services.is_saved_messages_target(None, sync_services.SAVED_MESSAGES_TARGET_ID))
        self.assertTrue(sync_services.is_saved_messages_target("channel", sync_services.SAVED_MESSAGES_TARGET_ID))
        # 普通频道目标
        self.assertFalse(sync_services.is_saved_messages_target("channel", -100456))
        self.assertFalse(sync_services.is_saved_messages_target(None, -100456))
        self.assertFalse(sync_services.is_saved_messages_target(None, None))

    async def test_resolve_destination_chat_id_saved_returns_current_own_id(self):
        # saved 目标每次即时解析当前辅助账号 own id，避免 stale-id 静默发错目标。
        fake_me = type("Me", (), {"id": 999})()
        fake_user_app = type("UserApp", (), {"is_initialized": True, "me": fake_me})()

        resolved = await sync_services.resolve_destination_chat_id(
            "saved", sync_services.SAVED_MESSAGES_TARGET_ID, fake_user_app
        )
        self.assertEqual(resolved, 999)

    async def test_resolve_destination_chat_id_saved_raises_when_user_not_logged_in(self):
        fake_user_app = type("UserApp", (), {"is_initialized": False, "me": None})()

        with self.assertRaises(ValueError):
            await sync_services.resolve_destination_chat_id(
                "saved", sync_services.SAVED_MESSAGES_TARGET_ID, fake_user_app
            )

    async def test_resolve_destination_chat_id_channel_returns_stored_value(self):
        resolved = await sync_services.resolve_destination_chat_id("channel", -100456, None)
        self.assertEqual(resolved, -100456)

    async def test_build_link_rewrite_context_returns_none_for_saved_target(self):
        # saved 目标短路：target_id 为 sentinel 时返回 None，避免 bot.get_chat(-1) 抛错与 t.me/c/1/* 失效链接。
        bot = FakeBot()
        ctx = await sync_services.build_link_rewrite_context(
            bot, -100123, sync_services.SAVED_MESSAGES_TARGET_ID
        )
        self.assertIsNone(ctx)

    def test_clone_parse_retry_after_seconds(self):
        self.assertEqual(history._parse_retry_after_seconds(Exception("retry after 16")), 16)
        self.assertIsNone(history._parse_retry_after_seconds(Exception("other error")))

    def test_clone_is_request_entity_too_large(self):
        self.assertTrue(history._is_request_entity_too_large(Exception("HTTP Client says - Request Entity Too Large")))
        self.assertFalse(history._is_request_entity_too_large(Exception("Too Many Requests")))

    def test_is_chat_forwards_restricted(self):
        self.assertTrue(history._is_chat_forwards_restricted(Exception('Telegram says: [400 CHAT_FORWARDS_RESTRICTED]')))
        self.assertTrue(history._is_chat_forwards_restricted(Exception("The chat restricts forwarding content")))
        self.assertFalse(history._is_chat_forwards_restricted(Exception("other error")))

    def test_pyrofork_messages_compat_defaults_topics(self):
        messages_cls = bot_engine.raw.types.messages.Messages
        topics_parameter = inspect.signature(messages_cls.__init__).parameters.get("topics")
        messages = messages_cls(messages=[], chats=[], users=[])

        if bot_engine._pyrofork_messages_topics_required(messages_cls):
            self.assertEqual(messages.topics, [])
        elif topics_parameter is None:
            self.assertFalse(hasattr(messages, "topics"))
        else:
            self.assertEqual(messages.topics, topics_parameter.default)

    def test_build_temp_download_path_uses_message_id_prefix(self):
        media = type("Media", (), {"file_name": "PixPin_2026-04-17_19-36-44.mp4"})()
        msg_a = type("Msg", (), {"id": 77, "video": media})()
        msg_b = type("Msg", (), {"id": 78, "video": media})()

        path_a = history._build_temp_download_path(msg_a, "video")
        path_b = history._build_temp_download_path(msg_b, "video")

        self.assertNotEqual(path_a, path_b)
        self.assertIn("77_", path_a)
        self.assertIn("78_", path_b)

    async def test_debug_log_bot_message_writes_terminal_debug_only(self):
        chat = type("Chat", (), {"id": -100123, "type": "supergroup", "username": None, "title": "测试群"})()
        user = type("User", (), {"id": 42})()
        message = type(
            "Message",
            (),
            {"chat": chat, "from_user": user, "message_id": 9, "text": "hello", "caption": None},
        )()

        with patch("bot_engine.get_config", return_value={"app": {"debug_terminal_logs": True}}), \
             patch("bot_engine.logger") as mock_logger:
            await bot_engine.debug_log_bot_message(message)

        mock_logger.debug.assert_called_once()

    async def test_safe_get_messages_falls_back_when_topics_parse_breaks_batch(self):
        msg1 = type("Msg", (), {"id": 1, "empty": False})()
        msg2 = type("Msg", (), {"id": 2, "empty": False})()

        class FakeApp:
            def __init__(self):
                self.calls = []

            async def get_messages(self, chat_id, ids):
                self.calls.append((chat_id, ids))
                if isinstance(ids, list):
                    raise TypeError("Messages.__init__() missing 1 required keyword-only argument: 'topics'")
                return {1: msg1, 2: msg2}.get(ids)

        app = FakeApp()
        result = await history._safe_get_messages(app, -100123, [1, 2])

        self.assertEqual(result, [msg1, msg2])
        self.assertEqual(app.calls, [(-100123, [1, 2]), (-100123, 1), (-100123, 2)])

    async def test_process_master_sync_logs_completion(self):
        with patch("sync_worker.clone.process.db.get_all_settings", AsyncMock(return_value={})), \
             patch("sync_worker.clone.process.resolve_chat_id", AsyncMock(return_value=-100456)), \
             patch("sync_worker.clone.process.process_json_sync", AsyncMock()), \
             patch("sync_worker.clone.process.db.add_log", AsyncMock()) as mock_add_log:
            await history.process_master_sync("json", "bot", "", "@target", 1, 0, 0, "fake.json", False, "", 3)

        mock_add_log.assert_any_await("INFO", "任务运行完毕：JSON | 已处理 0 / 0 | 跳过 0")

    async def test_sync_single_message_clone_downloads_before_reupload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            download_path = Path(temp_dir) / "demo.bin"
            download_path.write_bytes(b"doc-bytes")
            msg = type("Msg", (), {"id": 7, "text": None, "caption": None, "document": type("Document", (), {"file_name": "demo.bin"})()})()
            fake_app = type("FakeApp", (), {"download_media": AsyncMock()})()
            fake_sent = type("Sent", (), {"id": 101})()

            with patch("sync_worker.clone.process.get_msg_meta", return_value=("document", "sync_document")), \
                 patch("sync_worker.clone.process.db.apply_message_filters", AsyncMock(return_value=(False, ""))), \
                 patch("sync_worker.clone.process.update_state_and_check_skip", AsyncMock(return_value=False)), \
                 patch("sync_worker.clone.process.resolve_reply_target", AsyncMock(return_value=None)), \
                 patch("sync_worker.clone.process.build_link_rewrite_context", AsyncMock(return_value={})), \
                 patch("sync_worker.clone.process.rewrite_message_links", AsyncMock(return_value=("", 0))), \
                 patch("sync_worker.clone.process.has_media_spoiler", return_value=False), \
                 patch("sync_worker.clone.process.has_text_spoiler", return_value=False), \
                 patch("sync_worker.clone.process.execute_with_network_retry", AsyncMock(return_value=str(download_path))) as mock_download, \
                 patch("sync_worker.clone.process.prepare_media_for_send", AsyncMock(return_value=str(download_path))), \
                 patch("sync_worker.clone.process.resolve_upload_target", Mock(return_value=object())), \
                 patch("sync_worker.clone.process.safe_execute", AsyncMock(return_value={"sender": "user", "client": fake_app, "parse_mode": history.ParseMode.HTML, "label": "辅助账号"})), \
                 patch("sync_worker.clone.process._execute_with_clone_retry_interruptibly", AsyncMock(return_value=fake_sent)), \
                 patch("sync_worker.clone.process.dynamic_send", AsyncMock()), \
                 patch("sync_worker.clone.process.record_success", AsyncMock()) as mock_record_success, \
                 patch("sync_worker.clone.process.db.add_msg_log", AsyncMock()):
                await history.sync_single_message(
                    "clone",
                    "user",
                    fake_app,
                    object(),
                    -100123,
                    -100456,
                    msg,
                    0,
                    False,
                )

            mock_download.assert_awaited()
            mock_record_success.assert_awaited_once_with(-100123, -100456, 7, 101, force_send=False)

    async def test_sync_media_group_api_topics_error_does_not_record_zero_mapping(self):
        group = [
            type("Msg", (), {"id": 1, "text": None, "caption": None})(),
            type("Msg", (), {"id": 2, "text": None, "caption": None})(),
        ]
        fake_app = type("FakeApp", (), {"copy_media_group": AsyncMock(side_effect=TypeError("Messages.__init__() missing 1 required keyword-only argument: 'topics'"))})()

        with patch("sync_worker.clone.process.update_state_and_check_skip", AsyncMock(return_value=False)), \
             patch("sync_worker.clone.process.resolve_reply_target", AsyncMock(return_value=None)), \
             patch("sync_worker.clone.process.build_link_rewrite_context", AsyncMock(return_value={})), \
             patch("sync_worker.clone.process.rewrite_media_group_captions", AsyncMock(return_value=(["", ""], False, 0))), \
             patch("sync_worker.clone.process.get_msg_meta", return_value=("photo", "sync_photo")), \
             patch("sync_worker.clone.process.has_media_spoiler", return_value=False), \
             patch("sync_worker.clone.process.execute_with_network_retry", AsyncMock(side_effect=TypeError("Messages.__init__() missing 1 required keyword-only argument: 'topics'"))), \
             patch("sync_worker.clone.process.record_success", AsyncMock()) as mock_record_success:
            await history.sync_media_group(
                "api",
                "bot",
                fake_app,
                object(),
                -100123,
                -100456,
                group,
                0,
                False,
            )

        mock_record_success.assert_not_awaited()

    async def test_sync_media_group_api_generic_type_error_keeps_group_failed(self):
        group = [type("Msg", (), {"id": 1, "text": None, "caption": None})()]
        original_stop = history.sync_state["stop_requested"]
        history.sync_state["stop_requested"] = False

        with patch("sync_worker.clone.process.update_state_and_check_skip", AsyncMock(return_value=False)), \
             patch("sync_worker.clone.process.resolve_reply_target", AsyncMock(return_value=None)), \
             patch("sync_worker.clone.process.build_link_rewrite_context", AsyncMock(return_value={})), \
             patch("sync_worker.clone.process.rewrite_media_group_captions", AsyncMock(return_value=([""], False, 0))), \
             patch("sync_worker.clone.process.get_msg_meta", return_value=("photo", "sync_photo")), \
             patch("sync_worker.clone.process.has_media_spoiler", return_value=False), \
             patch("sync_worker.clone.process.execute_with_network_retry", AsyncMock(side_effect=TypeError("unexpected keyword argument 'captions'"))), \
             patch("sync_worker.clone.process.record_success", AsyncMock()) as mock_record_success, \
             patch("sync_worker.clone.process.log_sync_error", AsyncMock()) as mock_log_error:
            result = await history.sync_media_group(
                "api",
                "bot",
                object(),
                object(),
                -100123,
                -100456,
                group,
                0,
                False,
            )

        history.sync_state["stop_requested"] = original_stop
        self.assertEqual(result, history.SYNC_RESULT_FAILED)
        mock_record_success.assert_not_awaited()
        mock_log_error.assert_awaited_once()

    async def test_sync_media_group_api_partial_response_is_reported_unmapped(self):
        group = [
            type("Msg", (), {"id": 1, "text": None, "caption": None})(),
            type("Msg", (), {"id": 2, "text": None, "caption": None})(),
        ]
        sent = [type("Sent", (), {"id": 101})()]

        with patch("sync_worker.clone.process.update_state_and_check_skip", AsyncMock(return_value=False)), \
             patch("sync_worker.clone.process.resolve_reply_target", AsyncMock(return_value=None)), \
             patch("sync_worker.clone.process.rewrite_media_group_captions", AsyncMock(return_value=(["", ""], False, 0))), \
             patch("sync_worker.clone.process.get_msg_meta", return_value=("photo", "sync_photo")), \
             patch("sync_worker.clone.process.has_media_spoiler", return_value=False), \
             patch("sync_worker.clone.process.execute_with_network_retry", AsyncMock(return_value=sent)), \
             patch("sync_worker.clone.process.record_success", AsyncMock()) as mock_record_success, \
             patch("sync_worker.clone.process.count_unmapped_group") as mock_count_unmapped:
            result = await history.sync_media_group(
                "api",
                "bot",
                object(),
                object(),
                -100123,
                -100456,
                group,
                0,
                False,
            )

        self.assertEqual(result, history.SYNC_RESULT_SENT_UNMAPPED)
        mock_record_success.assert_awaited_once_with(-100123, -100456, 1, 101, force_send=False)
        mock_count_unmapped.assert_called_once_with()

    async def test_sync_media_group_drop_filter_blocks_whole_group(self):
        blocked_text = (
            "豆包提示词 视频教学 手机电脑均可使用 一张照片即可做出想要ai视频 "
            "进群学习提示词p图自助下单哦 @doubao40bot 预览群 https://t.me/+8F1U-0uMIys5ZmM1"
        )
        caption = type("Caption", (), {"html": blocked_text})()
        group = [
            type("Msg", (), {"id": 1, "text": None, "caption": None})(),
            type("Msg", (), {"id": 2, "text": None, "caption": caption})(),
        ]

        with patch("sync_worker.clone.process.update_state_and_check_skip", AsyncMock(return_value=False)), \
             patch("sync_worker.clone.process.db.apply_message_filters", AsyncMock(side_effect=[(False, ""), (True, blocked_text)])) as mock_filter, \
             patch("sync_worker.clone.process.resolve_reply_target", AsyncMock(return_value=None)), \
             patch("sync_worker.clone.process.rewrite_media_group_captions", AsyncMock(return_value=(["", blocked_text], False, 0))), \
             patch("sync_worker.clone.process.get_msg_meta", return_value=("photo", "sync_photo")), \
             patch("sync_worker.clone.process.has_media_spoiler", return_value=False), \
             patch("sync_worker.clone.process.execute_with_network_retry", AsyncMock()) as mock_send, \
             patch("sync_worker.clone.process.db.add_msg_log", AsyncMock()):
            result = await history.sync_media_group(
                "api",
                "user",
                object(),
                object(),
                -100123,
                -100456,
                group,
                0,
                False,
            )

        self.assertEqual(result, history.SYNC_RESULT_SKIPPED)
        self.assertEqual(mock_filter.await_count, 2)
        mock_send.assert_not_awaited()

    def test_pyrofork_topics_compat_defaults_topics_and_is_idempotent(self):
        from pyrogram import raw

        messages_cls = raw.types.messages.Messages
        if not bot_engine._pyrofork_messages_topics_required(messages_cls):
            self.assertFalse(getattr(messages_cls, bot_engine.PYRO_TOPICS_COMPAT_FLAG, False))
            return
        self.assertEqual(bot_engine._patch_pyrofork_messages_topics_default(), 0)
        self.assertTrue(getattr(messages_cls, bot_engine.PYRO_TOPICS_COMPAT_FLAG, False))
        self.assertEqual(list(messages_cls(messages=[], chats=[], users=[]).topics), [])

    def test_count_unmapped_group_accumulates_until_session_reset(self):
        original = history.sync_state.get("unmapped", 0)
        history.sync_state["unmapped"] = 0
        history.count_unmapped_group()
        history.count_unmapped_group()
        self.assertEqual(history.sync_state["unmapped"], 2)
        history.sync_state["unmapped"] = original

    def test_pyrofork_topics_compat_detects_required_topics_argument(self):
        class LegacyMessages:
            def __init__(self, *, messages, chats, users):
                self.messages = messages
                self.chats = chats
                self.users = users

        class ModernMessages:
            def __init__(self, *, messages, topics, chats, users):
                self.messages = messages
                self.topics = topics
                self.chats = chats
                self.users = users

        self.assertFalse(bot_engine._pyrofork_messages_topics_required(LegacyMessages))
        self.assertTrue(bot_engine._pyrofork_messages_topics_required(ModernMessages))

    async def test_download_clone_media_item_uses_normal_download(self):
        fake_app = type("FakeApp", (), {"download_media": AsyncMock(return_value="normal.bin")})()
        msg = type("Msg", (), {"id": 9, "document": type("Doc", (), {"file_id": "x", "file_size": 10})()})()

        with patch("sync_worker.clone.process.get_msg_meta", return_value=("document", "sync_document")), \
             patch("sync_worker.clone.process._build_temp_download_path", return_value="temp.bin"), \
             patch("sync_worker.clone.process.execute_with_network_retry", AsyncMock(return_value="normal.bin")) as mock_download:
            result = await history._download_clone_media_item(
                fake_app,
                msg,
                "clone",
                progress_label="组内下载 [1/2]",
            )

        self.assertEqual(result, "normal.bin")
        mock_download.assert_awaited_once()

    async def test_process_master_sync_logs_failed_summary_for_network_abort(self):
        async def _raise_with_progress(*args, **kwargs):
            history.sync_state["current"] = 36
            history.sync_state["total"] = 33
            history.sync_state["skipped"] = 36
            history.sync_state["unmapped"] = 2
            raise sync_services.SyncNetworkRetryExhaustedError("JSON 文本发送 1 连续重试 2 次后仍无法连接")

        with patch("sync_worker.clone.process.db.get_all_settings", AsyncMock(return_value={})), \
             patch("sync_worker.clone.process.resolve_chat_id", AsyncMock(return_value=-100456)), \
             patch(
                 "sync_worker.clone.process.process_json_sync",
                 AsyncMock(side_effect=_raise_with_progress),
             ), \
             patch("sync_worker.clone.process.log_sync_error", AsyncMock()) as mock_log_sync_error, \
             patch("sync_worker.clone.process.db.add_log", AsyncMock()) as mock_add_log:
            await history.process_master_sync("json", "bot", "", "@target", 1, 0, 0, "fake.json", False, "", 3)

        mock_log_sync_error.assert_awaited()
        mock_add_log.assert_any_await(
            "ERROR",
            "任务异常终止：JSON | 已处理 36 / 33 | 跳过 36 | 已发送但未记录映射 2 组（重跑会重复发送，建议先核对目标频道）",
        )

    async def test_process_master_sync_logs_unmapped_summary_when_stopped(self):
        async def _stop_with_unmapped(*args, **kwargs):
            history.sync_state["unmapped"] = 1
            history.sync_state["stop_requested"] = True

        with patch("sync_worker.clone.process.db.get_all_settings", AsyncMock(return_value={})), \
             patch("sync_worker.clone.process.resolve_chat_id", AsyncMock(return_value=-100456)), \
             patch("sync_worker.clone.process.process_json_sync", AsyncMock(side_effect=_stop_with_unmapped)), \
             patch("sync_worker.clone.process.db.add_log", AsyncMock()) as mock_add_log:
            await history.process_master_sync("json", "bot", "", "@target", 1, 0, 0, "fake.json", False, "", 3)

        mock_add_log.assert_any_await(
            "INFO",
            "任务结束：JSON 已停止 | 已发送但未记录映射 1 组（重跑会重复发送，建议先核对目标频道）",
        )

    def test_bot_media_group_can_attach_thumbnail(self):
        thumbnail = history.FSInputFile(__file__)
        media = history.AioVideo(media="video.bin", caption="x", parse_mode="HTML", thumbnail=thumbnail, supports_streaming=True)
        self.assertIs(media.thumbnail, thumbnail)
        self.assertTrue(media.supports_streaming)

    async def test_clone_group_download_retry_logs_failure(self):
        group = [type("Msg", (), {"id": 1})(), type("Msg", (), {"id": 2})()]
        results = [Exception("boom"), "temp.bin"]
        failure_details = []
        for item, result in zip(group, results):
            if isinstance(result, Exception):
                failure_details.append(f"消息ID:{item.id} -> {result}")
        self.assertIn("消息ID:1 -> boom", failure_details[0])

    async def test_mark_upload_bot_cooldown_sets_cooldown_and_logs(self):
        fake_client = object()
        original_upload_bots = bot_engine.upload_bots
        bot_engine.upload_bots = [{"client": fake_client, "label": "Bot#2", "cooldown_until": 0.0}]
        try:
            with patch("bot_engine.db.add_msg_log", AsyncMock()) as mock_add_msg_log:
                label = await bot_engine.mark_upload_bot_cooldown(fake_client, 12, "test")
            self.assertEqual(label, "Bot#2")
            self.assertGreater(bot_engine.upload_bots[0]["cooldown_until"], 0.0)
            mock_add_msg_log.assert_awaited()
        finally:
            bot_engine.upload_bots = original_upload_bots

    async def test_acquire_upload_bot_allows_single_upload_to_cross_threshold(self):
        fake_client = object()
        original_upload_bots = bot_engine.upload_bots
        original_rr_index = bot_engine.upload_bot_rr_index
        bot_engine.upload_bots = [{
            "client": fake_client,
            "label": "Bot#1",
            "window_started_at": 1000.0,
            "uploaded_bytes": 0,
            "cooldown_until": 0.0,
            "disabled": False,
            "disabled_reason": "",
        }]
        bot_engine.upload_bot_rr_index = 0
        try:
            with patch("bot_engine.get_config", return_value={"sync": {
                "bot_rate_limit_enabled": True,
                "bot_rate_limit_gb": 10,
                "bot_rate_limit_window_hours": 24,
                "bot_rate_limit_cooldown_minutes": 300,
            }}), \
                 patch("bot_engine.time.time", return_value=1200.0):
                selected = await bot_engine.acquire_upload_bot(15 * 1024 * 1024 * 1024, wait_if_unavailable=False)
            self.assertEqual(selected["client"], fake_client)
        finally:
            bot_engine.upload_bots = original_upload_bots
            bot_engine.upload_bot_rr_index = original_rr_index

    async def test_note_upload_success_locks_bot_after_threshold_reached(self):
        fake_client = object()
        original_upload_bots = bot_engine.upload_bots
        bot_engine.upload_bots = [{
            "client": fake_client,
            "label": "Bot#1",
            "window_started_at": 1000.0,
            "uploaded_bytes": 9 * 1024 * 1024 * 1024,
            "cooldown_until": 0.0,
            "disabled": False,
            "disabled_reason": "",
        }]
        try:
            with patch("bot_engine.get_config", return_value={"sync": {
                "bot_rate_limit_enabled": True,
                "bot_rate_limit_gb": 10,
                "bot_rate_limit_window_hours": 1,
                "bot_rate_limit_cooldown_minutes": 5,
            }}), \
                 patch("bot_engine.time.time", return_value=1200.0), \
                 patch("bot_engine.db.add_msg_log", AsyncMock()) as mock_add_msg_log:
                await bot_engine.note_upload_success(fake_client, 2 * 1024 * 1024 * 1024)
            self.assertGreaterEqual(bot_engine.upload_bots[0]["uploaded_bytes"], 11 * 1024 * 1024 * 1024)
            self.assertEqual(bot_engine.upload_bots[0]["cooldown_until"], 4600.0)
            mock_add_msg_log.assert_awaited()
        finally:
            bot_engine.upload_bots = original_upload_bots

    async def test_disable_upload_bot_marks_state_and_logs(self):
        fake_client = object()
        original_upload_bots = bot_engine.upload_bots
        bot_engine.upload_bots = [{
            "client": fake_client,
            "label": "Bot#3",
            "window_started_at": 0.0,
            "uploaded_bytes": 0,
            "cooldown_until": 0.0,
            "disabled": False,
            "disabled_reason": "",
        }]
        try:
            with patch("bot_engine.db.add_msg_log", AsyncMock()) as mock_add_msg_log:
                label = await bot_engine.disable_upload_bot(fake_client, "Unauthorized")
            self.assertEqual(label, "Bot#3")
            self.assertTrue(bot_engine.upload_bots[0]["disabled"])
            self.assertEqual(bot_engine.upload_bots[0]["disabled_reason"], "Unauthorized")
            mock_add_msg_log.assert_awaited()
        finally:
            bot_engine.upload_bots = original_upload_bots

    async def test_rewrite_media_group_captions_adds_header_only_to_first_caption(self):
        """测试媒体组转发信息：有文字的在文字上加，没有文字的在第一个媒体上加"""
        from sync_worker.core.links import rewrite_media_group_captions
        
        # 测试场景1：第一个没有 caption，第二个有 caption，第三个也有 caption
        # 应该只在第二个（第一个有文字的）上添加转发信息
        group1 = [
            type("Msg", (), {
                "caption": None,
                "forward_from_chat": FakeNamedChat(-100123, title="源频道"),
                "forward_from_message_id": 100,
                "forward_from": None,
                "forward_sender_name": None,
            })(),
            type("Msg", (), {
                "caption": type("Caption", (), {"html": "第一条说明"})(),
                "forward_from_chat": FakeNamedChat(-100123, title="源频道"),
                "forward_from_message_id": 101,
                "forward_from": None,
                "forward_sender_name": None,
            })(),
            type("Msg", (), {
                "caption": type("Caption", (), {"html": "第二条说明"})(),
                "forward_from_chat": FakeNamedChat(-100123, title="源频道"),
                "forward_from_message_id": 102,
                "forward_from": None,
                "forward_sender_name": None,
            })(),
        ]
        
        with patch("sync_worker.core.links.bot_engine.aiogram_bot", FakeBot()):
            captions1, changed1, _ = await rewrite_media_group_captions(
                -100123, -100456, group1, include_external_source_header=True
            )
        
        # 第一个媒体没有 caption，应该返回空字符串
        self.assertEqual(captions1[0], "")
        
        # 第二个媒体有 caption，应该添加转发信息
        self.assertIn("#转发自", captions1[1])
        self.assertIn("源频道", captions1[1])
        self.assertIn("第一条说明", captions1[1])
        
        # 第三个媒体有 caption，但不应该添加转发信息（避免 TG 隐藏文字介绍）
        self.assertNotIn("#转发自", captions1[2])
        self.assertIn("第二条说明", captions1[2])
        
        # 应该标记为已改变
        self.assertTrue(changed1)
        
        # 测试场景2：所有媒体都没有 caption
        # 应该在第一个媒体上添加转发信息
        group2 = [
            type("Msg", (), {
                "caption": None,
                "forward_from_chat": FakeNamedChat(-100123, title="源频道"),
                "forward_from_message_id": 200,
                "forward_from": None,
                "forward_sender_name": None,
            })(),
            type("Msg", (), {
                "caption": None,
                "forward_from_chat": FakeNamedChat(-100123, title="源频道"),
                "forward_from_message_id": 201,
                "forward_from": None,
                "forward_sender_name": None,
            })(),
        ]
        
        with patch("sync_worker.core.links.bot_engine.aiogram_bot", FakeBot()):
            captions2, changed2, _ = await rewrite_media_group_captions(
                -100123, -100456, group2, include_external_source_header=True
            )
        
        # 第一个媒体应该添加转发信息（因为没有任何媒体有文字）
        self.assertIn("#转发自", captions2[0])
        self.assertIn("源频道", captions2[0])
        
        # 第二个媒体不应该添加转发信息
        self.assertNotIn("#转发自", captions2[1])
        
        # 应该标记为已改变
        self.assertTrue(changed2)

    async def test_realtime_media_group_queue_debounces_and_separates_sources(self):
        original_cache = bot_engine.media_group_cache
        original_tasks = bot_engine.media_group_tasks
        original_delay = bot_engine.MEDIA_GROUP_FLUSH_DELAY_SECONDS
        bot_engine.media_group_cache = {}
        bot_engine.media_group_tasks = {}
        bot_engine.MEDIA_GROUP_FLUSH_DELAY_SECONDS = 60
        try:
            msg1 = type("Msg", (), {"media_group_id": "album", "message_id": 1})()
            msg2 = type("Msg", (), {"media_group_id": "album", "message_id": 2})()
            msg3 = type("Msg", (), {"media_group_id": "album", "message_id": 1})()

            await bot_engine._queue_realtime_media_group(msg1, -100111, [{"target_id": -100999}], "源1")
            first_task = bot_engine.media_group_tasks[(-100111, "album")]
            await bot_engine._queue_realtime_media_group(msg2, -100111, [{"target_id": -100999}], "源1")
            await asyncio.sleep(0)
            await bot_engine._queue_realtime_media_group(msg3, -100222, [{"target_id": -100999}], "源2")

            self.assertEqual([m.message_id for m in bot_engine.media_group_cache[(-100111, "album")]], [1, 2])
            self.assertEqual([m.message_id for m in bot_engine.media_group_cache[(-100222, "album")]], [1])
            self.assertTrue(first_task.cancelled())
            self.assertIn((-100111, "album"), bot_engine.media_group_tasks)
            self.assertIn((-100222, "album"), bot_engine.media_group_tasks)
        finally:
            for task in bot_engine.media_group_tasks.values():
                task.cancel()
            await asyncio.sleep(0)
            bot_engine.media_group_cache = original_cache
            bot_engine.media_group_tasks = original_tasks
            bot_engine.MEDIA_GROUP_FLUSH_DELAY_SECONDS = original_delay

    async def test_realtime_user_media_group_passes_spoiler_flags(self):
        fake_sent = [type("Sent", (), {"id": 11})(), type("Sent", (), {"id": 12})()]
        fake_user = type("FakeUser", (), {"is_initialized": True, "send_media_group": AsyncMock()})()
        original_user = bot_engine.pyro_user_app
        bot_engine.pyro_user_app = fake_user
        downloaded_files = [(object(), "a.jpg", "photo"), (object(), "b.jpg", "photo")]
        try:
            with patch("bot_engine.os.path.getsize", return_value=10), \
                 patch("bot_engine.build_user_media_group", Mock(return_value=["media1", "media2"])) as mock_build_group, \
                 patch("bot_engine.execute_with_network_retry", AsyncMock(return_value=fake_sent)):
                result = await bot_engine._send_realtime_media_group_via_user(
                    -100999,
                    downloaded_files,
                    ["caption", ""],
                    None,
                    None,
                    "测试媒体组",
                    spoiler_flags=[True, False],
                )

            self.assertEqual(result, [11, 12])
            mock_build_group.assert_called_once_with(
                downloaded_files,
                ["caption", ""],
                {},
                spoiler_flags=[True, False],
            )
        finally:
            bot_engine.pyro_user_app = original_user

    def test_public_channel_peer_accepts_username_and_numeric_id(self):
        self.assertEqual(bot_engine._public_channel_peer("@source"), "@source")
        self.assertEqual(bot_engine._public_channel_peer("source"), "@source")
        self.assertEqual(bot_engine._public_channel_peer("-100123"), -100123)

    async def test_public_poller_advances_checkpoint_after_completed_groups(self):
        from sync_worker.realtime import public_poller

        messages = [type("Msg", (), {"id": 6})(), type("Msg", (), {"id": 7})()]
        group = {
            "source_id": -100123,
            "source_ref": "source",
            "last_polled_message_id": 5,
            "mappings": [{"target_id": -100456}, {"target_id": -100789}],
        }

        with patch("sync_worker.realtime.public_poller.load_public_channel_new_messages", AsyncMock(return_value=messages)), \
             patch("sync_worker.clone.process.group_messages", return_value=[[messages[0]], [messages[1]]]), \
             patch("sync_worker.clone.process.sync_single_message", AsyncMock(return_value="sent_mapped")), \
             patch("sync_worker.realtime.public_poller.db.update_public_user_poll_position", AsyncMock()) as mock_update:
            await public_poller.process_public_channel_mapping_group(object(), object(), group)

        mock_update.assert_awaited_once_with(-100123, "source", 7)

    async def test_public_poller_does_not_advance_checkpoint_for_failed_group(self):
        from sync_worker.realtime import public_poller

        messages = [type("Msg", (), {"id": 6})(), type("Msg", (), {"id": 7})()]
        group = {
            "source_id": -100123,
            "source_ref": "source",
            "last_polled_message_id": 5,
            "mappings": [{"target_id": -100456}, {"target_id": -100789}],
        }

        with patch("sync_worker.realtime.public_poller.load_public_channel_new_messages", AsyncMock(return_value=messages)), \
             patch("sync_worker.clone.process.group_messages", return_value=[[messages[0], messages[1]]]), \
             patch("sync_worker.clone.process.sync_media_group", AsyncMock(side_effect=[None, RuntimeError("send failed")])), \
             patch("sync_worker.realtime.public_poller.db.update_public_user_poll_position", AsyncMock()) as mock_update:
            with self.assertRaises(RuntimeError):
                await public_poller.process_public_channel_mapping_group(object(), object(), group)

        mock_update.assert_not_awaited()

    async def test_public_poller_does_not_advance_checkpoint_when_sync_returns_failed(self):
        from sync_worker.realtime import public_poller

        message = type("Msg", (), {"id": 6})()
        group = {
            "source_id": -100123,
            "source_ref": "source",
            "last_polled_message_id": 5,
            "mappings": [{"target_id": -100456}],
        }

        with patch("sync_worker.realtime.public_poller.load_public_channel_new_messages", AsyncMock(return_value=[message])), \
             patch("sync_worker.clone.process.group_messages", return_value=[[message]]), \
             patch("sync_worker.clone.process.sync_single_message", AsyncMock(return_value="failed")), \
             patch("sync_worker.realtime.public_poller.db.update_public_user_poll_position", AsyncMock()) as mock_update:
            with self.assertRaises(RuntimeError):
                await public_poller.process_public_channel_mapping_group(object(), object(), group)

        mock_update.assert_not_awaited()

    async def test_mapping_source_numeric_id_falls_back_when_bot_cannot_receive(self):
        from services import channel_mapping_sources

        engine = type("Engine", (), {"aiogram_bot": object(), "pyro_user_app": FakePyroApp()})()
        with patch("services.channel_mapping_sources.bot_can_receive_channel_posts", AsyncMock(return_value=False)):
            source_id, source_mode, source_ref = await channel_mapping_sources.resolve_mapping_source(
                engine,
                "-100123",
                allow_public_user_fallback=True,
            )

        self.assertEqual(source_id, -100123)
        self.assertEqual(source_mode, "public_user")
        self.assertEqual(source_ref, "-100123")

    async def test_mapping_source_does_not_touch_user_when_fallback_disabled(self):
        from services import channel_mapping_sources

        fake_user = type("User", (), {"is_initialized": True, "get_chat": AsyncMock()})()
        engine = type("Engine", (), {"aiogram_bot": object(), "pyro_user_app": fake_user})()
        with patch("services.channel_mapping_sources.bot_can_receive_channel_posts", AsyncMock()) as mock_can_receive:
            source_id, source_mode, source_ref = await channel_mapping_sources.resolve_mapping_source(
                engine,
                "-100123",
                allow_public_user_fallback=False,
            )

        self.assertEqual(source_id, -100123)
        self.assertEqual(source_mode, "bot")
        self.assertEqual(source_ref, "")
        mock_can_receive.assert_not_awaited()
        fake_user.get_chat.assert_not_awaited()

    async def test_realtime_edit_failure_is_logged(self):
        fake_bot = type("Bot", (), {"edit_message_text": AsyncMock(side_effect=RuntimeError("cannot send"))})()
        message = type(
            "Msg",
            (),
            {"chat": type("Chat", (), {"id": -1001})(), "message_id": 10, "text": "new", "caption": None, "html_text": "new"},
        )()
        original_bot = bot_engine.aiogram_bot
        bot_engine.aiogram_bot = fake_bot
        try:
            with patch("bot_engine.db.get_all_target_msg_mappings", AsyncMock(return_value=[(-1002, 20, "channel")])), \
                 patch("bot_engine.db.apply_message_filters", AsyncMock(return_value=(False, "new"))), \
                 patch("bot_engine.build_link_rewrite_context", AsyncMock(return_value={})), \
                 patch("bot_engine.rewrite_message_links", AsyncMock(return_value=("new", 0))), \
                 patch("bot_engine.db.add_msg_log", AsyncMock()) as mock_log:
                await bot_engine.handle_edited_post(message)

            mock_log.assert_awaited()
            self.assertIn("编辑失败", mock_log.await_args.args[1])
        finally:
            bot_engine.aiogram_bot = original_bot

    async def test_realtime_edit_saved_message_uses_current_user_chat(self):
        fake_bot = type("Bot", (), {"edit_message_text": AsyncMock()})()
        fake_user = type(
            "User",
            (),
            {
                "is_initialized": True,
                "me": type("Me", (), {"id": 999})(),
                "edit_message_text": AsyncMock(),
            },
        )()
        message = type(
            "Msg",
            (),
            {"chat": type("Chat", (), {"id": -1001})(), "message_id": 10, "text": "new", "caption": None, "html_text": "new"},
        )()
        original_bot = bot_engine.aiogram_bot
        original_user = bot_engine.pyro_user_app
        bot_engine.aiogram_bot = fake_bot
        bot_engine.pyro_user_app = fake_user
        try:
            with patch("bot_engine.db.get_all_target_msg_mappings", AsyncMock(return_value=[(-1, 20, "saved")])), \
                 patch("bot_engine.db.apply_message_filters", AsyncMock(return_value=(False, "new"))), \
                 patch("bot_engine.build_link_rewrite_context", AsyncMock(return_value=None)), \
                 patch("bot_engine.rewrite_message_links", AsyncMock(return_value=("new", 0))), \
                 patch("bot_engine.db.add_msg_log", AsyncMock()):
                await bot_engine.handle_edited_post(message)

            fake_user.edit_message_text.assert_awaited_once_with(
                chat_id=999,
                message_id=20,
                text="new",
                parse_mode=bot_engine.ParseMode.HTML,
            )
            fake_bot.edit_message_text.assert_not_awaited()
        finally:
            bot_engine.aiogram_bot = original_bot
            bot_engine.pyro_user_app = original_user

    async def test_switch_user_account_invalidates_saved_message_mappings(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / "temp") as temp_dir:
            session_base = Path(temp_dir) / "sync_user_session"
            session_file = session_base.with_suffix(".session")
            session_file.write_text("session", encoding="utf-8")
            with patch("bot_engine.close_user_client", AsyncMock()), \
                 patch("bot_engine.pyrogram_user_session_base", return_value=session_base), \
                 patch("bot_engine.db.delete_message_mappings_for_target", AsyncMock()) as mock_delete:
                result = await bot_engine.switch_user_account()

            self.assertFalse(session_file.exists())

        self.assertEqual(result["status"], "success")
        mock_delete.assert_awaited_once_with(sync_services.SAVED_MESSAGES_TARGET_ID)

    async def test_switch_user_account_is_rejected_while_sync_is_running(self):
        with patch("bot_engine.sync_state", {"is_syncing": True}), \
             patch("bot_engine.close_user_client", AsyncMock()) as mock_close, \
             patch("bot_engine.db.delete_message_mappings_for_target", AsyncMock()) as mock_delete:
            with self.assertRaisesRegex(ValueError, "先中断当前同步任务"):
                await bot_engine.switch_user_account()

        mock_close.assert_not_awaited()
        mock_delete.assert_not_awaited()

    async def test_switch_user_account_waits_for_realtime_saved_edit_to_finish(self):
        edit_started = asyncio.Event()
        allow_edit_to_finish = asyncio.Event()

        async def edit_message_text(**kwargs):
            edit_started.set()
            await allow_edit_to_finish.wait()

        fake_bot = type("Bot", (), {"edit_message_text": AsyncMock()})()
        fake_user = type(
            "User",
            (),
            {
                "is_initialized": True,
                "me": type("Me", (), {"id": 999})(),
                "edit_message_text": AsyncMock(side_effect=edit_message_text),
            },
        )()
        message = type(
            "Msg",
            (),
            {"chat": type("Chat", (), {"id": -1001})(), "message_id": 10, "text": "new", "caption": None, "html_text": "new"},
        )()
        original_bot = bot_engine.aiogram_bot
        original_user = bot_engine.pyro_user_app
        bot_engine.aiogram_bot = fake_bot
        bot_engine.pyro_user_app = fake_user
        try:
            with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / "temp") as temp_dir, \
                 patch("bot_engine.sync_state", {"is_syncing": False}), \
                 patch("bot_engine.db.get_all_target_msg_mappings", AsyncMock(return_value=[(-1, 20, "saved")])), \
                 patch("bot_engine.db.apply_message_filters", AsyncMock(return_value=(False, "new"))), \
                 patch("bot_engine.build_link_rewrite_context", AsyncMock(return_value=None)), \
                 patch("bot_engine.rewrite_message_links", AsyncMock(return_value=("new", 0))), \
                 patch("bot_engine.db.add_msg_log", AsyncMock()), \
                 patch("bot_engine.close_user_client", AsyncMock()) as mock_close, \
                 patch("bot_engine.pyrogram_user_session_base", return_value=Path(temp_dir) / "sync_user_session"), \
                 patch("bot_engine.db.delete_message_mappings_for_target", AsyncMock()) as mock_delete:
                edit_task = asyncio.create_task(bot_engine.handle_edited_post(message))
                await asyncio.wait_for(edit_started.wait(), timeout=1)

                with self.assertRaisesRegex(ValueError, "收藏夹消息仍在处理中"):
                    await bot_engine.switch_user_account()
                mock_close.assert_not_awaited()

                allow_edit_to_finish.set()
                await edit_task
                result = await bot_engine.switch_user_account()

            self.assertEqual(result["status"], "success")
            mock_close.assert_awaited_once()
            mock_delete.assert_awaited_once_with(sync_services.SAVED_MESSAGES_TARGET_ID)
        finally:
            allow_edit_to_finish.set()
            bot_engine.aiogram_bot = original_bot
            bot_engine.pyro_user_app = original_user


