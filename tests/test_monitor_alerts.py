"""变盘类告警的「按轮合并」纯逻辑。

约束：不触网、不读写磁盘；时间全部注入。告警总线只在内存里，测完清空。
"""

import asyncio
import datetime as dt

import pytest

from services import alerts, monitor

NOW = dt.datetime(2026, 11, 5, 10, 30)


def sig(code: str, name: str, side: str, score: float) -> dict:
    return {
        "etf_code": code, "etf_name": name, "type": "reversal", "level": "critical",
        "dir": side, "score": score,
        "title": f"{name} 看{'多' if side == 'bull' else '空'}变盘信号 {score:.0f}分",
        "text": f"方向{score:.0f}分｜15m确认",
        "factors": ["15m确认"], "contract": None,
        "ts": NOW.isoformat(timespec="seconds"),
    }


@pytest.fixture(autouse=True)
def _clean():
    alerts._history.clear()
    monitor._key_fired.clear()
    monitor._manual_remind_ts = 0.0
    monitor._manual_remind_day = ""
    yield
    alerts._history.clear()
    monitor._key_fired.clear()
    monitor._manual_remind_ts = 0.0
    monitor._manual_remind_day = ""


class TestReversalAggregate:
    def test_single_signal_keeps_original_alert(self):
        s = sig("510300", "沪深300ETF华泰柏瑞", "bull", 72.0)
        asyncio.run(monitor._reversal_alert([s]))
        out = alerts.history(5)
        assert len(out) == 1
        assert out[0]["title"] == s["title"]
        assert out[0]["etf_code"] == "510300"
        assert "items" not in out[0]

    def test_multiple_signals_merge_into_one(self):
        sigs = [
            sig("510300", "沪深300ETF华泰柏瑞", "bull", 72.0),
            sig("510500", "中证500ETF南方", "bear", 68.0),
            sig("510050", "上证50ETF华夏", "bull", 70.0),
        ]
        asyncio.run(monitor._reversal_alert(sigs))
        out = alerts.history(10)
        assert len(out) == 1                     # 三条信号只发一条
        a = out[0]
        assert a["type"] == "reversal" and a["level"] == "critical"
        assert a["title"].startswith("3 只标的同步变盘")
        assert a["dir"] == "mixed"               # 多空混杂不给单边方向
        assert [i["code"] for i in a["items"]] == ["510300", "510050", "510500"]  # 按分数降序
        assert "510300" not in a                   # 市场级告警不带单标的代码（列表不可点错标的）

    def test_same_direction_keeps_side(self):
        asyncio.run(monitor._reversal_alert([
            sig("510300", "沪深300ETF华泰柏瑞", "bull", 72.0),
            sig("510050", "上证50ETF华夏", "bull", 66.0),
        ]))
        assert alerts.history(1)[0]["dir"] == "bull"

    def test_empty_is_noop(self):
        asyncio.run(monitor._reversal_alert([]))
        assert alerts.history(5) == []


class TestKeyTimeAlert:
    def test_one_alert_per_key_time(self):
        asyncio.run(monitor._key_time_alert("10:30", now=NOW))
        asyncio.run(monitor._key_time_alert("10:30", now=NOW))   # 同一时点重复调用
        out = alerts.history(10)
        assert len(out) == 1
        assert out[0]["type"] == "key_time"
        assert "10:30" in out[0]["title"]
        assert len(out[0]["items"]) == 6          # 六只标的列在明细里，而不是推六条

    def test_new_key_time_alerts_again(self):
        asyncio.run(monitor._key_time_alert("10:30", now=NOW))
        asyncio.run(monitor._key_time_alert("11:00", now=NOW))
        assert [a["type"] for a in alerts.history(5)] == ["key_time", "key_time"]

    def test_none_key_time_noop(self):
        asyncio.run(monitor._key_time_alert(None, now=NOW))
        assert alerts.history(5) == []


class TestManualLevelsReminder:
    """收盘前没设手动支撑/压力位就每 10 分钟提醒一次（09:15–15:00，工作日）。"""

    def _call(self, now, records, monkeypatch):
        monkeypatch.setattr(monitor.idxm, "load_records", lambda: records)
        asyncio.run(monitor._manual_levels_alert(now))

    def test_window_only_weekday_0915_to_1500(self):
        assert monitor.manual_reminder_window(dt.datetime(2026, 11, 9, 9, 15)) is True
        assert monitor.manual_reminder_window(dt.datetime(2026, 11, 9, 12, 30)) is True
        assert monitor.manual_reminder_window(dt.datetime(2026, 11, 9, 15, 0)) is True
        assert monitor.manual_reminder_window(dt.datetime(2026, 11, 9, 9, 14)) is False
        assert monitor.manual_reminder_window(dt.datetime(2026, 11, 9, 15, 1)) is False
        assert monitor.manual_reminder_window(dt.datetime(2026, 11, 7, 10, 0)) is False  # 周六

    def test_first_reminder_is_immediate_and_carries_old_values(self, monkeypatch):
        records = {"2026-11-06": {"support": 3850, "pressure": 3950}}
        self._call(dt.datetime(2026, 11, 9, 9, 15), records, monkeypatch)
        out = alerts.history(5)
        assert len(out) == 1
        a = out[0]
        assert a["type"] == "manual_levels"
        assert a["level"] == "warn" and a["silent"] is True
        assert a["title"] == "今天还没设上证支撑/压力位"
        assert "沿用 2026-11-06" in a["text"]
        assert "3850.00" in a["text"] and "3950.00" in a["text"]

    def test_no_values_says_none(self, monkeypatch):
        self._call(dt.datetime(2026, 11, 9, 9, 15), {}, monkeypatch)
        assert "还没有任何手动位" in alerts.history(5)[0]["text"]

    def test_ten_minute_cooldown(self, monkeypatch):
        for hh, mm in ((9, 15), (9, 20), (9, 24), (9, 25)):
            self._call(dt.datetime(2026, 11, 9, hh, mm), {}, monkeypatch)
        # 09:15 与 09:25 各一条，中间 9:20/9:24 被冷却挡掉
        assert len(alerts.history(5)) == 2

    def test_stops_once_today_saved(self, monkeypatch):
        records = {"2026-11-09": {"support": 3900, "pressure": 4000}}
        self._call(dt.datetime(2026, 11, 9, 9, 15), records, monkeypatch)
        assert alerts.history(5) == []

    def test_outside_window_never_reminds(self, monkeypatch):
        self._call(dt.datetime(2026, 11, 9, 23, 0), {}, monkeypatch)
        self._call(dt.datetime(2026, 11, 7, 10, 0), {}, monkeypatch)
        assert alerts.history(5) == []

    def test_reminds_again_next_day(self, monkeypatch):
        self._call(dt.datetime(2026, 11, 9, 14, 59), {}, monkeypatch)
        self._call(dt.datetime(2026, 11, 10, 9, 15), {}, monkeypatch)
        assert len(alerts.history(5)) == 2
