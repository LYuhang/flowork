import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import '@/lib/i18n';
import { InteractiveArtifactPreview } from './InteractiveArtifactBlock';

const bridge = vi.hoisted(() => ({ session: vi.fn(), draft: vi.fn(), write: vi.fn() }));
vi.mock('@/lib/api/interactive-artifacts', async importOriginal => ({
  ...await importOriginal<typeof import('@/lib/api/interactive-artifacts')>(),
  createInteractiveResourceSession: bridge.session,
  saveInteractiveDraft: bridge.draft,
  writeInteractiveVfsFile: bridge.write,
}));
beforeEach(() => {
  vi.clearAllMocks();
  bridge.session.mockResolvedValue({ artifact_id: 'original-artifact', resource_mounts: [],
    base_url: 'https://example.test/resources/', expires_in: 300, draft_debounce_ms: 600 });
  bridge.draft.mockResolvedValue({ status: 'saved' });
  bridge.write.mockResolvedValue({ path: '/data/result.txt' });
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

// The live case proves these messages reach the handler instead of being ignored.
it.each([false, true])('HTML bridge honors read_only=%s', async readOnly => {
  render(<InteractiveArtifactPreview artifact={{ kind: 'interactive_artifact',
    artifact_id: 'original-artifact', component_type: 'html_preview',
    completion_mode: 'render_only', read_only: readOnly, interaction_state: { status: 'none' },
    title: 'HTML branch fixture', props: { html: '<input name="answer">' },
  }} />);
  const iframe = await screen.findByTitle('HTML branch fixture') as HTMLIFrameElement;
  const reply = vi.spyOn(iframe.contentWindow!, 'postMessage');
  const send = (payload: Record<string, unknown>) => act(() => {
    window.dispatchEvent(new MessageEvent('message', { source: iframe.contentWindow, origin: 'null',
      data: { channel: 'vibecanvas:interactive:v1', artifactId: 'original-artifact',
        sessionNonce: 'fixture-session', ...payload } }));
  });
  send({ type: 'ready' });
  send({ type: 'draft', state: { answer: 'changed' }, flush: true });
  send({ type: 'vfs.write', requestId: 'write-1', method: 'PUT', contentType: 'text/plain', path: '/data/result.txt', content: 'changed' });
  if (readOnly) {
    expect(bridge.draft).not.toHaveBeenCalled();
    expect(bridge.write).not.toHaveBeenCalled();
    expect(reply).toHaveBeenCalledWith(expect.objectContaining({ type: 'vfs.write.result',
      requestId: 'write-1', ok: false, status: 409 }), '*');
  } else {
    await waitFor(() => expect(bridge.write).toHaveBeenCalledOnce());
    expect(bridge.draft).toHaveBeenCalledWith('original-artifact', { answer: 'changed' });
    await waitFor(() => expect(reply).toHaveBeenCalledWith(expect.objectContaining({
      type: 'vfs.write.result', ok: true, status: 200 }), '*'));
  }
});
