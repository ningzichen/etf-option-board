import ReactECharts from "echarts-for-react";
import { useEffect, useMemo, useRef, useState, type ChangeEvent } from "react";
import { Dialog } from "@/components/ui/misc";
import { api } from "@/lib/api";
import { clsUpDown, nf, pct, todayStr } from "@/lib/format";
import { cn } from "@/lib/utils";

const COLORS: Record<string, string> = {
  low20: "#2563eb",
  int_lower: "#60a5fa",
  high20: "#dc2626",
  int_upper: "#f87171",
  ma5: "#f59e0b",
  ma11: "#a855f7",
  // 手动位：图上只画这两条（其余自动位仍算分、仍进下方表格）
  manual_support: "#059669",
  manual_pressure: "#be123c",
};

/** 图上只显示手动支撑/压力线；20 日高低、整百、MA 留在因子表里 */
const isManual = (key: string) => key.startsWith("manual_");


type Level = { key: string; name: string; value: number; side: string; dist_pct: number | null; contrib: number };

const SIDE_LABEL: Record<string, string> = { support: "支撑", pressure: "压力", ma: "均线" };

export function IndexLevelsDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [data, setData] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const [hidden, setHidden] = useState<Set<string>>(new Set());

  // 手动支撑/压力位输入：默认回填当前生效值；用户改过之后 10s 轮询不再覆盖
  const [sup, setSup] = useState("");
  const [pre, setPre] = useState("");
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveErr, setSaveErr] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<string | null>(null);
  const dirtyRef = useRef(false);
  dirtyRef.current = dirty;

  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useEffect(() => {
    if (!open) return;
    let alive = true;
    const load = async () => {
      try {
        const d = await api.indexLevels();
        if (!alive) return;
        setData(d); setErr(d?.error ?? null);
        if (!dirtyRef.current) {
          setSup(d?.manual?.support != null ? String(d.manual.support) : "");
          setPre(d?.manual?.pressure != null ? String(d.manual.pressure) : "");
        }
      } catch (e: any) {
        if (alive) setErr(e?.message ?? "加载失败");
      }
    };
    load();
    const t = window.setInterval(load, 10_000);
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") closeRef.current(); };
    window.addEventListener("keydown", onKey);
    return () => { alive = false; window.clearInterval(t); window.removeEventListener("keydown", onKey); };
  }, [open]);

  const levels: Level[] = data?.levels ?? [];
  const manualLevels = useMemo(
    () => (data?.levels ?? []).filter((l: Level) => isManual(l.key)),
    [data?.levels],
  );

  const edit = (setter: (v: string) => void) => (e: ChangeEvent<HTMLInputElement>) => {
    setter(e.target.value);
    setDirty(true);
    setSaveErr(null);
  };

  const saveManual = async () => {
    const s = Number(sup), p = Number(pre);
    if (!sup.trim() || !pre.trim()) { setSaveErr("支撑位和压力位都必须填写"); return; }
    if (!Number.isFinite(s) || !Number.isFinite(p)) { setSaveErr("请填写有效数字"); return; }
    if (s < 100 || s > 99999 || p < 100 || p > 99999) { setSaveErr("点位需在 100–99999 之间"); return; }
    if (s >= p) { setSaveErr("支撑位必须小于压力位"); return; }
    setSaving(true);
    try {
      const day = data?.trends_day || todayStr();
      const res = await api.saveIndexManualLevels({ date: day, support: s, pressure: p });
      setDirty(false);
      setData((prev: any) => ({ ...(prev ?? {}), manual: res.manual, total: res.total, levels: res.levels }));
      setSavedAt(res.manual?.date ?? day);
    } catch (e: any) {
      setSaveErr(e?.message ?? "保存失败");
    } finally {
      setSaving(false);
    }
  };

  const option = useMemo(() => {
    const tr = data?.trends ?? [];
    const times = tr.map((p: any) => (p.time ?? "").slice(11, 16));
    const price = tr.map((p: any) => p.price);
    const avg = tr.map((p: any) => (p.avg ? p.avg : null));
    // 图上只画手动支撑/压力线：自动位（20日高低/整百/MA）仍算分、仍进下方表格
    const active = manualLevels.filter((l) => !hidden.has(l.key));
    const vals = [...price, ...avg].filter((v: any) => typeof v === "number" && Number.isFinite(v));
    const span = [...vals, ...active.map((l) => l.value)];
    const lo = span.length ? Math.min(...span) : 0;
    const hi = span.length ? Math.max(...span) : 1;
    const pad = Math.max((hi - lo) * 0.03, 0.5);
    return {
      backgroundColor: "transparent",
      animation: false,
      grid: { left: 58, right: 116, top: 16, bottom: 28 },
      textStyle: { color: "#71717a", fontSize: 11 },
      tooltip: {
        trigger: "axis",
        backgroundColor: "rgba(255,255,255,0.98)",
        borderColor: "rgba(0,0,0,0.12)",
        borderWidth: 1,
        textStyle: { color: "#18181b", fontSize: 11 },
      },
      xAxis: {
        type: "category", data: times, boundaryGap: false,
        axisLine: { lineStyle: { color: "rgba(0,0,0,0.12)" } },
        axisLabel: { color: "#71717a", hideOverlap: true },
        splitLine: { show: false },
      },
      yAxis: {
        type: "value", min: lo - pad, max: hi + pad,
        axisLine: { show: false },
        axisLabel: { color: "#71717a" },
        splitLine: { lineStyle: { color: "rgba(0,0,0,0.07)" } },
      },
      series: [
        {
          name: "上证指数", type: "line", data: price, showSymbol: false,
          lineStyle: { width: 1.8, color: "#18181b" },
          markLine: {
            silent: true, symbol: "none",
            label: {
              position: "insideEndTop", fontSize: 10,
              formatter: (p: any) => `${p.name} ${nf(p.value, 1)}${p.data.dist != null ? ` (${p.data.dist >= 0 ? "+" : ""}${p.data.dist}%)` : ""}`,
            },
            data: active.map((l) => ({
              name: l.name, yAxis: l.value, dist: l.dist_pct,
              lineStyle: { color: COLORS[l.key] ?? "#a1a1aa", width: 1, type: l.side === "ma" ? "dashed" : "solid", opacity: 0.9 },
              label: { color: COLORS[l.key] ?? "#71717a" },
            })),
          },
        },
        {
          name: "均价", type: "line", data: avg, showSymbol: false,
          lineStyle: { width: 1.2, color: "#9ca3af" },
        },
      ],
    };
  }, [data, manualLevels, hidden]);

  const toggle = (key: string) => {
    setHidden((prev) => {
      const next = new Set(prev);
      next.has(key) ? next.delete(key) : next.add(key);
      return next;
    });
  };

  return (
    <Dialog open={open} onClose={onClose} title="上证指数 · 关键位"
      className="w-[min(96vw,860px)]">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="text-lg font-semibold tnum">{data?.last != null ? nf(data.last, 2) : "—"}</span>
        <span className={cn("text-sm tnum", clsUpDown(data?.pct))}>{data?.pct != null ? pct(data.pct) : "—"}</span>
        <span className="text-[11px] text-muted-foreground tnum">000001 · {data?.ts ?? ""}</span>
        {data?.note && <span className="text-[11px] text-amber-600">{data.note}</span>}
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-md border border-border/70 bg-muted/30 px-2.5 py-2">
        <span className="text-[11px] font-medium text-muted-foreground">手动关键位</span>
        <label className="flex items-center gap-1 text-[11px]">
          <span className="text-muted-foreground">支撑位</span>
          <input
            type="text" inputMode="decimal" value={sup} placeholder="如 3900"
            onChange={edit(setSup)} onKeyDown={(e) => { if (e.key === "Enter") saveManual(); }}
            className="h-7 w-24 rounded-md border border-border bg-background px-2 text-right text-xs tnum outline-none focus:border-ring"
          />
        </label>
        <label className="flex items-center gap-1 text-[11px]">
          <span className="text-muted-foreground">压力位</span>
          <input
            type="text" inputMode="decimal" value={pre} placeholder="如 4000"
            onChange={edit(setPre)} onKeyDown={(e) => { if (e.key === "Enter") saveManual(); }}
            className="h-7 w-24 rounded-md border border-border bg-background px-2 text-right text-xs tnum outline-none focus:border-ring"
          />
        </label>
        <button
          type="button" onClick={saveManual} disabled={saving}
          className={cn(
            "h-7 rounded-md border px-2.5 text-[11px] transition-colors",
            saving ? "cursor-not-allowed border-border text-muted-foreground"
                   : "border-emerald-600/50 bg-emerald-600/10 text-emerald-700 hover:bg-emerald-600/20",
          )}
        >
          {saving ? "保存中…" : "保存"}
        </button>
        <span className="text-[11px] text-muted-foreground">
          {data?.manual
            ? `${data.manual.carried ? `沿用于 ${data.manual.date}` : `${data.manual.date} 今日设`} · 参与评分`
            : "未设置 · 保存后显示在图上"}
          {savedAt && <span className="ml-1 text-emerald-600">已保存</span>}
        </span>
      </div>
      {saveErr && <div className="mt-1 text-[11px] text-destructive">{saveErr}</div>}

      {err && <div className="mt-2 text-xs text-destructive">{String(err)}</div>}

      <div className="mt-2 flex flex-wrap gap-1.5">
        {manualLevels.map((l) => {
          const off = hidden.has(l.key);
          return (
            <button
              key={l.key}
              type="button"
              onClick={() => toggle(l.key)}
              aria-pressed={!off}
              className={cn(
                "rounded-md border px-2 py-1 text-[11px] transition-colors",
                off ? "border-border text-muted-foreground" : "border-border bg-muted/60",
              )}
            >
              <span className="mr-1 inline-block size-2 rounded-full" style={{ background: COLORS[l.key], opacity: off ? 0.3 : 1 }} />
              {l.name} {nf(l.value, 1)} · {SIDE_LABEL[l.side] ?? l.side}
            </button>
          );
        })}
        {!manualLevels.length && (
          <span className="text-[11px] text-muted-foreground">图上只画手动支撑/压力线；20 日高低、整百、MA 见下表</span>
        )}
      </div>

      <div className="mt-2 h-[260px] w-full sm:h-[300px]">
        {(data?.trends?.length ?? 0) > 0 ? (
          <ReactECharts option={option} style={{ height: "100%", width: "100%" }} notMerge />
        ) : (
          <div className="flex h-full items-center justify-center text-xs text-muted-foreground">
            暂无分时数据（关键位仍按日线计算）
          </div>
        )}
      </div>

      <div className="mt-2 overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-[11px] text-muted-foreground">
              <th className="py-1 text-left font-medium">关键位</th>
              <th className="py-1 text-right font-medium">点位</th>
              <th className="py-1 text-right font-medium">距离</th>
              <th className="py-1 text-right font-medium">贡献</th>
            </tr>
          </thead>
          <tbody>
            {levels.map((l) => (
              <tr key={l.key} className="border-t border-border/50">
                <td className="py-1">
                  <span className="mr-1.5 inline-block size-2 rounded-full" style={{ background: COLORS[l.key] }} />
                  {l.name}
                  <span className="ml-1 text-muted-foreground">（{SIDE_LABEL[l.side] ?? l.side}）</span>
                </td>
                <td className="py-1 text-right tnum">{nf(l.value, 2)}</td>
                <td className="py-1 text-right tnum">{l.dist_pct != null ? `${l.dist_pct >= 0 ? "+" : ""}${l.dist_pct}%` : "—"}</td>
                <td className={cn("py-1 text-right tnum", l.contrib > 0 ? "text-red-500" : l.contrib < 0 ? "text-green-600" : "text-muted-foreground")}>
                  {l.contrib ? `${l.contrib > 0 ? "+" : ""}${l.contrib}` : "0"}
                </td>
              </tr>
            ))}
            {!levels.length && (
              <tr><td colSpan={4} className="py-3 text-center text-muted-foreground">当前未触及关键位</td></tr>
            )}
          </tbody>
          <tfoot>
            <tr className="border-t border-border">
              <td className="py-1.5 font-medium" colSpan={3}>合计（clamp ±15）</td>
              <td className={cn("py-1.5 text-right font-semibold tnum", (data?.total ?? 0) > 0 ? "text-red-500" : (data?.total ?? 0) < 0 ? "text-green-600" : "")}>
                {data?.total != null ? `${data.total > 0 ? "+" : ""}${data.total}` : "—"}
              </td>
            </tr>
          </tfoot>
        </table>
      </div>
    </Dialog>
  );
}
