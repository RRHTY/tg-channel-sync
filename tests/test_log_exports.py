import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import database
import main


class LogExportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / "temp")
        self.db_path = Path(self.temp_dir.name) / "data.db"
        self.original_db_file = database.DB_FILE
        self.original_ensure_dirs = database.ensure_runtime_dirs
        self.session_patch = patch.object(database, "pyrogram_user_session_base", return_value=Path(self.temp_dir.name) / "fake-session", create=True)
        self.session_patch.start()
        database.clear_saved_message_account()
        self.original_main_db = main.db
        database.DB_FILE = str(self.db_path)
        database.ensure_runtime_dirs = lambda: self.db_path.parent.mkdir(parents=True, exist_ok=True)
        main.db = database
        await database.close_db()
        await database.init_db()

    async def asyncTearDown(self):
        await database.close_db()
        database.DB_FILE = self.original_db_file
        database.ensure_runtime_dirs = self.original_ensure_dirs
        database.clear_saved_message_account()
        self.session_patch.stop()
        main.db = self.original_main_db
        self.temp_dir.cleanup()

    async def test_export_system_logs_returns_full_txt(self):
        await database.add_sys_log("INFO", "first line")
        await database.add_sys_log("WARNING", "second line")

        response = await main.export_system_logs()
        body = response.body.decode("utf-8")

        self.assertEqual(response.media_type, "text/plain; charset=utf-8")
        self.assertIn('attachment; filename="system-logs-', response.headers["Content-Disposition"])
        self.assertIn("[INFO] first line", body)
        self.assertIn("[WARNING] second line", body)
        self.assertLess(body.index("first line"), body.index("second line"))

    async def test_export_message_logs_returns_full_txt(self):
        await database.add_msg_log("MAP", "first item")
        await database.add_msg_log("SEND", "second item")

        response = await main.export_message_logs()
        body = response.body.decode("utf-8")

        self.assertEqual(response.media_type, "text/plain; charset=utf-8")
        self.assertIn('attachment; filename="message-logs-', response.headers["Content-Disposition"])
        self.assertIn("[MAP] first item", body)
        self.assertIn("[SEND] second item", body)
        self.assertLess(body.index("first item"), body.index("second item"))

