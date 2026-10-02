import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from pyrogram import raw

import bot_engine
from sync_worker.clone import process as history


def message(message_id, album=None, media_type="photo"):
    item = SimpleNamespace(
        id=message_id,
        empty=False,
        media_group_id=album,
        text=None,
        caption=SimpleNamespace(html=f"caption {message_id}") if album else None,
        reply_to_message_id=None,
        quote=None,
    )
    if album:
        setattr(item, media_type, SimpleNamespace(file_id=f"file-{message_id}"))
    return item


class HistoryMediaResumeTests(unittest.IsolatedAsyncioTestCase):
    async def run_history(self, inventory, *, mode="api", start=1, end=200,
                          settings=None, stop_on_fetch=None, real_group=False):
        events = []
        fetches = []
        app = SimpleNamespace(
            is_initialized=True,
            copy_media_group=AsyncMock(return_value=[SimpleNamespace(id=i + 1000) for i in range(98, 103)]),
            send_media_group=AsyncMock(),
        )

        async def fetch(**kwargs):
            requested = kwargs["msg_ids"]
            fetches.append(requested)
            if len(fetches) == stop_on_fetch:
                history.sync_state["stop_requested"] = True
            return [item for item in inventory if item.id in requested]

        async def send_single(*args, **kwargs):
            events.append(("single", [args[6].id]))
            return history.SYNC_RESULT_SENT_MAPPED

        async def send_group(*args, **kwargs):
            events.append(("album", [item.id for item in args[6]]))
            return history.SYNC_RESULT_SENT_MAPPED

        async def perform(action, **kwargs):
            return await action()

        async def send_explicit(**kwargs):
            return [SimpleNamespace(id=500 + index) for index in range(len(kwargs["media"]))]

        async def copy_selected(client, group, **kwargs):
            media = history.build_user_media_group(
                [(item, getattr(item, history.get_msg_meta(item, "api")[0]).file_id,
                  history.get_msg_meta(item, "api")[0]) for item in group],
                kwargs["captions"], {}, kwargs.get("has_spoilers"),
            )
            send_kwargs = {key: value for key, value in kwargs.items()
                           if key not in {"from_chat_id", "message_id", "captions", "has_spoilers", "parse_mode"}}
            return await client.send_media_group(media=media, **send_kwargs)

        app.send_media_group.side_effect = send_explicit
        with ExitStack() as stack:
            stack.enter_context(patch.dict(history.sync_state, {"is_syncing": False, "stop_requested": False}))
            stack.enter_context(patch.object(bot_engine, "pyro_user_app", app))
            stack.enter_context(patch.object(bot_engine, "aiogram_bot", object()))
            stack.enter_context(patch.object(history.db, "get_all_settings", AsyncMock(return_value=settings or {})))
            stack.enter_context(patch.object(history.db, "add_log", AsyncMock()))
            stack.enter_context(patch.object(history.db, "add_msg_log", AsyncMock()))
            stack.enter_context(patch.object(history.db, "is_message_synced", AsyncMock(return_value=False)))
            stack.enter_context(patch.object(history.db, "get_target_msg_id", AsyncMock(return_value=None)))
            stack.enter_context(patch.object(history.db, "apply_message_filters", AsyncMock(side_effect=lambda text, *_: (False, text))))
            stack.enter_context(patch.object(history, "get_config", return_value={}))
            stack.enter_context(patch.object(history, "resolve_chat_id", AsyncMock(side_effect=[-100123, -100456])))
            stack.enter_context(patch.object(history, "clear_temp_dir_files", AsyncMock()))
            stack.enter_context(patch.object(history, "_safe_get_messages", AsyncMock(side_effect=fetch)))
            stack.enter_context(patch.object(history, "sync_single_message", AsyncMock(side_effect=send_single)))
            record = stack.enter_context(patch.object(history, "record_success", AsyncMock()))
            reply = stack.enter_context(patch.object(history, "resolve_reply_target", AsyncMock(return_value=602)))
            stack.enter_context(patch.object(history, "rewrite_media_group_captions", AsyncMock(
                side_effect=lambda _source, _target, group, **_: ([f"rewritten {item.id}" for item in group], True, 0))))
            stack.enter_context(patch.object(history, "execute_with_network_retry", AsyncMock(side_effect=perform)))
            stack.enter_context(patch.object(history, "_copy_selected_api_media_group", AsyncMock(side_effect=copy_selected)))
            stack.enter_context(patch.object(history, "log_sync_error", AsyncMock()))
            stack.enter_context(patch.object(history.asyncio, "sleep", AsyncMock()))
            if not real_group:
                stack.enter_context(patch.object(history, "sync_media_group", AsyncMock(side_effect=send_group)))
            await history.process_master_sync(mode, "user", "source", "target", .5, start, end, "")
            result = dict(history.sync_state["result"])
            total = history.sync_state["total"]
        return SimpleNamespace(events=events, fetches=fetches, app=app, result=result,
                               total=total, record=record, reply=reply)

    async def test_album_crossing_one_hundred_ids_is_sent_once_in_source_order(self):
        inventory = [message(97)] + [message(i, "album") for i in range(98, 103)] + [message(103)]
        for mode in ("api", "clone"):
            with self.subTest(mode=mode):
                observed = await self.run_history(inventory, mode=mode)
                self.assertEqual(observed.events, [
                    ("single", [97]), ("album", [98, 99, 100, 101, 102]), ("single", [103]),
                ])
                self.assertEqual(observed.result["status"], "completed")
                self.assertEqual(observed.result["sent"], 7)
                self.assertEqual(observed.total, 200)

    async def test_final_album_flushes_after_final_or_empty_tail_batch(self):
        for mode in ("api", "clone"):
            for album_ids, end in ((range(98, 103), 102), (range(98, 101), 200)):
                with self.subTest(mode=mode, end=end):
                    ids = list(album_ids)
                    observed = await self.run_history([message(i, "album") for i in ids], mode=mode, end=end)
                    self.assertEqual(observed.events, [("album", ids)])
                    self.assertEqual(observed.result["sent"], len(ids))
                    self.assertEqual(observed.result["failed"], 0)

    async def test_stop_during_next_fetch_does_not_send_pending_album(self):
        inventory = [message(97)] + [message(i, "album") for i in range(98, 103)]
        for mode in ("api", "clone"):
            with self.subTest(mode=mode):
                observed = await self.run_history(inventory, mode=mode, stop_on_fetch=2)
                self.assertEqual(observed.events, [("single", [97])])
                self.assertEqual(observed.result["status"], "stopped")
                self.assertEqual(observed.result["sent"], 1)
                self.assertEqual(len(observed.fetches), 2)

    async def test_api_range_sends_only_selected_file_ids_and_preserves_metadata(self):
        inventory = [message(i, "album") for i in range(98, 103)]
        selected_first = inventory[1]
        selected_first.reply_to_message_id = 42
        entities = [SimpleNamespace(type="bold", offset=0, length=6)]
        selected_first.quote = SimpleNamespace(text="quoted", position=0, entities=entities)
        inventory[1].photo.has_spoiler = True
        inventory[3].photo.has_spoiler = True
        observed = await self.run_history(inventory, start=99, end=101, real_group=True)

        observed.app.copy_media_group.assert_not_awaited()
        observed.app.send_media_group.assert_awaited_once()
        kwargs = observed.app.send_media_group.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], -100456)
        self.assertEqual([item.media for item in kwargs["media"]], ["file-99", "file-100", "file-101"])
        self.assertEqual([item.caption for item in kwargs["media"]], ["rewritten 99", "rewritten 100", "rewritten 101"])
        self.assertTrue(all(item.parse_mode == history.ParseMode.HTML for item in kwargs["media"]))
        self.assertEqual([bool(getattr(item, "has_spoiler", False)) for item in kwargs["media"]], [True, False, True])
        self.assertEqual(kwargs["reply_to_message_id"], 602)
        self.assertEqual(kwargs["quote_text"], "quoted")
        self.assertEqual(kwargs["quote_entities"], entities)
        observed.reply.assert_awaited_once_with(-100123, -100456, 42, "API", 99)
        self.assertEqual([(call.args[2], call.args[3]) for call in observed.record.await_args_list], [(99, 500), (100, 501), (101, 502)])
        self.assertTrue(all(call.args[:2] == (-100123, -100456) for call in observed.record.await_args_list))
        self.assertEqual(observed.result["sent"], 3)
        self.assertEqual(observed.total, 3)

    async def test_selected_api_album_preserves_spoilers_in_real_sdk_rpc(self):
        selected = [
            SimpleNamespace(photo=SimpleNamespace(file_id="photo-99"), video=None, audio=None,
                            document=None, caption="original photo", has_media_spoiler=False),
            SimpleNamespace(photo=None, video=SimpleNamespace(file_id="video-101"), audio=None,
                            document=None, caption="original video", has_media_spoiler=False),
        ]
        sent = [SimpleNamespace(id=500), SimpleNamespace(id=501)]
        app = SimpleNamespace(
            get_media_group=AsyncMock(return_value=[object(), *selected, object()]),
            resolve_peer=AsyncMock(return_value=raw.types.InputPeerChannel(channel_id=456, access_hash=0)),
            invoke=AsyncMock(return_value=SimpleNamespace(updates=[], users=[], chats=[])),
            rnd_id=Mock(side_effect=[1, 2]),
            parser=SimpleNamespace(parse=AsyncMock(side_effect=lambda text: {"message": text, "entities": None})),
        )
        original_members = dict(vars(app))

        def media_from_file_id(file_id, has_spoiler=False):
            if file_id.startswith("photo-"):
                return raw.types.InputMediaPhoto(
                    id=raw.types.InputPhoto(id=99, access_hash=0, file_reference=b"photo-99"),
                    spoiler=has_spoiler,
                )
            return raw.types.InputMediaDocument(
                id=raw.types.InputDocument(id=101, access_hash=0, file_reference=b"video-101"),
                spoiler=has_spoiler,
            )

        sdk_utils = "pyrogram.methods.messages.copy_media_group.utils"
        with patch(f"{sdk_utils}.get_input_media_from_file_id", side_effect=media_from_file_id) as resolve_media, \
             patch(f"{sdk_utils}.parse_text_entities", AsyncMock(return_value={"message": "quoted", "entities": None})), \
             patch(f"{sdk_utils}.get_reply_to", AsyncMock(return_value=raw.types.InputReplyToMessage(reply_to_msg_id=602))) as reply, \
             patch(f"{sdk_utils}.parse_messages", AsyncMock(return_value=sent)):
            result = await history._copy_selected_api_media_group(
                app, selected, chat_id=-100456, from_chat_id=-100123, message_id=99,
                captions=["selected photo", "selected video"], has_spoilers=[True, True],
                reply_to_message_id=602, quote_text="quoted", parse_mode=history.ParseMode.HTML,
            )

        self.assertEqual(result, sent)
        app.get_media_group.assert_not_awaited()
        self.assertEqual(vars(app), original_members)
        app.invoke.assert_awaited_once()
        rpc = app.invoke.await_args.args[0]
        self.assertIsInstance(rpc, raw.functions.messages.SendMultiMedia)
        self.assertEqual([item.media.id.id for item in rpc.multi_media], [99, 101])
        self.assertIsInstance(rpc.multi_media[0].media, raw.types.InputMediaPhoto)
        self.assertIsInstance(rpc.multi_media[1].media, raw.types.InputMediaDocument)
        self.assertEqual([item.media.spoiler for item in rpc.multi_media], [True, True])
        self.assertEqual([item.message for item in rpc.multi_media], ["selected photo", "selected video"])
        self.assertEqual([call.kwargs["file_id"] for call in resolve_media.call_args_list], ["photo-99", "video-101"])
        self.assertEqual(rpc.reply_to.reply_to_msg_id, 602)
        self.assertEqual(reply.await_args.kwargs["quote_text"], "quoted")

    async def test_disabled_media_type_is_excluded_without_expanding_album(self):
        inventory = [message(98, "album"), message(99, "album", "video"),
                     message(100, "album"), message(101, "album"), message(102)]
        for mode in ("api", "clone"):
            with self.subTest(mode=mode):
                observed = await self.run_history(inventory, mode=mode, settings={"sync_video": "0"})
                self.assertEqual(observed.events, [("album", [98, 100, 101]), ("single", [102])])
                self.assertEqual(observed.result["sent"], 4)
                self.assertEqual(observed.result["skipped"], 1)
