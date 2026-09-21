import { beforeEach, describe, expect, it } from 'vitest';

import { useAgentSettingsStore } from '@/stores/agent-settings';
import {
  getChatAgentSettings,
  useChatAgentSettingsStore,
} from '@/stores/chat-agent-settings';

describe('chat agent settings', () => {
  beforeEach(() => {
    useAgentSettingsStore.getState().setAll({
      modelId: 'codex:account:default',
      temperature: 0.2,
      maxTokens: null,
      timeout: null,
      reasoningEffort: 'medium',
    });
    useChatAgentSettingsStore.setState({ entries: {} });
  });

  it('seeds drafts from user defaults without leaking changes across Chats', () => {
    const store = useChatAgentSettingsStore.getState();
    store.initializeDraft('chat-a');
    store.set('chat-a', { modelId: 'codex:credential:a', reasoningEffort: 'high' });
    store.initializeDraft('chat-b');

    expect(getChatAgentSettings('chat-a')).toMatchObject({
      modelId: 'codex:credential:a',
      reasoningEffort: 'high',
    });
    expect(getChatAgentSettings('chat-b')).toMatchObject({
      modelId: 'codex:account:default',
      reasoningEffort: 'medium',
    });
  });

  it('hydrates a historical Chat and allows the next Turn to change model', () => {
    const store = useChatAgentSettingsStore.getState();
    store.hydrateBound('chat-a', {
      modelId: 'codex:account:gpt-5',
      temperature: null,
      maxTokens: null,
      timeout: null,
      reasoningEffort: 'high',
    });
    useChatAgentSettingsStore.getState().set('chat-a', {
      modelId: 'codex:credential:other',
      reasoningEffort: 'low',
    });

    expect(getChatAgentSettings('chat-a')).toMatchObject({
      modelId: 'codex:credential:other',
      reasoningEffort: 'low',
    });
  });
});
