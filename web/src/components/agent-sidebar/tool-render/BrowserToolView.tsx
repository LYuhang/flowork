/** Presentation for the reviewed official Playwright MCP `browser_*` surface.
 *
 * The upstream server returns standard MCP content. This component deliberately
 * knows nothing about Flowork's retired Browser MCP envelope or old custom tool
 * vocabulary. A historical envelope falls back to the generic envelope renderer
 * in `ToolCallBlock`; it cannot select this presenter or execute anything.
 */
import { useTranslation } from 'react-i18next';
import { ExternalLink } from 'lucide-react';
import { useSignedMediaSrc } from '@/pages/canvas/nodes/template-preview-media';
import { UniversalToolResult } from './UniversalToolResult';
import type { UniversalToolResultValue } from './parseStandardToolResult';

export interface BrowserToolViewProps {
  /** Tool name (e.g. `browser_navigate`). */
  toolName: string;
  /** Protocol-preserving result emitted by official Playwright MCP. */
  standardResult: UniversalToolResultValue;
  /** Raw `arguments` JSON string (acted element / purpose / expect live here). */
  arguments?: string;
  /** VFS scope id used to sign chat workspace media (`/data/browser-media/...`). */
  wfId?: string;
}

function isRecord(x: unknown): x is Record<string, unknown> {
  return typeof x === 'object' && x !== null;
}

function asString(x: unknown): string | undefined {
  return typeof x === 'string' && x.length > 0 ? x : undefined;
}

/** Parse the call `arguments` JSON (fail-soft → {}). */
function parseArgs(raw: string | undefined): Record<string, unknown> {
  if (!raw) return {};
  try {
    const v = JSON.parse(raw);
    return isRecord(v) ? v : {};
  } catch {
    return {};
  }
}

function standardText(value: UniversalToolResultValue | null | undefined): string {
  if (!value) return '';
  return value.content
    .filter((block) => block.type === 'text' && typeof block.text === 'string')
    .map((block) => block.text as string)
    .join('\n');
}

/** Only VFS paths under the browser-media output root are renderable. The
 * official server reports output-dir files as Markdown links such as
 * `browser-media/page.png`; call arguments may contain the absolute
 * `/data/browser-media/page.png` requested by the Agent. */
function normalizeBrowserMediaPath(value: string | undefined): string | undefined {
  if (!value) return undefined;
  let path = value.trim().replace(/^file:\/\//, '').split(/[?#]/, 1)[0];
  try {
    path = decodeURIComponent(path);
  } catch {
    return undefined;
  }
  if (path.startsWith('browser-media/')) path = `/data/${path}`;
  else if (path.startsWith('data/browser-media/')) path = `/${path}`;
  if (!path.startsWith('/data/browser-media/')) return undefined;
  if (path.includes('\\') || path.includes('\0')) return undefined;
  const parts = path.split('/');
  if (parts.some((part) => part === '.' || part === '..')) return undefined;
  if (!/\.(?:png|jpe?g|webp)$/i.test(path)) return undefined;
  return path;
}

function officialPlaywrightScreenshotPath(
  result: UniversalToolResultValue | null | undefined,
  args: Record<string, unknown>,
): string | undefined {
  const text = standardText(result);
  const markdownTarget = Array.from(text.matchAll(/\]\(([^)]+)\)/g))
    .map((match) => normalizeBrowserMediaPath(match[1]))
    .find(Boolean);
  if (markdownTarget) return markdownTarget;
  return normalizeBrowserMediaPath(asString(args.filename));
}

/** Small labelled key/value row used by the structured renderings. */
function KeyValue({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex gap-2 text-xs">
      <span className="shrink-0 font-semibold text-muted-foreground">{label}</span>
      <span className="min-w-0 break-words">{children}</span>
    </div>
  );
}

/** A bounded, always-shown monospace text block (snapshot / read_text). NOT a
 *  second collapse — the whole tool block already collapses; long text scrolls. */
function BoundedText({ title, text }: { title: string; text: string }) {
  return (
    <div className="text-xs">
      <div className="font-semibold text-muted-foreground">{title}</div>
      <pre className="mt-1 max-h-72 overflow-auto whitespace-pre-wrap break-words rounded border bg-muted/40 p-2 font-mono leading-snug">
        {text}
      </pre>
    </div>
  );
}

/** Screenshot / get_image → a signed `<img>` from the media VFS path. */
function MediaImage({ path, wfId, alt }: { path: string; wfId?: string; alt: string }) {
  const { t } = useTranslation();
  const src = useSignedMediaSrc(path, { wfId });
  if (!src) {
    return (
      <div className="text-xs text-muted-foreground" data-role="browser-image-loading">
        {t('browser.image_loading', 'Loading image…')}
      </div>
    );
  }
  return (
    <a href={src} target="_blank" rel="noreferrer" data-role="browser-image">
      <img
        src={src}
        alt={alt}
        className="max-h-72 w-auto max-w-full rounded border"
        loading="lazy"
      />
    </a>
  );
}

export function BrowserToolView({
  toolName,
  standardResult,
  arguments: rawArgs,
  wfId,
}: BrowserToolViewProps) {
  const { t } = useTranslation();
  const args = parseArgs(rawArgs);
  const protocolText = standardText(standardResult);

  switch (toolName) {
    // ── Navigation: final URL + resolved title ────────────────────────────
    case 'browser_navigate': {
      const finalUrl = asString(args.url);
      return (
        <div className="space-y-1" data-role="browser-navigate">
          {finalUrl && (
            <KeyValue label={t('browser.url', 'URL')}>
              <a
                href={finalUrl}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 text-primary hover:underline"
              >
                <span className="break-all">{finalUrl}</span>
                <ExternalLink className="h-3 w-3 shrink-0" />
              </a>
            </KeyValue>
          )}
          {protocolText && <BoundedText title={t('tool.output', 'Output')} text={protocolText} />}
        </div>
      );
    }

    // ── Pixels: signed <img> from the media VFS path ─────────────────────
    // browser_take_screenshot covers both whole-page and per-element capture
    // through the upstream target argument.
    case 'browser_take_screenshot': {
      const path = officialPlaywrightScreenshotPath(standardResult, args);
      if (!path) {
        return <UniversalToolResult value={standardResult} wfId={wfId} />;
      }
      return (
        <div className="space-y-1" data-role="browser-media">
          <MediaImage path={path} wfId={wfId} alt={toolName} />
          <div className="truncate text-xs text-muted-foreground" title={path}>
            {path}
          </div>
        </div>
      );
    }

    // ── Page reads: collapsible text / fields ────────────────────────────
    case 'browser_snapshot': {
      const text = protocolText || undefined;
      if (text) {
        return (
          <BoundedText
            title={t('browser.page_text', 'Page text')}
            text={text}
          />
        );
      }
      break;
    }

    // ── Actions: acted element + expected post-condition ─────────────────
    case 'browser_click':
    case 'browser_type':
    case 'browser_select_option':
    case 'browser_press_key': {
      const acted =
        asString(args.target) ??
        asString(args.element) ??
        asString(args.key) ??
        asString(args.option);
      const purpose = asString(args.purpose);
      const expect = asString(args.expect);
      const typedText = asString(args.text);
      const hasAny = acted || purpose || expect || typedText;
      return (
        <div className="space-y-1" data-role="browser-action">
          {acted && <KeyValue label={t('browser.element', 'Element')}>{acted}</KeyValue>}
          {typedText && <KeyValue label={t('browser.text', 'Text')}>{typedText}</KeyValue>}
          {purpose && <KeyValue label={t('browser.purpose', 'Purpose')}>{purpose}</KeyValue>}
          {expect && <KeyValue label={t('browser.expect', 'Expect')}>{expect}</KeyValue>}
          {!hasAny && (
            <UniversalToolResult value={standardResult} wfId={wfId} />
          )}
        </div>
      );
    }

    default:
      break;
  }

  return <UniversalToolResult value={standardResult} wfId={wfId} />;
}
