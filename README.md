# tg-channel-sync (杏铃同步台)

一个面向 Telegram 频道同步和历史迁移的 Web 工具。
支持实时同步、JSON 导入、API 复制、下载重传，适合做频道搬运、备份恢复和多目标分发。

[GitHub 仓库](https://github.com/RRHTY/tg-channel-sync)  | [Release](https://github.com/RRHTY/tg-channel-sync/releases)

**频道内容**

![频道内容预览 1](https://github.com/user-attachments/assets/7d25932c-2cce-4dea-9879-fde967e2fc21 "频道内容预览 1")
<img width="1322" height="776" alt="image" src="https://github.com/user-attachments/assets/31141f35-8756-4b69-98e1-7264a5ded53b" />

**Web 页面**

| 首页 | 设置 |
| :------: | :------: |
| <img width="909" height="1368" alt="9ad40504818ef0db25a80992ff046bf3" src="https://github.com/user-attachments/assets/a166c8f4-da75-4ee5-bdb6-3941deb9ed6c" />| <img width="1250" height="2247" alt="391a9af42d8e1a2585397b1e824a8089" src="https://github.com/user-attachments/assets/b7ee5d3c-1d7b-40a8-925b-6332ab18ce6a" />|

---

## 特性

| 功能      | 说明                                                      | 适合场景                 |
| --------- | --------------------------------------------------------- | ------------------------ |
| 实时同步  | 监听源频道新消息并按映射实时发送到目标频道                | 日常搬运、多目标分发     |
| JSON 导入 | 导入 Telegram 官方导出的 `result.json` 和同目录媒体文件 | 备份恢复、离线导入       |
| API 复制  | 通过辅助账号直接复制历史消息                              | 大批量历史迁移           |
| 下载重传  | 先下载再上传，适合需要重新发送媒体的场景                  | 弱化转发痕迹、大文件重发 |

### 通用能力

- **多对多频道映射**：实时同步支持一个源到多个目标，也支持多个源汇聚到同一个目标
- **发送策略可单独配置**：每条映射可分别设置发送身份、失败回退和实时重传策略
- **消息过滤**：支持按消息类型过滤，支持正则替换文本或直接丢弃消息
- **回复关系保留**：实时同步、API 复制、下载重传会尽量恢复回复关系，JSON 导入支持普通回复恢复
- **链接改写**：支持将消息内 `t.me` 链接改写到目标频道已同步消息
- **多 Bot 上传池**：支持多个 Bot Token 轮换上传，并按阈值自动冷却
- **媒体重传增强**：JSON 导入和下载重传支持媒体指纹扰动，下载重传支持大文件、媒体组、断点续传和失败回退
- **Web UI 控制台**：支持配置、登录、启动任务、查看日志和导出日志
- **便携式目录**：配置、数据库、日志、session 和临时文件均保存在项目目录内，便于迁移和备份

---

## 部署与运行

Windows x64 推荐直接下载 Release 中已构建完毕的 `full` 版本，无需额外安装 Python 环境：
[Release](https://github.com/RRHTY/tg-channel-sync/releases)

---

### 运行环境要求

- **Python 3.10+**

### 运行步骤

1. 克隆代码仓库并进入目录：

   ```powershell
   git clone https://github.com/RRHTY/tg-channel-sync.git
   cd tg-channel-sync
   ```
2. 启用虚拟环境：

   ```powershell
   .\venv\Scripts\activate
   ```
3. 安装依赖：

   ```powershell
   pip install -r requirements.txt
   ```
4. 启动服务：

   ```powershell
   python main.py
   ```
5. 首次配置：

   启动后访问 `http://127.0.0.1:8011`

   - **Bot Token**：必填，用于实时同步与基础发送
   - **Bot API Base URL**：可选，接入自建 Bot API 时填写
   - **API ID / API Hash**：使用 API 复制、下载重传时推荐填写
   - **辅助账号登录**：若配置了 API 参数，需要在设置页完成验证码登录

### 如何构建 Windows 便携版

如果你希望发布或自用 Windows 便携版，仓库内已提供 `PyInstaller` 打包文件和 PowerShell 构建脚本。

1. 进入项目目录并启用虚拟环境：

   ```powershell
   .\venv\Scripts\activate
   ```
2. 安装依赖并补充打包工具：

   ```powershell
   pip install -r requirements.txt
   pip install pyinstaller
   ```
3. 执行打包脚本：

   ```powershell
   .\build-portable.ps1
   ```
4. 打包完成后，产物会输出到 `dist-portable/`：

   - `tg-channel-sync-vX.Y.Z-windows-x64-portable.zip`：便携 exe 版，不包含 Python 环境
   - `tg-channel-sync-vX.Y.Z-windows-x64-full.zip`：完整运行环境版，包含源码、精简 Python 运行时和依赖
5. 便携版运行后会在程序目录旁生成或使用这些文件：

   - `config.json`：运行配置
   - `data/`：数据库、日志、session 等运行数据
   - `temp/`：下载重传和临时媒体处理目录

构建补充：

1. `full` 包不再复制整个开发 `venv`，而是内置精简运行时
2. 打包脚本默认使用当前 `VERSION` 文件内容生成压缩包名称
3. 媒体指纹扰动采用纯 Python 方式处理，目标是改变基础哈希特征，不保证适用于所有平台或更严格的媒体查重逻辑

### Docker 部署

Docker 部署只负责运行环境、端口监听和数据持久化。Bot Token、API ID、API Hash、代理和同步参数仍通过 Web 前端配置，并保存到宿主机挂载的 `config.json`。

1. 克隆代码仓库并进入目录：

   ```bash
   git clone https://github.com/RRHTY/tg-channel-sync.git
   cd tg-channel-sync
   ```

2. 创建宿主机配置文件和运行目录：

   ```bash
   touch config.json
   mkdir -p data temp
   ```

   Windows PowerShell 可使用：

   ```powershell
   New-Item -ItemType File -Force config.json
   New-Item -ItemType Directory -Force data, temp
   ```

3. 启动服务：

   ```bash
   docker compose up -d --build
   ```

4. 打开 `http://127.0.0.1:8011`，按初始化向导或设置页完成配置。

默认 `docker-compose.yml` 会持久化以下路径：

- `./config.json:/app/config.json`：前端保存的运行配置
- `./data:/app/data`：数据库、日志和 session
- `./temp:/app/temp`：下载重传和临时媒体处理目录

Compose 默认将宿主机端口绑定为 `127.0.0.1:8011:8011`，即只允许本机访问。容器内服务通过 `TG_SYNC_HOST=0.0.0.0` 监听，以便 Docker 端口映射正常工作。`TG_SYNC_HOST` 和 `TG_SYNC_PORT` 只影响容器启动时的实际监听地址，不会写入 `config.json`，也不会改变前端设置页保存的业务配置。

> [!WARNING]
> Web 控制台目前没有内置账号密码鉴权。不要直接将端口绑定到公网地址；如需远程访问，请在外层使用 VPN、带鉴权的反向代理，或 Cloudflare Tunnel 配合 Cloudflare Access 等访问控制方案。

---

## 功能矩阵

| 功能           | 实时同步           | JSON 导入                    | API 复制       | 下载重传       |
| -------------- | ------------------ | ---------------------------- | -------------- | -------------- |
| 频道映射       | 支持，多对多       | 不适用                       | 不适用         | 不适用         |
| 类型过滤       | 支持               | 支持                         | 支持           | 支持           |
| 正则过滤       | 支持               | 支持                         | 支持           | 支持           |
| 日志查看与导出 | 支持               | 支持                         | 支持           | 支持           |
| 链接改写       | 支持               | 支持，建议填写源频道用户名   | 支持           | 支持           |
| 普通回复恢复   | 支持               | 支持                         | 支持           | 支持           |
| 引用回复恢复   | 支持，尽量保留     | 不支持                       | 支持，尽量保留 | 支持，尽量保留 |
| 外部来源标头   | 支持               | 支持                         | 支持           | 支持           |
| 媒体组支持     | 支持               | 支持                         | 支持           | 支持           |
| 媒体指纹扰动   | 支持，可按映射配置 | 支持，处理临时副本不改原文件 | 不支持         | 支持           |
| 历史批量同步   | 不适用             | 支持                         | 支持           | 支持           |
| 断点续传       | 不适用             | 支持                         | 支持           | 支持           |

补充说明：

1. `频道映射` 目前主要服务于实时同步。历史任务仍然是手动指定 `source_id -> target_id` 启动，不会按映射表批量执行。
2. `JSON 导入` 已接入现有的类型过滤、正则过滤和链接改写逻辑。
3. `JSON 导入` 通常只能从导出文件中拿到 `reply_to_message_id`，因此最多只能恢复普通回复关系，不能恢复 Telegram 原生的引用回复片段。
4. `API 复制` 和 `下载重传` 依赖辅助账号登录；仅配置 `Bot Token` 时，主要可用实时同步和部分 JSON 导入能力。
5. `JSON 导入` 与 `下载重传` 模式均支持媒体指纹扰动：会在图片或视频文件尾部追加少量随机字节，以快速改变 MD5、SHA256 等基础哈希特征。
6. `JSON 导入` 会先复制媒体到临时目录后再处理，不会修改原始导出文件。
7. 该方案是“快、零依赖、弱对抗”的实现，适合绕过基础文件查重；不追求专业级去重对抗，也不保证在所有媒体处理链路中都稳定生效。

---

## 常见问题

**Q: 为什么点击“停止任务”后，UI 还会短暂显示进度等待？**
A: 程序在中断时会等待当前网络请求安全结束，并把断点和状态写回数据库，通常会有 1 到 2 秒的等待。

**Q: 下载重传模式对服务器有什么要求？**
A: 下载重传需要先把文件下载到本地 `temp` 目录再上传，因此需要一定的带宽和磁盘空间。处理大体积媒体组时，临时空间最好至少接近该媒体组总大小。

**Q: 旧版本运行目录可以直接复用吗？**
A: 当前版本按新环境初始化使用，不保证兼容旧版本数据库结构或旧运行目录。首次启动后，建议重新通过初始化向导或设置页填写配置。

## 开源协议

本项目采用 [MIT License](LICENSE) 开源。
