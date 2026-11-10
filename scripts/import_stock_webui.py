#!/usr/bin/env python3
"""把 stock-webui 已采集的板块/ETF/4 维资金数据导入本项目的 board_flow.db。

背景：本项目的采集器只在交易时段写库，收盘后接通或重建库时历史分钟点是空的，
四个板块页的分时图就没有数据。stock-webui 的采集器已长期在写同一结构的数据，
直接把最近 N 个交易日搬过来最省事。

特性：
- 源库以 **只读** 方式 ATTACH，不改动 stock-webui 的任何数据；
- 幂等：`INSERT OR IGNORE` 为主，已有数据不覆盖（本项目的真实采集点优先）；
- 排行快照只在目标库当天没有该日数据时导入（避免把真实涨跌幅覆盖成 0）；
- ETF 代码去掉 `sh/sz` 前缀，type 由 index/industry 映射为 broad/sector。

用法：
    python3 scripts/import_stock_webui.py --days 10
    python3 scripts/import_stock_webui.py --from 2026-10-20 --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import pathlib
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:                 # 脚本运行时 sys.path[0] 是 scripts/，需补仓库根
    sys.path.insert(0, ROOT)

from paths import data_path  # noqa: E402  — 必须在 sys.path 注入之后导入

TARGET = data_path("board_flow.db")      # 与服务同一口径，尊重 ETF_DATA_DIR
SOURCE = os.getenv("STOCK_WEBUI_DB", "/Users/xinxin/workspace/stock-webui/data/board_flow.db")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="date_from", default="", help="起始日期 YYYY-MM-DD（默认按 --days 推算）")
    p.add_argument("--days", type=int, default=10, help="导入最近 N 个自然日（默认 10）")
    p.add_argument("--dry-run", action="store_true", help="只打印将导入的量，不写入")
    return p.parse_args()


def run_import(target_path: str, source_path: str, start: str, dry_run: bool = False) -> dict:
    """执行导入，返回统计；dry_run 只统计不写。供 CLI 与回归测试共用。"""
    if not os.path.exists(source_path):
        raise FileNotFoundError(source_path)

    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    # uri=True 让连接支持 URI 解析（ATTACH 的只读 URI 依赖此开关，跨平台均生效）
    conn = sqlite3.connect(target_path, timeout=30, uri=True)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    sys.path.insert(0, ROOT)
    from services import boardflow as bf

    conn.executescript(bf._SCHEMA)  # noqa: SLF001 — 复用唯一 schema 定义
    # as_uri() 产出 file:/// 形式的正斜杠 + 百分号编码路径，
    # Windows 反斜杠路径直接拼进 file: URI 会导致 unable to open database
    source_uri = pathlib.Path(source_path).resolve().as_uri()
    conn.execute("ATTACH DATABASE ? AS src", (f"{source_uri}?mode=ro",))

    def count(sql: str) -> int:
        return conn.execute(sql).fetchone()[0]

    plan = {
        "flow_points": count(f"SELECT COUNT(*) FROM src.flow_points WHERE date >= '{start}'"),
        "rank_new_days": count(
            f"SELECT COUNT(*) FROM src.rank_snapshots s WHERE s.date >= '{start}' "
            f"AND s.date NOT IN (SELECT date FROM main.rank_snapshots)"),
        "etf_flow_points": count(f"SELECT COUNT(*) FROM src.etf_flow_points WHERE date >= '{start}'"),
        "etf_rank_new": count(
            f"SELECT COUNT(*) FROM src.etf_rank_snapshots s WHERE s.date >= '{start}' "
            f"AND s.date NOT IN (SELECT date FROM main.etf_rank_snapshots)"),
        "em_flow_points": count(f"SELECT COUNT(*) FROM src.em_flow_points WHERE date >= '{start}'"),
    }
    if dry_run:
        conn.close()
        return {"plan": plan, "dry_run": True}

    conn.execute("INSERT OR IGNORE INTO main.boards(code,name,type) SELECT code,name,type FROM src.boards")
    conn.execute(
        "INSERT OR IGNORE INTO main.flow_points(date,code,time,flow) "
        "SELECT date,code,time,flow FROM src.flow_points WHERE date >= ?", (start,))
    conn.execute(
        "INSERT OR IGNORE INTO main.rank_snapshots(date,code,name,type,flow,pct,rank,prev_rank,ts) "
        "SELECT date, code, name, type, flow, 0, "
        "       ROW_NUMBER() OVER (PARTITION BY date, type ORDER BY flow DESC), "
        "       NULL, date || ' 15:00' "
        "FROM src.rank_snapshots s WHERE s.date >= ? "
        "  AND s.date NOT IN (SELECT date FROM main.rank_snapshots)", (start,))

    conn.execute(
        "INSERT OR IGNORE INTO main.etf_boards(code,name,type) "
        "SELECT substr(code,3), name, CASE type WHEN 'index' THEN 'broad' ELSE 'sector' END "
        "FROM src.etf_boards WHERE code LIKE 'sh%' OR code LIKE 'sz%'")
    conn.execute(
        "INSERT OR IGNORE INTO main.etf_flow_points(date,code,time,main) "
        "SELECT date, substr(code,3), time, flow FROM src.etf_flow_points "
        "WHERE date >= ? AND (code LIKE 'sh%' OR code LIKE 'sz%')", (start,))
    conn.execute(
        "INSERT OR IGNORE INTO main.etf_rank_snapshots(date,code,name,type,flow,pct,rank,ts) "
        "SELECT date, substr(code,3), name, CASE type WHEN 'index' THEN 'broad' ELSE 'sector' END, "
        "       flow, 0, NULL, date || ' 15:00' "
        "FROM src.etf_rank_snapshots s WHERE s.date >= ? "
        "  AND (s.code LIKE 'sh%' OR s.code LIKE 'sz%') "
        "  AND s.date NOT IN (SELECT date FROM main.etf_rank_snapshots)", (start,))

    conn.execute(
        "INSERT OR IGNORE INTO main.em_flow_points(date,code,name,kind,time,inst,large,medium,retail) "
        "SELECT date, code, '', "
        "       CASE WHEN code LIKE 'BK%' OR code LIKE '90.%' THEN 'industry' "
        "            WHEN code = '47.800000' THEN 'market' ELSE 'index' END, "
        "       time, inst_flow, large_flow, medium_flow, retail_flow "
        "FROM src.em_flow_points WHERE date >= ?", (start,))

    conn.commit()
    stats = {t: conn.execute(f"SELECT COUNT(*) FROM main.{t}").fetchone()[0]
             for t in ("boards", "flow_points", "rank_snapshots", "etf_boards",
                       "etf_flow_points", "etf_rank_snapshots", "em_flow_points")}
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM main.flow_points ORDER BY date DESC LIMIT 5")]
    conn.execute("DETACH DATABASE src")
    conn.close()
    return {"plan": plan, "stats": stats, "dates": dates}


def main() -> int:
    args = parse_args()
    start = args.date_from or (dt.date.today() - dt.timedelta(days=args.days)).isoformat()
    print(f"源库 {SOURCE}\n目标 {TARGET}\n起始日期 {start}" + ("\n(dry-run)" if args.dry_run else ""))
    try:
        res = run_import(TARGET, SOURCE, start, dry_run=args.dry_run)
    except FileNotFoundError:
        print(f"[ERR] 源库不存在：{SOURCE}")
        return 1
    for k, v in res["plan"].items():
        print(f"  将导入 {k}: {v}")
    if args.dry_run:
        return 0
    print("\n导入完成，当前目标库统计：")
    for k, v in res["stats"].items():
        print(f"  {k}: {v}")
    print("最近有分时点的日期:", res["dates"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
