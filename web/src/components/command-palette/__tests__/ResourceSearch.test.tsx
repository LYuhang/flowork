import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ResourceSearch } from '../ResourceSearch';
import { searchResourcePage, type ResourceSearchPage } from '@/lib/api/resource-search';
import { useUIStore } from '@/stores/ui';
import i18n from '@/lib/i18n';

vi.mock('@/lib/api/resource-search', () => ({ searchResourcePage: vi.fn() }));
const search = vi.mocked(searchResourcePage);
function Location() { return <output aria-label="Location">{useLocation().pathname}</output>; }
function mount() {
  const close = vi.fn();
  const view = render(<MemoryRouter initialEntries={['/workspace']}><ResourceSearch onClose={close} /><Location /></MemoryRouter>);
  return { ...view, close };
}
async function submit() {
  await userEvent.type(screen.getByRole('textbox', { name: 'Resource name' }), 'needle');
  await userEvent.click(screen.getByRole('button', { name: 'Search' }));
}
beforeEach(async () => { search.mockReset(); await i18n.changeLanguage('en'); });

describe('resource title search', () => {
  it('continues past an empty scan page and opens the matching conversation', async () => {
    search.mockResolvedValueOnce({ items: [], scanned: 100, next_cursor: 'cursor-100' })
      .mockResolvedValueOnce({ items: [{ id: 'older-chat', title: 'needle older' }], scanned: 3, next_cursor: null });
    const { close } = mount();
    await submit();
    expect(screen.queryByText('Search complete.')).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: 'Continue searching' }));
    expect(search.mock.calls[1].slice(0, 3)).toEqual(['chats', 'needle', 'cursor-100']);
    await userEvent.click(await screen.findByRole('button', { name: /needle older/ }));
    expect(screen.getByLabelText('Location')).toHaveTextContent('/chat/open/older-chat');
    expect(close).toHaveBeenCalledOnce();
  });

  it('cancels on category changes and ignores a late response', async () => {
    let resolve!: (page: ResourceSearchPage) => void;
    search.mockImplementationOnce(() => new Promise(done => { resolve = done; }));
    mount();
    await submit();
    const signal = search.mock.calls[0][3];
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Category' }), 'tasks');
    expect(signal.aborted).toBe(true);
    await act(async () => resolve({ items: [{ id: 'late', title: 'late result' }], scanned: 1, next_cursor: null }));
    expect(screen.queryByText('late result')).toBeNull();
    expect(screen.getByRole('textbox')).toHaveValue('');
    expect(search).toHaveBeenCalledOnce();
  });

  it('stops a request, preserves prior matches, and never retries automatically', async () => {
    search.mockResolvedValueOnce({ items: [{ id: 'first', title: 'needle first' }], scanned: 100, next_cursor: 'next' })
      .mockImplementationOnce(() => new Promise(() => {}));
    const view = mount();
    await submit();
    await userEvent.click(screen.getByRole('button', { name: 'Continue searching' }));
    await userEvent.click(screen.getByRole('button', { name: 'Stop' }));
    expect(search.mock.calls[1][3].aborted).toBe(true);
    expect(screen.getByRole('button', { name: /needle first/ })).toBeVisible();
    expect(screen.getByText('Stopped. Results may be incomplete.')).toBeVisible();
    expect(search).toHaveBeenCalledTimes(2);
    view.unmount();
  });

  it('keeps partial results on failure and retries only the requested page on click', async () => {
    search.mockResolvedValueOnce({ items: [{ id: 'first', title: 'needle first' }], scanned: 100, next_cursor: 'next' })
      .mockRejectedValueOnce(new Error('HTTP 503'))
      .mockResolvedValueOnce({ items: [{ id: 'first', title: 'needle first' }, { id: 'second', title: 'needle second' }], scanned: 2, next_cursor: null });
    mount(); await submit();
    await userEvent.click(screen.getByRole('button', { name: 'Continue searching' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('HTTP 503');
    expect(search).toHaveBeenCalledTimes(2);
    await userEvent.click(screen.getByRole('button', { name: 'Retry this page' }));
    await waitFor(() => expect(screen.getByText('Search complete.')).toBeVisible());
    expect(search.mock.calls[2][2]).toBe('next');
    expect(screen.getAllByRole('button', { name: /needle first/ })).toHaveLength(1);
  });

  it.each(['tasks', 'deployments', 'projects'] as const)('opens a %s result without sending a message', async kind => {
    search.mockResolvedValue({ items: [{ id: 'resource-one', title: 'needle' }], scanned: 1, next_cursor: null });
    const { close } = mount();
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Category' }), kind);
    await submit();
    await userEvent.click(await screen.findByRole('button', { name: /needle/ }));
    expect(screen.getByLabelText('Location')).toHaveTextContent(kind === 'projects' ? '/chat' : `/${kind}/resource-one`);
    expect(close).toHaveBeenCalledOnce();
    if (kind === 'projects') {
      expect(useUIStore.getState().activeProjectId).toBe('resource-one');
      expect(useUIStore.getState().activeChatIds.chat).toBeNull();
      expect(useUIStore.getState().chatEntryIntent).toBe('default');
    }
  });
});
