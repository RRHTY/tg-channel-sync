import asyncio
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiogram import Dispatcher

import main


class ServerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.tasks = []
        self.polling_stopped = asyncio.Event()
        self.startup_started = asyncio.Event()
        self.engine = SimpleNamespace(
            dp=SimpleNamespace(stop_polling=AsyncMock(side_effect=self.polling_stopped.set)),
            close_user_client=AsyncMock(),
            close_bot_client=AsyncMock(),
        )
        self.database = SimpleNamespace(
            init_db=AsyncMock(), close_db=AsyncMock(), add_sys_log=AsyncMock()
        )
        fake_temp = SimpleNamespace(exists=Mock(return_value=False), mkdir=Mock())
        overrides = {
            "SHUTDOWN_EVENT": asyncio.Event(),
            "SERVER": SimpleNamespace(should_exit=False, force_exit=False),
            "RESTART_REQUESTED": False,
            "STOP_REQUESTED": False,
            "_cleanup_done": False,
            "startup_task": None,
            "polling_task": None,
            "public_channel_polling_task": None,
            "bot_engine_module": self.engine,
            "db": self.database,
            "ensure_runtime_dirs": Mock(),
            "temp_dir": Mock(return_value=fake_temp),
            "_reset_startup_app_info": Mock(),
            "_initialize_clients_in_background": self.start_background_work,
        }
        for name, value in overrides.items():
            self.patches.enter_context(patch.object(main, name, value))
        self.patches.enter_context(patch.object(main.signal, "signal", Mock()))
        self.patches.enter_context(patch.dict(main.sync_state, {"stop_requested": False}))

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def start_background_work(self):
        main.polling_task = asyncio.create_task(self.polling_stopped.wait())
        main.public_channel_polling_task = asyncio.create_task(asyncio.Event().wait())
        self.tasks.extend((main.polling_task, main.public_channel_polling_task, asyncio.current_task()))
        self.startup_started.set()
        await asyncio.Event().wait()

    async def assert_request_releases_resources(self, request):
        async with main.lifespan(main.app):
            await self.startup_started.wait()
            await request()
            # The endpoint schedules the request so the response can be returned first.
            await asyncio.sleep(0)
            self.assertTrue(main.SERVER.should_exit)
        self.engine.dp.stop_polling.assert_awaited_once()
        self.engine.close_user_client.assert_awaited_once()
        self.engine.close_bot_client.assert_awaited_once()
        self.database.close_db.assert_awaited_once()
        self.assertTrue(all(task.done() for task in self.tasks))
        self.assertIsNone(main.startup_task)
        self.assertIsNone(main.polling_task)
        self.assertIsNone(main.public_channel_polling_task)

    async def test_restart_releases_clients_and_background_tasks(self):
        await self.assert_request_releases_resources(main.restart_server)
        self.assertTrue(main.RESTART_REQUESTED)
        self.assertFalse(main.STOP_REQUESTED)

    async def test_stop_releases_clients_and_background_tasks(self):
        await self.assert_request_releases_resources(main.stop_server)
        self.assertTrue(main.STOP_REQUESTED)
        self.assertFalse(main.RESTART_REQUESTED)

    async def test_repeated_restart_request_cleans_up_once(self):
        async def request_twice():
            await main.restart_server()
            await main.restart_server()

        await self.assert_request_releases_resources(request_twice)
        self.database.add_sys_log.assert_awaited_once()

    async def test_signal_exit_cleans_up_once(self):
        async def signal_exit():
            main._request_server_exit()

        await self.assert_request_releases_resources(signal_exit)

    async def test_cancelled_polling_task_still_releases_clients(self):
        async with main.lifespan(main.app):
            await self.startup_started.wait()
            main.polling_task.cancel()
            await asyncio.gather(main.polling_task, return_exceptions=True)
            main._request_server_exit()
        self.engine.close_user_client.assert_awaited_once()
        self.engine.close_bot_client.assert_awaited_once()
        self.database.close_db.assert_awaited_once()
        self.assertTrue(all(task.done() for task in self.tasks))


class ServerRestartLoopTests(unittest.TestCase):
    def test_in_place_restart_reuses_event_loop_and_dispatcher(self):
        dispatcher = Dispatcher()
        dispatcher.emit_startup = AsyncMock()
        dispatcher.emit_shutdown = AsyncMock()

        async def offline_polling(**kwargs):
            await asyncio.Event().wait()

        dispatcher._polling = offline_polling
        bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))
        servers = []
        loops = []
        previous_work = []
        loader_released = asyncio.Event()
        engine = SimpleNamespace()
        real_asyncio_run = asyncio.run

        async def load_engine():
            await loader_released.wait()
            return engine

        async def serve():
            loops.append(asyncio.get_running_loop())
            if previous_work:
                self.assertTrue(
                    previous_work[0].done(),
                    "Work from the previous server cycle must end before restart",
                )
                self.assertFalse(main.bot_engine_load_task.cancelled())
                loader_released.set()
                self.assertIs(await main._ensure_bot_engine_loaded(), engine)
            self.assertFalse(main.SHUTDOWN_EVENT.is_set())
            self.assertFalse(main._cleanup_done)
            self.assertFalse(main.RESTART_REQUESTED)
            self.assertFalse(main.STOP_REQUESTED)
            polling = asyncio.create_task(
                dispatcher.start_polling(
                    bot, allowed_updates=[], handle_signals=False, close_bot_session=False
                )
            )
            for _ in range(5):
                await asyncio.sleep(0)
            await dispatcher.stop_polling()
            await polling
            if len(loops) == 1:
                previous_work.append(asyncio.create_task(asyncio.Event().wait()))
                main.bot_engine_load_task = asyncio.create_task(load_engine())
            main._cleanup_done = True
            main.SHUTDOWN_EVENT.set()
            main.RESTART_REQUESTED = len(loops) == 1
            main.STOP_REQUESTED = len(loops) == 2

        def make_server(config):
            self.assertLess(len(servers), 2, "Restart loop did not stop after the second server")
            server = SimpleNamespace(
                run=Mock(), serve=AsyncMock(side_effect=serve), should_exit=False, force_exit=False
            )
            servers.append(server)
            return server

        config = {"server": {"host": "127.0.0.1", "port": 8011}}
        with ExitStack() as patches:
            for name, value in {
                "SERVER": None,
                "SHUTDOWN_EVENT": asyncio.Event(),
                "RESTART_REQUESTED": False,
                "STOP_REQUESTED": False,
                "_cleanup_done": True,
                "bot_engine": SimpleNamespace(),
                "bot_engine_module": None,
                "bot_engine_load_task": None,
                "get_config": Mock(return_value=config),
                "resolve_server_config": Mock(return_value=config["server"]),
                "configure_terminal_logging": Mock(),
                "should_auto_open_browser": Mock(return_value=False),
                "launch_browser_when_ready": Mock(),
            }.items():
                patches.enter_context(patch.object(main, name, value))
            patches.enter_context(patch.object(main.uvicorn, "Config", Mock()))
            patches.enter_context(patch.object(main.uvicorn, "Server", side_effect=make_server))
            run = patches.enter_context(patch.object(main.asyncio, "run", wraps=real_asyncio_run))
            main.run_server()
            self.assertEqual(len(loops), 2, "Both server cycles must await serve()")
            self.assertIs(loops[0], loops[1])
            run.assert_called_once()
            self.assertIsNone(main.SERVER)
        for server in servers:
            server.serve.assert_awaited_once()
            server.run.assert_not_called()


class EngineLoadingTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_startup_does_not_cancel_shared_engine_loading(self):
        started = asyncio.Event()
        released = asyncio.Event()
        engine = SimpleNamespace()

        async def import_engine(*args):
            started.set()
            await released.wait()
            return engine

        with patch.object(main, "bot_engine_module", None), patch.object(
            main, "bot_engine_load_task", None
        ), patch.object(main, "bot_engine", SimpleNamespace()), patch.object(
            main.asyncio, "to_thread", AsyncMock(side_effect=import_engine)
        ) as importer:
            startup = asyncio.create_task(main._ensure_bot_engine_loaded())
            await started.wait()
            startup.cancel()
            await asyncio.gather(startup, return_exceptions=True)
            released.set()
            self.assertFalse(
                main.bot_engine_load_task.cancelled(),
                "Cancelling startup must leave the shared import available for restart",
            )
            self.assertIs(await main._ensure_bot_engine_loaded(), engine)
            importer.assert_awaited_once()

    async def test_previously_cancelled_engine_loader_can_be_restarted(self):
        cancelled_loader = asyncio.create_task(asyncio.sleep(0))
        cancelled_loader.cancel()
        await asyncio.gather(cancelled_loader, return_exceptions=True)
        engine = SimpleNamespace()
        with patch.object(main, "bot_engine_module", None), patch.object(
            main, "bot_engine_load_task", cancelled_loader
        ), patch.object(main, "bot_engine", SimpleNamespace()), patch.object(
            main.asyncio, "to_thread", AsyncMock(return_value=engine)
        ) as importer:
            self.assertIs(await main._ensure_bot_engine_loaded(), engine)
            importer.assert_awaited_once()
