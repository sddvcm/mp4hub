import { useSyncExternalStore } from 'react';
import { preference, savePreference } from './preferences';

// Display-only library layout switch. Never changes the indexed data or queries.
// The directory sidebar is on by default; an explicit user choice still wins.
type Layout = { directories: boolean };
let state: Layout = { directories: true };
const listeners = new Set<() => void>();
function publish(value: Layout) { state = value; listeners.forEach(listener => listener()); }
export function initializeLibraryLayout() { publish({ directories: preference('libraryDirectories', true) }); }
export function changeLibraryDirectories(directories: boolean) {
  if (directories === state.directories) return;
  savePreference('libraryDirectories', directories);
  publish({ directories });
}
function subscribe(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener); }; }
export function useLibraryLayout() { return useSyncExternalStore(subscribe, () => state); }
