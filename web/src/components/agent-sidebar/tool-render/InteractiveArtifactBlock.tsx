import { Component, lazy, Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import {
  Archive,
  AudioLines,
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  File,
  FileCode2,
  FileImage,
  FileSpreadsheet,
  FileText,
  Film,
  Loader2,
  MessageSquareWarning,
  Network,
  Presentation,
  RefreshCw,
  XCircle,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';
import { MessageAvatar } from '@/components/agent-sidebar/MessageItem';
import type { MergedToolCall } from '@/components/agent-sidebar/types';
import {
  interactiveArtifactRenderError,
  readInteractiveArtifact,
  type CompletionMode,
  type InteractiveArtifact,
} from '@/components/agent-sidebar/tool-render/interactive-artifact-contract';
import {
  buildInteractiveHtmlDocument,
  isInteractiveSandboxMessage,
  type InteractiveRenderDiagnostic,
} from '@/components/agent-sidebar/tool-render/interactive-html-runtime';
import { CopyButton } from '@/components/agent-sidebar/tool-render/CopyButton';
import { ChoicesCard } from './ChoicesCard';
import { useChatRenderIdentity } from '@/components/agent-sidebar/chat-render-context';
import { AsyncState } from '@/components/ui/async-state';
import { CHAT_RECONCILED_EVENT } from '@/lib/api/sse/chat-reconcile';
import type { HitlContinueControl } from '@/lib/api/sse/agent-stream';
import { getApiBase } from '@/lib/base-path';
import { standaloneWorkflowPreviewHref } from '@/lib/preview/standalone-preview';
import {
  createInteractiveResourceSession,
  InteractiveArtifactRequestError,
  saveInteractiveDraft,
  writeInteractiveVfsFile,
} from '@/lib/api/interactive-artifacts';
import { cn } from '@/lib/utils';
import {
  isPreviewSandboxLoaderMessage,
  loadPreviewSandboxDocument,
  PREVIEW_SANDBOX_LOADER_PATH,
} from '@/lib/preview/sandbox-loader';
import { useAuthStore } from '@/stores/auth';
import { useChatStreamStore } from '@/stores/chat-stream';

export type { InteractiveArtifact } from '@/components/agent-sidebar/tool-render/interactive-artifact-contract';
export type SubmitInteractiveAsNewTurn = (
  content: string,
  control?: HitlContinueControl,
) => Promise<void> | void;

const UrlPreviewRenderer = lazy(() =>
  import('@/pages/chat/preview/UrlPreviewRenderer').then((module) => ({
    default: module.UrlPreviewRenderer,
  })),
);
const WorkflowPreviewRenderer = lazy(() =>
  import('@/pages/chat/preview/WorkflowPreviewRenderer').then((module) => ({ default: module.WorkflowPreviewRenderer })),
);

interface InteractiveArtifactBlockProps {
  call: MergedToolCall;
  showAvatar?: boolean;
  compact?: boolean;
  onOpenFilePreview?: (path: string) => void;
  onOpenInteractivePreview?: (artifact: InteractiveArtifact) => void;
  onSubmitAsNewMessage?: SubmitInteractiveAsNewTurn;
}

interface InteractiveFeedbackContext {
  toolName: string;
  toolArguments: string;
}

function isObject(x: unknown): x is Record<string, unknown> {
  return typeof x === 'object' && x !== null && !Array.isArray(x);
}

/**
 * Re-fetch durable artifact state after the chat-level server reconciliation
 * loop runs or network connectivity returns. This is a frontend projection
 * trigger only; it does not store or infer authoritative interaction state.
 */
function useServerReconcileEpoch(): number {
  const [epoch, setEpoch] = useState(0);
  useEffect(() => {
    const bump = () => setEpoch((value) => value + 1);
    window.addEventListener(CHAT_RECONCILED_EVENT, bump);
    window.addEventListener('online', bump);
    return () => {
      window.removeEventListener(CHAT_RECONCILED_EVENT, bump);
      window.removeEventListener('online', bump);
    };
  }, []);
  return epoch;
}

function numberFrom(value: unknown, fallback: number): number {
  const n = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(n) ? n : fallback;
}

function stringFrom(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

function filePreviewName(path: string): string {
  return path.split('/').filter(Boolean).at(-1) || 'File';
}

function filePreviewType(path: string, explicitType = 'auto'): string {
  if (explicitType && explicitType !== 'auto') return explicitType.toLowerCase();
  const name = filePreviewName(path);
  const extension = name.includes('.') ? name.split('.').at(-1) : '';
  return extension?.toLowerCase() || 'file';
}

function filePreviewAppearance(path: string, explicitType = 'auto') {
  const type = filePreviewType(path, explicitType);
  if (['document', 'text', 'doc', 'docx', 'odt', 'rtf', 'txt', 'md', 'markdown'].includes(type)) {
    return { Icon: FileText, type, tone: 'bg-blue-500/10 text-blue-600 dark:text-blue-400' };
  }
  if (type === 'pdf') {
    return { Icon: FileText, type, tone: 'bg-red-500/10 text-red-600 dark:text-red-400' };
  }
  if (['spreadsheet', 'xls', 'xlsx', 'ods', 'csv', 'tsv'].includes(type)) {
    return { Icon: FileSpreadsheet, type, tone: 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400' };
  }
  if (['presentation', 'ppt', 'pptx', 'odp'].includes(type)) {
    return { Icon: Presentation, type, tone: 'bg-orange-500/10 text-orange-600 dark:text-orange-400' };
  }
  if (['image', 'png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg', 'ico', 'avif'].includes(type)) {
    return { Icon: FileImage, type, tone: 'bg-violet-500/10 text-violet-600 dark:text-violet-400' };
  }
  if (['audio', 'mp3', 'wav', 'ogg', 'm4a', 'aac', 'flac'].includes(type)) {
    return { Icon: AudioLines, type, tone: 'bg-pink-500/10 text-pink-600 dark:text-pink-400' };
  }
  if (['video', 'mp4', 'webm', 'mov', 'mkv', 'avi'].includes(type)) {
    return { Icon: Film, type, tone: 'bg-indigo-500/10 text-indigo-600 dark:text-indigo-400' };
  }
  if (['diagram', 'drawio', 'mermaid', 'mmd', 'plantuml', 'puml'].includes(type)) {
    return { Icon: Network, type, tone: 'bg-purple-500/10 text-purple-600 dark:text-purple-400' };
  }
  if (['zip', 'tar', 'gz', 'tgz', '7z', 'rar'].includes(type)) {
    return { Icon: Archive, type, tone: 'bg-amber-500/10 text-amber-600 dark:text-amber-400' };
  }
  if (['code', 'html', 'htm', 'xml', 'py', 'js', 'jsx', 'ts', 'tsx', 'java', 'go', 'rs', 'c', 'cpp', 'h', 'hpp', 'css', 'scss', 'sql', 'sh', 'yaml', 'yml', 'json', 'toml'].includes(type)) {
    return { Icon: FileCode2, type, tone: 'bg-cyan-500/10 text-cyan-600 dark:text-cyan-400' };
  }
  return { Icon: File, type, tone: 'bg-slate-500/10 text-slate-600 dark:text-slate-400' };
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function diagnosticSummary(
  diagnostic: InteractiveRenderDiagnostic,
  t: TFunction,
): string {
  if ((diagnostic.kind === 'fetch' || diagnostic.kind === 'xhr') && diagnostic.httpStatus) {
    return t('tool.interactive.diagnostic.http', 'Resource request failed with HTTP {{status}}.', {
      status: diagnostic.httpStatus,
    });
  }
  if ((diagnostic.kind === 'fetch' || diagnostic.kind === 'xhr') && diagnostic.severity === 'warning') {
    return t('tool.interactive.diagnostic.timeout', 'Resource request is still pending after 10 seconds.');
  }
  if (diagnostic.kind === 'resource') {
    return t('tool.interactive.diagnostic.resource', 'A page resource could not be loaded.');
  }
  if (diagnostic.kind === 'script') {
    return t('tool.interactive.diagnostic.script', 'The page script stopped with an error.');
  }
  if (diagnostic.kind === 'promise') {
    return t('tool.interactive.diagnostic.promise', 'An asynchronous page operation failed.');
  }
  if (diagnostic.kind === 'boot') {
    return t('tool.interactive.diagnostic.boot', 'The interactive page did not finish starting.');
  }
  if (diagnostic.kind === 'contract') {
    return t('tool.interactive.diagnostic.contract', 'The content does not match the interactive rendering contract.');
  }
  return diagnostic.message;
}

function formatInteractiveDiagnostics(diagnostics: InteractiveRenderDiagnostic[]): string {
  return diagnostics.map((diagnostic, index) => {
    const location = diagnostic.line
      ? ` line=${diagnostic.line}${diagnostic.column ? `:${diagnostic.column}` : ''}`
      : '';
    return [
      `${index + 1}. [${diagnostic.severity}] ${diagnostic.kind}: ${diagnostic.message}${location}`,
      diagnostic.path ? `   path: ${diagnostic.path}` : '',
      diagnostic.httpStatus ? `   http_status: ${diagnostic.httpStatus}` : '',
    ].filter(Boolean).join('\n');
  }).join('\n');
}

function InteractiveDiagnosticPanel({
  diagnostics,
  fatal = false,
  onRetry,
  feedbackContext,
}: {
  diagnostics: InteractiveRenderDiagnostic[];
  fatal?: boolean;
  onRetry?: () => void;
  feedbackContext?: InteractiveFeedbackContext;
}) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const setDraft = useChatStreamStore((state) => state.setDraft);
  const activeChatId = useChatRenderIdentity()?.chatId ?? null;
  const primary = diagnostics[0];
  const primarySummary = primary ? diagnosticSummary(primary, t) : '';
  const report = formatInteractiveDiagnostics(diagnostics);
  const sendFeedback = () => {
    setDraft(t(
      'tool.interactive.diagnostic.feedback_prefill',
      'The interactive preview failed. Please inspect and regenerate it.\n\nTool: {{tool}}\n\nTool input:\n{{input}}\n\nRender diagnostics:\n{{details}}',
      {
        tool: feedbackContext?.toolName || 'render_preview',
        input: feedbackContext?.toolArguments || '{}',
        details: report,
      },
    ), activeChatId);
  };
  return (
    <div
      className={cn(
        'm-3 overflow-hidden rounded-lg border text-xs',
        fatal
          ? 'border-destructive/35 bg-destructive/5'
          : 'border-state-warning/35 bg-state-warning/5',
      )}
      data-role="interactive-render-failed"
      data-severity={fatal ? 'error' : 'warning'}
      role={fatal ? 'alert' : 'status'}
    >
      <div className="flex items-start gap-2 px-3 py-2.5">
        <AlertTriangle
          className={cn('mt-0.5 h-4 w-4 shrink-0', fatal ? 'text-destructive' : 'text-state-warning')}
          aria-hidden="true"
        />
        <div className="min-w-0 flex-1">
          <div className="font-medium text-foreground">
            {fatal
              ? t('tool.interactive.render_failed_title', 'Interactive preview failed')
              : t('tool.interactive.diagnostic.attention', 'Interactive preview needs attention')}
          </div>
          {primary ? (
            <div className="mt-0.5 space-y-1 text-muted-foreground">
              <div>
                {primarySummary}
                {primary.path ? (
                  <code className="ml-1 break-all rounded bg-background/75 px-1 py-0.5 text-xs text-foreground">
                    {primary.path}
                  </code>
                ) : null}
              </div>
              {primary.message && primary.message !== primarySummary ? (
                <div className="break-words font-mono text-xs text-foreground">
                  {primary.message}
                </div>
              ) : null}
            </div>
          ) : (
            <div className="mt-0.5 text-muted-foreground">
              {t(
                'tool.interactive.render_failed',
                'This interactive content could not be rendered. Please tell the agent to generate it again.',
              )}
            </div>
          )}
          <div className="mt-2 flex flex-wrap items-center gap-1">
            {onRetry ? (
              <button
                type="button"
                className="inline-flex items-center gap-1 rounded px-1.5 py-1 font-medium text-primary hover:bg-background/70 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                data-action="interactive-retry"
                onClick={onRetry}
              >
                <RefreshCw className="h-3 w-3" />
                {t('retry', 'Retry')}
              </button>
            ) : null}
            <button
              type="button"
              className="inline-flex h-7 items-center gap-1.5 rounded-md border border-primary/20 bg-primary/10 px-2 font-medium text-primary transition-colors hover:bg-primary/15 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              data-action="interactive-feedback"
              onClick={sendFeedback}
            >
              <MessageSquareWarning className="h-3 w-3" />
              {t('tool.interactive.diagnostic.feedback', 'Feedback')}
            </button>
            {report ? <CopyButton value={report} className="py-1" /> : null}
            {diagnostics.length > 0 ? (
              <button
                type="button"
                className="inline-flex items-center gap-1 rounded px-1.5 py-1 text-muted-foreground hover:bg-background/70 hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                aria-expanded={expanded}
                data-action="interactive-diagnostic-details"
                onClick={() => setExpanded((value) => !value)}
              >
                <ChevronDown className={cn('h-3 w-3 transition-transform', expanded && 'rotate-180')} />
                {t('tool.interactive.diagnostic.details', 'Details ({{count}})', { count: diagnostics.length })}
              </button>
            ) : null}
          </div>
        </div>
      </div>
      {expanded ? (
        <div className="border-t border-current/10 bg-background/55 px-3 py-2" data-role="interactive-diagnostic-details">
          <ol className="space-y-2">
            {diagnostics.map((diagnostic) => (
              <li key={diagnostic.id} className="min-w-0">
                <div className="font-medium text-foreground">{diagnosticSummary(diagnostic, t)}</div>
                <dl className="mt-1 grid grid-cols-[auto_minmax(0,1fr)] gap-x-2 gap-y-0.5 text-xs text-muted-foreground">
                  <dt>{t('tool.interactive.diagnostic.stage', 'Stage')}</dt>
                  <dd className="font-mono">{diagnostic.kind}</dd>
                  {diagnostic.path ? (
                    <><dt>{t('tool.interactive.diagnostic.path', 'Path')}</dt><dd className="break-all font-mono text-foreground">{diagnostic.path}</dd></>
                  ) : null}
                  {diagnostic.httpStatus ? (
                    <><dt>HTTP</dt><dd className="font-mono text-foreground">{diagnostic.httpStatus}</dd></>
                  ) : null}
                  {diagnostic.line ? (
                    <><dt>{t('tool.interactive.diagnostic.location', 'Location')}</dt><dd className="font-mono text-foreground">{diagnostic.line}:{diagnostic.column ?? 0}</dd></>
                  ) : null}
                  <dt>{t('tool.interactive.diagnostic.message', 'Message')}</dt>
                  <dd className="break-words font-mono text-foreground">{diagnostic.message}</dd>
                </dl>
              </li>
            ))}
          </ol>
        </div>
      ) : null}
    </div>
  );
}

function InteractiveRenderFailure({
  detail,
  onRetry,
  feedbackContext,
}: {
  detail?: string;
  onRetry?: () => void;
  feedbackContext?: InteractiveFeedbackContext;
}) {
  return (
    <InteractiveDiagnosticPanel
      fatal
      onRetry={onRetry}
      feedbackContext={feedbackContext}
      diagnostics={[{
        id: 'render-failure',
        status: 'open',
        severity: 'error',
        kind: detail ? 'contract' : 'boot',
        message: detail || 'The interactive renderer could not start.',
      }]}
    />
  );
}

class InteractiveRenderBoundary extends Component<
  { children: ReactNode; fallback: (detail?: string) => ReactNode },
  { error: string | null }
> {
  state = { error: null as string | null };

  static getDerivedStateFromError(error: unknown) {
    return { error: error instanceof Error ? error.message : String(error || 'Unknown render error') };
  }

  render() {
    return this.state.error ? this.props.fallback(this.state.error) : this.props.children;
  }
}

function isPositiveHitlStatus(status: string): boolean {
  return status === 'approved' || status === 'submitted';
}

function SubmitControls({
  completionMode,
  schema,
  hitlRequestId,
  artifactId,
  widgetState,
  initialStatus,
  initialResult,
  submitDecision = 'submit',
  cancelDecision = 'cancel',
  resolvedPositiveLabel,
  resolvedNegativeLabel,
  onResolved,
  onSubmitAsNewMessage,
}: {
  completionMode: CompletionMode;
  schema: Record<string, unknown>;
  hitlRequestId?: string | null;
  artifactId?: string | null;
  widgetState: Record<string, unknown>;
  initialStatus?: string;
  initialResult?: Record<string, unknown>;
  submitDecision?: 'submit' | 'approve';
  cancelDecision?: 'cancel' | 'deny';
  resolvedPositiveLabel?: string;
  resolvedNegativeLabel?: string;
  onResolved?: (status: string, result: Record<string, unknown>) => void;
  onSubmitAsNewMessage?: SubmitInteractiveAsNewTurn;
}) {
  const { t } = useTranslation();
  const [state, setState] = useState<'idle' | 'pending' | 'submitted' | 'cancelled' | 'error'>(
    initialStatus && initialStatus !== 'pending'
      ? (isPositiveHitlStatus(initialStatus) ? 'submitted' : 'cancelled')
      : 'idle',
  );
  const [resolvedResult, setResolvedResult] = useState<Record<string, unknown>>(initialResult ?? {});
  useEffect(() => {
    if (!initialStatus) return;
    queueMicrotask(() => {
      setState((current) => {
        if (initialStatus === 'pending') {
          // A request started before the click may return the creation-time
          // `pending` snapshot after the hidden Continue Turn was already
          // accepted. Never let that stale snapshot revive a terminal action.
          if (current === 'submitted' || current === 'cancelled') return current;
          return current === 'pending' ? current : 'idle';
        }
        return isPositiveHitlStatus(initialStatus) ? 'submitted' : 'cancelled';
      });
    });
  }, [initialStatus]);
  useEffect(() => {
    let active = true;
    queueMicrotask(() => {
      if (active) setResolvedResult(initialResult ?? {});
    });
    return () => {
      active = false;
    };
  }, [initialResult]);
  const continueOnly = schema.interaction_type === 'continue';
  const resolve = useCallback(async (
    decision: 'submit' | 'cancel' | 'approve' | 'deny',
  ) => {
    if (!hitlRequestId) {
      setState('error');
      return;
    }
    setState('pending');
    try {
      const effectiveWidgetState = widgetState;
      const positiveDecision = decision === 'submit' || decision === 'approve';
      if (continueOnly) {
        if (!positiveDecision || !artifactId) {
          throw new Error('Continue requires a positive decision and artifact id');
        }
        if (!onSubmitAsNewMessage) {
          throw new Error('Continue is unavailable because the conversation submit handler is missing');
        }
        // Continue is one backend transaction: resolve the durable HITL and
        // reserve its unique hidden follow-up Turn. A separate decision request
        // would leave a frozen card with no Turn if the page disappeared
        // between the two requests.
        await onSubmitAsNewMessage('', {
          type: 'hitl_continue',
          version: 1,
          hitl_request_id: hitlRequestId,
          artifact_id: artifactId,
          action: 'continue',
        });
        setResolvedResult({});
        setState('submitted');
        onResolved?.('submitted', {});
        window.dispatchEvent(new CustomEvent(CHAT_RECONCILED_EVENT));
        return;
      }
      const decisionPayload = {
        artifact_id: artifactId ?? undefined,
        widget_state: effectiveWidgetState,
        decision,
      };
      const interactionResult = decisionPayload;
      const token = useAuthStore.getState().token;
      const base = getApiBase();
      const response = await fetch(`${base}/api/v1/hitl-requests/${hitlRequestId}/decision`, {
        method: 'POST',
        headers: {
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          decision,
          decision_payload: decisionPayload,
          interaction_result: interactionResult,
        }),
      });
      if (!response.ok) throw new Error(`HITL decision failed: ${response.status}`);
      const saved = await response.json() as {
        status?: string;
        interaction_result_json?: Record<string, unknown>;
      };
      const nextStatus = saved.status || (positiveDecision ? 'submitted' : 'cancelled');
      const nextResult = saved.interaction_result_json ?? interactionResult;
      setResolvedResult(nextResult);
      setState(isPositiveHitlStatus(nextStatus) ? 'submitted' : 'cancelled');
      onResolved?.(nextStatus, nextResult);
      // Inline Chat and the Preview pane are two projections of the same
      // durable artifact. Reconcile both immediately after the conditional
      // decision update so the surface that did not submit becomes frozen too.
      window.dispatchEvent(new CustomEvent(CHAT_RECONCILED_EVENT));
    } catch {
      setState('error');
    }
  }, [
    artifactId,
    continueOnly,
    hitlRequestId,
    onResolved,
    onSubmitAsNewMessage,
    widgetState,
  ]);
  if (completionMode === 'render_only') return null;
  if (state === 'submitted') {
    if (continueOnly) {
      return (
        <div className="flex flex-wrap items-center gap-2 border-t border-edge-subtle bg-surface-sunken/45 px-4 py-3">
          <button
            type="button"
            className="inline-flex h-8 cursor-not-allowed items-center gap-1.5 rounded-md border border-edge-structural bg-muted px-3 text-xs font-medium text-muted-foreground opacity-70"
            data-action="interactive-submit"
            data-state="continued"
            disabled
          >
            <CheckCircle2 className="h-3 w-3" />
            {resolvedPositiveLabel ?? t('tool.interactive.continued', 'Continued')}
          </button>
          <span className="min-w-[12rem] flex-1 text-right text-xs leading-5 text-muted-foreground">
            {t('tool.interactive.continue_completed_note', 'This interaction has already continued')}
          </span>
        </div>
      );
    }
    return (
      <div className="flex min-w-0 items-center gap-2 border-t border-state-success/20 bg-state-success/5 px-4 py-2.5 text-xs">
        <span className="inline-flex shrink-0 items-center gap-1.5 rounded-full bg-state-success/10 px-2 py-1 font-medium text-state-success">
          <CheckCircle2 className="h-3 w-3" />
          {resolvedPositiveLabel ?? t('tool.interactive.submitted', 'Submitted')}
        </span>
        {schema.hide_result !== true && Object.keys(resolvedResult).length > 0 ? (
          <code className="min-w-0 truncate text-xs text-muted-foreground" title={JSON.stringify(resolvedResult)}>
            {JSON.stringify(resolvedResult)}
          </code>
        ) : null}
      </div>
    );
  }
  if (state === 'cancelled') {
    return (
      <div className="border-t border-edge-subtle bg-surface-sunken/45 px-4 py-2.5 text-xs text-muted-foreground">
        <span className="inline-flex items-center gap-1.5 rounded-full bg-background/80 px-2 py-1 font-medium">
          <XCircle className="h-3 w-3" />
          {resolvedNegativeLabel ?? t('tool.interactive.cancelled', 'Cancelled')}
        </span>
      </div>
    );
  }
  const submitLabel = stringFrom(schema.submit_label, t('tool.interactive.submit', 'Submit'));
  const cancelLabel = stringFrom(schema.cancel_label, t('tool.interactive.cancel', 'Cancel'));
  return (
    <div className="flex flex-wrap items-center gap-2 border-t border-edge-subtle bg-surface-raised px-4 py-3">
      <button
        type="button"
        className="inline-flex h-8 items-center rounded-md bg-primary px-3 text-xs font-medium text-primary-foreground shadow-sm transition-colors hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60"
        data-action="interactive-submit"
        disabled={state === 'pending'}
        onClick={() => void resolve(submitDecision)}
      >
        {state === 'pending' ? t('common.saving', 'Saving...') : submitLabel}
      </button>
      {!continueOnly ? (
        <button
          type="button"
          className="inline-flex h-8 items-center rounded-md border border-edge-structural bg-background px-3 text-xs font-medium text-muted-foreground transition-colors hover:bg-surface-hover hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60"
          data-action="interactive-cancel"
          disabled={state === 'pending'}
          onClick={() => void resolve(cancelDecision)}
        >
          {cancelLabel}
        </button>
      ) : null}
      {state === 'error' ? (
        <span className="text-xs text-destructive">
          {t('tool.interactive.save_failed', 'Could not save the interaction. Try again.')}
        </span>
      ) : null}
      <span className="min-w-[12rem] flex-1 text-right text-xs leading-5 text-muted-foreground">
        {continueOnly
          ? t('tool.interactive.continue_note', 'Continue starts a new agent turn')
          : t('tool.interactive.wait_note', 'Waiting for user confirmation')}
      </span>
    </div>
  );
}

function PreToolApprovalBody({ artifact }: { artifact: InteractiveArtifact }) {
  const { t } = useTranslation();
  const fields = asArray(artifact.props?.fields).filter(isObject);
  const tool = fields.find((field) => stringFrom(field.name) === 'tool');
  const reason = fields.find((field) => stringFrom(field.name) === 'reason');
  const toolName = stringFrom(tool?.value, t('hitl.unknown_tool', 'Unknown tool'));
  const reasonText = stringFrom(
    reason?.value,
    stringFrom(artifact.interaction_schema?.prompt_text),
  );
  return (
    <div className="space-y-3 p-3" data-role="pre-tool-approval-body">
      <div className="rounded-lg border bg-muted/20 px-3 py-2">
        <div className="text-xs font-semibold text-muted-foreground">
          {t('hitl.tool', 'Tool')}
        </div>
        <div className="mt-1 break-all font-mono text-xs">{toolName}</div>
      </div>
      <div className="text-xs leading-relaxed text-muted-foreground">
        {reasonText || t('hitl.review_before_run', 'Review this action before it runs.')}
      </div>
    </div>
  );
}

function HtmlPreviewRenderer({
  artifactId,
  props,
  height,
  initialState,
  frozen,
  onWidgetStateChange,
  onOpenFilePreview,
  feedbackContext,
  stableSession = false,
}: {
  artifactId: string;
  props: Record<string, unknown>;
  height: number;
  initialState: Record<string, unknown>;
  frozen: boolean;
  onWidgetStateChange?: (state: Record<string, unknown>) => void;
  onOpenFilePreview?: (path: string) => void;
  feedbackContext?: InteractiveFeedbackContext;
  /** Keep completed inline history previews visually stable after first render. */
  stableSession?: boolean;
}) {
  const { t } = useTranslation();
  const html = stringFrom(props.html || props.srcdoc);
  const iframeRef = useRef<HTMLIFrameElement | null>(null);
  const sandboxSessionNonceRef = useRef('');
  const initialStateRef = useRef(initialState);
  const draftTimerRef = useRef<number | null>(null);
  const bootTimerRef = useRef<number | null>(null);
  const [session, setSession] = useState<Awaited<ReturnType<typeof createInteractiveResourceSession>> | null>(null);
  // This snapshot is consumed only when a new iframe document is built. Live
  // draft updates remain in the ref so typing does not continuously remount
  // the sandbox and destroy its DOM state.
  const [documentInitialState, setDocumentInitialState] = useState(initialState);
  const [sessionRevision, setSessionRevision] = useState(0);
  const [failure, setFailure] = useState<InteractiveRenderDiagnostic | null>(null);
  const [diagnostics, setDiagnostics] = useState<InteractiveRenderDiagnostic[]>([]);
  const [draftSaveFailed, setDraftSaveFailed] = useState(false);

  const resolvedHtml = useMemo(
    () => session ? buildInteractiveHtmlDocument({
      artifactId,
      html,
      resourceMounts: session.resource_mounts,
      baseUrl: session.base_url,
      initialState: documentInitialState,
      frozen,
    }) : '',
    [artifactId, documentInitialState, frozen, html, session],
  );

  const loadSandboxDocument = useCallback(() => {
    loadPreviewSandboxDocument(iframeRef.current, resolvedHtml);
  }, [resolvedHtml]);

  const retry = useCallback(() => {
    sandboxSessionNonceRef.current = '';
    if (bootTimerRef.current !== null) window.clearTimeout(bootTimerRef.current);
    setFailure(null);
    setDiagnostics([]);
    setSession(null);
    setSessionRevision((value) => value + 1);
  }, []);

  useEffect(() => {
    let cancelled = false;
    let refreshTimer: number | null = null;
    const load = async () => {
      try {
        const next = await createInteractiveResourceSession(artifactId);
        if (cancelled) return;
        setFailure(null);
        sandboxSessionNonceRef.current = '';
        setDocumentInitialState(initialStateRef.current);
        setSession(next);
        // Capabilities are intentionally ephemeral. Re-mount from the latest
        // in-memory/durable draft before expiry; no signed URL is persisted.
        if (!stableSession) {
          refreshTimer = window.setTimeout(
            () => { void load(); },
            Math.max(30_000, (next.expires_in - 30) * 1000),
          );
        }
      } catch (error) {
        if (!cancelled) {
          setFailure({
            id: 'resource-session',
            status: 'open',
            severity: 'error',
            kind: 'boot',
            message: error instanceof Error ? error.message : String(error || 'Resource session failed.'),
          });
        }
      }
    };
    void load();
    return () => {
      cancelled = true;
      if (refreshTimer !== null) window.clearTimeout(refreshTimer);
    };
  }, [artifactId, sessionRevision, stableSession]);

  useEffect(() => () => {
    if (draftTimerRef.current !== null) window.clearTimeout(draftTimerRef.current);
    if (bootTimerRef.current !== null) window.clearTimeout(bootTimerRef.current);
  }, []);

  const saveDraftNow = useCallback((state: Record<string, unknown>) => {
    initialStateRef.current = state;
    onWidgetStateChange?.(state);
    if (draftTimerRef.current !== null) window.clearTimeout(draftTimerRef.current);
    draftTimerRef.current = null;
    void saveInteractiveDraft(artifactId, state).then(
      (saved) => {
        setDraftSaveFailed(false);
        if (saved.status === 'frozen') setFailure(null);
      },
      () => setDraftSaveFailed(true),
    );
  }, [artifactId, onWidgetStateChange]);

  const persistDraft = useCallback((state: Record<string, unknown>) => {
    initialStateRef.current = state;
    onWidgetStateChange?.(state);
    if (draftTimerRef.current !== null) window.clearTimeout(draftTimerRef.current);
    draftTimerRef.current = window.setTimeout(
      () => saveDraftNow(state),
      session?.draft_debounce_ms ?? 600,
    );
  }, [onWidgetStateChange, saveDraftNow, session?.draft_debounce_ms]);

  useEffect(() => {
    const receive = (event: MessageEvent) => {
      if (event.source !== iframeRef.current?.contentWindow) return;
      if (event.origin !== 'null') return;
      if (isPreviewSandboxLoaderMessage(event.data)) {
        if (event.data.type === 'ready') {
          loadSandboxDocument();
        } else {
          setFailure({
            id: 'sandbox-loader',
            status: 'open',
            severity: 'error',
            kind: 'boot',
            message: event.data.message || 'The interactive preview could not be loaded.',
          });
        }
        return;
      }
      if (!isInteractiveSandboxMessage(event.data) || event.data.artifactId !== artifactId) return;
      if (event.data.type === 'diagnostic' && event.data.diagnostic) {
        if (
          !sandboxSessionNonceRef.current
          || event.data.sessionNonce !== sandboxSessionNonceRef.current
        ) return;
        setDiagnostics((current) => {
          if (event.data.diagnostic?.status === 'resolved') {
            return current.filter((item) => item.id !== event.data.diagnostic?.id);
          }
          const next = current.filter((item) => item.id !== event.data.diagnostic?.id);
          return [event.data.diagnostic as InteractiveRenderDiagnostic, ...next].slice(0, 8);
        });
        return;
      }
      if (event.data.type === 'ready') {
        if (
          sandboxSessionNonceRef.current
          && event.data.sessionNonce !== sandboxSessionNonceRef.current
        ) return;
        if (!sandboxSessionNonceRef.current) {
          sandboxSessionNonceRef.current = event.data.sessionNonce;
        }
        if (bootTimerRef.current !== null) {
          window.clearTimeout(bootTimerRef.current);
          bootTimerRef.current = null;
        }
        setDiagnostics((current) => current.filter((item) => item.id !== 'runtime-boot-timeout'));
        return;
      }
      if (!sandboxSessionNonceRef.current || event.data.sessionNonce !== sandboxSessionNonceRef.current) return;
      if (event.data.type === 'preview.open' && event.data.path) {
        onOpenFilePreview?.(event.data.path);
      } else if (
        event.data.type === 'vfs.write'
        && event.data.requestId
        && event.data.path
        && event.data.content !== undefined
      ) {
        const target = iframeRef.current?.contentWindow;
        const reply = (
          ok: boolean,
          status: number,
          result?: Record<string, unknown>,
          error?: string,
        ) => {
          target?.postMessage({
            channel: event.data.channel,
            artifactId,
            sessionNonce: event.data.sessionNonce,
            type: 'vfs.write.result',
            requestId: event.data.requestId,
            ok,
            status,
            result,
            error,
          }, '*');
        };
        void writeInteractiveVfsFile(artifactId, {
          path: event.data.path,
          content: event.data.content,
          contentType: event.data.contentType || 'application/octet-stream',
        }).then(
          (saved) => {
            setDraftSaveFailed(false);
            reply(true, 200, saved as unknown as Record<string, unknown>);
          },
          (error) => {
            setDraftSaveFailed(true);
            reply(
              false,
              error instanceof InteractiveArtifactRequestError
                ? error.status
                : 500,
              undefined,
              error instanceof Error ? error.message : String(error),
            );
          },
        );
      } else if (event.data.type === 'draft' && event.data.state) {
        if (event.data.flush) saveDraftNow(event.data.state);
        else persistDraft(event.data.state);
      }
    };
    window.addEventListener('message', receive);
    return () => window.removeEventListener('message', receive);
  }, [artifactId, loadSandboxDocument, onOpenFilePreview, onWidgetStateChange, persistDraft, saveDraftNow]);

  // Changing between the live and durable frozen document remounts the loader
  // iframe and therefore creates a new opaque-origin sandbox session. Reset
  // before the browser can deliver that document's first ordered `ready`
  // message; keeping the prior nonce would reject every message from the
  // current iframe.
  useLayoutEffect(() => {
    sandboxSessionNonceRef.current = '';
  }, [artifactId, frozen, resolvedHtml, sessionRevision]);

  useEffect(() => {
    if (!resolvedHtml) return;
    if (bootTimerRef.current !== null) window.clearTimeout(bootTimerRef.current);
    bootTimerRef.current = window.setTimeout(() => {
      const bootDiagnostic: InteractiveRenderDiagnostic = {
        id: 'runtime-boot-timeout',
        status: 'open',
        severity: 'warning',
        kind: 'boot',
        message: 'The iframe did not report ready within 8 seconds. A blocking or invalid script may have stopped startup.',
      };
      setDiagnostics((current) => [
        bootDiagnostic,
        ...current.filter((item) => item.id !== 'runtime-boot-timeout'),
      ].slice(0, 8));
    }, 8_000);
    return () => {
      if (bootTimerRef.current !== null) {
        window.clearTimeout(bootTimerRef.current);
        bootTimerRef.current = null;
      }
    };
  }, [resolvedHtml, sessionRevision]);

  if (failure) {
    return <InteractiveDiagnosticPanel diagnostics={[failure]} fatal onRetry={retry} feedbackContext={feedbackContext} />;
  }
  if (!resolvedHtml) return <div className="h-32 animate-pulse bg-muted/40" />;
  return (
    <div className="relative">
      {diagnostics.length > 0 ? (
        <InteractiveDiagnosticPanel diagnostics={diagnostics} onRetry={retry} feedbackContext={feedbackContext} />
      ) : null}
      <iframe
        key={`${artifactId}:${sessionRevision}:${frozen ? 'frozen' : 'live'}:${session?.base_url ?? ''}`}
        ref={iframeRef}
        sandbox="allow-scripts allow-forms"
        src={PREVIEW_SANDBOX_LOADER_PATH}
        onLoad={loadSandboxDocument}
        title={stringFrom(props.title, 'HTML preview')}
        className="w-full border-0 bg-white"
        style={{ height: Math.max(120, height - 58) }}
        data-role="interactive-html-preview"
      />
      {draftSaveFailed ? (
        <div className="absolute inset-x-2 bottom-2 rounded-md bg-destructive px-2 py-1 text-xs text-destructive-foreground" role="status">
          {t(
            'tool.interactive.draft_failed',
            'Draft could not be saved. Your current page remains open; try editing again.',
          )}
        </div>
      ) : null}
    </div>
  );
}

function ResolvedInteractiveFilePreview({
  path,
  fileType,
  onOpenFilePreview,
}: {
  path: string;
  fileType: string;
  onOpenFilePreview?: (path: string) => void;
}) {
  const { t } = useTranslation();
  const name = filePreviewName(path);
  const appearance = filePreviewAppearance(path, fileType);
  const { Icon } = appearance;
  const body = (
    <>
      <span className={`flex h-11 w-11 shrink-0 items-center justify-center rounded-xl ${appearance.tone}`}>
        <Icon className="h-5 w-5" aria-hidden="true" />
      </span>
      <span className="min-w-0 flex-1 text-left">
        <span className="block truncate text-sm font-medium text-foreground">{name}</span>
        <span className="mt-0.5 block text-xs text-muted-foreground">
          {appearance.type.toUpperCase()} · {t(
            'preview.inline.lightweightDescription',
            'Open to load the full preview',
          )}
        </span>
      </span>
    </>
  );
  const className = 'flex min-h-24 w-full items-center gap-3 px-4 py-3 transition-colors';

  return onOpenFilePreview ? (
    <button
      type="button"
      className={`${className} hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring`}
      aria-label={t('preview.inline.openFile', 'Open {{name}} in Preview', { name })}
      data-preview-render-state="summary"
      onClick={() => onOpenFilePreview(path)}
    >
      {body}
    </button>
  ) : (
    <div className={className} data-preview-render-state="summary">
      {body}
    </div>
  );
}

interface UserInputOption {
  label: string;
  value: string;
  description?: string;
}

interface UserInputQuestion {
  id: string;
  label: string;
  description?: string;
  secret?: boolean;
  multiple?: boolean;
  options: UserInputOption[];
}

function userInputQuestions(value: unknown): UserInputQuestion[] {
  return asArray(value).flatMap((entry) => {
    if (!isObject(entry)) return [];
    const id = stringFrom(entry.id).trim();
    const label = stringFrom(entry.label || entry.question || entry.header).trim();
    if (!id || !label) return [];
    const options = asArray(entry.options).flatMap((option) => {
      if (typeof option === 'string' && option.trim()) {
        return [{ label: option.trim(), value: option.trim() }];
      }
      if (!isObject(option)) return [];
      const optionLabel = stringFrom(option.label || option.title || option.value).trim();
      const optionValue = stringFrom(option.value || option.const || option.label).trim();
      if (!optionLabel || !optionValue) return [];
      return [{
        label: optionLabel,
        value: optionValue,
        description: stringFrom(option.description).trim() || undefined,
      }];
    });
    return [{
      id,
      label,
      description: stringFrom(entry.description).trim() || undefined,
      secret: Boolean(entry.secret || entry.isSecret),
      multiple: Boolean(entry.multiple),
      options,
    }];
  });
}

function safeInteractionUrl(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  try {
    const parsed = new URL(value);
    return parsed.protocol === 'https:' || parsed.protocol === 'http:' ? parsed.toString() : null;
  } catch {
    return null;
  }
}

function UserInputRenderer({
  props,
  initialState,
  frozen,
  onWidgetStateChange,
}: {
  props: Record<string, unknown>;
  initialState: Record<string, unknown>;
  frozen: boolean;
  onWidgetStateChange?: (state: Record<string, unknown>) => void;
}) {
  const { t } = useTranslation();
  const questions = userInputQuestions(props.questions);
  const url = safeInteractionUrl(props.url);
  const [state, setState] = useState<Record<string, unknown>>(initialState);

  useEffect(() => {
    let active = true;
    queueMicrotask(() => {
      if (active) setState(initialState);
    });
    return () => {
      active = false;
    };
  }, [initialState]);

  const update = (id: string, value: unknown) => {
    const next = { ...state, [id]: value };
    setState(next);
    onWidgetStateChange?.(next);
  };

  return (
    <div className="space-y-4 p-4" data-role="runtime-user-input">
      {stringFrom(props.message).trim() ? (
        <p className="text-sm leading-5 text-muted-foreground">{stringFrom(props.message)}</p>
      ) : null}
      {url ? (
        <a
          href={url}
          target="_blank"
          rel="noreferrer noopener"
          className="inline-flex min-h-9 items-center rounded-md border border-edge-structural bg-background px-3 text-sm font-medium text-primary hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {t('tool.interactive.open_external_request', 'Open requested page')}
        </a>
      ) : null}
      {questions.map((question) => {
        const current = state[question.id];
        return (
          <fieldset key={question.id} className="space-y-2" disabled={frozen}>
            <legend className="text-sm font-medium text-foreground">{question.label}</legend>
            {question.description ? (
              <p className="text-xs leading-4 text-muted-foreground">{question.description}</p>
            ) : null}
            {question.options.length > 0 && question.multiple ? (
              <div className="space-y-1.5">
                {question.options.map((option) => {
                  const selected = Array.isArray(current) && current.includes(option.value);
                  return (
                    <label key={option.value} className="flex min-h-9 cursor-pointer items-start gap-2 rounded-md border border-edge-subtle px-3 py-2 text-sm hover:bg-surface-hover">
                      <input
                        type="checkbox"
                        checked={selected}
                        onChange={(event) => {
                          const values = Array.isArray(current) ? current.filter((item): item is string => typeof item === 'string') : [];
                          update(
                            question.id,
                            event.target.checked
                              ? [...new Set([...values, option.value])]
                              : values.filter((value) => value !== option.value),
                          );
                        }}
                      />
                      <span>
                        <span className="block text-foreground">{option.label}</span>
                        {option.description ? <span className="block text-xs text-muted-foreground">{option.description}</span> : null}
                      </span>
                    </label>
                  );
                })}
              </div>
            ) : question.options.length > 0 ? (
              <select
                value={typeof current === 'string' ? current : ''}
                onChange={(event) => update(question.id, event.target.value)}
                className="h-10 w-full rounded-md border border-edge-structural bg-background px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                aria-label={question.label}
              >
                <option value="">{t('tool.interactive.select_option', 'Select an option')}</option>
                {question.options.map((option) => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
            ) : (
              <input
                type={question.secret ? 'password' : 'text'}
                value={typeof current === 'string' ? current : ''}
                onChange={(event) => update(question.id, event.target.value)}
                className="h-10 w-full rounded-md border border-edge-structural bg-background px-3 text-sm text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                aria-label={question.label}
                autoComplete={question.secret ? 'off' : undefined}
              />
            )}
          </fieldset>
        );
      })}
    </div>
  );
}

function ArtifactRenderer({
  artifact,
  previewOnly,
  onWidgetStateChange,
  onOpenFilePreview,
  frozen = false,
  heightOverride,
  fillAvailableHeight = false,
  feedbackContext,
  stableSession = false,
}: {
  artifact: InteractiveArtifact;
  previewOnly?: boolean;
  onWidgetStateChange?: (state: Record<string, unknown>) => void;
  onOpenFilePreview?: (path: string) => void;
  frozen?: boolean;
  heightOverride?: number;
  fillAvailableHeight?: boolean;
  feedbackContext?: InteractiveFeedbackContext;
  stableSession?: boolean;
}) {
  const { t } = useTranslation();
  const props = artifact.props ?? {};
  const height = heightOverride ?? numberFrom(artifact.height, 320);
  if (previewOnly) {
    return (
      <pre className="m-2 max-h-64 overflow-auto whitespace-pre-wrap rounded-md bg-muted/40 p-2 text-xs text-muted-foreground">
        {stringFrom(props.preview, '')}
      </pre>
    );
  }
  switch (artifact.component_type) {
    case 'html_preview':
      return (
        <HtmlPreviewRenderer
          key={artifact.artifact_id ?? 'html-preview'}
          artifactId={artifact.artifact_id ?? ''}
          props={{ ...props, title: props.title ?? artifact.title }}
          height={height}
          initialState={artifact.widget_state ?? {}}
          frozen={frozen}
          onWidgetStateChange={onWidgetStateChange}
          onOpenFilePreview={onOpenFilePreview}
          feedbackContext={feedbackContext}
          stableSession={stableSession}
        />
      );
    case 'file_preview': {
      const path = stringFrom(props.path || props.file_path || props.ref);
      return (
        <ResolvedInteractiveFilePreview
          path={path}
          fileType={stringFrom(props.file_type || props.fileType, 'auto')}
          onOpenFilePreview={onOpenFilePreview}
        />
      );
    }
    case 'workflow_preview':
      return (
        <div style={{ height: fillAvailableHeight ? '100%' : height }} className="min-h-0 min-w-0">
          <Suspense fallback={<AsyncState kind="loading" title={t('chat.preview.loadingWorkflow', 'Loading workflow...')} />}>
            <WorkflowPreviewRenderer workflowId={stringFrom(props.workflow_id)} version={stringFrom(props.version)} />
          </Suspense>
        </div>
      );
    case 'url_preview':
      return (
        <Suspense fallback={<AsyncState kind="loading" title={t('preview.url.loading', 'Loading web page…')} />}>
          <UrlPreviewRenderer
            url={stringFrom(props.url)}
            title={artifact.title ?? 'Web page'}
            description={stringFrom(props.description)}
          />
        </Suspense>
      );
    case 'user_input':
      return (
        <UserInputRenderer
          props={props}
          initialState={artifact.widget_state ?? {}}
          frozen={frozen}
          onWidgetStateChange={onWidgetStateChange}
        />
      );
    default:
      return (
        <pre className="m-2 max-h-64 overflow-auto whitespace-pre-wrap rounded-md bg-muted/40 p-2 font-mono text-xs">
          {JSON.stringify(props, null, 2)}
        </pre>
      );
  }
}

export function InteractiveArtifactPreview({
  artifact,
  maxHeight,
  fillAvailableHeight = false,
  onSubmitAsNewMessage,
  onOpenFilePreview,
}: {
  artifact: InteractiveArtifact;
  maxHeight?: number | string;
  fillAvailableHeight?: boolean;
  onSubmitAsNewMessage?: SubmitInteractiveAsNewTurn;
  onOpenFilePreview?: (path: string) => void;
}) {
  const { t } = useTranslation();
  const [hydration, setHydration] = useState<{
    artifactId: string;
    artifact: InteractiveArtifact;
  } | null>(null);
  const [widgetSnapshot, setWidgetSnapshot] = useState<{
    artifactId: string;
    state: Record<string, unknown>;
  }>(() => ({ artifactId: artifact.artifact_id ?? '', state: artifact.widget_state ?? {} }));
  const artifactId = artifact.artifact_id ?? '';
  const reconcileEpoch = useServerReconcileEpoch();
  useEffect(() => {
    if (!artifactId) return;
    let cancelled = false;
    void (async () => {
      try {
        const token = useAuthStore.getState().token;
        const base = getApiBase();
        const response = await fetch(
          `${base}/api/v1/interactive-artifacts/${encodeURIComponent(artifactId)}`,
          {
            headers: {
              ...(token ? { Authorization: `Bearer ${token}` } : {}),
              Accept: 'application/json',
            },
          },
        );
        if (!response.ok) return;
        const data = await response.json() as { artifact?: InteractiveArtifact };
        if (!cancelled && data.artifact?.kind === 'interactive_artifact') {
          setHydration({ artifactId, artifact: data.artifact });
          setWidgetSnapshot({ artifactId, state: data.artifact.widget_state ?? {} });
        }
      } catch {
        // Preview panes can still render the inline/stub artifact if hydration fails.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [artifactId, reconcileEpoch]);
  const hydratedArtifact = hydration?.artifactId === artifactId ? hydration.artifact : null;
  const effectiveArtifact = hydratedArtifact ?? artifact;
  const widgetState = widgetSnapshot.artifactId === artifactId
    ? widgetSnapshot.state
    : effectiveArtifact.widget_state ?? {};
  const renderError = interactiveArtifactRenderError(effectiveArtifact);
  const feedbackContext: InteractiveFeedbackContext = {
    toolName: 'render_preview',
    toolArguments: JSON.stringify({
      component_type: effectiveArtifact.component_type,
      completion_mode: effectiveArtifact.completion_mode,
      props: effectiveArtifact.props ?? {},
    }, null, 2),
  };
  const frozen = Boolean(
    effectiveArtifact.interaction_state?.status &&
      effectiveArtifact.interaction_state.status !== 'pending' &&
      effectiveArtifact.interaction_state.status !== 'none',
  );
  return (
    <div
      className={fillAvailableHeight ? 'flex h-full min-h-0 flex-col overflow-hidden' : 'overflow-auto'}
      style={fillAvailableHeight ? undefined : { maxHeight }}
      data-role="interactive-artifact-preview-surface"
    >
      {renderError ? (
        <InteractiveRenderFailure detail={renderError} feedbackContext={feedbackContext} />
      ) : (
        <div className={fillAvailableHeight ? 'min-h-0 flex-1 overflow-hidden' : undefined}>
          <InteractiveRenderBoundary
          key={`${effectiveArtifact.artifact_id ?? 'artifact'}:${JSON.stringify(effectiveArtifact.widget_state ?? {})}`}
          fallback={(detail) => <InteractiveRenderFailure detail={detail} feedbackContext={feedbackContext} />}
        >
          <ArtifactRenderer
            artifact={effectiveArtifact}
            previewOnly={false}
            onWidgetStateChange={(state) => setWidgetSnapshot({ artifactId, state })}
            onOpenFilePreview={onOpenFilePreview}
            frozen={frozen}
            heightOverride={720}
            fillAvailableHeight={fillAvailableHeight}
            feedbackContext={feedbackContext}
          />
          </InteractiveRenderBoundary>
        </div>
      )}
      {!renderError ? (
        <div className="shrink-0">
        <SubmitControls
          completionMode={effectiveArtifact.completion_mode ?? 'render_only'}
          schema={effectiveArtifact.interaction_schema ?? {}}
          hitlRequestId={effectiveArtifact.hitl_request_id}
          artifactId={effectiveArtifact.artifact_id}
          widgetState={widgetState}
          initialStatus={effectiveArtifact.interaction_state?.status}
          initialResult={effectiveArtifact.interaction_state?.result}
          resolvedPositiveLabel={
            effectiveArtifact.interaction_schema?.interaction_type === 'continue'
              ? t('tool.interactive.continued', 'Continued')
              : undefined
          }
          onSubmitAsNewMessage={onSubmitAsNewMessage}
          onResolved={(status, result) => {
            setHydration({
              artifactId,
              artifact: {
                ...effectiveArtifact,
                widget_state: widgetState,
                interaction_state: {
                  is_interacted: true,
                  status,
                  result,
                },
              },
            });
          }}
        />
        </div>
      ) : null}
    </div>
  );
}

function StatusLine({ call }: { call: MergedToolCall }) {
  const { t } = useTranslation();
  if (call.status === 'running') {
    return (
      <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
        <Loader2 className="h-3 w-3 animate-spin" />
        {t('tool.waiting', 'waiting for result…')}
      </span>
    );
  }
  if (call.status === 'error') {
    return (
      <span className="inline-flex items-center gap-1 text-xs text-destructive">
        <XCircle className="h-3 w-3" />
        {t('error.generic', 'The tool failed.')}
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
      <CheckCircle2 className="h-3 w-3 text-state-success" />
      {call.name === 'render_preview'
        ? t('tool.meta.render_preview', 'Preview')
        : call.name === 'render_url_preview'
          ? t('tool.meta.render_url_preview', 'Web preview')
          : t('tool.meta.render_interactive', 'File preview')}
    </span>
  );
}

function PreviewCornerIcon() {
  return (
    <span className="relative h-3.5 w-3.5 text-foreground" aria-hidden="true">
      <span className="absolute left-0 top-0 h-1.5 w-1.5 border-l border-t border-current" />
      <span className="absolute right-0 top-0 h-1.5 w-1.5 border-r border-t border-current" />
      <span className="absolute bottom-0 left-0 h-1.5 w-1.5 border-b border-l border-current" />
      <span className="absolute bottom-0 right-0 h-1.5 w-1.5 border-b border-r border-current" />
    </span>
  );
}

export function InteractiveArtifactBlock({
  call,
  showAvatar = true,
  compact = false,
  onOpenFilePreview,
  onOpenInteractivePreview,
  onSubmitAsNewMessage,
}: InteractiveArtifactBlockProps) {
  const { t } = useTranslation();
  const parsed = useMemo(() => readInteractiveArtifact(call), [call]);
  const [hydration, setHydration] = useState<{
    artifactId: string;
    artifact: InteractiveArtifact;
  } | null>(null);
  // The ToolMessage stores the creation-time projection. The durable artifact
  // endpoint is authoritative for submitted widget state and frozen status, so
  // hydrate every persisted artifact, not only VFS-offloaded previews.
  const artifactIdForHydration = parsed.artifact?.artifact_id ?? '';
  const reconcileEpoch = useServerReconcileEpoch();
  const isStableRenderOnly = parsed.artifact?.completion_mode === 'render_only';
  // A completed render-only card is immutable chat history. Hydrate it once,
  // then leave the rendered document alone instead of repainting it on every
  // 30-second chat reconciliation tick. Interactive/HITL cards still refresh.
  const artifactRefreshEpoch = isStableRenderOnly ? 0 : reconcileEpoch;
  useEffect(() => {
    if (!artifactIdForHydration) return;
    let cancelled = false;
    void (async () => {
      try {
        const token = useAuthStore.getState().token;
        const base = getApiBase();
        const response = await fetch(
          `${base}/api/v1/interactive-artifacts/${encodeURIComponent(artifactIdForHydration)}`,
          {
            headers: {
              ...(token ? { Authorization: `Bearer ${token}` } : {}),
              Accept: 'application/json',
            },
          },
        );
        if (!response.ok) return;
        const data = await response.json() as { artifact?: InteractiveArtifact };
        if (!cancelled && data.artifact?.kind === 'interactive_artifact') {
          setHydration({ artifactId: artifactIdForHydration, artifact: data.artifact });
        }
      } catch {
        // Best-effort hydration. The preview remains renderable if this fails.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [artifactIdForHydration, artifactRefreshEpoch]);
  const hydratedArtifact = hydration?.artifactId === artifactIdForHydration
    ? hydration.artifact
    : null;
  const artifact = hydratedArtifact ?? parsed.artifact;
  const previewOnly = Boolean(parsed.previewOnly && !hydratedArtifact);
  const renderError = interactiveArtifactRenderError(artifact, previewOnly);
  const height = Math.max(120, Math.min(numberFrom(artifact?.height, 320), 360));
  const [widgetState, setWidgetState] = useState<Record<string, unknown>>({});
  const [interactionStatus, setInteractionStatus] = useState<string | undefined>(
    artifact?.interaction_state?.status,
  );
  const [interactionResult, setInteractionResult] = useState<Record<string, unknown>>(
    artifact?.interaction_state?.result ?? {},
  );
  const hitlRequestId = artifact?.hitl_request_id ?? null;
  const filePreviewPath =
    artifact?.component_type === 'file_preview'
      ? stringFrom(artifact.props?.path || artifact.props?.file_path || artifact.props?.ref)
      : '';
  const filePreviewExplicitType = artifact?.component_type === 'file_preview'
    ? stringFrom(artifact.props?.file_type || artifact.props?.fileType, 'auto')
    : 'auto';
  const isFilePreviewSummary = !renderError && !!filePreviewPath && artifact?.component_type === 'file_preview';
  const fileSummaryName = filePreviewName(filePreviewPath);
  const fileSummaryAppearance = filePreviewAppearance(filePreviewPath, filePreviewExplicitType);
  const FileSummaryIcon = fileSummaryAppearance.Icon;
  const canOpenFilePreview = !renderError && !!filePreviewPath && !!onOpenFilePreview;
  const standaloneWorkflowHref = compact && !renderError && artifact?.component_type === 'workflow_preview'
    ? standaloneWorkflowPreviewHref(stringFrom(artifact.props?.workflow_id), stringFrom(artifact.props?.version))
    : null;
  const canOpenInteractivePreview = Boolean(
    !renderError &&
      (artifact?.component_type === 'html_preview' || artifact?.component_type === 'url_preview' || artifact?.component_type === 'workflow_preview') &&
      artifact.preview?.mode !== 'none' &&
      onOpenInteractivePreview &&
      !compact,
  );
  const callArtifactMeta = isObject(call.artifact?.meta) ? call.artifact.meta : {};
  const callArtifactPayload = isObject(call.artifact?.payload) ? call.artifact.payload : {};
  const isPreToolApproval = Boolean(
    artifact?.component_type === 'approval' &&
      (
        callArtifactMeta.hitl_type === 'pre_tool_approval' ||
        callArtifactPayload.hitl_type === 'pre_tool_approval' ||
        callArtifactMeta.pending_approval ||
        callArtifactPayload.pending_approval
      ),
  );
  const frozen = Boolean(
    interactionStatus &&
    interactionStatus !== 'pending' &&
    interactionStatus !== 'none',
  );
  const feedbackContext: InteractiveFeedbackContext = {
    toolName: call.name,
    toolArguments: call.arguments || '{}',
  };

  useEffect(() => {
    let active = true;
    queueMicrotask(() => {
      if (!active) return;
      setWidgetState(artifact?.widget_state ?? {});
      setInteractionStatus(artifact?.interaction_state?.status);
      setInteractionResult(artifact?.interaction_state?.result ?? {});
    });
    return () => {
      active = false;
    };
  }, [artifact]);

  useEffect(() => {
    if (!hitlRequestId) return;
    let cancelled = false;
    void (async () => {
      try {
        const token = useAuthStore.getState().token;
        const base = getApiBase();
        const response = await fetch(`${base}/api/v1/hitl-requests/${hitlRequestId}`, {
          headers: {
            ...(token ? { Authorization: `Bearer ${token}` } : {}),
            Accept: 'application/json',
          },
        });
        if (!response.ok) return;
        const data = await response.json() as {
          status?: string;
          interaction_result_json?: Record<string, unknown>;
          is_interacted?: boolean;
        };
        if (cancelled) return;
        setInteractionStatus(data.status || (data.is_interacted ? 'submitted' : 'pending'));
        setInteractionResult(data.interaction_result_json ?? {});
      } catch {
        // Best-effort history hydration. The card remains usable if this fails.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [hitlRequestId, reconcileEpoch]);

  if (artifact?.component_type === 'user_input' && artifact.props?.mode === 'choices' && !renderError) {
    return <ChoicesCard key={artifact.artifact_id} artifact={artifact} />;
  }

  return (
    <div
      className="flex items-start justify-start gap-3"
      data-message-role="assistant"
      data-tool-name={call.name}
      data-role="interactive-artifact"
      data-preview-render-state={isFilePreviewSummary ? 'summary' : undefined}
      data-file-preview-type={isFilePreviewSummary ? fileSummaryAppearance.type : undefined}
    >
      {!compact && (showAvatar ? <MessageAvatar label="A" tone="agent" /> : <div className="h-9 w-9 shrink-0" />)}
      <div
        className={cn(
          'w-full min-w-0',
          isFilePreviewSummary
            ? compact
              ? 'max-w-[94%]'
              : 'max-w-[30rem]'
            : compact
              ? 'max-w-[94%]'
              : 'max-w-[82%]',
        )}
        data-message-content-rail="assistant"
      >
        <div className="overflow-hidden rounded-xl border border-edge-structural bg-surface-raised shadow-sm">
          <div className={cn(
            'flex items-center gap-3 px-4 py-3',
            isFilePreviewSummary
              ? 'bg-surface-raised'
              : 'border-b border-edge-subtle bg-surface-sunken/45',
          )}>
            {isFilePreviewSummary ? (
              <span className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-xl ${fileSummaryAppearance.tone}`}>
                <FileSummaryIcon className="h-5 w-5" aria-hidden="true" />
              </span>
            ) : null}
            <div className="min-w-0 flex-1">
              <div className="truncate text-sm font-semibold leading-5 text-content-primary">
                {isFilePreviewSummary
                  ? fileSummaryName
                  : isPreToolApproval && artifact
                  ? t('hitl.approval_title', 'Approve {{tool}}', {
                      tool: stringFrom(
                        asArray(artifact.props?.fields)
                          .filter(isObject)
                          .find((field) => stringFrom(field.name) === 'tool')?.value,
                        t('hitl.unknown_tool', 'Unknown tool'),
                      ),
                    })
                  : artifact?.title || t('tool.interactive.untitled', 'Interactive artifact')}
              </div>
              <div className="mt-0.5">
                {isFilePreviewSummary ? (
                  <span className="text-xs uppercase tracking-wide text-muted-foreground">
                    {fileSummaryAppearance.type}
                  </span>
                ) : !isPreToolApproval ? <StatusLine call={call} /> : null}
              </div>
            </div>
            {standaloneWorkflowHref ? (
              <a href={standaloneWorkflowHref} target="_blank" rel="noopener noreferrer"
                className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-edge-structural text-muted-foreground hover:bg-surface-hover hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                data-action="interactive-open-workflow-tab"
                title={t('tool.interactive.open_preview_tab', 'Open in a new Preview tab')}
                aria-label={t('tool.interactive.open_preview_tab', 'Open in a new Preview tab')}>
                <PreviewCornerIcon />
              </a>
            ) : canOpenInteractivePreview ? (
              <button
                type="button"
                className="flex h-8 w-8 items-center justify-center rounded-lg border border-edge-structural bg-background/85 text-muted-foreground transition-colors hover:bg-surface-hover hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                data-action="interactive-open-artifact-preview"
                title={t('tool.interactive.open_preview', 'Open in preview')}
                aria-label={t('tool.interactive.open_preview', 'Open in preview')}
                onClick={() => artifact && onOpenInteractivePreview?.(artifact)}
              >
                <PreviewCornerIcon />
              </button>
            ) : canOpenFilePreview ? (
              <button
                type="button"
                className="flex h-8 w-8 items-center justify-center rounded-lg border border-edge-structural bg-background/85 text-muted-foreground transition-colors hover:bg-surface-hover hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                data-action="interactive-open-file-preview"
                title={compact
                  ? t('tool.interactive.open_preview_tab', 'Open in a new Preview tab')
                  : t('tool.interactive.open_preview', 'Open in preview')}
                aria-label={compact
                  ? t('tool.interactive.open_preview_tab', 'Open in a new Preview tab')
                  : t('tool.interactive.open_preview', 'Open in preview')}
                onClick={() => onOpenFilePreview?.(filePreviewPath)}
              >
                <PreviewCornerIcon />
              </button>
            ) : null}
          </div>
          {!isFilePreviewSummary ? (
            <div
              className="overflow-auto bg-surface-work"
              style={{ maxHeight: height }}
              data-role="interactive-artifact-body"
            >
              {renderError ? (
                <InteractiveRenderFailure detail={renderError} feedbackContext={feedbackContext} />
              ) : artifact ? (
                standaloneWorkflowHref ? (
                  <a href={standaloneWorkflowHref} target="_blank" rel="noopener noreferrer" className="flex w-full items-center justify-between gap-3 p-4 text-sm hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                    <span>{stringFrom(artifact.props?.version)}</span>
                    <span>{t('tool.interactive.open_preview_tab', 'Open in a new Preview tab')}</span>
                  </a>
                ) : artifact.component_type === 'workflow_preview' && canOpenInteractivePreview ? (
                  <button type="button" className="flex w-full items-center justify-between gap-3 p-4 text-left text-sm hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    onClick={() => onOpenInteractivePreview?.(artifact)}>
                    <span>{stringFrom(artifact.props?.version)}</span>
                    <span>{t('tool.interactive.open_preview', 'Open in preview')}</span>
                  </button>
                ) : isPreToolApproval ? (
                  <PreToolApprovalBody artifact={artifact} />
                ) : (
                  <InteractiveRenderBoundary
                    key={JSON.stringify(artifact.widget_state ?? {})}
                    fallback={(detail) => <InteractiveRenderFailure detail={detail} feedbackContext={feedbackContext} />}
                  >
                    <ArtifactRenderer
                      artifact={artifact}
                      previewOnly={previewOnly}
                      onWidgetStateChange={setWidgetState}
                      onOpenFilePreview={onOpenFilePreview}
                      frozen={frozen}
                      feedbackContext={feedbackContext}
                      stableSession={isStableRenderOnly}
                    />
                  </InteractiveRenderBoundary>
                )
              ) : (
                <InteractiveRenderFailure detail="Artifact data is missing." feedbackContext={feedbackContext} />
              )}
            </div>
          ) : null}
          {artifact && !renderError && !isFilePreviewSummary && (
            <SubmitControls
              completionMode={artifact.completion_mode ?? 'render_only'}
              schema={
                isPreToolApproval
                  ? {
                      ...(artifact.interaction_schema ?? {}),
                      submit_label: t('hitl.approve', 'Approve'),
                      cancel_label: t('hitl.deny', 'Deny'),
                    }
                  : artifact.interaction_schema ?? {}
              }
              hitlRequestId={hitlRequestId}
              artifactId={artifact.artifact_id ?? null}
              widgetState={widgetState}
              initialStatus={interactionStatus}
              initialResult={interactionResult}
              submitDecision={isPreToolApproval ? 'approve' : 'submit'}
              cancelDecision={isPreToolApproval ? 'deny' : 'cancel'}
              resolvedPositiveLabel={
                isPreToolApproval
                  ? t('hitl.approved', 'Approved')
                  : artifact.interaction_schema?.interaction_type === 'continue'
                    ? t('tool.interactive.continued', 'Continued')
                    : undefined
              }
              resolvedNegativeLabel={
                isPreToolApproval && interactionStatus === 'denied'
                  ? t('hitl.denied', 'Denied') : undefined
              }
              onResolved={(status, result) => {
                setInteractionStatus(status);
                setInteractionResult(result);
              }}
              onSubmitAsNewMessage={onSubmitAsNewMessage}
            />
          )}
          {parsed.ref && parsed.previewOnly && !hydratedArtifact && (
            <div className="border-t px-3 py-2 text-xs text-muted-foreground">
              {t('tool.interactive.offloaded', 'Full artifact stored at {{path}}', { path: parsed.ref })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
