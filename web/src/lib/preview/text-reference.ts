import type { components } from '@/lib/api/schema';
import type { PreviewDescriptorV1 } from './protocol';
export interface PreviewTextSelection { text: string; startLine?: number; endLine?: number }
export function textSelectionFromOffsets(text: string, from: number, to: number): PreviewTextSelection | null {
  const start = Math.min(from, to), end = Math.max(from, to);
  if (start === end) return null;
  return { text: text.slice(start, end), startLine: text.slice(0, start).split('\n').length,
    endLine: text.slice(0, Math.max(start, end - 1)).split('\n').length };
}
export function fileTextReference(descriptor: PreviewDescriptorV1, selection: PreviewTextSelection, unsaved: boolean): components['schemas']['QuoteContextAttachment'] {
  if (!selection.text.trim() || selection.text.length > 32768) throw new Error('invalid_quote_size');
  return { schema_version: 1, id: crypto.randomUUID(), type: 'quote', label: descriptor.name,
    source: { kind: 'file', file_ref: descriptor.fileRef, revision: descriptor.revision },
    selector: { kind: 'text', unsaved, ...(selection.startLine != null && selection.endLine != null
      ? { start_line: selection.startLine, end_line: selection.endLine } : {}) }, snapshot: { text: selection.text } };
}
