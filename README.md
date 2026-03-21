# tg-channel-sync (杏铃同步台)

一款基于 Web UI 管理的 Telegram 频道消息同步与数据迁移工具。采用 FastAPI + Vue3 前后端分离架构，支持频道实时监听、历史数据断点续传、以及基于正则表达式的高级内容过滤。

[GitHub 仓库](https://github.com/RRHTY/tg-channel-sync)

<img width="1376" height="1182" alt="image" src="https://github.com/user-attachments/assets/f24db856-036c-4000-bed9-f1bcb9db854d" />
<img width="1194" height="1002" alt="image" src="https://github.com/user-attachments/assets/87c66f35-dde2-4afc-b7df-1534b56d8032" />

-----

## 特点

  - **可视化控制台**：提供任务调度、实时进度条及双分屏日志（系统运行日志与消息处理流水）。
  - **双引擎工作流**：
      - **Bot 引擎 (Aiogram)**：默认激活。负责 24 小时稳定实时监听转发，及本地 JSON 数据的解析上传。
      - **User 引擎 (Pyrofork)**：按需激活（需配置 API\_ID）。通过挂载辅助用户账号绕过私密频道限制，提供历史消息的全量遍历提取。
  - **高级内容过滤**：
      - **类型过滤**：支持细粒度拦截文本、图片、视频、文件包、音频或贴纸等独立消息类型。
      - **正则过滤**：支持自定义正则表达式，可无损替换/抹除指定文本，或直接丢弃命中规则的整条消息。
  - **媒体组（Album）保护**：原生支持识别并打包转发媒体组，确保图文排版不被拆散；遇到严重风控限制时自动触发防丢图的单条降级发送机制。
  - **状态安全与断点续传**：内置 SQLite 持久化记录每一条消息的映射关系 (Source -\> Target)。支持平滑退出（Graceful Shutdown），中断任务重启后自动跳过已发内容。

-----

## 部署指南

### 环境依赖

  - **Python 3.9 \~ 3.13**

> ⚠️ **注意**：请勿使用 Python 3.14+ 版本。本项目底层加解密库 `tgcrypto` 包含 C 扩展，高版本 Python 暂无官方预编译轮子，会导致环境构建失败。

### 安装步骤

1.  克隆代码并安装依赖：

    ```bash
    git clone https://github.com/RRHTY/tg-channel-sync.git
    cd tg-channel-sync
    pip install -r requirements.txt
    ```

2.  配置文件：

    ```bash
    cp .env.example .env
    ```

    编辑 `.env` 文件填入必要信息：

    ```ini
    # [可选] Telegram API 配置。填写后才能解锁 User 引擎的历史批量拉取功能
    API_ID=
    API_HASH=

    # [必填] 机器人 Token
    BOT_TOKEN=

    # [可选] 运行端口，默认 8011
    PORT=8011
    ```

3.  启动服务：

    ```bash
    python main.py
    ```

    服务启动后，通过浏览器访问 `http://localhost:8011` 进入控制面板。
    *(注：若启用了 API 拉取模式，首次启动需在控制台终端完成用户账号的登录验证)*

-----

## 典型使用场景

### 1\. JSON 数据无损导入

适用于已有源频道数据备份的场景。
通过 Telegram 桌面端导出频道历史（选择 `Machine-readable JSON` 格式并包含媒体文件）。在 Web 控制台输入 `result.json` 的绝对路径，程序会自动解析本地媒体与文本并推送到目标频道。

### 2\. API 历史拉取

适用于需搬运无管理权限频道（或私密频道）的场景。
利用 User 引擎拉取历史消息 ID 列表并转发。支持指定 ID 区间，或留空交由系统自动计算首尾边界。

### 3\. 日常增量同步 (实时监听)

在 Web 端配置“源频道 -\> 目标频道”的映射规则（支持直接粘贴频道的 `@username` 或链接），挂机即可自动搬运源频道的新增消息与编辑动作。

-----

## FAQ

**Q: 点击“停止任务”为何需要等待进度条？**
A: 为确保 SQLite 数据库映射状态不出现脏写，程序采用平滑退出设计。指令下发后，需等待当前正在执行的网络 IO 及防风控（Anti-Flood）休眠完毕才会完全释放线程。

**Q: 控制台提示 `port is already in use`？**
A: 端口被其他进程占用。请在 `.env` 中修改 `PORT` 变量为一个新端口（例如 8012）后重新启动。

**Q: 为什么部分相册还是被拆分成了单张发送？**
A: 当相册中的某一张图片触发了正则过滤规则、或者 Telegram 官方接口短时间内抛出复杂异常时，程序为了保证“不漏发任何一张合法图片”，会主动触发安全降级策略，将相册拆散后逐条重试发送。