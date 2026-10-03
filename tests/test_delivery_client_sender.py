import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot_engine
from sync_worker.runtime.state import DeliveryGuard
from sync_worker.senders.single import dynamic_send


class DeliveryClientSenderTests(unittest.IsolatedAsyncioTestCase):
    async def send_video(self, client, *, main_bot=None, upload_bots=None):
        guard = DeliveryGuard(10, 20, [1])
        progress = AsyncMock()
        with patch.object(bot_engine, "aiogram_bot", main_bot), \
             patch.object(bot_engine, "upload_bots", upload_bots or []), \
             patch("database.prepare_message_delivery", AsyncMock()) as prepare:
            await dynamic_send(
                guard.wrap_client(client), "video", 20, "video-ref", "<tg-spoiler>hidden</tg-spoiler>", "HTML",
                reply_to_message_id=7, quote_data={"text": "quoted", "position": 2},
                thumbnail="thumb-ref", progress=progress, progress_args=(1,), has_spoiler=True,
            )
            prepare.assert_awaited_once()
        return client.send_video.await_args.kwargs

    async def test_guarded_main_and_pool_bots_keep_bot_api_parameters(self):
        for pooled in (False, True):
            with self.subTest(pooled=pooled):
                client = SimpleNamespace(send_video=AsyncMock(return_value=SimpleNamespace(message_id=99)))
                kwargs = await self.send_video(client, main_bot=None if pooled else client,
                                               upload_bots=[{"client": client}] if pooled else [])
                self.assertEqual(kwargs["caption"], "<tg-spoiler>hidden</tg-spoiler>")
                self.assertEqual(kwargs["reply_parameters"].message_id, 7)
                self.assertEqual(kwargs["reply_parameters"].quote, "quoted")
                self.assertEqual(kwargs["thumbnail"], "thumb-ref")
                self.assertTrue(kwargs["has_spoiler"])
                for name in ("quote_text", "thumb", "progress", "progress_args"):
                    self.assertNotIn(name, kwargs)
                client.send_video.assert_awaited_once()

    async def test_guarded_user_keeps_pyrogram_parameters(self):
        client = SimpleNamespace(send_video=AsyncMock(return_value=SimpleNamespace(id=99)))
        kwargs = await self.send_video(client)
        self.assertEqual(kwargs["caption"], "<spoiler>hidden</spoiler>")
        self.assertEqual(kwargs["quote_text"], "quoted")
        self.assertEqual(kwargs["reply_to_message_id"], 7)
        self.assertEqual(kwargs["thumb"], "thumb-ref")
        self.assertIn("progress", kwargs)
        self.assertNotIn("reply_parameters", kwargs)
        self.assertNotIn("thumbnail", kwargs)
