# 开发与构建

[项目首页](../README.md) · [使用指南](usage.md)

## 从源码运行

需要 Python 3.10 或更高版本，使用虚拟环境安装依赖。

### Windows PowerShell

```powershell
git clone https://github.com/RRHTY/tg-channel-sync.git
cd tg-channel-sync
python -m venv venv
.\venv\Scripts\activate
python -m pip install -r requirements.txt
python main.py
```

### Linux

```bash
git clone https://github.com/RRHTY/tg-channel-sync.git
cd tg-channel-sync
python3 -m venv venv
source venv/bin/activate
python -m pip install -r requirements.txt
python main.py
```

启动后访问 `http://127.0.0.1:8011`，按[配置说明](usage.md#首次配置)完成 Telegram 凭据与辅助账号登录。

## 构建原生版本

必须在目标平台构建：Windows 生成 Windows x64，Linux 生成 Linux x64。先按上方步骤安装依赖，并在已启用的虚拟环境中安装 PyInstaller：

```bash
python -m pip install pyinstaller
```

Windows PowerShell：

```powershell
.\build-release.ps1
```

Linux：

```bash
python scripts/build_release.py
```

产物位于 `dist-release/`。每个 ZIP 包含一个同名目录和一个自包含可执行文件；构建会解压最终 ZIP 执行 bundle smoke，验证动态模块、页面资源、版本与运行目录。

## 发布流程

[GitHub Actions](../.github/workflows/release.yml) 在推送版本标签后执行 Windows / Linux 测试、构建、冒烟验证和 Release 发布。版本号、标签与[发布说明](releases/)应保持一致。
