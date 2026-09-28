import { beforeEach, describe, expect, it, vi } from 'vitest';
import { openProjectChatDraft, acceptProjectChatDraft, clearProjectDraftMemory } from '../project-draft';
import { readRecentChatLocation, chatClientStateKey } from '../state-key';
import { useUIStore } from '@/stores/ui';
import { useChatStreamStore } from '@/stores/chat-stream';

const account = { tenant_id: 'tenant', user_id: 'user' };
const input = { account, projectId: 'project-a', scopeId: 'carrier' };

describe('Project composer placeholders', () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    clearProjectDraftMemory();
    useChatStreamStore.getState().reset();
    useUIStore.setState({ activeChatIds: { chat: null, browser: null }, optimisticChatSessions: [] });
  });

  it('opens and reuses a single local draft without adding history or making requests', () => {
    const fetch = vi.spyOn(globalThis, 'fetch');
    const id = openProjectChatDraft(input);
    const key = chatClientStateKey({ account, scopeId: 'carrier', surface: 'chat', chatId: id });
    useChatStreamStore.getState().setComposerInput(key, 'Unsent text');
    expect(openProjectChatDraft(input)).toBe(id);
    expect(useChatStreamStore.getState().composerInputs[key]).toBe('Unsent text');
    expect(useUIStore.getState().optimisticChatSessions).toEqual([]);
    expect(useUIStore.getState().activeProjectId).toBe('project-a');
    expect(fetch).not.toHaveBeenCalled();
    fetch.mockRestore();
  });

  it('restores the same draft after refresh and keeps Project/account drafts separate', () => {
    const id = openProjectChatDraft(input);
    expect(readRecentChatLocation(account, 'chat')).toEqual({ chatId: id, scopeId: 'carrier', projectId: 'project-a', draft: true });
    clearProjectDraftMemory();
    expect(openProjectChatDraft(input)).toBe(id);
    expect(openProjectChatDraft({ ...input, projectId: 'project-b' })).not.toBe(id);
    expect(openProjectChatDraft({ ...input, account: { ...account, user_id: 'other' } })).not.toBe(id);
    expect(openProjectChatDraft(input)).toBe(id);
  });

  it('promotes only an accepted draft and opens a fresh placeholder afterwards', () => {
    const id = openProjectChatDraft(input);
    acceptProjectChatDraft(account, 'project-a', 'carrier', id);
    expect(readRecentChatLocation(account, 'chat')).toEqual({ chatId: id, scopeId: 'carrier' });
    expect(openProjectChatDraft(input)).not.toBe(id);
  });

  it('never reuses a draft that has already become a conversation in another tab', () => {
    const id = openProjectChatDraft(input);
    expect(openProjectChatDraft({ ...input, startedChatIds: [id] })).not.toBe(id);
  });

  it('late acceptance cannot overwrite a different draft or the current selection', () => {
    const first = openProjectChatDraft(input);
    const second = openProjectChatDraft({ ...input, startedChatIds: [first] });
    acceptProjectChatDraft(account, 'project-a', 'carrier', first);
    expect(openProjectChatDraft(input)).toBe(second);
    expect(readRecentChatLocation(account, 'chat')?.chatId).toBe(second);
  });

  it('keeps opening and accepting drafts functional when Web Storage is unavailable', () => {
    const read = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('Storage unavailable'); });
    const write = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('Storage unavailable'); });
    try {
      const id = openProjectChatDraft(input);
      expect(openProjectChatDraft(input)).toBe(id);
      expect(() => acceptProjectChatDraft(account, 'project-a', 'carrier', id)).not.toThrow();
      expect(openProjectChatDraft(input)).not.toBe(id);
    } finally {
      read.mockRestore();
      write.mockRestore();
    }
  });
});
