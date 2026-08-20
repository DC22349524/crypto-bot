# crypto-bot — 用 Telegram 指挥 Claude 交易币安

在 Telegram 里用中文发指令，无头 Claude 判断并执行交易（通过 ccxt 直连币安），结果流式回传。

```
[Telegram] → bot.py → claude -p(无头) → Bash → trade_cli.py → 币安
```

## 快速开始

### 1. 创建 Telegram 机器人（一次性）
1. 用 Telegram 搜索 **@BotFather**，发送 `/newbot`，按提示起名，得到一个形如 `123456789:AA...` 的 **bot token**。
2. 发给 **@userinfobot** 任意消息，拿到你的 **数字 user_id**。

### 2. 填配置
复制 `config.example.json` 为 `config.json`，填入：
```json
"telegram": {
  "bot_token": "123456789:AA...",
  "allowed_user_ids": [123456789]
}
```
> `config.json` 已加入 `.gitignore`，密钥不会进 git。

### 3. 启动
```bat
C:\Users\<USER>\freqtrade\.venv\Scripts\python.exe bot.py
```
（隐藏后台运行：用 PowerShell 执行
`Start-Process -FilePath "C:\Users\<USER>\freqtrade\.venv\Scripts\python.exe" -ArgumentList "bot.py" -WorkingDirectory "C:\Users\<USER>\crypto-bot" -WindowStyle Hidden`）

### 4. 用起来
给你的 bot 发消息，例如：
- `查 BTC/USDT 现价`
- `买 50 USDT 的 BTC`
- `卖 0.001 BTC`
- `我的余额`
- `当前持仓`
- `/status` 查看模式 · `/reset` 重置会话

## 交易模式（config.json 的 `mode`）

| 模式 | 说明 | 前提 |
|---|---|---|
| `paper`（默认） | 虚拟账本 `paper_ledger.json`，真实公共行情成交，**绝不动真钱** | 无 |
| `testnet` | 币安现货测试网（`testnet.binance.vision`） | 需 testnet API key |
| `live` | 真实资金，**必须同时 `mode=live` 且 `live_confirmed=true`** 才放行 | 需真实 API key |

切换模式只改 `mode` 字段；`live` 还有一道 `live_confirmed` 硬闸（Claude 无写文件权限，改不了 config，只能人手切）。

### testnet / live 的币安 key 从哪来
- testnet：登录 https://testnet.binance.vision 生成测试 key，填入 `binance.testnet_api_key` / `testnet_secret`。
- live：币安官网 → API 管理 → 创建 API（**只勾选现货交易权限、禁用提现、设 IP 白名单**），填入 `binance.api_key` / `secret`。

## 合约交易（USDT-M 永续，`markets=usdt-m`）

机器人除现货外，支持 **U本位永续合约**（BTC/USDT:USDT 等）。合约可跑在**币安合约测试网**（`mode=testnet`，虚拟资金）或**真实资金**（`mode=live` + `live_confirmed=true` 双确认）。

### 切换市场
`config.json` 里 `markets`：`"spot"`（现货，默认）或 `"usdt-m"`（合约）；`mode` 切 `"testnet"` 走测试网。
> 合约**不走内置虚拟盘**：`mode=paper + markets=usdt-m` 会直接报错，请用 testnet。

### 拿合约测试网 key（一次性，约 2 分钟）
1. 打开 https://testnet.binancefuture.com，登录（无账号先注册）。
2. **API Management → Create API Key**（勾选合约交易权限），记下 key/secret。
3. 填入 `config.json`：
```json
"futures": {
  "default_leverage": 10,
  "max_leverage": 25,
  "margin_mode": "isolated",
  "min_notional": 5,
  "testnet_api_key": "你的key",
  "testnet_secret": "你的secret"
}
```
4. 把 `mode` 改 `"testnet"`、`markets` 改 `"usdt-m"`，重启 bot。
> 合约测试网 key 与现货测试网 key 是**两套独立系统**，互不通用。

### 合约子命令
```bat
python trade_cli.py leverage BTC/USDT 10         # 设杠杆（1~25，上限 config futures.max_leverage）
python trade_cli.py margin-mode BTC/USDT isolated # 保证金：isolated 逐仓 / cross 全仓
python trade_cli.py buy BTC/USDT 0.001 --lev 10  # 开多 0.001 BTC（市价）
python trade_cli.py sell BTC/USDT 0.001 --lev 10 # 开空（市价）
python trade_cli.py limit-sell BTC/USDT 0.001 65300 --lev 10  # 限价挂单：价格到 65300 才成交（交易所盯盘）
python trade_cli.py close BTC/USDT               # 全平（reduceOnly）
python trade_cli.py tp-sl BTC/USDT --tp 70000 --sl 64000   # 把止盈/止损保护单挂到交易所（closePosition 全平、mark 价触发；bot 挂了/关机也在；方向默认从持仓取）
python trade_cli.py positions                     # 全部持仓（含开仓价/强平价/盈亏/杠杆）
python trade_cli.py position BTC/USDT            # 单币对持仓
python trade_cli.py ticker BTC/USDT              # 行情（含 markPrice / fundingRate）
python trade_cli.py open                          # 查看挂单
python trade_cli.py cancel <订单ID>               # 撤单
python trade_cli.py cancel-all [BTC/USDT]         # 撤全部/按币对撤
python trade_cli.py plan add BTC/USDT short 64800 65300 --lev 10 --pct 10 --sl 65700 --tp 64100,63600  # 登记自动盯盘计划
python trade_cli.py plan list / plan remove BTC/USDT   # 查看/移除计划
```
Telegram 里直接说人话：`买 0.001 个 BTC 合约，10 倍杠杆`、`当前持仓`、`平仓 BTC`、`挂 65300 限价空单`、`撤单`、`盯盘：BTC 64800-65300 做空 10 倍 10%仓位 止损 65700`。

### 自动盯盘（plan）与撤单
- **限价单 = 交易所盯盘**：`limit-buy/limit-sell` 把单子挂到交易所，价格到了**交易所自动成交**（`open` 可看、`cancel` 可撤），电脑关机单子也挂着。
- **交易所保护单 = 交易所托管止盈止损**：`tp-sl` 把 `STOP_MARKET`（止损）+ `TAKE_PROFIT_MARKET`（止盈）挂到交易所（closePosition 全平、按标记价触发），**bot 挂了/电脑关机/断网都在**。plan 开仓成功后、ladder 首笔成交后 bot 会自动挂上；`open` 里 `algo_orders` 字段可见、`cancel`/`cancel-all` 可撤。
- **盯盘计划 = bot 盯盘**：`plan add` 登记后，bot 后台每 `poller.interval_seconds` 秒（默认 30s）查一次行情，价格进入 `[入场下沿, 入场上沿]` **自动开仓**，触发 `--sl`/`--tp` **自动平仓**，并 Telegram 通知。**依赖电脑开机 + bot 在跑**。
- **撤单**：`cancel <订单ID>`、`cancel-all`（全部/按币对）、`plan remove <币对>`（停止盯盘）。Telegram 直接说"撤单""全部撤""别盯了"即可。

### 固定格式信号 → 自动阶梯挂单（ladder）

收到这种固定格式信号时（含 `> 币对` 行 + 方向/入场/倍数/仓位/止盈/止损），bot 会自动识别并**直接执行**阶梯挂单，不用每次让 Claude 自由发挥：

```
> SNDK
  方向：做多
  入场：1510-1525附近
  倍数：5倍
  仓位：10%
  止盈：点位1：1580附近（求稳） 点位2：1625附近 点位3：1700附近（求稳）
  止损：小幅跌破1485一点。
```

转换与执行语义：
- **入场区间** → 挂 **50 张等差分段限价单**（做多挂买单、做空挂卖单），价位取每个小格中点、自动避开整数位（挂在支撑上方一点/阻力下方一点，满足信号"注"的要求）。
- **止盈点位1 / 止损** → bot 每 ~30 秒盯盘市价全平；**首笔成交后自动把止盈/止损挂成交易所保护单**（`tp-sl`，closePosition 全平），bot 挂了/关机也在。
- 点位 2/3 保留为参考，点位 1 触发即全平、其余不启用。
- 仓位 = 仓位% × 可用保证金 × 杠杆，平摊到每档；每档名义须 ≥ `futures.min_notional`（默认 5 USDT），不足会报错并提示减小档数。

命令行用法（`--config` 可切临时 testnet 配置预览/测试，不碰真钱）：
```bat
python ladder_signal.py parse  signal_sample.txt [--orders 50] [--config config.testnet.json]  REM 预览，不下单
python ladder_signal.py place  signal_sample.txt [--orders 50] [--config config.testnet.json]  REM 挂单+登记计划
python ladder_signal.py list / remove <计划id|币对>                                          REM 查看/撤销 Ladder 计划
```

与 `plan` 的区别：`plan add` 是**价格进区间后用市价一次性开仓**；ladder 是**提前把 50 档限价单挂到交易所**，随价格分层成交（平均建仓），到点位 1/止损由 bot 轮询市价全平并撤剩余挂单。同样依赖电脑开机 + bot 在跑（`ladder_signal.py` 位于项目目录，无头 Claude 的 allowedTools 已放行 `python ladder_signal.py *`）。

### 合约风险提示
- 杠杆放大盈亏，强平价随杠杆升高而逼近，可能爆仓归零。默认**逐仓 isolated**、默认 10x、**上限 25x**。
- 平仓请用 `close`（reduceOnly），避免反向开仓误操作。
- 真实资金合约（`mode=live + markets=usdt-m`）默认 `live_confirmed=false` 闸死，需人手改 config 才放行。

## 命令行工具（Claude 底层用的，也可手动试）
```bat
python trade_cli.py account                      # 当前模式/市场/账户
python trade_cli.py ticker BTC/USDT
python trade_cli.py balance
python trade_cli.py buy BTC/USDT --quote 50     # 花 50 USDT 市价买（现货）
python trade_cli.py sell BTC/USDT 0.001
python trade_cli.py position BTC/USDT
python trade_cli.py open
# 合约（markets=usdt-m + mode=testnet 时）
python trade_cli.py leverage BTC/USDT 10
python trade_cli.py buy BTC/USDT 0.001 --lev 10
python trade_cli.py close BTC/USDT
python trade_cli.py positions
```

## 桌面图标（Windows）

桌面有两个快捷方式，双击即可：

- **「交易」**——一键启动/重启机器人：杀旧实例 → 隐藏窗口启动（加载最新代码/配置）→ 显示 PID。
  若检测到**管理员启动的旧实例**（命令行读不到、停不掉），会停下并提示，需先从**管理员 PowerShell** 清理一次：
  ```powershell
  powershell -ExecutionPolicy Bypass -File C:\Users\<USER>\crypto-bot\stop_all_bot.ps1
  ```
  之后一直用「交易」图标启动即可（非管理员启动，之后都能一键重启）。
- **「交易日志」**——实时滚动查看 `logs\bot.log`（运行日志：指令/执行/回复/盯盘）与 `logs\operations.log`（账户操作审计）。关窗即退出。

> `logs\operations.log` 是**账户操作审计**：每次下单 / 平仓 / 撤单 / 改杠杆 / 改保证金 / 登记计划都追加一行 JSON（订单号、币对、方向、数量、价格、结果；密钥自动打码；`ticker`/`markets` 纯行情不记）。盯盘轮询自动开的单也在这里。

## 安全设计
- **白名单**：只响应 `allowed_user_ids` 里的账号，陌生消息忽略。
- **工具收口**：无头 claude 只能用 `python trade_cli.py <子命令>` 这一种 Bash，其余命令（pip/cd/rm 等）一律被权限系统拒绝（已实测 `cmd /c dir` 被拒）。
- **live 双闸**：`live_confirmed=false` 时真实下单被拒。
- **脱敏**：bot 回传前把 token / API key 替换为 `***`；`bot.log` 里 httpx 的 HTTP 日志已静音，**不会把 bot_token 写进日志**（`start_bot.ps1` 启动时还会顺手清掉旧日志里的这类行）。
- **单线程**：同时只处理一条指令，防止并发错乱。
- 第一版默认 `paper`，不会碰真实资金。

## 依赖
全部复用 freqtrade venv（`C:\Users\<USER>\freqtrade\.venv`），无需新增 pip 包：
ccxt、python-telegram-bot 22.8、httpx。

## 已知限制
- 合约只做 U本位永续（USDT-M）；不支持 COIN-M / 交割合约。
- 合约可跑 testnet（币安合约测试网，虚拟资金）或 live（真实资金，需 `live_confirmed=true` 双确认）；**不支持 `mode=paper`**（合约不走内置虚拟盘，会直接拒绝）。双向持仓（hedge）账户已适配，开/平/限价单自动带 positionSide。
- 盯盘计划（plan）的止盈止损由 **bot 软件监控**（不是交易所单），bot 停机即失效——与"电脑开机"依赖一致；限价单才是交易所托管的。
- paper 模式限价单按当前价立即模拟成交（简化撮合）。
- 现货每个币对最小下单约 10 USDT；合约最小名义约 5 USDT。
