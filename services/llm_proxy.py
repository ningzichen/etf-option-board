"""自带模型的临时代理：配置只随请求使用，不落盘、不缓存、不记录敏感内容。"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from services import em


def _text(data: dict, key: str, label: str, limit: int, *, required: bool = True) -> str:
    value = data.get(key, "")
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{label}格式或长度不正确")
    value = value.strip()
    if required and not value:
        raise ValueError(f"请填写{label}")
    return value


def request_parts(data: object) -> tuple[str, dict, dict]:
    """纯函数：固定 POST 模型协议，不接受任意方法、请求头或 URL 中的凭据。"""
    if not isinstance(data, dict):
        raise ValueError("代理请求必须是 JSON 对象")
    protocol = data.get("protocol")
    if protocol not in ("openai", "anthropic"):
        raise ValueError("请选择 OpenAI Chat 或 Anthropic 协议")
    base = _text(data, "baseUrl", "Base URL", 2048)
    try:
        url = urlsplit(base)
        if (url.scheme not in ("http", "https") or not url.hostname or
                url.username is not None or url.password is not None or
                "?" in base or "#" in base or "\\" in base or
                any(c.isspace() or ord(c) < 32 for c in base)):
            raise ValueError
        _ = url.port
    except ValueError:
        raise ValueError("Base URL 需为完整 HTTP/HTTPS 地址，不能包含凭据、查询参数或锚点") from None
    suffix = "/messages" if protocol == "anthropic" else "/chat/completions"
    path = url.path.rstrip("/")
    if path.endswith(("/messages", "/chat/completions")) and not path.endswith(suffix):
        raise ValueError("接口路径与兼容协议不一致")
    path = path if path.endswith(suffix) else (path or "/v1") + suffix
    endpoint = urlunsplit((url.scheme, url.netloc, path, "", ""))

    model = _text(data, "model", "模型 ID", 256)
    key = _text(data, "apiKey", "API Key", 8192, required=False)
    if any(ord(c) < 32 or ord(c) > 126 for c in key):
        raise ValueError("API Key 不能包含换行或非 ASCII 字符")
    system = _text(data, "system", "系统提示词", 65536)
    user = _text(data, "user", "用户提示词", 262144)
    test = data.get("test", False)
    if not isinstance(test, bool):
        raise ValueError("测试标记格式不正确")
    # 保留 Anthropic 的原预算；OpenAI 兼容推理模型需给推理和正文共同留足空间。
    max_tokens = data.get("maxTokens", 4096 if protocol == "anthropic" else 16384)
    if type(max_tokens) is not int or not 1024 <= max_tokens <= 65536:
        raise ValueError("最大输出 Token 需为 1024–65536 之间的整数")

    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    # opencode zen 的免费模型要求带会话标识（否则会被判定为"非 OpenCode 客户端"）。
    # 代理侧补一个随机 UUID，不落盘、不进日志。
    if url.netloc.endswith("opencode.ai"):
        import uuid
        headers["x-opencode-session"] = uuid.uuid4().hex
    body = {"model": model, "max_tokens": max_tokens, "stream": False}
    if protocol == "anthropic":
        if key:
            headers["x-api-key"] = key
        headers["anthropic-version"] = "2023-06-01"
        body["system"] = system
        body["messages"] = [{"role": "user", "content": user}]
    else:
        if key:
            headers["Authorization"] = "Bearer " + key
        body["messages"] = [{"role": "system", "content": system},
                            {"role": "user", "content": user}]
    return endpoint, headers, body


async def forward(data: object) -> dict:
    try:
        url, headers, body = request_parts(data)
    except ValueError as exc:
        # 校验错误均为上面定义的固定文案，不含输入值。
        return {"error": str(exc)}
    return await em.llm_post(url, headers, body)
