"""实时监控：轮询 9 只 ETF，触发量能/资金/背离/关键时点/国家队等告警。

（已按需求移除「价格异动 price_spike」与「合约异动 opt_spike」两类告警。）
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

from config import settings
from services import (alerts, decision, deploy as deploy_svc, em,
                      index_levels_manual as idxm, market_service as ms,
                      money as money_fmt, reversal, strategy, user_settings as usv)
from services.universe import ETFS

log = logging.getLogger("monitor")

_state: dict = {"running": False, "last_cycle": None, "cycles": 0, "last_error": None}
# 上次关键时点已提醒记录（key 含日期；服务常驻不重启时，跨天自动恢复提醒，
# 否则 key_time 告警只在进程启动当天生效一次）
_key_fired: set[str] = set()
# 临期提醒已发记录（key 含日期与档位）。告警总线默认 90 秒冷却完全挡不住
# 「9 只标的 × 已临期月份」的重复，必须按日去重。
_expiry_fired: set[str] = set()


_batch_busy = False
_warmed = False


async def _option_batch() -> None:
    """全市场期权快照 → 分钟累积（后台任务，上一轮没跑完就跳过）。"""
    global _batch_busy
    if _batch_busy:
        return
    _batch_busy = True
    try:
        await em.option_snapshot_batch()
    except Exception as exc:  # noqa: BLE001 — 累积失败不影响告警
        log.debug("合约分钟批量累积失败: %s", exc)
    finally:
        _batch_busy = False


async def _prewarm() -> None:
    """启动后预热方向表（冷启动要拉 12 个标的，8s）：否则用户重启后第一次
    打开看板要等十几秒。 """
    try:
        await strategy.directions()
        log.info("方向表预热完成")
    except Exception as exc:  # noqa: BLE001
        log.debug("预热方向表失败: %s", exc)


async def _expiry_alert(now: dt.datetime) -> None:
    """合约临期分级提醒（按「到期月 × 档位」每天最多一条）。

    到期日是市场级事实、与标的无关，所以不像其他告警挂在 _check_etf 里按标的循环
    ——9 只 ETF 会推 9 条同样的话，反倒掩盖真正重要的那条。
    ≤3 天 critical（弹窗+声音），4~7 天 warn（仅入列表）。
    """
    try:
        tier = (usv.load() or {}).get("tier")
    except Exception as exc:  # noqa: BLE001 — 读设置失败不该阻断监控
        log.debug("读取用户档位失败，按默认档位提醒: %s", exc)
        tier = None
    today = now.date()
    for row in decision.expiry_calendar(today):
        level = decision.near_expiry_level(row["days_left"])
        if not level:
            continue
        key = decision.expiry_alert_key(row["month"], tier, today)
        if key in _expiry_fired:
            continue
        _expiry_fired.add(key)
        # 顺手清理过期 key，防止常驻进程集合无限增长
        for k in [k for k in _expiry_fired if not k.startswith(today.isoformat())]:
            _expiry_fired.discard(k)
        await alerts.publish({
            "etf_name": "合约到期", "type": "expiry_near", "dir": "neutral",
            # 与既有告警的用词保持一致：critical / warn / info
            "level": "critical" if level == "critical" else "warn",
            "title": f"{row['month']} 月合约距今 {row['days_left']} 天到期",
            "text": decision.expiry_alert_text(tier, row["month"], row["days_left"],
                                               row["expiry"]),
            "month": row["month"], "days_left": row["days_left"],
            "expiry": row["expiry"], "tier": tier,
        })


def status() -> dict:
    return dict(_state)


async def _check_etf(etf: dict) -> dict | None:
    """单只标的的告警检查；变盘信号不在这里发，交给 cycle() 按轮合并成一条。"""
    ctx = await ms.etf_context(etf["code"], etf["secid"])
    q = ctx.get("quote") or {}
    if not q:
        return None
    name = etf["name"]

    # 量能骤放（冷却 300s）：近1分钟量能超近60分钟均量 5 倍，且需量价配合
    # （该分钟价格波动 ≥0.15% 或连续两分钟放量），避免把 ETF 申赎/套利篮子的一次性
    # 大单当成有效放量——这类成交常常量能暴增但价格纹丝不动。
    vs = ctx.get("volume") or {}
    if vs.get("confirmed") and alerts.should_alert(etf["code"], "volume_surge", 300):
        mv = vs.get("move_pct") or 0.0
        by_price = vs.get("confirmed_by") == "price"
        confirm_txt = f"该分钟价格 {mv * 100:+.2f}%" if by_price else "连续两分钟放量"
        await alerts.publish({
            "etf_code": etf["code"], "etf_name": name, "type": "volume_surge",
            "level": "warn", "dir": ("bull" if mv > 0 else "bear") if by_price else "neutral",
            "title": f"{name} 量能放大 {vs['ratio']}x",
            "text": f"近1分钟量能为近60分钟均量 {vs['ratio']} 倍（阈值 5 倍），{confirm_txt}，量价配合。",
        })

    # 主力资金突入（冷却 240s）
    d5 = ctx.get("flow_main_delta5m") or 0
    if abs(d5) >= 1.2e8 and alerts.should_alert(etf["code"], "main_inflow", 240):
        await alerts.publish({
            "etf_code": etf["code"], "etf_name": name, "type": "main_inflow",
            "level": "critical", "dir": "bull" if d5 > 0 else "bear",
            "title": f"{name} 主力{'流入' if d5>0 else '流出'} {money_fmt.money(abs(d5))}元",
            "text": f"近5分钟主力净{'流入' if d5>0 else '流出'} {money_fmt.money(abs(d5))}元。",
        })

    # MACD 背离（15 分钟K线口径；冷却 600s）
    div = (ctx.get("macd15") or {}).get("divergence") or {}
    if div.get("type") in ("bullish", "bearish") and alerts.should_alert(etf["code"], "divergence", 600):
        await alerts.publish({
            "etf_code": etf["code"], "etf_name": name, "type": "divergence",
            "level": "warn", "dir": "bull" if div["type"] == "bullish" else "bear",
            "title": f"{name} 15分钟MACD {'底背离' if div['type']=='bullish' else '顶背离'}",
            "text": "15分钟K线动能与价格背离，留意反转。",
        })

    # 关键时点 / 变盘信号都改为「按轮合并」：由 cycle() 汇总后各发一条，
    # 六只同一秒各推一条完全相同的提醒（见 _key_time_alert / _reversal_alert）。

    # 变盘点（5m/15m 高周期，阈值 65，需 15m 结构转向；同向 45 分钟冷却 / 反向 30 分钟抑制）
    try:
        return await reversal.check(etf, ctx)
    except Exception as exc:  # noqa: BLE001
        log.debug("变盘信号计算失败 %s: %s", etf["code"], exc)
        return None


async def _key_time_alert(key_hm: str | None, now: dt.datetime | None = None) -> None:
    """关键时点：市场级一条（每个时点每天只推一次）。

    原实现挂在 _check_etf 里按标的循环，六只 ETF 同一秒会各推一条内容几乎相同的
    「临近变盘时点」，既刷屏又看不出这是同一件事；改为一条，标题带时点与同步只数。
    """
    if not key_hm:
        return
    today = (now or dt.datetime.now()).strftime("%Y-%m-%d")
    kk = f"{today}:{key_hm}"
    if kk in _key_fired:
        return
    _key_fired.add(kk)
    # 顺手清理过期 key，防止常驻进程集合无限增长
    for k in [k for k in _key_fired if not k.startswith(today)]:
        _key_fired.discard(k)
    names = [e["name"] for e in ETFS]
    await alerts.publish({
        "etf_name": "宽基ETF", "type": "key_time", "level": "info", "dir": "neutral",
        "title": f"临近变盘时点 {key_hm}（{len(names)} 只同步）",
        "text": f"{'、'.join(names)} 同时进入关键时点窗口，关注方向选择与量能。",
        "items": [{"code": e["code"], "name": e["name"]} for e in ETFS],
    })


def _dir_cn(d: str | None) -> str:
    return "看多" if d == "bull" else "看空" if d == "bear" else "观望"


async def _reversal_alert(sigs: list[dict]) -> None:
    """一轮里所有标的的变盘信号合并成一条告警。

    单只触发保持原样（标题带名称、可点击跳转）；多只触发改为市场级一条，
    逐只的方向/分数/因子/合约明细放 items[]，避免六只各弹一次窗。
    """
    if not sigs:
        return
    sigs = sorted(sigs, key=lambda s: (-(s.get("score") or 0), s.get("etf_code") or ""))
    if len(sigs) == 1:
        await alerts.publish(sigs[0])
        return
    n_bull = sum(1 for s in sigs if s.get("dir") == "bull")
    n_bear = sum(1 for s in sigs if s.get("dir") == "bear")
    direction = "bull" if n_bull and not n_bear else "bear" if n_bear and not n_bull else "mixed"
    brief = " · ".join(f"{s['etf_name']}{_dir_cn(s['dir'])}{(s.get('score') or 0):.0f}分"
                       for s in sigs)
    await alerts.publish({
        "etf_name": "宽基ETF", "type": "reversal", "level": "critical", "dir": direction,
        "title": f"{len(sigs)} 只标的同步变盘：{brief}",
        "text": f"{len(sigs)} 只标的同轮触发变盘（15m 结构转向确认），"
                f"看多 {n_bull} 只 / 看空 {n_bear} 只；逐只点位与合约见明细。",
        "items": [{
            "code": s.get("etf_code"), "name": s.get("etf_name"),
            "dir": s.get("dir"), "score": s.get("score"),
            "factors": s.get("factors"), "contract": s.get("contract"),
            "text": s.get("text"), "ts": s.get("ts"),
        } for s in sigs],
    })


async def _national_team_alert(nt: dict) -> None:
    """国家队护盘：市场级单条告警（每 600s 最多一次），正文带逐 ETF 资金明细。

    原实现挂在 _check_etf 里按标的循环发出，9 只 ETF 会推 9 条内容完全相同的
    「国家队护盘信号增强」，既刷屏又看不出是哪些宽基在流入；改为市场级一条，
    明细放 etfs[]。
    """
    score = nt.get("score") or 0
    if score < 60 or not alerts.should_alert("MARKET", "national_team", 600):
        return
    entries = list(nt.get("etfs") or [])
    pos = [e for e in entries if (e.get("main_5m") or 0) > 0]
    picked = (pos or entries)[:3]

    def _amt(wan: float) -> str:
        return money_fmt.money(wan * 1e4)

    parts = []
    for e in picked:
        seg = f"{e.get('name')}（{e.get('code')}）{'+' if (e.get('main_5m') or 0) >= 0 else ''}{_amt(e.get('main_5m') or 0)}"
        if e.get("surge") and e.get("vol_ratio"):
            seg += f"·{e['vol_ratio']}x放量"
        parts.append(seg)
    total = sum((e.get("main_5m") or 0) for e in entries)
    n = len(entries)
    title = (f"国家队护盘信号增强（{len(pos)}/{n} 只净流入）" if pos
             else f"国家队护盘信号增强（{n} 只宽基共振）")
    text = (f"宽基ETF近5分钟主力合计净流入 {_amt(total)}，护盘评分 {score}。"
            + "；".join(parts) + "。关注指数能否在支撑位企稳。")
    await alerts.publish({
        "etf_name": "宽基ETF", "type": "national_team",
        "level": "critical", "dir": "bull",
        "title": title, "text": text, "score": score,
        "etfs": [{
            "code": e.get("code"), "name": e.get("name"),
            "main_inflow": round((e.get("main_5m") or 0) * 1e4),   # 元
            "main_total": e.get("main_total"),                     # 亿元
            "pct": e.get("pct"),
            "surge": e.get("vol_ratio") if e.get("surge") else None,
            "vol_ratio": e.get("vol_ratio"),
        } for e in picked],
    })


def _in_trading_window(now: dt.datetime) -> bool:
    """工作日 09:30–11:30 / 13:00–15:00；非交易时段不发请求（降频防封）。"""
    if now.weekday() >= 5:
        return False
    hm = now.strftime("%H:%M")
    return (settings.session_am[0] <= hm <= settings.session_am[1]) or \
           (settings.session_pm[0] <= hm <= settings.session_pm[1])


# 手动支撑/压力位提醒：交易日 09:15–15:00 内，当天还没保存就每 10 分钟提醒一次。
# 首条在开盘后的第一个轮次立即发（不等 10 分钟）；跨天自动重置。
_MANUAL_REMIND_SEC = 600
_manual_remind_ts: float = 0.0
_manual_remind_day: str = ""


def manual_reminder_window(now: dt.datetime) -> bool:
    """提醒时段：工作日 09:15–15:00（含集合竞价与午休）。纯函数，便于单测。"""
    if now.weekday() >= 5:
        return False
    return "09:15" <= now.strftime("%H:%M") <= "15:00"


async def _manual_levels_alert(now: dt.datetime) -> None:
    """当天（按自然日）还没保存手动支撑/压力位时，按 10 分钟节奏发一条提醒。

    判定用「今天有没有记录」，与弹窗的顺延生效无关：沿用了上一条旧值也算没设，
    因为口径要求每天重新确认。用户保存后下一轮就不再提醒。
    """
    global _manual_remind_ts, _manual_remind_day
    day = now.date().isoformat()
    if day != _manual_remind_day:
        _manual_remind_day, _manual_remind_ts = day, 0.0
    if not manual_reminder_window(now):
        return
    records = idxm.load_records()
    if idxm.has_record(records, day):
        return
    if _manual_remind_ts and now.timestamp() - _manual_remind_ts < _MANUAL_REMIND_SEC:
        return
    _manual_remind_ts = now.timestamp()
    cur = idxm.effective(records, day)
    tail = (f"当前沿用 {cur['date']} 的支撑 {cur['support']:.2f} / 压力 {cur['pressure']:.2f}。"
            if cur else "当前还没有任何手动位。")
    await alerts.publish({
        "type": "manual_levels", "level": "warn", "dir": "neutral",
        # 只弹浏览器通知与页面 toast，不响铃（铃声留给 critical）
        "silent": True,
        "title": "今天还没设上证支撑/压力位",
        "text": f"点左栏「上证指数」打开弹窗填写，保存后本提醒立即停止。{tail}",
    })



async def cycle() -> None:
    global _warmed
    now = dt.datetime.now()
    if not _warmed:
        _warmed = True
        asyncio.create_task(_prewarm())
    # 临期提醒是日期型信号，不该被交易时段挡住——开盘前就该看到「今天到期」
    await _expiry_alert(now)
    # 手动支撑/压力位：09:15 起若当天还没设，每 10 分钟提醒一次（也不受交易时段早退影响）
    await _manual_levels_alert(now)
    if not _in_trading_window(now):
        _state["last_cycle"] = now.isoformat(timespec="seconds")
        _state["cycles"] += 1
        return
    hm = now.strftime("%H:%M")
    key_hm = strategy.near_key_time(hm, window=2)
    nt: dict = {}
    try:
        nt = await strategy.national_team_cached()
    except Exception:  # noqa: BLE001
        nt = {}
    await _national_team_alert(nt)
    # 合约分钟累积：push2 的 stock/* 被接口级封禁，clist 仍可用 → 翻页记全市场。
    # 一轮约 20~40s（接口限流 + 重试），**后台跑不阻塞告警链路**；上一轮没完成就跳过。
    asyncio.create_task(_option_batch())
    results = await asyncio.gather(*[_check_etf(e) for e in ETFS], return_exceptions=True)
    sigs = [r for r in results if isinstance(r, dict)]
    await _reversal_alert(sigs)
    await _key_time_alert(key_hm)
    _state["last_cycle"] = now.isoformat(timespec="seconds")
    _state["cycles"] += 1


async def run_forever() -> None:
    _state["running"] = True
    log.info("监控循环启动，间隔 %ss", settings.poll_interval)
    while True:
        try:
            # 自部署令牌：Agent 改完代码写 data/.deploy_token 即触发热更新（不分交易时段）
            opts = deploy_svc.check_token()
            if opts:
                log.info("收到部署令牌，开始自部署: %s", opts.get("reason"))
                await deploy_svc.deploy_now(**opts)  # 成功则退出进程，不返回
            await cycle()
            _state["last_error"] = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _state["last_error"] = str(exc)
            log.warning("监控轮次异常: %s", exc)
        await asyncio.sleep(settings.poll_interval)
