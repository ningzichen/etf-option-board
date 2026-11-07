/**
 * 涨停复盘「周期表」排版回归检查（ego lite 驱动真实浏览器）。
 *
 * 用法： export PATH="$HOME/.local/bin:$PATH"
 *        ego-browser nodejs < scripts/check_limitup_ui.js
 * 依赖：本机 :6799 已启动（LaunchAgent com.etf-option-board），ego lite 已登录无需登录态。
 *
 * 断言的是 2026-11-07 用户反馈过的三个症状：
 *  1) 排名列太窄、标题换行；
 *  2) 红框不跟随点击，固定贴在最新交易日；
 *  3) 红框由逐格 border 拼出来，出现内横线。
 * 全绿退出码 0，任一项失败退出码 1。
 */
const task = await taskSpace("limitup-ui-check");
const page = task.page("p1");
await page.goto("http://127.0.0.1:6799/#/limitup");
await page.reload();
await page.waitForSelector("table", { timeout: 30000 });
await page.waitForTimeout(2500);

const fails = [];
const check = (name, ok, detail) => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}  ${detail ?? ""}`);
  if (!ok) fails.push(name);
};

const a = await page.evaluate(() => {
  const th = document.querySelector("table thead th");
  const range = document.createRange(); range.selectNodeContents(th);
  return { w: Math.round(th.getBoundingClientRect().width), lines: range.getClientRects().length,
    ws: getComputedStyle(th).whiteSpace };
});
check("排名列宽 >= 56px", a.w >= 56, JSON.stringify(a));
check("排名标题单行", a.lines <= 1, `lines=${a.lines}`);

// 逐个点 3 列，验证外框精确压在该列
for (const offset of [3, 5, 1]) {
  const pick = await page.evaluate((back) => {
    const t = document.querySelectorAll("table")[0];
    const heads = [...t.querySelectorAll("thead th")];
    const idx = heads.length - back;
    t.querySelectorAll("tbody tr")[0].children[idx].click();
    return { target: heads[idx].textContent.trim() };
  }, offset);
  await page.waitForTimeout(1400);
  const geo = await page.evaluate((label) => {
    const t = document.querySelectorAll("table")[0];
    const th = [...t.querySelectorAll("thead th")].find(x => x.textContent.trim() === label);
    const overlay = [...t.parentElement.querySelectorAll("div")]
      .find(d => d.className.includes("absolute") && d.className.includes("rose"));
    if (!th || !overlay) return { ok: false, th: null, ov: null };
    const a = th.getBoundingClientRect(), b = overlay.getBoundingClientRect();
    return { ok: Math.abs(a.left - b.left) < 3 && Math.abs(a.width - b.width) < 3 && b.height > a.height,
      th: [Math.round(a.left), Math.round(a.width)], ov: [Math.round(b.left), Math.round(b.width), Math.round(b.height)] };
  }, pick.target);
  check(`外框压在点击列 ${pick.target}`, geo.ok, JSON.stringify(geo));
}

const c = await page.evaluate(() => {
  const t = document.querySelectorAll("table")[0];
  const rose = (v) => /225, 29, 72|e11d48|rgb\(225/.test(v || "");
  const tds = [...t.querySelectorAll("tbody td")];
  return { horizRed: tds.filter(td => { const cs = getComputedStyle(td);
      return rose(cs.borderTopColor) || rose(cs.borderBottomColor); }).length,
    vertRed: tds.filter(td => { const cs = getComputedStyle(td);
      return rose(cs.borderLeftColor) || rose(cs.borderRightColor); }).length };
});
check("红框内部无红色横线", c.horizRed === 0, JSON.stringify(c));
check("红框不在单元格上（改为覆盖层）", c.vertRed === 0, JSON.stringify(c));

console.log(fails.length ? `\n结论: ${fails.length} 项未通过` : "\n结论: 全部通过");
await task.finish({ keep: [] });
process.exit(fails.length ? 1 : 0);
