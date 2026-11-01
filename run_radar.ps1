# 医药政策雷达 · 本机每日运行器
#
# 为什么跑在本机而不是 GitHub Actions：
#   2026-11-01 实测，云端 runner 抓中国政府站失败——
#     · NMPA 对数据中心 IP 直接返回 412（本地首请求 200，云端重试仍 412）
#     · 国家医保局 nhsa.gov.cn 在云端 ConnectTimeout
#     · 上海药事所也拿不到
#   而本地（中国网络）实测 118 条 / 13 源全部有产出。
#   所以：**抓取与成稿在本机跑**，跑完把 data/posts/site 提交推送到 GitHub，
#   由 pages.yml 部署站点；云端 radar.yml 保留作兜底（只有海外源，产出少）。
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File run_radar.ps1
#   powershell ... -File run_radar.ps1 -Date 2026-11-01 -NoPush
#   powershell ... -File run_radar.ps1 -SkipFetch    # 复用当天 data/raw
param(
    [string]$Date = "",
    [switch]$NoPush,
    [switch]$SkipFetch,
    [switch]$DryRun
)

$ErrorActionPreference = "Continue"
$Root = $PSScriptRoot
$Src = Join-Path $Root "src"
$LogDir = Join-Path $Root "data\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
if (-not $Date) { $Date = (Get-Date).ToString("yyyy-MM-dd") }
$LogFile = Join-Path $LogDir "policy-$Date.log"

function Log([string]$m) {
    $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $m
    Write-Host $line
    Add-Content -Path $LogFile -Value $line -Encoding utf8
}

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Log "==================== 医药政策雷达 $Date 启动 ===================="

# ---- 环境变量：从用户级注册表取（计划任务进程里默认是空的）----
foreach ($v in @("LLM_API_KEY", "MINIMAX_API_KEY", "DEEPSEEK_API_KEY",
                 "FEISHU_WEBHOOK", "FEISHU_SECRET")) {
    if (-not [Environment]::GetEnvironmentVariable($v, "Process")) {
        $uv = [Environment]::GetEnvironmentVariable($v, "User")
        if ($uv) { [Environment]::SetEnvironmentVariable($v, $uv, "Process") }
    }
}
if (-not $env:LLM_API_KEY -and -not $env:DEEPSEEK_API_KEY) {
    Log "✗ 缺少 LLM key（LLM_API_KEY / DEEPSEEK_API_KEY）——退出"
    exit 3
}

# ---- 依次跑管线 ----
$steps = @()
if (-not $SkipFetch) {
    $steps += @{ Name = "采集"; Args = @("fetch.py", $Date) }
    $steps += @{ Name = "去重"; Args = @("dedupe.py", $Date) }
}
$steps += @{ Name = "评分"; Args = @("score.py", $Date) }
$steps += @{ Name = "选题"; Args = @("pick.py", $Date) }
if (-not $DryRun) {
    $steps += @{ Name = "成稿"; Args = @("compose.py", $Date) }
    $steps += @{ Name = "发布站点+飞书"; Args = @("publish.py", $Date) }
} else {
    $steps += @{ Name = "成稿(dry)"; Args = @("compose.py", $Date, "--dry-run") }
}

$failed = ""
foreach ($s in $steps) {
    Log "---- $($s.Name) ----"
    Push-Location $Src
    try {
        & python @($s.Args) 2>&1 | ForEach-Object { Log "  $_" }
        $code = $LASTEXITCODE
    } finally { Pop-Location }
    if ($code -ne 0) {
        Log "✗ $($s.Name) 失败（退出码 $code）"
        $failed = $s.Name
        break
    }
}

if ($failed) {
    Log "✗ 管线中断于：$failed"
    exit 2
}
if ($DryRun) { Log "dry-run 完成，不提交"; exit 0 }

# ---- 提交 + 推送（触发 pages.yml 部署）----
Log "---- 提交产出 ----"
Push-Location $Root
try {
    git config user.name  "radar-bot" 2>$null
    git config user.email "radar-bot@users.noreply.github.com" 2>$null
    if (-not $NoPush) {
        & git add data posts site 2>&1 | ForEach-Object { Log "  $_" }
        $staged = (& git diff --staged --name-only) -join ","
        if ($staged) {
            & git commit -q -m "chore(radar): $Date 采集/评分/成稿" 2>&1 | ForEach-Object { Log "  $_" }
            & git pull -q --rebase origin main 2>&1 | ForEach-Object { Log "  $_" }
            & git push -q origin main 2>&1 | ForEach-Object { Log "  $_" }
            if ($LASTEXITCODE -eq 0) { Log "✓ 已推送，等待 pages.yml 部署" }
            else { Log "✗ 推送失败（退出码 $LASTEXITCODE）" }
        } else {
            Log "  无变更，跳过提交"
        }
    } else {
        Log "  -NoPush：跳过提交推送"
    }
} finally { Pop-Location }

Log "==================== 医药政策雷达 $Date 结束 ===================="
exit 0
