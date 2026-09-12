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
| <img width="1789" height="1539" alt="image" src="https://github.com/user-attachments/assets/47a43c9e-aa65-449e-b24b-1878234051ae" /> | <img width="1789" height="2385" alt="image" src="https://github.com/user-attachments/assets/b18e99f1-e5cb-4662-9a72-f789f1964399" /> |

---

## 特性

| 功能      | 说明                                                      | 适合场景                 |
| --------- | --------------------------------------------------------- | ------------------------ |
| 实时同步  | 监听源频道新消息并按映射实时发送到目标频道                | 日常搬运、多目标分发     |
| JSON 导入 | 导入 Telegram 官方导出的 `result.json` 和同目录媒体文件 | 备份恢复、离线导入       |
| API 复制  | 通过辅助账号直接复制历史消息                              | 大批量历史迁移           |
| 下载重传  | 先下载再上传，适合需要重新发送媒体的场景                  | 弱化转发痕迹、大文件重发 |

### 通用能力

- **映射快捷操作**：支持编辑显示名称与发送策略、暂停/恢复，以及一键填入历史同步。暂停会让正在发送的消息完成；公开频道恢复后从保留的读取位置补齐，Bot 监听暂停期间的消息需使用历史同步补齐。编辑不会清除去重记录。旧映射名称可通过编辑补充。

- **多对多频道映射**：实时同步支持一个源到多个目标，也支持多个源汇聚到同一个目标
- **发送策略可单独配置**：每条映射可分别设置发送身份、失败回退和实时重传策略
- **消息过滤**：支持按消息类型过滤、正则替换或屏蔽，保存前检查语法；可展开“测试当前规则”用示例文本/文件名预览草稿，不保存或发送消息。预览只测试当前草稿，实际规则按添加顺序作用于 HTML 文本；媒体组任一项命中屏蔽即整组跳过。实时 Bot 复制相册需要替换说明时会逐条复制，重传模式保持整组。
- **回复关系保留**：实时同步、API 复制、下载重传会尽量恢复回复关系，JSON 导入支持普通回复恢复
- **链接改写**：支持将消息内 `t.me` 链接改写到目标频道已同步消息
- **多 Bot 上传池**：支持多个 Bot Token 轮换上传，并按阈值自动冷却
- **媒体重传增强**：JSON 导入和下载重传支持媒体指纹扰动，下载重传支持大文件、媒体组、断点续传和失败回退
- **Web UI 控制台**：支持配置、登录、启动任务、查看日志和导出日志
- **便携式目录**：配置、数据库、日志、session 和临时文件均保存在项目目录内，便于迁移和备份

---

## 部署与运行

从 [Release](https://github.com/RRHTY/tg-channel-sync/releases) 下载与你系统对应的压缩包。原生版本均已包含运行环境，无需安装 Python。

### Windows x64

下载 `tg-channel-sync-v0.5.2-windows-x64.zip`，解压到具有写权限的目录，然后双击同名 `.exe`。首次启动会自动打开 Web 页面，并在程序旁创建配置和数据目录。

### Linux x64

下载 `tg-channel-sync-v0.5.2-linux-x64.zip` 并解压后运行：

```bash
cd tg-channel-sync-v0.5.2-linux-x64
chmod +x tg-channel-sync-v0.5.2-linux-x64
./tg-channel-sync-v0.5.2-linux-x64
```

Linux 原生版本面向 x86_64、glibc 2.35 或更高版本，不支持 Alpine/musl。NAS 和长期运行的服务器仍推荐使用下方的 Docker Compose 部署。

原生版本会在可执行文件所在目录创建或使用：

- `config.json`：运行配置
- `data/`：数据库、日志和 session
- `temp/`：下载重传和临时媒体

---

### 从源码运行（开发者）

需要 Python 3.10 或更高版本。

#### 运行步骤

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

### 控制台字段说明

为保持控制台简洁，以下非直观选项统一在此说明：

| 字段 | 作用 |
| --- | --- |
| JSON 导入 | 从 Telegram 导出的 `result.json` 及同目录媒体文件恢复消息 |
| API 复制 | 直接复制 Telegram 消息，适合快速迁移 |
| 下载重传 | 下载媒体后重新上传，适合需要更换发送身份或重置媒体指纹的场景 |
| 强制发送 | 忽略重复消息映射与断点记录，直接处理本次范围 |
| 外部来源前缀 | 在外部转发或回复消息前追加来源链接 |
| Bot 发送失败时改用辅助账号 | Bot 上传失败后自动切换为已登录的辅助账号继续发送 |
| 多 Bot 上传限流轮换 | 按配置的统计窗口、阈值和冷却时间轮换额外 Bot Token |
| Debug 终端日志 | 将系统日志、消息日志、接入库日志及 Bot 收到的消息同步输出到终端 |
| 日志保留条数 | 控制数据库中的日志上限；页面只展示近期记录，导出包含当前保留的全部日志 |
| 界面主题 | `CLover` 为黄绿、暖黄与青蓝配色；另提供 `Sakura Pop`、`Mint Melody`、`Starlight`，选择后即时预览，保存设置后持久化 |

### 构建原生单文件版本

构建必须在目标系统上执行：Windows 构建 Windows x64，Linux 构建 Linux x64。GitHub Actions 会在推送版本标签时并行完成两个平台的测试、构建、冒烟验证和 Release 发布。

1. 创建并启用虚拟环境，安装依赖和 PyInstaller：

   ```powershell
   python -m venv venv
   .\venv\Scripts\activate
   pip install -r requirements.txt
   pip install pyinstaller
   ```
2. Windows 执行：

   ```powershell
   .\build-release.ps1
   ```
   Linux 在已启用虚拟环境后执行：

   ```bash
   python scripts/build_release.py
   ```
3. 产物输出到 `dist-release/`。每个 ZIP 只有一个同名目录，目录内只有一个自包含可执行文件；构建过程会从最终 ZIP 解压并执行 bundle smoke，验证动态业务模块、页面资源、版本和运行目录。

媒体指纹扰动采用纯 Python 方式处理，目标是改变基础哈希特征，不保证适用于所有平台或更严格的媒体查重逻辑。

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
   JSON 导入未填写源用户名时，优先使用导出文件中的来源 ID 去重；缺少来源信息时按文件内容识别，移动同一文件不会重复导入，但修改或重新导出内容后可能重复发送。旧版共用来源 `0` 的记录无法安全归属，不自动迁移；升级后首次重跑旧导入请核对目标频道。已填写用户名的导入保留原有去重方式，重复导入时请保持来源填写方式一致。
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
