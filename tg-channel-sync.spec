import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


project_root = Path(SPECPATH).resolve()
binary_name = os.environ.get("TG_SYNC_BINARY_NAME", "tg-channel-sync").strip() or "tg-channel-sync"

hiddenimports = []
for package_name in [
    "aiogram",
    "pyrogram",
    "tgcrypto",
    "aiosqlite",
    "aiohttp",
    "python_socks",
    "sync_worker",
]:
    hiddenimports.extend(collect_submodules(package_name))
hiddenimports.append("bot_engine")

datas = [
    (str(project_root / "static"), "static"),
    (str(project_root / "VERSION"), "."),
]
datas += collect_data_files("pyrogram")
datas += collect_data_files("aiogram")


a = Analysis(
    [str(project_root / "main.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=binary_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
