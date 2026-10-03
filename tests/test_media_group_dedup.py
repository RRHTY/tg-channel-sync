import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sync_worker.clone import process as history
from sync_worker.runtime.result import SyncResult


def album(ids=(1, 2, 3)):
    return [SimpleNamespace(
        id=i, empty=False, media_group_id="album", text=None, caption=None,
        photo=SimpleNamespace(file_id=f"photo-{i}"),
    ) for i in ids]


class MediaGroupDedupTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, group, mapped=(), *, sender="user", blocked=False):
        stack = ExitStack()
        self.addCleanup(stack.close)
        saved = {i: 100 + i for i in mapped}
        stack.enter_context(patch.dict(history.sync_state, {
            "stop_requested": False, "current": 0, "skipped": 0, "mode": "API",
        }))
        stack.enter_context(patch.object(history.db, "is_message_synced", AsyncMock(
            side_effect=lambda source, msg, target: msg in saved,
        )))
        stack.enter_context(patch.object(history.db, "get_message_delivery", AsyncMock(return_value=None)))
        stack.enter_context(patch.object(history.db, "prepare_message_delivery", AsyncMock()))
        stack.enter_context(patch.object(history.db, "mark_message_delivery_unconfirmed", AsyncMock()))
        stack.enter_context(patch.object(history.db, "add_msg_log", AsyncMock()))
        stack.enter_context(patch.object(history.db, "apply_message_filters", AsyncMock(
            side_effect=lambda text, *_: (blocked and text == "blocked", text),
        )))
        stack.enter_context(patch.object(history, "resolve_reply_target", AsyncMock(return_value=None)))
        stack.enter_context(patch.object(history, "rewrite_media_group_captions", AsyncMock(
            side_effect=lambda source, target, items, **_: (["" for _ in items], False, 0),
        )))
        stack.enter_context(patch.object(history.asyncio, "sleep", AsyncMock()))

        async def execute(action, **kwargs):
            return await action()

        async def await_direct(coro, state):
            return await coro

        stack.enter_context(patch.object(history, "execute_with_network_retry", execute))
        stack.enter_context(patch.object(history, "_execute_with_clone_retry_interruptibly", execute))
        stack.enter_context(patch.object(history, "safe_execute", await_direct))

        async def save(source, target, msg, target_msg, **kwargs):
            saved[msg] = target_msg

        record = stack.enter_context(patch.object(history, "record_success", AsyncMock(side_effect=save)))

        async def send(**kwargs):
            return [SimpleNamespace(id=200 + index, message_id=200 + index)
                    for index, _ in enumerate(kwargs["media"])]

        app = SimpleNamespace(send_media_group=AsyncMock(side_effect=send),
                              copy_media_group=AsyncMock(return_value=[
                                  SimpleNamespace(id=200 + i) for i, _ in enumerate(group)
                              ]))
        async def copy_selected(client, items, **kwargs):
            media = history.build_user_media_group(
                [(item, item.photo.file_id, "photo") for item in items],
                ["" for _ in items], {},
            )
            return await client.send_media_group(chat_id=kwargs["chat_id"], media=media)

        stack.enter_context(patch.object(history, "_copy_selected_api_media_group", copy_selected))
        download = stack.enter_context(patch.object(history, "_download_clone_media_item", AsyncMock(
            side_effect=lambda app, msg, *args, **kwargs: f"temp/{msg.id}.jpg",
        )))
        stack.enter_context(patch.object(history, "_download_media_thumbnail", AsyncMock(return_value=None)))
        stack.enter_context(patch.object(history.os.path, "getsize", return_value=3))
        stack.enter_context(patch.object(history.os, "remove"))
        stack.enter_context(patch.object(history, "resolve_upload_target", AsyncMock(return_value={
            "sender": sender, "client": app, "label": "测试发送端", "parse_mode": history.ParseMode.HTML,
        })))
        stack.enter_context(patch.object(history.bot_engine, "note_upload_success", AsyncMock()))
        return app, record, download, saved

    async def test_mapped_leader_does_not_hide_missing_members(self):
        group = album()
        app, record, _, saved = self.fixture(group, (1,))
        result = await history.sync_media_group("api", "user", app, app, 10, -1, group, 0, False,
                                                chat_id=999)
        self.assertEqual(result, "sent_mapped")
        app.copy_media_group.assert_not_awaited()
        media = app.send_media_group.await_args.kwargs["media"]
        self.assertEqual([item.media for item in media], ["photo-2", "photo-3"])
        self.assertEqual(app.send_media_group.await_args.kwargs["chat_id"], 999)
        self.assertEqual([call.args[2] for call in record.await_args_list], [2, 3])
        self.assertEqual(saved[1], 101)
        self.assertEqual((history.sync_state["current"], history.sync_state["skipped"]), (3, 1))

    async def test_all_mapped_members_skip_without_sending(self):
        group = album()
        app, record, download, _ = self.fixture(group, (1, 2, 3))
        result = await history.sync_media_group("clone", "user", app, app, 10, -1, group, 0, False)
        self.assertEqual(result, "skipped")
        app.send_media_group.assert_not_awaited()
        app.copy_media_group.assert_not_awaited()
        download.assert_not_awaited()
        record.assert_not_awaited()
        self.assertEqual((history.sync_state["current"], history.sync_state["skipped"]), (3, 3))

    async def test_single_missing_member_uses_single_sender(self):
        group = album()
        app, _, download, _ = self.fixture(group, (1, 3))
        with patch.object(history, "sync_single_message", AsyncMock(return_value="sent_mapped")) as single:
            result = await history.sync_media_group("clone", "bot", app, app, 10, -1, group, 0, False,
                                                    hash_perturb=True, clone_fallback_to_user=False,
                                                    include_external_source_header=True, chat_id=999)
        self.assertEqual(result, "sent_mapped")
        single.assert_awaited_once()
        self.assertIs(single.await_args.args[6], group[1])
        self.assertEqual(single.await_args.kwargs["chat_id"], 999)
        self.assertTrue(single.await_args.kwargs["hash_perturb"])
        self.assertFalse(single.await_args.kwargs["clone_fallback_to_user"])
        app.send_media_group.assert_not_awaited()
        download.assert_not_awaited()

    async def test_clone_downloads_only_missing_members(self):
        for sender in ("user", "bot"):
            with self.subTest(sender=sender):
                group = album((1, 2, 3, 4))
                app, record, download, _ = self.fixture(group, (2, 4), sender=sender)
                result = await history.sync_media_group("clone", sender, app, app, 10, -1, group, 0, False)
                self.assertEqual(result, "sent_mapped")
                self.assertEqual([call.args[1].id for call in download.await_args_list], [1, 3])
                self.assertEqual([call.args[2] for call in record.await_args_list], [1, 3])

    async def test_force_send_includes_mapped_members(self):
        group = album()
        app, record, _, _ = self.fixture(group, (1, 2, 3))
        result = await history.sync_media_group("api", "user", app, app, 10, -1, group, 0, True)
        self.assertEqual(result, "sent_mapped")
        self.assertEqual([call.args[2] for call in record.await_args_list], [1, 2, 3])
        self.assertTrue(all(call.kwargs["force_send"] for call in record.await_args_list))
        self.assertEqual(history.sync_state["skipped"], 0)

    async def test_mapped_member_still_applies_whole_album_filter(self):
        group = album()
        group[0].caption = SimpleNamespace(html="blocked")
        app, record, _, _ = self.fixture(group, (1,), blocked=True)
        result = await history.sync_media_group("api", "user", app, app, 10, -1, group, 0, False)
        self.assertEqual(result, "skipped")
        app.send_media_group.assert_not_awaited()
        record.assert_not_awaited()

    async def test_outcome_separates_existing_and_new_members(self):
        group = album()
        app, _, _, _ = self.fixture(group, (1,))
        outcome = SyncResult()
        result = await history.sync_media_group("api", "user", app, app, 10, -1, group, 0, False,
                                                outcome=outcome)
        self.assertEqual(result, "sent_mapped")
        self.assertEqual((outcome.sent, outcome.skipped, outcome.failed), (2, 1, 0))


if __name__ == "__main__":
    unittest.main()
