import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { ExternalLink, Globe2, RefreshCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';

function normalizeWebUrl(value: string): string | null {
  try {
    const parsed = new URL(value.trim());
    return parsed.protocol === 'http:' || parsed.protocol === 'https:'
      ? parsed.href
      : null;
  } catch {
    return null;
  }
}

export function UrlPreviewRenderer({
  url,
  title,
  description,
}: {
  url: string;
  title: string;
  description?: string;
}) {
  const { t } = useTranslation();
  const initialUrl = useMemo(() => normalizeWebUrl(url), [url]);
  const [draftUrl, setDraftUrl] = useState(initialUrl ?? url);
  const [activeUrl, setActiveUrl] = useState(initialUrl);
  const [reloadKey, setReloadKey] = useState(0);
  const [loadState, setLoadState] = useState<'loading' | 'loaded'>(
    initialUrl ? 'loading' : 'loaded',
  );
  const frameRef = useRef<HTMLIFrameElement>(null);
  const activeHost = useMemo(() => {
    if (!activeUrl) return '';
    try {
      return new URL(activeUrl).hostname;
    } catch {
      return '';
    }
  }, [activeUrl]);
  const isSamePlatformOrigin = useMemo(() => {
    if (!activeUrl) return false;
    try {
      return new URL(activeUrl).origin === window.location.origin;
    } catch {
      return false;
    }
  }, [activeUrl]);
  const sandbox = [
    'allow-downloads',
    'allow-forms',
    'allow-modals',
    'allow-popups',
    'allow-popups-to-escape-sandbox',
    'allow-scripts',
    // A framed site may request access to its own unpartitioned cookies after
    // an explicit user gesture. The browser remains the policy authority and
    // no Flowork credentials are exposed to the frame.
    'allow-storage-access-by-user-activation',
    // External sites need their own origin for storage, cookies, and module
    // loading. A same-platform target stays opaque so it cannot obtain parent
    // DOM access even when it contains scripts.
    ...(isSamePlatformOrigin ? [] : ['allow-same-origin']),
  ].join(' ');

  useEffect(() => {
    if (!activeUrl || loadState !== 'loading') return;
    // Cross-origin iframe load events are not a dependable embeddability
    // signal: a browser may render the document while keeping its navigation
    // opaque to the parent. Keep the initial cue brief and non-blocking, then
    // let the frame or the site's own refusal page speak for itself.
    const settle = window.setTimeout(() => setLoadState('loaded'), 2_500);
    return () => window.clearTimeout(settle);
  }, [activeUrl, loadState, reloadKey]);

  useLayoutEffect(() => {
    const frame = frameRef.current;
    if (!frame || !activeUrl) return;
    // Attach before navigation. A cached cross-origin page can finish between
    // React assigning `src` and a passive effect, which leaves a stale loading
    // overlay even though the framed document is already visible.
    const markLoaded = () => setLoadState('loaded');
    frame.addEventListener('load', markLoaded);
    frame.src = activeUrl;
    return () => frame.removeEventListener('load', markLoaded);
  }, [activeUrl, reloadKey]);

  const navigate = () => {
    const next = normalizeWebUrl(draftUrl);
    if (!next) return;
    setLoadState('loading');
    setActiveUrl(next);
    setDraftUrl(next);
    setReloadKey((value) => value + 1);
  };

  return (
    <div className="flex h-full min-h-[360px] flex-col bg-surface-work" data-role="url-preview">
      <form
        className="flex shrink-0 items-center gap-2 border-b border-edge-subtle bg-surface-raised px-2 py-2"
        onSubmit={(event) => {
          event.preventDefault();
          navigate();
        }}
      >
        <Globe2 className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
        <Input
          value={draftUrl}
          onChange={(event) => setDraftUrl(event.target.value)}
          aria-label={t('preview.url.address', 'Web address')}
          spellCheck={false}
          className="h-8 min-w-0 flex-1 font-mono text-xs"
        />
        <Button type="submit" size="sm" disabled={!normalizeWebUrl(draftUrl)}>
          {t('preview.url.go', 'Go')}
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label={t('preview.url.reload', 'Reload page')}
          disabled={!activeUrl}
          onClick={() => {
            setLoadState('loading');
            setReloadKey((value) => value + 1);
          }}
        >
          <RefreshCw className="h-3.5 w-3.5" />
        </Button>
        {activeUrl ? (
          <Button asChild type="button" variant="ghost" size="icon-sm">
            <a
              href={activeUrl}
              target="_blank"
              rel="noreferrer noopener"
              aria-label={t('preview.url.openNewTab', 'Open page in a new tab')}
            >
              <ExternalLink className="h-3.5 w-3.5" />
            </a>
          </Button>
        ) : null}
      </form>
      {description ? (
        <div className="shrink-0 border-b border-edge-subtle px-3 py-1.5 text-xs text-muted-foreground">
          {description}
        </div>
      ) : null}
      <div className="relative min-h-0 flex-1 bg-white">
        {!activeUrl ? (
          <div className="flex h-full items-center justify-center p-6 text-sm text-destructive">
            {t('preview.url.invalid', 'Enter an absolute HTTP(S) address.')}
          </div>
        ) : (
          <>
            {loadState === 'loading' ? (
              <div className="absolute inset-0 z-10 flex items-center justify-center bg-background/80 text-sm text-muted-foreground">
                {t('preview.url.loading', 'Loading {{host}}…', { host: activeHost || title })}
              </div>
            ) : null}
            <iframe
              ref={frameRef}
              key={`${activeUrl}:${reloadKey}`}
              title={title}
              sandbox={sandbox}
              // Some legitimate embeds (notably YouTube) require a Referer to
              // identify the embedding application. Cross-origin requests only
              // receive the Flowork origin; paths, query parameters, auth
              // headers, and application state are never forwarded.
              referrerPolicy="strict-origin-when-cross-origin"
              allow="autoplay; encrypted-media; fullscreen; picture-in-picture"
              allowFullScreen
              onLoad={() => setLoadState('loaded')}
              className="h-full min-h-[360px] w-full border-0 bg-white"
            />
          </>
        )}
      </div>
      <div className="flex shrink-0 items-center gap-1 border-t border-edge-subtle bg-surface-raised px-3 py-1.5 text-xs text-muted-foreground">
        <span>
          {t(
            'preview.url.isolation',
            'This page is isolated from Flowork. If the site blocks embedding,',
          )}
        </span>
        {activeUrl ? (
          <a
            href={activeUrl}
            target="_blank"
            rel="noreferrer noopener"
            className="font-medium text-primary underline-offset-2 hover:underline"
          >
            {t('preview.url.openNewTab', 'open it in a new tab')}
          </a>
        ) : null}
      </div>
    </div>
  );
}
