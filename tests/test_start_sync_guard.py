import asyncio
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import BackgroundTasks
import main


class StartSyncGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(main.sync_state, {"is_syncing": False, "stop_requested": False, "starting": False}))
        engine = SimpleNamespace(aiogram_bot=object(), pyro_user_app=SimpleNamespace(is_initialized=True))
        self.stack.enter_context(patch.object(main, "_get_loaded_or_patched_bot_engine", return_value=engine))
        self.stack.enter_context(patch.object(main.db, "add_sys_log", AsyncMock()))
        self.resolve = self.stack.enter_context(patch.object(main, "resolve_chat_id", AsyncMock(side_effect=lambda bot, ref: ref)))
        self.worker = AsyncMock()
        self.stack.enter_context(patch.object(main, "_ensure_process_master_sync_loaded", AsyncMock(return_value=self.worker)))

    async def start(self, background):
        return await main.start_sync(background, mode="api", sender="user", source_id="source", target_id="target",
                                     delay=.5, start_id=1, end_id=2, json_path="", json_source_username="",
                                     json_media_group_window_seconds=3, force_send="0", hash_perturb="0",
                                     clone_fallback_to_user="1", target_type="channel")

    async def test_two_concurrent_requests_accept_only_one(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def resolve(bot, ref):
            entered.set()
            await release.wait()
            return ref

        self.resolve.side_effect = resolve
        backgrounds = [BackgroundTasks(), BackgroundTasks()]
        first = asyncio.create_task(self.start(backgrounds[0]))
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(self.start(backgrounds[1]))
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(first, second), 2)
        self.assertEqual(sorted(r["status"] for r in results), ["error", "success"])
        self.assertEqual(sum(len(b.tasks) for b in backgrounds), 1)
        self.assertTrue(main.sync_state["is_syncing"])
        for background in backgrounds:
            await background()
        self.worker.assert_awaited_once()
        self.assertFalse(main.sync_state["is_syncing"])

    async def test_failed_validation_releases_reservation(self):
        self.resolve.side_effect = ValueError("bad channel")
        self.assertEqual((await self.start(BackgroundTasks()))["status"], "error")
        self.assertFalse(main.sync_state["is_syncing"])
        self.resolve.side_effect = lambda bot, ref: ref
        self.assertEqual((await self.start(BackgroundTasks()))["status"], "success")

    async def test_cancelled_validation_releases_reservation(self):
        entered = asyncio.Event()

        async def wait(*_):
            entered.set()
            await asyncio.Event().wait()

        self.resolve.side_effect = wait
        task = asyncio.create_task(self.start(BackgroundTasks()))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(main.sync_state["is_syncing"])
        self.assertFalse(main.sync_state.get("starting"))

    async def test_stop_before_background_starts_does_not_send(self):
        background = BackgroundTasks()
        await self.start(background)
        self.assertEqual((await main.stop_sync())["status"], "success")
        await background()
        self.worker.assert_not_awaited()
        self.assertFalse(main.sync_state["is_syncing"])
        self.assertEqual(main.sync_state["result"]["status"], "stopped")

    async def test_same_resolved_channel_does_not_queue_task(self):
        self.resolve.side_effect = None
        self.resolve.return_value = -100123
        background = BackgroundTasks()
        result = await self.start(background)
        self.assertEqual(result['status'], 'error')
        self.assertIn('不能相同', result['message'])
        self.assertEqual(background.tasks, [])
        self.assertFalse(main.sync_state['is_syncing'])

    async def test_unexpected_worker_failure_releases_reservation(self):
        self.worker.side_effect = RuntimeError("worker failed")
        background = BackgroundTasks()
        await self.start(background)
        with self.assertRaisesRegex(RuntimeError, "worker failed"):
            await background()
        self.assertFalse(main.sync_state["is_syncing"])
