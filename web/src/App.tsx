import { useEffect, useState } from "react";
import { TopBar } from "@/components/TopBar";
import { SideNav } from "@/components/SideNav";
import { BoardSubNav } from "@/components/BoardSubNav";
import { SettingsDialog } from "@/components/SettingsDialog";
import { navigate, usePageRoute, titleOf, type PageKey } from "@/lib/route";import {
  isBoardPage, pageLabel, readNavOpen, rememberBoardPage, useIsWide, writeNavOpen,
} from "@/lib/nav";
import { requestRefresh } from "@/lib/refresh";
import { ensureAlertsStarted } from "@/lib/alertBus";
import { api } from "@/lib/api";
import { usePolling } from "@/lib/usePolling";
import { OptionsBoard } from "@/pages/OptionsBoard";
import { TodayPage } from "@/pages/Today";
import { EtfFlowPage } from "@/pages/EtfFlow";
import { EmFlowPage } from "@/pages/EmFlow";
import { BoardMultiPage } from "@/pages/BoardMulti";
import { BoardFlowPage } from "@/pages/BoardFlow";
import { TapeFlowPage } from "@/pages/TapeFlow";
import { WangWangPage } from "@/pages/WangWang";
import { AriskPage } from "@/pages/Arisk";
import { LimitUpPage } from "@/pages/LimitUp";

// 当前加载的前端 bundle 哈希（用于判断浏览器是否加载了最新构建）
const BUNDLE_ID = (() => {
  const s = document.querySelector('script[type="module"]') as HTMLScriptElement | null;
  const m = (s?.src || "").match(/index-([A-Za-z0-9_-]+)\.js/);
  return m ? m[1] : "dev";
})();

/** 页面注册表：唯一的分发表，新增页面只改这里 + route.ts 的 PAGES。 */
const VIEWS: Record<PageKey, () => JSX.Element> = {
  options: OptionsBoard,
  today: TodayPage,
  "board-flow": BoardFlowPage,
  "board-multi": BoardMultiPage,
  "board-etf": EtfFlowPage,
  "board-em": EmFlowPage,
  "flow-tape": TapeFlowPage,
  wangwang: WangWangPage,
  arisk: AriskPage,
  limitup: LimitUpPage,
};

/**
 * 应用外壳：顶栏 + 页面标签 + 路由分发 + 设置弹窗。
 * 每一页自己拉自己需要的数据（期权页的数据不再在板块页轮询）。
 */
export default function App() {
  const page = usePageRoute();
  const [clock, setClock] = useState(new Date().toLocaleTimeString("zh-CN", { hour12: false }));
  const [settingsOpen, setSettingsOpen] = useState(false);
  // 左侧导航：常驻展开状态（记住）+ 收起后的浮层抽屉（临时）
  const [navOpen, setNavOpen] = useState<boolean>(readNavOpen);
  const [drawer, setDrawer] = useState(false);
  const wide = useIsWide();
  const inlineNav = navOpen && wide;

  const setNav = (open: boolean) => {
    setNavOpen(open);
    writeNavOpen(open);
    if (open) setDrawer(false);
  };

  const toggleNav = () => {
    if (inlineNav) {
      setNav(false);
      return;
    }
    if (drawer) {
      setDrawer(false);
      return;
    }
    setDrawer(true); // 收起状态下点按钮 = 临时浮层，内容区不重排
  };

  // 设置弹窗的分页（origin 新增：模型后端配置）
  const [settingsTab, setSettingsTab] = useState<"trading" | "llm">("trading");
  useEffect(() => {
    const open = () => { setSettingsTab("llm"); setSettingsOpen(true); };
    window.addEventListener("etf:open-llm-settings", open);
    return () => window.removeEventListener("etf:open-llm-settings", open);
  }, []);

  // 顶栏全局信息：所有页面都要看（外围市场 / 国家队评分 / 服务健康）
  const { data: health } = usePolling(() => api.health(), 60_000);
  const { data: external } = usePolling(() => api.external(), 30_000);
  const { data: national } = usePolling(() => api.national(), 30_000);

  useEffect(() => {
    const t = setInterval(() => setClock(new Date().toLocaleTimeString("zh-CN", { hour12: false })), 1000);
    return () => clearInterval(t);
  }, []);

  // 告警长连接在全局建立：切到板块页也照常收告警（声音/通知/toast 由 alertBus 负责）
  useEffect(() => {
    ensureAlertsStarted();
  }, []);

  // 记住最近看的资金子页：点侧栏「资金分时」回到这里，而不是每次跳回第一页
  useEffect(() => {
    rememberBoardPage(page);
  }, [page]);

  // 常驻侧栏出现时（拉宽/固定），临时抽屉自动让位
  useEffect(() => {
    if (inlineNav) setDrawer(false);
  }, [inlineNav]);

  // 抽屉是浮层：Esc 关闭
  useEffect(() => {
    if (!drawer) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setDrawer(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [drawer]);

  // 页面标题跟着路由走（多设备/多标签时一眼能分清）
  useEffect(() => {
    document.title = titleOf(page);
  }, [page]);

  // 未知 key 一律回落期权看板（route.ts 已规范化 hash，这里是第二道保险）
  const View = VIEWS[page] ?? VIEWS.options;

  return (
    <div className="flex min-h-screen bg-background">
      {/* 常驻侧栏：收起后完全不占布局（内容区铺满） */}
      {inlineNav ? (
        <SideNav
          page={page}
          mode="inline"
          onClose={() => setNav(false)}
        />
      ) : null}

      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar
          external={external?.items ?? []}
          national={national}
          health={health}
          clock={clock}
          subtitle={pageLabel(page)}
          navOpen={inlineNav}
          onToggleNav={toggleNav}
          onRefresh={requestRefresh}
          onOpenSettings={() => { setSettingsTab("trading"); setSettingsOpen(true); }}
          mode={page === "today" ? "simple" : page === "options" ? "pro" : undefined}
          onModeChange={(m) => navigate(m === "simple" ? "today" : "options")}
        />
        <SettingsDialog open={settingsOpen} initialTab={settingsTab}
          onClose={() => setSettingsOpen(false)} />

        {/* 资金分时：四个子页的页内标签（路由仍是 #/board/*） */}
        {isBoardPage(page) ? <BoardSubNav page={page} /> : null}

        <View />

        <footer className="px-4 py-3 text-center text-[10px] text-muted-foreground">
          数据来源：东方财富 · 期权定价 BS · 预测 Kronos · 决策大模型 — 仅供研究，不构成投资建议
          <span className="ml-2">· 构建 {BUNDLE_ID}</span>
        </footer>
      </div>

      {/* 收起后点左上角按钮弹的浮层抽屉（选完自动关，内容区不重排） */}
      {!inlineNav && drawer ? (
        <>
          <div
            className="fixed inset-0 z-40 bg-black/40 backdrop-blur-[1px]"
            onClick={() => setDrawer(false)}
            aria-hidden
          />
          <div className="fixed inset-y-0 left-0 z-50">
            <SideNav
              page={page}
              mode="drawer"
              onNavigate={() => setDrawer(false)}
              onClose={() => setDrawer(false)}
              onPin={() => setNav(true)}
            />
          </div>
        </>
      ) : null}
    </div>
  );
}
