"""全A逐笔分档直方图：通达信 / Wind 两套「资金流向」口径的复刻。

口径要点（2026-11-06 实测，取舍见 `docs/adr/0005-资金流向全A逐笔分档直方图.md`）：

- 数据 = 东财 `details/get` 逐笔（每只 1 个请求；全A 约 5200 只、10 并发约 100~140 秒）；
- 方向字段 `p[4]`：`"2"` 才是**主动买**（外盘）、`"1"` 是主动卖（内盘），
  `"4"` 是**集合竞价虚拟撮合**（3 秒一次重复累计，必须剔除，否则茅台会被算成 3 倍成交额）；
- 每笔金额 = 价格 × 手数 × 100；**每笔只记主动方一次**，所以各档「买 + 卖」之和 = 全市场成交额，
  各档净额相加 = 全市场主动净额（外盘 − 内盘），不为 0；
- 与东财 `f62/f140/...` 的「双向记账（四档净额合计恒为 0）」不是同一口径，两套数字不可互相对账。

两家参考软件**分档维度与竞价处理都不一样**（这是实测出来的，不是猜的）：

| 口径 | 分档维度 | 门槛 | 集合竞价 |
|---|---|---|---|
| 通达信 888881 | 单笔**手数** | 主力 ≥700 手 / 跟风 ≥150 手 / 散户 <150 手 | **整段算主动买**（296 亿，买 119/卖 178） |
| Wind 资金流向 | 单笔**金额** | 超大单 ≥2000 万 / 主力 500–2000 万 / 大户 20–500 万 / 散户 <20 万 | 按方向拆（不并入买） |
| — | Wind「机构」线 | 按账户/席位识别，逐笔里拿不到 | 用「超大单」替代，不复刻 |

所以库里存**两张**直方图（金额档、手数档），各自按对应口径的方向规则计账。

本模块只做「抓取 → 分档 → 落库」与「按任意门槛求和」，不碰 HTTP 层，也不写前端。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from collections import deque
from typing import Iterable

from services import board_map
from services import boardflow as bf
from services import em

log = logging.getLogger("tape_flow")

# 沪深 A 股（不含北交所）：与通达信 888881 / Wind 全市场的成交额口径一致
FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
_LIST_FIELDS = "f12,f13,f14,f6"

# 金额档切点（万元，升序）。档位 i 覆盖 (CUTS[i-1], CUTS[i]]，档位 0 = < CUTS[0]
CUTS = [1, 2, 3, 5, 7, 10, 15, 20, 25, 30, 40, 50, 60, 70, 100, 130, 150, 200,
        250, 300, 400, 500, 600, 700, 1000, 1300, 1500, 2000, 2500, 3000, 4000,
        5000, 7000, 10000, 20000]
# 手数档切点（手，升序）：通达信按手数分档，700/120 附近必须细
LOTS = [10, 20, 30, 40, 50, 60, 80, 100, 120, 150, 200, 250, 300, 400, 500, 600,
        700, 800, 1000, 1200, 1500, 2000, 3000, 5000, 7000, 10000]

DIMS: dict[str, list[int]] = {"amt": CUTS, "lot": LOTS}

PRESETS: dict[str, dict] = {
    "tdx": {
        "label": "通达信",
        "dim": "lot",
        "auction": "buy",           # 集合竞价整段算主动买
        "buckets": [
            {"key": "main", "name": "主力", "thr": 700},
            {"key": "follow", "name": "跟风", "thr": 150},
            {"key": "retail", "name": "散户", "thr": 0},
        ],
    },
    "wind": {
        "label": "Wind",
        "dim": "amt",
        "auction": "drop_open",     # 剔除开盘竞价；收盘竞价按方向
        "buckets": [
            {"key": "inst", "name": "机构", "thr": 400},
            {"key": "main", "name": "主力", "thr": 70},
            {"key": "big", "name": "大户", "thr": 15},
            {"key": "retail", "name": "散户", "thr": 0},
        ],
    },
}

# 「Wind 主力净流入」= Wind 的「机构 + 主力」两档合并 = 单笔 ≥70 万（与资金流向页同口径）
WIND_MAIN_MIN = 70.0     # 万元
SESSION = (("09:30", "11:30"), ("13:00", "15:00"))
SCAN_INTERVAL = 600.0      # 盘中两轮**开始**之间至少隔 10 分钟（一次扫描实测 350~420 秒）
                           # 早先按「上一轮结束后再等 10 分钟」计，叠加 6~7 分钟扫描后实际
                           # 变成 16~17 分钟才更新一次，与页面「每 10 分钟」不符（2026-11-09 修）
FINAL_HM = "15:05"         # 收盘后再跑一次定稿
CONCURRENCY = 10
MIN_COVERAGE = 0.90        # 覆盖率不足就不覆盖已有数据
AMOUNT_TOL = 0.03          # 与官方两市成交额的允许偏差

_state: dict = {"cycles": 0, "busy": False, "last_run": None, "last_date": None,
                "stocks_ok": 0, "stocks_total": 0, "last_error": None}
# 计时基准分两个：next_scan_in 用「上一轮开始」算周期；started_in 是当前轮已跑时长。
_last_scan_mono: float | None = None        # 上一轮**开始**的 monotonic
_scan_started_mono: float | None = None     # 当前轮开始（busy 时用于「已用 x:xx」）
_final_attempt_day: str | None = None       # 哪一天的收盘定稿已经尝试过（失败也不重试，避免死循环轰炸上游）
# 单轮耗时的近几轮实测（用于「数据更新还有多久」预估，默认 420 秒）
_durations: deque = deque(maxlen=5)
SCAN_EST_DEFAULT = 420.0


# --------------------------------------------------------------------------
# 纯函数（可单测：不触网、不读时钟、不写盘）
# --------------------------------------------------------------------------
def minute_of(hhmmss: str) -> str | None:
    """逐笔时间 → 交易分钟（越界的并到首/尾，午休与盘后不进图）。"""
    s = str(hhmmss or "").strip()
    if len(s) < 5 or s[2] != ":":
        return None
    hm = s[:5]
    if hm < SESSION[0][0]:
        return SESSION[0][0]
    if hm > SESSION[1][1]:
        return SESSION[1][1]
    if SESSION[0][1] < hm < SESSION[1][0]:   # 午休不该有连续竞价成交，真有就并到 11:30
        return SESSION[0][1]
    return hm


def is_auction(hhmmss: str) -> bool:
    """是否集合竞价（09:15–09:30 的成交）。"""
    s = str(hhmmss or "").strip()
    return len(s) >= 8 and s < "09:30:00"


def bin_of(value: float, edges: Iterable = CUTS) -> int:
    """档位下标（value 与 edges 同单位）。"""
    if value is None or value <= 0:
        return 0
    i = 0
    for j, cut in enumerate(edges):
        if value >= cut:
            i = j + 1
    return i


def parse_tick(line: str) -> tuple[str, float, float, int, bool] | None:
    """解析一行逐笔 → (分钟, 金额元, 手数, 方向, 是否集合竞价)。

    方向 +1 主动买 / −1 主动卖；`p[4]=="4"`（集合竞价虚拟撮合）返回 None。
    """
    p = str(line or "").split(",")
    if len(p) < 5 or p[4] not in ("1", "2"):
        return None
    try:
        price = float(p[1])
        vol = float(p[2])
    except (TypeError, ValueError):
        return None
    if price <= 0 or vol <= 0:
        return None
    minute = minute_of(p[0])
    if minute is None:
        return None
    return minute, price * vol * 100.0, vol, (1 if p[4] == "2" else -1), is_auction(p[0])


def session_axis() -> list[str]:
    """09:30–11:30 + 13:00–15:00，共 242 个分钟点。"""
    out: list[str] = []
    for start, end in SESSION:
        sh, sm = (int(x) for x in start.split(":"))
        eh, em_ = (int(x) for x in end.split(":"))
        cur = sh * 60 + sm
        stop = eh * 60 + em_
        while cur <= stop:
            out.append(f"{cur // 60:02d}:{cur % 60:02d}")
            cur += 1
    return out


def snap_threshold(value: float, edges: Iterable = CUTS) -> float:
    """把任意门槛吸附到档位表上最近的不低于它的切点（单位同 edges）。

    库里存的是固定档位的直方图，门槛只能取在档边界上，否则该档无法拆分。
    """
    edges = list(edges)
    if value is None or value <= 0:
        return 0.0
    for cut in edges:
        if cut >= value:
            return float(cut)
    return float(edges[-1])


def norm_thresholds(values: Iterable, edges: Iterable = CUTS) -> list[float]:
    """门槛归一：吸附到档位边界、去重、降序、末位必须为 0（最后一档兜底到底）。"""
    out: list[float] = []
    for v in values or []:
        try:
            x = float(v)
        except (TypeError, ValueError):
            continue
        if x > 0:
            out.append(snap_threshold(x, edges))
    out = sorted(set(out), reverse=True)
    out.append(0.0)
    return out


def bucket_of_bin(bin_index: int, thresholds: list[float], edges: Iterable = CUTS) -> int:
    """档位下标 → 口径里的第几档（thresholds 降序、末位 0）。"""
    edges = list(edges)
    lo = 0.0 if bin_index <= 0 else float(edges[bin_index - 1])
    for k, thr in enumerate(thresholds):
        if lo >= thr:
            return k
    return len(thresholds) - 1


def summarize(rows: Iterable[tuple[str, int, float, float]],
              thresholds: Iterable, edges: Iterable = CUTS) -> list[dict]:
    """按门槛汇总各档「当日买 / 当日卖 / 净买额 / 占比」。rows = (分钟, 档位, 买, 卖)。"""
    edges = list(edges)
    ths = norm_thresholds(thresholds, edges)
    acc = [{"buy": 0.0, "sell": 0.0} for _ in ths]
    for _t, b, buy, sell in rows or []:
        k = bucket_of_bin(int(b), ths, edges)
        acc[k]["buy"] += float(buy or 0)
        acc[k]["sell"] += float(sell or 0)
    total = sum(x["buy"] + x["sell"] for x in acc) or 0.0
    out = []
    for thr, x in zip(ths, acc):
        out.append({"thr": thr, "buy": x["buy"], "sell": x["sell"],
                    "net": x["buy"] - x["sell"],
                    "share": (x["buy"] + x["sell"]) / total if total else 0.0})
    return out


def cumulative(rows: Iterable[tuple[str, int, float, float]],
               thresholds: Iterable, axis: list[str] | None = None,
               edges: Iterable = CUTS) -> list[list[float]]:
    """各档的分钟累计净额（元），按 axis 对齐、缺分钟用前值填充。"""
    edges = list(edges)
    axis = axis or session_axis()
    ths = norm_thresholds(thresholds, edges)
    per_min: dict[str, list[float]] = {}
    for t, b, buy, sell in rows or []:
        k = bucket_of_bin(int(b), ths, edges)
        slot = per_min.setdefault(str(t), [0.0] * len(ths))
        slot[k] += float(buy or 0) - float(sell or 0)

    out = [[0.0] * len(ths) for _ in axis]
    running = [0.0] * len(ths)
    for i, minute in enumerate(axis):
        delta = per_min.get(minute)
        if delta:
            for k in range(len(ths)):
                running[k] += delta[k]
        out[i] = list(running)
    return out


def wind_main_tick(amount: float, side: int, auction: bool) -> float:
    """单笔成交对「Wind 主力（机构 + 主力 = ≥70 万）」的净额贡献。

    集合竞价（开盘）不计；不足 70 万不计；返回 +金额（主动买）/−金额（主动卖）。
    """
    if auction or not amount or amount < WIND_MAIN_MIN * 1e4:
        return 0.0
    return float(amount) if side > 0 else -float(amount)


def align_price(axis: Iterable[str], points: Iterable[dict], date: str) -> list[float | None]:
    """把分时价格点对齐到资金轴（HH:MM）：只取当日、缺分钟用前值填充。

    价格线走图上的第二根 Y 轴，和资金净额共用一条时间轴。
    """
    axis = list(axis)
    by_minute: dict[str, float] = {}
    for p in points or []:
        t = str((p or {}).get("time") or "")
        if not t.startswith(str(date)):
            continue
        v = (p or {}).get("price")
        if v is None:
            continue
        try:
            by_minute[t[-5:]] = float(v)
        except (TypeError, ValueError):
            continue
    out: list[float | None] = []
    last: float | None = None
    for m in axis:
        if m in by_minute:
            last = by_minute[m]
        out.append(last)
    return out


def coverage_ok(ok: int, total: int, minimum: float = MIN_COVERAGE) -> bool:
    return bool(total) and ok / total >= minimum


def amount_ok(ours: float, official: float, tol: float = AMOUNT_TOL) -> bool:
    if not official or not ours:
        return False
    return abs(ours - official) / official <= tol


# --------------------------------------------------------------------------
# 抓取：全A逐笔 → 两张直方图（金额档按竞价方向、手数档竞价算买）
# --------------------------------------------------------------------------
async def _details(secid: str) -> list[str]:
    d = await em.get_json("https://push2.eastmoney.com/api/qt/stock/details/get",
                          {"secid": secid, "fields1": "f1,f2,f3,f4",
                           "fields2": "f51,f52,f53,f54,f55", "pos": "-100000"}, ttl=0)
    return ((d or {}).get("data") or {}).get("details") or []


async def scan() -> dict:
    """拉一次全A逐笔并聚合两种维度。"""
    rows = await em.clist_all(FS, _LIST_FIELDS, pz=100, max_page=80, ttl=0)
    # 注意 f13=0 是「深市」，不能用 `or ""` 兜（0 是假值，会把深市整片丢掉）
    items = []
    for r in rows:
        market = "" if r.get("f13") is None else str(r.get("f13"))
        code = "" if r.get("f12") is None else str(r.get("f12"))
        if market in ("0", "1") and code:
            items.append(f"{market}.{code}")
    total = len(items)
    if not total:
        raise RuntimeError("全A清单为空（clist 可能被限流）")

    amt: dict[tuple[str, int], list[float]] = {}
    lot: dict[tuple[str, int], list[float]] = {}
    stocks: dict[str, float] = {}     # 每只股票的 Wind「机构+主力」当日累计净额
    ok = 0
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(secid: str) -> None:
        nonlocal ok
        async with sem:
            try:
                lines = await _details(secid)
            except Exception:  # noqa: BLE001 — 单只失败不影响整体
                return
            if not lines:
                return
            ok += 1
            for line in lines:
                tick = parse_tick(line)
                if not tick:
                    continue
                minute, amount, lots, side, auction = tick
                # 金额档（Wind）：**剔除开盘竞价**（09:30 前），收盘竞价按方向；不剔会把四条线全压歪
                if not auction:
                    slot = amt.setdefault((minute, bin_of(amount / 1e4, CUTS)), [0.0, 0.0])
                    slot[0 if side > 0 else 1] += amount
                # 手数档（通达信）：开盘竞价整段算主动买，收盘竞价按方向
                side_lot = 1 if (auction or side > 0) else -1
                slot2 = lot.setdefault((minute, bin_of(lots, LOTS)), [0.0, 0.0])
                slot2[0 if side_lot > 0 else 1] += amount
                # 按股：Wind 主力（机构+主力 = ≥70 万、剔除开盘竞价）
                delta = wind_main_tick(amount, side, auction)
                if delta:
                    code = secid.split(".", 1)[-1]
                    stocks[code] = stocks.get(code, 0.0) + delta

    await asyncio.gather(*(one(s) for s in items))

    def flat(src):
        return [(m, b, v[0], v[1]) for (m, b), v in src.items() if v[0] or v[1]]

    # 成交额口径用「手数档」那张（含全部竞价），金额档那张刻意剔了开盘竞价，不能拿来校验
    amount = sum(v[0] + v[1] for v in lot.values())
    return {"amt": flat(amt), "lot": flat(lot), "amount": amount, "stocks": stocks,
            "stocks_ok": ok, "stocks_total": total}


async def _trading_day() -> tuple[str, float]:
    """最近一个交易日的日期 + 沪深两市官方成交额。

    日期取上证指数日 K 的最后一根（腾讯 K 线 amount 恒为 0，只能给日期）；
    成交额取 push2 ulist 的 f6（上证指数 + 深证成指 = 两市总额），取不到就返回 0 跳过校验。
    """
    bars = await em.kline("1.000001", 101, 3)
    date = str((bars[-1] or {}).get("date") or "") if bars else ""
    if len(date) != 10:
        raise RuntimeError("取不到交易日（上证日 K 为空）")
    total = 0.0
    try:
        d = await em.get_json("https://push2.eastmoney.com/api/qt/ulist.np/get",
                              {"fltt": "2", "invt": "2", "fields": "f12,f14,f6",
                               "secids": "1.000001,0.399001"}, ttl=0)
        for row in ((d or {}).get("data") or {}).get("diff") or []:
            if row.get("f12") in ("000001", "399001"):
                total += float(row.get("f6") or 0)
    except Exception:  # noqa: BLE001 — 校验是附加项，拿不到就不校验
        total = 0.0
    return date, total


async def run_scan(force: bool = False) -> dict:
    """跑一次全A逐笔并落库（整日覆盖）。force 保留给手动接口语义。"""
    global _last_scan_mono, _scan_started_mono
    if _state.get("busy"):
        return {"ok": False, "error": "上一轮还没跑完"}
    _state["busy"] = True
    _scan_started_mono = _last_scan_mono = time.monotonic()
    t0 = time.time()
    try:
        date, official = await _trading_day()
        got = await scan()
        ours, ok, total = got["amount"], got["stocks_ok"], got["stocks_total"]
        if not coverage_ok(ok, total):
            raise RuntimeError(f"覆盖率不足 {ok}/{total}")
        if official and not amount_ok(ours, official):
            raise RuntimeError(f"成交额对不上：逐笔 {ours/1e8:.0f} 亿 vs 官方 {official/1e8:.0f} 亿")
        await asyncio.to_thread(
            bf.save_tape_day, date, got["amt"], got["lot"],
            {"updated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
             "stocks_total": total, "stocks_ok": ok, "amount": ours})

        # Wind 口径的板块资金：把「每只股票 ≥70 万净额」按板块成分股求和
        boards = 0
        try:
            mapping = await board_map.build()
            flows = board_map.aggregate(got["stocks"], mapping)
            boards = await asyncio.to_thread(
                bf.save_tape_board, date, dt.datetime.now().strftime("%H:%M"), flows)
        except Exception as exc:  # noqa: BLE001 — 板块维度失败不影响全A口径
            log.warning("Wind 板块聚合失败: %s", exc)
        _state.update({"cycles": _state.get("cycles", 0) + 1,
                       "last_run": dt.datetime.now().isoformat(timespec="seconds"),
                       "last_date": date, "stocks_ok": ok, "stocks_total": total,
                       "last_error": None})
        _durations.append(time.time() - t0)
        log.info("全A逐笔入库 %s：%d 只 / 金额档 %d 行 / 手数档 %d 行 / 板块 %d 个 / %.0f 亿 / %.0fs",
                 date, ok, len(got["amt"]), len(got["lot"]), boards, ours / 1e8,
                 time.time() - t0)
        return {"ok": True, "date": date, "rows": len(got["amt"]) + len(got["lot"]),
                "amount": ours, "stocks_ok": ok, "stocks_total": total,
                "boards": boards, "seconds": round(time.time() - t0, 1)}
    except Exception as exc:  # noqa: BLE001
        _state["last_error"] = str(exc)
        log.warning("全A逐笔失败：%s", exc)
        return {"ok": False, "error": str(exc)}
    finally:
        _state["busy"] = False
        _scan_started_mono = None


def _after_final(now: dt.datetime) -> bool:
    return now.strftime("%H:%M") >= FINAL_HM


async def run_forever() -> None:
    """盘中每 10 分钟重算一次，收盘后 15:05 定稿；其余时间只睡。"""
    while True:
        try:
            now = dt.datetime.now()
            if bf.in_session(now):
                if _last_scan_mono is None or time.monotonic() - _last_scan_mono >= SCAN_INTERVAL:
                    await run_scan()
            elif now.weekday() < 5 and _after_final(now):
                # 定稿：今天还没有 15:05 之后成功跑过，就补一次（每天最多一次）。
                # 旧判据是 last_date != 今天——盘中每轮都会把它改成今天，导致收盘定稿
                # 永远不触发（数据停在最后一轮盘中扫描）。
                global _final_attempt_day
                last = _state.get("last_run") or ""
                today = now.strftime("%Y-%m-%d")
                done = last[:10] == today and last[11:16] >= FINAL_HM
                if not done and _final_attempt_day != today:
                    _final_attempt_day = today
                    await run_scan()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _state["last_error"] = str(exc)
            log.warning("逐笔采集循环异常：%s", exc)
        await asyncio.sleep(30)


def scan_est() -> float:
    """单轮预计耗时（近 5 轮实测均值；没有样本时用 420 秒）。"""
    return (sum(_durations) / len(_durations)) if _durations else SCAN_EST_DEFAULT


def state() -> dict:
    """采集器状态 + 供前端算倒计时的相对秒数。

    - ``next_scan_in``：距下一轮**开始**还有多少秒（盘中才有意义；非交易时段由前端按
      交易时段自行显示「休市/已定稿」，这里只给数字，不做展示决策）。
    - ``started_in``：当前轮已经跑了多少秒（busy 时用于「重算中 3:12」）。
    - ``interval``：周期秒数，前端据此校准本地逐秒递减。
    """
    st = dict(_state)
    now = time.monotonic()
    st["interval"] = SCAN_INTERVAL
    wait_in = (
        max(0.0, SCAN_INTERVAL - (now - _last_scan_mono))
        if _last_scan_mono is not None else 0.0)
    st["next_scan_in"] = wait_in
    st["started_in"] = (
        (now - _scan_started_mono) if _scan_started_mono is not None else None)
    # 「数据更新」= 下一轮跑完的时刻：空闲 = 等待 + 单轮预计；重算中 = 预计剩余。
    est = scan_est()
    st["scan_est"] = est
    st["next_update_in"] = (
        max(0.0, est - st["started_in"]) if st["started_in"] is not None
        else wait_in + est)
    st["next_update_at"] = (
        dt.datetime.now() + dt.timedelta(seconds=st["next_update_in"])
    ).strftime("%H:%M")
    return st
