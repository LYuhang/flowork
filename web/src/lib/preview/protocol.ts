export type ProjectFileRefV1 = {
  schemaVersion: 1;
  scope: 'project';
  projectId: string;
  path: `/${'data' | 'memory' | 'logs' | 'chats'}/${string}`;
};

export type MountFileRefV1 = {
  schemaVersion: 1;
  scope: 'mount';
  path: `/mount/${string}`;
};

export type RunFileRefV1 = {
  schemaVersion: 1;
  scope: 'run';
  runId: string;
  path: `/run/${string}`;
};

export type FileRefV1 = ProjectFileRefV1 | MountFileRefV1 | RunFileRefV1;

export type PreviewResourceRefV1 =
  | { schemaVersion: 1; kind: 'file'; fileRef: FileRefV1 }
  | { schemaVersion: 1; kind: 'interactive'; artifactId: string }
  | {
      schemaVersion: 1;
      kind: 'background_jobs';
      chatId: string;
      jobId?: string;
      deliveryBatchId?: string;
    }
  | { schemaVersion: 1; kind: 'workflow'; workflowId: string };

export type PreviewRendererId =
  | 'text'
  | 'markdown'
  | 'html'
  | 'pdf'
  | 'docx'
  | 'pptx'
  | 'spreadsheet'
  | 'image'
  | 'audio'
  | 'video'
  | 'drawio'
  | 'unsupported';

export interface DrawioIssueV1 {
  severity: 'error' | 'warning' | 'info';
  stage: 'schema' | 'semantic';
  code: string;
  json_pointer: string;
  message: string;
}

export interface PreviewErrorInfo {
  code: string;
  params: Record<string, string | number | boolean>;
}

export interface PreviewDescriptorV1 {
  schemaVersion: 1;
  fileRef: FileRefV1;
  name: string;
  sizeBytes: number;
  contentType: string;
  detectedType: string;
  revision: string;
  renderer: PreviewRendererId;
  loadPolicy: 'inline' | 'stream' | 'range' | 'manual' | 'unsupported';
  capabilities: {
    preview: boolean;
    edit: boolean;
    download: boolean;
  };
  content?: {
    inlineText?: string | null;
    url?: string | null;
    truncated: boolean;
    rangeSupported: boolean;
  } | null;
  rendition?: {
    format: 'pdf';
    contentType: 'application/pdf';
    url: string;
    sourceRevision: string;
  } | null;
  text?: {
    encoding: 'utf-8';
    bom: boolean;
    newline: 'LF' | 'CRLF';
    mixedNewlines: boolean;
  } | null;
  diagram?: {
    status: 'valid' | 'invalid';
    format?: 'drawio';
    issues: DrawioIssueV1[];
    sourceHash?: string;
    summary?: {
      pages: number;
      cells: number;
      vertices: number;
      edges: number;
    };
  } | null;
  error?: PreviewErrorInfo | null;
}

export interface PreviewFileWriteOut {
  fileRef: FileRefV1;
  revision: string;
  sizeBytes: number;
  contentType: string;
}

export interface PreviewResourceSessionV1 {
  schemaVersion: 1;
  resourceMounts: Array<{
    pathPrefix: string;
    rootUrl: string;
  }>;
  baseUrl: string;
  expiresIn: number;
}

export function fileRefKey(fileRef: FileRefV1): string {
  switch (fileRef.scope) {
    case 'project':
      return `project:${fileRef.projectId}:${fileRef.path}`;
    case 'mount':
      return `mount:${fileRef.path}`;
    case 'run':
      return `run:${fileRef.runId}:${fileRef.path}`;
  }
}

/** A legacy Agent citation candidate, not a filesystem normalization rule.
 * Resolve the literal filename first: colons are valid in user filenames.
 * Only a missing text file may fall back to its positive line/column suffix.
 */
export function fileCitationTarget(fileRef: FileRefV1): {
  fileRef: FileRefV1;
  line: number;
  column?: number;
} | null {
  const match = /^(.*\.(?:md|mdx|txt|rst|py|js|jsx|ts|tsx|json|jsonl|yaml|yml|toml|sql|sh|css|html|xml|csv|tsv|log)):([1-9]\d*)(?::([1-9]\d*))?$/i.exec(fileRef.path);
  if (!match || match[1].split('/').some((segment) => segment === '.' || segment === '..')) return null;
  const line = Number(match[2]);
  const column = match[3] ? Number(match[3]) : undefined;
  if (!Number.isSafeInteger(line) || (column !== undefined && !Number.isSafeInteger(column))) return null;
  return {
    fileRef: { ...fileRef, path: match[1] } as FileRefV1,
    line,
    ...(column !== undefined ? { column } : {}),
  };
}


export function fileRefFromAgentPath(
  path: string,
  options: { projectId?: string | null; runId?: string | null },
): FileRefV1 | null {
  if (
    (path.startsWith('/data/') || path.startsWith('/memory/') || path.startsWith('/logs/') || path.startsWith('/chats/'))
    && options.projectId
  ) {
    return {
      schemaVersion: 1,
      scope: 'project',
      projectId: options.projectId,
      path: path as ProjectFileRefV1['path'],
    };
  }
  if (path.startsWith('/mount/')) {
    return {
      schemaVersion: 1,
      scope: 'mount',
      path: path as `/mount/${string}`,
    };
  }
  if (path.startsWith('/run/') && options.runId) {
    return {
      schemaVersion: 1,
      scope: 'run',
      runId: options.runId,
      path: path as `/run/${string}`,
    };
  }
  return null;
}

/**
 * Resolve a Markdown href emitted by an Agent back to its private workspace
 * path. These paths are VFS references, not browser routes: navigating to
 * `/data/report.pdf` would incorrectly leave the app (and also drop a dynamic
 * deployment prefix). HTTP(S), mail, anchors, and arbitrary relative links are
 * deliberately left to the browser.
 */
export function agentFilePathFromHref(href: string | undefined): string | null {
  if (!href) return null;
  const value = href.trim();
  if (!value.startsWith('/')) return null;

  // Query/fragment syntax belongs to the Markdown link, not the VFS path.
  const pathPart = value.split(/[?#]/, 1)[0];
  let decoded: string;
  try {
    decoded = decodeURIComponent(pathPart);
  } catch {
    return null;
  }
  if (
    !decoded.startsWith('/data/')
    && !decoded.startsWith('/memory/')
    && !decoded.startsWith('/logs/')
    && !decoded.startsWith('/chats/')
    && !decoded.startsWith('/mount/')
    && !decoded.startsWith('/run/')
  ) {
    return null;
  }
  // Keep Preview resolution inside the selected VFS root even when an Agent
  // emits a hand-written or percent-encoded path.
  if (decoded.split('/').some((segment) => segment === '.' || segment === '..')) {
    return null;
  }
  return decoded;
}
