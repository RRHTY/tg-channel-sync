from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from app_paths import config_file, ensure_runtime_dirs


DEFAULT_CONFIG: dict[str, Any] = {
    "telegram": {
        "bot_token": "",
        "extra_bot_tokens": [],
        "api_id": 0,
        "api_hash": "",
        "bot_api_base_url": "",
    },
    "proxy": {
        "enabled": False,
        "host": "127.0.0.1",
        "port": 7897,
        "username": "",
        "password": "",
    },
    "server": {
        "host": "127.0.0.1",
        "port": 8011,
        "auto_open_browser": False,
    },
    "sync": {
        "default_delay": 5,
        "force_send": False,
        "add_external_source_header": False,
        "system_log_retention_limit": 1000,
        "message_log_retention_limit": 5000,
        "bot_upload_max_mb": 50,
        "bot_rate_limit_enabled": False,
        "bot_rate_limit_gb": 10,
        "bot_rate_limit_window_hours": 24,
        "bot_rate_limit_cooldown_minutes": 300,
        "realtime_sender": "bot",
        "realtime_fallback_to_user": True,
        "realtime_hash_perturb": False,
    },
    "app": {
        "portable_mode": True,
        "log_level": "INFO",
        "debug_terminal_logs": False,
    },
}

_CONFIG_CACHE: dict[str, Any] | None = None


def _merge_dict(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def _normalize_int(value: Any, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        normalized = int(str(value if value is not None else default).strip() or default)
    except (TypeError, ValueError):
        normalized = default
    if minimum is not None:
        normalized = max(minimum, normalized)
    if maximum is not None:
        normalized = min(maximum, normalized)
    return normalized


def _normalize_str(value: Any, default: str = "") -> str:
    return str(value or default).strip()


def _normalize_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if not normalized:
            return bool(default)
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    return bool(value if value is not None else default)


def _normalize_float(value: Any, default: float, *, minimum: float | None = None, maximum: float | None = None) -> float:
    try:
        normalized = float(value if value is not None else default)
    except (TypeError, ValueError):
        normalized = float(default)
    if minimum is not None:
        normalized = max(minimum, normalized)
    if maximum is not None:
        normalized = min(maximum, normalized)
    return normalized


def _normalize_token_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [token.strip() for token in value.replace(",", "\n").splitlines() if token.strip()]
    if isinstance(value, list):
        return [str(token or "").strip() for token in value if str(token or "").strip()]
    return []


def _normalize_config(config: dict[str, Any]) -> dict[str, Any]:
    merged = _merge_dict(DEFAULT_CONFIG, config)
    telegram = merged["telegram"]
    proxy = merged["proxy"]
    server = merged["server"]
    sync = merged["sync"]
    app = merged["app"]

    telegram["api_id"] = _normalize_int(telegram.get("api_id", 0), 0)
    telegram["api_hash"] = _normalize_str(telegram.get("api_hash", ""))
    telegram["bot_token"] = _normalize_str(telegram.get("bot_token", ""))
    telegram["bot_api_base_url"] = _normalize_str(telegram.get("bot_api_base_url", ""))
    telegram["extra_bot_tokens"] = _normalize_token_list(telegram.get("extra_bot_tokens", []))

    proxy["enabled"] = _normalize_bool(proxy.get("enabled", False))
    proxy["host"] = _normalize_str(proxy.get("host", ""))
    proxy["port"] = _normalize_int(proxy.get("port", 7897), 7897)
    proxy["username"] = _normalize_str(proxy.get("username", ""))
    proxy["password"] = _normalize_str(proxy.get("password", ""))

    server["host"] = _normalize_str(server.get("host", "127.0.0.1"), "127.0.0.1")
    server["port"] = _normalize_int(server.get("port", 8011), 8011)
    server["auto_open_browser"] = _normalize_bool(server.get("auto_open_browser", False))

    sync.pop("prefer_local_bot_api", None)
    sync["default_delay"] = _normalize_float(sync.get("default_delay", 5), 5, minimum=0.5)
    sync["force_send"] = _normalize_bool(sync.get("force_send", False))
    sync["add_external_source_header"] = _normalize_bool(sync.get("add_external_source_header", False))
    sync["system_log_retention_limit"] = _normalize_int(sync.get("system_log_retention_limit", 1000), 1000, minimum=100)
    sync["message_log_retention_limit"] = _normalize_int(sync.get("message_log_retention_limit", 5000), 5000, minimum=100)
    sync["bot_upload_max_mb"] = _normalize_float(sync.get("bot_upload_max_mb", 50), 50, minimum=1.0)
    sync["bot_rate_limit_enabled"] = _normalize_bool(sync.get("bot_rate_limit_enabled", False))
    sync["bot_rate_limit_gb"] = _normalize_float(sync.get("bot_rate_limit_gb", 10), 10, minimum=0.1)
    sync["bot_rate_limit_window_hours"] = _normalize_float(sync.get("bot_rate_limit_window_hours", 24), 24, minimum=1.0)
    sync["bot_rate_limit_cooldown_minutes"] = _normalize_float(sync.get("bot_rate_limit_cooldown_minutes", 300), 300, minimum=1.0)
    sync["realtime_sender"] = "user" if _normalize_str(sync.get("realtime_sender", "bot")) == "user" else "bot"
    sync["realtime_fallback_to_user"] = _normalize_bool(sync.get("realtime_fallback_to_user", True), True)
    sync["realtime_hash_perturb"] = _normalize_bool(sync.get("realtime_hash_perturb", False))
    app["portable_mode"] = _normalize_bool(app.get("portable_mode", True), True)
    app["log_level"] = _normalize_str(app.get("log_level", "INFO"), "INFO").upper() or "INFO"
    app["debug_terminal_logs"] = _normalize_bool(app.get("debug_terminal_logs", False))
    return merged


def load_config() -> dict[str, Any]:
    global _CONFIG_CACHE
    ensure_runtime_dirs()
    cfg_path = config_file()
    if not cfg_path.exists():
        config = deepcopy(DEFAULT_CONFIG)
        return save_config(config)

    try:
        config = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        config = deepcopy(DEFAULT_CONFIG)
    config = _normalize_config(config)
    _CONFIG_CACHE = deepcopy(config)
    return deepcopy(config)


def save_config(config: dict[str, Any]) -> dict[str, Any]:
    global _CONFIG_CACHE
    ensure_runtime_dirs()
    normalized = _normalize_config(config)
    config_file().write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    _CONFIG_CACHE = deepcopy(normalized)
    return deepcopy(normalized)


def get_config() -> dict[str, Any]:
    global _CONFIG_CACHE
    if _CONFIG_CACHE is None:
        _CONFIG_CACHE = load_config()
    return deepcopy(_CONFIG_CACHE)


def clear_config_cache() -> None:
    global _CONFIG_CACHE
    _CONFIG_CACHE = None


def get_setup_status(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or load_config()
    telegram = config["telegram"]
    missing_fields: list[str] = []

    if not telegram["bot_token"]:
        missing_fields.append("telegram.bot_token")

    has_api_credentials = bool(telegram["api_id"] and telegram["api_hash"])
    first_run = (
        not telegram["bot_token"]
        and not has_api_credentials
        and not config["telegram"].get("bot_api_base_url")
    )
    return {
        "first_run": first_run,
        "needs_setup": len(missing_fields) > 0,
        "missing_fields": missing_fields,
        "has_bot_token": bool(telegram["bot_token"]),
        "has_api_credentials": has_api_credentials,
    }
