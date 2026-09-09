import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sync_worker.json_import import process as json_sync


class FakeSentMessage:
    def __init__(self, message_id):
        self.message_id = message_id


class JsonSyncTests(unittest.IsolatedAsyncioTestCase):
    def test_parse_retry_after_seconds(self):
        self.assertEqual(json_sync._parse_retry_after_seconds(Exception("retry after 21")), 21)
        self.assertEqual(json_sync._parse_retry_after_seconds(Exception("A wait of 123 seconds is required.")), 123)
        self.assertIsNone(json_sync._parse_retry_after_seconds(Exception("other error")))

    def test_is_request_entity_too_large(self):
        self.assertTrue(json_sync._is_request_entity_too_large(Exception("HTTP Client says - Request Entity Too Large")))
        self.assertFalse(json_sync._is_request_entity_too_large(Exception("Too Many Requests")))

    def test_is_topics_parse_error(self):
        self.assertTrue(json_sync._is_topics_parse_error(TypeError("Messages.__init__() missing 1 required keyword-only argument: 'topics'")))
        self.assertFalse(json_sync._is_topics_parse_error(Exception("retry after 10")))

    def test_scan_json_import_media_groups_reports_large_groups_without_splitting(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            for name, size in [("a.bin", 3), ("b.bin", 4)]:
                (Path(temp_dir) / name).write_bytes(b"x" * size)
            groups = [[
                {"id": 1, "type": "message", "file": "a.bin"},
                {"id": 2, "type": "message", "file": "b.bin"},
            ]]

            with patch("sync_worker.json_import.process.JSON_STANDARD_USER_UPLOAD_MAX_BYTES", 6), \
                 patch("sync_worker.json_import.process.bot_engine.should_upload_via_bot", return_value=False):
                stats = json_sync._scan_json_import_media_groups(groups, temp_dir, "bot")

            self.assertEqual(stats["media_files"], 2)
            self.assertEqual(stats["media_groups"], 1)
            self.assertEqual(stats["file_media_groups"], 1)
            self.assertEqual(stats["large_group_count"], 1)
            self.assertEqual(stats["largest_group_first_id"], 1)
            self.assertEqual(stats["largest_group_bytes"], 7)
            self.assertEqual(stats["over_bot_limit"], 2)

    async def test_send_json_single_via_user_raises_fatal_when_user_not_logged_in(self):
        with patch("sync_worker.json_import.process.bot_engine.pyro_user_app") as mock_app:
            mock_app.is_initialized = False
            with self.assertRaises(json_sync.JsonSyncFatalError):
                await json_sync._send_json_single_via_user(
                    -100456,
                    "document",
                    "fake.txt",
                    "caption",
                    None,
                    json_sync.SharedUploadProgressTracker("上传中", 1),
                    "上传文件: fake.txt",
                )

    async def test_send_json_single_via_user_omits_none_caption(self):
        with patch("sync_worker.json_import.process.bot_engine.pyro_user_app") as mock_app:
            mock_app.is_initialized = True
            mock_app.send_photo = AsyncMock(return_value=type("Sent", (), {"id": 1001})())
            with patch("sync_worker.json_import.process.os.path.getsize", return_value=3):
                await json_sync._send_json_single_via_user(
                    -100456,
                    "photo",
                    "fake.jpg",
                    None,
                    None,
                    json_sync.SharedUploadProgressTracker("上传中", 3),
                    "上传图片: fake.jpg",
                )

        kwargs = mock_app.send_photo.await_args.kwargs
        self.assertNotIn("caption", kwargs)
        self.assertNotIn("parse_mode", kwargs)

    async def test_send_json_single_via_user_passes_empty_emoji_for_sticker(self):
        with patch("sync_worker.json_import.process.bot_engine.pyro_user_app") as mock_app:
            mock_app.is_initialized = True
            mock_app.send_sticker = AsyncMock(return_value=type("Sent", (), {"id": 1002})())
            with patch("sync_worker.json_import.process.os.path.getsize", return_value=3):
                await json_sync._send_json_single_via_user(
                    -100456,
                    "sticker",
                    "fake.webp",
                    None,
                    None,
                    json_sync.SharedUploadProgressTracker("上传中", 3),
                    "上传贴纸: fake.webp",
                )

        kwargs = mock_app.send_sticker.await_args.kwargs
        self.assertEqual(kwargs["emoji"], "")

    async def test_execute_with_retry_retries_pyrogram_wait_phrase(self):
        attempts = {"count": 0}

        async def flaky_call():
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise Exception("Telegram says: [420 FLOOD_WAIT_X] Pyrogram thinks: A wait of 123 seconds is required.")
            return "ok"

        async def passthrough(factory, **_kwargs):
            return await factory()

        with patch("sync_worker.json_import.helpers.execute_with_network_retry", AsyncMock(side_effect=passthrough)), \
             patch("sync_worker.json_import.helpers.db.add_msg_log", AsyncMock()) as mock_add_msg_log, \
             patch("sync_worker.json_import.helpers._sleep_retry_delay", AsyncMock()) as mock_sleep:
            result = await json_sync._execute_with_retry(
                flaky_call,
                action_label="文本消息 -> 辅助账号发送",
            )

        self.assertEqual(result, "ok")
        self.assertEqual(attempts["count"], 2)
        mock_sleep.assert_awaited_once_with(124)
        mock_add_msg_log.assert_any_await("JSON_RETRY", "文本消息 -> 辅助账号发送 | 遇到频控，等待 124 秒后重试")

    def test_group_json_messages_groups_documents_without_mixing_visual_media(self):
        messages = [
            {
                "id": 97,
                "type": "message",
                "date_unixtime": "1776427654",
                "file": "files/testfile.txt",
                "text": "文件组中1介绍",
            },
            {
                "id": 98,
                "type": "message",
                "date_unixtime": "1776427654",
                "file": "files/testfile (1).txt",
                "text": "文件组中2介绍",
            },
            {
                "id": 99,
                "type": "message",
                "date_unixtime": "1776427655",
                "file": "files/testfile (2).txt",
                "text": "文件组中3介绍",
            },
            {
                "id": 100,
                "type": "message",
                "date_unixtime": "1776427656",
                "file": "files/testfile (3).txt",
                "text": "",
            },
            {
                "id": 101,
                "type": "message",
                "date_unixtime": "1776427656",
                "photo": "photos/pic.jpg",
                "text": "图片说明",
            },
        ]

        grouped = json_sync.group_json_messages(messages, 3)

        self.assertEqual([[item["id"] for item in group] for group in grouped], [[97, 98, 99, 100], [101]])

    def test_group_json_messages_splits_explicit_media_group_above_10_items(self):
        messages = [
            {
                "id": index,
                "type": "message",
                "date_unixtime": str(index),
                "photo": f"photos/{index}.jpg",
                "media_group_id": "album-1",
                "text": "" if index > 1 else "首条说明",
            }
            for index in range(1, 12)
        ]

        grouped = json_sync.group_json_messages(messages, 3)

        self.assertEqual([[item["id"] for item in group] for group in grouped], [list(range(1, 11)), [11]])

    def test_group_json_messages_keeps_visual_group_with_multiple_captions(self):
        messages = [
            {
                "id": 1,
                "type": "message",
                "date_unixtime": "1776427654",
                "photo": "photos/1.jpg",
                "text": "第一条说明",
            },
            {
                "id": 2,
                "type": "message",
                "date_unixtime": "1776427654",
                "photo": "photos/2.jpg",
                "text": "第二条说明",
            },
            {
                "id": 3,
                "type": "message",
                "date_unixtime": "1776427655",
                "photo": "photos/3.jpg",
                "text": "",
            },
        ]

        grouped = json_sync.group_json_messages(messages, 3)

        self.assertEqual([[item["id"] for item in group] for group in grouped], [[1, 2, 3]])

    def test_group_json_messages_splits_heuristic_media_group_above_10_items(self):
        messages = [
            {
                "id": index,
                "type": "message",
                "date_unixtime": "1776427654",
                "file": f"files/testfile-{index}.txt",
                "text": "",
            }
            for index in range(1, 12)
        ]

        grouped = json_sync.group_json_messages(messages, 3)

        self.assertEqual([[item["id"] for item in group] for group in grouped], [list(range(1, 11)), [11]])

    def test_group_json_messages_keeps_long_window_sequence_when_later_items_reply_to_first(self):
        messages = [
            {
                "id": index,
                "type": "message",
                "date_unixtime": "1777273064" if index <= 18 else "1777273065",
                "photo": f"photos/{index}.jpg",
                "text": f"图片说明 {index}/32",
                **({"reply_to_message_id": 6} if index >= 16 else {}),
            }
            for index in range(6, 38)
        ]

        grouped = json_sync.group_json_messages(messages, 3)

        self.assertEqual(
            [[item["id"] for item in group] for group in grouped],
            [list(range(6, 16)), list(range(16, 26)), list(range(26, 36)), [36, 37]],
        )

    def test_group_json_messages_does_not_merge_reply_to_message_outside_window(self):
        messages = [
            {
                "id": 1,
                "type": "message",
                "date_unixtime": "1777273064",
                "photo": "photos/1.jpg",
                "text": "图片说明 1",
            },
            {
                "id": 2,
                "type": "message",
                "date_unixtime": "1777273064",
                "photo": "photos/2.jpg",
                "text": "图片说明 2",
                "reply_to_message_id": 999,
            },
        ]

        grouped = json_sync.group_json_messages(messages, 3)

        self.assertEqual([[item["id"] for item in group] for group in grouped], [[1], [2]])

    def test_group_json_messages_keeps_same_external_reply_in_one_group(self):
        messages = [
            {
                "id": index,
                "type": "message",
                "date_unixtime": "1777273064",
                "photo": f"photos/{index}.jpg",
                "text": f"图片说明 {index}",
                "reply_to_message_id": 1,
            }
            for index in range(10, 13)
        ]

        grouped = json_sync.group_json_messages(messages, 3)

        self.assertEqual([[item["id"] for item in group] for group in grouped], [[10, 11, 12]])

    def test_group_json_messages_groups_visual_video_files_with_photo(self):
        messages = [
            {
                "id": 38,
                "type": "message",
                "date_unixtime": "1778421550",
                "media_type": "video_file",
                "mime_type": "video/mp4",
                "file": "video_files/640 (1).mp4",
                "thumbnail": "video_files/640 (1).mp4_thumb.jpg",
                "width": 720,
                "height": 404,
                "duration_seconds": 3,
                "text": "caption",
            },
            {
                "id": 39,
                "type": "message",
                "date_unixtime": "1778421550",
                "media_type": "video_file",
                "mime_type": "video/mp4",
                "file": "video_files/640.mp4",
                "thumbnail": "video_files/640.mp4_thumb.jpg",
                "width": 626,
                "height": 352,
                "duration_seconds": 2,
                "text": "",
            },
            {
                "id": 40,
                "type": "message",
                "date_unixtime": "1778421550",
                "photo": "photos/photo_1.jpg",
                "width": 852,
                "height": 1280,
                "text": "",
            },
        ]

        grouped = json_sync.group_json_messages(messages, 3)

        self.assertEqual([[item["id"] for item in group] for group in grouped], [[38, 39, 40]])

    def test_split_json_text_for_send_keeps_html_balanced(self):
        text = "<b>" + ("x" * 5000) + "</b>"

        parts = json_sync._split_json_text_for_send(text)

        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(part) <= json_sync.JSON_TEXT_MESSAGE_LIMIT for part in parts))
        self.assertTrue(all(part.startswith("<b>") for part in parts))
        self.assertTrue(all(part.endswith("</b>") for part in parts))

    async def test_send_json_media_group_keeps_document_captions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            for name in ["a.txt", "b.txt"]:
                (Path(temp_dir) / name).write_text(name, encoding="utf-8")

            group = [
                {
                    "id": 1,
                    "type": "message",
                    "date_unixtime": "1",
                    "file": "a.txt",
                    "text": "第一条说明",
                },
                {
                    "id": 2,
                    "type": "message",
                    "date_unixtime": "2",
                    "file": "b.txt",
                    "text": "第二条说明",
                },
            ]

            mock_send = AsyncMock(return_value=[FakeSentMessage(1001), FakeSentMessage(1002)])
            with patch("sync_worker.json_import.process.update_state_and_check_skip", AsyncMock(return_value=False)), \
                 patch("sync_worker.json_import.process.resolve_reply_target", AsyncMock(return_value=None)), \
                 patch("sync_worker.json_import.process.rewrite_message_links", AsyncMock(side_effect=lambda text, *_: (text, 0))), \
                 patch("sync_worker.json_import.process.record_success", AsyncMock()), \
                 patch("sync_worker.json_import.process.db.add_msg_log", AsyncMock()), \
                 patch("sync_worker.json_import.process.bot_engine.aiogram_bot") as mock_bot:
                mock_bot.send_media_group = mock_send

                await json_sync.send_json_media_group(group, -100456, temp_dir, 0, False, {}, "bot", True)

            media = mock_send.await_args.args[1]
            self.assertEqual([item.caption for item in media], ["第一条说明", "第二条说明"])

    async def test_send_json_media_group_keeps_video_file_as_documents(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            videos_dir = Path(temp_dir) / "video_files"
            videos_dir.mkdir()
            for name in ["a.mp4", "b.mp4"]:
                (videos_dir / name).write_bytes(b"mp4")

            group = [
                {
                    "id": 1,
                    "type": "message",
                    "date_unixtime": "1",
                    "media_type": "video_file",
                    "file": "video_files/a.mp4",
                    "text": "第一条说明",
                },
                {
                    "id": 2,
                    "type": "message",
                    "date_unixtime": "2",
                    "media_type": "video_file",
                    "file": "video_files/b.mp4",
                    "text": "第二条说明",
                },
            ]

            mock_send = AsyncMock(return_value=[FakeSentMessage(1001), FakeSentMessage(1002)])
            with patch("sync_worker.json_import.process.update_state_and_check_skip", AsyncMock(return_value=False)), \
                 patch("sync_worker.json_import.process.resolve_reply_target", AsyncMock(return_value=None)), \
                 patch("sync_worker.json_import.process.rewrite_message_links", AsyncMock(side_effect=lambda text, *_: (text, 0))), \
                 patch("sync_worker.json_import.process.record_success", AsyncMock()), \
                 patch("sync_worker.json_import.process.db.add_msg_log", AsyncMock()), \
                 patch("sync_worker.json_import.process.bot_engine.aiogram_bot") as mock_bot:
                mock_bot.send_media_group = mock_send

                await json_sync.send_json_media_group(group, -100456, temp_dir, 0, False, {}, "bot", True)

            media = mock_send.await_args.args[1]
            self.assertTrue(all(item.__class__.__name__ == "InputMediaDocument" for item in media))

    async def test_send_json_media_group_keeps_media_spoiler(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            photos_dir = Path(temp_dir) / "photos"
            photos_dir.mkdir()
            for name in ["a.jpg", "b.jpg"]:
                (photos_dir / name).write_bytes(b"jpg")

            group = [
                {
                    "id": 1,
                    "type": "message",
                    "date_unixtime": "1",
                    "photo": "photos/a.jpg",
                    "media_spoiler": True,
                    "text": "第一条说明",
                },
                {
                    "id": 2,
                    "type": "message",
                    "date_unixtime": "2",
                    "photo": "photos/b.jpg",
                    "text": "",
                },
            ]

            mock_send = AsyncMock(return_value=[FakeSentMessage(1001), FakeSentMessage(1002)])
            with patch("sync_worker.json_import.process.update_state_and_check_skip", AsyncMock(return_value=False)), \
                 patch("sync_worker.json_import.process.resolve_reply_target", AsyncMock(return_value=None)), \
                 patch("sync_worker.json_import.process.rewrite_message_links", AsyncMock(side_effect=lambda text, *_: (text, 0))), \
                 patch("sync_worker.json_import.process.record_success", AsyncMock()), \
                 patch("sync_worker.json_import.process.db.add_msg_log", AsyncMock()), \
                 patch("sync_worker.json_import.process.bot_engine.aiogram_bot") as mock_bot:
                mock_bot.send_media_group = mock_send

                await json_sync.send_json_media_group(group, -100456, temp_dir, 0, False, {}, "bot", True)

            media = mock_send.await_args.args[1]
            self.assertTrue(media[0].has_spoiler)
            self.assertIsNone(media[1].has_spoiler)

    async def test_send_json_media_group_adds_external_header_to_first_captioned_visual_item(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            photos_dir = Path(temp_dir) / "photos"
            photos_dir.mkdir()
            for name in ["a.jpg", "b.jpg", "c.jpg"]:
                (photos_dir / name).write_bytes(b"jpg")

            group = [
                {
                    "id": 1,
                    "type": "message",
                    "date_unixtime": "1",
                    "photo": "photos/a.jpg",
                    "forwarded_from": "test_channel",
                    "forwarded_from_id": "channel3717669322",
                    "forwarded_from_message_id": "888",
                    "text": "",
                },
                {
                    "id": 2,
                    "type": "message",
                    "date_unixtime": "2",
                    "photo": "photos/b.jpg",
                    "forwarded_from": "test_channel",
                    "forwarded_from_id": "channel3717669322",
                    "forwarded_from_message_id": "888",
                    "text": "第二张说明",
                },
                {
                    "id": 3,
                    "type": "message",
                    "date_unixtime": "3",
                    "photo": "photos/c.jpg",
                    "forwarded_from": "test_channel",
                    "forwarded_from_id": "channel3717669322",
                    "forwarded_from_message_id": "888",
                    "text": "第三张说明",
                },
            ]

            mock_send = AsyncMock(return_value=[FakeSentMessage(1001), FakeSentMessage(1002), FakeSentMessage(1003)])
            with patch("sync_worker.json_import.process.update_state_and_check_skip", AsyncMock(return_value=False)), \
                 patch("sync_worker.json_import.process.resolve_reply_target", AsyncMock(return_value=None)), \
                 patch("sync_worker.json_import.process.rewrite_message_links", AsyncMock(side_effect=lambda text, *_: (text, 0))), \
                 patch("sync_worker.json_import.process.record_success", AsyncMock()), \
                 patch("sync_worker.json_import.process.db.add_msg_log", AsyncMock()), \
                 patch("sync_worker.json_import.process.bot_engine.aiogram_bot") as mock_bot:
                mock_bot.send_media_group = mock_send

                await json_sync.send_json_media_group(
                    group,
                    -100456,
                    temp_dir,
                    0,
                    False,
                    {},
                    "bot",
                    True,
                    False,
                    True,
                )

            media = mock_send.await_args.args[1]
            self.assertEqual(media[0].caption, "")
            self.assertIn("#转发自", media[1].caption or "")
            self.assertIn("第二张说明", media[1].caption or "")
            self.assertNotIn("#转发自", media[2].caption or "")
            self.assertIn("第三张说明", media[2].caption or "")

    async def test_send_json_media_group_user_topics_error_does_not_retry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            photos_dir = Path(temp_dir) / "photos"
            photos_dir.mkdir()
            for name in ["a.jpg", "b.jpg"]:
                (photos_dir / name).write_bytes(b"jpg")

            group = [
                {
                    "id": 1,
                    "type": "message",
                    "date_unixtime": "1",
                    "photo": "photos/a.jpg",
                    "text": "第一张",
                },
                {
                    "id": 2,
                    "type": "message",
                    "date_unixtime": "2",
                    "photo": "photos/b.jpg",
                    "text": "",
                },
            ]

            mock_send = AsyncMock(side_effect=TypeError("Messages.__init__() missing 1 required keyword-only argument: 'topics'"))
            with patch("sync_worker.json_import.process.update_state_and_check_skip", AsyncMock(return_value=False)), \
                 patch("sync_worker.json_import.process.resolve_reply_target", AsyncMock(return_value=None)), \
                 patch("sync_worker.json_import.process.rewrite_message_links", AsyncMock(side_effect=lambda text, *_: (text, 0))), \
                 patch("sync_worker.json_import.process.record_success", AsyncMock()) as mock_record_success, \
                 patch("sync_worker.json_import.process.db.add_msg_log", AsyncMock()) as mock_add_msg_log, \
                 patch("sync_worker.json_import.process.bot_engine.pyro_user_app") as mock_app, \
                 patch("sync_worker.json_import.process._select_json_upload_target", AsyncMock(return_value={"sender": "user", "client": object(), "label": "辅助账号"})):
                mock_app.is_initialized = True
                mock_app.send_media_group = mock_send

                result = await json_sync.send_json_media_group(group, -100456, temp_dir, 0, False, {}, "bot", True)

            self.assertEqual(mock_send.await_count, 1)
            self.assertEqual(result, json_sync.JSON_GROUP_SENT_UNMAPPED)
            mock_record_success.assert_not_awaited()
            self.assertIn(
                ("JSON_TOPICS_COMPAT", "组首消息ID:1 | 辅助账号发送后返回 topics 解析异常，已停止重试避免重复发送"),
                [call.args for call in mock_add_msg_log.await_args_list],
            )
            self.assertIn(
                ("JSON_GROUP_SEND_UNMAPPED", "组首消息ID:1 | 共 2 条 | 目标:[-100456] | 可能已发送，回包解析失败，未记录映射"),
                [call.args for call in mock_add_msg_log.await_args_list],
            )

    async def test_send_json_media_group_cleans_temp_file_when_prepare_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            photos_dir = Path(temp_dir) / "photos"
            photos_dir.mkdir()
            for name in ["a.jpg", "b.jpg"]:
                (photos_dir / name).write_bytes(b"jpg")
            temp_media = Path(temp_dir) / "prepared.tmp"

            group = [
                {"id": 1, "type": "message", "date_unixtime": "1", "photo": "photos/a.jpg", "text": ""},
                {"id": 2, "type": "message", "date_unixtime": "2", "photo": "photos/b.jpg", "text": ""},
            ]

            async def prepare(media_path, _media_type, item_id, _hash_perturb):
                if item_id == 1:
                    temp_media.write_bytes(b"temp")
                    return str(temp_media), True
                raise RuntimeError("prepare failed")

            with patch("sync_worker.json_import.process.update_state_and_check_skip", AsyncMock(return_value=False)), \
                 patch("sync_worker.json_import.process.rewrite_message_links", AsyncMock(side_effect=lambda text, *_: (text, 0))), \
                 patch("sync_worker.json_import.process._prepare_json_media_path", AsyncMock(side_effect=prepare)), \
                 patch("sync_worker.json_import.process.record_success", AsyncMock()) as mock_record_success:
                with self.assertRaises(RuntimeError):
                    await json_sync.send_json_media_group(group, -100456, temp_dir, 0, False, {}, "bot", True)

            self.assertFalse(temp_media.exists())
            mock_record_success.assert_not_awaited()

    async def test_process_json_sync_respects_type_filter(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            json_path = Path(temp_dir) / "result.json"
            json_path.write_text(
                json.dumps(
                    {
                        "messages": [
                            {
                                "id": 1,
                                "type": "message",
                                "photo": "photos/pic.jpg",
                                "text": "caption",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with patch("sync_worker.json_import.process.resolve_chat_id", AsyncMock(return_value=-100456)), \
                 patch("sync_worker.json_import.process.build_link_rewrite_context", AsyncMock(return_value={})), \
                 patch("sync_worker.json_import.process.db.get_all_settings", AsyncMock(return_value={"sync_photo": "0"})), \
                 patch("sync_worker.json_import.process.db.add_msg_log", AsyncMock()) as mock_add_msg_log, \
                 patch("sync_worker.json_import.process.db.apply_message_filters", AsyncMock(return_value=(False, "caption"))), \
                 patch("sync_worker.json_import.process.update_state_and_check_skip", AsyncMock(return_value=False)), \
                 patch("sync_worker.json_import.process.record_success", AsyncMock()), \
                 patch("sync_worker.json_import.process.bot_engine.aiogram_bot") as mock_bot:
                mock_bot.send_photo = AsyncMock(return_value=FakeSentMessage(1001))

                await json_sync.process_json_sync("bot", "@target", str(json_path), 0.5, False)

            mock_bot.send_photo.assert_not_awaited()
            mock_add_msg_log.assert_any_await("JSON_DROP_TYPE", "消息ID:1 | 类型:photo | 已被类型过滤拦截")

    async def test_process_json_sync_respects_regex_filter(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            json_path = Path(temp_dir) / "result.json"
            json_path.write_text(
                json.dumps(
                    {
                        "messages": [
                            {
                                "id": 2,
                                "type": "message",
                                "text": "blocked content",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with patch("sync_worker.json_import.process.resolve_chat_id", AsyncMock(return_value=-100456)), \
                 patch("sync_worker.json_import.process.build_link_rewrite_context", AsyncMock(return_value={})), \
                 patch("sync_worker.json_import.process.db.get_all_settings", AsyncMock(return_value={"sync_text": "1"})), \
                 patch("sync_worker.json_import.process.db.add_msg_log", AsyncMock()) as mock_add_msg_log, \
                 patch("sync_worker.json_import.process.db.apply_message_filters", AsyncMock(return_value=(True, "blocked content"))), \
                 patch("sync_worker.json_import.process.update_state_and_check_skip", AsyncMock(return_value=False)), \
                 patch("sync_worker.json_import.process.record_success", AsyncMock()), \
                 patch("sync_worker.json_import.process.bot_engine.aiogram_bot") as mock_bot:
                mock_bot.send_message = AsyncMock(return_value=FakeSentMessage(1002))

                await json_sync.process_json_sync("bot", "@target", str(json_path), 0.5, False)

    async def test_json_media_group_drop_filter_blocks_whole_group(self):
        blocked_text = (
            "豆包提示词 视频教学 手机电脑均可使用 一张照片即可做出想要ai视频 "
            "进群学习提示词p图自助下单哦 @doubao40bot 预览群 https://t.me/+8F1U-0uMIys5ZmM1"
        )
        group = [
            {"id": 1, "type": "message", "photo": "one.jpg", "text": ""},
            {"id": 2, "type": "message", "photo": "two.jpg", "text": blocked_text},
        ]

        with patch("sync_worker.json_import.process.update_state_and_check_skip", AsyncMock(return_value=False)), \
             patch("sync_worker.json_import.process.db.apply_message_filters", AsyncMock(side_effect=[(False, ""), (True, blocked_text)])) as mock_filter, \
             patch("sync_worker.json_import.process._prepare_json_media_group", AsyncMock(return_value=None)) as mock_prepare, \
             patch("sync_worker.json_import.process.db.add_msg_log", AsyncMock()):
            result = await json_sync.send_json_media_group(
                group,
                -100456,
                "unused",
                -100123,
                False,
                {},
                "bot",
                True,
            )

        self.assertEqual(result, "skipped")
        self.assertEqual(mock_filter.await_count, 2)
        mock_prepare.assert_not_awaited()

    async def test_process_json_sync_keeps_single_photo_spoiler(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            photos_dir = Path(temp_dir) / "photos"
            photos_dir.mkdir()
            (photos_dir / "pic.jpg").write_bytes(b"jpg")
            json_path = Path(temp_dir) / "result.json"
            json_path.write_text(
                json.dumps(
                    {
                        "messages": [
                            {
                                "id": 5,
                                "type": "message",
                                "photo": "photos/pic.jpg",
                                "media_spoiler": True,
                                "text": "caption",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with patch("sync_worker.json_import.process.resolve_chat_id", AsyncMock(return_value=-100456)), \
                 patch("sync_worker.json_import.process.build_link_rewrite_context", AsyncMock(return_value={})), \
                 patch("sync_worker.json_import.process.resolve_reply_target", AsyncMock(return_value=None)), \
                 patch("sync_worker.json_import.process.db.get_all_settings", AsyncMock(return_value={"sync_photo": "1"})), \
                 patch("sync_worker.json_import.process.db.add_msg_log", AsyncMock()), \
                 patch("sync_worker.json_import.process.db.apply_message_filters", AsyncMock(return_value=(False, "caption"))), \
                 patch("sync_worker.json_import.process.update_state_and_check_skip", AsyncMock(return_value=False)), \
                 patch("sync_worker.json_import.process.record_success", AsyncMock()), \
                 patch("sync_worker.json_import.process.bot_engine.note_upload_success", AsyncMock()), \
                 patch("sync_worker.json_import.process.bot_engine.aiogram_bot") as mock_bot:
                mock_bot.send_photo = AsyncMock(return_value=FakeSentMessage(1005))

                await json_sync.process_json_sync("bot", "@target", str(json_path), 0.5, False)

            self.assertTrue(mock_bot.send_photo.await_args.kwargs["has_spoiler"])

    async def test_process_json_sync_can_send_text_via_user(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            json_path = Path(temp_dir) / "result.json"
            json_path.write_text(
                json.dumps(
                    {
                        "messages": [
                            {
                                "id": 3,
                                "type": "message",
                                "text": "hello",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            fake_user = type("FakeUser", (), {"is_initialized": True, "send_message": AsyncMock(return_value=FakeSentMessage(1003))})()
            with patch("sync_worker.json_import.process.resolve_chat_id", AsyncMock(return_value=-100456)), \
                 patch("sync_worker.json_import.process.build_link_rewrite_context", AsyncMock(return_value={})), \
                 patch("sync_worker.json_import.process.db.get_all_settings", AsyncMock(return_value={"sync_text": "1"})), \
                 patch("sync_worker.json_import.process.db.add_msg_log", AsyncMock()), \
                 patch("sync_worker.json_import.process.db.apply_message_filters", AsyncMock(return_value=(False, "hello"))), \
                 patch("sync_worker.json_import.process.update_state_and_check_skip", AsyncMock(return_value=False)), \
                 patch("sync_worker.json_import.process.record_success", AsyncMock()), \
                 patch("sync_worker.json_import.process.bot_engine.pyro_user_app", fake_user), \
                     patch("sync_worker.json_import.process.bot_engine.aiogram_bot"):
                await json_sync.process_json_sync("user", "@target", str(json_path), 0.5, False)

            fake_user.send_message.assert_awaited_once()
            self.assertEqual(fake_user.send_message.await_args.kwargs["chat_id"], -100456)

    async def test_process_json_sync_splits_long_text_via_user(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            json_path = Path(temp_dir) / "result.json"
            json_path.write_text(
                json.dumps(
                    {
                        "messages": [
                            {
                                "id": 30,
                                "type": "message",
                                "text": "<" + ("x" * 5000),
                                "reply_to_message_id": 20,
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            fake_user = type("FakeUser", (), {"is_initialized": True, "send_message": AsyncMock(side_effect=[FakeSentMessage(1030), FakeSentMessage(1031)])})()
            with patch("sync_worker.json_import.process.resolve_chat_id", AsyncMock(return_value=-100456)), \
                 patch("sync_worker.json_import.process.build_link_rewrite_context", AsyncMock(return_value={})), \
                 patch("sync_worker.json_import.process.resolve_reply_target", AsyncMock(return_value=999)), \
                 patch("sync_worker.json_import.process.db.get_all_settings", AsyncMock(return_value={"sync_text": "1"})), \
                 patch("sync_worker.json_import.process.db.add_msg_log", AsyncMock()), \
                 patch("sync_worker.json_import.process.db.apply_message_filters", AsyncMock(side_effect=lambda text, *_: (False, text))), \
                 patch("sync_worker.json_import.process.update_state_and_check_skip", AsyncMock(return_value=False)), \
                 patch("sync_worker.json_import.process.record_success", AsyncMock()) as mock_record_success, \
                 patch("sync_worker.json_import.process.bot_engine.pyro_user_app", fake_user), \
                     patch("sync_worker.json_import.process.bot_engine.aiogram_bot"):
                await json_sync.process_json_sync("user", "@target", str(json_path), 0, False)

            self.assertEqual(fake_user.send_message.await_count, 2)
            first_kwargs = fake_user.send_message.await_args_list[0].kwargs
            second_kwargs = fake_user.send_message.await_args_list[1].kwargs
            self.assertEqual(first_kwargs["reply_to_message_id"], 999)
            self.assertNotIn("reply_to_message_id", second_kwargs)
            mock_record_success.assert_awaited_once_with(0, -100456, 30, 1030, force_send=False)

    async def test_process_json_sync_user_text_accepts_pyrogram_message_id_field(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            json_path = Path(temp_dir) / "result.json"
            json_path.write_text(
                json.dumps(
                    {
                        "messages": [
                            {
                                "id": 10,
                                "type": "message",
                                "text": "hello",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            fake_user = type("FakeUser", (), {"is_initialized": True, "send_message": AsyncMock(return_value=type("Sent", (), {"id": 1010})())})()
            with patch("sync_worker.json_import.process.resolve_chat_id", AsyncMock(return_value=-100456)), \
                 patch("sync_worker.json_import.process.build_link_rewrite_context", AsyncMock(return_value={})), \
                 patch("sync_worker.json_import.process.resolve_reply_target", AsyncMock(return_value=None)), \
                 patch("sync_worker.json_import.process.update_state_and_check_skip", AsyncMock(return_value=False)), \
                 patch("sync_worker.json_import.process.rewrite_message_links", AsyncMock(side_effect=lambda text, *_: (text, 0))), \
                 patch("sync_worker.json_import.process.db.get_all_settings", AsyncMock(return_value={"sync_text": "1"})), \
                 patch("sync_worker.json_import.process.db.add_msg_log", AsyncMock()), \
                 patch("sync_worker.json_import.process.db.apply_message_filters", AsyncMock(return_value=(False, "hello"))), \
                 patch("sync_worker.json_import.process.record_success", AsyncMock()) as mock_record_success, \
                 patch("sync_worker.json_import.process.bot_engine.pyro_user_app", fake_user), \
                 patch("sync_worker.json_import.process.bot_engine.aiogram_bot"):
                await json_sync.process_json_sync("user", "@target", str(json_path), 0, False)

            mock_record_success.assert_awaited_once_with(0, -100456, 10, 1010, force_send=False)

    async def test_process_json_sync_can_prepend_external_source_header(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            json_path = Path(temp_dir) / "result.json"
            json_path.write_text(
                json.dumps(
                    {
                        "messages": [
                            {
                                "id": 4,
                                "type": "message",
                                "forwarded_from": "test_channel",
                                "forwarded_from_id": "channel3717669322",
                                "forwarded_from_message_id": "888",
                                "reply_to_peer_id": "-100123",
                                "reply_to_message_id": "456",
                                "text": [
                                    {"type": "pre", "text": '{"a":1}', "language": "Json"},
                                ],
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with patch("sync_worker.json_import.process.resolve_chat_id", AsyncMock(return_value=-100456)), \
                 patch("sync_worker.json_import.process.build_link_rewrite_context", AsyncMock(return_value={})), \
                 patch("sync_worker.json_import.process.resolve_reply_target", AsyncMock(return_value=None)), \
                 patch("sync_worker.json_import.process.db.get_all_settings", AsyncMock(return_value={"sync_text": "1", "add_external_source_header": True})), \
                 patch("sync_worker.json_import.process.db.add_msg_log", AsyncMock()), \
                 patch("sync_worker.json_import.process.db.apply_message_filters", AsyncMock(side_effect=lambda text, *_: (False, text))), \
                 patch("sync_worker.json_import.process.update_state_and_check_skip", AsyncMock(return_value=False)), \
                 patch("sync_worker.json_import.process.record_success", AsyncMock()), \
                 patch("sync_worker.json_import.process.bot_engine.aiogram_bot") as mock_bot:
                mock_bot.send_message = AsyncMock(return_value=FakeSentMessage(1004))

                await json_sync.process_json_sync("bot", "@target", str(json_path), 0.5, False)

            sent_text = mock_bot.send_message.await_args.args[1]
            self.assertIn('href="https://t.me/c/3717669322/888"', sent_text)
            self.assertIn("#转发自", sent_text)
            self.assertIn(">test_channel</a>", sent_text)
            self.assertIn('href="https://t.me/c/123/456"', sent_text)
            self.assertIn('<pre><code class="language-Json">{&quot;a&quot;:1}</code></pre>', sent_text)

    async def test_process_json_sync_counts_media_group_progress_by_message(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            photos_dir = Path(temp_dir) / "photos"
            photos_dir.mkdir()
            for name in ["1.jpg", "2.jpg", "3.jpg"]:
                (photos_dir / name).write_bytes(b"jpg")

            json_path = Path(temp_dir) / "result.json"
            json_path.write_text(
                json.dumps(
                    {
                        "messages": [
                            {
                                "id": 1,
                                "type": "message",
                                "photo": "photos/1.jpg",
                                "date_unixtime": "100",
                                "text": "第一张",
                            },
                            {
                                "id": 2,
                                "type": "message",
                                "photo": "photos/2.jpg",
                                "date_unixtime": "100",
                                "text": "",
                            },
                            {
                                "id": 3,
                                "type": "message",
                                "photo": "photos/3.jpg",
                                "date_unixtime": "100",
                                "text": "",
                            },
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            mock_bot = type("FakeBot", (), {"send_media_group": AsyncMock(return_value=[FakeSentMessage(1001), FakeSentMessage(1002), FakeSentMessage(1003)])})()
            original_current = json_sync.sync_state["current"]
            original_total = json_sync.sync_state["total"]
            original_stop = json_sync.sync_state["stop_requested"]
            try:
                json_sync.sync_state["current"] = 0
                json_sync.sync_state["total"] = 0
                json_sync.sync_state["stop_requested"] = False

                with patch("sync_worker.json_import.process.resolve_chat_id", AsyncMock(return_value=-100456)), \
                     patch("sync_worker.json_import.process.build_link_rewrite_context", AsyncMock(return_value={})), \
                     patch("sync_worker.json_import.process.resolve_reply_target", AsyncMock(return_value=None)), \
                     patch("sync_worker.json_import.process.rewrite_message_links", AsyncMock(side_effect=lambda text, *_: (text, 0))), \
                     patch("sync_worker.json_import.process.record_success", AsyncMock()), \
                     patch("sync_worker.json_import.process.bot_engine.note_upload_success", AsyncMock()), \
                     patch("sync_worker.json_import.process._select_json_upload_target", AsyncMock(return_value={"sender": "bot", "client": mock_bot, "label": "Bot"})), \
                     patch("sync_worker.json_import.process.db.get_all_settings", AsyncMock(return_value={"sync_photo": "1"})), \
                     patch("sync_worker.json_import.process.db.apply_message_filters", AsyncMock(side_effect=lambda text, *_: (False, text))), \
                     patch("sync_worker.json_import.process.db.add_msg_log", AsyncMock()), \
                     patch("sync_worker.runtime.state.db.is_message_synced", AsyncMock(return_value=False)):
                    await json_sync.process_json_sync("bot", "@target", str(json_path), 0, False)

                self.assertEqual(json_sync.sync_state["total"], 3)
                self.assertEqual(json_sync.sync_state["current"], 3)
            finally:
                json_sync.sync_state["current"] = original_current
                json_sync.sync_state["total"] = original_total
                json_sync.sync_state["stop_requested"] = original_stop

    async def test_prepare_json_media_path_preserves_original_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            original = Path(temp_dir) / "photo.jpg"
            original.write_bytes(b"abc123")
            with patch("sync_worker.json_import.process.TEMP_DIR", temp_dir), \
                 patch("sync_worker.json_import.process.db.add_msg_log", AsyncMock()):
                prepared_path, created_temp = await json_sync._prepare_json_media_path(str(original), "photo", 11, True)

            self.assertTrue(created_temp)
            self.assertNotEqual(prepared_path, str(original))
            self.assertEqual(original.read_bytes(), b"abc123")
            self.assertTrue(Path(prepared_path).exists())
            self.assertGreater(Path(prepared_path).stat().st_size, original.stat().st_size)
