import { describe, expect, it } from 'vitest';

import type { PreviewDescriptorV1 } from '@/lib/preview/protocol';
import { routePreviewDescriptor } from '../preview-routing';

const descriptor: PreviewDescriptorV1 = {
  schemaVersion: 1,
  fileRef: {
    schemaVersion: 1,
    scope: 'project',
    projectId: 'chat-1',
    path: '/data/report.bin',
  },
  name: 'report.bin',
  sizeBytes: 42,
  contentType: 'application/octet-stream',
  detectedType: 'unsupported',
  revision: 'sha256:test',
  renderer: 'unsupported',
  loadPolicy: 'unsupported',
  capabilities: { preview: false, edit: false, download: true },
  content: {
    url: '/api/preview/report',
    truncated: false,
    rangeSupported: true,
  },
  error: { code: 'unsupported_file_type', params: {} },
};

describe('routePreviewDescriptor', () => {
  it.each(['json', '.JSON', 'code'])('keeps %s file hints on the shared text/code renderer', (hint) => {
    const routed = routePreviewDescriptor({ ...descriptor, name: 'workflow.json', contentType: 'application/json', detectedType: 'text' }, hint);
    expect(routed.renderer).toBe('text');
    expect(routed.capabilities.preview).toBe(true);
    expect(routed.error).toBeNull();
  });
  it('routes server-detected facts in the frontend in auto mode', () => {
    expect(routePreviewDescriptor({
      ...descriptor,
      detectedType: 'pdf',
      renderer: 'unsupported',
    }, 'auto').renderer).toBe('pdf');
  });

  it('routes a supported explicit hint entirely in the frontend', () => {
    const routed = routePreviewDescriptor(descriptor, '.docx');
    expect(routed.renderer).toBe('docx');
    expect(routed.detectedType).toBe('docx');
    expect(routed.capabilities.preview).toBe(true);
    expect(routed.error).toBeNull();
  });

  it('falls back to the unsupported renderer for an unknown hint', () => {
    const routed = routePreviewDescriptor(descriptor, 'pages');
    expect(routed.renderer).toBe('unsupported');
    expect(routed.error).toEqual({
      code: 'unsupported_file_type',
      params: { fileType: 'pages' },
    });
  });

  it('maps structured text hints to the spreadsheet renderer', () => {
    const routed = routePreviewDescriptor(descriptor, 'csv');
    expect(routed.renderer).toBe('spreadsheet');
    expect(routed.detectedType).toBe('csv');
  });
});
