import { lazy, Suspense, useState } from 'react';
import { useChatRenderIdentity } from './chat-render-context';
import { isLocalServiceUrl } from '@/lib/preview/local-service-url';
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog';
import type { InteractiveArtifact } from './tool-render/interactive-artifact-contract';
const ServicePreview = lazy(() => import('@/pages/chat/preview/ServicePreviewRenderer').then((module) => ({ default: module.ServicePreviewRenderer })));
import { hasMarkdownMath, normalizeMathDelimiters } from './markdown-math';
/**
 * Minimal Markdown renderer for chat-message bubbles.
 *
 * Wraps `react-markdown` (the de-facto standard for rendering LLM output in
 * React chat UIs — used by countless OSS assistants) with `remark-gfm` so
 * GitHub-flavored extras (tables, strikethrough, task lists, autolinks)
 * render. `react-markdown` does NOT render raw HTML by default, so this is
 * XSS-safe without a sanitizer — agent output is treated as untrusted text.
 *
 * We don't pull in `@tailwindcss/typography` (`prose`) because the bubble is
 * small and the `prose` defaults fight the bubble's own padding/colors.
 * Instead each element gets a tight, theme-aware class via `components`
 * overrides — enough for headings, lists, code, tables, quotes, links.
 *
 * Links open in a new tab with `rel="noreferrer"` (agent-suggested URLs are
 * untrusted). Code blocks scroll horizontally rather than wrapping so code
 * stays readable in the narrow sidebar.
 */
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { agentFilePathFromHref } from '@/lib/preview/protocol';
import { cn } from '@/lib/utils';

const MathMarkdown = lazy(() => import('./MathMarkdown'));

export interface MarkdownProps {
  children: string;
  className?: string;
  streaming?: boolean;
  publicView?: boolean;
  onOpenFilePreview?: (path: string) => void;
}

export function Markdown({
  children,
  className,
  streaming = false,
  publicView = false,
  onOpenFilePreview,
}: MarkdownProps) {
  const identity = useChatRenderIdentity();
  const [serviceUrl, setServiceUrl] = useState<string | null>(null);
  const math = hasMarkdownMath(children);
  const Renderer = math ? MathMarkdown : ReactMarkdown;
  return (
    <div
      className={cn('chat-message-copy chat-markdown', className)}
      data-role="markdown"
      data-streaming={streaming || undefined}
    >
      <Suspense fallback={<span className="whitespace-pre-wrap">{children}</span>}>
      <Renderer
        remarkPlugins={[remarkGfm]}
        components={{
          p: ({ children }) => <p>{children}</p>,
          a: ({ children, href }) => {
            const filePath = agentFilePathFromHref(href);
            const service = isLocalServiceUrl(href);
            if (service && publicView) return <span>{children}</span>;
            if (service && href && identity?.chatId) return <a href={href} data-action="open-service-preview" onClick={(event) => {
              event.preventDefault();
              const artifact: InteractiveArtifact = { kind: 'interactive_artifact', schema_version: 1,
                artifact_id: `service:${identity.chatId}:${href}`, title: href, component_type: 'url_preview',
                completion_mode: 'render_only', props: { url: href }, preview: { mode: 'preferred' } };
              if (identity.onOpenInteractivePreview) identity.onOpenInteractivePreview(artifact);
              else setServiceUrl(href);
            }}>{children}</a>;
            if (publicView && (filePath !== null || !/^https?:\/\//i.test(href ?? ''))) {
              return <span>{children}</span>;
            }
            const opensPreview = filePath !== null && onOpenFilePreview !== undefined;
            return (
              <a
                href={href}
                target={opensPreview ? undefined : '_blank'}
                rel={opensPreview ? undefined : 'noreferrer'}
                data-action={opensPreview ? 'open-file-preview' : undefined}
                data-file-path={opensPreview ? filePath : undefined}
                onClick={
                  opensPreview
                    ? (event) => {
                        event.preventDefault();
                        onOpenFilePreview(filePath);
                      }
                    : undefined
                }
              >
                {children}
              </a>
            );
          },
          // Public snapshots never fetch private files or third-party tracking images.
          ...(publicView ? { img: ({ alt }: { alt?: string }) => <span>{alt || '[image]'}</span> } : {}),
          ul: ({ children }) => <ul>{children}</ul>,
          ol: ({ children }) => <ol>{children}</ol>,
          li: ({ children }) => <li>{children}</li>,
          h1: ({ children }) => <h1>{children}</h1>,
          h2: ({ children }) => <h2>{children}</h2>,
          h3: ({ children }) => <h3>{children}</h3>,
          blockquote: ({ children }) => (
            <blockquote>{children}</blockquote>
          ),
          hr: () => <hr />,
          strong: ({ children }) => <strong>{children}</strong>,
          // react-markdown v9+ dropped the `inline` prop on `code`, so we
          // distinguish a fenced block (which carries a `language-*` class
          // from the ``` fence) from an inline span (no class) by the
          // className. Inline → a subtle pill; block → bare mono inside the
          // `pre` wrapper below (which owns the panel chrome).
          code: ({ className: cls, children, ...props }) => {
            const isBlock = /(^|\s)language-/.test(cls ?? '');
            return isBlock ? (
              <code className={cn('chat-markdown-code-block', cls)} {...props}>
                {children}
              </code>
            ) : (
              <code
                className="chat-markdown-code-inline"
                {...props}
              >
                {children}
              </code>
            );
          },
          pre: ({ children }) => (
            <pre>{children}</pre>
          ),
          table: ({ children }) => (
            <div className="chat-markdown-table-wrap">
              <table>{children}</table>
            </div>
          ),
          th: ({ children }) => <th>{children}</th>,
          td: ({ children }) => <td>{children}</td>,
        }}
      >
        {math ? normalizeMathDelimiters(children) : children}
      </Renderer>
      </Suspense>
      {serviceUrl && <Dialog open onOpenChange={(open) => { if (!open) setServiceUrl(null); }}>
        <DialogContent className="flex h-[85dvh] max-w-5xl flex-col">
          <DialogTitle>{serviceUrl}</DialogTitle>
          <div className="min-h-0 flex-1">
            {serviceUrl && <Suspense fallback={null}><ServicePreview url={serviceUrl} title={serviceUrl} /></Suspense>}
          </div>
        </DialogContent>
      </Dialog>}
    </div>
  );
}
