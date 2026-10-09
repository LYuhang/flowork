import { expect, it } from 'vitest';
import { relayError } from './relay-error';
it('preserves CDP request and session identities independently of envelope id', () => {
 const result = JSON.parse(relayError({ id: 'outer', channel: 'chat:one', transport: 'socket', data: { request: { id: 42, sessionId: 'cdp-session' } } }, 'session unavailable'));
 expect(result.id).toBe('outer');
 expect(result.data.message).toEqual({ id: 42, sessionId: 'cdp-session', error: { code: -32603, message: 'session unavailable' } });
});
it('initialization errors remain control-plane errors without a fabricated CDP id', () => {
 const result = JSON.parse(relayError({ id: 'init', data: { action: 'initialize' } }, 'no window'));
 expect(result.data.message.id).toBeUndefined();
 expect(result.data.message.error.message).toBe('no window');
});
