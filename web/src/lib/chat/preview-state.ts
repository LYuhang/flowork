import type { InteractiveArtifact } from '@/components/agent-sidebar/tool-render/interactive-artifact-contract';
import type { FileRefV1, PreviewResourceRefV1 } from '@/lib/preview/protocol';

export type ChatPreviewItem =
  | {
      id: string;
      title: string;
      resource: Extract<PreviewResourceRefV1, { kind: 'workflow' }>;
    }
  | {
      id: string;
      title: string;
      resource: Extract<PreviewResourceRefV1, { kind: 'file' }>;
    }
  | {
      id: string;
      title: string;
      resource: Extract<PreviewResourceRefV1, { kind: 'interactive' }>;
      artifact: InteractiveArtifact;
    }
  | {
      id: string;
      title: string;
      resource: Extract<PreviewResourceRefV1, { kind: 'background_jobs' }>;
    };

export function filePreviewItem(fileRef: FileRefV1, title: string): ChatPreviewItem {
  const owner = fileRef.scope === 'project'
    ? fileRef.projectId
    : fileRef.scope === 'run'
      ? fileRef.runId
      : 'self';
  return {
    id: `file:${fileRef.scope}:${owner}:${fileRef.path}`,
    title,
    resource: { schemaVersion: 1, kind: 'file', fileRef },
  };
}

/** Restore non-persisted artifacts from history without discarding open file tabs. */
export function previewItemToRestore(
  items: ChatPreviewItem[],
  resources: ChatPreviewItem[],
  activeId: string | null,
): ChatPreviewItem | null {
  if (items.some((item) => item.id === activeId)) return null;
  return resources.find((item) => item.id === activeId)
    ?? (items.length === 0 ? resources[0] ?? null : null);
}

export interface ChatViewState {
  explorerOpen: boolean;
  debugOpen: boolean;
  previewOpen: boolean;
  todoCollapsed: boolean;
  activePreviewId: string | null;
  previewItems: ChatPreviewItem[];
}

export type ChatViewPreferences = Pick<
  ChatViewState,
  | 'explorerOpen'
  | 'debugOpen'
  | 'previewOpen'
  | 'todoCollapsed'
  | 'activePreviewId'
  | 'previewItems'
>;

const MAX_PERSISTED_FILE_PREVIEWS = 20;

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function persistedFilePreviewItem(value: unknown): ChatPreviewItem | null {
  if (!object(value) || typeof value.title !== 'string' || value.title.length > 240) return null;
  const resource = value.resource;
  if (!object(resource) || resource.schemaVersion !== 1 || resource.kind !== 'file') return null;
  const fileRef = resource.fileRef;
  if (!object(fileRef) || fileRef.schemaVersion !== 1 || typeof fileRef.path !== 'string') return null;
  if (
    fileRef.scope === 'project'
    && typeof fileRef.projectId === 'string'
    && fileRef.projectId.length > 0
    && (
      fileRef.path.startsWith('/data/')
      || fileRef.path.startsWith('/memory/')
      || fileRef.path.startsWith('/logs/')
      || fileRef.path.startsWith('/chats/')
    )
  ) {
    return filePreviewItem({
      schemaVersion: 1,
      scope: 'project',
      projectId: fileRef.projectId,
      path: fileRef.path as Extract<FileRefV1, { scope: 'project' }>['path'],
    }, value.title);
  }
  if (fileRef.scope === 'mount' && fileRef.path.startsWith('/mount/')) {
    return filePreviewItem({
      schemaVersion: 1,
      scope: 'mount',
      path: fileRef.path as `/mount/${string}`,
    }, value.title);
  }
  if (
    fileRef.scope === 'run'
    && typeof fileRef.runId === 'string'
    && fileRef.runId.length > 0
    && fileRef.path.startsWith('/run/')
  ) {
    return filePreviewItem({
      schemaVersion: 1,
      scope: 'run',
      runId: fileRef.runId,
      path: fileRef.path as `/run/${string}`,
    }, value.title);
  }
  return null;
}

export const EMPTY_CHAT_VIEW_STATE: ChatViewState = {
  explorerOpen: false,
  debugOpen: false,
  previewOpen: false,
  todoCollapsed: false,
  activePreviewId: null,
  previewItems: [],
};

export function readChatViewPreferences(storageKey: string): ChatViewPreferences | null {
  if (typeof window === 'undefined') return null;
  try {
    const parsed = JSON.parse(window.localStorage.getItem(storageKey) ?? 'null') as Record<string, unknown> | null;
    if (!parsed) return null;
    const previewItems = Array.isArray(parsed.previewItems)
      ? parsed.previewItems
        .slice(-MAX_PERSISTED_FILE_PREVIEWS)
        .map(persistedFilePreviewItem)
        .filter((item): item is ChatPreviewItem => item !== null)
      : [];
    return {
      explorerOpen: parsed.explorerOpen === true,
      debugOpen: parsed.debugOpen === true,
      previewOpen: parsed.previewOpen === true,
      todoCollapsed: parsed.todoCollapsed === true,
      activePreviewId: typeof parsed.activePreviewId === 'string' ? parsed.activePreviewId : null,
      previewItems,
    };
  } catch {
    window.localStorage.removeItem(storageKey);
    return null;
  }
}

export function writeChatViewPreferences(storageKey: string, state: ChatViewState): void {
  if (typeof window === 'undefined') return;
  try {
    const preferences: ChatViewPreferences = {
      explorerOpen: state.explorerOpen,
      debugOpen: state.debugOpen,
      previewOpen: state.previewOpen,
      todoCollapsed: state.todoCollapsed,
      activePreviewId: state.activePreviewId,
      previewItems: state.previewItems
        .filter((item) => item.resource.kind === 'file')
        .slice(-MAX_PERSISTED_FILE_PREVIEWS),
    };
    window.localStorage.setItem(storageKey, JSON.stringify(preferences));
  } catch {
    // View state remains available for the current runtime.
  }
}
