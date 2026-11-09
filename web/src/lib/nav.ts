/* 左侧主导航：分组、最近子页、展开状态。
 *
 * 设计取舍（2026-11-06）：
 * - 侧栏**只放一级项**：期权看板 / 资金分时。分组由 `route.ts` 的 `PAGES[].section`
 *   自动派生 —— 以后加页面只要在 PAGES 里写 `section`，导航项不用另外登记。
 * - 资金分时内部的四个子页仍走原来的 `#/board/*` 路由，由页内 `BoardSubNav` 切换，
 *   旧链接、书签、分享一个都不失效。
 * - 收起 = 完全隐藏（不占布局），左上角按钮点开的是**浮层抽屉**，内容区不会跟着跳动；
 *   抽屉底部的「固定展开」把它变回常驻侧栏。
 * - 展开/收起状态存 localStorage，刷新、切页都保持。
 */

import { useEffect, useState } from "react";
import { PAGES, type PageKey, type SectionKey } from "@/lib/route";

export type NavIcon = "options" | "board" | "flow" | "wangwang" | "arisk" | "limitup";

export type NavItem = {
  /** 分组代表页（默认跳转目标，也是「点一级项」的落点） */
  key: PageKey;
  section: SectionKey;
  label: string;
  title: string;
  icon: NavIcon;
  /** 归属该分组的全部页面，顺序即页内子标签顺序 */
  pages: PageKey[];
};

/** 一级分组的外观与文案（加新分组：这里加一条 + PAGES 里给页面标 section） */
const SECTIONS: Record<SectionKey, { label: string; title: string; icon: NavIcon }> = {
  options: { label: "期权看板", title: "缅A决策看板", icon: "options" },
  board: { label: "资金分时", title: "资金分时：板块主力 / ETF / 四维资金", icon: "board" },
  flow: { label: "资金流向", title: "资金流向：全A逐笔分档（通达信 / Wind）", icon: "flow" },
  wangwang: { label: "汪汪队ETF", title: "汪汪队 ETF 跟踪器：国家队今天上班了吗", icon: "wangwang" },
  arisk: { label: "风险监控", title: "A股风险监控：ERP / 信贷脉冲 / 两层决策模型", icon: "arisk" },
  limitup: { label: "涨停复盘", title: "涨停复盘：涨停周期表 / 主题榜 / 个股池", icon: "limitup" },
};

const SECTION_ORDER: SectionKey[] = ["options", "board", "flow", "wangwang", "arisk", "limitup"];

export const NAV: NavItem[] = SECTION_ORDER.map((section) => {
  const pages = PAGES.filter((p) => p.section === section);
  const meta = SECTIONS[section];
  return { key: pages[0].key, section, ...meta, pages: pages.map((p) => p.key) };
});

/** 资金分时的四个子页（页内子标签顺序 = PAGES 里的声明顺序） */
export const BOARD_PAGES: PageKey[] = PAGES.filter((p) => p.section === "board").map((p) => p.key);

export function isBoardPage(page: PageKey): boolean {
  return BOARD_PAGES.includes(page);
}

/** 当前页属于哪个一级分组（未知页回落第一组，与路由兜底一致）。 */
export function navOf(page: PageKey): NavItem {
  const section = PAGES.find((p) => p.key === page)?.section;
  return NAV.find((n) => n.section === section) ?? NAV[0];
}

const LAST_BOARD_KEY = "etf.nav.lastBoard";

/** 上次访问的资金子页（下次点「资金分时」回到这里，而不是每次都跳回第一页）。 */
export function lastBoardPage(): PageKey {
  try {
    const v = localStorage.getItem(LAST_BOARD_KEY) as PageKey | null;
    if (v && isBoardPage(v)) return v;
  } catch {
    /* 隐私模式读不到 localStorage 时按默认页 */
  }
  return BOARD_PAGES[0];
}

export function rememberBoardPage(page: PageKey): void {
  if (!isBoardPage(page)) return;
  try {
    localStorage.setItem(LAST_BOARD_KEY, page);
  } catch {
    /* ignore */
  }
}

/** 点一级导航应该落到哪一页：期权看板固定，资金分时回到上次的子页。 */
export function navTarget(item: NavItem): PageKey {
  return item.section === "board" ? lastBoardPage() : item.key;
}

const OPEN_KEY = "etf.nav.open";

export function readNavOpen(): boolean {
  try {
    const v = localStorage.getItem(OPEN_KEY);
    if (v === "0") return false;
    if (v === "1") return true;
  } catch {
    /* ignore */
  }
  return true; // 首次进入默认展开
}

export function writeNavOpen(open: boolean): void {
  try {
    localStorage.setItem(OPEN_KEY, open ? "1" : "0");
  } catch {
    /* ignore */
  }
}

/** 窄屏（< md）不显示常驻侧栏，只能走浮层抽屉。 */
export function useIsWide(min = 768): boolean {
  const [wide, setWide] = useState<boolean>(() =>
    typeof window === "undefined" ? true : window.innerWidth >= min,
  );
  useEffect(() => {
    const onResize = () => setWide(window.innerWidth >= min);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [min]);
  return wide;
}

/** 页面标题文案（页内子标签用） */
export function pageLabel(key: PageKey): string {
  return PAGES.find((p) => p.key === key)?.label ?? key;
}
