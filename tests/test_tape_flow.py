"""全A逐笔分档的纯函数单测（不触网、不读时钟、不写盘）。"""

from services import tape_flow as tf


def test_minute_of_clamps_to_session():
    assert tf.minute_of("09:15:00") == "09:30"      # 集合竞价并到开盘
    assert tf.minute_of("09:30:00") == "09:30"
    assert tf.minute_of("10:03:59") == "10:03"
    assert tf.minute_of("11:45:00") == "11:30"      # 午休并入上午收盘
    assert tf.minute_of("12:59:00") == "11:30"
    assert tf.minute_of("14:59:00") == "14:59"
    assert tf.minute_of("15:19:58") == "15:00"      # 盘后固定价格并入收盘
    assert tf.minute_of("") is None
    assert tf.minute_of("abc") is None


def test_parse_tick_direction_and_amount():
    # 东财字段：time,price,volume(手),笔数,direction；"2"=主动买、"1"=主动卖、"4"=集合竞价虚拟撮合
    assert tf.parse_tick("10:00:00,10.00,3,2,2") == ("10:00", 3000.0, 3.0, 1, False)
    assert tf.parse_tick("10:00:00,10.00,3,2,1") == ("10:00", 3000.0, 3.0, -1, False)
    # 09:25 的竞价成交方向仍是 1/2，只是要标出来（通达信口径整段算买）
    assert tf.parse_tick("09:25:01,10.00,50,4,2") == ("09:30", 50000.0, 50.0, 1, True)
    assert tf.parse_tick("09:15:00,10.00,100,0,4") is None
    assert tf.parse_tick("10:00:00,10.00,0,0,2") is None
    assert tf.parse_tick("garbage") is None


def test_bin_of_boundaries():
    assert tf.bin_of(0) == 0
    assert tf.bin_of(0.9) == 0                       # 0.9 万 < 第一档 1 万
    assert tf.bin_of(1) == 1                         # 正好 1 万
    assert tf.bin_of(20) == tf.CUTS.index(20) + 1
    assert tf.bin_of(50000) == len(tf.CUTS)          # 超过最大档并入末档
    # 手数档（通达信口径）
    assert tf.bin_of(699, tf.LOTS) == tf.LOTS.index(700)
    assert tf.bin_of(700, tf.LOTS) == tf.LOTS.index(700) + 1


def test_norm_thresholds_sorts_and_appends_zero():
    assert tf.norm_thresholds([50, 200, 200, "x", -3]) == [200.0, 50.0, 0.0]
    assert tf.norm_thresholds([]) == [0.0]


def test_norm_thresholds_snap_to_bin_edges():
    # 140 万吸附到 150 万档（档位表里没有 140），50 万本身就在边界上
    assert tf.norm_thresholds([140, 50]) == [150.0, 50.0, 0.0]
    assert tf.snap_threshold(0) == 0.0
    assert tf.snap_threshold(99999) == float(tf.CUTS[-1])
    # 手数档：130 手吸附到 150 手
    assert tf.norm_thresholds([130, 50], tf.LOTS) == [150.0, 50.0, 0.0]


def test_bucket_of_bin():
    ths = [200.0, 50.0, 0.0]
    assert tf.bucket_of_bin(tf.bin_of(300), ths) == 0            # 300 万 → 主力
    assert tf.bucket_of_bin(tf.bin_of(100), ths) == 1            # 100 万 → 跟风
    assert tf.bucket_of_bin(tf.bin_of(10), ths) == 2             # 10 万 → 散户
    assert tf.bucket_of_bin(0, ths) == 2                         # 最小档也是散户


def test_summarize_buy_sell_net_and_share():
    rows = [
        ("09:31", tf.bin_of(300), 300.0, 100.0),         # 300 万 → 主力
        ("09:32", tf.bin_of(100), 50.0, 70.0),           # 100 万 → 跟风
        ("09:33", tf.bin_of(10), 10.0, 30.0),            # 10 万 → 散户
    ]
    out = tf.summarize(rows, [200, 50])
    assert [round(x["net"], 1) for x in out] == [200.0, -20.0, -20.0]
    assert [round(x["buy"], 1) for x in out] == [300.0, 50.0, 10.0]
    assert abs(sum(x["share"] for x in out) - 1.0) < 1e-9


def test_cumulative_carries_forward():
    axis = ["09:30", "09:31", "09:32"]
    rows = [("09:31", tf.bin_of(300), 100.0, 0.0)]
    out = tf.cumulative(rows, [200, 50], axis)
    assert [row[0] for row in out] == [0.0, 100.0, 100.0]   # 缺分钟用前值
    assert out[-1][2] == 0.0


def test_session_axis_length():
    axis = tf.session_axis()
    assert len(axis) == 242                       # 09:30–11:30 与 13:00–15:00 各 121 点
    assert axis[0] == "09:30" and axis[120] == "11:30"
    assert axis[121] == "13:00" and axis[-1] == "15:00"


def test_coverage_and_amount_checks():
    assert tf.coverage_ok(950, 1000) is True
    assert tf.coverage_ok(900, 1000) is True
    assert tf.coverage_ok(899, 1000) is False
    assert tf.coverage_ok(0, 0) is False
    assert tf.amount_ok(100.0, 101.0) is True
    assert tf.amount_ok(100.0, 110.0) is False
    assert tf.amount_ok(100.0, 0.0) is False


def test_presets_thresholds_are_descending():
    for spec in tf.PRESETS.values():
        ths = [b["thr"] for b in spec["buckets"]]
        assert ths == sorted(ths, reverse=True)
        assert ths[-1] == 0
        assert spec["dim"] in tf.DIMS


def test_presets_use_expected_dimension():
    # 通达信按手数、Wind 按金额；竞价处理也不同（这是实测出来的口径，别随手改）
    assert tf.PRESETS["tdx"]["dim"] == "lot"
    assert tf.PRESETS["tdx"]["auction"] == "buy"
    assert tf.PRESETS["wind"]["dim"] == "amt"
    assert tf.PRESETS["wind"]["auction"] == "drop_open"
    assert tf.PRESETS["tdx"]["buckets"][0]["thr"] == 700
    assert tf.PRESETS["wind"]["buckets"][0]["thr"] == 400     # 拟合 Wind 截图：机构 ≥400 万


def test_align_price_filters_date_and_carries_forward():
    axis = ["09:30", "09:31", "09:32", "09:33"]
    pts = [
        {"time": "2026-11-06 09:31", "price": 10.5},
        {"time": "2026-11-05 09:31", "price": 99.0},   # 别的交易日要丢掉
        {"time": "2026-11-06 09:33", "price": 11.0},
    ]
    assert tf.align_price(axis, pts, "2026-11-06") == [None, 10.5, 10.5, 11.0]
    assert tf.align_price([], pts, "2026-11-06") == []


def test_wind_main_tick_thresholds():
    # ≥70 万才计入；集合竞价整段不计
    assert tf.wind_main_tick(699_999, 1, False) == 0.0
    assert tf.wind_main_tick(700_000, 1, False) == 700_000.0
    assert tf.wind_main_tick(4_000_000, -1, False) == -4_000_000.0
    assert tf.wind_main_tick(10_000_000, 1, True) == 0.0      # 竞价剔除
    assert tf.wind_main_tick(0, 1, False) == 0.0
