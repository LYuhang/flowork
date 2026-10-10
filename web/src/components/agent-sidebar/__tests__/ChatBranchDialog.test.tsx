import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router';
import '@/lib/i18n';
import i18n from '@/lib/i18n';
import { useUIStore } from '@/stores/ui';
import { ChatBranchDialog } from '../ChatBranchDialog';
const mocks = vi.hoisted(() => ({ preview: vi.fn(), create: vi.fn(), workspace: vi.fn() }));
vi.mock('@/lib/api/chat-branches', () => ({ previewChatBranch: mocks.preview, createChatBranch: mocks.create }));
vi.mock('@/lib/api/queries/chats', () => ({ fetchChatWorkspace: mocks.workspace }));
function mount() {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter><Routes>
      <Route path="/" element={<ChatBranchDialog chatId="source" turnId="selected" excerpt="Selected response" />} />
      <Route path="/chat/open/:id" element={<p>Opened branch</p>} />
    </Routes></MemoryRouter>
  </QueryClientProvider>);
}
beforeEach(async () => {
  vi.clearAllMocks();
  await i18n.changeLanguage('en');
  mocks.preview.mockResolvedValue({ project_id: 'project', through_turn_id: 'selected' });
});
afterEach(cleanup);
it('previews without mutation, then creates once at the selected turn', async () => {
  mocks.create.mockResolvedValue({ chat_id: 'new-chat' });
  mount();
  expect(mocks.preview).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Branch from this turn' }));
  await screen.findByText('project');
  expect(mocks.create).not.toHaveBeenCalled();
  expect(screen.getByText(/share the project’s current files/)).toBeInTheDocument();
  fireEvent.change(screen.getByRole('textbox', { name: 'New conversation name' }), { target: { value: 'Exploration' } });
  const create = screen.getByRole('button', { name: 'Create branch' });
  fireEvent.click(create); fireEvent.click(create);
  await screen.findByText('Opened branch');
  expect(mocks.create).toHaveBeenCalledTimes(1);
  expect(mocks.create).toHaveBeenCalledWith('source', { chat_id: expect.any(String), turn_id: 'selected', name: 'Exploration' });
});
it('checks a lost response by the original ID without repeating creation', async () => {
  mocks.create.mockRejectedValue(new Error('connection lost'));
  mocks.workspace.mockResolvedValue({ project_id: 'project' });
  mount();
  fireEvent.click(screen.getByRole('button', { name: 'Branch from this turn' }));
  await screen.findByText('project');
  fireEvent.change(screen.getByRole('textbox', { name: 'New conversation name' }), { target: { value: 'Branch' } });
  fireEvent.click(screen.getByRole('button', { name: 'Create branch' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Check created conversation' }));
  await screen.findByText('Opened branch');
  expect(mocks.workspace).toHaveBeenCalledWith(mocks.create.mock.calls[0][1].chat_id);
  expect(mocks.create).toHaveBeenCalledTimes(1);
});
it('does not offer creation when the turn is ineligible', async () => {
  mocks.preview.mockRejectedValue(new Error('turn running'));
  mount();
  fireEvent.click(screen.getByRole('button', { name: 'Branch from this turn' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('cannot be branched');
  expect(screen.queryByRole('button', { name: 'Create branch' })).not.toBeInTheDocument();
  expect(mocks.create).not.toHaveBeenCalled();
});

it('selects a canvas branch in its current workflow instead of opening general chat', async () => {
  mocks.preview.mockResolvedValue({ project_id: 'project', scope_id: 'workflow-id' });
  mocks.create.mockResolvedValue({ chat_id: 'canvas-branch' });
  mount();
  fireEvent.click(screen.getByRole('button', { name: 'Branch from this turn' }));
  await screen.findByText('project');
  fireEvent.change(screen.getByRole('textbox', { name: 'New conversation name' }), { target: { value: 'Alternative' } });
  fireEvent.click(screen.getByRole('button', { name: 'Create branch' }));
  await screen.findByRole('button', { name: 'Branch from this turn' });
  await waitFor(() => expect(useUIStore.getState().activeChatIds.chat).toBe('canvas-branch'));
  expect(screen.queryByText('Opened branch')).not.toBeInTheDocument();
});
