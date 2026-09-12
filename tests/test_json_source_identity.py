import json
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, mock_open, patch

from sync_worker.json_import import process as importer


class JsonSourceIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def source_for(self, data, path="result.json", username=""):
        with ExitStack() as stack:
            stack.enter_context(patch.object(importer, "load_json_export", return_value=data))
            stack.enter_context(patch.object(importer, "resolve_chat_id", AsyncMock(return_value=-100456)))
            context = stack.enter_context(patch.object(importer, "build_link_rewrite_context", AsyncMock(return_value={})))
            stack.enter_context(patch.object(importer.db, "get_all_settings", AsyncMock(return_value={})))
            stack.enter_context(patch.object(importer.db, "add_msg_log", AsyncMock()))
            stack.enter_context(patch.object(importer, "get_config", return_value={}))
            stack.enter_context(patch.object(importer, "group_json_messages", return_value=[]))
            stack.enter_context(patch.object(importer, "_log_json_import_scan", AsyncMock()))
            await importer.process_json_sync("bot", "target", path, 0.5, False, username)
            return context.await_args.args[1]

    async def test_two_exported_channels_do_not_share_scope(self):
        first = await self.source_for({"id": 123, "type": "private_channel", "messages": []})
        second = await self.source_for({"id": 124, "type": "private_channel", "messages": []})
        self.assertEqual(first, -1000000000123)
        self.assertNotEqual(first, second)

    async def test_identity_survives_rename_move_and_new_export_messages(self):
        first = await self.source_for({"id": 123, "type": "public_channel", "name": "Old", "messages": []})
        second = await self.source_for({"id": 123, "type": "public_channel", "name": "New", "messages": [{"id": 99}]}, "moved/result.json")
        self.assertEqual(first, second)
        self.assertNotEqual(first, 0)

    async def test_unidentified_exports_use_content_not_path(self):
        data = {"messages": [{"id": 1, "text": "one"}]}
        first = await self.source_for(data)
        self.assertNotEqual(first, 0)
        self.assertEqual(first, await self.source_for(data, "moved.json"))
        self.assertNotEqual(first, await self.source_for({"messages": [{"id": 1, "text": "two"}]}))

    async def test_explicit_username_keeps_existing_identity(self):
        self.assertEqual(await self.source_for({"messages": []}, username="@source"), -100456)

    async def test_invalid_export_id_falls_back_and_does_not_break_username(self):
        data = {"id": "--123", "type": "private_channel", "messages": []}
        self.assertNotEqual(await self.source_for(data), 0)
        self.assertEqual(await self.source_for(data, username="@source"), -100456)
