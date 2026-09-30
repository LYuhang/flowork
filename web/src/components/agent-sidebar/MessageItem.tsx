import { MessageQuoteSelection } from './MessageQuoteSelection';
import { ContextAttachmentList } from './ContextAttachmentList';
/**
 * One merged chat-message row.
 *
 * The prop is a `MergedMessage` produced by `mergeChunks` over either the
 * persisted history list or the in-flight stream buffer — the caller
 * (`ChatMessageList`) runs both through the same reducer so this component
 * never sees raw `'tool'` chunks. Tool invocations are rendered by
 * `ChatMessageList` as separate activity groups, not nested inside message
 * response blocks.
 *
 */
import { memo } from 'react';
import { CheckCircle2, ChevronRight } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { cn } from '@/lib/utils';
import { Markdown } from './Markdown';
import { MessageActions } from './MessageActions';
import { useChatRenderIdentity } from './chat-render-context';
import type { MergedMessage } from './types';
import { useAuthStore } from '@/stores/auth';
import {
  emphasizeUserText,
} from './chat-attachments';

export interface MessageItemProps {
  message: MergedMessage;
  showAvatar?: boolean;
  compact?: boolean;
  /** Whether this is the actively growing assistant segment. Markdown is still
   * parsed while it grows so headings/lists/code never flash as plain text. */
  streaming?: boolean;
  actionsEnabled?: boolean;
  onOpenBackgroundJobs?: (options: {
    jobId?: string;
    deliveryBatchId?: string;
  }) => void;
  onOpenFilePreview?: (path: string) => void;
}

export function MessageAvatar({
  label,
  tone,
}: {
  label: string;
  tone: 'agent' | 'user';
}) {
  return (
    <div
      className={cn(
        'mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-xs font-semibold ring-1',
        tone === 'user'
          ? 'bg-focus/5 text-focus ring-focus/15'
          : 'bg-state-success/5 text-state-success ring-state-success/15',
      )}
      aria-hidden="true"
    >
      {label}
    </div>
  );
}

function getUserInitial(): string {
  const user = useAuthStore.getState().user;
  const name = (user?.displayName || user?.email || 'U').trim();
  return (name.charAt(0) || 'U').toUpperCase();
}

function MessageItemComponent({
  message,
  showAvatar = true,
  compact = false,
  streaming = false,
  actionsEnabled = false,
  onOpenBackgroundJobs,
  onOpenFilePreview,
}: MessageItemProps) {
  const { t } = useTranslation();
  const identity = useChatRenderIdentity();
  const isUser = message.role === 'user';
  const isSystemNotice = message.role === 'system';
  const hasContent = message.content.length > 0;
  const userInitial = getUserInitial();
  const attachments = message.attachments ?? [];
  const userTextParts = isUser
    ? emphasizeUserText(message.content, message.attachments ?? [])
    : [];

  if (isSystemNotice) {
    const activity = message.activity;
    const jobIds = activity?.job_ids ?? [];
    const canOpen = (
      activity?.type === 'background_jobs_delivered'
      && !!onOpenBackgroundJobs
    );
    const content = (
      <>
        <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-state-success" />
        <span className="min-w-0 flex-1 truncate">{message.content}</span>
        {canOpen ? (
          <span className="inline-flex shrink-0 items-center text-foreground/70">
            {t('chat.background.viewAll', 'View all')}
            <ChevronRight className="h-3.5 w-3.5" />
          </span>
        ) : null}
      </>
    );
    return (
      <div
        className={cn(
          'flex justify-start',
          compact ? 'pl-1' : 'pl-12',
        )}
        data-source-message-id={message.id || undefined}
      >
        {canOpen ? (
          <button
            type="button"
            className="flex h-9 max-w-[92%] items-center gap-2 rounded-full bg-surface-sunken px-3 text-left text-xs text-muted-foreground ring-1 ring-edge-subtle transition-colors hover:bg-surface-hover"
            onClick={() => onOpenBackgroundJobs({
              jobId: jobIds.length === 1 ? jobIds[0] : undefined,
              deliveryBatchId: activity?.delivery_batch_id,
            })}
            data-message-role="system"
            data-role="background-job-activity"
            data-delivery-batch-id={activity?.delivery_batch_id}
            data-action="open-background-jobs-preview"
          >
            {content}
          </button>
        ) : (
        <div
          className="flex h-9 max-w-[92%] items-center gap-2 rounded-full bg-surface-sunken px-3 text-xs text-muted-foreground ring-1 ring-edge-subtle"
          data-message-role="system"
        >
          {content}
        </div>
        )}
      </div>
    );
  }

  return (
    <div
      className={cn(
        'flex items-start gap-3',
        isUser ? 'justify-end' : 'justify-start',
      )}
      data-source-message-id={message.id || undefined}
    >
      {!compact && !isUser && (showAvatar ? <MessageAvatar label="A" tone="agent" /> : <div className="h-9 w-9 shrink-0" />)}
      <div
        className={cn(
          'min-w-0 text-foreground',
          isUser
            ? cn(
                'rounded-2xl rounded-br-sm border border-focus/15 bg-focus/10 px-3.5 py-2.5',
                compact ? 'max-w-[88%] rounded-xl px-3 py-2' : 'max-w-[82%]',
              )
            : compact
              ? 'max-w-[94%] py-1'
              : 'max-w-[min(100%,820px)] py-1.5 pr-3',
        )}
        data-message-role={message.role}
        data-message-surface={isUser ? 'bubble' : 'plain'}
        data-message-content-rail={!isUser ? 'assistant' : undefined}
      >
        <MessageQuoteSelection chatId={identity?.chatId ?? null} messageId={message.id ?? undefined}
          label={isUser ? t('composer.context.userExcerpt','Your message excerpt') : t('composer.context.assistantExcerpt','Assistant message excerpt')}
          enabled={actionsEnabled && !streaming}>
        {hasContent &&
          (isUser ? (
            <div className={cn('chat-message-copy whitespace-pre-wrap', compact && 'chat-message-copy-compact')}>
              {userTextParts.map((part, index) =>
                part.emphasized ? (
                  <strong
                    key={`${part.kind}:${index}`}
                    className="font-semibold text-foreground"
                    data-token-kind={part.kind}
                  >
                    {part.text}
                  </strong>
                ) : (
                  <span key={`text:${index}`}>{part.text}</span>
                ),
              )}
            </div>
          ) : (
            <Markdown
              className={compact ? 'chat-message-copy-compact' : undefined}
              streaming={streaming}
              onOpenFilePreview={onOpenFilePreview}
            >
              {message.content}
            </Markdown>
          ))}
        </MessageQuoteSelection>
        {isUser && attachments.length > 0 ? (
          <ContextAttachmentList attachments={attachments} />
        ) : null}
        <div className="flex flex-wrap items-center gap-1">
        {message.role === 'assistant' && hasContent && !streaming && actionsEnabled && identity?.chatId && message.id ? (
          <MessageActions chatId={identity.chatId} messageId={message.id} content={message.content} />
        ) : null}
        </div>
      </div>
      {!compact && isUser && <MessageAvatar label={userInitial} tone="user" />}
    </div>
  );
}

function sameVisibleMessage(previous: MessageItemProps, next: MessageItemProps): boolean {
  if (
    previous.showAvatar !== next.showAvatar ||
    previous.compact !== next.compact ||
    previous.streaming !== next.streaming
    || previous.actionsEnabled !== next.actionsEnabled
    || previous.onOpenBackgroundJobs !== next.onOpenBackgroundJobs
    || previous.onOpenFilePreview !== next.onOpenFilePreview
  ) return false;
  const a = previous.message;
  const b = next.message;
  if (
    a.id !== b.id
    || a.role !== b.role
    || a.content !== b.content
    || a.activity?.delivery_batch_id !== b.activity?.delivery_batch_id
  ) return false;
  const aAttachments = a.attachments ?? [];
  const bAttachments = b.attachments ?? [];
  if (aAttachments.length !== bAttachments.length) return false;
  return aAttachments.every((attachment, index) => {
    const other = bAttachments[index];
    return other != null &&
      (attachment === other || JSON.stringify(attachment) === JSON.stringify(other));
  });
}

// Streaming updates replace only the active assistant message. The transcript
// renderer still walks the list to maintain tool grouping, while memoization
// keeps completed Markdown responses from re-rendering on every token frame.
export const MessageItem = memo(MessageItemComponent, sameVisibleMessage);
