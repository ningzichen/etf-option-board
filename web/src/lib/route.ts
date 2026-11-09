/* 页面路由：用 hash 记录当前页，零依赖、刷新保持、链接可分享。
 *
 * 约定：`#/options`（期权看板，默认）与四个板块资金页 `#/board/*`。
 * 未知 hash 一律回落期权看板，绝不白屏。
 *
 * `section` 决定它属于左侧导航的哪个一级分组（见 lib/nav.ts）。
 * 加一个新页面 = 这里加一条 + App.tsx 的 VIEWS 加一行，导航会自动出现。
 */

import { useEffect, useState } from "react";

export type PageKey = "today" | "options" | "board-flow" | "board-multi" | "board-etf" | "board-em" | "flow-tape" | "wangwang" | "arisk" | "limitup";

/** 一级导航分组：期权看板 ｜ 资金分时（内含四个子页）｜ 资金流向（全A逐笔）｜ 汪汪队 ｜ 风险监控 ｜ 涨停复盘 */
export type SectionKey = "options" | "board" | "flow" | "wangwang" | "arisk" | "limitup";

export type PageDef = {
  key: PageKey;
  hash: string;
  label: string;
  title: string;
  /** 归属的一级导航分组 */
  section: SectionKey;
};

export const PAGES: PageDef[] = [
  { key: "options", hash: "#/options", label: "期权看板", title: "缅A决策看板", section: "options" },
  { key: "today", hash: "#/today", label: "今日结论", title: "今日结论 · 简洁模式（结论 / 动作 / 风险）", section: "options" },
  { key: "board-flow", hash: "#/board/flow", label: "主力资金分时", title: "主力资金分时", section: "board" },
  { key: "board-multi", hash: "#/board/multi", label: "主力资金多日", title: "主力资金多日", section: "board" },
  { key: "board-etf", hash: "#/board/etf", label: "ETF资金流", title: "ETF资金流", section: "board" },
  { key: "board-em", hash: "#/board/em", label: "四维资金", title: "四维资金", section: "board" },
  { key: "flow-tape", hash: "#/flow", label: "资金流向", title: "资金流向：全A逐笔分档（通达信 / Wind 两套口径）", section: "flow" },
  { key: "wangwang", hash: "#/wangwang", label: "汪汪队ETF", title: "汪汪队 ETF 跟踪器 · 今日国家队上班了吗", section: "wangwang" },
  { key: "arisk", hash: "#/arisk", label: "风险监控", title: "A股风险监控 · A-RISK（ERP / 信贷脉冲 / 两层决策模型）", section: "arisk" },
  { key: "limitup", hash: "#/limitup", label: "涨停复盘", title: "涨停复盘 · 涨停周期表 / 涨停棱镜 / 主题榜 / 个股池", section: "limitup" },
];

const BY_HASH = new Map(PAGES.map((p) => [p.hash, p.key]));
const BY_KEY = new Map(PAGES.map((p) => [p.key, p]));

/** hash 字符串 → 页面 key（容忍尾部斜杠与查询串，未知回落期权看板）。 */
export function pageFromHash(hash: string): PageKey {
  const clean = (hash || "").trim().split("?")[0].replace(/\/+$/, "");
  return BY_HASH.get(clean) ?? "options";
}

export function hashOf(key: PageKey): string {
  return (BY_KEY.get(key) ?? PAGES[0]).hash;
}

export function titleOf(key: PageKey): string {
  return (BY_KEY.get(key) ?? PAGES[0]).title;
}

/** 跳到指定页面（写 hash 会触发 hashchange，浏览器前进/后退同样可用）。 */
export function navigate(key: PageKey) {
  const h = hashOf(key);
  if (window.location.hash === h) return;
  window.location.hash = h;
}

/** 订阅当前页面。首帧就按现有 hash 解析，避免闪一下默认页。 */
export function usePageRoute(): PageKey {
  const [key, setKey] = useState<PageKey>(() =>
    typeof window === "undefined" ? "options" : pageFromHash(window.location.hash),
  );
  useEffect(() => {
    const onHash = () => {
      const raw = window.location.hash;
      const k = pageFromHash(raw);
      const canon = hashOf(k);
      // 只在「认不出」或「只差尾斜杠」时规范化；`#/board/em?date=…` 这类参数要保留
      if (raw !== canon && !raw.startsWith(canon + "?")) {
        window.history.replaceState(null, "", canon);
      }
      setKey(k);
    };
    window.addEventListener("hashchange", onHash);
    // 首次进入没有 hash 时补一个默认值（用 replace 不污染历史）
    if (!window.location.hash) {
      window.history.replaceState(null, "", hashOf("options"));
    } else {
      onHash();
    }
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  return key;
}
