import { PreviewToolbar } from './PreviewToolbar';
import { PreviewContextMenu } from './PreviewContextMenu';
import { fileTextReference, textSelectionFromOffsets, type PreviewTextSelection } from '@/lib/preview/text-reference';
import {
  isValidElement,
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ComponentPropsWithoutRef,
  type ReactNode,
} from 'react';
import { Code2, Columns2, Eye, Pencil, LogOut, RotateCcw, Save } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { hasMarkdownMath, normalizeMathDelimiters } from '@/components/agent-sidebar/markdown-math';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { ConfirmationDialog } from '@/components/ui/confirmation-dialog';
import { CopyButton } from '@/components/ui/copy-button';
import { Textarea } from '@/components/ui/textarea';
import { PreviewApiError, createPreviewResourceSession } from '@/lib/api/previews';
import { useWritePreviewFile } from '@/lib/api/queries/previews';
import type {
  PreviewDescriptorV1,
  PreviewErrorInfo,
  PreviewResourceSessionV1,
} from '@/lib/preview/protocol';
import { agentFilePathFromHref } from '@/lib/preview/protocol';
import {
  isPreviewSandboxLoaderMessage,
  loadPreviewSandboxDocument,
  PREVIEW_SANDBOX_LOADER_PATH,
} from '@/lib/preview/sandbox-loader';
import { cn } from '@/lib/utils';
import { useShiki } from '@/lib/use-shiki';
import {
  buildFilePreviewHtmlDocument,
  FILE_PREVIEW_CHANNEL,
} from './html-preview-document';
import { resolveMarkdownImageUrl } from './markdown-preview-resource';
import { PreviewErrorState } from './PreviewErrorState';
import { resolveCodePreviewLanguage } from './code-preview-language';
import type { PreviewRendererProps } from './renderer-types';

const CodePreviewEditor = lazy(() => import('./CodePreviewEditor').then(
  (module) => ({ default: module.CodePreviewEditor }),
));
const MathMarkdown = lazy(() => import('@/components/agent-sidebar/MathMarkdown'));

type TextKind = 'text' | 'markdown' | 'html';
type DisplayMode = 'source' | 'preview' | 'split';

async function loadText(descriptor: PreviewDescriptorV1, signal?: AbortSignal): Promise<string> {
  if (typeof descriptor.content?.inlineText === 'string') {
    return descriptor.content.inlineText;
  }
  if (!descriptor.content?.url) return '';
  const sampleOnly = descriptor.content.truncated && descriptor.sizeBytes > 10 * 1024 * 1024;
  const response = await fetch(descriptor.content.url, {
    signal,
    headers: sampleOnly ? { Range: `bytes=0-${2 * 1024 * 1024 - 1}` } : undefined,
  });
  if (!response.ok && response.status !== 206) {
    throw new Error(`File content request failed: ${response.status}`);
  }
  return response.text();
}

function HtmlDocument({
  descriptor,
  html,
  unsaved = false,
  onOpenFile,
}: {
  descriptor: PreviewDescriptorV1;
  html: string;
  unsaved?: boolean;
  onOpenFile?: (path: string) => void;
}) {
  const { t } = useTranslation();
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const contextText = useRef('');
  const [documentHtml, setDocumentHtml] = useState<string | null>(null);
  const [documentRevision, setDocumentRevision] = useState(0);
  const [error, setError] = useState<PreviewErrorInfo | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    void createPreviewResourceSession(descriptor.fileRef, controller.signal).then(
      (session) => {
        setError(null);
        setDocumentHtml(buildFilePreviewHtmlDocument(html, session));
        setDocumentRevision((value) => value + 1);
      },
      () => {
        if (!controller.signal.aborted) {
          setError({ code: 'content_unavailable', params: {} });
        }
      },
    );
    return () => controller.abort();
  }, [descriptor.fileRef, descriptor.revision, html]);

  const loadSandboxDocument = useCallback(() => {
    loadPreviewSandboxDocument(iframeRef.current, documentHtml ?? '');
  }, [documentHtml]);

  useEffect(() => {
    const listener = (event: MessageEvent) => {
      if (event.source !== iframeRef.current?.contentWindow) return;
      if (event.origin !== 'null') return;
      if (isPreviewSandboxLoaderMessage(event.data)) {
        if (event.data.type === 'ready') loadSandboxDocument();
        else setError({ code: 'content_unavailable', params: {} });
        return;
      }
      const message = event.data as Record<string, unknown> | null;
      if (message?.channel === FILE_PREVIEW_CHANNEL && message.type === 'preview.context'
          && typeof message.x === 'number' && Number.isFinite(message.x)
          && typeof message.y === 'number' && Number.isFinite(message.y)
          && typeof message.text === 'string' && message.text.length <= 32769) {
        const frame = iframeRef.current;
        if (!frame) return;
        const rect = frame.getBoundingClientRect();
        contextText.current = message.text;
        frame.dispatchEvent(new MouseEvent('contextmenu', {bubbles:true,cancelable:true,
          clientX:rect.left + Math.max(0, Math.min(rect.width, message.x)),
          clientY:rect.top + Math.max(0, Math.min(rect.height, message.y))}));
        return;
      }

      if (
        message?.channel === FILE_PREVIEW_CHANNEL
        && message.type === 'preview.open'
        && typeof message.path === 'string'
      ) {
        const path = agentFilePathFromHref(message.path);
        if (path) onOpenFile?.(path);
      }
    };
    window.addEventListener('message', listener);
    return () => window.removeEventListener('message', listener);
  }, [loadSandboxDocument, onOpenFile]);

  if (error) return <PreviewErrorState descriptor={descriptor} error={error} />;
  if (!documentHtml) return <div className="p-4 text-sm text-muted-foreground">{t('preview.html.loading', 'Loading HTML preview…')}</div>;
  return (
    <PreviewContextMenu className="h-full" build={() => contextText.current.trim()
      ? fileTextReference(descriptor, {text:contextText.current}, unsaved)
      : {schema_version:1,id:crypto.randomUUID(),type:'file',label:descriptor.name,
        resource:{kind:'file',file_ref:descriptor.fileRef,revision:descriptor.revision}}}>
    <iframe
      key={`${descriptor.revision}:${documentRevision}`}
      ref={iframeRef}
      title={descriptor.name}
      sandbox="allow-scripts"
      src={PREVIEW_SANDBOX_LOADER_PATH}
      onLoad={loadSandboxDocument}
      className="h-full min-h-[360px] w-full border-0 bg-white"
    />
    </PreviewContextMenu>
  );
}

function RenderedText({
  unsaved,
  kind,
  descriptor,
  value,
  onOpenFile,
}: {
  kind: TextKind;
  unsaved?: boolean;
  descriptor: PreviewDescriptorV1;
  value: string;
  onOpenFile?: (path: string) => void;
}) {
  if (kind === 'markdown') {
    return (
      <MarkdownDocument descriptor={descriptor} value={value} onOpenFile={onOpenFile} />
    );
  }
  if (kind === 'html') {
    return <HtmlDocument descriptor={descriptor} html={value} unsaved={unsaved} onOpenFile={onOpenFile} />;
  }
  return <pre className="min-h-full whitespace-pre-wrap break-words p-4 font-mono text-xs">{value}</pre>;
}

function markdownNodeText(node: ReactNode): string {
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(markdownNodeText).join('');
  if (isValidElement<{ children?: ReactNode }>(node)) {
    return markdownNodeText(node.props.children);
  }
  return '';
}

function markdownHeadingSlug(children: ReactNode): string {
  return markdownNodeText(children)
    .normalize('NFKC')
    .trim()
    .toLocaleLowerCase()
    .replace(/[^\p{L}\p{N}\s_-]/gu, '')
    .replace(/[\s_]+/g, '-')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '');
}

type MarkdownHeadingProps = ComponentPropsWithoutRef<'h1'> & { node?: unknown };
type MarkdownAnchorProps = ComponentPropsWithoutRef<'a'> & { node?: unknown };
type MarkdownImageProps = ComponentPropsWithoutRef<'img'> & { node?: unknown };
type MarkdownCodeProps = ComponentPropsWithoutRef<'code'> & { node?: unknown };
type MarkdownPreProps = ComponentPropsWithoutRef<'pre'> & { node?: unknown };

function MarkdownCodePanel({ children }: Pick<MarkdownPreProps, 'children'>) {
  const codeElement = isValidElement<MarkdownCodeProps>(children) ? children : null;
  const code = markdownNodeText(codeElement?.props.children ?? children).replace(/\n$/, '');
  const language = /(?:^|\s)language-([\w+-]+)/.exec(codeElement?.props.className ?? '')?.[1]
    ?? 'text';
  const highlighted = useShiki(code, language);

  return (
    <div className="markdown-document-code-panel" data-language={language}>
      <div className="markdown-document-code-header">
        <span>{language === 'text' ? 'Plain text' : language}</span>
        <CopyButton value={code} />
      </div>
      {highlighted ? (
        <div
          className="markdown-document-code-highlight"
          dangerouslySetInnerHTML={{ __html: highlighted }}
        />
      ) : (
        <pre><code>{code}</code></pre>
      )}
    </div>
  );
}

function MarkdownDocument({
  descriptor,
  value,
  onOpenFile,
}: {
  descriptor: PreviewDescriptorV1;
  value: string;
  onOpenFile?: (path: string) => void;
}) {
  const articleRef = useRef<HTMLElement>(null);
  const math = hasMarkdownMath(value);
  const Renderer = math ? MathMarkdown : ReactMarkdown;
  const resourceSessionKey = `${descriptor.revision}:${JSON.stringify(descriptor.fileRef)}`;
  const [resourceSessionState, setResourceSessionState] = useState<{
    key: string;
    session: PreviewResourceSessionV1;
  } | null>(null);
  const resourceSession = resourceSessionState?.key === resourceSessionKey
    ? resourceSessionState.session
    : null;

  useEffect(() => {
    const controller = new AbortController();
    void createPreviewResourceSession(descriptor.fileRef, controller.signal).then(
      (session) => setResourceSessionState({ key: resourceSessionKey, session }),
      () => undefined,
    );
    return () => controller.abort();
  }, [descriptor.fileRef, resourceSessionKey]);

  const slugCounts = new Map<string, number>();
  const renderHeading = (Tag: 'h1' | 'h2' | 'h3' | 'h4' | 'h5' | 'h6') => (
    { children, node: _node, ...props }: MarkdownHeadingProps
  ) => {
    void _node;
    const base = markdownHeadingSlug(children) || 'section';
    const seen = slugCounts.get(base) ?? 0;
    slugCounts.set(base, seen + 1);
    const id = seen === 0 ? base : `${base}-${seen}`;
    return <Tag {...props} id={id}>{children}</Tag>;
  };
  const renderLink = ({ children, href, node: _node, ...props }: MarkdownAnchorProps) => {
    void _node;
    const targetPath = href ? agentFilePathFromHref(href) : null;
    if (href?.startsWith('#')) {
      return (
        <a
          {...props}
          href={href}
          onClick={(event) => {
            event.preventDefault();
            let id = href.slice(1);
            try {
              id = decodeURIComponent(id);
            } catch {
              return;
            }
            const safeId = id.replace(/["\\]/g, '\\$&');
            articleRef.current
              ?.querySelector<HTMLElement>(`[id="${safeId}"]`)
              ?.scrollIntoView({ block: 'start' });
          }}
        >
          {children}
        </a>
      );
    }
    if (targetPath) {
      return (
        <a
          {...props}
          href={href}
          onClick={(event) => {
            event.preventDefault();
            onOpenFile?.(targetPath);
          }}
        >
          {children}
        </a>
      );
    }
    const external = href?.startsWith('https://') || href?.startsWith('http://');
    return (
      <a
        {...props}
        href={href}
        target={external ? '_blank' : undefined}
        rel={external ? 'noopener noreferrer' : undefined}
      >
        {children}
      </a>
    );
  };
  const renderImage = ({ src, node: _node, ...props }: MarkdownImageProps) => {
    void _node;
    const resolved = src && resourceSession
      ? resolveMarkdownImageUrl(src, resourceSession)
      : src && (/^https?:\/\//i.test(src) || /^data:image\//i.test(src))
        ? src
        : null;
    return <img {...props} src={resolved ?? undefined} />;
  };

  return (
    <article ref={articleRef} className="markdown-document" data-role="markdown-document">
      <div className="markdown-document-content">
      <Suspense fallback={<span className="whitespace-pre-wrap">{value}</span>}>
      <Renderer
        remarkPlugins={[remarkGfm]}
        components={{
          h1: renderHeading('h1'),
          h2: renderHeading('h2'),
          h3: renderHeading('h3'),
          h4: renderHeading('h4'),
          h5: renderHeading('h5'),
          h6: renderHeading('h6'),
          a: renderLink,
          img: renderImage,
          code: ({ children, className, node: _node, ...props }: MarkdownCodeProps) => {
            void _node;
            return <code className={cn('markdown-document-code-inline', className)} {...props}>{children}</code>;
          },
          pre: ({ children, node: _node }: MarkdownPreProps) => {
            void _node;
            return <MarkdownCodePanel>{children}</MarkdownCodePanel>;
          },
          table: ({ children }) => (
            <div className="markdown-document-table-wrap"><table>{children}</table></div>
          ),
        }}
      >
        {math ? normalizeMathDelimiters(value) : value}
      </Renderer>
      </Suspense>
      </div>
    </article>
  );
}

function TextDocumentRenderer({
  descriptor,
  onDirtyChange,
  onOpenFile,
  kind,
}: PreviewRendererProps & { kind: TextKind }) {
  const { t } = useTranslation();
  const write = useWritePreviewFile(descriptor.fileRef);
  const [source, setSource] = useState('');
  const [quoteSelection, setQuoteSelection] = useState<PreviewTextSelection | null>(null);
  const captureRenderedSelection = (root: HTMLElement) => {
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed || !selection.rangeCount) return;
    const range = selection.getRangeAt(0);
    if (!root.contains(range.startContainer) || !root.contains(range.endContainer)) return;
    // Rendered Markdown offsets are not source offsets.
    setQuoteSelection({ text: selection.toString() });
  };
  useEffect(() => setQuoteSelection(null), [descriptor.revision]);
  const [savedSource, setSavedSource] = useState('');
  const [mode, setMode] = useState<DisplayMode>(kind === 'text' ? 'source' : 'preview');
  const [editing, setEditing] = useState(false);
  const [loadedRevision, setLoadedRevision] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<PreviewErrorInfo | null>(null);
  const [saveConflict, setSaveConflict] = useState(false);
  const [confirmOverwrite, setConfirmOverwrite] = useState(false);
  const [confirmExitEditing, setConfirmExitEditing] = useState(false);
  const [reloadSequence, setReloadSequence] = useState(0);
  const editRevisionRef = useRef(descriptor.revision);
  const [editRevision, setEditRevision] = useState(descriptor.revision);
  const codeLanguage = kind === 'text' ? resolveCodePreviewLanguage(descriptor) : null;
  const dirty = editing && source !== savedSource;
  const conflict = saveConflict || (dirty && descriptor.revision !== editRevision);
  const dirtyRef = useRef(dirty);

  useEffect(() => {
    dirtyRef.current = dirty;
    onDirtyChange(dirty);
  }, [dirty, onDirtyChange]);
  useEffect(() => () => onDirtyChange(false), [onDirtyChange]);

  useEffect(() => {
    if (dirtyRef.current && descriptor.revision !== editRevisionRef.current) return;
    const controller = new AbortController();
    void loadText(descriptor, controller.signal).then(
      (value) => {
        setLoadError(null);
        setSource(value);
        setSavedSource(value);
        editRevisionRef.current = descriptor.revision;
        setEditRevision(descriptor.revision);
        setSaveConflict(false);
      },
      () => {
        if (!controller.signal.aborted) {
          setLoadError({ code: 'content_unavailable', params: {} });
        }
      },
    ).finally(() => {
      if (!controller.signal.aborted) setLoadedRevision(descriptor.revision);
    });
    return () => controller.abort();
  }, [descriptor, reloadSequence]);

  const save = useCallback(async (overwrite = false) => {
    setSaveConflict(false);
    try {
      const result = await write.mutateAsync({
        expectedRevision: overwrite ? descriptor.revision : editRevisionRef.current,
        contentType: descriptor.contentType,
        content: source,
      });
      setSavedSource(source);
      editRevisionRef.current = result.revision;
      setEditRevision(result.revision);
      setEditing(false);
    } catch (reason) {
      if (reason instanceof PreviewApiError && reason.status === 409) {
        setSaveConflict(true);
      }
    }
  }, [descriptor.contentType, descriptor.revision, source, write]);

  const reload = useCallback(() => {
    dirtyRef.current = false;
    setEditing(false);
    setSaveConflict(false);
    onDirtyChange(false);
    setLoadedRevision(null);
    setReloadSequence((value) => value + 1);
  }, [onDirtyChange]);

  const discardAndExit = useCallback(() => {
    setSource(savedSource);
    setEditing(false);
    setSaveConflict(false);
    setConfirmExitEditing(false);
    onDirtyChange(false);
  }, [onDirtyChange, savedSource]);

  const requestExitEditing = useCallback(() => {
    if (dirty) {
      setConfirmExitEditing(true);
      return;
    }
    setEditing(false);
    setSaveConflict(false);
    onDirtyChange(false);
  }, [dirty, onDirtyChange]);

  const modes = useMemo(() => kind === 'text'
    ? [{ id: 'source' as const, label: t('preview.mode.source', 'Source'), Icon: Code2 }]
    : [
        { id: 'source' as const, label: t('preview.mode.source', 'Source'), Icon: Code2 },
        { id: 'preview' as const, label: t('preview.mode.preview', 'Preview'), Icon: Eye },
        { id: 'split' as const, label: t('preview.mode.split', 'Split'), Icon: Columns2 },
      ], [kind, t]);

  if (loadedRevision !== descriptor.revision && !dirty && !conflict) {
    return <div className="p-4 text-sm text-muted-foreground">{t('preview.text.loading', 'Loading text…')}</div>;
  }
  if (loadError) return <PreviewErrorState descriptor={descriptor} error={loadError} />;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PreviewToolbar>
        {modes.map(({ id, label, Icon }) => (
          <Button
            key={id}
            type="button"
            size="icon-sm"
            title={label}
            aria-label={label}
            variant={mode === id ? 'secondary' : 'ghost'}
            onClick={() => { setMode(id); setQuoteSelection(null); }}
          >
            <Icon className="h-3.5 w-3.5" />
          </Button>
        ))}
        <div className="flex-1" />

        {descriptor.text?.mixedNewlines ? (
          <span className="text-xs text-amber-600">{t('preview.text.mixedNewlines', 'Mixed newlines will be normalized')}</span>
        ) : null}
        {descriptor.capabilities.edit && !editing ? (
          <Button size="icon-sm" variant="ghost" title={t('preview.action.edit', 'Edit')} aria-label={t('preview.action.edit', 'Edit')} onClick={() => {
            editRevisionRef.current = descriptor.revision;
            setEditRevision(descriptor.revision);
            setEditing(true);
            setMode('source');
          }}>
            <Pencil className="h-3.5 w-3.5" />
          </Button>
        ) : null}
        {editing ? (
          <div className="flex items-center gap-1">
            <Button size="icon-sm" variant="ghost" title={t('preview.action.exitEditing', 'Exit editing')} aria-label={t('preview.action.exitEditing', 'Exit editing')} onClick={requestExitEditing}>
              <LogOut className="h-3.5 w-3.5" />
            </Button>
            <Button size="icon-sm" title={t('preview.action.save', 'Save')} aria-label={t('preview.action.save', 'Save')} disabled={!dirty || write.isPending} onClick={() => void save()}>
              <Save className="h-3.5 w-3.5" />
            </Button>
          </div>
        ) : null}
      </PreviewToolbar>
      {conflict ? (
        <div role="alert" className="flex items-center gap-2 border-b border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-950">
          <span className="min-w-0 flex-1">
            {t(
              'preview.conflict.description',
              'This file changed after editing began. Reload it or explicitly overwrite the latest revision.',
            )}
          </span>
          <Button size="sm" variant="outline" onClick={reload}>
            <RotateCcw className="mr-1 h-3.5 w-3.5" />
            {t('preview.conflict.reload', 'Reload')}
          </Button>
          <Button size="sm" variant="destructive" onClick={() => setConfirmOverwrite(true)}>
            {t('preview.conflict.overwrite', 'Overwrite')}
          </Button>
        </div>
      ) : null}
      {write.error && !conflict ? (
        <div role="alert" className="border-b border-destructive/30 bg-destructive/5 px-3 py-2 text-xs text-destructive">
          {write.error.message}
        </div>
      ) : null}
      <PreviewContextMenu build={event => {
        let selected = (event.target as Element).closest('.cm-editor,textarea') ? quoteSelection : null;
        const native = window.getSelection();
        if (native && !native.isCollapsed && native.rangeCount) {
          const range = native.getRangeAt(0);
          if (event.currentTarget.contains(range.startContainer) && event.currentTarget.contains(range.endContainer)) {
            selected = { text: native.toString() };
          }
        }
        if (selected?.text.trim()) return fileTextReference({ ...descriptor, revision: loadedRevision || descriptor.revision }, selected, dirty);
        return { schema_version: 1, id: crypto.randomUUID(), type: 'file', label: descriptor.name,
          resource: { kind: 'file', file_ref: descriptor.fileRef, revision: descriptor.revision },
          content_type: descriptor.contentType, size_bytes: descriptor.sizeBytes };
      }} className={cn(
        'grid min-h-0 flex-1 overflow-hidden',
        mode === 'split' ? 'grid-cols-2 divide-x divide-edge-subtle' : 'grid-cols-1',
      )}>
        {mode !== 'preview' ? (
          codeLanguage ? (
            <Suspense
              fallback={(
                <pre className="h-full overflow-auto whitespace-pre-wrap break-words p-4 font-mono text-xs">
                  {source}
                </pre>
              )}
            >
              <CodePreviewEditor
                value={source}
                language={codeLanguage}
                readOnly={!editing}
                ariaLabel={`${descriptor.name} source`}
                onChange={setSource}
                onSelectionChange={setQuoteSelection}
              />
            </Suspense>
          ) : editing ? (
            <Textarea
              aria-label={`${descriptor.name} source`}
              value={source}
              onChange={(event) => { setSource(event.target.value); setQuoteSelection(null); }}
              onSelect={event => setQuoteSelection(textSelectionFromOffsets(event.currentTarget.value, event.currentTarget.selectionStart, event.currentTarget.selectionEnd))}
              className="h-full min-h-0 resize-none rounded-none border-0 font-mono text-xs focus-visible:ring-0"
            />
          ) : (
            <pre onMouseUp={event => captureRenderedSelection(event.currentTarget)}
              onKeyUp={event => { if (event.shiftKey) captureRenderedSelection(event.currentTarget); }}
              className="h-full overflow-auto whitespace-pre-wrap break-words p-4 font-mono text-xs">{source}</pre>
          )
        ) : null}
        {mode !== 'source' ? (
          <div className="min-h-0 overflow-auto" onMouseUp={event => captureRenderedSelection(event.currentTarget)}
            onKeyUp={event => { if (event.shiftKey) captureRenderedSelection(event.currentTarget); }}>
            <RenderedText
              unsaved={dirty}
              kind={kind}
              descriptor={descriptor}
              value={source}
              onOpenFile={onOpenFile}
            />
          </div>
        ) : null}
      </PreviewContextMenu>
      <ConfirmationDialog
        open={confirmExitEditing}
        onOpenChange={setConfirmExitEditing}
        title={t('preview.exitEditing.title', 'Discard unsaved changes?')}
        description={t(
          'preview.exitEditing.description',
          'Your edits have not been saved. Discard them and leave editing mode?',
        )}
        cancelLabel={t('preview.exitEditing.keepEditing', 'Keep editing')}
        confirmLabel={t('preview.exitEditing.discard', 'Discard and exit')}
        onConfirm={discardAndExit}
      />
      <ConfirmationDialog
        open={confirmOverwrite}
        onOpenChange={setConfirmOverwrite}
        title={t('preview.conflict.overwriteTitle', 'Overwrite latest version?')}
        description={t(
          'preview.conflict.overwriteDescription',
          'Overwrite the latest file version with your current edits?',
        )}
        cancelLabel={t('common.cancel', 'Cancel')}
        confirmLabel={t('preview.conflict.overwrite', 'Overwrite')}
        pending={write.isPending}
        onConfirm={() => {
          setConfirmOverwrite(false);
          void save(true);
        }}
      />
    </div>
  );
}

export function TextPreviewRenderer(props: PreviewRendererProps) {
  return <TextDocumentRenderer {...props} kind="text" />;
}

export function MarkdownPreviewRenderer(props: PreviewRendererProps) {
  return <TextDocumentRenderer {...props} kind="markdown" />;
}

export function HtmlPreviewRenderer(props: PreviewRendererProps) {
  return <TextDocumentRenderer {...props} kind="html" />;
}
