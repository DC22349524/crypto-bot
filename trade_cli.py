#!/usr/bin/env python
"""
crypto-bot 交易 CLI —— 通过 ccxt 直连币安，默认纸面模拟，可切 testnet / live。

用法（输出 JSON，供 Claude 直接阅读）:
  python trade_cli.py account                     当前模式/市场/账户概览
  python trade_cli.py balance                     余额（合约含可用保证金）
  python trade_cli.py ticker BTC/USDT             现价（合约含 markPrice/fundingRate）
  python trade_cli.py buy BTC/USDT --quote 50     现货市价买（花 50 USDT）
  python trade_cli.py buy BTC/USDT 0.001          现货市价买（买 0.001 BTC）
  python trade_cli.py sell BTC/USDT 0.001         市价卖
  python trade_cli.py limit-buy  BTC/USDT 0.001 60000 [--lev 10]   限价买（现货/合约；合约=挂单等成交）
  python trade_cli.py limit-sell BTC/USDT 0.001 70000 [--lev 10]   限价卖（现货/合约；合约=挂单等成交）
  python trade_cli.py position BTC/USDT           持仓/持有量（合约含强平价/盈亏）
  python trade_cli.py positions                   合约全部持仓
  python trade_cli.py leverage BTC/USDT 10        合约设置杠杆（1~25）
  python trade_cli.py margin-mode BTC/USDT isolated  合约保证金（isolated/cross）
  python trade_cli.py close BTC/USDT [数量]       合约平仓（reduceOnly）
  python trade_cli.py open                        未成交挂单
  python trade_cli.py orders [SYMBOL]             最近成交记录
  python trade_cli.py cancel ORDER_ID             撤单
  python trade_cli.py markets BTC/USDT            币对精度/最小下单量

市场（config markets）：
  - spot（默认）  现货，mode=paper 虚拟账本 / testnet 现货测试网 / live 真实。
  - usdt-m        U本位永续合约，只支持 testnet（币安合约测试网，虚拟资金）与 live。
                  合约不走虚拟盘（mode=paper + usdt-m 会拒绝），杠杆上限 config futures.max_leverage。

安全：
  - mode=paper   虚拟账本（paper_ledger.json），用真实公共行情成交，绝不动真实资金。
  - mode=testnet 币安测试网（现货 testnet.binance.vision / 合约 testnet.binancefuture.com）。
  - mode=live    真实资金！必须 mode=live 且 live_confirmed=true 双条件满足才放行。
"""

import argparse
import json
import os
import sys
import time

import ccxt

# 强制 UTF-8 输出，避免 Windows 控制台按 GBK 编码导致 bot/Claude 读串行
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
LEDGER_PATH = os.path.join(BASE_DIR, "paper_ledger.json")
PLANS_PATH = os.path.join(BASE_DIR, "plans.json")
# markets cache 按 spot/usdt-m 分开，见 _load_markets_cached

# paper 模式下每个币对的最小下单名义金额（USDT），防止产生微尘仓位
MIN_NOTIONAL = {"BTC/USDT": 10.0, "ETH/USDT": 10.0, "BNB/USDT": 10.0}


# ---------------------------------------------------------------- 基础工具

def load_config(path=None):
    if not path:
        path = CONFIG_PATH
    if not os.path.exists(path):
        sys.exit(json.dumps({"error": f"找不到配置 {path}，请先复制 config.example.json 为 config.json 并填写"}))
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def out(data):
    print(json.dumps(data, ensure_ascii=False, default=str))


_OP = {}  # 当前指令的 cfg/cmd/args，供 err() 写账户操作日志


def err(msg):
    out({"error": msg})
    cfg = _OP.get("cfg")
    if cfg:
        _oplog(cfg, _OP.get("cmd"), _OP.get("args"), {"error": msg}, status="error")
    sys.exit(2)


def _require_usdm(cfg):
    if cfg.get("markets", "spot") != "usdt-m":
        err("该命令仅合约（markets=usdt-m）可用，当前是现货")


# ---------------------------------------------------------------- 交易所

def make_spot_exchange(cfg, mode):
    if mode == "paper":
        # 空 key 必须显式 None，规避 ccxt 空字符串误判为有 key 的 bug
        ex = ccxt.binance({"apiKey": None, "secret": None, "enableRateLimit": True})
        ex.options["fetchCurrencies"] = False  # 避免 load_markets 走私有 SAPI 签名调用
        return ex
    b = cfg.get("binance", {})
    k = b.get("testnet_api_key", "") if mode == "testnet" else b.get("api_key", "")
    s = b.get("testnet_secret", "") if mode == "testnet" else b.get("secret", "")
    if not k or not s:
        err(f"mode={mode} 需要填写对应的币安 API key/secret（config.json → binance）")
    ex = ccxt.binance({"apiKey": k, "secret": s, "enableRateLimit": True})
    ex.options["fetchCurrencies"] = False
    ex.options["fetchOpenOrders"] = {"warnWithoutSymbol": False}
    if mode == "testnet":
        ex.set_sandbox_mode(True)  # spot 切到 testnet.binance.vision
    return ex


def make_usdm_exchange(cfg, mode):
    """U本位合约（USDT-M 永续）。只支持 testnet/live；合约不走虚拟盘（paper 直接拒绝）。"""
    f = cfg.get("futures", {})
    if mode == "paper":
        err("markets=usdt-m 不支持 mode=paper（合约不走虚拟盘）。请用 mode=testnet 跑币安合约测试网，或 mode=live（需 live_confirmed=true 双确认）。")
    if mode == "testnet":
        k, s = f.get("testnet_api_key", ""), f.get("testnet_secret", "")
        if not k or not s:
            err("mode=testnet + markets=usdt-m 需要合约测试网 key：config.json → futures → testnet_api_key/testnet_secret（去 https://testnet.binancefuture.com 注册）")
        ex = ccxt.binanceusdm({"apiKey": k, "secret": s, "enableRateLimit": True})
        ex.set_sandbox_mode(True)  # 切 testnet.binancefuture.com/fapi
        # ccxt 对合约测试网有个"已弃用"警告闸（私有调用即抛 NotSupported），
        # 实测测试网私有接口仍活着，这里显式关掉这个警告
        ex.options["disableFuturesSandboxWarning"] = True
    else:  # live
        b = cfg.get("binance", {})
        k, s = b.get("api_key", ""), b.get("secret", "")
        if not k or not s:
            err("mode=live + markets=usdt-m 需要真实币安 api_key/secret（config.json → binance）")
        ex = ccxt.binanceusdm({"apiKey": k, "secret": s, "enableRateLimit": True})
    # load_markets 默认会走 fetch_currencies（私有 SAPI 签名调用），这里显式关掉，只拉公开市场表
    ex.options["fetchCurrencies"] = False
    # fetch_open_orders 不带 symbol 会触发 ccxt 限速警告（被当成错误抛出来），关掉该警告
    ex.options["fetchOpenOrders"] = {"warnWithoutSymbol": False}
    return ex


def make_exchange(cfg):
    mode = cfg.get("mode", "paper")
    markets = cfg.get("markets", "spot")

    if markets == "usdt-m":
        ex = make_usdm_exchange(cfg, mode)
    else:
        ex = make_spot_exchange(cfg, mode)

    _load_markets_cached(ex, tag=markets)
    return ex


_MARKETS_CACHE_TTL_S = 24 * 3600  # 市场表缓存 24 小时后自动重拉，避免新上市币对一直查不到
_MARKETS_REFRESHED = set()  # 本进程内已做过"miss 重拉"的 tag，防止反复重拉


def _markets_cache_path(tag):
    return os.path.join(BASE_DIR, f"markets_cache_{tag}.json")


def _save_markets_cache(ex, tag):
    try:
        with open(_markets_cache_path(tag), "w", encoding="utf-8") as f:
            json.dump(ex.markets, f, ensure_ascii=False)
    except Exception:
        pass


def _load_markets_cached(ex, tag="spot"):
    """加载/缓存市场表。spot 与 usdt-m 分开缓存，避免现货/合约行情互相污染。

    缓存超过 24 小时就重拉一次（新币对/上币变更会自动补上）；
    缓存文件缺失或损坏则直接走 load_markets()。
    """
    ex._markets_tag = tag
    path = _markets_cache_path(tag)
    if os.path.exists(path):
        try:
            if time.time() - os.path.getmtime(path) < _MARKETS_CACHE_TTL_S:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and data:
                    ex.set_markets(data)
                    return
        except Exception:
            pass
    ex.load_markets()
    _save_markets_cache(ex, tag)


def require_order_allowed(cfg):
    """下单闸门：live 必须显式双确认（现货与合约都拦）。"""
    mode = cfg.get("mode", "paper")
    if mode == "live" and not cfg.get("live_confirmed", False):
        err("live 模式未确认：config.json 需 mode='live' 且 live_confirmed=true 才放行真实下单（含真实合约）。当前已拒绝。")
    return mode


# ---------------------------------------------------------------- 纸面账本

def load_ledger(cfg):
    if not os.path.exists(LEDGER_PATH):
        q = cfg.get("paper", {}).get("quote_currency", "USDT")
        init = cfg.get("paper", {}).get("initial_quote_balance", 1000.0)
        ledger = {"balances": {q: float(init)}, "orders": [], "open": []}
        save_ledger(ledger)
    with open(LEDGER_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_ledger(ledger):
    with open(LEDGER_PATH, "w", encoding="utf-8") as f:
        json.dump(ledger, f, ensure_ascii=False, indent=2)


def _check_notional(ex, symbol, cost):
    min_cost = MIN_NOTIONAL.get(symbol, 10.0)
    if cost < min_cost:
        err(f"下单金额过小：{symbol} 最小约 {min_cost} USDT，当前 {cost:.2f} USDT")


def _fmt_amt(ex, symbol, amt):
    try:
        return float(ex.amount_to_precision(symbol, amt))
    except Exception:
        return round(amt, 10)


def _fmt_price(ex, symbol, px):
    try:
        return float(ex.price_to_precision(symbol, px))
    except Exception:
        return round(px, 10)


def paper_execute(cfg, ex, symbol, side, amount=None, quote_amount=None, price=None, otype="market"):
    tick = ex.fetch_ticker(symbol)
    market = ex.market(symbol)
    base, quote = market["base"], market["quote"]
    ledger = load_ledger(cfg)
    bal = ledger["balances"]
    bal.setdefault(base, 0.0)
    bal.setdefault(quote, 0.0)
    fee_rate = float(cfg.get("ccxt", {}).get("taker_fee", 0.001))
    now = time.strftime("%Y-%m-%dT%H:%M:%S")

    if side == "buy":
        if quote_amount is not None:
            quote_amount = float(quote_amount)
            _check_notional(ex, symbol, quote_amount)
            if bal[quote] + 1e-9 < quote_amount:
                err(f"paper 余额不足：{quote} 只有 {bal[quote]:.2f}，需要 {quote_amount:.2f}")
            px = _fmt_price(ex, symbol, price if price else tick["ask"])
            base_amount = quote_amount / px
            fee = base_amount * fee_rate
            fill_qty = base_amount - fee
            bal[base] = round(bal[base] + fill_qty, 10)
            bal[quote] = round(bal[quote] - quote_amount, 10)
            fill = {"symbol": symbol, "side": "buy", "type": otype, "price": px,
                    "amount": _fmt_amt(ex, symbol, fill_qty), "cost": quote_amount,
                    "fee": fee, "time": now, "mode": "paper"}
        else:
            base_amount = float(amount)
            px = _fmt_price(ex, symbol, price if price else tick["ask"])
            cost = base_amount * px
            _check_notional(ex, symbol, cost)
            if bal[quote] + 1e-9 < cost:
                err(f"paper 余额不足：{quote} 只有 {bal[quote]:.2f}，需要 {cost:.2f}")
            fee = base_amount * fee_rate
            fill_qty = base_amount - fee
            bal[base] = round(bal[base] + fill_qty, 10)
            bal[quote] = round(bal[quote] - cost, 10)
            fill = {"symbol": symbol, "side": "buy", "type": otype, "price": px,
                    "amount": _fmt_amt(ex, symbol, fill_qty), "cost": cost,
                    "fee": fee, "time": now, "mode": "paper"}
    else:  # sell
        base_amount = float(amount)
        px = _fmt_price(ex, symbol, price if price else tick["bid"])
        if bal[base] + 1e-12 < base_amount:
            err(f"paper 余额不足：{base} 只有 {bal[base]:.10g}，需要 {base_amount:.10g}")
        proceeds = base_amount * px
        fee = proceeds * fee_rate
        bal[base] = round(bal[base] - base_amount, 10)
        bal[quote] = round(bal[quote] + proceeds - fee, 10)
        fill = {"symbol": symbol, "side": "sell", "type": otype, "price": px,
                "amount": base_amount, "cost": proceeds, "fee": fee, "time": now, "mode": "paper"}

    ledger["orders"].append(fill)
    save_ledger(ledger)
    return {"order": fill, "balances": {k: v for k, v in bal.items() if v and abs(v) > 1e-12}}


# ---------------------------------------------------------------- 真实下单

def real_execute(ex, symbol, side, amount=None, quote_amount=None, price=None, otype="market"):
    if otype == "market":
        if side == "buy" and quote_amount is not None:
            # spot 市价买：用 quoteOrderQty 指定花多少计价币，amount 传 None
            order = ex.create_order(symbol, "market", "buy", None, params={"quoteOrderQty": float(quote_amount)})
        else:
            order = ex.create_order(symbol, "market", side, amount)
    else:  # limit
        order = ex.create_order(symbol, "limit", side, amount, price)
    return order


# ---------------------------------------------------------------- 合约（USDT-M 永续）

def futures_config(cfg):
    return cfg.get("futures", {})


def _hedge_mode(cfg):
    """账户是否双向持仓（hedge）。双向模式下下单必须显式带 positionSide。"""
    return bool(futures_config(cfg).get("hedge_mode", False))


def _pos_side(cfg, side):
    """开仓的 positionSide：buy(开多)=LONG、sell(开空)=SHORT；单向模式返回 None。"""
    if not _hedge_mode(cfg):
        return None
    return "LONG" if side == "buy" else "SHORT"


def futures_setup(ex, symbol, cfg, lev=None):
    """开仓前设置保证金模式与杠杆（幂等：已有仓位时 Binance 会拒绝改动，忽略即可）。"""
    f = futures_config(cfg)
    lev = int(lev) if lev else int(f.get("default_leverage", 10))
    max_lev = int(f.get("max_leverage", 25))
    if not 1 <= lev <= max_lev:
        err(f"杠杆必须在 1~{max_lev} 之间（config futures.max_leverage），当前 {lev}")
    mm = f.get("margin_mode", "isolated")
    try:
        ex.set_margin_mode(mm, symbol)
    except Exception as e:
        # 已设成目标模式(-4046)可忽略；因该币对已有持仓改不了(-4052)才危险：
        # 新开仓会延续原保证金模式（如 cross），提示到 stderr 不污染 stdout 的 JSON
        msg = str(e)
        if "-4052" in msg or "not empty" in msg.lower():
            sys.stderr.write(f"WARN: {symbol} 无法设为 {mm}（已有持仓），新开仓将延续原保证金模式\n")
    try:
        ex.set_leverage(lev, symbol)
    except Exception:
        pass
    return lev


def futures_execute(cfg, ex, symbol, side, amount=None, quote_amount=None, lev=None):
    """合约市价开仓：buy=开多、sell=开空。按币数量下单；--quote 自动换算。"""
    if quote_amount is not None:
        last = ex.fetch_ticker(symbol)["last"]
        amount = float(quote_amount) / last
        amount = float(ex.amount_to_precision(symbol, amount))
        min_n = float(futures_config(cfg).get("min_notional", 5.0))
        if amount * last < min_n:
            err(f"下单金额过小：{symbol} 合约最小名义约 {min_n:.0f} USDT")
    amount = float(amount)
    lev = futures_setup(ex, symbol, cfg, lev)
    params = {}
    ps = _pos_side(cfg, side)
    if ps:
        params["positionSide"] = ps
    order = ex.create_order(symbol, "market", side, amount, params=params)
    return {"mode": cfg.get("mode"), "markets": "usdt-m", "side": side,
            "order": order, "leverage": lev}


def cmd_leverage(cfg, ex, symbol, lev):
    max_lev = int(futures_config(cfg).get("max_leverage", 25))
    lev = int(lev)
    if not 1 <= lev <= max_lev:
        err(f"杠杆必须在 1~{max_lev} 之间（config futures.max_leverage），当前 {lev}")
    ex.set_leverage(lev, symbol)
    return {"symbol": symbol, "leverage": lev}


def cmd_margin_mode(cfg, ex, symbol, mm):
    if mm not in ("isolated", "cross"):
        err("保证金模式只支持 isolated（逐仓）或 cross（全仓）")
    ex.set_margin_mode(mm, symbol)
    return {"symbol": symbol, "margin_mode": mm}


def _live_position(ex, symbol):
    """取某币对当前有持仓的一边。

    hedge 双向持仓下 fetch_positions 会返回 LONG/SHORT 两条，取 contracts>0 的那条，
    不能直接 pos[0]（可能是空仓的一边）。
    """
    try:
        ps = ex.fetch_positions([symbol]) or []
    except Exception:
        return None
    for p in ps:
        if float(p.get("contracts") or 0) > 0:
            return p
    return None


def cmd_close(cfg, ex, symbol, qty=None):
    """合约平仓（reduceOnly）。省略数量=全平；自动按持仓方向决定买卖方向。"""
    p = _live_position(ex, symbol) or {}
    side = p.get("side")
    contracts = float(p.get("contracts") or 0)
    if not side or contracts <= 0:
        err(f"{symbol} 当前无持仓可平")
    if qty is None:
        # 全平：按持仓数下，取到 lot 步长，避免非标数量被拒
        qty = float(ex.amount_to_precision(symbol, contracts))
        if qty <= 0 or qty > contracts + 1e-9:
            qty = contracts
    else:
        qty = float(qty)
        if qty > contracts + 1e-9:
            err(f"平仓数量 {qty} 超过当前持仓 {contracts}")
        qty = float(ex.amount_to_precision(symbol, qty))
    close_side = "sell" if side == "long" else "buy"
    params = {}
    if _hedge_mode(cfg):
        # 双向模式：Binance 不支持 reduceOnly（-1106 拒单），positionSide 指定平哪边，
        # 数量=持仓数、方向相反，交易所即识别为平仓（反向开仓量>持仓才会翻仓，这里已限 qty<=contracts）
        params["positionSide"] = "LONG" if side == "long" else "SHORT"
    else:
        params["reduceOnly"] = True
    order = ex.create_order(symbol, "market", close_side, qty, params=params)
    return {"mode": cfg.get("mode"), "markets": "usdt-m", "closed_side": side,
            "closed_qty": qty, "order": order}


# ---------------------------------------------------------------- 交易所托管止盈止损（条件单）

def _tp_sl_validate(side, mark, tp, sl):
    """校验止盈/止损触发价方向，返回错误列表（空=通过）。

    触发价方向反了（做多止损高于现价等）会一挂就触发，先拦下来。
    """
    errs = []
    if side == "long":
        if sl is not None and float(sl) >= mark:
            errs.append(f"做多止损价必须低于现价 {mark:,.2f}，当前 {sl}（否则一挂就触发）")
        if tp is not None and float(tp) <= mark:
            errs.append(f"做多止盈价必须高于现价 {mark:,.2f}，当前 {tp}（否则一挂就触发）")
    else:
        if sl is not None and float(sl) <= mark:
            errs.append(f"做空止损价必须高于现价 {mark:,.2f}，当前 {sl}（否则一挂就触发）")
        if tp is not None and float(tp) >= mark:
            errs.append(f"做空止盈价必须低于现价 {mark:,.2f}，当前 {tp}（否则一挂就触发）")
    return errs


def _place_protect(ex, symbol, side, ps, trigs):
    """挂止盈/止损条件单，返回 [{type, trigger, order_id}]。trigs=[("sl",价),("tp",价)]。

    closePosition=true 全平：交易所托管，bot 挂了/电脑关机/断网都生效；
    workingType=MARK_PRICE 按标记价触发（与 bot 盯盘用的 markPrice 一致）。
    """
    close_side = "sell" if side == "long" else "buy"
    placed = []
    for label, trig in trigs:
        if trig is None:
            continue
        otype = "STOP_MARKET" if label == "sl" else "TAKE_PROFIT_MARKET"
        params = {"stopPrice": float(trig), "closePosition": True,
                  "workingType": "MARK_PRICE", "timeInForce": "GTC"}
        if ps:
            params["positionSide"] = ps
        # closePosition=true 时数量被交易所忽略，传占位量即可
        order = ex.create_order(symbol, otype, close_side, 1.0, None, params=params)
        placed.append({"type": otype, "trigger": float(trig), "order_id": order["id"]})
    return placed


def _fetch_algo_orders(ex, symbol=None):
    """拉交易所 algo 条件单。

    STOP_MARKET/TAKE_PROFIT_MARKET 被币安强制走 /fapi/v1/algoOrder（普通单用 /order 会被
    -4120 拒），而 ccxt fetch_open_orders 只查 /fapi/v1/openOrders，看不到这些条件单，
    必须用 fapiPrivateGetOpenAlgoOrders 单独拉。
    """
    for _ in range(2):
        try:
            r = ex.fapiPrivateGetOpenAlgoOrders({})
        except Exception:
            return []
        items = r if isinstance(r, list) else (r.get("openAlgoOrders") or r.get("data") or [])
        if items or symbol is None:
            break
        time.sleep(0.6)  # 挂单后列表接口有 ~1s 传播延迟，空结果时重试一次
    if symbol:
        # 对比交易所 symbol id（如 BTCUSDT），不能用 ccxt 格式 BTC/USDT:USDT 直接 replace
        try:
            want = ex.market(symbol)["id"]
        except Exception:
            want = str(symbol).replace("/", "").split(":")[0]
        items = [it for it in items if str(it.get("symbol")) == want]
    return items or []


def _cancel_algo(ex, algo_id):
    """撤一张 algo 条件单，返回币安响应。"""
    return ex.fapiPrivateDeleteAlgoOrder({"algoId": algo_id})


def _cancel_all_algo(ex, symbol=None):
    """撤全部（或某币对）algo 条件单，返回撤掉数量。"""
    n = 0
    for it in _fetch_algo_orders(ex, symbol):
        aid = it.get("algoId")
        if aid is None:
            continue
        try:
            _cancel_algo(ex, aid)
            n += 1
        except Exception:
            pass
    return n


def cancel_order_ids(ex, symbol, ids):
    """撤一批已登记的单（保护单/入场单）。先按普通单撤，撤不到再按 algo 条件单撤。"""
    n = 0
    for oid in ids or []:
        try:
            ex.cancel_order(oid, symbol)
            n += 1
            continue
        except Exception:
            pass
        try:
            _cancel_algo(ex, oid)
            n += 1
        except Exception:
            pass
    return n


def cmd_tp_sl(cfg, ex, symbol, tp=None, sl=None, side=None):
    """把止盈/止损保护单挂到交易所（closePosition 全平，交易所托管）。

    方向 --side 显式指定，或从当前持仓自动取；触发价相对现价校验防止秒触。
    """
    if tp is None and sl is None:
        err("tp-sl 需要至少一个：--tp <止盈价> 和/或 --sl <止损价>")
    if not side:
        p = _live_position(ex, symbol) or {}
        side = p.get("side")
        if not side:
            err(f"{symbol} 当前无持仓，无法自动判断方向；可显式 --side long|short")
    if side not in ("long", "short"):
        err("--side 只支持 long 或 short")
    t = cmd_ticker(cfg, ex, symbol)
    mark = t.get("markPrice") or t.get("last")
    if not mark:
        err(f"取 {symbol} 现价失败，无法校验止盈/止损价")
    mark = float(mark)
    errs = _tp_sl_validate(side, mark, tp, sl)
    if errs:
        err("；".join(errs))
    futures_setup(ex, symbol, cfg, None)
    ps = _pos_side(cfg, "buy" if side == "long" else "sell")
    placed = _place_protect(ex, symbol, side, ps, (("sl", sl), ("tp", tp)))
    return {"mode": cfg.get("mode"), "markets": "usdt-m", "symbol": symbol,
            "side": side, "placed": placed,
            "note": "保护单已挂交易所（closePosition 全平、mark 价触发）；open 可看、cancel/cancel-all 可撤"}


def cmd_positions(cfg, ex):
    ps = ex.fetch_positions()
    live = [p for p in ps if float(p.get("contracts") or 0) > 0]
    return {"mode": cfg.get("mode"), "markets": "usdt-m",
            "positions": [
                {"symbol": p.get("symbol"), "side": p.get("side"),
                 "contracts": p.get("contracts"), "entryPrice": p.get("entryPrice"),
                 "liquidationPrice": p.get("liquidationPrice"), "markPrice": p.get("markPrice"),
                 "unrealizedPnl": p.get("unrealizedPnl"), "leverage": p.get("leverage"),
                 "marginMode": p.get("marginMode")}
                for p in live]}


# ---------------------------------------------------------------- 计划（自动盯盘）+ 撤单

def load_plans():
    if not os.path.exists(PLANS_PATH):
        return {"plans": []}
    try:
        with open(PLANS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"plans": []}


def save_plans(data):
    with open(PLANS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def cmd_plan_add(cfg, ex, symbol, side, entry_min, entry_max, lev=None, pct=None, qty=None, sl=None, tps=None):
    """登记一个自动盯盘计划（bot 每 ~30 秒查行情，价格进区间自动开仓）。"""
    if side not in ("long", "short"):
        err("方向只支持 long（做多）或 short（做空）")
    emin, emax = float(entry_min), float(entry_max)
    if not (0 < emin <= emax):
        err("入场区间非法：entry_min 应大于 0 且不大于 entry_max")
    f = futures_config(cfg)
    lev = int(lev) if lev else int(f.get("default_leverage", 10))
    max_lev = int(f.get("max_leverage", 25))
    if not 1 <= lev <= max_lev:
        err(f"杠杆必须在 1~{max_lev} 之间（config futures.max_leverage），当前 {lev}")
    if pct and qty:
        err("--pct 与 --qty 二选一")
    if not pct and not qty:
        err("需要 --pct 仓位百分比 或 --qty 币数量")
    if pct and not (0 < float(pct) <= 100):
        err("--pct 仓位百分比应在 1~100")
    plan = {
        "id": "p_" + time.strftime("%H%M%S"),
        "symbol": symbol,  # 已是规范化合约符号，如 BTC/USDT:USDT
        "side": side,
        "entry_min": emin, "entry_max": emax,
        "leverage": lev,
        "position_pct": float(pct) if pct else None,
        "qty": float(qty) if qty else None,
        "stop_loss": float(sl) if sl else None,
        "take_profits": [float(x) for x in tps] if tps else None,
        "status": "active",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "executed_at": None,
    }
    data = load_plans()
    for p in data["plans"]:
        if p.get("symbol") == symbol and p.get("status") == "active":
            err(f"{symbol} 已有进行中的计划（id={p.get('id')}），先 plan remove {symbol} 再添加")
    data["plans"].append(plan)
    save_plans(data)
    return {"plan": plan, "note": "bot 每 30 秒盯盘，价格进入区间自动执行；plan remove <symbol> 可移除"}


def cmd_plan_list(cfg):
    data = load_plans()
    active = [p for p in data["plans"] if p.get("status") == "active"]
    return {"plans": active}


def cmd_plan_remove(cfg, ex, symbol):
    sym = normalize_symbol(ex, symbol)
    data = load_plans()
    removed = 0
    cancelled_protect = 0
    for p in data["plans"]:
        if p.get("symbol") == sym and p.get("status") == "active":
            # 撤掉该计划的交易所保护单（止盈/止损条件单），不留孤儿单
            cancelled_protect += cancel_order_ids(ex, sym, p.get("protect_ids") or [])
            p["status"] = "cancelled"
            removed += 1
    save_plans(data)
    if not removed:
        err(f"{sym} 没有进行中的计划")
    return {"removed": removed, "symbol": sym, "cancelled_protect": cancelled_protect}


def cmd_cancel_all(cfg, ex, symbol=None):
    """撤掉全部挂单（可按币对）。paper 清账本挂单；testnet/live 走交易所。"""
    mode = cfg.get("mode", "paper")
    if mode == "paper":
        ledger = load_ledger(cfg)
        n = len(ledger["open"])
        ledger["open"] = []
        save_ledger(ledger)
        return {"mode": "paper", "cancelled": n}
    if symbol:
        sym = normalize_symbol(ex, symbol)
        orders = ex.fetch_open_orders(sym)
        for o in orders:
            ex.cancel_order(o["id"], sym)
        algo_n = _cancel_all_algo(ex, sym)
        return {"mode": mode, "markets": cfg.get("markets", "spot"),
                "cancelled": len(orders), "cancelled_algo": algo_n, "symbol": sym}
    orders = ex.fetch_open_orders()
    n = 0
    for o in orders:
        try:
            ex.cancel_order(o["id"], o.get("symbol") or o["symbol"])
            n += 1
        except Exception:
            pass
    algo_n = _cancel_all_algo(ex)
    return {"mode": mode, "markets": cfg.get("markets", "spot"),
            "cancelled": n, "cancelled_algo": algo_n}


# ---------------------------------------------------------------- 子命令

def cmd_account(cfg):
    mode = cfg.get("mode", "paper")
    markets = cfg.get("markets", "spot")
    b = cfg.get("binance", {})
    f = cfg.get("futures", {})
    return {
        "mode": mode,
        "markets": markets,
        "live_confirmed": cfg.get("live_confirmed", False),
        "exchange": "binance/" + ("spot" if markets == "spot" else "usdt-m"),
        "note": "spot=现货(paper/testnet/live) | usdt-m=U本位永续合约(testnet=测试网虚拟资金 / live=真实资金需双确认)",
        "has_spot_testnet_key": bool(b.get("testnet_api_key")),
        "has_live_key": bool(b.get("api_key")),
        "has_futures_testnet_key": bool(f.get("testnet_api_key")),
        "quote_currency": cfg.get("paper", {}).get("quote_currency", "USDT"),
        "futures": None if markets != "usdt-m" else {
            "default_leverage": f.get("default_leverage", 10),
            "max_leverage": f.get("max_leverage", 25),
            "margin_mode": f.get("margin_mode", "isolated"),
        },
    }


def cmd_balance(cfg, ex):
    mode = cfg.get("mode", "paper")
    markets = cfg.get("markets", "spot")
    if markets == "usdt-m":
        bal = ex.fetch_balance()
        # 用聚合字段 bal["total"]/bal["free"]，别遍历 bal.items()
        # （含 timestamp/info 等非币种键，值可能是 int/None，直接 .get 会崩）
        total = {k: v for k, v in (bal.get("total") or {}).items() if v}
        free = {k: v for k, v in (bal.get("free") or {}).items() if v}
        return {"mode": mode, "markets": "usdt-m", "balances": total, "available_margin": free}
    if mode == "paper":
        ledger = load_ledger(cfg)
        bal = {k: v for k, v in ledger["balances"].items() if v and abs(v) > 1e-12}
        return {"mode": "paper", "balances": bal}
    bal = ex.fetch_balance()
    total = {k: v for k, v in (bal.get("total") or {}).items() if v}
    return {"mode": mode, "balances": total}


def cmd_ticker(cfg, ex, symbol):
    markets = cfg.get("markets", "spot")
    t = ex.fetch_ticker(symbol)
    out = {"symbol": symbol, "last": t["last"], "bid": t["bid"], "ask": t["ask"],
           "high": t["high"], "low": t["low"], "volume": t["baseVolume"]}
    if markets == "usdt-m":
        # 期货 ticker 接口不带盘口/标记价/资金费率，单独补
        ob = ex.fetch_order_book(symbol, 5)
        out["bid"] = ob["bids"][0][0] if ob.get("bids") else None
        out["ask"] = ob["asks"][0][0] if ob.get("asks") else None
        try:
            fr = ex.fetch_funding_rate(symbol)
            out["markPrice"] = fr.get("markPrice")
            out["indexPrice"] = fr.get("indexPrice")
            out["fundingRate"] = fr.get("fundingRate")
        except Exception:
            pass
    return out


def cmd_position(cfg, ex, symbol):
    markets = cfg.get("markets", "spot")
    if markets == "usdt-m":
        p = _live_position(ex, symbol) or {}
        contracts = float(p.get("contracts") or 0)
        has = contracts > 0
        return {"symbol": symbol, "markets": "usdt-m",
                "side": p.get("side") or "none",
                "contracts": p.get("contracts") if has else 0,
                "entryPrice": p.get("entryPrice") if has else None,
                "liquidationPrice": p.get("liquidationPrice") if has else None,
                "markPrice": p.get("markPrice") if has else None,
                "unrealizedPnl": p.get("unrealizedPnl") if has else None,
                "leverage": p.get("leverage") if has else None,
                "marginMode": p.get("marginMode") if has else None}
    market = ex.market(symbol)
    base, quote = market["base"], market["quote"]
    t = ex.fetch_ticker(symbol)
    mode = cfg.get("mode", "paper")
    if mode == "paper":
        ledger = load_ledger(cfg)
        amt = ledger["balances"].get(base, 0.0)
    else:
        bal = ex.fetch_balance()
        amt = bal[base]["total"] if base in bal else 0.0
    return {"symbol": symbol, "mode": mode, "holding": _fmt_amt(ex, symbol, amt),
            "last_price": t["last"],
            "value_usdt": round(amt * t["last"], 6) if quote == "USDT" else None}


def cmd_open(cfg, ex):
    mode = cfg.get("mode", "paper")
    if mode == "paper":
        ledger = load_ledger(cfg)
        return {"mode": "paper", "open": ledger["open"]}
    regular = ex.fetch_open_orders()
    algo = _fetch_algo_orders(ex)
    return {"mode": mode, "markets": cfg.get("markets", "spot"),
            "open": regular,
            "algo_orders": algo,
            "note": "algo_orders=交易所条件单（止盈/止损，普通 openOrders 查不到，需单独查/单独撤）"}


def cmd_orders(cfg, ex, symbol):
    mode = cfg.get("mode", "paper")
    if mode == "paper":
        ledger = load_ledger(cfg)
        recs = ledger["orders"][-20:]
        return {"mode": "paper", "orders": recs}
    trades = ex.fetch_my_trades(symbol) if symbol else []
    return {"mode": mode, "orders": trades[-20:]}


def cmd_cancel(cfg, ex, order_id, symbol=None):
    mode = cfg.get("mode", "paper")
    if mode == "paper":
        ledger = load_ledger(cfg)
        before = len(ledger["open"])
        ledger["open"] = [o for o in ledger["open"] if str(o.get("id")) != str(order_id)]
        save_ledger(ledger)
        if len(ledger["open"]) == before:
            err(f"paper 未找到挂单 {order_id}")
        return {"mode": "paper", "cancelled": order_id, "remaining_open": len(ledger["open"])}
    if not symbol:
        err("testnet/live 撤单需要币对参数：python trade_cli.py cancel <ORDER_ID> <SYMBOL>")
    # 先按普通单撤；撤不到再按 algo 条件单撤（条件单在普通 openOrders 里不存在）
    try:
        return {"mode": mode, "cancelled": ex.cancel_order(order_id, symbol)}
    except Exception:
        pass
    try:
        r = _cancel_algo(ex, order_id)
        return {"mode": mode, "cancelled": r}
    except Exception as e:
        err(f"撤单失败（普通+algo 都试过）：{type(e).__name__}: {str(e)[:150]}")


def cmd_markets(ex, symbol):
    m = ex.market(symbol)
    return {
        "symbol": symbol,
        "base": m["base"], "quote": m["quote"],
        "amount_precision": m.get("precision", {}).get("amount"),
        "price_precision": m.get("precision", {}).get("price"),
        "limits": m.get("limits"),
    }


# ---------------------------------------------------------------- 主入口

def normalize_symbol(ex, sym):
    sym = sym.upper().strip()
    try:
        # binanceusdm 下 "BTC/USDT" 会自动解析为合约 "BTC/USDT:USDT"
        return ex.market(sym)["symbol"]
    except Exception:
        # 可能是缓存过旧（新上市币对不在表里）：重拉一次公开市场表再试
        tag = getattr(ex, "_markets_tag", "spot")
        if tag not in _MARKETS_REFRESHED:
            _MARKETS_REFRESHED.add(tag)
            try:
                ex.load_markets()
                _save_markets_cache(ex, tag)
                return ex.market(sym)["symbol"]
            except Exception:
                pass
        err(f"币对 {sym} 不存在。现货示例：BTC/USDT、ETH/USDT、BNB/USDT；合约（usdt-m）示例：BTC/USDT、ETH/USDT")


# ---------------------------------------------------------------- 账户操作审计

OPS_LOG_PATH = os.path.join(BASE_DIR, "logs", "operations.log")

# 不写审计的命令：纯公开行情。盯盘轮询会高频调 ticker，写日志只会刷屏。
_NO_OPLOG = {"ticker", "markets"}


def _secrets(cfg):
    out = set()
    for key in ("api_key", "secret", "testnet_api_key", "testnet_secret"):
        v = cfg.get("binance", {}).get(key)
        if v and len(v) >= 8:
            out.add(v)
    for key in ("testnet_api_key", "testnet_secret"):
        v = cfg.get("futures", {}).get(key)
        if v and len(v) >= 8:
            out.add(v)
    v = cfg.get("telegram", {}).get("bot_token")
    if v and len(v) >= 8:
        out.add(v)
    return [s for s in out if s]


def _oplog(cfg, cmd, args, result=None, status="ok"):
    """把一次账户操作追加写进 logs/operations.log（JSON 一行一条），供桌面日志图标查看。"""
    if not cfg or cmd in _NO_OPLOG:
        return
    get = (lambda k, d=None: getattr(args, k, d)) if args is not None else (lambda k, d=None: d)
    fields = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "op": f"{cmd}:{get('plan_cmd')}" if cmd == "plan" and get("plan_cmd") else cmd,
        "mode": cfg.get("mode"),
        "markets": cfg.get("markets", "spot"),
        "status": status,
    }
    result = result or {}
    if get("symbol"):
        fields["symbol"] = get("symbol")
    for k in ("amount", "price", "lev", "pct", "qty", "sl", "tp", "order_id", "entry_min", "entry_max"):
        if get(k) is not None:
            fields[k] = get(k)
    if get("entry_min") is not None:
        fields["entry_range"] = f"{get('entry_min')}~{get('entry_max')}"
    order = result.get("order") or {}
    info = order.get("info") or {}
    oid = result.get("order_id") or order.get("id") or info.get("orderId")
    if oid is not None:
        fields["order_id"] = oid
    for k in ("side", "type", "price", "amount", "filled"):
        v = result.get(k)
        if v is None:
            v = order.get(k)
        if v is None:
            v = info.get(k)
        if v is not None:
            fields[k] = v
    if result.get("leverage"):
        fields["leverage"] = result["leverage"]
    if result.get("cancelled") is not None:
        fields["cancelled"] = result["cancelled"]
    if result.get("removed") is not None:
        fields["removed"] = result["removed"]
    if result.get("closed_side"):
        fields["closed_side"] = result["closed_side"]
    if result.get("closed_qty") is not None:
        fields["closed_qty"] = result["closed_qty"]
    if result.get("plan"):
        p = result["plan"]
        fields.update({
            "plan_id": p.get("id"),
            "plan_symbol": p.get("symbol"),
            "plan_side": p.get("side"),
            "plan_entry": f"{p.get('entry_min')}~{p.get('entry_max')}",
        })
    if "error" in result:
        fields["error"] = str(result["error"])[:300]
    line = json.dumps(fields, ensure_ascii=False, default=str)
    for s in _secrets(cfg):
        if s and s in line:
            line = line.replace(s, "***")
    try:
        os.makedirs(os.path.dirname(OPS_LOG_PATH), exist_ok=True)
        with open(OPS_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def dispatch(args, cfg):
    """按子命令执行，返回结果 dict。业务报错经 err() 抛 SystemExit 由调用方记录。"""
    if args.cmd == "account":
        return cmd_account(cfg)

    ex = make_exchange(cfg)
    sym = normalize_symbol(ex, args.symbol) if hasattr(args, "symbol") and args.symbol else None

    if args.cmd == "balance":
        return cmd_balance(cfg, ex)
    if args.cmd == "ticker":
        return cmd_ticker(cfg, ex, sym)
    if args.cmd in ("buy", "sell"):
        mode = require_order_allowed(cfg)
        if cfg.get("markets", "spot") == "usdt-m":
            side = "buy" if args.cmd == "buy" else "sell"
            if not args.amount and args.quote is None:
                err("合约 buy/sell 需要币数量（如 buy BTC/USDT 0.001 --lev 10）或 --quote <USDT金额>")
            return futures_execute(cfg, ex, sym, side, amount=args.amount, quote_amount=args.quote, lev=args.lev)
        if args.cmd == "buy":
            if args.quote is not None:
                result = (paper_execute(cfg, ex, sym, "buy", quote_amount=args.quote)
                          if mode == "paper" else {"order": real_execute(ex, sym, "buy", quote_amount=args.quote)})
            else:
                if not args.amount:
                    err("buy 需要数量参数：python trade_cli.py buy BTC/USDT 0.001 或 --quote 50")
                result = (paper_execute(cfg, ex, sym, "buy", amount=args.amount)
                          if mode == "paper" else {"order": real_execute(ex, sym, "buy", amount=args.amount)})
            return result
        if not args.amount:
            err("sell 需要数量参数：python trade_cli.py sell BTC/USDT 0.001")
        return (paper_execute(cfg, ex, sym, "sell", amount=args.amount)
                if mode == "paper" else {"order": real_execute(ex, sym, "sell", amount=args.amount)})
    if args.cmd in ("limit-buy", "limit-sell"):
        mode = require_order_allowed(cfg)
        side = "buy" if args.cmd == "limit-buy" else "sell"
        if cfg.get("markets", "spot") == "usdt-m":
            # 合约限价单：挂到交易所，价格到了才成交（open 查看、cancel 撤单）
            lev = futures_setup(ex, sym, cfg, getattr(args, "lev", None))
            min_n = float(futures_config(cfg).get("min_notional", 5.0))
            if float(args.amount) * float(args.price) < min_n:
                err(f"限价单名义过小：{sym} 合约最小约 {min_n:.0f} USDT")
            order_params = {}
            ps = _pos_side(cfg, side)
            if ps:
                order_params["positionSide"] = ps
            order = ex.create_order(sym, "limit", side, args.amount, args.price, params=order_params)
            return {"mode": mode, "markets": "usdt-m", "side": side, "type": "limit",
                    "order": order, "leverage": lev,
                    "note": "限价单已挂盘，价格到了才成交；用 open 查看、cancel 撤单"}
        if mode == "paper":
            return paper_execute(cfg, ex, sym, side, amount=args.amount, price=args.price, otype="limit")
        return {"order": real_execute(ex, sym, side, amount=args.amount, price=args.price, otype="limit")}
    if args.cmd == "leverage":
        _require_usdm(cfg)
        return cmd_leverage(cfg, ex, sym, args.lev)
    if args.cmd == "margin-mode":
        _require_usdm(cfg)
        return cmd_margin_mode(cfg, ex, sym, args.mode)
    if args.cmd == "close":
        _require_usdm(cfg)
        return cmd_close(cfg, ex, sym, args.qty)
    if args.cmd == "tp-sl":
        _require_usdm(cfg)
        return cmd_tp_sl(cfg, ex, sym, tp=args.tp, sl=args.sl, side=args.side)
    if args.cmd == "positions":
        _require_usdm(cfg)
        return cmd_positions(cfg, ex)
    if args.cmd == "position":
        return cmd_position(cfg, ex, sym)
    if args.cmd == "open":
        return cmd_open(cfg, ex)
    if args.cmd == "orders":
        return cmd_orders(cfg, ex, sym)
    if args.cmd == "cancel":
        return cmd_cancel(cfg, ex, args.order_id, args.symbol)
    if args.cmd == "cancel-all":
        return cmd_cancel_all(cfg, ex, args.symbol)
    if args.cmd == "plan":
        _require_usdm(cfg)
        if args.plan_cmd == "add":
            tps = [x.strip() for x in args.tp.split(",") if x.strip()] if args.tp else None
            return cmd_plan_add(cfg, ex, sym, args.side, args.entry_min, args.entry_max,
                                lev=args.lev, pct=args.pct, qty=args.qty, sl=args.sl, tps=tps)
        if args.plan_cmd == "list":
            return cmd_plan_list(cfg)
        if args.plan_cmd == "remove":
            return cmd_plan_remove(cfg, ex, args.symbol)
    if args.cmd == "markets":
        return cmd_markets(ex, sym)
    err("未知子命令")


def main():
    p = argparse.ArgumentParser(prog="trade_cli", description="ccxt 币安交易 CLI（paper/testnet/live）")
    p.add_argument("--config", help="配置文件路径（默认 config.json）；测试/切换模式用，如 --config config.testnet.json")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("account")
    sub.add_parser("balance")
    sub.add_parser("open")

    t = sub.add_parser("ticker"); t.add_argument("symbol")
    b = sub.add_parser("buy"); b.add_argument("symbol"); b.add_argument("amount", nargs="?"); b.add_argument("--quote", type=float); b.add_argument("--lev", type=int)
    s = sub.add_parser("sell"); s.add_argument("symbol"); s.add_argument("amount", nargs="?"); s.add_argument("--quote", type=float); s.add_argument("--lev", type=int)
    lb = sub.add_parser("limit-buy"); lb.add_argument("symbol"); lb.add_argument("amount"); lb.add_argument("price", type=float); lb.add_argument("--lev", type=int)
    ls = sub.add_parser("limit-sell"); ls.add_argument("symbol"); ls.add_argument("amount"); ls.add_argument("price", type=float); ls.add_argument("--lev", type=int)
    po = sub.add_parser("position"); po.add_argument("symbol")
    ps_ = sub.add_parser("positions")
    cl = sub.add_parser("close"); cl.add_argument("symbol"); cl.add_argument("qty", nargs="?")
    tpsl = sub.add_parser("tp-sl", help="把止盈/止损保护单挂到交易所（closePosition 全平，交易所托管）")
    tpsl.add_argument("symbol")
    tpsl.add_argument("--tp", type=float, help="止盈触发价")
    tpsl.add_argument("--sl", type=float, help="止损触发价")
    tpsl.add_argument("--side", choices=["long", "short"], help="方向（默认从当前持仓自动取）")
    lv = sub.add_parser("leverage"); lv.add_argument("symbol"); lv.add_argument("lev", type=int)
    mm_ = sub.add_parser("margin-mode"); mm_.add_argument("symbol"); mm_.add_argument("mode")
    od = sub.add_parser("orders"); od.add_argument("symbol", nargs="?")
    c = sub.add_parser("cancel"); c.add_argument("order_id"); c.add_argument("symbol", nargs="?")
    ca = sub.add_parser("cancel-all"); ca.add_argument("symbol", nargs="?")
    pa = sub.add_parser("plan")
    pa_sub = pa.add_subparsers(dest="plan_cmd", required=True)
    pa_add = pa_sub.add_parser("add")
    pa_add.add_argument("symbol"); pa_add.add_argument("side"); pa_add.add_argument("entry_min", type=float); pa_add.add_argument("entry_max", type=float)
    pa_add.add_argument("--lev", type=int); pa_add.add_argument("--pct", type=float); pa_add.add_argument("--qty", type=float); pa_add.add_argument("--sl", type=float); pa_add.add_argument("--tp", type=str)
    pa_sub.add_parser("list")
    pa_rm = pa_sub.add_parser("remove"); pa_rm.add_argument("symbol")
    m = sub.add_parser("markets"); m.add_argument("symbol")

    args = p.parse_args()

    try:
        cfg = load_config(args.config)
    except SystemExit:
        raise
    except Exception as e:
        out({"error": f"{type(e).__name__}: {e}"})
        return

    _OP.update({"cfg": cfg, "cmd": args.cmd, "args": args})
    try:
        result = dispatch(args, cfg)
    except SystemExit:
        raise  # err() 已把业务报错写入操作日志
    except Exception as e:
        result = {"error": f"{type(e).__name__}: {e}"}
        _oplog(cfg, args.cmd, args, result, status="error")
        out(result)
        return
    _oplog(cfg, args.cmd, args, result)
    out(result)


if __name__ == "__main__":
    main()
