"""告警总线：内存发布/订阅 + 历史，供 SSE 推送与弹窗/声音使用。"""

from __future__ import annotations

import asyncio
import datetime as dt
import itertools
import logging
from collections import deque

log = logging.getLogger("alerts")

_history: deque[dict] = deque(maxlen=300)
_subscribers: set[asyncio.Queue] = set()
_seq = itertools.count(1)
# (etf, type) -> last_ts，用于去重冷却
_cooldown: dict[tuple, float] = {}


def _now_ts() -> float:
    return dt.datetime.now().timestamp()


async def publish(alert: dict) -> dict:
    alert = {
        "id": next(_seq),
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        **alert,
    }
    _history.append(alert)
    dead = []
    for q in _subscribers:
        try:
            q.put_nowait(alert)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        _subscribers.discard(q)
    # 远程推送（尽力而为）：后台派发，失败不影响总线与 SSE 主链路
    try:
        from services import push
        asyncio.get_running_loop().create_task(push.dispatch(alert))
    except Exception:  # noqa: BLE001 — 推送永远不能弄脏主链路
        pass
    return alert


def publish_sync(alert: dict) -> None:
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(publish(alert))
    except RuntimeError:
        pass


def should_alert(etf_code: str, atype: str, cooldown: float = 90) -> bool:
    key = (etf_code, atype)
    now = _now_ts()
    if now - _cooldown.get(key, 0) < cooldown:
        return False
    _cooldown[key] = now
    return True


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subscribers.discard(q)


def history(limit: int = 50) -> list[dict]:
    return list(_history)[-limit:][::-1]
