/**
 * Asserts the "remember the last picked folder" store.
 *
 * Covers the two ways this silently breaks user trust: a remembered folder that
 * has since been deleted (the dialog would open somewhere unexpected) and a
 * read-only data directory (must not throw during folder selection).
 */
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { readLastPicked, rememberPicked } from '../dist/desktopState.js';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const build = path.join(root, 'build');
mkdirSync(build, { recursive: true });
const temporary = mkdtempSync(path.join(build, 'desktop-state-'));

try {
  const first = path.join(temporary, 'first');
  const second = path.join(temporary, 'second');
  mkdirSync(first); mkdirSync(second);

  // 空目录：没有记录
  assert.equal(readLastPicked(temporary, 'video'), undefined);

  // 记一次，读取相同
  rememberPicked(temporary, 'video', first);
  assert.equal(readLastPicked(temporary, 'video'), first);

  // 两个 purpose 互不干扰
  rememberPicked(temporary, 'screenshot', second);
  assert.equal(readLastPicked(temporary, 'video'), first);
  assert.equal(readLastPicked(temporary, 'screenshot'), second);

  // 覆盖写入保留另一个 purpose
  rememberPicked(temporary, 'video', second);
  assert.equal(readLastPicked(temporary, 'video'), second);
  assert.equal(readLastPicked(temporary, 'screenshot'), second);

  // 目录消失后必须被忽略，否则对话框会开到无效位置
  const vanished = path.join(temporary, 'vanished');
  mkdirSync(vanished);
  rememberPicked(temporary, 'video', vanished);
  assert.equal(readLastPicked(temporary, 'video'), vanished);
  rmSync(vanished, { recursive: true, force: true });
  assert.equal(readLastPicked(temporary, 'video'), undefined, '已删除的目录不应继续被记住');

  // 损坏的 JSON 不能抛错
  writeFileSync(path.join(temporary, 'desktop-state.json'), '{ not json');
  assert.equal(readLastPicked(temporary, 'video'), undefined);

  // 空 dataDir 直接跳过，不产生任何文件
  assert.equal(readLastPicked('', 'video'), undefined);
  rememberPicked('', 'video', first);
  assert.equal(readLastPicked('', 'video'), undefined);

  // 写出的文件是稳定格式，便于人工排查
  rmSync(path.join(temporary, 'desktop-state.json'), { force: true });
  rememberPicked(temporary, 'video', first);
  assert.deepEqual(JSON.parse(readFileSync(path.join(temporary, 'desktop-state.json'), 'utf8')), { video: first });

  console.log('desktop-state store OK');
} finally {
  rmSync(temporary, { recursive: true, force: true });
}
