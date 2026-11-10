"""近月 ATM 隐含波动率历史（data/iv_history.jsonl），用于 IV 的 N 日分位（买方择时）。

每天每标的只追加一条（首次记录为准）；option_service.chain() 每次算出链统计后
调用 record()，percentile() 取最近 N 天算分位。文件按天自然增长
（9 标的 x 约 250 交易日 ≈ 2250 行/年），读取开销可忽略。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os

from paths import data_dir

log = logging.getLogger("ivhist")

_DATA_DIR = data_dir()
_PATH = os.path.join(_DATA_DIR, "iv_history.jsonl")


def record(code: str, atm_iv: float | None) -> None:
    """记录今日该标的近月 ATM IV；当天已记则跳过。失败只记 debug，不影响主流程。"""
    if atm_iv is None:
        return
    day = dt.date.today().isoformat()
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        if os.path.exists(_PATH):
            with open(_PATH, encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("date") == day and r.get("code") == code:
                        return
        with open(_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({"date": day, "code": code,
                                "atm_iv": round(float(atm_iv), 2)}) + "\n")
    except OSError as exc:
        log.debug("IV 历史写入失败: %s", exc)


def percentile(code: str, iv: float | None, days: int = 20) -> float | None:
    """当前 IV 在近 N 天历史 ATM IV 中的分位（0~100）；越高越贵，买方越不利。

    排除今日（今日为盘中实时值，与历史快照对比）；样本 <5 天时返回 None 避免误导。
    """
    if iv is None or not os.path.exists(_PATH):
        return None
    today = dt.date.today().isoformat()
    hist: dict[str, float] = {}
    try:
        with open(_PATH, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("code") != code or r.get("date") == today:
                    continue
                v = r.get("atm_iv")
                if v is not None:
                    hist[r["date"]] = float(v)
    except OSError:
        return None
    vals = [hist[d] for d in sorted(hist)[-days:]]
    if len(vals) < 5:
        return None
    le = sum(1 for v in vals if v <= iv)
    return round(le / len(vals) * 100, 1)
