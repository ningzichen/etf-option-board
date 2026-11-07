import { useCallback, useEffect, useRef, useState } from "react";
import { RefreshCw, Settings2, Sparkles } from "lucide-react";
import { Dialog } from "@/components/ui/misc";
import { Badge } from "@/components/reui/badge";
import { Frame, FramePanel } from "@/components/reui/frame";
import { api } from "@/lib/api";
import { browserChat, openLlmSettings, useLlmConfig } from "@/lib/browserLlm";
import { pct, signedFlow } from "@/lib/format";
import { cn } from "@/lib/utils";

type BriefStock = {
  name: string; code: string; lbc: number;
  main_net?: number | null; reason?: string; reason_detail?: string;
};
type BriefTheme = {
  board: string; name: string; zt: number;
  pct?: number | null; main?: number | null;
  reason_text?: string; reason_src?: string;
  stocks: BriefStock[];
};
type Brief = {
  date?: string; total?: number; headline?: string; summary?: string;
  themes?: BriefTheme[]; scope?: string; src?: string; error?: string;
};

const CACHE_PREFIX = "etf.limitup.brief.v2";
/** 数据源自带的免责声明：每条个股都带一遍太吵，统一放到弹窗底部说一次 */
const DISCLAIMER_RE = /\s*[（(]\s*免责声明[：:][^）)]*[）)]\s*/g;
const cleanReason = (text?: string | null) => (text || "").replace(DISCLAIMER_RE, "").trim();
const SYSTEM = "你是 A 股复盘助手，只做客观的信息归纳，不构成投资建议。严格只输出 JSON。";

/** 把后端数据拼成给大模型的提示词（与本文件里的渲染字段一一对应） */
function buildPrompt(b: Brief): string {
  const total = b.total ?? 0;
  const single = b.scope === "theme";
  const lines = single
    ? [`今日沪深两市共 ${total} 只股票涨停。下面是其中一个板块及其涨停股，请只分析这一个板块：`]
    : [`今日沪深两市共 ${total} 只股票涨停。下面是涨停最集中的板块，以及属于该板块的涨停股：`];
  for (const t of b.themes ?? []) {
    lines.push(`\n【${t.name}】涨停 ${t.zt} 家`
      + (t.pct == null ? "" : `，板块涨幅 ${t.pct.toFixed(2)}%`));
    for (const s of (t.stocks ?? []).slice(0, 6)) {
      const detail = (s.reason_detail || "").replace(/\s+/g, " ").slice(0, 110);
      lines.push(`- ${s.name}（${s.lbc}连板）：${s.reason || ""}${detail ? `；${detail}` : ""}`);
    }
  }
  const head = single
    ? '"headline": "一句话标题，概括这个板块今日的炒作主线（不超过 40 字）"'
    : '"headline": "一句话标题，格式：X、Y、Z涨停股领衔，成为两市明星板块（不超过 40 字，只列 3-4 个板块）"';
  const summary = single
    ? '"summary": "一句话概述：该板块今日 N 家涨停、涨幅多少、主线是什么（不超过 60 字）"'
    : '"summary": "一句话概述，格式：今日沪深两市共N只股票涨停，涨停集中在A、B、C等板块。（不超过 60 字）"';
  return `${lines.join("\n")}

请输出一个 JSON 对象（不要代码块标记），字段：
{
  ${head},
  ${summary},
  "themes": [{"name": "板块名（必须与上面完全一致）", "reason": "该板块今日的炒作原因，30-45 字，客观归纳驱动事件/逻辑，不要提个股买卖建议"}]
}
写作铁律：
1. reason 只能**归纳上面给出的这些个股原因**，不得引入上面没提到的宏观事件、政策、数据。
2. 若该板块的几只个股原因各不相同、没有共同驱动，就写"板块内个股各自炒作，暂无统一主线"。
3. 只能使用上面【】里出现过的板块名，不要新增板块。
4. headline 只列当日最强的 3-4 个板块（单板块分析时直接概括该板块）；summary 用上面给的总数。
只输出上述 JSON。`;
}

function parseJson(text: string): any {
  const t = (text || "").replace(/```json|```/g, "").trim();
  const m = t.match(/\{[\s\S]*\}/);
  if (!m) return null;
  try { return JSON.parse(m[0]); } catch { return null; }
}

/** 本机缓存（按 日期+口径+板块），避免每次开弹窗都重新调模型计费 */
function readCache(key: string): any {
  try { return JSON.parse(localStorage.getItem(`${CACHE_PREFIX}|${key}`) || "null"); }
  catch { return null; }
}
function writeCache(key: string, value: any) {
  try { localStorage.setItem(`${CACHE_PREFIX}|${key}`, JSON.stringify(value)); } catch { /* 忽略 */ }
}
function dropCache(key: string) {
  try { localStorage.removeItem(`${CACHE_PREFIX}|${key}`); } catch { /* 忽略 */ }
}

/** 标题里的时间戳 */
function stampOf(b: Brief | null, ai?: AiResult | null): string {
  return (ai?.ts || "").replace("T", " ");
}
type AiResult = { headline: string; summary: string; reasons: Record<string, string>; model: string; ts: string };

/**
 * 「主题猎手-涨停分析」弹窗。
 *
 * **数据来自后端，文案来自你配置的大模型**（设置 → 大模型：Base URL / 模型 / API Key，
 * 支持浏览器直连或看板后端代理）。**不走 opencode**：
 * - 没配大模型 → 直接显示后端给的规则拼接文案，并给「去配置大模型」入口；
 * - 配了 → 打开时调一次模型（结果按 日期+口径+板块 缓存在本机），失败也回落到规则文案。
 */
export function BriefDialog({
  open, onClose, date, mode, sort, level, theme, themeName,
}: {
  open: boolean;
  onClose: () => void;
  date?: string;
  mode: string;
  sort: string;
  level: string;
  /** 只分析这个板块（页面里永远传当前选中的板块） */
  theme?: string;
  themeName?: string;
}) {
  const cfg = useLlmConfig();
  const [data, setData] = useState<Brief | null>(null);
  const [ai, setAi] = useState<AiResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [thinking, setThinking] = useState(false);
  const [err, setErr] = useState("");
  const [aiErr, setAiErr] = useState("");
  const abortRef = useRef<AbortController | null>(null);
  useEffect(() => () => abortRef.current?.abort(), []);

  const cacheKey = `${date || ""}|${mode}|${sort}|${theme || ""}`;

  const load = useCallback(async (force = false) => {
    abortRef.current?.abort();
    const ac = new AbortController();
    abortRef.current = ac;
    setLoading(true);
    setErr(""); setAiErr(""); setAi(null);

    let payload: Brief | null = null;
    try {
      payload = await api.limitupBrief(date, mode, sort, level, false, theme) as Brief;
      if (payload?.error) setErr(payload.error);
      setData(payload);
    } catch (e: any) {
      setErr(e?.message ?? "数据加载失败");
      setLoading(false);
      return;
    }
    setLoading(false);

    if (!cfg || !payload?.themes?.length) return;

    if (!force) {
      const hit = readCache(cacheKey);
      if (hit && hit.headline) { setAi(hit); return; }
    }

    setThinking(true);
    try {
      const text = await browserChat(cfg, SYSTEM, buildPrompt(payload), ac.signal);
      const obj = parseJson(text);
      if (!obj) throw new Error("模型返回无法解析为 JSON");
      const reasons: Record<string, string> = {};
      for (const item of obj.themes || []) {
        const name = String(item?.name || "").trim();
        const reason = String(item?.reason || "").trim();
        if (name && reason) reasons[name] = reason;
      }
      const result: AiResult = {
        headline: String(obj.headline || "").trim(),
        summary: String(obj.summary || "").trim(),
        reasons,
        model: cfg.model,
        ts: new Date().toISOString(),
      };
      if (!result.headline && !Object.keys(reasons).length) throw new Error("模型没有给出有效解读");
      writeCache(cacheKey, result);
      setAi(result);
    } catch (e: any) {
      if (!ac.signal.aborted) setAiErr(e?.message ?? "大模型调用失败");
    } finally {
      if (!ac.signal.aborted) setThinking(false);
    }
  }, [date, mode, sort, level, theme, cfg, cacheKey]);

  useEffect(() => {
    if (!open) { abortRef.current?.abort(); return; }
    setData(null);
    load(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, date, mode, sort, level, theme, cfg?.model, cfg?.baseUrl, cfg?.transport]);

  const themes = data?.themes ?? [];
  const headline = ai?.headline || data?.headline || "";
  const summary = ai?.summary || data?.summary || "";
  const reasonOf = (t: BriefTheme) => ai?.reasons?.[t.name] || t.reason_text || "";

  return (
    <Dialog open={open} onClose={onClose} title={null}
      className="w-[min(96vw,980px)] p-0">
      {/* ============ 顶部标题栏 ============ */}
      <div className="flex flex-wrap items-center gap-2.5 border-b px-6 py-3.5 pr-12">
        <Sparkles size={18} className="shrink-0 text-rose-600" />
        <span className="text-base font-semibold tracking-tight">主题猎手 · 涨停分析</span>
        {themeName && <Badge variant="primary-light" size="lg" radius="full">{themeName}</Badge>}
        <span className="text-xs text-muted-foreground">{data?.date || date || ""}</span>
        <div className="ml-auto flex items-center gap-2">
          {ai && <Badge variant="secondary" size="sm" radius="full">{ai.model}</Badge>}
          <span className="text-[11px] text-muted-foreground">{stampOf(data, ai)}</span>
          <button
            onClick={() => { dropCache(cacheKey); setAi(null); load(true); }}
            disabled={loading || thinking || !cfg}
            title={cfg ? "重新生成解读（会再调一次大模型）" : "未配置大模型"}
            className="flex items-center gap-1 rounded-md border px-2 py-1 text-xs hover:bg-accent disabled:opacity-50"
          >
            <RefreshCw size={12} className={thinking ? "animate-spin" : ""} /> 重解读
          </button>
        </div>
      </div>

      <div className="max-h-[80vh] overflow-auto px-6 py-5">
        {loading && !themes.length && (
          <div className="py-20 text-center text-sm text-muted-foreground">加载当日数据…</div>
        )}

        {err && !themes.length && (
          <div className="py-12 text-center text-sm text-destructive">加载失败：{err}</div>
        )}

        {!loading && !err && !themes.length && (
          <div className="py-12 text-center text-sm text-muted-foreground">
            {themeName ? `「${themeName}」当日没有涨停个股` : "没有可分析的板块"}
          </div>
        )}

        {themes.length > 0 && (
          <div className="space-y-5">
            {/* ============ 解读主体 ============ */}
            <section className="rounded-xl border border-rose-100 bg-rose-50/40 px-5 py-4">
              <div className="flex items-center gap-2">
                <Badge variant="destructive" size="sm" radius="full">涨停解读</Badge>
                {data?.src === "rule" && !ai && (
                  <span className="text-[11px] text-muted-foreground">（规则文案，未用大模型）</span>
                )}
              </div>
              <p className="mt-2 text-lg font-semibold leading-8 text-rose-700">
                {headline || "—"}
              </p>
              {summary && (
                <p className="mt-1.5 text-sm leading-6 text-muted-foreground">{summary}</p>
              )}
            </section>

            {/* ============ 状态提示 ============ */}
            {!cfg && (
              <div className="flex items-center gap-2.5 rounded-lg border border-amber-200 bg-amber-50 px-3.5 py-2.5 text-xs leading-5 text-amber-800">
                <Settings2 size={14} className="shrink-0" />
                还没有配置大模型，当前显示的是规则拼接文案。
                <button onClick={openLlmSettings}
                  className="ml-auto shrink-0 rounded-md border border-amber-300 bg-white px-2 py-1 hover:bg-amber-100">
                  去设置大模型
                </button>
              </div>
            )}
            {cfg && thinking && (
              <div className="rounded-lg border bg-muted/40 px-3.5 py-2.5 text-xs text-muted-foreground">
                正在用 <b className="text-foreground">{cfg.model}</b> 生成解读…
              </div>
            )}
            {aiErr && (
              <div className="rounded-lg border border-destructive/30 bg-destructive/5 px-3.5 py-2.5 text-xs leading-5 text-destructive">
                大模型调用失败（已回落到规则文案）：{aiErr}
              </div>
            )}

            {/* ============ 各板块 ============ */}
            {themes.map((t) => (
              <Frame key={t.board} variant="inverse" spacing="sm">
                <FramePanel className="pb-3.5">
                  {/* FrameHeader 自带 flex-col（会竖排），这里用普通行容器保证一行 */}
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-base font-semibold">{t.name}</span>
                    <Badge variant="primary-light" size="sm">涨停 {t.zt} 家</Badge>
                    <Badge variant={(t.pct ?? 0) >= 0 ? "destructive" : "success"} size="sm">
                      涨幅 {t.pct == null ? "-" : pct(t.pct)}
                    </Badge>
                    {t.main != null && (
                      <Badge variant="secondary" size="sm">
                        主力 {signedFlow(t.main)}
                      </Badge>
                    )}
                  </div>

                  <div className="mt-2 rounded-lg bg-muted/60 px-3.5 py-2.5">
                    <div className="mb-1 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                      炒作原因
                    </div>
                    <p className="text-[13px] leading-6 text-foreground">
                      {reasonOf(t) || "—"}
                    </p>
                  </div>

                  <div className="mt-2.5 grid gap-2 md:grid-cols-2">
                    {t.stocks.map((s) => (
                      <div key={s.code}
                        className="rounded-lg border bg-card px-3.5 py-2.5 transition-colors hover:border-primary/40">
                        <div className="flex items-center gap-2">
                          <span className="text-sm font-semibold">{s.name}</span>
                          <Badge size="sm" radius="full"
                            variant={s.lbc >= 3 ? "destructive" : s.lbc === 2 ? "warning-light" : "secondary"}>
                            {s.lbc >= 2 ? `${s.lbc}连板` : "首板"}
                          </Badge>
                          <span className="text-[11px] text-muted-foreground">
                            {s.code.startsWith("6") ? "SH" : "SZ"}{s.code}
                          </span>
                          <span className={cn("ml-auto text-sm font-semibold tabular-nums",
                            (s.main_net ?? 0) >= 0 ? "text-rose-600" : "text-emerald-600")}>
                            {s.main_net == null ? "-" : signedFlow(s.main_net)}
                          </span>
                        </div>
                        <p className="mt-1.5 text-[13px] leading-6 text-slate-600">
                          <span className="font-medium text-muted-foreground">原因 </span>
                          {cleanReason(s.reason_detail) || cleanReason(s.reason) || "—"}
                        </p>
                      </div>
                    ))}
                  </div>
                </FramePanel>
              </Frame>
            ))}

            <p className="border-t pt-3 text-center text-xs leading-5 text-muted-foreground">
              {ai ? `解读由 ${ai.model} 生成` : "规则文案（未使用大模型）"}
              ，个股涨因汇总自同花顺公开信息 · 仅供参考，不构成投资建议
            </p>
          </div>
        )}
      </div>
    </Dialog>
  );
}
