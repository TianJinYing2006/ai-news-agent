# 每日 AI 简报 · 一键启动（Windows）
# 用法：双击 start.cmd，或在 PowerShell 里：powershell -ExecutionPolicy Bypass -File start.ps1
# 参数：-NoBrowser  不自动打开浏览器（测试 / 无头环境用）
param(
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Write-Host ""
Write-Host "== 每日 AI 简报 · 本地启动 ==" -ForegroundColor Cyan
Write-Host "项目目录：$root"

# 1) 服务已经在运行吗？
$running = $false
try {
    $resp = Invoke-WebRequest -Uri "http://127.0.0.1:8000/" -UseBasicParsing -TimeoutSec 2
    $running = $true
} catch { }

if ($running) {
    Write-Host "服务已在运行：http://127.0.0.1:8000" -ForegroundColor Green
    if (-not $NoBrowser) { Start-Process "http://127.0.0.1:8000" }
    exit 0
}

# 2) 虚拟环境
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "[1/3] 创建虚拟环境 .venv ..."
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 -m venv .venv
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        & python -m venv .venv
    } else {
        Write-Host "未找到 Python。请先安装 Python 3.11+（https://www.python.org/downloads/），安装时勾选 Add to PATH，然后重试。" -ForegroundColor Red
        Read-Host "按回车退出"
        exit 1
    }
    if (-not (Test-Path $python)) {
        Write-Host "虚拟环境创建失败，请检查 Python 安装后重试。" -ForegroundColor Red
        Read-Host "按回车退出"
        exit 1
    }
}

# 3) 依赖（首次较慢；已装则跳过）
$previous = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $python -c "import news_agent" 2>$null
$importOk = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = $previous

if (-not $importOk) {
    Write-Host "[2/3] 安装依赖（首次约 1-3 分钟）..."
    & $python -m pip install -e .
    if ($LASTEXITCODE -ne 0) {
        Write-Host "依赖安装失败：请检查网络后重试（可先用 .venv\Scripts\python.exe -m pip install -e . 手动排查）。" -ForegroundColor Red
        Read-Host "按回车退出"
        exit 1
    }
}

# 4) 凭据提示（不阻塞启动；网页浏览不需要凭据）
$hasUserKey = [bool][Environment]::GetEnvironmentVariable("DEEPSEEK_API_KEY", "User")
$hasDotEnv = Test-Path (Join-Path $root ".env")
if (-not ($hasUserKey -or $hasDotEnv)) {
    Write-Host "提示：未检测到 DEEPSEEK_API_KEY / SMTP 配置——网页可正常浏览，" -ForegroundColor Yellow
    Write-Host "      「立即运行」与每日推送需先配置（见 README「快速开始」）。" -ForegroundColor Yellow
}

# 5) 启动服务（前台运行，日志直接显示；Ctrl+C 停止）
Write-Host "[3/3] 启动服务：http://127.0.0.1:8000（本窗口保持打开，Ctrl+C 停止）" -ForegroundColor Green
if (-not $NoBrowser) {
    Start-Job -ScriptBlock { Start-Sleep -Seconds 3; Start-Process "http://127.0.0.1:8000" } | Out-Null
}
& $python -m news_agent.web
