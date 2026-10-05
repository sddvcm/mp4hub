/**
 * Asserts which whole-view actions each library view offers.
 *
 * The policy lives in frontend/src/viewActions.ts as plain data with no imports,
 * so this reads it straight from source. That keeps the check runnable with bare
 * node (no test runner, no bundler) while still failing on a real regression:
 * requirement is that 全部视频 offers nothing and 收藏 offers only 全部取消收藏.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const source = readFileSync(path.join(root, 'frontend/src/viewActions.ts'), 'utf8');

// Evaluate the module's data by stripping the TypeScript-only syntax and running
// the remainder. Avoids needing a TS loader for what is a data-only file.
const body = source
  .replace(/^export type[\s\S]*?;\s*$/m, '')
  .replace(/export const VIEW_ACTIONS\s*:\s*Record<[^>]*>\s*=/, 'const VIEW_ACTIONS =')
  .replace(/export function actionsForView[\s\S]*$/, '')
  + '\nreturn { VIEW_ACTIONS };';
const { VIEW_ACTIONS } = new Function(body)();

const expected = {
  all: [],
  continue: ['clear_history'],
  history: ['clear_history'],
  favorites: ['unfavorite'],
  movies: ['unfavorite', 'mark_watched', 'mark_unwatched', 'clear_history'],
  series: ['unfavorite', 'mark_watched', 'mark_unwatched', 'clear_history'],
};

assert.deepEqual(VIEW_ACTIONS, expected, '视图 → 全部操作 映射发生变化');

// 全部视频：一个都不显示（需求 5 的核心断言）
assert.deepEqual(VIEW_ACTIONS.all, [], '全部视频不应显示任何全部操作');
// 继续观看 / 观看历史：只有「全部清除观看记录」
for (const view of ['continue', 'history']) {
  assert.deepEqual(VIEW_ACTIONS[view], ['clear_history'], `${view} 只应显示全部清除观看记录`);
}
// 收藏：只有「全部取消收藏」
assert.deepEqual(VIEW_ACTIONS.favorites, ['unfavorite'], '收藏只应显示全部取消收藏');
// 每个动作名都必须是受支持的（防止拼写错误静默变成空按钮）
const known = new Set(['unfavorite', 'mark_watched', 'mark_unwatched', 'clear_history']);
for (const [view, actions] of Object.entries(VIEW_ACTIONS)) {
  for (const action of actions) assert.ok(known.has(action), `${view} 含未知动作 ${action}`);
}

console.log('view-action policy OK:', JSON.stringify(VIEW_ACTIONS));
