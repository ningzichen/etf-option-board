"""板块成分股聚合的纯函数单测（不触网）。"""

from services import board_map


def test_aggregate_sums_members_and_ignores_missing():
    stock_net = {"600519": 100.0, "000001": -40.0, "300750": 25.0}
    boards = {
        "BK0001": ["600519", "000001"],      # 60
        "BK0002": ["300750", "999999"],      # 25（999999 没数据，跳过）
        "BK0003": ["888888"],                # 一个都没命中 → 不产出
    }
    out = board_map.aggregate(stock_net, boards)
    assert out == {"BK0001": 60.0, "BK0002": 25.0}


def test_aggregate_empty_inputs():
    assert board_map.aggregate({}, {"BK1": ["1"]}) == {}
    assert board_map.aggregate({"1": 1.0}, {}) == {}
