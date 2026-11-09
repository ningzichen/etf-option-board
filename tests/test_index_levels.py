"""上证指数手动支撑/压力位 + 关键位评分的纯函数单测。

约束：只喂输入、断言输出 —— 不触网、不读系统时钟、不写磁盘。
文件读写薄壳（load/save）由端点冒烟覆盖。
"""

from __future__ import annotations

from services import index_levels as il
from services import index_levels_manual as m


def _bars(high: float = 3800.0, low: float = 3600.0, n: int = 20) -> list[dict]:
    return [{"date": f"2026-10-{i + 1:02d}", "open": 3700, "close": 3700,
             "high": high, "low": low} for i in range(n)]


class TestEvaluateWithoutManual:
    """不传 manual 时行为必须与旧版完全一致。"""

    def test_no_manual_same_as_legacy(self):
        prev, last, mins = _bars(), 3700.0, []
        assert il.evaluate(prev, last, mins) == il.evaluate(prev, last, mins, None)
        res = il.evaluate(prev, last, mins)
        keys = [l["key"] for l in res["levels"]]
        assert keys == ["low20", "high20", "int_lower", "int_upper", "ma5", "ma11"]
        assert not any(k.startswith("manual_") for k in keys)


class TestManualContribution:
    """手动位与自动位同规则：≤0.3% → ±8，0.3–0.8% → ±4，一起累加后 clamp ±15。"""

    def test_support_within_03pct_scores_8(self):
        res = il.evaluate(_bars(), 3700.0, [], {"support": 3695.0, "pressure": 3900.0})
        row = next(l for l in res["levels"] if l["key"] == "manual_support")
        assert row["contrib"] == 8.0
        assert row["side"] == "support"
        assert row["dist_pct"] == 0.14

    def test_support_between_03_and_08_scores_4(self):
        res = il.evaluate(_bars(), 3700.0, [], {"support": 3680.0, "pressure": 3900.0})
        row = next(l for l in res["levels"] if l["key"] == "manual_support")
        assert row["contrib"] == 4.0

    def test_support_far_away_scores_0(self):
        res = il.evaluate(_bars(), 3700.0, [], {"support": 3000.0, "pressure": 3900.0})
        row = next(l for l in res["levels"] if l["key"] == "manual_support")
        assert row["contrib"] == 0.0

    def test_pressure_within_03pct_scores_minus_8(self):
        res = il.evaluate(_bars(), 3700.0, [], {"support": 3000.0, "pressure": 3705.0})
        row = next(l for l in res["levels"] if l["key"] == "manual_pressure")
        assert row["contrib"] == -8.0
        assert row["dist_pct"] == 0.14

    def test_pressure_between_03_and_08_scores_minus_4(self):
        res = il.evaluate(_bars(), 3700.0, [], {"support": 3000.0, "pressure": 3720.0})
        row = next(l for l in res["levels"] if l["key"] == "manual_pressure")
        assert row["contrib"] == -4.0

    def test_manual_rows_do_not_trigger_breakout(self):
        # 手动压力被 3 根 1m K 突破，但 20 日高点没破 → 不算突破
        res = il.evaluate(_bars(high=3900.0), 3800.0, [3801, 3802, 3803],
                          {"support": 3000.0, "pressure": 3800.5})
        assert res["breakout"] is False
        assert res["breakdown"] is False

    def test_total_clamped_to_15(self):
        # 20 日低点 3600、整百 3600、均线多头、手动支撑 3599 → raw 25
        res = il.evaluate(_bars(high=4000.0, low=3600.0), 3600.5, [],
                          {"support": 3599.0, "pressure": 9999.0})
        assert res["total"] == 15.0

    def test_manual_counts_even_when_history_insufficient(self):
        res = il.evaluate(_bars(n=3), 3700.0, [], {"support": 3695.0, "pressure": 3900.0})
        assert res["insufficient"] is True
        assert [l["key"] for l in res["levels"]] == ["manual_support", "manual_pressure"]
        assert res["total"] == 8.0   # 支撑 +8，压力（2.7% 远）0


class TestParseRecord:
    """保存前的校验：必须两个都填、100–99999、支撑 < 压力、日期合法。"""

    def test_valid(self):
        rec, errs = m.parse_record({"support": "3900.456", "pressure": 4000}, day="2026-11-09")
        assert errs == []
        assert rec == {"date": "2026-11-09", "support": 3900.46, "pressure": 4000.0}

    def test_day_argument_overrides_payload_date(self):
        rec, errs = m.parse_record({"date": "2026-01-01", "support": 3900, "pressure": 4000},
                                   day="2026-11-09")
        assert errs == [] and rec["date"] == "2026-11-09"

    def test_missing_both(self):
        _, errs = m.parse_record({}, day="2026-11-09")
        assert "支撑位必须是数字" in errs and "压力位必须是数字" in errs

    def test_non_numeric(self):
        _, errs = m.parse_record({"support": "abc", "pressure": 4000}, day="2026-11-09")
        assert "支撑位必须是数字" in errs

    def test_bool_rejected(self):
        _, errs = m.parse_record({"support": True, "pressure": 4000}, day="2026-11-09")
        assert "支撑位必须是数字" in errs

    def test_out_of_range(self):
        _, errs = m.parse_record({"support": 50, "pressure": 4000}, day="2026-11-09")
        assert any("支撑位需在" in e for e in errs)

    def test_support_must_be_below_pressure(self):
        _, errs = m.parse_record({"support": 4000, "pressure": 4000}, day="2026-11-09")
        assert "支撑位必须小于压力位" in errs

    def test_bad_date(self):
        _, errs = m.parse_record({"support": 3900, "pressure": 4000}, day="2026/11/09")
        assert "日期必须形如 YYYY-MM-DD" in errs


class TestEffective:
    """顺延生效：取 date ≤ 今天的最大日期记录。"""

    RECORDS = {
        "2026-11-05": {"support": 3800, "pressure": 3900},
        "2026-11-07": {"support": 3850, "pressure": 3950},
    }

    def test_picks_latest_not_after_day(self):
        cur = m.effective(self.RECORDS, "2026-11-09")
        assert cur == {"support": 3850.0, "pressure": 3950.0,
                       "date": "2026-11-07", "carried": True}

    def test_same_day_is_not_carried(self):
        cur = m.effective(self.RECORDS, "2026-11-07")
        assert cur["carried"] is False and cur["date"] == "2026-11-07"

    def test_none_when_all_records_in_future(self):
        assert m.effective(self.RECORDS, "2026-11-01") is None

    def test_ignores_malformed_keys_and_values(self):
        recs = {"2026-11-07": {"support": 3850, "pressure": 3950},
                "oops": {"support": 1, "pressure": 2},
                "2026-11-08": {"support": None, "pressure": 4000}}
        cur = m.effective(recs, "2026-11-09")
        assert cur["date"] == "2026-11-07"

    def test_empty_records(self):
        assert m.effective({}, "2026-11-09") is None
