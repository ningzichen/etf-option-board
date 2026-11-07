import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { RefreshCw, Sparkles } from "lucide-react";
import { toast } from "sonner";
import { Frame, FrameHeader, FramePanel, FrameTitle } from "@/components/reui/frame";
import { PrismChart } from "@/components/charts/PrismChart";
import { BriefDialog } from "@/pages/limitup/BriefDialog";
import { api } from "@/lib/api";
import { usePolling } from "@/lib/usePolling";
import { flowMoney, pct, signedFlow } from "@/lib/format";
import { cn } from "@/lib/utils";

/* ============================ 类型 ============================ */
type Cell = {
  board: string; name: string; zt: number; max_lbc: number;
  count_7d?: number; tier?: string; type?: string; heat?: number;
};
type ThemeRow = {
  board: string; name: string; zt: number; zb: number; lan: number;
  max_lbc: number; lianban: number; first_board: number;
  pct?: number | null; main?: number | null; d3?: number | null; d5?: number | null;
  ratio?: number | null;
  tier: string; count_7d: number; type: string;
  expanding: boolean; climax: boolean; resonance: boolean; heat?: number;
};
type PoolItem = {
  code: string; name: string; reason?: string; lbc: number; zbc: number;
  fund?: number | null; pct?: number | null; hybk?: string; fbt?: number; sealed?: boolean;
};

const TIERS = ["0~3", "3~5", "5~10", "10~15", "15以上"];
const TYPES = ["全部类型", "产业链", "事件性", "风格性"];

const POOL_TABS = [
  { key: "theme", label: "板块涨停", hint: "属于该板块的涨停股（与主题榜「涨停」列同一口径）" },
  { key: "hot", label: "炒作涨停", hint: "同一批涨停股，按炒作强度（连板高度→涨幅）排序" },
  { key: "broken", label: "板块炸板", hint: "该板块当日炸板（触板未封住）的个股" },
  { key: "lan", label: "板块烂板", hint: "该板块当日炸板 ≥3 次的反复开板个股" },
] as const;

const fmtMD = (d: string) => (d.length === 8 ? `${d.slice(4, 6)}-${d.slice(6)}` : d);
const isST = (name: string) => /ST/i.test(name);

/** 涨停数 → 蓝色深浅：颜色深浅编码「统计期间综合排名」（backend 算好的 heat 0~1），
 *  整体压淡（透明度 0.08~0.50），避免 15×N 的网格糊成一片深蓝。 */
function heatStyle(heat: number, selected: boolean) {
  if (selected) return { backgroundColor: "#e11d48", color: "#ffffff" };
  const a = 0.08 + 0.42 * Math.max(0, Math.min(1, heat || 0));
  return { backgroundColor: `rgba(37,99,235,${a.toFixed(3)})` };
}

/** 选中主题的红底：**按涨停家数分深浅**（当天 0 家时最浅，窗口内最多家的那天最深） */
function selectedStyle(zt: number, maxZt: number) {
  const ratio = maxZt > 0 ? Math.max(0.12, Math.min(1, zt / maxZt)) : 1;
  const a = 0.35 + 0.65 * ratio;
  return { backgroundColor: `rgba(225,29,72,${a.toFixed(3)})`, color: "#ffffff" };
}

/* ============================ 页面 ============================ */
/**
 * 涨停复盘（页面名）：沪深 A 股涨停板扫描 + 主题分类。
 *
 * 四个面板（复刻经传「涨停棱镜」）：
 * ① 涨停周期表：近 N 个交易日 × 每日 Top10 主题，点单元格选主题；
 * ② 涨停棱镜图：黄线 = 沪深涨停总数，红面积 = 所选主题涨停数；
 * ③ 主题榜：当日各主题涨停数 + 涨幅/主力/3日/5日/主力占比；
 * ④ 个股池：炒作涨停 / 板块涨停 / 板块炸板 / 板块烂板。
 *
 * 数据全部来自本地库（`services/limitup.py` 盘中每 2 分钟采集、15:05 定稿），
 * 页面一次 `overview` 请求拿齐 ①③，②④ 跟着所选主题联动。
 */
export function LimitUpPage() {
  const [date] = useState("");
  const [theme, setTheme] = useState("");
  const [days, setDays] = useState(11);
  /** 主题等级：all / primary（一级主题）/ secondary（二级主题），按板块成分股数分档 */
  const [level, setLevel] = useState("all");
  /** 排序口径：hot=按炒作涨停 / board=按板块涨停 / pct=按涨幅 */
  const [sortBy, setSortBy] = useState("hot");
  const [briefOpen, setBriefOpen] = useState(false);
  const [type, setType] = useState("全部类型");
  const [onlyExpand, setOnlyExpand] = useState(false);
  const [onlyClimax, setOnlyClimax] = useState(false);
  const [onlyResonance, setOnlyResonance] = useState(false);
  const [dropST, setDropST] = useState(false);
  /** 扩充涨停：涨幅 > 9.9% 也算涨停（含未封板） */
  const [ext, setExt] = useState(false);
  const [poolTab, setPoolTab] = useState<string>("theme");
  /** 点周期表单元格选中的「那一天」：右侧主题榜/个股池跟着它走。
   *  与顶部日期选择器（date，只决定周期表的窗口）分开，点中间某列不会让整张表跳走。 */
  const [cellDate, setCellDate] = useState("");

  const mode = ext ? "ext" : "zt";
  const { data: ov, refresh: refreshOv } = usePolling(
    () => api.limitupOverview(date || undefined, days, 15, mode, sortBy, level),
    120_000, [date, days, mode, sortBy, level]);
  const [busy, setBusy] = useState(false);

  const dates: string[] = ov?.dates ?? [];
  /** 周期表窗口的末日（顶部日期选择器决定） */
  const curDate: string = ov?.date ?? "";
  /** 右侧面板实际看的日期：点过单元格就是单元格那天，否则跟窗口末日 */
  const focusDate = cellDate || curDate;
  const needAlt = !!cellDate && cellDate !== curDate;

  const { data: altThemes } = usePolling(
    () => (needAlt ? api.limitupThemes(cellDate, mode, sortBy, level) : Promise.resolve(null)),
    120_000, [cellDate, curDate, mode, sortBy, level]);

  const themes: ThemeRow[] = needAlt ? (altThemes?.themes ?? []) : (ov?.themes ?? []);
  /** 主题排除名单（`data/limitup_exclude.json`，默认排除央国企改革） */
  const excludedNames: string[] = (ov?.excluded ?? altThemes?.excluded ?? []) as string[];
  const focusStats = needAlt ? (altThemes?.stats ?? {}) : (ov?.stats ?? {});

  // 选中的主题：默认当日第一名
  useEffect(() => {
    if (!themes.length) return;
    if (theme && themes.some((t) => t.board === theme)) return;
    setTheme(themes[0].board);
  }, [themes, theme]);

  const themeName = themes.find((t) => t.board === theme)?.name ?? "";

  const { data: prism } = usePolling(
    () => api.limitupPrism(theme || undefined, focusDate || undefined, 25, mode, sortBy),
    120_000, [theme, focusDate, mode, sortBy]);

  /** 棱镜标题里当前日期的主题涨停家数（取所选日期那一格，不是序列末尾——切历史日期时两者不同） */
  const prismAtDate = (() => {
    const ds: string[] = prism?.dates ?? [];
    const i = ds.indexOf(focusDate);
    return i >= 0 ? (prism?.theme?.[i] ?? 0) : (prism?.theme?.[ds.length - 1] ?? 0);
  })();

  // 四个标签页都只列「当前选中板块」的个股（板块涨停 / 炒作涨停 / 板块炸板 / 板块烂板）
  const poolKind = poolTab === "theme" ? "zt" : poolTab;
  const { data: pool } = usePolling(
    () => api.limitupPool(focusDate || undefined, theme || undefined, poolKind, mode),
    120_000, [focusDate, theme, poolTab, mode]);

  /** 主题榜筛选（等级 / 类型 / 扩张·高潮·共振 / 去 ST） */
  const shownThemes = useMemo(() => themes.filter((t) => {
    if (dropST && isST(t.name)) return false;
    if (type !== "全部类型" && t.type !== type) return false;
    if (onlyExpand && !t.expanding) return false;
    if (onlyClimax && !t.climax) return false;
    if (onlyResonance && !t.resonance) return false;
    return true;
  }), [themes, type, onlyExpand, onlyClimax, onlyResonance, dropST]);

  /** 选中主题在窗口内的最大涨停家数：红底深浅按它归一 */
  const themeMaxZt = Math.max(1, ...(ov?.matrix ?? [])
    .flatMap((col) => col.filter((c): c is Cell => !!c && c.board === theme).map((c) => c.zt)));

  /** 周期表：同样过滤后，每列重新按名次排列（缺位补空） */
  const matrix = useMemo(() => {
    const raw: (Cell | null)[][] = ov?.matrix ?? [];
    return raw.map((col) => {
      const kept = col.filter((c): c is Cell => !!c && !(dropST && isST(c.name))
        && (type === "全部类型" || c.type === type));
      const out: (Cell | null)[] = [...kept];
      while (out.length < 15) out.push(null);
      return out;
    });
  }, [ov, type, dropST]);

  /** 选中列红框：单独一层绝对定位画外框（不用逐格 border，避免和行分隔线拼成内横线） */
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const headRefs = useRef<Record<string, HTMLTableCellElement | null>>({});
  const [box, setBox] = useState<{ left: number; top: number; width: number; height: number } | null>(null);

  useLayoutEffect(() => {
    const measure = () => {
      const th = headRefs.current[focusDate];
      const table = wrapRef.current?.querySelector("table");
      if (!th || !table) { setBox(null); return; }
      setBox({
        left: th.offsetLeft, top: th.offsetTop,
        width: th.offsetWidth, height: table.offsetHeight - th.offsetTop,
      });
    };
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [focusDate, dates, matrix, level, type, dropST, ext, sortBy]);


  const onRefresh = async () => {
    if (busy) return;
    setBusy(true);
    try {
      const r = await api.limitupRefresh(curDate || undefined);
      if (r?.error) toast.error(`刷新失败：${r.error}`);
      else toast.success(`已刷新 ${r.date}：涨停 ${r.total} 家 / 主题 ${r.themes} 个`);
      refreshOv();
    } catch (e: any) {
      toast.error(e?.message ?? "刷新失败");
    } finally {
      setBusy(false);
    }
  };

  // 顶部统计跟着「右侧正在看的日期」走，避免点历史单元格后数字和主题榜对不上
  const stats = focusStats;
  const rows = pool?.items ?? [];

  return (
    <div className="flex flex-col gap-3 px-4 pb-6 pt-3">
      {/* 顶栏 */}
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-base font-semibold">涨停复盘</h2>
        <span className="text-[11px] text-muted-foreground">
          {ext ? "扩充涨停" : "沪深涨停"} <b className="text-foreground">{ext
            ? (stats.total_ext ?? 0) : (stats.total_zt ?? 0)}</b> 家
          {ext ? `（封板 ${stats.total_zt ?? 0}）` : ""}
          · 炸板 {stats.total_zb ?? 0} 家 · 主题 {stats.theme_count ?? 0} 个
          {stats.finalized ? " · 已定稿" : ""}
        </span>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <select
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
            className="h-7 rounded border bg-background px-2 text-xs"
          >
            <option value={6}>近 6 日</option>
            <option value={11}>近 11 日</option>
            <option value={16}>近 16 日</option>
            <option value={20}>近 20 日</option>
            <option value={30}>近 30 日</option>
          </select>
          <select
            value={level}
            onChange={(e) => setLevel(e.target.value)}
            className="h-7 rounded border bg-background px-2 text-xs"
            title="一级主题 = 成分股 ≥150 只的大方向；二级主题 = 细分概念"
          >
            <option value="all">全部主题</option>
            <option value="primary">一级主题</option>
            <option value="secondary">二级主题</option>
          </select>
          <select
            value={sortBy}
            onChange={(e) => setSortBy(e.target.value)}
            className="h-7 rounded border bg-background px-2 text-xs"
            title="按炒作涨停=一票只算主概念；按板块涨停=一票计入所有所属概念；按涨幅=按板块当日涨幅"
          >
            <option value="hot">按炒作涨停</option>
            <option value="board">按板块涨停</option>
            <option value="pct">按涨幅</option>
          </select>
          <select
            value={type}
            onChange={(e) => setType(e.target.value)}
            className="h-7 rounded border bg-background px-2 text-xs"
          >
            {TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
          <button
            onClick={onRefresh}
            disabled={busy}
            className="flex h-7 items-center gap-1 rounded border bg-background px-2 text-xs hover:bg-accent disabled:opacity-50"
          >
            <RefreshCw size={12} className={busy ? "animate-spin" : ""} /> 刷新
          </button>
        </div>
      </div>

      {/* 筛选开关 */}
      <div className="flex flex-wrap items-center gap-3 text-[11px] text-muted-foreground">
        <Hint text="从榜单里过滤掉名称含 ST / *ST 的个股。ST 的涨跌停幅度是 5%（部分 20%），与常规 10%/20% 口径不同，混在一起会让涨停数虚高。">
          <label className="flex cursor-help items-center gap-1">
            <input type="checkbox" checked={dropST} onChange={(e) => setDropST(e.target.checked)} />
            删除ST
          </label>
        </Hint>
        <Hint text="只保留「当日涨停家数 > 前 5 个交易日均值 × 1.5」的主题，用来找正在加速发酵的方向。">
          <label className="flex cursor-help items-center gap-1">
            <input type="checkbox" checked={onlyExpand} onChange={(e) => setOnlyExpand(e.target.checked)} />
            扩张主题
          </label>
        </Hint>
        <Hint text="只保留「当日涨停家数创近 20 个交易日新高（且 ≥3 家）」的主题，通常对应情绪的高点。">
          <label className="flex cursor-help items-center gap-1">
            <input type="checkbox" checked={onlyClimax} onChange={(e) => setOnlyClimax(e.target.checked)} />
            高潮主题
          </label>
        </Hint>
        <Hint text="只保留「首板、连板、炸板三项同时高于各自前 5 日均值」的主题 —— 承接强、赚钱效应在扩散。">
          <label className="flex cursor-help items-center gap-1">
            <input type="checkbox" checked={onlyResonance}
              onChange={(e) => setOnlyResonance(e.target.checked)} />
            三共振
          </label>
        </Hint>
        <Hint text="把当日涨幅 > 9.9% 的个股也算作涨停（含涨超但没封板的），涨停数与主题统计会一起放大；N/C 开头的新股已剔除（上市初期无涨跌幅限制）。">
          <label className="flex cursor-help items-center gap-1 font-medium text-foreground">
            <input type="checkbox" checked={ext} onChange={(e) => setExt(e.target.checked)} />
            扩充涨停
          </label>
        </Hint>
        {excludedNames.length > 0 && (
          <span title="改 data/limitup_exclude.json 可增删，改完刷新页面即生效（读时过滤）">
            已排除：{excludedNames.join("、")}
          </span>
        )}
        <span className="ml-auto">数据源：东财涨停池 + 概念板块资金 · 涨停原因取自同花顺（失败降级行业名）</span>
      </div>

      <BriefDialog
        open={briefOpen}
        onClose={() => setBriefOpen(false)}
        date={focusDate || undefined}
        mode={mode}
        sort={sortBy}
        level={level}
        theme={theme || undefined}
        themeName={themeName}
      />

      <div className="grid gap-3 lg:grid-cols-[minmax(0,1.5fr)_minmax(0,1fr)]">
        {/* ==================== 左列 ==================== */}
        <div className="flex min-w-0 flex-col gap-3">
          {/* ① 涨停周期表 */}
          <Frame>
            <FrameHeader className="flex-row items-center gap-2">
              <FrameTitle>涨停周期表</FrameTitle>
              <span className="text-[10px] text-muted-foreground">
                单元格 = 当日该名次的主题，右上角数字 = 近 7 日涨停总数
              </span>
            </FrameHeader>
            <FramePanel className="p-0">
              {/* 统计期间热门主题（窗口内累计涨停家数） */}
              {(ov?.period_top ?? []).length > 0 && (
                <div className="flex flex-wrap items-center gap-x-5 gap-y-1 border-b bg-muted/30 px-3 py-2 text-[12px]">
                  <span className="text-muted-foreground">期间热门：</span>
                  {(ov?.period_top ?? []).map((t, i) => (
                    <button key={t.board} onClick={() => setTheme(t.board)}
                      className={cn("flex items-center gap-1.5 hover:underline",
                        t.board === theme ? "font-semibold text-rose-600" : "text-foreground")}>
                      <span className="text-muted-foreground">{i + 1}.</span>
                      {t.name}
                      <span className="rounded bg-rose-100 px-1 text-[11px] font-medium text-rose-700">{t.zt}</span>
                    </button>
                  ))}
                </div>
              )}
              <div className="overflow-x-auto">
                <div ref={wrapRef} className="relative">
                <table className="w-full border-collapse text-[12px]">
                  <thead>
                    <tr className="bg-muted/40">
                      <th className="w-16 min-w-[64px] whitespace-nowrap border-b px-2 py-2.5 text-center font-normal text-muted-foreground">
                        排名
                      </th>
                      {dates.map((d) => (
                        <th key={d}
                          ref={(el) => { headRefs.current[d] = el; }}
                          className={cn(
                            "border-b px-2 py-2.5 text-center font-normal text-muted-foreground",
                            d === focusDate ? "bg-rose-50 font-medium text-rose-700" : "",
                          )}
                          title={d}>
                          {fmtMD(d)}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {Array.from({ length: 15 }).map((_, r) => (
                      <tr key={r}>
                        <td className="w-16 min-w-[64px] whitespace-nowrap border-b px-2 py-2 text-center text-muted-foreground">{r + 1}</td>
                        {matrix.map((col, ci) => {
                          const cell = col[r] ?? null;
                          const active = cell?.board === theme;
                          return (
                            <td
                              key={ci}
                              onClick={() => { if (!cell) return; setTheme(cell.board); setCellDate(dates[ci]); }}
                              className={cn(
                                "border-b px-2 py-2 text-center",
                                cell ? "cursor-pointer" : "",
                                active ? "font-medium" : "text-slate-800 hover:brightness-[0.97]",
                              )}
                              style={cell
                                ? (active ? selectedStyle(cell.zt, themeMaxZt)
                                  : heatStyle(cell.heat ?? 0, false))
                                : undefined}
                              title={cell
                                ? `${cell.name}：${cell.zt} 家涨停（近7日 ${cell.count_7d ?? cell.zt}，综合排名强度 ${Math.round((cell.heat ?? 0) * 100)}%）`
                                : ""}
                            >
                              {cell ? (
                                <span className="flex items-center justify-center gap-0.5">
                                  <span className="truncate">{cell.name}</span>
                                  {r === 0 && cell.count_7d ? (
                                    <sup className="text-[9px] opacity-70">{cell.count_7d}</sup>
                                  ) : null}
                                </span>
                              ) : <span className="text-muted-foreground/40">—</span>}
                            </td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
                {/* 选中列的外框：单独一层绝对定位，避免和单元格边框拼出内横线 */}
                {box && (
                  <div aria-hidden
                    className="pointer-events-none absolute rounded-sm border-2 border-rose-500"
                    style={{ left: box.left, top: box.top, width: box.width, height: box.height }} />
                )}
                </div>
              </div>
              {/* 主题等级图例（顶部那条红色渐变条） */}
              <div className="flex items-center gap-3 px-3 py-2 text-[10px] text-muted-foreground">
                <span>综合排名（统计期间涨停强度）</span>
                <span>弱</span>
                <div className="flex h-2 flex-1 overflow-hidden rounded">
                  {[0.08, 0.18, 0.28, 0.39, 0.5].map((a) => (
                    <div key={a} className="flex-1"
                      style={{ background: `rgba(37,99,235,${a})` }} />
                  ))}
                </div>
                <span>强</span>
                <span className="ml-2 flex items-center gap-1">
                  <span className="inline-block h-2.5 w-2.5 rounded-sm" style={{ background: "#e11d48" }} />
                  当前选中
                </span>
              </div>
            </FramePanel>
          </Frame>

          {/* ② 涨停棱镜图 */}
          <Frame>
            <FrameHeader className="flex-row items-center gap-2">
              <FrameTitle>涨停棱镜</FrameTitle>
              <span className="text-[10px] text-muted-foreground">
                {curDate} · {ext ? "扩充涨停" : "沪深涨停"} <b className="text-foreground">{ext ? (stats.total_ext ?? 0) : (stats.total_zt ?? 0)}</b> 家 ·
                {themeName ? <> 主题 · {themeName} <b className="text-rose-600">
                  {prismAtDate}</b> 家</> : " 点左上表格选主题"}
              </span>
            </FrameHeader>
            <FramePanel className="p-1">
              <PrismChart
                dates={prism?.dates ?? []}
                total={prism?.total ?? []}
                theme={prism?.theme ?? []}
                themeName={prism?.theme_name ?? ""}
              />
            </FramePanel>
          </Frame>
        </div>

        {/* ==================== 右列 ==================== */}
        <div className="flex min-w-0 flex-col gap-3">
          {/* ③ 主题榜 */}
          <Frame>
            <FrameHeader className="flex-row items-center gap-2">
              <FrameTitle>主题榜</FrameTitle>
              <span className="text-[10px] text-muted-foreground">
                <b className="text-foreground">{focusDate || "-"}</b> · {shownThemes.length} 个主题
                {shownThemes.length > 40 ? "（显示前 40）" : ""} · 点单元格切换日期，点行切换主题
              </span>
            </FrameHeader>
            <FramePanel className="max-h-[420px] overflow-auto p-0">
              <table className="w-full text-[11px]">
                <thead className="sticky top-0 z-10 bg-muted/60 backdrop-blur">
                  <tr className="text-muted-foreground">
                    <th className="w-7 px-1 py-1 text-center font-normal">#</th>
                    <th className="px-1 py-1 text-left font-normal">主题</th>
                    <th className="w-12 px-1 py-1 text-right font-normal">涨停</th>
                    <th className="w-14 px-1 py-1 text-right font-normal">涨幅</th>
                    <th className="w-16 px-1 py-1 text-right font-normal">主力净额</th>
                    <th className="w-16 px-1 py-1 text-right font-normal">3日</th>
                    <th className="w-16 px-1 py-1 text-right font-normal">5日</th>
                    <th className="w-14 px-1 py-1 text-right font-normal">占比</th>
                  </tr>
                </thead>
                <tbody>
                  {shownThemes.slice(0, 40).map((t, i) => (
                    <tr
                      key={t.board}
                      onClick={() => setTheme(t.board)}
                      className={cn("cursor-pointer border-b last:border-0 hover:bg-accent/60",
                        t.board === theme && "bg-primary/5")}
                    >
                      <td className="px-1 py-0.5 text-center text-muted-foreground">{i + 1}</td>
                      <td className="max-w-0 px-1 py-0.5">
                        <div className="flex min-w-0 items-center gap-1">
                          <span className="truncate">{t.name}</span>
                          {t.climax && <Tag tone="hot">热</Tag>}
                          {t.expanding && <Tag tone="expand">扩张</Tag>}
                          {t.resonance && <Tag tone="res">共振</Tag>}
                        </div>
                      </td>
                      <td className="px-1 py-0.5 text-right font-medium">{t.zt}</td>
                      <td className={cn("px-1 py-0.5 text-right",
                        (t.pct ?? 0) >= 0 ? "text-rose-600" : "text-emerald-600")}>
                        {t.pct == null ? "-" : pct(t.pct)}
                      </td>
                      <td className="px-1 py-0.5 text-right">{t.main == null ? "-" : signedFlow(t.main)}</td>
                      <td className="px-1 py-0.5 text-right">{t.d3 == null ? "-" : signedFlow(t.d3)}</td>
                      <td className="px-1 py-0.5 text-right">{t.d5 == null ? "-" : signedFlow(t.d5)}</td>
                      <td className="px-1 py-0.5 text-right">{t.ratio == null ? "-" : pct(t.ratio)}</td>
                    </tr>
                  ))}
                  {!shownThemes.length && (
                    <tr><td colSpan={8} className="py-6 text-center text-muted-foreground">
                      当前筛选下没有主题
                    </td></tr>
                  )}
                </tbody>
              </table>
            </FramePanel>
          </Frame>

          {/* ④ 个股池 */}
          <Frame>
            <FrameHeader className="flex-row items-center gap-2">
              <FrameTitle>
                个股池
                {themeName && <span className="ml-1 font-normal text-muted-foreground">· {themeName}</span>}
              </FrameTitle>
              <button
                onClick={() => setBriefOpen(true)}
                className="ml-1 flex shrink-0 items-center gap-1 rounded border border-rose-200 bg-rose-50 px-1.5 py-0.5 text-[11px] text-rose-700 hover:bg-rose-100"
                title="主题猎手-涨停分析：只分析当前板块的炒作原因与个股涨停原因"
              >
                <Sparkles size={11} /> 涨停分析
              </button>
              <div className="ml-auto flex gap-1">
                {POOL_TABS.map((t) => (
                  <button
                    key={t.key}
                    title={t.hint}
                    onClick={() => setPoolTab(t.key)}
                    className={cn("rounded px-2 py-0.5 text-[11px]",
                      poolTab === t.key
                        ? "bg-primary text-primary-foreground"
                        : "bg-muted text-muted-foreground hover:bg-accent")}
                  >
                    {t.label}
                  </button>
                ))}
              </div>
            </FrameHeader>
            <FramePanel className="max-h-[420px] overflow-auto p-0">
              <table className="w-full text-[11px]">
                <thead className="sticky top-0 z-10 bg-muted/60 backdrop-blur">
                  <tr className="text-muted-foreground">
                    <th className="w-7 px-1 py-1 text-center font-normal">#</th>
                    <th className="w-[104px] whitespace-nowrap px-1 py-1 text-left font-normal">名称</th>
                    <th className="px-1 py-1 text-left font-normal">涨停原因</th>
                    <th className="w-14 px-1 py-1 text-center font-normal">
                      {poolTab === "broken" || poolTab === "lan" ? "炸板次数" : "连板数"}
                    </th>
                    <th className="w-16 px-1 py-1 text-right font-normal">封板金额</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((it: PoolItem, i: number) => (
                    <tr key={`${it.code}-${i}`} className="border-b last:border-0">
                      <td className="px-1 py-0.5 text-center text-muted-foreground">{i + 1}</td>
                      <td className="whitespace-nowrap px-1 py-0.5">
                        {it.name}
                        {it.sealed === false && (
                          <span className="ml-1 rounded bg-slate-100 px-1 text-[9px] text-slate-500">未封板</span>
                        )}
                      </td>
                      <td className="max-w-0 truncate px-1 py-0.5 text-muted-foreground"
                        title={it.reason ?? ""}>{it.reason || "-"}</td>
                      <td className="px-1 py-0.5 text-center">
                        {poolTab === "broken" || poolTab === "lan"
                          ? <span className="text-amber-600">{it.zbc}</span>
                          : <span className={cn("rounded px-1",
                            it.lbc >= 3 ? "bg-rose-600 text-white" : it.lbc === 2
                              ? "bg-rose-100 text-rose-700" : "bg-muted text-muted-foreground")}>
                            {it.lbc >= 2 ? `${it.lbc}连板` : "首板"}
                          </span>}
                      </td>
                      <td className="px-1 py-0.5 text-right">
                        {it.fund == null ? "-" : flowMoney(it.fund)}
                      </td>
                    </tr>
                  ))}
                  {!rows.length && (
                    <tr><td colSpan={5} className="py-6 text-center text-muted-foreground">
                      {poolTab === "broken" || poolTab === "lan" ? "该板块当日没有炸板个股"
                        : poolTab === "hot" ? "该板块没有以它为主概念（炒作）的涨停股，可切到「板块涨停」"
                          : "没有个股"}
                    </td></tr>
                  )}
                </tbody>
              </table>
            </FramePanel>
          </Frame>
        </div>
      </div>
    </div>
  );
}

/**
 * 轻量 tooltip：鼠标悬停或键盘聚焦（Tab 到复选框）都能触发。
 *
 * 没用 title 属性（浏览器原生 tooltip 有 1 秒延迟、样式不可控）；
 * 也没有引第三方库 —— 这里只是 CSS `group-hover` + `group-focus-within`。
 * `pointer-events-none` 保证浮层不会挡住点击。
 */
function Hint({ text, children }: { text: string; children: React.ReactNode }) {
  return (
    <span className="group/hint relative inline-flex">
      {children}
      <span
        role="tooltip"
        className={cn(
          "pointer-events-none absolute left-0 top-full z-[120] mt-1.5 hidden w-64",
          "rounded-md border border-border bg-popover px-2 py-1.5 text-[11px] font-normal",
          "leading-relaxed text-popover-foreground shadow-lg",
          "group-hover/hint:block group-focus-within/hint:block",
        )}
      >
        {text}
      </span>
    </span>
  );
}

function Tag({ tone, children }: { tone: "hot" | "expand" | "res"; children: React.ReactNode }) {
  const cls = {
    hot: "bg-rose-600 text-white",
    expand: "bg-amber-100 text-amber-700",
    res: "bg-violet-100 text-violet-700",
  }[tone];
  return (
    <span className={cn("shrink-0 rounded px-1 text-[9px] leading-4", cls)}>{children}</span>
  );
}
