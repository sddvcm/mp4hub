/**
 * Which whole-view actions each library view offers, in toolbar order.
 *
 * Kept in its own dependency-free module so the policy can be asserted directly
 * by scripts/test-view-actions.mjs; LibraryAllActions.tsx only renders it.
 *
 * Narrow on purpose — a view offers only what reads naturally there, so nobody
 * can "全部标记已看" a history list they are still working through:
 * - 全部视频 (all): none. The list is a browser, not a work queue.
 * - 继续观看 (continue): clear history only.
 * - 观看历史 (history): clear history only.
 * - 收藏 (favorites): unfavorite only.
 * 电影 / 剧集 keep the full set because they are ordinary browse views.
 */
export type AllActionName = 'unfavorite' | 'mark_watched' | 'mark_unwatched' | 'clear_history';

export const VIEW_ACTIONS: Record<string, AllActionName[]> = {
  all: [],
  continue: ['clear_history'],
  history: ['clear_history'],
  favorites: ['unfavorite'],
  movies: ['unfavorite', 'mark_watched', 'mark_unwatched', 'clear_history'],
  series: ['unfavorite', 'mark_watched', 'mark_unwatched', 'clear_history'],
};

export function actionsForView(view: string): AllActionName[] {
  return VIEW_ACTIONS[view] ?? [];
}
