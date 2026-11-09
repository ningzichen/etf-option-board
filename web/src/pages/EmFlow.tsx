import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { RefreshCw, Settings2 } from "lucide-react";
import { toast } from "sonner";
import { Frame, FramePanel, FrameHeader, FrameTitle } from "@/components/reui/frame";
import { Badge } from "@/components/reui/badge";
import { Dialog, EmptyState, Skeleton, Tabs } from "@/components/ui/misc";
import { FlowListPanel, type SortKey } from "@/components/FlowListPanel";
import { DayTag } from "@/components/DayTag";
import { MoneyLinesChart } from "@/components/charts";
import { BoardDetailDrawer } from "@/components/BoardDetailDrawer";
import { api } from "@/lib/api";
import { usePolling } from "@/lib/usePolling";
import { onGlobalRefresh } from "@/lib/refresh";
import { readHashParams, writeHashParams } from "@/lib/urlState";
import { signedFlow } from "@/lib/format";
import { cn } from "@/lib/utils";

const TARGETS = [
  { key: "concept", label: "概念" },
  { key: "industry", label: "行业" },
  { key: "index", label: "指数" },
  { key: "hot", label: "热门" },
  { key: "custom", label: "自定义" },
];
const DIM_LABEL: Record<string, string> = {
  inst: "机构（超大单）", large: "大单", medium: "中户", retail: "散户",
};
/** 图表右侧标签用的短名（全名太长，留白只有 210px） */
const DIM_SHORT: Record<string, string> = {
  inst: "机构", large: "大单", medium: "中户", retail: "散户",
};
/** 4 维各自的单笔金额口径（东财按单子大小拆分后的主动净买额） */
const DIM_HINT: Record<string, string> = {
  inst: "单笔 ≥ 100 万", large: "单笔 20 ~ 100 万",
  medium: "单笔 4 ~ 20 万", retail: "单笔 < 4 万",
};

type Row = {
  code: string; name: string; flow: number | null; pct: number | null; speed?: number | null;
  /** 该行所属口径（热门里概念/行业混排，取分时要用行自身的 kind） */
  kind?: "concept" | "industry" | "index";
};

type Kind = "concept" | "industry" | "index" | "hot" | "custom";

/**
 * 四维资金（原「指数资金分时」）：概念 / 行业 / 指数 / 热门的 4 维主动净买额曲线。
 * 列表与顶部按钮结构对齐「主力资金分时」：左列表搜索/排序（单选、无勾选框），
 * 右侧图为该目标的机构 / 大单 / 中户 / 散户。
 */
export function EmFlowPage() {
  const initial = useMemo(() => readHashParams(), []);
  const initKind = initial.kind;
  const [kind, setKind] = useState<Kind>(
    initKind === "industry" || initKind === "concept" || initKind === "hot" || initKind === "custom"
      ? initKind : "index");
  const [code, setCode] = useState(initial.code ?? "");
  const [search, setSearch] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("flow");
  const [sortDir, setSortDir] = useState(-1);
  const [cfgOpen, setCfgOpen] = useState(false);
  const [detailCode, setDetailCode] = useState<{ code: string; name: string } | null>(null);

  const { data: health } = usePolling(() => api.boardHealth(), 60_000, [], { kind: "ctx", code: "board-health" });
  const interval = 30_000;

  const { data: targets } = usePolling(() => api.emFlowTargets(), 60_000, [], { kind: "ctx", code: "em-targets" });
  const { data: industries, refresh: refreshIndustries } = usePolling(
    () => api.emFlowIndustries(), 300_000, [], { kind: "ctx", code: "em-industries" });
  const { data: concepts, refresh: refreshConcepts } = usePolling(
    () => api.emFlowConcepts(), 300_000, [], { kind: "ctx", code: "em-concepts" });
  // 财联社「热门板块」（热门行业/概念 + 异动）→ 后端模糊匹配到本地板块。
  // forceHot：只有「刷新」按钮才置 true → 请求带 force=1 绕过后端缓存回源财联社
  const forceHot = useRef(false);
  const { data: hotView, refresh: refreshHot } = usePolling(
    // 不挂 IndexedDB 缓存：热门板块变化快，前端缓存会让页面停在几分钟前的名单
    () => api.boardClsHot(undefined, forceHot.current), 120_000, []);
  // 自定义：与「主力资金分时」「主力资金多日」共用同一份自选（data/board_watch.json）
  const { data: watchView, refresh: refreshWatch } = usePolling(
    () => api.boardWatch(), 300_000, []);
  const [watchItems, setWatchItems] = useState<{ code: string; name: string }[]>([]);
  const [watchBusy, setWatchBusy] = useState<Record<string, boolean>>({});
  useEffect(() => {
    if (Array.isArray(watchView?.items)) setWatchItems(watchView.items);
  }, [watchView]);
  const watchSet = useMemo(() => new Set(watchItems.map((i) => i.code)), [watchItems]);

  /** CLS 拉不到时的回退：概念 + 行业按涨幅取前 40（与「热门」口径一致） */
  const fallbackHot: Row[] = useMemo(() => {
    const pick = (arr: any[], k: "concept" | "industry"): Row[] =>
      (arr ?? []).map((i: any) => ({
        code: i.code, name: i.name, flow: i.flow ?? null, pct: i.pct ?? null, kind: k,
      }));
    return [...pick(concepts?.display, "concept"), ...pick(industries?.display, "industry")]
      .sort((a, b) => (b.pct ?? -999) - (a.pct ?? -999))
      .slice(0, 40);
  }, [concepts, industries]);

  const options: Row[] = useMemo(() => {
    if (kind === "index") {
      return (targets?.indices ?? []).map((i: any) => ({
        code: i.code, name: i.name, flow: i.flow ?? null, pct: i.pct ?? null,
        speed: i.speed ?? 0, kind: "index" as const,
      }));
    }
    if (kind === "hot") {
      const items = (hotView?.items ?? []).map((i: any) => ({
        code: i.code, name: i.name, flow: i.flow ?? null, pct: i.pct ?? null, speed: i.speed ?? 0,
        kind: (i.type === "industry" ? "industry" : "concept") as "concept" | "industry",
      }));
      return items.length ? items : fallbackHot;
    }
    if (kind === "custom") {
      // 用三份列表拼一个 code → 行 的索引，尽量带上资金/涨幅；找不到就只显示名称
      const lookup = new Map<string, Row>();
      const put = (arr: any[], k: Row["kind"]) => (arr ?? []).forEach((i: any) => {
        lookup.set(i.code, { code: i.code, name: i.name, flow: i.flow ?? null,
                             pct: i.pct ?? null, speed: i.speed ?? 0, kind: k });
      });
      put(targets?.indices, "index");
      put(concepts?.display, "concept");
      put(industries?.display, "industry");
      return watchItems.map((w) => lookup.get(w.code) ?? {
        code: w.code, name: w.name, flow: null, pct: null, speed: 0,
        kind: w.code.startsWith("BK") ? "concept" : "index",
      });
    }
    const src = kind === "concept" ? concepts?.display : industries?.display;
    return (src ?? []).map((i: any) => ({
      code: i.code, name: i.name, flow: i.flow ?? null, pct: i.pct ?? null,
      speed: i.speed ?? 0, kind: kind as "concept" | "industry",
    }));
  }, [kind, targets, industries, concepts, hotView, fallbackHot, watchItems]);

  // 目标缺省：指数取第一个、行业取展示列表第一个；当前选中不在本口径列表里也回落
  const effectiveCode = useMemo(() => {
    if (code && options.some((o) => o.code === code)) return code;
    return options[0]?.code ?? "";
  }, [code, options]);

  const rows = useMemo(() => {
    const kw = search.trim();
    let list = options;
    if (kw) list = list.filter((o) => o.name.includes(kw) || o.code.toLowerCase().includes(kw.toLowerCase()));
    const val = (r: Row) =>
      sortKey === "pct" ? (r.pct ?? 0) : sortKey === "speed" ? (r.speed ?? 0) : (r.flow ?? 0);
    return [...list].sort((a, b) => (val(a) - val(b)) * sortDir);
  }, [options, search, sortKey, sortDir]);

  const selected = useMemo(
    () => (effectiveCode ? { [effectiveCode]: true } : {}),
    [effectiveCode],
  );

  // 热门/自定义混排概念、行业、指数：分时要按行自身口径查（em-flow/series 一次只认一个 target_type）
  const seriesKind = useMemo(() => {
    if (kind === "concept" || kind === "industry" || kind === "index") return kind;
    return options.find((o) => o.code === effectiveCode)?.kind ?? "concept";
  }, [kind, options, effectiveCode]);

  const { data: series, refresh, error } = usePolling(
    () => (effectiveCode ? api.emFlowSeries(seriesKind, effectiveCode) : Promise.resolve(null)),
    interval,
    [seriesKind, effectiveCode, interval],
    { kind: "emseries", code: `${seriesKind}:${effectiveCode}` },
  );

  // 刷新按钮：当前标的 4 维 + 热门板块一起刷，并给出转圈反馈（否则数据没变就像「点了没反应」）
  const [refreshing, setRefreshing] = useState(false);
  const doRefresh = useCallback(async () => {
    setRefreshing(true);
    forceHot.current = true;
    try {
      await Promise.all([refresh(), refreshHot(), refreshWatch()]);
    } finally {
      forceHot.current = false;
      setRefreshing(false);
    }
  }, [refresh, refreshHot, refreshWatch]);
  useEffect(() => onGlobalRefresh(() => { void doRefresh(); }), [doRefresh]);

  useEffect(() => {
    writeHashParams({ kind, code: effectiveCode || undefined });
  }, [kind, effectiveCode]);

  const dimSeries = useMemo(() => {
    if (!series?.axis?.length) return [];
    return Object.entries(series.dims ?? {}).map(([k, v]) => ({
      code: k, name: DIM_SHORT[k] ?? k, values: v as (number | null)[],
    }));
  }, [series]);

  /** 4 维当前「主动净买额」：取分时最后一个有效点 */
  const dimCards = useMemo(() => {
    const lastOf = (key: string) => {
      const arr = (series?.dims as any)?.[key] as (number | null)[] | undefined;
      const vals = (arr ?? []).filter((v) => v != null);
      return vals.length ? (vals[vals.length - 1] as number) : null;
    };
    const colors: Record<string, string> = {
      inst: "#ef4444", large: "#f59e0b", medium: "#8b5cf6", retail: "#22c55e",
    };
    return Object.keys(DIM_LABEL).map((key) => ({
      key, label: DIM_LABEL[key], hint: DIM_HINT[key] ?? "",
      value: lastOf(key), color: colors[key] ?? "#a1a1aa",
    }));
  }, [series]);

  /** 加入/移除自定义（与另外两页同一份数据，乐观更新 + 失败回滚） */
  const toggleWatch = async (b: { code: string; name: string }) => {
    if (watchBusy[b.code]) return;
    const isWatched = watchSet.has(b.code);
    setWatchBusy((m) => ({ ...m, [b.code]: true }));
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

  const nameOf = useMemo(
    () => options.find((o) => o.code === effectiveCode)?.name ?? effectiveCode,
    [options, effectiveCode],
  );

  const latestDay = health?.last?.date ?? "";

  return (
    <main className="grid gap-3 p-3 xl:grid-cols-[360px_minmax(0,1fr)]">
      <FlowListPanel
        title="标的列表"
        countText={`共 ${rows.length} 个 · 单击切换`}
        rows={rows.map((r) => ({
          code: r.code, name: r.name, flow: r.flow ?? 0, pct: r.pct,
          type: r.code.startsWith("BK") ? "industry" : "index",
          watch: watchSet.has(r.code),
        }))}
        selected={selected}
        onToggle={(c) => setCode(c)}
        search={search}
        onSearch={setSearch}
        sortKey={sortKey}
        sortDir={sortDir}
        onSort={(k) => {
          if (k === sortKey) setSortDir((d) => -d);
          else { setSortKey(k); setSortDir(-1); }
        }}
        sortOptions={[{ key: "pct", label: "涨幅" }, { key: "speed", label: "涨速" }, { key: "flow", label: "资金" }]}
        showCheckbox={false}
        onToggleWatch={(r) => void toggleWatch({ code: r.code, name: r.name })}
        watchBusy={watchBusy}
        onDetail={(r) => setDetailCode({ code: r.code, name: r.name })}
        loading={kind === "index" ? !targets
          : kind === "concept" ? !concepts
            : kind === "hot" ? !hotView && !fallbackHot.length
              : kind === "custom" ? !watchView
                : !industries}
        emptyTitle={kind === "index" ? "暂无指数数据"
          : kind === "concept" ? "暂无概念数据"
            : kind === "hot" ? "财联社热门板块暂不可用"
              : kind === "custom" ? "还没有自定义标的"
                : "暂无行业数据"}
        emptyHint={kind === "index" ? undefined
          : kind === "hot" ? (hotView?.error ?? "稍后重试（财联社接口 5 分钟缓存）")
            : kind === "custom" ? "在其他列表点行尾的 ＋ 图标加入；三页共用同一份自定义"
              : "稍后重试或检查采集状态"}
        maxHeight="calc(100vh - 15rem)"
      />

      <Frame spacing="sm" className="flex min-h-0 flex-col xl:sticky xl:top-[52px] xl:h-[calc(100vh-104px)]">
        <FramePanel dense className="flex min-h-0 flex-1 flex-col">
          <FrameHeader className="flex-row flex-wrap items-center justify-between gap-2 py-1.5">
            <div className="flex min-w-0 items-center gap-2">
              <FrameTitle className="text-sm">{nameOf || "四维资金"}</FrameTitle>
              <Tabs tabs={TARGETS} value={kind} onChange={(k) => { setKind(k as Kind); setCode(""); setSearch(""); }} />
              <DayTag date={series?.date} latest={latestDay} />
            </div>
            <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
              <span className="tnum text-muted-foreground">{series?.date ?? "—"}</span>
              {(kind === "industry" || kind === "concept") && (
                <button
                  type="button"
                  onClick={() => setCfgOpen(true)}
                  className="inline-flex items-center gap-1 rounded-md border border-border px-1.5 py-0.5 text-foreground transition-colors hover:bg-muted"
                  title={`${kind === "concept" ? "概念" : "行业"}展示配置（自定义列表）`}
                >
                  <Settings2 className="size-3" aria-hidden /> {kind === "concept" ? "概念配置" : "行业配置"}
                </button>
              )}
              <button
                type="button"
                onClick={() => void doRefresh()}
                title="立即刷新（4 维分时 + 热门板块）"
                aria-label="刷新"
                disabled={refreshing}
                className="rounded-md border border-border p-1 text-muted-foreground transition-colors hover:text-foreground disabled:opacity-60"
              >
                <RefreshCw className={cn("size-3", refreshing && "animate-spin")} aria-hidden />
              </button>
              {health?.trading ? <Badge variant="success-light">交易中</Badge> : <Badge variant="outline">休市</Badge>}
            </div>
          </FrameHeader>

          <div className="flex min-h-0 flex-1 flex-col px-2 pb-2">
            {error ? (
              <EmptyState title="接口失败" hint={String(error)} className="flex-1" />
            ) : !effectiveCode ? (
              <EmptyState title="没有可用的标的"
                hint={kind === "hot" ? "财联社热门板块为空，且没有可回退的板块" : "行业列表为空，稍后重试或检查采集状态"}
                className="flex-1" />
            ) : !series ? (
              <Skeleton className="m-1 h-[380px]" />
            ) : (
              <>
                {!!dimSeries.length && (
                  <div className="grid grid-cols-2 gap-1.5 px-0.5 pb-2 sm:grid-cols-4">
                    {dimCards.map((c) => (
                      <div
                        key={c.key}
                        className="rounded-lg border border-border/60 bg-background/50 px-2 py-1.5"
                        style={{ borderLeft: `3px solid ${c.color}` }}
                      >
                        <div className="flex items-center gap-1 text-[10px] text-muted-foreground">
                          <span className="inline-block size-1.5 rounded-full" style={{ background: c.color }} />
                          {c.label}主动净买额
                        </div>
                        <div className={cn("mt-0.5 text-sm font-semibold tnum",
                          (c.value ?? 0) >= 0 ? "text-up" : "text-down")}>
                          {c.value == null ? "—" : signedFlow(c.value)}
                        </div>
                        <div className="text-[10px] text-muted-foreground">{c.hint}</div>
                      </div>
                    ))}
                  </div>
                )}
                {!dimSeries.length ? (
                  <EmptyState title="暂无 4 维数据" hint={series.error ?? "该标的当日还没有累积到分时点"} className="flex-1" />
                ) : (
                  <div className="min-h-0 flex-1">
                    <MoneyLinesChart id={`em-${seriesKind}-${effectiveCode}`} axis={series.axis}
                      series={dimSeries as any} height="100%" maxLines={5} endLabels />
                  </div>
                )}
                <div className="mt-1 flex flex-wrap items-center justify-between gap-2 px-1 text-[10px] text-muted-foreground">
                  <span>纵轴为当日累计主动净买额（亿元）· 机构 / 大单 / 中户 / 散户 按单笔金额划分</span>
                  <span>
                    {kind === "hot"
                      ? "热门 = 财联社热门行业/概念板块 + 盘面异动，模糊匹配到本地板块（5 分钟缓存）；拉不到时回退涨幅前 40"
                      : `指数每 30 秒采集，行业 / 概念每 60 秒 · 行业、概念默认各取 |主力净流入| 前 ${industries?.default_limit ?? 40} 个`}
                  </span>
                </div>
              </>
            )}
          </div>
        </FramePanel>
      </Frame>

      <EmBoardConfig
        open={cfgOpen}
        onClose={() => setCfgOpen(false)}
        kind={kind === "concept" ? "concept" : "industry"}
        data={kind === "concept" ? concepts : industries}
        onChanged={() => { void refreshIndustries(); void refreshConcepts(); }}
      />

      <BoardDetailDrawer
        selected={detailCode}
        date={series?.date ?? health?.last?.date ?? ""}
        onClose={() => setDetailCode(null)}
      />
    </main>
  );
}

/** 板块展示配置（行业 / 概念各一份）：新增 / 移除 / 重置为默认 Top40。 */
function EmBoardConfig({
  open, onClose, kind, data, onChanged,
}: {
  open: boolean;
  onClose: () => void;
  kind: "industry" | "concept";
  data: any;
  onChanged: () => void;
}) {
  const label = kind === "concept" ? "概念" : "行业";
  const [busy, setBusy] = useState(false);
  const display: { code: string; name: string }[] = data?.display ?? [];
  const all: { code: string; name: string }[] = data?.all ?? [];
  const [pick, setPick] = useState("");

  const add = async () => {
    const item = all.find((a) => a.code === pick);
    if (!item || busy) return;
    setBusy(true);
    try {
      const res = await api.emFlowConfigAdd(item.code, item.name, kind);
      if (res?.error) throw new Error(res.error);
      setPick("");
      onChanged();
    } catch (e: any) {
      toast("添加失败", { description: String(e?.message || e) });
    } finally {
      setBusy(false);
    }
  };

  const remove = async (code: string) => {
    if (busy) return;
    setBusy(true);
    try {
      const res = await api.emFlowConfigRemove(code, kind);
      if (res?.error) throw new Error(res.error);
      onChanged();
    } catch (e: any) {
      toast("移除失败", { description: String(e?.message || e) });
    } finally {
      setBusy(false);
    }
  };

  const reset = async () => {
    if (busy) return;
    setBusy(true);
    try {
      const res = await api.emFlowConfigReset(kind);
      if (res?.error) throw new Error(res.error);
      onChanged();
      toast(`已重置${label}为默认 Top40`);
    } catch (e: any) {
      toast("重置失败", { description: String(e?.message || e) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onClose={onClose} title={`${label}展示配置（4 维主动净买额）`}>
      <div className="flex flex-col gap-3 text-xs">
        <div className="flex flex-wrap items-center gap-2">
          <select
            id="em-config-add" name="add-industry"
            value={pick}
            onChange={(e) => setPick(e.target.value)}
            aria-label="选择要添加的行业"
            className="min-w-0 flex-1 rounded border border-border bg-card px-2 py-1 text-[11px]"
          >
            <option value="">选择{label}（按 |主力净流入| 排序前 200）</option>
            {all.map((a) => <option key={a.code} value={a.code}>{a.name} {a.code}</option>)}
          </select>
          <button type="button" onClick={() => void add()} disabled={!pick || busy}
            className="rounded-md border border-border px-2 py-1 text-[11px] disabled:opacity-50">添加</button>
          <button type="button" onClick={() => void reset()} disabled={busy}
            className="rounded-md border border-border px-2 py-1 text-[11px] text-muted-foreground hover:text-foreground disabled:opacity-50">
            重置默认
          </button>
        </div>
        <div className="text-[11px] text-muted-foreground">
          当前 {display.length} 个（{data?.customized ? "自定义" : `默认 Top${data?.default_limit ?? 40}`}）
        </div>
        <div className="flex flex-wrap gap-1.5">
          {display.map((d) => (
            <span key={d.code} className="inline-flex items-center gap-1 rounded bg-muted/50 px-1.5 py-0.5 text-[11px]">
              {d.name}
              <button type="button" onClick={() => void remove(d.code)} disabled={busy}
                aria-label={`移除${d.name}`} title="移除" className="text-muted-foreground hover:text-destructive">✕</button>
            </span>
          ))}
        </div>
      </div>
    </Dialog>
  );
}
