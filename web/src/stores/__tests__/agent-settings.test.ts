/**
 * Agent settings store — persistence + reactivity contract.
 *
 * Covers: bootstrap defaults, setAll persisting to localStorage AND the
 * reactive store, and the non-reactive `getAgentSettings` snapshot used by the
 * SSE request builder.
 */
import { beforeEach, describe, expect, it } from 'vitest';

import {
  STORAGE_KEY,
  getAgentSettings,
  useAgentSettingsStore,
} from '@/stores/agent-settings';

beforeEach(() => {
  localStorage.clear();
  // Reset the runtime mirror to all-defaults between tests.
  useAgentSettingsStore.getState().setAll({
    modelId: null,
    temperature: null,
    maxTokens: null,
    timeout: null,
    reasoningEffort: null,
  });
  localStorage.clear();
});

describe('useAgentSettingsStore', () => {
  it('defaults to all-null (use platform/provider defaults)', () => {
    const s = getAgentSettings();
    expect(s).toEqual({
      modelId: null,
      temperature: null,
      maxTokens: null,
      timeout: null,
      reasoningEffort: null,
    });
  });

  it('setAll updates the store AND persists to localStorage', () => {
    useAgentSettingsStore.getState().setAll({
      modelId: 'codex:credential:cred-123',
      temperature: 0.7,
      maxTokens: 2048,
      timeout: 45,
      reasoningEffort: 'high',
    });

    // Reactive store reflects the write.
    const s = useAgentSettingsStore.getState();
    expect(s.modelId).toBe('codex:credential:cred-123');
    expect(s.temperature).toBe(0.7);
    expect(s.maxTokens).toBe(2048);
    expect(s.timeout).toBe(45);
    expect(s.reasoningEffort).toBe('high');

    // Persisted to localStorage under the documented key.
    const raw = JSON.parse(localStorage.getItem(STORAGE_KEY) as string);
    expect(raw).toEqual({
      modelId: 'codex:credential:cred-123',
      temperature: 0.7,
      maxTokens: 2048,
      timeout: 45,
      reasoningEffort: 'high',
    });
  });

  it('getAgentSettings returns a plain snapshot without setters', () => {
    useAgentSettingsStore.getState().setAll({
      modelId: 'codex:model-x',
      temperature: null,
      maxTokens: 100,
      timeout: null,
      reasoningEffort: 'low',
    });
    const snap = getAgentSettings();
    expect(snap).toEqual({
      modelId: 'codex:model-x',
      temperature: null,
      maxTokens: 100,
      timeout: null,
      reasoningEffort: 'low',
    });
    expect('setAll' in snap).toBe(false);
  });

  it('reset clears runtime state and persisted credential selection', () => {
    useAgentSettingsStore.getState().setAll({
      modelId: 'codex:credential:cred-old-user',
      temperature: 0.2,
      maxTokens: 1000,
      timeout: 30,
      reasoningEffort: 'medium',
    });
    expect(localStorage.getItem(STORAGE_KEY)).toBeTruthy();

    useAgentSettingsStore.getState().reset();

    expect(getAgentSettings()).toEqual({
      modelId: null,
      temperature: null,
      maxTokens: null,
      timeout: null,
      reasoningEffort: null,
    });
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });
});
