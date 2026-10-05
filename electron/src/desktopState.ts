import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';

// Remembers the folder the user last picked in a native dialog so the next
// "添加媒体目录" / "截图目录" starts there instead of the filesystem root.
// Stores only app-owned directory strings; never accepts renderer-supplied paths.
const FILE = 'desktop-state.json';
const PURPOSES = ['video', 'screenshot'] as const;
export type PickPurpose = typeof PURPOSES[number];
type State = Partial<Record<PickPurpose, string>>;

function read(directory: string): State {
  if (!directory) return {};
  const file = path.join(directory, FILE);
  try {
    if (!existsSync(file)) return {};
    const parsed: unknown = JSON.parse(readFileSync(file, 'utf8'));
    if (!parsed || typeof parsed !== 'object') return {};
    const source = parsed as Record<string, unknown>;
    const state: State = {};
    for (const purpose of PURPOSES) {
      const value = source[purpose];
      // A directory that has since been deleted or unmounted must not be handed
      // to the dialog, or it opens at an empty fallback location.
      if (typeof value === 'string' && value && existsSync(value)) state[purpose] = value;
    }
    return state;
  } catch {
    return {};
  }
}

export function readLastPicked(directory: string, purpose: PickPurpose): string | undefined {
  return read(directory)[purpose];
}

export function rememberPicked(directory: string, purpose: PickPurpose, value: string) {
  if (!directory || !value) return;
  try {
    writeFileSync(path.join(directory, FILE), JSON.stringify({ ...read(directory), [purpose]: value }, null, 2) + '\n');
  } catch { /* A read-only data directory must not break folder selection. */ }
}
