import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Dialog, Tabs } from "@/components/ui/misc";
import { api } from "@/lib/api";
import { LlmSettings } from "@/components/LlmSettings";
import { PushSettings } from "@/components/PushSettings";

/** 单笔风险%三档，与风险偏好档联动：1%→保守 / 2%→标准 / 3%→激进。 */
const RISK_TABS = [
  { key: "1", label: "保守 1%" },
  { key: "2", label: "标准 2%" },
  { key: "3", label: "激进 3%" },
];

// 敞口档位/规模的选项文案由后端下发（services/decision.py 是唯一定义处），
// 这里只做打开弹窗前的兜底，避免首次渲染前 Tabs 空白。
const TIER_TABS_FALLBACK = [
  { key: "flat", label: "空仓" },
  { key: "etf", label: "持有ETF" },
  { key: "call", label: "持认购" },
  { key: "put", label: "持认沽" },
];
const SIZE_TABS_FALLBACK = [
  { key: "light", label: "轻" },
  { key: "mid", label: "中" },
  { key: "heavy", label: "重" },
];

const fmt = (v: number) => v.toLocaleString("zh-CN", { maximumFractionDigits: 2 });

/**
 * 用户设置：总资金、单笔风险%、敞口档位与档位规模。
 *
 * 总资金 → 单笔风险预算 → 手数上限；敞口档位回答「我现在持有什么」（见 docs/adr/0001），
 * 档位规模决定估算敞口的大小。存于 data/settings.json，命中 .gitignore 的 data/*.json
 * 规则，不会随 auto-backup 进入 git 历史。
 */
export function SettingsDialog({ open, onClose, initialTab = "trading" }: {
  open: boolean; onClose: () => void; initialTab?: "trading" | "llm" | "push";
}) {
  const [tab, setTab] = useState(initialTab);
  useEffect(() => { if (open) setTab(initialTab); }, [open, initialTab]);
  const [capital, setCapital] = useState("");
  const [risk, setRisk] = useState("1");
  const [tier, setTier] = useState("flat");
  const [size, setSize] = useState("mid");
  const [tierTabs, setTierTabs] = useState(TIER_TABS_FALLBACK);
  const [sizeTabs, setSizeTabs] = useState(SIZE_TABS_FALLBACK);
  const [profile, setProfile] = useState<string | null>(null);
  const [configured, setConfigured] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    let alive = true;
    setErr(null);
    setOk(null);
    (async () => {
      try {
        const d = await api.settings();
        if (!alive) return;
        if (d?.error) { setErr(d.error); return; }
        setCapital(d?.total_capital != null ? String(d.total_capital) : "");
        setRisk(String(d?.risk_pct ?? 1));
        setTier(d?.tier ?? "flat");
        setSize(d?.size ?? "mid");
        setProfile(d?.tier_label && d?.size_label ? `${d.tier_label} · ${d.size_label}` : null);
        if (Array.isArray(d?.tier_choices) && d.tier_choices.length) setTierTabs(d.tier_choices);
        if (Array.isArray(d?.size_choices) && d.size_choices.length) setSizeTabs(d.size_choices);
        setConfigured(!!d?.configured);
      } catch (e: any) {
        if (alive) setErr(e?.message ?? "加载失败");
      }
    })();
    return () => { alive = false; };
  }, [open]);

  const onSave = async () => {
    setBusy(true);
    setErr(null);
    setOk(null);
    try {
      const r = await api.saveSettings({
        total_capital: capital.trim() === "" ? null : capital.trim(),
        risk_pct: Number(risk),
        tier,
        size,
      });
      if (r?.error) { setErr(r.error); return; }
      setCapital(r?.total_capital != null ? String(r.total_capital) : "");
      setRisk(String(r?.risk_pct ?? risk));
      setTier(r?.tier ?? tier);
      setSize(r?.size ?? size);
      setProfile(r?.tier_label && r?.size_label ? `${r.tier_label} · ${r.size_label}` : null);
      setConfigured(!!r?.configured);
      setOk("已保存");
    } catch (e: any) {
      setErr(e?.message ?? "保存失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onClose={onClose} title="设置" className="w-[min(92vw,460px)]">
      <div className="mb-4">
        <Tabs tabs={[{ key: "trading", label: "交易设置" }, { key: "llm", label: "大模型" }, { key: "push", label: "推送" }]}
          value={tab} onChange={(v) => setTab(v as "trading" | "llm" | "push")} />
      </div>
      {tab === "llm" ? <LlmSettings /> : tab === "push" ? <PushSettings /> : (
      <div className="space-y-4 text-xs">
        <div className="space-y-1.5">
          <div className="font-medium">总资金（元）</div>
          <input
            type="text"
            inputMode="decimal"
            value={capital}
            onChange={(e) => setCapital(e.target.value)}
            placeholder="如 500000"
            aria-label="总资金"
            className="tnum h-8 w-full rounded-md border border-input bg-transparent px-2 text-xs outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
          <div className="text-muted-foreground">用于反推单笔风险预算与手数上限；留空表示未设置。</div>
        </div>

        <div className="space-y-1.5">
          <div className="font-medium">单笔风险%</div>
          <Tabs tabs={RISK_TABS} value={risk} onChange={setRisk} />
          <div className="text-muted-foreground">
            与风险偏好档联动（保守 / 标准 / 激进），决定组合 Greeks 的限额。
          </div>
        </div>

        <div className="space-y-1.5">
          <div className="font-medium">敞口档位</div>
          <Tabs tabs={tierTabs} value={tier} onChange={setTier} />
          <div className="text-muted-foreground">
            当前持有什么：组合 Greeks 敞口按此档位估算（估算值，非逐笔持仓）。
          </div>
        </div>

        {tier !== "flat" && (
          <div className="space-y-1.5">
            <div className="font-medium">档位规模</div>
            <Tabs tabs={sizeTabs} value={size} onChange={setSize} />
            <div className="text-muted-foreground">
              该档位动用的资金比例：轻 10% / 中 30% / 重 60%。
            </div>
          </div>
        )}

        <div className="flex items-center gap-2">
          <Button size="sm" onClick={onSave} disabled={busy}>
            {busy ? "保存中…" : "保存"}
          </Button>
          {configured && !ok && <span className="text-muted-foreground">当前已配置</span>}
          {ok && <span className="text-success-foreground dark:text-success">{ok}</span>}
          {err && <span className="text-destructive">{err}</span>}
        </div>

        <div className="border-t border-border pt-2 leading-relaxed text-muted-foreground">
          存储于本机 data/settings.json，不进 git 历史。单笔风险预算 = 总资金 × 风险%，
          手数上限由它除以单张最大亏损得出。
          {configured && (
            <div className="mt-1 text-foreground">
              当前：总资金 {fmt(Number(capital) || 0)} 元 · 单笔风险 {risk}%
              {profile ? ` · 敞口 ${profile}` : ""}
            </div>
          )}
        </div>
      </div>
      )}
    </Dialog>
  );
}
