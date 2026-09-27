import type { MergedMessage, MergedToolCall } from '@/components/agent-sidebar/types';
import { parseEnvelope } from '@/components/agent-sidebar/tool-render/parseEnvelope';

function isObject(x: unknown): x is Record<string, unknown> {
  return typeof x === 'object' && x !== null && !Array.isArray(x);
}

function hasInteractiveProjection(value: unknown): boolean {
  if (!isObject(value)) return false;
  if (value.kind === 'interactive_artifact') return true;
  const artifact = value.artifact;
  if (isObject(artifact) && artifact.kind === 'interactive_artifact') return true;
  const payload = value.payload;
  return isObject(payload) && (
    payload.kind === 'interactive_artifact'
    || isObject(payload.artifact) && payload.artifact.kind === 'interactive_artifact'
    || isObject(payload.artifact_preview)
  );
}

function isPendingPreToolApproval(value: unknown): boolean {
  if (!isObject(value)) return false;
  const meta = isObject(value.meta) ? value.meta : null;
  const payload = isObject(value.payload) ? value.payload : null;
  return (
    meta?.hitl_type === 'pre_tool_approval' &&
    meta.pending_approval === true
  ) || (
    payload?.hitl_type === 'pre_tool_approval' &&
    payload.pending_approval === true
  );
}

export function isInteractiveArtifactCall(call: MergedToolCall): boolean {
  // Pre-tool authorization is intentionally unfinished: the tool must remain
  // `running` while the backend control loop waits for a durable user
  // decision. Its explicit HITL projection is nevertheless a standalone card.
  if (
    call.status === 'running' &&
    isPendingPreToolApproval(call.artifact) &&
    hasInteractiveProjection(call.artifact)
  ) {
    return true;
  }
  const artifactStatus = isObject(call.artifact) && typeof call.artifact.status === 'string'
    ? call.artifact.status
    : '';
  const resultStatus = parseEnvelope(call.result)?.status ?? '';
  if (artifactStatus === 'error' || resultStatus === 'error') return false;
  const backendSucceeded = artifactStatus === 'success' || resultStatus === 'success';
  // Backend success owns the transcript shape. The streaming projection can
  // still label the call `running` when the terminal artifact/result arrives
  // before a separate status frame. Do not hide a Continue gate inside the
  // generic collapsed tool group during that window (or forever after a
  // terminal history merge that preserves the older status).
  if (call.status !== 'done' && !backendSucceeded) return false;
  if (!backendSucceeded) return false;
  return call.name === 'render_preview'
    || call.name === 'render_interactive'
    || call.name === 'render_url_preview'
    || hasInteractiveProjection(call.artifact)
    || hasInteractiveProjection(parseEnvelope(call.result)?.output?.data);
}

export type RenderItem =
  | { kind: 'message'; message: MergedMessage; index: number; showAvatar: boolean }
  | { kind: 'tool_group'; calls: MergedToolCall[]; startIndex: number; endIndex: number; showAvatar: boolean }
  | { kind: 'interactive_artifact'; call: MergedToolCall; index: number; showAvatar: boolean };

type RenderItemWithoutAvatar =
  | { kind: 'message'; message: MergedMessage; index: number }
  | { kind: 'tool_group'; calls: MergedToolCall[]; startIndex: number; endIndex: number }
  | { kind: 'interactive_artifact'; call: MergedToolCall; index: number };

export function groupToolActivity(messages: MergedMessage[]): RenderItem[] {
  const items: RenderItemWithoutAvatar[] = [];
  let pendingCalls: MergedToolCall[] = [];
  let pendingStart = -1;
  let pendingEnd = -1;

  const flushTools = () => {
    if (pendingCalls.length === 0) return;
    items.push({
      kind: 'tool_group',
      calls: pendingCalls,
      startIndex: pendingStart,
      endIndex: pendingEnd,
    });
    pendingCalls = [];
    pendingStart = -1;
    pendingEnd = -1;
  };

  for (let i = 0; i < messages.length; i += 1) {
    const message = messages[i];
    if (message.role === 'user') {
      flushTools();
      if (message.content.length > 0) {
        items.push({ kind: 'message', message: { ...message, tool_calls: [] }, index: i });
      }
      continue;
    }

    if (message.content.length > 0) {
      flushTools();
      items.push({
        kind: 'message',
        message: { ...message, tool_calls: [] },
        index: i,
      });
    }

    for (const call of message.tool_calls) {
      if (isInteractiveArtifactCall(call)) {
        flushTools();
        items.push({ kind: 'interactive_artifact', call, index: i });
        continue;
      }
      if (pendingCalls.length === 0) pendingStart = i;
      pendingEnd = i;
      pendingCalls.push(call);
    }
  }
  flushTools();
  return withAgentAvatars(items);
}

function withAgentAvatars(items: RenderItemWithoutAvatar[]): RenderItem[] {
  const out: RenderItem[] = [];
  let previousAgentKind: 'message' | 'tool_group' | 'interactive_artifact' | null = null;
  let groupStartedWithLeadingTools = false;
  for (const item of items) {
    if (item.kind === 'message' && item.message.role === 'user') {
      previousAgentKind = null;
      groupStartedWithLeadingTools = false;
      out.push({ ...item, showAvatar: true });
      continue;
    }

    if (
      item.kind === 'tool_group'
      || item.kind === 'interactive_artifact'
    ) {
      const showAvatar = previousAgentKind === null;
      if (showAvatar) groupStartedWithLeadingTools = true;
      out.push({ ...item, showAvatar });
      previousAgentKind = item.kind;
      continue;
    }

    const continuesLeadingToolGroup =
      (
        previousAgentKind === 'tool_group'
        || previousAgentKind === 'interactive_artifact'
      ) &&
      groupStartedWithLeadingTools;
    const showAvatar = !continuesLeadingToolGroup;
    out.push({ ...item, showAvatar });
    previousAgentKind = 'message';
    groupStartedWithLeadingTools = false;
  }
  return out;
}
