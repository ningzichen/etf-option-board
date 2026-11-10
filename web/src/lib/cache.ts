/* 本地业务缓存：IndexedDB 持久化 + 内存 LRU。
 *
 * 用途：ETF 切换时先渲染上次的数据（stale-while-revalidate），避免空白骨架。
 * 约定：
 *  - 键 = 类型 + 标的代码（+ 期权链月份），值里记录交易日；跨日只保留 ETF 上下文。
 *  - 存的是接口原始响应，渲染层照旧。
 *  - IndexedDB 不可用/配额满/隐私模式 → 静默退化为纯内存，绝不影响页面。
 *  - 总量 20MB 上限，按最后访问时间淘汰。
 */

export type CacheKind =
  | "ctx" | "chain" | "plan" | "sig" | "trends" | "levels"
  // 板块资金：排行 / 分时 / 详情 / 多日 / ETF / 4维
  | "brank" | "bseries" | "bdetail" | "bmulti" | "etfrank" | "etfseries" | "emseries";

const DB_NAME = "etf-board";
const STORE = "entries";
// 板块资金四页 + 详情的缓存量比期权上下文大一个量级，容量提到 60MB
const CAP_BYTES = 60 * 1024 * 1024;
const MEM_MAX = 40;

type Entry = { k: string; day: string; ts: number; at: number; size: number; data: any };

const mem = new Map<string, Entry>();
const meta = new Map<string, { size: number; at: number }>();
let total = 0;
let dbp: Promise<IDBDatabase | null> | null = null;
let indexReady: Promise<void> | null = null;

/** 北京时间的交易日（YYYY-MM-DD）。 */
export function dayKey(d = new Date()): string {
  try {
    return new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai" }).format(d);
  } catch {
    return d.toISOString().slice(0, 10);
  }
}

// v2：修复前「切合约时旧请求把数据写进新合约的键」污染过缓存，加版本段让旧键自然失效。
// v3：板块排行新增拼音首字母（py）字段，旧缓存没有该字段会让拼音搜索失效。
// v4：排行新增涨速（speed）、指数新增涨跌幅（pct），旧缓存会让「涨速」排序失效。
// v5：ETF 分时轴补成完整分钟（半小时刻度可对齐），旧缓存的稀疏轴会让刻度缺失。
const CACHE_VER = "v8";

const keyOf = (kind: CacheKind, code: string, extra?: string | number | null) =>
  `${CACHE_VER}|${kind}|${code}|${extra ?? "-"}`;

function open(): Promise<IDBDatabase | null> {
  if (dbp) return dbp;
  dbp = new Promise<IDBDatabase | null>((resolve) => {
    try {
      if (typeof indexedDB === "undefined") return resolve(null);
      const req = indexedDB.open(DB_NAME, 1);
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE, { keyPath: "k" });
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => resolve(null);
      req.onblocked = () => resolve(null);
    } catch {
      resolve(null);
    }
  });
  return dbp;
}

function tx(db: IDBDatabase, mode: IDBTransactionMode) {
  return db.transaction(STORE, mode).objectStore(STORE);
}

function idbGet(k: string): Promise<Entry | null> {
  return open().then(
    (db) =>
      new Promise<Entry | null>((resolve) => {
        if (!db) return resolve(null);
        try {
          const req = tx(db, "readonly").get(k);
          req.onsuccess = () => resolve((req.result as Entry) ?? null);
          req.onerror = () => resolve(null);
        } catch {
          resolve(null);
        }
      }),
  );
}

function idbPut(e: Entry): Promise<void> {
  return open().then(
    (db) =>
      new Promise<void>((resolve) => {
        if (!db) return resolve();
        try {
          const req = tx(db, "readwrite").put(e);
          req.onsuccess = () => resolve();
          req.onerror = () => resolve();
        } catch {
          resolve();
        }
      }),
  );
}

function idbDel(k: string): Promise<void> {
  return open().then(
    (db) =>
      new Promise<void>((resolve) => {
        if (!db) return resolve();
        try {
          const req = tx(db, "readwrite").delete(k);
          req.onsuccess = () => resolve();
          req.onerror = () => resolve();
        } catch {
          resolve();
        }
      }),
  );
}

/** 首次写入前把索引载入内存（用于容量统计与 LRU 淘汰）。 */
function ensureIndex(): Promise<void> {
  if (indexReady) return indexReady;
  indexReady = open().then(
    (db) =>
      new Promise<void>((resolve) => {
        if (!db) return resolve();
        try {
          const req = tx(db, "readonly").getAll();
          req.onsuccess = () => {
            for (const e of (req.result as Entry[]) ?? []) {
              if (!e?.k) continue;
              meta.set(e.k, { size: e.size ?? 0, at: e.at ?? 0 });
              total += e.size ?? 0;
            }
            resolve();
          };
          req.onerror = () => resolve();
        } catch {
          resolve();
        }
      }),
  );
  return indexReady;
}

function touchMem(e: Entry) {
  mem.delete(e.k);
  mem.set(e.k, e);
  while (mem.size > MEM_MAX) {
    const oldest = mem.keys().next().value as string | undefined;
    if (oldest == null) break;
    mem.delete(oldest);
  }
}

/** 板块资金类缓存：容量吃紧时优先淘汰它们，别把期权上下文挤掉。 */
const BOARD_KINDS = ["|brank|", "|bseries|", "|bdetail|", "|bmulti|", "|etfrank|", "|etfseries|", "|emseries|"];
const isBoardEntry = (k: string) => BOARD_KINDS.some((seg) => k.includes(seg));

async function evictOne(): Promise<boolean> {
  // 第一轮只淘汰板块类；没有可淘汰的再退回全局 LRU（保证一定能腾出空间）
  for (const boardOnly of [true, false]) {
    let pick: string | null = null;
    let at = Infinity;
    for (const [k, m] of meta) {
      if (boardOnly && !isBoardEntry(k)) continue;
      if (m.at < at) {
        at = m.at;
        pick = k;
      }
    }
    if (pick) {
      total -= meta.get(pick)?.size ?? 0;
      meta.delete(pick);
      mem.delete(pick);
      await idbDel(pick);
      return true;
    }
  }
  return false;
}

export type Cached<T = any> = { data: T; ts: number; day: string };

/** 读缓存。非上下文类型跨日视为失效并删除。 */
export async function cacheRead<T = any>(
  kind: CacheKind,
  code: string,
  extra?: string | number | null,
): Promise<Cached<T> | null> {
  try {
    const k = keyOf(kind, code, extra);
    let e = mem.get(k) ?? (await idbGet(k));
    if (!e) return null;
    const today = dayKey();
    if (e.day !== today && kind !== "ctx") {
      meta.delete(k);
      mem.delete(k);
      total -= e.size ?? 0;
      void idbDel(k);
      return null;
    }
    e.at = Date.now();
    touchMem(e);
    const m = meta.get(k);
    if (m) m.at = e.at;
    return { data: e.data as T, ts: e.ts, day: e.day };
  } catch {
    return null;
  }
}

/** 写缓存。错误响应不写。 */
export async function cacheWrite(
  kind: CacheKind,
  code: string,
  extra: string | number | null | undefined,
  data: any,
): Promise<void> {
  try {
    if (!data || data.error) return;
    const body = JSON.stringify(data);
    if (!body) return;
    const size = body.length * 2;
    if (size > CAP_BYTES) return;
    await ensureIndex();
    const k = keyOf(kind, code, extra);
    total -= meta.get(k)?.size ?? 0;
    while (total + size > CAP_BYTES) {
      if (!(await evictOne())) break;
    }
    const now = Date.now();
    const e: Entry = { k, day: dayKey(), ts: now, at: now, size, data };
    meta.set(k, { size, at: now });
    total += size;
    touchMem(e);
    await idbPut(e);
    if (debugOn()) {
      console.debug(`[cache] put ${kind}:${code} ${(size / 1024).toFixed(0)}KB · ${meta.size} 项 / ${(total / 1048576).toFixed(1)}MB`);
    }
  } catch {
    /* 缓存失败一律静默 */
  }
}

/** 调试摘要开关：localStorage 设 board.cache-debug=1 时才打印。 */
function debugOn(): boolean {
  try {
    return localStorage.getItem("board.cache-debug") === "1";
  } catch {
    return false;
  }
}
