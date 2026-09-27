import type { PreviewDescriptorV1, PreviewRendererId } from '@/lib/preview/protocol';

const FILE_TYPE_ROUTES: Record<string, {
  renderer: PreviewRendererId;
  detectedType?: string;
}> = {
  text: { renderer: 'text', detectedType: 'text' },
  txt: { renderer: 'text', detectedType: 'text' },
  json: { renderer: 'text', detectedType: 'text' },
  code: { renderer: 'text', detectedType: 'text' },
  markdown: { renderer: 'markdown', detectedType: 'markdown' },
  md: { renderer: 'markdown', detectedType: 'markdown' },
  html: { renderer: 'html', detectedType: 'html' },
  htm: { renderer: 'html', detectedType: 'html' },
  pdf: { renderer: 'pdf', detectedType: 'pdf' },
  docx: { renderer: 'docx', detectedType: 'docx' },
  pptx: { renderer: 'pptx', detectedType: 'pptx' },
  spreadsheet: { renderer: 'spreadsheet', detectedType: 'spreadsheet' },
  xlsx: { renderer: 'spreadsheet', detectedType: 'spreadsheet' },
  csv: { renderer: 'spreadsheet', detectedType: 'csv' },
  tsv: { renderer: 'spreadsheet', detectedType: 'tsv' },
  jsonl: { renderer: 'spreadsheet', detectedType: 'jsonl' },
  ndjson: { renderer: 'spreadsheet', detectedType: 'jsonl' },
  image: { renderer: 'image', detectedType: 'image' },
  png: { renderer: 'image', detectedType: 'image' },
  jpg: { renderer: 'image', detectedType: 'image' },
  jpeg: { renderer: 'image', detectedType: 'image' },
  gif: { renderer: 'image', detectedType: 'image' },
  webp: { renderer: 'image', detectedType: 'image' },
  svg: { renderer: 'image', detectedType: 'image' },
  audio: { renderer: 'audio', detectedType: 'audio' },
  mp3: { renderer: 'audio', detectedType: 'audio' },
  wav: { renderer: 'audio', detectedType: 'audio' },
  video: { renderer: 'video', detectedType: 'video' },
  mp4: { renderer: 'video', detectedType: 'video' },
  webm: { renderer: 'video', detectedType: 'video' },
  drawio: { renderer: 'drawio', detectedType: 'drawio' },
};

const DETECTED_TYPE_ROUTES: Record<string, PreviewRendererId> = {
  text: 'text',
  markdown: 'markdown',
  html: 'html',
  pdf: 'pdf',
  docx: 'docx',
  pptx: 'pptx',
  spreadsheet: 'spreadsheet',
  csv: 'spreadsheet',
  tsv: 'spreadsheet',
  jsonl: 'spreadsheet',
  image: 'image',
  audio: 'audio',
  video: 'video',
  drawio: 'drawio',
};

/**
 * Select the browser renderer in one place. The API supplies factual metadata,
 * bounded content URLs, and validation results; UI routing remains a frontend
 * concern. Explicit hints are useful for extensionless files and never alter
 * the stored file or the render_interactive tool contract.
 */
export function routePreviewDescriptor(
  descriptor: PreviewDescriptorV1,
  fileType = 'auto',
): PreviewDescriptorV1 {
  const hint = fileType.trim().toLowerCase().replace(/^\./, '') || 'auto';
  const route = hint === 'auto'
    ? {
        renderer: DETECTED_TYPE_ROUTES[descriptor.detectedType] ?? 'unsupported',
        detectedType: descriptor.detectedType,
      }
    : FILE_TYPE_ROUTES[hint];
  if (!route) {
    return {
      ...descriptor,
      renderer: 'unsupported',
      capabilities: { ...descriptor.capabilities, preview: false },
      error: {
        code: 'unsupported_file_type',
        params: { fileType: hint },
      },
    };
  }
  if (hint === 'auto' && route.renderer === 'unsupported') {
    return {
      ...descriptor,
      renderer: 'unsupported',
      capabilities: { ...descriptor.capabilities, preview: false },
    };
  }
  return {
    ...descriptor,
    renderer: route.renderer,
    detectedType: route.detectedType ?? descriptor.detectedType,
    capabilities: { ...descriptor.capabilities, preview: true },
    error: descriptor.error?.code === 'unsupported_file_type'
      ? null
      : descriptor.error,
  };
}
