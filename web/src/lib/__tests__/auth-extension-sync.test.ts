import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/extension', () => ({
  syncAuthToExtension: vi.fn(),
}));

import { initAuthExtensionSync } from '@/lib/auth-extension-sync';
import { syncAuthToExtension } from '@/lib/extension';
import { useAuthStore } from '@/stores/auth';
import { useAgentSettingsStore } from '@/stores/agent-settings';

const syncMock = vi.mocked(syncAuthToExtension);

function resetStores() {
  useAuthStore.setState({
    token: null,
    authenticated: true,
    user: null,
    bootstrapped: true,
  });
  useAgentSettingsStore.setState({
    modelId: null,
    temperature: null,
    maxTokens: null,
    timeout: null,
    reasoningEffort: null,
    approvalMode: 'agent',
  });
}

describe('initAuthExtensionSync', () => {
  beforeEach(() => {
    localStorage.clear();
    resetStores();
    syncMock.mockClear();
  });

  it('syncs authenticated state, tenant hydration, model settings, and clear', () => {
    initAuthExtensionSync();

    expect(syncMock).toHaveBeenLastCalledWith(true, null);

    useAuthStore.setState({
      user: {
        user_id: 'u1',
        tenant_id: 'tenant_1',
        email: 'u@example.com',
        displayName: 'User',
      },
    });

    expect(syncMock).toHaveBeenLastCalledWith(true, 'tenant_1');

    useAgentSettingsStore.getState().setAll({
      modelId: 'codex:credential:cred_1',
      temperature: 0.2,
      maxTokens: 2048,
      timeout: 30,
      reasoningEffort: 'medium',
    });

    expect(syncMock).toHaveBeenLastCalledWith(true, 'tenant_1');
    const callsAfterModelSettings = syncMock.mock.calls.length;

    useAgentSettingsStore.getState().set({ reasoningEffort: 'high' });

    expect(syncMock).toHaveBeenCalledTimes(callsAfterModelSettings + 1);
    expect(syncMock).toHaveBeenLastCalledWith(true, 'tenant_1');
    const callsAfterReasoning = syncMock.mock.calls.length;

    useAgentSettingsStore.getState().setApprovalMode('always_ask');

    expect(syncMock).toHaveBeenCalledTimes(callsAfterReasoning);

    document.dispatchEvent(new CustomEvent('flowork:extension-auth-refresh'));
    expect(syncMock).toHaveBeenLastCalledWith(true, 'tenant_1');

    useAuthStore.setState({
      token: null,
      authenticated: false,
      user: null,
    });

    expect(syncMock).toHaveBeenLastCalledWith(false, null);
  });
});
