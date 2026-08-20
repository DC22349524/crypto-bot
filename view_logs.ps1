# 交易机器人 日志查看
# 实时滚动显示：
#   logs\bot.log           机器人运行日志（指令/执行/回复/盯盘）
#   logs\operations.log    账户操作审计（下单/平仓/撤单/杠杆/计划）
# 手动增量轮询两个文件，避免 Get-Content -Wait 多文件的不确定性。关窗即退出。
$ErrorActionPreference = "SilentlyContinue"
$base = "C:\Users\<USER>\crypto-bot"
$bot = Join-Path $base "logs\bot.log"
$ops = Join-Path $base "logs\operations.log"

try { $Host.UI.RawUI.WindowTitle = "交易机器人日志" } catch {}

Write-Host ""
Write-Host "  📋 交易机器人日志（实时刷新，关闭窗口即退出）" -ForegroundColor Cyan
Write-Host "  ============================================" -ForegroundColor DarkGray
Write-Host "  · bot.log          运行日志：指令 / 执行 / 回复 / 盯盘" -ForegroundColor DarkGray
Write-Host "  · operations.log   账户操作：下单 / 平仓 / 撤单 / 杠杆 / 计划" -ForegroundColor DarkGray
Write-Host "  ============================================" -ForegroundColor DarkGray

$files = @()
if (Test-Path $bot) { $files += $bot } else { Write-Host "  ⚠️ 还没有 bot.log —— 先双击「交易」启动机器人" -ForegroundColor Yellow }
if (Test-Path $ops) { $files += $ops } else { Write-Host "  ℹ️ 还没有 operations.log —— 第一次账户操作后自动生成" -ForegroundColor Yellow }
if (-not $files) { Start-Sleep -Seconds 10; exit }

$pos = @{}
foreach ($f in $files) {
    $pos[$f] = (Get-Item $f).Length
    Write-Host ("  [已加载最近 {0}]" -f (Split-Path $f -Leaf)) -ForegroundColor DarkGray
    Get-Content -Encoding UTF8 -LiteralPath $f -Tail 20 | ForEach-Object { Write-Host $_ }
}
Write-Host ""

while ($true) {
    # 允许运行中新出现的日志文件
    foreach ($f in @($bot, $ops)) {
        if ((Test-Path $f) -and -not $pos.ContainsKey($f)) {
            $pos[$f] = (Get-Item $f).Length
        }
    }
    foreach ($f in @($files)) {
        if (-not (Test-Path $f)) { continue }
        $len = (Get-Item $f).Length
        if ($len -gt $pos[$f]) {
            $fs = $null
            try {
                $fs = [System.IO.File]::Open($f, 'Open', 'Read', 'ReadWrite')
                $fs.Seek($pos[$f], 'Begin') | Out-Null
                $sr = New-Object System.IO.StreamReader($fs)
                while (-not $sr.EndOfStream) { Write-Host $sr.ReadLine() }
                $sr.Close()
            } catch {
                # 文件被短暂占用时跳过本轮
            } finally {
                if ($fs) { $fs.Close() }
            }
            $pos[$f] = $len
        }
    }
    Start-Sleep -Milliseconds 700
}
