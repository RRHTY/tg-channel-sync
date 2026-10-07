import asyncio
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramUnauthorizedError
from aiogram.methods import GetMe
from aiogram.utils.token import TokenValidationError
from aiohttp_socks import ProxyConnectionError, ProxyError, ProxyTimeoutError

import main


class BotStartupTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.shutdown = asyncio.Event()
        self.logs = AsyncMock()
        self.patches.enter_context(patch.object(main, "SHUTDOWN_EVENT", self.shutdown))
        self.patches.enter_context(patch.object(main.db, "add_sys_log", self.logs))
        self.me = SimpleNamespace(first_name="Test Bot", username="test_bot")
        self.bot = SimpleNamespace(get_me=AsyncMock(return_value=self.me))
        self.original_sleep = asyncio.sleep

    async def quick_sleep(self, delay):
        await self.original_sleep(0)

    def network_error(self, message="ClientOSError: "):
        return TelegramNetworkError(method=GetMe(), message=message)

    async def get_me_with_retry(self):
        with patch.object(main.asyncio, "sleep", AsyncMock(side_effect=self.quick_sleep)) as sleep:
            result = await main._get_bot_me_with_retry(self.bot)
        return result, sleep


class BotStartupRetryTests(BotStartupTestCase):
    async def test_transient_network_failure_recovers_without_restarting(self):
        self.bot.get_me.side_effect = [self.network_error(), self.me]
        result, sleep = await self.get_me_with_retry()
        self.assertIs(result, self.me)
        self.assertEqual(self.bot.get_me.await_args_list, [call(request_timeout=10)] * 2)
        self.assertEqual(sleep.await_count, 1)
        self.assertGreater(sleep.await_args.args[0], 0)
        self.assertLessEqual(sleep.await_args.args[0], 4)

    async def test_timeout_recovers_with_bounded_attempts(self):
        self.bot.get_me.side_effect = [asyncio.TimeoutError(), asyncio.TimeoutError(), self.me]
        result, sleep = await self.get_me_with_retry()
        self.assertIs(result, self.me)
        self.assertEqual(self.bot.get_me.await_args_list, [call(request_timeout=10)] * 3)
        self.assertEqual(sleep.await_count, 2)
        self.assertTrue(all(0 < attempt.args[0] <= 4 for attempt in sleep.await_args_list))

    async def test_existing_temporary_network_error_recovers(self):
        self.bot.get_me.side_effect = [OSError("Connection reset by peer"), self.me]
        result, sleep = await self.get_me_with_retry()
        self.assertIs(result, self.me)
        self.assertEqual(self.bot.get_me.await_count, 2)
        self.assertEqual(sleep.await_count, 1)

    async def test_proxy_connection_failure_recovers(self):
        failure = ProxyConnectionError("[Errno 10061] Couldn't connect to proxy 127.0.0.1:1080")
        self.bot.get_me.side_effect = [failure, self.me]
        result, sleep = await self.get_me_with_retry()
        self.assertIs(result, self.me)
        self.assertEqual(self.bot.get_me.await_args_list, [call(request_timeout=10)] * 2)
        self.assertEqual(sleep.await_count, 1)

    async def test_proxy_timeout_recovers(self):
        self.bot.get_me.side_effect = [ProxyTimeoutError("Proxy connection timed out: 10"), self.me]
        result, sleep = await self.get_me_with_retry()
        self.assertIs(result, self.me)
        self.assertEqual(self.bot.get_me.await_args_list, [call(request_timeout=10)] * 2)
        self.assertEqual(sleep.await_count, 1)

    async def test_proxy_authentication_failure_is_not_retried(self):
        failure = ProxyError("Invalid username/password")
        self.bot.get_me.side_effect = failure
        with patch.object(main.asyncio, "sleep", AsyncMock()) as sleep:
            with self.assertRaises(ProxyError) as raised:
                await main._get_bot_me_with_retry(self.bot)
        self.assertIs(raised.exception, failure)
        self.bot.get_me.assert_awaited_once_with(request_timeout=10)
        sleep.assert_not_awaited()

    async def test_invalid_token_is_not_retried(self):
        failure = TelegramUnauthorizedError(method=GetMe(), message="Unauthorized")
        self.bot.get_me.side_effect = failure
        with patch.object(main.asyncio, "sleep", AsyncMock()) as sleep:
            with self.assertRaises(TelegramUnauthorizedError) as raised:
                await main._get_bot_me_with_retry(self.bot)
        self.assertIs(raised.exception, failure)
        self.bot.get_me.assert_awaited_once_with(request_timeout=10)
        sleep.assert_not_awaited()

    async def test_non_network_api_failure_is_not_retried(self):
        failure = TelegramBadRequest(method=GetMe(), message="Bad Request")
        self.bot.get_me.side_effect = failure
        with patch.object(main.asyncio, "sleep", AsyncMock()) as sleep:
            with self.assertRaises(TelegramBadRequest) as raised:
                await main._get_bot_me_with_retry(self.bot)
        self.assertIs(raised.exception, failure)
        self.bot.get_me.assert_awaited_once_with(request_timeout=10)
        sleep.assert_not_awaited()

    async def test_token_validation_failure_is_not_retried(self):
        failure = TokenValidationError("Token is invalid!")
        self.bot.get_me.side_effect = failure
        with patch.object(main.asyncio, "sleep", AsyncMock()) as sleep:
            with self.assertRaises(TokenValidationError) as raised:
                await main._get_bot_me_with_retry(self.bot)
        self.assertIs(raised.exception, failure)
        self.bot.get_me.assert_awaited_once_with(request_timeout=10)
        sleep.assert_not_awaited()

    async def test_exhaustion_preserves_last_network_exception(self):
        failures = [self.network_error(f"ClientOSError: attempt {index}") for index in range(3)]
        self.bot.get_me.side_effect = failures
        with patch.object(main.asyncio, "sleep", AsyncMock(side_effect=self.quick_sleep)) as sleep:
            with self.assertRaises(TelegramNetworkError) as raised:
                await main._get_bot_me_with_retry(self.bot)
        self.assertIs(raised.exception, failures[-1])
        self.assertEqual(self.bot.get_me.await_args_list, [call(request_timeout=10)] * 3)
        self.assertEqual(sleep.await_count, 2)

    async def test_cancel_during_retry_delay_does_not_retry(self):
        waiting = asyncio.Event()

        async def wait_for_cancel(delay):
            waiting.set()
            await asyncio.Event().wait()

        self.bot.get_me.side_effect = self.network_error()
        with patch.object(main.asyncio, "sleep", AsyncMock(side_effect=wait_for_cancel)) as sleep:
            task = asyncio.create_task(main._get_bot_me_with_retry(self.bot))
            try:
                await asyncio.wait_for(waiting.wait(), timeout=1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.bot.get_me.assert_awaited_once_with(request_timeout=10)
        sleep.assert_awaited_once()

    async def test_cancel_during_http_request_is_not_retried(self):
        started = asyncio.Event()

        async def blocked_get_me(**kwargs):
            started.set()
            await asyncio.Event().wait()

        self.bot.get_me.side_effect = blocked_get_me
        with patch.object(main.asyncio, "sleep", AsyncMock()) as sleep:
            task = asyncio.create_task(main._get_bot_me_with_retry(self.bot))
            try:
                await asyncio.wait_for(started.wait(), timeout=1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.bot.get_me.assert_awaited_once_with(request_timeout=10)
        sleep.assert_not_awaited()

    async def test_shutdown_during_retry_delay_prevents_next_request(self):
        async def shutdown_after_delay(delay):
            self.shutdown.set()
            await self.original_sleep(0)

        self.bot.get_me.side_effect = self.network_error()
        with patch.object(main.asyncio, "sleep", AsyncMock(side_effect=shutdown_after_delay)) as sleep:
            with self.assertRaises(asyncio.CancelledError):
                await main._get_bot_me_with_retry(self.bot)
        self.bot.get_me.assert_awaited_once_with(request_timeout=10)
        sleep.assert_awaited_once()

    async def test_shutdown_before_attempt_prevents_http_request(self):
        self.shutdown.set()
        with patch.object(main.asyncio, "sleep", AsyncMock()) as sleep:
            with self.assertRaises(asyncio.CancelledError):
                await main._get_bot_me_with_retry(self.bot)
        self.bot.get_me.assert_not_awaited()
        sleep.assert_not_awaited()


class BotStartupIntegrationTests(BotStartupTestCase):
    async def test_local_api_unauthorized_fails_without_official_fallback(self):
        self.bot.get_me.side_effect = TelegramUnauthorizedError(method=GetMe(), message="Unauthorized")
        official_bot = SimpleNamespace(get_me=AsyncMock(return_value=self.me))
        engine = SimpleNamespace(
            init_bot_client=Mock(side_effect=[self.bot, official_bot]),
            has_local_bot_api_server=Mock(return_value=True),
            is_using_local_bot_api=Mock(return_value=True),
            has_user_api_credentials=Mock(return_value=False),
            close_bot_client=AsyncMock(),
            dp=SimpleNamespace(start_polling=AsyncMock()),
        )
        with patch.object(main, "_ensure_bot_engine_loaded", AsyncMock(return_value=engine)), \
             patch.object(main, "polling_task", None), \
             patch.dict(main.app_info_cache, {"bot": {"name": "", "username": "", "status": main.STATUS_INITIALIZING}}):
            await main._initialize_clients_in_background()
            if main.polling_task is not None:
                await main.polling_task
            self.assertEqual(main.app_info_cache["bot"]["status"], main.STATUS_START_FAILED)
            self.assertIsNone(main.polling_task)
        engine.init_bot_client.assert_called_once_with()
        self.bot.get_me.assert_awaited_once_with(request_timeout=10)
        official_bot.get_me.assert_not_awaited()
        engine.close_bot_client.assert_not_awaited()
        engine.dp.start_polling.assert_not_awaited()
        self.assertFalse(any(attempt.args[0] == "WARNING" for attempt in self.logs.await_args_list))
        errors = [attempt.args for attempt in self.logs.await_args_list if attempt.args[0] == "ERROR"]
        self.assertEqual(len(errors), 1)
        self.assertIn("Unauthorized", errors[0][1])

    async def test_local_api_invalid_token_fails_before_creating_official_client(self):
        official_bot = SimpleNamespace(get_me=AsyncMock(return_value=self.me))
        engine = SimpleNamespace(
            init_bot_client=Mock(side_effect=[TokenValidationError("Token is invalid!"), official_bot]),
            has_local_bot_api_server=Mock(return_value=True),
            is_using_local_bot_api=Mock(return_value=True),
            has_user_api_credentials=Mock(return_value=False),
            close_bot_client=AsyncMock(),
            dp=SimpleNamespace(start_polling=AsyncMock()),
        )
        with patch.object(main, "_ensure_bot_engine_loaded", AsyncMock(return_value=engine)), \
             patch.object(main, "polling_task", None), \
             patch.dict(main.app_info_cache, {"bot": {"name": "", "username": "", "status": main.STATUS_INITIALIZING}}):
            await main._initialize_clients_in_background()
            self.assertEqual(main.app_info_cache["bot"]["status"], main.STATUS_START_FAILED)
            self.assertIsNone(main.polling_task)
        engine.init_bot_client.assert_called_once_with()
        self.bot.get_me.assert_not_awaited()
        official_bot.get_me.assert_not_awaited()
        engine.close_bot_client.assert_not_awaited()
        engine.dp.start_polling.assert_not_awaited()
        errors = [attempt.args for attempt in self.logs.await_args_list if attempt.args[0] == "ERROR"]
        self.assertEqual(len(errors), 1)
        self.assertIn("Token is invalid!", errors[0][1])

    async def test_local_api_bad_request_still_falls_back_to_official_api(self):
        self.bot.get_me.side_effect = TelegramBadRequest(method=GetMe(), message="Bad Request")
        official_bot = SimpleNamespace(get_me=AsyncMock(return_value=self.me))
        engine = SimpleNamespace(
            init_bot_client=Mock(side_effect=[self.bot, official_bot]),
            has_local_bot_api_server=Mock(return_value=True),
            is_using_local_bot_api=Mock(return_value=True),
            has_user_api_credentials=Mock(return_value=False),
            close_bot_client=AsyncMock(),
            dp=SimpleNamespace(start_polling=AsyncMock()),
        )
        with patch.object(main, "_ensure_bot_engine_loaded", AsyncMock(return_value=engine)), \
             patch.object(main, "polling_task", None), \
             patch.dict(main.app_info_cache, {"bot": {"name": "", "username": "", "status": main.STATUS_INITIALIZING}}):
            await main._initialize_clients_in_background()
            self.assertEqual(main.app_info_cache["bot"]["status"], main.STATUS_CONNECTED)
            self.assertIsNotNone(main.polling_task)
            await main.polling_task
        self.assertEqual(engine.init_bot_client.call_args_list, [call(), call(use_local_api=False)])
        self.bot.get_me.assert_awaited_once_with(request_timeout=10)
        official_bot.get_me.assert_awaited_once_with(request_timeout=10)
        engine.close_bot_client.assert_awaited_once()
        engine.dp.start_polling.assert_awaited_once_with(official_bot, handle_signals=False, close_bot_session=False)

    async def test_local_api_fallback_retries_official_api_before_polling(self):
        self.bot.get_me.side_effect = self.network_error()
        official_bot = SimpleNamespace(get_me=AsyncMock(side_effect=[self.network_error(), self.me]))
        engine = SimpleNamespace(
            init_bot_client=Mock(side_effect=[self.bot, official_bot]),
            has_local_bot_api_server=Mock(return_value=True),
            is_using_local_bot_api=Mock(return_value=True),
            has_user_api_credentials=Mock(return_value=False),
            close_bot_client=AsyncMock(),
            dp=SimpleNamespace(start_polling=AsyncMock()),
        )
        with patch.object(main, "_ensure_bot_engine_loaded", AsyncMock(return_value=engine)), \
             patch.object(main, "polling_task", None), \
             patch.dict(main.app_info_cache, {"bot": {"name": "", "username": "", "status": main.STATUS_INITIALIZING}}), \
             patch.object(main.asyncio, "sleep", AsyncMock(side_effect=self.quick_sleep)):
            await main._initialize_clients_in_background()
            self.assertEqual(main.app_info_cache["bot"]["status"], main.STATUS_CONNECTED)
            self.assertIsNotNone(main.polling_task)
            await main.polling_task
        self.assertEqual(self.bot.get_me.await_args_list, [call(request_timeout=10)] * 3)
        self.assertEqual(official_bot.get_me.await_args_list, [call(request_timeout=10)] * 2)
        self.assertEqual(engine.init_bot_client.call_args_list, [call(), call(use_local_api=False)])
        engine.close_bot_client.assert_awaited_once()
        engine.dp.start_polling.assert_awaited_once_with(official_bot, handle_signals=False, close_bot_session=False)
        self.assertFalse(any(attempt.args[0] == "ERROR" for attempt in self.logs.await_args_list))

    async def test_background_startup_retries_then_starts_polling(self):
        self.bot.get_me.side_effect = [self.network_error(), self.me]
        engine = SimpleNamespace(
            init_bot_client=Mock(return_value=self.bot),
            has_local_bot_api_server=Mock(return_value=False),
            is_using_local_bot_api=Mock(return_value=False),
            has_user_api_credentials=Mock(return_value=False),
            start_user_client_if_authorized=AsyncMock(),
            dp=SimpleNamespace(start_polling=AsyncMock()),
        )
        with patch.object(main, "_ensure_bot_engine_loaded", AsyncMock(return_value=engine)), \
             patch.object(main, "polling_task", None), \
             patch.dict(main.app_info_cache, {"bot": {"name": "", "username": "", "status": main.STATUS_INITIALIZING}}), \
             patch.object(main.asyncio, "sleep", AsyncMock(side_effect=self.quick_sleep)):
            await main._initialize_clients_in_background()
            self.assertEqual(main.app_info_cache["bot"]["status"], main.STATUS_CONNECTED)
            self.assertEqual(main.app_info_cache["bot"]["name"], "Test Bot")
            self.assertIsNotNone(main.polling_task)
            await main.polling_task
        self.assertEqual(self.bot.get_me.await_args_list, [call(request_timeout=10)] * 2)
        engine.dp.start_polling.assert_awaited_once_with(self.bot, handle_signals=False, close_bot_session=False)
        engine.start_user_client_if_authorized.assert_not_awaited()
        self.assertFalse(any(attempt.args[0] == "ERROR" for attempt in self.logs.await_args_list))

    async def test_background_startup_marks_failure_after_retry_exhaustion(self):
        self.bot.get_me.side_effect = self.network_error()
        engine = SimpleNamespace(
            init_bot_client=Mock(return_value=self.bot),
            has_local_bot_api_server=Mock(return_value=False),
            is_using_local_bot_api=Mock(return_value=False),
            has_user_api_credentials=Mock(return_value=False),
            dp=SimpleNamespace(start_polling=AsyncMock()),
        )
        with patch.object(main, "_ensure_bot_engine_loaded", AsyncMock(return_value=engine)), \
             patch.object(main, "polling_task", None), \
             patch.dict(main.app_info_cache, {"bot": {"name": "", "username": "", "status": main.STATUS_INITIALIZING}}), \
             patch.object(main.asyncio, "sleep", AsyncMock(side_effect=self.quick_sleep)):
            await main._initialize_clients_in_background()
            self.assertEqual(main.app_info_cache["bot"]["status"], main.STATUS_START_FAILED)
            self.assertIsNone(main.polling_task)
        self.assertEqual(self.bot.get_me.await_count, 3)
        engine.dp.start_polling.assert_not_awaited()
        errors = [attempt.args for attempt in self.logs.await_args_list if attempt.args[0] == "ERROR"]
        self.assertEqual(len(errors), 1)
        self.assertIn("ClientOSError", errors[0][1])
