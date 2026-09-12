import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sync_worker.core import links
from sync_worker.json_import import process
import bot_engine
from sync_worker.clone.process import _api_group_captions
from pyrogram.methods.messages.copy_media_group import CopyMediaGroup
from pyrogram.parser import Parser
from pyrogram.enums import ParseMode


class AlbumFilterTextTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_copy_dependency_really_removes_original_caption(self):
        parser = Parser(None)
        parsed = []

        async def capture(text):
            result = await parser.parse(text, ParseMode.HTML)
            parsed.append(result['message'])
            raise RuntimeError('stop before Telegram send')

        item = SimpleNamespace(photo=SimpleNamespace(file_id='fake'), has_media_spoiler=False, caption='original')
        client = SimpleNamespace(get_media_group=AsyncMock(return_value=[item]), rnd_id=lambda:1, parser=SimpleNamespace(parse=capture))
        with patch('pyrogram.methods.messages.copy_media_group.utils.parse_text_entities', AsyncMock(return_value={'message':'', 'entities':None})), \
             patch('pyrogram.methods.messages.copy_media_group.utils.get_input_media_from_file_id', return_value=object()):
            with self.assertRaisesRegex(RuntimeError, 'stop before'):
                await CopyMediaGroup.copy_media_group(client, 2, 1, 1, captions=_api_group_captions(['']))
        self.assertEqual(parsed, [''])

    async def test_realtime_copy_and_reupload_keep_replaced_caption(self):
        for reupload in (False, True):
            with self.subTest(reupload=reupload):
                item = SimpleNamespace(message_id=1, text=None, caption='old', html_text='old', document=None, video=None)
                bot = SimpleNamespace(copy_message=AsyncMock(return_value=SimpleNamespace(message_id=9)))
                with patch.object(bot_engine, 'media_group_cache', {(1, 'album'): [item]}), \
                     patch.object(bot_engine, 'aiogram_bot', bot), \
                     patch.object(bot_engine, 'get_quote_payload', return_value=None), \
                     patch.object(bot_engine, '_realtime_sync_options', return_value={'include_external_source_header':False}), \
                     patch.object(bot_engine, 'get_msg_type', return_value='photo'), \
                     patch.object(bot_engine, '_realtime_needs_reupload', return_value=reupload), \
                     patch.object(bot_engine, 'begin_saved_messages_operation', AsyncMock()), \
                     patch.object(bot_engine, 'finish_saved_messages_operation', AsyncMock()), \
                     patch.object(bot_engine, 'resolve_destination_chat_id', AsyncMock(return_value=2)), \
                     patch.object(bot_engine, 'resolve_reply_for_forward', AsyncMock(return_value=None)), \
                     patch.object(bot_engine, 'build_link_rewrite_context', AsyncMock(return_value={})), \
                     patch.object(bot_engine, 'rewrite_message_links', AsyncMock(side_effect=lambda text, *_: (text, 0))), \
                     patch.object(bot_engine, '_send_realtime_media_group_upload', AsyncMock(return_value=[9])) as upload, \
                     patch('database.apply_message_filters', AsyncMock(return_value=(False, 'new'))), \
                     patch('database.add_msg_log', AsyncMock()), \
                     patch('database.save_msg_mapping', AsyncMock()):
                    await bot_engine._process_realtime_media_group((1, 'album'), 1, [{'target_id':2}], 'source')
                if reupload:
                    self.assertEqual(upload.await_args.args[3], ['new'])
                else:
                    self.assertEqual(bot.copy_message.await_args.kwargs['caption'], 'new')

    async def test_history_caption_uses_replacement_and_marks_changed(self):
        item = SimpleNamespace(caption=SimpleNamespace(html='old'))
        with patch.object(links, 'build_link_rewrite_context', AsyncMock(return_value={})), \
             patch.object(links, 'rewrite_message_links', AsyncMock(side_effect=lambda text, *_: (text, 0))), \
             patch('database.apply_message_filters', AsyncMock(return_value=(False, 'new'))):
            captions, changed, _ = await links.rewrite_media_group_captions(1, 2, [item])
        self.assertEqual(captions, ['new'])
        self.assertTrue(changed)

    async def test_json_caption_uses_replacement(self):
        group = [{'id': 1, 'type': 'message', 'photo': 'one.jpg', 'text': 'old'}]
        with patch.object(process.os.path, 'exists', return_value=True), \
             patch.object(process.os.path, 'getsize', return_value=3), \
             patch.object(process, '_prepare_json_media_path', AsyncMock(return_value=('one.jpg', False))), \
             patch.object(process, 'rewrite_message_links', AsyncMock(side_effect=lambda text, *_: (text, 0))), \
             patch('database.apply_message_filters', AsyncMock(return_value=(False, 'new'))):
            result = await process._prepare_json_media_group(group, 'temp', 1, {}, False, False, [])
        self.assertEqual(result.rewritten_captions, ['new'])

    async def test_json_filters_use_original_filename_after_hash_perturb(self):
        group = [{'id': 1, 'type': 'message', 'photo': 'one.jpg', 'text': 'old'}]
        async def apply_filter(text, media, file_name):
            return (True, text) if file_name.startswith('1_') else (False, text.replace('old', 'new'))
        with patch.object(process.os.path, 'exists', return_value=True), \
             patch.object(process.os.path, 'getsize', return_value=3), \
             patch.object(process, '_prepare_json_media_path', AsyncMock(return_value=('1_one.jpg', True))), \
             patch.object(process, 'rewrite_message_links', AsyncMock(side_effect=lambda text, *_: (text, 0))), \
             patch('database.apply_message_filters', AsyncMock(side_effect=apply_filter)) as filtered:
            result = await process._prepare_json_media_group(group, 'temp', 1, {}, True, False, [])
        self.assertEqual(result.rewritten_captions, ['new'])
        self.assertEqual(filtered.await_args.args[2], 'one.jpg')
