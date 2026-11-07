const BASE = "/api";

async function get<T = any>(path: string): Promise<T> {
  const r = await fetch(BASE + path, { headers: { Accept: "application/json" } });
  if (!r.ok) throw new Error(`${r.status} ${path}`);
  return r.json();
}

export const api = {
  health: () => get("/health"),
  universe: () => get("/universe"),
  indices: () => get("/indices"),
  indexLevels: () => get("/index/levels"),
  external: () => get("/external"),
  national: () => get("/national"),
  etfs: () => get("/etfs"),
  directions: () => get("/directions"),
  etfs_spark: () => get("/etfs/spark"),
  etf: (code: string, klt = 1) => get(`/etf/${code}?klt=${klt}`),
  etfKline: (code: string, klt = 1, lmt = 240) => get(`/etf/${code}/kline?klt=${klt}&lmt=${lmt}`),
  chain: (code: string, month?: number) =>
    get(`/option/chain/${code}${month ? `?month=${month}` : ""}`),
  optQuote: (secid: string) => get(`/option/quote?secid=${secid}`),
  optKline: (secid: string, klt = 1, lmt = 240) =>
    get(`/option/kline?secid=${secid}&klt=${klt}&lmt=${lmt}`),
  optTrends: (secid: string) => get(`/option/trends?secid=${secid}`),
  optLevels: (code: string, secid: string) =>
    get(`/option/levels?code=${code}&secid=${secid}`),
  topPicks: (n = 6) => get(`/picks/top?n=${n}`),
  calibration: () => get("/calibration"),
  indexOptionOverview: () => get("/index-option/overview"),
  indexOption: (symbol: string) => get(`/index-option/${symbol}`),
  strategy: (code: string, kronos = 0, klt = 1) =>
    get(`/strategy/${code}?kronos=${kronos ? 1 : 0}&klt=${klt}`),
  predict: (secid: string, klt = 5, lookback = 120, pred_len = 20) =>
    get(`/predict?secid=${secid}&klt=${klt}&lookback=${lookback}&pred_len=${pred_len}`),
  llmContext: async (code: string, signal?: AbortSignal, klt = 1) => {
    const r = await fetch(`${BASE}/llm/${encodeURIComponent(code)}/context?klt=${klt}`, {
      signal, cache: "no-store", headers: { Accept: "application/json" },
    });
    if (!r.ok) throw new Error(`行情上下文请求失败（HTTP ${r.status}）`);
    return r.json();
  },
  llmProxy: async (body: {
    protocol: string; baseUrl: string; model: string; apiKey: string;
    system: string; user: string; test: boolean; maxTokens: number;
  }, signal: AbortSignal) => {
    const r = await fetch(`${BASE}/llm/proxy`, {
      method: "POST", signal, cache: "no-store", credentials: "omit",
      referrerPolicy: "no-referrer", headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(`看板代理请求失败（HTTP ${r.status}）`);
    return r.json();
  },
  expiry: () => get("/expiry"),
  settings: () => get("/settings"),
  saveSettings: async (body: {
    total_capital: number | string | null;
    risk_pct: number;
    tier: string;
    size: string;
  }) => {
    const r = await fetch(`${BASE}/settings`, {
      method: "PUT",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body),
    });
    return r.json();
  },
  alerts: () => get("/alerts?limit=60"),
  reversalSignals: (code: string) => get(`/reversal/signals?code=${code}`),
  monitorCycle: async () => (await fetch(`${BASE}/monitor/cycle`, { method: "POST" })).json(),

  // 板块资金（读本地库，采集由后端定时进行）
  boardHealth: () => get("/board/health"),
  boardRank: (type = "concept", date?: string, mode = "east") =>
    get(`/board/rank?type=${type}&mode=${mode}${date ? `&date=${date}` : ""}`),
  boardIndexRank: (date?: string) =>
    get(`/board/index-rank${date ? `?date=${date}` : ""}`),
  boardSeries: (codes: string, date?: string, type = "concept", mode = "east") =>
    get(`/board/series?codes=${encodeURIComponent(codes)}&type=${type}&mode=${mode}${date ? `&date=${date}` : ""}`),
  boardDates: (limit = 60) => get(`/board/dates?limit=${limit}`),
  /** 财联社「热门板块」：热门行业/概念 + 盘面异动，模糊匹配到本地板块。
   *  force=true 跳过后端 5 分钟缓存，直接回源财联社（页面刷新按钮用）。 */
  boardClsHot: (date?: string, force = false) => {
    const qs = [date ? `date=${date}` : "", force ? "force=1" : ""].filter(Boolean).join("&");
    return get(`/board/cls-hot${qs ? `?${qs}` : ""}`);
  },
  boardDetail: (code: string, date?: string) =>
    get(`/board/detail?code=${encodeURIComponent(code)}${date ? `&date=${date}` : ""}`),
  boardMulti: (start?: string, end?: string, type = "concept") =>
    get(`/board/multi?type=${type}${start ? `&start=${start}` : ""}${end ? `&end=${end}` : ""}`),
  boardMultiSeries: (codes: string, start?: string, end?: string) =>
    get(`/board/multi-series?codes=${encodeURIComponent(codes)}`
      + `${start ? `&start=${start}` : ""}${end ? `&end=${end}` : ""}`),
  boardWatch: () => get("/board/watch"),

  // ETF 资金流
  etfFlowRank: (scope = "all", date?: string) =>
    get(`/etf-flow/rank?scope=${scope}${date ? `&date=${date}` : ""}`),
  etfFlowSeries: (codes: string, date?: string) =>
    get(`/etf-flow/series?codes=${encodeURIComponent(codes)}${date ? `&date=${date}` : ""}`),
  etfFlow4dim: (code: string, date?: string) =>
    get(`/etf-flow/4dim?code=${encodeURIComponent(code)}${date ? `&date=${date}` : ""}`),
  etfFlowWatch: () => get("/etf-flow/watch"),
  etfFlowWatchAdd: async (code: string, name: string) =>
    (await fetch(`${BASE}/etf-flow/watch?code=${encodeURIComponent(code)}&name=${encodeURIComponent(name)}`,
      { method: "POST" })).json(),
  etfFlowWatchRemove: async (code: string) =>
    (await fetch(`${BASE}/etf-flow/watch?code=${encodeURIComponent(code)}`, { method: "DELETE" })).json(),

  // 东财 4 维资金
  emFlowTargets: () => get("/em-flow/targets"),
  emFlowIndustries: () => get("/em-flow/industries"),
  emFlowConcepts: () => get("/em-flow/concepts"),
  emFlowSeries: (kind: string, code: string) =>
    get(`/em-flow/series?target_type=${kind}&code=${encodeURIComponent(code)}`),
  emFlowConfigAdd: async (code: string, name: string) =>
    (await fetch(`${BASE}/em-flow/config/add?code=${encodeURIComponent(code)}&name=${encodeURIComponent(name)}`,
      { method: "POST" })).json(),
  emFlowConfigRemove: async (code: string) =>
    (await fetch(`${BASE}/em-flow/config?code=${encodeURIComponent(code)}`, { method: "DELETE" })).json(),
  emFlowConfigReset: async () => (await fetch(`${BASE}/em-flow/config/reset`, { method: "POST" })).json(),

  // 全A逐笔分档（资金流向页）：通达信 / Wind 两套口径
  tapeFlowDates: (limit = 120) => get(`/tape-flow/dates?limit=${limit}`),
  tapeFlowHealth: () => get("/tape-flow/health"),
  tapeFlowSeries: (preset: string, date?: string, th?: string) => {
    const qs = [`preset=${preset}`, date ? `date=${date}` : "", th ? `th=${encodeURIComponent(th)}` : ""]
      .filter(Boolean).join("&");
    return get(`/tape-flow/series?${qs}`);
  },
  tapeFlowScan: async () => (await fetch(`${BASE}/tape-flow/scan`, { method: "POST" })).json(),
  boardWatchAdd: async (code: string, name: string) =>
    (await fetch(`${BASE}/board/watch?code=${encodeURIComponent(code)}&name=${encodeURIComponent(name)}`,
      { method: "POST" })).json(),
  boardWatchRemove: async (code: string) =>
    (await fetch(`${BASE}/board/watch?code=${encodeURIComponent(code)}`, { method: "DELETE" })).json(),

  // 汪汪队 ETF 跟踪器（自包含：24 只宽基 ETF 的成交额倍数 + 主动净买入）
  wangwangSnapshot: (force = false) => get(`/wangwang/snapshot${force ? "?force=1" : ""}`),
  wangwangHealth: () => get("/wangwang/health"),

  // 涨停棱镜（自包含：data/limitup.db，涨停池 + 主题聚合 + 棱镜图）
  limitupOverview: (date?: string, days = 11, top = 15, mode = "zt", sort = "hot", level = "all") =>
    get(`/limitup/overview?days=${days}&top=${top}&mode=${mode}&sort=${sort}&level=${level}`
      + (date ? `&date=${date}` : "")),
  limitupThemes: (date?: string, mode = "zt", sort = "hot", level = "all") =>
    get(`/limitup/themes?mode=${mode}&sort=${sort}&level=${level}${date ? `&date=${date}` : ""}`),
  limitupPrism: (theme?: string, date?: string, days = 25, mode = "zt", sort = "hot") => {
    const qs = [theme ? `theme=${encodeURIComponent(theme)}` : "",
      date ? `date=${date}` : "", `days=${days}`, `mode=${mode}`, `sort=${sort}`]
      .filter(Boolean).join("&");
    return get(`/limitup/prism?${qs}`);
  },
  /** 「主题猎手-涨停分析」弹窗：解读 + 板块炒作原因 + 个股详细涨停原因 */
  limitupBrief: (date?: string, mode = "zt", sort = "hot", level = "all",
                 refresh = false, theme?: string) => {
    const qs = [`mode=${mode}`, `sort=${sort}`, `level=${level}`,
      date ? `date=${date}` : "", refresh ? "refresh=1" : "",
      theme ? `theme=${encodeURIComponent(theme)}` : ""].filter(Boolean).join("&");
    return get(`/limitup/brief?${qs}`);
  },
  limitupPool: (date?: string, theme?: string, kind = "all", mode = "zt") => {
    const qs = [date ? `date=${date}` : "", theme ? `theme=${encodeURIComponent(theme)}` : "",
      `kind=${kind}`, `mode=${mode}`].filter(Boolean).join("&");
    return get(`/limitup/pool?${qs}`);
  },
  limitupHealth: () => get("/limitup/health"),
  limitupRefresh: async (date?: string) =>
    (await fetch(`${BASE}/limitup/refresh${date ? `?date=${date}` : ""}`, { method: "POST" })).json(),
};
