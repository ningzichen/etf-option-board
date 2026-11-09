"""板块资金采集器：交易时段把概念/行业的主力净流入写进本地库。

- 约 30 秒一轮（每轮约 7 个东财请求），只在连续竞价时段运行；
- 上一轮未跑完直接跳过，避免堆积；
- 任何异常只记日志，绝不影响期权看板与告警链路。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

from services import boardflow as bf
from services import cls_hot, em

log = logging.getLogger("board_collector")

# 概念板块多（约 500 个，5 页），行业约 1 页；一次翻页上限留足余量
_BOARD_FS = (("m:90+t:3", "concept"), ("m:90+t:2", "industry"))
_FIELDS = "f2,f3,f4,f12,f13,f14,f22,f62"
_CYCLE_SEC = 30
_IDLE_SEC = 120

_state: dict = {"cycles": 0, "last_cycle": None, "last_error": None, "boards": 0}

# 5 分钟涨速：库里每天只留一行快照（没有 pct 历史），所以在采集器内存里按轮次记录
# pct，取「5 分钟前那一轮」的 pct 与当前 pct 相减。进程重启后 5 分钟内为 None
# （回落成东财 f22 的 3 分钟涨速，只在启动初期出现）。
_SPEED_WINDOW = 300.0
_speed_hist: dict[str, list[tuple[float, float]]] = {}


def speed5(code: str, pct: float | None, now: float,
           hist: dict | None = None, window: float = _SPEED_WINDOW) -> float | None:
    """用 pct 历史算 5 分钟涨速（%）。返回 None 表示历史不足（刚启动）。纯函数（hist 可注入）。"""
    if pct is None:
        return None
    h = _speed_hist if hist is None else hist
    arr = h.setdefault(code, [])
    while arr and now - arr[0][0] > window * 2:   # 只保留最近 10 分钟
        arr.pop(0)
    cutoff = now - window
    ref = None
    for t, p in reversed(arr):                    # 最新的「至少 5 分钟前」那一轮
        if t <= cutoff:
            ref = p
            break
    try:
        arr.append((now, float(pct)))
    except (TypeError, ValueError):
        return None
    if ref is None:
        return None
    return round(float(pct) - ref, 2)
_busy = False
_prev_ranks: dict[str, int] = {}
_prev_date: str | None = None
_pruned_date: str | None = None


def init() -> None:
    """建表 + 首次裁剪（由 app 在启动时同步调用一次）。"""
    bf.init_db()
    bf.prune()


def state() -> dict:
    """采集器状态（健康接口用）。"""
    return dict(_state)


async def _fetch_type(fs: str, board_type: str) -> list[dict]:
    rows = await em.clist_all(fs, _FIELDS, pz=100, max_page=8, ttl=20.0)
    return bf.parse_rank_rows(rows, board_type)


async def _cycle() -> None:
    global _busy, _prev_ranks, _prev_date, _pruned_date
    if _busy:
        return
    _busy = True
    now = dt.datetime.now()
    date, hhmm = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
    try:
        results = await asyncio.gather(
            *(_fetch_type(fs, t) for fs, t in _BOARD_FS), return_exceptions=True)
        rows: list[dict] = []
        errors = []
        for (fs, btype), res in zip(_BOARD_FS, results):
            if isinstance(res, Exception):
                errors.append(f"{btype}: {res}")
                continue
            if not res:
                errors.append(f"{btype}: 空结果")
                continue
            rows.extend(res)
        if not rows:
            _state["last_error"] = "; ".join(errors) or "空结果"
            log.warning("板块采集无数据，跳过本轮: %s", _state["last_error"])
            return

        # 跨日重置名次基准（重启后第一轮从库里恢复）
        if _prev_date != date:
            _prev_ranks = await asyncio.to_thread(bf.load_prev_ranks, date)
            _prev_date = date

        ranked = bf.assign_ranks_by_type(rows, _prev_ranks)
        mono = asyncio.get_event_loop().time()
        for r in ranked:
            sp = speed5(str(r["code"]), r.get("pct"), mono)
            if sp is not None:
                r["speed"] = sp          # 5 分钟涨速（历史不足时保留 f22 的 3 分钟值）
        n = await asyncio.to_thread(bf.save_cycle, date, hhmm, ranked)
        _prev_ranks = {r["code"]: r["rank"] for r in ranked}

        if _pruned_date != date:
            await asyncio.to_thread(bf.prune, now.date())
            _pruned_date = date

        _state.update({"cycles": _state["cycles"] + 1, "last_cycle": f"{date} {hhmm}",
                       "last_error": ("; ".join(errors) or None), "boards": len(ranked)})
        log.debug("板块采集完成 %s %s：%d 个板块", date, hhmm, n)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — 采集失败不影响主流程
        _state["last_error"] = str(exc)
        log.warning("板块采集失败: %s", exc)
    finally:
        _busy = False


async def run_forever() -> None:
    """随 FastAPI 启动的后台循环。"""
    log.info("板块资金采集器启动")
    # 启动时先建表/裁剪一次，避免查询报 no such table
    try:
        await asyncio.to_thread(init)
        _pruned_date = dt.date.today().isoformat()
    except Exception as exc:  # noqa: BLE001
        log.warning("板块库初始化失败: %s", exc)

    while True:
        try:
            if bf.in_session(dt.datetime.now()):
                await _cycle()
                try:
                    await _etf_cycle()
                except Exception as exc:  # noqa: BLE001 — ETF 失败不拖累板块
                    log.warning("ETF 资金流采集失败: %s", exc)
                try:
                    await _em_cycle(dt.datetime.now())
                except Exception as exc:  # noqa: BLE001 — 4 维失败不拖累其它
                    log.warning("东财 4 维采集失败: %s", exc)
                await asyncio.sleep(_CYCLE_SEC)
            else:
                await asyncio.sleep(_IDLE_SEC)
        except asyncio.CancelledError:
            log.info("板块资金采集器停止")
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("板块采集循环异常: %s", exc)
            await asyncio.sleep(_IDLE_SEC)


# --------------------------------------------------------------------------
# ETF 资金流（9 只期权标的 + 宽基 Top30 + 行业 Top30，30 秒一轮）
# --------------------------------------------------------------------------
_ETF_FS = "b:MK0021,b:MK0022,b:MK0023,b:MK0024"
_etf_universe: list[dict] = []
_etf_universe_date: str | None = None
_ETF_CONCURRENCY = 8


async def _build_etf_universe(date: str) -> list[dict]:
    """每日重建一次采集范围（成交额排序 Top 200 → 分类挑 Top30/30 + 核心 9 只）。"""
    from services.universe import ETFS

    rows: list[dict] = []
    for pn in (1, 2):
        part = await em.clist(_ETF_FS, "f12,f13,f14,f6", pn=pn, pz=100, fid="f6", po=1, ttl=1800.0)
        if not part:
            break
        for it in part:
            code = str(it.get("f12") or "")
            name = str(it.get("f14") or "")
            mk = str(it.get("f13") if it.get("f13") is not None else "")
            if not code or not name:
                continue
            secid = f"{mk}.{code}" if mk in ("0", "1") else ""
            rows.append({"code": code, "name": name, "secid": secid,
                         "amount": it.get("f6") or 0})
        if len(part) < 100:
            break
    picked = bf.select_etf_universe(rows, [e["code"] for e in ETFS])
    have = {p["code"] for p in picked}
    # 核心标的即使被分类过滤掉，也必须采（页面上要能跟期权看板对照）
    for e in ETFS:
        if e["code"] not in have:
            picked.append({"code": e["code"], "secid": e["secid"], "name": e["name"],
                           "type": "broad", "amount": 0})
    log.info("ETF 采集范围：%d 只（列表 %d 行）", len(picked), len(rows))
    return picked


async def _fetch_etf(item: dict) -> dict | None:
    q = await em.stock_get(item["secid"], "f57,f58,f137,f140,f143,f146,f149,f170", ttl=20.0)
    row = bf.parse_etf_quote(item["code"], q)
    if row:
        row["type"] = item.get("type") or "sector"
    return row


async def _etf_speeds(universe: list[dict]) -> dict[str, float]:
    """ETF 涨速（f22）：ulist 批量取，一次覆盖当天采集范围（每 100 只一个请求）。"""
    secids = [it["secid"] for it in universe if it.get("secid")]
    out: dict[str, float] = {}
    for i in range(0, len(secids), 100):
        d = await em.get_json("https://push2.eastmoney.com/api/qt/ulist.np/get",
                              {"fltt": 2, "invt": 2, "secids": ",".join(secids[i:i + 100]),
                               "fields": "f12,f22"}, ttl=20.0)
        for r in ((d or {}).get("data") or {}).get("diff") or []:
            code = str(r.get("f12") or "")
            v = r.get("f22")
            if code and isinstance(v, (int, float)):
                out[code] = float(v)
    return out


async def _etf_cycle() -> None:
    global _etf_universe, _etf_universe_date
    now = dt.datetime.now()
    date, hhmm = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
    if _etf_universe_date != date or not _etf_universe:
        picked = await _build_etf_universe(date)
        # 只拿到核心 9 只（ETF 列表空回包）时不要锁定当天：下一轮继续尝试补齐宽基/行业
        if len(picked) > 9:
            _etf_universe = picked
            _etf_universe_date = date
        elif not _etf_universe:
            _etf_universe = picked
    if not _etf_universe:
        return
    sem = asyncio.Semaphore(_ETF_CONCURRENCY)

    async def one(item):
        async with sem:
            try:
                return await _fetch_etf(item)
            except Exception:  # noqa: BLE001 — 单只失败跳过
                return None

    results = await asyncio.gather(*(one(it) for it in _etf_universe))
    rows = [r for r in results if r]
    if rows:
        mono = asyncio.get_event_loop().time()
        f22: dict[str, float] = {}
        try:
            f22 = await _etf_speeds(_etf_universe)   # 冷启动前 5 分钟的回退值（东财 f22）
        except Exception as exc:  # noqa: BLE001 — 涨速拉不到不影响资金流采集
            log.debug("ETF 涨速批量获取失败: %s", exc)
        for r in rows:
            sp = speed5(str(r["code"]), r.get("pct"), mono)
            r["speed"] = sp if sp is not None else f22.get(r["code"], 0.0)
        await asyncio.to_thread(bf.save_etf_cycle, date, hhmm, rows)


# --------------------------------------------------------------------------
# 东财 4 维资金：指数（30s）+ 行业 Top40（60s）+ 全 A（30s）
# --------------------------------------------------------------------------
_EM_INDEX_INTERVAL = 30
_EM_INDUSTRY_INTERVAL = 60
_EM_CONCEPT_INTERVAL = 60
_em_last = {"index": 0.0, "industry": 0.0, "concept": 0.0, "extra": 0.0}
# 自选/热门补充目标：2 分钟一轮（它们通常不在 Top40 里，不补就永远没有 4 维曲线）
_EM_EXTRA_INTERVAL = 120
_EM_FIELDS = "f57,f58,f140,f143,f146,f149,f170"


async def _em_fetch(secid: str, kind: str, name: str = "") -> dict | None:
    q = await em.stock_get(secid, _EM_FIELDS, ttl=20.0)
    row = bf.parse_em_flow(secid, kind, q, name)
    if row and kind in ("industry", "concept"):
        # 存 BK 代码，查询统一用 BKxxxx（与板块排行、前端下拉一致）
        row["code"] = secid.split(".", 1)[-1]
    return row


async def _em_cycle(now: dt.datetime) -> None:
    from services.universe import INDICES

    date, hhmm = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
    mono = asyncio.get_event_loop().time()
    targets: list[tuple[str, str, str]] = []

    if mono - _em_last["index"] >= _EM_INDEX_INTERVAL:
        _em_last["index"] = mono
        targets += [(i["secid"], "index", i["name"]) for i in INDICES]
        # 历史数据里出现过、universe 里没有的指数（中证2000），保持继续采集
        targets.append(("2.932000", "index", "中证2000"))

    if mono - _em_last["industry"] >= _EM_INDUSTRY_INTERVAL:
        _em_last["industry"] = mono
        try:
            rank = await asyncio.to_thread(bf.read_rank, date, "industry")
        except Exception:
            rank = []
        cfg = await asyncio.to_thread(bf.load_em_config)
        picked = cfg["items"] if cfg.get("customized") and cfg.get("items") else bf.pick_em_industries(rank)
        # 板块 secid 必须是 90.BKxxxx（只传 BK 代码会全部失败并触发重试，拖慢整轮）
        targets += [(f"90.{t['code']}", "industry", t["name"]) for t in picked]

    if mono - _em_last["concept"] >= _EM_CONCEPT_INTERVAL:
        _em_last["concept"] = mono
        try:
            crank = await asyncio.to_thread(bf.read_rank, date, "concept")
        except Exception:
            crank = []
        ccfg = await asyncio.to_thread(bf.load_em_config, "concept")
        cpicked = (ccfg["items"] if ccfg.get("customized") and ccfg["items"]
                   else bf.pick_em_concepts(crank))
        targets += [(f"90.{t['code']}", "concept", t["name"]) for t in cpicked]

    if mono - _em_last["extra"] >= _EM_EXTRA_INTERVAL:
        _em_last["extra"] = mono
        try:
            all_rank = await asyncio.to_thread(bf.read_rank, date)
        except Exception:  # noqa: BLE001
            all_rank = []
        try:
            watch = await asyncio.to_thread(bf.load_watch)
        except Exception:  # noqa: BLE001
            watch = []
        hot_items: list[dict] = []
        try:
            boards = [{"code": r["code"], "name": r["name"], "kind": r.get("type")}
                      for r in all_rank if str(r.get("code", "")).startswith("BK")]
            hot_items = (await cls_hot.hot_boards(boards)).get("items") or []
        except Exception as exc:  # noqa: BLE001
            log.debug("em extra: 热门板块匹配失败 %s", exc)
        targets += [(f"90.{t['code']}", t["kind"], t["name"])
                    for t in bf.extra_em_targets(all_rank, watch, hot_items)]

    if not targets:
        return

    # 同一标的可能同时命中 Top40 / 自选 / 热门：按 secid 去重，别重复请求
    seen: set[str] = set()
    targets = [t for t in targets if not (t[0] in seen or seen.add(t[0]))]
    sem = asyncio.Semaphore(8)

    async def one(t):
        async with sem:
            try:
                return await _em_fetch(*t)
            except Exception:  # noqa: BLE001
                return None

    rows = [r for r in await asyncio.gather(*(one(t) for t in targets)) if r]
    if rows:
        await asyncio.to_thread(bf.save_em_cycle, date, hhmm, rows)
