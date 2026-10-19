import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { Markdown } from '../Markdown';
import { ChatRenderProvider } from '../chat-render-context';

describe('loopback message links', () => {
  it('opens the Preview callback without navigating to the user machine', () => {
    const open = vi.fn();
    render(<ChatRenderProvider value={{ chatId: 'chat-a', surface: 'chat', onOpenInteractivePreview: open }}>
      <Markdown>{'[Live page](http://localhost:3000/demo?q=2)'}</Markdown>
    </ChatRenderProvider>);
    fireEvent.click(screen.getByText('Live page'));
    expect(open).toHaveBeenCalledWith(expect.objectContaining({ component_type: 'url_preview', props: { url: 'http://localhost:3000/demo?q=2' } }));
  });
  it('recognizes a bare localhost URL in an Agent message', () => {
    const open = vi.fn();
    render(<ChatRenderProvider value={{ chatId: 'chat-a', surface: 'chat', onOpenInteractivePreview: open }}><Markdown>{'Open http://localhost:3000/demo to see it.'}</Markdown></ChatRenderProvider>);
    fireEvent.click(screen.getByRole('link'));
    expect(open).toHaveBeenCalledTimes(1);
  });
  it('does not activate private service links in public snapshots', () => {
    render(<Markdown publicView>{'[Private](http://localhost:3000)'}</Markdown>);
    expect(screen.queryByRole('link')).toBeNull();
  });
});
