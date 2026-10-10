import { create } from 'zustand';

export type SendShortcut = 'modifier-enter' | 'enter';
const STORAGE_KEY = 'flowork.composer.sendShortcut';

function readShortcut(): SendShortcut {
  try {
    return localStorage.getItem(STORAGE_KEY) === 'enter' ? 'enter' : 'modifier-enter';
  } catch {
    return 'modifier-enter';
  }
}

/** A device preference, separate from model settings sent to the agent. */
export const useComposerPreferences = create<{
  sendShortcut: SendShortcut;
  setSendShortcut: (shortcut: SendShortcut) => void;
}>((set) => ({
  sendShortcut: readShortcut(),
  setSendShortcut: (sendShortcut) => {
    set({ sendShortcut });
    try {
      localStorage.setItem(STORAGE_KEY, sendShortcut);
    } catch {
      // The setting still works for this page when browser storage is unavailable.
    }
  },
}));
