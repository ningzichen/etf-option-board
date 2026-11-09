# 01 — 四维资金/资金流向：鼠标悬停分时图时十字线与右侧标签闪烁

Status: ready-for-human
Type: bug
Created: 2026-11-09
Area: `web/src/components/charts/index.tsx`（MoneyLinesChart / FlowEndLabels）、`web/src/pages/EmFlow.tsx`、`web/src/pages/TapeFlow.tsx`

## 现象（用户原话）

> 鼠标悬停在分时图上，十字线和标签还是在闪烁。

- 页面：`#/board/em`（四维资金）、`#/flow`（资金流向，同一组件）
- 表现：鼠标停在主图上（不动或缓慢移动）时，**十字线（axisPointer）与右侧「虚线 + 名称/值」标签**出现可见的抖动/闪烁；刷新数据时更明显。
- 环境：Chrome（本机 macOS），深色主题；生产构建由 `web/build.sh` 产出、后端 `:6799` 托管 dist。

## 已尝试且已在代码里的改动（仍未彻底解决）

1. `option` 不再依赖 `hoverCode`（避免悬停触发 `setOption(notMerge)` 清掉 tooltip）。
2. 标签悬停从 `setHoverCode` 改为命令式 `chart.dispatchAction(downplay/highlight)`，`labels` 不再带 `active`，`itemKey` 不随悬停变化。
3. tooltip 补发只在 option 引用变化（数据刷新）时执行，且收敛为 60/700ms 两次。
4. `FlowEndLabels` 像素坐标只在 effect 里算（不在 render 期碰实例）；标签层 `pointer-events` 只给文字。
5. 两图用 `echarts.connect(group)` 联动，`linkGroup` 在数据到位后重新入组。

## 反馈环与已获得的证据

**合成事件测不出用户看到的闪烁**（关键线索）：

```js
// 在页面里对 canvas 连续 dispatch MouseEvent('mousemove') 20 次（每 100ms），
// 采样 tooltip 可见性 + 统计容器 DOM mutation：
{ flickers: 0, samples: "11111111111111111111", mutations: 38 }
// 修复「标签 hover 触发重渲染」前：标签上来回 8 次 → mutations 70；修复后 → 3
```

即：**程序化 mousemove 下已经稳定**，但真实鼠标（尤其连续移动/快速划过标签区）仍会闪。说明合成事件没有覆盖真实输入路径（如每帧 mousemove、`mouseenter/leave` 抖动、浏览器 tooltip 过渡动画）。

## 待验证的假设（按可能性排序）

1. **真实 mousemove 每帧触发**：ECharts 每次 `updateAxisPointer` 重建 tooltip DOM，`transitionDuration`（默认 0.4s）叠加 hide/show → 视觉闪烁。验证：`tooltip.transitionDuration = 0` 后是否消失。
2. **`echarts.connect` 双图互推**：主图 showTip 同步到副图，副图再回推，形成振荡。验证：临时去掉 `linkGroup` 看是否仍闪。
3. **`FlowEndLabels` 每帧 `convertToPixel` + `setLayout`**：标签位置在亚像素级抖动（`Math.min(p[0], …)` / `ly` 取整缺失）。验证：给 `x/y` 加 `Math.round` 并停止在 `mousemove` 期间重算。
4. **`NowMarker` / 「现在」竖线**每秒 setState（资金流向页）导致整层重渲染。验证：暂停 1s 定时器看是否仍闪。
5. **`notMerge` 轮询刷新**在盘中 30/60s 触发一次全量 `setOption`，与鼠标状态冲突。

## 复现步骤

1. 打开 `https://<host>:6799/#/board/em`（盘中/收盘后均可复现，收盘后更明显，因为数据不再变）。
2. 鼠标停在主图中间不动 5 秒，再缓慢左右移动。
3. 观察主图的竖向十字线与右侧彩色标签是否抖动/闪烁。
4. 在 `#/flow` 复现同样现象（同一 MoneyLinesChart 组件）。

## 需要接手者做的事

- 用**真实鼠标**（或 CDP `Input.dispatchMouseEvent` 连续序列，每 16ms 一帧）复现，录屏 3~5 秒作为基线。
- 按上面 5 个假设逐个二分（每次只改一处，录屏对比）。
- 修好后请补一条自动化回归：至少断言「稳定悬停 2 秒内 tooltip 可见性无翻转」；
  当前仓库没有前端测试运行器，可用 `ego-browser nodejs` 脚本或浏览器内脚本形式落在 `scripts/`。

## 验收标准

- [ ] 鼠标在分时图上静止/缓慢移动时，十字线与右侧标签不出现可见闪烁（录屏对比）
- [ ] 数据刷新（点击「刷新」/轮询）后 tooltip 不消失、不闪
- [ ] 四维资金（`#/board/em`）与资金流向（`#/flow`）两页均通过
- [ ] 保留现有交互：悬停标签高亮对应线、副图与主图十字线联动、前一日幽灵线及其标签
