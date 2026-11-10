# 上证指数弹窗：手动支撑/压力位（按天保存、顺延生效）

**Status:** done（2026-11-09）
**Source:** 用户当面需求
**分支:** `feat/index-manual-levels`

## 需求

在「上证指数 · 关键位」弹窗里手动填支撑位/压力位，按天保存，方便以后调取。
**补充要求（同日）：图表只显示手动这两条线**，20 日低点/高点、下方整百/上方整百不再上图。

## 已拍板的决策（与用户逐条确认）

1. **参与评分**：手动位与自动位同规则（≤0.3% → ±8、0.3–0.8% → ±4），一起累加后整体 clamp ±15；
   方向卡底部仍只有一条「指数关键位」明细，不新增明细键。
2. **不判突破/跌破**：只有手动位的突破不触发 `breakout`。
3. **按交易日存但顺延生效**：写入日 = 弹窗当前展示的指数分时交易日（`trends_day`）；
   读取取「日期 ≤ 今天」的最大日期记录，次日没重设就沿用上一条。
4. **不提供历史 UI**：界面只有一个「当前生效值」输入区，历史记录只留在文件里。
5. **必须两个都填**，严格校验（100–99999、支撑 < 压力），**没有清除入口**（只能覆盖）。
6. **输入区位置**：价格行下方一行紧凑输入 + 保存按钮 + 「沿用于 / 今日设 · 参与评分」小字；
   输入框默认回填当前生效值，Enter 也可提交。
7. **图表**：只画手动支撑/压力两条线（图例也只这两条可开关），Y 轴范围只按分时价 + 均价 + 手动位算；
   **因子表保持原样**（自动位照旧列出并计分）。

## 实现

- `services/index_levels_manual.py`（新）：`parse_record()` / `effective()` 纯函数；`load_records()` /
  `save()` / `current()` 文件薄壳，失败降级不抛异常。落盘 `data/index_levels_manual.json`
  （`{"records": {"2026-11-07": {"support", "pressure", "at"}}}`）。
- `services/index_levels.py`：`evaluate(prev, last, mins, manual=None)` 追加 `manual_support` /
  `manual_pressure` 两行；缓存改为只存取数结果，关键位每次按当前手动值重算；新增 `invalidate()`；
  `snapshot()` 返回 `manual`。
- `routers/api.py`：`GET /api/index/levels` 带 `manual`；新增 `PUT /api/index/manual-levels`
  （校验失败 400 + `errors`）。
- `web/src/components/IndexLevelsDialog.tsx`：输入区 + 保存（本地校验 + 服务端错误回显）、
  `dirty` 保护（用户改过之后 10s 轮询不覆盖输入）、`markLine` 只含手动位、图例只手动位。
- `web/src/lib/api.ts`：`saveIndexManualLevels`。
- `tests/test_index_levels.py`（新，22 例）：不传 manual 与旧版一致、±8/±4/0、clamp ±15、
  历史不足仍计手动位、手动位不触发突破、`parse_record` 各校验分支、`effective` 顺延/跨日/脏数据。

## 验证

- `pytest -q`：720 passed。
- 接口：`GET /api/index/levels` 无记录时 `manual=null`、levels 6 条；`PUT` 支撑>压力返回 400
  「支撑位必须小于压力位」；保存 3650/3670 → `manual.carried=false`、levels 8 条、
  手动支撑 +8 / 手动压力 −8、total 仍 5（clamp 口径未变）。
- 弹窗（ego-browser + canvas 取色）：图内只有 `#059669`（手动支撑）与 `#be123c`（手动压力）两条线，
  `#2563eb`/`#dc2626`/`#f59e0b`/`#a855f7` 像素为 0；图例只有手动两条；表格 11 行（含自动位与合计）；
  非法输入提示可见；保存后「已保存」出现且输入/图例/表格同步；删掉数据文件重开 →
  输入框空、提示「未设置 · 保存后显示在图上」、图上无横线。
- 测试数据已清除（`data/index_levels_manual.json` 删除），未留假记录。

## 追加需求（同日）：当天没设置就每 10 分钟提醒

**口径（逐条确认）**
1. 判定：**今天（自然日）没有保存过记录**就算没设 —— 顺延来的旧值不算已设。
2. 通道：走告警总线 `type=manual_levels`，`level=warn` + `silent=true`：
   右栏告警卡 + 浏览器通知 + 页面 toast，**不响铃**；远程推送默认（min_level=critical）不会发。
3. 时段：**工作日 09:15–15:00**（含集合竞价与午休），收盘后停；周末/节假日不提醒。
4. 节奏：**10 分钟**一条；首条在开幕后第一个监控轮次立即发（09:15 起）；跨天自动重置。
5. 文案：标题「今天还没设上证支撑/压力位」；正文「点左栏『上证指数』打开弹窗填写，保存后本提醒
   立即停止。当前沿用 YYYY-MM-DD 的支撑 x / 压力 y。」（没有任何记录时说「当前还没有任何手动位。」）

**实现**
- `services/index_levels_manual.py`：新增纯函数 `has_record(records, day)`（today 有且数值合法）。
- `services/monitor.py`：`manual_reminder_window(now)` 纯函数 + `_manual_levels_alert(now)`（10 分钟冷却、
  跨天重置），在 `cycle()` 里排在 `_expiry_alert` 之后、交易时段早退之前调用，所以 09:15 就会跑。
- `web/src/lib/alertBus.ts`：告警带 `silent: true` 时跳过 `sound.play()`，通知与 toast 照发。
- `web/src/components/AlertCenter.tsx`：`TYPE_LABEL` 增加 `manual_levels: "手动关键位"`。

**验证**
- `pytest -q`：731 passed（新增 7 例提醒 + 4 例 `has_record`：窗口边界、首条立即发、10 分钟冷却、
  今天已设不提醒、跨天再提醒、正文带顺延值）。
- 进程内模拟（注入 now + 替身 load_records）：9:15 发第一条（warn/silent，正文带 2026-11-06 的
  3850/3950）→ 9:20 冷却挡住 → 9:25 再发 → 23:00 不提醒 → 今天已设则不提醒。
- 未做真实 09:15 的 SSE 端到端（当时是深夜），留待次日开盘核对告警卡与浏览器通知。
