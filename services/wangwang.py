"""汪汪队 ETF 跟踪器：24 只宽基 ETF 的成交额放大倍数 + 主动净买入。

复刻 <https://etf-turnover-tracker.app.workbuddy.host/> 的「今日国家队上班了吗」判断。
**完全自包含**：不依赖本项目其它业务模块（只用 httpx 和本地 SQLite），
不写 `board_flow.db`，也不参与期权链路。

数据口径（2026-11-07 实测）：

- 实时快照 `qt.gtimg.cn/q=`：一次批量 24 只，**GBK 编码**；字段
  a[3] 价 / a[4] 昨收 / a[6] 成交量(手) / a[7] 外盘 / a[8] 内盘 /
  a[30] 时间戳 / a[32] 涨跌幅 / a[37] 成交额(万) / a[38] 换手 / a[45] 规模(亿) / a[72] 份额(份)。
- 新浪 5 分钟 K `quotes.sina.cn`：`scale=5&datalen=1500` 返回 32 个交易日 × 48 根，
  每根带 `amount`。**一份数据同时算出** 20 日均额、日内成交额分布 shape、尾盘占比、近 30 日柱图，
  不必再单独拉日 K（腾讯日 K 的 amount 恒为 0，day/query 只给 5 天）。
- 份额历史：公开源拿不到日频历史，只能每日累积 → `data/wangwang.db` 的 `shares_daily`。

主动净买入 = (外盘 − 内盘) ÷ (外盘 + 内盘) × 成交额，是**逐笔成交方向**的推算，
不是资金净流入，也不是基金申赎，与本项目 ADR-0005 的全A逐笔主动净额**不是同一口径**。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import re
import sqlite3
import time

import httpx

from paths import data_path

log = logging.getLogger("wangwang")

_DB_FILE = data_path("wangwang.db")

_QT = "https://qt.gtimg.cn/q="
_SINA = ("https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_x=/"
         "CN_MarketDataService.getKLineData")

_QUOTE_TTL = 5.0          # 实时快照进程内缓存（秒）
_DAILY_TTL = 900.0        # 5 分钟 K 派生的日频基准（20 日均额/分布/尾盘）缓存 15 分钟

# ---- 池子：20 只「汪汪队重点关注」+ 4 只「扩展观察」--------------------------------
# huijin_yi = Wind 统计的 2025 年报中央汇金/汇金资管进入前十大持有人的份额（亿份）。
# 2026-05-01 起信披新规施行，定期报告不再点名披露前十大持有人 → 这是最后一次公开实名数据，
# 只能当「历史上买过」的静态标签，不是当前持仓。
POOL = [
    {"code": "510300", "name": "沪深300ETF华泰柏瑞", "index": "沪深300",   "group": "core", "huijin_yi": 735.13},
    {"code": "510310", "name": "沪深300ETF易方达",   "index": "沪深300",   "group": "core", "huijin_yi": 558.94},
    {"code": "510050", "name": "上证50ETF华夏",     "index": "上证50",    "group": "core", "huijin_yi": 487.59},
    {"code": "510330", "name": "沪深300ETF华夏",     "index": "沪深300",   "group": "core", "huijin_yi": 420.28},
    {"code": "159919", "name": "沪深300ETF嘉实",     "index": "沪深300",   "group": "core", "huijin_yi": 354.98},
    {"code": "512100", "name": "中证1000ETF南方",    "index": "中证1000",  "group": "core", "huijin_yi": 221.69},
    {"code": "588080", "name": "科创50ETF易方达",    "index": "科创50",    "group": "core", "huijin_yi": 216.07},
    {"code": "159915", "name": "创业板ETF易方达",    "index": "创业板指",  "group": "core", "huijin_yi": 170.20},
    {"code": "510500", "name": "中证500ETF南方",     "index": "中证500",   "group": "core", "huijin_yi": 142.17},
    {"code": "159845", "name": "中证1000ETF华夏",    "index": "中证1000",  "group": "core", "huijin_yi": 137.26},
    {"code": "560010", "name": "中证1000ETF广发",    "index": "中证1000",  "group": "core", "huijin_yi": 105.50},
    {"code": "510180", "name": "上证180ETF华安",     "index": "上证180",   "group": "core", "huijin_yi": 52.32},
    {"code": "159629", "name": "中证1000ETF富国",    "index": "中证1000",  "group": "core", "huijin_yi": 36.34},
    {"code": "510230", "name": "金融ETF国泰",        "index": "上证180金融", "group": "core", "huijin_yi": 25.64},
    {"code": "159922", "name": "中证500ETF嘉实",     "index": "中证500",   "group": "core", "huijin_yi": 23.11},
    {"code": "512500", "name": "中证500ETF华夏",     "index": "中证500",   "group": "core", "huijin_yi": 20.51},
    {"code": "515800", "name": "中证800ETF汇添富",   "index": "中证800",   "group": "core", "huijin_yi": 13.08},
    {"code": "159952", "name": "创业板ETF广发",      "index": "创业板指",  "group": "core", "huijin_yi": 10.91},
    {"code": "588050", "name": "科创50ETF工银",      "index": "科创50",    "group": "core", "huijin_yi": 9.70},
    {"code": "159901", "name": "深证100ETF易方达",   "index": "深证100",   "group": "core", "huijin_yi": 7.19},
    {"code": "588000", "name": "科创50ETF华夏",      "index": "科创50",    "group": "watch", "huijin_yi": 0.0},
    {"code": "512050", "name": "A500ETF华夏",        "index": "中证A500",  "group": "watch", "huijin_yi": 0.0},
    {"code": "159338", "name": "中证A500ETF国泰",    "index": "中证A500",  "group": "watch", "huijin_yi": 0.0},
    {"code": "159339", "name": "A500ETF银华",        "index": "中证A500",  "group": "watch", "huijin_yi": 0.0},
]

BY_CODE = {p["code"]: p for p in POOL}


def tcode_of(code: str) -> str:
    """6 位代码 → 腾讯行情代码。5/6 开头是沪市，1 开头是深市。"""
    return ("sh" if code[:1] in ("5", "6") else "sz") + code


# ---- HTTP -----------------------------------------------------------------------
_CLIENT: httpx.AsyncClient | None = None
_Q_CACHE: dict[str, tuple[float, dict]] = {}
_D_CACHE: dict[str, tuple[float, dict]] = {}


def _client() -> httpx.AsyncClient:
    global _CLIENT
    if _CLIENT is None or _CLIENT.is_closed:
        _CLIENT = httpx.AsyncClient(
            timeout=httpx.Timeout(12.0, connect=6.0),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=8),
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"},
        )
    return _CLIENT


async def close() -> None:
    global _CLIENT
    if _CLIENT is not None and not _CLIENT.is_closed:
        await _CLIENT.aclose()
    _CLIENT = None


async def _text(url: str, tries: int = 2) -> str:
    last: Exception | None = None
    for _ in range(tries):
        try:
            r = await _client().get(url)
            if r.status_code == 200:
                return r.content.decode("gbk", "ignore")
            last = RuntimeError(f"HTTP {r.status_code}")
        except Exception as exc:  # noqa: BLE001
            last = exc
        await asyncio.sleep(0.35)
    log.warning("取数失败 %s: %s", url.split("?")[0], last)
    return ""


def _f(v, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if x == x else default     # 过滤 NaN
    except (TypeError, ValueError):
        return default


# ---- 实时快照 --------------------------------------------------------------------
async def quotes(codes: list[str] | None = None) -> dict[str, dict]:
    """批量取腾讯实时快照（24 只一个请求），5 秒进程内缓存。"""
    codes = codes or [p["code"] for p in POOL]
    key = ",".join(codes)
    hit = _Q_CACHE.get(key)
    if hit and time.time() - hit[0] < _QUOTE_TTL:
        return hit[1]
    url = _QT + ",".join(tcode_of(c) for c in codes)
    txt = await _text(url)
    out: dict[str, dict] = {}
    for line in txt.splitlines():
        if '="' not in line:
            continue
        body = line.split('="', 1)[1].rstrip('"; \r\n')
        a = body.split("~")
        if len(a) < 45:
            continue
        code = a[2].strip()
        if not code:
            continue
        shares = _f(a[72]) if len(a) > 72 else 0.0
        out[code] = {
            "code": code,
            "name": a[1].strip(),
            "price": _f(a[3]),
            "prev_close": _f(a[4]),
            "volume_lots": _f(a[6]),
            "outer_lots": _f(a[7]),
            "inner_lots": _f(a[8]),
            "ts": a[30].strip() if len(a) > 30 else "",
            "pct": _f(a[32]),
            "amount_wan": _f(a[37]),
            "turnover": _f(a[38]),
            "mktcap_yi": _f(a[45]) if len(a) > 45 else 0.0,
            "shares": shares,
            "shares_yi": round(shares / 1e8, 4),
        }
    if out:
        _Q_CACHE[key] = (time.time(), out)
    return out


# ---- 新浪 5 分钟 K → 日频基准 -----------------------------------------------------
_JSONP = re.compile(r"\((\[[\s\S]*\])\)")


async def _sina_bars(tcode: str, datalen: int = 1500) -> list[dict]:
    """新浪 5 分钟 K，32 个交易日 × 48 根（09:35 … 15:00，按根结束时间标注）。"""
    url = (f"{_SINA}?symbol={tcode}&scale=5&ma=no&datalen={datalen}")
    txt = await _text(url)
    m = _JSONP.search(txt)
    if not m:
        return []
    try:
        raw = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []
    out = []
    for b in raw:
        day = str(b.get("day") or "")
        if len(day) < 16:
            continue
        out.append({
            "date": day[:10],
            "hm": day[11:16],
            "amount": _f(b.get("amount")),
            "volume": _f(b.get("volume")),
        })
    return out


def _daily_from_bars(bars: list[dict], today: str | None = None) -> dict:
    """把 5 分钟 K 折叠成：日成交额、日内累计分布 shape、尾盘占比、近 30 日柱图。

    `today` 为当日日期（YYYYMMDD）时，当天数据不完整，一律排除在基准之外。
    """
    by_day: dict[str, list[dict]] = {}
    for b in bars:
        by_day.setdefault(b["date"], []).append(b)
    days = sorted(by_day)
    complete = [d for d in days
                if len(by_day[d]) >= 44 and (today is None or d.replace("-", "") != today)]
    complete = complete[-31:]
    if not complete:
        return {}

    daily = [{"date": d, "amount": sum(x["amount"] for x in by_day[d]),
              "volume": sum(x["volume"] for x in by_day[d])} for d in complete]

    # 日内累计成交额分布：每个时点取「该时点累计额 / 全天额」的 20 日均值
    profile: dict[str, list[float]] = {}
    for d in complete:
        total = sum(x["amount"] for x in by_day[d]) or 1.0
        acc = 0.0
        for x in sorted(by_day[d], key=lambda y: y["hm"]):
            acc += x["amount"]
            profile.setdefault(x["hm"], []).append(acc / total)
    shape = [[hm, round(sum(v) / len(v), 6)] for hm, v in sorted(profile.items())]

    def tail_of(d: str) -> float:
        total = sum(x["amount"] for x in by_day[d]) or 1.0
        tail = sum(x["amount"] for x in by_day[d] if x["hm"] > "14:30")
        return tail / total

    base_days = complete[-20:]
    tail_base = sum(tail_of(d) for d in base_days) / len(base_days)
    # 当日尾盘：优先用今天（含盘中已走完的部分），否则用最后一个完整日
    tail_cur = tail_of(days[-1]) if len(by_day[days[-1]]) >= 44 else tail_base

    return {
        "daily": daily,
        "avg20_yi": round(sum(x["amount"] for x in daily[-20:]) / len(daily[-20:]) / 1e8, 4),
        "shape": shape,
        "tail_ratio": round(tail_cur, 6),
        "tail_ratio_base": round(tail_base, 6),
        "last_date": complete[-1].replace("-", ""),
    }


async def daily(code: str, today: str | None = None) -> dict:
    """单只 ETF 的日频基准（15 分钟缓存，24 只 ≈ 24 个请求/15 分钟）。

    `today`（YYYYMMDD）= 快照交易日：基准一律取**它之前**的完整交易日，
    与目标页「20 日均额取快照交易日之前 20 个交易日」一致，避免把当天算进分母。
    """
    key = f"{code}|{today or ''}"
    hit = _D_CACHE.get(key)
    if hit and time.time() - hit[0] < _DAILY_TTL:
        return hit[1]
    bars = await _sina_bars(tcode_of(code))
    val = _daily_from_bars(bars, today) if bars else {}
    if val:
        _D_CACHE[key] = (time.time(), val)
    return val


async def _daily_many(codes: list[str], today: str | None = None,
                      conc: int = 6) -> dict[str, dict]:
    sem = asyncio.Semaphore(conc)

    async def one(c: str) -> tuple[str, dict]:
        async with sem:
            try:
                return c, await daily(c, today)
            except Exception as exc:  # noqa: BLE001
                log.warning("日频基准失败 %s: %s", c, exc)
                return c, {}

    return dict(await asyncio.gather(*[one(c) for c in codes]))


# ---- 份额每日累积（SQLite） --------------------------------------------------------
def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB_FILE), exist_ok=True)
    conn = sqlite3.connect(_DB_FILE, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with sqlite3.connect(_DB_FILE) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS shares_daily(
                            date TEXT NOT NULL,
                            code TEXT NOT NULL,
                            shares_yi REAL NOT NULL,
                            ts TEXT,
                            PRIMARY KEY(date, code))""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ww_date ON shares_daily(date)")


def record_shares(items: list[dict], date: str) -> int:
    """把当日份额落库（同一日重复写只更新，取最后一次的值 = 收盘后的最终份额）。"""
    if not date or not items:
        return 0
    rows = [(date, it["code"], it["shares_yi"], it.get("ts") or "")
            for it in items if it.get("shares_yi")]
    if not rows:
        return 0
    with _conn() as conn:
        conn.executemany(
            "INSERT INTO shares_daily(date, code, shares_yi, ts) VALUES(?,?,?,?) "
            "ON CONFLICT(date, code) DO UPDATE SET shares_yi=excluded.shares_yi, ts=excluded.ts",
            rows)
    return len(rows)


def shares_history(days: int = 250) -> tuple[dict[str, dict[str, float]], str]:
    """近 N 个交易日的份额（亿份），返回 ({'20261106': {'510300': 466.4}}, 上一日)。"""
    if not os.path.exists(_DB_FILE):
        return {}, ""
    try:
        with _conn() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT DISTINCT date FROM shares_daily ORDER BY date DESC LIMIT ?", (days,))
            dates = [r["date"] for r in cur]
            if not dates:
                return {}, ""
            marks = ",".join("?" * len(dates))
            cur = conn.execute(
                f"SELECT date, code, shares_yi FROM shares_daily WHERE date IN ({marks})", dates)
            out: dict[str, dict[str, float]] = {}
            for r in cur:
                out.setdefault(r["date"], {})[r["code"]] = round(r["shares_yi"], 4)
            return out, dates[0]
    except sqlite3.Error as exc:
        log.warning("读份额历史失败: %s", exc)
        return {}, ""


# ---- 汇总 ------------------------------------------------------------------------
_SNAP: dict = {"ts": 0.0, "data": None, "lock": None}


async def snapshot(force: bool = False) -> dict:
    """整页数据：24 只的实时 + 日频基准 + 份额历史 + 判断所需的全部原始字段。

    verdict（上班/没上班）由前端算：它依赖浏览器本地时钟判断交易时段，
    后端只看数据本身。
    """
    now = time.time()
    if not force and _SNAP["data"] and now - _SNAP["ts"] < _QUOTE_TTL:
        return _SNAP["data"]

    if not os.path.exists(_DB_FILE):
        await asyncio.to_thread(init_db)

    qs = await quotes()
    codes = [p["code"] for p in POOL]
    today = next((q.get("ts", "")[:8] for q in qs.values() if q.get("ts")), "")
    dly = await _daily_many([c for c in codes if c in qs] or codes, today)

    items: list[dict] = []
    for p in POOL:
        q = qs.get(p["code"]) or {}
        d = dly.get(p["code"]) or {}
        shares_yi = q.get("shares_yi") or 0.0
        items.append({
            "code": p["code"],
            "tcode": tcode_of(p["code"]),
            "name": p["name"],
            "index": p["index"],
            "group": p["group"],
            "huijin_yi": p["huijin_yi"],
            "price": q.get("price", 0.0),
            "prev_close": q.get("prev_close", 0.0),
            "pct": q.get("pct", 0.0),
            "amount_wan": q.get("amount_wan", 0.0),
            "amount_yi": round(q.get("amount_wan", 0.0) / 1e4, 4),
            "mktcap_yi": q.get("mktcap_yi", 0.0),
            "shares_yi": shares_yi,
            "turnover": q.get("turnover", 0.0),
            "outer_lots": q.get("outer_lots", 0.0),
            "inner_lots": q.get("inner_lots", 0.0),
            "ts": q.get("ts", ""),
            "date": (q.get("ts") or "")[:8],
            "avg20_yi": d.get("avg20_yi", 0.0),
            "shape": d.get("shape", []),
            "tail_ratio": d.get("tail_ratio"),
            "tail_ratio_base": d.get("tail_ratio_base"),
            "hist": [{"date": x["date"], "amount_yi": round(x["amount"] / 1e8, 4),
                      "volume_lots": round(x["volume"] / 100)} for x in d.get("daily", [])][-30:],
        })

    # 当日份额入库（幂等）：同一天反复写只更新为最新值
    today = next((it["date"] for it in items if it.get("date")), "")
    if today:
        try:
            await asyncio.to_thread(record_shares, items, today)
        except Exception as exc:  # noqa: BLE001
            log.warning("份额入库失败: %s", exc)

    hist, latest = await asyncio.to_thread(shares_history)
    prev_date = max((d for d in hist if d < today), default="") if today else ""
    prev = hist.get(prev_date, {}) if prev_date else {}

    for it in items:
        prev_shares = prev.get(it["code"])
        it["prev_shares"] = round(prev_shares, 4) if prev_shares is not None else None
        if prev_shares is not None and it["price"]:
            it["subscr_net_yi"] = round((it["shares_yi"] - prev_shares) * it["price"], 2)
        else:
            it["subscr_net_yi"] = None

    data = {
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "trade_date": today or latest or "",
        "prev_share_date": prev_date,
        "share_days": len(hist),
        "items": items,
        "shares_history": hist,
        "source": "腾讯行情公开接口 qt.gtimg.cn；日内成交额分布来自新浪 5 分钟 K quotes.sina.cn；"
                  "份额为场内存量份额，每日累积（无公开日频历史源）",
        "huijin_basis": "Wind 统计 2025 年报中央汇金 / 汇金资管进入前十大的持仓份额（2026-03 披露）。"
                        "2026-05-01 起信披新规施行，定期报告不再强制披露前十大持有人，"
                        "此后公开渠道不可验证——名单只是「历史上买过」的静态标签，不是当前持仓。",
    }
    _SNAP["ts"] = time.time()
    _SNAP["data"] = data
    return data


def health() -> dict:
    try:
        with _conn() as conn:
            n = conn.execute("SELECT COUNT(*) c, MAX(date) d FROM shares_daily").fetchone()
        return {"ok": True, "share_rows": n[0], "latest_share_date": n[1],
                "cache_items": len(_D_CACHE)}
    except sqlite3.Error as exc:
        return {"ok": False, "error": str(exc)}


# ---- 后台：收盘后定稿份额（15:02 之后每天一次） -------------------------------------
async def run_forever() -> None:
    """每天 15:02 之后把当日份额写死一次；失败只记日志，不影响其它任务。"""
    await asyncio.to_thread(init_db)
    done = ""
    while True:
        try:
            now = dt.datetime.now()
            hm = now.strftime("%H:%M")
            today = now.strftime("%Y%m%d")
            if now.weekday() < 5 and hm >= "15:02" and done != today:
                snap = await snapshot(force=True)
                if snap.get("trade_date"):
                    done = today
                    log.info("汪汪队份额已定稿 %s（%d 只）", snap["trade_date"], len(snap["items"]))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("份额定稿失败: %s", exc)
        await asyncio.sleep(120)
