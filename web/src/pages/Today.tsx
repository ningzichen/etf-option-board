import { navigate } from "@/lib/route";
import { api } from "@/lib/api";
import { usePolling } from "@/lib/usePolling";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/reui/badge";
import { Layers } from "lucide-react";

type Reason = { name: string; value: number; text: string };

type TodayItem = {
  code: string; name: string;
  tone: "bull" | "bear" | "neutral";
  label: string; strength: string | null; strength_label: string;
  score: number; bar_pct: number; reasons: Reason[];
};

type Action = {
  etf_code: string; etf_name: string; name: string; code: string;
  side: "call" | "put"; side_label: string;
  strike: number | null; last: number | null; delta: number | null;
  leverage: number | null; spread: number | null; days: number | null;
  breakeven_pct: number | null;
  verdict: { label: string; strength_label: string };
  reasons: Reason[];
};

type Today = {
  ts: string;
  session: { key: string; label: string; late: boolean };
  threshold: number;
  items: TodayItem[];
  action: Action | null;
  notes: string[];
};

// 涨红跌绿（A 股口径）；观望用灰
const TONE_BAR: Record<string, string> = {
  bull: "bg-[#E24B4A]", bear: "bg-[#1D9E75]", neutral: "bg-muted-foreground/40",
};
const TONE_TEXT: Record<string, string> = {
  bull: "text-[#A32D2D] dark:text-[#F09595]",
  bear: "text-[#0F6E56] dark:text-[#5DCAA5]",
  neutral: "text-muted-foreground",
};

/**
 * 简洁模式：一屏结论。
 *
 * 与专业看板共用同一份后端快照（/api/today 聚合 direction / top_picks / 到期 / 告警），
 * 这里只做渲染：**文案与阈值全部由后端 humanize 下发**，前端不重算方向、不硬编码口径。
 * 一屏最多一个动作；没有明确方向时明确说「不做也是决策」。
 */
export function TodayPage() {
  const { data, error } = usePolling<Today>(() => api.today(), 15_000);

  if (!data) {
    return (
      <div className="p-6 text-sm text-muted-foreground">
        {error ? `加载失败：${error}` : "正在汇总今日结论…"}
      </div>
    );
  }

  const items = data.items ?? [];
  const acted = items.filter((i) => i.tone !== "neutral");
  const rest = items.filter((i) => i.tone === "neutral");

  return (
    <div className="mx-auto w-full max-w-[920px] space-y-3 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-semibold">今日结论</span>
        <Badge variant="outline">{data.session.label}</Badge>
        <span className="text-[11px] text-muted-foreground tnum">{data.ts} 更新 · 每 15 秒</span>
        <div className="flex-1" />
        <Button variant="outline" size="sm" onClick={() => navigate("options")}>
          <Layers aria-hidden /> 专业模式
        </Button>
      </div>

      {(data.notes ?? []).length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {data.notes.map((n) => (
            <span key={n}
              className="rounded-full border border-border px-2.5 py-1 text-[11px] text-muted-foreground">
              {n}
            </span>
          ))}
        </div>
      )}

      <div className="grid gap-3 md:grid-cols-[1.05fr_1fr]">
        <div className="rounded-xl border border-border bg-card p-3.5">
          <div className="mb-2 flex items-baseline justify-between">
            <span className="text-xs text-muted-foreground">六只标的现在怎么看</span>
            <span className="text-[10px] text-muted-foreground">|分数| &lt; {data.threshold} 视为观望</span>
          </div>
          <div className="divide-y divide-border">
            {acted.map((i) => (
              <div key={i.code} className="flex items-start gap-2.5 py-2">
                <div className={`mt-0.5 w-1 self-stretch rounded ${TONE_BAR[i.tone]}`} />
                <div className="min-w-0 flex-1">
                  <div className="flex items-baseline justify-between gap-2">
                    <span className="text-[13px] font-medium">{i.name}</span>
                    <span className={`shrink-0 text-[13px] font-medium ${TONE_TEXT[i.tone]}`}>
                      {i.label}{i.strength_label ? ` · ${i.strength_label}` : ""}
                    </span>
                  </div>
                  <div className="mt-1 text-xs text-muted-foreground">
                    {i.reasons.map((r) => r.text).join("；") || "暂无明确因子"}
                  </div>
                </div>
              </div>
            ))}
            {acted.length === 0 && (
              <div className="py-6 text-center text-xs text-muted-foreground">
                六只标的均无明确方向
              </div>
            )}
          </div>
          {rest.length > 0 && (
            <div className="mt-2 border-t border-border pt-2 text-[11px] text-muted-foreground">
              看不清 · 观望：{rest.map((i) => i.name.replace("ETF", "")).join("、")}
            </div>
          )}
        </div>

        <div className="rounded-xl border border-border bg-card p-3.5">
          <div className="mb-2 flex items-center justify-between">
            <span className="text-xs text-muted-foreground">今天只做这一笔</span>
            {data.action && (
              <Badge variant={data.action.side === "call" ? "destructive-light" : "success-light"}>
                {data.action.side_label}
              </Badge>
            )}
          </div>

          {data.action ? (
            <>
              <div className="text-sm font-medium">{data.action.name}</div>
              <div className="mt-0.5 text-[11px] text-muted-foreground">
                {data.action.etf_name} · {data.action.days != null ? `${data.action.days} 天到期 · ` : ""}
                现价 {data.action.last ?? "—"} · Δ {data.action.delta?.toFixed(2) ?? "—"}
              </div>
              <div className="mt-3 rounded-lg bg-muted/50 p-2.5 text-xs leading-relaxed">
                {data.action.breakeven_pct != null ? (
                  <>
                    <span className="text-muted-foreground">回本门槛</span> 标的需走{" "}
                    <span className="font-medium tnum">{data.action.breakeven_pct.toFixed(2)}%</span>{" "}
                    才覆盖价差与手续费
                    {data.action.spread != null && (
                      <div className="mt-0.5 text-[11px] text-muted-foreground">
                        买卖价差 {data.action.spread.toFixed(2)}% · 有效杠杆 {data.action.leverage?.toFixed(1)} 倍
                      </div>
                    )}
                  </>
                ) : (
                  <span className="text-muted-foreground">价差或杠杆数据缺失，回本门槛暂不可算</span>
                )}
              </div>
              <div className="mt-2 text-xs text-muted-foreground">
                方向依据：{data.action.reasons.map((r) => r.text).join("；")}
              </div>
              <div className="mt-2 text-[11px] text-muted-foreground">
                入场、止损、手数上限见专业模式的策略卡；先定止损再进场。
              </div>
            </>
          ) : (
            <div className="py-6 text-center text-xs text-muted-foreground">
              今天没有值得做的方向。
              <div className="mt-1">空仓等待也是决策，不必强行找单。</div>
            </div>
          )}
        </div>
      </div>

      <div className="text-[11px] leading-relaxed text-muted-foreground">
        「偏多 / 偏空」来自多因子方向评分，「强 / 中 / 弱」只表示信号一致度，不代表胜率。
        想看分项因子、资金流与期权链，点右上角「专业模式」。仅供研究学习，不构成投资建议。
      </div>
    </div>
  );
}
