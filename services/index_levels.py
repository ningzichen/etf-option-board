"""上证指数关键位因子：20 日高低 / 整百关口 / MA5·MA11 排列 + 盘中突破确认。

9 只 ETF 共用同一份结果（30s 缓存），不按关联指数分别计算。仅用上证 000001，
只用 20 日窗口（不含今日未完成日线），不引入 60/120 日阶段位。

另叠加用户手动填写的支撑/压力位（`index_levels_manual`）：按交易日落盘、顺延生效，
与自动位同规则计分（≤0.3% ±8、0.3–0.8% ±4），一起累加后仍整体 clamp ±15；
手动位不参与突破/跌破判定。缓存只存取数结果，关键位每次按当前手动位重算，
所以改完手动值立刻生效（`invalidate()` 用于强制刷新取数）。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import math
import time

from services import em, index_levels_manual as idxm, market_service as ms

SECID = "1.000001"
_TTL = 30
_CACHE: tuple[float, dict] | None = None   # (ts, 原始输入)



def _pct(a: float, b: float) -> float | None:
    """支撑口径：a 相对 b 的距离（%）。"""
    if not b:
        return None
    return round((a / b - 1) * 100, 2)


def _pct_pressure(level: float, last: float) -> float | None:
    """压力口径：位相对现价还有多少空间（%）。"""
    if not last:
        return None
    return round((level / last - 1) * 100, 2)


async def _minute_closes() -> list[float]:
    """最近已收盘的 1 分钟 K 收盘（剔除正在形成的当分钟 K，仅取今日）。"""
    bars = await em.kline(SECID, klt=1, lmt=30, ttl=30)
    today = dt.date.today().isoformat()
    cur_min = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    out: list[float] = []
    for b in em.parse_klines(bars):
        t = str(b.get("date") or "")
        if not t.startswith(today) or t[:16] == cur_min:
            continue
        if b.get("close"):
            out.append(float(b["close"]))
    return out


def evaluate(prev: list[dict], last: float, mins: list[float],
             manual: dict | None = None) -> dict:
    """纯计算：给定「不含今日」的日线、最新点位与已收盘分钟收盘序列，产出关键位。

    `manual` 为手动支撑/压力位（可选，形如 {"support": 3900, "pressure": 4000}）：
    与自动位同规则计分（≤0.3% ±8、0.3–0.8% ±4），一起累加后整体 clamp ±15，
    不参与突破/跌破判定；不传时行为与旧版完全一致。
    """
    window = prev[-20:]
    enough = len(window) >= 5
    hist = [b["close"] for b in prev]
    high20 = max(b["high"] for b in window) if enough else None
    low20 = min(b["low"] for b in window) if enough else None
    ma5 = sum((hist + [last])[-5:]) / 5 if enough else None
    ma11 = sum((hist + [last])[-11:]) / 11 if enough and len(hist) >= 10 else None
    int_lower = math.floor(last / 100) * 100
    int_upper = int_lower + 100

    breakout = breakdown = False
    if enough and len(mins) >= 3 and high20 and low20:
        tail = mins[-3:]
        breakout = all(c >= high20 * 1.002 for c in tail)
        breakdown = all(c <= low20 * 0.998 for c in tail)

    levels: list[dict] = []
    raw = 0.0

    def add(key: str, name: str, value: float | None, side: str, dist: float | None, contrib: float):
        nonlocal raw
        if value is None:
            return
        raw += contrib
        levels.append({"key": key, "name": name, "value": round(value, 2),
                       "side": side, "dist_pct": dist, "contrib": round(contrib, 1)})

    if enough:
        d_low = _pct(last, low20)
        d_high = _pct_pressure(high20, last)
        if breakdown:
            c_low = -10.0
        elif d_low is not None and 0 <= d_low <= 0.3:
            c_low = 8.0
        elif d_low is not None and 0.3 < d_low <= 0.8:
            c_low = 4.0
        else:
            c_low = 0.0
        if breakout:
            c_high = 10.0
        elif d_high is not None and 0 <= d_high <= 0.3:
            c_high = -8.0
        elif d_high is not None and 0.3 < d_high <= 0.8:
            c_high = -4.0
        else:
            c_high = 0.0
        add("low20", "20日低点", low20, "support", d_low, c_low)
        add("high20", "20日高点", high20, "pressure", d_high, c_high)

        d_lower = _pct(last, int_lower)
        d_upper = _pct_pressure(int_upper, last)
        add("int_lower", "下方整百", int_lower, "support", d_lower,
            4.0 if d_lower is not None and d_lower <= 0.5 else 0.0)
        add("int_upper", "上方整百", int_upper, "pressure", d_upper,
            -4.0 if d_upper is not None and d_upper <= 0.5 else 0.0)

        if ma5:
            add("ma5", "MA5", ma5, "ma", _pct(last, ma5),
                (5.0 if ma11 and last > ma5 > ma11
                 else -5.0 if ma11 and last < ma5 < ma11 else 0.0))
        if ma11:
            add("ma11", "MA11", ma11, "ma", _pct(last, ma11), 0.0)

    # 手动支撑/压力位：与自动位同一套距离规则，不判突破/跌破
    if manual:
        sup = manual.get("support")
        if sup is not None:
            d = _pct(last, sup)
            c = (8.0 if d is not None and 0 <= d <= 0.3
                 else 4.0 if d is not None and 0.3 < d <= 0.8 else 0.0)
            add("manual_support", "手动支撑位", sup, "support", d, c)
        pre = manual.get("pressure")
        if pre is not None:
            d = _pct_pressure(pre, last)
            c = (-8.0 if d is not None and 0 <= d <= 0.3
                 else -4.0 if d is not None and 0.3 < d <= 0.8 else 0.0)
            add("manual_pressure", "手动压力位", pre, "pressure", d, c)

    has_manual = any(l["key"].startswith("manual_") for l in levels)
    total = round(max(-15.0, min(15.0, raw)), 1) if (enough or has_manual) else 0.0
    note = None
    if not enough:
        note = "历史不足 5 日，关键位不计分"
    elif len(window) < 20:
        note = f"历史不足 20 日（仅 {len(window)} 日）"
    return {"high20": high20, "low20": low20, "ma5": ma5, "ma11": ma11,
            "int_upper": int_upper, "int_lower": int_lower,
            "total": total, "levels": levels, "note": note,
            "insufficient": not enough, "breakout": breakout, "breakdown": breakdown}


async def _inputs(force: bool = False) -> dict:
    """取数（日线 45 根 / 实时报价 / 已收盘 1m 收盘），30s 缓存；只缓存原始输入。"""
    global _CACHE
    now = time.time()
    if not force and _CACHE and now - _CACHE[0] < _TTL:
        return _CACHE[1]

    daily, q, mins = await asyncio.gather(
        em.kline(SECID, klt=101, lmt=45, ttl=60),
        ms.quote(SECID),
        _minute_closes(),
    )
    bars = em.parse_klines(daily)
    today = dt.date.today().isoformat()
    prev = [b for b in bars if str(b.get("date") or "")[:10] < today]
    last = (q or {}).get("price") or (prev[-1]["close"] if prev else None)
    inp = {"prev": prev, "last": last, "mins": mins, "quote": q or {}}
    _CACHE = (now, inp)
    return inp


def invalidate() -> None:
    """丢掉取数缓存（保存手动位后调用，下一次 snapshot 立即回源）。"""
    global _CACHE
    _CACHE = None


async def snapshot(force: bool = False) -> dict:
    inp = await _inputs(force)
    prev, last, q = inp["prev"], inp["last"], inp["quote"]

    if not prev or not last:
        return {"ok": False, "last": last, "total": 0.0, "levels": [],
                "note": "日线数据不足", "insufficient": True,
                "breakout": False, "breakdown": False, "manual": None,
                "pct": q.get("pct")}

    manual = idxm.current()
    ev = evaluate(prev, last, inp["mins"], manual)
    return {
        "ok": True, "secid": SECID, "code": "000001", "name": "上证指数",
        "last": last, "pct": q.get("pct"), "prev_close": q.get("prev_close"),
        "high20": round(ev["high20"], 2) if ev["high20"] else None,
        "low20": round(ev["low20"], 2) if ev["low20"] else None,
        "ma5": round(ev["ma5"], 2) if ev["ma5"] else None,
        "ma11": round(ev["ma11"], 2) if ev["ma11"] else None,
        "int_upper": ev["int_upper"], "int_lower": ev["int_lower"],
        "total": ev["total"], "levels": ev["levels"], "note": ev["note"],
        "insufficient": ev["insufficient"], "breakout": ev["breakout"],
        "breakdown": ev["breakdown"], "manual": manual,
        "ts": dt.datetime.now().strftime("%H:%M:%S"),
    }



def factor_text(levels: dict) -> str | None:
    """关键位因子 ≥8 或发生突破时，给出点名具体位的文案。"""
    if not levels.get("ok"):
        return None
    if levels.get("breakout"):
        return f"上证指数突破 20 日高点 {levels['high20']:.1f}（3 根 1 分钟 K 均在位外）"
    if levels.get("breakdown"):
        return f"上证指数跌破 20 日低点 {levels['low20']:.1f}"
    if abs(levels.get("total") or 0) < 8:
        return None
    row = max(levels.get("levels") or [], key=lambda x: abs(x.get("contrib") or 0), default=None)
    if not row or not row.get("contrib"):
        return None
    if row["side"] == "support":
        return f"上证距 {row['name']} {row['dist_pct']:.2f}%，支撑有效"
    if row["side"] == "pressure":
        return f"上证距 {row['name']} {row['dist_pct']:.2f}%，压力临近"
    return None
