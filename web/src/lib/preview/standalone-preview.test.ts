import { previewOriginFromSearch } from './context-origin';
import { describe, expect, it } from 'vitest';

import {
  previewReturnPath,
  standalonePreviewHref,
  standalonePreviewTarget,
  standaloneWorkflowPreviewHref,
  standaloneWorkflowPreviewTarget,
} from '@/lib/preview/standalone-preview';

describe('standalone Preview links', () => {
  it('preserves source conversation independently of file ownership through a refreshed URL', () => {
    const origin = { chatId: 'chat-original', messageId: 'message-2', artifactId: 'artifact-3' };
    const hrefs = [
      standalonePreviewHref({ schemaVersion: 1, scope: 'project', projectId: 'different-owner', path: '/data/test.txt' }, 'text', origin),
      standaloneWorkflowPreviewHref('workflow-1', 'v2.sv4', origin),
    ];
    for (const href of hrefs) expect(previewOriginFromSearch(new URL(href, 'https://flowork.test').searchParams)).toEqual(origin);
    expect(previewOriginFromSearch(new URLSearchParams('projectId=owner'))).toBeNull();
    expect(previewOriginFromSearch(new URLSearchParams({ originChatId: 'x'.repeat(513) }))).toBeNull();
  });
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
      scope: 'project',
      projectId: 'chat-1',
      path: '/data/季度报告 2026.docx',
    });
    const url = new URL(href, 'https://flowork.test');

    expect(url.pathname).toBe('/preview');
    expect(url.search).not.toContain('token');
    expect(standalonePreviewTarget(url.searchParams)).toEqual({
      fileRef: {
        schemaVersion: 1,
        scope: 'project',
        projectId: 'chat-1',
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
      scope: 'project',
      projectId: 'chat-1',
      path: '/data/../memory/private.txt',
    }))).toBeNull();
  });
});


it.each(['https://elsewhere.test/tasks/1', '//elsewhere.test/tasks/1', '/preview?x=1', '/tasks\\elsewhere', 'javascript:alert(1)'])('rejects invalid preview return destination %s', value => {
  expect(previewReturnPath(value)).toBeNull();
});
it('preserves the source details tab in a preview return destination', () => {
  expect(previewReturnPath('/tasks/1?tab=logs')).toBe('/tasks/1?tab=logs');
});
