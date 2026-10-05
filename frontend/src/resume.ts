import { useSyncExternalStore } from 'react';
import { preference, savePreference } from './preferences';

// Where a new playback session starts when the video already has progress.
// restart -> always from 0; resume -> always the saved position; ask -> prompt.
export type ResumeMode = 'restart' | 'resume' | 'ask';
type Resume = { mode: ResumeMode };
let state: Resume = { mode: 'ask' };
const listeners = new Set<() => void>();
function publish(value: Resume) { state = value; listeners.forEach(listener => listener()); }
export function initializeResume() { publish({ mode: preference<ResumeMode>('resumeMode', 'ask') }); }
export function changeResumeMode(mode: ResumeMode) {
  if (mode === state.mode) return;
  savePreference('resumeMode', mode);
  publish({ mode });
}
function subscribe(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener); }; }
export function useResume() { return useSyncExternalStore(subscribe, () => state); }
export function currentResumeMode() { return state.mode; }
