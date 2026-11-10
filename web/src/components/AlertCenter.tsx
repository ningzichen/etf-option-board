import { useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { Frame, FrameHeader, FramePanel, FrameTitle } from "@/components/reui/frame";
import { Badge } from "@/components/reui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, EmptyState, HoverTip } from "@/components/ui/misc";
import { api } from "@/lib/api";
import { subscribeAlerts } from "@/lib/alertBus";
import { notifyPermission, requestNotifyPermission, autoPromptPermissionOnce, reportNotifyState, showNotification } from "@/lib/notify";
import { clsUpDown, signedFlow } from "@/lib/format";
import { AlertTriangle, BellRing, Volume2 } from "lucide-react";

const TYPE_LABEL: Record<string, string> = {
  price_spike: "价格异动", volume_surge: "量能放大", main_inflow: "主力资金",
  divergence: "MACD背离", key_time: "变盘时点", national_team: "国家队护盘",
  opt_spike: "合约异动", reversal: "变盘开仓", manual_levels: "手动关键位",
};

/** 合并告警（变盘信号 / 变盘时点）的逐只明细：有分数就带方向+分数 */
const itemTxt = (e: any) =>
  e.score == null ? e.name : `${e.name}${e.dir === "bull" ? "看多" : e.dir === "bear" ? "看空" : ""}${Math.round(e.score)}分`;
const itemDir = (e: any) => (e.dir === "bull" ? 1 : e.dir === "bear" ? -1 : 0);

export function AlertCenter({ onSelect }: { onSelect?: (code: string) => void }) {
  const [items, setItems] = useState<any[]>([]);
  const [popup, setPopup] = useState<any>(null);
  const [perm, setPerm] = useState(notifyPermission());
  const seen = useRef<Set<number>>(new Set());

  useEffect(() => {
    api.alerts().then((d) => {
      const list = d.items ?? [];
      // 关键：把首屏历史的 id 记入 seen，否则 SSE 连接时的 backfill（重发最近 10 条、
      // 且带相同 id）会被当成新告警再次插入，历史告警在列表里重复 2~3 次。
      list.forEach((a: any) => seen.current.add(a.id));
      setItems(list);
    }).catch(() => {});
  }, []);

  // 通知授权：任意首次点击自动弹权限框（免去找按钮）；并上报诊断信标。
  useEffect(() => {
    reportNotifyState("mount");
    autoPromptPermissionOnce();
    const t = setInterval(() => {
      const p = notifyPermission();
      setPerm((prev) => (prev === p ? prev : p));
    }, 3000);
    return () => clearInterval(t);
  }, []);

  const onAlert = (a: any) => {
    // 副作用（声音/通知）已由全局 alertBus 处理，这里只管列表与弹窗
    if (seen.current.has(a.id)) return;
    seen.current.add(a.id);
    setItems((prev) => [a, ...prev].slice(0, 80));
    // 主力资金与变盘开仓弹模态框（后者是可执行的交易信号，优先级最高）
    if (a.type === "main_inflow" || a.type === "reversal") setPopup(a);
  };
  useEffect(() => subscribeAlerts(onAlert), []);

  const onEnable = async () => {
    const p = await requestNotifyPermission();
    setPerm(p);
    reportNotifyState("enable");
    if (p === "granted") toast("浏览器通知已开启", { description: "盘中告警将以 Chrome 系统通知弹出。" });
    else if (p === "denied") toast("浏览器通知被拒绝", { description: "请在地址栏左侧站点设置里重新允许通知。" });
  };

  // 测试通知：只在本浏览器弹一条，走与真实告警相同的 showNotification；
  // 不调 /api/admin/test-alert——那会推给所有在线设备并写进告警历史。
  const onTest = async () => {
    let p = notifyPermission();
    if (p === "default") {
      p = await requestNotifyPermission();
      setPerm(p);
    }
    reportNotifyState("test");
    if (p === "granted") {
      const at = new Date().toLocaleTimeString("zh-CN", { hour12: false });
      showNotification({ title: "【测试】浏览器通知", text: `看到这条系统通知，说明盘中告警可以正常弹出（${at}）。` });
      toast("已发送测试通知", {
        description: "没看到系统弹窗？检查系统通知设置是否允许浏览器（macOS：系统设置 → 通知 → Google Chrome），以及是否开着专注/勿扰模式。",
      });
    } else if (p === "denied") {
      toast("浏览器通知被拒绝", { description: "请在地址栏左侧站点设置里重新允许通知，再点一次测试。" });
    } else if (p === "unsupported") {
      toast("当前地址不支持浏览器通知", { description: "通知只在 HTTPS 或 localhost 下可用；跨设备请用 HTTPS 入口访问。" });
    }
  };

  // 切到告警对应标的：关闭弹窗；窄屏（单列）时右栏在下方，顺带滚到行情图
  const jump = (code?: string) => {
    if (!code || !onSelect) return;
    onSelect(code);
    setPopup(null);
    if (window.matchMedia("(max-width: 1023px)").matches) {
      requestAnimationFrame(() => document.getElementById("main-chart")?.scrollIntoView({ behavior: "smooth", block: "center" }));
    }
  };

  // 系统通知被点击（Service Worker 回传）时同样切到对应标的
  useEffect(() => {
    const onPick = (e: Event) => jump((e as CustomEvent<string>).detail);
    window.addEventListener("etf:select", onPick);
    return () => window.removeEventListener("etf:select", onPick);
  }, [onSelect]);

  // 兜底去重：无论如何渲染层都不出现同一 id 的两行
  const rows = useMemo(() => {
    const s = new Set<any>();
    return items.filter((a) => a.id != null && !s.has(a.id) && (s.add(a.id), true));
  }, [items]);

  return (
    <>
      <Frame spacing="sm">
        <FramePanel dense>
          <FrameHeader className="flex-row items-center justify-between">
            <FrameTitle className="flex items-center gap-1.5 text-xs">
              <BellRing className="size-3.5 text-warning" /> 实时告警
              <Button variant="outline" size="xs" className="h-5 px-1.5 text-[10px] font-normal" onClick={onTest}
                title="在本浏览器弹一条测试通知，验证系统通知能否正常弹出（不推给其它设备、不进告警列表）">
                测试通知
              </Button>
            </FrameTitle>
            <span className="flex items-center gap-1.5">
              <span className="flex items-center gap-1 text-[10px] text-muted-foreground">
                <Volume2 className="size-3" /> 声音已开启
              </span>
              {perm === "granted" ? (
                <Badge variant="success-light" title="告警将以 Chrome 系统通知弹出">浏览器通知</Badge>
              ) : perm === "default" ? (
                <Button variant="outline" size="xs" onClick={onEnable}>开启浏览器通知</Button>
              ) : perm === "denied" ? (
                <Badge variant="warning-light" title="浏览器通知被拒绝，请在地址栏站点设置里重新允许">通知已拒绝</Badge>
              ) : (
                <Badge variant="outline" title="当前为非 HTTPS 地址，浏览器通知不可用；跨设备请用 HTTPS 入口">通知不可用</Badge>
              )}
            </span>
          </FrameHeader>
          {/* 列表自身限高滚动是刻意的：本卡位于随页面滚动的右栏，没有定高父链可依赖 */}
          <div className="max-h-[260px] min-h-0 divide-y divide-border/60 overflow-auto" role="status" aria-live="polite">
            {rows.length === 0 && <EmptyState title="暂无告警" hint="量能、资金异动与变盘信号会在此推送" />}
            {rows.map((a) => (
              // 点击整行切到该告警对应的标的（无 etf_code 的全局告警不可点）
              <button
                key={a.id}
                type="button"
                disabled={!a.etf_code}
                onClick={() => jump(a.etf_code)}
                title={a.etf_code ? `点击切换到 ${a.etf_name ?? a.etf_code}（${a.etf_code}）` : undefined}
                className="flex w-full cursor-pointer items-start gap-2 px-3 py-1.5 text-left transition-colors hover:bg-muted/40 disabled:cursor-default disabled:hover:bg-transparent"
              >
                <span className="mt-0.5">{a.level === "critical" ? <AlertTriangle className="size-3 text-destructive" /> : null}</span>
                {/* 整行悬浮：代码 + 完整标题 + 完整正文（列表里标题 truncate、正文 line-clamp-2 会截断） */}
                <HoverTip label={`${a.etf_code ? a.etf_code + " " : ""}${a.title}\n${a.text}${
                  (a.etfs ?? []).map((e: any) => `\n${e.name} ${signedFlow(e.main_inflow)}${e.surge ? " · " + e.surge + "x放量" : ""}`).join("")
                }${(a.items ?? []).map((e: any) => `\n${itemTxt(e)}`).join("")}`} className="min-w-[240px]">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5">
                      <span className={`truncate text-xs font-medium ${clsUpDown(a.dir === "bull" ? 1 : a.dir === "bear" ? -1 : 0)}`}>{a.title}</span>
                      <Badge variant="outline" className="shrink-0">{TYPE_LABEL[a.type] ?? a.type}</Badge>
                    </div>
                    <div className="line-clamp-2 text-[10px] leading-snug text-muted-foreground">{a.text}</div>
                    {a.items?.length ? (
                      <div className="mt-0.5 flex flex-wrap gap-x-2 gap-y-0.5 text-[10px] leading-snug text-muted-foreground tnum">
                        {a.items.map((e: any) => (
                          <span key={`${e.code}-${e.dir ?? ""}`} className="whitespace-nowrap">
                            <span className="text-foreground">{e.name}</span>
                            {e.score != null && (
                              <span className={`ml-1 ${clsUpDown(itemDir(e))}`}>
                                {e.dir === "bull" ? "看多" : e.dir === "bear" ? "看空" : "观望"} {Math.round(e.score)}分
                              </span>
                            )}
                          </span>
                        ))}
                      </div>
                    ) : null}
                    {a.etfs?.length ? (
                      <div className="mt-0.5 flex flex-wrap gap-x-2 gap-y-0.5 text-[10px] leading-snug text-muted-foreground tnum">
                        {a.etfs.map((e: any) => (
                          <span key={e.code} className="whitespace-nowrap">
                            <span className="text-foreground">{e.name}</span>
                            <span className={`ml-1 ${clsUpDown(e.main_inflow ?? 0)}`}>{signedFlow(e.main_inflow)}</span>
                            {e.surge ? <span className="ml-1">· {e.surge}x</span> : null}
                          </span>
                        ))}
                      </div>
                    ) : null}
                  </div>
                </HoverTip>
                <span className="text-[10px] text-muted-foreground tnum">{a.ts?.slice(11)}</span>
              </button>
            ))}
          </div>
        </FramePanel>
      </Frame>

      <Dialog open={!!popup} onClose={() => setPopup(null)} className="w-[min(90vw,460px)] p-5"
        title={<span className="flex items-center gap-2"><AlertTriangle className="size-4 text-destructive" />重要提醒</span>}>
        {popup && (
          <div className="flex flex-col gap-2">
            <div className="text-base font-semibold">{popup.title}</div>
            <div className="text-sm text-muted-foreground">{popup.text}</div>
            {popup.items?.length ? (
              <div className="flex flex-col gap-1 rounded-lg bg-muted/50 px-2 py-1.5 text-[11px] leading-snug">
                {popup.items.map((e: any) => (
                  <div key={`${e.code}-${e.dir ?? ""}`} className="flex flex-col gap-0.5">
                    <div className="flex items-center justify-between gap-3">
                      <span className="text-foreground">{e.name}
                        <span className="ml-1 text-muted-foreground">{e.code}</span></span>
                      {e.score != null && (
                        <span className={clsUpDown(itemDir(e))}>
                          {e.dir === "bull" ? "看多" : e.dir === "bear" ? "看空" : "观望"} {Math.round(e.score)}分
                        </span>
                      )}
                    </div>
                    {e.text ? <div className="text-muted-foreground">{e.text}</div> : null}
                  </div>
                ))}
              </div>
            ) : null}
            {popup.etfs?.length ? (
              <div className="flex flex-col gap-0.5 rounded-lg bg-muted/50 px-2 py-1.5 text-xs tnum">
                {popup.etfs.map((e: any) => (
                  <div key={e.code} className="flex items-center justify-between gap-3">
                    <span>{e.name}<span className="ml-1 text-muted-foreground">{e.code}</span></span>
                    <span className={clsUpDown(e.main_inflow ?? 0)}>
                      {signedFlow(e.main_inflow)}{e.surge ? ` · ${e.surge}x` : ""}
                    </span>
                  </div>
                ))}
              </div>
            ) : null}
            <div className="flex items-center gap-2 text-xs">
              <Badge variant={popup.dir === "bull" ? "destructive-light" : popup.dir === "bear" ? "success-light" : "outline"}>
                {popup.dir === "bull" ? "看多" : popup.dir === "bear" ? "看空" : "观望"}
              </Badge>
              <span className="text-muted-foreground tnum">
                {popup.etf_code && <span className="mr-1 font-medium text-foreground">{popup.etf_code}</span>}
                {popup.etf_name} · {popup.ts?.slice(11)}
              </span>
            </div>
            {popup.etf_code && (
              <div className="flex justify-end">
                <Button size="xs" onClick={() => jump(popup.etf_code)}>查看该标的</Button>
              </div>
            )}
          </div>
        )}
      </Dialog>
    </>
  );
}
