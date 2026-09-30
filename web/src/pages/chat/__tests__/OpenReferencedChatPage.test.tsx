import '@/lib/i18n';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Routes, Route, useLocation } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { OpenReferencedChatPage } from '../OpenReferencedChatPage';
import { useUIStore } from '@/stores/ui';
const fetchWorkspace = vi.hoisted(() => vi.fn());
vi.mock('@/lib/api/queries/chats', () => ({ fetchChatWorkspace: fetchWorkspace }));
function Destination() {
  const location = useLocation();
  return <div>Returned to chat<span data-testid="destination-search">{location.search}</span></div>;
}
function open(client = new QueryClient({ defaultOptions: { queries: { retry: false } } }), search = '') {
  return render(<QueryClientProvider client={client}>
    <MemoryRouter initialEntries={['/chat/open/original' + search]}><Routes>
      <Route path="/chat/open/:chatId" element={<OpenReferencedChatPage />} />
      <Route path="/chat" element={<Destination />} />
    </Routes></MemoryRouter>
  </QueryClientProvider>);
}
describe('authorized conversation deep links', () => {
  beforeEach(() => { fetchWorkspace.mockReset(); useUIStore.getState().setActiveChatId('chat', 'existing'); });
  it('selects the authorized source project and chat before navigation', async () => {
    fetchWorkspace.mockResolvedValue({ chat_id: 'original', project_id: 'source-project' });
    const client = new QueryClient();
    open(client);
    await screen.findByText('Returned to chat');
    expect(fetchWorkspace).toHaveBeenCalledWith('original');
    expect(useUIStore.getState().activeChatIds.chat).toBe('original');
    expect(useUIStore.getState().activeProjectId).toBe('source-project');
    expect(screen.getByTestId('destination-search')).toHaveTextContent('?resumeChat=original');
    expect(client.getQueryData(['chat-workspace', 'original'])).toEqual({ chat_id: 'original', project_id: 'source-project' });
  });
  it('preserves a specific message target through authorization', async () => {
    fetchWorkspace.mockResolvedValue({ chat_id: 'original', project_id: 'source-project' });
    open(new QueryClient(), '?focusMessage=message-42');
    await screen.findByText('Returned to chat');
    expect(screen.getByTestId('destination-search')).toHaveTextContent('?resumeChat=original&focusMessage=message-42');
  });
  it('preserves a task target through authorization', async () => {
    fetchWorkspace.mockResolvedValue({chat_id:'original',project_id:'source-project'});
    open(new QueryClient(), '?focusJob=job-42');
    await screen.findByText('Returned to chat');
    expect(screen.getByTestId('destination-search')).toHaveTextContent('?resumeChat=original&focusJob=job-42');
  });
  it('keeps the existing selection when the original chat is unavailable', async () => {
    fetchWorkspace.mockRejectedValue(new Error('forbidden'));
    open();
    await screen.findByText('The source conversation is unavailable or you no longer have access.');
    expect(useUIStore.getState().activeChatIds.chat).toBe('existing');
    expect(screen.queryByText('Returned to chat')).not.toBeInTheDocument();
  });
});
