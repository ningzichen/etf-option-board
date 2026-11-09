"""两市/全A 成交额因子：同时间点累计成交额同比 + 量价配合。

2026-11-09 起口径改为「今日此刻累计 ÷ 昨日同一时刻累计」（实现在 `services/volume_ratio`）：
- 全A = 上证指数 + 深证成指（分别是沪市/深市总额，不含北交所）；
- 放量 ≥1.10 → 顺着上证涨跌 ±10；缩量 ≤0.90 → 背离口径反向 ±10；平量 0；
- |涨跌| ≤0.1% 不做量价配合；09:45 前样本太小不计分；收盘后按全天同比。
旧的「按交易进度折算全天 vs 前一日全天」口径已下线（它与分钟级数据同源，
不存在独立的降级路径；分钟数据缺失时因子直接记 0 并给 note）。

模块保留原来的 `snapshot()` / `factor_text()` 接口，调用方无需改动。
"""

from __future__ import annotations

from services import volume_ratio


async def snapshot(force: bool = False) -> dict:
    """全A 成交额同时间点同比（30 秒缓存）。"""
    return await volume_ratio.all_a(force=force)


def factor_text(t: dict) -> str | None:
    """放量/缩量信号文案；平量、开盘初期、无数据都不提示。"""
    return volume_ratio.factor_text(t)
