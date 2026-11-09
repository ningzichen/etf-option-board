/* 缅A看板 Service Worker：通知、PWA 与浏览器直连模型；不缓存请求或模型配置。 */
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

// 模型请求走 MessageChannel，不经看板的 HTTP 路由。密钥仅存在于本次请求内存中。
self.addEventListener("message", (e) => {
  const port = e.ports[0];
  if (!port || !e.source?.url || new URL(e.source.url).origin !== self.location.origin) return;
  if (e.data?.type === "etf:llm:ping") {
    port.postMessage({ version: 1 });
    port.close();
    return;
  }
  if (e.data?.type !== "etf:llm:request") return;
  e.waitUntil((async () => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 300_000);
    port.onmessage = (message) => { if (message.data?.type === "abort") controller.abort(); };
    try {
      const req = e.data.request;
      const url = new URL(req.url);
      if (!["http:", "https:"].includes(url.protocol) || url.origin === self.location.origin ||
          url.username || url.password || url.search || url.hash || typeof req.body !== "string") {
        throw new Error("请求地址无效");
      }
      const r = await fetch(url.href, {
        method: "POST", headers: req.headers, body: req.body, signal: controller.signal,
        mode: "cors", credentials: "omit", cache: "no-store", redirect: "error",
        referrer: "", referrerPolicy: "no-referrer",
      });
      port.postMessage({ status: r.status, text: await r.text() });
    } catch {
      port.postMessage({ error: controller.signal.aborted ? "模型请求已取消或超时" :
        "浏览器直连失败：请检查地址、网络、HTTPS 和服务端 CORS。无法调整跨域配置时，可在模型设置中选择「看板后端代理」。本次请求未转交后端。" });
    } finally {
      clearTimeout(timer);
      port.close();
    }
  })());
});

// 只做透传，且必须跳过 /api（SSE 与动态接口交给浏览器/后端，避免流被拦截）
self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET" || new URL(e.request.url).pathname.startsWith("/api/")) return;
  e.respondWith(fetch(e.request).catch(() => Response.error()));
});

self.addEventListener("notificationclick", (e) => {
  const data = e.notification.data || {};
  const code = data.etf_code;
  e.notification.close();
  e.waitUntil(
    (async () => {
      const list = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      const target = list.find((c) => c.url.includes(location.host)) || list[0];
      if (target) {
        await target.focus();
        if (code) target.postMessage({ type: "etf:select", code });
        return;
      }
      const win = await self.clients.openWindow("/");
      if (win && code) win.postMessage({ type: "etf:select", code });
    })(),
  );
});
