"""push 模块纯函数与派发逻辑的单元测试（Seam 1：纯决策函数层）。

约束与 test_user_settings 一致：只喂输入、断言输出 —— 不触网、不读系统时钟、
不写磁盘。网络层 `_send` 一律打桩；`load/save` 文件薄壳不在本层覆盖。
"""

import asyncio

import pytest

from services import alerts, push


# ---------------------------------------------------------------------------
# parse_config：校验与归一化
# ---------------------------------------------------------------------------

class TestParseConfig:

    def test_unconfigured_returns_safe_defaults(self):
        cfg, errs = push.parse_config(None)
        assert errs == []
        assert cfg["enabled"] is False
        assert cfg["channel"] == "none"
        assert cfg["min_level"] == "critical"
        assert cfg["configured"] is False

    @pytest.mark.parametrize("raw", [[], "x", 3, object()])
    def test_non_dict_input_degrades(self, raw):
        cfg, errs = push.parse_config(raw)
        assert errs == []
        assert cfg["channel"] == "none"

    def test_valid_serverchan(self):
        cfg, errs = push.parse_config(
            {"enabled": True, "channel": "serverchan", "key": " SCT1234567890abcd "})
        assert errs == []
        assert cfg["enabled"] is True
        assert cfg["key"] == "SCT1234567890abcd"   # 剥离首尾空白
        assert cfg["configured"] is True

    def test_unknown_channel_rejected(self):
        cfg, errs = push.parse_config({"channel": "telegram"})
        assert any("渠道" in e for e in errs)
        assert cfg["channel"] == "none"

    def test_serverchan_requires_key(self):
        cfg, errs = push.parse_config({"channel": "serverchan"})
        assert any("SendKey" in e for e in errs)
        assert cfg["configured"] is False

    def test_bark_requires_key_and_https_server(self):
        cfg, errs = push.parse_config({"channel": "bark"})
        assert any("Key" in e for e in errs)

        cfg, errs = push.parse_config(
            {"channel": "bark", "key": "AbCdEf123456", "server": "http://self.host"})
        assert any("https" in e for e in errs)

        cfg, errs = push.parse_config(
            {"channel": "bark", "key": "AbCdEf123456"})
        assert errs == []
        assert cfg["server"] == push.DEFAULT_BARK_SERVER

    @pytest.mark.parametrize("server", [
        "https://127.0.0.1:8443",
        "https://[::1]:8443",
        "https://localhost:8443",
        "https://api.local",
        "https://192.168.1.10",
        "https://169.254.169.254",
        "https://0.0.0.0",
    ])
    def test_bark_rejects_loopback_and_private_server(self, server):
        """自建 Bark 只放行公网 https：/api/push/test 会按配置发请求，不能变成 SSRF 打点。"""
        cfg, errs = push.parse_config(
            {"channel": "bark", "key": "AbCdEf123456", "server": server})
        assert any("内网" in e for e in errs), server

    @pytest.mark.parametrize("server", [
        "https://api.day.app",
        "https://bark.my-domain.com",
        "https://bark.example.cn:8443",
    ])
    def test_bark_allows_public_https_server(self, server):
        cfg, errs = push.parse_config(
            {"channel": "bark", "key": "AbCdEf123456", "server": server})
        assert errs == [], server

    def test_wecom_requires_official_webhook(self):
        cfg, errs = push.parse_config({"channel": "wecom"})
        assert any("Webhook" in e for e in errs)

        cfg, errs = push.parse_config({"channel": "wecom", "url": "http://evil.host/hook"})
        assert errs                      # 非 https / 非官方域名都拒绝

        cfg, errs = push.parse_config(
            {"channel": "wecom",
             "url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"})
        assert errs == []
        assert cfg["configured"] is True

    def test_key_must_not_contain_whitespace(self):
        cfg, errs = push.parse_config({"channel": "serverchan", "key": "SCT 1234 abcd"})
        assert any("空白" in e for e in errs)

    def test_min_level_validation(self):
        cfg, errs = push.parse_config({"min_level": "warn"})
        assert errs == []
        assert cfg["min_level"] == "warn"

        cfg, errs = push.parse_config({"min_level": "loud"})
        assert any("级别" in e for e in errs)
        assert cfg["min_level"] == "critical"

    @pytest.mark.parametrize("raw,want", [
        (True, True), (False, False),
        ("true", True), ("OFF", False), ("1", True), ("0", False),
        (1, True), (0, False),
    ])
    def test_enabled_accepts_boolish(self, raw, want):
        cfg, errs = push.parse_config({"enabled": raw})
        assert errs == []
        assert cfg["enabled"] is want

    def test_enabled_none_means_off(self):
        cfg, errs = push.parse_config({"enabled": None})
        assert errs == []
        assert cfg["enabled"] is False


# ---------------------------------------------------------------------------
# should_push：开关 + 级别过滤
# ---------------------------------------------------------------------------

class TestShouldPush:

    def _cfg(self, **kw):
        cfg, _ = push.parse_config({"channel": "serverchan", "key": "SCT12345678", **kw})
        return cfg

    def test_disabled_never_pushes(self):
        cfg = self._cfg(enabled=False, min_level="info")
        assert push.should_push({"level": "critical"}, cfg) is False

    def test_unconfigured_never_pushes(self):
        cfg = self._cfg(enabled=True, channel="none")
        assert push.should_push({"level": "critical"}, cfg) is False

    def test_min_level_critical_filters_lower(self):
        cfg = self._cfg(enabled=True, min_level="critical")
        assert push.should_push({"level": "critical"}, cfg) is True
        assert push.should_push({"level": "warn"}, cfg) is False
        assert push.should_push({"level": "info"}, cfg) is False

    def test_min_level_info_pushes_everything(self):
        cfg = self._cfg(enabled=True, min_level="info")
        for lv in ("critical", "warn", "info"):
            assert push.should_push({"level": lv}, cfg) is True

    def test_missing_or_unknown_level_treated_as_info(self):
        cfg = self._cfg(enabled=True, min_level="info")
        assert push.should_push({}, cfg) is True
        assert push.should_push({"level": "weird"}, cfg) is True


# ---------------------------------------------------------------------------
# build_request：请求构造（纯函数，不触网）
# ---------------------------------------------------------------------------

class TestBuildRequest:

    def _cfg(self, channel, **kw):
        cfg, _ = push.parse_config({"channel": channel, **kw})
        assert cfg["configured"] is True
        return cfg

    def test_serverchan_form_payload(self):
        cfg = self._cfg("serverchan", key="SCT1234567890abcd")
        req = push.build_request(cfg, {"title": "T", "text": "B", "ts": "2026-11-09T15:00:00"})
        assert req["url"] == "https://sctapi.ftqq.com/SCT1234567890abcd.send"
        assert req["data"]["desp"].startswith("B")
        assert "2026-11-09T15:00:00" in req["data"]["desp"]

    def test_bark_push_json(self):
        cfg = self._cfg("bark", key="AbCdEf123456")
        req = push.build_request(cfg, {"title": "T", "text": "B"})
        assert req["url"].endswith("/push")
        assert req["json"]["device_key"] == "AbCdEf123456"
        assert req["json"]["group"] == "etf-board"

    def test_wecom_content_byte_limit(self):
        cfg = self._cfg("wecom", url="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=k")
        req = push.build_request(cfg, {"title": "T", "text": "字" * 5000})
        content = req["json"]["text"]["content"]
        assert len(content.encode("utf-8")) <= push._WECOM_CONTENT_BYTES

    def test_none_channel_returns_none(self):
        cfg, _ = push.parse_config({})
        assert push.build_request(cfg, {"title": "T"}) is None


# ---------------------------------------------------------------------------
# merge_secret：PUT 语义 —— 掩码/留空保留原值
# ---------------------------------------------------------------------------

class TestMergeSecret:

    def test_masked_and_empty_preserve_current(self):
        cur = {"key": "REALKEY123456", "url": "https://qyapi.weixin.qq.com/x?y=z"}
        out = push.merge_secret({"key": "REAL****3456", "url": ""}, cur)
        assert out["key"] == "REALKEY123456"
        assert out["url"] == "https://qyapi.weixin.qq.com/x?y=z"

    def test_new_value_overrides(self):
        out = push.merge_secret({"key": "NEWKEY999999"}, {"key": "OLDKEY111111"})
        assert out["key"] == "NEWKEY999999"

    def test_none_payload_safe(self):
        assert push.merge_secret(None, {"key": "K"})["key"] == "K"


# ---------------------------------------------------------------------------
# view：密钥脱敏
# ---------------------------------------------------------------------------

class TestView:

    def test_key_masked(self):
        cfg, _ = push.parse_config({"channel": "serverchan", "key": "SCT1234567890abcd"})
        v = push.view(cfg)
        assert v["key"].startswith("SCT1") and "****" in v["key"]
        assert "567890abcd" not in v["key"]

    def test_short_key_fully_masked(self):
        cfg, _ = push.parse_config({"channel": "bark", "key": "shortkey"})
        assert push.view(cfg)["key"] == "****"

    def test_wecom_url_masked(self):
        cfg, _ = push.parse_config(
            {"channel": "wecom", "url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=SECRET"})
        v = push.view(cfg)
        assert "SECRET" not in v["url"]
        assert v["url"].startswith("https://qyapi")

    def test_choices_exposed(self):
        v = push.view(push.parse_config({})[0])
        assert [c["key"] for c in v["channel_choices"]] == list(push.CHANNELS)
        assert [c["key"] for c in v["level_choices"]] == list(push.LEVELS)


# ---------------------------------------------------------------------------
# dispatch：派发（网络层打桩）
# ---------------------------------------------------------------------------

class TestDispatch:

    def _enable(self, monkeypatch, **kw):
        cfg, _ = push.parse_config(
            {"enabled": True, "channel": "serverchan", "key": "SCT12345678", **kw})
        monkeypatch.setattr(push, "load", lambda: cfg)
        return cfg

    def test_disabled_skips_network(self, monkeypatch):
        cfg, _ = push.parse_config({})
        monkeypatch.setattr(push, "load", lambda: cfg)
        calls = []
        monkeypatch.setattr(push, "_send", lambda req: asyncio.sleep(0, result=calls.append(req)))
        r = asyncio.run(push.dispatch({"level": "critical", "title": "T"}))
        assert calls == []
        assert r["skipped"] is True

    def test_below_min_level_skips(self, monkeypatch):
        self._enable(monkeypatch, min_level="critical")
        calls = []

        async def fake_send(req):
            calls.append(req)
            return True, "ok"

        monkeypatch.setattr(push, "_send", fake_send)
        r = asyncio.run(push.dispatch({"level": "warn", "title": "T"}))
        assert calls == []
        assert r["skipped"] is True

    def test_matching_level_sends_once(self, monkeypatch):
        self._enable(monkeypatch)
        calls = []

        async def fake_send(req):
            calls.append(req)
            return True, "已推送"

        monkeypatch.setattr(push, "_send", fake_send)
        r = asyncio.run(push.dispatch({"level": "critical", "title": "T", "text": "B"}))
        assert len(calls) == 1
        assert r["ok"] is True

    def test_send_failure_degrades_without_raising(self, monkeypatch):
        self._enable(monkeypatch)

        async def boom(req):
            raise RuntimeError("network down")

        monkeypatch.setattr(push, "_send", boom)
        r = asyncio.run(push.dispatch({"level": "critical", "title": "T"}))
        assert r["ok"] is False

    def test_load_failure_degrades(self, monkeypatch):
        def boom():
            raise OSError("disk error")

        monkeypatch.setattr(push, "load", boom)
        r = asyncio.run(push.dispatch({"level": "critical", "title": "T"}))
        assert r["ok"] is False


# ---------------------------------------------------------------------------
# send_test：连通性测试（网络层打桩）
# ---------------------------------------------------------------------------

class TestSendTest:

    def test_unconfigured_refuses(self, monkeypatch):
        monkeypatch.setattr(push, "load", lambda: push.parse_config({})[0])
        called = []

        async def fake_send(req):
            called.append(req)
            return True, "ok"

        monkeypatch.setattr(push, "_send", fake_send)
        ok, msg = asyncio.run(push.send_test())
        assert ok is False and called == []

    def test_configured_sends_test_alert(self, monkeypatch):
        cfg, _ = push.parse_config(
            {"enabled": True, "channel": "serverchan", "key": "SCT12345678"})
        monkeypatch.setattr(push, "load", lambda: cfg)
        calls = []

        async def fake_send(req):
            calls.append(req)
            return True, "已推送"

        monkeypatch.setattr(push, "_send", fake_send)
        ok, msg = asyncio.run(push.send_test())
        assert ok is True
        assert "测试" in calls[0]["data"]["title"]


# ---------------------------------------------------------------------------
# alerts.publish 挂钩：远程推送不阻塞总线、失败不冒泡
# ---------------------------------------------------------------------------

class TestAlertsHook:

    def test_publish_dispatches_push(self, monkeypatch):
        seen = []

        async def fake_dispatch(alert):
            seen.append(alert)

        monkeypatch.setattr(push, "dispatch", fake_dispatch)
        a = asyncio.run(alerts.publish({"title": "T", "level": "critical"}))
        assert a["id"] > 0
        assert len(seen) == 1 and seen[0]["title"] == "T"

    def test_push_failure_does_not_break_publish(self, monkeypatch):
        async def boom(alert):
            raise RuntimeError("x")

        monkeypatch.setattr(push, "dispatch", boom)
        a = asyncio.run(alerts.publish({"title": "T", "level": "critical"}))
        assert a["id"] > 0          # 总线照常入库返回
