import json
import os
import socket
import threading
import time
import urllib.request
from asyncio import Event

import webbrowser


_BROWSER_OPEN_LOCK = threading.Lock()
_BROWSER_OPENED_URLS: set[str] = set()
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8011
SERVER_HOST_ENV = "TG_SYNC_HOST"
SERVER_PORT_ENV = "TG_SYNC_PORT"


def _normalize_host(value, default: str = DEFAULT_HOST) -> str:
    normalized = str(value or "").strip()
    return normalized or default


def _normalize_port(value, default: int = DEFAULT_PORT) -> int:
    try:
        normalized = int(str(value if value is not None else default).strip() or default)
    except (TypeError, ValueError):
        normalized = default
    return normalized


def resolve_server_config(server_cfg: dict) -> dict:
    file_host = _normalize_host(server_cfg.get("host"), DEFAULT_HOST)
    file_port = _normalize_port(server_cfg.get("port"), DEFAULT_PORT)
    host = _normalize_host(os.getenv(SERVER_HOST_ENV), file_host)
    port = _normalize_port(os.getenv(SERVER_PORT_ENV), file_port)
    return {
        **server_cfg,
        "host": host,
        "port": port,
        "auto_open_browser": bool(server_cfg.get("auto_open_browser", False)),
    }


def browser_url(host: str, port: int) -> str:
    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    return f"http://{browser_host}:{port}"


def should_auto_open_browser(server_cfg: dict) -> bool:
    disabled_by_env = os.getenv("TG_CHANNEL_SYNC_NO_BROWSER", "").strip().lower() in {"1", "true", "yes", "on"}
    return bool(server_cfg.get("auto_open_browser", False)) and not disabled_by_env


def _is_port_open(host: str, port: int) -> bool:
    target_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    try:
        with socket.create_connection((target_host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _request_json(url: str, timeout: float = 1.0):
    request = urllib.request.Request(url, headers={"User-Agent": "tg-channel-sync"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _is_our_instance_running(host: str, port: int) -> bool:
    base_url = browser_url(host, port)
    try:
        health = _request_json(f"{base_url}/health")
        if health.get("status") != "ok":
            return False
        app_info = _request_json(f"{base_url}/api/app_info")
        return isinstance(app_info, dict) and "bot" in app_info and "user" in app_info
    except Exception:
        return False


def reuse_existing_instance_or_exit(host: str, port: int, auto_open_browser: bool) -> bool:
    if not _is_port_open(host, port):
        return True

    for _ in range(10):
        if _is_our_instance_running(host, port):
            url = browser_url(host, port)
            print(f"[INFO] 检测到程序已在运行，复用现有实例: {url}")
            if auto_open_browser:
                webbrowser.open(url)
            return False
        time.sleep(0.3)

    print(
        f"[ERROR] 端口 {port} 已被其他程序占用，当前实例不会启动。"
        f" 请修改设置中的服务端口，或关闭占用该端口的程序后重试。"
    )
    return False


def launch_browser_when_ready(host: str, port: int, shutdown_event: Event) -> None:
    url = browser_url(host, port)

    with _BROWSER_OPEN_LOCK:
        if url in _BROWSER_OPENED_URLS:
            return
        _BROWSER_OPENED_URLS.add(url)

    def _worker():
        try:
            for _ in range(120):
                if shutdown_event.is_set():
                    return
                if _is_port_open(host, port):
                    webbrowser.open(url)
                    return
                threading.Event().wait(0.1)
        finally:
            with _BROWSER_OPEN_LOCK:
                _BROWSER_OPENED_URLS.discard(url)

    threading.Thread(target=_worker, daemon=True, name="browser-launcher").start()
