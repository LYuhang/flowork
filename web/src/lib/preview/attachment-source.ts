import type { ChatAttachment } from '@/components/agent-sidebar/chat-attachments';
import { originChatHref, type PreviewOrigin } from './context-origin';
import { standalonePreviewHref, standalonePreviewTarget, standaloneWorkflowPreviewHref } from './standalone-preview';

/** Persistent coordinates only; never use signed content URLs or blob URLs. */
export function attachmentSourceHref(attachment: ChatAttachment, origin?: PreviewOrigin | null): string | null {
  const source = attachment.type === 'quote' ? attachment.source
    : 'resource' in attachment ? attachment.resource : null;
  if (!source) return null;
  switch (source.kind) {
    case 'message': return `${originChatHref(source.chat_id)}?focusMessage=${encodeURIComponent(source.message_id)}`;
    case 'artifact': return originChatHref(source.chat_id);
    case 'job': return `${originChatHref(source.chat_id)}?focusJob=${encodeURIComponent(source.job_id)}`;
    case 'file': {
      const file = source.file_ref;
      const query = new URLSearchParams({ scope: file.scope, path: file.path });
      if (file.scope === 'project') query.set('projectId', file.projectId);
      if (file.scope === 'run') query.set('runId', file.runId);
      const target = standalonePreviewTarget(query);
      if (!target) return null;
      const href = standalonePreviewHref(target.fileRef, 'auto', origin);
      const selector = attachment.type === 'resource' ? attachment.selector : null;
      const page = selector?.kind === 'pages' ? selector.pages[0] : null;
      return href + (source.revision ? `&expectedRevision=${encodeURIComponent(source.revision)}` : '')
        + (page ? `&page=${page}` : '');
    }
    case 'workflow': {
      const href = standaloneWorkflowPreviewHref(source.workflow_id, source.version, origin);
      const selector = attachment.type === 'resource' ? attachment.selector : null;
      return selector?.kind === 'workflow_elements'
        ? `${href}&focus=${encodeURIComponent(JSON.stringify({ nodeIds: selector.node_ids, edges: selector.edges }))}` : href;
    }
    case 'web': {
      try {
        const url = new URL(source.url);
        return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : null;
      } catch { return null; }
    }
  }
}
