# 启动本地网站
#   用法： .\run_web.ps1                正常模式（调模型）
#          .\run_web.ps1 -Mock          不调模型，只看界面
#          .\run_web.ps1 -Port 9000     换端口
#          .\run_web.ps1 -NoBrowser     不自动开浏览器
#
# 为什么要挑解释器：PATH 上的 python 未必装了 python-docx / PyMuPDF / Pillow，
# 直接用 `python web/server.py` 会让 PDF、Word、图片全部报 ImportError。
# 这里按「显式指定 → 项目 venv → 已知装好依赖的解释器 → PATH 上的 python」顺序探测，
# 选中后再校验一次依赖，避免"起得来、一上传就炸"。

param(
    [switch]$Mock,
    [int]$Port = 8765,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$env:PYTHONUTF8 = "1"

function Test-PythonDeps([string]$exe) {
    if (-not $exe) { return $false }
    if (-not (Test-Path $exe) -and -not (Get-Command $exe -ErrorAction SilentlyContinue)) { return $false }
    $code = "import docx, fitz, PIL"   # 解析 docx / 渲染 pdf / 图片转 PNG 三件套
    try {
        & $exe -c $code 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

$candidates = @()
if ($env:DSH_PYTHON) { $candidates += $env:DSH_PYTHON }
$candidates += (Join-Path $PSScriptRoot ".venv\Scripts\python.exe")
# 助手自带的解释器（只有装过 WorkBuddy 的机器才有，找不到会自动跳过）
$candidates += (Join-Path $env:USERPROFILE ".workbuddy\binaries\python\envs\default\Scripts\python.exe")
$candidates += "python"
$candidates += "py"

$py = $null
$pyWithDeps = $false
foreach ($c in $candidates) {
    if (-not (Get-Command $c -ErrorAction SilentlyContinue) -and -not (Test-Path $c)) { continue }
    if (-not $py) { $py = $c }                                  # 先记第一个能跑的兜底
    if (Test-PythonDeps $c) { $py = $c; $pyWithDeps = $true; break }
}

if (-not $py) {
    Write-Host "找不到 Python。装一个 Python 3.10+ 再回来。" -ForegroundColor Red
    Read-Host "按回车退出"
    exit 1
}

if (-not $pyWithDeps) {
    Write-Host "⚠️  $py 缺少依赖（python-docx / PyMuPDF / Pillow）。" -ForegroundColor Yellow
    Write-Host "   安装： $py -m pip install -r requirements.txt`n"
    Write-Host "   不装也能启动，但 PDF / Word / 图片上传会失败，只有粘贴文本可用。`n"
}

if (-not (Test-Path ".env") -and -not $Mock) {
    Write-Host "⚠️  没有找到 .env" -ForegroundColor Yellow
    Write-Host "   复制 .env.example 为 .env，填入 DSH_LLM_API_KEY 后再启动。"
    Write-Host "   或先用 .\run_web.ps1 -Mock 看界面。`n"
}

$serverArgs = @("web/server.py", "--port", "$Port")
if ($Mock) { $serverArgs += "--mock" }

Write-Host ""
Write-Host "  简历评审助手" -ForegroundColor Cyan
Write-Host "  → 浏览器打开： http://127.0.0.1:$Port" -ForegroundColor Cyan
Write-Host "  → Python： $py" -ForegroundColor DarkGray
Write-Host "  → 这个窗口要一直开着，关掉就等于关掉网站。停止：Ctrl+C" -ForegroundColor Yellow
Write-Host ""

# 服务起来后自动开浏览器（延迟 2 秒，等端口 listen 完）
try {
    Start-Job -ScriptBlock {
        param($u)
        Start-Sleep -Seconds 2
        Start-Process $u
    } -ArgumentList "http://127.0.0.1:$Port" | Out-Null
} catch { }

& $py @serverArgs
$code = $LASTEXITCODE
if ($code -ne 0) {
    Write-Host "`n服务异常退出（exit $code）。把上面的报错发我。" -ForegroundColor Red
    Read-Host "按回车退出"
}
