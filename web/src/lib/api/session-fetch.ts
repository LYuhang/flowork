import { resolveApiUrl } from '@/lib/base-path';
import {
  requestWebAuthnStepUp,
  responseRequiresWebAuthnStepUp,
} from '@/lib/auth/step-up-broker';

const CSRF_HEADER = 'X-CSRF-Token';
const UNSAFE_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);
const CSRF_COOKIE_NAMES = [
  '__Host-vibecanvas-support-csrf',
  'vibecanvas-support-csrf',
  '__Host-vibecanvas-extension-csrf',
  'vibecanvas-extension-csrf',
  '__Host-vibecanvas-web-csrf',
  'vibecanvas-web-csrf',
] as const;

let nativeFetch: typeof globalThis.fetch | null = null;
let installed = false;

function cookieValue(name: string): string | null {
  if (typeof document === 'undefined') return null;
  for (const item of document.cookie.split(';')) {
    const separator = item.indexOf('=');
    if (separator < 0) continue;
    if (item.slice(0, separator).trim() !== name) continue;
    try {
      return decodeURIComponent(item.slice(separator + 1));
    } catch {
      return item.slice(separator + 1);
    }
  }
  return null;
}

export function readSessionCsrfToken(): string | null {
  for (const name of CSRF_COOKIE_NAMES) {
    const value = cookieValue(name);
    if (value) return value;
  }
  return null;
}

function isPlatformRequest(url: URL): boolean {
  if (typeof window === 'undefined') return false;
  const apiRoot = new URL(resolveApiUrl('/api/'), window.location.href);
  return url.origin === apiRoot.origin && url.pathname.startsWith(apiRoot.pathname);
}

/**
 * Fetch only adds ambient browser credentials to Flowork API requests.
 * Third-party URLs retain native fetch semantics and never receive CSRF data.
 */
export async function sessionFetch(
  input: RequestInfo | URL,
  init?: RequestInit,
): Promise<Response> {
  const request = new Request(input, init);
  const url = new URL(request.url, typeof window === 'undefined' ? 'http://localhost' : window.location.href);
  const delegate = nativeFetch ?? globalThis.fetch.bind(globalThis);
  if (!isPlatformRequest(url)) return delegate(request);

  const headers = new Headers(request.headers);
  const method = request.method.toUpperCase();
  if (UNSAFE_METHODS.has(method)) {
    const csrf = readSessionCsrfToken();
    if (csrf) headers.set(CSRF_HEADER, csrf);
  }
  if (typeof window !== 'undefined' && window.parent !== window) {
    headers.set('X-VibeCanvas-Session-Audience', 'extension');
  }
  const authenticatedRequest = new Request(request, {
    headers,
    credentials: 'include',
  });
  let retryRequest: Request | null = null;
  if (UNSAFE_METHODS.has(method)) {
    try {
      retryRequest = authenticatedRequest.clone();
    } catch {
      // Streaming request bodies cannot be replayed. The original response is
      // returned so callers can ask the user to retry explicitly.
    }
  }
  const response = await delegate(authenticatedRequest);
  if (
    !retryRequest
    || url.pathname.endsWith('/registration/options')
    || url.pathname.endsWith('/registration/verify')
    || url.pathname.endsWith('/authentication/options')
    || url.pathname.endsWith('/authentication/verify')
    || !(await responseRequiresWebAuthnStepUp(response))
  ) {
    return response;
  }
  if (!(await requestWebAuthnStepUp())) return response;

  // WebAuthn verification rotates both the HttpOnly Session and the CSRF
  // cookie. Rebuild the retry headers instead of replaying the stale token.
  const retryHeaders = new Headers(retryRequest.headers);
  const refreshedCsrf = readSessionCsrfToken();
  if (refreshedCsrf) retryHeaders.set(CSRF_HEADER, refreshedCsrf);
  return delegate(new Request(retryRequest, {
    headers: retryHeaders,
    credentials: 'include',
  }));
}

/** Install once before React, query clients, and SSE transports issue I/O. */
export function installSessionFetch(): void {
  if (installed || typeof window === 'undefined') return;
  installed = true;
  nativeFetch = globalThis.fetch.bind(globalThis);
  globalThis.fetch = sessionFetch;
}
