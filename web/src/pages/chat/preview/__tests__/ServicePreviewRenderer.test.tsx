import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { PreviewOriginContext } from '@/lib/preview/context-origin';
import { UrlPreviewRenderer } from '../UrlPreviewRenderer';
const resolve = vi.hoisted(() => vi.fn());
const stop = vi.hoisted(() => vi.fn());
vi.mock('@/lib/api/previews', () => ({ resolveServicePreview: resolve, stopPreviewService: stop }));

describe('service URL preview shared by render cards and message links', () => {
  beforeEach(() => { resolve.mockReset(); stop.mockReset(); });
  it('uses the originating chat and loads the forwarding URL, never raw localhost', async () => {
    resolve.mockResolvedValue({ url: 'https://p3000-sqa.preview.test/open', expires_in: 3600 });
    render(<PreviewOriginContext.Provider value={{ chatId: 'origin-chat' }}><UrlPreviewRenderer url="http://localhost:3000/demo" title="Demo" /></PreviewOriginContext.Provider>);
    await waitFor(() => expect(screen.getByTitle('Demo')).toHaveAttribute('src', 'https://p3000-sqa.preview.test/open'));
    expect(resolve).toHaveBeenCalledWith('origin-chat', 'http://localhost:3000/demo', expect.any(AbortSignal));
    expect(screen.getByTitle('Demo')).toHaveAttribute('sandbox', 'allow-scripts allow-same-origin allow-forms allow-downloads');
  });
  it('shows deployment errors and reconnects only when the user asks', async () => {
    resolve.mockRejectedValue(new Error('service_preview_domain_not_configured'));
    render(<PreviewOriginContext.Provider value={{ chatId: 'origin-chat' }}><UrlPreviewRenderer url="http://127.0.0.1:3000" title="Demo" /></PreviewOriginContext.Provider>);
    await screen.findByText('Service preview domain is not configured yet.');
    expect(resolve).toHaveBeenCalledTimes(1);
    expect(screen.queryByTitle('Demo')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(resolve).toHaveBeenCalledTimes(2));
  });
  it('does not resolve a private sandbox in a contextless preview', async () => {
    render(<UrlPreviewRenderer url="http://localhost:3000" title="Demo" />);
    await screen.findByText('Open this service from its original conversation.');
    expect(resolve).not.toHaveBeenCalled();
  });
  it('stops the resolved service and removes its iframe only after confirmation', async () => {
    const service = { runtime_id: 'runtime-a', expected: [{ port: 3000, pid: 12, start: '100', chat_id: 'origin-chat' }] };
    resolve.mockResolvedValue({ url: 'https://example.test/preview/access/3000/', service });
    stop.mockRejectedValueOnce(new Error('service_changed_reopen_preview')).mockResolvedValueOnce({ status: 'stopped' });
    render(<PreviewOriginContext.Provider value={{ chatId: 'origin-chat' }}><UrlPreviewRenderer url="http://localhost:3000" title="Demo" /></PreviewOriginContext.Provider>);
    const button = await screen.findByRole('button', { name: 'Stop service' });
    fireEvent.click(button);
    await screen.findByRole('alert');
    expect(screen.getByTitle('Demo')).toBeInTheDocument();
    expect(stop).toHaveBeenCalledWith('origin-chat', 'http://localhost:3000', service);
    fireEvent.click(button);
    await screen.findByText('Service stopped.');
    expect(screen.queryByTitle('Demo')).toBeNull();
  });

});
