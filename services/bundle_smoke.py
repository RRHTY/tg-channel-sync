from __future__ import annotations

import importlib

from app_config import get_config, save_config
from app_paths import (
    app_root,
    config_file,
    data_dir,
    ensure_runtime_dirs,
    logs_dir,
    sessions_dir,
    static_dir,
    temp_dir,
)
from services.version_service import get_local_version


DYNAMIC_BUSINESS_MODULES = ("bot_engine", "sync_worker.clone.process")


def run_bundle_smoke() -> dict[str, object]:
    ensure_runtime_dirs()
    for module_name in DYNAMIC_BUSINESS_MODULES:
        importlib.import_module(module_name)

    index_file = static_dir() / "index.html"
    if not index_file.is_file() or index_file.stat().st_size == 0:
        raise RuntimeError(f"missing bundled page resource: {index_file}")

    save_config(get_config())
    required_paths = (data_dir(), temp_dir(), logs_dir(), sessions_dir())
    missing_directories = [str(path) for path in required_paths if not path.is_dir()]
    if missing_directories:
        raise RuntimeError(f"runtime directories were not created: {missing_directories}")
    if not config_file().is_file():
        raise RuntimeError(f"runtime config was not written: {config_file()}")

    return {
        "version": get_local_version(),
        "runtime_root": str(app_root()),
        "modules": list(DYNAMIC_BUSINESS_MODULES),
        "static_index": True,
        "config_written": True,
    }
