"""告警远程推送：SSE/弹窗/声音之外的第三条通道（人不在电脑前也收得到）。

支持渠道：Server酱（sctapi.ftqq.com，微信服务号）、Bark（iOS）、
企业微信群机器人 webhook。告警总线每条 publish 后异步派发，**尽力而为**：

- 任何失败只记日志（且不记 URL——其中含密钥），绝不影响告警主链路；
- 失败不重试：总线自带冷却与去重，重试会把一次抖动放大成刷屏；
- 派发按开关 + 最低级别过滤，missing/未知 level 一律按 info 处理（宁可多推）。

约定与 user_settings 一致：`parse_config()` 纯函数校验归一化；
`load()/save()` 是文件读写薄壳（失败降级、绝不抛异常）；
`build_request()` 纯函数构造请求、不触网；网络层 `_send()` 可整体打桩。
密钥存 data/push_settings.json，命中 .gitignore 的 data/*.json 规则不进 git；
GET 接口只回掩码，PUT 时掩码/留空一律保留原值（见 `merge_secret`）。
"""

from __future__ import annotations

import json
import logging
import os

import httpx

from paths import data_dir

log = logging.getLogger("push")

_DATA_DIR = data_dir()
_PATH = os.path.join(_DATA_DIR, "push_settings.json")

CHANNELS: tuple[str, ...] = ("none", "serverchan", "bark", "wecom")
CHANNEL_LABELS = {
    "none": "不推送",
    "serverchan": "Server酱（微信）",
    "bark": "Bark（iOS）",
    "wecom": "企业微信机器人",
}
LEVELS: tuple[str, ...] = ("critical", "warn", "info")
_LEVEL_RANK = {"critical": 0, "warn": 1, "info": 2}
_LEVEL_LABELS = {"critical": "严重", "warn": "提醒", "info": "通知"}

DEFAULT_CHANNEL = "none"
DEFAULT_MIN_LEVEL = "critical"
DEFAULT_BARK_SERVER = "https://api.day.app"
_TIMEOUT = 10.0

# 企业微信 text.content 硬限 2048 字节（UTF-8）；标题过长在 Server酱/Bark 里被截断
_WECOM_CONTENT_BYTES = 2048
_TITLE_MAX = 48

_FIELDS = ("enabled", "channel", "min_level", "key", "server", "url")


# ---------------------------------------------------------------------------
# 纯函数：校验 / 过滤 / 构造 / 合并 / 掩码
# ---------------------------------------------------------------------------

def _clean_str(value) -> str | None:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def _boolish(value) -> tuple[bool, bool]:
    """把布尔/字符串/0-1 归一为 (值, 是否合法)；None 表示未设置（视为关）。"""
    if value is None:
        return False, True
    if isinstance(value, bool):
        return value, True
    if isinstance(value, int) and value in (0, 1):
        return bool(value), True
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("true", "1", "on", "yes"):
            return True, True
        if s in ("false", "0", "off", "no", ""):
            return False, True
    return False, False


def _is_public_https_host(url: str) -> bool:
    """URL 是否指向公网 https 主机（拒绝本机/内网/保留地址）。纯函数，不做 DNS 解析。

    只按字面判断：IP 字面量走 `ipaddress` 分类，域名拒绝 `localhost` 与 `.local`
    `.internal` `.lan` `.localhost` 后缀。DNS 重绑定（域名解析到内网）不在此覆盖范围，
    因为校验发生在配置保存时、解析发生在发送时，二者无法原子对应。
    """
    from urllib.parse import urlsplit
    import ipaddress

    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme != "https" or not parts.hostname:
        return False
    host = parts.hostname.lower().rstrip(".")
    if host in ("localhost", "localhost.localdomain"):
        return False
    if host.endswith((".local", ".internal", ".lan", ".localhost", ".home")):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True  # 普通域名：放行（自建 Bark 常见）
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_unspecified or ip.is_multicast
    )


def parse_config(raw) -> tuple[dict, list[str]]:
    """校验并归一化推送配置，返回 (配置, 错误列表)。纯函数：不读文件不读时钟。"""
    if not isinstance(raw, dict):
        raw = {}
    errors: list[str] = []

    enabled, ok = _boolish(raw.get("enabled"))
    if not ok:
        errors.append("推送开关取值非法")
        enabled = False

    channel = DEFAULT_CHANNEL
    raw_ch = raw.get("channel")
    if raw_ch not in (None, ""):
        if raw_ch in CHANNELS:
            channel = raw_ch
        else:
            errors.append("推送渠道不认识")

    min_level = DEFAULT_MIN_LEVEL
    raw_lv = raw.get("min_level")
    if raw_lv not in (None, ""):
        if raw_lv in LEVELS:
            min_level = raw_lv
        else:
            errors.append("最低推送级别只能是 critical / warn / info 之一")

    key = _clean_str(raw.get("key"))
    if key and any(c.isspace() for c in key):
        errors.append("Key 不能包含空白字符")

    server = _clean_str(raw.get("server")) or DEFAULT_BARK_SERVER
    url = _clean_str(raw.get("url"))

    if channel == "serverchan":
        if not key:
            errors.append("Server酱需要填写 SendKey")
    elif channel == "bark":
        if not key:
            errors.append("Bark 需要填写设备 Key")
        if not server.startswith("https://"):
            errors.append("Bark 服务器必须是 https:// 地址")
        elif not _is_public_https_host(server):
            # 允许自建 Bark（任意公网 https），但不允许指向本机/内网：
            # /api/push/test 会按配置发起请求，放任内网地址等于开放一个 SSRF 打点。
            errors.append("Bark 服务器不能指向本机或内网地址")
    elif channel == "wecom":
        if not url:
            errors.append("企业微信需要填写机器人 Webhook 地址")
        else:
            # 只认官方域名，防把告警（含行情摘要）推到任意第三方
            from urllib.parse import urlsplit
            parts = urlsplit(url)
            if parts.scheme != "https" or parts.hostname != "qyapi.weixin.qq.com":
                errors.append("企业微信 Webhook 必须是 https://qyapi.weixin.qq.com/ 地址")

    configured = channel in ("serverchan", "bark", "wecom") and bool(key or url)
    return {
        "enabled": enabled,
        "channel": channel,
        "min_level": min_level,
        "key": key,
        "server": server,
        "url": url,
        "configured": configured,
    }, errors


def should_push(alert: dict, cfg: dict) -> bool:
    """开关 + 最低级别过滤。纯函数。"""
    if not cfg.get("enabled") or not cfg.get("configured"):
        return False
    level = (alert or {}).get("level") or "info"
    # rank 越小级别越高：告警级别不低于用户设置的最低级别才推
    return _LEVEL_RANK.get(level, 2) <= _LEVEL_RANK[cfg.get("min_level") or DEFAULT_MIN_LEVEL]


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def build_request(cfg: dict, alert: dict) -> dict | None:
    """按渠道构造 HTTP 请求描述。纯函数；none 渠道返回 None。"""
    ch = cfg.get("channel")
    alert = alert or {}
    lv = _LEVEL_LABELS.get(alert.get("level") or "", "通知")
    title = _clip(f"【{lv}】{alert.get('title') or '看板告警'}", _TITLE_MAX + 8)
    text = str(alert.get("text") or "")
    if alert.get("ts"):
        text = f"{text}\n\n{alert['ts']}"

    if ch == "serverchan":
        return {"url": f"https://sctapi.ftqq.com/{cfg['key']}.send",
                "data": {"title": title, "desp": text}}
    if ch == "bark":
        return {"url": f"{(cfg.get('server') or DEFAULT_BARK_SERVER).rstrip('/')}/push",
                "json": {"device_key": cfg["key"], "title": title,
                         "body": text, "group": "etf-board"}}
    if ch == "wecom":
        content = f"{title}\n{text}"
        raw = content.encode("utf-8")
        if len(raw) > _WECOM_CONTENT_BYTES:
            content = raw[: _WECOM_CONTENT_BYTES - 3].decode("utf-8", errors="ignore") + "…"
        return {"url": cfg["url"],
                "json": {"msgtype": "text", "text": {"content": content}}}
    return None


def merge_secret(payload, current: dict) -> dict:
    """PUT 语义：key/url 留空或仍是掩码 → 保留已存值（掩码来自 view()，不可逆）。"""
    out = dict(payload or {})
    for f in ("key", "url"):
        v = out.get(f)
        if v is None or (isinstance(v, str) and (v.strip() == "" or "****" in v)):
            out[f] = current.get(f)
    return out


def _mask(s: str | None) -> str | None:
    if not s:
        return s
    if len(s) <= 8:
        return "****"
    return f"{s[:4]}****{s[-4:]}"


def _mask_url(u: str | None) -> str | None:
    """URL 只保留 scheme://host，路径与 query（含 webhook key）全部打码。"""
    if not u:
        return u
    from urllib.parse import urlsplit
    parts = urlsplit(u)
    return f"{parts.scheme}://{parts.netloc}/****"


def view(cfg: dict) -> dict:
    """给前端的视图：密钥脱敏 + 渠道/级别选项。纯函数。"""
    return {
        **cfg,
        "key": _mask(cfg.get("key")),
        "url": _mask_url(cfg.get("url")),
        "channel_choices": [{"key": k, "label": CHANNEL_LABELS[k]} for k in CHANNELS],
        "level_choices": [{"key": k, "label": _LEVEL_LABELS[k]} for k in LEVELS],
    }


# ---------------------------------------------------------------------------
# I/O 薄壳（失败降级、绝不抛异常）
# ---------------------------------------------------------------------------

def load() -> dict:
    raw = None
    try:
        with open(_PATH, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        log.warning("读取推送设置失败，改用默认值: %s", exc)
    cfg, errors = parse_config(raw)
    for err in errors:
        log.warning("推送设置含非法值，已回落默认: %s", err)
    return cfg


def save(cfg: dict) -> bool:
    payload = {k: cfg.get(k) for k in _FIELDS}
    tmp = _PATH + ".tmp"
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _PATH)
        return True
    except OSError as exc:
        log.warning("写入推送设置失败: %s", exc)
        return False


# ---------------------------------------------------------------------------
# 网络与派发
# ---------------------------------------------------------------------------

async def _send(req: dict) -> tuple[bool, str]:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
            r = await client.request("POST", req["url"],
                                     data=req.get("data"), json=req.get("json"))
        if 200 <= r.status_code < 300:
            return True, "已推送"
        return False, f"HTTP {r.status_code}"
    except Exception as exc:  # noqa: BLE001 — 不记异常正文，可能含带密钥的 URL
        log.info("推送失败: %s", type(exc).__name__)
        return False, "网络错误"


async def dispatch(alert: dict) -> dict:
    """告警 → 远程推送的唯一入口。任何异常都吞掉，绝不影响告警总线。"""
    try:
        cfg = load()
        if not should_push(alert, cfg):
            return {"ok": False, "skipped": True, "msg": "未启用或低于最低推送级别"}
        req = build_request(cfg, alert)
        if not req:
            return {"ok": False, "skipped": True, "msg": "渠道未配置"}
        ok, msg = await _send(req)
        return {"ok": ok, "msg": msg}
    except Exception as exc:  # noqa: BLE001
        log.info("推送派发异常: %s", type(exc).__name__)
        return {"ok": False, "msg": "推送派发失败"}


async def send_test() -> tuple[bool, str]:
    """连通性测试：按当前已保存配置发一条【测试】告警。"""
    cfg = load()
    if not cfg.get("enabled"):
        return False, "请先启用推送并保存设置"
    if not cfg.get("configured"):
        return False, "请先完成渠道配置"
    import datetime as dt
    alert = {
        "type": "test", "level": "critical", "dir": "neutral",
        "etf_name": "推送测试",
        "title": "【测试】看板推送连通性",
        "text": "这是一条测试推送：收到说明渠道配置成功。",
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
    }
    req = build_request(cfg, alert)
    if not req:
        return False, "渠道未配置"
    return await _send(req)
