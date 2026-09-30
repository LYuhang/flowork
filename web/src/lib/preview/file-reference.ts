import type { components } from '@/lib/api/schema';
import type { PreviewDescriptorV1 } from './protocol';

type Resource = components['schemas']['ResourceContextAttachment'];
export function fileResourceReference(descriptor: PreviewDescriptorV1, selector: NonNullable<Resource['selector']>, label: string): Resource {
  return { schema_version: 1, id: crypto.randomUUID(), type: 'resource', label,
    resource: { kind: 'file', file_ref: descriptor.fileRef, revision: descriptor.revision }, selector };
}
export function filePageReference(descriptor: PreviewDescriptorV1, page: number): Resource {
  if (!Number.isInteger(page) || page < 1) throw new Error('invalid_page');
  return fileResourceReference(descriptor, { kind: 'pages', pages: [page],
    ...(descriptor.rendition ? { rendition_revision: descriptor.rendition.sourceRevision } : {}) }, `${descriptor.name} · p. ${page}`);
}
export function fileTimeReference(descriptor: PreviewDescriptorV1, seconds: number): Resource {
  if (!Number.isFinite(seconds) || seconds < 0) throw new Error('invalid_time');
  return fileResourceReference(descriptor, { kind: 'time', start_seconds: seconds }, `${descriptor.name} · ${seconds.toFixed(2)}s`);
}
