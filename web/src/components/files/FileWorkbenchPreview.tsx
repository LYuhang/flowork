import { lazy, Suspense, useEffect, useMemo, useState } from 'react';
import { Download, LoaderCircle } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { useTranslation } from 'react-i18next';

import { FileTypeIcon } from '@/components/presentation/FileTypeIcon';
import { Button } from '@/components/ui/button';
import { resolveFileCapability } from '@/lib/files/capabilities';
import { resolveCodePreviewLanguage } from '@/pages/chat/preview/code-preview-language';

const CodePreviewEditor = lazy(() => import('@/pages/chat/preview/CodePreviewEditor').then(
  (module) => ({ default: module.CodePreviewEditor }),
));

type FileWorkbenchPreviewProps = {
  fileName: string;
  mimeType?: string | null;
  blob?: Blob | null;
  text?: string | null;
  loading?: boolean;
  error?: string | null;
};

function leafName(path: string): string {
  return path.split('/').filter(Boolean).at(-1) ?? path;
}

function typeLabelKey(kind: ReturnType<typeof resolveFileCapability>['kind']): string {
  return `files.kind.${kind}`;
}

function useObjectUrl(blob: Blob | null): string | null {
  const objectUrl = useMemo(() => (blob ? URL.createObjectURL(blob) : null), [blob]);
  useEffect(() => {
    return () => {
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [objectUrl]);
  return objectUrl;
}

/**
 * Read-only package file surface shared by Skill and Knowledge.
 *
 * It deliberately owns presentation only: callers keep their existing package
 * storage, permissions, and file-loading contracts while every package gains
 * the same type routing and visual hierarchy.
 */
export function FileWorkbenchPreview({
  fileName,
  mimeType,
  blob = null,
  text: providedText = null,
  loading = false,
  error = null,
}: FileWorkbenchPreviewProps) {
  const { t } = useTranslation();
  const capability = resolveFileCapability(fileName, mimeType || blob?.type);
  const language = capability.source
    ? resolveCodePreviewLanguage({ name: fileName, contentType: capability.mime })
    : null;
  const [blobText, setBlobText] = useState<{
    blob: Blob;
    value: string | null;
    failed: boolean;
  } | null>(null);
  const needsText = capability.source;
  const objectUrl = useObjectUrl(blob);

  useEffect(() => {
    let active = true;
    if (providedText === null && blob && needsText) {
      void blob.text().then(
        (value) => { if (active) setBlobText({ blob, value, failed: false }); },
        () => { if (active) setBlobText({ blob, value: null, failed: true }); },
      );
    }
    return () => { active = false; };
  }, [blob, needsText, providedText]);

  const loadedText = providedText
    ?? (blobText?.blob === blob ? blobText.value : null);
  const textFailed = blobText?.blob === blob && blobText.failed;
  const displayName = leafName(fileName);
  const typeLabel = capability.kind === 'code'
    ? capability.label
    : t(typeLabelKey(capability.kind), capability.label);
  const previewError = error || (textFailed
    ? t('files.preview.readFailed', 'The file content could not be read.')
    : null);
  const textPending = needsText && loadedText === null && Boolean(blob);
  const isPdf = capability.mime === 'application/pdf' || /\.pdf$/i.test(fileName);
  // Keep package metadata readable instead of interpreting the closing YAML
  // delimiter as a Markdown setext heading. Preserve the original file text.
  const frontmatter = capability.kind === 'markdown'
    ? loadedText?.match(/^\uFEFF?---\r?\n([\s\S]*?)\r?\n(?:---|\.\.\.)[ \t]*(?:\r?\n|$)/)
    : null;

  let content;
  if (loading || textPending) {
    content = (
      <div className="flex min-h-64 items-center justify-center gap-2 text-sm text-content-secondary">
        <LoaderCircle className="size-4 animate-spin" />
        {t('files.preview.loading', 'Loading preview…')}
      </div>
    );
  } else if (previewError) {
    content = (
      <div className="flex min-h-64 items-center justify-center px-6 text-center text-sm text-state-danger">
        {previewError}
      </div>
    );
  } else if (capability.kind === 'markdown' && loadedText !== null) {
    content = (
      <article className="markdown-document" data-role="markdown-document">
        <div className="markdown-document-content">
          {frontmatter ? (
            <div className="markdown-document-code-panel">
              <pre className="whitespace-pre-wrap break-words"><code>{frontmatter[0].trim()}</code></pre>
            </div>
          ) : null}
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            components={{
              code: ({ className, children, node: _node, ...props }) => {
                void _node;
                return <code className={`markdown-document-code-inline ${className ?? ''}`} {...props}>{children}</code>;
              },
              pre: ({ children }) => <div className="markdown-document-code-panel"><pre>{children}</pre></div>,
              table: ({ children }) => <div className="markdown-document-table-wrap"><table>{children}</table></div>,
            }}
          >
            {frontmatter ? loadedText.slice(frontmatter[0].length) : loadedText}
          </ReactMarkdown>
        </div>
      </article>
    );
  } else if (language && loadedText !== null) {
    content = (
      <Suspense fallback={<pre className="min-h-full overflow-auto p-5 font-mono text-xs">{loadedText}</pre>}>
        <CodePreviewEditor
          value={loadedText}
          language={language}
          readOnly
          ariaLabel={t('files.preview.codeLabel', '{{name}} source code', { name: displayName })}
          onChange={() => undefined}
        />
      </Suspense>
    );
  } else if (loadedText !== null) {
    content = (
      <div className="min-h-full bg-surface-work px-5 py-6 sm:px-8">
        <pre className="mx-auto max-w-4xl whitespace-pre-wrap break-words rounded-lg border border-edge-subtle bg-background p-5 font-mono text-xs leading-6 shadow-sm">
          {loadedText}
        </pre>
      </div>
    );
  } else if (objectUrl && capability.kind === 'image') {
    content = (
      <div className="flex min-h-full items-center justify-center bg-surface-sunken/60 p-6">
        <img src={objectUrl} alt={fileName} className="max-h-full max-w-full rounded-md object-contain shadow-sm" />
      </div>
    );
  } else if (objectUrl && capability.kind === 'video') {
    content = <div className="flex min-h-full items-center justify-center bg-black/90 p-5"><video src={objectUrl} controls className="max-h-full max-w-full" /></div>;
  } else if (objectUrl && capability.kind === 'audio') {
    content = <div className="flex min-h-64 items-center justify-center bg-surface-sunken/60 p-6"><audio src={objectUrl} controls className="w-full max-w-xl" /></div>;
  } else if (objectUrl && isPdf) {
    content = <iframe title={fileName} src={objectUrl} className="h-full min-h-[34rem] w-full border-0 bg-white" />;
  } else {
    content = (
      <div className="flex min-h-64 flex-col items-center justify-center gap-3 p-6 text-center">
        <FileTypeIcon fileName={fileName} mimeType={mimeType} className="size-12 rounded-xl [&>svg]:size-6" />
        <div>
          <p className="text-sm font-medium">{t('files.preview.unavailable', 'Preview is not available for this file type')}</p>
          <p className="mt-1 text-xs text-content-secondary">{typeLabel} · {capability.mime}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col" data-role="file-workbench-preview">
      <header className="flex min-h-14 shrink-0 items-center gap-3 border-b border-edge-subtle bg-background px-3 py-2 sm:px-4">
        <FileTypeIcon fileName={fileName} mimeType={mimeType} className="size-9 rounded-lg [&>svg]:size-[18px]" />
        <div className="min-w-0 flex-1">
          <h3 className="truncate text-sm font-semibold text-content-primary">{displayName}</h3>
          <p className="truncate font-mono text-xs text-content-tertiary" title={fileName}>{fileName}</p>
        </div>
        <span className="hidden rounded-full border border-edge-subtle bg-surface-sunken px-2 py-0.5 text-xs font-medium text-content-secondary sm:inline-flex">
          {typeLabel}
        </span>
        {objectUrl ? (
          <Button variant="ghost" size="sm" asChild>
            <a href={objectUrl} download={displayName} aria-label={t('files.preview.download', 'Download {{name}}', { name: displayName })}>
              <Download className="size-4" />
              <span className="hidden lg:inline">{t('download', 'Download')}</span>
            </a>
          </Button>
        ) : null}
      </header>
      <div className="page-scroll-region min-h-0 flex-1 bg-surface-work">
        {content}
      </div>
    </div>
  );
}
