import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Loader2, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { Frame, FramePanel, FrameHeader, FrameTitle } from "@/components/reui/frame";
import { Badge } from "@/components/reui/badge";
import { EmptyState, Skeleton, Tabs } from "@/components/ui/misc";
import { FlowListPanel } from "@/components/FlowListPanel";
import { DayTag, isLiveDay } from "@/components/DayTag";
import { BoardFlowChart, type FlowLine } from "@/components/charts";
import { BoardDetailDrawer } from "@/components/BoardDetailDrawer";
import { api } from "@/lib/api";
import { usePolling } from "@/lib/usePolling";
import { onGlobalRefresh } from "@/lib/refresh";
import { joinCodes, parseCodes, readHashParams, writeHashParams } from "@/lib/urlState";
import { afterPreOpen, dateWithWeek, signedFlow, todayStr } from "@/lib/format";
import { cn } from "@/lib/utils";

/** 与 stock-webui 一致的五个口径 */
const TYPES = [
  { key: "concept", label: "概念" },
  { key: "industry", label: "行业" },
  { key: "index", label: "指数" },
  { key: "hot", label: "热门" },
  { key: "custom", label: "自定义" },
];
type SortKey = "flow" | "pct" | "speed";
const SERIES_TTL = 90_000;

type Bar = {
  code: string; name: string; type: string;
  flow: number; pct: number | null; speed?: number; watch?: boolean;
  /** 名称的拼音首字母（后端给），用于拼音搜索 */
  py?: string;
};
type CacheEntry = { points: [string, number][]; pct: number | null; name: string; at: number };

/** 板块资金分时：复刻 stock-webui /board-flow 的列表 + 叠加曲线交互。 */
export function BoardFlowPage() {
  const initial = useMemo(() => readHashParams(), []);
  const initCodes = parseCodes(initial.codes);
  const [type, setType] = useState(initial.type ?? "concept");
  /** 资金口径：east = 东财 f62（主力=超大+大）/ wind = 逐笔 ≥70 万（剔除开盘竞价） */
  const [mode, setMode] = useState<"east" | "wind">("east");
  // 「指数」tab 不支持 Wind（成分股接口对指数拿不到），自动回落东财
  const effMode = type === "index" ? "east" : mode;
  const [search, setSearch] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("flow");
  const [sortDir, setSortDir] = useState(-1);
  const [date, setDate] = useState(initial.date ?? "");
  const [selected, setSelected] = useState<Record<string, boolean>>(
    () => Object.fromEntries(initCodes.map((c) => [c, true])),
  );
  const [hoverCode, setHoverCode] = useState<string | null>(null);
  const [detailCode, setDetailCode] = useState<{ code: string; name: string } | null>(null);
  const [cacheVer, setCacheVer] = useState(0);
  // 手动刷新时递增：清空分时缓存后要让拉取 effect 重新跑（deps 里没有缓存内容）
  const [refreshTick, setRefreshTick] = useState(0);
  const cache = useRef<Record<string, CacheEntry>>({});
  const inflight = useRef<Set<string>>(new Set());
  // 用户在哪些口径上手动改过勾选（决定是否还套用默认 Top10+Bottom5）
  const touched = useRef<Record<string, boolean>>({});
  // URL 里带了勾选就视为「用户已有选择」，不再套用默认 Top10
  useEffect(() => {
    if (initCodes.length) touched.current[type] = true;
  }, []);

  const { data: health } = usePolling(() => api.boardHealth(), 60_000, [], { kind: "ctx", code: "board-health" });
  const trading = !!health?.trading;
  const interval = trading ? 30_000 : 300_000;

  const { data: datesView } = usePolling(() => api.boardDates(60), 600_000, [], { kind: "ctx", code: "board-dates" });
  const dates: string[] = datesView?.dates ?? [];

  // 排行响应里记下「这次请求用的日期」：usePolling 切 deps 时不清空旧 data，
  // 直接拿 rankView.date 回填「今日」的落点，会把日期弹回上一次的历史日期，
  // 请求还没来得及发就被改回历史 → 点「今日」看起来毫无反应。
  const { data: rankView, refresh: refreshRank } = usePolling(
    () => api.boardRank("all", date || undefined, effMode).then((d) => ({ ...d, _asked: date })),
    interval,
    [interval, date, effMode],
    // 口径进了缓存键：否则切到 Wind 时 IndexedDB 会先喂回东财的旧数据
    { kind: "brank", code: `all:${effMode}:${date || "latest"}` },
  );
  const { data: indexView, refresh: refreshIndex } = usePolling(
    () => api.boardIndexRank(date || undefined),
    interval,
    [interval, date],
    { kind: "brank", code: `index:${date || "latest"}` },
  );
  const { data: watchView, refresh: refreshWatch } = usePolling(
    () => api.boardWatch(), 300_000, []);
  // 财联社「热门」：热门行业/概念 + 盘面异动，后端模糊匹配到本地板块。
  // forceHot：只有「刷新」按钮才置 true → 请求带 force=1 绕过后端缓存回源财联社
  const forceHot = useRef(false);
  const { data: hotView, refresh: refreshHot } = usePolling(
    // 不挂 IndexedDB 缓存：热门板块变化快，前端缓存会让页面停在几分钟前的名单
    () => api.boardClsHot(date || undefined, forceHot.current), 120_000, [date]);
  // 刷新按钮的忙碌态：至少让用户看到图标在转，而不是「点了没反应」
  const [refreshing, setRefreshing] = useState(false);
  // 自选的本地真实来源：点击 + 立即翻转（乐观更新），服务端返回后以 watchView 对齐
  const [watchItems, setWatchItems] = useState<{ code: string; name: string }[]>([]);
  const [watchBusy, setWatchBusy] = useState<Record<string, boolean>>({});
  useEffect(() => {
    if (Array.isArray(watchView?.items)) setWatchItems(watchView.items);
  }, [watchView]);
  const watchSet = useMemo(() => new Set(watchItems.map((i) => i.code)), [watchItems]);

  // 下拉不再提供「最新」伪选项：进入时把日期固定到库内最新交易日。
  // 只认「无日期请求」回来的响应，历史日期的旧响应不能当落点（见上面 _asked 注释）。
  const latestDate = rankView?._asked === "" ? rankView?.date : "";
  /** 今天是「盘前之后的工作日」：此时「今日」= 当天本身（数据 09:30 才开始采集） */
  const isTodayMode = afterPreOpen();
  useEffect(() => {
    if (date) return;
    // 09:15 之后点「今日」落到当天：哪怕库里最新还停在昨天，也要先切到今天
    if (isTodayMode) {
      setDate(todayStr());
      return;
    }
    if (latestDate) setDate(latestDate);
  }, [date, latestDate, isTodayMode]);

  // 下拉选项：把「今天」补进去（今天可能还没采集到，不在 dates 里）
  const dateOptions = useMemo(() => {
    const base = [...dates].sort().reverse();
    return date && !base.includes(date) ? [date, ...base] : base;
  }, [dates, date]);

  const refresh = useCallback(() => {
    cache.current = {};
    setRefreshTick((t) => t + 1);
    setRefreshing(true);
    forceHot.current = true;
    void Promise.all([refreshRank(), refreshIndex(), refreshHot()])
      .finally(() => { forceHot.current = false; setRefreshing(false); });
  }, [refreshRank, refreshIndex, refreshHot]);

  useEffect(() => onGlobalRefresh(() => refresh()), [refresh]);

  const allBoards: Bar[] = useMemo(
    () => (rankView?.items ?? []).map((r: any) => ({
      code: r.code, name: r.name, type: r.type, flow: r.flow ?? 0, pct: r.pct ?? null,
      speed: r.speed ?? 0, watch: watchSet.has(r.code), py: r.py,
    })),
    [rankView, watchSet],
  );
  const indexRows: Bar[] = useMemo(
    () => (indexView?.items ?? []).map((r: any) => ({
      code: r.code, name: r.name, type: "index", flow: r.flow ?? 0, pct: r.pct ?? null,
      speed: 0, py: r.py, watch: watchSet.has(r.code),
    })),
    [indexView, watchSet],
  );

  /** 「热门」：财联社匹配结果；拉不到时回退到 |主力净流入| 前 100 */
  const hotRows: Bar[] = useMemo(() => {
    const byCode = new Map(allBoards.map((b) => [b.code, b] as const));
    const items = (hotView?.items ?? []).map((r: any) => {
      const base = byCode.get(r.code);
      return {
        code: r.code,
        name: r.name || base?.name || r.code,
        type: r.type || base?.type || "concept",
        flow: r.flow ?? base?.flow ?? 0,
        pct: r.pct ?? base?.pct ?? null,
        speed: base?.speed ?? 0,
        watch: watchSet.has(r.code),
        py: base?.py,
      } as Bar;
    });
    if (items.length) return items;
    return [...allBoards].sort((a, b) => Math.abs(b.flow) - Math.abs(a.flow)).slice(0, 100);
  }, [hotView, allBoards, watchSet]);

  /** 当前口径的原始列表（未搜索、未排序之外的过滤） */
  const baseRows: Bar[] = useMemo(() => {
    if (type === "index") return indexRows;
    if (type === "hot") return hotRows;
    if (type === "custom") {
      const byCode = new Map([...allBoards, ...indexRows].map((b) => [b.code, b] as const));
      return watchItems
        .map((w) => byCode.get(w.code) ?? { code: w.code, name: w.name, type: "concept", flow: 0, pct: null, watch: true })
        .map((b: any) => ({ ...b, watch: true }));
    }
    return allBoards.filter((b) => b.type === type);
  }, [type, allBoards, indexRows, watchItems, hotRows]);

  /** 搜索 + 排序后的可见列表 */
  const rows: Bar[] = useMemo(() => {
    const kw = search.trim();
    let list = baseRows;
    if (kw) {
      const k = kw.toLowerCase();
      list = list.filter((b) =>
        b.name.includes(kw)
        || b.code.toLowerCase().includes(k)
        || (b.py ?? "").includes(k));
    }
    const dir = sortDir;
    return [...list].sort((a, b) => {
      const va = sortKey === "pct" ? (a.pct ?? 0) : sortKey === "speed" ? (a.speed ?? 0) : a.flow;
      const vb = sortKey === "pct" ? (b.pct ?? 0) : sortKey === "speed" ? (b.speed ?? 0) : b.flow;
      return (va - vb) * dir;
    });
  }, [baseRows, search, sortKey, sortDir]);

  const rowsKey = rows.map((r) => r.code).join(",");
  const baseKey = useMemo(() => baseRows.map((r) => r.code).sort().join(","), [baseRows]);

  // 默认勾选：概念/行业 = 流入 Top10 + 流出末 5；指数/热门/自定义 = 全选。
  // 只在「集合本身」变化时重置（排序/搜索不算），且手动改过就只做存在性过滤。
  useEffect(() => {
    if (!rows.length) return;
    const codes = new Set(rows.map((r) => r.code));
    if (touched.current[type]) {
      setSelected((prev) => Object.fromEntries(
        Object.keys(prev).filter((c) => codes.has(c)).map((c) => [c, true])));
      return;
    }
    let saved: string[] = [];
    try {
      saved = JSON.parse(localStorage.getItem(`board.flow.sel.${type}`) || "[]");
    } catch {
      saved = [];
    }
    const kept = (saved || []).filter((c) => codes.has(c));
    if (kept.length) {
      setSelected(Object.fromEntries(kept.map((c) => [c, true])));
      return;
    }
    if (type === "index" || type === "hot" || type === "custom") {
      setSelected(Object.fromEntries(rows.map((r) => [r.code, true])));
      return;
    }
    const pos = rows.filter((r) => r.flow >= 0).slice(0, 10);
    const neg = rows.filter((r) => r.flow < 0).slice(-5);
    setSelected(Object.fromEntries([...pos, ...neg].map((r) => [r.code, true])));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [baseKey, type]);

  const selectedCodes = useMemo(
    () => rows.filter((r) => selected[r.code]).map((r) => r.code),
    [rows, selected],
  );
  const selectedKey = selectedCodes.join(",");

  const persist = (next: Record<string, boolean>) => {
    touched.current[type] = true;
    setSelected(next);
    try {
      localStorage.setItem(`board.flow.sel.${type}`, JSON.stringify(
        Object.keys(next).filter((c) => next[c])));
    } catch { /* 隐私模式忽略 */ }
  };

  const toggle = (code: string) => {
    const next = { ...selected };
    if (next[code]) delete next[code];
    else next[code] = true;
    persist(next);
  };

  const batch = (mode: "pos10" | "neg10" | "all" | "none" | "invert") => {
    const cur = sortedBase();
    let next: Record<string, boolean> = {};
    if (mode === "all") cur.forEach((b) => (next[b.code] = true));
    else if (mode === "pos10") cur.filter((b) => b.flow >= 0).slice(0, 10).forEach((b) => (next[b.code] = true));
    else if (mode === "neg10") cur.filter((b) => b.flow < 0).slice(-10).forEach((b) => (next[b.code] = true));
    else if (mode === "invert") cur.forEach((b) => { if (!selected[b.code]) next[b.code] = true; });
    persist(next);
  };

  /** 批量操作按「资金流降序」的口径取前 N（与 stock-webui 一致） */
  const sortedBase = () => [...baseRows].sort((a, b) => b.flow - a.flow);

  // 分时缓存键带日期：切日期必须重新拉（旧日期数据不能复用）
  const keyOf = useCallback((code: string) => `${date}|${effMode}|${code}`, [date, effMode]);

  // 分时数据：按缺失/过期分块拉取（最多 60 个一批），命中缓存直接渲染
  useEffect(() => {
    if (!selectedCodes.length) return;
    const now = Date.now();
    const missing = selectedCodes.filter((c) => {
      const e = cache.current[keyOf(c)];
      return !e || now - e.at > SERIES_TTL;
    }).filter((c) => !inflight.current.has(keyOf(c)));
    if (!missing.length) return;

    (async () => {
      for (let i = 0; i < missing.length; i += 60) {
        const chunk = missing.slice(i, i + 60);
        chunk.forEach((c) => inflight.current.add(keyOf(c)));
        try {
          const d = await api.boardSeries(chunk.join(","), date || undefined,
            type === "index" ? "index" : "concept", effMode);
          const byCode = new Map((d?.series ?? []).map((s: any) => [s.code, s]));
          chunk.forEach((c) => {
            const s: any = byCode.get(c);
            // 即使没有该板块也写一条空记录：区分「没拉到」和「当日无点」，避免一直转圈
            const pts: [string, number][] = [];
            (s?.values ?? []).forEach((v: number | null, idx: number) => {
              if (v != null) pts.push([d.axis[idx], v]);
            });
            cache.current[keyOf(c)] = {
              points: pts, pct: s?.pct ?? null, name: s?.name ?? c, at: Date.now(),
            };
          });
        } catch {
          /* 单块失败静默，下一轮再试 */
        } finally {
          chunk.forEach((c) => inflight.current.delete(keyOf(c)));
        }
      }
      // 即便本次 effect 已被新的日期/标的替换，缓存写入是按日期隔离的，仍要触发一次重渲染
      setCacheVer((v) => v + 1);
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedKey, date, refreshTick, keyOf]);

  // 悬停未勾选的板块：临时拉它的分时，画一条橙色虚线（不改变勾选）
  const [tempLine, setTempLine] = useState<FlowLine | null>(null);
  const ensureTemp = async (b: Bar) => {
    const hit = cache.current[keyOf(b.code)];
    if (hit && Date.now() - hit.at < SERIES_TTL) {
      setTempLine({ code: b.code, name: b.name, points: hit.points });
      return;
    }
    try {
      const d = await api.boardSeries(b.code, date || undefined,
        type === "index" ? "index" : "concept", effMode);
      const s: any = (d?.series ?? [])[0];
      if (!s) return;
      const pts: [string, number][] = [];
      (s.values ?? []).forEach((v: number | null, idx: number) => {
        if (v != null) pts.push([d.axis[idx], v]);
      });
      cache.current[keyOf(b.code)] = { points: pts, pct: s.pct ?? null, name: s.name ?? b.code, at: Date.now() };
      setTempLine({ code: b.code, name: b.name, points: pts });
    } catch {
      /* 忽略 */
    }
  };

  const lines: FlowLine[] = useMemo(() => {
    void cacheVer;
    return selectedCodes
      .map((code) => {
        const e = cache.current[`${date}|${effMode}|${code}`];
        if (!e || !e.points.length) return null;
        const row = rows.find((r) => r.code === code);
        return { code, name: e.name || row?.name || code, points: e.points, pct: row?.pct ?? e.pct };
      })
      .filter(Boolean) as FlowLine[];
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedKey, cacheVer, rowsKey]);

  // 底部标注：优先显示悬停中的那条线，否则显示当前条数
  const activeLine = useMemo(
    () => lines.find((l) => l.code === hoverCode) ?? lines[0] ?? null,
    [lines, hoverCode],
  );
  const activeLast = activeLine?.points.length
    ? activeLine.points[activeLine.points.length - 1][1] : null;

  const toggleCustom = async (b: Bar) => {
    if (watchBusy[b.code]) return;
    const isWatched = watchSet.has(b.code);
    setWatchBusy((m) => ({ ...m, [b.code]: true }));
    // 乐观更新：按钮/「自定义」列表立刻变化，失败再回滚
    setWatchItems((prev) => (isWatched
      ? prev.filter((i) => i.code !== b.code)
      : [...prev, { code: b.code, name: b.name }]));
    try {
      const res = isWatched ? await api.boardWatchRemove(b.code) : await api.boardWatchAdd(b.code, b.name);
      if (res?.error) throw new Error(res.error);
      toast(isWatched ? `已从自定义移除：${b.name}` : `已添加到自定义：${b.name}`);
      void refreshWatch();
    } catch (e: any) {
      setWatchItems((prev) => (isWatched
        ? [...prev, { code: b.code, name: b.name }]
        : prev.filter((i) => i.code !== b.code)));
      toast("自定义操作失败", { description: String(e?.message ?? e) });
    } finally {
      setWatchBusy((m) => ({ ...m, [b.code]: false }));
    }
  };

  // 状态写回 URL（刷新/分享保持）：代码过多时不写，避免超长
  useEffect(() => {
    const codes = Object.keys(selected).filter((c) => selected[c]);
    writeHashParams({
      type,
      date: date || undefined,
      codes: codes.length <= 80 ? joinCodes(codes) : undefined,
    });
  }, [type, date, selected]);

  const last = health?.last;
  const latestDay = last?.date ?? "";
  const live = isLiveDay(date, latestDay);
  /** 已切到今天，但库里最新还停在更早的交易日 → 当天数据还没开始采集 */
  const preOpenToday = !!date && !!latestDay && date > latestDay && date === todayStr();
  const updatedAt = preOpenToday
    ? "今日盘前 · 09:30 开始采集"
    : live
      ? `更新于 ${last?.last_ts?.slice(11) ?? "—"}`
      : `${date} 历史数据`;

  return (
    <main className="grid gap-3 p-3 xl:grid-cols-[360px_minmax(0,1fr)]">
      <FlowListPanel
        title="板块列表"
        countText={`共 ${rows.length} 个 · 已选 ${selectedCodes.length}`}
        rows={rows.map((b) => ({
          code: b.code, name: b.name, flow: b.flow, pct: b.pct ?? null,
          speed: b.speed, type: b.type, watch: watchSet.has(b.code), py: b.py,
        }))}
        selected={selected}
        onToggle={toggle}
        search={search}
        onSearch={setSearch}
        sortKey={sortKey}
        sortDir={sortDir}
        onSort={(k) => {
          if (k === sortKey) setSortDir((d) => -d);
          else { setSortKey(k); setSortDir(-1); }
        }}
        onBatch={batch}
        onDetail={(r) => setDetailCode({ code: r.code, name: r.name })}
        onToggleWatch={(r) => {
          const row = rows.find((x) => x.code === r.code);
          if (row) void toggleCustom(row);
        }}
        watchBusy={watchBusy}
        onHoverRow={(code) => {
          if (!code) { setHoverCode(null); setTempLine(null); return; }
          const on = !!selected[code];
          setHoverCode(on ? code : null);
          if (!on) {
            const b = rows.find((r) => r.code === code);
            if (b) void ensureTemp(b);
          }
        }}
        loading={!rankView}
        emptyTitle={type === "custom" ? "还没有自定义板块" : "无匹配板块"}
        emptyHint={type === "custom" ? "在其他列表点行尾的 ＋ 图标即可加入自定义"
          : preOpenToday ? "今日数据 09:30 开始采集，稍等会自动刷新" : undefined}
        maxHeight="calc(100vh - 19rem)"
      />

      {/* 右：分时图 */}
      <Frame spacing="sm" className="flex min-h-0 flex-col xl:sticky xl:top-[52px] xl:h-[calc(100vh-104px)]">
        <FramePanel dense className="flex min-h-0 flex-1 flex-col">
          <FrameHeader className="flex-row flex-wrap items-center justify-between gap-2 py-1.5">
            <div className="flex items-center gap-2">
              <FrameTitle className="text-sm">主力资金分时</FrameTitle>
              <Tabs tabs={TYPES} value={type} onChange={(k) => { setType(k); setSearch(""); }} />
              <span className="mx-0.5 h-3.5 w-px bg-border" aria-hidden />
              <Tabs
                tabs={[{ key: "east", label: "东财口径" }, { key: "wind", label: "Wind 口径" }]}
                value={mode}
                onChange={(k) => setMode(k as "east" | "wind")}
              />
              <DayTag date={date} latest={latestDay} />
              {mode === "wind" && type === "index" ? (
                <Badge variant="warning-light">指数暂不支持 Wind</Badge>
              ) : null}
              {rankView?.fallback === "no_wind" && mode === "wind" ? (
                <Badge variant="warning-light" title="该日没有 Wind 口径的逐笔数据（自 2026-11-07 起采集）">
                  无 Wind 数据，已用东财
                </Badge>
              ) : null}
            </div>
            <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
              <select
                id="board-flow-date" name="date" aria-label="选择日期"
                value={date}
                onChange={(e) => setDate(e.target.value)}
                className="rounded border border-border bg-card px-1.5 py-0.5 text-[11px] text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              >
                {dateOptions.map((d) => <option key={d} value={d}>{dateWithWeek(d)}</option>)}
              </select>
              <button
                type="button"
                onClick={() => setDate("")}
                title={isTodayMode ? "切到今天" : "切到最近交易日"}
                className="rounded border border-border px-1.5 py-0.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
              >
                今日
              </button>
              <span className="hidden text-muted-foreground tnum sm:inline">{updatedAt}</span>
              <button
                type="button"
                onClick={() => refresh()}
                title="刷新（当前口径 + 热门板块 + 分时）"
                aria-label="刷新"
                disabled={refreshing}
                className="rounded-md border border-border p-1 text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60"
              >
                <RefreshCw className={cn("size-3", refreshing && "animate-spin")} aria-hidden />
              </button>
            </div>
          </FrameHeader>

          <div className="flex min-h-0 flex-1 flex-col px-1 pb-1">
            {!selectedCodes.length ? (
              <EmptyState title="未选择板块" hint="请在左侧勾选，或点「流入Top10」批量激活"
                className="flex-1" />
            ) : !lines.length ? (
              selectedCodes.every((c) => cache.current[`${date}|${c}`]) ? (
                <EmptyState title="所选板块暂无分时数据" hint="换一天或换一批板块试试" className="flex-1" />
              ) : (
                <div className="flex flex-1 items-center justify-center gap-2 text-xs text-muted-foreground">
                  <Loader2 className="size-3.5 animate-spin" aria-hidden /> 加载分时数据…
                </div>
              )
            ) : (
              <div className="min-h-0 flex-1">
                <BoardFlowChart id="board-flow-lines" lines={lines} hoverCode={hoverCode}
                  temp={tempLine} height="100%" onHoverCode={setHoverCode} />
              </div>
            )}
            <div className="mt-1 flex items-center justify-between px-2 text-[10px] text-muted-foreground">
              <span>{lines.length}/{selectedCodes.length} 条线 · 悬停左侧名称预览，行尾按钮看详情</span>
              <span>
                主力净流入（累计，亿元）
                {activeLine && (
                  <> · <span className="text-foreground/80">{activeLine.name}</span>{" "}
                    <span className={cn("tnum", (activeLast ?? 0) >= 0 ? "text-up" : "text-down")}>
                      {signedFlow(activeLast)}
                    </span>
                  </>
                )}
              </span>
            </div>
          </div>
        </FramePanel>
      </Frame>

      <BoardDetailDrawer
        selected={detailCode}
        date={date || latestDate || ""}
        onClose={() => setDetailCode(null)}
      />
    </main>
  );
}
