import { useEffect, useMemo, useRef, useState } from "react";
import { RefreshCw, Zap } from "lucide-react";
import { toast } from "sonner";
import { Frame, FramePanel, FrameHeader, FrameTitle } from "@/components/reui/frame";
import { Badge } from "@/components/reui/badge";
import { Tabs } from "@/components/ui/misc";
import { DayTag } from "@/components/DayTag";
import { MoneyLinesChart } from "@/components/charts";
import { api } from "@/lib/api";
import { usePolling } from "@/lib/usePolling";
import { dateWithWeek, flowMoney, signedFlow } from "@/lib/format";
import { cn } from "@/lib/utils";

const PRESETS = [
  { key: "tdx", label: "通达信口径" },
  { key: "wind", label: "Wind 口径" },
];

/** 两套口径的默认门槛（通达信按「手」，Wind 按「万元」），页面输入框可改 */
const DEFAULTS: Record<string, string[]> = {
  tdx: ["700", "150"],
  wind: ["400", "70", "15"],
};
const UNIT: Record<string, string> = { tdx: "手", wind: "万元" };
/** 叠加价格线的颜色（与 MoneyLinesChart 里的 EXTRA_COLORS 同序） */
const PRICE_COLORS = ["#0ea5e9", "#64748b"];

const NOTES: Record<string, string> = {
  tdx: "口径：全A（沪深，不含北交所）逐笔成交按单笔手数分档、每笔只记主动方一次，开盘集合竞价整段算主动买；"
    + "主力 ≥700 手，跟风 150–700 手，散户 <150 手。各档「买+卖」合计 = 全市场成交额。",
  wind: "口径：Wind 按单笔金额分档、剔除开盘集合竞价（09:30 前）、收盘竞价按方向计入；"
    + "机构 ≥400 万，主力 70–400 万，大户 15–70 万，散户 <15 万（门槛按 Wind 11-06 截图拟合）。",
};

/** 采集只在连续竞价跑：09:30–11:30 / 13:00–15:00（东财逐笔只能取最新交易日）。 */
const SESSIONS: [number, number][] = [[9 * 60 + 30, 11 * 60 + 30], [13 * 60, 15 * 60]];
const FINAL_MIN = 15 * 60 + 5;

/** 当前时钟处在哪个阶段——倒计时只对「盘中」有意义，其余给明确文案。 */
function phaseOf(now: Date): "morning" | "lunch" | "afternoon" | "pre" | "closed" | "holiday" {
  const wd = now.getDay();
  if (wd === 0 || wd === 6) return "holiday";
  const m = now.getHours() * 60 + now.getMinutes();
  if (m < SESSIONS[0][0]) return "pre";
  if (m >= FINAL_MIN) return "closed";
  if (m <= SESSIONS[0][1]) return "morning";
  if (m < SESSIONS[1][0]) return "lunch";
  return "afternoon";
}

const mmss = (sec: number): string => {
  const s = Math.max(0, Math.round(sec));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
};

type Bucket = {
  key: string; name: string; thr: number;
  buy?: number; sell?: number; net?: number; share?: number;
};

/**
 * 资金流向：全A 逐笔分档的两张分时图（通达信 / Wind 两套口径）。
 *
 * 数据来自 `data/board_flow.db` 的逐笔直方图（每分钟 × 金额档 × 买卖）。
 *
 * 三个动作别混：**重算** = 重跑全A逐笔入库（单次实测 6~7 分钟）；**刷新** = 读本地库
 * 重绘（毫秒）；**应用** = 改门槛后按库里直方图重算（毫秒，不重跑逐笔）。
 * 盘中两轮重算的开始时间至少隔 10 分钟（`SCAN_INTERVAL`），收盘后 15:05 补跑定稿。
 */
export function TapeFlowPage() {
  const [preset, setPreset] = useState<string>("tdx");
  const [date, setDate] = useState("");
  const [vals, setVals] = useState<string[]>(DEFAULTS.tdx);
  /** 已应用的自定义门槛（空 = 用默认口径） */
  const [applied, setApplied] = useState("");
  /** 叠加价格线的显隐（点图例图标切换） */
  const [showPrice, setShowPrice] = useState<Record<string, boolean>>({ avg: true, sh: true });

  useEffect(() => {
    setVals(DEFAULTS[preset] ?? []);
    setApplied("");
  }, [preset]);

  const { data: datesView } = usePolling(
    () => api.tapeFlowDates(120), 600_000, [], { kind: "ctx", code: "tape-dates" });
  const dates: string[] = datesView?.dates ?? [];
  const latest = dates[0] ?? "";

  // 采集状态用于倒计时：15 秒一轮（只读库头，很轻），倒计时再靠本地秒表递减
  const { data: health, refresh: refreshHealth } = usePolling(
    () => api.tapeFlowHealth(), 15_000, [], { kind: "ctx", code: "tape-health" });

  const { data, refresh, loading } = usePolling(
    () => api.tapeFlowSeries(preset, date || undefined, applied || undefined),
    60_000, [preset, date, applied]);

  // 服务端只给「相对秒数」，本地逐秒递减 + 每次 health 轮询重新校准（避免两端时钟差）
  const [nowTs, setNowTs] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNowTs(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);
  // 倒计时目标 = **数据更新完成**（不是下一轮开始）：空闲 = 等待 + 单轮预计耗时；
  // 重算中 = 预计剩余。后端给相对秒数，本地逐秒递减、每次轮询重新校准。
  const eta = useRef<{ at: number; updateIn: number; est: number; started: number | null; at0: string } | null>(null);
  const etasig = `${health?.state?.last_run ?? ""}|${health?.state?.next_update_in ?? ""}|${health?.state?.busy ?? ""}`;
  useEffect(() => {
    const st = health?.state;
    if (!st) return;
    eta.current = {
      at: Date.now(),
      updateIn: Number(st.next_update_in ?? 0),
      est: Number(st.scan_est ?? 0),
      started: st.started_in == null ? null : Number(st.started_in),
      at0: String(st.next_update_at ?? ""),
    };
  }, [etasig]); // eslint-disable-line react-hooks/exhaustive-deps

  const drift = eta.current ? (nowTs - eta.current.at) / 1000 : 0;
  const busy = !!health?.state?.busy;
  const updateIn = eta.current ? Math.max(0, eta.current.updateIn - drift) : null;
  const estMin = eta.current ? Math.round(eta.current.est / 60) : null;
  // 预估只能取近几轮均值，单轮变慢时会跑到 0——别显示停在 0:00，改成「收尾中 · 已用 x:xx」
  const overrun = busy && updateIn != null && updateIn <= 0;
  const used = eta.current?.started != null ? Math.max(0, eta.current.started + drift) : null;

  // 新数据入库（last_run 变化）→ 立刻重绘曲线，不等下一轮 60s
  const refreshRef = useRef(refresh);
  refreshRef.current = refresh;
  const lastRunRef = useRef<string>("");
  useEffect(() => {
    const lr = String(health?.state?.last_run ?? "");
    if (!lr) return;
    if (lastRunRef.current && lr !== lastRunRef.current) void refreshRef.current();
    lastRunRef.current = lr;
  }, [health?.state?.last_run]);

  const buckets: Bucket[] = data?.buckets ?? [];
  const series = useMemo(
    () => (data?.series ?? []).map((s: any) => ({ code: s.code, name: s.name, values: s.values })),
    [data]);
  // 叠加价格线：A股平均股价 / 上证指数（走第二 Y 轴）
  const prices = (data?.prices ?? []) as { key: string; name: string; values: (number | null)[]; last?: number }[];
  const extra = prices
    .map((p, i) => ({ p, color: PRICE_COLORS[i % PRICE_COLORS.length] }))
    .filter(({ p }) => showPrice[p.key] !== false)
    .map(({ p, color }) => ({ code: p.key, name: p.name, values: p.values, color }));
  const meta = data?.meta ?? {};
  const updated = meta.updated_at || health?.state?.last_run || "";
  const err = data?.error;
  const phase = phaseOf(new Date(nowTs));

  const dateOptions = useMemo(() => {
    const out = [...dates];
    if (date && !out.includes(date)) out.unshift(date);
    return out;
  }, [dates, date]);

  const apply = () => {
    const clean = vals.map((v) => v.trim()).filter(Boolean);
    if (!clean.length) {
      setApplied("");
      return;
    }
    setApplied(clean.join(","));
    toast.success(`门槛已改为 ${clean.join(" / ")} ${UNIT[preset] ?? "万元"}`);
  };

  // 「立即重算」= 重跑全A逐笔（单次实测 6~7 分钟）。采集器忙时后端返回 ok:false，
  // 这里照实提示「已重算 x:xx」，不要谎报成功（旧版无条件弹成功，点了没反应）。
  const scan = async () => {
    let r: any = null;
    try {
      r = await api.tapeFlowScan();
    } catch {
      toast.error("重算请求失败，请稍后重试");
      return;
    }
    if (r?.ok) {
      toast.success("已开始重算，约 6 分钟；完成后本页自动刷新");
      refreshHealth();
    } else {
      toast.info(r?.msg || "正在重算中，完成后本页自动刷新");
    }
  };

  return (
    <div className="flex h-[calc(100vh-92px)] min-h-0 flex-col px-2 pb-1 pt-2">
      <Frame spacing="sm" className="flex min-h-0 flex-1 flex-col">
        <FramePanel dense className="flex min-h-0 flex-1 flex-col">
          <FrameHeader className="flex-row flex-wrap items-center justify-between gap-2 py-1.5">
            <div className="flex flex-wrap items-center gap-2">
              <FrameTitle className="text-sm">资金流向</FrameTitle>
              <Tabs tabs={PRESETS} value={preset} onChange={setPreset} />
              <DayTag date={data?.date} latest={latest} />
              {busy ? (
                <Badge
                  variant="info-light"
                  title={overrun
                    ? `本轮比预估慢（单轮预计约 ${estMin ?? 9} 分钟），仍在重算，完成后本页自动刷新`
                    : `正在重算，预计 ${eta.current?.at0 ?? "—"} 更新完成`}
                >
                  {overrun ? `收尾中 · 已用 ${used != null ? mmss(used) : "—"}` : `更新中 ${updateIn != null ? mmss(updateIn) : "…"}`}
                </Badge>
              ) : phase === "lunch" ? (
                <span className="text-[11px] text-muted-foreground">休市 · 13:00 继续</span>
              ) : phase === "closed" ? (
                <span className="text-[11px] text-muted-foreground">已定稿 15:05</span>
              ) : phase === "holiday" ? (
                <span className="text-[11px] text-muted-foreground">休市</span>
              ) : phase === "pre" ? (
                <span className="text-[11px] text-muted-foreground">09:30 开始重算</span>
              ) : updateIn != null ? (
                <span
                  className="text-[11px] text-muted-foreground tnum"
                  title={`全A逐笔每 10 分钟重算一次，单轮约 ${estMin ?? 6} 分钟；` +
                    `预计 ${eta.current?.at0 ?? "—"} 完成更新，也可点右侧「立即重算」`}
                >
                  数据更新 {mmss(updateIn)}
                </span>
              ) : null}
            </div>
            <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
              {prices.length ? (
                <>
                  <span className="text-muted-foreground">叠加</span>
                  {prices.map((p, i) => {
                    const on = showPrice[p.key] !== false;
                    return (
                      <button
                        key={p.key}
                        type="button"
                        aria-pressed={on}
                        title={on ? `隐藏${p.name}` : `显示${p.name}`}
                        onClick={() => setShowPrice((prev) => ({ ...prev, [p.key]: !on }))}
                        className={cn(
                          "flex items-center gap-1 rounded border px-1.5 py-0.5 transition-colors",
                          on
                            ? "border-border text-foreground hover:bg-muted"
                            : "border-dashed border-border/70 text-muted-foreground opacity-60 hover:opacity-100",
                        )}
                      >
                        <span className="size-1.5 rounded-full"
                          style={{ background: PRICE_COLORS[i % PRICE_COLORS.length] }} />
                        {p.name}
                        <span className="tnum">{p.last != null ? p.last.toFixed(2) : "—"}</span>
                      </button>
                    );
                  })}
                  <span className="mx-0.5 h-3 w-px bg-border" aria-hidden />
                </>
              ) : null}
              <select
                id="tape-flow-date" name="date" aria-label="选择日期"
                value={data?.date ?? date}
                onChange={(e) => setDate(e.target.value)}
                className="rounded border border-border bg-card px-1.5 py-0.5 text-[11px] text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              >
                {dateOptions.length === 0 ? <option value="">—</option> : null}
                {dateOptions.map((d) => <option key={d} value={d}>{dateWithWeek(d)}</option>)}
              </select>
              <button
                type="button"
                onClick={() => setDate("")}
                title="切到最近交易日"
                className="rounded border border-border px-1.5 py-0.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
              >
                今日
              </button>
              <span className="hidden text-muted-foreground tnum sm:inline">{updated}</span>
              <button
                type="button"
                onClick={() => { refreshHealth(); refresh(); }}
                title="刷新（本地库）"
                aria-label="刷新"
                className="rounded-md border border-border p-1 text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <RefreshCw className={cn("size-3", loading && "animate-spin")} aria-hidden />
              </button>
              <button
                type="button"
                onClick={scan}
                disabled={busy || phase === "lunch" || phase === "holiday" || phase === "pre"}
                title={busy
                  ? "正在重算，完成后自动刷新"
                  : phase === "lunch" || phase === "pre"
                    ? "休市不重算（逐笔只反映最新交易日，盘中再跑）"
                    : phase === "closed"
                      ? "补跑收盘定稿（单次约 6 分钟，后台跑）"
                      : "立即重跑全A逐笔（单次约 6 分钟，后台跑）"}
                className="flex items-center gap-1 rounded-md border border-border px-1.5 py-0.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:opacity-50"
              >
                <Zap className={cn("size-3", busy && "animate-pulse")} aria-hidden />立即重算
              </button>
            </div>
          </FrameHeader>

          {/* 门槛输入：改完点「应用」用库里的直方图重算（毫秒级，不重跑逐笔） */}
          <div className="flex shrink-0 flex-wrap items-center gap-1.5 border-b border-border/60 px-2 py-1 text-[11px]">
            <span className="text-muted-foreground">门槛（{UNIT[preset] ?? "万元"}）</span>
            {vals.map((v, i) => (
              <input
                key={`${preset}-${i}`}
                value={v}
                inputMode="decimal"
                aria-label={`第 ${i + 1} 档门槛（${UNIT[preset] ?? "万元"}）`}
                onChange={(e) => setVals((prev) => prev.map((x, j) => (j === i ? e.target.value : x)))}
                className="w-16 rounded border border-border bg-card px-1.5 py-0.5 text-[11px] tnum focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              />
            ))}
            <button
              type="button"
              onClick={apply}
              className="rounded border border-border px-1.5 py-0.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
            >
              应用
            </button>
            <button
              type="button"
              onClick={() => { setVals(DEFAULTS[preset] ?? []); setApplied(""); }}
              className="rounded border border-border px-1.5 py-0.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
            >
              恢复默认
            </button>
            {applied ? <span className="text-warning">已用自定义门槛</span> : null}
          </div>

          {/* 各档当日买 / 卖 / 净买额（紧凑一行，把高度留给图） */}
          <div className="grid shrink-0 grid-cols-2 gap-1 px-2 py-1 sm:grid-cols-4">
            {buckets.map((b) => (
              <div key={b.key} className="rounded border border-border px-1.5 py-0.5">
                <div className="flex items-baseline justify-between gap-1">
                  <span className="text-[11px] font-medium text-foreground">{b.name}</span>
                  <span className={cn("text-[12px] font-semibold tnum",
                    (b.net ?? 0) >= 0 ? "text-up" : "text-down")}>
                    {signedFlow(b.net)}
                  </span>
                </div>
                <div className="flex justify-between gap-1 text-[10px] text-muted-foreground tnum">
                  <span>买 {flowMoney(b.buy)}</span>
                  <span>卖 {flowMoney(b.sell)}</span>
                </div>
              </div>
            ))}
            {buckets.length === 0 ? (
              <div className="col-span-full rounded border border-dashed border-border px-2 py-4 text-center text-[11px] text-muted-foreground">
                {err || "还没有数据"}
              </div>
            ) : null}
          </div>

          {/* 分时图：独占剩余高度，随窗口缩放重排 */}
          <div className="min-h-0 flex-1 px-2 pb-1">
            <MoneyLinesChart
              id={`tape-flow-${preset}`}
              axis={data?.axis ?? []}
              series={series}
              extra={extra}
              lineWidth={2}
              height="100%"
              endLabels
            />
          </div>

          <div className="shrink-0 space-y-0.5 px-2 pb-1 pt-0.5 text-[10px] leading-snug text-muted-foreground">
            <div className="truncate" title={NOTES[preset]}>{NOTES[preset]}</div>
            <div className="truncate">
              {meta.stocks_ok ? `覆盖 ${meta.stocks_ok}/${meta.stocks_total} 只 · ` : ""}
              {meta.amount ? `逐笔成交额 ${flowMoney(meta.amount)} · ` : ""}
              {data?.date ? `${data.date} · ` : ""}
              盘中每 10 分钟重算一次（单次约 6 分钟）、收盘后 15:05 补跑定稿；逐笔只能取最新一个交易日，历史靠本地库留存。
            </div>
            {err ? <div className="text-warning">{err}</div> : null}
          </div>
        </FramePanel>
      </Frame>
    </div>
  );
}
