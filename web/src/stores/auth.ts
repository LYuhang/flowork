/**
 * Auth store — holds only authentication state + current user.
 *
 * Authentication is restored from the server session, never a pasted dev token.
 * The store now owns the real auth state:
 *
 *   - The raw Session is a Secure/HttpOnly cookie and is never readable here.
 *   - `token` remains as a temporary null-only compatibility field while API
 *     modules shed their optional Authorization header branches.
 *   - `authenticated` is the browser projection of the server-owned Session.
 *   - `user` — `{user_id, tenant_id, email}` populated from
 *     `GET /api/v1/auth/me` after login/register and on boot.
 *
 * Login pages call `login(...)` / `signup(...)` directly — they throw on
 * failure so the page can map HTTP status to a localized message. The
 * shared api client's `handle401` middleware still runs for business
 * routes and routes the user to `/login` on Session expiry.
 *
 * Auth requests go through plain `fetch()` (not the openapi-fetch client)
 * for two reasons: (a) the auth endpoints are not yet in the generated
 * OpenAPI schema, and (b) we explicitly do NOT want the apiClient's
 * `handle401` middleware firing on the /login route when the user types
 * the wrong password — that would clear local state and redirect them to
 * themselves.
 */
import { create } from 'zustand';
import { subscribeWithSelector } from 'zustand/middleware';
import { getApiBase } from '@/lib/base-path';
import {
  resetAuthScopedClientState,
  resetOrganizationScopedClientState,
} from '@/lib/auth/reset-client-state';
import { sessionFetch } from '@/lib/api/session-fetch';
import { queryClient } from '@/app/query-client';
import { readRecentChatLocation } from '@/lib/chat/state-key';

const API_BASE = getApiBase();

async function warmRecentChatHistory(user: AuthUser | null): Promise<void> {
  if (!user || typeof window === 'undefined') return;
  const pathname = window.location.pathname.replace(/\/$/, '');
  if (!pathname.endsWith('/chat') || pathname.endsWith('/embed/chat')) return;
  const recent = readRecentChatLocation(user, 'chat');
  if (!recent?.scopeId) return;
  const params = new URLSearchParams({ limit: '30', offset: '0', tail: 'true' });
  const response = await sessionFetch(
    `${API_BASE}/api/v1/chat-scopes/${encodeURIComponent(recent.scopeId)}`
      + `/chats/${encodeURIComponent(recent.chatId)}/messages?${params.toString()}`,
    { credentials: 'include' },
  );
  if (!response.ok) return;
  const page = await response.json() as {
    items?: unknown[];
    total?: number;
    limit?: number;
    offset?: number;
  };
  if (!Array.isArray(page.items)) return;
  queryClient.setQueryData(
    ['chat-history', recent.scopeId, recent.chatId, null],
    {
      items: page.items,
      total: typeof page.total === 'number' ? page.total : page.items.length,
      limit: typeof page.limit === 'number' ? page.limit : 30,
      offset: typeof page.offset === 'number' ? page.offset : 0,
    },
  );
}

// One-way migration: an older build persisted the raw Session bearer here.
// Remove it before any application code or extension sync can observe it.
if (typeof localStorage !== 'undefined') {
  localStorage.removeItem('vibecanvas.token');
}

export interface AuthUser {
  user_id: string;
  tenant_id: string;
  email: string;
  displayName: string;
  platformManagementRole?: 'platform_support' | 'platform_security_admin' | null;
}

export interface PrivilegedAccessProjection {
  requestId: string;
  organizationId: string;
  resourceType: string | null;
  resourceId: string | null;
  actions: string[];
  expiresAt: string;
}

export interface AuthState {
  /** @deprecated Raw browser Sessions are HttpOnly; this is always null. */
  token: string | null;
  authenticated: boolean;
  user: AuthUser | null;
  sessionAudience: string | null;
  privilegedAccess: PrivilegedAccessProjection | null;
  /** Set to `true` once the boot-time `/auth/me` hydration has settled. */
  bootstrapped: boolean;
  /** Hide organization-scoped surfaces while the server rotates context. */
  organizationSwitching: boolean;
  /** POST /auth/login and hydrate the resulting Session. */
  login: (email: string, password: string) => Promise<void>;
  /** POST /auth/register. The API auto-logs the new user in. */
  signup: (email: string, password: string, username: string) => Promise<void>;
  /** POST /auth/logout (best-effort), then clear local state. */
  logout: () => Promise<void>;
  /** Delete the current account, then clear local auth state. */
  deleteAccount: (email: string) => Promise<void>;
  /** Cancel a delayed deletion request before the purge worker claims it. */
  cancelAccountDeletion: (email: string, password: string) => Promise<void>;
  /** Atomically rotate the Session to another active organization. */
  switchOrganization: (organizationId: string) => Promise<void>;
  /** End the active support capability while preserving its parent Web Session. */
  exitPrivilegedAccess: () => Promise<void>;
  /** Reconcile remote support activation, expiry, or revocation. */
  refreshPrivilegedAccess: () => Promise<void>;
  /** Called by the api client middleware when any business route returns 401. */
  handle401: () => void;
  /**
   * Hydrate through /auth/me. Embedded surfaces may first redeem a one-time
   * exchange code, which sets a partitioned HttpOnly extension cookie.
   */
  bootstrap: (extensionExchangeCode?: string) => Promise<void>;
}

/**
 * POST JSON helper for the /auth/* endpoints. Throws `AuthApiError` (with
 * the parsed `{detail}` string if present) on any non-2xx so callers can
 * map status → localized message at the page layer.
 */
export class AuthApiError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) {
    super(`auth ${status}: ${detail}`);
    this.status = status;
    this.detail = detail;
  }
}

async function authPost(
  path: string,
  body: Record<string, unknown> | null,
): Promise<unknown> {
  const headers: Record<string, string> = {};
  if (body !== null) headers['Content-Type'] = 'application/json';
  const res = await sessionFetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers,
    credentials: 'include',
    body: body === null ? null : JSON.stringify(body),
  });
  if (res.status === 204 || res.status === 205) return null;
  let payload: unknown = null;
  const text = await res.text();
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = text;
    }
  }
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    if (
      payload &&
      typeof payload === 'object' &&
      'detail' in (payload as Record<string, unknown>)
    ) {
      const d = (payload as Record<string, unknown>).detail;
      if (typeof d === 'string') detail = d;
    }
    throw new AuthApiError(res.status, detail);
  }
  return payload;
}

interface LoginResponse {
  user: { user_id: string; email: string; display_name?: string };
}

interface MeResponse {
  user_id: string;
  tenant_id: string;
  active_organization_id?: string;
  email: string;
  display_name?: string;
  platform_management_role?: 'platform_support' | 'platform_security_admin' | null;
  session?: { audience?: string };
  privileged_access?: {
    active?: boolean;
    request_id?: string | null;
    resource_type?: string | null;
    resource_id?: string | null;
    actions?: string[];
    expires_at?: string | null;
  };
}

interface OrganizationSwitchResponse {
  organization_id: string;
  session_generation: number;
}

interface AuthProjection {
  user: AuthUser;
  sessionAudience: string;
  privilegedAccess: PrivilegedAccessProjection | null;
}

function projectionFromMe(me: MeResponse, fallbackDisplayName = ''): AuthProjection {
  const privileged = me.privileged_access;
  return {
    user: {
      user_id: me.user_id,
      email: me.email,
      tenant_id: me.active_organization_id ?? me.tenant_id,
      displayName: me.display_name ?? fallbackDisplayName,
      platformManagementRole: me.platform_management_role ?? null,
    },
    sessionAudience: me.session?.audience ?? 'web',
    privilegedAccess: privileged?.active
      && privileged.request_id
      && privileged.expires_at
      ? {
          requestId: privileged.request_id,
          organizationId: me.active_organization_id ?? me.tenant_id,
          resourceType: privileged.resource_type ?? null,
          resourceId: privileged.resource_id ?? null,
          actions: [...(privileged.actions ?? [])],
          expiresAt: privileged.expires_at,
        }
      : null,
  };
}

async function authenticatedProjection(data: LoginResponse): Promise<AuthProjection> {
  const me = (await fetchMe()) as MeResponse;
  return projectionFromMe(me, data.user.display_name ?? '');
}

export const useAuthStore = create<AuthState>()(
  subscribeWithSelector((set, get) => ({
    token: null,
    authenticated: false,
    user: null,
    sessionAudience: null,
    privilegedAccess: null,
    bootstrapped: false,
    organizationSwitching: false,

    login: async (email, password) => {
      const data = await authPost('/api/v1/auth/login', {
        email,
        password,
      });
      set({ token: null, authenticated: false, user: null, sessionAudience: null, privilegedAccess: null, organizationSwitching: false });
      resetAuthScopedClientState();
      const projection = await authenticatedProjection(data as LoginResponse);
      set({ token: null, authenticated: true, ...projection, bootstrapped: true, organizationSwitching: false });
    },

    signup: async (email, password, username) => {
      const data = (await authPost('/api/v1/auth/register', {
        email,
        username,
        password,
      })) as LoginResponse;
      set({ token: null, authenticated: false, user: null, sessionAudience: null, privilegedAccess: null, organizationSwitching: false });
      resetAuthScopedClientState();
      const me = (await fetchMe()) as MeResponse;
      const projection = projectionFromMe(
        me,
        me.display_name ?? data.user.display_name ?? username,
      );
      set({ token: null, authenticated: true, ...projection, bootstrapped: true, organizationSwitching: false });
    },

    logout: async () => {
      try {
        await authPost('/api/v1/auth/logout', null);
      } catch {
        // Best-effort: an expired cookie must still clear local state.
      }
      set({ token: null, authenticated: false, user: null, sessionAudience: null, privilegedAccess: null, organizationSwitching: false });
      resetAuthScopedClientState();
    },

    deleteAccount: async (email) => {
      if (!get().authenticated) throw new AuthApiError(401, 'Not authenticated');
      await authPost('/api/v1/auth/delete-account', { email });
      set({ token: null, authenticated: false, user: null, sessionAudience: null, privilegedAccess: null, organizationSwitching: false });
      resetAuthScopedClientState();
    },

    cancelAccountDeletion: async (email, password) => {
      await authPost('/api/v1/auth/cancel-delete-account', { email, password });
    },

    switchOrganization: async (organizationId) => {
      if (!get().authenticated || !get().user) {
        throw new AuthApiError(401, 'Not authenticated');
      }
      // Unmount every organization-scoped surface before starting the Session
      // rotation. Query notifications are batched, so clearing the cache and
      // publishing the new tenant id alone can otherwise produce one React
      // frame with the new organization label and the old organization's rows.
      set({ organizationSwitching: true });
      try {
        const switched = (await authPost('/api/v1/organizations/active', {
          organization_id: organizationId,
        })) as OrganizationSwitchResponse;
        // The server has already rotated the HttpOnly Session and generation.
        // Purge old-org data while the routed surfaces remain unmounted, then
        // publish the new tenant and remount against an empty query cache.
        resetOrganizationScopedClientState();
        set((state) => ({
          user: state.user
            ? { ...state.user, tenant_id: switched.organization_id }
            : null,
          sessionAudience: 'web',
          privilegedAccess: null,
          organizationSwitching: false,
        }));
      } catch (error) {
        // A rejected switch leaves the original server Session authoritative;
        // its still-mounted cache was never cleared and can safely reappear.
        set({ organizationSwitching: false });
        throw error;
      }
    },

    exitPrivilegedAccess: async () => {
      const active = get().privilegedAccess;
      if (!active) return;
      try {
        await authPost(
          `/api/v1/auth/privileged-access/organizations/${encodeURIComponent(active.organizationId)}`
          + `/requests/${encodeURIComponent(active.requestId)}/revoke`,
          null,
        );
      } catch {
        // Remote revocation or natural expiry may win the race. The status
        // probe clears only the stale support cookie; /me then restores the
        // still-live parent Web Session.
        await sessionFetch(`${API_BASE}/api/v1/auth/privileged-access/status`, {
          credentials: 'include',
        });
      }
      resetOrganizationScopedClientState();
      const projection = projectionFromMe(await fetchMe());
      set({ token: null, authenticated: true, ...projection, organizationSwitching: false });
    },

    refreshPrivilegedAccess: async () => {
      try {
        const response = await sessionFetch(
          `${API_BASE}/api/v1/auth/privileged-access/status`,
          { credentials: 'include' },
        );
        if (!response.ok) return;
        const status = (await response.json()) as {
          active?: boolean;
          request_id?: string;
        };
        const current = get().privilegedAccess;
        const changed = status.active
          ? !current || current.requestId !== status.request_id
          : current !== null;
        if (!changed) return;
        resetOrganizationScopedClientState();
        const projection = projectionFromMe(await fetchMe());
        set({ token: null, authenticated: true, ...projection, organizationSwitching: false });
      } catch {
        // A transient status failure never broadens or clears local authority.
      }
    },

    handle401: () => {
      set({ token: null, authenticated: false, user: null, sessionAudience: null, privilegedAccess: null, organizationSwitching: false });
      resetAuthScopedClientState();
    },

    bootstrap: async (extensionExchangeCode) => {
      try {
        if (extensionExchangeCode) {
          await authPost('/api/v1/auth/extension/exchange', {
            code: extensionExchangeCode,
          });
        }
        const me = (await fetchMe()) as MeResponse;
        const projection = projectionFromMe(me);
        const previous = get();
        if (previous.user && (
          previous.user.user_id !== projection.user.user_id
          || previous.user.tenant_id !== projection.user.tenant_id
          || previous.sessionAudience !== projection.sessionAudience
        )) {
          // Extension exchanges can switch accounts without visiting login or
          // logout in this frame. Clear the previous namespace before publishing
          // the new identity; renewing the same identity must preserve live Chat.
          resetAuthScopedClientState();
        }
        set({
          token: null,
          authenticated: true,
          ...projection,
          bootstrapped: true,
          organizationSwitching: false,
        });
        // The authenticated projection contains the account namespace needed
        // to read the tab-scoped recent Chat hint. Warm its bounded tail while
        // React is still loading the route chunks, then ChatPage can paint from
        // TanStack Query immediately and reconcile with the server normally.
        void warmRecentChatHistory(projection.user).catch(() => undefined);
      } catch (err) {
        if (err instanceof AuthApiError && err.status === 401) {
          set({
            token: null,
            authenticated: false,
            user: null,
            sessionAudience: null,
            privilegedAccess: null,
            bootstrapped: true,
            organizationSwitching: false,
          });
          resetAuthScopedClientState();
        } else {
          // Network / transient error: preserve the last known projection.
          set({ bootstrapped: true });
        }
      }
    },
  })),
);

async function fetchMe(): Promise<MeResponse> {
  const res = await sessionFetch(`${API_BASE}/api/v1/auth/me`, {
    credentials: 'include',
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const j = (await res.json()) as { detail?: string };
      if (j.detail) detail = j.detail;
    } catch {
      /* ignore body parse errors */
    }
    throw new AuthApiError(res.status, detail);
  }
  return (await res.json()) as MeResponse;
}

/**
 * Standalone helpers for the password-reset flow. Pages call these
 * directly — they don't mutate auth state, so they don't belong on the
 * store. Errors throw `AuthApiError` so the page can localize them.
 */
export async function requestPasswordReset(email: string): Promise<void> {
  await authPost('/api/v1/auth/password-reset/request', { email });
}

export async function confirmPasswordReset(
  resetToken: string,
  newPassword: string,
): Promise<void> {
  await authPost('/api/v1/auth/password-reset/confirm', {
    reset_token: resetToken,
    new_password: newPassword,
  });
}
