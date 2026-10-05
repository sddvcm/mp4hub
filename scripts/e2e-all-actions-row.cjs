/**
 * e2e: 全部操作 buttons must sit on the SAME toolbar row as 批量操作,
 * immediately to its right — not wrapped onto their own line.
 */
const { chromium } = require('C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules/playwright-core');

const BASE = 'http://127.0.0.1:7311';
const EXE = 'C:/Users/Administrator/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe';

(async () => {
  const browser = await chromium.launch({
    executablePath: EXE,
    args: ['--no-proxy-server', '--proxy-bypass-list=*'],
    proxy: { server: 'direct://', bypass: '*' },
  });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });

  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.toolbar', { timeout: 25000 });

  const fail = [];
  const check = (v, msg) => { console.log(`${v ? 'PASS' : 'FAIL'}  ${msg}`); if (!v) fail.push(msg); };

  // No standalone 全部操作 bar anywhere.
  const barCount = await page.locator('.library-all-bar').count();
  check(barCount === 0, `独立的 .library-all-bar 已移除（count=${barCount}）`);

  // The literal "全部操作 ·" caption must be gone.
  const caption = await page.locator('text=全部操作 ·').count();
  check(caption === 0, `旧的「全部操作 · 范围」说明文字已移除（count=${caption}）`);

  async function rowOf(view, label, expectedButtons) {
    // Views are switched through the header nav; the URL query param is the
    // most reliable way to land on one deterministically.
    await page.goto(`${BASE}/?view=${view}`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.toolbar', { timeout: 25000 });
    await page.waitForTimeout(1000);
    // The nav button for this view must be the active one, otherwise we are
    // measuring the wrong screen.
    const activeNav = await page.locator('.primary-nav button.active').first().textContent().catch(() => '');
    console.log(`  · view=${view} 当前激活分类: ${ (activeNav || '').trim() }`);

    const bulk = page.locator('.toolbar button', { hasText: /批量操作|批量整理/ }).first();
    if (!await bulk.count()) { check(false, `[${label}] 找到 批量操作 按钮`); return false; }
    const bulkBox = await bulk.boundingBox();

    const found = [];
    for (const text of expectedButtons) {
      const btn = page.locator('.toolbar button', { hasText: text }).first();
      const n = await btn.count();
      if (!n) { found.push({ text, missing: true }); continue; }
      const box = await btn.boundingBox();
      found.push({ text, box, parentRow: box.y + box.height / 2 });
    }

    const bulkMid = bulkBox.y + bulkBox.height / 2;
    const toolbarBox = await page.locator('.toolbar').boundingBox();
    for (const f of found) {
      if (f.missing) { check(false, `[${label}] 「${f.text}」按钮存在`); continue; }
      const sameRow = Math.abs(f.parentRow - bulkMid) < bulkBox.height;
      const toTheRight = f.box.x > bulkBox.x + bulkBox.width - 4;
      const insideToolbar = f.box.y >= toolbarBox.y - 1 && f.box.y + f.box.height <= toolbarBox.y + toolbarBox.height + 1;
      check(sameRow, `[${label}] 「${f.text}」与 批量操作 同一行（Δy=${(f.parentRow - bulkMid).toFixed(1)}px）`);
      check(toTheRight, `[${label}] 「${f.text}」在 批量操作 右侧（Δx=${(f.box.x - bulkBox.x).toFixed(1)}px）`);
      check(insideToolbar, `[${label}] 「${f.text}」位于工具栏内（未换行到下一区域）`);
    }

    // title carries the scope hint.
    for (const text of expectedButtons) {
      const btn = page.locator('.toolbar button', { hasText: text }).first();
      if (!await btn.count()) continue;
      const title = await btn.getAttribute('title');
      check(!!title && title.includes(text), `[${label}] 「${text}」title 含操作名与范围（${title}）`);
    }
    return true;
  }

  await rowOf('movies', '电影', ['全部取消收藏', '全部标记已看', '全部标记未看', '全部清除观看记录']);
  await rowOf('continue', '继续观看', ['全部清除观看记录']);
  await rowOf('history', '观看历史', ['全部清除观看记录']);
  await rowOf('favorites', '收藏', ['全部取消收藏']);

  // 全部视频 must show none of them.
  await page.goto(`${BASE}/?view=all`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.toolbar', { timeout: 25000 });
  await page.waitForTimeout(900);
  for (const text of ['全部取消收藏', '全部标记已看', '全部标记未看', '全部清除观看记录']) {
    const n = await page.locator('.toolbar button', { hasText: text }).count();
    check(n === 0, `[全部视频] 不显示「${text}」（count=${n}）`);
  }

  console.log('\nJS errors:', errors.length ? errors : 'none');
  check(errors.length === 0, '无 JS 错误');

  await page.screenshot({ path: 'C:/Users/Administrator/WorkBuddy/2026-10-04-16-49-48/mp4hub/dist/e2e-all-actions-row.png', fullPage: false });
  await browser.close();
  console.log(`\n${fail.length ? 'FAILED: ' + fail.length : 'ALL PASSED'}`);
  process.exit(fail.length ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
