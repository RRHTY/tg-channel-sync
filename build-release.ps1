$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectRoot "venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "未找到项目虚拟环境：$Python"
}

& $Python (Join-Path $ProjectRoot "scripts\build_release.py") @args
exit $LASTEXITCODE
