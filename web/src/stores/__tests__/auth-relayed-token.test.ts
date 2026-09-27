import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useAuthStore } from '@/stores/auth';
import { useUIStore } from '@/stores/ui';
import { queryClient } from '@/app/query-client';

describe('auth extension exchange hydration', () => {
  beforeEach(() => {
    localStorage.clear();
    useAuthStore.setState({
      token: null,
      authenticated: false,
      user: null,
      bootstrapped: false,
      sessionAudience: null,
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    queryClient.clear();
  });

  it.each([
    ['account', 'user-new', 'tenant-old', 'extension'],
    ['organization', 'user-old', 'tenant-new', 'extension'],
    ['audience', 'user-old', 'tenant-old', 'web'],
  ])('clears previous Chat/cache on a bootstrap %s change', async (_, userId, tenantId, audience) => {
    useAuthStore.setState({ authenticated: true, sessionAudience: 'extension', user: {
      user_id: 'user-old', tenant_id: 'tenant-old', email: 'old@example.test', displayName: 'Old',
    } });
    useUIStore.getState().setActiveChatId('browser', 'old-chat');
    queryClient.setQueryData(['chat-bootstrap', 'browser'], { carrier_scope_id: 'old-scope' });
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
      user_id: userId, tenant_id: tenantId, email: 'new@example.test', session: { audience },
    }), { status: 200 })));
    await useAuthStore.getState().bootstrap();
    expect(useUIStore.getState().activeChatIds.browser).toBeNull();
    expect(queryClient.getQueryData(['chat-bootstrap', 'browser'])).toBeUndefined();
    expect(useAuthStore.getState().user?.user_id).toBe(userId);
  });

  it('preserves Chat and cache on same-identity extension renewal', async () => {
    useAuthStore.setState({ authenticated: true, sessionAudience: 'extension', user: {
      user_id: 'user-old', tenant_id: 'tenant-old', email: 'old@example.test', displayName: 'Old',
    } });
    useUIStore.getState().setActiveChatId('browser', 'live-chat');
    queryClient.setQueryData(['chat-bootstrap', 'browser'], { carrier_scope_id: 'live-scope' });
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
      user_id: 'user-old', tenant_id: 'tenant-old', email: 'old@example.test', session: { audience: 'extension' },
    }), { status: 200 })));
    await useAuthStore.getState().bootstrap();
    expect(useUIStore.getState().activeChatIds.browser).toBe('live-chat');
    expect(queryClient.getQueryData(['chat-bootstrap', 'browser'])).toEqual({ carrier_scope_id: 'live-scope' });
  });

  it('redeems a one-time code without persisting a raw Session', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ ok: true }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            user_id: 'user_embed',
            tenant_id: 'tenant_embed',
            email: 'embed@example.test',
            display_name: 'Embed User',
            session: {
              session_id: 'derived-extension-session',
              generation: 1,
              authentication_strength: 'password',
              step_up_expires_at: null,
              audience: 'extension',
            },
          }),
          {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          },
        ),
      );
    vi.stubGlobal('fetch', fetchMock);

    await useAuthStore.getState().bootstrap('one_time_exchange_code');

    const exchangeRequest = fetchMock.mock.calls[0]?.[0] as Request;
    const meRequest = fetchMock.mock.calls[1]?.[0] as Request;
    expect(exchangeRequest.url).toContain('/api/v1/auth/extension/exchange');
    expect(exchangeRequest.credentials).toBe('include');
    await expect(exchangeRequest.clone().json()).resolves.toEqual({
      code: 'one_time_exchange_code',
    });
    expect(meRequest.url).toContain('/api/v1/auth/me');
    expect(meRequest.headers.has('Authorization')).toBe(false);
    expect(localStorage.getItem('vibecanvas.token')).toBeNull();
    expect(useAuthStore.getState()).toMatchObject({
      token: null,
      authenticated: true,
      bootstrapped: true,
      sessionAudience: 'extension',
      user: {
        user_id: 'user_embed',
        tenant_id: 'tenant_embed',
        email: 'embed@example.test',
        displayName: 'Embed User',
      },
    });
  });
});
