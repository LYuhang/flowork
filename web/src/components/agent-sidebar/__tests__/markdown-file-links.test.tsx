import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { Markdown } from '../Markdown';

describe('Agent file links', () => {
  it('opens a sandbox download link in Preview instead of stripping its address', () => {
    const open = vi.fn();
    render(<Markdown onOpenFilePreview={open}>
      {'[Download](sandbox:/chats/chat-1/My%20Report.pptx)'}
    </Markdown>);
    const link = screen.getByRole('link', { name: 'Download' });
    expect(link).toHaveAttribute('href', '/chats/chat-1/My%20Report.pptx');
    fireEvent.click(link);
    expect(open).toHaveBeenCalledWith('/chats/chat-1/My Report.pptx');
    expect(link).not.toHaveAttribute('target');
  });

  it('does not expose private sandbox links in public conversations', () => {
    render(<Markdown publicView>{'[Report](sandbox:/data/report.docx)'}</Markdown>);
    expect(screen.getByText('Report')).toBeVisible();
    expect(screen.queryByRole('link')).toBeNull();
  });

  it('opens the virtual workspace URL used by the DOCX example', () => {
    const open = vi.fn();
    render(<Markdown onOpenFilePreview={open}>
      {'[DOCX](<https://file+workspace/chats/chat-1/简报.docx>)'}
    </Markdown>);
    fireEvent.click(screen.getByRole('link', { name: 'DOCX' }));
    expect(open).toHaveBeenCalledWith('/chats/chat-1/简报.docx');
  });

  it('retains protocol filtering for unsafe links and sandbox authorities', () => {
    render(<Markdown onOpenFilePreview={vi.fn()}>
      {'[Unsafe](javascript:alert) [Remote](sandbox://example.com/data/file.pdf)'}
    </Markdown>);
    for (const name of ['Unsafe', 'Remote']) {
      const link = screen.getByText(name).closest('a');
      expect(link).toHaveAttribute('href', '');
      expect(link).not.toHaveAttribute('data-action');
    }
  });
});
