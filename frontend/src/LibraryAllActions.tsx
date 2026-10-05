import { useState } from 'react';
import { api, errorText, json } from './api';
import { Button, Dialog, StatusMessage } from './ui';
import { Icon } from './Icon';
import type { BatchAction } from './BatchActions';
import { actionsForView } from './viewActions';

/**
 * Whole-library actions rendered inline on the toolbar, immediately to the right
 * of 批量操作 — they share that row rather than starting a new one.
 *
 * They deliberately do not require selecting anything: the row set is derived
 * from the active view (and media directory) on the server, so "全部清除观看记录"
 * inside 观看历史 empties exactly that list.
 *
 * Because the buttons no longer sit under a "全部操作 · 范围" caption, each button
 * carries the scope in its `title` so the target set stays discoverable.
 */
export type AllAction = Extract<BatchAction, 'unfavorite' | 'mark_watched' | 'mark_unwatched' | 'clear_history'>;

const labels: Record<AllAction, string> = {
  unfavorite: '全部取消收藏', clear_history: '全部清除观看记录',
  mark_watched: '全部标记已看', mark_unwatched: '全部标记未看',
};

/** Actions that need an explicit confirmation because they are hard to undo. */
const destructive: Partial<Record<AllAction, (scope: string) => string>> = {
  unfavorite: scope => `将取消${scope}全部视频的收藏。继续？`,
  clear_history: scope => `将清除${scope}全部视频的观看进度与观看时间。收藏、标签和源文件不受影响，但无法撤销。继续？`,
};

/**
 * Actions rendered in the red "danger" style. 全部取消收藏 and 全部清除观看记录
 * are both destructive (they discard user data), so they share the same colour;
 * the additive 标记 actions stay neutral.
 */
const dangerActions: ReadonlySet<AllAction> = new Set<AllAction>(['unfavorite', 'clear_history']);

/**
 * Which whole-view actions this view offers. The policy itself lives in
 * viewActions.ts so it stays assertable without a DOM.
 */
export function libraryAllActions(view: string): AllAction[] {
  return actionsForView(view) as AllAction[];
}

export function LibraryAllActions({ view, rootId, scope, done }: {
  view: string; rootId: string; scope: string;
  done: (action: AllAction, count: number) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [pending, setPending] = useState<AllAction | null>(null);
  const actions = libraryAllActions(view);
  // 全部视频等视图不提供全部操作，整个容器（含空占位）都不渲染。
  if (!actions.length) return null;

  async function run(action: AllAction) {
    // The confirmation lives in a dialog rather than window.confirm so the exact
    // row count the server will touch is visible before committing.
    const confirm = destructive[action];
    if (confirm && pending !== action) { setPending(action); return; }
    setBusy(true); setError(''); setPending(null);
    try {
      const result = await api<{ updated: number }>('/api/library/all-action',
        json('POST', { view, action, root_id: rootId ? Number(rootId) : null }));
      done(action, result.updated);
    } catch (e) { setError(errorText(e)); }
    finally { setBusy(false); }
  }

  return <>
    {actions.map(action => <Button key={action} disabled={busy}
      variant={dangerActions.has(action) ? 'danger' : 'default'}
      title={`${labels[action]} · ${scope}`}
      onClick={() => void run(action)}>{labels[action]}</Button>)}
    {error && <StatusMessage kind="error">{error}<Button icon="close" onClick={() => setError('')}>关闭</Button></StatusMessage>}
    {pending && <Dialog label="确认全部操作" closeLabel="取消全部操作" busy={busy} close={() => setPending(null)}
      className="modal library-tool-dialog">
      <h2 className="dialog-title"><Icon name="edit" size={22}/>{labels[pending]}</h2>
      <p className="tool-description">{destructive[pending]!(scope)}</p>
      <div className="tool-footer">
        <Button disabled={busy} onClick={() => setPending(null)}>取消</Button>
        <Button variant="danger" busy={busy} icon="close" onClick={() => void run(pending)}>确认执行</Button>
      </div>
    </Dialog>}
  </>;
}
