"""板块资金本地库（SQLite）：采集器唯一写入，读接口只读。

设计取舍见 `.scratch/board-flow/spec.md` 与 `docs/adr/0003`：
- 单写路径：交易时段由后台采集器写库，页面读库，打开页面不再触发批量抓取；
- 纯函数与存储层分离：时段判定、解析归一、名次、裁剪计划都可离线单测；
- 连接每次加锁打开、显式 close（`with sqlite3.connect()` 只提交不关闭，会泄漏 fd）。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterable

log = logging.getLogger("boardflow")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_DIR = os.path.join(_ROOT, "data")
_DB_FILE = os.path.join(_DATA_DIR, "board_flow.db")
_WATCH_FILE = os.path.join(_DATA_DIR, "board_watch.json")
_ETF_WATCH_FILE = os.path.join(_DATA_DIR, "etf_watch.json")
_EM_CONFIG_FILE = os.path.join(_DATA_DIR, "em_flow_config.json")
_EM_CONCEPT_CONFIG_FILE = os.path.join(_DATA_DIR, "em_flow_concept_config.json")

# 保留窗口（交易日）
KEEP_POINT_DAYS = 30
KEEP_RANK_DAYS = 120

_SESSION_AM = ("09:30", "11:30")
_SESSION_PM = ("13:00", "15:00")

# 「当日资金流」两处口径对齐用（2026-11-05）：
# 排行快照在收盘后可能被更新一版（实测 2026-11-04 半导体 19:08 写回 195.89 亿，
# 而当日分时末端 15:00 是 192.54 亿），详情页「近 20 日」柱与「当日资金分时」图
# 就对不上。凡该交易日快照晚于 _POST_CLOSE_HM 才有更新的，一律改用当日分时
# 最后一个点（且该点须到 _SESSION_LAST 之后，否则说明当天采集中断，保留快照值）。
_POST_CLOSE_HM = "15:30"
_SESSION_LAST = "14:55"

_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS boards (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'concept'
);
CREATE TABLE IF NOT EXISTS rank_snapshots (
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    flow REAL NOT NULL,
    pct REAL NOT NULL DEFAULT 0,
    rank INTEGER,
    prev_rank INTEGER,
    speed REAL NOT NULL DEFAULT 0,
    ts TEXT,
    PRIMARY KEY (date, code)
);
CREATE INDEX IF NOT EXISTS idx_rank_date ON rank_snapshots(date, type, rank);
CREATE TABLE IF NOT EXISTS flow_points (
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    time TEXT NOT NULL,
    flow REAL NOT NULL,
    PRIMARY KEY (date, code, time)
);
CREATE INDEX IF NOT EXISTS idx_points_date ON flow_points(date, code, time);
CREATE TABLE IF NOT EXISTS etf_boards (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'sector'
);
CREATE TABLE IF NOT EXISTS etf_rank_snapshots (
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    flow REAL NOT NULL,
    pct REAL NOT NULL DEFAULT 0,
    rank INTEGER,
    ts TEXT,
    speed REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (date, code)
);
CREATE TABLE IF NOT EXISTS etf_flow_points (
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    time TEXT NOT NULL,
    main REAL NOT NULL,
    super_ REAL NOT NULL DEFAULT 0,
    large REAL NOT NULL DEFAULT 0,
    medium REAL NOT NULL DEFAULT 0,
    small REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (date, code, time)
);
CREATE INDEX IF NOT EXISTS idx_etf_points_date ON etf_flow_points(date, code, time);
CREATE TABLE IF NOT EXISTS em_flow_points (
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'industry',
    time TEXT NOT NULL,
    inst REAL NOT NULL DEFAULT 0,
    large REAL NOT NULL DEFAULT 0,
    medium REAL NOT NULL DEFAULT 0,
    retail REAL NOT NULL DEFAULT 0,
    pct REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (date, code, time)
);
CREATE INDEX IF NOT EXISTS idx_em_points_date ON em_flow_points(date, code, time);
-- 全A逐笔分档直方图（资金流向页）：每分钟 × 金额档 × 买卖两列
-- 存"直方图"而不是最终四档结果，是为了以后调门槛只重算、不重跑逐笔（逐笔不可回补）
CREATE TABLE IF NOT EXISTS tape_flow_bins (
    date TEXT NOT NULL,
    time TEXT NOT NULL,
    bin INTEGER NOT NULL,
    buy REAL NOT NULL DEFAULT 0,
    sell REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (date, time, bin)
);
CREATE INDEX IF NOT EXISTS idx_tape_bins_date ON tape_flow_bins(date, time);
-- 手数档直方图（通达信口径按手数分档，与金额档并存；两张表同一 shape）
CREATE TABLE IF NOT EXISTS tape_flow_lots (
    date TEXT NOT NULL,
    time TEXT NOT NULL,
    bin INTEGER NOT NULL,
    buy REAL NOT NULL DEFAULT 0,
    sell REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (date, time, bin)
);
CREATE INDEX IF NOT EXISTS idx_tape_lots_date ON tape_flow_lots(date, time);
-- Wind 口径的板块主力净流入（逐笔 ≥70 万、剔除开盘竞价，按成分股聚合）
CREATE TABLE IF NOT EXISTS tape_board_points (
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    time TEXT NOT NULL,
    flow REAL NOT NULL,
    PRIMARY KEY (date, code, time)
);
CREATE INDEX IF NOT EXISTS idx_tape_board_date ON tape_board_points(date, time);
CREATE TABLE IF NOT EXISTS tape_flow_meta (
    date TEXT PRIMARY KEY,
    updated_at TEXT,
    stocks_total INTEGER NOT NULL DEFAULT 0,
    stocks_ok INTEGER NOT NULL DEFAULT 0,
    amount REAL NOT NULL DEFAULT 0
);
"""


# --------------------------------------------------------------------------
# 纯函数（可单测：不触网、不读时钟、不写盘）
# --------------------------------------------------------------------------
def _hm(t: dt.time) -> int:
    return t.hour * 60 + t.minute


def _window(hm: int, se: tuple[str, str]) -> bool:
    a, b = se
    start = int(a[:2]) * 60 + int(a[3:])
    end = int(b[:2]) * 60 + int(b[3:])
    return start <= hm <= end


def in_session(now: dt.datetime) -> bool:
    """是否处于 A 股连续竞价时段（工作日 09:30–11:30 / 13:00–15:00，含边界）。"""
    if now.weekday() >= 5:
        return False
    hm = _hm(now.time())
    return _window(hm, _SESSION_AM) or _window(hm, _SESSION_PM)


def _fnum(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_rank_rows(rows: Iterable[dict], board_type: str) -> list[dict]:
    """把东财 clist 行归一成内部结构；坏行丢弃而不是抛异常。

    f12=板块代码、f14=名称、f62=主力净流入（元）、f3=涨跌幅（%）、f22=涨速（%）。
    非 BK 代码、缺名称、资金不可解析的行一律跳过；同代码重复时保留首行。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for it in rows or []:
        if not isinstance(it, dict):
            continue
        code = str(it.get("f12") or "").strip()
        name = str(it.get("f14") or "").strip()
        if not code.startswith("BK") or not name or code in seen:
            continue
        flow = _fnum(it.get("f62"))
        if flow is None:
            continue
        pct = _fnum(it.get("f3"))
        speed = _fnum(it.get("f22"))
        seen.add(code)
        out.append({"code": code, "name": name, "flow": flow,
                    "pct": 0.0 if pct is None else pct,
                    "speed": 0.0 if speed is None else speed,
                    "type": board_type})
    return out


def assign_ranks(rows: Iterable[dict], prev: dict[str, int] | None = None) -> list[dict]:
    """按主力净流入降序编号，并带上与上一轮的名次差（正数=名次上升）。

    prev 缺省或某板块上一轮不在榜上时 prev_rank 为 None，前端显示「新」而不是伪造数字。
    """
    prev = prev or {}
    ordered = sorted(rows or [], key=lambda r: (-float(r.get("flow") or 0), str(r.get("code") or "")))
    out = []
    for i, r in enumerate(ordered, start=1):
        item = dict(r)
        item["rank"] = i
        before = prev.get(str(r.get("code")))
        item["prev_rank"] = before if isinstance(before, int) else None
        out.append(item)
    return out


def assign_ranks_by_type(rows: Iterable[dict], prev: dict[str, int] | None = None) -> list[dict]:
    """分类型各自编号（概念与行业互不干扰），再合并返回。"""
    groups: dict[str, list[dict]] = {}
    for r in rows or []:
        groups.setdefault(str(r.get("type") or "concept"), []).append(r)
    out: list[dict] = []
    for key in sorted(groups):
        out.extend(assign_ranks(groups[key], prev))
    return out


def _in_auction_time(t: str) -> bool:
    """HH:MM 是否落在连续竞价时段（过滤盘后 15:0x、跨午夜 00:00 之类脏点）。"""
    try:
        hm = int(t[:2]) * 60 + int(t[3:5])
    except (TypeError, ValueError):
        return False
    return _window(hm, _SESSION_AM) or _window(hm, _SESSION_PM)


def build_series(points_by_code: dict[str, list[tuple[str, float]]]) -> dict:
    """把各板块的 (时间, 累计净流入) 点拼成同一条时间轴（前值填充 + 缺失留空）。

    纯函数：入参是已按日期取出的点，不做同日校验以外的任何 I/O。
    某板块在轴上出现前的时点为 None（前端断线而不是画到 0）。
    """
    pts = {code: sorted([(str(t), float(v)) for t, v in (rows or []) if _in_auction_time(str(t))],
                        key=lambda x: x[0])
           for code, rows in (points_by_code or {}).items()}
    axis = sorted({str(t) for rows in pts.values() for t, _ in rows})
    series = []
    for code, rows in pts.items():
        lookup = {str(t): float(v) for t, v in rows}
        vals: list[float | None] = []
        last: float | None = None
        for t in axis:
            if t in lookup:
                last = lookup[t]
            vals.append(last)
        series.append({"code": code, "values": vals, "last": last})
    return {"axis": axis, "series": series}


def prune_plan(today: dt.date, point_dates: Iterable[str], rank_dates: Iterable[str],
               keep_points: int = KEEP_POINT_DAYS, keep_rank: int = KEEP_RANK_DAYS) -> dict:
    """算出应删除的日期列表（纯函数，只返回计划，不删数据）。

    日期为 `YYYY-MM-DD`；按「今天往前保留 N 个自然日」近似交易日窗口 —— 窗口只是
    防止库无限增长，宽松几天不影响查询正确性。无法解析的日期一律保留（不误删）。
    """

    def stale(dates: Iterable[str], keep: int) -> list[str]:
        out = []
        for d in dates or []:
            try:
                day = dt.date.fromisoformat(str(d))
            except (TypeError, ValueError):
                continue
            if (today - day).days > keep:
                out.append(str(d))
        return sorted(set(out))

    return {"flow_points": stale(point_dates, keep_points),
            "rank_snapshots": stale(rank_dates, keep_rank)}


# --------------------------------------------------------------------------
# 存储层
# --------------------------------------------------------------------------
def _conn() -> sqlite3.Connection:
    os.makedirs(_DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(_DB_FILE, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def _db():
    with _LOCK:
        conn = _conn()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """老库补列（SQLite 没有 ADD COLUMN IF NOT EXISTS，只能先查 PRAGMA）。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(rank_snapshots)").fetchall()}
    if "speed" not in cols:
        conn.execute("ALTER TABLE rank_snapshots ADD COLUMN speed REAL NOT NULL DEFAULT 0")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(em_flow_points)").fetchall()}
    if "pct" not in cols:
        conn.execute("ALTER TABLE em_flow_points ADD COLUMN pct REAL NOT NULL DEFAULT 0")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(etf_rank_snapshots)").fetchall()}
    if "speed" not in cols:
        conn.execute("ALTER TABLE etf_rank_snapshots ADD COLUMN speed REAL NOT NULL DEFAULT 0")


def init_db() -> None:
    with _db() as conn:
        conn.executescript(_SCHEMA)
        _migrate(conn)


def save_cycle(date: str, hhmm: str, rows: list[dict]) -> int:
    """写入一轮采集：板块元数据 + 排行快照 + 该分钟资金点（全部幂等 upsert）。"""
    if not rows:
        return 0
    ts = f"{date} {hhmm}"
    with _db() as conn:
        conn.executemany(
            "INSERT INTO boards(code,name,type) VALUES(?,?,?) "
            "ON CONFLICT(code) DO UPDATE SET name=excluded.name, type=excluded.type",
            [(r["code"], r["name"], r["type"]) for r in rows])
        conn.executemany(
            "INSERT INTO rank_snapshots(date,code,name,type,flow,pct,rank,prev_rank,speed,ts) "
            "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(date,code) DO UPDATE SET "
            "name=excluded.name, type=excluded.type, flow=excluded.flow, pct=excluded.pct, "
            "rank=excluded.rank, prev_rank=excluded.prev_rank, speed=excluded.speed, ts=excluded.ts",
            [(date, r["code"], r["name"], r["type"], float(r["flow"]), float(r.get("pct") or 0),
              r.get("rank"), r.get("prev_rank"), float(r.get("speed") or 0), ts) for r in rows])
        conn.executemany(
            "INSERT INTO flow_points(date,code,time,flow) VALUES(?,?,?,?) "
            "ON CONFLICT(date,code,time) DO UPDATE SET flow=excluded.flow",
            [(date, r["code"], hhmm, float(r["flow"])) for r in rows])
    return len(rows)


def load_prev_ranks(date: str) -> dict[str, int]:
    """上一轮名次（重启后从库恢复，避免第一轮全部显示「新」）。"""
    with _db() as conn:
        cur = conn.execute("SELECT code, rank FROM rank_snapshots WHERE date=? AND rank IS NOT NULL", (date,))
        return {str(c): int(r) for c, r in cur.fetchall()}


def speed5_map(conn: sqlite3.Connection, table: str, date: str,
               time_col: str = "ts") -> dict[str, float]:
    """5 分钟涨速（%）：用 pct 的变化量近似 —— pct_now - pct_5min_ago。

    东财 f22 是「3 分钟涨速」，用户口径要 5 分钟。4 维表每分钟一行、带 pct（相对昨收），
    两行相减即 5 分钟涨跌幅（小幅度下与价格口径误差 <0.03pp）。
    注意：rank_snapshots / etf_rank_snapshots 每天只保留**一行**最终快照，没有 pct 历史，
    板块/ETF 的 5 分钟涨速由采集器在内存里按 pct 历史算好再写入 speed 列。
    ``time_col``：4 维表用 time。表名只来自内部常量，不走外部输入。
    """
    row = conn.execute(f"SELECT MAX({time_col}) FROM {table} WHERE date=?", (date,)).fetchone()
    last = row[0] if row else None
    if not last:
        return {}
    cut = None
    for fmt in ("%Y-%m-%d %H:%M", "%H:%M"):   # 排行表是完整时间，4 维表只有 HH:MM
        try:
            cut = (dt.datetime.strptime(str(last), fmt)
                   - dt.timedelta(minutes=5)).strftime(fmt)
            break
        except ValueError:
            continue
    if not cut:
        return {}
    cur = {str(c): p for c, p in conn.execute(
        f"SELECT code, pct FROM {table} WHERE date=? AND {time_col}=?", (date, last))}
    past = {str(c): p for c, p in conn.execute(
        f"SELECT t.code, t.pct FROM {table} t JOIN ("
        f"  SELECT code, MAX({time_col}) AS mts FROM {table} WHERE date=? AND {time_col}<=?"
        f"  GROUP BY code) m ON t.code=m.code AND t.{time_col}=m.mts WHERE t.date=?",
        (date, cut, date))}
    out: dict[str, float] = {}
    for code, p in cur.items():
        q = past.get(code)
        if p is None or q is None:
            continue
        try:
            out[code] = round(float(p) - float(q), 2)
        except (TypeError, ValueError):
            continue
    return out


def read_rank(date: str, board_type: str | None = None) -> list[dict]:
    sql = ("SELECT code,name,type,flow,pct,rank,prev_rank,speed,ts FROM rank_snapshots WHERE date=?")
    args: list = [date]
    if board_type:
        sql += " AND type=?"
        args.append(board_type)
    sql += " ORDER BY flow DESC"
    with _db() as conn:
        cur = conn.execute(sql, args)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def read_points(date: str, codes: list[str]) -> dict[str, list[tuple[str, float]]]:
    """按日期 + 板块代码取资金分时点；空入参直接返回空。"""
    codes = [c for c in (codes or []) if c]
    if not codes:
        return {}
    marks = ",".join("?" for _ in codes)
    with _db() as conn:
        cur = conn.execute(
            f"SELECT code, time, flow FROM flow_points WHERE date=? AND code IN ({marks}) ORDER BY time",
            [date, *codes])
        out: dict[str, list[tuple[str, float]]] = {c: [] for c in codes}
        for code, t, flow in cur.fetchall():
            out.setdefault(str(code), []).append((str(t), float(flow)))
    return out


_DATE_TABLES = {"board": "rank_snapshots", "etf": "etf_rank_snapshots", "em": "em_flow_points",
                "tape": "tape_flow_bins"}


def available_dates(limit: int = 60, table: str = "board") -> list[str]:
    """有数据的日期（table: board / etf / em）。表名走白名单，不接受外部任意值。"""
    name = _DATE_TABLES.get(table, "rank_snapshots")
    with _db() as conn:
        cur = conn.execute(f"SELECT DISTINCT date FROM {name} ORDER BY date DESC LIMIT ?", (int(limit),))
        return [row[0] for row in cur.fetchall()]


def last_cycle() -> dict | None:
    with _db() as conn:
        cur = conn.execute("SELECT date, MAX(ts) FROM rank_snapshots GROUP BY date ORDER BY date DESC LIMIT 1")
        row = cur.fetchone()
        if not row:
            return None
        date, ts = row
        cnt = conn.execute("SELECT COUNT(*) FROM rank_snapshots WHERE date=?", (date,)).fetchone()[0]
        pts = conn.execute("SELECT COUNT(*) FROM flow_points WHERE date=?", (date,)).fetchone()[0]
        return {"date": date, "last_ts": ts, "boards": int(cnt), "points": int(pts)}


def counts() -> dict:
    with _db() as conn:
        boards = conn.execute("SELECT COUNT(*) FROM boards").fetchone()[0]
        days = conn.execute("SELECT COUNT(DISTINCT date) FROM rank_snapshots").fetchone()[0]
    return {"boards": int(boards), "days": int(days)}


def prune(today: dt.date | None = None) -> dict:
    """按保留窗口清理旧数据，返回删除的行数。"""
    today = today or dt.date.today()
    with _db() as conn:
        p_dates = [r[0] for r in conn.execute("SELECT DISTINCT date FROM flow_points").fetchall()]
        r_dates = [r[0] for r in conn.execute("SELECT DISTINCT date FROM rank_snapshots").fetchall()]
        etf_p = [r[0] for r in conn.execute("SELECT DISTINCT date FROM etf_flow_points").fetchall()]
        etf_r = [r[0] for r in conn.execute("SELECT DISTINCT date FROM etf_rank_snapshots").fetchall()]
        em_p = [r[0] for r in conn.execute("SELECT DISTINCT date FROM em_flow_points").fetchall()]
        tape_p = [r[0] for r in conn.execute("SELECT DISTINCT date FROM tape_flow_bins").fetchall()]
    plan = prune_plan(today, p_dates, r_dates)
    # ETF / 4 维 与板块同口径：分时 30 天、排行 120 天
    plan["etf_flow_points"] = prune_plan(today, etf_p, etf_p)["flow_points"]
    plan["em_flow_points"] = prune_plan(today, em_p, em_p)["flow_points"]
    plan["etf_rank_snapshots"] = prune_plan(today, etf_r, etf_r)["rank_snapshots"]
    # 逐笔直方图：一天只有 1 万行，留 120 天（逐笔本身补不回来，宁可多留）
    plan["tape_flow_bins"] = prune_plan(today, tape_p, tape_p)["rank_snapshots"]
    plan["tape_flow_lots"] = list(plan["tape_flow_bins"])
    plan["tape_board_points"] = list(plan["tape_flow_bins"])
    removed = {"flow_points": 0, "rank_snapshots": 0, "etf_flow_points": 0,
               "etf_rank_snapshots": 0, "em_flow_points": 0, "tape_flow_bins": 0,
               "tape_flow_lots": 0, "tape_board_points": 0, "tape_flow_meta": 0}
    with _db() as conn:
        for table, dates in plan.items():
            for d in dates:
                cur = conn.execute(f"DELETE FROM {table} WHERE date=?", (d,))
                removed[table] += cur.rowcount or 0
                if table in ("tape_flow_bins", "tape_flow_lots"):
                    removed["tape_flow_meta"] += conn.execute(
                        "DELETE FROM tape_flow_meta WHERE date=?", (d,)).rowcount or 0
    if any(removed.values()):
        log.info("板块库裁剪完成: %s", removed)
    return removed


# --------------------------------------------------------------------------
# 自选板块（JSON，命中 .gitignore 的 data/*.json）
# --------------------------------------------------------------------------
def _is_index_code(code: str) -> bool:
    """指数 / 全 A 的 secid（1.000300、47.800000）。"""
    head, _, tail = str(code or "").partition(".")
    return head.isdigit() and tail.isdigit()


def load_watch() -> list[dict]:
    try:
        with open(_WATCH_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        log.warning("读取自选板块失败，按空处理: %s", exc)
        return []
    out = []
    for it in raw if isinstance(raw, list) else []:
        code = str((it or {}).get("code") or "").strip()
        # 板块 BKxxxx 与指数 secid（1.000300 / 47.800000）都允许进自定义
        if code.upper().startswith("BK") or _is_index_code(code):
            out.append({"code": code, "name": str((it or {}).get("name") or code).strip()})
    return out


def update_watch(add: dict | None = None, remove: str | None = None) -> tuple[list[dict], bool]:
    """一次性完成「读—改—写」（加锁 + 原子替换），返回 (列表, 是否写盘成功)。"""
    with _LOCK:
        items = load_watch()
        if remove:
            items = [it for it in items if it["code"] != remove]
        if add and add.get("code"):
            code = str(add["code"])
            if not any(it["code"] == code for it in items):
                items.append({"code": code, "name": str(add.get("name") or code)})
        ok = save_watch(items)
        return items, ok


def save_watch(items: list[dict]) -> bool:
    # 唯一临时名：多进程/并发写不会互相覆盖半截文件
    tmp = f"{_WATCH_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _WATCH_FILE)
        return True
    except OSError as exc:
        log.warning("写入自选板块失败: %s", exc)
        return False


def read_history(code: str, days: int = 20, until: str | None = None) -> list[dict]:
    """某板块近 N 个交易日的资金流（来自每日最后一轮排行快照）。

    收盘后被更新过的交易日，取值改用当日分时末端（见 `_daily_flow_fix`），
    保证「近 20 日」柱与「当日资金分时」图的终点是同一个数。
    `until` 给历史回看用：只取该日（含）之前的快照，避免看 11-04 时把 11-05 也画进来。
    """
    sql = "SELECT date, flow, pct, rank FROM rank_snapshots WHERE code=?"
    args: list = [code]
    if until:
        sql += " AND date<=?"
        args.append(until)
    sql += " ORDER BY date DESC LIMIT ?"
    args.append(max(1, int(days)))
    with _db() as conn:
        cur = conn.execute(sql, args)
        rows = [{"date": d, "flow": float(f), "pct": float(p or 0), "rank": r}
                for d, f, p, r in cur.fetchall()]
        fix = _daily_flow_fix(conn, [r["date"] for r in rows])
    for r in rows:
        v = fix.get((r["date"], code))
        if v is not None:
            r["flow"] = v
    return list(reversed(rows))


def daily_flow(code: str, date: str, fallback: float | None = None) -> float | None:
    """单个板块某交易日的资金流（与 read_history 同口径，供详情页头部用）。"""
    with _db() as conn:
        fix = _daily_flow_fix(conn, [date])
    return fix.get((date, code), fallback)


def _daily_flow_fix(conn: sqlite3.Connection,
                    dates: Iterable[str]) -> dict[tuple[str, str], float]:
    """盘后有更新过的交易日 → 用当日分时最后一点覆盖资金流。返回 {(date, code): flow}。"""
    uniq = [d for d in dict.fromkeys(dates or []) if d]
    if not uniq:
        return {}
    qs = ",".join("?" * len(uniq))
    late = [d for d, ts in conn.execute(
        f"SELECT date, MAX(ts) FROM rank_snapshots WHERE date IN ({qs}) GROUP BY date", uniq)
        if ts and ts[11:16] > _POST_CLOSE_HM]
    if not late:
        return {}
    qs2 = ",".join("?" * len(late))
    rows = conn.execute(
        f"""SELECT p.date, p.code, p.flow FROM flow_points p
            JOIN (SELECT date, code, MAX(time) AS mt FROM flow_points
                   WHERE date IN ({qs2}) GROUP BY date, code) m
              ON m.date = p.date AND m.code = p.code AND m.mt = p.time
            WHERE p.time >= ?""", [*late, _SESSION_LAST]).fetchall()
    return {(d, c): float(f) for d, c, f in rows}


def align_against(axis: list[str], mapping: dict[str, float]) -> list[float | None]:
    """把另一条序列（时间→值）对齐到给定时间轴，缺失点补 None。纯函数。"""
    return [mapping.get(t) for t in (axis or [])]


def read_rank_range(start: str, end: str, board_type: str | None = None) -> list[dict]:
    """区间内每个交易日的排行快照（一日一行/板块）。

    盘后被更新过的交易日同样改用当日分时末端（见 `_daily_flow_fix`），
    否则「主力资金多日图」的区间累计会和详情页「近 20 日」对不上。
    """
    sql = ("SELECT date, code, name, type, flow, pct, speed FROM rank_snapshots "
           "WHERE date BETWEEN ? AND ?")
    args: list = [start, end]
    if board_type:
        sql += " AND type=?"
        args.append(board_type)
    sql += " ORDER BY date, code"
    with _db() as conn:
        cur = conn.execute(sql, args)
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        fix = _daily_flow_fix(conn, {r["date"] for r in rows})
    for r in rows:
        v = fix.get((r["date"], r["code"]))
        if v is not None:
            r["flow"] = v
    return rows


def aggregate_daily(rows: Iterable[dict], dates: list[str]) -> list[dict]:
    """把区间快照按板块聚合成「区间累计 + 每日序列」（缺失日留 None）。纯函数。

    金额口径：每个交易日的资金流是**当日净流入**，区间累计 = 各日相加（不是末值相减）。
    """
    dates = list(dates or [])
    idx = {d: i for i, d in enumerate(dates)}
    acc: dict[str, dict] = {}
    for r in rows or []:
        code = str(r.get("code") or "")
        if not code:
            continue
        item = acc.get(code)
        if item is None:
            item = acc[code] = {"code": code, "name": r.get("name") or code,
                                "type": r.get("type"), "total": 0.0, "days": 0,
                                "pct": 0.0, "speed": 0.0, "last_date": "",
                                "values": [None] * len(dates)}
        flow = r.get("flow")
        try:
            flow = float(flow)
        except (TypeError, ValueError):
            continue
        item["total"] += flow
        item["days"] += 1
        # 列表里的「涨幅」= 区间内各交易日涨跌幅之和（用户口径：统计区间累计涨幅）
        if str(r.get("date") or "") >= item["last_date"]:
            item["last_date"] = str(r.get("date") or "")
            # 区间页的「涨速」取最新交易日的瞬时涨速
            try:
                item["speed"] = float(r.get("speed") or 0)
            except (TypeError, ValueError):
                pass
        try:
            item["pct"] = round(float(item["pct"] or 0) + float(r.get("pct") or 0), 4)
        except (TypeError, ValueError):
            pass
        i = idx.get(str(r.get("date")))
        if i is not None:
            item["values"][i] = flow
    return sorted(acc.values(), key=lambda x: (-x["total"], x["code"]))


# --------------------------------------------------------------------------
# ETF 资金流：口径分类 + 存储（与板块共用同一个 SQLite，仍由采集器单写）
# --------------------------------------------------------------------------
_ETF_EXCLUDE = ("债", "货币", "现金", "短融", "添益", "日利", "快线", "国债", "政金",
                "恒生", "港股", "纳指", "标普", "日经", "中概", "跨境", "黄金", "原油",
                "商品", "REIT", "证金", "标普", "美国", "德国", "法国", "亚太")
_ETF_BROAD = ("沪深300", "上证50", "中证500", "中证1000", "中证2000", "中证A500", "中证A50",
              "中证800", "中证100", "创业板", "科创50", "科创板50", "科创100", "上证180",
              "深证100", "双创", "北证50", "上证综指", "中证全指")
_ETF_SECTOR = ("半导体", "芯片", "医药", "医疗", "生物", "证券", "银行", "保险", "军工", "国防",
               "新能源", "光伏", "电池", "锂", "汽车", "有色", "稀土", "煤炭", "钢铁", "化工",
               "消费", "食品", "酒", "家电", "地产", "农业", "养殖", "传媒", "游戏", "通信",
               "计算机", "软件", "人工智能", "机器人", "电子", "电力", "环保", "基建", "旅游",
               "教育", "科技", "信息", "机械", "建材", "运输", "物流", "零售", "纺织")


def classify_etf(name: str) -> str | None:
    """ETF 口径分类：broad（宽基）/ sector（行业）/ None（不采）。

    纯函数、按关键词判定；跨境与固收类一律排除（与看板的「境内股票 ETF」定位一致）。
    """
    n = str(name or "")
    if not n or any(k in n for k in _ETF_EXCLUDE):
        return None
    if any(k in n for k in _ETF_BROAD):
        return "broad"
    if any(k in n for k in _ETF_SECTOR):
        return "sector"
    return None


def select_etf_universe(rows: Iterable[dict], core_codes: Iterable[str] = (),
                        broad_n: int = 30, sector_n: int = 30) -> list[dict]:
    """从「按成交额降序」的 ETF 列表里挑采集范围：必采核心 + 宽基 Top N + 行业 Top N。

    返回 [{code, secid, name, type, amount}]；核心标的即使不在 Top 里也一定保留。
    """
    cores = {str(c) for c in core_codes or ()}
    out: list[dict] = []
    seen: set[str] = set()
    broad = sector = 0
    for r in rows or []:
        code = str(r.get("code") or "")
        name = str(r.get("name") or "")
        secid = str(r.get("secid") or "")
        if not code or not name or not secid or code in seen:
            continue
        kind = classify_etf(name)
        take = code in cores
        if not take and kind == "broad" and broad < broad_n:
            take, broad = True, broad + 1
        elif not take and kind == "sector" and sector < sector_n:
            take, sector = True, sector + 1
        if not take:
            continue
        seen.add(code)
        out.append({"code": code, "secid": secid, "name": name,
                    "type": kind or ("broad" if code in cores else "sector"),
                    "amount": float(r.get("amount") or 0)})
    # 核心标的若没出现在列表里（接口没翻到），由调用方另行补齐
    return out


def parse_etf_quote(code: str, data: dict | None) -> dict | None:
    """把 getZJLXData 的快照归一成内部结构；缺主字段直接丢弃（避免写入全 0 假数据）。"""
    if not isinstance(data, dict):
        return None
    def num(key):
        v = data.get(key)
        if isinstance(v, bool) or v in (None, "", "-"):
            return 0.0
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0
    if data.get("f137") in (None, "", "-"):
        return None
    return {"code": code, "name": str(data.get("f58") or code),
            "flow": num("f137"), "pct": num("f170"), "speed": num("f22"),
            "super": num("f140"), "large": num("f143"),
            "medium": num("f146"), "small": num("f149")}


def save_etf_cycle(date: str, hhmm: str, rows: list[dict]) -> int:
    """写入一轮 ETF 资金流（幂等 upsert）。"""
    if not rows:
        return 0
    ts = f"{date} {hhmm}"
    ranked = sorted(rows, key=lambda r: -float(r.get("flow") or 0))
    with _db() as conn:
        conn.executemany(
            "INSERT INTO etf_boards(code,name,type) VALUES(?,?,?) "
            "ON CONFLICT(code) DO UPDATE SET name=excluded.name, type=excluded.type",
            [(r["code"], r["name"], r["type"]) for r in ranked])
        conn.executemany(
            "INSERT INTO etf_rank_snapshots(date,code,name,type,flow,pct,rank,ts,speed) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(date,code) DO UPDATE SET "
            "name=excluded.name, type=excluded.type, flow=excluded.flow, "
            "pct=excluded.pct, rank=excluded.rank, ts=excluded.ts, speed=excluded.speed",
            [(date, r["code"], r["name"], r["type"], float(r["flow"]), float(r["pct"]),
              i, ts, float(r.get("speed") or 0)) for i, r in enumerate(ranked, start=1)])
        conn.executemany(
            "INSERT INTO etf_flow_points(date,code,time,main,super_,large,medium,small) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(date,code,time) DO UPDATE SET "
            "main=excluded.main, super_=excluded.super_, large=excluded.large, "
            "medium=excluded.medium, small=excluded.small",
            [(date, r["code"], hhmm, float(r["flow"]), float(r.get("super") or 0),
              float(r.get("large") or 0), float(r.get("medium") or 0),
              float(r.get("small") or 0)) for r in ranked])
    return len(ranked)


def read_etf_rank(date: str) -> list[dict]:
    with _db() as conn:
        cur = conn.execute(
            "SELECT code,name,type,flow,pct,rank,ts,speed FROM etf_rank_snapshots "
            "WHERE date=? ORDER BY flow DESC",
            (date,))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def read_etf_points(date: str, codes: list[str]) -> dict[str, list[tuple[str, float, dict]]]:
    """取 ETF 资金分时点：{code: [(time, main, 四维dict)]}。"""
    codes = [c for c in (codes or []) if c]
    if not codes:
        return {}
    marks = ",".join("?" for _ in codes)
    with _db() as conn:
        cur = conn.execute(
            f"SELECT code,time,main,super_,large,medium,small FROM etf_flow_points "
            f"WHERE date=? AND code IN ({marks}) ORDER BY time", [date, *codes])
        out: dict[str, list] = {c: [] for c in codes}
        for code, t, main, sup, large, medium, small in cur.fetchall():
            out.setdefault(str(code), []).append(
                (str(t), float(main), {"super": float(sup), "large": float(large),
                                       "medium": float(medium), "small": float(small)}))
    return out


# --------------------------------------------------------------------------
# ETF 自选
# --------------------------------------------------------------------------
def load_etf_watch() -> list[dict]:
    try:
        with open(_ETF_WATCH_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        log.warning("读取自选 ETF 失败，按空处理: %s", exc)
        return []
    out = []
    for it in raw if isinstance(raw, list) else []:
        code = str((it or {}).get("code") or "").strip()
        if code.isdigit() and len(code) == 6:
            out.append({"code": code, "name": str((it or {}).get("name") or code).strip()})
    return out


def update_etf_watch(add: dict | None = None, remove: str | None = None) -> tuple[list[dict], bool]:
    with _LOCK:
        items = load_etf_watch()
        if remove:
            items = [it for it in items if it["code"] != remove]
        if add and add.get("code"):
            code = str(add["code"])
            if not any(it["code"] == code for it in items):
                items.append({"code": code, "name": str(add.get("name") or code)})
        tmp = f"{_ETF_WATCH_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"
        ok = True
        try:
            os.makedirs(_DATA_DIR, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(items, f, ensure_ascii=False, indent=2)
            os.replace(tmp, _ETF_WATCH_FILE)
        except OSError as exc:
            ok = False
            log.warning("写入自选 ETF 失败: %s", exc)
        return items, ok


# --------------------------------------------------------------------------
# 东财 4 维资金（机构/大单/中户/散户）：指数 + 行业 + 概念 + 全 A
# --------------------------------------------------------------------------
EM_DEFAULT_INDUSTRIES = 40
EM_DEFAULT_CONCEPTS = 40

# 不展示也不采集的指数（用户要求去掉）：47.800000 东方财富全A、1.000985 中证全指。
# 库里可能还有历史行，读接口要一并过滤（见 /board/index-rank）。
EM_HIDDEN: dict[str, str] = {"47.800000": "东方财富全A", "1.000985": "中证全指"}

# 指数中文名（含未进 universe.INDICES 但历史数据里有的，如中证2000 2.932000）
INDEX_NAMES: dict[str, str] = {
    "1.000001": "上证指数", "1.000016": "上证50", "1.000300": "沪深300",
    "1.000905": "中证500", "1.000852": "中证1000", "1.000688": "科创50",
    "0.399006": "创业板指", "0.399001": "深证成指",
    "0.899050": "北证50", "2.932000": "中证2000",
    "47.800005": "A股平均股价",
}


def parse_em_flow(code: str, kind: str, data: dict | None,
                  name: str = "") -> dict | None:
    """归一东财 4 维快照：f140 机构(超大单)、f143 大单、f146 中户、f149 散户。

    主字段缺失时返回 None（不写全 0 假数据）。
    """
    if not isinstance(data, dict) or data.get("f140") in (None, "", "-"):
        return None

    def num(key):
        v = data.get(key)
        if isinstance(v, bool) or v in (None, "", "-"):
            return 0.0
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    return {"code": code, "kind": kind, "name": str(data.get("f58") or name or code),
            "inst": num("f140"), "large": num("f143"),
            "medium": num("f146"), "retail": num("f149"),
            "pct": num("f170")}


def save_em_cycle(date: str, hhmm: str, rows: list[dict]) -> int:
    if not rows:
        return 0
    with _db() as conn:
        conn.executemany(
            "INSERT INTO em_flow_points(date,code,name,kind,time,inst,large,medium,retail,pct) "
            "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(date,code,time) DO UPDATE SET "
            "name=excluded.name, kind=excluded.kind, inst=excluded.inst, "
            "large=excluded.large, medium=excluded.medium, retail=excluded.retail, pct=excluded.pct",
            [(date, r["code"], r["name"], r["kind"], hhmm,
              float(r["inst"]), float(r["large"]), float(r["medium"]), float(r["retail"]),
              float(r.get("pct") or 0))
             for r in rows])
    return len(rows)


def read_em_points(date: str, code: str) -> list[tuple[str, float, float, float, float]]:
    with _db() as conn:
        cur = conn.execute(
            "SELECT time,inst,large,medium,retail FROM em_flow_points "
            "WHERE date=? AND code=? ORDER BY time", (date, code))
        return [(str(t), float(a), float(b), float(c), float(d)) for t, a, b, c, d in cur.fetchall()]


def latest_em_flows(date: str) -> list[dict]:
    """每个目标在当日最后一刻的 4 维快照（指数/全A 排行用）。"""
    with _db() as conn:
        cur = conn.execute(
            "SELECT p.code, p.name, p.kind, p.inst, p.large, p.medium, p.retail, p.pct "
            "FROM em_flow_points p JOIN ("
            "  SELECT code, MAX(time) AS t FROM em_flow_points WHERE date=? GROUP BY code"
            ") m ON p.code=m.code AND p.time=m.t"
            " WHERE p.date=?", (date, date))
        sp = speed5_map(conn, "em_flow_points", date, time_col="time")
        return [{"code": c, "name": n, "kind": k, "inst": float(i), "large": float(l),
                 "medium": float(md), "retail": float(r), "pct": float(pc or 0),
                 "speed": sp.get(str(c), 0.0)}
                for c, n, k, i, l, md, r, pc in cur.fetchall()]


def save_tape_day(date: str, rows_amt: Iterable[tuple[str, int, float, float]],
                  rows_lot: Iterable[tuple[str, int, float, float]], meta: dict) -> int:
    """整日覆盖写全A逐笔的两张直方图（每次重算都是全天累计，所以先删后插）。

    rows_*: (HH:MM, 档位下标, 主动买金额, 主动卖金额)；
    meta: updated_at / stocks_total / stocks_ok / amount。
    """
    rows_amt = list(rows_amt)
    rows_lot = list(rows_lot)
    with _db() as conn:
        for table, rows in (("tape_flow_bins", rows_amt), ("tape_flow_lots", rows_lot)):
            conn.execute(f"DELETE FROM {table} WHERE date=?", (date,))
            conn.executemany(
                f"INSERT INTO {table}(date,time,bin,buy,sell) VALUES(?,?,?,?,?)",
                [(date, t, int(b), float(x), float(y)) for t, b, x, y in rows])
        conn.execute(
            "INSERT INTO tape_flow_meta(date,updated_at,stocks_total,stocks_ok,amount) "
            "VALUES(?,?,?,?,?) ON CONFLICT(date) DO UPDATE SET "
            "updated_at=excluded.updated_at, stocks_total=excluded.stocks_total, "
            "stocks_ok=excluded.stocks_ok, amount=excluded.amount",
            (date, str(meta.get("updated_at") or ""), int(meta.get("stocks_total") or 0),
             int(meta.get("stocks_ok") or 0), float(meta.get("amount") or 0)))
    return len(rows_amt) + len(rows_lot)


def read_tape_bins(date: str, dim: str = "amt") -> list[tuple[str, int, float, float]]:
    """某日的逐笔直方图：[(HH:MM, 档位下标, 主动买, 主动卖)]，按时间+档位排序。

    dim: "amt"（金额档，Wind 口径）/ "lot"（手数档，通达信口径）。
    """
    table = "tape_flow_lots" if dim == "lot" else "tape_flow_bins"
    with _db() as conn:
        cur = conn.execute(
            f"SELECT time, bin, buy, sell FROM {table} WHERE date=? ORDER BY time, bin",
            (date,))
        return [(str(t), int(b), float(x), float(y)) for t, b, x, y in cur.fetchall()]


def save_tape_board(date: str, time_hm: str, flows: dict) -> int:
    """写一轮 Wind 口径板块资金（同一 (date,code,time) 幂等 upsert）。"""
    if not flows:
        return 0
    with _db() as conn:
        conn.executemany(
            "INSERT INTO tape_board_points(date,code,time,flow) VALUES(?,?,?,?) "
            "ON CONFLICT(date,code,time) DO UPDATE SET flow=excluded.flow",
            [(date, str(c), time_hm, float(v)) for c, v in flows.items()])
    return len(flows)


def read_tape_board_points(date: str,
                           codes: Iterable[str] | None = None) -> dict[str, list[tuple[str, float]]]:
    """某日各板块的 Wind 口径资金分时：{板块代码: [(HH:MM, flow)]}。"""
    with _db() as conn:
        if codes:
            wanted = [c for c in codes if c]
            if not wanted:
                return {}
            marks = ",".join("?" for _ in wanted)
            cur = conn.execute(
                f"SELECT code, time, flow FROM tape_board_points WHERE date=? AND code IN ({marks}) "
                "ORDER BY time", (date, *wanted))
        else:
            cur = conn.execute(
                "SELECT code, time, flow FROM tape_board_points WHERE date=? ORDER BY time", (date,))
        out: dict[str, list[tuple[str, float]]] = {}
        for code, t, v in cur.fetchall():
            out.setdefault(str(code), []).append((str(t), float(v)))
        return out


def read_tape_board_latest(date: str) -> dict[str, tuple[str, float]]:
    """某日各板块 Wind 口径资金的最新一点：{板块代码: (HH:MM, flow)}。"""
    with _db() as conn:
        cur = conn.execute(
            "SELECT p.code, p.time, p.flow FROM tape_board_points p JOIN ("
            "  SELECT code, MAX(time) AS t FROM tape_board_points WHERE date=? GROUP BY code"
            ") m ON p.code=m.code AND p.time=m.t WHERE p.date=?", (date, date))
        return {str(c): (str(t), float(v)) for c, t, v in cur.fetchall()}


def tape_board_dates(limit: int = 60) -> list[str]:
    with _db() as conn:
        cur = conn.execute(
            "SELECT DISTINCT date FROM tape_board_points ORDER BY date DESC LIMIT ?", (limit,))
        return [str(r[0]) for r in cur.fetchall()]


def read_tape_meta(date: str) -> dict:
    with _db() as conn:
        row = conn.execute(
            "SELECT updated_at, stocks_total, stocks_ok, amount FROM tape_flow_meta WHERE date=?",
            (date,)).fetchone()
    if not row:
        return {}
    return {"updated_at": row[0], "stocks_total": int(row[1] or 0),
            "stocks_ok": int(row[2] or 0), "amount": float(row[3] or 0)}


def read_em_meta(date: str, code: str) -> dict:
    """4 维目标的名称与口径（供详情标题用）。"""
    with _db() as conn:
        row = conn.execute(
            "SELECT name, kind FROM em_flow_points WHERE date=? AND code=? LIMIT 1",
            (date, code)).fetchone()
    return {"name": row[0], "kind": row[1]} if row else {}


def pick_em_boards(rank: list[dict], limit: int) -> list[dict]:
    """按 |主力净流入| 取前 N 个板块（行业/概念共用，纯函数）。"""
    rows = [r for r in rank or [] if str(r.get("code", "")).startswith("BK")]
    rows.sort(key=lambda r: -abs(float(r.get("flow") or 0)))
    return [{"code": r["code"], "name": r.get("name") or r["code"]} for r in rows[:max(1, limit)]]


def pick_em_industries(rank: list[dict], limit: int = EM_DEFAULT_INDUSTRIES) -> list[dict]:
    """按 |主力净流入| 取前 N 个行业作为 4 维采集/展示目标（纯函数）。"""
    return pick_em_boards(rank, limit)


def pick_em_concepts(rank: list[dict], limit: int = EM_DEFAULT_CONCEPTS) -> list[dict]:
    """按 |主力净流入| 取前 N 个概念作为 4 维采集/展示目标（纯函数）。"""
    return pick_em_boards(rank, limit)


def extra_em_targets(rank: list[dict], watch: list[dict],
                     hot_items: list[dict]) -> list[dict]:
    """4 维采集的补充目标：自选板块 + 当日财联社热门匹配到的板块。纯函数。

    Top40 行业/概念是按 |主力净流入| 轮换的，自选与热门里的板块常常不在其中，
    导致「四维资金」里这些标的永远没有曲线。这里把它们也纳入采集（按 BK 去重）。
    """
    kind_of: dict[str, str] = {}
    name_of: dict[str, str] = {}
    for r in rank or []:
        c = str(r.get("code") or "")
        if c.startswith("BK"):
            kind_of[c] = r.get("type") or "concept"
            name_of[c] = r.get("name") or c

    out: list[dict] = []
    seen: set[str] = set()

    def add(code, name, kind):
        c = str(code or "")
        if not c.startswith("BK") or c in seen:
            return
        seen.add(c)
        out.append({"code": c, "name": name or name_of.get(c) or c,
                    "kind": kind or kind_of.get(c) or "concept"})

    for w in watch or []:
        add(w.get("code"), w.get("name"), kind_of.get(str(w.get("code") or "")))
    for it in hot_items or []:
        add(it.get("code"), it.get("name"), it.get("kind"))
    return out


def _em_config_path(kind: str = "industry") -> str:
    """行业与概念各有一份展示配置（互不影响）。"""
    return _EM_CONCEPT_CONFIG_FILE if kind == "concept" else _EM_CONFIG_FILE


def load_em_config(kind: str = "industry") -> dict:
    """板块展示配置（行业/概念各一份）：{customized: bool, items: [{code,name}]}。"""
    try:
        with open(_em_config_path(kind), encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {"customized": False, "items": []}
    except (OSError, ValueError) as exc:
        log.warning("读取 4 维行业配置失败，按默认处理: %s", exc)
        return {"customized": False, "items": []}
    items = []
    for it in (raw or {}).get("items") or []:
        code = str((it or {}).get("code") or "").strip()
        if code.startswith("BK"):
            items.append({"code": code, "name": str((it or {}).get("name") or code)})
    return {"customized": bool((raw or {}).get("customized")), "items": items}


def save_em_config(config: dict, kind: str = "industry") -> bool:
    path = _em_config_path(kind)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except OSError as exc:
        log.warning("写入 4 维板块配置失败: %s", exc)
        return False


def session_minutes() -> list[str]:
    """连续竞价时段的完整分钟轴（09:30–11:30、13:00–15:00，含两端共 242 个点）。"""
    out: list[str] = []
    for start_h, start_m in ((9, 30), (13, 0)):
        h, m = start_h, start_m
        while (h, m) <= ((11, 30) if start_h == 9 else (15, 0)):
            out.append(f"{h:02d}:{m:02d}")
            m += 1
            if m == 60:
                h, m = h + 1, 0
    return out


def densify_axis(times: list[str]) -> list[str]:
    """把稀疏时间轴补成完整分钟轴（只在首末点之间补，不凭空延长）。纯函数。"""
    if not times:
        return []
    full = session_minutes()
    try:
        a = full.index(times[0])
        b = full.index(times[-1])
    except ValueError:
        return list(times)
    return full[a:b + 1] if a <= b else list(times)


def align_to_axis(points: list[tuple[str, float]], axis: list[str]) -> list[float | None]:
    """把稀疏点按「最近前值」填到给定时间轴。纯函数。"""
    lookup = {str(t): v for t, v in points or []}
    out: list[float | None] = []
    last: float | None = None
    for t in axis:
        if t in lookup:
            last = lookup[t]
        out.append(last)
    return out
