"""成交额同比因子（全A / 当前指数）的纯函数单测。

约束：只喂输入、断言输出 —— 不触网、不读系统时钟、不写磁盘。
"""

from __future__ import annotations

import datetime as dt

from services import market_turnover as mt
from services import volume_ratio as vr


def _pts(*pairs) -> list[tuple]:
    return list(pairs)


TODAY = "2026-11-09"
PREV = "2026-11-06"


class TestCumAt:
    def test_takes_last_point_not_after_hhmm(self):
        pts = _pts(("0930", 100.0), ("1000", 200.0), ("1030", 300.0))
        assert vr.cum_at(pts, "1010") == 200.0
        assert vr.cum_at(pts, "1030") == 300.0
        assert vr.cum_at(pts, "1100") == 300.0

    def test_none_when_nothing_matches(self):
        assert vr.cum_at(_pts(("1000", 1.0)), "0930") is None
        assert vr.cum_at([], "1000") is None


class TestCompareAt:
    def test_ratio(self):
        today = _pts(("0930", 50.0), ("1000", 150.0))
        prev = _pts(("0930", 100.0), ("1000", 100.0))
        assert vr.compare_at(today, prev, "1000") == 1.5
        assert vr.compare_at(today, prev, "0930") == 0.5

    def test_none_on_missing_side(self):
        assert vr.compare_at(_pts(("1000", 1.0)), [], "1000") is None
        assert vr.compare_at([], _pts(("1000", 1.0)), "1000") is None
        assert vr.compare_at([], [], "1000") is None


class TestScore:
    """放量顺势、缩量背离反向；平量/横盘/开盘初期都不计分。"""

    def test_surge_with_up_move_is_bullish(self):
        assert vr.score(1.20, 0.50, 10.0)["factor"] == 10.0

    def test_surge_with_down_move_is_bearish(self):
        assert vr.score(1.20, -0.50, 10.0)["factor"] == -10.0

    def test_shrink_with_up_move_is_bearish(self):
        r = vr.score(0.80, 0.50, 10.0)
        assert r["factor"] == -10.0 and r["status"] == "缩量"

    def test_shrink_with_down_move_is_bullish(self):
        assert vr.score(0.80, -0.50, 8.0)["factor"] == 8.0

    def test_flat_volume_scores_zero(self):
        r = vr.score(1.00, 0.80, 10.0)
        assert r["factor"] == 0.0 and r["status"] == "平量"

    def test_threshold_boundaries(self):
        assert vr.score(1.10, 0.50, 10.0)["status"] == "放量"
        assert vr.score(1.099, 0.50, 10.0)["status"] == "平量"
        assert vr.score(0.90, 0.50, 10.0)["status"] == "缩量"
        assert vr.score(0.901, 0.50, 10.0)["status"] == "平量"

    def test_flat_index_move_scores_zero(self):
        assert vr.score(1.30, 0.05, 10.0)["factor"] == 0.0
        assert vr.score(1.30, 0.10, 10.0)["factor"] == 0.0
        assert vr.score(1.30, None, 10.0)["factor"] == 0.0

    def test_open_guard(self):
        assert vr.score(1.30, 0.50, 10.0, hhmm="0940")["factor"] == 0.0
        assert vr.score(1.30, 0.50, 10.0, hhmm="0940")["status"] == "开盘初期"
        assert vr.score(1.30, 0.50, 10.0, hhmm="0945")["factor"] == 10.0

    def test_no_data(self):
        assert vr.score(None, 0.5, 10.0)["factor"] == 0.0


class TestMerge:
    def test_sums_two_markets_by_hhmm(self):
        sh = _pts(("0930", 100.0), ("0931", 200.0))
        sz = _pts(("0930", 50.0), ("0932", 70.0))
        assert vr.merge([sh, sz]) == [("0930", 150.0), ("0931", 200.0), ("0932", 70.0)]


class TestPickDays:
    def _series(self, dates):
        return [{"date": d, "points": _pts(("0930", 1.0), ("1500", 10.0))}
                for d in dates]

    def test_intraday_aligns_now(self):
        picked = vr.pick_days(self._series([PREV, TODAY]), TODAY, "1030")
        assert picked["live"] is True and picked["hhmm"] == "1030"
        assert picked["prev"]["date"] == PREV

    def test_after_close_uses_full_day(self):
        picked = vr.pick_days(self._series([PREV, TODAY]), TODAY, "1600")
        assert picked["live"] is False and picked["hhmm"] == "1500"

    def test_weekend_uses_last_two_days(self):
        picked = vr.pick_days(self._series(["2026-11-04", PREV]), TODAY, "1000")
        assert picked["live"] is False
        assert picked["last"]["date"] == PREV

    def test_not_enough_days(self):
        assert vr.pick_days(self._series([TODAY]), TODAY, "1000") is None


class TestEvaluate:
    def _series(self, today_amt: float, prev_amt: float):
        return [
            {"date": PREV, "points": _pts(("0930", prev_amt * 0.2), ("1030", prev_amt), ("1500", prev_amt * 1.2))},
            {"date": TODAY, "points": _pts(("0930", today_amt * 0.2), ("1030", today_amt), ("1500", today_amt * 1.2))},
        ]

    def test_surge_up_scores_full_weight(self):
        ev = vr.evaluate(self._series(120.0, 100.0), 0.60, 10.0, label="全A",
                         now=dt.datetime(2026, 11, 9, 10, 30))
        assert ev["ok"] is True
        assert ev["ratio"] == 1.2
        assert ev["factor"] == 10.0
        assert ev["status"] == "放量"
        assert "较昨日同时点放量 1.20x" in ev["text"]
        assert "10:30 累计" in ev["text"]

    def test_shrink_up_flips_sign(self):
        ev = vr.evaluate(self._series(80.0, 100.0), 0.60, 10.0, label="全A",
                         now=dt.datetime(2026, 11, 9, 10, 30))
        assert ev["factor"] == -10.0 and ev["status"] == "缩量"

    def test_open_guard_zeroes_factor(self):
        ev = vr.evaluate(self._series(120.0, 100.0), 0.60, 10.0, label="全A",
                         now=dt.datetime(2026, 11, 9, 9, 40))
        assert ev["factor"] == 0.0 and ev["status"] == "开盘初期"

    def test_missing_prev_marks_note(self):
        ev = vr.evaluate([{"date": TODAY, "points": _pts(("0930", 1.0), ("1030", 2.0))}],
                         0.6, 10.0, label="全A", now=dt.datetime(2026, 11, 9, 10, 30))
        assert ev["ok"] is False and ev["factor"] == 0.0
        assert ev["note"] == "成交额数据不足"

    def test_after_close_compares_full_day(self):
        ev = vr.evaluate(self._series(120.0, 100.0), 0.60, 8.0, label="指数",
                         now=dt.datetime(2026, 11, 9, 16, 0))
        assert ev["live"] is False
        assert ev["ratio"] == 1.2 and ev["factor"] == 8.0
        assert "全天" in ev["text"]


class TestFactorText:
    def test_surge_has_text_with_index_pct(self):
        t = {"ok": True, "status": "放量", "ratio": 1.2, "text": "全A 10:30 累计成交 1.20 万亿",
             "pct": 0.42}
        assert mt.factor_text(t) == "全A 10:30 累计成交 1.20 万亿，上证 +0.42%"

    def test_flat_and_open_guard_silent(self):
        assert mt.factor_text({"ok": True, "status": "平量", "ratio": 1.0}) is None
        assert mt.factor_text({"ok": True, "status": "开盘初期", "ratio": 1.3}) is None
        assert mt.factor_text({"ok": False, "status": "无数据", "ratio": None}) is None


class TestStaleGuard:
    """工作日开盘后最新序列里没有今天 → 触发带时间戳重取（腾讯边缘缓存偶发滞后）。"""

    def _series(self, last_date):
        return [{"date": PREV, "points": _pts(("0930", 1.0))},
                {"date": last_date, "points": _pts(("0930", 1.0))}]

    def test_stale_when_today_missing_after_open(self):
        now = dt.datetime(2026, 11, 9, 10, 0)
        assert vr.looks_stale(self._series(PREV), now) is True

    def test_fresh_when_today_present(self):
        now = dt.datetime(2026, 11, 9, 10, 0)
        assert vr.looks_stale(self._series(TODAY), now) is False

    def test_no_check_before_0935_or_weekend(self):
        assert vr.looks_stale(self._series(PREV), dt.datetime(2026, 11, 9, 9, 30)) is False
        assert vr.looks_stale(self._series(PREV), dt.datetime(2026, 11, 7, 10, 0)) is False

    def test_after_close_full_day_wording(self):
        ev = vr.evaluate(TestEvaluate()._series(120.0, 100.0), 0.60, 10.0, label="全A",
                         now=dt.datetime(2026, 11, 9, 16, 0))
        assert "较前一交易日放量" in ev["text"]
        assert "基准" in ev["text"]


class TestParseDaySeries:
    """腾讯 day/query 返回新→旧，必须重排升序后再取最近 N 天。"""

    def _rows(self, dates):
        return [{"date": d, "data": ["0930 3600 100 1.0e9", "1500 3660 200 2.0e9"]}
                for d in dates]

    def test_sorts_ascending_and_takes_newest(self):
        from services import em
        rows = self._rows(["20261109", "20261106", "20261105", "20261104", "20261103"])
        out = em.parse_day_series(rows, ndays=2)
        assert [d["date"] for d in out] == ["2026-11-06", "2026-11-09"]
        assert out[-1]["points"][-1] == ("1500", 2.0e9)

    def test_skips_bad_dates(self):
        from services import em
        rows = self._rows(["20261109", "bad", "20261105"])
        out = em.parse_day_series(rows, ndays=5)
        assert [d["date"] for d in out] == ["2026-11-05", "2026-11-09"]
