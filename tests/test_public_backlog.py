import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sync_worker.realtime import public_poller


class PublicBacklogTests(unittest.IsolatedAsyncioTestCase):
    async def test_backlog_over_100_is_read_in_order_without_splitting_album(self):
        class User:
            async def get_chat_history(self, peer, limit=0):
                messages = [SimpleNamespace(id=i, media_group_id="album" if 109 <= i <= 112 else None)
                            for i in range(260, 0, -1)]
                for message in messages[:limit or None]:
                    yield message

        messages = await public_poller.load_public_channel_new_messages(User(), "source", 10)
        self.assertEqual([m.id for m in messages], list(range(11, 261)))

    async def test_checkpoint_stays_put_when_history_read_fails(self):
        class User:
            async def get_chat_history(self, peer, limit=0):
                yield SimpleNamespace(id=260)
                raise RuntimeError("connection lost")

        with patch.dict("sync_worker.runtime.sync_state", {"is_syncing": False}), \
             patch.object(public_poller.db, "update_public_user_poll_position", AsyncMock()) as checkpoint:
            with self.assertRaisesRegex(RuntimeError, "connection lost"):
                await public_poller.process_public_channel_mapping_group(None, User(), {
                    "source_id": 1, "source_ref": "source", "last_polled_message_id": 10, "mappings": [],
                })
        checkpoint.assert_not_awaited()
