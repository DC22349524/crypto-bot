# 停掉所有交易机器人实例（含“管理员启动、命令行读不到”的残留）
# 用法：管理员 PowerShell 里运行一次：
#   powershell -ExecutionPolicy Bypass -File C:\Users\<USER>\crypto-bot\stop_all_bot.ps1
# 清完后用桌面「交易」图标启动（非管理员启动，之后都能用图标一键重启）。
$ErrorActionPreference = "SilentlyContinue"
$all = Get-CimInstance Win32_Process
$par = @{}
$all | ForEach-Object { $par[[int]$_.ProcessId] = $_ }
$py = $all | Where-Object { $_.Name -in @('python.exe', 'pythonw.exe') }
$hit = @()
foreach ($p in $py) {
    $cmd = $p.CommandLine
    $pp = $par[[int]$p.ParentProcessId]
    if (($cmd -and $cmd -like "*bot.py*") -or ((-not $cmd) -and $pp -and $pp.Name -like "powershell*")) {
        $hit += $p
    }
}
if ($hit) {
    Write-Host ("  停掉 {0} 个实例: {1}" -f $hit.Count, (($hit.ProcessId | ForEach-Object { "$_" }) -join ", "))
    foreach ($p in $hit) { Stop-Process -Id $p.ProcessId -Force }
    Start-Sleep -Seconds 1
} else {
    Write-Host "  没有检测到交易机器人实例在运行。"
}
$left = Get-CimInstance Win32_Process -Filter "Name='python.exe' or Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like "*bot.py*" }
if ($left) {
    Write-Host ("  ⚠️ 仍有残留: {0}" -f ($left.ProcessId -join ", ")) -ForegroundColor Red
} else {
    Write-Host "  ✅ 已全部停掉。现在双击桌面「交易」图标启动即可。" -ForegroundColor Green
}
