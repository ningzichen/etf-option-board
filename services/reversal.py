"""变盘点引擎（高周期）：以 5 分钟为主、15 分钟确认为辅，识别真正的日内级别转折。

定位（2026-10-19 重构）：
- 变盘点是「趋势方向的加分因子」，参与 strategy.bias_score 综合方向判定，**不再**作为
  高频独立提醒刷屏。已确认的转折点由 reversal.factor(code) 折算为 ±15 的分值并入方向
  合力（60 分钟内线性衰减），让「方向结论」自己带上变盘信息。
- 判定周期从 1m/3m 放大到 **15m（结构转向，核心门槛）+ 5m（确认）**：只有 15 分钟
  MACD 金叉/死叉（或 15m 背离）才算「变盘」，再叠加 5m 确认、突破近 30 分钟高低、
  放量、主力资金、指数同向、关键时点等因子打分。噪声因此大幅下降，日内通常仅 1~2 次。
- 告警阈值 65；同向 45 分钟冷却、反向 30 分钟抑制（防震荡反复打脸）。以信号为准，
  不设每日次数硬上限——高周期 + 核心门槛已天然稀疏。

打分（多/空各一套，0~100）：
  15m MACD 金叉/死叉（近2根，**核心门槛**）  +30
  15m 趋势同向（柱体同向且方向一致）           +10
  5m  MACD 金叉/死叉（近2根，确认）           +20
  5m  柱体同向                               +10
  突破/跌破近 6 根 5m 高低（≈30 分钟）          +15
  站上/跌破分时均价                            +5
  量能放大（≥3x +15；≥2x +10；近1分钟 ÷ 日内均量）
  主力5分钟同向（≥1.2亿 +15；≥0.5亿 +10）
  关联指数同向                                +10
  15m 顶/底背离                              +10
  关键时点共振（±6 分钟）                      +5
核心门槛 = 15m MACD 转向 或 15m 背离；不满足直接判为噪声（不计分、不告警）。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from collections import deque

from services import alerts, em, money as money_fmt, option_service as osvc, strategy
from services.indicators import macd, macd_state

log = logging.getLogger("reversal")

THRESHOLD = 65          # 触发/入库阈值（高周期 + 核心门槛下已很稀疏）
TRACK_FETCH_SCORE = 50  # 达到此分才拉关联指数做确认（省请求）
COOLDOWN_SAME = 2700    # 同标的同方向冷却 45 分钟
COOLDOWN_OPPO = 1800    # 反向抑制 30 分钟（防震荡）
TIME_START = "09:40"    # 开盘前 10 分钟噪音，不发信号
TIME_END = "14:45"      # 临近收盘不新开仓

REV_WEIGHT = 15.0       # 变盘点并入综合方向的最大分值
REV_FRESH_SEC = 3600    # 转折点对方向的影响时长（60 分钟线性衰减）

KLT_TREND = 15          # 结构周期
KLT_CONFIRM = 5         # 确认周期
TREND_BARS = 90         # 15m 抓取根数（跨日，保证 MACD 预热）
CONFIRM_BARS = 120      # 5m 抓取根数

_signals: dict[str, deque] = {}            # code -> deque[signal记录]（供分时图打标记）
_state: dict[str, tuple[str, float, float]] = {}  # code -> (方向, 分数, 时间戳)：供方向加分

# 落盘：这两份都是「当日」语义，重启/部署后必须还在——否则左栏变盘角标、分时图标记、
# 方向加分（±15）全部丢失，要等下一次真实变盘才回来（每次发版都会踩一次）。
# 跨日文件直接作废（旧信号对新交易日没有意义），写入时只保留当天。
_SIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "reversal_signals.json")
_loaded = False


def _load() -> None:
    """首次访问时加载当日信号与冷却状态（文件不存在/跨日/损坏都按空处理）。"""
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        with open(_SIG_PATH, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return
    today = dt.datetime.now().strftime("%Y-%m-%d")
    if d.get("day") != today:
        return
    for code, rows in (d.get("signals") or {}).items():
        if not isinstance(rows, list):
            continue
        keep = [r for r in rows if isinstance(r, dict) and r.get("day") == today]
        if keep:
            _signals[code] = deque(keep, maxlen=20)
    for code, st in (d.get("state") or {}).items():
        try:
            side, score, ts = st
            _state[code] = (str(side), float(score), float(ts))
        except (TypeError, ValueError):
            continue


def _save() -> None:
    try:
        os.makedirs(os.path.dirname(_SIG_PATH), exist_ok=True)
        payload = {
            "day": dt.datetime.now().strftime("%Y-%m-%d"),
            "signals": {c: list(q) for c, q in _signals.items() if q},
            "state": {c: list(v) for c, v in _state.items()},
        }
        with open(_SIG_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except OSError as exc:
        log.debug("变盘信号落盘失败: %s", exc)


def _recent_cross(closes: list[float], lookback: int) -> str | None:
    """近 lookback 根内是否出现金叉/死叉（监控 30 秒一轮，不能只认当根）。"""
    md = macd(closes)
    dif, dea = md["dif"], md["dea"]
    if len(dif) < lookback + 1:
        return None
    for i in range(len(dif) - 1, len(dif) - 1 - lookback, -1):
        if dif[i] > dea[i] and dif[i - 1] <= dea[i - 1]:
            return "golden"
        if dif[i] < dea[i] and dif[i - 1] >= dea[i - 1]:
            return "dead"
    return None


def _score_side(ctx: dict, bars: list[dict], track: dict | None,
                side: str) -> tuple[float, list[str], bool]:
    """side: 'bull' | 'bear'。返回 (分数, 命中因子, 是否满足核心门槛)。"""
    bull = side == "bull"
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    if len(closes) < 40:
        return 0.0, [], False
    c5 = closes                      # bars 即 5 分钟 K（确认周期）
    c15 = [b["close"] for b in _agg(bars, 3)]  # 3 根 5m ≈ 15 分钟（结构周期）
    m5 = macd_state(c5)
    m15 = macd_state(c15)
    s = 0.0
    hits: list[str] = []

    # 15m 结构（核心门槛）：近 2 根内金叉/死叉，或 15m 背离
    rc15 = _recent_cross(c15, 2)
    div15 = (m15.get("divergence") or {}).get("type")
    turned15 = (bull and rc15 == "golden") or (not bull and rc15 == "dead")
    div_ok = (bull and div15 == "bullish") or (not bull and div15 == "bearish")
    core = turned15 or div_ok
    if turned15:
        s += 30
        hits.append("15m" + ("金叉" if bull else "死叉"))
    elif m15.get("ok") and ((bull and m15.get("hist_rising") and (m15.get("hist") or 0) > 0) or
                            (not bull and m15.get("hist_falling") and (m15.get("hist") or 0) < 0)):
        s += 10
        hits.append("15m趋势同向")

    # 5m 确认：近 2 根内金叉/死叉
    rc5 = _recent_cross(c5, 2)
    if (bull and rc5 == "golden") or (not bull and rc5 == "dead"):
        s += 20
        hits.append("5m确认")
    elif m5.get("ok") and ((bull and m5.get("hist_rising")) or
                           (not bull and m5.get("hist_falling"))):
        s += 10
        hits.append("5m柱体同向")

    # 突破/跌破近 6 根 5m 高低（≈30 分钟）
    if len(closes) >= 7:
        ref = max(highs[-7:-1]) if bull else min(lows[-7:-1])
        last = closes[-1]
        if (bull and last > ref) or (not bull and last < ref):
            s += 15
            hits.append(f"突破30分钟{'高' if bull else '低'}{ref:.3f}")

    # 分时均价
    tr = ctx.get("trends") or []
    if tr and tr[-1].get("avg"):
        px, avg = tr[-1]["price"], tr[-1]["avg"]
        if (bull and px > avg) or (not bull and px < avg):
            s += 5
            hits.append("站上均价" if bull else "跌破均价")

    # 量能（ratio 为「近1分钟 ÷ 日内均量」口径，见 indicators.volume_stats）
    ratio = (ctx.get("volume") or {}).get("ratio") or 0
    if ratio >= 3.0:
        s += 15
        hits.append(f"放量{ratio:.1f}x")
    elif ratio >= 2.0:
        s += 10
        hits.append(f"放量{ratio:.1f}x")

    # 主力资金（近5分钟净额）
    d5 = ctx.get("flow_main_delta5m") or 0
    if (bull and d5 >= 1.2e8) or (not bull and d5 <= -1.2e8):
        s += 15
        hits.append(f"主力{'流入' if bull else '流出'}{money_fmt.money(abs(d5))}")
    elif (bull and d5 >= 5e7) or (not bull and d5 <= -5e7):
        s += 10
        hits.append(f"主力{'流入' if bull else '流出'}{money_fmt.money(abs(d5))}")

    # 关联指数同向
    if track:
        qp = (track.get("quote") or {}).get("pct")
        tm = (track.get("macd") or {}).get("cross")
        if qp is not None and ((bull and qp > 0.1) or (not bull and qp < -0.1)) \
                and (bull and tm != "dead" or not bull and tm != "golden"):
            s += 10
            hits.append(f"指数同向{qp:+.2f}%")

    # 15m 背离（反转起点加分）
    if div_ok and not turned15:
        s += 10
        hits.append("15m底背离" if bull else "15m顶背离")

    # 关键时点共振
    if strategy.near_key_time(dt.datetime.now().strftime("%H:%M"), window=6):
        s += 5
        hits.append("关键时点共振")

    return round(s, 1), hits, core


def _agg(bars: list[dict], n: int) -> list[dict]:
    """按 n 根聚合 5m K 线（OHLC 合并），用于合成 15m 结构。"""
    out = []
    for i in range(0, len(bars), n):
        seg = bars[i:i + n]
        if not seg:
            continue
        out.append({
            "open": seg[0]["open"], "close": seg[-1]["close"],
            "high": max(b["high"] for b in seg),
            "low": min(b["low"] for b in seg),
        })
    return out


async def _trend_bars(secid: str) -> list[dict]:
    """确认周期（5 分钟）K 线，供打分与聚合出 15m 结构。"""
    return await em.kline(secid, klt=KLT_CONFIRM, lmt=CONFIRM_BARS, ttl=45.0)


async def _build_plan(etf: dict, ctx: dict, side: str, score: float,
                      hits: list[str]) -> dict:
    """组装开仓建议：方向 / 标的点位 / 优选合约 / IV 提示。"""
    direction = "call" if side == "bull" else "put"
    cn_dir = "看多" if side == "bull" else "看空"       # 方向（观点）词：全站统一「看多/看空/观望」
    cn_type = "认购" if side == "bull" else "认沽"      # 合约类型：只用于描述合约，勿与方向混用
    q = ctx.get("quote") or {}
    last = q.get("price")
    levels = strategy.plan_levels(ctx, direction)
    entry_s = levels.get("entry_break")
    stop_s = levels.get("stop")
    target_s = levels.get("target")

    contract_txt = "合约：暂无"
    contract = None
    iv_note = ""
    try:
        chain = await osvc.chain(etf["code"])
        cands = osvc.rank_candidates(chain, direction, top=3,
                                     trend={"direction": direction, "score": score},
                                     levels=levels, spot=last)
        s_now = (chain.get("underlying") or {}).get("price") or last or 0
        if cands and entry_s and stop_s and target_s:
            cl = strategy.contract_levels(cands[0], levels, s_now)
            if cl:
                contract = cl
                contract_txt = (
                    f"{cn_type}合约{cl['contract']} Δ{cl['delta']:.2f}："
                    f"入场{cl['entry']:.4f} / 止损{cl['stop']:.4f}({cl['stop_pct']:+.0f}%) / "
                    f"目标{cl['target']:.4f}"
                )
        pctl = (chain.get("stats") or {}).get("atm_iv_pctl20")
        if pctl is not None:
            iv_note = (f"IV分位{pctl:.0f}%偏高，权利金贵，降仓位"
                       if pctl >= 80 else f"IV分位{pctl:.0f}%")
    except Exception as exc:  # noqa: BLE001
        log.debug("变盘信号合约优选失败 %s: %s", etf["code"], exc)

    under_txt = "标的：—"
    if last and entry_s and stop_s and target_s:
        under_txt = (f"标的{last:.3f}：突破{entry_s:.3f}入场 / "
                     f"止损{stop_s:.3f} / 目标{target_s:.3f}")
    parts = [f"方向{cn_dir}（{score:.0f}分）", "·".join(hits), under_txt,
             contract_txt]
    if iv_note:
        parts.append(iv_note)
    return {"text": "｜".join(parts), "contract": contract,
            "underlying_price": last}


async def check(etf: dict, ctx: dict) -> dict | None:
    """评估变盘点；达阈值且通过过滤则返回告警 dict，否则 None。"""
    from services import market_service as ms  # 延迟导入，避免循环
    _load()
    code, name = etf["code"], etf["name"]
    hm = dt.datetime.now().strftime("%H:%M")
    if not (TIME_START <= hm < TIME_END):
        return None
    if len(ctx.get("kline_1m") or []) < 40:
        return None
    # 数据新鲜度：最后一根 1m K 距现在 ≤20 分钟（防用过期缓存误报）
    k1 = ctx.get("kline_1m") or []
    try:
        last_ts = dt.datetime.strptime(k1[-1]["date"], "%Y-%m-%d %H:%M")
        if (dt.datetime.now() - last_ts).total_seconds() > 1200:
            return None
    except (ValueError, KeyError):
        pass

    bars = await _trend_bars(etf["secid"])
    if len(bars) < 40:
        return None

    sb, hb, core_b = _score_side(ctx, bars, None, "bull")
    ss, hs, core_s = _score_side(ctx, bars, None, "bear")
    if not (core_b or core_s):
        return None                              # 高周期未转向 → 纯噪声
    if max(sb, ss) >= TRACK_FETCH_SCORE:         # 够高才拉关联指数做确认（省请求）
        try:
            track = await ms.track_index_state(etf["track"])
        except Exception:  # noqa: BLE001
            track = None
        sb, hb, core_b = _score_side(ctx, bars, track, "bull")
        ss, hs, core_s = _score_side(ctx, bars, track, "bear")

    side, score, hits = None, 0.0, []
    if core_b and sb >= THRESHOLD and sb >= ss:
        side, score, hits = "bull", sb, hb
    elif core_s and ss >= THRESHOLD and ss > sb:
        side, score, hits = "bear", ss, hs
    if not side:
        return None

    # 冷却 / 反向抑制（防震荡反复打脸）
    now_ts = dt.datetime.now().timestamp()
    last = _state.get(code)
    if last:
        ldir, _ls, lts = last
        if ldir == side and now_ts - lts < COOLDOWN_SAME:
            return None
        if ldir != side and now_ts - lts < COOLDOWN_OPPO:
            return None
    if not alerts.should_alert(code, f"reversal_{side}", COOLDOWN_SAME):
        return None
    _state[code] = (side, score, now_ts)

    plan = await _build_plan(etf, ctx, side, score, hits)
    now = dt.datetime.now()
    sig = {
        "etf_code": code, "etf_name": name, "type": "reversal",
        "level": "critical", "dir": side, "score": score,
        "title": f"{name} 看{'多' if side == 'bull' else '空'}变盘信号 {score:.0f}分",
        "text": plan["text"],
        "factors": hits, "contract": plan.get("contract"),
        "ts": now.isoformat(timespec="seconds"),
    }
    _signals.setdefault(code, deque(maxlen=20)).append({
        "day": now.strftime("%Y-%m-%d"), "ts": sig["ts"], "dir": side,
        "score": score, "price": plan.get("underlying_price"),
        "title": sig["title"],
    })
    _save()
    log.info("变盘点 %s %s %.0f分：%s", code, side, score, "·".join(hits))
    return sig


def factor(code: str | None) -> tuple[float, str | None]:
    """把当日已确认的变盘点折算为综合方向的加分（±REV_WEIGHT，60 分钟内衰减）。

    供 strategy.bias_score 并入方向合力；无有效转折点返回 (0, None)。
    """
    if not code:
        return 0.0, None
    _load()
    st = _state.get(code)
    if not st:
        return 0.0, None
    side, score, ts = st
    age = dt.datetime.now().timestamp() - ts
    if age > REV_FRESH_SEC:
        return 0.0, None
    decay = 1 - age / REV_FRESH_SEC
    v = REV_WEIGHT * min(1.0, score / 100.0) * decay
    return (round(v, 1) if side == "bull" else round(-v, 1),
            "看多" if side == "bull" else "看空")


def recent(code: str, limit: int = 10) -> list[dict]:
    """当日该标的的变盘点（供分时图打标记）。"""
    _load()
    today = dt.datetime.now().strftime("%Y-%m-%d")
    return [s for s in _signals.get(code, {}) if s.get("day") == today][-limit:]
