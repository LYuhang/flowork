import { useShiki } from '@/lib/use-shiki';
import { cn } from '@/lib/utils';
import { CopyButton } from '@/components/ui/copy-button';

/** Readable immediately; highlighting loads only when this block is mounted. */
export function CodeSnippet({ code, language, className, showHeader = true, testId }: {
  code: string;
  language: string;
  className?: string;
  showHeader?: boolean;
  testId?: string;
}) {
  const html = useShiki(code, language);
  return (
    <div className={cn('min-w-0 overflow-hidden rounded-xl border border-edge-subtle bg-surface-sunken', className)} data-testid={testId}>
      {showHeader && (
        <div className="flex items-center justify-between gap-3 border-b border-edge-subtle px-3 py-2">
          <span className="font-mono text-xs text-content-tertiary">{language.toUpperCase()}</span>
          <CopyButton value={code} />
        </div>
      )}
      <div className="code-snippet-highlight app-scrollbar max-h-96 overflow-auto text-xs leading-5 [&_pre]:!m-0 [&_pre]:!bg-transparent [&_pre]:p-4 [&_pre]:font-mono" tabIndex={0}>
        {html ? (
          // Shiki escapes source text; this is not user-authored HTML.
          <div dangerouslySetInnerHTML={{ __html: html }} />
        ) : <pre className="whitespace-pre"><code>{code}</code></pre>}
      </div>
    </div>
  );
}
