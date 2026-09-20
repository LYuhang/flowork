import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { signVfs } from '@/lib/api/vfs';
import {
  rewriteRenderedHtml,
  type SignMedia,
} from './template-preview-media';

const IFRAME_BASE_STYLE = `<style>
  :root { color-scheme: light dark; }
  body { margin: 8px; font: 12px/1.5 system-ui, sans-serif; overflow-wrap: anywhere; word-break: break-word; }
  img, video { max-width: 100%; height: auto; }
  table { max-width: 100%; }
  * { max-width: 100%; }
</style>`;

export interface TemplateHoverPreviewProps {
  rendered: string;
  wfId?: string;
  runId?: string;
  signFn?: SignMedia;
}

/**
 * Sandboxed preview for the rendered output of a Template node. VFS media
 * references are resolved to short-lived signed URLs before the document is
 * mounted; scripts remain disabled by the empty iframe sandbox policy.
 */
export function TemplateHoverPreview({
  rendered,
  wfId,
  runId,
  signFn,
}: TemplateHoverPreviewProps) {
  const { t } = useTranslation();
  const sign = signFn ?? signVfs;
  const key = useMemo(
    () => `${rendered}|${wfId ?? ''}|${runId ?? ''}`,
    [rendered, runId, wfId],
  );
  const [result, setResult] = useState<{
    key: string;
    state: 'ready' | 'error';
    srcDoc: string;
  } | null>(null);

  useEffect(() => {
    let cancelled = false;
    void rewriteRenderedHtml(rendered, { wfId, runId, sign }).then(
      (html) => {
        if (cancelled) return;
        setResult({
          key,
          state: 'ready',
          srcDoc: `<!doctype html><html><head>${IFRAME_BASE_STYLE}</head><body>${html}</body></html>`,
        });
      },
      () => {
        if (!cancelled) setResult({ key, state: 'error', srcDoc: '' });
      },
    );
    return () => {
      cancelled = true;
    };
  }, [key, rendered, runId, sign, wfId]);

  const state = result?.key === key ? result.state : 'loading';
  if (state === 'error') {
    return (
      <p data-hover-template-error role="alert" className="mt-1.5 text-xs leading-5 text-state-danger">
        {t('canvas.template.previewError', 'Preview could not be loaded. Collapse and reopen it to retry.')}
      </p>
    );
  }
  if (state === 'loading') {
    return (
      <p data-hover-template-loading className="mt-1.5 text-xs text-muted-foreground">
        {t('canvas.template.previewLoading', 'Loading preview…')}
      </p>
    );
  }

  return (
    <div
      data-hover-template-preview
      className="pointer-events-auto mt-1.5 max-w-[360px] overflow-auto rounded border"
      style={{ maxHeight: 320 }}
    >
      <iframe
        title="template-preview"
        data-testid="template-preview-iframe"
        sandbox=""
        srcDoc={result?.key === key ? result.srcDoc : ''}
        className="block h-[300px] w-full border-0"
      />
    </div>
  );
}
