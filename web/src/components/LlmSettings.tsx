import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Tabs } from "@/components/ui/misc";
import {
  clearLlmConfig, defaultMaxTokens, EMPTY_LLM_CONFIG, llmEndpoint, loadLlmConfig, saveLlmConfig,
  testLlmConnection, type LlmConfig, type LlmProtocol, type LlmTransport,
} from "@/lib/browserLlm";

const INPUT = "h-8 w-full rounded-md border border-input bg-transparent px-2 text-xs outline-none focus-visible:ring-2 focus-visible:ring-ring";

/**
 * 快速填入的预设。API Key 一律留空由用户自己粘。
 *
 * - zen 免费模型：**必须走看板后端代理**（它不给浏览器 CORS），且请求要带
 *   `x-opencode-session`（代码按域名自动补）；额度用尽返回 429，次日 08:00 恢复。
 * - DeepSeek 官方：实测允许浏览器跨域（OPTIONS 会回显 `access-control-allow-origin`），
 *   所以默认**直连**，Key 连看板后端都不经过。
 */
const PRESETS = [
  {
    label: "opencode zen 免费模型",
    patch: { protocol: "openai" as const, transport: "proxy" as const,
      baseUrl: "https://opencode.ai/zen/v1", model: "deepseek-v4.1-flash-free" },
    hint: "免费用量每天 08:00（北京时间）恢复；Key 取 ~/.local/share/opencode/auth.json 的 opencode-go。"
      + "额度用尽会 429，此时涨停分析退回规则文案。",
  },
  {
    label: "DeepSeek 官方",
    patch: { protocol: "openai" as const, transport: "direct" as const,
      baseUrl: "https://api.deepseek.com/v1", model: "deepseek-chat" },
    hint: "platform.deepseek.com 申请 Key；直连（服务端已允许跨域），Key 不经过看板后端。"
      + "要更强推理可把模型 ID 换成 deepseek-reasoner（更慢、更贵）。",
  },
] as const;

export function LlmSettings() {
  const [config, setConfig] = useState<LlmConfig>(() => loadLlmConfig() || { ...EMPTY_LLM_CONFIG });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);
  const change = <K extends keyof LlmConfig,>(field: K, value: LlmConfig[K]) => {
    request.current?.abort();
    request.current = null;
    setBusy(false);
    setStatus(""); setError("");
    setConfig((old) => ({ ...old,
      ...(field === "protocol" && old.maxTokens === defaultMaxTokens(old.protocol) ?
        { maxTokens: defaultMaxTokens(value as LlmProtocol) } : {}), [field]: value }));
  };
  let endpoint = "";
  try { if (config.baseUrl.trim()) endpoint = llmEndpoint(config); } catch { /* 保存或测试时显示校验错误 */ }
  const save = () => {
    setStatus(""); setError("");
    try { saveLlmConfig(config); setStatus("已保存到当前浏览器"); }
    catch (e: any) { setError(e.message); }
  };
  const clear = () => {
    setStatus(""); setError("");
    try {
      clearLlmConfig(); request.current?.abort(); request.current = null; setBusy(false);
      setConfig({ ...EMPTY_LLM_CONFIG }); setStatus("已清除当前浏览器的模型配置");
    } catch (e: any) { setError(e.message); }
  };
  const test = async () => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setBusy(true); setStatus(""); setError("");
    try {
      await testLlmConnection(config, controller.signal);
      if (!controller.signal.aborted) setStatus("连接成功，模型已返回文本；点击保存后用于决策");
    } catch (e: any) {
      if (!controller.signal.aborted) setError(e.message || "连接测试失败");
    } finally {
      if (request.current === controller) { request.current = null; setBusy(false); }
    }
  };

  /** 一键填入某个厂商的参数（API Key 一律由用户自己粘） */
  const usePreset = (preset: (typeof PRESETS)[number]) => {
    setStatus(""); setError("");
    setConfig((old) => ({ ...old, ...preset.patch, maxTokens: defaultMaxTokens("openai") }));
    setStatus(`已填入${preset.label}参数，粘贴 API Key 后点「保存模型配置」`);
  };

  return <div className="space-y-4 text-xs">
    <div className="rounded-md bg-muted/40 px-3 py-2 leading-relaxed text-muted-foreground">
      配置和 API Key 仅保存在当前浏览器的本地存储中。行情快照由看板提供，模型请求按下方选择的方式发送。
      <br />填写 Base URL / 模型 ID / API Key 即可，**期权决策**与**涨停复盘的涨停分析**都用这套配置
      （涨停复盘不再依赖 opencode）。
    </div>
    <div className="space-y-1.5">
      <div className="font-medium">快速填入</div>
      <div className="flex flex-wrap gap-2">
        {PRESETS.map((p) => (
          <Button key={p.label} size="sm" variant="outline" onClick={() => usePreset(p)}>
            {p.label}
          </Button>
        ))}
      </div>
      <div className="space-y-1 leading-relaxed text-muted-foreground">
        {PRESETS.map((p) => (
          <div key={p.label}><b className="text-foreground">{p.label}</b>：{p.hint}</div>
        ))}
      </div>
    </div>
    <div className="space-y-1.5">
      <div className="font-medium">请求方式</div>
      <Tabs tabs={[{ key: "proxy", label: "看板后端代理" }, { key: "direct", label: "浏览器直连" }]}
        value={config.transport} onChange={(v) => change("transport", v as LlmTransport)} />
      <div className="leading-relaxed text-muted-foreground">
        {config.transport === "proxy" ?
          "适合无法调整模型服务跨域设置的情况。API Key 仅随本次请求交给看板后端转发，不保存到服务器，也不写入日志。" :
          "适合已允许浏览器跨域的模型服务。请求直接发送给模型服务，API Key 与模型请求不经过看板后端。"}
      </div>
    </div>
    <div className="space-y-1.5">
      <div className="font-medium">兼容协议</div>
      <Tabs tabs={[{ key: "openai", label: "OpenAI Chat" }, { key: "anthropic", label: "Anthropic" }]}
        value={config.protocol} onChange={(v) => change("protocol", v as LlmProtocol)} />
    </div>
    <label className="block space-y-1.5">
      <span className="font-medium">Base URL</span>
      <input className={INPUT} aria-label="模型 Base URL" autoComplete="off" spellCheck={false}
        value={config.baseUrl} onChange={(e) => change("baseUrl", e.target.value)} placeholder="https://你的模型服务/v1" />
      <span className="block leading-relaxed text-muted-foreground">填写 API 根路径（如 /v1），也支持完整接口地址；仅填域名时自动补 /v1。</span>
      {endpoint && <span className="block break-all text-[11px] text-muted-foreground">请求地址：{endpoint}</span>}
    </label>
    <label className="block space-y-1.5">
      <span className="font-medium">模型 ID</span>
      <input className={INPUT} aria-label="模型 ID" autoComplete="off" spellCheck={false}
        value={config.model} onChange={(e) => change("model", e.target.value)} placeholder="服务提供的模型 ID" />
    </label>
    <label className="block space-y-1.5">
      <span className="font-medium">API Key</span>
      <input className={INPUT} aria-label="模型 API Key" type="password" autoComplete="new-password" spellCheck={false}
        value={config.apiKey} onChange={(e) => change("apiKey", e.target.value)} placeholder="服务无需鉴权时可留空" />
    </label>
    <label className="block space-y-1.5">
      <span className="font-medium">最大输出 Token</span>
      <input className={INPUT} aria-label="最大输出 Token" type="number" min={1024} max={65536} step={1}
        value={config.maxTokens || ""} onChange={(e) => change("maxTokens", Number(e.target.value))} />
      <span className="block leading-relaxed text-muted-foreground">推理模型的思考与最终正文共用此额度；DeepSeek 等推理模型建议从 16384 起。请勿超过模型支持的上限，连接测试也使用此设置。</span>
    </label>
    <div className="flex flex-wrap items-center gap-2">
      <Button size="sm" onClick={save}>保存模型配置</Button>
      <Button size="sm" variant="outline" onClick={test} disabled={busy}>{busy ? "测试中…" : "测试连接"}</Button>
      {busy && <Button size="sm" variant="ghost" onClick={() => {
        request.current?.abort(); request.current = null; setBusy(false); setStatus("已取消测试");
      }}>取消测试</Button>}
      <Button size="sm" variant="ghost" onClick={clear}>清除配置</Button>
    </div>
    {status && <div role="status" className="text-success-foreground dark:text-success">{status}</div>}
    {error && <div role="alert" className="break-words leading-relaxed text-destructive">{error}</div>}
    <div className="border-t border-border pt-2 leading-relaxed text-muted-foreground">
      {config.transport === "proxy" ?
        "模型地址由看板服务器访问；localhost / 127.0.0.1 指看板服务器本机。代理不记录 API Key、提示词、模型响应或原始错误正文。" :
        "请求不携带 Referer 或 Cookie。模型服务仍需允许浏览器跨域请求（CORS）；Service Worker 无法绕过跨域限制。"}
      <div className="mt-1">测试连接仅发送简短测试提示。清除站点数据会删除配置，不同设备或访问地址需分别配置。</div>
    </div>
    {config.transport === "direct" && <details className="rounded-md border border-border px-3 py-2">
      <summary className="cursor-pointer font-medium">如何配置模型服务的跨域（CORS）</summary>
      <div className="mt-2 space-y-2 leading-relaxed text-muted-foreground">
        <p>在模型服务或其网关设置以下响应头，放行当前看板地址：</p>
        <pre className="whitespace-pre-wrap break-all rounded bg-muted/40 p-2 text-[11px] text-foreground">{[
          `Access-Control-Allow-Origin: ${location.origin}`,
          "Access-Control-Allow-Methods: POST, OPTIONS",
          `Access-Control-Allow-Headers: ${config.protocol === "anthropic" ?
            "Content-Type, X-Api-Key, Anthropic-Version, Anthropic-Dangerous-Direct-Browser-Access" : "Content-Type, Authorization"}`,
          "Vary: Origin",
        ].join("\n")}</pre>
        <p>OPTIONS 预检无需 API Key，应直接返回 200 或 204；实际 POST 响应（包括错误响应）也要带 Allow-Origin。</p>
        <p>Allow-Origin 只能返回一份、一个值。网关和应用不要重复添加；Authorization 必须显式列出，不能只写 Allow-Headers: *。</p>
        <p>通过 HTTPS 打开看板时，模型服务也应使用 HTTPS。跨设备访问地址改变后，需同步调整允许的 Origin。</p>
      </div>
    </details>}
  </div>;
}
