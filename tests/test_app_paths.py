import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app_paths


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FrozenAppPathsTests(unittest.TestCase):
    def test_frozen_paths_do_not_depend_on_current_working_directory(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "temp") as temp_dir:
            sandbox = Path(temp_dir)
            runtime_root = sandbox / "installed-app"
            bundle_root = sandbox / "pyinstaller-bundle"
            unrelated_cwd = sandbox / "launch-location"
            runtime_root.mkdir()
            bundle_root.mkdir()
            unrelated_cwd.mkdir()
            executable = runtime_root / "tg-channel-sync.exe"
            original_cwd = Path.cwd()

            try:
                os.chdir(unrelated_cwd)
                with (
                    patch.object(sys, "frozen", True, create=True),
                    patch.object(sys, "executable", str(executable)),
                    patch.object(sys, "_MEIPASS", str(bundle_root), create=True),
                ):
                    self.assertEqual(app_paths.app_root(), runtime_root.resolve())
                    self.assertEqual(app_paths.bundle_root(), bundle_root.resolve())
                    self.assertEqual(app_paths.static_dir(), bundle_root.resolve() / "static")
                    self.assertEqual(app_paths.version_file(), bundle_root.resolve() / "VERSION")
                    self.assertEqual(app_paths.config_file(), runtime_root.resolve() / "config.json")
                    self.assertEqual(app_paths.data_dir(), runtime_root.resolve() / "data")
                    self.assertEqual(app_paths.temp_dir(), runtime_root.resolve() / "temp")
                    self.assertEqual(app_paths.logs_dir(), runtime_root.resolve() / "data" / "logs")
                    self.assertEqual(
                        app_paths.sessions_dir(), runtime_root.resolve() / "data" / "sessions"
                    )
                    self.assertEqual(app_paths.database_file(), runtime_root.resolve() / "data" / "data.db")
                    self.assertEqual(
                        app_paths.pyrogram_user_session_base(),
                        runtime_root.resolve() / "data" / "sessions" / "sync_user_session",
                    )

                    app_paths.ensure_runtime_dirs()

                    for path in (
                        app_paths.data_dir(),
                        app_paths.temp_dir(),
                        app_paths.logs_dir(),
                        app_paths.sessions_dir(),
                    ):
                        self.assertTrue(path.is_dir())
                    self.assertFalse((unrelated_cwd / "data").exists())
                    self.assertFalse((unrelated_cwd / "temp").exists())
            finally:
                os.chdir(original_cwd)


if __name__ == "__main__":
    unittest.main()
