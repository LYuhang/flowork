// Boot-time wiring that keeps the MV3 extension's stored account in sync with
// the main app's login, so the side-panel embed shares the same session WITHOUT
// any chat handoff. Kept OUT of the auth store to avoid a circular import
// (extension.ts → agent-stream.ts → auth store).
import { useAuthStore } from '@/stores/auth';
import { useAgentSettingsStore } from '@/stores/agent-settings';
import { syncAuthToExtension } from '@/lib/extension';

let installed = false;

/**
 * Call once at app start. After cookie bootstrap, push a one-time exchange code
 * to the extension. Never emit an unauthenticated clear before bootstrap has
 * checked the server-owned Session.
 */
export function initAuthExtensionSync(): void {
  // Only the top-level web app owns the extension account projection. The
  // side-panel iframe starts with partitioned storage and receives its token
  // from the extension shell; treating that initial empty store as a logout
  // would send AUTH_CLEAR back to the extension, erase the relayed token, and
  // close the browser WebSocket before the binding handshake completes.
  if (typeof window !== 'undefined' && window.parent !== window) return;

  const push = () => {
    const { authenticated, bootstrapped, user } = useAuthStore.getState();
    if (!bootstrapped) return;
    void syncAuthToExtension(authenticated, user?.tenant_id ?? null);
  };

  push();
  if (installed) return;
  installed = true;

  useAuthStore.subscribe((state, previous) => {
    if (
      state.authenticated !== previous.authenticated ||
      state.bootstrapped !== previous.bootstrapped ||
      state.user?.tenant_id !== previous.user?.tenant_id
    ) {
      push();
    }
  });

  useAgentSettingsStore.subscribe((state, previous) => {
    if (
      state.modelId !== previous.modelId ||
      state.temperature !== previous.temperature ||
      state.maxTokens !== previous.maxTokens ||
      state.timeout !== previous.timeout ||
      state.reasoningEffort !== previous.reasoningEffort
    ) {
      push();
    }
  });

  // A side panel may open long after the boot-time one-time exchange code has
  // expired. Its content script asks the already-open main app to mint a fresh
  // code only after the embedded partition confirms it has no Session.
  document.addEventListener('flowork:extension-auth-refresh', push);
}
