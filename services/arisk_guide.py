"""arisk 安装引导页 —— 未检测到 `~/workspace/arisk` 时，`/arisk/page` 返回的 HTML。

为什么不复用面板本身：面板是一个外部仓库的单文件 HTML，没装的时候我们手上什么都没有，
直接返回 JSON 会让 iframe 里显示一坨 `{"error": ...}`。这里给一个**能照着做的说明页**：
项目来源（GitHub）+ 期望路径 + 现场体检（目录/文件/端口）+ 逐步命令（可一键复制）+ 配置项。

样式刻意与看板一致（浅色、system 字体、圆角卡片），但**不依赖前端构建**：这是纯字符串
HTML，后端在面板缺失时直接吐给 iframe，所以无论前端是哪个版本都能看到。
"""

from __future__ import annotations

import asyncio
import html as _html
import platform
import shlex

GITHUB_URL = "https://github.com/zhysh3/a-share-monitor-en"


async def port_open(host: str, port: int, timeout: float = 0.35) -> bool:
    """本机端口是否可连（只用于体检展示，失败一律当 False，不抛异常）。"""
    try:
        fut = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(fut, timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        del reader
        return True
    except Exception:  # noqa: BLE001
        return False


def _row(ok: bool, label: str, detail: str) -> str:
    mark = "✅" if ok else "❌"
    color = "#15803d" if ok else "#b91c1c"
    return (
        f'<li><span style="color:{color};font-weight:600">{mark}</span> '
        f'<code style="background:#f1f5f9;padding:1px 5px;border-radius:4px">{_html.escape(label)}</code> '
        f'<span style="color:#64748b">{_html.escape(detail)}</span></li>'
    )


def _code_block(cmd: str) -> str:
    esc = _html.escape(cmd)
    return (
        '<pre style="margin:6px 0 0;background:#0f172a;color:#e2e8f0;padding:10px 12px;'
        'border-radius:8px;font-size:12px;line-height:1.7;overflow-x:auto;'
        'font-family:ui-monospace,SFMono-Regular,Menlo,monospace">'
        f"<code>{esc}</code></pre>"
    )


def _step(n: int, title: str, cmd: str | None = None, note: str = "") -> str:
    body = _code_block(cmd) if cmd else ""
    tail = (
        f'<div style="margin-top:6px;color:#64748b;font-size:12px;line-height:1.7">{note}</div>'
        if note
        else ""
    )
    return (
        '<div style="margin:14px 0 0">'
        f'<div style="font-weight:600;font-size:13px;color:#0f172a">{n}. {_html.escape(title)}</div>'
        f"{body}{tail}</div>"
    )


def render_install_guide(
    arisk_dir: str,
    live_url: str,
    missing: list[str],
    port_web: bool,
    port_live: bool,
) -> str:
    """拼安装引导页。`missing` 是缺失的关键文件/目录列表（人话描述）。"""
    d = shlex.quote(arisk_dir)
    py = platform.python_version()

    checks = "".join(
        [
            _row(not missing, "安装目录", arisk_dir),
            _row(not any("arisk_monitor_local.html" in m for m in missing), "面板页面", "arisk_monitor_local.html"),
            _row(not any("arisk_data.json" in m for m in missing), "日更数据", "arisk_data.json"),
            _row(port_web, "面板服务 :8788", "静态服务器（本机 http 打开面板用）"),
            _row(port_live, "实时代理 :8899", "盘中实时抓数 / 妙想 API 转发"),
        ]
    )
    miss_html = ""
    if missing:
        items = "".join(f"<li>{_html.escape(m)}</li>" for m in missing)
        miss_html = (
            '<div style="margin-top:10px;border-left:3px solid #f59e0b;background:#fffbeb;'
            'padding:8px 12px;border-radius:6px;font-size:12px;color:#92400e">'
            "<b>缺少：</b>"
            f'<ul style="margin:4px 0 0 18px;padding:0">{items}</ul></div>'
        )

    steps = "".join(
        [
            _step(
                1,
                "克隆项目（默认装到上面的「安装目录」）",
                f"git clone {GITHUB_URL}.git {d}",
            ),
            _step(
                2,
                "建虚拟环境并装依赖（Python 3.9+，当前环境 Python " + py + "）",
                f"cd {d} && python3 -m venv venv && ./venv/bin/pip install -r requirements.txt",
                "国内网络慢可加镜像：<code>./venv/bin/pip install -r requirements.txt "
                "-i https://pypi.tuna.tsinghua.edu.cn/simple</code>",
            ),
            _step(
                3,
                "首次拉全量数据（约 95 秒，写 arisk_data.json）",
                "./venv/bin/python update_arisk_data.py",
                "数据源在东财/新浪/央行，境外服务器可能很慢；失败常见于被风控，重跑一次通常就好。",
            ),
            _step(
                4,
                "启动服务（8788 面板 + 8899 实时代理）",
                "bash start.sh",
                "不需要 8788 也能用：本看板直接读 <code>arisk_data.json</code>，"
                "只依赖 :8899 做盘中实时刷新。",
            ),
            _step(
                5,
                "可选：配置妙想 API Key（不配也能跑）",
                "cd " + d + " && cp .env.example .env   # 再填入 MX_APIKEY",
                "妙想只是增强项；没有 Key 时脚本自动退回 AKShare / 央行官网。",
            ),
            _step(
                6,
                "可选：开机常驻 + 每交易日晚 22:10 自动更新（macOS launchd）",
                "cd " + d + " && cp com.arisk.web.plist com.arisk.proxy.plist com.arisk.update.plist "
                "~/Library/LaunchAgents/ && for p in web proxy update; do "
                "launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.arisk.$p.plist; done",
                "仓库里另有 <code>.plist.example</code> 模板；Linux 用 cron："
                "<code>10 16-22 * * * /bin/bash "
                + _html.escape(arisk_dir)
                + "/check_and_update.sh</code>",
            ),
        ]
    )

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>风险监控 · 未安装（A-RISK）</title>
<style>
  :root{{color-scheme:light}}
  *{{box-sizing:border-box}}
  body{{margin:0;padding:28px 20px 48px;background:#f8fafc;color:#0f172a;
       font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
       font-size:13px;line-height:1.75;-webkit-font-smoothing:antialiased}}
  .wrap{{max-width:820px;margin:0 auto}}
  .card{{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:18px 20px;
        box-shadow:0 1px 2px rgba(15,23,42,.04)}}
  .card+.card{{margin-top:14px}}
  h1{{margin:0 0 4px;font-size:17px}}
  h2{{margin:0 0 8px;font-size:13px;color:#0f172a;letter-spacing:.02em}}
  a{{color:#2563eb;text-decoration:none}}
  a:hover{{text-decoration:underline}}
  code{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}}
  ul{{margin:6px 0 0;padding-left:18px}}
  .muted{{color:#64748b;font-size:12px}}
  .badge{{display:inline-block;padding:1px 8px;border-radius:999px;background:#fef3c7;
         color:#92400e;font-size:11px;font-weight:600;vertical-align:2px;margin-left:6px}}
</style></head>
<body><div class="wrap">

  <div class="card">
    <h1>A股风险监控（A-RISK）未安装<span class="badge">未检测到面板</span></h1>
    <div class="muted" style="margin-top:2px">
      这是一个独立的外部项目：<b>A股风险监控 / A-RISK/MONITOR</b> —— ERP、信贷脉冲、两融、
      ETF 资金分类、申万行业热力图与「两层漏斗决策模型」。本看板不重写它的图表，
      而是把它的面板原样嵌进这个页面。
    </div>
    <div style="margin-top:10px">
      <div>项目地址：<a href="{GITHUB_URL}" target="_blank" rel="noreferrer">{GITHUB_URL}</a></div>
      <div class="muted">
        本项目只<b>读</b>对方产出的文件（<code>arisk_monitor_local.html</code> /
        <code>arisk_data.json</code>）并转发它的实时接口，不修改、不写入，所以对方仓库
        <code>git pull</code> 互不影响。
      </div>
    </div>
  </div>

  <div class="card">
    <h2>现场体检</h2>
    <ul style="list-style:none;padding-left:0">{checks}</ul>
    {miss_html}
    <div class="muted" style="margin-top:8px">
      安装目录来自环境变量 <code>ARISK_DIR</code>（当前 <code>{_html.escape(arisk_dir)}</code>），
      实时代理来自 <code>ARISK_LIVE</code>（当前 <code>{_html.escape(live_url)}</code>）。
      换路径就在 <code>run.sh</code> 或 LaunchAgent 的环境变量里改，然后
      <code>curl -X POST http://127.0.0.1:6799/api/admin/restart</code> 重启后端。
    </div>
  </div>

  <div class="card">
    <h2>安装步骤</h2>
    <div class="muted">照抄即可；每一步都可以单独重跑。装完回到本页刷新（⌘R）就能看到面板。</div>
    {steps}
  </div>

  <div class="card">
    <h2>安装后自检</h2>
    {_code_block(
        "curl -s http://127.0.0.1:6799/arisk/health            # 本看板视角的体检（JSON）\\n"
        "curl -s http://127.0.0.1:6799/arisk/data.json | head   # 能读到日更数据\\n"
        "curl -s -o /dev/null -w '%{http_code}\\\\n' http://127.0.0.1:8899/pe   # 实时代理（200 即正常）"
    )}
    <div class="muted" style="margin-top:8px">
      面板缺失不影响看板其它页面：本页只是 iframe，顶部导航、期权、资金、涨停复盘都照常可用。
    </div>
  </div>

  <div class="muted" style="margin-top:16px;text-align:center">
    仅供研究，不构成投资建议 · 数据源：东方财富 / 新浪 / 央行
  </div>

</div></body></html>"""
