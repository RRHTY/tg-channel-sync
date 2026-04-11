# tg-channel-sync (杏铃同步台)

一款基于 Web UI 管理的 Telegram 频道同步、复制、数据迁移工具。采用 FastAPI + Vue3 前后端分离架构，支持频道实时监听、多模式历史数据爬取、断点续传以及基于正则表达式的高级内容过滤。

[GitHub 仓库](https://github.com/RRHTY/tg-channel-sync)

<img width="1385" height="1315" alt="image" src="https://github.com/user-attachments/assets/0fb63bac-ea06-4ae4-b46a-92d4de27fce1" />

<img width="1194" height="1002" alt="image" src="https://github.com/user-attachments/assets/87c66f35-dde2-4afc-b7df-1534b56d8032" />

-----

## 核心特性

1. **双模式运行**：仅 Bot Token 即可运行实时同步功能，配置 TG API 后解锁全部历史迁移能力
2. **三种历史同步模式**：
   - **JSON 导入**：读取 Telegram 官方导出的本地数据备份并上传
   - **API 转发**：通过 API 无引用转发到目标频道，速度快
   - **下载重传**：下载媒体后重新上传，彻底去除转发特征
3. **实时频道映射**：配置源频道与目标频道的对应关系，新消息自动同步
4. **消息过滤**：支持正则替换、类型过滤（文本/图片/视频/文档等）
5. **WebUI 控制台**：Vue.js + Tailwind CSS，实时日志、状态监控、一键启停
6. **相册智能处理**：自动识别媒体组，保持相册完整性转发
7. **断点续传**：记录已同步消息 ID，避免重复
8. **FloodWait 自动处理**：触发风控时自动休眠重试

-----

## 部署与运行

### 环境要求

  - **Python 3.9 \~ 3.11** \> ⚠️ **注意**：请勿使用 Python 3.14 及以上版本。底层依赖的加解密库 `tgcrypto` 包含 C 扩展，过高的 Python 版本暂无兼容的预编译包，会导致构建失败。

### 安装步骤

1.  克隆代码仓库并进入目录：

    ```bash
    git clone https://github.com/RRHTY/tg-channel-sync.git
    cd tg-channel-sync
    ```

2.  安装依赖包：

    ```bash
    pip install -r requirements.txt
    ```

3.  环境变量配置：

    ```bash
    cp .env.example .env
    ```

    编辑 `.env` 文件，填入所需凭证：

    ```ini
    # [可选] Telegram API 凭据，用于激活 User 引擎 (API拉取/克隆模式必需)
    API_ID=12345678
    API_HASH=your_api_hash_here

    # [必填] 机器人 Token
    BOT_TOKEN=123456789:ABCDefgh...

    # [可选] Web 面板运行端口，默认 8011
    PORT=8011
    ```

4.  启动服务：

    ```bash
    python main.py
    ```

    服务启动后，通过浏览器访问 `http://localhost:8011` 进入控制面板。
    *(注：若配置了 API\_ID，首次启动需在命令行终端完成辅助账号的登录验证)*

-----

## 常见问题 (FAQ)

**Q: 为什么点击“停止任务”后，UI 会出现进度条等待？**
A: 为确保 SQLite 数据不出现脏写及底层网络流的安全释放，程序触发中断时会等待正在执行的网络请求切断并持久化当前断点。等待时间通常在 1\~2 秒。

**Q: 控制台提示 `port is already in use` 怎么办？**
A: 端口冲突。请在 `.env` 文件中修改 `PORT` 变量为其他可用端口（如 8012）后重启程序。

**Q: 下载重传模式 对服务器有什么要求？**
A: 下载重传模式需将文件先下载至本地 `temp` 目录再上传，因此需要一定的带宽与磁盘空间。单个文件上传成功即删除，但在处理含几十 GB 视频的巨型媒体组时，仍需保证本地有等同于该媒体组大小的临时空间。
