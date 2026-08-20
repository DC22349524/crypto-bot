# 重启 crypto-bot Telegram 守护（杀旧 bot.py 进程 → 隐藏窗口重新启动）
$ErrorActionPreference = "SilentlyContinue"
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*bot.py*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Sleep -Seconds 1
Start-Process -FilePath "C:\Users\<USER>\freqtrade\.venv\Scripts\python.exe" `
    -ArgumentList "bot.py" `
    -WorkingDirectory "C:\Users\<USER>\crypto-bot" `
    -WindowStyle Hidden
Start-Sleep -Seconds 4
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*bot.py*" } |
    Select-Object ProcessId, CommandLine
