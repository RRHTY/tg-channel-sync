# tg-channel-sync · Telegram Channel Sync & History Migration

[简体中文](README.md) · **English** · [Download](https://github.com/RRHTY/tg-channel-sync/releases)

## Overview

tg-channel-sync is a self-hosted Telegram channel forwarder and history migration tool with a Web UI. Use it for real-time channel mirroring, copying existing messages, restoring a Telegram Desktop JSON export, or downloading and re-uploading media. Windows and Linux executables include the runtime; Docker Compose is available for servers and NAS deployments.

## Features

| Mode | Purpose |
| --- | --- |
| Real-time sync | Forward new messages between multiple source and target channels, with separate rules for each mapping. |
| API history copy | Copy a selected message range through a signed-in auxiliary Telegram account. |
| Media re-upload | Download media and upload it to the destination, including large files and media groups. |
| JSON export restore | Import a single-chat `result.json` export from Telegram Desktop with its local media files. |

Message-type filters, regular-expression rules, resumable history tasks, duplicate detection, and multiple upload bots are supported. Reply restoration and linked-message rewriting are best effort: the referenced messages must already have destination mappings. Full preservation of the source channel's structure is not guaranteed.

JSON import has unresolved bugs. Validate a small batch before a larger migration. It restores ordinary replies but cannot restore native quoted excerpts. The tool does not create Telegram exports or automatic backups.

## Quick start

### Windows / Linux

Download and extract the appropriate archive from [Releases](https://github.com/RRHTY/tg-channel-sync/releases). No Python installation is required.

- **Windows x64:** open the extracted `.exe`.
- **Linux x64:** run the executable after granting execute permission:

  ```bash
  cd tg-channel-sync-v0.5.4-linux-x64
  chmod +x tg-channel-sync-v0.5.4-linux-x64
  ./tg-channel-sync-v0.5.4-linux-x64
  ```

The Linux executable requires **glibc ≥ 2.35**; Alpine/musl is unsupported. Use Docker Compose when appropriate for your server.

### Docker Compose

```bash
git clone https://github.com/RRHTY/tg-channel-sync.git
cd tg-channel-sync
touch config.json
mkdir -p data temp
docker compose up -d --build
```

For an existing installation, keep your current `config.json`. PowerShell commands and persistence settings are documented in the [usage guide (Chinese)](docs/usage.md#docker-部署).

Open `http://127.0.0.1:8011` and configure a Bot Token. API history copy and media re-upload also require API ID, API Hash, and auxiliary-account login. Keep `config.json` and `data/` when moving an installation or making a backup.

> [!WARNING]
> The Web UI has no built-in authentication. Access is local-only by default. For remote access, use a VPN or an authenticated reverse proxy; do not expose the console directly to the public internet.

## Python libraries

| Library | Role |
| --- | --- |
| [aiogram](https://docs.aiogram.dev/) | Telegram Bot API receiving and sending |
| [pyrofork](https://github.com/Mayuri-Chan/pyrofork), TgCrypto-pyrofork | User-account MTProto sessions, history access, and cryptographic acceleration |
| [FastAPI](https://fastapi.tiangolo.com/), [Uvicorn](https://uvicorn.dev/) | Web API and application server |
| [aiosqlite](https://aiosqlite.omnilib.dev/) | Async SQLite storage for rules, message mappings, and delivery records |
| python-multipart | Web form parsing |
| aiohttp-socks, PySocks | Telegram client proxy support |

See [requirements.txt](requirements.txt) for dependencies and the [development guide (Chinese)](docs/development.md) for Python 3.10+ source installation and native builds.

## Screenshots

| Dashboard: layout 1 | Dashboard: layout 2 | Settings |
| :---: | :---: | :---: |
| <img width="280" alt="Telegram channel sync dashboard, layout 1" src="https://github.com/user-attachments/assets/0a326892-f753-4794-95bd-5870ca2c4f92" /> | <img width="280" alt="Telegram channel sync dashboard, layout 2" src="https://github.com/user-attachments/assets/4d7efb37-f610-446f-90c9-57230bc07738" /> | <img width="280" alt="Telegram channel sync settings" src="https://github.com/user-attachments/assets/cc4bcb13-d747-4ba3-844b-c3a829be6e54" /> |

## License

[MIT](LICENSE)
