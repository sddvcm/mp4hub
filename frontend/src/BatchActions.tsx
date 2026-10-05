import { useState } from 'react';
import { api, errorText, json } from './api';
import { Button, StatusMessage } from './ui';

export type BatchAction = 'favorite' | 'unfavorite' | 'clear_history' | 'mark_watched' | 'mark_unwatched' | 'reset_watched';

const labels: Record<BatchAction, string> = {
  favorite: '批量收藏', unfavorite: '批量取消收藏', clear_history: '批量清除观看记录',
  mark_watched: '批量标记已看', mark_unwatched: '批量标记未看', reset_watched: '恢复自动判断',
};

// Destructive or history-changing actions ask once, and name the exact effect.
const confirmations: Partial<Record<BatchAction, (count: number) => string>> = {
  clear_history: count => `将清除 ${count} 个视频的观看进度与观看时间。收藏、标签和源文件不受影响。继续？`,
  unfavorite: count => `将取消 ${count} 个视频的收藏。继续？`,
};

/** Per-view action set. The order here is the order shown on the toolbar. */
const actionSets: Record<string, BatchAction[]> = {
  history: ['favorite', 'unfavorite', 'mark_watched', 'mark_unwatched', 'reset_watched', 'clear_history'],
  continue: ['favorite', 'unfavorite', 'mark_watched', 'mark_unwatched', 'clear_history'],
  favorites: ['unfavorite', 'mark_watched', 'mark_unwatched', 'reset_watched'],
};

export function BatchActions({ ids, view, done, clear }: {
  ids: number[]; view: string;
  done: (action: BatchAction, count: number) => void;
  clear: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [open, setOpen] = useState(false);
  async function run(action: BatchAction) {
    const confirm = confirmations[action];
    if (confirm && !window.confirm(confirm(ids.length))) return;
    setBusy(true); setError('');
    try {
      const result = await api<{ updated: number }>('/api/library/batch-action', json('POST', { media_ids: ids, action }));
      done(action, result.updated);
    } catch (e) { setError(errorText(e)); }
    finally { setBusy(false); }
  }
  // Every applicable action is a top-level button: no hidden overflow menu, so
  // the destructive ones stay visible and are confirmed individually.
  const actions = actionSets[view] ?? actionSets.history;
  return <div className="bulk-selection-bar batch-actions" aria-label="批量操作">
    <span>已选 {ids.length} / 500 · 支持跨页选择</span>
    {actions.map(action => <Button key={action} disabled={!ids.length || busy}
      variant={action === 'clear_history' ? 'danger' : 'default'}
      onClick={() => void run(action)}>{labels[action]}</Button>)}
    <Button disabled={!ids.length || busy} onClick={clear}>清空选择</Button>
    {error && <StatusMessage kind="error">{error}<Button icon="close" onClick={() => setError('')}>关闭</Button></StatusMessage>}
  </div>;
}
