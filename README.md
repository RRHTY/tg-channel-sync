# tg-channel-sync · 杏铃同步台

[简体中文](README.md) · [English](README_EN.md)

## English overview

tg-channel-sync is a self-hosted **Telegram channel forwarder and history migration tool** with a Web UI. It combines real-time channel sync and mirroring, many-to-many message routing, API-based history copying, Telegram Desktop JSON export restoration, and media download and re-upload. Native Windows x64 and Linux x64 builds run without installing Python; Docker Compose is also supported.

Configure Telegram access, mappings and filters, and monitor tasks and logs in your browser. History tasks can resume using stored progress, with best-effort album and reply handling and link rewriting for already synchronized messages. The Python backend uses **aiogram, Pyrofork, FastAPI and aiosqlite**. See the [English README](README_EN.md) for setup, dependencies and limitations.

## 中文介绍

自托管的 Telegram 频道同步与历史迁移工具。提供 Web 控制台、Windows / Linux 原生发行包和 Docker 部署方式。

[下载](https://github.com/RRHTY/tg-channel-sync/releases) · [使用指南](docs/usage.md) · [开发与构建](docs/development.md)

## 功能

| 模式 | 用途 |
| --- | --- |
| 实时同步 | 将源频道新消息发送到目标频道，支持多对多映射 |
| API 复制 | 通过辅助账号复制指定范围内的历史消息 |
| 下载重传 | 下载媒体后重新上传，支持大文件和媒体组 |
| JSON 导入 | 从 Telegram Desktop 单聊天导出的 `result.json` 和本地媒体恢复消息 |

支持类型与正则过滤、尽量恢复回复关系、消息链接改写、历史任务续传及多 Bot 上传池。具体支持范围见[功能矩阵](docs/usage.md#功能矩阵)。

JSON 导入仍有问题待修复，建议先验证少量消息。

## 快速开始

### 原生版本

从 [Releases](https://github.com/RRHTY/tg-channel-sync/releases) 下载对应压缩包，解压后运行，无需安装 Python。

- **Windows x64**：下载 `tg-channel-sync-v0.5.4-windows-x64.zip`，双击 `.exe`。
- **Linux x64**：下载 `tg-channel-sync-v0.5.4-linux-x64.zip`，解压后执行：

  ```bash
  cd tg-channel-sync-v0.5.4-linux-x64
  chmod +x tg-channel-sync-v0.5.4-linux-x64
  ./tg-channel-sync-v0.5.4-linux-x64
  ```

Linux 原生包要求 glibc ≥ 2.35，不支持 Alpine/musl。NAS 和服务器可使用 Docker Compose。

### Docker Compose

```bash
git clone https://github.com/RRHTY/tg-channel-sync.git
cd tg-channel-sync
touch config.json
mkdir -p data temp
docker compose up -d --build
```

Windows 命令与持久化配置见[Docker 部署](docs/usage.md#docker-部署)。

### 首次配置

打开 `http://127.0.0.1:8011`，按向导填写 Bot Token。API 复制和下载重传还需 API ID、API Hash，并完成辅助账号登录。

> [!WARNING]
> Web 控制台没有内置鉴权。默认仅允许本机访问；远程使用需通过 VPN 或带鉴权的反向代理，勿直接暴露到公网。

## 核心 Python 依赖

| 库 | 职责 |
| --- | --- |
| [aiogram](https://docs.aiogram.dev/) | Bot API 消息接收与发送 |
| [pyrofork](https://github.com/Mayuri-Chan/pyrofork)（Pyrogram 分支）、TgCrypto-pyrofork | 用户账号 MTProto 会话、历史读取与加密加速 |
| [FastAPI](https://fastapi.tiangolo.com/)、[Uvicorn](https://uvicorn.dev/) | Web API 与服务运行 |
| [aiosqlite](https://aiosqlite.omnilib.dev/) | 异步 SQLite，保存规则、消息映射与发送记录 |
| python-multipart | Web 表单解析 |
| aiohttp-socks、PySocks | Telegram 客户端代理支持 |

完整依赖见 [requirements.txt](requirements.txt)。

## 界面

| 首页样式 1 | 首页样式 2 | 设置 |
| :---: | :---: | :---: |
| <img width="1462" height="2155" alt="首页样式 1 / tg-channel-sync Web dashboard layout 1" src="https://github.com/user-attachments/assets/0a326892-f753-4794-95bd-5870ca2c4f92" /> | <img width="1462" height="1054" alt="首页样式 2 / tg-channel-sync Web dashboard layout 2" src="https://github.com/user-attachments/assets/4d7efb37-f610-446f-90c9-57230bc07738" /> | <img width="1462" height="2548" alt="设置 / tg-channel-sync settings" src="https://github.com/user-attachments/assets/cc4bcb13-d747-4ba3-844b-c3a829be6e54" /> |

### 频道内容示例

![频道内容预览 1 / Telegram channel content preview 1](https://github.com/user-attachments/assets/7d25932c-2cce-4dea-9879-fde967e2fc21)
<img width="1322" height="776" alt="频道内容预览 2 / Telegram channel content preview 2" src="https://github.com/user-attachments/assets/31141f35-8756-4b69-98e1-7264a5ded53b" />

## 协议

[MIT License](LICENSE)
