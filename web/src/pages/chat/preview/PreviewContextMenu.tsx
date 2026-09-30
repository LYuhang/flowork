import { useState, type MouseEvent, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { ContextMenu, ContextMenuContent, ContextMenuTrigger } from '@/components/ui/context-menu';
import type { ChatAttachment } from '@/components/agent-sidebar/chat-attachments';
import { PreviewReferenceButton } from './PreviewReferenceButton';

/** Freeze coordinates/selection at right click, before menu focus changes it. */
export function PreviewContextMenu({ children, build, label, className }: {
  children: ReactNode;
  build: (event: MouseEvent<HTMLDivElement>) => ChatAttachment;
  label?: string;
  className?: string;
}) {
  const { t } = useTranslation();
  const [snapshot, setSnapshot] = useState<ChatAttachment | null>(null);
  const [error, setError] = useState<string | null>(null);
  return <ContextMenu><ContextMenuTrigger asChild>
    <div className={className} data-role="preview-content-context" onContextMenu={event => {
      event.stopPropagation();
      try { setSnapshot(build(event)); setError(null); }
      catch (cause) { setSnapshot(null); setError(cause instanceof Error ? cause.message : 'unavailable'); }
    }}>{children}</div>
  </ContextMenuTrigger><ContextMenuContent>
    <PreviewReferenceButton menu label={snapshot?.type === 'quote'
      ? t('preview.reference.selectedText', 'Quote selected text') : label}
      disabledReason={error === 'invalid_quote_size' ? t('composer.context.tooLarge', 'Select a smaller excerpt (up to 32,768 characters).') : error === 'page_not_ready' ? t('preview.reference.loadingPage', 'Wait for the page to finish loading.') : error || !snapshot ? t('preview.reference.unavailable', 'The source conversation is unavailable or you no longer have access.') : undefined}
      build={() => { if (!snapshot) throw new Error('no_context_snapshot'); return snapshot; }} />
  </ContextMenuContent></ContextMenu>;
}
