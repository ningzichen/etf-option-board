"""涨停棱镜：沪深 A 股涨停板扫描 + 主题分类（复刻经传「涨停棱镜」）。

**完全自包含**：只读写 `data/limitup.db`，不碰 `board_flow.db`、不参与期权链路。

四个面板的数据来源（2026-11-07 实测）：

1. 涨停池 —— 东财 `push2ex.eastmoney.com/getTopicZTPool`
   `tc` 当日涨停总数 / `lbc` 连板数 / `fund` 封单额 / `fbt`,`lbt` 首次/最后封板时间 /
   `zbc` 炸板次数 / `hybk` 所属行业 / `zttj` 近 N 日涨停统计。
2. 炸板池 —— 同主机 `getTopicZBPool`（炸板 = 曾触板未封住，`zbc` 是开板次数）。
3. 概念板块 + 资金列 —— `push2.eastmoney.com/api/qt/clist/get?fs=m:90+t:3`（531 个概念），
   字段：`f3` 今日涨幅 / `f62` 今日主力净额 / `f184` 今日主力净占比 /
   `f267` 3 日主力净额 / `f268` 3 日占比 / `f164` 5 日主力净额 / `f165` 5 日占比。
4. 个股所属概念 —— `datacenter.eastmoney.com/securities/api/data/v1/get`
   `reportName=RPT_F10_CORETHEME_BOARDTYPE`（每只涨停股 1 请求）。
   返回里混有行业/地域/财务特征板块，**只保留命中东财概念板块名单的名字**。
5. 涨停原因 —— 同花顺 `data.10jqka.com.cn/dataapi/limit_up/limit_up_pool` 的
   `reason_type`（"光伏银粉+低温光伏浆料+电子皮肤+次新股"）。同花顺有 WAF 风控，
   **失败即降级**为东财 `hybk` 行业名，绝不阻塞采集。

## 口径与近似（不是经传的专有算法，见 docs/adr/0006）

- 「全市场涨停数」用东财 `tc`：ST/北交所过滤口径与经传略有差异（实测 32 vs 33）。
- 「主题」= 东财**概念板块**，不等于经传自己的主题库。
- 主题等级按 **20 日涨停总数**分档（0~3 / 3~5 / 5~10 / 10~15 / 15 以上）。
- 扩张 = 当日涨停数 > 前 5 日均值 × 1.5；高潮 = 当日涨停数创 20 日新高；
  三共振 = 首板、连板、炸板三项同时高于各自前 5 日均值。
- 产业链 / 事件性 / 风格性 按关键词近似归类。

## 采集节奏

盘中每 2 分钟刷当日快照，15:05 定稿写死（与 tape_flow 的定稿思路一致）；
启动时若库内不足 `BACKFILL_DAYS` 个交易日，后台回补一次（东财涨停池只能回溯约 1 个月）。
**历史日期的资金四列不采集**：clist 只给"当下"的资金，标到历史日期上是错的，一律留空。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import re
import sqlite3
from typing import Any, Iterable

from services import em

log = logging.getLogger("limitup")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DB_FILE = os.path.join(_ROOT, "data", "limitup.db")

_ZT_URL = "https://push2ex.eastmoney.com/getTopicZTPool"
_ZB_URL = "https://push2ex.eastmoney.com/getTopicZBPool"
_CLIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
_F10_URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
_THS_URL = "https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool"
_THS_POOL_URL = _THS_URL   # 同花顺涨停池（东财窗口外的历史兜底）
_THS_BLOCK_URL = "https://data.10jqka.com.cn/dataapi/limit_up/block_top"
_ULIST_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
# 概念板块成分股数 ≥ 这个值就算「一级主题」（大方向），否则算「二级主题」（细分）。
# 经传的主题分级是自有体系，这里用"板块有多宽"当粒度代理，见 ADR-0006。
LEVEL_PRIMARY_MIN = 150

_EM_UT = "7eea3edcaed734bea9cbfc24409ed989"
_THS_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

BACKFILL_DAYS = 30          # 启动回补的日历天数（含节假日，实际约 20 个交易日；棱镜图默认看 20~25 天）
KEEP_DAYS = 150             # 库内保留窗口
CONCEPT_RANK_LIMIT = 8      # 每只涨停股最多取排名前 N 个概念（越往后越牵强）
PRISM_DAYS = 25
BADGE_DAYS = 7              # 周期表右上角角标的统计窗口（近 N 个交易日涨停总数）
EXT_PCT = 9.9               # 「扩充涨停」的涨幅门槛（> 9.9% 即算涨停）
# 默认「涨停」口径：东财涨停池（封板）∪ 收盘涨幅落在 [9.90%, 10.29%] 的个股。
# 10.29 是主板 10% 涨停因股价四舍五入能出现的上界（1.01→1.11 是 9.90%，2.05→2.26 是 10.24%）。
ZT_PCT_MIN = 9.90
ZT_PCT_MAX = 10.29
MODES = ("zt", "ext")       # zt = 东财涨停池；ext = 涨停池 ∪ 涨幅>9.9%

# ---- 概念噪音黑名单 -----------------------------------------------------------
# 这些「概念」几乎每只票都有（交易属性 / 财务特征），不做过滤的话周期表永远是它们霸榜。
_NOISE_EXACT = {
    "题材股", "融资融券", "深股通", "沪股通", "转融券标的", "MSCI概念", "MSCI中国",
    "富时罗素", "标普道琼斯A股", "标普道琼斯", "昨日涨停", "昨日连板", "昨日触板",
    "股权转让", "预盈预增", "预亏预减", "大盘股", "中盘股", "小盘股", "微盘股",
    "机构重仓", "基金重仓", "QFII重仓", "社保重仓", "证金持股", "汇金持股",
}
_NOISE_SUBSTR = ("预增", "预减", "预亏", "扭亏", "中报", "三季报", "年报", "季报",
                 "高送转", "填权", "摘帽", "举牌", "昨日", "打板", "表现", "连板", "重仓")


_EXCLUDE_FILE = os.path.join(_ROOT, "data", "limitup_exclude.json")
DEFAULT_EXCLUDE = ["央国企改革"]


def excluded_names() -> set[str]:
    """主题排除名单（`data/limitup_exclude.json`，默认排除「央国企改革」）。

    **读时过滤**：改名单立刻生效、不用重采；文件不存在时自动按默认值写一个出来。
    名单是"概念名"而不是板块代码，改起来直观（编辑 json 加一行即可）。
    """
    try:
        with open(_EXCLUDE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return {str(x).strip() for x in data if str(x).strip()}
    except FileNotFoundError:
        try:
            with open(_EXCLUDE_FILE, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_EXCLUDE, f, ensure_ascii=False, indent=2)
        except OSError:
            pass
    except (OSError, ValueError):
        pass
    return set(DEFAULT_EXCLUDE)


def _is_noise(name: str) -> bool:
    """地域板块（"福建板块"）、交易属性、财务特征板块一律当作噪音。"""
    if not name:
        return True
    if name in _NOISE_EXACT:
        return True
    if name.endswith("板块") and len(name) <= 6:
        return True
    return any(s in name for s in _NOISE_SUBSTR)


# ---- 主题类型近似归类 ---------------------------------------------------------
_STYLE_KEYS = ("高股息", "低价", "破净", "次新", "新股", "中字头", "国资", "央企",
               "ST", "重组", "壳资源", "微盘", "高送转", "举牌", "增持", "回购")
_EVENT_KEYS = ("业绩", "涨价", "中标", "订单", "并购", "定增", "政策", "中标",
               "签署", "合作", "重组", "获批", "减税", "补贴", "出口", "关税")


def classify_theme(name: str) -> str:
    """产业链 / 事件性 / 风格性（近似，只用于页面筛选）。"""
    if any(k in name for k in _STYLE_KEYS):
        return "风格性"
    if any(k in name for k in _EVENT_KEYS):
        return "事件性"
    return "产业链"


# ---- 主题等级分档（UI 里那条红色渐变条） ---------------------------------------
# 口径：**当日的涨停家数**分档（与经传那条 0~3 / 3~5 / 5~10 / 10~15 / 15以上 色条一致）。
# 单元格右上角的数字才是「近 7 日涨停总数」（BADGE_DAYS），别把两者混起来。
TIERS = ["0~3", "3~5", "5~10", "10~15", "15以上"]


def tier_of(n: int) -> str:
    """按涨停家数分档。边界取下限（3 → 3~5，10 → 10~15，15 → 15以上）。"""
    if n < 3:
        return "0~3"
    if n < 5:
        return "3~5"
    if n < 10:
        return "5~10"
    if n < 15:
        return "10~15"
    return "15以上"


# ---- 派生标签（纯函数，单测覆盖） ---------------------------------------------
def is_expanding(series: list[int], today_index: int = -1) -> bool:
    """扩张：当日涨停数 > 前 5 日均值 × 1.5（前 5 日不含当日）。"""
    idx = len(series) + today_index if today_index < 0 else today_index
    prev = [x for x in series[max(0, idx - 5):idx]]
    if not prev:
        return False
    avg = sum(prev) / len(prev)
    if avg <= 0:
        return series[idx] > 0
    return series[idx] > avg * 1.5


def is_climax(series: list[int], today_index: int = -1) -> bool:
    """高潮：当日涨停数创近 20 日（含当日）新高。"""
    idx = len(series) + today_index if today_index < 0 else today_index
    window = series[max(0, idx - 19): idx + 1]
    return bool(window) and series[idx] == max(window) and series[idx] >= 3


def is_resonance(first_boards: list[int], lianbans: list[int], brokens: list[int]) -> bool:
    """三共振：首板、连板、炸板三项同时高于各自前 5 日均值（且都为正）。"""
    def rising(s: list[int]) -> bool:
        if len(s) < 2 or s[-1] <= 0:
            return False
        prev = s[-6:-1]
        return s[-1] > (sum(prev) / len(prev) if prev else 0)

    return rising(first_boards) and rising(lianbans) and rising(brokens)


# ---- 周期表矩阵构建（纯函数） -------------------------------------------------
def build_matrix(per_day: list[dict[str, Any]], top: int = 10) -> list[list[dict[str, Any] | None]]:
    """把 [{date, themes:[...]}] 转成「行=名次、列=交易日」的矩阵，缺位用 None 补齐。

    每列内按 zt 降序、max_lbc 降序，取前 `top` 个。
    """
    cols: list[list[dict[str, Any] | None]] = []
    for day in per_day:
        items = day.get("themes") or []
        if items and all(t.get("rank") is not None for t in items):
            ranked = sorted(items, key=lambda t: int(t["rank"]))   # 已按排序口径排好
        else:
            ranked = sorted(items,
                            key=lambda t: (-int(t.get("zt") or 0), -int(t.get("max_lbc") or 0),
                                           t.get("name") or ""))
        col = [dict(t) for t in ranked[:top]]
        col += [None] * (top - len(col))
        cols.append(col)
    return cols


# ============================ SQLite ===========================================
_SEQ = """
CREATE TABLE IF NOT EXISTS lu_pool(
  date TEXT, code TEXT, name TEXT, lbc INTEGER, fund REAL, fbt INTEGER, lbt INTEGER,
  zbc INTEGER, hybk TEXT, pct REAL, amount REAL, reason TEXT, sealed INTEGER, src TEXT,
  reason_detail TEXT, main_net REAL,
  PRIMARY KEY(date, code));
CREATE TABLE IF NOT EXISTS lu_broken(
  date TEXT, code TEXT, name TEXT, zbc INTEGER, pct REAL, hybk TEXT,
  PRIMARY KEY(date, code));
CREATE TABLE IF NOT EXISTS lu_stock_concepts(
  date TEXT, code TEXT, board TEXT, name TEXT, rank INTEGER,
  PRIMARY KEY(date, code, board));
CREATE TABLE IF NOT EXISTS lu_concepts(
  code TEXT PRIMARY KEY, data TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS lu_themes(
  mode TEXT, date TEXT, board TEXT, name TEXT, zt INTEGER, zb INTEGER, lan INTEGER,
  max_lbc INTEGER, lianban INTEGER, first_board INTEGER,
  pct REAL, main REAL, d3 REAL, d5 REAL, ratio REAL,
  PRIMARY KEY(mode, date, board));
CREATE TABLE IF NOT EXISTS lu_boards(
  date TEXT, board TEXT, name TEXT, pct REAL, main REAL, d3 REAL, d5 REAL, ratio REAL,
  PRIMARY KEY(date, board));
CREATE TABLE IF NOT EXISTS lu_board_meta(
  board TEXT PRIMARY KEY, name TEXT, size INTEGER, ts TEXT);
CREATE TABLE IF NOT EXISTS lu_meta(
  date TEXT PRIMARY KEY, updated_at TEXT, total_zt INTEGER, total_ext INTEGER,
  total_zb INTEGER, finalized INTEGER);
CREATE INDEX IF NOT EXISTS ix_lu_themes_date ON lu_themes(mode, date, zt DESC);
CREATE INDEX IF NOT EXISTS ix_lu_pool_date ON lu_pool(date);
"""


def _cols(c: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _migrate(c: sqlite3.Connection) -> None:
    """老库结构升级：`lu_themes` 缺 `mode` / `lu_pool` 缺 `sealed` 说明是加「扩充涨停」
    之前的版本，直接丢掉重建（只有十几个交易日，回补一次就回来了）。"""
    tc = _cols(c, "lu_themes")
    pc = _cols(c, "lu_pool")
    if tc and "mode" not in tc:
        for t in ("lu_pool", "lu_broken", "lu_stock_concepts", "lu_themes",
                  "lu_boards", "lu_meta"):
            c.execute(f"DROP TABLE IF EXISTS {t}")
    elif pc and ("sealed" not in pc or "src" not in pc):
        for t in ("lu_pool", "lu_themes", "lu_meta"):
            c.execute(f"DROP TABLE IF EXISTS {t}")
    else:
        # 增量列（同花顺详细原因 / 个股主力净额）直接 ALTER，保留已有 30 个交易日
        for col, typ in (("reason_detail", "TEXT"), ("main_net", "REAL")):
            if pc and col not in pc:
                c.execute(f"ALTER TABLE lu_pool ADD COLUMN {col} {typ}")


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB_FILE), exist_ok=True)
    c = sqlite3.connect(_DB_FILE, timeout=15)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    with _conn() as c:
        c.executescript(_SEQ)
        _migrate(c)
        c.executescript(_SEQ)


# ============================ 抓取层 ===========================================
def _f(x: Any, default: float | None = None) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v


def _iso(code: str, mkt: int) -> str:
    """涨停池的 m：0=深、1=沪；北交所（4/8 开头）没有可靠市场位，按代码兜底。"""
    if code[:1] in ("4", "8"):
        return f"{code}.BJ"
    return f"{code}.SH" if mkt == 1 else f"{code}.SZ"


async def _zt_pool(date: str) -> dict[str, Any]:
    data = await em.get_json(
        _ZT_URL,
        {"ut": _EM_UT, "dpt": "wz.ztzt", "Pageindex": 0, "pagesize": 600,
         "sort": "fbt:asc", "date": date, "_": "1"},
        ttl=20, referer="https://quote.eastmoney.com/")
    body = (data or {}).get("data") or {}
    return {"tc": int(body.get("tc") or 0), "rows": body.get("pool") or []}


async def _zb_pool(date: str) -> list[dict[str, Any]]:
    data = await em.get_json(
        _ZB_URL,
        {"ut": _EM_UT, "dpt": "wz.ztzt", "Pageindex": 0, "pagesize": 600,
         "sort": "fbt:asc", "date": date, "_": "1"},
        ttl=20, referer="https://quote.eastmoney.com/")
    return ((data or {}).get("data") or {}).get("pool") or []


async def _concept_boards() -> dict[str, dict[str, Any]]:
    """全部东财概念板块（实测 531 个）的当日涨幅与资金四列。name → row。

    注意 `pz` 被上游截到 100（传 1000 也只回 100 条），必须按 `pn` 翻页，
    否则概念名单只剩前 100 个，绝大多数涨停股的概念归属会被全部丢掉。
    """
    out: dict[str, dict[str, Any]] = {}
    for pn in range(1, 10):
        data = await em.get_json(
            _CLIST_URL,
            {"pn": pn, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2, "fid": "f62",
             "fs": "m:90 t:3",
             "fields": "f12,f14,f3,f62,f184,f267,f268,f164,f165,f104,f105,f106", "_": "1"},
            ttl=120, referer="https://data.eastmoney.com/")
        body = (data or {}).get("data") or {}
        diff = body.get("diff") or []
        for r in diff:
            name = r.get("f14")
            if not name:
                continue
            size = sum(int(_f(r.get(k), 0) or 0) for k in ("f104", "f105", "f106"))
            out[name] = {
                "code": r.get("f12"),
                "name": name,
                "pct": _f(r.get("f3")),
                "main": _f(r.get("f62")),
                "ratio": _f(r.get("f184")),
                "d3": _f(r.get("f267")),
                "d5": _f(r.get("f164")),
                "size": size or None,
            }
        total = int(body.get("total") or 0)
        if not diff or len(out) >= total:
            break
    return out


async def _ths_pool(date: str) -> list[dict[str, Any]]:
    """同花顺涨停池 —— 东财回溯窗口（约 20 个自然日）之外只能用它。

    返回与东财同构的字段：`lbc`（连板数，从 `high_days`「3天3板」解析）、
    `fund`（封单额 `order_amount`，单位元，与东财 `fund` 同口径）、
    `reason`（`reason_type`，如「光伏银粉+低温光伏浆料+电子皮肤」）。
    **涨停原因这里最全**，东财没有这个字段。
    """
    out: list[dict[str, Any]] = []
    total = None
    for page in range(1, 6):
        data = await em.get_json(
            _THS_POOL_URL,
            {"page": page, "limit": 200,
             "field": "199112,10,9001,330329,133970,9002,330325",
             "filter": "HS,GEM2STAR", "order_field": "199112", "order_type": 0,
             "date": date},
            ttl=120, referer="https://data.10jqka.com.cn/", tries=2,
            headers={"User-Agent": _THS_UA})
        body = (data or {}).get("data") or {}
        info = body.get("info") or []
        if not info:
            break
        for r in info:
            code = str(r.get("code") or "")
            if not code:
                continue
            out.append({
                "code": code, "name": r.get("name") or code,
                "lbc": _lbc_of(r.get("high_days")),
                "fund": _f(r.get("order_amount")),
                "fbt": int(_f(r.get("first_limit_up_time"), 0) or 0),
                "lbt": int(_f(r.get("last_limit_up_time"), 0) or 0),
                "zbc": int(_f(r.get("open_num"), 0) or 0),
                "hybk": "", "pct": _f(r.get("change_rate")), "amount": None,
                "reason": r.get("reason_type") or "", "sealed": 1,
                "src": "ths", "mkt": _mkt_of(code),
            })
        total = int(((body.get("page") or {}).get("total") or 0))
        if len(out) >= total:
            break
    return out


def _lbc_of(high_days: Any) -> int:
    """同花顺 `high_days`：「首板」→ 1，「3天3板」→ 3，「5天4板」→ 4。"""
    m = re.search(r"(\d+)\s*板", str(high_days or ""))
    return int(m.group(1)) if m else 1


async def _ths_block_top(date: str) -> dict[str, dict[str, str]]:
    """同花顺板块榜：一次请求拿到当日各板块的涨停股 + **每只票的详细涨停原因**。

    `reason_info` 是 300~400 字的分点说明（"1、据2026年9月23日投资者关系活动记录表…"），
    正是弹窗里要展示的长文本；`reason_type` 是短标签（"光伏银粉+低温光伏浆料+…"）。
    历史也能取（实测 6 月都有），所以可用来回补老日期的详细原因。
    """
    data = await em.get_json(
        _THS_BLOCK_URL, {"filter": "HS,GEM2STAR", "date": date},
        ttl=600, referer="https://data.10jqka.com.cn/", tries=2,
        headers={"User-Agent": _THS_UA})
    out: dict[str, dict[str, str]] = {}
    for b in (data or {}).get("data") or []:
        for st in b.get("stock_list") or []:
            code = str(st.get("code") or "")
            if not code:
                continue
            cur = out.get(code) or {}
            detail = _clean_reason(st.get("reason_info"))
            # 同一只票会出现在多个板块里，取最长的那段原因（信息最全）
            if len(detail) > len(cur.get("detail") or ""):
                cur["detail"] = detail
            if not cur.get("type"):
                cur["type"] = st.get("reason_type") or ""
            out[code] = cur
    return out


async def _stock_main_net(codes: list[str]) -> dict[str, float]:
    """批量取个股「今日主力净额」（东财 ulist，一次最多 400 只 → 涨停股 1 个请求）。

    只有实时采集当天有意义：ulist 给的是当下的资金，历史日期一律留空。
    """
    out: dict[str, float] = {}
    for i in range(0, len(codes), 300):
        chunk = codes[i:i + 300]
        secids = ",".join(
            (f"1.{c}" if _mkt_of(c) == 1 else f"0.{c}") for c in chunk)
        data = await em.get_json(
            _ULIST_URL,
            {"fltt": 2, "invt": 2, "fields": "f12,f62", "secids": secids, "_": "1"},
            ttl=60, referer="https://quote.eastmoney.com/")
        for r in ((data or {}).get("data") or {}).get("diff") or []:
            code = str(r.get("f12") or "")
            v = _f(r.get("f62"))
            if code and v is not None:
                out[code] = v
    return out


def is_zt_row(sealed: Any, pct: Any) -> bool:
    """默认口径算不算「涨停」：封板（在涨停池）**或**收盘涨幅落在 9.90%~10.29%。

    纯函数，单测覆盖。`pct` 是百分数（10.1 表示 +10.1%）。
    """
    if sealed:
        return True
    if pct is None:
        return False
    try:
        return ZT_PCT_MIN <= float(pct) <= ZT_PCT_MAX
    except (TypeError, ValueError):
        return False


def is_ext_row(sealed: Any, pct: Any) -> bool:
    """扩充口径：封板**或**涨幅 > 9.9%（含 20cm 涨停、含涨超未封板）。"""
    if sealed:
        return True
    if pct is None:
        return False
    try:
        return float(pct) > EXT_PCT
    except (TypeError, ValueError):
        return False


def top_concepts(concepts: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    """「炒作口径」用的一票一主概念：每只票只保留 BOARD_RANK 最靠前的那一个概念。

    与「板块口径」（一只票计入它所有命中概念，数字更大更泛）相对。
    纯函数，单测覆盖。
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for code, lst in concepts.items():
        if not lst:
            continue
        best = min(lst, key=lambda c: (c.get("rank") or 99, c.get("name") or ""))
        out[code] = [best]
    return out


METHOD_NAMES = {"hot": "炒作涨停", "board": "板块涨停", "pct": "涨幅"}


async def _expanded_rows(date: str) -> list[dict[str, Any]]:
    """「扩充涨停」口径：全 A 按涨幅降序，取涨幅 > 9.9% 的个股。

    按 `f3` 降序翻页，一旦本页末位涨幅 ≤ 9.9% 就停（首屏通常就有 40+ 只，最多 4 页）。
    **剔除 `N`/`C` 开头的新股**（上市首日/前 5 日没有涨跌幅限制，+120% 不是涨停）。
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pn in range(1, 5):
        data = await em.get_json(
            _CLIST_URL,
            {"pn": pn, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2, "fid": "f3",
             "fs": "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048",
             "fields": "f12,f14,f3,f2,f62", "_": "1"},
            ttl=30, referer="https://quote.eastmoney.com/")
        diff = ((data or {}).get("data") or {}).get("diff") or []
        if not diff:
            break
        for r in diff:
            code = str(r.get("f12") or "")
            name = str(r.get("f14") or "")
            p = _f(r.get("f3"))
            if not code or p is None or code in seen:
                continue
            if p <= EXT_PCT:
                continue
            if name[:1] in ("N", "C"):
                continue
            seen.add(code)
            out.append({"code": code, "name": name, "pct": p})
        last = _f(diff[-1].get("f3"))
        if last is None or last <= EXT_PCT:
            break
    return out


async def _stock_concepts(code: str, mkt: int, concept_names: set[str]) -> list[dict[str, Any]]:
    """单只股票的所属概念（已过滤噪音、按 BOARD_RANK 截断）。

    东财这个接口返回的是「所属板块」大杂烩：行业、地域、财务特征、特色数据
    （最近多板 / 东方财富热股 / 连续亏损 / QFII新进 …）全在里面。
    **`IS_PRECISE == "1"` 才是真正的精确概念**（实测每只票 4~16 个，全部命中概念板块名单，
    而噪音项清一色是 0 或 null）——这是最关键的一道过滤，比名字黑名单可靠得多。
    万一某只票一个精确概念都没有，才退回"名字命中概念名单"的那批。

    结果按代码缓存在 `lu_concepts`（7 天）：成分本身就是"当前"口径、与具体日期无关，
    「扩充涨停」把每日待查股票数从 30 只抬到 100+ 只，没有缓存回补会很慢。
    """
    cached = await asyncio.to_thread(_concept_cache_get, code)
    if cached is not None:
        return [c for c in cached if c.get("name") in concept_names][:CONCEPT_RANK_LIMIT]

    data = await em.get_json(
        _F10_URL,
        {"reportName": "RPT_F10_CORETHEME_BOARDTYPE",
         "columns": "SECUCODE,SECURITY_CODE,BOARD_CODE,BOARD_NAME,IS_PRECISE,BOARD_RANK",
         "filter": f'(SECUCODE="{_iso(code, mkt)}")',
         "pageNumber": 1, "pageSize": 100, "sortTypes": 1, "sortColumns": "BOARD_RANK",
         "source": "HSF10", "client": "PC"},
        ttl=3600, referer="https://emweb.securities.eastmoney.com/")
    rows = ((data or {}).get("result") or {}).get("data") or []

    kept: list[dict[str, Any]] = []
    precise: list[dict[str, Any]] = []
    for r in rows:
        name = r.get("BOARD_NAME") or ""
        if _is_noise(name):
            continue
        item = {"name": name, "rank": int(_f(r.get("BOARD_RANK"), 99) or 99)}
        kept.append(item)
        if str(r.get("IS_PRECISE")) == "1":
            precise.append(item)

    out = precise or kept
    out.sort(key=lambda x: x["rank"])
    out = out[:CONCEPT_RANK_LIMIT]
    if rows:                       # 只有真拿到响应才写缓存，空响应下次还能重试
        await asyncio.to_thread(_concept_cache_put, code, out)
    return [c for c in out if c["name"] in concept_names]


def _concept_cache_get(code: str) -> list[dict[str, Any]] | None:
    try:
        with _conn() as c:
            r = c.execute("SELECT data, ts FROM lu_concepts WHERE code=?", (code,)).fetchone()
    except sqlite3.Error:
        return None
    if not r:
        return None
    try:
        ts = dt.datetime.strptime(r["ts"], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    if (dt.datetime.now() - ts).days >= 7:
        return None
    try:
        return json.loads(r["data"])
    except ValueError:
        return None


def _concept_cache_put(code: str, items: list[dict[str, Any]]) -> None:
    try:
        with _conn() as c:
            c.execute("INSERT OR REPLACE INTO lu_concepts VALUES(?,?,?)",
                      (code, json.dumps(items, ensure_ascii=False),
                       dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    except sqlite3.Error:
        pass


async def _ths_reasons(date: str) -> dict[str, str]:
    """同花顺涨停原因（code → reason_type）。任何失败都静默返回空表。

    注意 `limit` 上限 200（传 500 会被直接拒：`limitmust be less than or equal to 200`），
    所以按 200 一页翻到 `total` 为止，最多 5 页。
    """
    out: dict[str, str] = {}
    for page in range(1, 6):
        try:
            data = await em.get_json(
                _THS_URL,
                {"page": page, "limit": 200,
                 "field": "199112,10,9001,330329,133970,9002,9004",
                 "filter": "HS,GEM2STAR", "order_field": "199112", "order_type": 0,
                 "date": date},
                ttl=60, referer="https://data.10jqka.com.cn/", tries=1,
                headers={"User-Agent": _THS_UA})
        except Exception as exc:  # noqa: BLE001
            log.info("同花顺涨停原因不可用（%s），降级为行业名", exc)
            return out
        body = (data or {}).get("data") or {}
        info = body.get("info") or []
        for r in info:
            if r.get("code"):
                out[str(r.get("code"))] = r.get("reason_type") or ""
        total = int(((body.get("page") or {}).get("total") or 0))
        if not info or len(out) >= total:
            break
    return out


# ============================ 聚合层 ===========================================
def aggregate_themes(pool: list[dict[str, Any]],
                     concepts: dict[str, list[dict[str, Any]]],
                     broken: list[dict[str, Any]],
                     boards: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """按「概念 × 当日涨停股」聚合。纯函数，单测覆盖。

    pool: [{code,name,lbc,...}]；concepts: code → [{name}]；broken: 炸板池行。
    """
    acc: dict[str, dict[str, Any]] = {}

    def touch(name: str) -> dict[str, Any]:
        return acc.setdefault(name, {
            "name": name, "board": (boards.get(name) or {}).get("code"),
            "zt": 0, "zb": 0, "lan": 0, "max_lbc": 0, "lianban": 0, "first_board": 0,
        })

    for row in pool:
        lbc = int(_f(row.get("lbc"), 1) or 1)
        for c in concepts.get(str(row.get("code"))) or []:
            t = touch(c["name"])
            t["zt"] += 1
            t["max_lbc"] = max(t["max_lbc"], lbc)
            if lbc >= 2:
                t["lianban"] += 1
            else:
                t["first_board"] += 1

    broken_codes = {str(b.get("code")) for b in broken}
    for row in broken:
        zbc = int(_f(row.get("zbc"), 1) or 1)
        for c in concepts.get(str(row.get("code"))) or []:
            t = touch(c["name"])
            t["zb"] += 1
            if zbc >= 3:
                t["lan"] += 1

    out = []
    for name, t in acc.items():
        b = boards.get(name) or {}
        t.update({"pct": b.get("pct"), "main": b.get("main"),
                  "d3": b.get("d3"), "d5": b.get("d5"), "ratio": b.get("ratio")})
        out.append(t)
    out.sort(key=lambda x: (-x["zt"], -x["max_lbc"], x["name"]))
    return out


def _save_board_meta(boards: dict[str, dict[str, Any]]) -> None:
    """缓存概念板块的成分股数（一级/二级主题分级用，跨日期共用一份）。"""
    rows = [(b["code"], b["name"], b.get("size"),
             dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            for b in boards.values() if b.get("code")]
    if not rows:
        return
    try:
        with _conn() as c:
            c.executemany("INSERT OR REPLACE INTO lu_board_meta VALUES(?,?,?,?)", rows)
    except sqlite3.Error as exc:
        log.warning("板块元数据写入失败: %s", exc)


def _write_snapshot(date: str, *, pool: list[dict[str, Any]],
                    broken: list[dict[str, Any]],
                    themes: dict[str, list[dict[str, Any]]],
                    concepts: dict[str, list[dict[str, Any]]],
                    boards: dict[str, dict[str, Any]],
                    total_zt: int, total_ext: int, finalized: bool,
                    write_boards: bool = True) -> None:
    """一次写入两种口径（`themes` 的键是 mode）。

    `boards` 必须**永远传全量概念名单**：它既用于 `lu_boards` 资金快照（`write_boards`
    控制写不写），也用于把概念名映射成板块代码（`lu_stock_concepts.board`）。
    历史日期不写资金列时若把 `boards` 传空，概念行的 board 会退化成中文名，
    导致「按主题筛个股池」永远筛不到（2026-11-07 踩过）。
    """
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as c:
        c.execute("DELETE FROM lu_pool WHERE date=?", (date,))
        c.execute("DELETE FROM lu_broken WHERE date=?", (date,))
        c.execute("DELETE FROM lu_stock_concepts WHERE date=?", (date,))
        c.execute("DELETE FROM lu_themes WHERE date=?", (date,))
        c.execute("DELETE FROM lu_boards WHERE date=?", (date,))
        c.executemany(
            "INSERT OR REPLACE INTO lu_pool VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(date, p["code"], p["name"], p["lbc"], p["fund"], p["fbt"], p["lbt"],
              p["zbc"], p["hybk"], p["pct"], p["amount"], p["reason"], p["sealed"],
              p.get("src") or "em", p.get("reason_detail"), p.get("main_net"))
             for p in pool])
        c.executemany(
            "INSERT OR REPLACE INTO lu_broken VALUES(?,?,?,?,?,?)",
            [(date, b["code"], b["name"], b["zbc"], b["pct"], b["hybk"]) for b in broken])
        c.executemany(
            "INSERT OR REPLACE INTO lu_stock_concepts VALUES(?,?,?,?,?)",
            [(date, code, boards.get(cc["name"], {}).get("code") or cc["name"],
              cc["name"], cc["rank"])
             for code, lst in concepts.items() for cc in lst])
        rows = []
        for mode, lst in themes.items():
            rows += [(mode, date, t["board"] or t["name"], t["name"], t["zt"], t["zb"],
                      t["lan"], t["max_lbc"], t["lianban"], t["first_board"],
                      t["pct"], t["main"], t["d3"], t["d5"], t["ratio"]) for t in lst]
        c.executemany(
            "INSERT OR REPLACE INTO lu_themes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        if write_boards and boards:
            c.executemany(
                "INSERT OR REPLACE INTO lu_boards VALUES(?,?,?,?,?,?,?,?)",
                [(date, b["code"], b["name"], b.get("pct"), b.get("main"),
                  b.get("d3"), b.get("d5"), b.get("ratio")) for b in boards.values()])
        c.execute("INSERT OR REPLACE INTO lu_meta VALUES(?,?,?,?,?,?)",
                  (date, now, total_zt, total_ext, len(broken), 1 if finalized else 0))
        # 裁剪老数据
        c.execute("DELETE FROM lu_pool WHERE date < date('now', ?)", (f"-{KEEP_DAYS} day",))
        c.execute("DELETE FROM lu_themes WHERE date < date('now', ?)", (f"-{KEEP_DAYS} day",))
        c.execute("DELETE FROM lu_boards WHERE date < date('now', ?)", (f"-{KEEP_DAYS} day",))
        c.execute("DELETE FROM lu_meta WHERE date < date('now', ?)", (f"-{KEEP_DAYS} day",))


# ============================ 采集入口 =========================================
async def collect(date: str, *, finalize: bool = False, with_funds: bool = True) -> dict[str, Any]:
    """抓取并落库某一天的快照（两种口径：东财涨停池 / 扩充涨停）。

    涨停池优先东财（字段最全）；东财对 20 个自然日前一律返回空，这时**自动退回同花顺**，
    并把 `lu_pool.src` 标成 `ths`。同花顺没有历史炸板池（接口 404），所以老日期的
    炸板/烂板为空、三共振判定不成立——这是上游能拿到的极限，不是 bug。
    """
    zt = await _zt_pool(date)
    src = "em"
    base: list[dict[str, Any]] = []
    for r in zt["rows"]:
        base.append({
            "code": str(r.get("c")), "name": r.get("n"),
            "lbc": int(_f(r.get("lbc"), 1) or 1), "fund": _f(r.get("fund")),
            "fbt": int(_f(r.get("fbt"), 0) or 0), "lbt": int(_f(r.get("lbt"), 0) or 0),
            "zbc": int(_f(r.get("zbc"), 0) or 0), "hybk": r.get("hybk"),
            "pct": _f(r.get("zdp")), "amount": _f(r.get("amount")),
            "reason": "", "sealed": 1, "src": "em",
            "mkt": int(_f(r.get("m"), 0) or 0),
        })
    if not base:
        base = await _ths_pool(date)
        if base:
            src = "ths"
    if not base:
        return {"error": "涨停池为空（非交易日或上游不可用）", "date": date, "total": 0}

    zb_raw = await _zb_pool(date) if src == "em" else []
    # 概念名单必须每次都拉（用于选股归属过滤 + 板块代码）；with_funds 只决定「资金四列」是否落库，
    # 因为 clist 给的是"当下"的资金，标到历史日期上是错的。
    boards = await _concept_boards()
    concept_names = set(boards)

    # 扩充涨停只在「实时采集」时才拉：clist 给的是当下的涨幅，套到历史日期上一定是错的。
    expanded = await _expanded_rows(date) if (src == "em" and with_funds) else []
    sealed_codes = {b["code"] for b in base}
    ext_only = [r for r in expanded if r["code"] not in sealed_codes]

    # 概念归属：只查当天涨停/炸板的票（并发 8）
    codes: dict[str, int] = {}
    for b in base:
        codes[b["code"]] = int(b["mkt"])
    for r in zb_raw:
        codes.setdefault(str(r.get("c")), int(_f(r.get("m"), 0) or 0))
    for r in ext_only:
        codes.setdefault(r["code"], _mkt_of(r["code"]))

    reasons = await _ths_reasons(date) if src == "em" else {}
    if src == "ths":                      # 同花顺的行自带 reason_type，不必再查
        reasons = {}
    # 详细原因（弹窗里的长文本）与个股主力净额：一次板块榜 + 一次 ulist 批量
    detail = await _ths_block_top(date)
    mainnet = await _stock_main_net([b["code"] for b in base]) if with_funds else {}

    sem = asyncio.Semaphore(8)

    async def _one(code: str, mkt: int):
        async with sem:
            if concept_names:
                return code, await _stock_concepts(code, mkt, concept_names)
            return code, []

    pairs = await asyncio.gather(*[_one(k, v) for k, v in codes.items()],
                                 return_exceptions=True)
    concepts: dict[str, list[dict[str, Any]]] = {}
    for p in pairs:
        if isinstance(p, tuple):
            concepts[p[0]] = p[1]

    pool = []
    for b in base:
        pool.append({
            "code": b["code"], "name": b["name"], "lbc": b["lbc"], "fund": b["fund"],
            "fbt": b["fbt"], "lbt": b["lbt"], "zbc": b["zbc"], "hybk": b["hybk"],
            "pct": b["pct"], "amount": b["amount"],
            "reason": _clean_reason(b.get("reason") or reasons.get(b["code"])
                                    or (detail.get(b["code"]) or {}).get("type")
                                    or b["hybk"] or ""),
            "reason_detail": (detail.get(b["code"]) or {}).get("detail") or "",
            "main_net": mainnet.get(b["code"]),
            "sealed": 1, "src": b["src"],
        })
    # 扩充涨停：涨幅已超 9.9% 但没封板（封板金额/时间留空，标 sealed=0）
    for r in ext_only:
        pool.append({
            "code": r["code"], "name": r["name"], "lbc": 1, "fund": None,
            "fbt": 0, "lbt": 0, "zbc": 0, "hybk": "", "pct": r["pct"],
            "amount": None,
            "reason": reasons.get(r["code"]) or "涨幅超9.9%（未封板）",
            "sealed": 0, "src": "em",
        })

    broken = []
    for r in zb_raw:
        broken.append({
            "code": str(r.get("c")), "name": r.get("n"),
            "zbc": int(_f(r.get("zbc"), 0) or 0), "pct": _f(r.get("zdp")),
            "hybk": r.get("hybk"),
        })

    zt_rows = [p for p in pool if is_zt_row(p["sealed"], p["pct"])]
    ext_rows = [p for p in pool if is_ext_row(p["sealed"], p["pct"])]
    hot = top_concepts(concepts)          # 炒作口径：一票只算一个主概念
    themes = {
        "zt": aggregate_themes(zt_rows, concepts, broken, boards),
        "ext": aggregate_themes(ext_rows, concepts, broken, boards),
        "zt_hot": aggregate_themes(zt_rows, hot, broken, boards),
        "ext_hot": aggregate_themes(ext_rows, hot, broken, boards),
    }
    if not with_funds:
        for lst in themes.values():
            for t in lst:
                t.update({"pct": None, "main": None, "d3": None, "d5": None, "ratio": None})
    await asyncio.to_thread(_save_board_meta, boards)
    await asyncio.to_thread(_write_snapshot, date, pool=pool, broken=broken,
                            themes=themes, concepts=concepts, boards=boards,
                            total_zt=len(zt_rows) or zt["tc"],
                            total_ext=len(ext_rows), finalized=finalize,
                            write_boards=with_funds)
    log.info("涨停棱镜 %s[%s]：涨停 %d / 扩充 %d / 炸板 %d / 主题 %d%s", date, src,
             len(zt_rows), len(ext_rows), len(broken), len(themes["zt"]),
             "（定稿）" if finalize else "")
    return {"date": date, "src": src, "total": len(zt_rows),
            "ext_total": len(ext_rows), "broken": len(broken),
            "themes": len(themes["zt"]), "ext_themes": len(themes["ext"])}


def _mkt_of(code: str) -> int:
    """按代码猜市场位（1=沪，0=深/北）。"""
    if code[:1] in ("4", "8") or code[:2] in ("92",):
        return 0
    return 1 if code[:1] == "6" else 0


# ============================ 读接口 ===========================================
def _dates(limit: int) -> list[str]:
    with _conn() as c:
        rows = c.execute("SELECT date FROM lu_meta ORDER BY date DESC LIMIT ?",
                         (limit,)).fetchall()
    return [r["date"] for r in rows][::-1]


def _latest_date() -> str | None:
    with _conn() as c:
        r = c.execute("SELECT date FROM lu_meta ORDER BY date DESC LIMIT 1").fetchone()
    return r["date"] if r else None


def composite_rank(series: dict[str, list[int]]) -> dict[str, float]:
    """统计期间「综合排名」强度（0~1，1 = 最强 → 颜色最深）。纯函数，单测覆盖。

    算法：`score = Σ zt_d × 0.9^(距今天数)`——涨停家数按日累加，**越近的交易日权重越高**
    （0.9 的日衰减，10 个交易日前权重约 0.35）。

    再用 `(score / max_score) ** 0.75` 归一，**而不是按名次百分位**：概念有 130+ 个、
    尾部大量 1~2 家的噪音主题，按名次算的话前 15 名会全部挤在 0.95~1.0，
    颜色深浅根本看不出区别（实测）。
    """
    scores: dict[str, float] = {}
    for board, vals in series.items():
        n = len(vals)
        s = 0.0
        for i, v in enumerate(vals):
            w = 0.9 ** (n - 1 - i)
            s += (v or 0) * w
        scores[board] = s
    if not scores:
        return {}
    top = max(scores.values()) or 1.0
    return {b: round((s / top) ** 0.75, 3) for b, s in scores.items()}


def apply_board_counts(rows: list[dict[str, Any]],
                       counts: dict[str, int]) -> list[dict[str, Any]]:
    """把「涨停」数换成**板块口径**，但保持传入的顺序（排序口径只决定顺序）。

    否则主题榜按"炒作"排序时会显示 3 家、个股池却列出 5 只，看起来像数据错乱。
    纯函数，单测覆盖。
    """
    out: list[dict[str, Any]] = []
    for r in rows:
        n = counts.get(r.get("board"))
        out.append({**r, "zt": int(n) if n is not None else int(r.get("zt") or 0)})
    return out


def _board_zt_map(dates: list[str], mode: str) -> dict[tuple[str, str], int]:
    """(日期, 板块) → 该口径的涨停家数；用于把显示数换成板块口径。"""
    if not dates:
        return {}
    q = ",".join("?" * len(dates))
    with _conn() as c:
        rows = c.execute(
            f"SELECT date, board, zt FROM lu_themes WHERE mode=? AND date IN ({q})",
            (mode, *dates)).fetchall()
    return {(r["date"], r["board"]): int(r["zt"] or 0) for r in rows}


def _mode_key(mode: str, sort: str = "hot") -> str:
    """排序口径 → 库里的 mode 键。

    `hot`（按炒作涨停）= 一票一主概念（`*_hot`）；`board`（按板块涨停）= 一票计入所有概念；
    `pct`（按涨幅）沿用板块口径的集合，只是排序时改用板块涨幅。
    """
    m = mode if mode in ("zt", "ext") else "zt"
    return f"{m}_hot" if sort == "hot" else m


def board_levels() -> dict[str, str]:
    """板块代码 → `primary`（成分股 ≥ LEVEL_PRIMARY_MIN）/ `secondary`。

    没有元数据的板块不返回（调用方按"未知"处理，两个等级都保留，别因为缺数据就丢主题）。
    """
    try:
        with _conn() as c:
            rows = c.execute("SELECT board, size FROM lu_board_meta").fetchall()
    except sqlite3.Error:
        return {}
    out: dict[str, str] = {}
    for r in rows:
        if r["size"] is None:
            continue
        out[r["board"]] = "primary" if int(r["size"]) >= LEVEL_PRIMARY_MIN else "secondary"
    return out


def _keep_level(board: str, level: str, levels: dict[str, str]) -> bool:
    if level not in ("primary", "secondary"):
        return True
    lv = levels.get(board)
    return lv is None or lv == level


def _streak_map(dates: list[str], mode: str = "zt") -> dict[str, int]:
    """每个主题在这批日期里的涨停总数（周期表角标的「20 日涨停数」）。"""
    if not dates:
        return {}
    q = ",".join("?" * len(dates))
    with _conn() as c:
        rows = c.execute(
            f"SELECT board, SUM(zt) AS n FROM lu_themes WHERE mode=? AND date IN ({q}) "
            f"GROUP BY board", (mode, *dates)).fetchall()
    return {r["board"]: int(r["n"] or 0) for r in rows}


def _theme_series(boards: list[str], dates: list[str],
                  mode: str = "zt") -> dict[str, dict[str, list[int]]]:
    """按主题取 zt/first_board/lianban/zb 的逐日序列（缺失补 0）。"""
    out: dict[str, dict[str, list[int]]] = {}
    if not boards or not dates:
        return out
    bq = ",".join("?" * len(boards))
    dq = ",".join("?" * len(dates))
    with _conn() as c:
        rows = c.execute(
            f"SELECT date, board, zt, first_board, lianban, zb FROM lu_themes "
            f"WHERE mode=? AND board IN ({bq}) AND date IN ({dq})",
            (mode, *boards, *dates)).fetchall()
    idx = {d: i for i, d in enumerate(dates)}
    for b in boards:
        out[b] = {k: [0] * len(dates) for k in ("zt", "first_board", "lianban", "zb")}
    for r in rows:
        i = idx.get(r["date"])
        if i is None:
            continue
        slot = out.get(r["board"])
        if slot is None:
            continue
        for k in ("zt", "first_board", "lianban", "zb"):
            slot[k][i] = int(r[k] or 0)
    return out


def overview(date: str | None = None, *, days: int = 11, top: int = 15,
             mode: str = "zt", sort: str = "hot",
             level: str = "all") -> dict[str, Any]:
    """周期表 + 主题榜 + 图例分档（一次拿全，页面首屏一个请求）。

    `sort`：hot=按炒作涨停（一票一主概念）/ board=按板块涨停（一票计入所有概念）/
    pct=按涨幅（板块口径的集合，排序改用当日板块涨幅；历史日期没有涨幅 → 回落按涨停数）。
    `level`：all / primary（一级主题，成分股 ≥150）/ secondary（二级主题）。
    """
    mode = _mode_key(mode, sort)
    dates = _dates(KEEP_DAYS)
    if not dates:
        return {"error": "尚未采集到涨停数据", "matrix": [], "themes": [], "dates": []}
    d = date or dates[-1]
    cols = [x for x in dates if x <= d][-days:] or dates[-days:]
    if d not in cols:
        cols.append(d)
        cols.sort()

    # 周期表：每天的 Top N 主题
    dq = ",".join("?" * len(cols))
    with _conn() as c:
        rows = c.execute(
            f"SELECT date, board, name, zt, max_lbc, pct FROM lu_themes "
            f"WHERE mode=? AND date IN ({dq}) ORDER BY date, zt DESC", (mode, *cols)).fetchall()
    by_date: dict[str, list[dict[str, Any]]] = {x: [] for x in cols}
    for r in rows:
        by_date[r["date"]].append({"board": r["board"], "name": r["name"],
                                   "zt": int(r["zt"] or 0), "max_lbc": int(r["max_lbc"] or 0),
                                   "pct": r["pct"]})
    # 主题排除名单（默认排除「央国企改革」）：在算热度和建矩阵之前就剔掉，
    # 这样剩余主题的深浅仍然按"当前可见集合"归一，颜色不会被一个被排除的巨无霸压扁。
    excl = excluded_names()
    levels = board_levels() if level in ("primary", "secondary") else {}
    for x in cols:
        by_date[x] = [t for t in by_date[x]
                      if t["name"] not in excl and _keep_level(t["board"], level, levels)]
    # 按涨幅：有涨幅的日期按涨幅降序（没有涨幅的历史日期回落按涨停数，已在 SQL 里排好）
    if sort == "pct":
        for x in cols:
            if any(t.get("pct") is not None for t in by_date[x]):
                by_date[x] = sorted(by_date[x],
                                    key=lambda t: (-(t.get("pct") or -1e9), -t["zt"]))
    # 顺序已按「排序口径」定好；再把对外显示的涨停数换成**板块口径**（与个股池一致），
    # 并记下 rank 让 build_matrix 别再按 zt 重排。
    base_mode = mode[:-4] if mode.endswith("_hot") else mode
    board_counts = _board_zt_map(cols, base_mode)
    for x in cols:
        fixed = apply_board_counts(
            by_date[x], {b: n for (d0, b), n in board_counts.items() if d0 == x})
        by_date[x] = [{**t, "rank": i} for i, t in enumerate(fixed)]
    per_day = [{"date": x, "themes": by_date[x]} for x in cols]

    # 统计期间的综合排名（颜色深浅）：窗口 = 周期表实际展示的那些交易日
    all_boards = sorted({t["board"] for t in by_date.get(d, [])} | {
        t["board"] for col in per_day for t in col["themes"]} - {None})
    heat = composite_rank({b: v["zt"] for b, v in
                           _theme_series(all_boards, cols, mode).items() if b})

    # 角标统计窗口（右上角数字）：近 7 个交易日涨停总数；扩张/高潮/三共振仍按各自窗口算
    win = dates[-BADGE_DAYS:] if len(dates) >= BADGE_DAYS else dates
    streak = _streak_map(win, base_mode)
    matrix = build_matrix(per_day, top)
    for col in matrix:
        for cell in col:
            if not cell:
                continue
            cell["count_7d"] = streak.get(cell["board"], cell["zt"])
            cell["tier"] = tier_of(cell["zt"])
            cell["type"] = classify_theme(cell["name"])
            cell["heat"] = round(heat.get(cell["board"], 0.0), 3)

    boards_today = [t["board"] for t in by_date.get(d, []) if t["board"]]
    series = _theme_series(list({*boards_today, *[b for b in streak if streak[b] >= 3]}),
                           dates, mode)

    themes = []
    for t in by_date.get(d, []):
        s = series.get(t["board"]) or {}
        zt_series = s.get("zt") or [0]
        n20 = streak.get(t["board"], t["zt"])
        themes.append({
            **t,
            "tier": tier_of(t["zt"]),
            "count_7d": n20,
            "heat": round(heat.get(t["board"], 0.0), 3),
            "expanding": is_expanding(zt_series),
            "climax": is_climax(zt_series),
            "resonance": is_resonance(s.get("first_board") or [0],
                                      s.get("lianban") or [0],
                                      s.get("zb") or [0]),
            "type": classify_theme(t["name"]),
        })

    # 资金四列（只有实时采集的那天才写）
    with _conn() as c:
        fund_rows = c.execute(
            "SELECT board, pct, main, d3, d5, ratio FROM lu_themes WHERE mode=? AND date=?",
            (mode, d)).fetchall()
    funds = {r["board"]: dict(r) for r in fund_rows}
    for t in themes:
        f = funds.get(t["board"]) or {}
        t.update({k: f.get(k) for k in ("pct", "main", "d3", "d5", "ratio")})

    with _conn() as c:
        meta = c.execute("SELECT * FROM lu_meta WHERE date=?", (d,)).fetchone()

    # 统计期间热门主题：把窗口内每个主题的涨停数累加，取前 5（顶部条展示）
    period: dict[str, dict[str, Any]] = {}
    for x in cols:
        for t in by_date.get(x, []):
            if not t.get("board"):
                continue
            slot = period.setdefault(t["board"], {"board": t["board"], "name": t["name"], "zt": 0})
            slot["zt"] += int(t.get("zt") or 0)
    period_top = sorted(period.values(), key=lambda t: -t["zt"])[:5]

    return {
        "date": d,
        "mode": mode,
        "dates": cols,
        "period_top": period_top,
        "matrix": matrix,
        "themes": themes,
        "tiers": TIERS,
        "excluded": sorted(excl),
        "stats": {
            "total_zt": int(meta["total_zt"] or 0) if meta else 0,
            "total_ext": int(meta["total_ext"] or 0) if meta else 0,
            "total_zb": int(meta["total_zb"] or 0) if meta else 0,
            "theme_count": len(themes),
            "finalized": bool(meta["finalized"]) if meta else False,
            "updated_at": meta["updated_at"] if meta else None,
        },
    }


def themes_of(date: str | None = None, mode: str = "zt", sort: str = "hot",
              level: str = "all") -> dict[str, Any]:
    """某一天的主题榜（供"点周期表单元格 → 右侧切到那一天"用）。

    周期表的窗口锚定在页面选的日期上、不跟着点走，所以右侧要单独取这一天的主题榜。
    """
    mode = _mode_key(mode, sort)
    dates = _dates(KEEP_DAYS)
    d = date or (dates[-1] if dates else None)
    if not d:
        return {"error": "尚未采集到涨停数据", "date": None, "themes": []}

    with _conn() as c:
        rows = c.execute(
            "SELECT board, name, zt, zb, max_lbc, lianban, first_board, pct, main, d3, d5, ratio "
            "FROM lu_themes WHERE mode=? AND date=?", (mode, d)).fetchall()
    excl = excluded_names()
    levels = board_levels() if level in ("primary", "secondary") else {}
    rows = [r for r in rows if r["name"] not in excl
            and _keep_level(r["board"], level, levels)]
    if sort == "pct" and any(r["pct"] is not None for r in rows):
        rows.sort(key=lambda r: (-(r["pct"] or -1e9), -int(r["zt"] or 0)))
    else:
        rows.sort(key=lambda r: (-int(r["zt"] or 0), -int(r["max_lbc"] or 0), r["name"] or ""))
    # 显示数统一用板块口径（与个股池列出的家数一致），排序口径只决定顺序
    base_mode = mode[:-4] if mode.endswith("_hot") else mode
    board_counts = {b: n for (d0, b), n in _board_zt_map([d], base_mode).items()}
    rows = [{**dict(r), "zt": board_counts.get(r["board"], int(r["zt"] or 0))} for r in rows]

    win = dates[-BADGE_DAYS:] if len(dates) >= BADGE_DAYS else dates
    streak = _streak_map(win, base_mode)
    boards = [r["board"] for r in rows]
    series = _theme_series(boards, dates, mode)

    themes = []
    for r in rows:
        sr = series.get(r["board"]) or {}
        zt_series = sr.get("zt") or [0]
        themes.append({
            "board": r["board"], "name": r["name"], "zt": int(r["zt"] or 0),
            "max_lbc": int(r["max_lbc"] or 0),
            "tier": tier_of(int(r["zt"] or 0)),
            "count_7d": streak.get(r["board"], int(r["zt"] or 0)),
            "expanding": is_expanding(zt_series),
            "climax": is_climax(zt_series),
            "resonance": is_resonance(sr.get("first_board") or [0],
                                      sr.get("lianban") or [0],
                                      sr.get("zb") or [0]),
            "type": classify_theme(r["name"] or ""),
            "pct": r["pct"], "main": r["main"], "d3": r["d3"], "d5": r["d5"],
            "ratio": r["ratio"],
        })

    with _conn() as c:
        meta = c.execute("SELECT * FROM lu_meta WHERE date=?", (d,)).fetchone()
    return {
        "date": d, "mode": mode, "themes": themes, "excluded": sorted(excl),
        "stats": {
            "total_zt": int(meta["total_zt"] or 0) if meta else 0,
            "total_ext": int(meta["total_ext"] or 0) if meta else 0,
            "total_zb": int(meta["total_zb"] or 0) if meta else 0,
            "theme_count": len(themes),
            "finalized": bool(meta["finalized"]) if meta else False,
            "updated_at": meta["updated_at"] if meta else None,
        },
    }


def prism(theme: str | None = None, date: str | None = None,
          days: int = PRISM_DAYS, mode: str = "zt", sort: str = "hot") -> dict[str, Any]:
    """棱镜图：全市场涨停数（黄线）+ 该主题涨停数（红面积）。"""
    mode = _mode_key(mode, sort)
    dates = _dates(days)
    if not dates:
        return {"dates": [], "total": [], "theme": [], "theme_name": None}
    d = date or dates[-1]
    if d > dates[-1]:
        dates.append(d)
    board = theme or ""
    name = board
    if board:
        with _conn() as c:
            r = c.execute("SELECT name FROM lu_themes WHERE board=? ORDER BY date DESC LIMIT 1",
                          (board,)).fetchone()
        name = r["name"] if r else board
    totals = {r["date"]: int((r["total_ext"] if mode == "ext" else r["total_zt"]) or 0)
              for r in _meta_rows(dates)}
    # 主题线也用**板块口径**：否则棱镜标题会写"3 家"、主题榜写"5 家"，同一页两个数
    base_mode = mode[:-4] if mode.endswith("_hot") else mode
    tseries = _theme_series([board] if board else [], dates, base_mode).items() if board else []
    tmap = {b: v["zt"] for b, v in tseries}
    tvals = tmap.get(board, [0] * len(dates))
    return {
        "dates": dates,
        "total": [totals.get(x, 0) for x in dates],
        "theme": tvals,
        "theme_board": board or None,
        "theme_name": name or None,
        "mode": mode,
    }


def _meta_rows(dates: list[str]) -> list[sqlite3.Row]:
    if not dates:
        return []
    q = ",".join("?" * len(dates))
    with _conn() as c:
        return c.execute(
            f"SELECT date, total_zt, total_ext FROM lu_meta WHERE date IN ({q})",
            dates).fetchall()


def pool(date: str | None = None, *, theme: str | None = None,
         kind: str = "all", mode: str = "zt") -> dict[str, Any]:
    """个股池。kind: all=全部涨停 / zt=该主题涨停 / broken=该主题炸板 / lan=该主题烂板。

    mode=ext（扩充涨停）时把「涨幅 > 9.9% 但没封板」的个股也算进来（`sealed=False`）。
    """
    mode = mode if mode in MODES else "zt"
    d = date or _latest_date()
    if not d:
        return {"error": "尚未采集到涨停数据", "date": None, "items": []}

    kind = (kind or "all").lower()
    if kind == "all":
        kind = "zt"          # 兼容旧参数：不再有"全市场"页，一律按板块口径
    if kind in ("broken", "lan"):
        with _conn() as c:
            rows = c.execute(
                "SELECT code, name, zbc, pct, hybk FROM lu_broken WHERE date=? "
                "ORDER BY zbc DESC", (d,)).fetchall()
        items = [{"code": r["code"], "name": r["name"], "reason": r["hybk"],
                  "lbc": 0, "zbc": int(r["zbc"] or 0), "fund": None,
                  "pct": r["pct"], "kind": "broken", "sealed": False} for r in rows]
        if kind == "lan":
            items = [x for x in items if x["zbc"] >= 3]
        if theme:
            codes = _theme_codes(d, theme)
            items = [x for x in items if x["code"] in codes]
        return {"date": d, "kind": kind, "items": items, "theme": theme, "mode": mode}

    sql = ("SELECT p.code, p.name, p.lbc, p.fund, p.fbt, p.zbc, p.hybk, p.pct, "
           "p.reason, p.sealed FROM lu_pool p WHERE p.date=?")
    args: list[Any] = [d]
    if mode == "zt":
        # 默认口径：封板 ∪ 9.90~10.29 涨幅
        sql += (" AND (p.sealed=1 OR (p.pct IS NOT NULL AND p.pct BETWEEN ? AND ?))")
        args += [ZT_PCT_MIN, ZT_PCT_MAX]
    else:
        # 扩充口径：封板 ∪ 涨幅 > 9.9
        sql += " AND (p.sealed=1 OR (p.pct IS NOT NULL AND p.pct > ?))"
        args.append(EXT_PCT)
    if theme:
        sql += " AND p.code IN (SELECT code FROM lu_stock_concepts WHERE date=? AND board=?)"
        args += [d, theme]
        # 注意：这里**不按主概念过滤**。「炒作涨停」只是排序不同（连板高度→涨幅），
        # 集合与「板块涨停」一致 —— 否则同一板块的主题榜家数和个股池行数会对不上
        # （2026-11-07 用户反馈）。炒作口径只影响周期表/主题榜的**排序**。
    if kind == "hot":
        # 「炒作涨停」= 同一批板块涨停股，但按**炒作强度**排（连板高度 → 涨幅），
        # 而不是按封单额。四个标签页都只看当前选中板块，不再有"全市场"页。
        sql += " ORDER BY p.sealed DESC, p.lbc DESC, p.pct DESC, p.fund DESC"
    else:
        sql += " ORDER BY p.sealed DESC, p.lbc DESC, p.fund DESC, p.pct DESC"
    with _conn() as c:
        rows = c.execute(sql, args).fetchall()
    items = [{"code": r["code"], "name": r["name"], "reason": r["reason"],
              "lbc": int(r["lbc"] or 1), "zbc": int(r["zbc"] or 0), "fund": r["fund"],
              "fbt": int(r["fbt"] or 0), "pct": r["pct"], "hybk": r["hybk"],
              "sealed": bool(r["sealed"]), "kind": "zt"} for r in rows]
    return {"date": d, "kind": kind, "items": items,
            "theme": theme, "mode": mode}


def _theme_codes(date: str, board: str) -> set[str]:
    with _conn() as c:
        rows = c.execute("SELECT code FROM lu_stock_concepts WHERE date=? AND board=?",
                         (date, board)).fetchall()
    return {r["code"] for r in rows}


BRIEF_THEMES = 5          # 弹窗里展示的板块数
BRIEF_STOCKS = 8          # 每个板块列出的个股数


_DISCLAIMER_RE = re.compile(r"\s*[（(]\s*免责声明[：:][^）)]*[）)]\s*")


def _clean_reason(text: str | None) -> str:
    """去掉同花顺 reason_info 末尾那段「（免责声明：…以上市公司公告为准）」。

    这段是数据源自带的，弹窗里每条个股都重复一遍很吵，统一放到页面底部小字说一次。
    纯函数，单测覆盖。
    """
    if not text:
        return ""
    return _DISCLAIMER_RE.sub("", str(text)).strip()


def _top_tags(reasons: list[str], n: int = 3) -> list[str]:
    """从 "光伏边框+液冷铝材+机器人布局" 这类标签串里取出现最多的 n 个关键词。"""
    cnt: dict[str, int] = {}
    for r in reasons:
        for tag in str(r or "").split("+"):
            tag = tag.strip()
            if 1 < len(tag) <= 8:
                cnt[tag] = cnt.get(tag, 0) + 1
    return [k for k, _ in sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))[:n]]


def _rule_brief(themes: list[dict[str, Any]], total: int,
                single: bool = False) -> dict[str, Any]:
    """大模型不可用时的兜底文案：全部由数据拼出来，不编。"""
    names = [t["name"] for t in themes]
    head = names[:3] or ["无"]
    if single and themes:
        t0 = themes[0]
        pct_txt = f"，板块涨幅 {t0['pct']:.2f}%" if t0.get("pct") is not None else ""
        return {
            "headline": f"{t0['name']}今日 {t0['zt']} 家涨停{pct_txt}",
            "summary": f"{t0['name']}是今日热门板块，共 {t0['zt']} 家个股涨停"
                       + (f"，板块涨幅 {t0['pct']:.2f}%" if t0.get("pct") is not None else "")
                       + "。",
            "themes": {t0["name"]: (f"涨停原因集中在 "
                                    f"{'、'.join(_top_tags([st['reason'] for st in t0['stocks']]))} 等方向"
                                    if _top_tags([st["reason"] for st in t0["stocks"]])
                                    else "涨停原因待补充")},
        }
    return {
        "headline": f"{'、'.join(head)}涨停股领衔，成为两市明星板块" if names
                    else "今日无涨停个股",
        "summary": f"今日沪深两市共 {total} 只股票涨停"
                   + (f"，涨停集中在 {'、'.join(names[:5])} 等板块。" if names else "。"),
        "themes": {t["name"]: (f"涨停原因集中在 {'、'.join(_top_tags([st['reason'] for st in t['stocks']]))} 等方向"
                               if _top_tags([st['reason'] for st in t['stocks']])
                               else "涨停原因待补充")
                   for t in themes},
    }


async def brief(date: str | None = None, mode: str = "zt", sort: str = "hot",
                level: str = "all", refresh: bool = False,
                theme: str | None = None) -> dict[str, Any]:
    """「主题猎手-涨停分析」弹窗：一句话解读 + 各板块炒作原因 + 板块内个股与详细涨停原因。

    **本函数不调用任何大模型**（2026-11-07 用户要求：涨停分析不依赖 opencode）。
    这里只负责把数据拼好，并给出一份**规则兜底文案**；真正的"炒作原因"由前端用
    「设置 → 大模型」里配置的 API（Base URL / 模型 / API Key，浏览器直连或后端代理）生成，
    失败时页面直接显示这份规则文案。

    传 `theme`（板块代码）时**只分析这一个板块**（页面弹窗就是这种用法）；
    不传则按当日涨停最集中的前几个板块分析。
    """
    mode_key = _mode_key(mode, sort)
    levels = board_levels() if level in ("primary", "secondary") else {}
    dates = _dates(KEEP_DAYS)
    d = date or (dates[-1] if dates else None)
    if not d:
        return {"error": "尚未采集到涨停数据", "themes": []}

    excl = excluded_names()
    with _conn() as c:
        meta = c.execute("SELECT * FROM lu_meta WHERE date=?", (d,)).fetchone()
        rows = c.execute(
            "SELECT board, name, zt, max_lbc, pct, main FROM lu_themes "
            "WHERE mode=? AND date=? ORDER BY zt DESC",
            (mode_key[:-4] if mode_key.endswith("_hot") else mode_key, d)).fetchall()
    if theme:
        # 明确点名某个板块时，排除名单与等级筛选都让位（用户就是要看它）
        rows = [r for r in rows if r["board"] == theme][:1]
        if not rows and mode_key.endswith("_hot"):
            # 炒作口径下，从不作为"主概念"的宽泛板块（如央国企改革）没有记录，
            # 退回板块口径再查一次，保证点哪个板块都能分析
            mode_key = mode_key[:-4]
            with _conn() as c:
                rows = c.execute(
                    "SELECT board, name, zt, max_lbc, pct, main FROM lu_themes "
                    "WHERE mode=? AND date=? AND board=?",
                    (mode_key, d, theme)).fetchall()
    else:
        rows = [r for r in rows if r["name"] not in excl
                and _keep_level(r["board"], level, levels)][:BRIEF_THEMES]

    total = int(meta["total_ext"] if (meta and str(mode).startswith("ext")) else
                (meta["total_zt"] if meta else 0) or 0)

    themes = []
    for r in rows:
        with _conn() as c:
            st = c.execute(
                "SELECT p.name, p.code, p.lbc, p.main_net, p.reason, p.reason_detail "
                "FROM lu_pool p WHERE p.date=? AND p.code IN "
                "(SELECT code FROM lu_stock_concepts WHERE date=? AND board=?) "
                "ORDER BY p.lbc DESC, p.main_net DESC LIMIT ?",
                (d, d, r["board"], BRIEF_STOCKS)).fetchall()
        themes.append({
            "board": r["board"], "name": r["name"], "zt": int(r["zt"] or 0),
            "pct": r["pct"], "main": r["main"],
            "stocks": [{"name": x["name"], "code": x["code"], "lbc": int(x["lbc"] or 1),
                        "main_net": x["main_net"], "reason": x["reason"],
                        "reason_detail": x["reason_detail"]} for x in st],
        })

    if not themes:
        return {"date": d, "mode": mode_key, "themes": [], "headline": "",
                "summary": "该板块当日没有涨停数据。" if theme else "今日没有可展示的涨停板块。",
                "total": total, "scope": "theme" if theme else "market"}

    rule = _rule_brief(themes, total, single=bool(theme))
    for t in themes:
        t["reason_text"] = rule["themes"].get(t["name"]) or ""
        t["reason_src"] = "rule"
    return {
        "date": d, "mode": mode_key, "total": total,
        "scope": "theme" if theme else "market",
        # 规则兜底文案：前端拿不到大模型配置/调用失败时直接显示它
        "headline": rule["headline"], "summary": rule["summary"],
        "src": "rule",
        "themes": themes,
    }


def health() -> dict[str, Any]:
    try:
        init_db()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    with _conn() as c:
        meta = c.execute("SELECT * FROM lu_meta ORDER BY date DESC LIMIT 1").fetchone()
        dates = c.execute("SELECT COUNT(*) AS n FROM lu_meta").fetchone()["n"]
        themes = c.execute("SELECT COUNT(*) AS n FROM lu_themes").fetchone()["n"]
        pools = c.execute("SELECT COUNT(*) AS n FROM lu_pool").fetchone()["n"]
    return {"db": _DB_FILE, "dates": dates, "themes": themes, "pools": pools,
            "latest": dict(meta) if meta else None}


# ============================ 定时任务 =========================================
def _trade_date(now: dt.datetime | None = None) -> str | None:
    """当前应采集的交易日（YYYYMMDD）；周末与 09:15 前返回 None（交由回补处理）。"""
    now = now or dt.datetime.now()
    if now.weekday() >= 5:
        return None
    if now.strftime("%H:%M") < "09:15":
        return None
    return now.strftime("%Y%m%d")


async def _backfill(days: int = BACKFILL_DAYS) -> None:
    """启动时补齐最近 `days` 个交易日。已有数据的日期跳过。"""
    have = set(_dates(KEEP_DAYS))
    need = max(0, days - len(have))     # 「库内不足 days 个交易日就补够」，不是每次加 days 个
    if need <= 0:
        return
    empties = 0
    now = dt.datetime.now()
    todo: list[str] = []
    probe = now
    while len(todo) < need + 10 and probe > now - dt.timedelta(days=days * 3 + 30):
        if probe.weekday() < 5:
            d = probe.strftime("%Y%m%d")
            if d not in have:
                todo.append(d)
        probe -= dt.timedelta(days=1)
    got = 0
    for d in todo:
        if got >= need:
            break
        try:
            r = await collect(d, with_funds=False)
            if r.get("error"):
                # 空日期可能是节假日，也可能是同花顺限流 —— 隔一会儿重试一次再判死。
                await asyncio.sleep(1.2)
                r = await collect(d, with_funds=False)
            if r.get("error"):
                empties += 1
                log.info("回补 %s 跳过：%s", d, r["error"])
                # 国庆这类连休有 5 个工作日是空的，阈值必须大于它，否则会把好数据也截掉。
                if empties >= 8:
                    log.info("涨停棱镜回补提前结束（连续空日期过多，上游到底了）")
                    break
            else:
                empties = 0
                got += 1
            await asyncio.sleep(0.4)      # 对同花顺温柔一点，别触发风控
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("回补 %s 失败: %s", d, exc)


async def _ensure_funds() -> None:
    """回补只写涨停/主题（历史资金列不采），收尾时给「最新交易日」补一次资金四列。

    clist 只给"当下"的资金，所以只在数据日期足够新（≤4 天，覆盖周末/小长假）时才补，
    否则宁可留空也不把今天的钱标到上周五头上。
    """
    d = _latest_date()
    if not d:
        return
    with _conn() as c:
        r = c.execute("SELECT COUNT(*) AS n FROM lu_themes WHERE date=? AND main IS NOT NULL",
                      (d,)).fetchone()
    if r["n"]:
        return
    try:
        dd = dt.datetime.strptime(d, "%Y%m%d")
    except ValueError:
        return
    if (dt.datetime.now() - dd).days > 4:
        return
    await collect(d, finalize=d != dt.datetime.now().strftime("%Y%m%d"), with_funds=True)


async def run_forever() -> None:
    """盘中每 2 分钟刷当日快照；15:05 定稿；启动补历史。"""
    await asyncio.to_thread(init_db)
    try:
        await _backfill()
        await _ensure_funds()
    except Exception as exc:  # noqa: BLE001
        log.warning("涨停棱镜回补失败: %s", exc)

    while True:
        try:
            now = dt.datetime.now()
            d = _trade_date(now)
            if d:
                finalized = False
                with _conn() as c:
                    r = c.execute("SELECT finalized FROM lu_meta WHERE date=?", (d,)).fetchone()
                    finalized = bool(r and r["finalized"])
                if not finalized:
                    after_close = now.strftime("%H:%M") >= "15:05"
                    await collect(d, finalize=after_close)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("涨停棱镜采集失败: %s", exc)
        await asyncio.sleep(120)
