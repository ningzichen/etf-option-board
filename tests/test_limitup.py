"""涨停棱镜的纯函数单测（不触网、不读系统时钟、不写磁盘）。

覆盖：主题等级分档、扩张/高潮/三共振判定、周期表矩阵、概念噪音过滤、
按概念聚合涨停数（含连板/首板/炸板/烂板）。
"""

from __future__ import annotations

from services import limitup as lu


def test_tier_of_boundaries():
    assert lu.tier_of(0) == "0~3"
    assert lu.tier_of(2) == "0~3"
    assert lu.tier_of(3) == "3~5"
    assert lu.tier_of(5) == "5~10"
    assert lu.tier_of(10) == "10~15"
    assert lu.tier_of(15) == "15以上"
    assert lu.tier_of(99) == "15以上"


def test_is_noise_filters_geo_and_financial():
    assert lu._is_noise("福建板块")
    assert lu._is_noise("题材股")
    assert lu._is_noise("2026中报首亏")
    assert lu._is_noise("信托重仓")
    assert lu._is_noise("昨日涨停")
    # 真正的概念要留下
    assert not lu._is_noise("光伏概念")
    assert not lu._is_noise("人形机器人")
    assert not lu._is_noise("次新股")


def test_classify_theme():
    assert lu.classify_theme("高股息精选") == "风格性"
    assert lu.classify_theme("业绩预增") == "事件性"
    assert lu.classify_theme("光伏概念") == "产业链"


def test_is_expanding():
    # 前 5 日均值 2，当日 4 > 2*1.5=3 → 扩张
    assert lu.is_expanding([2, 2, 2, 2, 2, 4])
    # 当日 3 不大于 3 → 不算
    assert not lu.is_expanding([2, 2, 2, 2, 2, 3])
    # 没有历史可比 → 不判定为扩张
    assert not lu.is_expanding([1])
    assert not lu.is_expanding([])


def test_is_climax():
    assert lu.is_climax([1, 2, 3, 4, 5])
    assert not lu.is_climax([1, 5, 3, 4, 2])
    assert not lu.is_climax([1, 2])              # 不足 3 家
    assert lu.is_climax([2, 2, 3])


def test_is_resonance():
    # 三项都高于各自前 5 日均值
    assert lu.is_resonance([1, 1, 1, 2], [1, 1, 1, 2], [1, 1, 1, 2])
    # 炸板没放大 → 不算
    assert not lu.is_resonance([1, 1, 1, 2], [1, 1, 1, 2], [3, 3, 3, 1])
    # 单项为 0 → 不算
    assert not lu.is_resonance([1, 1, 1, 2], [0, 0, 0, 0], [1, 1, 1, 2])


def test_build_matrix_pads_and_ranks():
    per_day = [
        {"date": "20261105", "themes": [
            {"board": "A", "name": "甲", "zt": 2, "max_lbc": 1},
            {"board": "B", "name": "乙", "zt": 5, "max_lbc": 2},
        ]},
        {"date": "20261106", "themes": []},
    ]
    cols = lu.build_matrix(per_day, top=3)
    assert len(cols) == 2
    assert [c["name"] if c else None for c in cols[0]] == ["乙", "甲", None]
    assert cols[1] == [None, None, None]


def test_aggregate_themes_counts():
    pool = [
        {"code": "000001", "name": "甲", "lbc": 3},
        {"code": "000002", "name": "乙", "lbc": 1},
        {"code": "000003", "name": "丙", "lbc": 2},
    ]
    concepts = {
        "000001": [{"name": "光伏概念", "rank": 1}],
        "000002": [{"name": "光伏概念", "rank": 2}],
        "000003": [{"name": "光伏概念", "rank": 3}, {"name": "机器人", "rank": 4}],
    }
    broken = [{"code": "000003", "name": "丙", "zbc": 3}]
    boards = {"光伏概念": {"code": "BK1031", "pct": 1.2, "main": 1e8},
              "机器人": {"code": "BK0800", "pct": -0.5, "main": -2e7}}

    out = lu.aggregate_themes(pool, concepts, broken, boards)
    pv = next(t for t in out if t["name"] == "光伏概念")
    assert pv["zt"] == 3
    assert pv["max_lbc"] == 3
    assert pv["lianban"] == 2      # lbc>=2 的两只
    assert pv["first_board"] == 1
    assert pv["zb"] == 1 and pv["lan"] == 1
    assert pv["board"] == "BK1031"
    assert pv["pct"] == 1.2 and pv["main"] == 1e8
    # 排序：涨停数多的在前
    assert out[0]["name"] == "光伏概念"


def test_composite_rank_prefers_recent_and_normalizes():
    # 同期总量相同，但 A 集中在最近一天 → 综合排名更高
    heat = lu.composite_rank({
        "A": [0, 0, 0, 10],
        "B": [10, 0, 0, 0],
        "C": [5, 5, 5, 5],
    })
    assert heat["A"] > heat["B"]
    # 最强的一个恒为 1.0，且全部落在 0~1
    assert max(heat.values()) == 1.0
    assert all(0.0 <= v <= 1.0 for v in heat.values())
    # 空输入不炸
    assert lu.composite_rank({}) == {}


def test_composite_rank_keeps_colour_spread():
    """130+ 个概念时，前几名不能全挤在 0.95 以上（否则颜色看不出区别）。"""
    series = {f"t{i}": [max(0, 20 - i)] for i in range(60)}
    heat = lu.composite_rank(series)
    top = sorted(heat.values(), reverse=True)
    assert top[0] == 1.0
    assert top[4] < 0.9          # 第 5 名明显淡于第 1 名
    assert top[-1] < 0.1


def test_aggregate_themes_keeps_zt_only_theme():
    """只有涨停没有炸板的主题也要出现在榜里（zb/lan 为 0）。"""
    out = lu.aggregate_themes(
        [{"code": "1", "name": "甲", "lbc": 1}],
        {"1": [{"name": "冷门概念", "rank": 1}]},
        [],
        {},
    )
    assert out[0]["name"] == "冷门概念"
    assert out[0]["zt"] == 1 and out[0]["zb"] == 0 and out[0]["lan"] == 0
    assert out[0]["board"] is None      # 名单里没有的板块，代码缺失也能落库


def test_top_concepts_keeps_one_per_stock():
    """炒作口径：每只票只留 BOARD_RANK 最靠前的主概念。"""
    concepts = {
        "000001": [{"name": "甲概念", "rank": 5}, {"name": "乙概念", "rank": 2}],
        "000002": [{"name": "甲概念", "rank": 3}],
        "000003": [],
    }
    out = lu.top_concepts(concepts)
    assert out["000001"] == [{"name": "乙概念", "rank": 2}]
    assert out["000002"] == [{"name": "甲概念", "rank": 3}]
    assert "000003" not in out          # 没有任何概念的票不参与


def test_board_level_threshold():
    """一级/二级主题：成分股数 ≥ LEVEL_PRIMARY_MIN 算一级，未知（缺元数据）不丢。"""
    levels = {"BK1": "primary", "BK2": "secondary"}
    assert lu._keep_level("BK1", "primary", levels)
    assert not lu._keep_level("BK1", "secondary", levels)
    assert lu._keep_level("BK2", "secondary", levels)
    # 没有元数据的板块两个等级都保留，别因为缺数据就丢主题
    assert lu._keep_level("BK9", "primary", levels)
    assert lu._keep_level("BK9", "secondary", levels)
    assert lu._keep_level("BK1", "all", levels)


def test_mode_key():
    assert lu._mode_key("zt", "hot") == "zt_hot"
    assert lu._mode_key("ext", "hot") == "ext_hot"
    assert lu._mode_key("zt", "board") == "zt"
    assert lu._mode_key("ext", "pct") == "ext"
    assert lu._mode_key("bad", "board") == "zt"


def test_rule_brief_uses_real_numbers():
    themes = [{"name": "光伏概念", "zt": 9, "stocks": [
        {"reason": "光伏边框+液冷铝材"}, {"reason": "光伏边框+储能"}]}]
    out = lu._rule_brief(themes, 33)
    assert "33" in out["summary"]
    assert out["headline"].startswith("光伏概念")
    assert "光伏边框" in out["themes"]["光伏概念"]


def test_clean_reason_strips_disclaimer():
    """同花顺 reason_info 末尾的免责声明要剥掉（弹窗底部统一说一次）。"""
    raw = ("1、公司主营光伏边框。\n\n"
           "（免责声明：本内容由AI技术搜集公开信息总结生成，仅供参考，不构成投资建议，以上市公司公告为准）")
    assert lu._clean_reason(raw) == "1、公司主营光伏边框。"
    # 半角括号也要能剥
    assert lu._clean_reason("原因(免责声明：xx)") == "原因"
    # 正常文本与空值不动
    assert lu._clean_reason("光伏边框+液冷铝材") == "光伏边框+液冷铝材"
    assert lu._clean_reason(None) == ""
    assert lu._clean_reason("") == ""
