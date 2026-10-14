import { useRef, useState, type MouseEvent, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { ContextMenu, ContextMenuContent, ContextMenuTrigger } from '@/components/ui/context-menu';
import type { ChatAttachment } from '@/components/agent-sidebar/chat-attachments';
import { PreviewReferenceButton } from './PreviewReferenceButton';

type ReferencePoint = Pick<MouseEvent<HTMLDivElement>, 'target' | 'currentTarget' | 'clientX' | 'clientY'>;

/** Freeze the reference before the mouse or touch menu takes focus. */
export function PreviewContextMenu({ children, build, label, className }: {
  children: ReactNode;
  build: (event: ReferencePoint) => ChatAttachment;
  label?: string;
  className?: string;
}) {
  const { t } = useTranslation();
  const [snapshot, setSnapshot] = useState<ChatAttachment | null>(null);
  const [error, setError] = useState<string | null>(null);
  const touchPoint = useRef<ReferencePoint | null>(null);
  function capture(point: ReferencePoint) {
    try { setSnapshot(build(point)); setError(null); }
    catch (cause) { setSnapshot(null); setError(cause instanceof Error ? cause.message : 'unavailable'); }
  }
  return <ContextMenu onOpenChange={open => {
    // Radix opens touch menus on its long-press timer without a contextmenu event.
    // Capture now, rather than on pointer-down, so native text selection is ready.
    if (open && touchPoint.current) capture(touchPoint.current);
    if (!open) touchPoint.current = null;
  }}><ContextMenuTrigger asChild>
    <div className={className} data-role="preview-content-context"
      onPointerDown={event => {
        // Nested previews must open only the innermost touch menu.
        if (event.pointerType === 'touch' || event.pointerType === 'pen') event.stopPropagation();
        touchPoint.current = event.pointerType === 'touch' || event.pointerType === 'pen'
          ? { target: event.target, currentTarget: event.currentTarget, clientX: event.clientX, clientY: event.clientY }
          : null;
      }}
      onContextMenu={event => {
        event.stopPropagation();
        touchPoint.current = null;
        capture(event);
      }}>{children}</div>
  </ContextMenuTrigger><ContextMenuContent>
    <PreviewReferenceButton menu label={label ?? (snapshot && 'label' in snapshot && snapshot.type !== 'file'
      ? t('preview.reference.location', 'Quote {{location}}', { location: snapshot.label }) : undefined)}
      disabledReason={error === 'invalid_quote_size' ? t('composer.context.tooLarge', 'Select a smaller excerpt (up to 32,768 characters).') : error === 'page_not_ready' ? t('preview.reference.loadingPage', 'Wait for the page to finish loading.') : error || !snapshot ? t('preview.reference.unavailable', 'The source conversation is unavailable or you no longer have access.') : undefined}
      build={() => { if (!snapshot) throw new Error('no_context_snapshot'); return snapshot; }} />
  </ContextMenuContent></ContextMenu>;
}
