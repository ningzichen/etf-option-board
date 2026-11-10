"""跨标的优选合约：把 9 只 ETF 的期权链放在一起比较，挑「性价比 + 波动」兼顾的合约。

与单标的「优选合约」卡是同一套打分（``option_service.SCORE_WEIGHTS``，流动性用绝对
锚点、跨标的可比），差别只有三点：

1. **横向比较**：遍历 9 只 ETF，各取本标的方向（``strategy.directions`` 同源）同向侧
   的候选，再放在一张表里排序——回答「今天整个市场最值得买的是哪张合约」；
2. **多一维波动分**：有效杠杆（|Δ|×标的价/权利金）+ 合约当日涨跌幅 + 标的日内振幅，
   单独归一化，衡量「同样的钱，谁的日内爆发力更大」；
3. **综合分** = 0.6×性价比（质量分）+ 0.4×波动分；单标的最多入选 2 只，避免一只标的霸榜。

开销：9 条链实测 0.6~1.5s（em 层有短 TTL 缓存），30s SWR 缓存，够前端轮询。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time

from services import em, option_service as osvc, pick_tracker as tracker, strategy
from services.universe import ETFS

log = logging.getLogger("top_picks")

_TTL = 45.0             # 缓存有效期（前端 30s 轮询，约每两轮真正重建一次）
_STALE_MAX = 180.0      # 上游持续失败时旧值最长顶多久
_MAX_PER_ETF = 2        # 单标的最多入选数
_W_Q, _W_V = 0.6, 0.4   # 综合分：性价比 / 波动

_CACHE: tuple[float, list[dict]] | None = None
_REFRESHING = False


def _vol_score(c: dict, amp: float | None) -> float:
    """波动分 0~100：有效杠杆为主，合约当日振幅与标的振幅为辅。

    有效杠杆 = 静态杠杆(标的价/权利金) × |Δ|，表示标的涨 1% 时合约约涨多少；
    用绝对锚点做对数映射（3~30 倍打满），保证跨标的可比。
    """
    eff = (c.get("leverage") or 0) * abs(c.get("delta") or 0)
    s_lev = osvc._lognorm(eff, 2.5, 30.0)
    s_move = osvc._lognorm(abs(c.get("pct") or 0), 2.0, 40.0)
    s_amp = osvc._lognorm(amp or 0, 0.5, 5.0)
    return round(100 * (0.55 * s_lev + 0.25 * s_move + 0.20 * s_amp), 1)


async def _one(etf: dict, dirs: dict, calib: dict) -> list[dict]:
    """单只 ETF：定方向 → 算标的点位 → rank_candidates → 折算波动分。"""
    code = etf["code"]
    ch = await osvc.chain(code)
    if not ch or ch.get("error") or not ch.get("contracts"):
        return []
    und = ch.get("underlying") or {}
    d = dirs.get(code) or {}
    direction = d.get("direction") or "neutral"
    pct = und.get("pct") or 0.0
    # 观望时按标的涨跌择边（与 rank_candidates(auto) 同口径）
    side = direction if direction in ("call", "put") else ("call" if pct >= 0 else "put")

    levels = {}
    try:
        bars = em.parse_klines(await em.kline(etf["secid"], klt=1, lmt=60, ttl=45.0))
        if len(bars) >= 15:
            levels = strategy.plan_levels({"kline_1m": bars, "quote": und}, side)
    except Exception as exc:  # noqa: BLE001 — 没有点位只是少了 EV 分，不阻断横评
        log.debug("top_picks 点位失败 %s: %s", code, exc)

    trend = {"direction": direction, "score": d.get("score") or 0,
             "confidence": d.get("confidence") or 0}
    rows = osvc.rank_candidates(ch, side, top=4, trend=trend, levels=levels,
                                spot=und.get("price"), calib=calib)
    amp = und.get("amplitude")
    # ATM IV 20 日分位：与专业模式 decision.select_strategy 同一输入口径，
    # 下游（简洁模式选合约）据此判「高 IV 是否该改价差」。
    iv_pct = (ch.get("stats") or {}).get("atm_iv_pctl20")
    out = []
    for c in rows:
        q = float(c.get("score") or 0)
        v = _vol_score(c, amp)
        out.append({
            "code": c.get("code"), "secid": c.get("secid"), "name": c.get("name"),
            "month": c.get("month"), "strike": c.get("strike"), "type": c.get("type"),
            "last": c.get("last"), "pct": c.get("pct"), "delta": c.get("delta"),
            "gamma": c.get("gamma"), "iv": c.get("iv"), "leverage": c.get("leverage"),
            "spread": c.get("spread"), "amount": c.get("amount"), "oi": c.get("oi"),
            "days": c.get("days"), "premium_rate": c.get("premium_rate"),
            "etf_code": code, "etf_name": etf["name"], "side": side,
            "direction": direction, "trend_score": d.get("score"),
            "trend_aligned": bool(c.get("trend_aligned")),
            "underlying_price": und.get("price"), "underlying_pct": pct,
            "underlying_amplitude": amp, "iv_pct": iv_pct,
            "quality": round(q, 1), "vol": v,
            "pick_score": round(_W_Q * q + _W_V * v, 1),
            "reasons": (c.get("reasons") or [])[:3],
        })
    return out


async def _build() -> list[dict]:
    try:
        dirs = await strategy.directions()
    except Exception as exc:  # noqa: BLE001 — 方向缺失时全部按涨跌择边
        log.warning("top_picks 方向获取失败: %s", exc)
        dirs = {}
    calib = tracker.calibration()
    groups = await asyncio.gather(*[_one(e, dirs, calib) for e in ETFS],
                                  return_exceptions=True)
    picks: list[dict] = []
    for g in groups:
        if isinstance(g, Exception):
            log.debug("top_picks 单标的失败: %s", g)
            continue
        if not g:
            continue
        g.sort(key=lambda x: -x["pick_score"])
        picks.extend(g[:_MAX_PER_ETF])
    picks.sort(key=lambda x: -x["pick_score"])
    for i, p in enumerate(picks):
        p["rank"] = i + 1
    return picks


async def _rebuild() -> list[dict]:
    global _CACHE, _REFRESHING
    try:
        data = await _build()
        _CACHE = (time.time(), data)
        return data
    finally:
        _REFRESHING = False


async def snapshot(n: int = 6, force: bool = False) -> dict:
    """跨标的优选列表（SWR：缓存过期先回旧值，后台异步重建）。"""
    global _REFRESHING
    now = time.time()
    hit = _CACHE
    if force:
        items = await _rebuild()
    elif hit and now - hit[0] < _TTL:
        items = hit[1]
    elif hit and now - hit[0] < _STALE_MAX:
        if not _REFRESHING:
            _REFRESHING = True
            asyncio.get_running_loop().create_task(_rebuild())
        items = hit[1]
    else:
        items = await _rebuild()
    return {
        "items": items[:max(1, min(n, 12))],
        "count": len(items),
        "weights": {"quality": _W_Q, "vol": _W_V},
        "ts": dt.datetime.now().strftime("%H:%M:%S"),
    }
