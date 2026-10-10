import { expect, it } from 'vitest';
import { http, HttpResponse } from 'msw';
import { server } from '@/__tests__/msw-handlers';
import { fetchPendingChatActions } from '../chat-engagement';

it('uses the shared API prefix once and safely encodes the pagination cursor', async () => {
  let seen: URL | undefined;
  server.use(http.get('*/api/v1/chat-scopes/scope/attention', ({ request }) => {
    seen = new URL(request.url);
    return HttpResponse.json({ items: [], next_cursor: null });
  }));
  expect(await fetchPendingChatActions('scope', 'request&next=other')).toEqual({ items: [], next_cursor: null });
  expect(seen?.pathname).toBe('/api/v1/chat-scopes/scope/attention');
  expect(seen?.searchParams.get('after_id')).toBe('request&next=other');
  expect(seen?.searchParams.get('limit')).toBe('30');
});
