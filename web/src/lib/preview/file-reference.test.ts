import { describe, expect, it } from 'vitest';
import { filePageReference, fileTimeReference } from './file-reference';
import type { PreviewDescriptorV1 } from './protocol';
const descriptor: PreviewDescriptorV1 = {
  schemaVersion: 1, fileRef: { schemaVersion: 1, scope: 'mount', path: '/mount/report.docx' },
  name: 'report.docx', revision: 'source-v2', sizeBytes: 100, contentType: 'application/octet-stream',
  detectedType: 'docx', renderer: 'docx', loadPolicy: 'range',
  capabilities: { preview: true, edit: false, download: true },
  rendition: { format: 'pdf', contentType: 'application/pdf', url: '/private/temporary-url', sourceRevision: 'source-v2' },
};
describe('file selection references', () => {
  it('binds Office rendition pages to source bytes without persisting temporary URLs', () => {
    const result = filePageReference(descriptor, 3);
    expect(result.selector).toEqual({ kind: 'pages', pages: [3], rendition_revision: 'source-v2' });
    expect(result.resource).toEqual({ kind: 'file', file_ref: descriptor.fileRef, revision: 'source-v2' });
    expect(JSON.stringify(result)).not.toContain('temporary-url');
  });
  it('preserves fractional seconds and rejects invalid coordinates', () => {
    expect(fileTimeReference(descriptor, 42.125).selector).toEqual({ kind: 'time', start_seconds: 42.125 });
    for (const time of [-1, NaN, Infinity]) expect(() => fileTimeReference(descriptor, time)).toThrow();
    for (const page of [-1, 0, 1.5]) expect(() => filePageReference(descriptor, page)).toThrow();
  });
});
