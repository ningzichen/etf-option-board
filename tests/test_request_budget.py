"""`_REQUEST_BUDGET` 全覆盖回归：全局槽等待、传输执行、重试间隔都在预算内。

review 指出的盲区：原先预算只在"进入 _throttled 之前"校验，`async with _SEM`
的等待与 `await fn()` 本身都不受约束——全局槽拥塞时请求仍会无限挂住，
超过预算后还会继续发起新的传输。

用例全部注入时钟（monkeypatch em.time）、打桩传输、按用例隔离全局状态，
不触网、不起真实子进程。
"""

import asyncio
from types import SimpleNamespace

import pytest

from services import em


@pytest.fixture()
def clock(monkeypatch):
    """注入时钟；信号量与 em 的全局状态按用例隔离。"""
    now = [1000.0]
    monkeypatch.setattr(em, "time", SimpleNamespace(
        time=lambda: now[0],
        monotonic=lambda: now[0],
        strftime=lambda fmt: "2026-11-09",
    ))
    monkeypatch.setattr(em, "_SEM", asyncio.Semaphore(16))
    monkeypatch.setattr(em, "_V6_SEM", asyncio.Semaphore(2))
    monkeypatch.setattr(em, "_CACHE", {})
    monkeypatch.setattr(em, "_MEMO", {})
    return now


def _prefer_curl(monkeypatch, host: str) -> None:
    """让非 IPv6 主机的传输顺序固定为 curl → direct（避免 direct 触网）。"""
    monkeypatch.setitem(em._MEMO, host, ("curl", 999999.0))  # noqa: SLF001


def test_global_slot_wait_respects_budget(monkeypatch, clock):
    """全局槽占满时，请求应在预算耗尽后返回，而不是无限等（review 场景 1）。"""
    async def main():
        # 占满全局槽：模拟全局拥塞
        for _ in range(16):
            await em._SEM.acquire()  # noqa: SLF001
        called: list[int] = []

        async def fake_curl(url, params=None, referer=None, headers=None, deadline=None):
            called.append(1)
            return {"hot": True}

        monkeypatch.setattr(em, "_curl_json", fake_curl)
        _prefer_curl(monkeypatch, "emdatah5.eastmoney.com")

        task = asyncio.ensure_future(em.get_json(
            "https://emdatah5.eastmoney.com/api/qt/stock/get", ttl=0, force=True))
        await asyncio.sleep(0.3)          # 已进入全局槽等待（0.1s 分片轮询中）
        assert not called                 # 没有槽位就不会发起传输
        clock[0] += em._REQUEST_BUDGET + 1  # noqa: SLF001 — 假时钟推过 deadline
        result = await asyncio.wait_for(task, timeout=3)
        assert result is None             # 预算耗尽 → 降级（无 stale 缓存）
        assert not called                 # 从未发起过传输

    asyncio.run(main())


def test_no_new_transport_started_after_budget(monkeypatch, clock):
    """某条通道在预算内启动、跨过 deadline 才返回：响应照收，但不再起新通道。"""
    async def main():
        gate = asyncio.Event()
        called: list[str] = []

        async def fake_curl(url, params=None, referer=None, headers=None, deadline=None):
            called.append("curl")
            await gate.wait()
            return {"late": True}

        async def fake_direct():  # 若预算失效，这条会被再次发起
            called.append("direct")
            return {"should-not-happen": True}

        monkeypatch.setattr(em, "_curl_json", fake_curl)
        monkeypatch.setattr(em, "_client", lambda: SimpleNamespace(
            get=fake_direct))  # noqa: SLF001 — direct 走 _client().get
        _prefer_curl(monkeypatch, "emdatah5.eastmoney.com")

        task = asyncio.ensure_future(em.get_json(
            "https://emdatah5.eastmoney.com/api/qt/stock/get", ttl=0, force=True))
        await asyncio.sleep(0.05)
        assert called == ["curl"]         # curl 已在预算内启动并挂起
        clock[0] += em._REQUEST_BUDGET + 1  # noqa: SLF001 — 执行期间跨过 deadline
        gate.set()
        result = await asyncio.wait_for(task, timeout=3)
        assert result == {"late": True}   # 已到手的响应不丢弃
        assert called == ["curl"]         # 预算耗尽后没有再发起任何传输

    asyncio.run(main())


class _HangingCurl:
    """communicate 永远挂起的 curl 进程替身：用于验证超预算时的回收。"""

    returncode = None

    def __init__(self) -> None:
        self.killed = False
        self.reaped = False

    async def communicate(self):
        await asyncio.Event().wait()      # 永不 set
        return b"", b""

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> None:
        self.reaped = True


def test_curl_kills_subprocess_when_budget_expires(monkeypatch, clock):
    """curl 执行跨过预算：抛 _BudgetExceeded 并终止回收子进程（不允许孤儿进程）。"""
    async def main():
        procs: list[_HangingCurl] = []

        async def fake_exec(*args, **kwargs):
            proc = _HangingCurl()
            procs.append(proc)
            return proc

        monkeypatch.setattr(em.asyncio, "create_subprocess_exec", fake_exec)
        deadline = clock[0] + 0.4         # 剩余 0.4s（真实等待，确定触发）
        with pytest.raises(em._BudgetExceeded):
            await asyncio.wait_for(
                em._curl_json("https://emdatah5.eastmoney.com/x", deadline=deadline),
                timeout=3)
        assert len(procs) == 1
        assert procs[0].killed and procs[0].reaped

    asyncio.run(main())


def test_curl6_kills_subprocess_when_budget_expires(monkeypatch, clock):
    """IPv6 通道的 curl 执行跨过预算：同样终止回收子进程。"""
    async def main():
        async def no_addr(host, deadline=None):
            return None

        procs: list[_HangingCurl] = []

        async def fake_exec(*args, **kwargs):
            proc = _HangingCurl()
            procs.append(proc)
            return proc

        monkeypatch.setattr(em, "_v6_addr", no_addr)
        monkeypatch.setattr(em.asyncio, "create_subprocess_exec", fake_exec)
        deadline = clock[0] + 0.6         # 高于 0.5s 的"预算耗尽跳过"阈值
        with pytest.raises(em._BudgetExceeded):
            await asyncio.wait_for(
                em._curl_json_v6(
                    "https://push2.eastmoney.com/api/qt/stock/get", deadline=deadline),
                timeout=3)
        assert procs[0].killed and procs[0].reaped

    asyncio.run(main())


def test_retry_sleep_skipped_when_budget_tight(monkeypatch, clock):
    """预算只剩很少时，重试间隔不再空等，直接进入降级路径。"""
    async def main():
        sleeps: list[float] = []
        calls = 0

        async def fake_sleep(delay):
            sleeps.append(delay)

        async def fail_curl(url, params=None, referer=None, headers=None, deadline=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                # 首次传输"耗时"几乎整个预算：失败后剩余预算不足一个重试间隔
                clock[0] += em._REQUEST_BUDGET - 0.2  # noqa: SLF001
            raise RuntimeError("上游拒绝")

        monkeypatch.setattr(em.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(em, "_curl_json", fail_curl)
        monkeypatch.setattr(em, "_client", lambda: SimpleNamespace(
            get=fail_curl))  # noqa: SLF001
        _prefer_curl(monkeypatch, "emdatah5.eastmoney.com")
        result = await asyncio.wait_for(
            em.get_json("https://emdatah5.eastmoney.com/x", ttl=0, force=True),
            timeout=3)
        assert result is None
        assert not sleeps                 # 剩余预算不足一个重试间隔 → 不空等

    asyncio.run(main())


def test_slot_returned_when_cancelled_right_after_acquire(clock):
    """P1：刚取得槽位就被取消，槽位必须归还（否则并发额度被永久吃掉）。"""
    async def main():
        holder = None

        class CancelAfterAcquire(asyncio.Semaphore):
            """取得槽位后立即取消等待者，模拟"拿到位置那一刻被取消"。"""

            async def acquire(self):
                ok = await super().acquire()
                if holder is not None:
                    holder.cancel()
                return ok

        sem = CancelAfterAcquire(1)
        holder = asyncio.ensure_future(
            em._acquire_with_budget(sem, clock[0] + 30, "全局槽"))  # noqa: SLF001
        outcome = await asyncio.gather(holder, return_exceptions=True)
        assert isinstance(outcome[0], asyncio.CancelledError)
        assert sem._value == 1            # 位置已归还，不是 0

    asyncio.run(main())


def test_direct_aborted_when_budget_expires(monkeypatch, clock):
    """P2：direct 必须受总预算约束（httpx 的分阶段超时不是整次请求的总时限）。"""
    async def main():
        curl_calls: list[str] = []

        async def hanging_get(*args, **kwargs):
            await asyncio.Event().wait()   # 永不返回，模拟服务端慢速滴数据

        async def fake_curl(url, params=None, referer=None, headers=None, deadline=None):
            curl_calls.append("curl")
            return {"should-not-happen": True}

        monkeypatch.setattr(em, "_client", lambda: SimpleNamespace(get=hanging_get))
        monkeypatch.setattr(em, "_curl_json", fake_curl)
        monkeypatch.setattr(em, "_REQUEST_BUDGET", 0.05)  # noqa: SLF001
        monkeypatch.setitem(em._MEMO, "emdatah5.eastmoney.com",
                            ("direct", 999999.0))  # noqa: SLF001 — 让 direct 先试
        loop = asyncio.get_running_loop()
        started = loop.time()
        result = await asyncio.wait_for(
            em.get_json("https://emdatah5.eastmoney.com/x", ttl=0, force=True),
            timeout=3)
        elapsed = loop.time() - started
        assert result is None             # 预算耗尽 → 降级返回，不挂住
        assert elapsed < 1.0              # 远小于等待上限，说明被预算截断
        assert not curl_calls             # 预算已尽，不再发起下一条通道

    asyncio.run(main())
