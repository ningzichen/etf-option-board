"""板块成分股映射（Wind 口径按板块聚合用）。

- 来源：东财 `clist fs=b:BKxxxx` 拿每个板块的成分股；板块清单来自 `m:90+t:3`（概念）与
  `m:90+t:2`（行业）。
- 成本：约 1000 个板块 × 1~2 页，10 并发约 1~3 分钟 → **只能日级缓存**，不能跟着 10 分钟
  的逐笔扫描重建。
- 缓存：`data/tape_board_map.json`（命中 `.gitignore` 的 `data/*.json`），跨日或手动 force 才重建；
  写失败只记日志，本次用内存结果（不阻塞采集）。

本模块只做「拉清单 → 拉成分股 → 缓存」，不碰 HTTP 层。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os

from paths import data_path
from services import em

log = logging.getLogger("board_map")

_MAP_FILE = data_path("tape_board_map.json")

# 概念/行业板块清单（两套 fs 都是板块列表）
_BOARD_FS = ("m:90+t:3", "m:90+t:2")
CONCURRENCY = 8

_state: dict = {"date": None, "boards": 0, "members": 0, "built_at": None, "last_error": None}
_cache: dict | None = None
_lock = asyncio.Lock()


def _today() -> str:
    return dt.date.today().isoformat()


def load_from_disk(date: str) -> dict | None:
    """读当日缓存（纯函数化的文件读取，异常一律当作没有）。"""
    try:
        with open(_MAP_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("date") != date:
        return None
    boards = data.get("boards")
    if not isinstance(boards, dict) or not boards:
        return None
    return boards


def save_to_disk(date: str, boards: dict) -> bool:
    try:
        os.makedirs(os.path.dirname(_MAP_FILE), exist_ok=True)
        tmp = _MAP_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"date": date, "at": dt.datetime.now().isoformat(timespec="seconds"),
                       "boards": boards}, f, ensure_ascii=False)
        os.replace(tmp, _MAP_FILE)
        return True
    except OSError as exc:
        log.warning("板块映射写盘失败: %s", exc)
        return False


def aggregate(stock_net: dict, boards: dict) -> dict:
    """把「每只股票的主力净额」按板块成分股求和。纯函数（可单测）。

    stock_net: {股票代码: 净额（元）}；boards: {板块代码: [股票代码…]}。
    返回 {板块代码: 净额}，只保留至少命中一只成分股的板块。
    """
    out: dict[str, float] = {}
    for board, members in (boards or {}).items():
        total = 0.0
        hit = False
        for code in members or ():
            v = stock_net.get(str(code))
            if v is None:
                continue
            total += float(v)
            hit = True
        if hit:
            out[str(board)] = total
    return out


async def _board_list() -> list[str]:
    codes: list[str] = []
    for fs in _BOARD_FS:
        rows = await em.clist_all(fs, "f12,f14", pz=100, max_page=8, ttl=600)
        for r in rows:
            code = "" if r.get("f12") is None else str(r.get("f12"))
            if code.startswith("BK") and code not in codes:
                codes.append(code)
    return codes


async def _members(board: str, sem: asyncio.Semaphore) -> tuple[str, list[str]]:
    async with sem:
        try:
            rows = await em.clist_all(f"b:{board}", "f12", pz=100, max_page=8, ttl=600)
        except Exception:  # noqa: BLE001 — 单个板块失败不影响整体
            return board, []
    codes = [str(r.get("f12")) for r in rows if r.get("f12")]
    return board, codes


async def build(force: bool = False) -> dict:
    """取板块映射（命中当日缓存就直接返回）。"""
    global _cache
    async with _lock:
        date = _today()
        if _cache and not force:
            return _cache
        cached = None if force else load_from_disk(date)
        if cached:
            _cache = cached
            _state.update({"date": date, "boards": len(cached),
                           "members": sum(len(v) for v in cached.values()),
                           "built_at": "cache"})
            return cached

        try:
            boards = await _board_list()
            sem = asyncio.Semaphore(CONCURRENCY)
            pairs = await asyncio.gather(*(_members(b, sem) for b in boards))
            mapping = {b: codes for b, codes in pairs if codes}
            if not mapping:
                raise RuntimeError("板块成分股全为空")
            _cache = mapping
            save_to_disk(date, mapping)
            _state.update({"date": date, "boards": len(mapping),
                           "members": sum(len(v) for v in mapping.values()),
                           "built_at": dt.datetime.now().isoformat(timespec="seconds"),
                           "last_error": None})
            log.info("板块映射就绪：%d 个板块 / %d 条成分股关系",
                     len(mapping), sum(len(v) for v in mapping.values()))
            return mapping
        except Exception as exc:  # noqa: BLE001
            _state["last_error"] = str(exc)
            log.warning("板块映射构建失败: %s", exc)
            return _cache or {}


def state() -> dict:
    return dict(_state)
