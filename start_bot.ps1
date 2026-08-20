# 交易机器人 一键启动
# 停掉旧 bot.py 实例 → 隐藏窗口启动新实例（加载最新代码/配置）→ 验证 → 自动关窗
# 若检测到“管理员启动的实例”（命令行读不到、停不掉），会停下并提示先清理一次，
# 之后正常用本图标启动即可（非管理员启动，之后都能用图标一键重启）。
$ErrorActionPreference = "SilentlyContinue"
$python = "C:\Users\<USER>\freqtrade\.venv\Scripts\python.exe"
$wd = "C:\Users\<USER>\crypto-bot"
$botLog = Join-Path $wd "logs\bot.log"

try { $Host.UI.RawUI.WindowTitle = "交易机器人 启动" } catch {}

Write-Host ""
Write-Host "  📈 交易机器人 一键启动" -ForegroundColor Cyan
Write-Host "  ========================" -ForegroundColor DarkGray

# 1) 停掉旧实例（venv 重定向器 + 真实解释器，命令行都含 bot.py）
$old = Get-CimInstance Win32_Process -Filter "Name='python.exe' or Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like "*bot.py*" }
if ($old) {
    Write-Host ("  停掉旧实例 PID {0}..." -f (($old.ProcessId | ForEach-Object { "$_" }) -join ", ")) -ForegroundColor Yellow
    foreach ($p in $old) { Stop-Process -Id $p.ProcessId -Force }
    Start-Sleep -Seconds 1
}

# 2) 杀完后重新全量查询，检查“读不到命令行”的残留 bot 进程
#    （管理员启动的实例：CommandLine 为空且父进程是 powershell）
$all = Get-CimInstance Win32_Process
$par = @{}
$all | ForEach-Object { $par[[int]$_.ProcessId] = $_ }
$py = $all | Where-Object { $_.Name -in @('python.exe', 'pythonw.exe') }
$ghost = @()
foreach ($p in $py) {
    $cmd = $p.CommandLine
    $pp = $par[[int]$p.ParentProcessId]
    if (($cmd -and $cmd -like "*bot.py*") -or ((-not $cmd) -and $pp -and $pp.Name -like "powershell*")) {
        $ghost += $p.ProcessId
    }
}
if ($ghost) {
    Write-Host ("  ⚠️ 检测到 {0} 个无法直接停止的实例 (PID {1})" -f $ghost.Count, ($ghost -join ", ")) -ForegroundColor Red
    Write-Host "     它们可能是由“管理员 PowerShell”启动的。请先从管理员 PowerShell 运行一次：" -ForegroundColor Red
    Write-Host "       powershell -ExecutionPolicy Bypass -File C:\Users\<USER>\crypto-bot\stop_all_bot.ps1" -ForegroundColor Yellow
    Write-Host "     清理后再双击本图标。之后一直用本图标启动即可。" -ForegroundColor Red
    Write-Host ""
    Write-Host "  窗口 8 秒后自动关闭..." -ForegroundColor DarkGray
    Start-Sleep -Seconds 8
    exit
}

# 3) 清掉旧 bot.log 里可能含 bot_token 的 httpx 行（保留最近 200 行），再启动新实例
if (Test-Path $botLog) {
    $keep = Get-Content -Encoding UTF8 -LiteralPath $botLog -Tail 200 |
        Where-Object { $_ -notmatch 'api\.telegram\.org/bot' }
    Set-Content -LiteralPath $botLog -Value $keep -Encoding UTF8
}

# 4) 启动新实例（隐藏窗口，日志写 logs\bot.log）
Start-Process -FilePath $python -ArgumentList "bot.py" -WorkingDirectory $wd -WindowStyle Hidden
Start-Sleep -Seconds 4

# 4) 验证
$new = Get-CimInstance Win32_Process -Filter "Name='python.exe' or Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like "*bot.py*" }
if ($new) {
    Write-Host ("  ✅ 已启动，PID {0}" -f (($new.ProcessId | ForEach-Object { "$_" }) -join ", ")) -ForegroundColor Green
    if (Test-Path $botLog) {
        $last = Get-Content -Encoding UTF8 -LiteralPath $botLog -Tail 1
        if ($last) { Write-Host ("  🕘 最近日志：{0}" -f $last) -ForegroundColor DarkGray }
    }
} else {
    Write-Host "  ❌ 启动失败，请双击「交易日志」查看 logs\bot.log" -ForegroundColor Red
}
Write-Host ""
Write-Host "  窗口 5 秒后自动关闭..." -ForegroundColor DarkGray
Start-Sleep -Seconds 5
