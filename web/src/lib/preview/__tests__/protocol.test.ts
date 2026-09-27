import { describe, expect, it } from 'vitest';
import {
  agentFilePathFromHref,
  fileCitationTarget,
  fileRefFromAgentPath,
} from '@/lib/preview/protocol';

describe('Agent file preview protocol', () => {
  it('parses citation coordinates separately while preserving scope and colon filenames', () => {
    const ref = { schemaVersion: 1, scope: 'chat', chatId: 'c1', path: '/data/notes:review.md:24:3' } as const;
    expect(fileCitationTarget(ref)).toEqual({
      fileRef: { ...ref, path: '/data/notes:review.md' }, line: 24, column: 3,
    });
    expect(agentFilePathFromHref('/data/notes%3Areview.md%3A24')).toBe('/data/notes:review.md:24');
    for (const path of ['/data/notes:review.md', '/data/report.pdf:2', '/data/a.md:0', '/data/a.md:1:0', '/data/a.md:99999999999999999', '/data/../a.md:2']) {
      expect(fileCitationTarget({ ...ref, path: path as `/data/${string}` })).toBeNull();
    }
  });

  it('recognizes private VFS paths from Markdown links', () => {
    expect(agentFilePathFromHref('/data/workflow.json')).toBe('/data/workflow.json');
    expect(agentFilePathFromHref('/data/My%20Report.pdf#page=2')).toBe('/data/My Report.pdf');
    expect(agentFilePathFromHref('/mount/shared/report.docx')).toBe('/mount/shared/report.docx');
    expect(agentFilePathFromHref('/run/output.csv?download=1')).toBe('/run/output.csv');
    expect(agentFilePathFromHref('/memory/state.md')).toBe('/memory/state.md');
    expect(agentFilePathFromHref('/logs/runtime.log')).toBe('/logs/runtime.log');
  });

  it('does not reinterpret web URLs, routes, or traversal as VFS files', () => {
    expect(agentFilePathFromHref('https://example.com/data/report.pdf')).toBeNull();
    expect(agentFilePathFromHref('/settings')).toBeNull();
    expect(agentFilePathFromHref('workflow.json')).toBeNull();
    expect(agentFilePathFromHref('/data/%2e%2e/secrets.txt')).toBeNull();
  });

  it('turns a recognized chat path into a scoped FileRef', () => {
    expect(fileRefFromAgentPath('/data/workflow.json', { chatId: 'chat-1' })).toEqual({
      schemaVersion: 1,
      scope: 'chat',
      chatId: 'chat-1',
      path: '/data/workflow.json',
    });
    expect(fileRefFromAgentPath('/memory/state.md', { chatId: 'chat-1' })).toEqual({
      schemaVersion: 1,
      scope: 'chat',
      chatId: 'chat-1',
      path: '/memory/state.md',
    });
    expect(fileRefFromAgentPath('/logs/runtime.log', { chatId: 'chat-1' })).toEqual({
      schemaVersion: 1,
      scope: 'chat',
      chatId: 'chat-1',
      path: '/logs/runtime.log',
    });
  });
});
