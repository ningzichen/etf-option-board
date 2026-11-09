import { useEffect, useState, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { Tabs } from "@/components/ui/misc";
import { api } from "@/lib/api";

// 渠道/级别选项由后端下发（services/push.py 是唯一定义处），这里只做首帧兜底
const CHANNELS_FALLBACK = [
  { key: "none", label: "不推送" },
  { key: "serverchan", label: "Server酱（微信）" },
  { key: "bark", label: "Bark（iOS）" },
  { key: "wecom", label: "企业微信机器人" },
];
const LEVELS_FALLBACK = [
  { key: "critical", label: "严重" },
  { key: "warn", label: "提醒" },
  { key: "info", label: "通知" },
];

// 各渠道的配置方法说明：选择渠道后在下方展示，避免用户去翻文档
const CHANNEL_GUIDES: Record<string, ReactNode> = {
  serverchan: (
    <>
      1. 打开 <span className="text-foreground">sct.ftqq.com</span>，微信扫码登录；
      <br />2. 在「SendKey」页复制 SCT 开头的密钥，填入下方输入框；
      <br />3. 关注页面提示的公众号，推送消息经它送达微信。
      <div className="mt-1">免费版每天约 5 条额度，建议搭配「严重」级别使用。</div>
    </>
  ),
  bark: (
    <>
      1. App Store 搜索安装 <span className="text-foreground">Bark</span>；
      <br />2. 打开 App，复制示例地址 https://api.day.app/<span className="text-foreground">你的Key</span>/ 推送内容 中的 Key 填入下方；
      <br />3. 通知直达系统横幅，无需关注公众号。
      <div className="mt-1">自建服务器可填自己的 https:// 地址（默认官方 api.day.app）。</div>
    </>
  ),
  wecom: (
    <>
      1. 手机端：企业微信群聊 → 右上角「…」→ 群机器人 → 添加；
      <br />2. 复制机器人 Webhook 地址（qyapi.weixin.qq.com 开头）填入下方；
      <br />3. 消息以机器人名义发到该群。
      <div className="mt-1">仅接受官方域名地址；同群可添加多个机器人分渠道接收。</div>
    </>
  ),
};

const inputCls =
  "tnum h-8 w-full rounded-md border border-input bg-transparent px-2 text-xs outline-none focus-visible:ring-2 focus-visible:ring-ring";

/**
 * 告警远程推送设置：Server酱 / Bark / 企业微信机器人。
 * key/url 留空提交 = 保留已存值（后端掩码不可逆）；「测试推送」按已保存配置发一条。
 */
export function PushSettings() {
  const [enabled, setEnabled] = useState(false);
  const [channel, setChannel] = useState("none");
  const [minLevel, setMinLevel] = useState("critical");
  const [key, setKey] = useState("");
  const [server, setServer] = useState("");
  const [url, setUrl] = useState("");
  const [masked, setMasked] = useState<{ key?: string; url?: string }>({});
  const [channelTabs, setChannelTabs] = useState(CHANNELS_FALLBACK);
  const [levelTabs, setLevelTabs] = useState(LEVELS_FALLBACK);
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [testing, setTesting] = useState(false);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const d = await api.pushSettings();
        if (!alive) return;
        if (d?.error) { setErr(d.error); return; }
        setEnabled(!!d?.enabled);
        setChannel(d?.channel ?? "none");
        setMinLevel(d?.min_level ?? "critical");
        setMasked({ key: d?.key ?? "", url: d?.url ?? "" });
        if (Array.isArray(d?.channel_choices) && d.channel_choices.length) setChannelTabs(d.channel_choices);
        if (Array.isArray(d?.level_choices) && d.level_choices.length) setLevelTabs(d.level_choices);
      } catch (e: any) {
        if (alive) setErr(e?.message ?? "加载失败");
      }
    })();
    return () => { alive = false; };
  }, []);

  const onSave = async () => {
    setBusy(true); setErr(null); setOk(null);
    try {
      // 只有用户输入了新值才携带对应字段，空值一律不传 → 后端保留原值
      const body: Record<string, unknown> = { enabled, channel, min_level: minLevel };
      if (key.trim() !== "") body.key = key.trim();
      if (server.trim() !== "") body.server = server.trim();
      if (url.trim() !== "") body.url = url.trim();
      const r = await api.savePushSettings(body as any);
      if (r?.error) { setErr(r.error); return; }
      setMasked({ key: r?.key ?? "", url: r?.url ?? "" });
      setChannel(r?.channel ?? channel);
      setKey(""); setUrl(""); setServer("");
      setOk("已保存");
    } catch (e: any) {
      setErr(e?.message ?? "保存失败");
    } finally {
      setBusy(false);
    }
  };

  const onTest = async () => {
    setTesting(true); setErr(null); setOk(null);
    try {
      const r = await api.pushTest();
      if (r?.ok) setOk("测试推送已发送，请查看手机");
      else setErr(r?.msg ?? "测试失败");
    } catch (e: any) {
      setErr(e?.message ?? "测试失败");
    } finally {
      setTesting(false);
    }
  };

  return (
    <div className="space-y-4 text-xs">
      <div className="space-y-1.5">
        <label className="flex items-center gap-2 font-medium">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
          启用远程推送
        </label>
        <div className="text-muted-foreground">
          告警弹窗/声音只在看板页面开着时有效；开启后 critical 等告警会推到手机。
        </div>
      </div>

      <div className="space-y-1.5">
        <div className="font-medium">推送渠道</div>
        <Tabs tabs={channelTabs} value={channel} onChange={setChannel} />
        {CHANNEL_GUIDES[channel] && (
          <div className="rounded-md border border-border bg-muted/40 p-2 leading-relaxed text-muted-foreground">
            {CHANNEL_GUIDES[channel]}
          </div>
        )}
      </div>

      <div className="space-y-1.5">
        <div className="font-medium">最低推送级别</div>
        <Tabs tabs={levelTabs} value={minLevel} onChange={setMinLevel} />
        <div className="text-muted-foreground">只有达到该级别的告警才推送（严重 &gt; 提醒 &gt; 通知）。</div>
      </div>

      {(channel === "serverchan" || channel === "bark") && (
        <div className="space-y-1.5">
          <div className="font-medium">{channel === "serverchan" ? "SendKey" : "设备 Key"}</div>
          <input type="password" value={key} onChange={(e) => setKey(e.target.value)}
            placeholder={masked.key ? `已保存 ${masked.key}，留空保持不变` : channel === "serverchan" ? "SCT…" : "Bark 设备 Key"}
            aria-label="推送 Key" className={inputCls} />
          {channel === "bark" && (
            <>
              <div className="font-medium mt-2">Bark 服务器（可选）</div>
              <input type="text" value={server} onChange={(e) => setServer(e.target.value)}
                placeholder="默认 https://api.day.app" aria-label="Bark 服务器" className={inputCls} />
            </>
          )}
        </div>
      )}

      {channel === "wecom" && (
        <div className="space-y-1.5">
          <div className="font-medium">机器人 Webhook 地址</div>
          <input type="password" value={url} onChange={(e) => setUrl(e.target.value)}
            placeholder={masked.url ? `已保存 ${masked.url}，留空保持不变` : "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…"}
            aria-label="企业微信 Webhook" className={inputCls} />
          <div className="text-muted-foreground">仅接受 qyapi.weixin.qq.com 官方地址。</div>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" onClick={onSave} disabled={busy}>{busy ? "保存中…" : "保存"}</Button>
        <Button size="sm" variant="outline" onClick={onTest} disabled={testing}>测试推送</Button>
        {ok && <span className="text-success-foreground dark:text-success">{ok}</span>}
        {err && <span className="text-destructive">{err}</span>}
      </div>

      <div className="border-t border-border pt-2 leading-relaxed text-muted-foreground">
        密钥存储于本机 data/push_settings.json，不进 git 历史；页面只显示掩码。
        推送为尽力而为：失败不重试，也不影响看板内的弹窗与声音。
      </div>
    </div>
  );
}
