import { useEffect, useState } from "react";
import { api } from "@/lib/api";

export type LlmProtocol = "openai" | "anthropic";
export type LlmTransport = "direct" | "proxy";
export interface LlmConfig {
  protocol: LlmProtocol;
  transport: LlmTransport;
  baseUrl: string;
  model: string;
  apiKey: string;
  maxTokens: number;
}

const STORAGE_KEY = "etf-board.llm-config.v1";
const CHANGE_EVENT = "etf:llm-config-changed";
const REQUEST_TIMEOUT = 300_000;
export const defaultMaxTokens = (protocol: LlmProtocol) => protocol === "anthropic" ? 4096 : 16384;
export const EMPTY_LLM_CONFIG: LlmConfig = { protocol: "openai", transport: "direct", baseUrl: "", model: "", apiKey: "", maxTokens: defaultMaxTokens("openai") };
export const protocolLabel = (p: LlmProtocol) => p === "anthropic" ? "Anthropic" : "OpenAI Chat";
export const transportLabel = (t: LlmTransport) => t === "proxy" ? "看板后端代理" : "浏览器直连";

export function llmEndpoint(config: LlmConfig): string {
  let url: URL;
  try { url = new URL(config.baseUrl.trim()); }
  catch { throw new Error("Base URL 需填写完整的 http:// 或 https:// 地址"); }
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
    throw new Error("Base URL 只支持 HTTP/HTTPS，不能包含账号、密码、查询参数或锚点");
  }
  if (url.origin === location.origin) throw new Error("请填写模型服务地址，不能使用看板自身的地址");
  const path = url.pathname.replace(/\/+$/, "");
  const suffix = config.protocol === "anthropic" ? "/messages" : "/chat/completions";
  if ((path.endsWith("/messages") || path.endsWith("/chat/completions")) && !path.endsWith(suffix)) {
    throw new Error("接口路径与所选兼容协议不一致");
  }
  url.pathname = path.endsWith(suffix) ? path : `${path || "/v1"}${suffix}`;
  return url.href;
}

export function validateLlmConfig(config: LlmConfig): LlmConfig {
  if (!["openai", "anthropic"].includes(config.protocol)) throw new Error("请选择兼容协议");
  // 旧配置没有 transport，继续使用浏览器直连，避免升级后自动把密钥传给后端。
  const transport = config.transport ?? "direct";
  if (!["direct", "proxy"].includes(transport)) throw new Error("请选择请求方式");
  const maxTokens = config.maxTokens ?? defaultMaxTokens(config.protocol);
  if (!Number.isInteger(maxTokens) || maxTokens < 1024 || maxTokens > 65536) {
    throw new Error("最大输出 Token 需为 1024–65536 之间的整数，并在模型支持的范围内");
  }
  const out = { protocol: config.protocol, transport, baseUrl: config.baseUrl.trim(), model: config.model.trim(), apiKey: config.apiKey.trim(), maxTokens };
  llmEndpoint(out);
  if (!out.model) throw new Error("请填写模型 ID");
  if (/[\r\n]/.test(out.apiKey)) throw new Error("API Key 不能包含换行");
  return out;
}

export function loadLlmConfig(): LlmConfig | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? validateLlmConfig(JSON.parse(raw)) : null;
  } catch { return null; }
}

/** 这两个写入口只操作当前浏览器，从不调用 /api/settings。 */
export function saveLlmConfig(config: LlmConfig): void {
  const value = validateLlmConfig(config);
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(value)); }
  catch { throw new Error("浏览器不允许保存本地配置，请检查站点存储权限"); }
  window.dispatchEvent(new Event(CHANGE_EVENT));
}

export function clearLlmConfig(): void {
  try { localStorage.removeItem(STORAGE_KEY); }
  catch { throw new Error("浏览器不允许清除本地配置，请检查站点存储权限"); }
  window.dispatchEvent(new Event(CHANGE_EVENT));
}

export function useLlmConfig(): LlmConfig | null {
  const [config, setConfig] = useState(loadLlmConfig);
  useEffect(() => {
    const sync = () => setConfig(loadLlmConfig());
    const onStorage = (e: StorageEvent) => { if (e.key === STORAGE_KEY || e.key === null) sync(); };
    window.addEventListener(CHANGE_EVENT, sync);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener(CHANGE_EVENT, sync);
      window.removeEventListener("storage", onStorage);
    };
  }, []);
  return config;
}

export function openLlmSettings(): void {
  window.dispatchEvent(new Event("etf:open-llm-settings"));
}

interface ModelRequest { url: string; headers: Record<string, string>; body: string }
interface ModelReply { status?: number; text?: string; error?: string }

function modelRequest(config: LlmConfig, system: string, user: string): ModelRequest {
  const headers: Record<string, string> = { "Content-Type": "application/json", Accept: "application/json" };
  // 推理模型的 max_tokens 同时包含推理与最终正文，不能只按最终 JSON 的长度预算。
  const body: Record<string, unknown> = { model: config.model, max_tokens: config.maxTokens, stream: false };
  if (config.protocol === "anthropic") {
    if (config.apiKey) headers["x-api-key"] = config.apiKey;
    headers["anthropic-version"] = "2023-06-01";
    headers["anthropic-dangerous-direct-browser-access"] = "true";
    body.system = system;
    body.messages = [{ role: "user", content: user }];
  } else {
    if (config.apiKey) headers.Authorization = `Bearer ${config.apiKey}`;
    body.messages = [{ role: "system", content: system }, { role: "user", content: user }];
  }
  // opencode zen 的免费模型必须带会话标识，否则会被拦（"Free models work in OpenCode"）。
  try {
    if (new URL(llmEndpoint(config)).hostname.endsWith("opencode.ai")) {
      headers["x-opencode-session"] = crypto.randomUUID().replace(/-/g, "");
    }
  } catch { /* URL 不合法时留给 validateLlmConfig 报错 */ }
  return { url: llmEndpoint(config), headers, body: JSON.stringify(body) };
}

// 激活与握手都发生在发送模型请求之前；旧 SW / 非安全上下文只回退到页面直接 fetch。
// 已经发出的模型请求绝不自动重试，避免重复计费。
async function modelWorker(): Promise<ServiceWorker | null> {
  if (!window.isSecureContext || !("serviceWorker" in navigator)) return null;
  try {
    const reg = await navigator.serviceWorker.register("/sw.js", { scope: "/", updateViaCache: "none" });
    const pending = reg.installing || reg.waiting;
    if (pending && pending.state !== "activated") {
      await new Promise<void>((resolve) => {
        const done = () => { clearTimeout(timer); pending.removeEventListener("statechange", check); resolve(); };
        const check = () => { if (["activated", "redundant"].includes(pending.state)) done(); };
        const timer = setTimeout(done, 4000);
        pending.addEventListener("statechange", check);
        check();
      });
    }
    const worker = reg.active;
    if (!worker) return null;
    return await new Promise<ServiceWorker | null>((resolve) => {
      const channel = new MessageChannel();
      const done = (value: ServiceWorker | null) => { clearTimeout(timer); channel.port1.close(); resolve(value); };
      const timer = setTimeout(() => done(null), 1000);
      channel.port1.onmessage = (e) => done(e.data?.version === 1 ? worker : null);
      worker.postMessage({ type: "etf:llm:ping" }, [channel.port2]);
    });
  } catch { return null; }
}

async function sendFromBrowser(request: ModelRequest, signal: AbortSignal): Promise<ModelReply> {
  const worker = await modelWorker();
  signal.throwIfAborted();
  if (!worker) {
    const r = await fetch(request.url, {
      method: "POST", headers: request.headers, body: request.body, signal,
      mode: "cors", credentials: "omit", cache: "no-store", redirect: "error",
      referrer: "", referrerPolicy: "no-referrer",
    });
    return { status: r.status, text: await r.text() };
  }
  return new Promise((resolve, reject) => {
    const channel = new MessageChannel();
    const clean = () => { channel.port1.close(); signal.removeEventListener("abort", abort); };
    const abort = () => { channel.port1.postMessage({ type: "abort" }); clean(); reject(signal.reason); };
    signal.addEventListener("abort", abort, { once: true });
    channel.port1.onmessage = (e) => { clean(); resolve(e.data); };
    channel.port1.onmessageerror = () => { clean(); reject(new Error("浏览器模型通道响应无效，请重试")); };
    try { worker.postMessage({ type: "etf:llm:request", request }, [channel.port2]); }
    catch { clean(); reject(new Error("浏览器模型通道已更新，请重试")); }
  });
}

async function modelText(config: LlmConfig, system: string, user: string, signal: AbortSignal, test = false): Promise<string> {
  let reply: ModelReply;
  try {
    reply = config.transport === "proxy" ? await api.llmProxy({
      protocol: config.protocol, baseUrl: config.baseUrl, model: config.model, apiKey: config.apiKey,
      system, user, test, maxTokens: config.maxTokens,
    }, signal) : await sendFromBrowser(modelRequest(config, system, user), signal);
  }
  catch (e) {
    if (signal.aborted) throw signal.reason;
    if (e instanceof TypeError) throw new Error(config.transport === "proxy" ?
      "无法连接看板后端，请检查看板服务与网络" :
      "浏览器直连失败：请检查地址、网络、HTTPS 和服务端 CORS。无法调整跨域配置时，可在模型设置中选择「看板后端代理」。本次请求未转交后端。");
    throw e;
  }
  if (reply.error) throw new Error(reply.error);
  const status = reply.status || 0;
  // 不回显供应商原始错误正文，避免网关把请求头或密钥反射到界面。
  if (status < 200 || status >= 300) {
    const hint = status === 401 || status === 403 ? "请检查 API Key 和模型访问权限" :
      status === 429 ? "额度或速率受限，请稍后重试" : status === 404 ? "请检查 Base URL 和模型 ID" : "请检查协议、模型参数及供应商服务状态";
    throw new Error(`模型服务返回 HTTP ${status}：${hint}`);
  }
  let data: any;
  try { data = JSON.parse(reply.text || ""); }
  catch { throw new Error("模型服务返回的不是 JSON，请检查接口地址与兼容协议"); }
  if (data?.error) throw new Error("模型服务返回错误，请检查模型配置或联系服务提供方");
  const choice = data?.choices?.[0];
  const content = config.protocol === "anthropic" ? data?.content : choice?.message?.content;
  const text = typeof content === "string" ? content : Array.isArray(content) ?
    content.filter((p) => p?.type === "text" && typeof p.text === "string").map((p) => p.text).join("\n") : "";
  const stop = config.protocol === "anthropic" ? data?.stop_reason : choice?.finish_reason;
  const thinking = config.protocol === "anthropic" ?
    Array.isArray(content) && content.some((p) => p?.type === "thinking" || p?.type === "redacted_thinking") :
    !!choice?.message?.reasoning_content || !!choice?.message?.reasoning || data?.usage?.completion_tokens_details?.reasoning_tokens > 0;
  if (stop === "length" || stop === "max_tokens") {
    const used = config.protocol === "anthropic" ? data?.usage?.output_tokens : data?.usage?.completion_tokens;
    const usage = Number.isSafeInteger(used) && used >= 0 ? `，已生成 ${used} token` : "";
    throw new Error(`模型输出被截断（本次上限 ${config.maxTokens} token${usage}）${thinking ? "，推理过程也会占用输出额度" : ""}。请在模型设置中提高「最大输出 Token」（需在模型支持范围内）；若已达模型上限，请缩短输入或更换模型。`);
  }
  if (stop === "content_filter" || stop === "refusal" || choice?.message?.refusal) {
    throw new Error("模型拒绝回答或响应被服务方过滤，请调整请求或更换模型");
  }
  if (!text.trim()) {
    if (thinking) throw new Error("模型只返回了推理过程，没有最终正文。请提高「最大输出 Token」或更换模型；推理过程不能作为决策结果。");
    throw new Error("模型未返回可用文本，请确认模型支持所选协议或稍后重试");
  }
  return text;
}

/** 与后端规则一致：动作与手数由上下文约束，模型结果留在浏览器校正、展示。 */
function parseDecision(text: string, guardrails: any): any {
  const cleaned = text.trim().replace(/^```(?:json)?\s*|\s*```$/g, "");
  let parsed: any;
  try { parsed = JSON.parse(cleaned); }
  catch {
    try { parsed = JSON.parse(cleaned.slice(cleaned.indexOf("{"), cleaned.lastIndexOf("}") + 1)); }
    catch { throw new Error("模型决策无法解析为 JSON，请重试或更换模型"); }
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("模型决策必须是 JSON 对象");
  const labels = guardrails?.action_labels || {};
  if (!Object.prototype.hasOwnProperty.call(labels, parsed.action)) throw new Error("模型未返回有效的决策动作，请重试");
  const string = (v: unknown) => typeof v === "string" ? v : "";
  const number = (v: unknown) => typeof v === "number" && Number.isFinite(v) ? v : null;
  const list = (v: unknown) => Array.isArray(v) ? v.filter((x) => typeof x === "string") : [];
  const leg = (v: any) => v && typeof v === "object" ? { code: string(v.code), name: string(v.name), strike: number(v.strike) } : null;
  // 显式选取可展示字段，防止供应商返回对象/数组导致 React 渲染崩溃。
  const out: any = { action: parsed.action, confidence: Math.min(100, Math.max(0, number(parsed.confidence) || 0)),
    contracts: number(parsed.contracts), legs: { long: leg(parsed.legs?.long), short: leg(parsed.legs?.short) },
    reasons: list(parsed.reasons), risks: list(parsed.risks), adjustments: [] };
  for (const field of ["contract_code", "contract_name", "hold_window", "summary"]) out[field] = string(parsed[field]);
  for (const field of ["entry", "stop", "target", "stop_pct", "target_pct"]) out[field] = number(parsed[field]);
  const want = guardrails?.action;
  if (want && want !== out.action) {
    out.model_action = out.action;
    out.adjustments.push(`模型建议「${labels[out.action]}」与规则层策略「${labels[want]}」不一致，已按规则层为准。`);
    out.action = want;
    // 换方向/策略后旧合约和点位不可沿用，避免展示自相矛盾的买单。
    out.contract_code = out.contract_name = "";
    out.legs = null;
    for (const field of ["entry", "stop", "target", "stop_pct", "target_pct"]) out[field] = null;
  }
  const cap = guardrails?.max_contracts;
  if (Number.isInteger(cap) && cap >= 0 && out.contracts != null && out.contracts > cap) {
    out.model_contracts = out.contracts;
    out.adjustments.push(`模型建议 ${out.contracts} 张超过手数上限 ${cap} 张，已收敛到上限。`);
    out.contracts = cap;
  }
  if (out.contracts != null) out.contracts = Math.max(0, Math.floor(out.contracts));
  out.direction = out.action === "wait" ? "neutral" : ["buy_call", "bull_call_spread"].includes(out.action) ? "call" : "put";
  if (out.action === "wait") {
    out.contract_code = out.contract_name = "";
    out.legs = null;
    out.contracts = 0;
    for (const field of ["entry", "stop", "target", "stop_pct", "target_pct"]) out[field] = null;
  }
  return out;
}

async function withTimeout<T>(signal: AbortSignal, task: (signal: AbortSignal) => Promise<T>): Promise<T> {
  const controller = new AbortController();
  const abort = () => controller.abort(signal.reason);
  signal.addEventListener("abort", abort, { once: true });
  if (signal.aborted) abort();
  const timer = setTimeout(() => controller.abort(new Error("模型请求超过 300 秒，请稍后重试或更换响应更快的模型")), REQUEST_TIMEOUT);
  try { controller.signal.throwIfAborted(); return await task(controller.signal); }
  finally { clearTimeout(timer); signal.removeEventListener("abort", abort); }
}

export async function testLlmConnection(config: LlmConfig, signal: AbortSignal): Promise<void> {
  const valid = validateLlmConfig(config);
  await withTimeout(signal, (s) => modelText(valid, "你是连接测试助手。", "请只回复 OK。", s, true));
}

export async function browserDecision(code: string, config: LlmConfig, signal: AbortSignal): Promise<any> {
  const valid = validateLlmConfig(config);
  return withTimeout(signal, async (s) => {
    const context = await api.llmContext(code, s);
    if (context.error) throw new Error(context.error);
    if (!context.system || !context.user) throw new Error("行情上下文不完整，请稍后重试");
    const text = await modelText(valid, context.system, context.user, s);
    return { decision: { ...parseDecision(text, context.guardrails), _model: valid.model,
      _via: valid.transport === "proxy" ? "proxy" : "browser" }, ts: context.ts };
  });
}

/**
 * 通用文本补全：用「大模型」设置里的同一套配置（Base URL / 模型 / API Key），
 * 走浏览器直连或看板后端代理，**不经过 opencode**。
 * 供涨停复盘的「涨停分析」这类纯文本场景使用。
 */
export async function browserChat(config: LlmConfig, system: string, user: string,
                                  signal: AbortSignal): Promise<string> {
  const valid = validateLlmConfig(config);
  return withTimeout(signal, (s) => modelText(valid, system, user, s));
}
