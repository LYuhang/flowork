import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useAuthStore } from '@/stores/auth';
import { useUIStore } from '@/stores/ui';
import { queryClient } from '@/app/query-client';
const identity = { user_id: 'user', tenant_id: 'personal', email: 'user@example.test', displayName: 'User' };
const me = () => new Response(JSON.stringify({ user_id: 'user', tenant_id: 'company', email: identity.email, session: { audience: 'web' } }), { status: 200 });
describe('browser Session reconciliation', () => {
  beforeEach(() => {
    useAuthStore.setState({ authenticated: true, user: { ...identity }, sessionAudience: 'web', organizationSwitching: false, bootstrapped: true });
    useUIStore.getState().setActiveChatId('chat', 'private-chat');
    queryClient.setQueryData(['private-history'], ['private message']);
  });
  afterEach(() => { vi.unstubAllGlobals(); queryClient.clear(); });
  it('reconciles a late 401 without replaying the rejected business request', async () => {
    const fetch = vi.fn().mockResolvedValue(me()); vi.stubGlobal('fetch', fetch);
    useAuthStore.getState().handle401();
    expect(useAuthStore.getState().organizationSwitching).toBe(true);
    expect(useUIStore.getState().activeChatIds.chat).toBeNull();
    expect(queryClient.getQueryData(['private-history'])).toBeUndefined();
    await vi.waitFor(() => expect(useAuthStore.getState().user?.tenant_id).toBe('company'));
    expect(useAuthStore.getState().authenticated).toBe(true);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect((fetch.mock.calls[0][0] as Request).url).toContain('/auth/me');
  });
  it('coalesces concurrent 401s and clears an actually expired Session', async () => {
    let resolve!: (response: Response) => void;
    const fetch = vi.fn().mockReturnValue(new Promise<Response>(r => { resolve = r; })); vi.stubGlobal('fetch', fetch);
    useAuthStore.getState().handle401(); useAuthStore.getState().handle401();
    expect(fetch).toHaveBeenCalledTimes(1);
    resolve(new Response('{}', { status: 401 }));
    await vi.waitFor(() => expect(useAuthStore.getState().authenticated).toBe(false));
    expect(useAuthStore.getState().user).toBeNull();
    expect(useAuthStore.getState().organizationSwitching).toBe(false);
  });
  it('reconciles another tab workspace notification and removes old data immediately', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(me()));
    window.dispatchEvent(new StorageEvent('storage', { key: 'flowork.session-context-changed', newValue: 'change-id', storageArea: localStorage }));
    expect(queryClient.getQueryData(['private-history'])).toBeUndefined();
    await vi.waitFor(() => expect(useAuthStore.getState().user?.tenant_id).toBe('company'));
  });
  it('does not restore an identity after explicit logout supersedes the read', async () => {
    let resolve!: (response: Response) => void;
    vi.stubGlobal('fetch', vi.fn().mockReturnValue(new Promise<Response>(r => { resolve = r; })));
    useAuthStore.getState().handle401();
    useAuthStore.setState({ authenticated: false, user: null, organizationSwitching: false });
    resolve(me()); await new Promise(r => setTimeout(r, 10));
    expect(useAuthStore.getState().authenticated).toBe(false);
    expect(useAuthStore.getState().user).toBeNull();
  });
});
