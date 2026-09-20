import type { JSX } from 'react';
import type { VfsReadOut } from '@/lib/api/vfs';
import { TableRenderer } from './TableRenderer';
import { JsonRenderer } from './JsonRenderer';
import { TextRenderer } from './TextRenderer';
import { HtmlRenderer } from './HtmlRenderer';
import { LinkRenderer } from './LinkRenderer';
import { FallbackRenderer } from './FallbackRenderer';
import { resolveFileCapability } from '@/lib/files/capabilities';

export function renderVfsContent(entry: VfsReadOut): JSX.Element {
  if (typeof entry.content !== 'string') return <FallbackRenderer entry={entry} />;
  const kind = resolveFileCapability(entry.path, entry.content_type).kind;
  const C = kind === 'json'
    ? JsonRenderer
    : kind === 'delimited' || kind === 'jsonl'
      ? TableRenderer
      : kind === 'html'
        ? HtmlRenderer
        : kind === 'link'
          ? LinkRenderer
          : kind === 'text' || kind === 'markdown' || kind === 'python' || kind === 'code'
            ? TextRenderer
            : FallbackRenderer;
  return <C entry={entry} />;
}
