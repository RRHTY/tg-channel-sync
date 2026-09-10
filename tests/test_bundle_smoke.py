import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services import bundle_smoke


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class BundleSmokeTests(unittest.TestCase):
    def test_bundle_smoke_checks_modules_resources_and_runtime_files(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "temp") as temp_dir:
            root = Path(temp_dir)
            static = root / "static"
            static.mkdir()
            (static / "index.html").write_text("<!doctype html>", encoding="utf-8")
            config = root / "config.json"
            imported = []

            def save_config(value):
                config.write_text("{}", encoding="utf-8")
                return value

            def ensure_dirs():
                for name in ("data", "temp", "data/logs", "data/sessions"):
                    (root / name).mkdir(parents=True, exist_ok=True)

            with (
                patch.object(bundle_smoke, "app_root", return_value=root),
                patch.object(bundle_smoke, "static_dir", return_value=static),
                patch.object(bundle_smoke, "config_file", return_value=config),
                patch.object(bundle_smoke, "data_dir", return_value=root / "data"),
                patch.object(bundle_smoke, "temp_dir", return_value=root / "temp"),
                patch.object(bundle_smoke, "logs_dir", return_value=root / "data/logs"),
                patch.object(bundle_smoke, "sessions_dir", return_value=root / "data/sessions"),
                patch.object(bundle_smoke, "ensure_runtime_dirs", side_effect=ensure_dirs),
                patch.object(bundle_smoke, "get_config", return_value={"app": {}}),
                patch.object(bundle_smoke, "save_config", side_effect=save_config),
                patch.object(bundle_smoke, "get_local_version", return_value="v0.5.2"),
                patch.object(
                    bundle_smoke.importlib,
                    "import_module",
                    side_effect=lambda name: imported.append(name),
                ),
            ):
                report = bundle_smoke.run_bundle_smoke()

            self.assertEqual(report["version"], "v0.5.2")
            self.assertEqual(imported, ["bot_engine", "sync_worker.clone.process"])
            self.assertTrue(report["config_written"])
            self.assertTrue(report["static_index"])
            self.assertEqual(report["runtime_root"], str(root))


if __name__ == "__main__":
    unittest.main()
