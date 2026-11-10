"""方向快照 → 人话结论：简洁模式的唯一翻译层（纯函数，不触网、不读时钟）。

设计原则（与 CONTEXT 的方向评分口径一致）：

- **先判阈值再给结论**：|score| < 15 一律「看不清」，不为界面对称强行给方向；
- **强度只说信号一致度**（分数绝对值分档），不代表胜率，前端文案不得暗示概率；
- **理由只取同向且贡献最大的两项**，来源就是已算好的 ``detail`` 因子分项，不二次推断；
- **一屏一个动作**：动作只从「非观望标的」的优选合约里挑，宁可不给也不硬凑。

时钟（会话时段、告警新鲜度）一律由调用方注入 ``now``，便于确定性测试。
"""

from __future__ import annotations

import datetime as dt
import math

# 与 direction_score.score_result / strategy.bias_score 同一阈值：≥15 看多、≤-15 看空
THRESHOLD = 15.0
# 强度分档（只看分数绝对值，不是胜率）
STRONG = 60.0
MEDIUM = 30.0
# 尾盘之后不再建议新开仓（流动性下降、跳价风险）
LATE_HM = "14:45"
# 最近多少分钟内的 critical 告警值得在简洁模式提示
ALERT_FRESH_MIN = 30
# 回本口径：一次完整价差 + 往返手续费（佣金+经手费，保守 6 元/张）
FEE_PER_LOT_RT = 6.0
MULT = 10000

_TONE_LABELS = {"bull": "偏多", "bear": "偏空", "neutral": "看不清"}
_STRENGTH_LABELS = {"strong": "强", "medium": "中", "weak": "弱"}
_SIDE_LABELS = {"call": "认购", "put": "认沽"}

# 因子名 → 人话。值为 (看多措辞, 看空措辞)；无方向的因子只给一句。
_FACTOR_TEXT: dict[str, tuple[str, str]] = {
    "标的5分钟动量": ("近 5 分钟价格向上", "近 5 分钟价格向下"),
    "价格vs均价": ("价格站在分时均价之上", "价格跌破分时均价"),
    "关联指数": ("关联指数当日上涨", "关联指数当日下跌"),
    "指数MACD金叉": ("指数 MACD 金叉", "指数 MACD 金叉"),
    "指数MACD死叉": ("指数 MACD 死叉", "指数 MACD 死叉"),
    "国家队护盘": ("宽基资金出现护盘迹象", "宽基资金出现护盘迹象"),
    "Kronos预测": ("量化模型偏向上涨", "量化模型偏向下跌"),
    "指数关键位": ("指数靠近支撑位", "指数靠近压力位"),
    "外围市场": ("外围市场偏暖", "外围市场偏冷"),
    "市场总成交额": ("成交额放大配合", "成交额萎缩、量能不足"),
}


def _num(value) -> float | None:
    """转 float；bool/非数字/NaN/Inf 一律 None（bool 是 int 子类，须显式排除）。"""
    if isinstance(value, bool) or value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def verdict(score, threshold: float = THRESHOLD) -> dict:
    """分数 → 结论词 + 强度 + 进度条长度。纯函数，非法输入按观望处理。"""
    num = _num(score)
    raw = max(-100.0, min(100.0, num)) if num is not None else 0.0
    tone = "bull" if raw >= threshold else "bear" if raw <= -threshold else "neutral"
    strength = None
    if tone != "neutral":
        strength = "strong" if abs(raw) >= STRONG else "medium" if abs(raw) >= MEDIUM else "weak"
    return {
        "tone": tone,
        "label": _TONE_LABELS[tone],
        "strength": strength,
        "strength_label": _STRENGTH_LABELS.get(strength, ""),
        "score": round(raw, 1),
        "bar_pct": round(abs(raw), 1),
    }


def _factor_text(name: str, value: float) -> str:
    if name.startswith("变盘点"):
        return f"刚出现变盘信号（{name.split('·', 1)[-1]}）"
    pair = _FACTOR_TEXT.get(name)
    if pair:
        return pair[0] if value > 0 else pair[1]
    return name           # 未知因子直接用人话名兜底，不编造含义


def reasons(detail: dict | None, tone: str, limit: int = 2) -> list[dict]:
    """取同向、贡献最大的因子做理由；观望时给一句固定说明，不硬凑方向。"""
    if tone == "neutral":
        return [{"name": "", "value": 0.0, "text": "多空信号相互抵消，暂不参与"}]
    if not isinstance(detail, dict):
        return []
    rows = []
    for name, value in detail.items():
        num = _num(value)
        if num is None or num == 0:
            continue
        if (tone == "bull" and num < 0) or (tone == "bear" and num > 0):
            continue
        rows.append({"name": name, "value": round(num, 1), "text": _factor_text(name, num)})
    rows.sort(key=lambda r: -abs(r["value"]))
    return rows[:max(0, limit)]


def breakeven_pct(pick: dict | None) -> float | None:
    """回本门槛：价差 + 往返手续费折算成「标的需走多少百分比」。

    成本按权利金百分比计：``spread``（已是百分比）+ 手续费/权利金。
    再用有效杠杆换算成标的涨跌幅，便于和日内波动幅度直接比较。
    """
    pick = pick or {}
    spread = _num(pick.get("spread"))
    leverage = _num(pick.get("leverage"))
    last = _num(pick.get("last"))
    if spread is None or not leverage or leverage <= 0 or not last or last <= 0:
        return None
    fee_pct = FEE_PER_LOT_RT / MULT / last * 100
    return round((spread + fee_pct) / leverage, 3)


def session_label(now: dt.datetime) -> dict:
    """交易时段标签 + 是否尾盘。不读系统时钟。"""
    if now.weekday() >= 5:
        return {"key": "closed", "label": "休市", "late": False}
    hm = now.strftime("%H:%M")
    if hm < "09:30":
        return {"key": "preopen", "label": "未开盘", "late": False}
    if "11:30" < hm < "13:00":
        return {"key": "lunch", "label": "午休", "late": False}
    if hm > "15:00":
        return {"key": "closed", "label": "已收盘", "late": False}
    return {"key": "trading", "label": "盘中", "late": hm >= LATE_HM}


def risk_notes(*, expiry: list[dict] | None, alerts: list[dict] | None,
               now: dt.datetime, all_neutral: bool) -> list[str]:
    """风险提示：临期 / 尾盘 / 近 30 分钟严重告警 / 全线观望。最多 4 条。"""
    notes: list[str] = []
    for row in expiry or []:
        try:
            days = int(row.get("days_left"))
            month = row.get("month")
        except (TypeError, ValueError):
            continue
        if days <= 3:
            notes.append(f"{month} 月合约 {days} 天后到期，买方 Theta 加速，注意止损纪律")
            break
        if days <= 7:
            notes.append(f"{month} 月合约进入临期（{days} 天），考虑移仓下月")
            break

    if session_label(now)["late"]:
        notes.append("尾盘流动性变差，不建议新开仓；已有持仓考虑收盘前了结")

    fresh = []
    cutoff = now - dt.timedelta(minutes=ALERT_FRESH_MIN)
    for a in alerts or []:
        if a.get("level") != "critical":
            continue
        try:
            ts = dt.datetime.fromisoformat(str(a.get("ts", "")))
        except (TypeError, ValueError):
            continue
        if ts.tzinfo is not None or ts < cutoff:
            continue
        fresh.append(a)
    if fresh:
        latest = max(fresh, key=lambda a: str(a.get("ts")))
        n = f"（近 {ALERT_FRESH_MIN} 分钟共 {len(fresh)} 条）" if len(fresh) > 1 else ""
        notes.append(f"刚触发严重告警：{latest.get('title')}{n}")

    if all_neutral:
        notes.insert(0, "六只标的均无明确方向，今天多看少动，不做也是决策")
    return notes[:4]


def _strategy(direction: str, iv_pct, expiry: list[dict] | None) -> dict:
    """复用专业模式的策略判定（``decision.select_strategy``），口径完全一致。

    专业模式（``strategy.build_plan``）用同一函数的输出去过滤合约月份、决定是否
    改用垂直价差；简洁模式若自己另判一套，两个入口必然给出互相冲突的建议。
    """
    from services import decision        # 延迟导入：避免模块级连带 option_service

    cal = expiry or []
    front = cal[0].get("days_left") if cal else None
    nxt = cal[1].get("days_left") if len(cal) > 1 else None
    return decision.select_strategy(direction, iv_pct,
                                    days_to_expiry=front, next_month_days=nxt)


def _action(picks: list[dict], items: list[dict],
            expiry: list[dict] | None = None) -> tuple[dict | None, str | None]:
    """从非观望标的、方向一致、且**通过专业模式策略判定**的合约里挑一条。

    返回 ``(动作, 无动作原因)``。临期排除当月、高 IV 改价差这两条规则必须在这里
    生效，否则用户切换模式后会看到两个互相冲突的操作建议。
    """
    by_code = {i["code"]: i for i in items}
    cal = expiry or []
    next_month = cal[1].get("month") if len(cal) > 1 else None
    note = None
    for p in picks:
        item = by_code.get(p.get("etf_code"))
        if not item or item["tone"] == "neutral":
            continue
        want = "call" if item["tone"] == "bull" else "put"
        if p.get("side") != want:
            continue
        strat = _strategy(want, p.get("iv_pct"), cal)
        kind = strat.get("strategy")
        if kind == "wait":
            note = note or strat.get("reason")
            continue
        if kind == "vertical":
            # 专业模式要求用价差降成本时，单张合约不能包装成「今天只做这一笔」
            note = note or f"{strat.get('reason')} 简洁模式不给单腿建议，价差组合请在专业模式查看。"
            continue
        if strat.get("month") == "next" and next_month and p.get("month") != next_month:
            note = note or strat.get("reason")      # 当月临期 → 只认次月合约
            continue
        return {
            "etf_code": p.get("etf_code"), "etf_name": p.get("etf_name"),
            "name": p.get("name"), "code": p.get("code"), "side": want,
            "side_label": _SIDE_LABELS[want],
            "strike": p.get("strike"), "last": p.get("last"),
            "delta": p.get("delta"), "leverage": p.get("leverage"),
            "spread": p.get("spread"), "days": p.get("days"),
            "month": p.get("month"),
            "strategy": kind, "strategy_label": strat.get("label"),
            "breakeven_pct": breakeven_pct(p),
            "verdict": {"label": item["label"], "strength_label": item["strength_label"]},
            "reasons": item["reasons"],
        }, None
    return None, note


def compose(*, directions: dict, etf_meta: list[dict], picks: dict | list | None,
            expiry: list[dict] | None, alerts: list[dict] | None,
            now: dt.datetime) -> dict:
    """把方向快照 / 优选合约 / 临期 / 告警拼成简洁模式的一屏结论。纯函数。

    ``directions`` 含股指期权（MO/IO/HO），简洁模式只服务 ``etf_meta`` 里的 ETF。
    """
    pick_items = picks.get("items") if isinstance(picks, dict) else (picks or [])
    items = []
    for etf in etf_meta:
        d = (directions or {}).get(etf.get("code")) or {}
        v = verdict(d.get("raw_score", d.get("score")))
        items.append({
            "code": etf.get("code"), "name": etf.get("name"),
            **v,
            "reasons": reasons(d.get("detail"), v["tone"]),
        })
    # 有方向在前、按强度降序；观望放最后按代码稳定排序，盘面不变时不跳位置
    items.sort(key=lambda i: (i["tone"] == "neutral", -i["bar_pct"], i["code"]))

    all_neutral = all(i["tone"] == "neutral" for i in items) if items else True
    act, act_note = _action(pick_items, items, expiry)
    return {
        "ts": now.strftime("%H:%M"),
        "session": session_label(now),
        "threshold": THRESHOLD,
        "items": items,
        "action": act,
        # 有方向但不给动作时，说明是专业模式的哪条规则拦下的（临期/价差/观望）
        "action_note": None if act else act_note,
        "notes": risk_notes(expiry=expiry, alerts=alerts, now=now, all_neutral=all_neutral),
    }
