/**
 * Shared `errorMessage` helper for mutation toasts.
 *
 * openapi-fetch rejects the raw response body for non-2xx responses —
 * typically a FastAPI `HTTPValidationError` shaped as
 * `{ detail: string | { msg: string }[] }` — which is not an `Error`
 * instance. A naive `String(e)` against that body produces the famously
 * unhelpful `[object Object]`. Centralizing the coercion here keeps every
 * mutation file's `onError` handler one-liner and consistent.
 *
 * Handled shapes (in order):
 *   1. `Error` instance → `e.message`.
 *   2. Object with `detail: string` → that string.
 *   3. Structured detail / validation arrays → readable message or error code.
 *   4. Unknown objects → a safe generic message, never object coercion or a
 *      dump of arbitrary response fields (which may contain private data).
 *
 * Originally inlined in `mutations/workflows.ts`. Extracted as part of
 * CanvasToolbar introduces the second consumer in
 * `mutations/workflow-ops.ts`.
 */
import i18n from '@/lib/i18n';

function readableError(value: unknown, depth = 0): string | undefined {
  if (typeof value === 'string') return value.trim() || undefined;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (value instanceof Error) return value.message.trim() || undefined;
  // Bound traversal even for malformed or circular errors raised locally.
  if (!value || typeof value !== 'object' || depth >= 4) return undefined;
  if (Array.isArray(value)) {
    return value.map((item) => readableError(item, depth + 1)).filter(Boolean).join('; ') || undefined;
  }
  const record = value as Record<string, unknown>;
  const detail = readableError(record.detail, depth + 1);
  if (detail) return detail;
  for (const key of ['message', 'msg']) {
    const message = record[key];
    if (typeof message === 'string' && message.trim()) return message.trim();
  }
  if (typeof record.code === 'string' && record.code.trim()) {
    switch (record.code) {
      case 'invalid_request_origin':
        return i18n.t('api.error.invalidRequestOrigin', 'This page address is not trusted by the server. Ask the administrator to configure its allowed origins, then retry.');
      case 'csrf_validation_failed':
        return i18n.t('api.error.csrfValidationFailed', 'Session verification failed. Keep a copy of your unsaved changes, then sign in again and retry.');
      default:
        return record.code;
    }
  }
  return undefined;
}

export const errorMessage = (e: unknown): string =>
  readableError(e) ?? i18n.t('api.error.unexpected', 'The request failed. Please try again.');

/** A loaded resource that becomes unavailable commonly indicates live revoke. */
export const isAuthorizationChangedError = (e: unknown): boolean => {
  const message = errorMessage(e).toLowerCase();
  return message === 'resource_not_found'
    || message.includes('permission')
    || message.includes('authorization');
};
