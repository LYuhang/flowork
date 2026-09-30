import { useEffect, useMemo, useReducer } from 'react';
import { useChatStreamStore } from '@/stores/chat-stream';
import { CONTEXT_DRAFT_EVENT, fetchContextDraft, mutateContextDraft } from '@/lib/api/context-draft';
import { CONTEXT_DRAFT_RESET_EVENT, DraftController, type DraftCache, type DraftValue } from './context-draft';

let reloadDraftKey: string | undefined;
const documentWasReloaded = typeof performance !== 'undefined'
  && (performance.getEntriesByType('navigation')[0] as PerformanceNavigationTiming | undefined)?.type === 'reload';
/** Only the initially opened composer is discarded on a full browser reload. */
export function discardReloadedDraft(key: string): boolean {
  if (!documentWasReloaded) return false;
  reloadDraftKey ??= key;
  return reloadDraftKey === key;
}

const controllers = new Map<string, DraftController>();
const listeners = new Map<string, Set<() => void>>();
if (typeof window !== 'undefined') window.addEventListener(CONTEXT_DRAFT_RESET_EVENT, () => {
  controllers.forEach(controller => controller.dispose());
  controllers.clear(); listeners.clear();
});
const storageKey = (key: string) => `vibecanvas:chat-composer:v1:${key}`;
function readCache(key: string): DraftCache | undefined {
  try {
    const value = JSON.parse(localStorage.getItem(storageKey(key)) ?? 'null');
    if (value && typeof value.text === 'string' && Array.isArray(value.attachments)) return value;
  } catch { /* Local recovery must not disable the server draft. */ }
}
function controllerFor(key: string, chatId: string) {
  const existing = controllers.get(key);
  if (existing) return existing;
  const read = (): DraftValue => {
    const state = useChatStreamStore.getState();
    return {text:state.composerInputs[key] ?? '',attachments:state.pendingAttachments[key] ?? []};
  };
  const controller = new DraftController(chatId, {
    get:() => fetchContextDraft(chatId), mutate:operation => mutateContextDraft(chatId,operation,false),
  }, {
    read,
    write:value => {
      const current = read();
      if (current.text === value.text && JSON.stringify(current.attachments) === JSON.stringify(value.attachments)) return;
      useChatStreamStore.setState(state => ({
        composerInputs:{...state.composerInputs,[key]:value.text},
        pendingAttachments:{...state.pendingAttachments,[key]:value.attachments},
      }));
    },
    save:value => {
      try { localStorage.setItem(storageKey(key),JSON.stringify(value)); } catch { /* Keep in-memory draft. */ }
    },
    changed:() => listeners.get(key)?.forEach(listener => listener()),
  }, discardReloadedDraft(key) ? undefined : readCache(key), discardReloadedDraft(key));
  controllers.set(key,controller);
  // Retain unsent operations and active sends; evict only idle clean entries.
  if (controllers.size > 30) for (const [candidate,item] of controllers) {
    if (candidate !== key && !listeners.get(candidate)?.size && !item.pending && !item.sending) {
      item.dispose(); controllers.delete(candidate);
      if (controllers.size <= 30) break;
    }
  }
  return controller;
}

export function useContextDraft(key: string | null, chatId: string | null, enabled: boolean) {
  const [, rerender] = useReducer((value:number) => value + 1, 0);
  const controller = useMemo(() => key && chatId ? controllerFor(key,chatId) : null,[key,chatId]);
  useEffect(() => {
    if (!key || !controller) return;
    const subscribers = listeners.get(key) ?? new Set();
    subscribers.add(rerender); listeners.set(key,subscribers);
    let timer: ReturnType<typeof setTimeout> | undefined;
    const schedule = () => {
      controller.save();
      if (!enabled || controller.sending || controller.conflict) return;
      clearTimeout(timer);
      timer = setTimeout(() => void controller.flush().catch(() => undefined),600);
    };
    const unsubscribe = useChatStreamStore.subscribe((next,previous) => {
      if (next.composerInputs[key] !== previous.composerInputs[key] || next.pendingAttachments[key] !== previous.pendingAttachments[key]) schedule();
    });
    const refresh = () => { if (enabled && !controller.sending) void controller.refresh().then(schedule).catch(() => undefined); };
    const onLocal = (event: Event) => { if ((event as CustomEvent).detail === chatId) refresh(); };
    const channel = typeof BroadcastChannel !== 'undefined' ? new BroadcastChannel(CONTEXT_DRAFT_EVENT) : null;
    if (channel) channel.onmessage = event => { if (event.data?.chatId === chatId) refresh(); };
    window.addEventListener(CONTEXT_DRAFT_EVENT,onLocal);
    window.addEventListener('focus',refresh);
    window.addEventListener('online',refresh);
    const poll = setInterval(() => { if (document.visibilityState === 'visible') refresh(); },20000);
    refresh();
    return () => {
      clearTimeout(timer); clearInterval(poll); unsubscribe(); channel?.close();
      window.removeEventListener(CONTEXT_DRAFT_EVENT,onLocal);
      window.removeEventListener('focus',refresh); window.removeEventListener('online',refresh);
      subscribers.delete(rerender);
      // Store subscription is scoped to mounted UI; unsent cache survives.
    };
  },[controller,key,chatId,enabled]);
  return controller;
}
