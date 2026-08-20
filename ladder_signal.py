#!/usr/bin/env python
"""
ladder_signal —— 固定格式信号 → 50 档等差阶梯挂单 + 自动止盈止损。

把这种固定格式信号(Telegram/文本)改写并执行为一套完整动作:

  > SNDK
    方向：做多
    入场：1510-1525附近
    倍数：5倍
    仓位：10%
    止盈：点位1：1580附近（求稳） 点位2：1625附近 点位3：1700附近（求稳）
    止损：小幅跌破1485一点。
    理由：...
    注：挂单不要挂整数位...

执行语义(转换结果):
  1. 入场区间 [1510, 1525] → 挂 N 张等差(等间隔)分段限价单。
     做多挂买单、做空挂卖单;价位取每个小格的中点,避开整数位、
     天然落在支撑上方一点/阻力下方一点(满足信号"注"的要求)。
  2. 止盈点位1(1580)→ 市价到该价:市价全平 + 撤销全部剩余挂单。
  3. 止损位(1485)→ 市价到该价:市价全平。
  4. 点位2/3 仅作参考保留(实际在点位1全平)。

挂单后写一条 type="ladder" 计划进 plans.json,由 bot.py 的 30 秒轮询
盯盘执行第 2/3 步(依赖 bot 在跑 + 电脑开机)。

用法(输出 JSON,含重写后的中文指令):
  python ladder_signal.py parse  <信号文件> [--orders N] [--config PATH]
  python ladder_signal.py place  <信号文件> [--orders N] [--config PATH]
  python ladder_signal.py list
  python ladder_signal.py remove <计划id|币对>

安全:复用 trade_cli 的闸门——live 必须 mode=live + live_confirmed=true 才放行;
测试可用 --config config.testnet.json(mode=testnet)不碰真钱。
"""

import argparse
import json
import os
import re
import sys
import time
from types import SimpleNamespace

from trade_cli import (
    cancel_order_ids,
    cmd_ticker,
    futures_config,
    futures_setup,
    load_config,
    load_plans,
    make_exchange,
    normalize_symbol,
    _live_position,
    _oplog,
    _place_protect,
    _pos_side,
    _tp_sl_validate,
    require_order_allowed,
    save_plans,
)

# 强制 UTF-8,避免 Windows 控制台按 GBK 编码导致读串行
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ORDERS = 50
MIN_ORDERS = 2
# 档间间隔,避免 50 张连下被币安限速
ORDER_PACE_S = 0.08


def out(data):
    print(json.dumps(data, ensure_ascii=False, default=str))


def err(msg):
    out({"error": msg})
    sys.exit(2)


# ---------------------------------------------------------------- 固定格式解析

def parse_signal(text):
    """解析固定格式信号文本 → dict。字段取不到就留 None,由调用方校验。"""
    lines = text.splitlines()

    def field(key):
        for line in lines:
            m = re.match(r"\s*" + key + r"\s*[:：]\s*(.*)", line)
            if m:
                return m.group(1).strip()
        return ""

    def first_num(s):
        m = re.search(r"\d+(?:\.\d+)?", s or "")
        return float(m.group(0)) if m else None

    # 符号:首行 > SNDK（也兼容没有 > 前缀的裸符号首行，与 bot.py _is_ladder_signal 判定一致）
    sym_line = next((l.strip() for l in lines if l.strip().startswith(">")), "")
    if sym_line:
        symbol = sym_line.lstrip(">").strip().split()[0]
    else:
        symbol = next((l.strip() for l in lines
                       if l.strip() and re.match(r"^[A-Za-z0-9\-]{1,20}$", l.strip())), "")

    # 方向:做多/多/long → long;做空/空/short → short
    d = field("方向")
    if "做空" in d or "空" in d:
        side = "short"
    elif "做多" in d or "多" in d or "long" in d.lower():
        side = "long"
    else:
        side = None

    # 入场:1510-1525附近 / 1510~1525 / 1510—1525
    entry = field("入场")
    m = re.search(r"(\d+(?:\.\d+)?)\s*[-~—－]\s*(\d+(?:\.\d+)?)", entry)
    entry_min = float(m.group(1)) if m else None
    entry_max = float(m.group(2)) if m else None

    # 止盈:收集所有 点位N:价格,点位1 作为平仓触发价
    tp_line = field("止盈")
    tps = [(int(n), float(p)) for n, p in
           re.findall(r"点位\s*(\d+)\s*[:：]\s*(\d+(?:\.\d+)?)", tp_line)]
    tps.sort(key=lambda x: x[0])

    return {
        "symbol": symbol,
        "side": side,
        "entry_min": entry_min,
        "entry_max": entry_max,
        "leverage": first_num(field("倍数")),
        "position_pct": first_num(field("仓位")),
        "take_profit": tps[0][1] if tps else None,
        "take_profits": [{"point": n, "price": p} for n, p in tps],
        "stop_loss": first_num(field("止损")),
        "confidence": field("信心度"),
        "reason": field("理由"),
        "note": field("注"),
    }


def validate_signal(sig):
    """校验解析结果,缺关键字段直接报错。"""
    if not sig["symbol"]:
        err("信号缺币对(首行应为 `> SNDK` 形式)")
    if sig["side"] not in ("long", "short"):
        err("信号方向无法识别(应为 做多/做空)")
    if not (sig["entry_min"] and sig["entry_max"]):
        err("信号缺入场区间(如 `入场:1510-1525附近`)")
    if not (0 < sig["entry_min"] <= sig["entry_max"]):
        err(f"入场区间非法:entry_min={sig['entry_min']} entry_max={sig['entry_max']}")
    if not sig["leverage"]:
        err("信号缺倍数(如 `倍数:5倍`)")
    if not sig["position_pct"]:
        err("信号缺仓位(如 `仓位:10%`)")
    if not (0 < sig["position_pct"] <= 100):
        err(f"仓位百分比应在 1~100,当前 {sig['position_pct']}")
    if not sig["take_profit"]:
        err("信号缺止盈点位1(如 `止盈:点位1:1580附近`)")
    if not sig["stop_loss"]:
        err("信号缺止损(如 `止损:小幅跌破1485一点`)")
    # 止盈/止损与方向是否自洽由 bot 触发判断兜底,这里不强求


# ---------------------------------------------------------------- 算档

def ladder_prices(ex, symbol, lo, hi, n):
    """在 [lo, hi] 内生成 n 档等差价格(取每个格子中点,避开整数位)。

    中点法:价格 = lo + step*(i+0.5),天然落在支撑上方一点/阻力下方一点,
    端点不会是整数;再按币对 tick 取整,若仍是整数(且 tick<1)偏 half-tick。
    精度太粗导致合并时,返回去重后的实际档数(可能 < n)。
    """
    market = ex.market(symbol)
    prec = market.get("precision", {}).get("price")
    tick = float(prec) if isinstance(prec, (int, float)) else None
    step = (hi - lo) / n
    prices = []
    for i in range(n):
        p = lo + step * (i + 0.5)
        try:
            p = float(ex.price_to_precision(symbol, p))
        except Exception:
            p = round(p, 8)
        if tick and tick < 1 and abs(p - round(p)) < 1e-9:
            # 命中整数位,偏半个 tick 避开
            p = float(ex.price_to_precision(symbol, p + tick / 2.0))
        prices.append(p)
    # 精度过粗时相邻价会合并,去重
    uniq = []
    for p in prices:
        if not uniq or abs(p - uniq[-1]) > 1e-9:
            uniq.append(p)
    return uniq


def _avail_usdt(ex, cfg):
    """合约账户可用保证金(USDT/USDC 优先)。失败返回 None。"""
    try:
        bal = ex.fetch_balance()
        free = bal.get("free") or {}
        for c in ("USDT", "USDC"):
            if free.get(c):
                return float(free[c])
    except Exception:
        return None
    return None


def size_orders(ex, cfg, sym, sig, lo, hi, requested):
    """按 仓位% × 可用保证金 × 杠杆 算总名义,平摊到每档。

    返回 (prices, qtys, sizing)。每档名义若低于交易所最小 min_notional,
    自动减档到最大可行数(仍 >= MIN_ORDERS),不再硬报错——否则 50 档在小账户上永远挂不出。
    """
    f = futures_config(cfg)
    lev = int(sig["leverage"])
    max_lev = int(f.get("max_leverage", 25))
    if not 1 <= lev <= max_lev:
        err(f"杠杆必须在 1~{max_lev} 之间(config futures.max_leverage),当前 {lev}")

    avail = _avail_usdt(ex, cfg)
    if avail is None:
        err("取可用保证金失败(网络/权限?),无法按仓位% 算单量。"
            "确认 config 的 mode 与 key 正确后再试。")
    pct = float(sig["position_pct"])
    notional_total = max(avail * pct / 100.0 * lev, 0.0)
    min_n = float(f.get("min_notional", 5.0))
    # 币对最小下单量(枚),乘价格 = 每档最小名义(USDT)。如 SNDK 最小 0.01 枚 ≈ 15 USDT,比 min_notional 还高
    market = ex.market(sym)
    amt_limits = market.get("limits", {}).get("amount") or {}
    lot_min = float(amt_limits.get("min")
                    or market.get("precision", {}).get("amount") or 0.01)
    # 保守用区间上沿价算:保证任一一档数量 >= 最小下单量
    eff_min = max(min_n, lot_min * hi)

    # 减档:每档名义 >= eff_min
    max_orders = int(notional_total // eff_min) if notional_total >= eff_min else 0
    n = min(requested, max_orders) if max_orders >= MIN_ORDERS else 0
    adjusted = 0 < n < requested
    if n < MIN_ORDERS:
        err(f"可用保证金 {avail:.2f} × 仓位 {pct:.0f}% × 杠杆 {lev} 只能支撑每档 ≥ {eff_min:.2f} USDT 时最多 {max_orders} 档,低于最低 {MIN_ORDERS} 档。请加大仓位或保证金。")

    prices = ladder_prices(ex, sym, lo, hi, n)
    per_order = notional_total / len(prices)
    qtys = []
    for p in prices:
        q = float(ex.amount_to_precision(sym, per_order / p))
        if q <= 0:
            err(f"档位 {p} 换算数量为 0:每档名义 {per_order:.2f} USDT 低于币对最小下单量。")
        qtys.append(q)
    actual_total = sum(q * p for q, p in zip(qtys, prices))
    sizing = {
        "leverage": lev,
        "avail_margin": avail,
        "notional_total_target": round(notional_total, 2),
        "notional_total_actual": round(actual_total, 2),
        "per_order_notional": round(per_order, 2),
        "orders_requested": requested,
        "orders_adjusted": adjusted,
        "qtys": qtys,
    }
    return prices, qtys, sizing


# ---------------------------------------------------------------- 挂单与登记

def place_ladder_orders(ex, cfg, sym, side, lev, prices, qtys):
    """逐档挂限价单,返回 (order_ids, failures)。设置杠杆/保证金一次。"""
    futures_setup(ex, sym, cfg, lev)
    # 注意:_pos_side 期望 buy/sell(做多=LONG、做空=SHORT),别传 long/short
    order_side = "buy" if side == "long" else "sell"
    ps = _pos_side(cfg, order_side)  # hedge 模式才非 None
    order_ids, failures = [], []
    for p, q in zip(prices, qtys):
        params = {"timeInForce": "GTC"}
        if ps:
            params["positionSide"] = ps
        try:
            o = ex.create_order(sym, "limit", "buy" if side == "long" else "sell",
                                q, p, params=params)
            order_ids.append(o["id"])
        except Exception as e:
            failures.append({"price": p, "qty": q,
                             "error": f"{type(e).__name__}: {str(e)[:200]}"})
        time.sleep(ORDER_PACE_S)
    return order_ids, failures


def register_ladder_plan(cfg, sym, sig, sizing, prices, qtys, order_ids):
    data = load_plans()
    for p in data["plans"]:
        if p.get("type") == "ladder" and p.get("symbol") == sym and p.get("status") == "active":
            err(f"{sym} 已有进行中的 Ladder 计划(id={p.get('id')}),"
                f"先 `ladder_signal.py remove {p.get('id')}` 再添加")
    plan = {
        "id": "l_" + time.strftime("%H%M%S"),
        "type": "ladder",
        "symbol": sym,
        "side": sig["side"],
        "entry_min": sig["entry_min"],
        "entry_max": sig["entry_max"],
        "leverage": sizing["leverage"],
        "position_pct": sig["position_pct"],
        "orders": len(prices),
        "take_profit": sig["take_profit"],
        "stop_loss": sig["stop_loss"],
        "entry_prices": prices,
        "entry_qtys": qtys,
        "notional_total": sizing["notional_total_actual"],
        "order_ids": order_ids,
        "status": "active",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "closed_at": None,
        "close_reason": None,
        "signal": {
            "confidence": sig["confidence"],
            "reason": sig["reason"],
            "note": sig["note"],
            "take_profits": sig["take_profits"],
        },
    }
    data["plans"].append(plan)
    save_plans(data)
    return plan


def place_protect_orders(cfg, ex, sym, sig, plan):
    """把 ladder 的止盈点位1/止损挂成交易所条件单（closePosition 全平，mark 价触发）。

    closePosition 条件单必须已有持仓才能挂（币安 -4509），ladder 挂单时通常还没成交，
    所以这里只在已有持仓时挂；没持仓就提示由 bot 在首笔成交后自动挂。
    返回 (placed 列表, warning 或 None)；成功后把 order_id 记进计划的 protect_ids。
    """
    p = _live_position(ex, sym)
    if p is None or float(p.get("contracts") or 0) <= 0:
        return [], "尚无持仓，保护单将由 bot 在首笔成交后自动挂出"
    try:
        t = cmd_ticker(cfg, ex, sym)
        mark = float(t.get("markPrice") or t.get("last"))
    except Exception:
        return [], "取现价失败，未挂保护单"
    errs = _tp_sl_validate(sig["side"], mark, sig["take_profit"], sig["stop_loss"])
    if errs:
        return [], "；".join(errs)
    ps = _pos_side(cfg, "buy" if sig["side"] == "long" else "sell")
    try:
        futures_setup(ex, sym, cfg, int(sig["leverage"]))
        placed = _place_protect(ex, sym, sig["side"], ps,
                                (("sl", sig["stop_loss"]), ("tp", sig["take_profit"])))
    except Exception as e:
        return [], f"{type(e).__name__}: {str(e)[:200]}"
    data = load_plans()
    for p in data["plans"]:
        if p.get("id") == plan["id"]:
            p["protect_ids"] = [o["order_id"] for o in placed]
            p["protect"] = placed
    save_plans(data)
    return placed, None


# ---------------------------------------------------------------- 重写指令文本

def rewrite_text(sig, sym, prices, qtys, sizing, order_ids=None, plan_id=None):
    """把原始信号"改写"成具体可执行的中文指令(转换结果,供人/Claude 阅读)。"""
    side_txt = "做多" if sig["side"] == "long" else "做空"
    buy_sell = "买" if sig["side"] == "long" else "卖"
    n = len(prices)
    lines = [
        f"> {sig['symbol']}  {side_txt}  {sig['leverage']}x  仓位 {sig['position_pct']:.0f}%",
        f"入场区间 [{sig['entry_min']}, {sig['entry_max']}] → 挂 {n} 张等差限价{buy_sell}单",
        f"  每档名义约 {sizing['per_order_notional']:.2f} USDT,总量约 {sizing['notional_total_actual']:.2f} USDT",
    ]
    if sizing.get("orders_adjusted"):
        lines.append(f"  ⚠️ 每档低于交易所最小单量,已自动从 {sizing['orders_requested']} 档减到 {n} 档")
    # 每行 4 档,紧凑列出全部挂单价与数量
    cells = [f"{p:.2f}×{q:g}" for p, q in zip(prices, qtys)]
    for i in range(0, len(cells), 4):
        lines.append("  " + "  ".join(cells[i:i + 4]))
    lines.append(f"止盈点位1 {sig['take_profit']:.0f} → 市价全平 + 撤销全部剩余挂单")
    lines.append(f"止损 {sig['stop_loss']:.0f} → 市价全平")
    if order_ids:
        lines.append(f"已挂 {len(order_ids)} 张单,计划 {plan_id}(bot 每 30 秒盯盘)")
    elif sig["take_profits"]:
        extra = " / ".join(f"点位{d['point']} {d['price']:.0f}" for d in sig["take_profits"])
        lines.append(f"参考止盈:{extra}(点位1触发即全平,其余不启用)")
    return "\n".join(lines)


# ---------------------------------------------------------------- 子命令

def _load_cfg(args):
    path = getattr(args, "config", None)
    if path:
        if not os.path.exists(path):
            err(f"找不到配置 {path}")
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return load_config()


def _require_usdm(cfg):
    if cfg.get("markets", "spot") != "usdt-m":
        err("ladder 仅支持 U 本位永续合约(markets=usdt-m)。"
            "当前市场不是 usdt-m;测试可用 --config config.testnet.json")


def _read_signal_file(path):
    if not os.path.exists(path):
        err(f"找不到信号文件 {path}")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _build_sym(cfg, sig_symbol):
    ex = make_exchange(cfg)
    if "/" not in sig_symbol:
        sig_symbol = sig_symbol.upper() + "/USDT"  # 信号里通常是裸符号: SNDK
    sym = normalize_symbol(ex, sig_symbol)
    return ex, sym


def cmd_parse(cfg, args):
    _require_usdm(cfg)
    sig = parse_signal(_read_signal_file(args.file))
    validate_signal(sig)
    ex, sym = _build_sym(cfg, sig["symbol"])
    try:
        prices, qtys, sizing = size_orders(
            ex, cfg, sym, sig, sig["entry_min"], sig["entry_max"], args.orders)
    except SystemExit:
        raise
    except Exception as e:
        err(f"按仓位% 算单量失败: {type(e).__name__}: {e}")
    result = {
        "cmd": "parse",
        "mode": cfg.get("mode"),
        "markets": cfg.get("markets"),
        "symbol": sym,
        "side": sig["side"],
        "orders": len(prices),
        "entry_prices": prices,
        "entry_qtys": qtys,
        "sizing": {k: v for k, v in sizing.items() if k != "qtys"},
        "rewritten": rewrite_text(sig, sym, prices, qtys, sizing),
        "signal": sig,
        "note": "parse 只预览不下单;确认无误后用 place 实际挂单。",
    }
    out(result)


def cmd_place(cfg, args):
    mode = require_order_allowed(cfg)  # live 双闸照常
    _require_usdm(cfg)
    sig = parse_signal(_read_signal_file(args.file))
    validate_signal(sig)
    ex, sym = _build_sym(cfg, sig["symbol"])
    prices, qtys, sizing = size_orders(
        ex, cfg, sym, sig, sig["entry_min"], sig["entry_max"], args.orders)
    order_ids, failures = place_ladder_orders(
        ex, cfg, sym, sig["side"], sizing["leverage"], prices, qtys)

    if not order_ids:
        # 全失败也留审计,否则下回连错误都查不到
        _oplog(cfg, "ladder", _fail_ns(sig, sym),
               {"error": json.dumps(failures, ensure_ascii=False)[:300]}, status="error")
        err("全部挂单失败,未登记计划:" + json.dumps(failures, ensure_ascii=False))

    plan = register_ladder_plan(cfg, sym, sig, sizing, prices, qtys, order_ids)
    protect_placed, protect_warn = place_protect_orders(cfg, ex, sym, sig, plan)
    protect_txt = ""
    if protect_placed:
        protect_txt = "；".join(f"{o['type']}@{o['trigger']:,.0f}" for o in protect_placed)
    result = {
        "cmd": "place",
        "mode": mode,
        "markets": cfg.get("markets"),
        "symbol": sym,
        "side": sig["side"],
        "orders": len(prices),
        "placed": len(order_ids),
        "failed": failures,
        "plan": {k: v for k, v in plan.items() if k not in ("entry_prices", "entry_qtys", "protect")},
        "order_ids": order_ids,
        "protect": protect_placed,
        "protect_warning": protect_warn,
        "rewritten": rewrite_text(sig, sym, prices, qtys, sizing,
                                  order_ids=order_ids, plan_id=plan["id"]),
        "note": (f"止盈/止损保护单已挂交易所（{protect_txt}），bot 挂了/关机也在；"
                 f"bot 每 ~30 秒仍盯盘兜底，到点位1/止损市价全平并撤剩余挂单。"
                 if protect_placed else
                 f"保护单：{protect_warn or '未知原因'}（closePosition 需已有持仓，"
                 f"首笔成交后 bot 会自动挂上）。bot 每 ~30 秒盯盘兜底："
                 f"到止盈点位1 {sig['take_profit']:.0f} 市价全平并撤剩余挂单，"
                 f"到止损 {sig['stop_loss']:.0f} 市价全平。"),
    }
    return result, order_ids, plan


def cmd_list(cfg):
    _require_usdm(cfg)
    data = load_plans()
    ladders = [p for p in data["plans"] if p.get("type") == "ladder"]
    out({"cmd": "list", "ladder_plans": ladders})


def cmd_remove(cfg, args):
    _require_usdm(cfg)
    target = (args.target or "").strip()
    if not target:
        err("remove 需要计划 id 或币对,如: ladder_signal.py remove l_123456 或 ladder_signal.py remove SNDK/USDT")
    data = load_plans()
    plan = None
    for p in data["plans"]:
        if p.get("type") != "ladder":
            continue
        if p["id"] == target or p["symbol"] == target or \
                target.upper().replace("/USDT:USDT", "").replace("/USDT", "") in p["symbol"]:
            plan = p
            break
    if plan is None:
        err(f"没有找到匹配的 Ladder 计划: {target}(可用 ladder_signal.py list 查看)")
    sym = plan["symbol"]
    ex = make_exchange(cfg)
    cancelled = 0
    for oid in plan.get("order_ids", []):
        try:
            ex.cancel_order(oid, sym)
            cancelled += 1
        except Exception:
            pass  # 已成交/已撤的忽略
    # 一并撤掉交易所保护单（止盈/止损条件单）
    cancelled += cancel_order_ids(ex, sym, plan.get("protect_ids") or [])
    plan["status"] = "cancelled"
    plan["closed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    plan["close_reason"] = "removed"
    save_plans(data)
    result = {"cmd": "remove", "plan_id": plan["id"], "symbol": sym,
              "cancelled_orders": cancelled,
              "note": "入场挂单+保护单已撤销,计划已移除。已有持仓需自行 close 处理。"}
    return result


# ---------------------------------------------------------------- 主入口

def main():
    p = argparse.ArgumentParser(prog="ladder_signal",
                                description="固定格式信号 → 50 档等差阶梯挂单 + 自动止盈止损")
    sub = p.add_subparsers(dest="cmd", required=True)

    pa = sub.add_parser("parse", help="解析并预览(不下单)")
    pa.add_argument("file"); pa.add_argument("--orders", type=int, default=DEFAULT_ORDERS); pa.add_argument("--config")

    pl = sub.add_parser("place", help="解析+挂单+登记 Ladder 计划")
    pl.add_argument("file"); pl.add_argument("--orders", type=int, default=DEFAULT_ORDERS); pl.add_argument("--config")

    pls = sub.add_parser("list", help="列出 Ladder 计划"); pls.add_argument("--config")
    pr = sub.add_parser("remove", help="撤剩余挂单并移除计划"); pr.add_argument("target"); pr.add_argument("--config")

    args = p.parse_args()
    if getattr(args, "orders", DEFAULT_ORDERS) < MIN_ORDERS:
        err(f"--orders 至少 {MIN_ORDERS} 张")

    try:
        cfg = _load_cfg(args)
    except SystemExit:
        raise
    except Exception as e:
        out({"error": f"{type(e).__name__}: {e}"})
        return

    try:
        if args.cmd == "parse":
            cmd_parse(cfg, args)
        elif args.cmd == "place":
            result, _oids, _plan = cmd_place(cfg, args)
            _oplog(cfg, "ladder", _args_ns(result), result)
            out(result)
        elif args.cmd == "list":
            cmd_list(cfg)
        elif args.cmd == "remove":
            result = cmd_remove(cfg, args)
            _oplog(cfg, "ladder_remove", _args_ns(result), result)
            out(result)
    except SystemExit:
        raise  # err() 已输出
    except Exception as e:
        out({"error": f"{type(e).__name__}: {e}"})


def _args_ns(result):
    """把结果里的关键字段装成命名空间,供 _oplog 记审计日志。"""
    plan = result.get("plan") or {}
    return SimpleNamespace(
        symbol=result.get("symbol"),
        entry_min=plan.get("entry_min"),
        entry_max=plan.get("entry_max"),
        lev=plan.get("leverage"),
        pct=plan.get("position_pct"),
        sl=plan.get("stop_loss"),
        tp=plan.get("take_profit"),
    )


def _fail_ns(sig, sym):
    """挂单失败时的审计命名空间(没生成 plan,直接从信号取字段)。"""
    return SimpleNamespace(
        symbol=sym,
        entry_min=sig.get("entry_min"),
        entry_max=sig.get("entry_max"),
        lev=sig.get("leverage"),
        pct=sig.get("position_pct"),
        sl=sig.get("stop_loss"),
        tp=sig.get("take_profit"),
    )


if __name__ == "__main__":
    main()
