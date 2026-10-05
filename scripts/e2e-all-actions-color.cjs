/**
 * 验证「全部取消收藏」与「全部清除观看记录」按钮都是红色（danger 样式），
 * 而「全部标记已看 / 全部标记未看」保持默认中性色。
 *
 * 同时验证批量操作行（BatchActions）里的「批量取消收藏」也是红色。
 */
const { chromium } = require('C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules/playwright-core');

const BASE = process.env.BASE || 'http://127.0.0.1:7391';
const EXE = 'C:/Users/Administrator/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe';

let pass = 0, fail = 0;
function check(label, ok, detail = '') {
  if (ok) { pass++; console.log(`  PASS  ${label}`); }
  else { fail++; console.log(`  FAIL  ${label}  ${detail}`); }
}

(async () => {
  const browser = await chromium.launch({
    executablePath: EXE,
    args: ['--no-proxy-server', '--proxy-bypass-list=*'],
    proxy: { server: 'direct://', bypass: '*' },
  });
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });

  // 读取某按钮的实际计算样式，判断是否为 danger（红色前景 + 淡红背景）
  const probe = async (view, text) => {
    await page.goto(`${BASE}/?view=${view}`, { waitUntil: 'networkidle' });
    await page.waitForTimeout(900);
    const btn = page.locator(`button:has-text("${text}")`).first();
    const cnt = await btn.count();
    if (!cnt) return { found: false };
    const info = await btn.evaluate(el => {
      const cs = getComputedStyle(el);
      return {
        cls: el.className,
        color: cs.color,
        bg: cs.backgroundColor,
        border: cs.borderColor,
      };
    });
    return { found: true, ...info };
  };

  console.log('\n=== 全部操作行（LibraryAllActions）===');

  // 收藏视图：只有「全部取消收藏」
  const fav = await probe('favorites', '全部取消收藏');
  check('收藏视图存在「全部取消收藏」', fav.found);
  if (fav.found) {
    check('「全部取消收藏」带 danger-action 类', /danger-action/.test(fav.cls), fav.cls);
  }

  // 观看历史视图：只有「全部清除观看记录」
  const hist = await probe('history', '全部清除观看记录');
  check('观看历史存在「全部清除观看记录」', hist.found);
  if (hist.found) {
    check('「全部清除观看记录」带 danger-action 类', /danger-action/.test(hist.cls), hist.cls);
  }

  // 两者颜色必须一致
  if (fav.found && hist.found) {
    check('两按钮前景色一致（同一红色）', fav.color === hist.color, `${fav.color} vs ${hist.color}`);
    check('两按钮背景色一致', fav.bg === hist.bg, `${fav.bg} vs ${hist.bg}`);
    check('两按钮边框色一致', fav.border === hist.border, `${fav.border} vs ${hist.border}`);
    check('前景色确为红色系（R 明显大于 G、B）', (() => {
      const m = (fav.color || '').match(/(\d+),\s*(\d+),\s*(\d+)/);
      if (!m) return false;
      const [r, g, b] = [+m[1], +m[2], +m[3]];
      return r > 120 && r > g + 60 && r > b + 60;
    })(), fav.color);
  }

  // 电影视图：四个按钮，取消收藏应红、标记应中性
  console.log('\n=== 电影视图（四个动作）===');
  const mvUnfav = await probe('movies', '全部取消收藏');
  const mvWatch = await probe('movies', '全部标记已看');
  const mvUnwatch = await probe('movies', '全部标记未看');
  check('电影视图「全部取消收藏」为红色', mvUnfav.found && /danger-action/.test(mvUnfav.cls), mvUnfav.cls);
  check('电影视图「全部标记已看」保持中性', mvWatch.found && !/danger-action/.test(mvWatch.cls), mvWatch.cls);
  check('电影视图「全部标记未看」保持中性', mvUnwatch.found && !/danger-action/.test(mvUnwatch.cls), mvUnwatch.cls);
  if (mvUnfav.found && mvWatch.found) {
    check('红/中性两按钮颜色确实不同', mvUnfav.color !== mvWatch.color, `${mvUnfav.color} vs ${mvWatch.color}`);
  }

  // 批量操作行：需要先进入批量模式（点「批量操作 / 批量整理」），检查「批量取消收藏」
  console.log('\n=== 批量操作行（BatchActions）===');
  await page.goto(`${BASE}/?view=favorites`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(900);
  // 展开批量操作栏（工具栏的「批量操作」按钮，aria-pressed 切换 bulkMode）
  const toggle = page.getByRole('button', { name: '批量操作', exact: true }).first();
  if (await toggle.count()) {
    await toggle.click();
    await page.waitForTimeout(900);
  } else {
    console.log('  (未找到「批量操作」按钮)');
  }
  await page.locator('.batch-actions').first().waitFor({ timeout: 3000 }).catch(() => {});
  const batchBtn = page.locator('button:has-text("批量取消收藏")').first();
  const batchFound = await batchBtn.count();
  check('收藏视图存在「批量取消收藏」', batchFound > 0);
  if (batchFound) {
    const batch = await batchBtn.evaluate(el => ({
      cls: el.className,
      color: getComputedStyle(el).color,
      bg: getComputedStyle(el).backgroundColor,
      border: getComputedStyle(el).borderColor,
    }));
    check('「批量取消收藏」带 danger-action 类', /danger-action/.test(batch.cls), batch.cls);
    if (fav.found) {
      check('批量版与全部版颜色一致', batch.color === fav.color, `${batch.color} vs ${fav.color}`);
    }
  }

  console.log(`\n=== 结果：${pass} 通过 / ${fail} 失败 ===`);
  await browser.close();
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error('脚本异常:', e); process.exit(2); });
