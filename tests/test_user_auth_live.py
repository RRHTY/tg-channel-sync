import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import main


class UserAuthLiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_loaded_engine_still_connecting_is_not_logged_out(self):
        engine = SimpleNamespace(get_user_auth_status=Mock(return_value={'status': 'idle'}))
        with patch.object(main, '_get_loaded_bot_engine', return_value=engine), patch.dict(
            main.app_info_cache, {'user': {'status': main.STATUS_INITIALIZING}}
        ):
            self.assertEqual((await main.get_user_auth_status())['status'], 'initializing')
            engine.get_user_auth_status.return_value = {'status': 'authorized'}
            self.assertEqual((await main.get_user_auth_status())['status'], 'authorized')

    async def test_stream_reports_startup_completion_without_reconnecting(self):
        engine = SimpleNamespace(get_user_auth_status=Mock(return_value={'status': 'idle'}))
        with patch.object(main, '_get_loaded_bot_engine', return_value=engine), patch.dict(
            main.app_info_cache, {'user': {'status': main.STATUS_INITIALIZING}}
        ), patch.object(main, 'SHUTDOWN_EVENT', asyncio.Event()), patch.object(
            main.db, 'get_sys_logs_after', AsyncMock(return_value=[])
        ), patch.object(main.db, 'get_msg_logs_after', AsyncMock(return_value=[])):
            response = await main.sse_stream(SimpleNamespace(is_disconnected=AsyncMock(return_value=False)))
            stream = response.body_iterator
            try:
                first = json.loads((await anext(stream)).removeprefix('data: '))
                self.assertEqual(first['user_auth']['status'], 'initializing')
                engine.get_user_auth_status.return_value = {'status': 'authorized'}
                main.app_info_cache['user'] = {'status': main.STATUS_LOGGED_IN}
                second = json.loads((await anext(stream)).removeprefix('data: '))
                self.assertEqual(second['user_auth']['status'], 'authorized')
                self.assertEqual(second['app_info']['user']['status'], main.STATUS_LOGGED_IN)
            finally:
                await stream.aclose()
