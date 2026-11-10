"""东方财富行情接口客户端（异步 httpx，直连 + curl 兜底 + 短 TTL 缓存）。

所有请求手拼 query（safe=",:"），否则 fs=m:10+t:10 等会被编码成 %3A 而返回空数据。
注意 `+` **不能**放进 safe：raw `+` 在 query 里等价空格，emdatah5 会忽略 fs 过滤
（实测 fs=m:90+t:3 返回 1113 条全板块，编码成 %2B 才返回正确的 531 条概念板块）。
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import os
import re
import time
from urllib.parse import urlencode

import httpx

from config import settings
from paths import data_path

log = logging.getLogger("em")

_UA = settings.user_agent
_UT = settings.em_token
_REFERER = "https://quote.eastmoney.com/"

# H5 快照主源：emdatah5 直连稳定（push2 被代理/限流时仍可用）。
#   getZJLXData  = push2 stock/get 等价（个股/指数/外围/期权行情 + 资金流）
#   getZDYLBData = push2 clist 等价（列表：期权链/板块/成分股）
_H5_URL = "https://emdatah5.eastmoney.com/dc/ZJLX"
_H5_UT = "fa5fd1943c7b386f172d6893dbfba10b"
_H5_REF = "https://data.eastmoney.com/"
# 腾讯行情：K 线/分时兜底（push2his / push2 trends 不可用时）
# 注意：一律用 ifzq.gtimg.cn（不带 web. 前缀）。web.ifzq.gtimg.cn 会触发腾讯 WAF，
# 间歇性返回 JS 挑战页 → json 解析失败 → 回落 push2（多为空），造成"没数据/旧数据"。
_TX_DAY = "https://ifzq.gtimg.cn/appstock/app/fqkline/get"
# 不复权日/周/月线：fqkline（复权）端点会间歇命中腾讯 WAF（返回挑战页 → json 解析失败），
# 指数尤其如此（qfq 对指数本无意义）。取不到复权线时退回这一条。
_TX_RAW = "https://ifzq.gtimg.cn/appstock/app/kline/kline"
_TX_MIN = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
_TX_TREND = "https://ifzq.gtimg.cn/appstock/app/minute/query"
# 多日分时：盘前当日只有 09:30 一个占位点时，用它回退到上一完整交易日
_TX_TREND_DAYS = "https://ifzq.gtimg.cn/appstock/app/day/query"
# 回退到昨日分时时，记录该日的昨收（day/query 的 prec），供前端算涨跌幅基准
_TREND_PREV_CLOSE: dict[str, float] = {}


_CLIENT: httpx.AsyncClient | None = None
_CACHE: dict[str, tuple[float, float, object]] = {}
# 传输路径记忆：(host|path) -> (mode, ts)；mode: "direct" | "curl"
_MEMO: dict[str, tuple[str, float]] = {}
_MEMO_TTL = 300
# 死主机熔断：push2/push2his 的行情类端点已确认"对普通 GET 一律空回包"（curl、真实
# Chrome、国内 5G 关闭代理均为 0 字节），但它们在兜底路径上会被反复尝试，每次白等
# connect 超时（实测 /api/option/trends 因此要 2.8s）。这里按主机记一次失败，60s 内
# 直接跳过；到期自动重试，上游若恢复即刻生效。
_PUSH2_DEAD: dict[str, float] = {}
_PUSH2_DEAD_TTL = 60.0


def _push2_dead(host: str) -> bool:
    return time.time() < _PUSH2_DEAD.get(host, 0.0)


def _push2_mark_dead(host: str) -> None:
    _PUSH2_DEAD[host] = time.time() + _PUSH2_DEAD_TTL
    log.info("em push2 兜底主机 %s 空回包，%ds 内跳过", host, int(_PUSH2_DEAD_TTL))


# push2 的 IPv6 通道（合约分时/K线的主力来源）
#
# 本机出口是境外 IPv4，push2/push2his 对境外 IP 反爬（GET 一律空回包，连 ndays=5 的
# 历史分时都拿不到）。但这些域名的 AAAA 记录指向国内 IPv6 节点，走 IPv6 出网就能拿到
# 真实数据——这是期权分时唯一的实时来源，所以单独给它一条通道：
#   - 用 curl -6 --doh-url 走国内 DoH 解析（系统 DNS 常把 AAAA 过滤掉）；
#   - 并发上限 2（IPv6 路径容易丢包，压太多反而全超时）；
#   - 抖动很正常，**连续 5 次**失败才熔断 60s，别一次空包就放弃整条通道。
_V6_HOSTS = ("push2.eastmoney.com", "push2his.eastmoney.com", "push2delay.eastmoney.com")
_DOH_URLS = ("https://dns.alidns.com/dns-query", "https://doh.pub/dns-query")
_V6_SEM = asyncio.Semaphore(2)
_V6_DEAD: dict[str, float] = {}
_V6_DEAD_TTL = 60.0
_V6_FAILS: dict[str, int] = {}
_V6_FAIL_LIMIT = 2
_V6_TIMEOUT = "8"
# IPv6 槽排队与整次取数的预算：等槽时间 curl 的 -m 覆盖不到，必须单独兜住，
# 否则链路拥塞时请求会一直挂在队列里。
_V6_QUEUE_BUDGET = 5.0
_REQUEST_BUDGET = 20.0

# AAAA 解析缓存：curl 每次用 --doh-url 都要现场 DoH 解析，实测占单请求 1.4s 里的
# **1.11s**（79%）。解析一次缓存 10 分钟、改用 --resolve 固定 IPv6 地址，单请求降到
# 0.2~0.3s。IP 轮换/失效时下一次 DoH 兜底会重新解析。
_V6_ADDR: dict[str, tuple[str, float]] = {}
_V6_ADDR_TTL = 600.0


async def _v6_addr(host: str, deadline: float | None = None) -> str | None:
    """DoH 查 AAAA 并缓存（阿里/腾讯的 JSON 接口，走普通 IPv4 请求）。"""
    hit = _V6_ADDR.get(host)
    if hit and time.time() < hit[1]:
        return hit[0]
    for base in ("https://dns.alidns.com/resolve", "https://doh.pub/resolve"):
        try:
            d = await _curl_json(base, {"name": host, "type": "AAAA"},
                                 referer=base, deadline=deadline)
        except Exception:  # noqa: BLE001
            continue
        ips = [a.get("data") for a in (d or {}).get("Answer", [])
               if isinstance(a, dict) and a.get("type") == 28]
        if ips:
            _V6_ADDR[host] = (ips[0], time.time() + _V6_ADDR_TTL)
            return ips[0]
    return None


def _host_of(url: str) -> str:
    return url.split("//", 1)[-1].split("/", 1)[0]


def _v6_host(host: str) -> bool:
    return host in _V6_HOSTS


def _v6_dead(host: str) -> bool:
    return time.time() < _V6_DEAD.get(host, 0.0)


def _v6_mark_dead(host: str) -> None:
    _V6_DEAD[host] = time.time() + _V6_DEAD_TTL
    _V6_FAILS[host] = 0
    log.info("em push2 IPv6 通道 %s 连续 %d 次无回包，%ds 内跳过",
             host, _V6_FAIL_LIMIT, int(_V6_DEAD_TTL))


def _v6_ok(host: str) -> None:
    _V6_FAILS.pop(host, None)


def _v6_fail(host: str) -> None:
    n = _V6_FAILS.get(host, 0) + 1
    _V6_FAILS[host] = n
    if n >= _V6_FAIL_LIMIT:
        _v6_mark_dead(host)


class _V6EmptyReply(RuntimeError):
    """IPv6 通道正常，但该接口空回包（东财按接口封禁，不代表通道坏了）。"""


async def _curl_json_v6(url: str, params: dict | None = None,
                        referer: str | None = None,
                        deadline: float | None = None) -> object:
    """curl -6 取 JSON（push2 域名的 IPv6 通道）。

    优先用缓存的 AAAA 地址 + `--resolve`（省掉每次 1.1s 的 DoH 解析）；
    解析失败/地址失效时才退回 `--doh-url` 现场解析。

    限流由调用方 `_throttled()` 统一负责（IPv6 专用槽必须先于全局槽获取，见该函数）。
    `deadline` 为 `time.monotonic()` 口径的截止时刻：预算用尽后不再发起新的 curl 尝试，
    因为 `curl -m` 只约束单次进程运行时长，管不住排队和多次重试的累计耗时。
    """
    host = _host_of(url)
    p = dict(params or {})
    p["_"] = int(time.time() * 1000)
    full = _build(url, p)
    addr = await _v6_addr(host, deadline=deadline)
    attempts: list[list[str]] = []
    if addr:
        attempts.append(["--resolve", f"{host}:443:[{addr}]"])
    for doh in _DOH_URLS:
        attempts.append(["--doh-url", doh])
    err: str | None = None
    codes: list[int | None] = []
    for i, extra in enumerate(attempts):
        budget = None if deadline is None else deadline - time.monotonic()
        if budget is not None and budget <= 0.5:
            err = f"try{i} 跳过（预算耗尽）"
            break
        timeout = _V6_TIMEOUT if budget is None else \
            f"{max(0.1, min(float(_V6_TIMEOUT), budget)):.1f}"
        proc = await asyncio.create_subprocess_exec(
            "curl", "-s", "-6", "-m", timeout, *extra,
            "-A", _UA, "-e", referer or _REFERER,
            "-H", "Accept: application/json, text/plain, */*",
            full,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=(
                None if budget is None else max(0.05, budget)))
        except asyncio.TimeoutError as exc:
            # 预算内没等到结果：终止并回收子进程（预算用尽不应留下孤儿 curl）
            proc.kill()
            await proc.wait()
            raise _BudgetExceeded(f"curl6 执行超预算 {host}") from exc
        except asyncio.CancelledError:
            proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            raise
        codes.append(proc.returncode)
        body = out.decode("utf-8", "replace").strip()
        if not body:
            # 固定 IP 那条路空回包：可能地址已轮换，丢掉缓存让下一次重新解析
            if i == 0 and addr:
                _V6_ADDR.pop(host, None)
            err = f"try{i} 空回包(exit={proc.returncode})"
            continue
        try:
            return json.loads(body)
        except ValueError as exc:
            err = f"try{i} 非法 JSON: {exc}"
    # 全部是 curl exit 52（Empty reply）= 接口级封禁，通道本身是通的：
    # 这种情况不能算通道故障，否则 trends2 被封会把 clist 等可用接口一起熔断。
    if codes and all(c == 52 for c in codes):
        raise _V6EmptyReply(f"接口空回包 {host}（{err}）")
    raise RuntimeError(f"curl6 无回包 {host}（{err}）")
# 东财/腾讯抖动重试：重试会放大请求量（触发反爬），配合"过期缓存兜底"改为 2 轮
_RETRY_TRIES = 2
_RETRY_DELAY = 0.3
# 全局并发闸：东财对同 IP 并发突发很敏感；主源已切 H5/腾讯，且上游实测
# httpx 直连 0.02~0.2s，放宽到 16 提升吞吐（9 只标的并发聚合时不再排队）
_SEM = asyncio.Semaphore(16)


class _V6Busy(RuntimeError):
    """IPv6 专用槽排队超预算（通道未必坏，只是拥塞）。"""


class _BudgetExceeded(RuntimeError):
    """整次取数的总预算耗尽（拥塞兜底，不算通道故障）。"""


# 槽等待的轮询间隔：预算按注入时钟计算，实际等待按 loop 时间分片推进，
# 既保证生产环境语义正确，也让测试注入的假时钟推进能被及时感知。
_BUDGET_POLL = 0.1


async def _acquire_with_budget(sem: asyncio.Semaphore, deadline: float,
                               what: str) -> None:
    """在预算内获取一个信号量槽（等待受 `deadline` 约束，不再无限挂住）。

    预算用 `time.monotonic()`（可被测试注入）计算；等待用 `asyncio.wait`
    分片观察，超预算立即放弃并抛 `_BudgetExceeded`。

    退出清理要区分三种状态：仍在等待（cancel 即可）、异常结束（无槽可还）、
    **已经取得但父任务被取消**（cancel 撤不回已完成的获取，必须显式 release，
    否则信号量额度会被永久吃掉，反复发生后并发能力持续衰减）。
    """
    if time.monotonic() >= deadline:
        raise _BudgetExceeded(f"{what}排队超预算（预算已耗尽）")
    task = asyncio.ensure_future(sem.acquire())
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _BudgetExceeded(f"{what}排队超预算")
            done, _ = await asyncio.wait({task}, timeout=min(remaining, _BUDGET_POLL))
            if task in done:
                return
    except BaseException:
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        # 取消发生在"已取得之后"：acquire 不可撤销，槽位必须归还
        if task.done() and not task.cancelled() and task.exception() is None:
            sem.release()
        raise


@contextlib.asynccontextmanager
async def _throttled(name: str, deadline: float):
    """按层次获取限流槽：IPv6 专用槽必须先于全局 `_SEM` 获取。

    调用方（`get_json`）原本先拿全局槽再进传输函数，若 IPv6 专用槽在内部排队，
    拥塞时 16 个全局槽会被等待者占满（实际只有 2 个 curl 在跑），共用 `_SEM`
    的 H5/腾讯/直连请求会被一起拖住。这里把专用槽提到全局槽外层。

    两把锁的等待都受整次取数预算约束：全局槽拥塞时最多等到预算耗尽，
    然后抛 `_BudgetExceeded` 让上层换通道/降级，而不是无限挂住。
    """
    if name != "curl6":
        await _acquire_with_budget(_SEM, deadline, "全局槽")
        try:
            yield
        finally:
            _SEM.release()
        return
    budget = max(0.0, min(deadline - time.monotonic(), _V6_QUEUE_BUDGET))
    try:
        await _acquire_with_budget(_V6_SEM, time.monotonic() + budget, "IPv6 专用槽")
    except _BudgetExceeded as exc:
        raise _V6Busy(str(exc)) from exc
    try:
        await _acquire_with_budget(_SEM, deadline, "全局槽")
        try:
            yield
        finally:
            _SEM.release()
    finally:
        _V6_SEM.release()


def _client() -> httpx.AsyncClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.http_timeout, connect=2.5),
            headers={"User-Agent": _UA, "Referer": _REFERER},
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        )
    return _CLIENT


async def close() -> None:
    global _CLIENT
    if _CLIENT is not None:
        await _CLIENT.aclose()
        _CLIENT = None


async def llm_post(url: str, headers: dict, body: dict) -> dict:
    """临时模型请求：独立客户端，不复用行情缓存、Cookie、Referer 或失败重试。"""
    try:
        async with asyncio.timeout(280):
            async with httpx.AsyncClient(timeout=httpx.Timeout(270, connect=10),
                                         follow_redirects=False) as client:
                async with client.stream("POST", url, headers=headers, json=body) as response:
                    if not 200 <= response.status_code < 300:
                        # 错误正文可能反射 Authorization，既不读取也不返回它。
                        return {"status": response.status_code}
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > 4 * 1024 * 1024:
                            return {"error": "模型响应过大，请更换模型或缩短输出"}
                    return {"status": response.status_code, "text": content.decode("utf-8", errors="replace")}
    except (TimeoutError, httpx.TimeoutException):
        return {"error": "看板后端连接模型超时，请稍后重试"}
    except Exception:  # noqa: BLE001
        # 不打印异常或请求对象：其中可能含 API Key、地址或提示词。
        return {"error": "看板后端无法连接模型服务，请检查地址、网络和证书"}


def _build(url: str, params: dict) -> str:
    if not params:
        return url
    return f"{url}?{urlencode(params, safe=',:')}"


def _hdr_args(headers: dict[str, str] | None) -> list[str]:
    """自定义请求头 → curl 参数（User-Agent 单独走 -A，避免与默认 UA 打架）。"""
    args: list[str] = []
    for k, v in (headers or {}).items():
        if k.lower() == "user-agent":
            continue
        args += ["-H", f"{k}: {v}"]
    return args


async def _curl_json(url: str, params: dict | None = None,
                     referer: str | None = None,
                     headers: dict[str, str] | None = None,
                     deadline: float | None = None) -> object:
    p = dict(params or {})
    p["_"] = int(time.time() * 1000)
    full = _build(url, p)
    ua = (headers or {}).get("User-Agent") or (headers or {}).get("user-agent") or _UA
    # -m 尽量贴合剩余预算（下限 0.1 防止负数/0），wait_for 仅作保险并负责 kill/reap
    timeout = "8"
    if deadline is not None:
        timeout = f"{max(0.1, min(8.0, deadline - time.monotonic())):.1f}"
    proc = await asyncio.create_subprocess_exec(
        "curl", "-s", "-m", timeout, "-A", ua, "-e", referer or _REFERER,
        "-H", "Accept: application/json, text/plain, */*", *_hdr_args(headers),
        full,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=(
            None if deadline is None else max(0.05, deadline - time.monotonic())))
    except asyncio.TimeoutError as exc:
        # 预算内没等到结果：终止并回收子进程，不能只释放信号量让它继续跑
        proc.kill()
        await proc.wait()
        raise _BudgetExceeded(f"curl 执行超预算 {_host_of(url)}") from exc
    except asyncio.CancelledError:
        proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        raise
    return json.loads(out.decode("utf-8", "replace"))


async def get_json(url: str, params: dict | None = None, ttl: float = 3.0,
                   force: bool = False, referer: str | None = None,
                   tries: int | None = None,
                   headers: dict[str, str] | None = None) -> object | None:
    """带缓存与传输兜底的 GET JSON。失败返回 None（绝不抛给上层）。

    tries 可覆盖重试轮数（默认 _RETRY_TRIES）；对"已确定死掉且非关键"的兜底主机
    传 tries=1 以快速失败。headers 用于个别上游要求特定 UA/头的场景（如同花顺），
    只在 httpx 直连与 curl 两条通道上生效，不影响缓存键。
    """
    params = params or {}
    key = _build(url, params)
    now = time.time()
    if not force:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < hit[1]:
            return hit[2]

    n_tries = _RETRY_TRIES if tries is None else max(1, tries)
    # 整次取数（含所有传输通道与重试）的总预算，避免拥塞时无限延长
    deadline = time.monotonic() + _REQUEST_BUDGET
    hostkey = _host_of(url)
    memo = _MEMO.get(hostkey)
    prefer = memo[0] if memo and now - memo[1] < _MEMO_TTL else None

    async def _direct():
        h = {"Referer": referer} if referer else {}
        h.update(headers or {})
        # httpx.Timeout 是分阶段（connect/read/write）计时，read 超时按"每个数据块"
        # 计算，服务端慢速滴数据时整个请求仍可能远超总预算 —— 这里必须再包一层
        # 以剩余预算为准的总时限（取消协程无副作用，连接会被回收）
        try:
            r = await asyncio.wait_for(
                _client().get(_build(url, params), headers=h or None),
                timeout=max(0.05, deadline - time.monotonic()))
        except asyncio.TimeoutError as exc:
            raise _BudgetExceeded(f"direct 执行超预算 {hostkey}") from exc
        r.raise_for_status()
        return r.json()

    async def _curl():
        return await _curl_json(url, params, referer, headers, deadline=deadline)

    async def _curl6():
        return await _curl_json_v6(url, params, referer, deadline=deadline)

    if _v6_host(hostkey):
        # push2 家族：IPv6 优先（境外 IPv4 必被反爬），IPv6 熔断期间才退回 IPv4
        transports = [] if _v6_dead(hostkey) else [("curl6", _curl6)]
        if not _push2_dead(hostkey):
            transports += [("curl", _curl), ("direct", _direct)]
    else:
        transports = [("curl", _curl), ("direct", _direct)]
        if prefer != "curl":
            transports.reverse()

    data = None
    used = None
    v6_empty = False
    v6_busy = False
    budget_out = False
    # 东财限流：单次成功率约 30~50%，失败即重试（direct 被 WAF 拒绝时很快失败）
    for attempt in range(n_tries):
        for name, fn in transports:
            # 预算一旦判定耗尽就停止尝试（不依赖下一次时钟检查"顺带"挡住）
            if budget_out or time.monotonic() >= deadline:
                break
            try:
                async with _throttled(name, deadline):
                    if time.monotonic() >= deadline:
                        # 拿到槽时预算已尽：不再发起传输（review 指出的盲区）
                        raise _BudgetExceeded(f"预算耗尽（发起 {name} 前）")
                    data = await fn()
            except Exception as exc:  # noqa: BLE001
                if isinstance(exc, _V6EmptyReply):
                    v6_empty = True
                elif isinstance(exc, _V6Busy):
                    # 排队超预算是"拥塞"，不是通道故障，不能计入熔断
                    v6_busy = True
                elif isinstance(exc, _BudgetExceeded):
                    budget_out = True
                log.debug("em fetch %s via %s 失败: %s", hostkey, name, exc)
                data = None
            if data is not None:
                used = name
                break
        if data is not None or budget_out:
            break
        if attempt < n_tries - 1 and time.monotonic() < deadline - _RETRY_DELAY:
            await asyncio.sleep(_RETRY_DELAY)

    if _v6_host(hostkey):
        if data is not None:
            _v6_ok(hostkey)
        elif not _v6_dead(hostkey) and not v6_empty and not v6_busy and not budget_out:
            _v6_fail(hostkey)

    if data is None:
        # 全部尝试失败 → 退回上次成功值（忽略 TTL），避免上游瞬时抖动导致页面缺数据
        stale = _CACHE.get(key)
        if stale and stale[2] is not None:
            log.debug("em 使用过期缓存 url=%s", key)
            return stale[2]
        return None
    _MEMO[hostkey] = (used, now)

    if isinstance(data, dict) and data.get("rc") not in (None, 0):
        log.debug("em rc=%s url=%s", data.get("rc"), key)

    _CACHE[key] = (now, ttl, data)
    return data


def _fields1():
    return "f1,f2,f3,f4,f5,f6"


def _fields2():
    return "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"


# --------------------------------------------------------------------------
# 行情（主源 emdatah5，兜底 push2）
# --------------------------------------------------------------------------
async def clist(fs: str, fields: str, pn: int = 1, pz: int = 100, fid: str = "f12",
                po: int = 1, ttl: float = 15.0) -> list[dict]:
    # 主源：emdatah5 getZDYLBData（与 push2 clist 同参同构，稳定直连）
    d = await get_json(f"{_H5_URL}/getZDYLBData",
                       {"fields": fields, "pn": pn, "pz": pz, "fid": fid,
                        "po": po, "fs": fs, "ut": _H5_UT},
                       ttl=ttl, referer=_H5_REF)
    data = (d or {}).get("data") if isinstance(d, dict) else None
    if data and data.get("diff"):
        return data["diff"]
    # 兜底：push2 clist
    d = await get_json("https://push2.eastmoney.com/api/qt/clist/get",
                       {"pn": pn, "pz": pz, "po": po, "np": 1, "fltt": 2, "invt": 2,
                        "fid": fid, "fs": fs, "fields": fields, "ut": _UT}, ttl=ttl)
    if not d or not d.get("data"):
        return []
    return d["data"].get("diff") or []


async def ulist_quotes(secids: list[str], batch: int = 50, ttl: float = 12.0,
                       fields: str = "f12,f2,f3,f4,f31,f32,f5,f6,f108") -> dict[str, dict]:
    """批量取指定 secid 的最新行情（push2 `ulist.np/get`）。

    中金所期权（m:221）的 `clist` 不返回最新价（f2 恒为 "-"），只能靠它补齐；
    实测一批 60 个 secid 仍全部有价，50 一批够用。
    """
    out: dict[str, dict] = {}
    for i in range(0, len(secids), batch):
        chunk = secids[i:i + batch]
        d = await get_json("https://push2.eastmoney.com/api/qt/ulist.np/get",
                           {"secids": ",".join(chunk), "ut": _UT, "fields": fields,
                            "fltt": 2, "invt": 2}, ttl=ttl)
        for it in ((d or {}).get("data") or {}).get("diff") or []:
            code = it.get("f12")
            if code:
                out[code] = it
    return out


async def clist_all(fs: str, fields: str, pz: int = 100, max_page: int = 8,
                    ttl: float = 20.0) -> list[dict]:
    out: list[dict] = []
    for pn in range(1, max_page + 1):
        rows = await clist(fs, fields, pn=pn, pz=pz, ttl=ttl)
        if not rows:
            break
        out.extend(rows)
        if len(rows) < pz:
            break
    return out


async def stock_get(secid: str, fields: str, ttl: float = 12.0) -> dict | None:
    # 主源：emdatah5 getZJLXData（个股/指数/外围/期权行情）
    d = await get_json(f"{_H5_URL}/getZJLXData",
                       {"secid": secid, "fields": fields, "ut": _H5_UT},
                       ttl=ttl, referer=_H5_REF)
    data = (d or {}).get("data") if isinstance(d, dict) else None
    if data:
        return data
    # 兜底：push2 stock/get
    d = await get_json("https://push2.eastmoney.com/api/qt/stock/get",
                       {"secid": secid, "fltt": 2, "invt": 2, "fields": fields, "ut": _UT}, ttl=ttl)
    if not d or not d.get("data"):
        return None
    return d["data"]


def _tx_code(secid: str) -> str | None:
    """东财 secid -> 腾讯行情代码（沪 1./深 0.）；期权(10./12.)与外围(100.)不支持。"""
    mk, _, code = secid.partition(".")
    if mk == "1":
        return "sh" + code
    if mk == "0":
        return "sz" + code
    return None


def _numf(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


async def _tx_kline(secid: str, klt: int, lmt: int, ttl: float) -> list[dict]:
    code = _tx_code(secid)
    if not code:
        return []
    if klt >= 100:  # 日/周/月线
        period = {101: "day", 102: "week", 103: "month"}.get(klt, "day")
        d = await get_json(_TX_DAY, {"param": f"{code},{period},,,{lmt},qfq"}, ttl=ttl)
        node = ((d or {}).get("data") or {}).get(code) or {}
        rows = node.get(f"qfq{period}") or node.get(period) or []
        if not rows:
            # 复权端点被 WAF 拦（返 None）时退回不复权 K 线，否则日线/MA20/均线关键位全空
            d = await get_json(_TX_RAW, {"param": f"{code},{period},,,{lmt}"}, ttl=ttl)
            node = ((d or {}).get("data") or {}).get(code) or {}
            rows = node.get(period) or []
        # row: [date, open, close, high, low, volume, ...]
        return [{"date": r[0], "open": _numf(r[1]), "close": _numf(r[2]),
                 "high": _numf(r[3]), "low": _numf(r[4]),
                 "volume": _numf(r[5]), "amount": 0.0, "pct": None}
                for r in rows if len(r) >= 6]
    m = {3: "m1", 5: "m5", 15: "m15", 30: "m30", 60: "m60"}.get(klt, "m1")
    d = await get_json(_TX_MIN, {"param": f"{code},{m},,{lmt}"}, ttl=ttl)
    node = ((d or {}).get("data") or {}).get(code) or {}
    out = []
    for r in node.get(m) or []:
        # row: [datetime, open, close, high, low, volume, {}, amount]
        if len(r) < 6:
            continue
        t = str(r[0])
        if len(t) == 12:  # "202610141102" -> "2026-10-14 11:02"
            t = f"{t[0:4]}-{t[4:6]}-{t[6:8]} {t[8:10]}:{t[10:12]}"
        out.append({"date": t, "open": _numf(r[1]), "close": _numf(r[2]),
                    "high": _numf(r[3]), "low": _numf(r[4]),
                    "volume": _numf(r[5]), "amount": 0.0, "pct": None})
    return out


async def kline(secid: str, klt: int = 1, lmt: int = 240, fqt: int = 1,
                ttl: float = 45.0) -> list[dict]:
    """历史 K 线（归一化 bars）。腾讯优先（稳定），push2his 兜底（期权只剩这条路）。"""
    bars = await _tx_kline(secid, klt, lmt, ttl)
    if bars:
        return bars
    h = "push2his.eastmoney.com"
    # 两条通道都熔断才短路；IPv6 是期权 K 线唯一的真实来源，不能因 IPv4 死掉就放弃
    if _push2_dead(h) and _v6_dead(h):
        return []
    d = await get_json("https://push2his.eastmoney.com/api/qt/stock/kline/get",
                       {"secid": secid, "fields1": _fields1(), "fields2": _fields2(),
                        "klt": klt, "fqt": fqt, "end": "20500101", "lmt": lmt, "ut": _UT},
                       ttl=ttl)
    if d is None:
        _push2_mark_dead(h)
        return []
    return parse_klines(d.get("data"))


def parse_day_series(rows: list, ndays: int = 5) -> list[dict]:
    """把腾讯 day/query 的 data 列表解析成**按日期升序**的分钟累计成交额序列。

    以数据源自身排序为准：腾讯返回的是新→旧，这里统一升序再取最近 ndays 天，
    避免「取最后 N 条」实际取到最旧的几天（曾因此把前 4 天的成交量当今日比）。
    """
    out = []
    for day in rows or []:
        date = str(day.get("date") or "")
        if len(date) != 8:
            continue
        points = []
        for line in day.get("data") or []:
            p = str(line).split()
            if len(p) > 3:
                amt = _numf(p[3])
                if amt > 0:
                    points.append((p[0], amt))
        if points:
            out.append({"date": f"{date[0:4]}-{date[4:6]}-{date[6:8]}",
                        "points": points})
    out.sort(key=lambda d: d["date"])
    return out[-max(1, ndays):]


async def day_series(secid: str, ndays: int = 5, ttl: float = 60.0,
                     bust: bool = False) -> list[dict]:
    """最近 N 个交易日的**分钟级累计成交额**（腾讯 day/query）。

    腾讯 day/query 每天给一串 "HHMM 价格 累计成交量 累计成交额"，
    返回 [{"date": "YYYY-MM-DD", "points": [(hhmm, 累计成交额元), ...]}]，按日期升序。
    指数成交额即该市场总额（沪市/深市），是全A成交额同比因子的数据源。

    `bust=True` 会在 query 里带一个时间戳参数：腾讯的边缘节点偶尔回上一天
    的缓存（盘中会拿到缺今天的 5 日窗口），靠它绕开 CDN 缓存重取。
    """
    code = _tx_code(secid)
    if not code:
        return []
    params = {"code": code}
    if bust:
        params["_"] = str(int(time.time() * 1000))
    d = await get_json(_TX_TREND_DAYS, params, ttl=ttl)
    node = ((d or {}).get("data") or {}).get(code) or {}
    return parse_day_series(node.get("data") or [], ndays)


async def day_amounts(secid: str, ndays: int = 5, ttl: float = 120.0) -> list[dict]:
    """最近 N 个交易日的累计成交额（腾讯 day/query）。

    腾讯日 K（fqkline）只给成交量、amount 恒为 0；day/query 的每行是
    "HHMM 价格 累计成交量 累计成交额"，取每天最后一笔即得当日成交额。
    """
    series = await day_series(secid, ndays=ndays, ttl=ttl)
    return [{"date": d["date"], "amount": max(a for _, a in d["points"])}
            for d in series if d["points"]]


def _trends_trim(out: list[dict], ndays: int) -> list[dict]:
    """分时清洗：单日视图只保留最后一个交易日；盘中若拿到的是旧日数据则丢弃。

    典型故障：腾讯/push2 拉取失败 → get_json 回退"上次成功缓存"（可能是上一交易日的
    全天分时）→ 若用墙上时钟重新贴日期，就会把整日旧数据渲染成"今天"，图上出现一条
    延伸到收盘时间的错线。这里以数据源自身日期为准做校验。

    另统一裁掉非连续竞价时段（09:30–11:30 / 13:00–15:00）的点：腾讯分时在收盘后
    仍返回 15:06–15:30 的盘后成交/残余快照，而资金流只到 15:00；两套轴不一致会让
    叠加柱状图尾部整段空洞。
    """
    out = [p for p in (out or []) if _hm_in_session((p.get("time") or "")[-5:])]
    if not out or ndays > 1:
        return out
    last_day = (out[-1].get("time") or "")[:10]
    if _now_in_session() and last_day != time.strftime("%Y-%m-%d"):
        return []                      # 盘中却拿到非今日数据 → 宁可空，不误导
    return [p for p in out if (p.get("time") or "")[:10] == last_day]


def _tx_trend_points(raw: list[str] | None, day_fmt: str) -> list[dict]:
    """腾讯分时行 "HHMM price volume amount" -> 归一化点（单日/多日接口共用）。"""
    out = []
    for line in raw or []:
        p = line.split()
        if len(p) < 2:
            continue
        t = p[0]
        hhmm = f"{t[0:2]}:{t[2:4]}" if len(t) == 4 else t
        out.append({"time": f"{day_fmt} {hhmm}", "price": _numf(p[1]),
                    "high": _numf(p[1]), "low": _numf(p[1]),
                    "volume": _numf(p[2]) if len(p) > 2 else None,
                    "amount": _numf(p[3]) if len(p) > 3 else None, "avg": None})
    return out


def trend_prev_close(secid: str) -> float | None:
    """分时回退到昨日时，该日的昨收（用于前端涨跌幅基准）。"""
    return _TREND_PREV_CLOSE.get(secid)


async def trends(secid: str, ndays: int = 1, ttl: float = 30.0) -> list[dict]:
    """分时（归一化）。腾讯优先（稳定），push2 trends2 兜底。

    时间戳取自数据源本身（腾讯 node.data.date / push2 行首），不再用"今天"重贴。
    """
    code = _tx_code(secid)
    if code:
        d = await get_json(_TX_TREND, {"code": code}, ttl=ttl)
        node = ((d or {}).get("data") or {}).get(code) or {}
        tdata = node.get("data") or {}
        raw = tdata.get("data") or []
        day = str(tdata.get("date") or "")          # "20261015"
        day_fmt = (f"{day[0:4]}-{day[4:6]}-{day[6:8]}"
                   if len(day) == 8 else time.strftime("%Y-%m-%d"))
        out = _trends_trim(_tx_trend_points(raw, day_fmt), ndays)
        if len(out) >= 2:
            _TREND_PREV_CLOSE.pop(secid, None)
            return out
        # 盘前（或开盘首分钟）腾讯单日分时只有今日 09:30 的一个占位点。
        # 若直接返回，轴只剩 1 点，资金流/期权叠加会被同日校验全部丢弃，
        # 页面看起来就是「昨日数据不见了」。改为回退最近一个完整交易日。
        if ndays == 1:
            d2 = await get_json(_TX_TREND_DAYS, {"code": code}, ttl=ttl)
            node2 = ((d2 or {}).get("data") or {}).get(code) or {}
            today = time.strftime("%Y-%m-%d")
            for item in node2.get("data") or []:
                raw_day = str(item.get("date") or "")
                if len(raw_day) != 8:
                    continue
                day2 = f"{raw_day[0:4]}-{raw_day[4:6]}-{raw_day[6:8]}"
                if day2 >= today:      # 今日占位/未来日期不算
                    continue
                pts = [p for p in _tx_trend_points(item.get("data"), day2)
                       if _hm_in_session(p["time"][-5:])]
                if len(pts) >= 2:
                    try:
                        prec = float(item.get("prec"))
                    except (TypeError, ValueError):
                        prec = None
                    if prec and prec > 0:
                        _TREND_PREV_CLOSE[secid] = prec
                    return pts
    h = "push2.eastmoney.com"
    # 两条通道都熔断才短路：IPv6 是合约分时的真实来源（境外 IPv4 拿不到）
    if _push2_dead(h) and _v6_dead(h):
        return []
    d = await get_json("https://push2.eastmoney.com/api/qt/stock/trends2/get",
                       {"secid": secid, "fields1": "f1,f2,f3,f4,f5,f6,f7,f8",
                        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58", "iscr": 0,
                        "ndays": ndays, "ut": _UT}, ttl=ttl)
    if d is None:
        _push2_mark_dead(h)
        return []
    return _trends_trim(parse_trends(d.get("data")), ndays)


# 资金流快照累积：交易时段内每次调用都用 emdatah5 快照累积一分钟点（内存+磁盘），
# push2 fflow 不可用/收盘后返回空 klines 时，用本地累积序列兜底，保证分时资金流
# 图表在收盘后、进程重启后仍可画出当日完整序列（复盘用）。
# 另保留最近一个有数据的昨日序列：9:15 开盘前当日无数据时展示昨日，界面不空。
_FLOW_HIST: dict[str, list[str]] = {}   # 当日累积
_FLOW_PREV: dict[str, list[str]] = {}   # 最近一个有数据的昨日
_FLOW_DAY: str | None = None            # _FLOW_HIST 对应的日期
_FLOW_MAX = 240
_FLOW_PATH = data_path("flow_history.jsonl")
_FLOW_LOADED = False
_FLOW_FLUSHED: dict[str, str] = {}  # secid -> 已落盘的最后分钟 "YYYY-MM-DD HH:MM"


def _flow_load() -> None:
    """进程首次使用时从磁盘恢复（幂等）：当日 → _FLOW_HIST，最近一个有数据的
    昨日 → _FLOW_PREV（9:15 前展示用）；磁盘只保留近 7 天的行。"""
    global _FLOW_LOADED, _FLOW_DAY
    if _FLOW_LOADED:
        return
    _FLOW_LOADED = True
    today = time.strftime("%Y-%m-%d")
    _FLOW_DAY = today
    if not os.path.exists(_FLOW_PATH):
        return

    def _feed(bucket: dict[str, list[str]], secid: str, fline: str) -> None:
        hist = bucket.setdefault(secid, [])
        minute = fline.split(",", 1)[0]
        if hist and hist[-1].split(",", 1)[0] == minute:
            hist[-1] = fline
        else:
            hist.append(fline)
            if len(hist) > _FLOW_MAX:
                del hist[:len(hist) - _FLOW_MAX]

    try:
        by_day: dict[str, list[tuple[str, str]]] = {}
        kept: list[str] = []
        cutoff = (dt.datetime.now() - dt.timedelta(days=7)).strftime("%Y-%m-%d")
        with open(_FLOW_PATH, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                d = r.get("date")
                if not d or d < cutoff or d > today:
                    continue
                kept.append(line)
                secid, fline = r.get("secid"), r.get("line")
                if secid and fline:
                    by_day.setdefault(d, []).append((secid, fline))
        with open(_FLOW_PATH, "w", encoding="utf-8") as f:
            for line in kept:
                f.write(line + "\n")
        for secid, fline in by_day.get(today, []):
            _feed(_FLOW_HIST, secid, fline)
            _FLOW_FLUSHED[secid] = fline.split(",", 1)[0]
        prev_days = sorted(d for d in by_day if d < today)
        if prev_days:
            for secid, fline in by_day[prev_days[-1]]:
                _feed(_FLOW_PREV, secid, fline)
    except OSError as exc:
        log.debug("资金流历史加载失败: %s", exc)


def _flow_persist(secid: str, line: str) -> None:
    """新分钟定稿时追加落盘；同一分钟内多次调用只落一次。"""
    minute = line.split(",", 1)[0]
    if _FLOW_FLUSHED.get(secid) == minute:
        return
    _FLOW_FLUSHED[secid] = minute
    try:
        os.makedirs(os.path.dirname(_FLOW_PATH), exist_ok=True)
        with open(_FLOW_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(
                {"date": minute.split(" ", 1)[0], "secid": secid, "line": line},
                ensure_ascii=False) + "\n")
    except OSError as exc:
        log.debug("资金流历史落盘失败: %s", exc)


def _hm_in_session(hm: str) -> bool:
    """HH:MM 是否落在交易时段内（用于过滤掉非交易时段累积的脏点）。"""
    return (settings.session_am[0] <= hm <= settings.session_am[1]) or \
           (settings.session_pm[0] <= hm <= settings.session_pm[1])


def _now_in_session(now: dt.datetime | None = None) -> bool:
    now = now or dt.datetime.now()
    return now.weekday() < 5 and _hm_in_session(now.strftime("%H:%M"))


def _flow_clean(klines: list[str] | None) -> list[str]:
    """资金流分钟序列清洗：只留连续竞价时段、去重，口径与分时轴一致。

    push2 收盘后可能带回 15:00 之后的点，盘前还可能整段返回上一交易日；
    调用方据首/末点日期回填 day，避免把昨日序列标成今日。
    """
    out: list[str] = []
    last_minute = ""
    for line in klines or []:
        p = line.split(",")
        if len(p) < 6 or not _hm_in_session((p[0] or "")[-5:]):
            continue
        if p[0] == last_minute:
            continue
        last_minute = p[0]
        out.append(line)
    return out


async def fflow(secid: str, klt: int = 1, lmt: int = 0, ttl: float = 20.0) -> dict | None:
    """分钟级资金流序列。

    1) 交易时段内：每次调用都用 emdatah5 getZJLXData 快照累积一分钟点
       （内存+磁盘落盘），无论 push2 是否可用——保证收盘后/进程重启后
       仍有当日完整序列；
    2) push2 fflow（真·分钟序列，时间戳=行情时间）优先——tries=1 快速失败；
    3) push2 无数据（收盘后常返回空 klines）时，用本地累积序列兜底；
    4) 当日累积不足 2 点时（如 9:15 开盘前），用最近一个有数据的昨日序列
       兜底，保证界面不空。

    返回 {"klines": [...], "day": "YYYY-MM-DD"}（day 供前端判断是否昨日数据）。
    注意累积的是快照的日累计值（f137 等为当日累计净流入），与 push2 的分钟增量
    口径不同；下游的 flow_main_delta5m 等按累计口径计算（尾减首）。
    """
    global _FLOW_DAY, _FLOW_HIST, _FLOW_PREV
    _flow_load()
    today = time.strftime("%Y-%m-%d")
    if _FLOW_DAY != today:
        # 跨天：昨日 ← 旧今日（若有数据；周末/节假日保持上一个交易日），今日清空
        if _FLOW_HIST:
            _FLOW_PREV = _FLOW_HIST
        _FLOW_HIST = {}
        _FLOW_FLUSHED.clear()
        _FLOW_DAY = today
    hist = _FLOW_HIST.setdefault(secid, [])
    if _now_in_session():
        # **只在交易时段累积**，否则墙上时钟点(如午休 12:xx)会落在行情时间轴之外、
        # 图上一根柱都画不出。
        q = await get_json(f"{_H5_URL}/getZJLXData",
                           {"secid": secid, "fields": "f57,f58,f137,f140,f143,f146,f149",
                            "ut": _H5_UT}, ttl=ttl, referer=_H5_REF)
        row = (q or {}).get("data") if isinstance(q, dict) else None
        if row:
            ts = time.strftime("%Y-%m-%d %H:%M")
            line = ",".join(str(x) for x in (
                ts, _numf(row.get("f137")), _numf(row.get("f149")),
                _numf(row.get("f146")), _numf(row.get("f143")), _numf(row.get("f140"))))
            if hist and hist[-1].split(",", 1)[0] == ts:  # 同一分钟 → 覆盖
                hist[-1] = line
            else:
                hist.append(line)
                if len(hist) > _FLOW_MAX:
                    del hist[:len(hist) - _FLOW_MAX]
                _flow_persist(secid, line)
    data = None
    if not _push2_dead("push2.eastmoney.com"):
        d = await get_json("https://push2.eastmoney.com/api/qt/stock/fflow/kline/get",
                           {"secid": secid, "klt": klt, "lmt": lmt, "fields1": "f1,f2,f3,f7",
                            "fields2": "f51,f52,f53,f54,f55,f56", "ut": _UT},
                           ttl=ttl, tries=1)
        if d is None:
            _push2_mark_dead("push2.eastmoney.com")
        data = d.get("data") if d else None
    klines = _flow_clean((data or {}).get("klines"))
    if len(klines) >= 2:
        # day 取序列自身日期：盘前 push2 常整段返回上一交易日，不能硬贴 today。
        day = (klines[-1].split(",", 1)[0] or "")[:10] or today
        return {"klines": klines, "day": day}
    # 只回交易时段内的点（清掉任何历史脏点）
    kept_today = [l for l in hist if _hm_in_session(l.split(",", 1)[0][-5:])]
    if len(kept_today) >= 2:
        return {"klines": kept_today, "day": today}
    # 今日不足 2 点（9:15 前 / 首日无数据 / 收盘后首日）：用最近有数据的昨日兜底
    prev = _FLOW_PREV.get(secid, [])
    kept_prev = [l for l in prev if _hm_in_session(l.split(",", 1)[0][-5:])]
    if kept_prev:
        return {"klines": kept_prev, "day": kept_prev[-1].split(" ", 1)[0]}
    return None


# --------------------------------------------------------------------------
# 板块/指数分时快照累积
#
# 板块代码（47.xxxxxx，如 A股平均股价 47.800005）既无腾讯映射（_tx_code 只认 1./0.），
# push2 分时又被反爬打空 —— 迷你走势图历来是空的。这里复用期权分时那套
# emdatah5 快照累积（交易时段逐分钟落 data/index_history.jsonl），
# 让「上证指数/平均股价」这类底栏指标也有日内曲线可看。
# --------------------------------------------------------------------------
_IDX_HIST: dict[str, list[str]] = {}
_IDX_PREV: dict[str, list[str]] = {}
_IDX_DAY: str | None = None
_IDX_MAX = 240
_IDX_PATH = data_path("index_history.jsonl")
_IDX_LOADED = False
_IDX_FLUSHED: dict[str, str] = {}


def _idx_load() -> None:
    """进程首次使用时从磁盘恢复当日/昨日序列（幂等）。"""
    global _IDX_LOADED, _IDX_DAY
    if _IDX_LOADED:
        return
    _IDX_LOADED = True
    today = time.strftime("%Y-%m-%d")
    _IDX_DAY = today
    if not os.path.exists(_IDX_PATH):
        return

    def _feed(bucket: dict[str, list[str]], secid: str, fline: str) -> None:
        hist = bucket.setdefault(secid, [])
        minute = fline.split(",", 1)[0]
        if hist and hist[-1].split(",", 1)[0] == minute:
            hist[-1] = fline
        else:
            hist.append(fline)
            if len(hist) > _IDX_MAX:
                del hist[:len(hist) - _IDX_MAX]

    try:
        by_day: dict[str, list[tuple[str, str]]] = {}
        kept: list[str] = []
        cutoff = (dt.datetime.now() - dt.timedelta(days=7)).strftime("%Y-%m-%d")
        with open(_IDX_PATH, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                d = r.get("date")
                if not d or d < cutoff or d > today:
                    continue
                kept.append(line)
                secid, fline = r.get("secid"), r.get("line")
                if secid and fline:
                    by_day.setdefault(d, []).append((secid, fline))
        with open(_IDX_PATH, "w", encoding="utf-8") as f:
            for line in kept:
                f.write(line + "\n")
        for secid, fline in by_day.get(today, []):
            _feed(_IDX_HIST, secid, fline)
            _IDX_FLUSHED[secid] = fline.split(",", 1)[0]
        prev_days = sorted(d for d in by_day if d < today)
        if prev_days:
            for secid, fline in by_day[prev_days[-1]]:
                _feed(_IDX_PREV, secid, fline)
    except OSError as exc:
        log.debug("指数分时历史加载失败: %s", exc)


def _idx_persist(secid: str, line: str) -> None:
    minute = line.split(",", 1)[0]
    if _IDX_FLUSHED.get(secid) == minute:
        return
    _IDX_FLUSHED[secid] = minute
    try:
        os.makedirs(os.path.dirname(_IDX_PATH), exist_ok=True)
        with open(_IDX_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(
                {"date": minute.split(" ", 1)[0], "secid": secid, "line": line},
                ensure_ascii=False) + "\n")
    except OSError as exc:
        log.debug("指数分时落盘失败: %s", exc)


async def snapshot_trends(secid: str, ttl: float = 20.0) -> list[dict]:
    """板块/指数分时（emdatah5 快照累积）。输出与 trends() 同构。

    交易时段内取一次快照并入当日序列；当日不足 2 点时用最近一个有数据的昨日。
    """
    global _IDX_DAY, _IDX_HIST, _IDX_PREV
    _idx_load()
    today = time.strftime("%Y-%m-%d")
    if _IDX_DAY != today:
        if _IDX_HIST:
            _IDX_PREV = _IDX_HIST
        _IDX_HIST = {}
        _IDX_FLUSHED.clear()
        _IDX_DAY = today
    hist = _IDX_HIST.setdefault(secid, [])
    if _now_in_session():
        q = await get_json(f"{_H5_URL}/getZJLXData",
                           {"secid": secid, "fields": "f43", "ut": _H5_UT},
                           ttl=ttl, referer=_H5_REF)
        row = (q or {}).get("data") if isinstance(q, dict) else None
        px = _numf(row.get("f43")) if row else 0.0
        if px > 0:
            ts = time.strftime("%Y-%m-%d %H:%M")
            line = f"{ts},{px}"
            if hist and hist[-1].split(",", 1)[0] == ts:
                hist[-1] = line
            else:
                hist.append(line)
                if len(hist) > _IDX_MAX:
                    del hist[:len(hist) - _IDX_MAX]
                _idx_persist(secid, line)
    kept = [l for l in hist if _hm_in_session(l.split(",", 1)[0][-5:])]
    if len(kept) < 2:
        kept = [l for l in _IDX_PREV.get(secid, [])
                if _hm_in_session(l.split(",", 1)[0][-5:])]
    out: list[dict] = []
    for l in kept:
        ts, _, sp = l.partition(",")
        p = _numf(sp)
        out.append({"time": ts, "price": p, "high": p, "low": p,
                    "volume": None, "amount": None, "avg": None})
    return out


async def trends_or_snapshot(secid: str, ndays: int = 1,
                             ttl: float = 30.0) -> list[dict]:
    """真实分时优先，取不到时退回快照累积（板块类代码的唯一来源）。"""
    real = await trends(secid, ndays=ndays, ttl=ttl)
    if len(real) >= 2:
        return real
    return await snapshot_trends(secid)


# --------------------------------------------------------------------------
# 期权分时快照累积（与资金流 fflow 同一策略）
#
# 期权 secid 形如 10.xxx / 12.xxx：_tx_code 无腾讯映射（不支持期权），push2 分时
# 又常被代理/反爬打空 -> 期权历来取不到任何分时数据。改用 emdatah5 getZJLXData
# 快照在交易时段内逐分钟累积一份当日序列（已实测该接口对期权返回最新价/昨收）。
# 与 fflow 一样，只能累积自此刻起的分钟点，已经过去的分钟无法回补；非交易时段
# 不累积（否则墙上时钟点落在行情时间轴之外，图上画不出来）。
# --------------------------------------------------------------------------
_OPT_HIST: dict[str, list[str]] = {}   # 当日累积
_OPT_PREV: dict[str, list[str]] = {}   # 最近一个有数据的昨日
_OPT_DAY: str | None = None
_OPT_MAX = 240
_OPT_PATH = data_path("opt_history.jsonl")
_OPT_LOADED = False
_OPT_FLUSHED: dict[str, str] = {}      # secid -> 已落盘的最后分钟

# 全市场合约清单：ulist 批量报价需要先知道 secid，而 clist（唯一能翻出全量清单的接口）
# 也会被接口级封禁。所以清单落盘复用：clist 能翻的时候建一次，之后每分钟只用 ulist
# 更新价格（1 次 400 只，实测净耗时 0.2s）。
_OPT_UNIVERSE_PATH = data_path("option_universe.json")
_OPT_UNIVERSE: list[str] = []
_OPT_UNIVERSE_LOADED = False
_ULIST_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
_CFX_MARKET = 221
_CFX_DAY = ""
_CFX_SECIDS: list[str] = []
_CFX_RE = re.compile(r"^([A-Z]+)(\d{4})-[CP]-")


async def _cffex_option_secids() -> list[str]:
    """中金所股指期权合约清单（近月 + 次月）。

    push2 的 clist 已被接口级封禁，但 `em.clist` 的主源是 emdatah5（`getZDYLBData`），
    m:221 一次能翻出 760 条（约 0.9s），每天取一次即可。
    只保留最近两个到期月：远月几乎没人交易，却会和近月一起把分钟累积的落盘撑大。
    """
    global _CFX_DAY, _CFX_SECIDS
    today = time.strftime("%Y-%m-%d")
    if _CFX_SECIDS and _CFX_DAY == today:
        return _CFX_SECIDS
    rows = await clist_all(f"m:{_CFX_MARKET}", "f12,f14", pz=100, max_page=9, ttl=1800.0)
    codes = [r.get("f12") for r in rows if r.get("f12")]
    if not codes:
        return _CFX_SECIDS
    months: dict[str, set[str]] = {}
    for c in codes:
        m = _CFX_RE.match(c)
        if m:
            months.setdefault(m.group(1), set()).add(m.group(2))
    keep = {sym: sorted(yms)[:2] for sym, yms in months.items()}
    out = []
    for c in codes:
        m = _CFX_RE.match(c)
        if m and m.group(2) in keep.get(m.group(1), ()):
            out.append(f"{_CFX_MARKET}.{c}")
    if out:
        _CFX_SECIDS = out
        _CFX_DAY = today
    return _CFX_SECIDS


def _opt_universe_load() -> None:
    global _OPT_UNIVERSE, _OPT_UNIVERSE_LOADED
    if _OPT_UNIVERSE_LOADED:
        return
    _OPT_UNIVERSE_LOADED = True
    try:
        with open(_OPT_UNIVERSE_PATH, encoding="utf-8") as f:
            d = json.load(f)
        _OPT_UNIVERSE = [s for s in (d.get("secids") or []) if isinstance(s, str)]
    except (OSError, ValueError):
        _OPT_UNIVERSE = []


def _opt_universe_save(secids: list[str]) -> None:
    global _OPT_UNIVERSE
    _OPT_UNIVERSE = sorted(set(secids))
    try:
        os.makedirs(os.path.dirname(_OPT_UNIVERSE_PATH), exist_ok=True)
        with open(_OPT_UNIVERSE_PATH, "w", encoding="utf-8") as f:
            json.dump({"day": time.strftime("%Y-%m-%d"), "secids": _OPT_UNIVERSE}, f)
    except OSError as exc:
        log.debug("合约清单落盘失败: %s", exc)


async def option_chain_from_universe(market: int, fields: str,
                                     chunk: int = 400) -> list[dict]:
    """用落盘的合约清单 + ulist 批量取全字段——clist 被接口级封禁时的主路径。

    返回结构与 clist 的 `diff` 同构（f12/f14/f2/f18…），调用方无需区分来源。
    ulist 一次 400 只，沪 692 / 深 570 各 2 个请求、并发后约 0.5s
    （clist 翻页被封时要 12s 且只回 1~2 页）。
    """
    _opt_universe_load()
    prefix = f"{market}."
    secids = [s for s in _OPT_UNIVERSE if s.startswith(prefix)]
    if not secids:
        return []
    parts = [secids[i:i + chunk] for i in range(0, len(secids), chunk)]

    async def _one(part: list[str]) -> list[dict]:
        d = await get_json(_ULIST_URL, {
            "secids": ",".join(part), "fltt": 2, "invt": 2, "ut": _UT,
            "fields": fields,
        }, ttl=45)
        return ((d or {}).get("data") or {}).get("diff") or []

    res = await asyncio.gather(*[_one(p) for p in parts], return_exceptions=True)
    out: list[dict] = []
    for r in res:
        if isinstance(r, list):
            out.extend(r)
    return out


async def _ulist_prices(secids: list[str], chunk: int = 400) -> list[tuple[str, float]]:
    """批量报价：一次 400 只，返回 [(secid, price)]。

    比 clist 翻页省得多（13 个请求 → 3~4 个），而且 ulist 目前没被封。
    分片并发：全市场 1268 只约 0.7s（串行 2.5s）。
    """
    parts = [secids[i:i + chunk] for i in range(0, len(secids), chunk)]

    async def _one(part: list[str]) -> list[tuple[str, float]]:
        d = await get_json(_ULIST_URL, {
            "secids": ",".join(part), "fltt": 2, "invt": 2, "ut": _UT,
            "fields": "f12,f13,f2",
        }, ttl=25)
        out: list[tuple[str, float]] = []
        for it in ((d or {}).get("data") or {}).get("diff") or []:
            market, code, px = it.get("f13"), it.get("f12"), it.get("f2")
            try:
                v = float(px)
            except (TypeError, ValueError):
                continue
            if market and code and v > 0:
                out.append((f"{market}.{code}", v))
        return out

    res = await asyncio.gather(*[_one(p) for p in parts], return_exceptions=True)
    rows: list[tuple[str, float]] = []
    for r in res:
        if isinstance(r, list):
            rows.extend(r)
    return rows


def _opt_load() -> None:
    """进程首次使用时从磁盘恢复（幂等）：当日 -> _OPT_HIST，最近一个有数据的
    昨日 -> _OPT_PREV；磁盘只保留近 7 天的行。"""
    global _OPT_LOADED, _OPT_DAY
    if _OPT_LOADED:
        return
    _OPT_LOADED = True
    today = time.strftime("%Y-%m-%d")
    _OPT_DAY = today
    if not os.path.exists(_OPT_PATH):
        return

    def _feed(bucket: dict[str, list[str]], secid: str, fline: str) -> None:
        hist = bucket.setdefault(secid, [])
        minute = fline.split(",", 1)[0]
        if hist and hist[-1].split(",", 1)[0] == minute:
            hist[-1] = fline
        else:
            hist.append(fline)
            if len(hist) > _OPT_MAX:
                del hist[:len(hist) - _OPT_MAX]

    try:
        by_day: dict[str, list[tuple[str, str]]] = {}
        kept: list[str] = []
        cutoff = (dt.datetime.now() - dt.timedelta(days=7)).strftime("%Y-%m-%d")
        with open(_OPT_PATH, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                d = r.get("date")
                if not d or d < cutoff or d > today:
                    continue
                kept.append(line)
                secid, fline = r.get("secid"), r.get("line")
                if secid and fline:
                    by_day.setdefault(d, []).append((secid, fline))
        with open(_OPT_PATH, "w", encoding="utf-8") as f:
            for line in kept:
                f.write(line + "\n")
        for secid, fline in by_day.get(today, []):
            _feed(_OPT_HIST, secid, fline)
            _OPT_FLUSHED[secid] = fline.split(",", 1)[0]
        prev_days = sorted(d for d in by_day if d < today)
        if prev_days:
            for secid, fline in by_day[prev_days[-1]]:
                _feed(_OPT_PREV, secid, fline)
    except OSError as exc:
        log.debug("期权分时历史加载失败: %s", exc)


def _opt_persist(secid: str, line: str) -> None:
    """新分钟定稿时追加落盘；同一分钟内多次调用只落一次。"""
    minute = line.split(",", 1)[0]
    if _OPT_FLUSHED.get(secid) == minute:
        return
    _OPT_FLUSHED[secid] = minute
    try:
        os.makedirs(os.path.dirname(_OPT_PATH), exist_ok=True)
        with open(_OPT_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(
                {"date": minute.split(" ", 1)[0], "secid": secid, "line": line},
                ensure_ascii=False) + "\n")
    except OSError as exc:
        log.warning("期权分时落盘失败 %s %s: %s", secid, minute, exc)


def _opt_accumulate(secid: str, px: float) -> None:
    """把一次快照价并入该合约的当日分钟序列（同分钟覆盖，新分钟落盘）。"""
    hist = _OPT_HIST.setdefault(secid, [])
    if not px or px <= 0:
        return
    ts = time.strftime("%Y-%m-%d %H:%M")
    line = f"{ts},{px}"
    if hist and hist[-1].split(",", 1)[0] == ts:   # 同一分钟 -> 覆盖
        hist[-1] = line
        return
    hist.append(line)
    if len(hist) > _OPT_MAX:
        del hist[:len(hist) - _OPT_MAX]
    _opt_persist(secid, line)


async def opt_snapshot_many(secids: list[str], ttl: float = 12.0) -> dict[str, float]:
    """批量取合约最新价并**一次性**并入分钟累积（避免逐个请求 + 逐行落盘）。

    emdatah5 不接受 secid 逗号批量（rc=102），只能并发单取；但累积与落盘原本按
    合约逐个写盘，合约一多就是几十次 open/append。这里统一在交易时段内并发取价、
    再批量并入内存序列，落盘仍由 _opt_persist 按「新分钟」触发一次。
    """
    if not secids:
        return {}
    _opt_load()
    _roll_opt_day()
    if not _now_in_session():
        return {}
    rows = await asyncio.gather(*[
        get_json(f"{_H5_URL}/getZJLXData",
                 {"secid": s, "fields": "f43", "ut": _H5_UT},
                 ttl=ttl, referer=_H5_REF)
        for s in secids], return_exceptions=True)
    out: dict[str, float] = {}
    for secid, q in zip(secids, rows):
        row = (q or {}).get("data") if isinstance(q, dict) else None
        px = _numf(row.get("f43")) if row else 0.0
        if not px:
            continue
        out[secid] = px
        _opt_accumulate(secid, px)
    return out


def _opt_accumulate_many(rows: list[tuple[str, float]]) -> int:
    """批量并入分钟累积（同分钟覆盖），新分钟一次性落盘（避免几百次 open）。"""
    ts = time.strftime("%Y-%m-%d %H:%M")
    pending: list[tuple[str, str]] = []
    added = 0
    for secid, px in rows:
        if not secid or not px or px <= 0:
            continue
        hist = _OPT_HIST.setdefault(secid, [])
        line = f"{ts},{px}"
        if hist and hist[-1].split(",", 1)[0] == ts:      # 同一分钟 -> 覆盖
            hist[-1] = line
            continue
        hist.append(line)
        if len(hist) > _OPT_MAX:
            del hist[:len(hist) - _OPT_MAX]
        if _OPT_FLUSHED.get(secid) != ts:
            _OPT_FLUSHED[secid] = ts
            pending.append((secid, line))
        added += 1
    if pending:
        try:
            os.makedirs(os.path.dirname(_OPT_PATH), exist_ok=True)
            with open(_OPT_PATH, "a", encoding="utf-8") as f:
                for secid, line in pending:
                    f.write(json.dumps({"date": ts.split(" ", 1)[0], "secid": secid,
                                        "line": line}, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.warning("期权分时批量落盘失败: %s", exc)
    return added


_CLIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"


async def _universe_from_clist(pages: int = 9) -> list[str]:
    """clist 翻页建立全市场合约清单（只在这个清单为空时才需要）。"""

    async def _page(market: int, pn: int):
        d = await get_json(_CLIST_URL, {
            "pn": pn, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2,
            "fid": "f6", "fs": f"m:{market}", "ut": _UT, "fields": "f12,f2",
        }, ttl=25)
        return ((d or {}).get("data") or {}).get("diff") or []

    async def _market(market: int) -> list[str]:
        out: list[str] = []
        empty_streak = 0
        for pn in range(1, pages + 1):
            diff: list[dict] = []
            # 东财对连续翻页会空回包（实测 pn=1 空、pn=2 正常）：重试几次再放弃。
            for attempt in range(3):
                diff = await _page(market, pn)
                if diff:
                    break
                await asyncio.sleep(0.4)
            if not diff:
                empty_streak += 1
                if empty_streak >= 2:      # 连续两页都空 = 到头/被限流
                    break
                continue
            empty_streak = 0
            out.extend(f"{market}.{it['f12']}" for it in diff if it.get("f12"))
            if len(diff) < 100:            # 最后一页
                break
            await asyncio.sleep(0.2)       # 翻页太快会被限流
        return out

    res = await asyncio.gather(_market(10), _market(12), return_exceptions=True)
    codes: list[str] = []
    for r in res:
        if isinstance(r, list):
            codes.extend(r)
    return codes


async def option_snapshot_batch(pages: int = 9) -> int:
    """全市场期权快照 → 分钟累积（交易时段内每分钟一次）。

    主路径是 **ulist 批量报价**（1 次 400 只、净耗时约 0.2s，3 次覆盖全市场），
    清单来自落盘的 `option_universe.json` / 当日已累积的合约——不再每分钟翻 13 页
    clist。只有清单为空（首次冷启动）时才用 clist 翻页建一次清单。
    """
    if not _now_in_session():
        return 0
    _opt_load()
    _roll_opt_day()
    _opt_universe_load()

    secids = sorted(set(_OPT_HIST) | set(_OPT_UNIVERSE))
    # 股指期权（IO/MO/HO）的 secid 是 `221.IO2611-C-3550` 这种非纯数字形式，
    # 从前只累积 ETF 期权，导致它们的合约分时图永远没有价格线。
    fx = await _cffex_option_secids()
    if fx:
        secids = sorted(set(secids) | set(fx))
    if not secids:
        secids = await _universe_from_clist(pages)
        if not secids:
            log.debug("合约清单为空且 clist 不可用，跳过本轮分钟累积")
            return 0
        _opt_universe_save(secids)

    rows = await _ulist_prices(secids)
    if not rows:
        return 0
    n = _opt_accumulate_many(rows)
    # 清单外出现的新合约（换月/新挂牌）补进清单
    fresh = [s for s, _ in rows if s not in set(_OPT_UNIVERSE)]
    if fresh:
        _opt_universe_save(list(_OPT_UNIVERSE) + fresh)
    return n


def _roll_opt_day() -> None:
    """跨天滚动：昨日 ← 旧今日，今日清空。"""
    global _OPT_DAY, _OPT_HIST, _OPT_PREV
    today = time.strftime("%Y-%m-%d")
    if _OPT_DAY == today:
        return
    if _OPT_HIST:
        _OPT_PREV = _OPT_HIST
    _OPT_HIST = {}
    _OPT_FLUSHED.clear()
    _OPT_DAY = today


async def opt_trends(secid: str, ttl: float = 12.0) -> list[dict]:
    """合约当日分时：交易时段内用 emdatah5 快照逐分钟累积。

    返回 [{"time": "YYYY-MM-DD HH:MM", "price": ...}]，与 trends() 输出同构，
    OverlayChart 按 time 末 5 位（HH:MM）对齐。
    当日累积不足 2 点时（开盘前/刚切换合约），用最近一个有数据的昨日序列兜底，
    保证界面不空——但那是昨日数据。
    """
    _opt_load()
    _roll_opt_day()
    hist = _OPT_HIST.setdefault(secid, [])
    if _now_in_session():
        q = await get_json(f"{_H5_URL}/getZJLXData",
                           {"secid": secid, "fields": "f43,f57,f58",
                            "ut": _H5_UT}, ttl=ttl, referer=_H5_REF)
        row = (q or {}).get("data") if isinstance(q, dict) else None
        _opt_accumulate(secid, _numf(row.get("f43")) if row else 0.0)
    kept = [l for l in hist if _hm_in_session(l.split(",", 1)[0][-5:])]
    if len(kept) < 2:
        kept = [l for l in _OPT_PREV.get(secid, [])
                if _hm_in_session(l.split(",", 1)[0][-5:])]
    out: list[dict] = []
    for l in kept:
        ts, _, sp = l.partition(",")
        p = float(sp) if sp else 0.0
        out.append({"time": ts, "price": p, "high": p, "low": p,
                    "volume": None, "amount": None, "avg": None})
    return out


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------
def parse_klines(data: dict | None) -> list[dict]:
    """kline 接口 klines: "date,open,close,high,low,vol,amount,amp,pct,chg,turn"

    兼容已归一化的 bars 列表（kline() 直接返回 list 时原样透传）。
    """
    if isinstance(data, list):
        return data
    if not data:
        return []
    out = []
    for line in data.get("klines") or []:
        p = line.split(",")
        if len(p) < 7:
            continue
        out.append({
            "date": p[0], "open": float(p[1]), "close": float(p[2]),
            "high": float(p[3]), "low": float(p[4]), "volume": float(p[5]),
            "amount": float(p[6]),
            "pct": float(p[8]) if len(p) > 8 and p[8] not in ("", "-") else None,
        })
    return out


def parse_trends(data: dict | None) -> list[dict]:
    """trends2 行，兼容 8 列与 7 列两种布局。

    8 列（当前 push2 fields2=f51..f58）：time,open,close,high,low,vol,amount,avg
    7 列（旧版）：time,close,high,low,vol,amount,avg
    旧实现固定按 7 列读、且把 open 当成了 price，导致 push2 兜底时分时价整体错位
    （价=开盘、量=价、额=量、均价=额）。
    兼容已归一化的 list（trends() 直接返回时原样透传）。
    """
    if isinstance(data, list):
        return data
    if not data:
        return []
    out = []
    for line in data.get("trends") or []:
        p = line.split(",")
        if len(p) < 5:
            continue
        i = 1 if len(p) >= 8 else 0          # 8 列时跳过开盘点

        def g(k: int, default=None):
            j = i + k
            if j >= len(p) or p[j] in ("", "-"):
                return default
            try:
                return float(p[j])
            except ValueError:
                return default

        out.append({
            "time": p[0], "price": g(1, 0.0),
            "high": g(2, 0.0), "low": g(3, 0.0),
            "volume": g(4, 0.0), "amount": g(5, 0.0), "avg": g(6),
        })
    return out


def parse_fflow(data: dict | None) -> list[dict]:
    """fflow kline: "time,主力净,小单净,中单净,大单净,超大单净" """
    if not data:
        return []
    out = []
    for line in data.get("klines") or []:
        p = line.split(",")
        if len(p) < 6:
            continue
        out.append({
            "time": p[0],
            "main": float(p[1]), "small": float(p[2]), "mid": float(p[3]),
            "big": float(p[4]), "super": float(p[5]),
        })
    return out


def f(x, default=None):
    """安全取数：东财空值为 '-' 或 None。"""
    if x in (None, "-", ""):
        return default
    return x


def num(x, default=0.0) -> float:
    v = f(x)
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# 板块成分股 / 研报热度（板块资金详情抽屉用）
# --------------------------------------------------------------------------
async def board_stocks(bk_code: str, limit: int = 50, ttl: float = 30.0) -> list[dict]:
    """板块成分股的主力净流入排行（降序）。

    字段：f2 最新价、f3 涨跌幅、f12 代码、f14 名称、f20 市值、f62 主力净流入。
    emdatah5 主源与 push2 兜底由 `clist` 负责；失败返回空列表，不抛异常。
    """
    if not str(bk_code).startswith("BK"):
        return []
    rows = await clist(f"b:{bk_code}", "f2,f3,f12,f14,f20,f62", pz=100, fid="f62", ttl=ttl)
    out: list[dict] = []
    for it in rows[:max(1, limit)]:
        code = str(it.get("f12") or "")
        name = str(it.get("f14") or "")
        if not code or not name:
            continue
        out.append({
            "code": code, "name": name,
            "price": _numf(it.get("f2")), "pct": _numf(it.get("f3")),
            "flow": _numf(it.get("f62")), "mv": _numf(it.get("f20")),
        })
    return out


async def report_hits(stock_code: str, days: int = 90, ttl: float = 3600.0) -> int:
    """个股近 N 天研报数（东财 reportapi 的 hits），失败返回 0。"""
    end = time.strftime("%Y-%m-%d")
    begin = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    d = await get_json("https://reportapi.eastmoney.com/report/list",
                       {"cb": "", "pageSize": 1, "pageNo": 1, "code": stock_code,
                        "reportType": 1, "qType": 0, "beginTime": begin, "endTime": end},
                       ttl=ttl, referer="https://data.eastmoney.com/report/")
    try:
        return int((d or {}).get("hits") or 0)
    except (TypeError, ValueError):
        return 0
