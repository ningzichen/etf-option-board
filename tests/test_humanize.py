"""humanize 纯函数测试：方向快照 → 人话结论（简洁模式的唯一翻译层）。

约束：只喂输入、断言输出 —— 不触网、不读系统时钟、不写磁盘。
时钟一律以参数注入（session_label / compose 的 now），与仓库测试约定一致。
"""

import datetime as dt

import pytest

from services import humanize as hz

NOW_PM = dt.datetime(2026, 11, 9, 14, 0)          # 周一盘中
NOW_LATE = dt.datetime(2026, 11, 9, 14, 50)       # 尾盘
NOW_WEEKEND = dt.datetime(2026, 11, 7, 10, 0)     # 周六

ETFS = [
    {"code": "510300", "name": "沪深300ETF华泰柏瑞"},
    {"code": "588000", "name": "科创50ETF华夏"},
    {"code": "159915", "name": "创业板ETF易方达"},
]


def _d(score, detail=None, direction=None):
    d = score if direction is None else (
        "call" if score >= 15 else "put" if score <= -15 else "neutral")
    return {"score": round(score, 1), "raw_score": float(score),
            "direction": d, "detail": detail or {}}


# ---------------------------------------------------------------------------
# verdict：阈值先判、强度只说一致度
# ---------------------------------------------------------------------------

class TestVerdict:

    @pytest.mark.parametrize("score,tone", [
        (14.9, "neutral"), (-14.9, "neutral"), (0, "neutral"),
        (15, "bull"), (-15, "bear"), (15.1, "bull"),
    ])
    def test_threshold_decides_before_rounding(self, score, tone):
        v = hz.verdict(score)
        assert v["tone"] == tone

    @pytest.mark.parametrize("score,strength", [
        (15, "weak"), (29.9, "weak"),
        (30, "medium"), (59.9, "medium"),
        (60, "strong"), (88, "strong"),
    ])
    def test_strength_tiers(self, score, strength):
        assert hz.verdict(score)["strength"] == strength

    def test_neutral_has_no_strength(self):
        v = hz.verdict(3)
        assert v["strength"] is None
        assert v["strength_label"] == ""
        assert v["label"] == "看不清"

    def test_labels(self):
        assert hz.verdict(40)["label"] == "偏多"
        assert hz.verdict(-40)["label"] == "偏空"

    @pytest.mark.parametrize("bad", [None, "abc", float("nan"), float("inf")])
    def test_non_numeric_degrades_to_neutral(self, bad):
        assert hz.verdict(bad)["tone"] == "neutral"

    def test_clamped_to_hundred(self):
        assert hz.verdict(180)["score"] == 100.0
        assert hz.verdict(-180)["score"] == -100.0

    def test_bar_pct_for_ui(self):
        assert hz.verdict(-47.2)["bar_pct"] == 47.2


# ---------------------------------------------------------------------------
# reasons：只取同向、贡献最大的两项
# ---------------------------------------------------------------------------

class TestReasons:

    def test_takes_top_two_aligned(self):
        detail = {"标的5分钟动量": 12.0, "价格vs均价": 6.5, "外围市场": -3.0,
                  "指数关键位": 9.0}
        rs = hz.reasons(detail, "bull")
        assert len(rs) == 2
        assert rs[0]["name"] == "标的5分钟动量"
        assert rs[1]["name"] == "指数关键位"

    def test_excludes_opposite_sign(self):
        rs = hz.reasons({"标的5分钟动量": -12.0, "价格vs均价": 4.0}, "bull")
        assert [r["name"] for r in rs] == ["价格vs均价"]

    def test_unknown_factor_falls_back_to_name(self):
        rs = hz.reasons({"新因子": 8.0}, "bull")
        assert rs and "新因子" in rs[0]["text"]

    def test_neutral_returns_fixed_note(self):
        rs = hz.reasons({"标的5分钟动量": 9.0}, "neutral")
        assert len(rs) == 1
        assert "抵消" in rs[0]["text"]

    def test_empty_detail(self):
        assert hz.reasons({}, "bull") == []
        assert hz.reasons(None, "bear") == []

    def test_limit_respected(self):
        detail = {"标的5分钟动量": 12.0, "价格vs均价": 6.0, "外围市场": 4.0}
        assert len(hz.reasons(detail, "bull", limit=1)) == 1


# ---------------------------------------------------------------------------
# breakeven_pct：回本门槛（价差 + 手续费折算成标的需走多少）
# ---------------------------------------------------------------------------

class TestBreakeven:

    def test_basic(self):
        # 成本口径 = 一次完整价差 + 往返手续费，再按有效杠杆折算成标的需走的百分比：
        # spread 1.0% + 手续费 (6 元/张 ÷ 10000 份) ÷ 0.06 元/份 ≈ 1.0%
        # → 2.0% ÷ 杠杆 20 ≈ 0.10%
        pick = {"spread": 1.0, "leverage": 20.0, "last": 0.06}
        be = hz.breakeven_pct(pick)
        assert be == pytest.approx(0.10, abs=0.005)

    def test_missing_fields(self):
        assert hz.breakeven_pct({"spread": None, "leverage": 20, "last": 0.1}) is None
        assert hz.breakeven_pct({"spread": 1.0, "leverage": 0, "last": 0.1}) is None
        assert hz.breakeven_pct({}) is None

    def test_zero_premium_guard(self):
        assert hz.breakeven_pct({"spread": 1.0, "leverage": 5, "last": 0}) is None

    def test_wider_spread_costs_more(self):
        a = hz.breakeven_pct({"spread": 0.5, "leverage": 20, "last": 0.1})
        b = hz.breakeven_pct({"spread": 2.0, "leverage": 20, "last": 0.1})
        assert b > a


# ---------------------------------------------------------------------------
# session_label：不读系统时钟，注入 now
# ---------------------------------------------------------------------------

class TestSession:

    @pytest.mark.parametrize("now,label", [
        (dt.datetime(2026, 11, 9, 9, 0), "未开盘"),
        (dt.datetime(2026, 11, 9, 9, 30), "盘中"),
        (dt.datetime(2026, 11, 9, 11, 30), "盘中"),
        (dt.datetime(2026, 11, 9, 11, 31), "午休"),
        (dt.datetime(2026, 11, 9, 12, 59), "午休"),
        (dt.datetime(2026, 11, 9, 13, 0), "盘中"),
        (dt.datetime(2026, 11, 9, 15, 0), "盘中"),
        (dt.datetime(2026, 11, 9, 15, 1), "已收盘"),
        (NOW_WEEKEND, "休市"),
    ])
    def test_labels(self, now, label):
        assert hz.session_label(now)["label"] == label

    def test_late_session_flag(self):
        assert hz.session_label(NOW_LATE)["late"] is True
        assert hz.session_label(NOW_PM)["late"] is False


# ---------------------------------------------------------------------------
# risk_notes：临期 / 尾盘 / 严重告警 / 全线观望
# ---------------------------------------------------------------------------

class TestNotes:

    def test_expiry_critical(self):
        notes = hz.risk_notes(expiry=[{"month": 11, "days_left": 2}], alerts=[],
                              now=NOW_PM, all_neutral=False)
        assert any("到期" in n for n in notes)

    def test_expiry_far_no_note(self):
        notes = hz.risk_notes(expiry=[{"month": 12, "days_left": 30}], alerts=[],
                              now=NOW_PM, all_neutral=False)
        assert notes == []

    def test_late_session_note(self):
        notes = hz.risk_notes(expiry=[], alerts=[], now=NOW_LATE, all_neutral=False)
        assert any("尾盘" in n for n in notes)

    def test_recent_critical_alert_only(self):
        alerts = [
            {"level": "critical", "title": "主力流入", "ts": "2026-11-09T13:50:00"},
            {"level": "warn", "title": "量能放大", "ts": "2026-11-09T13:55:00"},
            {"level": "critical", "title": "早盘旧告警", "ts": "2026-11-09T09:40:00"},
        ]
        notes = hz.risk_notes(expiry=[], alerts=alerts, now=NOW_PM, all_neutral=False)
        assert any("主力流入" in n for n in notes)
        assert not any("早盘旧告警" in n for n in notes)

    def test_all_neutral_note(self):
        notes = hz.risk_notes(expiry=[], alerts=[], now=NOW_PM, all_neutral=True)
        assert any("多看少动" in n for n in notes)


# ---------------------------------------------------------------------------
# compose：整包聚合（纯函数核心）
# ---------------------------------------------------------------------------

class TestCompose:

    def _dirs(self):
        return {
            "510300": _d(46.3, {"标的5分钟动量": 12.5, "关联指数": 9.0,
                                "价格vs均价": 5.2, "外围市场": -3.0}),
            "588000": _d(6.4, {"标的5分钟动量": 3.0, "外围市场": -2.0}),
            "159915": _d(-52.0, {"变盘点·15m结构转向": -15.0, "关联指数": -18.0}),
            "MO": _d(-60.0, {"指数关键位": -10.0}),        # 股指期权：不进简洁模式
        }

    def _picks(self):
        return {"items": [
            {"code": "10007891", "name": "300ETF购12月4000", "etf_code": "510300",
             "etf_name": "沪深300ETF华泰柏瑞", "side": "call", "type": "call",
             "strike": 4.0, "last": 0.0612, "delta": 0.48, "leverage": 19.8,
             "spread": 1.1, "pick_score": 72.0, "month": 12, "days": 33},
            {"code": "10007892", "name": "500ETF沽12月6000", "etf_code": "510500",
             "etf_name": "中证500ETF南方", "side": "put", "type": "put",
             "strike": 6.0, "last": 0.09, "delta": -0.45, "leverage": 15.0,
             "spread": 1.6, "pick_score": 55.0, "month": 12, "days": 33},
        ]}

    def _compose(self, **kw):
        args = dict(directions=self._dirs(), etf_meta=ETFS, picks=self._picks(),
                    expiry=[], alerts=[], now=NOW_PM)
        args.update(kw)
        return hz.compose(**args)

    def test_items_only_cover_tracked_etfs(self):
        out = self._compose()
        codes = {i["code"] for i in out["items"]}
        assert codes == {"510300", "588000", "159915"}
        assert "MO" not in codes

    def test_items_sorted_by_conviction(self):
        out = self._compose()
        assert [i["code"] for i in out["items"]] == ["159915", "510300", "588000"]

    def test_item_carries_label_and_reasons(self):
        out = self._compose()
        item = next(i for i in out["items"] if i["code"] == "510300")
        assert item["label"] == "偏多"
        assert item["strength_label"] == "中"
        assert item["reasons"] and item["reasons"][0]["name"] == "标的5分钟动量"

    def test_action_picks_top_aligned_contract(self):
        out = self._compose()
        assert out["action"] is not None
        assert out["action"]["etf_code"] == "510300"
        assert out["action"]["side_label"] == "认购"
        # 价差 1.1% + 手续费约 1.0% → 2.1% ÷ 19.8 ≈ 0.11%
        assert out["action"]["breakeven_pct"] == pytest.approx(0.11, abs=0.01)

    def test_action_none_when_all_neutral(self):
        dirs = {"510300": _d(3.0), "588000": _d(-2.0), "159915": _d(0.0)}
        out = self._compose(directions=dirs)
        assert out["action"] is None
        assert any("多看少动" in n for n in out["notes"])

    def test_action_prefers_aligned_side(self):
        # 最强的标的是看空，但优选列表第一条是认购（不同标的）→ 不能拿来当动作
        out = self._compose()
        assert out["action"]["side"] == "call"      # 159915 看空但没有对应合约，退到 510300
        assert out["action"]["etf_code"] == "510300"

    def test_session_and_ts(self):
        out = self._compose()
        assert out["session"]["label"] == "盘中"
        assert out["ts"] == "14:00"
        assert out["threshold"] == 15

    def test_empty_directions_still_valid(self):
        out = self._compose(directions={})
        assert out["items"] and all(i["tone"] == "neutral" for i in out["items"])
        assert out["action"] is None


# ---------------------------------------------------------------------------
# 回归（review P1）：动作卡必须复用专业模式的选合约规则
# 口径来源 services/decision.select_strategy：临期排除当月、高 IV 改价差
# ---------------------------------------------------------------------------

class TestActionFollowsStrategyRules:
    """简洁模式不能给出与专业模式冲突的建议。"""

    DIRS = {"510300": _d(46.3, {"标的5分钟动量": 12.5, "关联指数": 9.0}),
            "588000": _d(6.4), "159915": _d(-52.0, {"关联指数": -18.0})}

    FRONT_ONLY = [{"code": "10007890", "name": "300ETF购11月4000", "etf_code": "510300",
                   "etf_name": "沪深300ETF华泰柏瑞", "side": "call", "type": "call",
                   "strike": 4.0, "last": 0.041, "delta": 0.42, "leverage": 21.0,
                   "spread": 1.4, "pick_score": 78.0, "month": 11, "days": 2}]
    NEXT_ONLY = [{"code": "10007891", "name": "300ETF购12月4000", "etf_code": "510300",
                  "etf_name": "沪深300ETF华泰柏瑞", "side": "call", "type": "call",
                  "strike": 4.0, "last": 0.0612, "delta": 0.48, "leverage": 19.8,
                  "spread": 1.1, "pick_score": 72.0, "month": 12, "days": 30}]

    def _compose(self, *, expiry, items, iv_pct=None):
        picks = {"items": [{**p, "iv_pct": iv_pct} for p in items]}
        return hz.compose(directions=self.DIRS, etf_meta=ETFS, picks=picks,
                          expiry=expiry, alerts=[], now=NOW_PM)

    def test_critical_front_month_is_excluded(self):
        # 当月只剩 2 天 → 专业模式排除新开当月；当月合约哪怕综合分更高也不能当动作
        out = self._compose(
            expiry=[{"month": 11, "days_left": 2}, {"month": 12, "days_left": 30}],
            items=self.FRONT_ONLY + self.NEXT_ONLY, iv_pct=50.0)
        assert out["action"] is not None
        assert out["action"]["month"] == 12
        assert out["action"]["code"] == "10007891"

    def test_critical_front_month_without_next_month_waits(self):
        # 只有当月可用且临期 → 专业模式明确观望，简洁模式不能给动作
        out = self._compose(expiry=[{"month": 11, "days_left": 2}],
                            items=self.FRONT_ONLY, iv_pct=50.0)
        assert out["action"] is None
        assert out["action_note"] and "临期" in out["action_note"]

    def test_high_iv_does_not_recommend_single_leg(self):
        # IV 分位 ≥75：专业模式改垂直价差，简洁模式不得把单腿包装成「今天只做这一笔」
        out = self._compose(expiry=[{"month": 12, "days_left": 30}],
                            items=self.NEXT_ONLY, iv_pct=82.0)
        assert out["action"] is None
        assert out["action_note"] and "价差" in out["action_note"]

    def test_low_iv_keeps_single_leg(self):
        out = self._compose(expiry=[{"month": 12, "days_left": 30}],
                            items=self.NEXT_ONLY, iv_pct=18.0)
        assert out["action"] is not None
        assert out["action"]["month"] == 12
        assert out["action"]["strategy"] == "single_call"

    def test_missing_iv_percentile_degrades_to_single_leg(self):
        # IV 分位样本不足：专业模式按中性处理（不改变策略类型）
        out = self._compose(expiry=[{"month": 12, "days_left": 30}],
                            items=self.NEXT_ONLY, iv_pct=None)
        assert out["action"] is not None
        assert out["action"]["strategy"] == "single_call"

    def test_action_carries_strategy_label(self):
        out = self._compose(expiry=[{"month": 12, "days_left": 30}],
                            items=self.NEXT_ONLY, iv_pct=50.0)
        assert out["action"]["strategy_label"]
        assert out["action_note"] is None
