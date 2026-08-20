#!/usr/bin/env python
"""
Telegram 守护：白名单用户发指令 → 无头 claude 判断并执行（Bash 调 trade_cli.py）→ 流式回传。

跑法：C:\\Users\\<USER>\\freqtrade\\.venv\\Scripts\\python.exe bot.py
"""

import asyncio
import json
import logging
import os
import re
import sys
import time

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

# 强制 UTF-8，避免 Windows 控制台 GBK 编码问题
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
SESSIONS_PATH = os.path.join(BASE_DIR, "sessions.json")
PLANS_PATH = os.path.join(BASE_DIR, "plans.json")
LOG_DIR = os.path.join(BASE_DIR, "logs")

os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "bot.log"), encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
# 别让 httpx 把 bot_token 写进日志：它的 HTTP Request 行会带上
# https://api.telegram.org/bot<token>/getUpdates 这样的完整 URL
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("crypto-bot")

CFG = {}
ALLOWED_IDS = set()
SYSTEM_PROMPT = ""

# 单线程：一次只处理一条指令
processing = False


# ---------------------------------------------------------------- 配置与状态

def load_config():
    if not os.path.exists(CONFIG_PATH):
        raise SystemExit(f"找不到配置 {CONFIG_PATH}，请先复制 config.example.json 为 config.json 并填写")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_sessions():
    if not os.path.exists(SESSIONS_PATH):
        return {}
    try:
        with open(SESSIONS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_sessions(data):
    with open(SESSIONS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_session(chat_id):
    return load_sessions().get(str(chat_id), {}).get("session_id")


def set_session(chat_id, sid):
    data = load_sessions()
    data[str(chat_id)] = {"session_id": sid, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    save_sessions(data)


def clear_session(chat_id):
    data = load_sessions()
    data.pop(str(chat_id), None)
    save_sessions(data)


def _secrets():
    """收集需要脱敏的密钥（长度≥8 才处理，避免误伤短串）。"""
    out = set()
    tg = CFG.get("telegram", {})
    if tg.get("bot_token"):
        out.add(tg["bot_token"])
    for k in ("api_key", "secret", "testnet_api_key", "testnet_secret"):
        v = CFG.get("binance", {}).get(k)
        if v:
            out.add(v)
    return [s for s in out if len(s) >= 8]


def redact(text):
    for s in _secrets():
        if s and s in text:
            text = text.replace(s, "***")
    return text


def _truncate(text, limit=3950):
    text = redact(text)
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…(已截断)"


async def _safe_edit(tg_msg, text):
    """编辑 Telegram 消息；内容未变化/网络抖动等无害错误静默忽略。"""
    try:
        await tg_msg.edit_text(_truncate(text))
    except Exception:
        pass


# ---------------------------------------------------------------- claude 无头

def build_claude_args(cfg, prompt, session_id, sys_prompt):
    c = cfg["claude"]
    args = [
        c["claude_exe"],
        "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--append-system-prompt", sys_prompt,
        "--tools", "Bash,WebSearch,WebFetch",
        "--allowedTools", "Bash(python trade_cli.py *),Bash(python ladder_signal.py *)",
        "--permission-mode", "acceptEdits",
        "--add-dir", c["project_dir"],
    ]
    if session_id:
        args += ["--resume", session_id]
    return args


async def process_with_claude(cfg, tg_msg, prompt, session_id):
    """跑一个无头 claude，边跑边 edit_text 回传进度，结束时回传最终结果。

    返回 (新 session_id, 是否出错)。
    """
    c = cfg["claude"]
    # 注入当前本地时间，用户要求标记时间时 Claude 有准确依据
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    sys_prompt = SYSTEM_PROMPT + f"\n\n（当前本地时间：{now}。用户要求标记时间时使用这个，写到分钟即可，如 13:05）"
    args = build_claude_args(cfg, prompt, session_id, sys_prompt)
    env = dict(os.environ)
    env["PATH"] = c["venv_scripts"] + os.pathsep + env.get("PATH", "")

    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=c["project_dir"],
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    timeout = float(c.get("timeout_seconds", 240))
    deadline = time.monotonic() + timeout
    progress = ""
    new_sid = session_id
    last_sent = ""
    last_sent_at = 0.0
    final = None
    is_err = False
    timed_out = False
    rest_err = b""

    async def refresh(force=False):
        nonlocal last_sent, last_sent_at
        now = time.monotonic()
        if force or (progress != last_sent and now - last_sent_at >= 1.2):
            await _safe_edit(tg_msg, progress if progress else "🔄 处理中...")
            last_sent = progress
            last_sent_at = now

    try:
        while True:
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError()
                raw = await asyncio.wait_for(proc.stdout.readline(), min(remaining, 30))
            except asyncio.TimeoutError:
                timed_out = True
                is_err = True
                proc.kill()
                break
            if not raw:
                break
            try:
                evt = json.loads(raw.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue
            t = evt.get("type")

            if t == "system" and evt.get("subtype") == "init":
                new_sid = evt.get("session_id") or new_sid
            elif t == "assistant":
                msg = evt.get("message") or {}
                for block in msg.get("content", []):
                    bt = block.get("type")
                    if bt == "text":
                        txt = block.get("text", "")
                        if txt and not progress.endswith(txt):
                            progress += txt
                    elif bt == "tool_use":
                        command = (block.get("input") or {}).get("command", "")
                        if command:
                            progress += f"\n⚙️ {command}"
                            logger.info("chat=%s 执行: %s", tg_msg.chat_id, redact(command)[:200])
                await refresh()
            elif t == "result":
                final = evt.get("result", "") or progress
                is_err = evt.get("is_error", False)
                break
    finally:
        # 完整回收子进程：读走剩余输出并等待退出，避免 unclosed transport
        try:
            _out, rest_err = await asyncio.wait_for(proc.communicate(), 10)
        except Exception:
            proc.kill()
            try:
                _out, rest_err = await proc.communicate()
            except Exception:
                pass

    if final:
        logger.info("chat=%s 回复: %s", tg_msg.chat_id, redact(final)[:600])
        await _safe_edit(tg_msg, final)
        return new_sid, is_err

    if timed_out:
        logger.warning("chat=%s 超时终止", tg_msg.chat_id)
        await _safe_edit(tg_msg, progress + "\n\n⚠️ 处理超时，已终止。")
        return new_sid, True

    if progress:
        logger.info("chat=%s 回复: %s", tg_msg.chat_id, redact(progress)[:600])
        await refresh(force=True)
        return new_sid, True

    tail = rest_err.decode("utf-8", "replace")[:500]
    logger.error("chat=%s 调用异常: %s", tg_msg.chat_id, tail[:300])
    await _safe_edit(tg_msg, f"⚠️ 调用异常 (exit {proc.returncode})\n{tail}")
    return new_sid, True


# ---------------------------------------------------------------- 盯盘轮询（自动开单/止盈止损）

def _load_plans():
    if not os.path.exists(PLANS_PATH):
        return {"plans": []}
    try:
        with open(PLANS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"plans": []}


def _save_plans(data):
    with open(PLANS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _venv_python():
    return os.path.join(CFG.get("claude", {}).get("venv_scripts", ""), "python.exe")


async def _run_cli(args, script="trade_cli.py", timeout=30):
    """跑一个脚本子命令，返回 JSON dict（复用 CLI 的精度/杠杆/闸门逻辑）。

    timeout：普通查询默认 30s 够用；ladder place（50 档挂单）要走几十次交易所调用，
    30s 太紧会被 kill 造成"挂到一半→孤儿挂单、计划没登记"，调用方要单独放宽到 120s。
    """
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(_venv_python()) + os.pathsep + env.get("PATH", "")
    proc = await asyncio.create_subprocess_exec(
        _venv_python(), os.path.join(BASE_DIR, script), *args,
        cwd=BASE_DIR, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except Exception:
        proc.kill()
        out, err = await proc.communicate()
    try:
        return json.loads(out.decode("utf-8", "replace"))
    except Exception:
        return {"error": (err or b"").decode("utf-8", "replace")[:300]}


async def _cancel_ids(symbol, ids):
    """撤掉一批已登记订单 id（保护单等），逐个 cancel，已成交/已撤的忽略。"""
    for oid in ids or []:
        await _run_cli(["cancel", str(oid), symbol])


def _check_sltp(plan, price):
    """返回触发的止盈/止损说明，或 None。"""
    side = plan.get("side")
    sl = plan.get("stop_loss")
    tps = plan.get("take_profits") or []
    if side == "long":
        if sl and price <= float(sl):
            return f"止损（现价 {price:,.0f} ≤ {sl:,.0f}）"
        for tp in tps:
            if price >= float(tp):
                return f"止盈@{float(tp):,.0f}"
    else:
        if sl and price >= float(sl):
            return f"止损（现价 {price:,.0f} ≥ {sl:,.0f}）"
        for tp in tps:
            if price <= float(tp):
                return f"止盈@{float(tp):,.0f}"
    return None


def _ladder_trigger(plan, price):
    """Ladder 计划触发判断：返回 'take_profit' / 'stop_loss' / None。"""
    side = plan.get("side")
    sl = plan.get("stop_loss")
    tp = plan.get("take_profit")
    if side == "long":
        if sl and price <= float(sl):
            return "stop_loss"
        if tp and price >= float(tp):
            return "take_profit"
    else:
        if sl and price >= float(sl):
            return "stop_loss"
        if tp and price <= float(tp):
            return "take_profit"
    return None


async def _handle_ladder(app, chat_ids, p, data):
    """盯一条 Ladder 计划：到止盈点位1/止损 → 市价全平 + 撤剩余挂单。"""
    tick = await _run_cli(["ticker", p["symbol"]])
    if "error" in tick:
        return
    price = tick.get("markPrice") or tick.get("last")
    if not price:
        return
    trig = _ladder_trigger(p, price)
    if trig:
        close_res = await _run_cli(["close", p["symbol"]])
        cancel_res = await _run_cli(["cancel-all", p["symbol"]])
        p["status"] = "closed"
        p["closed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        p["close_reason"] = trig
        _save_plans(data)
        extra = ""
        if "error" in close_res and "无持仓" not in str(close_res.get("error", "")):
            extra = f"\n⚠️ 平仓失败：{close_res['error']}"
        reason_txt = "止盈点位1" if trig == "take_profit" else "止损"
        await _notify(app, chat_ids,
                      f"🎯 Ladder {p['symbol']} {reason_txt} 触发（现价 {price:,.2f}）："
                      f"已市价全平{extra}，并撤销 {cancel_res.get('cancelled', '?')} 张剩余挂单")
        return
    # 未触发：有持仓就确保交易所保护单挂上（首笔成交后自动挂，幂等），再补一次性成交通知
    pos = await _run_cli(["position", p["symbol"]])
    has_pos = not ("error" in pos) and float(pos.get("contracts") or 0) > 0
    if has_pos and not p.get("protect_ids"):
        tp_args = ["tp-sl", p["symbol"], "--side", p["side"]]
        if p.get("take_profit"):
            tp_args += ["--tp", str(p["take_profit"])]
        if p.get("stop_loss"):
            tp_args += ["--sl", str(p["stop_loss"])]
        prot = await _run_cli(tp_args, timeout=60)
        if "error" in prot:
            logger.info("Ladder %s 挂保护单失败: %s", p["symbol"], prot["error"])
        else:
            p["protect_ids"] = [o["order_id"] for o in prot.get("placed", [])]
            _save_plans(data)
            await _notify(app, chat_ids,
                          f"🛡 Ladder {p['symbol']} 已把止盈/止损保护单挂到交易所"
                          f"（止盈 {p.get('take_profit')} / 止损 {p.get('stop_loss')}），bot 挂了/关机也在")
    if has_pos and not p.get("notified_fill"):
        p["notified_fill"] = True
        _save_plans(data)
        await _notify(app, chat_ids,
                      f"🏗️ Ladder {p['symbol']} 已成交 {pos.get('contracts')} "
                      f"（现价 {price:,.2f}），bot 持续盯止盈/止损")


async def _plan_order_quote(plan):
    """按仓位百分比算名义金额（USDT）：可用保证金 × pct% × 杠杆；qty 方式返回币数量。"""
    if plan.get("qty"):
        return None, str(plan["qty"])
    bal = await _run_cli(["balance"])
    avail = 0.0
    margin = bal.get("available_margin") or {}
    if margin.get("USDT"):
        avail = float(margin["USDT"])
    elif margin.get("USDC"):
        avail = float(margin["USDC"])
    pct = float(plan.get("position_pct") or 10)
    lev = float(plan.get("leverage") or 10)
    notional = max(avail * pct / 100.0 * lev, 5.0)
    return str(round(notional, 2)), None


async def _notify(app, chat_ids, text):
    for cid in chat_ids:
        try:
            await app.bot.send_message(cid, redact(text))
        except Exception:
            logger.exception("通知发送失败")


async def _poll_once(app, chat_ids):
    """一轮盯盘：处理 active 计划（入场）与 executed 计划（止盈止损）。"""
    data = _load_plans()
    for p in data.get("plans", []):
        try:
            if p.get("type") == "ladder":
                # Ladder 计划：挂单已由 ladder_signal 提前挂好，这里只盯止盈/止损/首笔成交
                if p.get("status") == "active":
                    await _handle_ladder(app, chat_ids, p, data)
                continue
            status = p.get("status")
            if status == "active":
                tick = await _run_cli(["ticker", p["symbol"]])
                if "error" in tick:
                    continue
                price = tick.get("markPrice") or tick.get("last")
                if not price:
                    continue
                if p["entry_min"] <= price <= p["entry_max"]:
                    quote, qty = await _plan_order_quote(p)
                    cmd = "buy" if p["side"] == "long" else "sell"
                    args = [cmd, p["symbol"]]
                    if quote:
                        args += ["--quote", quote]
                    else:
                        args += [qty]
                    args += ["--lev", str(p["leverage"])]
                    res = await _run_cli(args)
                    p["executed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                    side_txt = "做多" if p["side"] == "long" else "做空"
                    msg = (f"📈 计划自动执行：{p['symbol']} {side_txt} "
                           f"@ ~{price:,.0f}，{p['leverage']}x，现价进入区间 [{p['entry_min']:,.0f}, {p['entry_max']:,.0f}]")
                    if "error" in res:
                        p["status"] = "failed"
                        msg += f"\n⚠️ 下单失败：{res['error']}"
                    else:
                        p["status"] = "executed"
                        msg += f"\n✅ 订单 {res.get('order', {}).get('id')}"
                        # 开仓成功后，把止盈/止损保护单挂到交易所（托管：bot 挂了/关机也在）
                        if p.get("stop_loss") or p.get("take_profits"):
                            tp_args = ["tp-sl", p["symbol"], "--side", p["side"]]
                            if p.get("take_profits"):
                                tp_args += ["--tp", str(p["take_profits"][0])]
                            if p.get("stop_loss"):
                                tp_args += ["--sl", str(p["stop_loss"])]
                            prot = await _run_cli(tp_args)
                            if "error" in prot:
                                msg += f"\n⚠️ 挂保护单失败：{prot['error']}"
                            else:
                                p["protect_ids"] = [o["order_id"] for o in prot.get("placed", [])]
                                desc = "、".join(f"{o['type']}@{o['trigger']:,.0f}"
                                                 for o in prot.get("placed", []))
                                msg += f"\n🛡 交易所保护单：{desc}"
                    _save_plans(data)
                    await _notify(app, chat_ids, msg)
            elif status == "executed":
                if not p.get("stop_loss") and not p.get("take_profits"):
                    continue
                tick = await _run_cli(["ticker", p["symbol"]])
                if "error" in tick:
                    continue
                price = tick.get("markPrice") or tick.get("last")
                if not price:
                    continue
                trig = _check_sltp(p, price)
                if trig:
                    res = await _run_cli(["close", p["symbol"]])
                    # 平掉后撤掉交易所保护单（closePosition 单在仓位清空后通常会自动取消，这里兜底）
                    await _cancel_ids(p["symbol"], p.get("protect_ids"))
                    p["status"] = "closed"
                    p["closed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                    extra = ""
                    if "error" in res and "无持仓" not in str(res.get("error", "")):
                        extra = f"\n⚠️ 平仓失败：{res['error']}"
                    _save_plans(data)
                    await _notify(app, chat_ids,
                                  f"🎯 计划止盈止损触发（{trig}）：已平仓 {p['symbol']}{extra}")
        except Exception:
            logger.exception("盯盘处理计划异常 symbol=%s", p.get("symbol"))


async def poller_loop(app):
    if not CFG.get("poller", {}).get("enabled", True):
        logger.info("盯盘轮询已禁用（config poller.enabled=false）")
        return
    interval = float(CFG.get("poller", {}).get("interval_seconds", 30))
    chat_ids = list(ALLOWED_IDS)
    logger.info("盯盘轮询启动，每 %.0f 秒", interval)
    while True:
        try:
            await _poll_once(app, chat_ids)
        except Exception:
            logger.exception("盯盘轮询周期异常")
        await asyncio.sleep(interval)


# ---------------------------------------------------------------- Telegram 处理

def _allowed(update):
    user = update.effective_user
    return user is not None and user.id in ALLOWED_IDS


def _is_ladder_signal(text):
    """判断是否为固定格式阶梯信号：有 方向/入场/止盈/止损，且首行是币对（带不带 > 前缀都认）。"""
    if not all(k in text for k in ("方向", "入场", "止盈", "止损")):
        return False
    for line in text.splitlines():
        s = line.strip().lstrip(">").strip()
        if not s:
            continue
        # 首行币对：短 token、无空格、无中文
        return bool(re.match(r"^[A-Za-z0-9\-]{1,20}$", s))
    return False


async def _handle_ladder_signal(update, text):
    """识别到固定格式信号 → 存临时文件 → ladder_signal.py place 挂单+登记计划。"""
    logger.info("chat=%s 收到阶梯信号 len=%d: %.120s",
                update.effective_chat.id, len(text), redact(text.replace("\n", " ⏎ ")))
    tg_msg = await update.message.reply_text("🪜 识别到阶梯信号，解析并挂单中...")
    tmp = os.path.join(LOG_DIR, f"ladder_signal_{int(time.time())}.txt")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        res = await _run_cli(["place", tmp], script="ladder_signal.py", timeout=120)
        if "error" in res:
            await _safe_edit(tg_msg, f"⚠️ 阶梯信号执行失败：\n{res['error']}")
            return
        msg = res.get("rewritten") or json.dumps(res, ensure_ascii=False)
        fails = res.get("failed") or []
        if fails:
            brief = "；".join(f"{f.get('price')}:{f.get('error')}" for f in fails[:3])
            msg += f"\n\n⚠️ {len(fails)} 档挂单失败：{brief}"
        await _safe_edit(tg_msg, msg)
    except Exception as e:
        logger.exception("阶梯信号处理失败")
        await _safe_edit(tg_msg, f"⚠️ 阶梯信号处理异常：{e}")
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


async def on_text(update, context):
    global processing
    if not _allowed(update):
        user = update.effective_user
        logger.info("忽略非白名单用户 id=%s username=%s", getattr(user, "id", "?"), getattr(user, "username", ""))
        return
    text = (update.message or {}).text
    if not text:
        return
    if processing:
        await update.message.reply_text("⏳ 上一条指令还在处理中，请稍候再发。")
        return
    processing = True
    chat_id = update.effective_chat.id
    try:
        if CFG.get("poller", {}).get("auto_ladder", True) and _is_ladder_signal(text):
            await _handle_ladder_signal(update, text)
            return
        tg_msg = await update.message.reply_text("🔄 处理中...")
        # 不截断输入：Telegram 单条消息天然上限 4096 字符，直接原样交给 Claude。
        # 以前 [:4000] 会静默截掉尾部，且日志只记 80 字符，用户误以为输入丢了。
        prompt = text.strip()
        sid = get_session(chat_id)
        logger.info("chat=%s 收到指令 len=%d: %.300s", chat_id, len(prompt), redact(prompt))
        new_sid, had_error = await process_with_claude(CFG, tg_msg, prompt, sid)
        if new_sid and new_sid != sid:
            set_session(chat_id, new_sid)
        logger.info("chat=%s 完成 error=%s session=%s", chat_id, had_error, new_sid)
    except Exception as e:
        logger.exception("处理指令失败")
        try:
            await update.message.reply_text(f"⚠️ 内部错误：{e}")
        except Exception:
            pass
    finally:
        processing = False


async def cmd_status(update, context):
    if not _allowed(update):
        return
    chat_id = update.effective_chat.id
    sid = get_session(chat_id)
    await update.message.reply_text(
        f"🤖 状态\n"
        f"模式: {CFG.get('mode', '?')}\n"
        f"市场: {CFG.get('markets', 'spot')}\n"
        f"live_confirmed: {CFG.get('live_confirmed', False)}\n"
        f"会话: {sid or '无'}\n"
        f"白名单: {', '.join(map(str, ALLOWED_IDS)) if ALLOWED_IDS else '未配置'}"
    )


async def cmd_reset(update, context):
    if not _allowed(update):
        return
    clear_session(update.effective_chat.id)
    await update.message.reply_text("🧹 已重置会话，下一条消息开始全新对话。")


async def cmd_help(update, context):
    if not _allowed(update):
        return
    await update.message.reply_text(
        "🗣 直接发中文指令即可，例如：\n"
        "· 查 BTC/USDT 现价\n"
        "· 买 50 USDT 的 BTC\n"
        "· 卖 0.001 BTC\n"
        "· 我的余额\n"
        "· 当前持仓\n"
        "合约（markets=usdt-m 时）：\n"
        "· 买 0.001 个 BTC 合约，10 倍杠杆\n"
        "· 当前持仓 / 平仓 BTC\n\n"
        "命令：/status 状态 · /reset 重置会话\n"
        f"当前模式：{CFG.get('mode', '?')} · 市场：{CFG.get('markets', 'spot')}"
    )


# ---------------------------------------------------------------- 主入口

def main():
    global CFG, ALLOWED_IDS, SYSTEM_PROMPT
    CFG = load_config()
    ALLOWED_IDS = set(CFG.get("telegram", {}).get("allowed_user_ids", []))

    sp = CFG.get("claude", {}).get("system_prompt_file")
    sp_path = os.path.join(BASE_DIR, sp) if sp else None
    if sp_path and os.path.exists(sp_path):
        with open(sp_path, "r", encoding="utf-8") as f:
            SYSTEM_PROMPT = f.read().strip()

    token = CFG.get("telegram", {}).get("bot_token", "")
    if not token:
        print("config.json 里 telegram.bot_token 为空。请用 @BotFather 创建机器人并填入 token 后再启动。")
        raise SystemExit(1)
    if not ALLOWED_IDS:
        print("警告：telegram.allowed_user_ids 为空，机器人会忽略所有人。请填入你的 Telegram user_id。")

    builder = ApplicationBuilder().token(token)
    app = builder.build()

    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # 后台盯盘轮询：应用启动后创建任务，停止时取消
    async def _post_init(app):
        app._poller_task = asyncio.create_task(poller_loop(app))

    async def _post_stop(app):
        t = getattr(app, "_poller_task", None)
        if t:
            t.cancel()

    app.post_init = _post_init
    app.post_stop = _post_stop

    logger.info("bot 启动，模式=%s，白名单=%s", CFG.get("mode"), sorted(ALLOWED_IDS))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
