/* 告警长连接（全局单例）。
 *
 * 为什么不在 AlertCenter 里订阅：页面标签切到板块资金后 AlertCenter 会卸载，
 * SSE 一断就再也收不到告警（盘中等于静默）。这里把连接与「打扰」副作用
 * （声音 / 系统通知 / toast）提到全局，列表与弹窗仍由 AlertCenter 展示。
 */

import { toast } from "sonner";
import { showNotification } from "./notify";
import { sound } from "./sound";

type Alert = any;

const subs = new Set<(a: Alert) => void>();
// 已处理过的 id：SSE 重连会重放最近若干条，必须只打扰一次
const seen = new Set<number>();
let es: EventSource | null = null;

/** 保证长连接已建立（多次调用幂等）。 */
export function ensureAlertsStarted() {
  if (es || typeof EventSource === "undefined") return;
  es = new EventSource("/api/alerts/stream");
  es.onmessage = (ev) => {
    try {
      dispatch(JSON.parse(ev.data));
    } catch {
      /* 单条坏数据不影响后续 */
    }
  };
  // 断了浏览器会自动重连，这里无需处理
  es.onerror = () => {};
}

/** 订阅告警事件，返回取消订阅函数。 */
export function subscribeAlerts(cb: (a: Alert) => void): () => void {
  subs.add(cb);
  ensureAlertsStarted();
  return () => subs.delete(cb);
}

function dispatch(a: Alert) {
  if (!a) return;
  if (a.id != null) {
    if (seen.has(a.id)) return;
    seen.add(a.id);
  }
  // 打扰只在这里做一次：页面在不在期权看板都能收到。
  // 系统通知可能被 macOS 专注模式 / 系统通知设置静默丢弃（Chrome 里显示已授权但桌面不弹），
  // 所以页面内 toast 永远兜底显示，不再以「通知是否发出」作为唯一提示。
  if (!a.backfill && a.level !== "info") {
    // silent=true：只弹通知与 toast，不响铃（手动关键位提醒每 10 分钟一次，响铃会烦）
    if (!a.silent) sound.play(a.level);
    showNotification(a);
    toast(a.title, { description: a.text, duration: 6000 });
  }
  subs.forEach((f) => {
    try {
      f(a);
    } catch {
      /* 单个订阅者异常不影响其它订阅者 */
    }
  });
}
