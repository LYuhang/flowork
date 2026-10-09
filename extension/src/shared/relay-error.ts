import { encode } from './envelope';

/** Preserve the transport envelope and CDP identity on every failure path. */
export function relayError(env: { id?: unknown; channel?: unknown; transport?: unknown; data?: unknown }, message: string): string {
  const request = (env.data as { request?: { id?: unknown; sessionId?: unknown } } | undefined)?.request;
  return encode('playwright_relay', {
    id: String(env.id || ''), channel: String(env.channel || ''), transport: String(env.transport || ''),
    data: { action: 'message', message: {
      ...(Number.isInteger(request?.id) ? { id: request!.id } : {}),
      ...(typeof request?.sessionId === 'string' ? { sessionId: request.sessionId } : {}),
      error: { code: -32603, message },
    } },
  });
}
