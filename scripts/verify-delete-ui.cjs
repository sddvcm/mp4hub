/**
 * 浏览器模式 UI 验证：三点菜单里的删除项 + 二选一弹窗 + 真实删除后列表刷新。
 *
 * 前置：脚本外部已用 Python 启动后端（AVHUB_DATA_DIR 指向 .delprobe/data），
 *      并在 .delprobe/library 中放好两个测试视频。
 * 本脚本只负责浏览器交互与断言，不 spawn 任何进程（沙箱下 Node spawn 受限）。
 */
const { chromium } = require('C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules/playwright-core');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = 'C:/Users/Administrator/WorkBuddy/2026-10-04-16-49-48/mp4hub';
const PROBE = path.join(ROOT, '.delprobe');
const LIBRARY = path.join(PROBE, 'library');
// 端口由 prepare-delete-probe.py 动态分配后写入 server.port。
const portFile = path.join(PROBE, 'server.port');
const BASE = process.env.PROBE_BASE
  || (fs.existsSync(portFile) ? `http://127.0.0.1:${fs.readFileSync(portFile, 'utf8').trim()}` : 'http://127.0.0.1:5687');
const CHROME = 'C:/Users/Administrator/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe';

// Clash 的 HTTP_PROXY 会把 127.0.0.1 也代理出去并返回 502，必须显式放行本机。
for (const key of ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy']) delete process.env[key];
process.env.NO_PROXY = '127.0.0.1,localhost';
process.env.no_proxy = '127.0.0.1,localhost';

async function main() {
  const recycleFile = path.join(LIBRARY, 'ui-recycle.mp4');
  const purgeFile = path.join(LIBRARY, 'ui-purge.mp4');
  if (!fs.existsSync(recycleFile) || !fs.existsSync(purgeFile)) {
    throw new Error('测试视频缺失，请先运行 prepare-delete-probe.py');
  }
  console.log('base        :', BASE);
  console.log('probe videos:', fs.readdirSync(LIBRARY).join(', '));

  let items = [];
  for (let i = 0; i < 40; i++) {
    try {
      const r = await fetch(`${BASE}/api/media?page=1&page_size=50`, { signal: AbortSignal.timeout(4000) });
      const body = await r.json();
      items = body.items || body.media || (Array.isArray(body) ? body : []);
      if (items.length >= 2) break;
    } catch (e) { if (i === 0) console.log('  [fetch]', e.message); }
    await new Promise(r => setTimeout(r, 500));
  }
  console.log('indexed     :', items.length, items.map(x => x.title).join(' | '));
  if (items.length < 2) throw new Error('期望至少 2 条媒体记录');

  const browser = await chromium.launch({
    executablePath: CHROME, headless: true,
    args: ['--no-proxy-server', '--proxy-bypass-list=*'],
    proxy: { server: 'direct://', bypass: '*' },
  });
  const context = await browser.newContext({ viewport: { width: 1400, height: 940 } });
  const page = await context.newPage();
  page.on('console', m => { if (m.type() === 'error') console.log('  [page error]', m.text()); });

  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.media-actions .media-more', { timeout: 25000 });

  const cards = await page.locator('.media-actions .media-more').count();
  console.log('cards       :', cards);

  // ---- 1. 三点菜单包含删除项 ----
  await page.locator('.media-actions .media-more').first().click();
  await page.waitForSelector('.media-menu[role="menu"]', { timeout: 5000 });
  const menuText = await page.locator('.media-menu').first().innerText();
  console.log('menu items  :\n' + menuText.split('\n').map(l => '   ' + l).join('\n'));
  if (!/删除视频/.test(menuText)) throw new Error('菜单缺少删除项');
  if (!/删除是唯一会改动原文件的操作/.test(menuText)) throw new Error('底部提示未更新');
  const dangerColor = await page.locator('.media-menu button.danger').evaluate(el => getComputedStyle(el).color);
  console.log('danger color:', dangerColor);

  // ---- 2. 点开删除弹窗 ----
  await page.locator('.media-menu button.danger').first().click();
  await page.waitForSelector('.modal.delete-dialog', { timeout: 5000 });
  const dialogText = await page.locator('.modal.delete-dialog').innerText();
  console.log('dialog      :\n' + dialogText.split('\n').map(l => '   ' + l).join('\n'));
  if (!/移到回收站/.test(dialogText) || !/彻底删除/.test(dialogText)) throw new Error('弹窗缺少两种删除方式');
  const defaultMode = await page.locator('.delete-dialog input[name="delete-mode"]:checked').getAttribute('value');
  console.log('default mode:', defaultMode);
  if (defaultMode !== 'recycle') throw new Error('默认应为移到回收站（更安全）');

  // ---- 3. 切到彻底删除，按钮文案与配色跟随 ----
  await page.locator('.delete-dialog input[value="permanent"]').check({ force: true });
  await page.waitForTimeout(200);
  const confirmLabel = await page.locator('.delete-dialog .delete-buttons button:last-child').innerText();
  const confirmColor = await page.locator('.delete-dialog .delete-buttons button:last-child').evaluate(el => getComputedStyle(el).color);
  console.log('confirm btn :', confirmLabel.trim(), '| color =', confirmColor);
  if (!/彻底删除/.test(confirmLabel)) throw new Error('确认按钮文案未跟随模式切换');

  // ---- 4. 取消不删文件 ----
  await page.locator('.delete-dialog .delete-buttons button:first-child').click();
  await page.waitForTimeout(300);
  if (await page.locator('.modal.delete-dialog').count()) throw new Error('取消后弹窗未关闭');
  console.log('cancel      : dialog closed | files intact =', fs.existsSync(recycleFile) && fs.existsSync(purgeFile));

  // ---- 5. Esc 也能关闭 ----
  await page.locator('.media-actions .media-more').first().click();
  await page.locator('.media-menu button.danger').first().click();
  await page.waitForSelector('.modal.delete-dialog');
  await page.keyboard.press('Escape');
  await page.waitForTimeout(250);
  if (await page.locator('.modal.delete-dialog').count()) throw new Error('Escape 未关闭弹窗');
  console.log('escape      : dialog closed');

  // ---- 6. 真正执行回收站删除 ----
  const beforeTitles = await page.locator('.video-title').allInnerTexts();
  console.log('before      :', beforeTitles.join(' | '));

  await page.locator('.media-actions .media-more').first().click();
  await page.locator('.media-menu button.danger').first().click();
  await page.waitForSelector('.modal.delete-dialog');
  await page.locator('.delete-dialog .delete-modes label').first().click();
  await page.locator('.delete-dialog .delete-buttons button:last-child').click();

  await page.waitForTimeout(3000);
  const afterTitles = await page.locator('.video-title').allInnerTexts();
  console.log('after       :', afterTitles.join(' | '));
  const toast = await page.locator('.toast').first().innerText().catch(() => '(none)');
  console.log('toast       :', toast.replace(/\n/g, ' '));

  if (afterTitles.length >= beforeTitles.length) throw new Error('删除后列表数量未减少');
  const anyGone = !fs.existsSync(recycleFile) || !fs.existsSync(purgeFile);
  console.log('disk        : recycle exists =', fs.existsSync(recycleFile), '| purge exists =', fs.existsSync(purgeFile));
  if (!anyGone) throw new Error('删除后物理文件仍存在');

  // ---- 7. 刷新后依然消失（记录真被移除，不是前端假隐藏）----
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.media-actions .media-more', { timeout: 15000 });
  await page.waitForTimeout(600);
  const reloadTitles = await page.locator('.video-title').allInnerTexts();
  console.log('after reload:', reloadTitles.join(' | '));
  if (reloadTitles.length >= beforeTitles.length) throw new Error('刷新后记录仍在，列表未真正移除');

  await browser.close();
  console.log('\nUI ASSERTIONS PASSED');
}

main().catch(err => {
  console.error('FAILED:', err.message);
  process.exitCode = 1;
});
