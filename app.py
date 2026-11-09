"""缅A决策看板 — FastAPI 入口。

    /Users/xinxin/.browser-use-env/bin/python3 -m uvicorn app:app --host 0.0.0.0 --port 6799
    （run.sh 实际绑 0.0.0.0，供局域网/Tailscale 访问）
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from config import settings
from routers import api, arisk, limitup, wangwang
from services import (board_collector, em, kronos, limitup as lu_svc, monitor, tape_flow,
                      wangwang as ww_svc)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("app")

WEB_DIST = os.path.join(os.path.dirname(__file__), "web", "dist")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 板块库先建表（同步）：否则冷启动瞬间的 /api/board/* 会撞 no such table
    try:
        await asyncio.to_thread(board_collector.init)
    except Exception as exc:  # noqa: BLE001
        log.warning("板块库初始化失败: %s", exc)
    log.info("启动：预热 Kronos（后台）")
    task_k = asyncio.create_task(kronos.warmup())
    task_m = asyncio.create_task(monitor.run_forever())
    # 板块资金采集：交易时段每 30 秒写本地库，异常只记日志
    task_b = asyncio.create_task(board_collector.run_forever())
    # 全A逐笔分档：盘中每 10 分钟重算一次（约 1~2 分钟），收盘后 15:05 定稿
    task_t = asyncio.create_task(tape_flow.run_forever())
    # 汪汪队 ETF 跟踪器：15:02 之后把当日份额写死一次（份额历史无公开源，只能每日累积）
    task_w = asyncio.create_task(ww_svc.run_forever())
    # 涨停棱镜：盘中每 2 分钟刷当日快照，15:05 定稿；启动时回补最近 20 个交易日
    task_l = asyncio.create_task(lu_svc.run_forever())
    yield
    log.info("关闭：停止监控与推理子进程")
    for t in (task_k, task_m, task_b, task_t, task_w, task_l):
        t.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.gather(task_k, task_m, task_b, task_t, task_w, task_l,
                             return_exceptions=True)
    await kronos.shutdown()
    await em.close()
    await ww_svc.close()


app = FastAPI(title="缅A决策看板", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


@app.middleware("http")
async def _no_cache_html(request, call_next):
    """HTML 一律 no-cache（每次重新校验），避免浏览器缓存旧 bundle 导致看不到新版。"""
    resp = await call_next(request)
    if "text/html" in resp.headers.get("content-type", ""):
        resp.headers["Cache-Control"] = "no-store"
    return resp


app.include_router(api.router)
app.include_router(wangwang.router)
app.include_router(limitup.router)
app.include_router(arisk.router)


@app.get("/")
async def index():
    idx = os.path.join(WEB_DIST, "index.html")
    if os.path.exists(idx):
        return FileResponse(idx, headers={"Cache-Control": "no-cache"})
    return JSONResponse({
        "msg": "前端尚未构建。开发模式请运行 web 目录的 vite dev（默认 http://localhost:5173）；"
               "或执行 web/build.sh 生成 web/dist。",
        "api_docs": "/docs",
    })


@app.get("/robots.txt", include_in_schema=False)
async def robots():
    return PlainTextResponse("User-agent: *\nAllow: /\n")


@app.get("/llms.txt", include_in_schema=False)
async def llms():
    return PlainTextResponse(
        "# 缅A决策看板\n\n"
        "> A 股 6 只宽基 ETF 期权的日内决策看板（实时行情/期权链/资金流/预测），仅供研究，不构成投资建议。\n\n"
        "- [看板首页](/): 实时行情与期权链\n",
        media_type="text/markdown; charset=utf-8")


if os.path.isdir(WEB_DIST):
    app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="static")
