import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app_config
from server_runtime import resolve_server_config


class AppConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp_dir.name) / "config.json"
        self.original_config_file = app_config.config_file
        self.original_ensure_dirs = app_config.ensure_runtime_dirs
        app_config.config_file = lambda: self.config_path
        app_config.ensure_runtime_dirs = lambda: self.config_path.parent.mkdir(parents=True, exist_ok=True)
        app_config.clear_config_cache()

    def tearDown(self):
        app_config.config_file = self.original_config_file
        app_config.ensure_runtime_dirs = self.original_ensure_dirs
        app_config.clear_config_cache()
        self.temp_dir.cleanup()

    def test_load_config_does_not_rewrite_existing_file(self):
        raw = {"telegram": {"bot_token": "abc"}, "server": {"port": "9000"}}
        self.config_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        original_content = self.config_path.read_text(encoding="utf-8")

        loaded = app_config.load_config()

        self.assertEqual(loaded["telegram"]["bot_token"], "abc")
        self.assertEqual(loaded["server"]["port"], 9000)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), original_content)

    def test_get_config_uses_cache_until_save(self):
        first = app_config.get_config()
        second = app_config.get_config()

        self.assertEqual(first, second)
        self.assertTrue(self.config_path.exists())

        updated = app_config.save_config({"telegram": {"bot_token": "next"}})
        cached = app_config.get_config()

        self.assertEqual(updated["telegram"]["bot_token"], "next")
        self.assertEqual(cached["telegram"]["bot_token"], "next")

    def test_load_config_creates_file_and_returns_saved_config(self):
        loaded = app_config.load_config()
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))

        self.assertEqual(loaded, saved)
        self.assertEqual(app_config.get_config(), saved)

    def test_save_config_ignores_server_environment_overrides(self):
        with patch.dict("os.environ", {"TG_SYNC_HOST": "0.0.0.0", "TG_SYNC_PORT": "9001"}, clear=False):
            saved = app_config.save_config({"server": {"host": "127.0.0.1", "port": 8011}})

        self.assertEqual(saved["server"]["host"], "127.0.0.1")
        self.assertEqual(saved["server"]["port"], 8011)
        on_disk = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["server"]["host"], "127.0.0.1")
        self.assertEqual(on_disk["server"]["port"], 8011)

    def test_resolve_server_config_applies_dedicated_environment_overrides(self):
        with patch.dict("os.environ", {"TG_SYNC_HOST": "0.0.0.0", "TG_SYNC_PORT": "9001"}, clear=True):
            resolved = resolve_server_config({"host": "127.0.0.1", "port": 8011, "auto_open_browser": True})

        self.assertEqual(resolved["host"], "0.0.0.0")
        self.assertEqual(resolved["port"], 9001)
        self.assertTrue(resolved["auto_open_browser"])

    def test_resolve_server_config_falls_back_for_empty_or_invalid_environment(self):
        with patch.dict("os.environ", {"TG_SYNC_HOST": "", "TG_SYNC_PORT": "bad"}, clear=True):
            resolved = resolve_server_config({"host": "127.0.0.1", "port": "8022"})

        self.assertEqual(resolved["host"], "127.0.0.1")
        self.assertEqual(resolved["port"], 8022)

    def test_resolve_server_config_ignores_generic_host_port_environment(self):
        with patch.dict("os.environ", {"HOST": "0.0.0.0", "PORT": "9001"}, clear=True):
            resolved = resolve_server_config({"host": "127.0.0.1", "port": 8011})

        self.assertEqual(resolved["host"], "127.0.0.1")
        self.assertEqual(resolved["port"], 8011)

    def test_log_retention_defaults_are_present(self):
        config = app_config.get_config()

        self.assertEqual(config["sync"]["system_log_retention_limit"], 1000)
        self.assertEqual(config["sync"]["message_log_retention_limit"], 5000)
        self.assertFalse(config["app"]["debug_terminal_logs"])

    def test_debug_terminal_logs_normalizes_to_bool(self):
        config = app_config.save_config({"app": {"debug_terminal_logs": 1}})

        self.assertTrue(config["app"]["debug_terminal_logs"])

    def test_string_boolean_values_parse_common_literals(self):
        config = app_config.save_config(
            {
                "proxy": {"enabled": "false"},
                "server": {"auto_open_browser": "0"},
                "sync": {"force_send": "no", "realtime_fallback_to_user": ""},
                "app": {"portable_mode": "off", "debug_terminal_logs": "true"},
            }
        )

        self.assertFalse(config["proxy"]["enabled"])
        self.assertFalse(config["server"]["auto_open_browser"])
        self.assertFalse(config["sync"]["force_send"])
        self.assertTrue(config["sync"]["realtime_fallback_to_user"])
        self.assertFalse(config["app"]["portable_mode"])
        self.assertTrue(config["app"]["debug_terminal_logs"])

    def test_log_retention_invalid_values_fall_back_to_defaults(self):
        config = app_config.save_config(
            {
                "sync": {
                    "system_log_retention_limit": "abc",
                    "message_log_retention_limit": None,
                }
            }
        )

        self.assertEqual(config["sync"]["system_log_retention_limit"], 1000)
        self.assertEqual(config["sync"]["message_log_retention_limit"], 5000)

    def test_float_config_invalid_values_fall_back_to_defaults(self):
        config = app_config.save_config(
            {
                "sync": {
                    "default_delay": "abc",
                    "bot_upload_max_mb": "bad",
                    "bot_rate_limit_gb": object(),
                }
            }
        )

        self.assertEqual(config["sync"]["default_delay"], 5)
        self.assertEqual(config["sync"]["bot_upload_max_mb"], 50)
        self.assertEqual(config["sync"]["bot_rate_limit_gb"], 10)

    def test_log_retention_values_are_clamped_to_minimum(self):
        config = app_config.save_config(
            {
                "sync": {
                    "system_log_retention_limit": 1,
                    "message_log_retention_limit": "99",
                }
            }
        )

        self.assertEqual(config["sync"]["system_log_retention_limit"], 100)
        self.assertEqual(config["sync"]["message_log_retention_limit"], 100)
