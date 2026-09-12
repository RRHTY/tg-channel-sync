import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sync_worker.media.hash_perturb import perturb_clone_media


class HashPerturbTests(unittest.TestCase):
    def test_start_sync_accepts_hash_reset_without_ffmpeg(self):
        from fastapi import BackgroundTasks
        import main

        async def run_test():
            with patch("main.sync_state", {"is_syncing": False}), \
                 patch("main.bot_engine.aiogram_bot", object()), \
                 patch("main.bot_engine.pyro_user_app", SimpleNamespace(is_initialized=True)), \
                 patch("main.resolve_chat_id", AsyncMock(side_effect=[-1001, -1002])), \
                 patch("main.db.add_sys_log", AsyncMock()), \
                 patch("main.process_master_sync", AsyncMock()):
                result = await main.start_sync(
                    BackgroundTasks(),
                    mode="clone",
                    sender="bot",
                    source_id="@source",
                    target_id="@target",
                    delay=5,
                    start_id=1,
                    end_id=2,
                    json_path="",
                    json_source_username="",
                    force_send="0",
                    hash_perturb="1",
                    target_type="channel",
                    clone_fallback_to_user="1",
                )
            self.assertEqual(result["status"], "success")

        import asyncio
        asyncio.run(run_test())

    def test_photo_appends_tail_bytes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "image.jpg"
            original = b"\xff\xd8\xfftest-image"
            path.write_bytes(original)
            result = perturb_clone_media(str(path), "photo")
            mutated = path.read_bytes()

        self.assertTrue(result.changed)
        self.assertGreater(len(mutated), len(original))
        self.assertTrue(mutated.startswith(original))

    def test_unsupported_type_is_skipped(self):
        result = perturb_clone_media("fake.bin", "document")
        self.assertFalse(result.changed)
        self.assertEqual(result.reason, "unsupported_type")

    def test_video_appends_tail_bytes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            video_path = Path(temp_dir) / "clip.mp4"
            original = b"\x00\x00\x00\x18ftypmp42video"
            video_path.write_bytes(original)
            result = perturb_clone_media(str(video_path), "video")
            mutated = video_path.read_bytes()

        self.assertTrue(result.changed)
        self.assertGreater(len(mutated), len(original))
        self.assertTrue(mutated.startswith(original))
