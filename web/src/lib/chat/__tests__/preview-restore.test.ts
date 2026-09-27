import { describe, expect, it } from 'vitest';
import { previewItemToRestore, type ChatPreviewItem } from '../preview-state';

const file: ChatPreviewItem = {
  id: 'file:report', title: 'Report',
  resource: { schemaVersion: 1, kind: 'file', fileRef: {
    schemaVersion: 1, scope: 'chat', chatId: 'chat', path: '/data/report.pdf',
  } },
};
const url: ChatPreviewItem = {
  id: 'interactive:url', title: 'Website',
  resource: { schemaVersion: 1, kind: 'interactive', artifactId: 'url' },
  artifact: {
    kind: 'interactive_artifact', artifact_id: 'url', component_type: 'url_preview',
    props: { url: 'https://example.com' },
  },
};

describe('side preview restoration', () => {
  it('restores the active URL even when file tabs were persisted', () => {
    expect(previewItemToRestore([file], [file, url], url.id)).toBe(url);
  });
  it('does not reopen an already restored active tab', () => {
    expect(previewItemToRestore([file, url], [file, url], url.id)).toBeNull();
  });
  it('preserves available file tabs when an artifact is unavailable', () => {
    expect(previewItemToRestore([file], [file], url.id)).toBeNull();
  });
  it('restores the selected artifact into an empty pane', () => {
    expect(previewItemToRestore([], [file, url], url.id)).toBe(url);
  });
  it('chooses the first resource only when no tabs remain', () => {
    expect(previewItemToRestore([], [file], null)).toBe(file);
    expect(previewItemToRestore([], [], url.id)).toBeNull();
  });
});
