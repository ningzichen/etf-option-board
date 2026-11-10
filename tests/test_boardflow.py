"""板块资金纯函数（ticket 02）。

约束：只喂输入、断言输出 —— 不触网、不读系统时钟、不写磁盘（日期时间全部注入）。
"""

import datetime as dt

import pytest

from services import boardflow as bf

# 2026-11-04 是星期三
WED = dt.date(2026, 11, 4)


def at(hh: int, mm: int, day: dt.date = WED) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hh, mm)


class TestInSession:
    @pytest.mark.parametrize("hh,mm,expected", [
        (9, 29, False), (9, 30, True), (10, 0, True),
        (11, 29, True), (11, 30, True), (11, 31, False),
        (12, 0, False), (12, 59, False),
        (13, 0, True), (14, 59, True), (15, 0, True), (15, 1, False),
        (0, 0, False), (20, 0, False),
    ])
    def test_weekday_windows(self, hh, mm, expected):
        assert bf.in_session(at(hh, mm)) is expected

    @pytest.mark.parametrize("day", [dt.date(2026, 11, 7), dt.date(2026, 11, 8)])
    def test_weekend_closed(self, day):
        assert bf.in_session(at(10, 0, day)) is False


class TestParseRankRows:
    def test_keeps_valid_rows_and_normalizes(self):
        rows = [
            {"f12": "BK0475", "f14": "半导体", "f62": 1.2e8, "f3": 3.5},
            {"f12": "BK0476", "f14": "证券", "f62": "-2.5e7", "f3": -1.2},
        ]
        out = bf.parse_rank_rows(rows, "industry")
        assert [r["code"] for r in out] == ["BK0475", "BK0476"]
        assert out[0] == {"code": "BK0475", "name": "半导体", "flow": 1.2e8,
                          "pct": 3.5, "speed": 0.0, "type": "industry"}
        assert out[1]["flow"] == -2.5e7 and out[1]["type"] == "industry"

    @pytest.mark.parametrize("row", [
        {"f12": "600519", "f14": "贵州茅台", "f62": 1.0},        # 非板块
        {"f12": "BK0001", "f14": "", "f62": 1.0},                # 缺名称
        {"f12": "BK0002", "f14": "无资金", "f62": "-"},           # 资金不可解析
        {"f12": "BK0003", "f14": "坏值", "f62": None},            # 资金缺失
        "not-a-dict",
        None,
    ])
    def test_drops_bad_rows(self, row):
        assert bf.parse_rank_rows([row], "concept") == []

    def test_keeps_speed_and_defaults_zero(self):
        rows = [
            {"f12": "BK0001", "f14": "甲", "f62": 1.0, "f3": 1.0, "f22": 0.35},
            {"f12": "BK0002", "f14": "乙", "f62": 2.0, "f3": 2.0, "f22": "-"},
        ]
        out = bf.parse_rank_rows(rows, "concept")
        assert out[0]["speed"] == 0.35
        assert out[1]["speed"] == 0.0

    def test_pct_defaults_and_dedupes(self):
        rows = [
            {"f12": "BK0001", "f14": "甲", "f62": 1.0},
            {"f12": "BK0001", "f14": "甲-重复", "f62": 9.0},
            {"f12": "BK0002", "f14": "乙", "f62": 2.0, "f3": "坏"},
        ]
        out = bf.parse_rank_rows(rows, "concept")
        assert len(out) == 2
        assert out[0]["name"] == "甲" and out[0]["flow"] == 1.0
        assert out[1]["pct"] == 0.0


class TestAssignRanks:
    def test_orders_by_flow_desc(self):
        rows = [
            {"code": "BK1", "name": "一", "flow": 1.0},
            {"code": "BK2", "name": "二", "flow": 5.0},
            {"code": "BK3", "name": "三", "flow": -3.0},
        ]
        out = bf.assign_ranks(rows)
        assert [r["code"] for r in out] == ["BK2", "BK1", "BK3"]
        assert [r["rank"] for r in out] == [1, 2, 3]
        assert all(r["prev_rank"] is None for r in out)

    def test_prev_rank_delta_and_new_entries(self):
        rows = [{"code": "BK1", "flow": 9.0}, {"code": "BK2", "flow": 5.0}]
        out = bf.assign_ranks(rows, {"BK1": 2, "BK9": 1})
        by_code = {r["code"]: r for r in out}
        assert by_code["BK1"]["rank"] == 1 and by_code["BK1"]["prev_rank"] == 2
        assert by_code["BK2"]["prev_rank"] is None      # 新上榜不伪造数字

    def test_tie_broken_by_code(self):
        rows = [{"code": "BK9", "flow": 1.0}, {"code": "BK3", "flow": 1.0}]
        assert [r["code"] for r in bf.assign_ranks(rows)] == ["BK3", "BK9"]


class TestPrunePlan:
    def test_keeps_recent_and_returns_plan_only(self):
        dates = [(WED - dt.timedelta(days=d)).isoformat() for d in (0, 10, 31, 200)]
        plan = bf.prune_plan(WED, dates, dates, keep_points=30, keep_rank=120)
        assert plan["flow_points"] == [(WED - dt.timedelta(days=200)).isoformat(),
                                       (WED - dt.timedelta(days=31)).isoformat()]
        assert plan["rank_snapshots"] == [(WED - dt.timedelta(days=200)).isoformat()]

    def test_bad_dates_are_kept(self):
        plan = bf.prune_plan(WED, ["", "not-a-date", "2026-13-40"], ["2026-01-01"])
        assert plan["flow_points"] == []
        assert plan["rank_snapshots"] == ["2026-01-01"]

    def test_boundary_day_kept(self):
        d30 = (WED - dt.timedelta(days=30)).isoformat()
        assert bf.prune_plan(WED, [d30], [])["flow_points"] == []


class TestAssignRanksByType:
    def test_each_type_ranked_separately(self):
        rows = [
            {"code": "BK1", "type": "concept", "flow": 1.0},
            {"code": "BK2", "type": "concept", "flow": 9.0},
            {"code": "BK3", "type": "industry", "flow": 5.0},
            {"code": "BK4", "type": "industry", "flow": 7.0},
        ]
        out = {r["code"]: r for r in bf.assign_ranks_by_type(rows)}
        assert out["BK2"]["rank"] == 1 and out["BK1"]["rank"] == 2
        assert out["BK4"]["rank"] == 1 and out["BK3"]["rank"] == 2

    def test_missing_type_defaults_to_concept(self):
        out = bf.assign_ranks_by_type([{"code": "BK1", "flow": 1.0}])
        assert out[0]["rank"] == 1


class TestBuildSeries:
    def test_aligns_on_union_axis_with_forward_fill(self):
        pts = {
            "BK1": [("09:31", 1.0), ("09:33", 3.0)],
            "BK2": [("09:32", -2.0)],
        }
        out = bf.build_series(pts)
        assert out["axis"] == ["09:31", "09:32", "09:33"]
        s1 = next(s for s in out["series"] if s["code"] == "BK1")
        s2 = next(s for s in out["series"] if s["code"] == "BK2")
        assert s1["values"] == [1.0, 1.0, 3.0]
        assert s2["values"] == [None, -2.0, -2.0]
        assert s1["last"] == 3.0 and s2["last"] == -2.0

    def test_same_minute_last_point_wins(self):
        out = bf.build_series({"BK1": [("09:31", 1.0), ("09:31", 2.0)]})
        assert out["axis"] == ["09:31"]
        assert out["series"][0]["values"] == [2.0]

    def test_empty_input(self):
        assert bf.build_series({}) == {"axis": [], "series": []}
        assert bf.build_series({"BK1": []})["series"][0]["values"] == []


class TestBuildSeriesSessionTrim:
    def test_drops_off_session_times(self):
        pts = {"BK1": [("09:29", 1.0), ("09:30", 2.0), ("12:00", 9.0),
                       ("15:00", 3.0), ("15:06", 7.0), ("00:00", 5.0)]}
        out = bf.build_series(pts)
        assert out["axis"] == ["09:30", "15:00"]
        assert out["series"][0]["values"] == [2.0, 3.0]
        assert out["series"][0]["last"] == 3.0

    def test_all_off_session_yields_empty(self):
        assert bf.build_series({"BK1": [("15:06", 1.0)]})["axis"] == []


class TestAlignAgainst:
    def test_alignment_fills_missing_with_none(self):
        axis = ["09:31", "09:32", "09:33"]
        assert bf.align_against(axis, {"09:32": 5.0}) == [None, 5.0, None]

    def test_empty_axis(self):
        assert bf.align_against([], {"09:31": 1.0}) == []


class TestAggregateDaily:
    def test_sums_and_aligns_missing_days(self):
        dates = ["2026-11-01", "2026-11-02", "2026-11-03"]
        rows = [
            {"date": "2026-11-01", "code": "BK1", "name": "甲", "type": "concept", "flow": 1e8},
            {"date": "2026-11-03", "code": "BK1", "name": "甲", "type": "concept", "flow": -2e7},
            {"date": "2026-11-02", "code": "BK2", "name": "乙", "type": "concept", "flow": 5e7},
        ]
        out = bf.aggregate_daily(rows, dates)
        assert [i["code"] for i in out] == ["BK1", "BK2"]
        bk1 = out[0]
        assert bk1["total"] == 8e7 and bk1["days"] == 2
        assert bk1["values"] == [1e8, None, -2e7]
        assert out[1]["values"] == [None, 5e7, None]

    def test_bad_flow_skipped(self):
        out = bf.aggregate_daily([{"date": "2026-11-01", "code": "BK1", "flow": "x"}], ["2026-11-01"])
        assert out[0]["total"] == 0 and out[0]["days"] == 0

    def test_empty(self):
        assert bf.aggregate_daily([], ["2026-11-01"]) == []


class TestEmFlow:
    def test_parse_missing_fields_rejected(self):
        assert bf.parse_em_flow("1.000300", "index", None) is None
        assert bf.parse_em_flow("1.000300", "index", {"f140": "-"}) is None

    def test_parse_normalizes(self):
        row = bf.parse_em_flow("BK1036", "industry",
                               {"f58": "半导体", "f140": 3, "f143": 2, "f146": 1,
                                "f149": -4, "f170": 5.5})
        assert row == {"code": "BK1036", "kind": "industry", "name": "半导体",
                       "inst": 3.0, "large": 2.0, "medium": 1.0, "retail": -4.0,
                       "pct": 5.5}

    def test_pick_industries_by_abs_flow(self):
        rank = [
            {"code": "BK1", "name": "甲", "flow": 1e8},
            {"code": "BK2", "name": "乙", "flow": -5e8},
            {"code": "BK3", "name": "丙", "flow": 2e8},
            {"code": "000001", "name": "非板块", "flow": 9e9},
        ]
        picked = bf.pick_em_industries(rank, limit=2)
        assert [p["code"] for p in picked] == ["BK2", "BK3"]

    def test_pick_concepts_uses_same_abs_flow_rule(self):
        """概念标签与行业共用同一套挑选规则：只要 BK 板块、按 |主力净流入| 取前 N。"""
        rank = [
            {"code": "BK11", "name": "概念甲", "flow": 3e8},
            {"code": "BK12", "name": "概念乙", "flow": -8e8},
            {"code": "BK13", "name": "概念丙", "flow": 5e8},
            {"code": "000001", "name": "非板块", "flow": 9e9},
        ]
        picked = bf.pick_em_concepts(rank, limit=2)
        assert [p["code"] for p in picked] == ["BK12", "BK13"]
        assert picked[0]["name"] == "概念乙"
        assert bf.pick_em_concepts([], limit=5) == []


class TestPruneCoversAllTables:
    """ETF / 4 维 表也要按窗口裁剪（曾漏掉，库会无限增长）。

    用临时目录切换库文件，保持「单测不碰真实数据」的约定。
    """

    @pytest.fixture()
    def temp_db(self, tmp_path, monkeypatch):
        from services import boardflow as b
        monkeypatch.setattr(b, "_DATA_DIR", str(tmp_path))
        monkeypatch.setattr(b, "_DB_FILE", str(tmp_path / "board_flow.db"))
        b.init_db()
        return b

    def test_prune_removes_old_etf_and_em_dates(self, temp_db):
        b = temp_db
        old = "2020-01-02"
        with b._db() as conn:
            conn.execute("INSERT OR REPLACE INTO etf_flow_points(date,code,time,main) VALUES(?,?,?,?)",
                         (old, "510300", "09:31", 1.0))
            conn.execute("INSERT OR REPLACE INTO etf_rank_snapshots(date,code,name,type,flow) VALUES(?,?,?,?,?)",
                         (old, "510300", "沪深300ETF", "broad", 1.0))
            conn.execute("INSERT OR REPLACE INTO em_flow_points(date,code,name,kind,time,inst) VALUES(?,?,?,?,?,?)",
                         (old, "1.000300", "沪深300", "index", "09:31", 1.0))
        b.prune(dt.date(2026, 11, 4))
        with b._db() as conn:
            assert conn.execute("SELECT COUNT(*) FROM etf_flow_points WHERE date=?", (old,)).fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM etf_rank_snapshots WHERE date=?", (old,)).fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM em_flow_points WHERE date=?", (old,)).fetchone()[0] == 0

    def test_available_dates_per_table(self, temp_db):
        b = temp_db
        assert b.available_dates(5, "etf") == []
        assert b.available_dates(5, "em") == []
        assert b.available_dates(5, "不存在的表") == b.available_dates(5, "board")


class TestUpdateWatchAtomic:
    @pytest.fixture()
    def temp_db(self, tmp_path, monkeypatch):
        from services import boardflow as b
        monkeypatch.setattr(b, "_DATA_DIR", str(tmp_path))
        monkeypatch.setattr(b, "_DB_FILE", str(tmp_path / "board_flow.db"))
        monkeypatch.setattr(b, "_WATCH_FILE", str(tmp_path / "board_watch.json"))
        monkeypatch.setattr(b, "_ETF_WATCH_FILE", str(tmp_path / "etf_watch.json"))
        b.init_db()
        return b

    def test_add_and_remove_return_ok(self, temp_db):
        b = temp_db
        items, ok = b.update_watch({"code": "BK0001", "name": "甲"})
        assert ok and [i["code"] for i in items] == ["BK0001"]
        items, ok = b.update_watch(None, "BK0001")
        assert ok and items == []

    def test_etf_watch_roundtrip(self, temp_db):
        b = temp_db
        items, ok = b.update_etf_watch({"code": "510300", "name": "沪深300ETF"})
        assert ok and items[0]["code"] == "510300"
        items, ok = b.update_etf_watch(None, "510300")
        assert ok and items == []


class TestAggregateDailyPct:
    def test_pct_is_sum_over_range(self):
        rows = [
            {"date": "2026-11-01", "code": "BK1", "name": "甲", "type": "concept", "flow": 1.0, "pct": 1.5},
            {"date": "2026-11-02", "code": "BK1", "name": "甲", "type": "concept", "flow": 2.0, "pct": 3.2},
        ]
        out = bf.aggregate_daily(rows, ["2026-11-01", "2026-11-02"])
        assert out[0]["pct"] == 4.7      # 区间涨跌幅之和
        assert out[0]["total"] == 3.0


class TestDensifyAxis:
    def test_fills_missing_minutes_between_first_and_last(self):
        axis = bf.densify_axis(["09:58", "09:59", "10:01", "10:02"])
        assert axis == ["09:58", "09:59", "10:00", "10:01", "10:02"]

    def test_skips_lunch_break(self):
        axis = bf.densify_axis(["11:29", "13:01"])
        assert axis[-2:] == ["13:00", "13:01"]
        assert "12:00" not in axis

    def test_session_minutes_count(self):
        assert len(bf.session_minutes()) == 242

    def test_align_forward_fills(self):
        assert bf.align_to_axis([("09:58", 1.0), ("10:00", 3.0)], ["09:58", "09:59", "10:00"]) == [1.0, 1.0, 3.0]


class TestDailyFlowConsistency:
    """详情页「近 20 日」柱必须和「当日资金分时」末端一致（2026-11-04 实测对不上）。"""

    @pytest.fixture()
    def temp_db(self, tmp_path, monkeypatch):
        from services import boardflow as b
        monkeypatch.setattr(b, "_DATA_DIR", str(tmp_path))
        monkeypatch.setattr(b, "_DB_FILE", str(tmp_path / "board_flow.db"))
        b.init_db()
        return b

    @staticmethod
    def seed(b, date, rank_flow, ts, points):
        with b._db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO rank_snapshots(date,code,name,type,flow,pct,rank,ts)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (date, "BK0001", "甲板块", "concept", rank_flow, 1.0, 1, ts))
            for t, v in points:
                conn.execute("INSERT OR REPLACE INTO flow_points(date,code,time,flow) VALUES(?,?,?,?)",
                             (date, "BK0001", t, v))

    def test_post_close_snapshot_uses_last_point(self, temp_db):
        b = temp_db
        # 收盘后 19:08 又更新了一版（分时末端 15:00 是 192.54 亿）
        self.seed(b, "2026-11-04", 195.89e8, "2026-11-04 19:08",
                  [("14:59", 192.0e8), ("15:00", 192.54e8)])
        h = b.read_history("BK0001", 20)
        assert h[-1]["date"] == "2026-11-04"
        assert h[-1]["flow"] == pytest.approx(192.54e8)

    def test_intraday_snapshot_is_kept(self, temp_db):
        b = temp_db
        # 快照就是盘中/收盘时写的（15:00），无需覆盖
        self.seed(b, "2026-11-03", 111.0e8, "2026-11-03 15:00",
                  [("15:00", 100.0e8)])
        assert b.read_history("BK0001", 20)[-1]["flow"] == pytest.approx(111.0e8)

    def test_truncated_session_keeps_snapshot(self, temp_db):
        b = temp_db
        # 当天采集中断（最后一点 10:30），分时末端不代表全天 → 保留快照
        self.seed(b, "2026-11-04", 195.89e8, "2026-11-04 19:08",
                  [("10:29", 10.0e8), ("10:30", 12.0e8)])
        assert b.read_history("BK0001", 20)[-1]["flow"] == pytest.approx(195.89e8)

    def test_rank_range_and_daily_flow_share_rule(self, temp_db):
        b = temp_db
        self.seed(b, "2026-11-04", 195.89e8, "2026-11-04 19:08",
                  [("15:00", 192.54e8)])
        rows = b.read_rank_range("2026-11-04", "2026-11-04")
        assert rows[0]["flow"] == pytest.approx(192.54e8)
        assert b.daily_flow("BK0001", "2026-11-04", 195.89e8) == pytest.approx(192.54e8)

    def test_until_limits_history_window(self, temp_db):
        b = temp_db
        self.seed(b, "2026-11-03", 10.0e8, "2026-11-03 15:00", [("15:00", 10.0e8)])
        self.seed(b, "2026-11-04", 20.0e8, "2026-11-04 15:00", [("15:00", 20.0e8)])
        h = b.read_history("BK0001", 20, "2026-11-03")
        assert [x["date"] for x in h] == ["2026-11-03"]


class TestExtraEmTargets:
    """自选/热门里不在 Top40 的板块也要纳入 4 维采集。"""

    rank = [
        {"code": "BK0001", "name": "半导体", "type": "concept", "flow": 1e8},
        {"code": "BK0002", "name": "电力设备", "type": "industry", "flow": 2e8},
        {"code": "1.000001", "name": "上证指数", "type": "index", "flow": 3e8},
    ]

    def test_takes_watch_and_hot_with_kind_from_rank(self):
        out = bf.extra_em_targets(
            self.rank,
            [{"code": "BK0001", "name": "半导体"}, {"code": "1.000001", "name": "上证指数"}],
            [{"code": "BK0002", "name": "电力设备", "kind": "industry"}],
        )
        assert [(x["code"], x["kind"]) for x in out] == [
            ("BK0001", "concept"), ("BK0002", "industry"),
        ]

    def test_dedup_and_skip_non_bk(self):
        out = bf.extra_em_targets(
            self.rank,
            [{"code": "BK0001", "name": "半导体"}, {"code": "BK0001", "name": "半导体"}],
            [{"code": "BK0001", "name": "半导体"}, {"code": "1.000300", "name": "沪深300"}],
        )
        assert [x["code"] for x in out] == ["BK0001"]

    def test_name_falls_back_to_rank(self):
        out = bf.extra_em_targets(self.rank, [{"code": "BK0002"}], [])
        assert out == [{"code": "BK0002", "name": "电力设备", "kind": "industry"}]


class TestSpeed5:
    """涨速统一为「近 5 分钟涨跌幅」（东财 f22 是 3 分钟，不能用）。"""

    date = "2026-11-06"

    def test_speed5_uses_pct_five_minutes_ago(self):
        from services import board_collector as bc
        hist: dict = {}
        assert bc.speed5("BK1", -1.0, 1000.0, hist) is None      # 无 5 分钟前基准
        bc.speed5("BK1", -1.2, 1120.0, hist)
        bc.speed5("BK1", -1.4, 1240.0, hist)
        # 1300 - 300 = 1000：基准取 t=1000 的 -1.0 → -1.6 - (-1.0) = -0.6
        assert bc.speed5("BK1", -1.6, 1300.0, hist) == pytest.approx(-0.6)

    def test_speed5_ignores_stale_history(self):
        from services import board_collector as bc
        hist = {"BK1": [(0.0, 5.0)]}                           # 20 分钟前的旧记录被清掉
        assert bc.speed5("BK1", 1.0, 1200.0, hist) is None

    @pytest.fixture()
    def temp_db(self, tmp_path, monkeypatch):
        from services import boardflow as b
        monkeypatch.setattr(b, "_DATA_DIR", str(tmp_path))
        monkeypatch.setattr(b, "_DB_FILE", str(tmp_path / "board_flow.db"))
        b.init_db()
        return b

    def test_latest_em_flows_speed_from_pct(self, temp_db):
        b = temp_db
        with b._db() as conn:
            for t, pct in [("09:30", 0.0), ("09:35", 0.2), ("09:40", 0.9)]:
                conn.execute(
                    "INSERT OR REPLACE INTO em_flow_points"
                    "(date,code,name,kind,time,inst,large,medium,retail,pct)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (self.date, "1.000001", "上证指数", "index", t, 1, 2, 3, 4, pct))
        rows = b.latest_em_flows(self.date)
        # 5 分钟前（09:35）0.2 → 最新 0.9 = +0.7
        assert rows[0]["speed"] == pytest.approx(0.7)


class TestDensifySeries:
    def test_sparse_axis_is_filled_forward(self):
        axis = ["09:30", "09:32", "09:34"]
        values = [1.0, 3.0, None]
        dense_axis, dense_vals = bf.densify_series(axis, values)
        assert dense_axis == ["09:30", "09:31", "09:32", "09:33", "09:34"]
        # 缺失分钟沿用前值；末尾未采到的点也沿用最后已知值
        assert dense_vals == [1.0, 1.0, 3.0, 3.0, 3.0]

    def test_dense_axis_is_unchanged(self):
        axis = ["09:30", "09:31", "09:32"]
        values = [1.0, 2.0, 3.0]
        assert bf.densify_series(axis, values) == (axis, values)

    def test_empty(self):
        assert bf.densify_series([], []) == ([], [])
