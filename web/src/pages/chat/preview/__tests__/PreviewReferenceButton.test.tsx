import { http, HttpResponse } from 'msw';
import { server } from '@/__tests__/msw-handlers';
import '@/lib/i18n';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { describe, expect, it } from 'vitest';
import { PreviewOriginProvider } from '@/lib/preview/PreviewOriginProvider';
import { PreviewReferenceButton } from '../PreviewReferenceButton';
import { fetchContextDraft, mutateContextDraft } from '@/lib/api/context-draft';
import type { ChatAttachment } from '@/components/agent-sidebar/chat-attachments';

const build = (): ChatAttachment => ({ schema_version: 1, id: crypto.randomUUID(), type: 'resource', label: 'Web research',
  resource: { kind: 'web', url: 'https://example.com/research' } });
function view(origin: string | null) {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <PreviewOriginProvider origin={origin ? { chatId: origin } : null}>
      <PreviewReferenceButton build={build} />
    </PreviewOriginProvider>
  </QueryClientProvider>);
}
describe('Preview durable references', () => {
  it('retries one temporary authorization failure before enabling references', async () => {
    let calls = 0;
    server.use(http.get('*/api/v1/chats/starting-chat/draft', () => {
      calls++;
      return calls === 1 ? HttpResponse.json({ detail: { code: 'authorization_unavailable' } }, { status: 503 })
        : HttpResponse.json({ chat_id: 'starting-chat', version: 0, generation: 0, text: '', attachments: [] });
    }));
    view('starting-chat');
    await waitFor(() => expect(screen.getByRole('button')).toBeEnabled(), { timeout: 2500 });
    expect(calls).toBe(2);
  });

  it('disables orphan previews instead of guessing a destination', () => {
    view(null);
    expect(screen.getByRole('button')).toBeDisabled();
    expect(screen.getByRole('button')).toHaveAccessibleName('Open this preview from a conversation to add a reference.');
  });
  it('appends to the bound source and keeps both conversations’ text unchanged', async () => {
    await mutateContextDraft('original-chat', { kind: 'text', operation_id: 'original-text', text: 'Keep this question', previous_text: '' });
    await mutateContextDraft('other-chat', { kind: 'text', operation_id: 'other-text', text: 'Another question', previous_text: '' });
    view('original-chat');
    const button = screen.getByRole('button');
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    await waitFor(async () => expect((await fetchContextDraft('original-chat')).attachments).toHaveLength(1));
    expect((await fetchContextDraft('original-chat')).text).toBe('Keep this question');
    const other = await fetchContextDraft('other-chat');
    expect(other.text).toBe('Another question');
    expect(other.attachments).toHaveLength(0);
  });
});
