import { beforeEach, describe, expect, it } from 'vitest';

import { readChatViewPreferences, writeChatViewPreferences, EMPTY_CHAT_VIEW_STATE } from '@/lib/chat/preview-state';
import {
  chatAccountNamespace,
  chatClientStateKey,
  clearRecentChatSelections,
  readRecentChatLocation,
  readRecentChatSelection,
  writeRecentChatSelection,
} from '@/lib/chat/state-key';

describe('chat client state identity', () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
  });

  it('namespaces the same chat by tenant, user, scope, and surface', () => {
    const accountA = { tenant_id: 'tenant-a', user_id: 'user-a' };
    const accountB = { tenant_id: 'tenant-b', user_id: 'user-a' };
    expect(chatAccountNamespace(accountA)).toBe('tenant-a:user-a');
    expect(chatClientStateKey({ account: accountA, scopeId: 'scope', surface: 'chat', chatId: 'chat-1' }))
      .not.toBe(chatClientStateKey({ account: accountB, scopeId: 'scope', surface: 'chat', chatId: 'chat-1' }));
    expect(chatClientStateKey({ account: accountA, scopeId: 'scope', surface: 'chat', chatId: 'chat-1' }))
      .not.toBe(chatClientStateKey({ account: accountA, scopeId: 'scope', surface: 'browser', chatId: 'chat-1' }));
  });

  it('restores only an account-scoped recent chat id from tab storage', () => {
    const accountA = { tenant_id: 'tenant-a', user_id: 'user-a' };
    const accountB = { tenant_id: 'tenant-b', user_id: 'user-a' };

    writeRecentChatSelection(accountA, 'chat', 'chat-123', 'scope-456');

    expect(readRecentChatSelection(accountA, 'chat')).toBe('chat-123');
    expect(readRecentChatLocation(accountA, 'chat')).toEqual({
      chatId: 'chat-123',
      scopeId: 'scope-456',
    });
    expect(readRecentChatSelection(accountA, 'browser')).toBeNull();
    expect(readRecentChatSelection(accountB, 'chat')).toBeNull();
    expect([...Array(window.sessionStorage.length)].map((_, index) => (
      window.sessionStorage.getItem(window.sessionStorage.key(index)!)
    )).join('|')).not.toContain('message');
  });

  it('accepts an id-only rolling-upgrade hint without inventing a scope', () => {
    const account = { tenant_id: 'tenant-a', user_id: 'user-a' };
    writeRecentChatSelection(account, 'chat', 'legacy-chat');

    expect(readRecentChatLocation(account, 'chat')).toEqual({
      chatId: 'legacy-chat',
      scopeId: null,
    });
  });

  it('clears recent chat navigation hints at an authorization boundary', () => {
    writeRecentChatSelection({ tenant_id: 'tenant-a', user_id: 'user-a' }, 'chat', 'chat-a');
    writeRecentChatSelection({ tenant_id: 'tenant-b', user_id: 'user-b' }, 'browser', 'chat-b');
    window.sessionStorage.setItem('unrelated', 'keep');

    clearRecentChatSelections();

    expect(window.sessionStorage.getItem('unrelated')).toBe('keep');
    expect(window.sessionStorage.length).toBe(1);
  });

  it('persists presentation preferences without persisting artifact payloads', () => {
    const storageKey = 'chat-view-test';
    writeChatViewPreferences(storageKey, {
      ...EMPTY_CHAT_VIEW_STATE,
      previewOpen: true,
      todoCollapsed: true,
      activePreviewId: 'artifact-1',
      previewItems: [{
        id: 'artifact-1',
        title: 'Sensitive artifact',
        resource: { schemaVersion: 1, kind: 'interactive', artifactId: 'artifact-1' },
        artifact: {
          kind: 'interactive_artifact',
          artifact_id: 'artifact-1',
          component_type: 'html_preview',
          completion_mode: 'render_only',
          title: 'Sensitive artifact',
          props: { html: '<p>not persisted</p>' },
        },
      }],
    });

    expect(readChatViewPreferences(storageKey)).toEqual({
      explorerOpen: false,
      debugOpen: false,
      previewOpen: true,
      todoCollapsed: true,
      activePreviewId: 'artifact-1',
      previewItems: [],
    });
    expect(window.localStorage.getItem(storageKey)).not.toContain('not persisted');
  });

  it('restores bounded file references without trusting a persisted id', () => {
    const storageKey = 'chat-view-file-preview';
    writeChatViewPreferences(storageKey, {
      ...EMPTY_CHAT_VIEW_STATE,
      previewOpen: true,
      activePreviewId: 'file:chat:chat-1:/data/diagrams/example.drawio',
      previewItems: [{
        id: 'file:chat:chat-1:/data/diagrams/example.drawio',
        title: 'example.drawio',
        resource: {
          schemaVersion: 1,
          kind: 'file',
          fileRef: {
            schemaVersion: 1,
            scope: 'chat',
            chatId: 'chat-1',
            path: '/data/diagrams/example.drawio',
          },
        },
      }],
    });

    const raw = JSON.parse(window.localStorage.getItem(storageKey)!) as {
      previewItems: Array<{ id: string }>;
    };
    raw.previewItems[0].id = 'forged-id';
    window.localStorage.setItem(storageKey, JSON.stringify(raw));

    expect(readChatViewPreferences(storageKey)?.previewItems).toEqual([{
      id: 'file:chat:chat-1:/data/diagrams/example.drawio',
      title: 'example.drawio',
      resource: {
        schemaVersion: 1,
        kind: 'file',
        fileRef: {
          schemaVersion: 1,
          scope: 'chat',
          chatId: 'chat-1',
          path: '/data/diagrams/example.drawio',
        },
      },
    }]);
  });
});
