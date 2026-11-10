"""自部署：文件触发的后端热更新 + 可选前端重建。

背景：Agent 没有 Mac shell 通道，无法直接重启服务。约定如下——
- Agent 改完代码并做完语法校验后，把部署指令写入 data/.deploy_token（JSON，
  如 {"build_frontend": true, "reason": "..."}）。
- monitor 主循环每轮检查该文件：存在即消费（删除）并执行部署：
  1) 先对 app.py / config.py / services/ / routers/ 做 py_compile，失败则中止；
  2) 可选：运行 web/build.sh 重建前端（失败则中止，不重启）；
  3) 优雅关闭 Kronos 子进程与网络客户端后 os._exit(42)，由 LaunchAgent 自动拉起。
- 结果写入 data/.deploy_status（Agent 可据此确认；构建日志截断保留）。
- 用户也可直接 POST /api/admin/deploy 手动触发（body 可选 {"build_frontend": bool}）。

注意：首次上线本机制需要一次手动重启（launchctl kickstart -k ...），之后全自动。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import py_compile

from paths import data_path

log = logging.getLogger("deploy")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN_PATH = data_path(".deploy_token")
STATUS_PATH = data_path(".deploy_status")
_BUILD_TIMEOUT = 300


def _write_status(status: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STATUS_PATH), exist_ok=True)
        with open(STATUS_PATH, "w", encoding="utf-8") as f:
            json.dump({**status, "at": dt.datetime.now().isoformat(timespec="seconds")},
                      f, ensure_ascii=False)
    except OSError as exc:
        log.warning("部署状态写入失败: %s", exc)


def check_token() -> dict | None:
    """检查并消费部署令牌；无令牌或解析失败返回 None（下轮重试）。"""
    if not os.path.exists(TOKEN_PATH):
        return None
    try:
        with open(TOKEN_PATH, encoding="utf-8") as f:
            opts = json.load(f)
        if not isinstance(opts, dict):
            return None
    except (OSError, ValueError) as exc:
        log.debug("部署令牌解析失败，等待下轮: %s", exc)
        return None
    try:
        os.remove(TOKEN_PATH)
    except OSError:
        pass
    return {"build_frontend": bool(opts.get("build_frontend", False)),
            "reason": str(opts.get("reason", ""))[:200]}


def _compile_check() -> tuple[bool, str]:
    targets = [os.path.join(ROOT, "app.py"), os.path.join(ROOT, "config.py")]
    for sub in ("services", "routers"):
        d = os.path.join(ROOT, sub)
        if os.path.isdir(d):
            targets += [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".py")]
    bad = []
    for p in targets:
        if not os.path.exists(p):
            continue
        try:
            py_compile.compile(p, doraise=True)
        except py_compile.PyCompileError as exc:
            bad.append(f"{os.path.relpath(p, ROOT)}: {exc}")
    return (not bad), "; ".join(bad)


async def _build_frontend() -> tuple[bool, str]:
    script = os.path.join(ROOT, "web", "build.sh")
    if not os.path.exists(script):
        return False, "web/build.sh 不存在"
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", script, cwd=ROOT,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_BUILD_TIMEOUT)
        log_text = out.decode("utf-8", "replace")[-3000:]
        if proc.returncode != 0:
            return False, f"build.sh 退出码 {proc.returncode}\n{log_text}"
        return True, log_text
    except asyncio.TimeoutError:
        return False, "前端构建超时（>300s）"
    except Exception as exc:  # noqa: BLE001
        return False, f"构建异常: {exc}"


async def deploy_now(build_frontend: bool = False, reason: str = "") -> dict:
    """执行部署：校验 → 可选构建前端 → 退出进程（launchd 拉起）。
    校验/构建失败则不重启，返回状态供排查。"""
    from services import em, kronos  # 延迟导入，避免循环
    status: dict = {"reason": reason, "build_frontend": build_frontend, "ok": True}
    ok, err = _compile_check()
    if not ok:
        status.update(ok=False, error=f"语法校验失败: {err}")
        _write_status(status)
        return status
    if build_frontend:
        ok, blog = await _build_frontend()
        status["build_ok"] = ok
        status["build_log"] = blog
        if not ok:
            status.update(ok=False, error="前端构建失败，未重启")
            _write_status(status)
            return status
    _write_status(status)
    log.info("自部署：退出进程，由 LaunchAgent 拉起（reason=%s）", reason)
    await kronos.shutdown()
    await em.close()
    os._exit(42)
    return status  # unreachable
