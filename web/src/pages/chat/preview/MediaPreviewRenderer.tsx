import { useRef, useState } from 'react';
import { PreviewContextMenu } from './PreviewContextMenu';
import { fileTimeReference } from '@/lib/preview/file-reference';

import type { PreviewErrorInfo } from '@/lib/preview/protocol';
import { MediaPreviewSurface } from './MediaPreviewSurface';
import { PreviewErrorState } from './PreviewErrorState';
import type { PreviewRendererProps } from './renderer-types';

export function MediaPreviewRenderer({ descriptor, onReload }: PreviewRendererProps) {
  const url = descriptor.content?.url;
  const resourceKey = `${descriptor.revision}:${url ?? ''}`;
  const [failure, setFailure] = useState<{
    resourceKey: string;
    error: PreviewErrorInfo;
  } | null>(null);
  const autoReloadedRevision = useRef<string | null>(null);
  const error = failure?.resourceKey === resourceKey ? failure.error : null;
  if (!url) {
    return (
      <PreviewErrorState
        descriptor={descriptor}
        error={{ code: 'content_unavailable', params: {} }}
      />
    );
  }
  if (error) return <PreviewErrorState descriptor={descriptor} error={error} />;
  const kind = descriptor.renderer === 'image'
    ? 'image'
    : descriptor.renderer === 'audio'
      ? 'audio'
      : 'video';
  return (
    <PreviewContextMenu className="h-full min-h-0" build={event => {
      const media = event.currentTarget.querySelector<HTMLMediaElement>('audio,video');
      if (media && (event.target === media || media.contains(event.target as Node))) {
        if (!media.readyState) throw new Error('page_not_ready');
        return fileTimeReference(descriptor, media.currentTime);
      }
      return { schema_version: 1, id: crypto.randomUUID(), type: 'file', label: descriptor.name,
        resource: { kind: 'file', file_ref: descriptor.fileRef, revision: descriptor.revision },
        content_type: descriptor.contentType, size_bytes: descriptor.sizeBytes };
    }}>
    <MediaPreviewSurface
      key={resourceKey}
      url={url}
      name={descriptor.name}
      kind={kind}
      onError={() => {
        if (onReload && autoReloadedRevision.current !== descriptor.revision) {
          autoReloadedRevision.current = descriptor.revision;
          onReload();
          return;
        }
        setFailure({
          resourceKey,
          error: { code: 'render_failed', params: {} },
        });
      }}
    />
    </PreviewContextMenu>
  );
}
