# AGENTS.md — ETF 期权日内决策看板

> **2026-11-01 源码回滚事故已恢复**（另一个会话把 10-30 之后的所有改动 `git checkout` 掉并删了
> `services/index_option.py`）。恢复过程与规格见 `.scratch/restore-2026-11-01.md`。
> 教训：**同一工作区不要并行开两个会话说改代码**，会互相覆盖；需要并行时用 `enter_worktree` 隔离。

## 项目概览

A 股 6 只可交易期权的宽基 ETF 的**日内决策看板**。后端 FastAPI（Python）聚合东财行情/期权链/
资金流/指数/外围，内置 BS 定价、Kronos 预测桥、大模型决策、实时告警；前端 REUI + React +
Vite + Tailwind v4，浅色看板（`.dark` 变量仍在但未启用，2026-10-13 起改浅色），SSE 弹窗+声音告警。

左侧是**一级导航**（`web/src/components/SideNav.tsx`），当前 6 组：**期权看板**、**资金分时**、
**资金流向**、**汪汪队ETF**、**风险监控**、**涨停棱镜**；
**顶栏不再放页面标签**（顶点要留给外围行情/评分/时钟）。hash 路由 `#/options` 与 `#/board/*`
不变，所以旧链接/书签照旧可用。资金分时内含从 stock-webui 移植的**板块资金四页**
（主力资金分时 / 主力资金多日 / ETF资金流 / 四维资金），四页切换放在页内子标签
`BoardSubNav.tsx` 里（**水平居中**于内容区），共用 `services/boardflow.py` 的本地 SQLite。规格见 `.scratch/board-flow/spec.md`、
取舍见 `docs/adr/0003-板块资金单写路径与本地缓存.md`。
四页的「实时 / 历史」标签统一由 `web/src/components/DayTag.tsx` 给出（所选日期 = 库里最新
交易日 → 实时，否则历史）；左侧列表统一用 `web/src/components/FlowListPanel.tsx`。

## 关键命令

```bash
bash run.sh           # 启动后端 :6799（含监控循环 + Kronos 预热，托管 web/dist）
bash web/build.sh     # 构建前端到 web/dist
bash web/dev.sh       # 前端开发模式 :5273（/api 代理到 :6799）
/Users/xinxin/.browser-use-env/bin/python3 -m pytest -q   # 单元测试（纯决策函数）
```

- **Python 解释器必须用** `/Users/xinxin/.browser-use-env/bin/python3`（含 fastapi/uvicorn/httpx）。
- 前端 npm 依赖走国内镜像（npmmirror），脚本已设 `npm_config_registry`。
- **测试**：`/Users/xinxin/.browser-use-env/bin/python3 -m pytest -q`（配置 `pytest.ini`，用例在 `tests/`）。
  只测**纯决策函数**：不触网、不读系统时钟、不写磁盘。HTTP 层与前端不做自动化测试，
  仍用 `curl http://127.0.0.1:6799/api/health` 冒烟 + 浏览器核对。
- 后端托管 `web/dist`（生产模式）：**改前端后必须 `bash web/build.sh`**，否则页面不变。
  HTML 响应带 `no-store`，页脚有「构建 <bundle-hash>」戳可用于核对已加载的构建。
- **跨设备 HTTPS 入口（浏览器通知必需）**：Chrome 系统通知（Web Notifications）只在安全上下文
  可用，`http://100.x.x.x:6799`（Tailscale IP，明文）会被浏览器拒绝。已配
  `tailscale serve --bg --https=8443 http://127.0.0.1:6799`，跨设备访问用
  **https://xinxin-mac-mini.tail407c31.ts.net:8443**（Let's Encrypt 有效证书），
  在告警卡头部点「开启浏览器通知」授权即可。`tailscale serve status` 查看入口。
  测试通知：`curl -X POST http://127.0.0.1:6799/api/admin/test-alert`（发 3 条【测试】告警走真实 SSE）。
- 已装 LaunchAgent `com.etf-option-board` 常驻 `:6799`（崩溃自动拉起）。手动起服务前先确认
  端口未被它占用（`launchctl print gui/$(id -u)/com.etf-option-board`），日志 `/tmp/etf-option-board.log`。
- 板块资金排障：`curl /api/board/health` 看采集时间与库里条数；`/api/board/rank?type=all`、
  `/api/etf-flow/rank`、`/api/em-flow/series?target_type=index&code=1.000300` 逐个核。
  库文件 `data/board_flow.db`（gitignore），自选在 `data/board_watch.json`、`data/etf_watch.json`，
  4 维行业配置在 `data/em_flow_config.json`。
- **分时图没数据**（重建库/收盘后接通时的常见情形）：先跑
  `python3 scripts/check_board_series.py`（红的就说明某条分时链路为空），
  再从 stock-webui 已采集的库里回补最近 N 个交易日：
  `python3 scripts/import_stock_webui.py --days 10`（源库只读 ATTACH、幂等、不覆盖本项目的真实采集点）。
  回补后再跑一次探针应变绿。历史数据缺「涨速/指数涨跌幅」时：
  `python3 scripts/backfill_speed_pct.py`（按当前快照回填最新交易日的 speed 与 pct）。
- 后端代码更新后重启生效：`curl -s -X POST http://127.0.0.1:6799/api/admin/restart`
  （先优雅关闭 Kronos 子进程再以非零码退出，LaunchAgent 约 5 秒自动拉起；curl 报连接重置属正常）。
  首次上线该接口前需手动重启一次：`launchctl kickstart -k gui/$(id -u)/com.etf-option-board`。
- **Agent 自部署（2026-10-15 起）**：Agent 改完代码后写 `data/.deploy_token`
  （JSON，如 `{"build_frontend": true, "reason": "..."}`），monitor 主循环 30 秒内自动消费
  并执行：`py_compile` 全量校验 → 可选 `bash web/build.sh` 重建前端 → `os._exit(42)`
  由 LaunchAgent 拉起。校验/构建失败则不重启，结果见 `data/.deploy_status`。
  用户也可手动触发：`curl -s -X POST http://127.0.0.1:6799/api/admin/deploy \
  -H 'Content-Type: application/json' -d '{"build_frontend": true}'`（立即返回，后台执行）。
  部署期间服务中断约 1-2 分钟（含前端构建），尽量避开交易时段。

## 架构与约定

- `services/em.py` 是唯一网络出口：手拼 query（`safe=",:"`，否则 `fs=m:10` 变 `%3A` 返回空），
  直连失败自动退 `curl` 并记忆路径，统一 TTL 缓存；`get_json` 失败**返回上次成功缓存**
  （stale-while-error，绝不抛异常）。新接口都加在这里。
- 加一个接口：后端路由写 `routers/api.py`（前缀 `/api`），业务逻辑放 `services/`；
  前端在 `web/src/lib/api.ts` 加方法，轮询用 `web/src/lib/usePolling.ts`。K 线 `klt∈{1,5,15,30,60,101}`（3 分钟本地由 1 分钟聚合）。
- **数据源策略（2026-11-01 实测更新）**：东财 `push2` 的 **`stock/*` 接口在本机被接口级封禁**
  （`trends2` / `kline` / `get`：TCP 443 可连、TLS 握手和证书都正常、请求发出后对端空回包，
  IPv4/IPv6、TLS1.2、跳过证书、换 UA/Cookie、真实 Chrome 全部一样；根路径 404 说明服务器活着）。
  **但 `clist/get` 与 `ulist.np/get` 仍 200**（2026-11-02 复测：**clist 也开始空回包**，
  ulist 稳定 200，所以全市场快照已改走 ulist）—— 所以：
  - 合约分钟线靠 **`em.option_snapshot_batch()`** 每分钟累积，由 `monitor` 后台任务调用
    （不阻塞告警链路）。**主路径是 ulist 批量报价**：合约清单落盘 `data/option_universe.json`
    （1268 个 secid，clist 能翻时建一次），之后每轮 4 个 ulist 请求（400 只/次、并发）
    即可覆盖全市场，实测 **0.8s**；旧版 clist 翻页要 39s，且 clist 被封时直接拿不到。
    clist 只在清单为空（首次冷启动）时兜底一次。
  - 期权链 `option_service._raw_chain` 同理：优先 `em.option_chain_from_universe()`（清单+ulist，
    ulist 返回字段与 clist diff 同构），`len(rows) < 20` 才退回 `clist_all`
    （clist 被封时它要 12s 且只回 1~2 页 → 链不完整、月份缺）。
  - `em._curl_json_v6` 走 IPv6：**先查一次 AAAA 并缓存 10 分钟，再用 `curl -6 --resolve`**
    （fake-IP 下系统 DNS 只给 198.18.x 伪地址，**不能**用裸 `curl -6`）。
    每次请求现场 `--doh-url` 解析要 1.11s（占单请求 1.4s 的 79%），缓存后降到 0.2~0.3s；
    固定 IP 空回包会自动丢缓存并退回 `--doh-url` 重新解析。
    **接口空回包（curl exit 52）不算通道故障**，不触发 `_V6_DEAD`，
    否则 trends2 被封会把 clist/ulist 一起熔断；
  - 行情/期权链主源仍是 **`emdatah5.eastmoney.com`**（`getZJLXData`/`getZDYLBData`），
    K线/分时走**腾讯**（`ifzq.gtimg.cn`：复权 `fqkline` 被 WAF 拦时要退回不复权 `kline/kline`，否则日线/MA/关键位全空）。
  排查"没数据"按这三条链路走，别先怪没开盘。
- **冷启动预热**：`monitor._prewarm()` 启动后台跑一次 `strategy.directions()`（9 个标的要 6s），
  否则用户重启服务后第一次打开看板要干等十几秒。
- 资金流分钟序列：`services/em.py::fflow` 在交易时段内每次调用都用 emdatah5 快照累积
  并落盘 `data/flow_history.jsonl`（push2 fflow 收盘后常返回空 klines，且经代理易被限流）。
  收盘后/进程重启后用本地累积兜底，保证分时资金流图表可复盘；注意累积的是日累计值
  口径（f137 等），与 push2 的分钟增量口径不同。当日已错过的数据无法回补。
  另保留最近一个有数据的昨日序列：9:15 开盘前当日无数据时自动展示昨日（前端打
  「昨日资金流」角标，`etf_context.flow_day` 标识数据日期），界面不空。
- **资金流向（2026-11-07 新增一级导航，ADR-0005）**：`services/tape_flow.py` 拉全A逐笔
  （东财 `details/get`，1 请求/只，约 5200 只、100~140 秒），按「分钟 × 档位 × 买卖」
  存进 `board_flow.db` 的**两张**直方图表：`tape_flow_bins`（金额档，Wind 口径）+
  `tape_flow_lots`（手数档，通达信口径），一天约 1.3 万行，留 120 天；只读接口
  `/api/tape-flow/dates|series|health`，页面 `#/flow`（Tab 切通达信/Wind 两套口径）。
  **三个坑**：`p[4]=="2"` 才是主动买、`"1"` 是主动卖（方向反着）；`p[4]=="4"` 是集合竞价
  虚拟撮合必须剔除（不剔除茅台会被算成 3 倍成交额）；解析列表时 `f13` 为 0 表示深市，
  不能用 `or ""` 兜（0 是假值会把深市整片丢掉）。
  写库前校验覆盖率 ≥90% + 成交额偏差 ≤3%，不达标不覆盖。盘中每 10 分钟重算、15:05 定稿；
  **两家口径维度不同**（实测，别随手改）：通达信按**手数**（主力 ≥700 手 / 跟风 ≥150 手 /
  散户 <150 手）且**集合竞价整段算主动买**；Wind 按**金额**（机构 ≥400 万 / 主力 70–400 万 /
  大户 15–70 万 / 散户 <15 万，按 Wind 截图拟合）且**剔除开盘集合竞价、收盘竞价按方向**——
  这条最关键：296 亿开盘竞价若按档摊进 09:30 首点，机构/主力会被压成负数、大户从 +95 亿起步、
  尾盘断崖，四条线形状全毁（曾经因此误判「机构不可复刻」）。
  门槛可在页面改，只重算直方图不重跑逐笔。页面上还可叠加 A股平均股价 / 上证指数价格线
  （**各一根独立 Y 轴**，放在左侧依次外移，右侧留给金额右侧标签；图例在顶栏、点击切换，
  不要挪回图区，否则会压低图高）。
  口径差异与取舍见 `docs/adr/0005-资金流向全A逐笔分档直方图.md`——**东财四档净额合计恒为 0，
  与这套「主动净额」不可对账**，别把两边的数字放在一起比。
- **板块级 Wind 口径（2026-11-07，`feat/wind-board-flow`）**：`/api/board/rank|series` 支持
  `mode=wind`——「主力」= Wind 的**机构 + 主力 = 单笔 ≥70 万、剔除开盘竞价**（与资金流向页同一套），
  逐笔扫描时按股累计（`wind_main_tick`）→ 按板块成分股求和 → `tape_board_points(date, code, time, flow)`
  （一天约 1000 板块 × 24 轮 ≈ 2.5 万行，留 120 天）。
  **成分股映射**在 `services/board_map.py`：`clist fs=b:BKxxxx` 逐板块拉（概念 531 + 行业 496，
  约 1~3 分钟），**日级缓存**到 `data/tape_board_map.json`，不能跟 10 分钟扫描重建。
  **只存板块结果不存每股**：换门槛/映射后历史不可重算（逐笔本就补不回来），这是有意的取舍。
  **不支持「指数」tab**（成分股接口对指数返回空）→ 前端自动回落东财并打「指数暂不支持 Wind」；
  历史日期没有 Wind 数据时后端返回 `fallback: "no_wind"`，前端角标「无 Wind 数据，已用东财」。
  口径切换后名次按 Wind 资金重算、`prev_rank` 置空（不做跨口径的名次变化）。
- **汪汪队 ETF 跟踪器（2026-11-07 新增一级导航，`#/wangwang`）**：复刻外部的
  「今日国家队上班了吗」页面，判断 24 只宽基 ETF（20 只汇金历史持仓 + 4 只扩展观察）
  今天的成交额放大倍数 + 主动净买入是否像护盘。**完全自包含**：`services/wangwang.py`
  只用 httpx + 自己的 `data/wangwang.db`，不碰 board_flow.db，也不参与期权链路；
  接口在 `routers/wangwang.py`（`/api/wangwang/snapshot|health`）。
  数据口径：腾讯 `qt.gtimg.cn/q=` 一次批量 24 只（**GBK 解码**，a[7]/a[8] 外盘内盘、
  a[37] 成交额万、a[45] 规模亿、a[72] 份额份）+ 新浪 5 分钟 K
  （`quotes.sina.cn`，`scale=5&datalen=1500` 一次拿到 32 个交易日 × 48 根，
  **一份数据同时算 20 日均额、日内累计分布 shape、尾盘占比、近 30 日柱图**，
  不必再拉日 K——腾讯日 K 的 amount 恒为 0）。
  **份额日历史没有公开源**，只能每日累积（`run_forever` 在 15:02 后定稿），所以两张
  份额图从接入日起才有点；公开接口查不到 2026 年以前的历史，别去「补全」。
  **判定在前端算**（`web/src/pages/wangwang/logic.ts`）：它要按浏览器本地时钟判断
  交易时段，后端只给原始字段。两个必须保留的口径——① 盘中成交额只走完一部分，
  分母要用「20 日均额 × 历史同时点已走完比例」（同时点对齐），否则 11:30 会被算成 2 倍；
  ② 快照日期 ≠ 今天时（实时通道没连上）必须退回全天对全天，否则倍数凭空翻倍。
  一侧市场 ≥70% 标的两日份额完全相同时判「份额源未日更」（沪市行情商份额不按日更新）。
  样式是源页 CSS 1:1，全部选择器收在 `.ww-page` 之下（原页的 `html/body/table/button`
  都加了前缀，`@keyframes` 改名 `ww-pulse`），**不套 REUI、不污染其它页面**。
- **风险监控（2026-11-07 新增一级导航，`#/arisk`，ADR 无需新增）**：把外部项目
  `~/workspace/arisk`（github `zhysh3/a-share-monitor-en`，A股风险监控 A-RISK：ERP /
  信贷脉冲 / 两层决策模型）**以 iframe 原样嵌入**，不重写它的十几张 Chart.js 图。
  页面 `web/src/pages/Arisk.tsx` 只有一段 iframe，真正的活在后端 `routers/arisk.py`：
  - `GET /arisk/page` 读 `arisk_monitor_local.html` 后**改写外部依赖**再返回：
    `http://localhost:8899` → `/arisk/live`、`./arisk_data.json` → `/arisk/data.json`、
    Chart.js CDN → `/arisk/chart.umd.js`（`web/public/arisk/` 随构建出，离线可用）、
    顺手删掉 Google Fonts 的 `@import`（国内超时会拖首屏）。
  - `GET /arisk/data.json` 直读 `~/workspace/arisk/arisk_data.json`（日更，no-store）。
  - `GET|POST /arisk/live/<path>` 反代 arisk 本地实时代理 `127.0.0.1:8899`，上游挂了
    降级成 `{"error": ...}` 的 200（面板自己有 `r.ok` 判断 + 静态兜底）。
  **不能让 iframe 直连 8788**：面板里硬编码 `localhost:8899`，跨设备（Tailscale/局域网）
  时 localhost 指客户端、走 HTTPS 还会被混合内容拦掉 —— 必须由后端收成同源。
  arisk 自己的采集链路（LaunchAgent `com.arisk.web` :8788 / `com.arisk.proxy` :8899 /
  `com.arisk.update` 每交易日晚 22:10 更新数据）**完全独立**，本项目只读不写，
  上游 `git pull` 不会影响这里。`ARISK_DIR` / `ARISK_LIVE` 可覆盖路径与代理地址。
- **涨停复盘（2026-11-07 新增一级导航，`#/limitup`，页面名「涨停复盘」，ADR-0006）**：
  复刻经传「涨停棱镜」（页内那张图表卡片仍叫「涨停棱镜」），
  四个面板 —— 涨停周期表（近 N 日 × 每日 Top10 主题网格，单元格角标 = 该主题近 20 日涨停总数）、
  涨停棱镜图（黄线 = 沪深涨停总数、红面积 = 所选主题涨停数）、主题榜（涨停数 / 涨幅 / 今日・3日・5日
  主力净额 / 主力占比 + 热・扩张・共振标签）、个股池（炒作涨停 / 板块涨停 / 板块炸板 / 板块烂板，
  带涨停原因 / 连板数 / 封板金额）。**完全自包含**：`services/limitup.py` 只读写
  `data/limitup.db`，不碰 `board_flow.db`、不参与期权链路；接口 `routers/limitup.py`。
  数据源：东财 `push2ex` 涨停池/炸板池 + `clist m:90 t:3` 概念板块资金（`f62` 今日、
  `f267` 3日、`f164` 5日、`f184` 占比）+ `datacenter RPT_F10_CORETHEME_BOARDTYPE` 个股所属概念，
  涨停原因取同花顺 `limit_up_pool` 的 `reason_type`（被 WAF 拦时降级成东财行业名）。
  **四个必须保留的坑**：① `clist` 的 `pz` 被截到 100，概念名单必须翻页，否则 82 家涨停只落到
  29 条归属、主题全空；② 个股板块列表必须用 `IS_PRECISE == "1"` 过滤，否则当日 Top1 会是
  `最近多板` / `东方财富热股` / `连续亏损` 这类特色数据噪音；③ 同花顺 `limit` 上限 200（传 500
  直接报错）；④ 东财涨停池**只能回溯约 20 个自然日**，历史靠自己攒：每日 15:05 定稿写库、
  启动回补缺口、连续 3 个空日期即停；**历史日期不写资金四列**（clist 只有"当下"的钱，
  标到上周五头上是错的，只在最新交易日且 ≤4 天时补）。主题等级/扩张/高潮/三共振都是
  **可推导近似**（见 ADR-0006），别当成结论。
  **两种口径同时落库**（`lu_themes` 主键带 `mode`）：`zt` = 东财涨停池（封板，默认）/
  `ext` = 「扩充涨停」= 涨停池 ∪ 涨幅>9.9% 的个股（`lu_pool.sealed=0`，剔除 N/C 开头新股），
  页面「三共振」右边的勾选就是切这个，不用重跑上游。周期表单元格底色是**蓝色**，
  深浅编码 `composite_rank()` 算的**统计期间综合排名**（`Σ zt×0.9^距今天数`，按分数归一
  再开 0.75 次方 —— **不能按名次百分位**，130+ 个概念会把前 15 名全挤在 0.95 以上）；
  选中主题的所有格子直接换成红底（不用外框）。概念归属按股票代码缓存 7 天（`lu_concepts`），
  否则扩充口径回补会很慢。
  **历史兜底**：东财涨停池只回溯到 20261019（约 19 个自然日），更长历史由
  `_ths_pool()`（同花顺 `limit_up_pool`，至少到 2026-01）补，`lu_pool.src` 标 `ths`；
  同花顺**没有历史炸板池**（接口 404）→ 老日期「板块炸板/烂板」为空、三共振不成立，
  扩充口径也只在实时采集那天有效（clist 只有当下的涨幅）。`_backfill(N)` 的语义是
  「库内不足 N 个交易日就补够」；同花顺连续请求会被限流，所以空日期要隔 1.2s 重试、
  日期之间 sleep 0.4s、连续空阈值取 8（国庆连休有 5 个工作日为空）。
  **主题排除名单** `data/limitup_exclude.json`（默认 `["央国企改革"]`）在读时过滤，
  改完刷新生效、不用重采；它在算热度前就剔掉，否则剩余主题的颜色会被压扁。
  **点周期表单元格 = 右侧切到那一天**：窗口锚点（`date`）与右侧看的日期（`cellDate`）分开，
  主题榜另走 `GET /api/limitup/themes?date=`，别让整张表跟着跳。
  **坑**：`_write_snapshot` 的 `boards` 必须永远传全量（它同时用于把概念名映射成
  `lu_stock_concepts.board` 的板块代码），历史回补不写资金时用 `write_boards=False` 控制，
  **不能把 boards 传空**——否则概念行落成中文名，按主题筛个股池恒为空。
  **主题分级与排序口径**：`lu_themes.mode` 有 4 种（`zt`/`ext` × `_hot`）——
  `_hot` 是「按炒作涨停」（一票一主概念，BOARD_RANK 最靠前那个），非 `_hot` 是「按板块涨停」
  （一票计入所有命中概念）；「按涨幅」只改排序不改集合（历史日期没涨幅 → 回落按涨停数）。
  主题等级用**板块成分股数**分档：`f104+f105+f106 ≥ 150` 算一级主题（存 `lu_board_meta`），
  缺元数据的板块两个等级都保留。
  **「涨停分析」弹窗**（`GET /api/limitup/brief`）：个股详细原因取同花顺 `block_top` 的
  `reason_info`（长文本，历史可回补）→ `lu_pool.reason_detail`；个股主力净额 `ulist.np` 批量
  → `lu_pool.main_net`（只有实时采集当天有值）；板块炒作原因**整份一次**大模型调用
  （`llm.complete()`，REST 失败自动走 CLI），按 `日期+mode` 缓存 `lu_brief`。提示词必须保留
  「只能归纳给出的个股原因、没有共同主线就明说」这条硬约束，否则会编宏观政策。
  缓存命中判 `theme_reasons`（payload 不含个股列表），别判 `themes`。
  个股池四个标签页**全部按当前板块过滤**，没有"全市场"页；名称列 104px + nowrap 防折行。
  弹窗按钮在**个股池标题右侧**，`GET /api/limitup/brief?theme=` 只分析当前板块；
  点名板块时排除名单/等级筛选让位，炒作口径查不到就退回板块口径。
  **「涨停分析」不调用任何服务端大模型（2026-11-07 用户要求，不依赖 opencode）**：
  后端 `brief()` 只拼数据 + 给规则兜底文案（`src="rule"`），真正的解读由前端
  `browserChat()` 用「设置 → 大模型」里配的 API（Base URL / 模型 / API Key，直连或后端代理）
  生成，按 `日期|口径|排序|板块` 缓存在 localStorage；没配置/失败就显示规则文案并引导去设置。
  别再往 `brief()` 里加大模型调用，也别恢复 `lu_brief` 缓存表。
  **角标口径**：周期表右上角数字 = **近 7 个交易日**涨停总数（`BADGE_DAYS=7`，字段 `count_7d`）；
  扩张/高潮/三共振的判定窗口**没变**（高潮仍是近 20 日新高），别把两者混成一个窗口。
  **口径对齐（2026-11-07 用户反馈钉死）**：主题榜「涨停」数**一律用板块口径**（= 个股池能列出的家数），
  排序口径（炒作/板块/涨幅）只决定**顺序**，不决定数字；为此 `apply_board_counts()` 在返回前把
  显示数换成板块口径，`build_matrix` 靠 `rank` 保持排序口径的顺序。**棱镜图（`prism`）的主题线、
  「涨停分析」弹窗（`brief`）也必须用 `base_mode` 读** —— 只改 overview 会剩这两处仍显示炒作口径
  （3 vs 5，2026-11-07 二次反馈）。周期表另有版面约定：当前日期列红框、选中主题红底按家数分深浅、
  顶部「期间热门」条取 `overview.period_top`、右上角**没有**日期下拉（窗口固定最新交易日）。个股池「炒作涨停」tab
  **不再按主概念过滤**（只改排序）——试过过滤，切标签页后家数又对不上。别再让"家数"随排序/标签变化。
  **弹窗排版**：980px 宽，关键信息一律用 ReUI `Badge`（板块名/涨停数/涨幅/连板/模型名），
  板块块用 `Frame variant="inverse"`，个股是两列卡片（正文 13px/行高 24px），
  不要退回密集 `<table>` —— 长文原因在表格里读不动。
  板块标题行**必须是单行**（`FrameHeader` 自带 `flex-col`，直接用会竖排，要用普通 `div.flex`）；
  个股原因默认 `line-clamp-3` + 「展开全部」（同花顺 `reason_info` 常有 300~500 字，铺开一张卡 360px）。
  **免责声明从数据里剥掉**：同花顺每条个股原因尾部都带「（免责声明：AI 生成…以上市公司公告为准）」，
  `_clean_reason()` 在写入与渲染两侧都清，统一放到弹窗底部一行小字（12px）。
  **筛选勾选项的说明用本地 `Hint` 组件**（`LimitUp.tsx`，CSS `group-hover` + `group-focus-within`
  双触发），不是原生 `title` —— 原生 tooltip 有 1 秒延迟且样式不可控，别再退回 title。
- **板块资金（2026-11-04 新增，ADR-0003）**：单写路径 —— 只有 `services/board_collector.py`
  在交易时段写 `data/board_flow.db`（SQLite），`/api/board/*`、`/api/etf-flow/*`、`/api/em-flow/*`
  一律只读，**不要为了让页面有数据去实时抓东财**。采集分频：板块排行 30s（约 11 请求）、
  ETF 30s（69 只）、指数 4 维 30s、行业 4 维 60s（默认 `|净流入|` 前 40，可配置）、
  概念 4 维 60s（默认前 40）、**自选 + 当日热门匹配板块 4 维 2 分钟一轮**
  （`bf.extra_em_targets`：Top40 是轮换的，自选/热门里不在 Top40 的板块不补采就永远没曲线）。
  保留窗口 30/120 天，启动与每日裁剪；页面走 IndexedDB SWR，盘中 30s / 休市 300s。
  纯函数（时段/解析/名次/区间聚合/序列对齐/裁剪）在 `boardflow.py`，单测在 `tests/test_boardflow.py`。
  **两个数据源坑**：emdatah5 的 `fs=m:90+t:3` 必须编码成 `%2B`（raw `+` 被当空格会忽略过滤）；
  板块 4 维 secid 必须是 `90.BKxxxx`。
- **右侧「虚线 + 彩色标签」图层（2026-11-06）**：`web/src/components/charts/endLabels.tsx`，
  主力资金分时与四维资金共用。`LABEL_GUTTER`=260（右侧留白中含 50px 引导虚线），
  标签按 y 排序做 12.5px 最小行距避让。**严禁在 render 期间调用 ECharts 实例方法**：
  `ReactECharts` 的 `key`（图表 id）变化会销毁旧实例，此时 `chart.getWidth()` 直接抛
  `Cannot read properties of null`，会把整页打进 ErrorBoundary（显示「页面 渲染出错」）。
  像素坐标只能在 effect 里算、先判 `chart.isDisposed()`，结果放 state。
- **财联社「热门」（2026-11-06）**：`services/cls_hot.py`。异动取
  `v3/transaction/articles` 里**所有带 `plate_name` 的条目**（每条都是一次板块异动播报，
  不能按标题含「异动」过滤，否则一天只拿到 1~2 条）；热门取
  `web_quote/plate/hot_plate?type=industry,concept`（行业/概念各 6 个）。
  CLS 与东财命名不一致的靠 `_ALIASES` 显式对齐（异质结电池→HJT电池、短剧→短剧互动游戏、
  豆包大模型→抖音概念…），归一化会先去掉括号注释（`抖音概念(字节概念)` → `抖音`）。
  只缓存 CLS 名称 **2 分钟**，资金/涨幅按请求日期现算（缓存整份结果会让历史日期显示错的钱）。
  **只收上涨方向的异动**：`is_up_move()` 先用下跌措辞（跌/低开/走弱/下挫…）一票否决，
  再要求命中上涨措辞（拉升/走强/涨停/活跃/异动…），把「CPO 低开跌超10%」这类下跌播报
  和「盘前题材挖掘/早间新闻精选/商品期货开盘」这类纯新闻噪音挡在「热门」之外。
  前端三个页面的 `cls-hot` 轮询**不要挂 IndexedDB 缓存**（页面会停在几分钟前的名单，
  用户会看到已经跌下去的板块），客户端 2 分钟轮询一次即可。
  **刷新按钮要带 `?force=1`**：`/api/board/cls-hot?force=1` 会同时绕过 `cls_hot` 的名称缓存
  与 `em.get_json` 的 URL 缓存，真正回源财联社；普通轮询不带 force，避免刷爆上游。
- **三页统一「概念｜行业｜指数｜热门｜自定义」+ 共用自定义（2026-11-06）**：主力资金分时 /
  主力资金多日 / 四维资金三个页面 tab 完全一致，**自定义是同一份 `data/board_watch.json`**
  （`/api/board/watch`），任一页行尾 ＋/－ 立刻同步到另两页。因此 `boardWatch` 的 `usePolling`
  **不要传 IndexedDB cache 配置**——缓存新鲜时会跳过首轮请求，别的页面刚改完，这边要等 5 分钟
  才更新（表现为「自定义不同步」）。多日页的指数走 `/board/multi-series`（非 BK 代码用
  `read_em_points` 合成机构+大单），列表口径取区间末日快照。
- **「今日」按钮（2026-11-06）**：工作日 **09:15 之后**（集合竞价开始）一律落到当天日期本身，
  即使库里最新还停在上一交易日（数据 09:30 才开始采集）；之前/周末落到库内最新交易日。
  判定用 `web/src/lib/format.ts` 的 `afterPreOpen()`/`todayStr()`，`isLiveDay()` 同步把
  「09:15 后的今天」视为实时。今天不在 `dates` 里时下拉要补一条，否则 `select` 显示错项。
- `services/universe.py` 维护标的/指数/外围/国家队清单；改标的只改这里。
  **2026-11-04 起 ETF 标的收敛为 6 只**（按跟踪指数去重，删 159919/159922/588080，见
  `docs/adr/0004-期权标的收敛为6只.md`）；下线后记得跑
  `python3 scripts/prune_option_universe.py` 剪掉 `data/option_universe.json` 里残留的合约，
  否则后台每分钟仍会为这些合约拉报价、写分钟线。
- 期权链：东财 `fs=m:10`(沪)/`m:12`(深) 全量列表，按简称前缀切分。**注意前缀顺序**
  （`科创50` vs `科创板50`，`300ETF` vs `沪深300ETF`）。
- 隐波/希腊字母由 `services/bs.py` 自算（Eastmoney 不直接给），到期日 = 到期月第 4 个星期三。
- 前端组件：REUI（`web/src/components/reui/*`，Tailwind v4 语法）与自研 shadcn 风格
  `web/src/components/ui/*` 混用；图表用 ECharts（`components/charts`）。
- **加页面的唯一入口（2026-11-06 起）**：`web/src/lib/route.ts` 的 `PAGES` 加一条（带
  `key/hash/label/title/section`）+ `web/src/App.tsx` 的 `VIEWS` 加一行，**左侧导航项会自动出现**
  （`lib/nav.ts` 从 `PAGES[].section` 派生分组）；同一 `section` 下的页面自动成为该组的页内
  子标签（如 `BoardSubNav` 的四个资金页，顶栏没有页面标签，导航入口只有侧栏 + 页内子标签）。
  要新开一个一级分组，再在 `nav.ts` 的 `SECTIONS`（文案）与 `SECTION_ORDER`（顺序）里登记，
  并在 `SideNav.tsx` 的 `ICONS` 里给它一个 lucide 图标（`NavIcon` 类型同步加）。
- **顶栏标题带当前页名**：`ETF 期权日内决策看板 - {页面名}`（页面名取 `pageLabel(page)`，
  资金页显示具体子页如「四维资金」；`span[title]` 存全串，窄屏靠 `truncate` 省略）。
- **左侧导航行为**：常驻侧栏 200px（`md` 以上），收起后完全不占布局；收起状态点左上角
  按钮弹 224px **浮层抽屉**（带遮罩、Esc 关闭、选完自动关、底部「固定展开」变回常驻）。
  展开状态与「上次看的资金子页」都存 localStorage（`etf.nav.open` / `etf.nav.lastBoard`），
  所以点侧栏「资金分时」会回到上次那个子页，而不是每次跳回第一页。
- **中栏布局约定（2026-11-01）**：行情卡（分时图 + 两个横线开关 + 高度×2）→ T 型报价 →
  **合约分时图** → 决策区（策略与仓位 / 今日推荐合约 / 标的点位 / 合约买卖点，都在 `PlanCard`）
  → 优选合约 → 大模型决策。决策类卡片一律放中栏、合约分时图**下面**；右栏只留实时告警、
  日内方向与信号、Kronos、到期日历。
- **合约图三组横线**（支撑/压力、止盈/止损、打和点）：按钮常驻（数据没到只置灰），
  且**线不依赖分时数据存在**——`optPrice` 为空时仍保留一个空 data 的「合约价格」series
  来承载 `markLine`，否则分时源一断，线和开关会一起「消失」（用户会以为开关失效）。
  分时源全断时用 `optLast`（合约现价）+ Δ 合成一条虚线走势兜底。
- **分时缓存**：合约分时/买卖点走 `usePolling` 的 IndexedDB 缓存（kind=`trends`/`levels`），
  切合约先渲染缓存；**缓存比轮询周期新时不发首轮请求**，来回切标的/合约几乎不产生请求。
  盘中 15s、非交易 120s，页面切后台暂停。
- **排序与口径（2026-11-06）**：四个资金页的排序统一为 `涨幅｜涨速｜资金`。
  **涨速 = 近 5 分钟涨跌幅**（用户口径；东财 `f22` 是 3 分钟，不能用）。板块/ETF 由采集器
  在内存里记 pct 历史算好写进 `rank_snapshots.speed` / `etf_rank_snapshots.speed`
  （进程重启后 5 分钟内回落到 f22 的 3 分钟值）；指数/4 维用 `em_flow_points` 的分钟 pct
  历史现算（`speed5_map`，`time` 只有 HH:MM）；多日页取区间最新交易日收盘时的 5 分钟涨速。
  ETF资金流口径 tab = **宽基(broad)｜窄基(sector)｜自定义(自选)**，不再有「全部」；
  旧链接 `scope=all` 自动归到宽基。**已下线两个指数**：47.800000 东方财富全A、
  1.000985 中证全指（`bf.EM_HIDDEN`，采集与 `/board/index-rank`、`/em-flow/targets` 都不再出现）。
- **右侧标签悬停联动（2026-11-06）**：`FlowEndLabels` 的标签 `pointer-events:auto`，
  hover 回调 series code → 主力资金分时走 `onHoverCode`（该线变红/绿分段），
  四维资金在 `MoneyLinesChart` 内本地 `hoverCode`（加粗+红绿、其余淡出）。
  `_temp_hover` 临时线不参与联动。
- **期权看板左侧迷你走势（2026-11-06）**：`/api/etfs/spark` 一次并发拉 12 条分时（6 ETF +
  4 股指 + 2 大盘指数），冷启动 1~6 秒。两层缓存：前端 `usePolling(..., 60_000, [],
  { kind: "trends", code: "etfs-spark" })` 存 IndexedDB（跨日失效），重开页面秒出；
  后端整包 15 秒进程内缓存 + 单飞锁（`_SPARK_CACHE`/`_spark_lock`），多端同时冷启动只拉一次。
- **分时图时间轴约定（2026-10-19 起）**：轴一律由完整行情分时（`trends`，腾讯）定义；
  资金流/期权等稀疏序列必须用 `web/src/lib/series.ts` 的 `trimSession` + `alignToAxis`
  按「最近前值 + 同日校验」对齐。**禁止再用 `HH:MM` 精确相等 join**——东财资金流首点是
  09:31/13:01、只到 15:00，行情轴有 09:30/13:00 且收盘后常有 15:06–15:30 的盘后点，
  两者错位会导致柱状图成段空洞。后端 `_trends_trim` / `_flow_clean` 已统一裁掉
  非连续竞价时段的点。盘前腾讯单日分时只有当日 09:30 占位点时，`trends()` 回退到
  多日接口的上一交易日，并返回 `trends_prev_close` 作为涨跌幅基准（前端优先用它）。
- **左侧面板布局约定**：`EtfList` 里「期权标的」列表是唯一的滚动区（`flex-1 overflow-auto`），
  「大盘指数」是**面板固定底栏**（不随列表滚动、矮窗口时内部滚动）——保证 4 个指数行
  始终完整可见。别再把它塞回滚动列表，那正是"看不到某一行"类投诉的根源。
- **排查"用户设备上页面不对"的标准手法**（用户常经 Tailscale 从另一台设备访问，本地无法复现）：
  前端加临时信标，把 bundle 哈希/UA/视口/滚动状态/目标元素 `getBoundingClientRect`+
  `getComputedStyle` 编码进一次 `fetch('/api/health?dbg=...')` 的 query，uvicorn 访问日志
  （`/tmp/etf-option-board.log`）会原样记录，无需重启后端、无需用户操作（用户刷新即可）。
  用 `/tmp/etf-option-board.log` 按客户端 IP 区分设备。**定位后必须删信标并重建。**
- 所有 API 失败**降级返回 `{error}`，不抛 500**。
- **资金金额口径（2026-11-05 统一）**：资金流/成交额一律 **≥1 亿用「亿」（2 位小数）、
  不足 1 亿用「万」**（万的小数位随量级收减，避免小额抹成 0 万）。前端用
  `web/src/lib/format.ts` 的 `flowMoney`/`signedFlow`，后端用 `services/money.py` 的 `money()`。
  **不要**用它格式化持仓敞口/张数等元级或计件数字（那些仍用 `money()`），否则几百元会显示成 0.05 万。
- **变盘开仓信号**（`services/reversal.py`，2026-10-15 新增）：多/空各一套 0~100 打分
  （1m近3根金叉/死叉+25、3m确认+20、突破近15分钟高低+15、放量+10/+15、主力同向+10/+15、
  指数同向+10、背离+10、时点共振+5），**55 分触发**，critical 级告警带开仓建议
  （方向/因子/标的点位/优选合约/IV分位）。仅 09:40–14:45；同向 20 分钟冷却；
  反向 15 分钟抑制。`GET /api/reversal/signals?code=` 供分时图打标记。

## 依赖的外部资产

- Kronos：`/Users/xinxin/workspace/facecat-kronos`（`.venv` + `model/hf-cache`）。缺则预测区显示离线。
- LLM：`~/.local/share/opencode/auth.json` 的 `opencode-go` key（免费模型也用这个 key），
  免费模型（`*-free`）端点 `https://opencode.ai/zen/v1`，Go 付费模型才是 `https://opencode.ai/zen/go/v1`；
  **必须带 `x-opencode-session: <uuid>` 头**。默认模型 `deepseek-v4.1-flash-free`，可用
  `LLM_API_BASE`/`LLM_MODEL`/`LLM_API_KEY` 覆盖。免费额度用尽时返回 429 + `retry-after`，
  约 8 小时（每日 00:00 UTC / 北京时间 08:00）恢复。模型为推理模型，`max_tokens` 需给足。
- **免费模型只能走 opencode 客户端通道（2026-10-30 实测）**：免费额度用尽后，同 key 直连 REST
  （`zen/v1`、`inference/openai/v1`，各种 `x-opencode-*` 头/UA/IPv4/IPv6）一律 429，但
  `opencode run -m opencode/deepseek-v4.1-flash-free` 仍可跑（17–30s/次）。故 `services/llm.py`
  在 REST 失败时自动回落 CLI：`opencode run --pure --format json -m <LLM_CLI_MODEL> --dir <LLM_CLI_DIR>`，
  解析 NDJSON 的 `text` 事件；`LLM_CLI_FALLBACK=0` 关闭，`LLM_CLI_MODEL`/`LLM_CLI_TIMEOUT`(默认 240s)/`LLM_CLI_DIR` 可调。
  决策结果带 `_via: "cli"|"api"` 便于排查。免费额度无公开查询接口，只能看 429 的 `retry-after`。
- **设置 → 大模型支持 opencode zen（2026-11-07）**：面板有「opencode zen 免费模型」预设，一键填
  `https://opencode.ai/zen/v1` + `deepseek-v4.1-flash-free`，**API Key 由用户自己粘**
  （取 `~/.local/share/opencode/auth.json` 的 `opencode-go`）。zen 必须带
  `x-opencode-session` 头，前端（`browserLlm.modelRequest`）与后端代理（`llm_proxy.request_parts`）
  都按「域名以 opencode.ai 结尾」自动补随机 UUID —— 免费模型判客户端就看这个头，别删。
  默认走**看板后端代理**（zen 不给浏览器 CORS）；免费额度用尽时 REST 一律 429
  `FreeUsageLimitError`，次日 08:00（北京时间）恢复，这期间涨停分析会退回规则文案。

## 风险/合规

- 数据为公开接口快照，非交易级；仅供研究，不构成投资建议。
- 不提交 `.env`/密钥；`web/node_modules`、`__pycache__` 已 gitignore。

## Agent skills

### Issue tracker

Issues and specs live as markdown files under `.scratch/<feature>/`. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical triage roles, each label string equal to its name (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.

## 协作约定（记忆）

- **对话语言：中文。** 本仓库相关的回复、工单、注释一律用中文。
- **GitHub 只在你发话时动（2026-11-07 用户明确要求，优先级最高）**：
  **禁止自动 `git push`、禁止自动 `gh pr create` / `gh pr merge`**。改完代码只做**本地** commit
  （按全局规则照常 auto-backup），推送与开 PR/合并一律等用户明确说"推上去 / 发 PR"再执行。
  这条**覆盖**下面第 2 条的自动 PR 流程。
- **新特性走分支 + PR（2026-11-07 起）**：开发新功能/较大改动**不要直接改 `master`**，先开分支
  （建议 `feat/<主题>`、`fix/<主题>`、`perf/<主题>`），在分支上本地提交。
  **推送与提 PR 要等用户发话**（见上一条）；用户点头后再
  `git push -u origin feat/xxx` → `gh pr create`（写清动机/改动/验证）→ `gh pr merge --squash`。
  流程里的自测照旧：改前端记得 `bash web/build.sh`、
  `/Users/xinxin/.browser-use-env/bin/python3 -m pytest -q` 全绿。
  只有紧急修复、改文档/错字这类极小改动才允许直接提交主干。
  仓库已迁到组织 `NIO-SAI/etf-option-board`（个人账号仓库不支持 Admin 角色）。
- **项目定位一览**（供新会话快速上手，不必重复调研）：
  - 是什么：6 只可交易期权宽基 ETF 的日内决策看板 —— 聚合行情/期权链/资金流/指数/外围
    → 信号引擎打分 → 大模型出可执行方案 → SSE 声音+弹窗告警。
  - 规模：后端 `services/` 15 个模块约 5.3k 行；前端 `web/src` 约 3.5k 行 TS/TSX。
  - 文档地图：架构与策略逻辑 → `CONTEXT.md`；部署/命令/数据源策略 → 本文件；
    标的清单 → `README.md`；工作追踪与 triage 结论 → `.scratch/`。
  - 4 个独立 agent skill（策略顾问/链数据/策略报告/长期备兑）**已移出本仓库**，
    现在在 `~/workspace/Skills/疯疯旭SKILL/期权/`，不属于看板运行时；
    本仓库的 `.gitignore` 仍保留 `期权/` 规则，防止误拷回来。
- **易踩的三个点**：行情主源是 `emdatah5` + 腾讯（K线/分时），`push2` 仅兜底，排查「没数据」
  别先怪没开盘；改前端必须 `bash web/build.sh`，否则线上不变；用户设置（总资金/单笔风险%）
  存 `data/settings.json`，靠 `.gitignore` 的 `data/*.json` 规则规避 auto-backup —— **新增长期数据
  文件一律用 `.json` 后缀**；`data/*.jsonl` 已于 2026-11-02 移出跟踪（分钟线累积会持续增长）。
- **动态状态不写进本文件**（会过期），按需现取：追踪器进度看 `.scratch/<feature>/` 与
  `.scratch/triage-*.md`；服务健康 `curl http://127.0.0.1:6799/api/health`；
  最近一次部署结果看 `data/.deploy_status`。
