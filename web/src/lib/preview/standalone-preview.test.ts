import { describe, expect, it } from 'vitest';

import {
  standalonePreviewHref,
  standalonePreviewTarget,
  standaloneWorkflowPreviewHref,
  standaloneWorkflowPreviewTarget,
} from '@/lib/preview/standalone-preview';

describe('standalone Preview links', () => {
  it('uses the runtime deployment prefix for file and workflow links', () => {
    const previous = window.__VIBECANVAS_RUNTIME_CONFIG__;
    window.__VIBECANVAS_RUNTIME_CONFIG__ = { basePath: '/team/studio/' };
    try {
      expect(standaloneWorkflowPreviewHref('wf', 'v1.sv0')).toBe('/team/studio/preview?type=workflow&workflowId=wf&version=v1.sv0');
      expect(standalonePreviewHref({ schemaVersion: 1, scope: 'mount', path: '/mount/report.pdf' })).toMatch(/^\/team\/studio\/preview\?/);
    } finally {
      window.__VIBECANVAS_RUNTIME_CONFIG__ = previous;
    }
  });
  it('pins workflow previews to an explicit version and never falls back to latest', () => {
    const url = new URL(standaloneWorkflowPreviewHref('wf id', 'v2.sv3'), 'https://flowork.test');
    expect(standaloneWorkflowPreviewTarget(url.searchParams)).toEqual({ workflowId: 'wf id', version: 'v2.sv3' });
    expect(standalonePreviewTarget(url.searchParams)).toBeNull();
    for (const version of ['', 'latest', 'v1', 'v0.sv1', 'v2.sv-1']) {
      expect(standaloneWorkflowPreviewTarget(new URLSearchParams({ type: 'workflow', workflowId: 'wf', version }))).toBeNull();
    }
  });
  it('round-trips a Chat file without exposing credentials', () => {
    const href = standalonePreviewHref({
      schemaVersion: 1,
      scope: 'chat',
      chatId: 'chat-1',
      path: '/data/季度报告 2026.docx',
    });
    const url = new URL(href, 'https://flowork.test');

    expect(url.pathname).toBe('/preview');
    expect(url.search).not.toContain('token');
    expect(standalonePreviewTarget(url.searchParams)).toEqual({
      fileRef: {
        schemaVersion: 1,
        scope: 'chat',
        chatId: 'chat-1',
        path: '/data/季度报告 2026.docx',
      },
      fileType: 'auto',
    });
  });

  it('supports mount and run files while rejecting traversal or missing ownership', () => {
    expect(standalonePreviewTarget(new URLSearchParams({
      scope: 'mount',
      path: '/mount/team/brief.pdf',
      fileType: 'pdf',
    }))).toEqual({
      fileRef: {
        schemaVersion: 1,
        scope: 'mount',
        path: '/mount/team/brief.pdf',
      },
      fileType: 'pdf',
    });
    expect(standalonePreviewTarget(new URLSearchParams({
      scope: 'run',
      path: '/run/output.csv',
    }))).toBeNull();
    expect(standalonePreviewTarget(new URLSearchParams({
      scope: 'chat',
      chatId: 'chat-1',
      path: '/data/../memory/private.txt',
    }))).toBeNull();
  });
});
