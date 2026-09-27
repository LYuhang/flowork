import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { DebugMessageContent } from '../DebugMessageContent';
import { readVfs } from '@/lib/api/vfs';

vi.mock('@/lib/api/vfs', () => ({ readVfs: vi.fn() }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_key: string, fallback: string) => fallback }) }));

describe('Debug message expansion', () => {
  beforeEach(() => vi.clearAllMocks());
  it('updates a growing current-turn message without remounting', () => {
    const { rerender } = render(<DebugMessageContent workspaceId="chat" content="partial" />);
    rerender(<DebugMessageContent workspaceId="chat" content="partial then completed" />);
    expect(screen.getByText('partial then completed')).toBeInTheDocument();
    expect(readVfs).not.toHaveBeenCalled();
  });
  it('renders only a prefix and loads each message independently on demand', async () => {
    const full = 'A'.repeat(1200) + 'EXPANDED-MARKER' + 'Z'.repeat(10000);
    vi.mocked(readVfs).mockResolvedValue({ content: full, truncated: false } as Awaited<ReturnType<typeof readVfs>>);
    const { container } = render(<div>
      <DebugMessageContent workspaceId="chat" content={full.slice(0, 1200)} contentRef="/logs/.debug/s.messages/0001" partCount={1} totalChars={full.length} />
      <DebugMessageContent workspaceId="chat" content="Next round remains visible" />
    </div>);
    expect(readVfs).not.toHaveBeenCalled();
    expect(container.textContent).not.toContain('EXPANDED-MARKER');
    fireEvent.click(screen.getByRole('button', { name: 'Show more' }));
    await waitFor(() => expect(container.textContent).toContain('EXPANDED-MARKER'));
    expect(readVfs).toHaveBeenCalledWith({ wf_id: 'chat', path: '/logs/.debug/s.messages/0001/0.txt' });
    expect(screen.getByText('Next round remains visible')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Collapse' }));
    expect(container.textContent).not.toContain('EXPANDED-MARKER');
    fireEvent.click(screen.getByRole('button', { name: 'Show more' }));
    expect(readVfs).toHaveBeenCalledTimes(1);
  });
  it('retains the preview on failure and retries the same chunk', async () => {
    vi.mocked(readVfs).mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({ content: 'prefix then full', truncated: false } as Awaited<ReturnType<typeof readVfs>>);
    render(<DebugMessageContent workspaceId="chat" content="prefix" contentRef="/logs/.debug/parts" partCount={1} totalChars={16} />);
    fireEvent.click(screen.getByRole('button', { name: 'Show more' }));
    await screen.findByRole('alert');
    expect(screen.getByText('prefix')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry loading' }));
    await screen.findByText('prefix then full');
    expect(vi.mocked(readVfs).mock.calls[0]).toEqual(vi.mocked(readVfs).mock.calls[1]);
  });
});
