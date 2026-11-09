"""成交额同比因子：全A / 当前指数「今日累计 vs 昨日同时点」的量价配合。

口径（2026-11-09 与用户确认）：
- 全A = 上证指数 + 深证成指（腾讯 day/query 的分钟级**累计成交额**，两者分别就是
  沪市/深市总额；不含北交所）；
- 对比 = 今日此刻累计 ÷ 昨日**同一时刻**累计（HHMM 对齐，昨日取 ≤T 的最后一点）；
- 放量 ≥1.10x → 顺着当日涨跌 ±权重；缩量 ≤0.90x → **背离口径反向** ±权重
  （缩量上涨记负、缩量下跌记正）；0.90–1.10 平量记 0；
- |涨跌| ≤0.1% 不做量价配合（横盘时不凭空给方向）；09:45 前样本太小不计分；
- 收盘后/非交易日按「最后交易日 vs 其前一交易日」的全天同比。

`cum_at` / `compare_at` / `score` / `merge` / `pick_days` 都是纯函数（不读时钟、不触网），
`all_a()` / `index_ratio()` 只做取数与 30s 缓存。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time

from services import em, market_service as ms

SURGE, SHRINK = 1.10, 0.90
FLAT_PCT = 0.1          # 涨跌幅小于 0.1% 视为无方向，不做量价配合
OPEN_GUARD = "0945"    # 开盘前 15 分钟累计额样本太小（HHMM，无冒号）

_SESSIONS = ((9 * 60 + 30, 11 * 60 + 30), (13 * 60, 15 * 60))
_TOTAL_MIN = sum(e - s for s, e in _SESSIONS)

_TTL = 30
_CACHE: dict[tuple, tuple[float, dict]] = {}

# 全A = 上证（沪市）+ 深证成指（深市总额）
ALL_A_SECIDS = ("1.000001", "0.399001")


def session_progress(now: dt.datetime | None = None) -> float:
    """已完成交易分钟数 / 240，0 表示未开盘、1 表示已收盘。"""
    now = now or dt.datetime.now()
    cur = now.hour * 60 + now.minute + now.second / 60
    done = 0.0
    for start, end in _SESSIONS:
        if cur >= end:
            done += end - start
        elif cur > start:
            done += cur - start
    return max(0.0, min(1.0, done / _TOTAL_MIN))


def cum_at(points: list[tuple], hhmm: str) -> float | None:
    """取 ≤ hhmm 的最后一个累计成交额（昨日的同一时刻对齐用）。纯函数。"""
    out = None
    for stamp, amount in points or []:
        if str(stamp) <= hhmm:
            out = amount
    return out


def compare_at(today_points: list[tuple], prev_points: list[tuple],
               hhmm: str) -> float | None:
    """今日累计 ÷ 昨日同时点累计；任一侧缺数据返回 None。纯函数。"""
    cur = cum_at(today_points, hhmm)
    prev = cum_at(prev_points, hhmm)
    if not cur or not prev:
        return None
    return cur / prev


def score(ratio: float | None, pct: float | None, weight: float,
          hhmm: str | None = None) -> dict:
    """量价配合打分：放量顺势、缩量背离反向。纯函数。

    返回 {"factor": 分数, "status": 放量/缩量/平量/开盘初期/无数据}。
    """
    if ratio is None:
        return {"factor": 0.0, "status": "无数据"}
    if hhmm and hhmm < OPEN_GUARD:
        return {"factor": 0.0, "status": "开盘初期"}
    if ratio >= SURGE:
        status, sign = "放量", 1.0
    elif ratio <= SHRINK:
        status, sign = "缩量", -1.0
    else:
        return {"factor": 0.0, "status": "平量", "ratio": round(ratio, 3)}
    if pct is None or abs(pct) <= FLAT_PCT:
        return {"factor": 0.0, "status": status, "ratio": round(ratio, 3)}
    factor = weight * sign * (1.0 if pct > 0 else -1.0)
    return {"factor": factor, "status": status, "ratio": round(ratio, 3)}


def merge(points_list: list[list[tuple]]) -> list[tuple]:
    """多市场分钟点按 HHMM 求和（取并集，缺的按 0 计）。纯函数。"""
    acc: dict[str, float] = {}
    for points in points_list:
        for stamp, amount in points or []:
            key = str(stamp)
            acc[key] = acc.get(key, 0.0) + float(amount)
    return sorted(acc.items())


def pick_days(series: list[dict], today: str, hhmm_now: str) -> dict | None:
    """选出「对比日」与「基准日」以及要对齐的时刻。纯函数。

    盘中（最后一天就是今天且未收盘）→ 对齐 hhmm_now，live=True；
    收盘后/非交易日 → 对齐 1500，全天同比。
    """
    days = [d for d in series or [] if d.get("points")]
    if len(days) < 2:
        return None
    last, prev = days[-1], days[-2]
    live = last.get("date") == today and hhmm_now < "1500"
    return {"last": last, "prev": prev, "hhmm": hhmm_now if live else "1500",
            "live": live}


def _fmt_amt(v: float | None) -> str:
    if not v:
        return "—"
    return f"{v / 1e12:.2f} 万亿"


def _text(label: str, hhmm: str, ratio: float | None, status: str,
          amount: float | None, prev_amount: float | None, live: bool) -> str:
    when = f"{hhmm[:2]}:{hhmm[2:]} 累计" if live else "全天"
    head = f"{label} {when}成交 {_fmt_amt(amount)}"
    if ratio is None:
        return head + "（缺少昨日同时点数据）"
    base = "较昨日同时点" if live else "较前一交易日"
    return f"{head}，{base}{status} {ratio:.2f}x（基准 {_fmt_amt(prev_amount)}）"


def evaluate(series: list[dict], pct: float | None, weight: float, *, label: str,
             now: dt.datetime | None = None) -> dict:
    """纯函数：给定分钟累计序列与涨跌幅，产出因子（不触网、可注入时钟）。"""
    now = now or dt.datetime.now()
    today = now.date().isoformat()
    hhmm_now = now.strftime("%H%M")
    base = {"ok": False, "factor": 0.0, "status": "无数据", "ratio": None,
            "amount": None, "prev_amount": None, "prev_date": None, "day": None,
            "hhmm": None, "pct": pct, "label": label,
            "progress": round(session_progress(now), 3), "live": False,
            "note": None, "text": "", "ts": now.strftime("%H:%M:%S")}
    picked = pick_days(series, today, hhmm_now)
    if not picked:
        return {**base, "note": "成交额数据不足"}
    last, prev = picked["last"], picked["prev"]
    hhmm = picked["hhmm"]
    ratio = compare_at(last["points"], prev["points"], hhmm)
    sc = score(ratio, pct, weight, hhmm=hhmm if picked["live"] else None)
    amount = cum_at(last["points"], hhmm)
    prev_amount = cum_at(prev["points"], hhmm)
    return {
        **base,
        "ok": ratio is not None,
        "factor": round(sc["factor"], 1),
        "status": sc["status"],
        "ratio": round(ratio, 3) if ratio is not None else None,
        "amount": amount, "prev_amount": prev_amount,
        "prev_date": prev["date"], "day": last["date"], "hhmm": hhmm,
        "live": picked["live"],
        "note": None if ratio is not None else "缺少昨日同时点成交额",
        "text": _text(label, hhmm, ratio, sc["status"], amount, prev_amount,
                      picked["live"]),
    }


async def _quote_pct(secid: str) -> float | None:
    try:
        q = await ms.quote(secid)
    except Exception:  # noqa: BLE001 — 涨跌幅拿不到只影响量价配合方向
        return None
    return (q or {}).get("pct")


def _cached(key: tuple) -> dict | None:
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    return None


def looks_stale(series: list[dict], now: dt.datetime) -> bool:
    """工作日开盘 5 分钟后，最新的日序列还不是今天 → 十有八九拿到了 CDN 旧缓存。纯函数。"""
    days = [d for d in series or [] if d.get("points")]
    if not days or now.weekday() >= 5:
        return False
    if now.strftime("%H%M") < "0935":
        return False
    return days[-1].get("date") != now.date().isoformat()


async def _series(secid: str, now: dt.datetime) -> list[dict]:
    """取两日分钟累计；疑似旧缓存时带时间戳重取一次（腾讯边缘节点偶发回前天数据）。"""
    series = await em.day_series(secid, ndays=2, ttl=30)
    if looks_stale(series, now):
        series = await em.day_series(secid, ndays=2, ttl=0, bust=True)
    return series


async def all_a(force: bool = False) -> dict:
    """全A（沪市+深市）成交额同时间点同比，权重 ±10。"""
    hit = None if force else _cached(("all_a",))
    if hit:
        return hit
    now = dt.datetime.now()
    parts = await asyncio.gather(*[_series(s, now) for s in ALL_A_SECIDS],
                                 return_exceptions=True)
    series = []
    per = []
    for rows in parts:
        per.append({d["date"]: d["points"] for d in rows}
                   if isinstance(rows, list) else {})
    shared = set(per[0]) & set(per[1]) if len(per) == 2 else set()
    if shared:
        series = [{"date": day, "points": merge([per[0][day], per[1][day]])}
                  for day in sorted(shared)]
    res = evaluate(series, await _quote_pct(ALL_A_SECIDS[0]), 10.0, label="全A",
                   now=now)
    _CACHE[("all_a",)] = (time.time(), res)
    return res


async def index_ratio(secid: str, pct: float | None = None, *,
                      label: str = "指数", force: bool = False) -> dict:
    """单个指数（当前 ETF 的关联指数）成交额同时间点同比，权重 ±8。

    `pct` 由调用方给（strategy 已有 track.quote.pct），避免重复请求。
    """
    if not secid:
        return evaluate([], pct, 8.0, label=label)
    key = ("idx", secid)
    hit = None if force else _cached(key)
    if hit:
        return hit
    now = dt.datetime.now()
    series = await _series(secid, now)
    if pct is None:
        pct = await _quote_pct(secid)
    res = evaluate(series, pct, 8.0, label=label, now=now)
    _CACHE[key] = (time.time(), res)
    return res


def factor_text(t: dict) -> str | None:
    """放量/缩量信号文案；平量、开盘初期、无数据都不提示。"""
    if not t.get("ok") or t.get("status") in ("平量", "无数据", "开盘初期"):
        return None
    if t.get("ratio") is None:
        return None
    pct = t.get("pct")
    tail = f"，上证 {pct:+.2f}%" if pct is not None else ""
    return f"{t.get('text') or ''}{tail}"
