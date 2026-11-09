import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { Loader2, Maximize2, Minimize2, PanelLeftClose, PanelLeftOpen, PanelRightClose, PanelRightOpen } from "lucide-react";
import { Frame, FrameHeader, FramePanel, FrameTitle } from "@/components/reui/frame";
import { Badge } from "@/components/reui/badge";
import { IntradayChart, LineToggle } from "@/components/charts";
import { BiasBar, BiasBarSkeleton, CandidateList, CandidateSkeleton, EtfList, ExpiryCalendar, FutContracts, OptionTQuote, PlanCard, SignalFeed, TopPicksList } from "@/components/panels";
import { ContractDetail, KronosPredict, LlmPanel } from "@/components/detail";
import { AlertCenter } from "@/components/AlertCenter";
import { EmptyState, Skeleton } from "@/components/ui/misc";
import { Fold } from "@/components/ui/fold";
import { Tip } from "@/components/ui/term";
import { ErrorBoundary } from "@/components/ui/ErrorBoundary";
import { api } from "@/lib/api";
import { usePolling, type CacheMeta } from "@/lib/usePolling";
import { dayKey } from "@/lib/cache";
import { onGlobalRefresh } from "@/lib/refresh";
import { subscribeAlerts } from "@/lib/alertBus";
import { usePrefetch } from "@/lib/prefetch";
import { clsUpDown, hhmmss, nf, pct, signedFlow } from "@/lib/format";

// 图线开关的色带（与图上的横线同色）
const SWATCH_LEVELS = { background: "linear-gradient(90deg,#ef4444 50%,#16a34a 50%)" };
const SWATCH_PLAN = { background: "linear-gradient(90deg,#f59e0b,#ef4444,#16a34a)" };
// 图表入参的空数组兜底必须是同一个引用：写成 `?? []` 每次渲染都是新数组，
// 页面上任一轮询返回都会让图表 option 失效、notMerge 全量重建（悬浮框随之消失）。
const EMPTY: any[] = [];

/** 期权看板（原 App 主体）。外壳负责顶栏、路由与设置弹窗，这里只管本页数据与布局。 */
export function OptionsBoard() {
  // 选中标的/月份/合约跨页面切换与刷新都要保持（否则切去板块页再回来会重置）
  const [etfCode, setEtfCode] = usePersistedState<string>("board.etf", "510300");
  const [month, setMonth] = usePersistedState<number | null>("board.month", null);
  const [contractSecid, setContractSecid] = usePersistedState<string | null>("board.contract", null);
  // 切换月份后链数据要等一次请求；给用户即时反馈，数据到达即清除
  const [monthPending, setMonthPending] = useState(false);
  // 行情卡的两个图线开关 + 图表高度 ×2。开关常驻（数据没到只置灰），
  // 否则每次数据到达就多/少一排按钮，整张图会上下跳。
  const [lineToggles, setLineToggles] = usePersistedFlag2<{ levels: boolean; plan: boolean }>(
    "board.lines", { levels: true, plan: true });
  const [chartExpanded, setChartExpanded] = usePersistedFlag("board.chart-expanded");
  // 左右栏折叠状态存在本页：切到板块资金页再回来保持原样
  const [leftCollapsed, setLeftCollapsed] = usePersistedFlag("board.left-collapsed");
  const [rightCollapsed, setRightCollapsed] = usePersistedFlag("board.right-collapsed");

  const { data: universe } = usePolling(() => api.universe(), 3600_000);
  const { data: indices } = usePolling(() => api.indices(), 10_000);
  const { data: etfQuotes } = usePolling(() => api.etfs(), 10_000);
  const { data: directions, refresh: refreshDirs } = usePolling(() => api.directions(), 60_000);
  // 跨 6 只标的的合约横评（后端 30s SWR 缓存，轮询 30s 即可）
  const { data: topPicks, refresh: refreshPicks } = usePolling(() => api.topPicks(6), 30_000);
  // 左侧迷你走势：把整包序列缓存到 IndexedDB（当日有效），刷新/重开页面先秒出
  // 缓存曲线，再后台刷新；后端另有 15 秒整包缓存，避免多端同时冷启动各拉 12 条分时。
  const { data: spark } = usePolling(
    () => api.etfs_spark(), 60_000, [], { kind: "trends", code: "etfs-spark" });
  // 到期日历：一天只变一次，10 分钟轮询足够（换来的是「当月还剩几天」永不陈旧）
  const { data: expiryView } = usePolling(() => api.expiry(), 600_000);

  // 四类数据都走「先渲染本地缓存 → 同时发真实请求」的 SWR，切标的时首帧即有内容。
  const { data: ctx, refresh: refreshCtx, cacheAt: ctxCache } = usePolling(
    () => api.etf(etfCode, 1), 10_000, [etfCode], { kind: "ctx", code: etfCode });
  const { data: chain, refresh: refreshChain, cacheAt: chainCache } = usePolling(
    () => api.chain(etfCode, month ?? undefined), 15_000, [etfCode, month],
    { kind: "chain", code: etfCode, extra: month ?? null });
  const { data: strategy, refresh: refreshStrategy, cacheAt: planCache } = usePolling(
    () => api.strategy(etfCode, 0, 1), 30_000, [etfCode], { kind: "plan", code: etfCode });
  const { data: revSignals, refresh: refreshSig, cacheAt: sigCache } = usePolling(
    () => api.reversalSignals(etfCode), 30_000, [etfCode], { kind: "sig", code: etfCode });
  // 自校准命中率（全局口径，与标的无关），10 分钟一轮即可
  const { data: calib } = usePolling(() => api.calibration(), 600_000);

  // 合约：优先链内按 secid 定位，保持实时刷新
  const contract = useMemo(() => {
    if (!chain?.contracts) return null;
    return chain.contracts.find((c: any) => c.secid === contractSecid) ?? null;
  }, [chain, contractSecid]);

  // 跨标的优选点选：目标合约先存起来，等链切到该标的后再选中（切标的会重置合约/月份）
  const pendingPick = useRef<{ code: string; secid: string; month: number | null } | null>(null);
  useEffect(() => {
    const p = pendingPick.current;
    if (p && p.code === etfCode) {
      setMonth(p.month ?? null);
      setContractSecid(p.secid);
      pendingPick.current = null;
      return;
    }
    setMonth(null);
    setContractSecid(null);
  }, [etfCode]);

  // 缓存里的合约可能已不在最新链里（换了月份/已到期）：链就绪后回退到默认优选合约
  const chainCode = chain?.etf?.code;
  useEffect(() => {
    if (!chainCode || chainCode !== etfCode || !contractSecid) return;
    const exists = (chain?.contracts ?? []).some((c: any) => c.secid === contractSecid);
    if (exists) return;
    const secid = chain?.best?.recommended?.secid
      ?? chain?.ranked?.call?.[0]?.secid ?? chain?.ranked?.put?.[0]?.secid ?? null;
    setContractSecid(secid);
  }, [chainCode, etfCode, chain, contractSecid]);

  // 未选合约时，自动选中该标的的优选（近月）合约，避免详情区空白
  useEffect(() => {
    if (contractSecid || !chain) return;
    // 优先选中「结合趋势」的主推合约，其次回退到旧版排序
    const secid = chain.best?.recommended?.secid
      ?? chain.ranked?.call?.[0]?.secid ?? chain.ranked?.put?.[0]?.secid;
    if (secid) setContractSecid(secid);
  }, [chain, contractSecid]);

  const quotes: Record<string, any> = useMemo(() => {
    const m: Record<string, any> = {};
    (etfQuotes?.items ?? []).forEach((q: any) => (m[q.code] = q));
    return m;
  }, [etfQuotes]);

  const etfs = universe?.etfs ?? [];
  // 股指品种 IO/HO/MO（期权）+ IC（仅期货）与 ETF 并列进左栏；选中后整页数据源切到 /strategy/{symbol}
  // 与 /etf/{symbol}（后端已按代码前缀分流），组件不变。
  const futOpts: any[] = universe?.fut_opts ?? [];
  const isFut = futOpts.some((f: any) => f.code === etfCode);

  // 标的被下线后的兜底：localStorage 里可能还存着已删除的代码（如 159919），
  // 直接请求会拿到 {error} 让整页空白。清单到达后校验，失效就回落默认标的。
  const DEFAULT_ETF = "510300";
  useEffect(() => {
    if (!etfs.length) return;
    if (etfs.some((e: any) => e.code === etfCode)) return;
    if (futOpts.some((f: any) => f.code === etfCode)) return;
    setEtfCode(DEFAULT_ETF);
  }, [etfs, futOpts, etfCode, setEtfCode]);
  // 标的名称：先查 ETF 清单，再查股指期权清单（两者共用同一个选中状态）
  const nameOf = (code: string) =>
    etfs.find((e: any) => e.code === code)?.name
    ?? futOpts.find((f: any) => f.code === code)?.name ?? code;
  const trendsMap = spark?.items ?? {};

  // 切换标的时 usePolling 会保留上一只的数据，直到新请求返回；用 code 校验避免显示旧数据
  const ctxReady = !!ctx && ctx.etf?.code === etfCode;
  const chainReady = !!chain && chain.etf?.code === etfCode;
  // 主力资金序列 <2 点时不成序列，避免渲染成一根孤立柱子
  const flowSeries = (ctx?.flow?.length ?? 0) >= 2 ? ctx!.flow : EMPTY;

  // 图上横线的数据源：支撑/压力取自 /etf/{code} 的 support/resistance，
  // 止盈/止损取自策略计划的 entry_break/stop/target。
  // 开关的 disabled 就按「有没有数据 + 有没有昨收」判定——没有昨收就换不成百分比。
  const support: number[] = (ctxReady ? ctx?.support : null) ?? EMPTY;
  const resistance: number[] = (ctxReady ? ctx?.resistance : null) ?? EMPTY;
  // 计划横线按数值记忆：strategy 每次轮询都是新对象，直接写对象字面量会让主图每次渲染都重建
  const lv = strategy?.levels;
  const chartPlan = useMemo(
    () => (lv ? { entry: lv.entry_break, stop: lv.stop, target: lv.target } : null),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [!!lv, lv?.entry_break, lv?.stop, lv?.target],
  );
  const planLevels = support.length + resistance.length > 0;
  // 昨收优先用分时自带的（回退到上一交易日时它才非空），否则退到报价昨收——
  // 只认 trends_prev_close 会让按钮在盘前/回退场景永远 disabled（用户点了没反应）。
  const linePrevClose = ctx?.trends_prev_close ?? ctx?.quote?.prev_close ?? null;
  const planLines = !!(ctxReady && strategy?.levels
    && [strategy.levels.entry_break, strategy.levels.stop, strategy.levels.target]
      .some((v: any) => v != null)
    && linePrevClose != null && Number(linePrevClose) > 0);

  // 预取：交易时段 + 可见 + 当前标的四类已就绪后，按左栏顺序串行预热其它标的
  usePrefetch(etfCode, etfs.map((e: any) => e.code), !!(ctxReady && chainReady && strategy && revSignals));

  // 顶栏「刷新」广播 → 只刷本页主数据（其它页有自己的订阅）
  useEffect(() => onGlobalRefresh(() => { void refreshCtx(); }), [refreshCtx]);

  // 变盘告警**到达即刷**：左栏角标取自 /api/directions，原本只靠 60s 轮询，
  // 信号出现后要等下一轮才显示（用户会以为没算进去、只能刷新页面）。
  // 命中当前标的时顺带刷中栏（ctx + 策略），避免「角标说变盘看空、策略卡还看多」。
  useEffect(() => subscribeAlerts((a: any) => {
    if (a?.type !== "reversal" || a.backfill) return;
    void refreshDirs();
    const hit = a.etf_code === etfCode
      || (a.items ?? []).some((it: any) => it?.code === etfCode);
    if (hit) {
      void refreshCtx();
      void refreshStrategy();
    }
  }), [etfCode, refreshDirs, refreshCtx, refreshStrategy]);

  // 切月份：立刻给「切换中」反馈，等真实网络请求回来再撤；最短展示 450ms，避免秒回时看不到反馈
  const onMonth = async (m: number) => {
    if (m === (month ?? chain?.front_month)) return;
    setMonth(m);
    setMonthPending(true);
    const t0 = Date.now();
    try {
      await refreshChain();
    } catch {
      /* 失败也会在轮询里重试，这里只负责收起加载态 */
    } finally {
      const left = 450 - (Date.now() - t0);
      if (left > 0) await new Promise((r) => setTimeout(r, left));
      setMonthPending(false);
    }
  };

  // 点选合约（含右侧优选列表）：若该合约不在当前展示月份，顺带切月，否则详情区找不到合约会显示空白
  const onPickContract = (c: any) => {
    setContractSecid(c.secid);
    if (c.month != null && c.month !== (month ?? chain?.front_month)) void onMonth(c.month);
  };

  // 右侧「全市场优选」可能是别的标的：记下目标合约，切标的后由上面的 effect 选中
  const onPickTop = (p: any) => {
    const code = p.etf_code as string;
    if (code === etfCode) {
      setContractSecid(p.secid);
      if (p.month != null && p.month !== (month ?? chain?.front_month)) void onMonth(p.month);
      return;
    }
    pendingPick.current = { code, secid: p.secid, month: p.month ?? null };
    setEtfCode(code);
  };

  return (
    <main
      className="board-grid grid grid-cols-1 gap-3 p-3 transition-[grid-template-columns] duration-200 ease-[cubic-bezier(0.22,1,0.36,1)]"
      style={{ "--board-left": leftCollapsed ? "3rem" : "340px", "--board-right": rightCollapsed ? "3rem" : "360px" } as CSSProperties}
    >
      {/* 左：标的 */}
      <div className="flex min-h-0 flex-col lg:sticky lg:top-[52px] lg:h-[calc(100vh-3.25rem)]">
        {leftCollapsed ? (
          <PanelRail side="left" label="标的" onExpand={() => setLeftCollapsed(false)} />
        ) : (
          <>
            <div className="mb-1 flex h-6 items-center justify-end">
              <button
                type="button"
                onClick={() => setLeftCollapsed(true)}
                aria-label="收起左侧面板"
                title="收起左侧面板"
                className="rounded-md p-1 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <PanelLeftClose className="size-3.5" />
              </button>
            </div>
            <div className="min-h-0 flex-1">
              <EtfList etfs={etfs} futOpts={futOpts} quotes={quotes} trendsMap={trendsMap} directions={directions?.items ?? {}} indices={indices?.items ?? []} selected={etfCode} onSelect={setEtfCode} />
            </div>
          </>
        )}
      </div>

      {/* 中：标的行情 + 合约详情 + T报价 */}
      <div className="flex min-w-0 flex-col gap-3">
        <Frame spacing="sm">
          <FramePanel dense>
            <FrameHeader className="flex-row items-center justify-between">
              <div className="flex items-center gap-2">
                <FrameTitle className="text-sm">
                  {ctxReady ? (ctx!.etf?.name ?? nameOf(etfCode)) : nameOf(etfCode)}
                </FrameTitle>
                <Badge variant="outline" className="shrink-0">{etfCode}</Badge>
                {ctxReady && ctx?.quote ? (
                  <>
                    <span className="text-sm font-semibold tnum">{nf(ctx.quote.price, 3)}</span>
                    <span className={`text-xs tnum ${clsUpDown(ctx.quote.pct)}`}>{pct(ctx.quote.pct)}</span>
                  </>
                ) : (
                  <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
                    <Loader2 className="size-3 animate-spin" />加载中…
                  </span>
                )}
              </div>
              <div className="flex shrink-0 items-center gap-1.5">
                <LineToggle label="支撑/压力" title="标的图：显示/隐藏支撑压力线" swatch={SWATCH_LEVELS}
                  active={lineToggles.levels}
                  disabled={!planLevels} onToggle={() => setLineToggles((v) => ({ ...v, levels: !v.levels }))} />
                <LineToggle label="止盈/止损" title="标的图：入场/止盈/止损三条计划横线" swatch={SWATCH_PLAN}
                  active={lineToggles.plan} disabled={!planLines}
                  onToggle={() => setLineToggles((v) => ({ ...v, plan: !v.plan }))} />
                <button
                  type="button"
                  onClick={() => setChartExpanded((v) => !v)}
                  aria-pressed={chartExpanded}
                  title={chartExpanded ? "还原分时图高度" : "分时图高度 ×2"}
                  className="rounded-md border border-border p-1 text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  {chartExpanded ? <Minimize2 className="size-3" aria-hidden /> : <Maximize2 className="size-3" aria-hidden />}
                </button>
                <CacheTag meta={ctxCache} onRefresh={refreshCtx} />
              </div>
            </FrameHeader>
            {ctxReady && (
              <div className="flex flex-wrap items-center gap-2 px-3 pb-1 text-[11px] text-muted-foreground">
                <MiniKv k="量能" v={`${ctx.volume?.ratio ?? "—"}x`} tone={ctx.volume?.is_surge ? "warn" : undefined} />
                <MiniKv k="主力5分" v={signedFlow(ctx.flow_main_delta5m)} tone={ctx.flow_main_delta5m == null ? undefined : ctx.flow_main_delta5m > 0 ? "up" : "down"} />
                <MiniKv k="主力累计" v={signedFlow(ctx.flow_snapshot?.main)} tone={ctx.flow_snapshot?.main == null ? undefined : ctx.flow_snapshot.main > 0 ? "up" : "down"} />
                {ctx.flow_day && ctx.flow_day < new Date().toLocaleDateString("en-CA") && (
                  <Badge variant="warning-light" title="当日资金流序列尚未产生，展示的是最近一个有数据的交易日">昨日资金流</Badge>
                )}
                <MiniKv k="MACD" v={ctx.macd?.cross === "golden" ? "金叉" : ctx.macd?.cross === "dead" ? "死叉" : "—"} />
                <MiniKv k="背离15分" v={ctx.macd15?.divergence?.type === "bullish" ? "底背离" : ctx.macd15?.divergence?.type === "bearish" ? "顶背离" : "无"} />
                {!isFut && <MiniKv k="关联指数" v={`${ctx.track?.quote?.name ?? ""} ${pct(ctx.track?.quote?.pct)}`} />}
                {isFut && (
                  <>
                    <MiniKv k="期指基差" v={strategy?.basis ? `${nf(strategy.basis.price, 2)}（${pct(strategy.basis.basis_pct)}）` : "—"} />
                    <MiniKv k="IV/RV" v={(strategy as any)?.iv_rv?.ratio ?? "—"} />
                    <MiniKv k="乘数" v="100 元/点" />
                  </>
                )}
              </div>
            )}
            <div className="px-1 pb-2">
              <ErrorBoundary label="标的行情图">
                {!ctxReady ? (
                  <Skeleton className="h-[240px] w-full" />
                ) : ctx?.trends?.length ? (
                  <IntradayChart id="main-chart" trends={ctx.trends} indexTrends={ctx.track?.trends ?? EMPTY} indexFlow={ctx.track?.flow ?? EMPTY} indexName={ctx.track?.quote?.name ?? ""} flow={flowSeries} signals={revSignals?.items ?? EMPTY} prevClose={ctx.trends_prev_close ?? ctx.quote?.prev_close ?? null} indexPrevClose={ctx.track?.trends_prev_close ?? ctx.track?.quote?.prev_close ?? null}
                    name={isFut ? "指数" : "标的ETF"}
                    flowStyle={isFut ? "bar" : "line"}
                    flowLabel={isFut ? "指数主力净流入" : "标的主力净流入"}
                    support={support} resistance={resistance}
                    plan={chartPlan}
                    showLevels={lineToggles.levels} showPlan={lineToggles.plan}
                    height={chartExpanded ? 460 : 230} />
                ) : (
                  <Skeleton className="h-[240px] w-full" />
                )}
              </ErrorBoundary>
            </div>
          </FramePanel>
        </Frame>

        <Frame spacing="sm">
          <FramePanel dense>
            {chainReady && chain?.options === false ? (
              // 中证500 没有股指期权：这个位置改列全部 IC 期货合约（含基差/持仓）
              <FutContracts items={chain?.futures ?? []} spot={chain?.underlying?.price} />
            ) : chainReady && !chain.error ? (
              <OptionTQuote
                chain={chain}
                cacheTag={<CacheTag meta={chainCache} onRefresh={refreshChain} />}
                month={month ?? chain.front_month}
                onMonth={onMonth}
                loading={monthPending}
                selectedSecid={contractSecid ?? undefined}
                onSelect={onPickContract}
              />
            ) : <Skeleton className="m-3 h-[380px]" />}
          </FramePanel>
        </Frame>

        <ContractDetail contract={contract} code={etfCode} etfTrends={ctxReady ? (ctx?.trends ?? EMPTY) : EMPTY}
          underName={isFut ? "指数" : "标的ETF"}
          flow={ctxReady ? flowSeries : EMPTY} etfPrevClose={ctxReady ? (ctx?.trends_prev_close ?? ctx?.quote?.prev_close ?? null) : null} />

        {/* 决策区统一放在合约分时图下面：策略与仓位 / 今日推荐合约 / 标的点位 → 优选合约 → 大模型决策 */}
        <PlanCard bundle={strategy} onSelect={onPickContract} />

        <Frame spacing="sm">
          <FramePanel dense>
            <Fold
              title="优选合约（按日内性价比）"
              hint={strategy?.candidates?.length
                ? <span className="shrink-0 text-[10px] font-normal text-muted-foreground tnum">共 {strategy.candidates.length} 只</span>
                : undefined}
              right={(
                <div className="flex items-center gap-2">
                  {(calib?.n ?? 0) >= 10 && (
                    <span className="text-[10px] text-muted-foreground tnum"
                      title="全部标的主选合约的历史命中率（按 5/15/30 分钟收益评价，明细见 /api/calibration）">
                      近{calib.n}笔命中{Math.round(calib.hit ?? 0)}%
                    </span>
                  )}
                  <CacheTag meta={planCache} onRefresh={refreshStrategy} />
                </div>
              )}
            >
              <div className="px-3 pb-3">
                {!strategy ? (
                  <CandidateSkeleton />
                ) : strategy?.candidates?.length ? (
                  <CandidateList candidates={strategy.candidates} selected={contractSecid ?? undefined} onSelect={onPickContract} />
                ) : strategy?.error ? (
                  <EmptyState title="策略数据获取失败" hint="接口未返回，稍后自动重试" />
                ) : (
                  <EmptyState title="暂无优选合约" hint="期权链未返回可用报价或 Delta" />
                )}
              </div>
            </Fold>
          </FramePanel>
        </Frame>

        <LlmPanel code={etfCode} />
      </div>

      {/* 右：决策栏 */}
      <div className="flex min-h-0 flex-col">
        {rightCollapsed ? (
          <PanelRail side="right" label="决策" onExpand={() => setRightCollapsed(false)} />
        ) : (
          <div className="mb-1 flex h-6 items-center justify-start">
            <button
              type="button"
              onClick={() => setRightCollapsed(true)}
              aria-label="收起右侧面板"
              title="收起右侧面板"
              className="rounded-md p-1 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <PanelRightClose className="size-3.5" />
            </button>
          </div>
        )}
        <div className={`flex flex-col gap-3 ${rightCollapsed ? "hidden" : ""}`}>
        <AlertCenter onSelect={setEtfCode} />

        <Frame spacing="sm">
          <FramePanel dense>
            <FrameHeader className="flex-row items-center justify-between">
              <div className="flex min-w-0 items-center gap-1.5">
                <FrameTitle className="text-xs">优选合约（全市场）</FrameTitle>
                <span className="truncate text-[10px] text-muted-foreground" title="综合分 = 性价比×0.6 + 波动×0.4；性价比与单标的优选同一套打分，波动=有效杠杆+当日涨幅+标的振幅">
                  性价比×0.6 + 波动×0.4
                </span>
              </div>
              <CacheTag meta={null} onRefresh={refreshPicks} />
            </FrameHeader>
            <div className="px-2 pb-3">
              <TopPicksList items={topPicks?.items ?? []} loading={!topPicks} onSelect={onPickTop} selectedSecid={contractSecid} />
            </div>
          </FramePanel>
        </Frame>

        <Frame spacing="sm">
          <FramePanel dense>
            <FrameHeader className="flex-row items-center justify-between">
              <FrameTitle className="text-xs">日内方向与信号</FrameTitle>
              <CacheTag meta={mergeCache(planCache, sigCache)} onRefresh={() => Promise.all([refreshStrategy(), refreshSig()]).then(() => {})} />
            </FrameHeader>
            <div className="flex flex-col gap-2 px-3 pb-3">
              {strategy ? <BiasBar bias={strategy.bias} /> : <BiasBarSkeleton />}
              <SignalFeed signals={strategy?.signals ?? []} loading={!strategy} />
            </div>
          </FramePanel>
        </Frame>

        <Frame spacing="sm">
          <FramePanel dense>
            <FrameHeader className="flex-row items-center justify-between">
              <FrameTitle className="text-xs">Kronos 量化预测</FrameTitle>
              <span className="text-[10px] text-muted-foreground">5分钟级</span>
            </FrameHeader>
            <KronosPredict secid={ctx?.etf?.secid ?? etfs.find((e: any) => e.code === etfCode)?.secid ?? "1.510300"} klt={5} />
          </FramePanel>
        </Frame>

        {/* 股指期权走中金所自己的到期规则（第三个星期五），ETF 的到期日历对它无意义 */}
        {!isFut && <ExpiryCalendar view={expiryView} />}
        </div>
      </div>
    </main>
  );
}

function usePersistedFlag(key: string) {
  const [on, setOn] = useState(() => {
    try {
      return localStorage.getItem(key) === "1";
    } catch {
      return false;
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(key, on ? "1" : "0");
    } catch {
      /* 隐私模式等场景忽略：仅丢失持久化，不影响交互 */
    }
  }, [key, on]);
  return [on, setOn] as const;
}

/** 对象型偏好（如 {levels, plan}）：按 JSON 存取，缺键回落默认值。 */
function usePersistedFlag2<T extends Record<string, boolean>>(key: string, defaults: T) {  const [val, setVal] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(key);
      return raw ? { ...defaults, ...JSON.parse(raw) } : defaults;
    } catch {
      return defaults;
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(key, JSON.stringify(val));
    } catch {
      /* 同上 */
    }
  }, [key, val]);
  return [val, setVal] as const;
}

/** 任意可 JSON 序列化的状态：用于跨页切换/刷新保持选中标的、月份、合约。 */
function usePersistedState<T>(key: string, initial: T) {
  const [val, setVal] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(key);
      return raw == null ? initial : (JSON.parse(raw) as T);
    } catch {
      return initial;
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(key, JSON.stringify(val));
    } catch {
      /* 隐私模式忽略 */
    }
  }, [key, val]);
  return [val, setVal] as const;
}

function PanelRail({ side, label, onExpand }: { side: "left" | "right"; label: string; onExpand: () => void }) {
  return (
    <div className="flex items-center gap-2 rounded-lg border border-border/60 bg-card/40 px-2 py-1.5 lg:h-full lg:w-12 lg:flex-col lg:justify-start lg:gap-3 lg:px-0 lg:py-2">
      <button
        type="button"
        onClick={onExpand}
        aria-label={`展开${label}面板`}
        title={`展开${label}面板`}
        className="flex size-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        {side === "left" ? <PanelLeftOpen className="size-4" /> : <PanelRightOpen className="size-4" />}
      </button>
      <span className="text-[10px] text-muted-foreground lg:[writing-mode:vertical-rl]">{label}</span>
    </div>
  );
}

/** 合并两个缓存元信息：取更旧的一条，任一为昨日则按昨日展示。 */
function mergeCache(a: CacheMeta | null, b: CacheMeta | null): CacheMeta | null {
  if (!a) return b;
  if (!b) return a;
  if (a.day !== b.day) return a.day < b.day ? a : b;
  return a.ts <= b.ts ? a : b;
}

/** 数据新鲜度角标：每次刷新后都显式标注——缓存 = 红色，实时 = 绿色。 */
function CacheTag({ meta, onRefresh }: { meta: CacheMeta | null; onRefresh?: () => Promise<void> }) {
  const [busy, setBusy] = useState(false);
  const stale = !!meta && meta.day !== dayKey();
  const pending = busy;
  const badge = pending ? (
    <Badge variant="destructive-light" className="shrink-0" title="正在重新获取实时数据">
      <Loader2 className="size-3 animate-spin" aria-hidden />
      刷新中
    </Badge>
  ) : meta ? (
    <Badge
      variant="destructive-light"
      className="shrink-0"
      title={stale ? "展示的是上一交易日缓存的数据，点击立即刷新" : "本地缓存数据，点击立即刷新"}
    >
      {stale ? "缓存·昨日" : "缓存"} {hhmmss(new Date(meta.ts))}
    </Badge>
  ) : (
    <Badge variant="success-light" className="shrink-0" title="当前展示的是本轮刷新取回的实时数据">
      实时
    </Badge>
  );

  if (!onRefresh) return badge;
  return (
    <button
      type="button"
      onClick={async () => {
        if (busy) return;
        setBusy(true);
        try {
          await onRefresh();
        } finally {
          setBusy(false);
        }
      }}
      disabled={pending}
      aria-label={pending ? "正在刷新，等待实时数据" : "点击立即获取实时数据"}
      className="shrink-0 rounded-md transition-colors hover:opacity-80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-wait"
    >
      {badge}
    </button>
  );
}

function MiniKv({ k, v, tone }: { k: string; v: any; tone?: "up" | "down" | "warn" }) {
  return (
    <span className="inline-flex items-center gap-1 rounded bg-muted/40 px-1.5 py-0.5">
      <span className="text-muted-foreground"><Tip label={k} /></span>
      <span className={tone === "up" ? "text-up" : tone === "down" ? "text-down" : tone === "warn" ? "text-warning" : ""}>{v}</span>
    </span>
  );
}
