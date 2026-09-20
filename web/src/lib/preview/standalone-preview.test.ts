import { describe, expect, it } from 'vitest';

import {
  standalonePreviewHref,
  standalonePreviewTarget,
} from '@/lib/preview/standalone-preview';

describe('standalone Preview links', () => {
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
