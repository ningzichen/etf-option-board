"""优选合约的实盘跟踪 + 有界自校准（``data/pick_history.json``）。

**记录**：每次算出优选后把「主选合约」按 (标的, 方向, 15 分钟一档) 记一条，
同一档只记一次——轮询频率远高于 15 分钟，不去重会把样本灌成噪音。

**评估**：下次拿到同一标的的期权链时，用链里的最新价回填 5/15/30 分钟收益
（不再额外发请求），30 分钟后置 done 停止跟踪。

**自校准**：只在样本足够（≥30 笔）且差异显著（≥4 个百分点）时做**有界**微调：
Δ 目标 ±0.05、Theta 惩罚 0.85~1.25 倍。数据不够就返回中性值——宁可不动，
也不拿噪声去改打分。这是唯一能让权重/目标「从实盘里长出来」的机制。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os

from paths import data_path

log = logging.getLogger("picks")

_PATH = data_path("pick_history.json")

MAX_RECORDS = 6000          # 约 30 个交易日（12 个标的 × 每 15 分钟一条）
HORIZONS = (5, 15, 30)      # 分钟
MIN_SAMPLES = 30            # 自校准最小总样本
MIN_BUCKET = 10             # 单个桶的最小样本
MIN_EDGE = 4.0              # 触发调参的最小收益差（百分点）

_CACHE: list[dict] | None = None
_MTIME: float | None = None


# --------------------------------------------------------------------------
# 存取
# --------------------------------------------------------------------------
def _load() -> list[dict]:
    global _CACHE, _MTIME
    try:
        st = os.stat(_PATH)
    except OSError:
        return []
    if _CACHE is not None and _MTIME == st.st_mtime:
        return _CACHE
    try:
        with open(_PATH, encoding="utf-8") as f:
            data = json.load(f)
        _CACHE = data if isinstance(data, list) else []
        _MTIME = st.st_mtime
    except (OSError, ValueError) as exc:
        log.debug("读优选历史失败: %s", exc)
        _CACHE = _CACHE or []
    return _CACHE


def _save(records: list[dict]) -> None:
    global _CACHE, _MTIME
    records = records[-MAX_RECORDS:]
    try:
        os.makedirs(os.path.dirname(_PATH), exist_ok=True)
        tmp = f"{_PATH}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, _PATH)
        _CACHE = records
        _MTIME = os.stat(_PATH).st_mtime
    except OSError as exc:
        log.debug("写优选历史失败: %s", exc)


def _reset_cache() -> None:
    """测试用：丢弃内存缓存，强制下次从磁盘读。"""
    global _CACHE, _MTIME
    _CACHE, _MTIME = None, None


def in_session(now: dt.datetime) -> bool:
    """是否处于连续竞价时段（周末与午休不记样本）。"""
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return (9 * 60 + 30) <= hm <= (11 * 60 + 30) or (13 * 60) <= hm <= (15 * 60)


# --------------------------------------------------------------------------
# 记录与回填
# --------------------------------------------------------------------------
def record(code: str, direction: str, pick: dict | None, iv_pctl: float | None = None,
           now: dt.datetime | None = None) -> dict | None:
    """记录一次主选（同一标的同一 15 分钟档只记一次）。"""
    now = now or dt.datetime.now()
    try:
        if not pick or not in_session(now):
            return None
        price = pick.get("last")
        if not price or price <= 0:
            return None
        bucket = now.replace(minute=now.minute // 15 * 15, second=0,
                             microsecond=0).isoformat(timespec="minutes")
        recs = _load()
        if any(r.get("code") == code and r.get("bucket") == bucket for r in recs):
            return None
        last = float(price)
        theta = abs(pick.get("theta") or 0.0)
        rec = {
            "code": code, "date": now.date().isoformat(), "bucket": bucket,
            "entry_ts": now.isoformat(timespec="seconds"), "entry": round(last, 4),
            "direction": direction, "secid": pick.get("secid"), "name": pick.get("name"),
            "strike": pick.get("strike"), "month": pick.get("month"),
            "days": pick.get("days"), "delta": round(abs(pick.get("delta") or 0), 3),
            "score": pick.get("score"), "spread": pick.get("spread"),
            "iv_pctl": iv_pctl,
            "theta_decay": round(theta / last, 4) if last else None,
            "rets": {}, "done": False,
        }
        _save([*recs, rec])
        return rec
    except Exception as exc:  # noqa: BLE001 — 记录失败绝不影响主流程
        log.debug("优选记录失败 %s: %s", code, exc)
        return None


def update(code: str, chain: dict, now: dt.datetime | None = None) -> int:
    """用当前期权链的最新价回填该标的未完成记录的 5/15/30 分钟收益。"""
    now = now or dt.datetime.now()
    try:
        recs = _load()
        pending = [r for r in recs if r.get("code") == code and not r.get("done")]
        if not pending:
            return 0
        quotes = {c.get("secid"): c.get("last") for c in (chain.get("contracts") or [])}
        filled = 0
        for r in pending:
            price = quotes.get(r.get("secid"))
            if not price:
                continue
            try:
                t0 = dt.datetime.fromisoformat(r["entry_ts"])
            except (KeyError, ValueError):
                r["done"] = True
                continue
            mins = (now - t0).total_seconds() / 60.0
            rets = r.setdefault("rets", {})
            for h in HORIZONS:
                key = f"ret{h}"
                if mins >= h and key not in rets:
                    rets[key] = round((float(price) / r["entry"] - 1) * 100, 2)
                    filled += 1
            if mins >= HORIZONS[-1] + 5:
                r["done"] = True
        if filled or any(r.get("done") for r in pending):
            _save(recs)
        return filled
    except Exception as exc:  # noqa: BLE001
        log.debug("优选回填失败 %s: %s", code, exc)
        return 0


# --------------------------------------------------------------------------
# 统计与自校准（纯函数，便于测试）
# --------------------------------------------------------------------------
def bench_ret(rec: dict) -> float | None:
    """评价口径：优先 30 分钟收益，未到 30 分钟退 15 分钟，再退 5 分钟。"""
    rets = rec.get("rets") or {}
    for h in (30, 15, 5):
        v = rets.get(f"ret{h}")
        if v is not None:
            return float(v)
    return None


def _delta_bucket(delta: float | None) -> str:
    d = abs(delta or 0)
    if d < 0.30:
        return "0.15-0.30"
    if d < 0.45:
        return "0.30-0.45"
    if d < 0.60:
        return "0.45-0.60"
    if d < 0.75:
        return "0.60-0.75"
    return "0.75+"


def _theta_bucket(decay: float | None) -> str:
    if decay is None:
        return "未知"
    if decay < 0.02:
        return "<2%/日"
    if decay < 0.04:
        return "2-4%/日"
    if decay < 0.06:
        return "4-6%/日"
    return ">6%/日"


def _iv_bucket(pctl: float | None) -> str:
    if pctl is None:
        return "未知"
    if pctl < 25:
        return "<25 分位"
    if pctl <= 75:
        return "25-75 分位"
    return ">75 分位"


def bucket_of(rec: dict) -> dict[str, str]:
    """一条样本落进哪几个桶（delta / theta / iv 分位）。"""
    return {"delta": _delta_bucket(rec.get("delta")),
            "theta": _theta_bucket(rec.get("theta_decay")),
            "iv": _iv_bucket(rec.get("iv_pctl"))}


def _agg(rows: list[dict]) -> dict:
    vals = [v for v in (bench_ret(r) for r in rows) if v is not None]
    if not vals:
        return {"n": 0, "hit": None, "avg": None}
    return {"n": len(vals),
            "hit": round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1),
            "avg": round(sum(vals) / len(vals), 2)}


def stats(records: list[dict] | None = None) -> dict:
    """总体与分桶的命中率/平均收益（按 bench_ret 口径）。"""
    rows = records if records is not None else _load()
    rows = [r for r in rows if bench_ret(r) is not None]
    buckets: dict[str, dict[str, dict]] = {"delta": {}, "theta": {}, "iv": {}}
    for r in rows:
        for dim, label in bucket_of(r).items():
            buckets[dim].setdefault(label, []).append(r)
    out = {dim: {label: _agg(group) for label, group in groups.items()}
           for dim, groups in buckets.items()}
    recent = sorted(rows, key=lambda r: r.get("entry_ts") or "")[-5:]
    return {
        **_agg(rows),
        "buckets": out,
        "since": rows[0].get("date") if rows else None,
        "recent": [{"name": r.get("name"), "entry_ts": r.get("entry_ts"),
                    "ret": bench_ret(r)} for r in reversed(recent)],
    }


def calibration(records: list[dict] | None = None) -> dict:
    """有界自校准：返回 Δ 目标平移量与 Theta 惩罚倍数。

    只有总样本 ≥30、单桶 ≥10、且差异 ≥4 个百分点时才改；否则返回中性值。
    """
    rows = [r for r in (records if records is not None else _load())
            if bench_ret(r) is not None]
    base = {"target_delta_shift": 0.0, "theta_gain": 1.0,
            "n": len(rows), "hit": _agg(rows)["hit"], "source": "default"}
    if len(rows) < MIN_SAMPLES:
        return base

    by_delta: dict[str, list[dict]] = {}
    by_theta: dict[str, list[dict]] = {}
    for r in rows:
        b = bucket_of(r)
        by_delta.setdefault(b["delta"], []).append(r)
        by_theta.setdefault(b["theta"], []).append(r)

    centres = {"0.15-0.30": 0.225, "0.30-0.45": 0.375, "0.45-0.60": 0.525,
               "0.60-0.75": 0.675, "0.75+": 0.80}
    known = {k: _agg(v) for k, v in by_delta.items() if len(v) >= MIN_BUCKET}
    shift = 0.0
    if len(known) >= 2:
        best_label = max(known, key=lambda k: known[k]["avg"])
        mid = known.get("0.45-0.60")
        if mid and known[best_label]["avg"] - mid["avg"] >= MIN_EDGE:
            shift = max(-0.05, min(0.05, centres.get(best_label, 0.525) - 0.525))

    theta_gain = 1.0
    fast = _agg(by_theta.get(">6%/日", []))
    slow = _agg([*by_theta.get("<2%/日", []), *by_theta.get("2-4%/日", [])])
    if fast["n"] >= MIN_BUCKET and slow["n"] >= MIN_BUCKET:
        gap = slow["avg"] - fast["avg"]
        if gap >= MIN_EDGE:
            theta_gain = 1.25
        elif gap <= -MIN_EDGE:
            theta_gain = 0.85

    return {**base, "target_delta_shift": round(shift, 3),
            "theta_gain": theta_gain,
            "source": "calibrated" if (shift or theta_gain != 1.0) else "default"}


def summary() -> dict:
    """给接口/看板的一揽子输出。"""
    st = stats()
    return {"stats": st, "calibration": calibration(),
            "path": _PATH, "horizons": HORIZONS}
