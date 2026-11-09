"""子进程通道的限流分层与 stderr 消费（全部打桩：不触网、不起真实子进程）。

三条回归：
1. em 的 IPv6 专用槽必须覆盖「起进程 + 读结果」全程，并发上限才真正生效；
2. 等 IPv6 专用槽时不能占着共用 `_SEM` 的全局槽，否则 H5/腾讯/直连请求被一起拖住；
3. kronos 的 stderr 尾部缓冲要随读随更新（进程未退出也能报错），且按进程隔离。
"""

import asyncio
import json

import pytest

from services import em, kronos


async def _no_addr(host: str, deadline: float | None = None) -> None:
    """跳过 DoH 解析，让用例不碰网络。"""
    return None


class _FakeCurlProc:
    """curl 子进程替身：communicate 挂起到测试放行，用来观测真实并发数。"""

    returncode = 0

    def __init__(self, gate: asyncio.Event) -> None:
        self._gate = gate

    async def communicate(self):
        await self._gate.wait()
        return json.dumps({"data": {"ok": 1}}).encode(), b""


def test_v6_semaphore_caps_concurrent_processes(monkeypatch):
    """9 个并发请求，同时在跑的 curl 进程数不得超过专用槽上限。"""
    async def main():
        monkeypatch.setattr(em, "_V6_SEM", asyncio.Semaphore(2))
        monkeypatch.setattr(em, "_SEM", asyncio.Semaphore(16))
        monkeypatch.setattr(em, "_v6_addr", _no_addr)
        gate = asyncio.Event()
        spawned = 0

        async def fake_exec(*args, **kwargs):
            nonlocal spawned
            spawned += 1
            return _FakeCurlProc(gate)

        monkeypatch.setattr(em.asyncio, "create_subprocess_exec", fake_exec)
        deadline = em.time.monotonic() + 30

        async def one():
            # 与 get_json 的真实调用链一致：先过分层限流，再进传输函数
            async with em._throttled("curl6", deadline):
                return await em._curl_json_v6(
                    "https://push2.eastmoney.com/api/qt/stock/get", deadline=deadline)

        tasks = [asyncio.ensure_future(one()) for _ in range(9)]
        for _ in range(100):
            await asyncio.sleep(0)
            if spawned >= 2:
                break
        assert spawned == 2      # 只有 2 个 curl 被创建，其余仍在排队
        gate.set()
        await asyncio.gather(*tasks)
        assert spawned == 9      # 放行后排队者依次执行

    asyncio.run(main())


def test_v6_queue_does_not_hold_global_slot(monkeypatch):
    """IPv6 槽排队期间不占全局槽，共用 _SEM 的请求照常执行（review P1）。"""
    async def enter_v6(deadline):
        async with em._throttled("curl6", deadline):
            pass

    async def main():
        v6 = asyncio.Semaphore(2)
        sem = asyncio.Semaphore(16)
        monkeypatch.setattr(em, "_V6_SEM", v6)
        monkeypatch.setattr(em, "_SEM", sem)
        await v6.acquire()
        await v6.acquire()          # 模拟 IPv6 链路拥塞：专用槽已满

        waiter = asyncio.ensure_future(enter_v6(em.time.monotonic() + 0.2))
        await asyncio.sleep(0.05)
        assert sem._value == 16     # 排队中没有占用任何一个全局槽

        async def other():
            async with em._throttled("direct", em.time.monotonic() + 5):
                return sem._value

        assert await asyncio.wait_for(other(), timeout=1) == 15
        with pytest.raises(em._V6Busy):
            await waiter            # 等槽超预算 → 交给上层换通道，而不是无限挂住
        assert sem._value == 16     # 超时后全局槽完整归还
        assert v6._value == 0       # 专用槽仍被那 2 个"拥塞"请求占着

    asyncio.run(main())


def test_backlog_does_not_starve_other_transports(monkeypatch):
    """IPv6 积压时，其他数据源通过 get_json 仍能正常取数（review 要求覆盖的场景）。"""
    async def main():
        v6 = asyncio.Semaphore(2)
        sem = asyncio.Semaphore(16)
        monkeypatch.setattr(em, "_V6_SEM", v6)
        monkeypatch.setattr(em, "_SEM", sem)
        monkeypatch.setattr(em, "_v6_host", lambda host: host.startswith("push2"))
        monkeypatch.setattr(em, "_v6_dead", lambda host: False)
        monkeypatch.setattr(em, "_push2_dead", lambda host: True)   # 只留 IPv6 通道
        monkeypatch.setattr(em, "_v6_fail", lambda host: None)
        monkeypatch.setattr(em, "_REQUEST_BUDGET", 0.2)
        await v6.acquire()
        await v6.acquire()          # IPv6 通道积压

        async def fake_curl(url, params=None, referer=None, headers=None, deadline=None):
            return {"hot": True}

        monkeypatch.setattr(em, "_curl_json", fake_curl)
        monkeypatch.setitem(em._MEMO, "emdatah5.eastmoney.com", ("curl", em.time.time()))

        backlog = [
            asyncio.ensure_future(em.get_json(
                "https://push2.eastmoney.com/api/qt/stock/get", ttl=0, force=True))
            for _ in range(16)      # 占满全局槽的数量：旧实现会把 _SEM 全部吃光
        ]
        # 共用同一批限流槽（含全局 _SEM）的其它数据源不受影响
        got = await asyncio.wait_for(
            em.get_json("https://emdatah5.eastmoney.com/api/qt/stock/get",
                        ttl=0, force=True),
            timeout=2)
        assert got == {"hot": True}
        assert all(r is None for r in await asyncio.gather(*backlog))

    asyncio.run(main())


def test_throttle_releases_both_slots_on_error(monkeypatch):
    """传输失败时专用槽与全局槽都要归还，避免一次异常永久吃掉并发额度。"""
    async def main():
        v6 = asyncio.Semaphore(2)
        sem = asyncio.Semaphore(16)
        monkeypatch.setattr(em, "_V6_SEM", v6)
        monkeypatch.setattr(em, "_SEM", sem)
        with pytest.raises(RuntimeError, match="boom"):
            async with em._throttled("curl6", em.time.monotonic() + 5):
                raise RuntimeError("boom")
        assert v6._value == 2
        assert sem._value == 16

    asyncio.run(main())


class _FakeStderrProc:
    """最小子进程替身：只有 stderr 流与 returncode，进程处于存活状态。"""

    def __init__(self) -> None:
        self.stderr = asyncio.StreamReader(limit=1 << 20)
        self.returncode = None


async def _drain_until(worker, predicate, tries: int = 200) -> bool:
    for _ in range(tries):
        await asyncio.sleep(0)
        if predicate():
            return True
    return False


def test_stderr_hint_visible_while_process_alive():
    """进程未退出就超时，也要能拿到已经打出来的 stderr（review P2）。"""
    async def main():
        worker = kronos._Worker()
        proc = _FakeStderrProc()
        worker._start_stderr_drain(proc)
        try:
            proc.stderr.feed_data(b"loading weights...\nRuntimeError: CUDA OOM\n")
            assert await _drain_until(worker, lambda: bool(worker._stderr_hint()))
            assert proc.returncode is None          # 进程还活着，并非退出后才可见
            assert "CUDA OOM" in worker._stderr_hint()
        finally:
            await worker._stop_stderr_drain()

    asyncio.run(main())


def test_stderr_tail_isolated_per_process():
    """换进程后不得把上一个进程的日志当成本次错误提示。"""
    async def main():
        worker = kronos._Worker()
        old = _FakeStderrProc()
        worker._start_stderr_drain(old)
        old.stderr.feed_data(b"old: model crashed\n")
        assert await _drain_until(worker, lambda: bool(worker._stderr_hint()))
        assert "old: model crashed" in worker._stderr_hint()

        new = _FakeStderrProc()
        worker._start_stderr_drain(new)             # 模拟重建子进程
        assert worker._stderr_hint() == ""
        new.stderr.feed_data(b"new: model ready\n")
        assert await _drain_until(worker, lambda: bool(worker._stderr_hint()))
        assert "new: model ready" in worker._stderr_hint()
        await worker._stop_stderr_drain()

    asyncio.run(main())


def test_stderr_tail_is_bounded():
    """stderr 洪水只保留尾部固定行数，不随输出无限增长。"""
    async def main():
        worker = kronos._Worker()
        proc = _FakeStderrProc()
        worker._start_stderr_drain(proc)
        try:
            proc.stderr.feed_data(b"".join(f"warn {i}\n".encode() for i in range(4000)))
            assert await _drain_until(
                worker, lambda: "warn 3999" in worker._stderr_hint())
            assert len(worker._stderr_tail) == kronos._STDERR_TAIL
            assert "warn 3999" in worker._stderr_hint()
        finally:
            await worker._stop_stderr_drain()

    asyncio.run(main())
