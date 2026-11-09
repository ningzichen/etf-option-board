"""HTTP API 路由。"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
import time

import logging

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from services import (alerts, board_collector as bcol, boardflow as bf, cls_hot,
                      decision,
                      pinyin as pinyin_svc,
                      deploy as deploy_svc, em, index_levels, kronos, llm, llm_proxy,
                      market_service as ms, monitor, option_playbook as playbook,
                      option_service as osvc, push as push_svc, reversal, strategy, tape_flow as tf,
                      user_settings as usv,
                      vol)
from services.universe import ETF_BY_CODE, ETFS, EXTERNAL, INDICES, NATIONAL_TEAM_ETFS

log = logging.getLogger("api")

router = APIRouter(prefix="/api")

_SIGNAL_FRESH_SEC = 3600   # 变盘信号在左侧列表角标里的时效（秒）；过期自动消失


def _is_fut_opt(code: str) -> bool:
    """股指期权代码（MO/IO/HO）。三者与 ETF 代码不会冲突，前缀判定足够。"""
    from services.index_option import _BY_CODE
    return code.upper() in _BY_CODE


async def _fut_chain(code: str, month: int | None = None) -> dict:
    """股指期权链，补齐与 ETF 链同构的 t_quote/ranked/best。

    前端复用同一套 T 型报价与优选组件，字段名必须与 `osvc.chain` 完全一致；
    区别只在标的名（`etf` 字段沿用前者位置，值为指数）与乘数。
    """
    from services import index_option as idxo
    ch = await idxo.chain(code.upper())
    if ch.get("error"):
        return ch
    fut = ch.get("fut") or {}
    ch["etf"] = {"code": fut.get("code"), "name": fut.get("name"),
                 "secid": fut.get("underlying"), "track": fut.get("track")}
    # 中证500(IC) 没有中金所股指期权：只回现货/期货基差上下文，别再造空链
    if ch.get("options") is False:
        return ch
    ch["t_quote"] = osvc.t_quote(ch, month)
    ch["ranked"] = {
        "call": osvc.rank_candidates(ch, "call", 6),
        "put": osvc.rank_candidates(ch, "put", 6),
    }
    b = await _fut_bias(ch)
    ch["best"] = osvc.best_buys(ch, b)
    return ch


async def _fut_bias(ch: dict) -> dict:
    """读取与列表/详情相同的方向快照，不重复拉链、不按涨跌幅降级择边。"""
    fut = ch.get("fut") or {}
    return await strategy.direction_for(fut.get("code") or "")


@router.get("/health")
async def health():
    return {
        "ok": True,
        "kronos_available": kronos.available(),
        "llm_available": llm.available(),
        "monitor": monitor.status(),
    }


@router.get("/universe")
async def universe():
    # fut_opts：股指期权 IO/HO/MO 与股指期货 IC（无期权链）并列进左栏（前端按 code 是否在此判定数据源）。
    from services.index_option import FUT_OPTS
    return {"etfs": ETFS, "indices": INDICES, "external": EXTERNAL,
            "national_team_etfs": NATIONAL_TEAM_ETFS, "fut_opts": FUT_OPTS}


@router.get("/indices")
async def indices():
    return {"items": await ms.indices_snapshot()}


@router.get("/index/levels")
async def index_levels_view():
    """上证指数关键位（6 只 ETF 共用一份）+ 分时，供指数弹窗。"""
    snap, tr = await asyncio.gather(
        index_levels.snapshot(), ms.trends("1.000001"))
    day = (tr[-1].get("time") or "")[:10] if tr else None
    return {**snap, "trends": tr, "trends_day": day,
            "trends_prev_close": (em.trend_prev_close("1.000001")
                                  if day and day != dt.date.today().isoformat() else None)}


@router.get("/etfs")
async def etfs_quotes():
    """6 只 ETF + 4 个股指品种（IO/HO/MO + 仅期货的 IC）的轻量快照（供左侧列表快速刷新）。

    股指期权按**标的指数**取价（IO→000300、MO→000852、HO→000016），
    并把 code 改写成 IO/HO/MO/IC——行情接口回的 f57 是纯指数代码，前端按品种代码索引。
    """
    from services.index_option import FUT_OPTS
    res = await asyncio.gather(*[ms.quote(e["secid"]) for e in ETFS],
                               *[ms.quote(f["underlying"]) for f in FUT_OPTS],
                               return_exceptions=True)
    etf_part, fut_part = res[:len(ETFS)], res[len(ETFS):]
    items = [q for q in etf_part if isinstance(q, dict) and q]
    for f, q in zip(FUT_OPTS, fut_part):
        if isinstance(q, dict) and q:
            items.append({**q, "code": f["code"], "name": f["name"],
                          "track": f.get("track")})
    return {"items": items}


@router.get("/directions")
async def directions():
    """6 只 ETF + 4 个股指品种的实时综合方向（看多/看空/观望）+ 当日最新变盘点，供左侧列表标签。

    注意：综合方向（strategy.bias_score 多因子合力，阈值 ±15）已把「变盘点」作为加分
    因子并入（reversal.factor，±15，60 分钟衰减），二者同源；此处另返回当日最新变盘点，
    由前端**另打**一个「变盘·看多/看空」角标，标注方向结论的高周期转折依据。
    变盘点为 5m/15m 结构转向（reversal 阈值 65），日内通常仅 1~2 次。
    只回新鲜信号（60 分钟内），避免早盘的信号一直挂到收盘。
    """
    items = await strategy.directions()
    now = dt.datetime.now()
    for code, v in items.items():
        sig = reversal.recent(code, limit=1)
        if not sig:
            continue
        s = sig[-1]
        try:
            ts = dt.datetime.fromisoformat(s["ts"])
        except (KeyError, ValueError, TypeError):
            continue
        if (now - ts).total_seconds() > _SIGNAL_FRESH_SEC:
            continue
        v["signal"] = {"dir": s.get("dir"), "score": s.get("score"), "ts": s.get("ts")}
    return {"items": items}


@router.get("/picks/top")
async def picks_top(n: int = 6, force: int = 0):
    """跨 6 只 ETF 的优选合约横评（性价比 0.6 + 波动 0.4）。"""
    from services import top_picks
    return await top_picks.snapshot(n=n, force=bool(force))


# 左侧迷你走势的整包缓存：冷启动要并发拉 12 条分时（1~6 秒），
# 多端/多标签同时打开时没必要各付一次；15 秒内复用，够新又不影响盘中观感。
_SPARK_CACHE: dict = {"at": 0.0, "data": None}
_SPARK_TTL = 15.0
_spark_lock = asyncio.Lock()


@router.get("/etfs/spark")
async def etfs_spark():
    """6 只 ETF + 4 个股指品种 + 底栏两个大盘指数的日内收盘价序列（供列表迷你走势）。

    板块代码（47.800005 A股平均股价）没有腾讯映射、push2 分时又被反爬打空，
    必须走 trends_or_snapshot 的 emdatah5 快照累积，否则永远是空曲线。
    整包带 15 秒进程内缓存 + 单飞锁：冷启动的 12 条并发只做一次。
    """
    now = time.time()
    hit = _SPARK_CACHE["data"]
    if hit is not None and now - _SPARK_CACHE["at"] < _SPARK_TTL:
        return hit
    async with _spark_lock:
        now = time.time()
        hit = _SPARK_CACHE["data"]
        if hit is not None and now - _SPARK_CACHE["at"] < _SPARK_TTL:
            return hit
        return await _build_spark()


async def _build_spark() -> dict:
    from services.index_option import FUT_OPTS
    targets = [(e["secid"], e["code"]) for e in ETFS] + [
        (f["underlying"], f["code"]) for f in FUT_OPTS
    ] + [
        ("1.000001", "000001"), ("47.800005", "800005"),
    ]

    async def one(secid: str, code: str):
        tr = await em.trends_or_snapshot(secid)
        return code, [x["price"] for x in tr][::3]

    res = await asyncio.gather(*[one(s, c) for s, c in targets], return_exceptions=True)
    out: dict[str, list] = {}
    for r in res:
        if isinstance(r, tuple) and r[1]:
            out[r[0]] = r[1]
    data = {"items": out}
    _SPARK_CACHE.update(at=time.time(), data=data)
    return data


@router.get("/external")
async def external():
    return {"items": await ms.external_snapshot()}


@router.get("/national")
async def national():
    return await ms.national_team_signal()


@router.get("/etf/{code}")
async def etf_context(code: str, klt: int = 1):
    if _is_fut_opt(code):
        return await _fut_context(code)
    etf = next((e for e in ETFS if e["code"] == code), None)
    if not etf:
        return {"error": f"未知标的 {code}"}
    # 串行会是两者耗时相加；并发聚合可减半
    ctx, track = await asyncio.gather(
        ms.etf_context(code, etf["secid"]),
        ms.track_index_state(etf["track"]),
    )
    return {"etf": etf, **ctx, "track": track}


async def _fut_context(code: str) -> dict:
    """股指期权的「标的上下文」，与 `/etf/{code}` **完全同构**。

    现货是**指数**而不是 ETF，但取数链路可以完全复用：`ms.etf_context` 里的行情、
    1 分钟 K、5 分钟 K、分时、资金流（含分钟序列与当日累计快照）、量能、MACD/背离
    对指数同样成立。此前这里手写了一个子集，导致股指期权页缺「量能 / 主力5分 /
    主力累计 / MACD / flow_day」，和 ETF 页面对不上——统一走同一条链路后不会再漂移。
    """
    from services import index_option as idxo
    fut = idxo._BY_CODE.get(code.upper())
    if not fut:
        return {"error": f"未知股指期权品种 {code}"}
    secid = fut["underlying"]
    ctx = await ms.etf_context(fut["code"], secid)
    q = ctx.get("quote") or {}
    return {
        "etf": {"code": fut["code"], "name": fut["name"], "secid": secid,
                "track": fut.get("track"), "kind": "index"},
        # 股指期权的「标的」就是现货指数：underlying 与 quote 同口径（含振幅）
        "underlying": {"kind": "index", **q},
        **ctx,
        "quote": q,
    }


@router.get("/etf/{code}/kline")
async def etf_kline(code: str, klt: int = 1, lmt: int = 240):
    etf = next((e for e in ETFS if e["code"] == code), None)
    if not etf:
        return {"error": f"未知标的 {code}"}
    return {"bars": await ms.kline(etf["secid"], klt=klt, lmt=lmt)}


@router.get("/option/chain/{code}")
async def option_chain(code: str, month: int | None = None):
    if _is_fut_opt(code):
        return await _fut_chain(code, month)
    ch = await osvc.chain(code)
    if ch.get("error"):
        return ch
    ch["t_quote"] = osvc.t_quote(ch, month)
    ch["ranked"] = {
        "call": osvc.rank_candidates(ch, "call", 6),
        "put": osvc.rank_candidates(ch, "put", 6),
    }
    trend = await strategy.direction_for(code)
    ch["best"] = osvc.best_buys(ch, trend)
    return ch


@router.get("/option/surface/{code}")
async def option_surface(code: str, month: int | None = None):
    """波动率曲面：逐月 ATM IV（期限结构）+ 25Δ 偏斜 + 一句话结论。"""
    ch = await osvc.chain(code)
    if ch.get("error"):
        return ch
    s = vol.surface(ch)
    if month and s.get("skew"):
        s["skew"] = vol.skew_25d(ch, month) or s["skew"]
    return {"code": code, "underlying": ch.get("underlying"),
            "stats": ch.get("stats"), **s}


@router.get("/option/playbook/{code}")
async def option_playbook(code: str, direction: str | None = None,
                          iv_pct: float | None = None,
                          month: int | None = None, next: int = 0):
    """多腿策略册：按「方向 × IV 分位」给出带到期损益的组合建议。

    ``direction`` 缺省时复用 ``/directions`` 的实时方向（与推荐卡同源），
    ``iv_pct`` 缺省时取该标的的 20 日 IV 分位。
    """
    ch = await osvc.chain(code)
    if ch.get("error"):
        return ch
    dirn = direction
    if dirn is None:
        try:
            dirn = ((await strategy.directions()).get(code) or {}).get("direction")
        except Exception:  # noqa: BLE001 — 方向不可得时按中性处理，不阻断策略册
            dirn = None
    iv = iv_pct if iv_pct is not None else (ch.get("stats") or {}).get("atm_iv_pctl20")
    out = playbook.suggest(ch, direction=dirn, iv_pct=iv,
                           prefer_next=bool(next), month=month)
    out["surface"] = vol.surface(ch)
    out["stats"] = ch.get("stats")
    out["underlying"] = ch.get("underlying")
    out["front_month"] = ch.get("front_month")
    return out


@router.get("/option/quote")
async def option_quote(secid: str):
    return await osvc.option_quote(secid) or {"error": "无数据"}


@router.get("/index-option/overview")
async def index_option_overview():
    """股指品种（IO/HO/MO 期权 + IC 期货）概览（方向评分 + 建议）。"""
    from services import index_option as idxo
    return await idxo.overview()


@router.get("/index-option/{symbol}")
async def index_option_state(symbol: str):
    """单品种完整包（与 /strategy/{code} 同构，前端复用行情卡）。"""
    from services import index_option as idxo
    return await idxo.bundle(symbol.upper())


@router.get("/option/kline")
async def option_kline(secid: str, klt: int = 1, lmt: int = 240):
    return {"bars": await osvc.option_kline(secid, klt=klt, lmt=lmt)}


@router.get("/option/trends")
async def option_trends(secid: str, ndays: int = 1):
    return {"trends": await osvc.option_trends(secid, ndays=ndays)}


@router.get("/calibration")
async def calibration():
    """自校准参数（合约实盘跟踪回算出的 Δ 偏移/Theta 折扣），前端角标展示。"""
    from services import pick_tracker
    return pick_tracker.calibration()


@router.get("/option/levels")
async def option_levels(code: str, secid: str):
    """合约图三组横线的数据：**合约价**买卖点 + 按 |Δ| 折算成合约价的支撑/压力。

    走 `plan_hint` 的 5 分钟缓存，不跑完整 `analyze`——否则每切一次合约就把策略链路
    重算一遍（链+方向+Kronos），既慢又把接口压力放大好几倍。
    方向与主计划不一致时用 `pullback_plan` 换成回踩入场（反向备选不追突破价）。
    """
    fut = _is_fut_opt(code)
    if fut:
        from services import index_option as idxo
        ch = await idxo.chain(code.upper())
    else:
        ch = await osvc.chain(code)
    if ch.get("error"):
        return ch
    contract = next((c for c in (ch.get("contracts") or []) if c.get("secid") == secid), None)
    if not contract:
        return {"error": f"标的不含该合约 {secid}"}

    asset = ETF_BY_CODE.get(code)
    ctx: dict = {}
    if asset:
        try:
            ctx = await ms.etf_context(code, asset["secid"])
        except Exception:  # noqa: BLE001 — 支撑/压力拿不到不影响买卖点
            ctx = {}

    s_now = (ch.get("underlying") or {}).get("price") or 0
    side = "call" if contract.get("is_call") else "put"
    if fut:
        data = await idxo.bundle(code.upper())
        by_side = data.get("levels_by_side") or {}
        levels = by_side.get(side) or data.get("levels") or {}
    else:
        hint = strategy.plan_hint(code) or {}
        if not hint.get("levels"):
            # 首次（服务刚起 / 还没打开过策略卡）：跑一次 analyze 把点位缓存填上，
            # 之后一律走 plan_hint 的 5 分钟缓存。
            data = await strategy.analyze(code)
            hint = {"levels": data.get("levels") or {},
                    "spot": (data.get("underlying") or {}).get("price")}
        levels = hint.get("levels") or {}
        s_now = hint.get("spot") or s_now
        if levels:
            primary = "call" if levels.get("side") == "long" else "put"
            if side != primary:
                levels = strategy.pullback_plan(strategy.plan_levels(ctx, side)) or levels
    if not levels:
        return {"error": "点位缓存未就绪（等待一次策略分析）"}

    plan = strategy.contract_levels(contract, levels, s_now)
    delta = contract.get("delta") or 0
    last = contract.get("last") or 0

    def px(v: float) -> float:
        """标的价位 → 合约价（与 contract_levels 同一折算公式）。"""
        return round(last + delta * (v - s_now), 4)

    ok = bool(delta and s_now and last)
    return {"code": code, "secid": secid, "side": side,
            "contract": contract.get("name"), "spot": s_now,
            "plan": plan,
            "support": [px(v) for v in (ctx.get("support") or [])] if ok else [],
            "resistance": [px(v) for v in (ctx.get("resistance") or [])] if ok else [],
            "levels": levels, "multiplier": contract.get("multiplier") or 10000}


@router.get("/strategy/{code}")
async def strategy_view(code: str, kronos: int = 1, klt: int = 1):
    if _is_fut_opt(code):
        # 股指期权自带一套 bundle（与 strategy.analyze 同构，含 levels_by_side）
        from services import index_option as idxo
        out = await idxo.bundle(code.upper())
        # 前端按 best.recommended 自动选中合约；bundle 只有 candidates，
        # 这里补一份同构的 best，避免详情区一直空白等用户手点。
        if not out.get("error"):
            c0 = (out.get("candidates") or [None])[0]
            out["best"] = {"recommended": c0} if c0 else {}
        return out
    return await strategy.analyze(code, with_kronos=bool(kronos), klt=klt)


@router.get("/predict")
async def predict(secid: str, klt: int = 5, lookback: int = 120, pred_len: int = 20,
                  temperature: float = 1.0, top_p: float = 0.9):
    bars = await ms.kline(secid, klt=klt, lmt=max(lookback + 5, 200))
    if len(bars) < 30:
        return {"error": f"K线不足：{len(bars)} 根"}
    freq = {1: "1m", 3: "3m", 5: "5m", 15: "15m", 30: "30m", 101: "1d"}.get(klt, "5m")
    pred = await kronos.predict(bars, lookback=lookback, pred_len=pred_len,
                                temperature=temperature, top_p=top_p, frequency=freq)
    pred["bars"] = bars[-60:]
    if "predictions" in pred:
        pred["summary"] = kronos.summarize(pred, bars[-1]["close"])
    return pred


@router.get("/llm/{code}/context")
async def llm_context(code: str, klt: int = 1):
    """只生成行情提示词，模型调用由页面选择直连或临时代理。"""
    try:
        bundle = await strategy_view(code, kronos=1, klt=klt)
        out = bundle if bundle.get("error") else llm.browser_context(bundle)
    except Exception:  # noqa: BLE001
        log.exception("生成大模型决策上下文失败")
        out = {"error": "行情上下文暂不可用，请稍后重试"}
    return JSONResponse(out, headers={"Cache-Control": "no-store"})


@router.post("/llm/proxy")
async def llm_proxy_request(request: Request):
    """密钥只存在本次请求内存；不经设置存储、日志、缓存或旧模型的 CLI 回退。"""
    def reply(data: dict) -> JSONResponse:
        return JSONResponse(data, headers={"Cache-Control": "no-store"})

    # 浏览器跨站页面不能借用本机代理；Vite 的同源 /api 转发仍可正常使用。
    if request.headers.get("sec-fetch-site") in ("cross-site", "same-site"):
        return reply({"error": "请从看板页面发起模型代理请求"})
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
        return reply({"error": "代理请求需使用 application/json"})
    try:
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 1024 * 1024:
                return reply({"error": "模型请求内容过大"})
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        # 手动解析，避免参数校验响应或日志回显带密钥的原始输入。
        return reply({"error": "模型请求内容无效"})

    async def disconnected():
        # 请求体已读完，只等待 ASGI 断开事件。is_disconnected 的零等待轮询在
        # BaseHTTPMiddleware 下会先被取消，漏掉真正的断开，导致上游继续生成。
        while True:
            message = await request.receive()
            if message["type"] == "http.disconnect":
                return

    upstream = asyncio.create_task(llm_proxy.forward(data))
    watcher = asyncio.create_task(disconnected())
    try:
        done, _ = await asyncio.wait((upstream, watcher), return_when=asyncio.FIRST_COMPLETED)
        return reply(upstream.result() if upstream in done else {"error": "模型请求已取消"})
    except Exception:  # noqa: BLE001
        return reply({"error": "模型代理请求失败，请稍后重试"})
    finally:
        for task in (upstream, watcher):
            if not task.done():
                task.cancel()
        await asyncio.gather(upstream, watcher, return_exceptions=True)


@router.post("/llm/{code}")
async def llm_decision(code: str, klt: int = 1):
    bundle = await strategy.analyze(code, with_kronos=True, klt=klt)
    if bundle.get("error"):
        return bundle
    decision = await llm.decide(bundle)
    return {"bundle_bias": bundle.get("bias"), "signals": bundle.get("signals"),
            "decision": decision, "ts": bundle.get("ts")}


@router.get("/alerts")
async def alerts_history(limit: int = 50):
    return {"items": alerts.history(limit)}


@router.post("/admin/test-alert")
async def admin_test_alert():
    """发 4 条【测试】告警走真实 SSE 通道，用于验证浏览器通知/声音/弹窗，
    以及「变盘告警 → 左栏角标即时刷新」链路（第 4 条 type=reversal）。

    会进告警历史与列表（标题带【测试】前缀便于区分）。"""
    now = dt.datetime.now().isoformat(timespec="seconds")
    samples = [
        {"type": "volume_surge", "level": "warn", "dir": "neutral",
         "etf_code": "510300", "etf_name": "沪深300ETF华泰柏瑞",
         "title": "【测试】量能放大 5.6x · 510300",
         "text": "这是一条测试告警：若已授权，应弹出 Chrome 系统通知并提示音。"},
        {"type": "divergence", "level": "warn", "dir": "bear",
         "etf_code": "588000", "etf_name": "科创50ETF华夏",
         "title": "【测试】15分钟MACD 顶背离 · 588000",
         "text": "第二条测试告警：15 分钟 K 线动能与价格背离（方向看空示例）。"},
        {"type": "national_team", "level": "critical", "dir": "bull",
         "etf_name": "宽基ETF",
         "title": "【测试】国家队护盘信号增强（2/2 只净流入）",
         "text": "第三条测试告警（critical）：宽基ETF近5分钟主力合计净流入 4.40亿，护盘评分 72。",
         "etfs": [
             {"code": "510300", "name": "沪深300ETF华泰柏瑞", "main_inflow": 320000000,
              "main_total": 4.1, "pct": 0.82, "surge": 2.1, "vol_ratio": 2.1},
             {"code": "510050", "name": "上证50ETF华夏", "main_inflow": 120000000,
              "main_total": 1.2, "pct": 0.41, "surge": None, "vol_ratio": 1.4},
         ]},
        # 变盘告警：前端收到后会立即刷新 /api/directions（左栏角标）与当前标的详情，
        # 用来验证「信号出现即时可见」而不必等真实变盘。
        {"type": "reversal", "level": "critical", "dir": "bear",
         "etf_code": "510300", "etf_name": "沪深300ETF华泰柏瑞",
         "title": "【测试】510300 看空变盘信号 66分",
         "text": "第四条测试告警（reversal）：收到后左栏应变盘角标与中栏策略立即刷新，无需手动刷新页面。"},
    ]
    for s in samples:
        await alerts.publish({**s, "test": True, "ts": now})
    return {"ok": True, "count": len(samples)}


@router.post("/monitor/cycle")
async def monitor_once():
    await monitor.cycle()
    return {"ok": True, "status": monitor.status()}


@router.post("/admin/restart")
async def admin_restart():
    """热重启后端（代码更新后生效）：先优雅关闭 Kronos 子进程与网络客户端，
    再以非零码退出，由 LaunchAgent KeepAlive 自动拉起（约 5 秒）。

    仅内网/Tailscale 可达；调用方会看到连接重置，属正常。注意：若服务是手动
    `bash run.sh` 起的（非 launchd），退出后不会自动拉起，需手动再起。"""
    await kronos.shutdown()
    await em.close()
    os._exit(42)  # noqa: unreachable，launchd 视非零退出为崩溃并拉起


@router.post("/admin/deploy")
async def admin_deploy(payload: dict | None = None):
    """手动触发自部署（与 Agent 写 data/.deploy_token 等价）：先做 py_compile
    校验，可选重建前端，然后热重启。立即返回，部署在后台进行，约 1-2 分钟后生效。
    body 可选 {"build_frontend": bool}。"""
    build = bool((payload or {}).get("build_frontend", False))

    async def _bg():
        await asyncio.sleep(2)  # 先让响应发出
        await deploy_svc.deploy_now(build_frontend=build, reason="api手动触发")

    asyncio.create_task(_bg())
    return {"ok": True, "msg": "部署已触发，稍后生效（前端构建约需 1 分钟）"}


@router.get("/reversal/signals")
async def reversal_signals(code: str = Query(...)):
    """当日该标的的变盘信号（供分时图打标记）。"""
    return {"items": reversal.recent(code)}


@router.get("/alerts/stream")
async def alerts_stream():
    q = alerts.subscribe()

    async def gen():
        try:
            # 先补发最近若干条（标记 backfill，前端只入列表、不弹窗不出声）
            for a in reversed(alerts.history(10)):
                yield f"data: {json.dumps({**a, 'backfill': True}, ensure_ascii=False)}\n\n"
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            alerts.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.get("/expiry")
async def expiry_view():
    """合约到期日历：当月 / 下月 / 随后两个季月的到期日与倒计时、临期级别。

    零新数据源：月份序列与「到期月第 4 个星期三」均由纯决策模块算出。
    """
    today = dt.date.today()
    rows = decision.expiry_calendar(today)
    for row in rows:
        row["level"] = decision.near_expiry_level(row["days_left"])
    return {"today": today.isoformat(), "items": rows,
            "critical_days": decision.CRITICAL_DAYS,
            "warning_days": decision.WARNING_DAYS}


@router.get("/settings")
async def settings_view():
    """用户设置：总资金与单笔风险%（存 data/settings.json，不进 git 历史）。"""
    return usv.view(usv.load())


@router.put("/settings")
async def settings_update(payload: dict):
    """写入用户设置。非法输入拒绝并给出原因，不落盘。"""
    settings, errors = usv.parse(payload)
    if errors:
        return {"error": "；".join(errors), "errors": errors}
    if not usv.save(settings):
        return {"error": "设置写入失败"}
    return usv.view(settings)


# --------------------------------------------------------------------------
# 告警远程推送（Server酱 / Bark / 企业微信；密钥存 data/push_settings.json 不进 git）
# --------------------------------------------------------------------------
@router.get("/push/settings")
async def push_settings_view():
    """推送配置：密钥只回掩码。"""
    return push_svc.view(push_svc.load())


@router.put("/push/settings")
async def push_settings_update(payload: dict):
    """写入推送配置。key/url 留空或为掩码时保留原值；非法输入拒绝并给出原因。"""
    merged = push_svc.merge_secret(payload, push_svc.load())
    cfg, errors = push_svc.parse_config(merged)
    if errors:
        return {"error": "；".join(errors), "errors": errors}
    if not push_svc.save(cfg):
        return {"error": "推送设置写入失败"}
    return push_svc.view(cfg)


@router.post("/push/test")
async def push_test():
    """按当前已保存配置发一条【测试】推送，验证渠道连通性。"""
    ok, msg = await push_svc.send_test()
    return {"ok": ok, "msg": msg}


# --------------------------------------------------------------------------
# 板块资金（本地库只读 + 采集器状态；数据由 board_collector 在交易时段写入）
# --------------------------------------------------------------------------
@router.get("/board/health")
async def board_health():
    """采集器状态与库里最新一轮的时间，前端用它判断「交易中 / 最后更新时间」。"""
    try:
        last = await asyncio.to_thread(bf.last_cycle)
        counts = await asyncio.to_thread(bf.counts)
    except Exception as exc:  # noqa: BLE001 — 库未初始化/损坏时降级，不抛 500
        last, counts = None, {"boards": 0, "days": 0}
        log.warning("板块库状态读取失败: %s", exc)
    return {
        "trading": bf.in_session(dt.datetime.now()),
        "last": last,
        "counts": counts,
        "collector": bcol.state(),
        "retention": {"points_days": bf.KEEP_POINT_DAYS, "rank_days": bf.KEEP_RANK_DAYS},
    }


@router.get("/board/rank")
async def board_rank(type: str = Query("concept",
                                       description="concept / industry / watch"),
                     date: str | None = Query(None, description="YYYY-MM-DD，缺省为最近一个有数据的交易日"),
                     mode: str = Query("east", description="east（东财 f62）/ wind（逐笔 ≥70 万）")):
    """板块资金排行（只读本地库，不会触发任何上游请求）。

    库为空时返回 `items=[]` 与 `error` 提示，前端显示空态而不是 500。
    """
    btype = type if type in ("concept", "industry", "watch", "all") else "concept"
    want_watch = btype == "watch"
    if not date:
        try:
            dates = await asyncio.to_thread(bf.available_dates, 1)
        except Exception:  # noqa: BLE001 — 库未初始化时按今日空态返回
            dates = []
        date = dates[0] if dates else dt.date.today().isoformat()

    watch = await asyncio.to_thread(bf.load_watch)
    watch_codes = {w["code"] for w in watch}

    try:
        if want_watch or btype == "all":
            rows = await asyncio.to_thread(bf.read_rank, date)
            if want_watch:
                rows = [r for r in rows if r["code"] in watch_codes]
        else:
            rows = await asyncio.to_thread(bf.read_rank, date, btype)
    except Exception as exc:  # noqa: BLE001 — 库损坏/未初始化一律降级
        return {"date": date, "type": btype, "items": [], "error": f"板块库读取失败: {exc}"}

    flow_src = "east"
    wind_time = None
    fallback = None
    if mode == "wind":
        try:
            wind = await asyncio.to_thread(bf.read_tape_board_latest, date)
        except Exception:  # noqa: BLE001
            wind = {}
        if wind:
            flow_src = "wind"
            for r in rows:
                hit = wind.get(r["code"])
                if hit:
                    wind_time = wind_time or hit[0]
                    r["flow"] = hit[1]
                    r["flow_src"] = "wind"
                else:
                    r["flow_src"] = "east"      # 指数 / 未覆盖的板块仍用东财
            # 口径变了名次必须重算；上一轮名次是东财口径的，不做「名次变化」
            for t in ("concept", "industry"):
                part = [r for r in rows if r.get("type") == t]
                part.sort(key=lambda x: (-float(x.get("flow") or 0), str(x.get("code"))))
                for i, r in enumerate(part, start=1):
                    r["rank"] = i
                    r["prev_rank"] = None
        else:
            fallback = "no_wind"

    for r in rows:
        r["watch"] = r["code"] in watch_codes
        r["py"] = pinyin_svc.initials(r.get("name") or "")
        if r.get("prev_rank") is not None:
            r["rank_delta"] = int(r["prev_rank"]) - int(r["rank"]) if r.get("rank") else None
        else:
            r["rank_delta"] = None

    today = dt.date.today().isoformat()
    out = {"date": date, "type": btype, "items": rows, "is_today": date == today,
           "trading": bf.in_session(dt.datetime.now()),
           "watch_count": len(watch_codes),
           "mode": "wind" if flow_src == "wind" else "east",
           "flow_src": flow_src, "wind_time": wind_time, "fallback": fallback}
    if not rows:
        out["error"] = ("还没有自选板块，点列表右侧星标即可加入"
                        if want_watch and not watch_codes
                        else "暂无采集数据（交易时段每 30 秒采集一次）")
    return out


@router.get("/board/cls-hot")
async def board_cls_hot(date: str | None = Query(None),
                       force: bool = Query(False, description="跳过 5 分钟缓存，直接回源财联社（刷新按钮用）")):
    """财联社「热门板块」：热门行业/概念板块 + 盘面异动 → 模糊匹配本地板块。

    只做名称匹配，不写库；拉不到数据时 `items=[]` + `error`，前端回退到按资金排序。
    """
    if not date:
        try:
            dates = await asyncio.to_thread(bf.available_dates, 1)
        except Exception:  # noqa: BLE001
            dates = []
        date = dates[0] if dates else dt.date.today().isoformat()
    try:
        rows = await asyncio.to_thread(bf.read_rank, date)
    except Exception as exc:  # noqa: BLE001
        return {"date": date, "items": [], "error": f"板块库读取失败: {exc}"}

    boards = [{"code": r["code"], "name": r["name"], "kind": r.get("type"),
               "flow": r.get("flow"), "pct": r.get("pct"), "speed": r.get("speed") or 0}
              for r in rows if str(r.get("code", "")).startswith("BK")]
    res = await cls_hot.hot_boards(boards, force=force)
    items = []
    for it in res.get("items") or []:
        items.append({"code": it["code"], "name": it["name"], "type": it.get("kind"),
                      "flow": it.get("flow"), "pct": it.get("pct"), "speed": it.get("speed") or 0,
                      "cls_name": it.get("cls_name"), "source": it.get("source")})
    return {"date": date, "items": items,
            "error": res.get("error"),
            "updated": dt.datetime.fromtimestamp(res.get("at") or 0).strftime("%H:%M:%S")}


@router.get("/board/watch")
async def board_watch():
    items = await asyncio.to_thread(bf.load_watch)
    for it in items:
        it["py"] = pinyin_svc.initials(it.get("name") or "")
    return {"items": items}


@router.post("/board/watch")
async def board_watch_add(code: str = Query(...), name: str = Query("")):
    """加入自选板块（幂等）。板块 BKxxxx 与指数 secid（1.000300 / 47.800000）都可以加。"""
    code = (code or "").strip()
    is_board = code.upper().startswith("BK")
    is_index = bool(re.fullmatch(r"\d+\.\d+", code))
    if not (is_board or is_index):
        return {"error": "代码必须是 BK 板块或指数 secid"}
    items, ok = await asyncio.to_thread(
        bf.update_watch, {"code": code, "name": (name or code).strip()}, None)
    if not ok:
        return {"error": "自选板块写入失败"}
    return {"items": items}


@router.delete("/board/watch")
async def board_watch_remove(code: str = Query(...)):
    items, ok = await asyncio.to_thread(bf.update_watch, None, code)
    if not ok:
        return {"error": "自选板块写入失败"}
    return {"items": items}


@router.get("/board/series")
async def board_series(codes: str = Query("", description="逗号分隔的板块代码，缺省取当前口径前 8"),
                       date: str | None = Query(None),
                       type: str = Query("concept", description="codes 缺省时按此口径取前 8"),
                       mode: str = Query("east", description="east / wind")):
    """板块当日资金分时（多线，已按统一时间轴对齐）。只读本地库。"""
    if not date:
        try:
            dates = await asyncio.to_thread(bf.available_dates, 1)
        except Exception:  # noqa: BLE001
            dates = []
        date = dates[0] if dates else dt.date.today().isoformat()

    wanted = [c.strip() for c in codes.split(",") if c.strip()]
    try:
        rank = await asyncio.to_thread(bf.read_rank, date)
    except Exception as exc:  # noqa: BLE001
        return {"date": date, "axis": [], "series": [], "error": f"板块库读取失败: {exc}"}

    watch = {w["code"] for w in await asyncio.to_thread(bf.load_watch)}
    meta = {r["code"]: r for r in rank}
    # 指数 / 全 A 不在板块排行里，用名称表补中文名（否则图例只显示 secid）
    for code, name in bf.INDEX_NAMES.items():
        meta.setdefault(code, {"name": name, "type": "index"})
    if not wanted:
        pool = [r for r in rank if r["type"] == type] or rank
        wanted = [r["code"] for r in pool[:8]]

    try:
        bk_codes = [c for c in wanted if c.startswith("BK")]
        idx_codes = [c for c in wanted if not c.startswith("BK")]
        if mode == "wind":
            points = await asyncio.to_thread(bf.read_tape_board_points, date, bk_codes) \
                if bk_codes else {}
            points = {c: v for c, v in points.items() if v}
            flow_src = "wind" if points else "east"
            if flow_src == "east":       # 该日没有 Wind 数据：回落东财
                points = await asyncio.to_thread(bf.read_points, date, bk_codes) \
                    if bk_codes else {}
                fallback = "no_wind"
            else:
                fallback = None
        else:
            points = await asyncio.to_thread(bf.read_points, date, bk_codes) if bk_codes else {}
            flow_src, fallback = "east", None
        # 指数 / 全 A：库里只有 4 维点，主力口径用「机构 + 大单」合成
        for code in idx_codes:
            pts = await asyncio.to_thread(bf.read_em_points, date, code)
            points[code] = [(t, inst + large) for t, inst, large, _m, _r in pts]
    except Exception as exc:  # noqa: BLE001
        return {"date": date, "axis": [], "series": [], "error": f"分时数据读取失败: {exc}"}

    aligned = bf.build_series(points)
    for s in aligned["series"]:
        r = meta.get(s["code"]) or {}
        s["name"] = r.get("name") or s["code"]
        s["type"] = r.get("type")
        s["watch"] = s["code"] in watch
        s["pct"] = r.get("pct")
        s["final_flow"] = s.get("last")
    points_n = sum(len(v) for v in points.values())
    out = {"date": date, "axis": aligned["axis"], "series": aligned["series"],
           "codes": wanted, "points": points_n,
           "mode": flow_src, "flow_src": flow_src, "fallback": fallback}
    if points_n < 2:
        out["error"] = "当日分时点不足（服务启动后从下一分钟开始累积）"
    return out


@router.get("/board/dates")
async def board_dates(limit: int = Query(60, ge=1, le=240)):
    """可回看的日期（有排行快照的交易日）。"""
    try:
        dates = await asyncio.to_thread(bf.available_dates, limit)
    except Exception:  # noqa: BLE001
        dates = []
    return {"dates": dates, "today": dt.date.today().isoformat()}


@router.get("/board/detail")
async def board_detail(code: str = Query(..., description="BK 板块代码"),
                       date: str | None = Query(None)):
    """板块详情抽屉：一次拿全六块（分时、成分股、近 20 日、对标指数、研报热度）。

    各块失败互不影响：失败的块返回 null 并在 errors 里标注原因，前端逐块降级。
    """
    code = (code or "").strip().upper()
    if not code.startswith("BK"):
        return {"error": "code 必须是 BK 板块代码", "code": code}

    if not date:
        try:
            dates = await asyncio.to_thread(bf.available_dates, 1)
        except Exception:  # noqa: BLE001
            dates = []
        date = dates[0] if dates else dt.date.today().isoformat()

    out: dict = {"code": code, "date": date, "errors": {}}

    # 1) 元信息（名称/口径/涨跌幅/主力净流入）—— 来自当日排行快照
    try:
        rows = await asyncio.to_thread(bf.read_rank, date)
        row = next((r for r in rows if r["code"] == code), None)
        if row:
            out.update({"name": row["name"], "type": row["type"],
                        "flow": row["flow"], "pct": row["pct"], "rank": row["rank"]})
            # 头部「主力净流入」与 20 日柱/当日分时同口径（盘后被更新过的日期取分时末端）
            out["flow"] = await asyncio.to_thread(bf.daily_flow, code, date, row["flow"])
    except Exception as exc:  # noqa: BLE001
        out["errors"]["meta"] = str(exc)

    # 2) 当日分时（本地库）
    try:
        points = await asyncio.to_thread(bf.read_points, date, [code])
        aligned = bf.build_series(points)
        out["series"] = {"axis": aligned["axis"],
                         "values": (aligned["series"][0]["values"] if aligned["series"] else [])}
        if len(aligned["axis"]) < 2:
            out["errors"]["series"] = "当日分时点不足（服务启动后从下一分钟开始累积）"
    except Exception as exc:  # noqa: BLE001
        out["series"] = None
        out["errors"]["series"] = str(exc)

    # 3) 近 20 个交易日资金流（本地库）
    try:
        out["history"] = await asyncio.to_thread(bf.read_history, code, 20, date)
    except Exception as exc:  # noqa: BLE001
        out["history"] = None
        out["errors"]["history"] = str(exc)

    # 4+5) 成分股资金排行与环境指数分时并发抓（两者互不依赖，串行会白白多等一个往返）
    today = dt.date.today().isoformat()

    async def _no_index():
        return None

    idx_task = em.fflow("1.000001") if date == today else _no_index()
    gathered = await asyncio.gather(em.board_stocks(code, limit=20), idx_task, return_exceptions=True)
    stocks_res, idx = gathered[0], gathered[1]

    if isinstance(stocks_res, Exception):
        out["stocks"] = []
        out["errors"]["stocks"] = str(stocks_res)
        stocks: list[dict] = []
    else:
        stocks = stocks_res or []
        out["stocks"] = stocks
        if not stocks:
            out["errors"]["stocks"] = "成分股接口未返回数据"

    if date != today:
        out["vs_index"] = None
        out["errors"]["vs_index"] = "历史日期不回补指数分时"
    elif isinstance(idx, Exception):
        out["vs_index"] = None
        out["errors"]["vs_index"] = str(idx)
    else:
        mapping: dict[str, float] = {}
        for line in (idx or {}).get("klines") or []:
            parts = line.split(",")
            if len(parts) >= 2 and len(parts[0]) >= 16:
                mapping[parts[0][-5:]] = float(parts[1] or 0)
        s_axis = (out.get("series") or {}).get("axis") or []
        # 板块当日还没累积点时，退化成只用指数时间轴（板块一侧留空）
        axis = s_axis or sorted(mapping)
        out["vs_index"] = {"name": "上证指数", "axis": axis,
                           "board": (out.get("series") or {}).get("values") or [None] * len(axis),
                           "index": bf.align_against(axis, mapping),
                           "index_day": (idx or {}).get("day")}
        if not mapping:
            out["errors"]["vs_index"] = "指数分时暂无数据"

    # 6) 研报热度：成分股前 3 的近 90 天研报数（并发，失败按 0 计）
    try:
        top = stocks[:3]
        hits = await asyncio.gather(*(em.report_hits(s["code"]) for s in top), return_exceptions=True)
        items = [{"code": s["code"], "name": s["name"],
                  "hits": (h if isinstance(h, int) else 0)} for s, h in zip(top, hits)]
        out["report_heat"] = {"days": 90, "count": sum(i["hits"] for i in items), "top": items}
    except Exception as exc:  # noqa: BLE001
        out["report_heat"] = None
        out["errors"]["report_heat"] = str(exc)

    return out


@router.get("/board/multi")
async def board_multi(start: str | None = Query(None, description="YYYY-MM-DD"),
                      end: str | None = Query(None),
                      type: str = Query("concept", description="concept / industry / all")):
    """区间累计排行 + 每日序列（主力资金多日图）。只读本地库，一次请求拿全。

    type=all 返回概念+行业合并（前端「自定义」页签需要跨类型查名字与累计）。
    """
    btype = type if type in ("concept", "industry", "all") else "concept"
    try:
        dates = await asyncio.to_thread(bf.available_dates, 120)
    except Exception:  # noqa: BLE001
        dates = []
    if not dates:
        return {"start": start or "", "end": end or "", "dates": [], "items": [],
                "error": "暂无采集数据（交易时段每 30 秒采集一次）"}

    # 默认最近 5 个交易日（dates 为倒序）
    asc = sorted(dates)
    end = end or asc[-1]
    start = start or (asc[-5] if len(asc) >= 5 else asc[0])
    if start > end:
        start, end = end, start
    window = [d for d in asc if start <= d <= end]

    try:
        rows = await asyncio.to_thread(bf.read_rank_range, start, end,
                                       None if btype == "all" else btype)
    except Exception as exc:  # noqa: BLE001
        return {"start": start, "end": end, "dates": window, "items": [],
                "error": f"板块库读取失败: {exc}"}

    items = bf.aggregate_daily(rows, window)
    for it in items:
        it["py"] = pinyin_svc.initials(it.get("name") or "")
    return {"start": start, "end": end, "type": btype, "dates": window,
            "items": items, "available_dates": asc,
            "error": None if items else "该区间没有数据（可能还没采集到这些交易日）"}


# --------------------------------------------------------------------------
# ETF 资金流（本地库只读）
# --------------------------------------------------------------------------
@router.get("/etf-flow/rank")
async def etf_flow_rank(scope: str = Query("all", description="all / watch"),
                        date: str | None = Query(None)):
    if not date:
        try:
            dates = await asyncio.to_thread(bf.available_dates, 1, "etf")
        except Exception:  # noqa: BLE001
            dates = []
        date = dates[0] if dates else dt.date.today().isoformat()
    try:
        rows = await asyncio.to_thread(bf.read_etf_rank, date)
    except Exception as exc:  # noqa: BLE001
        return {"date": date, "items": [], "error": f"ETF 资金流读取失败: {exc}"}

    watch = await asyncio.to_thread(bf.load_etf_watch)
    watch_codes = {w["code"] for w in watch}
    if scope == "watch":
        rows = [r for r in rows if r["code"] in watch_codes]
    elif scope == "hot":
        # 热门 = |主力净流入| 前 30（与全部排行区分，页面上更聚焦）
        rows = sorted(rows, key=lambda r: -abs(float(r.get("flow") or 0)))[:30]
    for r in rows:
        r["watch"] = r["code"] in watch_codes
        r["py"] = pinyin_svc.initials(r.get("name") or "")
    out = {"date": date, "scope": scope, "items": rows, "watch_count": len(watch_codes),
           "is_today": date == dt.date.today().isoformat(),
           "trading": bf.in_session(dt.datetime.now())}
    if not rows:
        out["error"] = ("还没有自选 ETF，点列表右侧星标即可加入" if scope == "watch" and not watch_codes
                        else "暂无 ETF 采集数据（交易时段每 30 秒采集一次）")
    return out


@router.get("/etf-flow/series")
async def etf_flow_series(codes: str = Query(""), date: str | None = Query(None)):
    if not date:
        try:
            dates = await asyncio.to_thread(bf.available_dates, 1, "etf")
        except Exception:  # noqa: BLE001
            dates = []
        date = dates[0] if dates else dt.date.today().isoformat()
    wanted = [c.strip() for c in codes.split(",") if c.strip()]
    try:
        rank = await asyncio.to_thread(bf.read_etf_rank, date)
    except Exception as exc:  # noqa: BLE001
        return {"date": date, "axis": [], "series": [], "error": str(exc)}
    meta = {r["code"]: r for r in rank}
    if not wanted:
        wanted = [r["code"] for r in rank[:8]]

    try:
        raw = await asyncio.to_thread(bf.read_etf_points, date, wanted)
    except Exception as exc:  # noqa: BLE001
        return {"date": date, "axis": [], "series": [], "error": str(exc)}

    aligned = bf.build_series({code: [(t, main) for t, main, _ in pts] for code, pts in raw.items()})
    # ETF 历史点较稀疏（采集有跳点），补成完整分钟轴：半小时刻度/竖线才能落在整点上
    dense_axis = bf.densify_axis(aligned["axis"])
    if dense_axis and dense_axis != aligned["axis"]:
        for s in aligned["series"]:
            pts = [(t, main) for t, main, _ in raw.get(s["code"], [])]
            s["values"] = bf.align_to_axis(pts, dense_axis)
        aligned["axis"] = dense_axis
    for s in aligned["series"]:
        s["name"] = (meta.get(s["code"]) or {}).get("name") or s["code"]
    points_n = sum(len(v) for v in raw.values())
    out = {"date": date, "axis": aligned["axis"], "series": aligned["series"], "points": points_n}
    if points_n < 2:
        out["error"] = "当日分时点不足（服务启动后从下一分钟开始累积）"
    return out


@router.get("/etf-flow/4dim")
async def etf_flow_4dim(code: str = Query(...), date: str | None = Query(None)):
    """单只 ETF 的 4 维资金累计曲线（机构/大单/中户/散户）。

    取数优先级：
    1) 东财分钟资金流（push2 fflow kline，含 5 档：主力/小/中/大/超大）——当日有真实 4 维分时；
    2) 本地库（采集器累积，导入的历史只有主力一列）；
    3) 都拿不到时回查一次实时快照，供前端展示四个数字。
    """
    if not date:
        try:
            dates = await asyncio.to_thread(bf.available_dates, 1, "etf")
        except Exception:  # noqa: BLE001
            dates = []
        date = dates[0] if dates else dt.date.today().isoformat()

    today = dt.date.today().isoformat()

    def _secid() -> str:
        return f"{'1' if code.startswith('5') else '0'}.{code}"

    # 1) 东财分钟 5 档资金（f51 时间, f52 主力, f53 小单, f54 中单, f55 大单, f56 超大单）
    if date == today:
        try:
            ff = await em.fflow(_secid(), ttl=20.0)
            klines = (ff or {}).get("klines") or []
            if klines:
                axis_k: list[str] = []
                d_main, d_small, d_med, d_large, d_super = [], [], [], [], []
                for line in klines:
                    parts = line.split(",")
                    if len(parts) < 6 or len(parts[0]) < 16:
                        continue
                    axis_k.append(parts[0][-5:])
                    d_main.append(float(parts[1] or 0))
                    d_small.append(float(parts[2] or 0))
                    d_med.append(float(parts[3] or 0))
                    d_large.append(float(parts[4] or 0))
                    d_super.append(float(parts[5] or 0))
                if len(axis_k) >= 2:
                    return {"date": date, "code": code, "axis": axis_k,
                            "dims": {"main": d_main, "super": d_super, "large": d_large,
                                     "medium": d_med, "small": d_small},
                            "source": "em_kline", "errors": None}
        except Exception:  # noqa: BLE001 — 取不到就走本地库
            pass

    try:
        raw = await asyncio.to_thread(bf.read_etf_points, date, [code])
    except Exception as exc:  # noqa: BLE001
        return {"date": date, "code": code, "axis": [], "dims": {}, "error": str(exc)}
    pts = raw.get(code) or []

    def aligned(vals):
        out = bf.build_series({code: vals})
        return out["axis"], (out["series"][0]["values"] if out["series"] else [])

    axis, main_vals = aligned([(t, m) for t, m, _ in pts])
    dims = {"main": main_vals}
    for key in ("super", "large", "medium", "small"):
        _, dims[key] = aligned([(t, d.get(key) or 0) for t, _, d in pts])
    if not axis:
        dims = {}
    # 历史分时点可能只有「主力」一列（导入数据），四维全 0 时用一次实时快照兜底
    def all_zero() -> bool:
        return not any(
            v for key in ("super", "large", "medium", "small")
            for v in (dims.get(key) or []) if v)

    out = {"date": date, "code": code, "axis": axis, "dims": dims}
    if not axis or all_zero():
        try:
            q = await em.stock_get(_secid(), "f57,f58,f137,f140,f143,f146,f149,f170", ttl=20.0)
            row = bf.parse_etf_quote(code, q)
            if row:
                out["snapshot"] = {"main": row["flow"], "super": row["super"],
                                   "large": row["large"], "medium": row["medium"],
                                   "small": row["small"], "pct": row["pct"]}
        except Exception as exc:  # noqa: BLE001 — 快照失败不影响主返回
            out["errors"] = str(exc)
    if len(axis) < 2:
        out["error"] = ("当日 4 维分时点不足（导入的历史数据只有主力一列），下方为实时快照"
                        if out.get("snapshot") else "当日分时点不足（服务启动后从下一分钟开始累积）")
    return out


@router.get("/etf-flow/watch")
async def etf_flow_watch():
    return {"items": await asyncio.to_thread(bf.load_etf_watch)}


@router.post("/etf-flow/watch")
async def etf_flow_watch_add(code: str = Query(...), name: str = Query("")):
    if not (code.isdigit() and len(code) == 6):
        return {"error": "ETF 代码必须是 6 位数字"}
    items, ok = await asyncio.to_thread(
        bf.update_etf_watch, {"code": code, "name": name or code}, None)
    if not ok:
        return {"error": "自选 ETF 写入失败"}
    return {"items": items}


@router.delete("/etf-flow/watch")
async def etf_flow_watch_remove(code: str = Query(...)):
    items, ok = await asyncio.to_thread(bf.update_etf_watch, None, code)
    if not ok:
        return {"error": "自选 ETF 写入失败"}
    return {"items": items}


# --------------------------------------------------------------------------
# 东财 4 维资金（机构/大单/中户/散户）
# --------------------------------------------------------------------------
@router.get("/em-flow/targets")
async def em_flow_targets(date: str | None = Query(None)):
    """4 维页的目标清单：核心指数 + 全 A 聚合。

    附带当日最新「机构 + 大单」主动净买额与涨跌幅，供左侧列表按同口径排序
    （与「主力资金分时」列表一致：一行 = 名称 + 涨跌幅 + 资金）。
    """
    from services.universe import INDICES

    if not date:
        try:
            dates = await asyncio.to_thread(bf.available_dates, 1, "em")
        except Exception:  # noqa: BLE001
            dates = []
        date = dates[0] if dates else dt.date.today().isoformat()
    try:
        rows = await asyncio.to_thread(bf.latest_em_flows, date)
    except Exception:  # noqa: BLE001
        rows = []
    by = {r["code"]: r for r in rows}

    def item(code: str, name: str) -> dict:
        r = by.get(code)
        return {"code": code, "name": name,
                "flow": (float(r["inst"]) + float(r["large"])) if r else None,
                "pct": r.get("pct") if r else None,
                "speed": (r.get("speed") or 0) if r else 0}

    return {"date": date,
            "indices": [item(i["secid"], i["name"]) for i in INDICES]}


async def _em_board_list(date: str | None, board_type: str) -> dict:
    """板块（行业/概念）展示列表 + 全量候选（含最新主力净流入与涨跌幅）。

    行业有自定义展示配置（`em_flow_config.json`），概念固定按 |主力净流入| 取前 N。
    """
    if not date:
        try:
            dates = await asyncio.to_thread(bf.available_dates, 1)
        except Exception:  # noqa: BLE001
            dates = []
        date = dates[0] if dates else dt.date.today().isoformat()
    try:
        rank = await asyncio.to_thread(bf.read_rank, date, board_type)
    except Exception as exc:  # noqa: BLE001
        return {"date": date, "display": [], "all": [], "error": str(exc)}

    limit = bf.EM_DEFAULT_CONCEPTS if board_type == "concept" else bf.EM_DEFAULT_INDUSTRIES
    pick = bf.pick_em_concepts if board_type == "concept" else bf.pick_em_industries
    cfg = await asyncio.to_thread(bf.load_em_config, board_type)
    by = {r["code"]: r for r in rank}
    base = cfg["items"] if cfg.get("customized") and cfg["items"] else pick(rank)

    def enrich(it: dict) -> dict:
        r = by.get(it["code"]) or {}
        return {"code": it["code"], "name": it.get("name") or r.get("name") or it["code"],
                "flow": r.get("flow"), "pct": r.get("pct"), "speed": r.get("speed") or 0}

    display = [enrich(it) for it in base]
    all_rows = [{"code": r["code"], "name": r["name"], "flow": r["flow"],
                 "pct": r.get("pct"), "speed": r.get("speed") or 0,
                 # 拼音首字母：配置弹窗支持「bdt → 半导体」式模糊搜索
                 "py": pinyin_svc.initials(r["name"])}
                for r in sorted(rank, key=lambda r: -abs(float(r.get("flow") or 0)))[:200]]
    return {"date": date, "display": display, "all": all_rows,
            "customized": bool(cfg.get("customized")),
            "default_limit": limit}


@router.get("/em-flow/industries")
async def em_flow_industries(date: str | None = Query(None)):
    """行业展示列表（自定义优先，否则按 |主力净流入| 前 40）+ 全量候选。"""
    return await _em_board_list(date, "industry")


@router.get("/em-flow/concepts")
async def em_flow_concepts(date: str | None = Query(None)):
    """概念展示列表（按 |主力净流入| 前 40）+ 全量候选。"""
    return await _em_board_list(date, "concept")


@router.post("/em-flow/config/add")
async def em_flow_config_add(code: str = Query(...), name: str = Query(""),
                             kind: str = Query("industry", description="industry / concept")):
    kind = "concept" if kind == "concept" else "industry"
    if not code.startswith("BK"):
        return {"error": "板块代码必须是 BK 开头"}
    cfg = await asyncio.to_thread(bf.load_em_config, kind)
    items = cfg["items"] if (cfg.get("customized") and cfg.get("items")) else []
    if not items:
        # 尚未自定义（或刚重置）：以当前默认展示（Top N）为起点，
        # 否则第一次添加会把默认内容整个替换掉，用户会以为「重置后被清空」。
        try:
            dates = await asyncio.to_thread(bf.available_dates, 1)
        except Exception:  # noqa: BLE001
            dates = []
        date = dates[0] if dates else dt.date.today().isoformat()
        try:
            rank = await asyncio.to_thread(bf.read_rank, date, kind)
        except Exception:  # noqa: BLE001
            rank = []
        pick = bf.pick_em_concepts if kind == "concept" else bf.pick_em_industries
        items = [{"code": it["code"], "name": it["name"]} for it in pick(rank)]
    if not any(it["code"] == code for it in items):
        items.append({"code": code, "name": name or code})
    if not await asyncio.to_thread(bf.save_em_config, {"customized": True, "items": items}, kind):
        return {"error": "配置写入失败"}
    return {"customized": True, "items": items}


@router.delete("/em-flow/config")
async def em_flow_config_remove(code: str = Query(...),
                                kind: str = Query("industry", description="industry / concept")):
    kind = "concept" if kind == "concept" else "industry"
    cfg = await asyncio.to_thread(bf.load_em_config, kind)
    items = [it for it in (cfg["items"] if cfg.get("customized") else []) if it["code"] != code]
    if not await asyncio.to_thread(bf.save_em_config, {"customized": True, "items": items}, kind):
        return {"error": "配置写入失败"}
    return {"customized": True, "items": items}


@router.post("/em-flow/config/reset")
async def em_flow_config_reset(kind: str = Query("industry", description="industry / concept")):
    kind = "concept" if kind == "concept" else "industry"
    if not await asyncio.to_thread(bf.save_em_config, {"customized": False, "items": []}, kind):
        return {"error": "配置写入失败"}
    return {"customized": False, "items": []}


@router.get("/em-flow/series")
async def em_flow_series(target_type: str = Query("index", description="index / industry / concept / market"),
                         code: str = Query(""),
                         date: str | None = Query(None)):
    """单个目标的 4 维资金分时（累计）。"""
    kind = target_type if target_type in ("index", "industry", "concept") else "index"
    if not date:
        try:
            dates = await asyncio.to_thread(bf.available_dates, 1, "em")
        except Exception:  # noqa: BLE001
            dates = []
        date = dates[0] if dates else dt.date.today().isoformat()

    if not code:
        if kind == "index":
            from services.universe import INDICES
            code = INDICES[0]["secid"]
        else:
            try:
                rank = await asyncio.to_thread(bf.read_rank, date, kind)
            except Exception:  # noqa: BLE001
                rank = []
            picked = bf.pick_em_boards(rank, 1)
            code = picked[0]["code"] if picked else ""

    if not code:
        return {"date": date, "code": code, "kind": kind, "axis": [], "dims": {},
                "error": "没有可用的行业标的"}

    try:
        pts = await asyncio.to_thread(bf.read_em_points, date, code)
    except Exception as exc:  # noqa: BLE001
        return {"date": date, "code": code, "kind": kind, "axis": [], "dims": {}, "error": str(exc)}

    def aligned(vals):
        out = bf.build_series({code: vals})
        return out["axis"], (out["series"][0]["values"] if out["series"] else [])

    # pts 元组是 (time, inst, large, medium, retail)：large 在第 2 位，
    # 之前写成 1/2/3 会整体错位一列（大单显示成机构、散户显示成中户）。
    axis, inst = aligned([(p[0], p[1]) for p in pts])
    dims = {"inst": inst}
    for idx, key in ((2, "large"), (3, "medium"), (4, "retail")):
        _, dims[key] = aligned([(p[0], p[idx]) for p in pts])

    meta = await asyncio.to_thread(bf.read_em_meta, date, code)
    out = {"date": date, "code": code, "kind": kind, "axis": axis, "dims": dims,
           "name": meta.get("name") or code}
    if not axis:
        out["error"] = ("该标的还没有 4 维分时点：自选/热门板块每 2 分钟自动纳入采集，稍后刷新即可；"
                        "概念/行业默认只采 |主力净流入| 前 40")
    return out


# --------------------------------------------------------------------------
# 全A逐笔分档（资金流向页）：通达信 / Wind 两套口径
# --------------------------------------------------------------------------
@router.get("/tape-flow/dates")
async def tape_flow_dates(limit: int = Query(60, ge=1, le=365)):
    """有逐笔直方图的交易日（新 → 旧）。"""
    try:
        dates = await asyncio.to_thread(bf.available_dates, limit, "tape")
    except Exception:  # noqa: BLE001 — 库未初始化时返回空而不是 500
        dates = []
    return {"dates": dates}


@router.get("/tape-flow/health")
async def tape_flow_health():
    """采集器状态 + 最新一天的数据概况（页面用来显示「覆盖 N/M 只」）。"""
    meta: dict = {}
    try:
        dates = await asyncio.to_thread(bf.available_dates, 1, "tape")
        if dates:
            meta = await asyncio.to_thread(bf.read_tape_meta, dates[0])
            meta["date"] = dates[0]
    except Exception:  # noqa: BLE001
        pass
    return {"state": tf.state(), "latest": meta}


@router.get("/tape-flow/series")
async def tape_flow_series(
        date: str | None = Query(None),
        preset: str = Query("tdx", description="tdx（通达信）/ wind（Wind）"),
        th: str | None = Query(None, description="逗号分隔的门槛（万元，降序），覆盖 preset")):
    """全A逐笔分档的当日累计曲线 + 各档买/卖/净额。

    门槛可临时覆盖（用库里存的直方图重算，毫秒级），用于现场拟合参考软件的口径。
    """
    key = preset if preset in tf.PRESETS else "tdx"
    spec = tf.PRESETS[key]
    dim = spec.get("dim", "amt")
    edges = tf.DIMS[dim]
    if th:
        raw: list[float] = []
        for part in th.split(","):
            try:
                raw.append(float(str(part).strip()))
            except (TypeError, ValueError):
                continue
        thresholds = tf.norm_thresholds(raw, edges)
        buckets = []
        for i, thr in enumerate(thresholds):
            base = spec["buckets"][i] if i < len(spec["buckets"]) else None
            buckets.append({"key": (base or {}).get("key") or f"b{i}",
                            "name": (base or {}).get("name") or f"档{i + 1}", "thr": thr})
    else:
        thresholds = [b["thr"] for b in spec["buckets"]]
        buckets = [dict(b) for b in spec["buckets"]]

    if not date:
        try:
            dates = await asyncio.to_thread(bf.available_dates, 1, "tape")
        except Exception:  # noqa: BLE001
            dates = []
        date = dates[0] if dates else dt.date.today().isoformat()

    try:
        rows = await asyncio.to_thread(bf.read_tape_bins, date, dim)
        meta = await asyncio.to_thread(bf.read_tape_meta, date)
    except Exception as exc:  # noqa: BLE001
        return {"date": date, "preset": key, "axis": [], "series": [], "buckets": buckets,
                "error": f"逐笔库读取失败: {exc}"}

    axis = tf.session_axis()
    out = {"date": date, "preset": key, "preset_label": spec["label"], "dim": dim,
           "auction": spec.get("auction", "side"),
           "thresholds": thresholds, "buckets": buckets, "axis": axis, "series": [], "meta": meta}
    if not rows:
        out["error"] = ("这一天还没有逐笔数据：盘中每 10 分钟重算一次、收盘后 15:05 定稿"
                        "（逐笔只保留最新一个交易日，历史靠本地库）")
        return out

    # 价格叠加线（第二 Y 轴）：A股平均股价 + 上证指数
    try:
        prices = []
        for key, name, secid in (("avg", "A股平均股价", "47.800005"),
                                 ("sh", "上证指数", "1.000001")):
            pts = await ms.trends(secid)
            vals = tf.align_price(axis, pts, date)
            last = next((v for v in reversed(vals) if v is not None), None)
            if last is not None:
                prices.append({"key": key, "name": name, "values": vals, "last": last})
        out["prices"] = prices
    except Exception:  # noqa: BLE001 — 叠加线是附加项，失败不影响资金曲线
        out["prices"] = []

    cum = tf.cumulative(rows, thresholds, axis, edges)
    sums = tf.summarize(rows, thresholds, edges)
    for k, b in enumerate(buckets):
        vals = [row[k] for row in cum] if cum and k < len(cum[0]) else []
        item = dict(b)
        item.update({"values": vals, "last": vals[-1] if vals else None})
        if k < len(sums):
            item.update({x: sums[k][x] for x in ("buy", "sell", "net", "share")})
        out["series"].append({"code": item["key"], "name": item["name"], "values": vals,
                             "last": item["last"]})
        item.pop("values", None)
        out["buckets"][k] = item
    return out


@router.post("/tape-flow/scan")
async def tape_flow_scan():
    """手动重算一次全A逐笔（单次实测 6~7 分钟，后台跑，立即返回）。

    采集器忙时**必须返回 ok:false**：旧版无条件 ok:true，前端点了没反应却弹「已重算」。
    不允许并发（`run_scan` 自带 busy 守卫，这里提前拦下以便给出已用时长）。
    """
    st = tf.state()
    if st.get("busy"):
        started = st.get("started_in")
        used = f"（已 {int(started)} 秒）" if started is not None else ""
        return {"ok": False, "busy": True, "started_in": started,
                "msg": f"正在重算{used}，完成后本页自动刷新"}
    asyncio.create_task(tf.run_scan(force=True))
    return {"ok": True, "msg": "已开始重算全A逐笔，约 6 分钟；完成后本页自动刷新"}


@router.get("/board/index-rank")
async def board_index_rank(date: str | None = Query(None)):
    """指数/全 A 排行（由 4 维资金表的「机构 + 大单」合成主力净流入）。"""

    if not date:
        try:
            dates = await asyncio.to_thread(bf.available_dates, 1, "em")
        except Exception:  # noqa: BLE001
            dates = []
        date = dates[0] if dates else dt.date.today().isoformat()

    names = dict(bf.INDEX_NAMES)
    try:
        rows = await asyncio.to_thread(bf.latest_em_flows, date)
    except Exception as exc:  # noqa: BLE001
        return {"date": date, "items": [], "error": str(exc)}

    items = []
    for r in rows:
        if r.get("kind") in ("industry", "concept"):
            continue
        if r.get("code") in bf.EM_HIDDEN:  # 已下线的指数（东方财富全A / 中证全指）
            continue
        nm = names.get(r["code"]) or r.get("name") or r["code"]
        items.append({"code": r["code"], "name": nm, "type": "index",
                      "flow": r["inst"] + r["large"], "pct": r.get("pct"),
                      "speed": r.get("speed") or 0, "py": pinyin_svc.initials(nm)})
    items.sort(key=lambda x: -abs(x["flow"]))
    out = {"date": date, "items": items, "error": None if items else "暂无 4 维采集数据"}
    return out


@router.get("/board/multi-series")
async def board_multi_series(codes: str = Query(""), start: str | None = Query(None),
                             end: str | None = Query(None)):
    """多日「分钟级」序列：把区间内每天的分时点首尾相接，供多日图看细节。

    返回 axis 为「MM-DD HH:MM」，day_breaks 为每天的第一个标签（前端画日分隔虚线）。
    """
    wanted = [c.strip() for c in codes.split(",") if c.strip()]
    if not wanted:
        return {"axis": [], "series": [], "day_breaks": [], "error": "codes 不能为空"}
    try:
        dates = await asyncio.to_thread(bf.available_dates, 120)
    except Exception as exc:  # noqa: BLE001
        return {"axis": [], "series": [], "day_breaks": [], "error": str(exc)}
    asc = sorted(dates)
    end = end or (asc[-1] if asc else "")
    start = start or (asc[-5] if len(asc) >= 5 else (asc[0] if asc else ""))
    if start > end:
        start, end = end, start
    window = [d for d in asc if start <= d <= end]

    names = dict(bf.INDEX_NAMES)
    try:
        rank = await asyncio.to_thread(bf.read_rank, end)
    except Exception:  # noqa: BLE001
        rank = []
    for r in rank:
        names.setdefault(r["code"], r["name"])

    axis: list[str] = []
    day_breaks: list[str] = []
    per: dict[str, list[float | None]] = {c: [] for c in wanted}
    offsets: dict[str, float] = {c: 0.0 for c in wanted}
    try:
        for d in window:
            bk = [c for c in wanted if c.startswith("BK")]
            points = await asyncio.to_thread(bf.read_points, d, bk) if bk else {}
            for code in [c for c in wanted if not c.startswith("BK")]:
                pts = await asyncio.to_thread(bf.read_em_points, d, code)
                points[code] = [(t, inst + large) for t, inst, large, _m, _r in pts]
            aligned = bf.build_series(points)
            if not aligned["axis"]:
                continue
            labels = [f"{d[5:]} {t}" for t in aligned["axis"]]
            day_breaks.append(labels[0])
            axis.extend(labels)
            by_code = {s["code"]: s["values"] for s in aligned["series"]}
            for c in wanted:
                vals = by_code.get(c)
                vals = vals if vals else [None] * len(labels)
                # 「区间累计」：把前几天每天的累计额加到当天曲线上，跨日不断层
                offset = offsets.get(c, 0.0)
                last = None
                shifted = [(None if v is None else v + offset) for v in vals]
                for v in reversed(shifted):
                    if v is not None:
                        last = v
                        break
                if last is not None:
                    offsets[c] = last
                per[c].extend(shifted)
    except Exception as exc:  # noqa: BLE001
        return {"axis": [], "series": [], "day_breaks": [], "error": str(exc)}

    series = [{"code": c, "name": names.get(c) or c, "values": per[c]} for c in wanted]
    return {"axis": axis, "series": series, "day_breaks": day_breaks,
            "start": start, "end": end, "dates": window}
