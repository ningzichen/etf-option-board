"""日内信号引擎与交易计划：方向判定、关键时点、量能/资金异动、MACD 背离、买卖点与止盈止损。"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time

from services import (decision, em, index_levels, kronos, market_service as ms,
                      market_turnover, money as money_fmt, option_service as osvc,
                      pick_tracker as tracker, user_settings as usv,
                      volume_ratio, vol)
from services.indicators import atr, macd_state, pct_change, support_resistance, volume_stats
from services.direction_score import index_bias, recent_change, score_result
from services.universe import ETF_BY_CODE, ETFS, TRACK_INDEX

log = logging.getLogger("strategy")

_NT_CACHE: tuple[float, dict] | None = None
_NT_TTL = 30

_DIR_CACHE: tuple[float, dict] | None = None
_DIR_TTL = 60
# 过期值最长继续服务时间：SWR 期间上游若持续失败，旧值也不该无限顶下去。
# 2026-11-09 从 300s 收到 90s：300s 时徽标（看多/观望）会滞后 5 分钟，
# 与「变盘角标已现、徽标还没变」自相矛盾；90s 仍够一次后台重建（1~3s）不阻塞。
_DIR_STALE_MAX = 90
_DIR_REFRESHING = False
_DIR_LOCK = asyncio.Lock()

# 最近一次 analyze 算出的标的买卖点（供期权链接口复用，保证盈亏比口径一致）
_PLAN_CACHE: dict[str, tuple[float, dict, float]] = {}
_PLAN_TTL = 300


def plan_hint(code: str) -> dict:
    """返回最近一次 analyze 的评分上下文 ``{levels, spot, iv_rv_ratio}``（5 分钟内）。

    期权链接口自己没有 K 线，借用这里缓存的点位与 IV/RV，避免「优选合约表」与
    「T 型报价高亮」因为输入不同而给出不同分数。没有缓存时返回 ``{}``。
    """
    hit = _PLAN_CACHE.get(code)
    if not hit or time.time() - hit[0] > _PLAN_TTL:
        return {}
    return hit[1]


async def national_team_cached() -> dict:
    global _NT_CACHE
    now = time.time()
    if _NT_CACHE and now - _NT_CACHE[0] < _NT_TTL:
        return _NT_CACHE[1]
    data = await ms.national_team_signal()
    _NT_CACHE = (now, data)
    return data


async def directions() -> dict:
    """全部标的的实时综合方向（看多/看空/观望）。轻量：不含 Kronos，带 60s 缓存。

    缓存过期后走 stale-while-revalidate：**旧值立即返回**，后台异步重建。
    重建要 1~3s（9 只标的 etf_context + 指数），此前 /option/chain 每 15s 轮询
    撞上缓存失效就卡 3s，前端表现为行情刷新慢。
    """
    global _DIR_CACHE, _DIR_REFRESHING
    now = time.time()
    hit = _DIR_CACHE
    if hit and now - hit[0] < _DIR_TTL:
        return hit[1]
    if hit and now - hit[0] < _DIR_STALE_MAX:
        if not _DIR_REFRESHING:
            _DIR_REFRESHING = True
            asyncio.get_running_loop().create_task(_rebuild_dirs())
        return hit[1]
    return await _rebuild_dirs()


async def _rebuild_dirs() -> dict:
    """冷启动也单飞，避免多个详情/链请求同时重建整张方向表。"""
    global _DIR_REFRESHING
    async with _DIR_LOCK:
        if _DIR_CACHE and time.time() - _DIR_CACHE[0] < _DIR_TTL:
            _DIR_REFRESHING = False
            return _DIR_CACHE[1]
        return await _compute_dirs()


async def direction_for(code: str) -> dict:
    """列表、详情、合约推荐统一读取同一份方向快照；失败时观望。"""
    try:
        return dict((await directions()).get(code.upper()) or score_result(0))
    except Exception as exc:  # noqa: BLE001 — 不按涨跌幅偷偷切换判断规则
        log.warning("方向快照不可用 %s: %s", code, exc)
        return score_result(0)


async def _compute_dirs() -> dict:
    global _DIR_CACHE, _DIR_REFRESHING
    try:
        national, ext, idx_lv, turnover = await asyncio.gather(
            national_team_cached(), ms.external_snapshot(), index_levels.snapshot(),
            market_turnover.snapshot())

        async def one(etf: dict) -> tuple[str, dict]:
            try:
                ctx, track = await asyncio.gather(
                    ms.etf_context(etf["code"], etf["secid"]),
                    ms.track_index_state(etf["track"]),
                )
                # 当前指数量能：关联指数的同时间点累计成交额同比（同一指数 30s 缓存）
                track_secid = TRACK_INDEX.get(etf["track"])
                index_vol = (await volume_ratio.index_ratio(
                    track_secid, (track.get("quote") or {}).get("pct"),
                    label=etf["track"]) if track_secid else None)
                from services import reversal
                b = bias_score(ctx, track, national, ext, None, idx_lv, turnover,
                               index_vol,
                               reversal_signal=reversal.factor(etf["code"]))
                return etf["code"], b
            except Exception as exc:  # noqa: BLE001
                log.debug("direction 计算失败 %s: %s", etf.get("code"), exc)
                return etf["code"], score_result(0)

        out = dict(await asyncio.gather(*[one(e) for e in ETFS]))
        # 股指期权（MO/IO/HO）也要进这张表：否则左栏那三行永远显示「观望」，
        # 点进去却是「看空 -54.5」，同一份方向两处口径打架。
        from services import index_option as idxo
        fut = await asyncio.gather(*[idxo.direction(f["code"]) for f in idxo.FUT_OPTS],
                                   return_exceptions=True)
        for f, d in zip(idxo.FUT_OPTS, fut):
            out[f["code"]] = d if isinstance(d, dict) else score_result(0, model="index_option")
        # 左栏底栏的两个大盘指数也要有同样的三档徽标（看多/看空/观望）
        idx = await asyncio.gather(*[index_direction(c, s) for c, s in _BIG_INDEX],
                                   return_exceptions=True)
        for (c, _), d in zip(_BIG_INDEX, idx):
            out[c] = d if isinstance(d, dict) else score_result(0, threshold=20, model="index")
        _DIR_CACHE = (time.time(), out)
        return out
    finally:
        _DIR_REFRESHING = False


# 左栏底栏固定显示的两个大盘指数（code -> 东财 secid）
_BIG_INDEX = [("000001", "1.000001"), ("800005", "47.800005")]


async def index_direction(code: str, secid: str) -> dict:
    """大盘指数：近 30/15 分钟动量、价格相对分时均值，阈值 ±20。"""
    try:
        # 平均股价这类板块代码没有外部分钟源，只能靠交易时段内的快照累积，
        # 所以必须走 trends_or_snapshot（em.trends 对它恒为空）。
        tr = em.parse_trends(await em.trends_or_snapshot(secid, ndays=1))
    except Exception:  # noqa: BLE001 — 指数方向失败不影响主流程
        tr = []
    return index_bias(tr)


def _now_hm() -> str:
    return dt.datetime.now().strftime("%H:%M")


def near_key_time(now_hm: str, window: int = 6) -> str | None:
    """是否临近关键变盘时点（分钟窗口）。"""
    h, m = map(int, now_hm.split(":"))
    cur = h * 60 + m
    for kt in ("10:30", "11:30", "13:00", "14:30"):
        kh, km = map(int, kt.split(":"))
        if abs(cur - (kh * 60 + km)) <= window:
            return kt
    return None


def detect_signals(etf_ctx: dict, track: dict, national: dict, ext: list[dict],
                   kronos_sum: dict | None, idx_levels: dict | None = None,
                   turnover: dict | None = None) -> list[dict]:
    """产出带类型/方向/强度的信号列表。"""
    sig: list[dict] = []
    q = etf_ctx.get("quote") or {}
    if not q:
        return sig
    k1 = etf_ctx.get("kline_1m") or []
    closes = [b["close"] for b in k1]
    vols = [b["volume"] for b in k1]

    # 量能异动
    vs = etf_ctx.get("volume") or {}
    if vs.get("confirmed"):
        mv = vs.get("move_pct") or 0.0
        by_price = vs.get("confirmed_by") == "price"
        sig.append({"type": "volume_surge", "level": "warn",
                    "dir": ("bull" if mv > 0 else "bear") if by_price else "neutral",
                    "text": f"标的量价配合，量能放大 {vs['ratio']}x（近1分钟 vs 近60分钟均量）"})

    # 主力资金突入
    d5 = etf_ctx.get("flow_main_delta5m") or 0
    if abs(d5) > 8e7:
        sig.append({"type": "main_inflow", "level": "warn",
                    "dir": "bull" if d5 > 0 else "bear",
                    "text": f"主力资金近5分钟净{'流入' if d5>0 else '流出'} {money_fmt.money(abs(d5))}"})

    # 价格突动
    p3 = pct_change(closes, 3)
    if p3 is not None and abs(p3) > 0.6:
        sig.append({"type": "price_spike", "level": "warn",
                    "dir": "bull" if p3 > 0 else "bear",
                    "text": f"标的3分钟急{'拉' if p3>0 else '跌'} {p3:+.2f}%"})

    # MACD（金叉/死叉看 1 分钟；背离看 15 分钟 K 线）
    mm = etf_ctx.get("macd") or {}
    if mm.get("cross") == "golden":
        sig.append({"type": "macd", "level": "info", "dir": "bull", "text": "1分钟 MACD 金叉"})
    elif mm.get("cross") == "dead":
        sig.append({"type": "macd", "level": "info", "dir": "bear", "text": "1分钟 MACD 死叉"})
    md15 = (etf_ctx.get("macd15") or {}).get("divergence") or {}
    if md15.get("type") == "bearish":
        sig.append({"type": "divergence", "level": "warn", "dir": "bear", "text": "15分钟 MACD 顶背离，上涨动能不足"})
    elif md15.get("type") == "bullish":
        sig.append({"type": "divergence", "level": "warn", "dir": "bull", "text": "15分钟 MACD 底背离，下跌动能衰竭"})

    # 关键时点
    kt = near_key_time(_now_hm())
    if kt:
        sig.append({"type": "key_time", "level": "info", "dir": "neutral", "text": f"临近变盘时点 {kt}"})

    # 国家队护盘
    if national.get("score", 0) >= 40:
        sig.append({"type": "national_team", "level": "warn", "dir": "bull",
                    "text": f"国家队护盘信号：{national['level']}（{national['score']}分，放量{national['surge_count']}只）"})

    # 指数支撑
    sup = (track.get("support") or {})
    if sup.get("at_support"):
        sig.append({"type": "index_support", "level": "info", "dir": "bull",
                    "text": f"关联指数 {track.get('quote',{}).get('name','')} 逼近支撑（距MA20 {sup.get('dist_ma20_pct')}%）"})

    # 上证关键位（≥8 分或突破/跌破才点名具体位；状态型信号，不做时间去重）
    idx_text = index_levels.factor_text(idx_levels or {})
    if idx_text:
        lv = idx_levels or {}
        direction = "bear" if lv.get("breakdown") or (lv.get("total") or 0) < 0 else "bull"
        sig.append({"type": "index_levels", "level": "warn", "dir": direction,
                    "text": idx_text})

    # 两市成交额放量/缩量（市场级量能，与前一日全天对比）
    tt = market_turnover.factor_text(turnover or {})
    if tt:
        tv = (turnover or {}).get("factor") or 0
        sig.append({"type": "market_turnover", "level": "info",
                    "dir": "bull" if tv > 0 else "bear" if tv < 0 else "neutral",
                    "text": tt})

    # 外围
    for e in ext:
        if e.get("pct") is not None and abs(e["pct"]) >= 1.5:
            sig.append({"type": "external", "level": "info",
                        "dir": "bull" if e["pct"] > 0 else "bear",
                        "text": f"{e['name_cn']} {e['pct']:+.2f}%（外围扰动）"})

    # Kronos
    if kronos_sum and kronos_sum.get("ok"):
        sig.append({"type": "kronos", "level": "info",
                    "dir": "bull" if kronos_sum["bias"] == "up" else "bear" if kronos_sum["bias"] == "down" else "neutral",
                    "text": f"Kronos 预测未来 {len(kronos_sum['path'])} 根：{kronos_sum['pred_pct']:+.2f}%（看{'涨' if kronos_sum['bias']=='up' else '跌' if kronos_sum['bias']=='down' else '平'}）"})
    return sig


def bias_score(etf_ctx, track, national, ext, kronos_sum, idx_levels=None,
               turnover=None, index_volume=None, *,
               reversal_signal=(0.0, None)) -> dict:
    """多因子方向评分（纯函数），原始分数 ≥15 看多、≤-15 看空。

    含「变盘点」加分（reversal.factor，±15，60 分钟衰减）：5m/15m 高周期转折点作为
    条件并入方向合力，而非单独提醒。
    """
    score = 0.0
    detail = {}
    q = etf_ctx.get("quote") or {}
    # 标的日内动量（权重 25）
    mom = recent_change(etf_ctx.get("kline_1m"), 5, price_key="close", time_key="date")
    if mom is None:
        mom = recent_change(etf_ctx.get("trends"), 5)
    if mom is not None:
        v = max(-25, min(25, mom * 25))
        score += v
        detail["标的5分钟动量"] = round(v, 1)
    # 收盘 vs 均价（权重 10）
    tr = etf_ctx.get("trends") or []
    if tr and tr[-1].get("avg"):
        v = max(-10, min(10, (tr[-1]["price"] / tr[-1]["avg"] - 1) * 100 * 3))
        score += v
        detail["价格vs均价"] = round(v, 1)
    # 关联指数动量（权重 20）
    qi = (track.get("quote") or {})
    if qi.get("pct") is not None:
        v = max(-20, min(20, qi["pct"] * 12))
        score += v
        detail["关联指数"] = round(v, 1)
    # 指数 MACD（权重 10）
    tm = track.get("macd") or {}
    if tm.get("cross") == "golden":
        score += 10; detail["指数MACD金叉"] = 10
    elif tm.get("cross") == "dead":
        score -= 10; detail["指数MACD死叉"] = -10
    # 国家队（权重 15，护盘看多）
    nt = national.get("score", 0)
    if nt >= 40:
        v = 15 * min(nt / 100, 1)
        score += v; detail["国家队护盘"] = round(v, 1)
    # Kronos（权重 20）
    if kronos_sum and kronos_sum.get("ok") and kronos_sum.get("pred_pct") is not None:
        v = max(-20, min(20, kronos_sum["pred_pct"] * 20))
        score += v; detail["Kronos预测"] = round(v, 1)
    # 指数关键位（上证 20 日高低/整百关口/均线排列，内部求和后 clamp ±15）
    lv_total = (idx_levels or {}).get("total") or 0
    if lv_total:
        score += lv_total
        detail["指数关键位"] = round(lv_total, 1)
    # 外围（权重 10）：只参考韩国 KOSPI 与纳指期货主连（NQmain），各 ±5、合计钳 ±10
    ext_score = 0.0
    for e in ext:
        if e.get("key") in ("kospi", "nq") and e.get("pct") is not None:
            ext_score += max(-5.0, min(5.0, e["pct"] * 2.5))
    if ext_score:
        v = max(-10.0, min(10.0, ext_score))
        score += v; detail["外围市场"] = round(v, 1)
    # 市场总成交额（全A 同时间点累计同比，放量顺势 / 缩量背离，±10）
    tv = (turnover or {}).get("factor") or 0
    if tv:
        score += tv
        detail["市场总成交额"] = round(tv, 1)
    # 当前指数量能（关联指数同时间点累计同比，±8，同一套量价配合口径）
    iv = (index_volume or {}).get("factor") or 0
    if iv:
        score += iv
        detail["指数量能"] = round(iv, 1)
    # 变盘点（5m/15m 高周期转折，权重 15）：当日已确认转折点并入方向合力，60 分钟内衰减。
    # 这是「变盘时间点作为条件参与趋势方向分析」的落点——不再单独刷屏，而是加权到方向结论。
    rev_v, rev_label = reversal_signal
    if rev_v:
        score += rev_v
        detail[f"变盘点·{rev_label}"] = round(rev_v, 1)

    return score_result(score, detail=detail)


def _struct_target(entry: float, stop: float, levels: list | None, *,
                   up: bool, atr_v: float) -> float:
    """止盈 = 入场方向上的下一道结构位（不再机械地取 2R）。

    买方止盈要落在真实的流动性墙上：太近（<1.2R）性价比不够，跳到再上一档；
    太远（>3.5R）多半到不了，收敛到 3.5R；没有可用结构位时退回 2.2×ATR 的动量目标。
    这样不同标的/不同时段算出来的盈亏比才不一样（1.4:1、2.6:1…），而不是永远 2.0:1。
    """
    risk = abs(entry - stop) or atr_v or max(entry * 0.01, 1e-4)
    cands = [x for x in (levels or []) if (x - entry if up else entry - x) > 1.2 * risk]
    if cands:
        t = min(cands) if up else max(cands)
        cap = 3.5 * risk
        if abs(t - entry) > cap:
            t = entry + cap if up else entry - cap
        return round(t, 4)
    # 没有可用结构位时按 ATR 走，但**至少 2R**：ATR 很小（窄幅震荡）时
    # 2.2×ATR 会算出比止损距离还短的目标，盈亏比掉到 0.3:1 这种东西。
    dist = max(2.2 * atr_v, 2.0 * risk)
    return round(entry + dist if up else entry - dist, 4)


def plan_levels(etf_ctx, direction: str) -> dict:
    """基于标的日内结构给出入场/止损/止盈（标的层面），再折算到合约。"""
    k1 = etf_ctx.get("kline_1m") or []
    if not k1:
        return {}
    closes = [b["close"] for b in k1]
    highs = [b["high"] for b in k1]
    lows = [b["low"] for b in k1]
    a = atr(k1, 14) or (max(highs) - min(lows)) * 0.02
    sr = support_resistance(closes, highs, lows)
    last = closes[-1]
    # 近 15 分钟高低
    h15 = max(highs[-15:]) if len(highs) >= 15 else max(highs)
    l15 = min(lows[-15:]) if len(lows) >= 15 else min(lows)

    if direction == "call":
        entry = round(h15, 4)                 # 突破近端高点入场
        pullback = round(max(last - 0.5 * a, l15 + 0.3 * a), 4)
        stop = round(min(l15 - 0.5 * a, last - 1.5 * a), 4)
        target = _struct_target(entry, stop, sr.get("resistances"), up=True, atr_v=a)
        return {"side": "long", "entry_break": entry, "entry_pullback": pullback,
                "stop": stop, "target": target, "atr": round(a, 4),
                "day_high": sr.get("day_high"), "day_low": sr.get("day_low")}
    if direction == "put":
        entry = round(l15, 4)                 # 跌破近端低点入场
        pullback = round(min(last + 0.5 * a, h15 - 0.3 * a), 4)
        stop = round(max(h15 + 0.5 * a, last + 1.5 * a), 4)
        target = _struct_target(entry, stop, sr.get("supports"), up=False, atr_v=a)
        return {"side": "short", "entry_break": entry, "entry_pullback": pullback,
                "stop": stop, "target": target, "atr": round(a, 4),
                "day_high": sr.get("day_high"), "day_low": sr.get("day_low")}
    return {"side": "flat", "note": "方向不明，观望"}


def pullback_plan(levels: dict) -> dict:
    """把「突破/跌破入场」计划换成「回踩入场」，用于反向备选卡。

    反向是逆势小仓，追突破价往往比现价高/低一大截（实测 +10% 触发），性价比差；
    改用回踩结构位入场，止损仍用原结构位，目标按新入场重算 2R，盈亏比不变。
    """
    if not levels:
        return levels
    break_px = levels.get("entry_break")
    pull = levels.get("entry_pullback")
    stop = levels.get("stop")
    if break_px is None or pull is None or stop is None:
        return levels
    long_side = break_px > stop                 # 看涨计划：目标在入场上方
    risk = abs(pull - stop)
    if risk <= 0:
        return levels
    target = round(pull + 2 * risk, 4) if long_side else round(pull - 2 * risk, 4)
    return {**levels, "entry_break": round(pull, 4), "target": target}


def chain_for_month(chain_data: dict, month: int | None) -> dict:
    """按合约月份过滤链（``osvc.chain()`` 返回全部月份，月份筛选在调用方做）。"""
    if not month:
        return chain_data
    return {**chain_data,
            "contracts": [c for c in (chain_data.get("contracts") or [])
                          if c.get("month") == month]}


def mid_or_last(contract: dict | None) -> float | None:
    """合约参考价：能拿到买一卖一时取中值，否则退回最新价。"""
    if not contract:
        return None
    bid, ask, last = contract.get("bid"), contract.get("ask"), contract.get("last")
    if bid and ask:
        return round((bid + ask) / 2, 4)
    return last or None


def vertical_short_leg(long_leg: dict | None, chain_data: dict, direction: str) -> dict | None:
    """垂直价差的对腿：同一到期月、同一方向、更虚值一档。

    看多 -> 卖出更高行权价的认购；看空 -> 卖出更低行权价的认沽。
    都对腿不可得时返回 None，由调用方降级处理。
    """
    if not long_leg or direction not in ("call", "put"):
        return None
    side = long_leg.get("type")
    month = long_leg.get("month")
    strike = long_leg.get("strike") or 0
    pool = [c for c in (chain_data.get("contracts") or [])
            if c.get("type") == side and c.get("month") == month
            and c.get("strike") and mid_or_last(c) is not None]
    if direction == "call":
        further = sorted((c for c in pool if c["strike"] > strike),
                         key=lambda c: c["strike"])
    else:
        further = sorted((c for c in pool if c["strike"] < strike),
                         key=lambda c: c["strike"], reverse=True)
    return further[0] if further else None


def contract_levels(contract: dict, und_levels: dict, s_now: float) -> dict:
    """把标的点位用 delta 折算为合约买卖点与止盈止损。"""
    if not contract or not und_levels:
        return {}
    if und_levels.get("entry_break") is None or und_levels.get("stop") is None \
            or und_levels.get("target") is None:
        return {}
    delta = contract.get("delta") or 0
    cur = contract.get("last")
    if not cur or not delta:
        return {}
    def px(s_target):
        return round(max(cur + delta * (s_target - s_now), 0.0001), 4)
    entry = px(und_levels.get("entry_break"))
    stop = px(und_levels.get("stop"))
    target = px(und_levels.get("target"))
    # 合约层硬止损：相对入场价 -30%，认购/认沽双向生效
    # （标的触及止损位时合约价恒低于入场价，故 stop 取两者较高者即收紧到 -30%）
    hard_stop = round(entry * 0.70, 4)
    stop = max(stop, hard_stop)
    return {
        "contract": contract.get("name"), "code": contract.get("code"),
        "current": cur, "delta": delta,
        "entry": entry, "stop": stop, "target": target,
        # entry_pct 是触发价相对现价的偏离（≥3% 标「触发买入」）；
        # 止损/止盈幅度一律相对入场价——买方实际承担/赚取的就是这笔，
        # 相对现价算会把触发偏离混进去，盈亏比跟着失真。
        "entry_pct": round((entry / cur - 1) * 100, 1) if entry else None,
        "stop_pct": round((stop / entry - 1) * 100, 1) if entry else None,
        "target_pct": round((target / entry - 1) * 100, 1) if entry else None,
    }


def recommend_card(cand: dict | None, levels: dict | None, spot: float,
                   direction: str, kind: str = "primary") -> dict | None:
    """把一只合约整理成前端「推荐卡」：买入/止损/止盈 + 理由。

    价格全部来自 ``contract_levels``（按 Δ 折算标的点位），前端不再二次计算——
    一份公式两处实现，迟早会分叉。``kind`` 区分主推（primary）与反向（reverse）。
    """
    if not cand:
        return None
    cl = contract_levels(cand, levels or {}, spot) or {}
    rr = cand.get("rr") or {}
    reasons = list(cand.get("reasons") or [])
    if cl.get("stop_pct") is not None and cl.get("target_pct") is not None:
        reasons.append(f"按 {abs(cand.get('delta') or 0):.2f}Δ 折算："
                       f"止损 {cl['stop_pct']:+.0f}% / 止盈 {cl['target_pct']:+.0f}%")
    if rr.get("roi") is not None:
        reasons.append(f"期望收益 {rr['roi'] * 100:+.0f}% 权利金"
                       f"（p≈{rr.get('p')}，赚 {rr.get('up')} / 亏 {rr.get('down')}）")
    # 买 1 张的现金盈亏（乘数：ETF 10000 份/张、股指期权 100 元/点）
    mult = cand.get("multiplier") or 10000
    entry, stop, target = cl.get("entry"), cl.get("stop"), cl.get("target")
    profit = round((target - entry) * mult, 0) if target is not None and entry is not None else None
    loss = round((entry - stop) * mult, 0) if stop is not None and entry is not None else None
    return {
        "kind": kind, "side": direction,
        "code": cand.get("code"), "secid": cand.get("secid"),
        "name": cand.get("name"), "month": cand.get("month"),
        "days": cand.get("days"), "score": cand.get("score"),
        "delta": cand.get("delta"), "iv": cand.get("iv"),
        "price": cand.get("last"),
        "entry": cl.get("entry"), "stop": cl.get("stop"), "target": cl.get("target"),
        "entry_pct": cl.get("entry_pct"), "stop_pct": cl.get("stop_pct"),
        "target_pct": cl.get("target_pct"),
        "multiplier": mult, "profit": profit, "loss": loss,
        "cost": round(entry * mult, 0) if entry is not None else None,
        "rr": rr, "reasons": reasons[:5],
    }


async def analyze(etf_code: str, with_kronos: bool = True, klt: int = 1) -> dict:
    etf = ETF_BY_CODE.get(etf_code)
    if not etf:
        return {"error": f"未知标的 {etf_code}"}
    chain, etf_ctx, national, ext, idx_lv, turnover, daily, track = await asyncio.gather(
        osvc.chain(etf_code),
        ms.etf_context(etf_code, etf["secid"]),
        national_team_cached(),
        ms.external_snapshot(),
        index_levels.snapshot(),
        market_turnover.snapshot(),
        # 20 日已实现波动：给买方判断「隐波贵不贵」（em 层 45s 缓存，不额外压请求）
        ms.kline(etf["secid"], klt=101, lmt=30),
        # 关联指数状态与上述 7 路并发（此前在 gather 之后串行等 0.68s）
        ms.track_index_state(etf["track"]),
    )

    kronos_sum = None
    if with_kronos:
        freq = {1: "1m", 3: "3m", 5: "5m"}.get(klt, "1m")
        bars = etf_ctx.get("kline_1m") or []
        if len(bars) >= 60:
            pred = await kronos.predict(bars, lookback=min(len(bars), 120),
                                        pred_len=20, frequency=freq)
            kronos_sum = kronos.summarize(pred, bars[-1]["close"])

    signals = detect_signals(etf_ctx, track, national, ext, kronos_sum, idx_lv, turnover)
    # 方向与列表/T 型报价共用快照；Kronos 仍在独立预测和信号区展示。
    bias = await direction_for(etf_code)
    # 当前指数量能明细（关联指数同时间点同比；与方向快照同一份 30s 缓存）
    track_secid = TRACK_INDEX.get(etf["track"])
    index_vol = (await volume_ratio.index_ratio(
        track_secid, (track.get("quote") or {}).get("pct"),
        label=etf["track"]) if track_secid else None)
    plan_dir = bias["direction"] if bias["direction"] != "neutral" else (
        "call" if bias["score"] >= 0 else "put")
    levels = plan_levels(etf_ctx, plan_dir)

    s_now = (chain.get("underlying") or {}).get("price") or 0
    # 买方择时：IV 相对 20 日已实现波动（IV 分位样本不足时的即时替代口径）
    ivrv = vol.iv_rv(chain, daily)
    _PLAN_CACHE[etf_code] = (time.time(), {"levels": levels, "spot": s_now,
                                           "iv_rv_ratio": (ivrv or {}).get("ratio")})
    iv_note = vol.buyer_note((chain.get("stats") or {}).get("atm_iv_pctl20"),
                             (ivrv or {}).get("ratio"))
    if iv_note:
        signals = [*signals, {"type": "iv_cost", "level": "warn",
                              "dir": "neutral", "text": iv_note}]

    # ── 决策层：到期约束 → 策略类型 → 手数上限 → 撜口与限额 ─────────────
    us = usv.load()
    today = dt.date.today()
    calendar_rows = decision.expiry_calendar(today)
    front_days = calendar_rows[0]["days_left"] if calendar_rows else None
    next_days = calendar_rows[1]["days_left"] if len(calendar_rows) > 1 else None
    iv_pct = (chain.get("stats") or {}).get("atm_iv_pctl20")

    strat = decision.select_strategy(bias["direction"], iv_pct,
                                     days_to_expiry=front_days, next_month_days=next_days)

    # 临期时整体改推次月（ticket 08）：只影响「新开仓」建议
    target_month = None
    if strat.get("month") == "next" and len(calendar_rows) > 1:
        target_month = calendar_rows[1]["month"]
    work_chain = chain_for_month(chain, target_month)
    rank_dir = bias["direction"] if bias["direction"] != "neutral" else "auto"
    calib = tracker.calibration()
    rank_trend = {**bias, "iv_rv_ratio": (ivrv or {}).get("ratio")}
    candidates = osvc.rank_candidates(work_chain, rank_dir, top=6, trend=rank_trend,
                                      levels=levels, spot=s_now, calib=calib)
    # 实盘跟踪：记录主选、回填历史记录的 5/15/30 分钟收益（失败只记日志）
    tracker.record(etf_code, plan_dir, candidates[0] if candidates else None,
                   iv_pctl=iv_pct)
    tracker.update(etf_code, chain)

    # 合约没有外部分钟源，只能本地逐分钟累积：给前 3 只候选各记一次快照，
    # 用户点开详情时就不会是一条断断续续的线（失败忽略，不影响主流程）。
    try:
        await asyncio.gather(*[osvc.option_trends(c["secid"]) for c in candidates[:3]],
                             return_exceptions=True)
    except Exception as exc:  # noqa: BLE001
        log.debug("合约分时预热失败 %s: %s", etf_code, exc)

    # 反向合约：同一条链上反方向再选一只；买卖点用**回踩入场**的反向点位折算
    # （逆势小仓不该追突破价，实测反向卡触发价能比现价高 10%）。
    reverse_dir = "put" if plan_dir == "call" else "call"
    rev_levels = pullback_plan(plan_levels(etf_ctx, reverse_dir))
    rev_cands = osvc.rank_candidates(work_chain, reverse_dir, top=3, trend=rank_trend,
                                     levels=rev_levels, spot=s_now, calib=calib)
    recommendation = {
        "primary": recommend_card(candidates[0] if candidates else None,
                                  levels, s_now, plan_dir, "primary"),
        "reverse": recommend_card(rev_cands[0] if rev_cands else None,
                                  rev_levels, s_now, reverse_dir, "reverse"),
        "reverse_note": "逆势方向，只在对冲或明确反转信号时小仓参与",
    }

    if strat.get("month") == "next" and not candidates:
        strat = {**strat, "strategy": decision.STRATEGY_WAIT,
                 "label": decision.STRATEGY_LABELS[decision.STRATEGY_WAIT],
                 "no_alternative": True,
                 "reason": f"{strat['reason']} 次月无可用合约，建议观望。"}

    best = candidates[0] if candidates else None
    vertical_dir = {"bull_call": "call", "bear_put": "put"}.get(strat.get("vertical_kind"))

    # 垂直价差：找对腿，算行权价宽度与净权利金（正=收到，负=付出）
    short_leg = vertical_short_leg(best, work_chain, vertical_dir) if vertical_dir else None
    width = credit = None
    if short_leg and best:
        width = round(abs((short_leg.get("strike") or 0) - (best.get("strike") or 0)), 4)
        long_px, short_px = mid_or_last(best), mid_or_last(short_leg)
        if long_px and short_px:
            credit = round(short_px - long_px, 4)

    if strat["strategy"] == decision.STRATEGY_VERTICAL and short_leg is None:
        # 对腿不可用则退回单腿，保证手数仍可算（不要因缺一条腿而丢掉风控）
        single = (decision.STRATEGY_SINGLE_CALL if bias["direction"] == "call"
                  else decision.STRATEGY_SINGLE_PUT)
        strat = {**strat, "strategy": single, "label": decision.STRATEGY_LABELS[single],
                 "vertical_kind": None, "degraded_vertical": True,
                 "reason": f"{strat['reason']} 同月份对腿不可用，退回单腿买入。"}

    # 手数上限：单腿按权利金全额，价差按宽度与净权利金
    if strat["strategy"] == decision.STRATEGY_VERTICAL:
        loss_per = decision.max_loss_per_contract("vertical", width=width, net_credit=credit)
    elif best:
        loss_per = decision.max_loss_per_contract("single", premium=mid_or_last(best))
    else:
        loss_per = None
    budget = decision.risk_budget(us.get("total_capital"), us.get("risk_pct"))
    position = decision.max_contracts(budget, loss_per)

    # 撜口与限额（撜口是估算值，见 docs/adr/0001）
    unit = None
    if best:
        unit = {"premium": mid_or_last(best), "delta": best.get("delta"),
                "price": s_now, "theta": best.get("theta"), "vega": best.get("vega")}
    exposure = decision.exposure_notional(us.get("tier"), us.get("size"),
                                         us.get("total_capital"), unit=unit)
    lim = decision.limits(us.get("risk_pct"), us.get("total_capital"))
    limit_check = decision.check_limits(exposure, lim,
                                        per_contract_delta=exposure.get("per_contract_delta"))

    plans = [contract_levels(c, levels, s_now) for c in candidates[:3]]

    return {
        "etf": etf,
        "underlying": chain.get("underlying"),
        "chain_stats": chain.get("stats"),
        "iv_rv": ivrv,
        "calibration": calib,
        "front_month": chain.get("front_month"),
        "bias": bias,
        "signals": signals,
        "levels": levels,
        # 合约图按所选合约方向取计划：认购看涨计划、认沽看跌计划，
        # 否则看跌主推时点开购合约会看到「止盈在止损下面」（方向反了）。
        "levels_by_side": {
            "call": levels if plan_dir == "call" else rev_levels,
            "put": levels if plan_dir == "put" else rev_levels,
        },
        "plans": [p for p in plans if p],
        "recommendation": recommendation,
        "candidates": [{k: v for k, v in c.items() if not k.startswith("_")} for c in candidates],
        "kronos": kronos_sum,
        "track": {"quote": track.get("quote"), "support": track.get("support"), "macd": track.get("macd")},
        "index_levels": idx_lv,
        "market_turnover": turnover,
        "index_volume": index_vol,
        "national_team": national,
        "external": ext,
        "volume": etf_ctx.get("volume"),
        "macd": etf_ctx.get("macd"),
        "macd15": etf_ctx.get("macd15"),
        "flow_main_delta5m": etf_ctx.get("flow_main_delta5m"),
        # ── 本次新增的决策层输出 ──
        "strategy": strat,
        "expiry": {
            "today": today.isoformat(),
            "rows": calendar_rows,
            "front_days": front_days,
            "next_days": next_days,
            "level": decision.near_expiry_level(front_days),
            "target_month": target_month,
            "excluded_front": bool(strat.get("excluded_front")),
        },
        "position": position,
        "exposure": exposure,
        "limits": lim,
        "limit_check": limit_check,
        "settings": us,
        "vertical": ({"short_leg": {"name": short_leg.get("name"),
                                    "code": short_leg.get("code"),
                                    "strike": short_leg.get("strike"),
                                    "month": short_leg.get("month")},
                      "width": width, "net_credit": credit} if short_leg else None),
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
    }
