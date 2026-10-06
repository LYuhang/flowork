import { PreviewToolbar } from './PreviewToolbar';
import { useCallback, useRef, useState, type ReactNode } from 'react';
import { Minus, Plus, RotateCcw } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';

const MIN_ZOOM = 0.5;
const MAX_ZOOM = 4;
const ZOOM_STEP = 0.25;

export interface MediaPreviewSurfaceProps {
  url: string;
  name: string;
  kind: 'image' | 'audio' | 'video';
  onError: () => void;
  renderTimeReference?: (getTime: () => number, ready: boolean) => ReactNode;
}

/** Shared media content surface for the unified Preview renderer. */
export function MediaPreviewSurface({
  url,
  name,
  kind,
  onError,
  renderTimeReference,
}: MediaPreviewSurfaceProps) {
  const { t } = useTranslation();
  const [zoom, setZoom] = useState(1);
  const mediaRef = useRef<HTMLMediaElement | null>(null);
  const [ready, setReady] = useState(false);
  const getTime = useCallback(() => mediaRef.current?.currentTime ?? 0, []);
  const reference = renderTimeReference ? <TimeReferenceSlot render={renderTimeReference} getTime={getTime} ready={ready} /> : null;

  if (kind === 'image') {
    return (
      <div className="relative flex h-full min-h-0 flex-col bg-surface-sunken">
        <PreviewToolbar>
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            disabled={zoom <= MIN_ZOOM}
            onClick={() => setZoom((current) => Math.max(MIN_ZOOM, current - ZOOM_STEP))}
            aria-label={t('preview.media.zoomOut', 'Zoom out')}
          >
            <Minus />
          </Button>
          <span
            className="min-w-12 text-center text-xs tabular-nums text-muted-foreground"
            aria-live="polite"
          >
            {Math.round(zoom * 100)}%
          </span>
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            disabled={zoom >= MAX_ZOOM}
            onClick={() => setZoom((current) => Math.min(MAX_ZOOM, current + ZOOM_STEP))}
            aria-label={t('preview.media.zoomIn', 'Zoom in')}
          >
            <Plus />
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            disabled={zoom === 1}
            onClick={() => setZoom(1)}
            aria-label={t('preview.media.resetZoom', 'Reset zoom')}
          >
            <RotateCcw />
          </Button>
        </PreviewToolbar>
        <div className="flex min-h-0 flex-1 items-center justify-center overflow-auto p-6">
          <img
            src={url}
            alt={name}
            className="max-h-full max-w-full object-contain transition-transform duration-feedback motion-reduce:transition-none"
            style={{ transform: `scale(${zoom})` }}
            onDoubleClick={() => setZoom(1)}
            onError={onError}
          />
        </div>
      </div>
    );
  }

  if (kind === 'audio') {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 p-6">
        {reference}
        <audio
          ref={element => { mediaRef.current = element; }}
          onLoadedMetadata={() => setReady(true)}
          controls
          preload="metadata"
          src={url}
          className="w-full max-w-2xl"
          onError={onError}
        />
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col bg-surface-sunken">
      <div className="flex shrink-0 justify-end border-b px-2">{reference}</div>
      <div className="flex min-h-0 flex-1 items-center justify-center bg-black p-4">
      <video
        ref={element => { mediaRef.current = element; }}
        onLoadedMetadata={() => setReady(true)}
        controls
        preload="metadata"
        src={url}
        className="max-h-full max-w-full"
        onError={onError}
      >
        {t('preview.media.videoUnsupported', 'Your browser cannot play this video.')}
      </video>
      </div>
    </div>
  );
}

function TimeReferenceSlot({ render, getTime, ready }: { render: NonNullable<MediaPreviewSurfaceProps['renderTimeReference']>; getTime: () => number; ready: boolean }) {
  return render(getTime, ready);
}
