import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  fetchPreviewRendition,
  resolvePreview,
  resolvePreviewResourceUrl,
} from '@/lib/api/previews';
import type { FileRefV1, PreviewResourceSessionV1 } from '@/lib/preview/protocol';

vi.mock('@/stores/auth', () => ({
  useAuthStore: {
    getState: () => ({ token: 'preview-token', handle401: vi.fn() }),
  },
}));

afterEach(() => vi.restoreAllMocks());

describe('resolvePreview citation compatibility', () => {
  const ref: FileRefV1 = { schemaVersion: 1, scope: 'chat', chatId: 'chat-1', path: '/data/README.md:24:2' };
  const missing = () => Response.json({ detail: 'preview_file_not_found' }, { status: 404 });

  it('retries a missing legacy citation once with the same scope and actual text path', async () => {
    const canonical = { ...ref, path: '/data/README.md' };
    const descriptor = { fileRef: canonical, renderer: 'markdown' };
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(missing())
      .mockResolvedValueOnce(Response.json(descriptor));
    const controller = new AbortController();
    expect(await resolvePreview(ref, controller.signal)).toEqual(descriptor);
    expect(fetchSpy).toHaveBeenCalledTimes(2);
    expect(fetchSpy.mock.calls.map(([, init]) => JSON.parse(String(init?.body)).fileRef)).toEqual([ref, canonical]);
    for (const [, init] of fetchSpy.mock.calls) {
      expect(init?.signal).toBe(controller.signal);
      expect(new Headers(init?.headers).get('Authorization')).toBe('Bearer preview-token');
    }
  });

  it('prefers an existing literal colon filename without extra requests', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(Response.json({ fileRef: ref }));
    expect(await resolvePreview(ref)).toEqual({ fileRef: ref });
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  it.each([401, 403, 404, 500])('does not reinterpret unrelated HTTP %i failures', async (status) => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(Response.json({ detail: 'unavailable' }, { status }));
    await expect(resolvePreview(ref)).rejects.toThrow('unavailable');
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  it('propagates a missing fallback without looping', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => missing());
    await expect(resolvePreview(ref)).rejects.toThrow('preview_file_not_found');
    expect(fetchSpy).toHaveBeenCalledTimes(2);
  });

  it('does not retry after cancellation', async () => {
    const controller = new AbortController();
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => {
      controller.abort();
      return missing();
    });
    await expect(resolvePreview(ref, controller.signal)).rejects.toThrow('preview_file_not_found');
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });
});

const session: PreviewResourceSessionV1 = {
  schemaVersion: 1,
  resourceMounts: [
    { pathPrefix: '/', rootUrl: 'https://api.test/resources/workspace/' },
    { pathPrefix: '/mount/', rootUrl: 'https://api.test/resources/mount/' },
  ],
  baseUrl: 'https://api.test/resources/workspace/data/diagrams/',
  expiresIn: 3600,
};

describe('resolvePreviewResourceUrl', () => {
  it('uses the most specific capability mount', () => {
    expect(resolvePreviewResourceUrl('/data/images/chart.png', session)).toBe(
      'https://api.test/resources/workspace/data/images/chart.png',
    );
    expect(resolvePreviewResourceUrl('/mount/brand/logo.svg', session)).toBe(
      'https://api.test/resources/mount/brand/logo.svg',
    );
  });

  it('rejects external, relative, and traversal references', () => {
    expect(resolvePreviewResourceUrl('https://example.com/tracker.png', session)).toBeNull();
    expect(resolvePreviewResourceUrl('images/chart.png', session)).toBeNull();
    expect(resolvePreviewResourceUrl('/data/../memory/private.png', session)).toBeNull();
    expect(resolvePreviewResourceUrl('/data\\private.png', session)).toBeNull();
  });
});

describe('fetchPreviewRendition', () => {
  it('loads a private Office rendition with the current Bearer session', async () => {
    const payload = new Uint8Array([0x25, 0x50, 0x44, 0x46]);
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      new Response(payload, {
        status: 200,
        headers: { 'Content-Type': 'application/pdf' },
      }),
    );

    const result = await fetchPreviewRendition(
      '/api/v1/previews/office-rendition?scope=chat&path=%2Fdata%2Fbrief.docx',
    );

    expect(new Uint8Array(result)).toEqual(payload);
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const [url, init] = fetchSpy.mock.calls[0];
    expect(String(url)).toContain('/api/v1/previews/office-rendition');
    expect(new Headers((init as RequestInit).headers).get('Authorization')).toBe(
      'Bearer preview-token',
    );
  });
});
