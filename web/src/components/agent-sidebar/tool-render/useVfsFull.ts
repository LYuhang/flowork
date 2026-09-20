/**
 * Lazy "View full" loader for a tool output's VFS body.
 *
 * The envelope's inline `output.data` is the backend head+tail (or absent
 * when large). The *full* body lives at `output.path` in the VFS. This hook
 * fetches it via the existing `readVfs` client (`GET /api/v1/vfs/content`,
 * byte-bounded at `VFS_HTTP_MAX_BYTES = 256_000`) — ON DEMAND only, never on
 * mount. The renderers call `load()` from a button click.
 *
 * Fail-soft: a fetch error is surfaced as `error` (a readable message), never
 * thrown, so the chat keeps rendering.
 */
import { useCallback, useState } from 'react';
import { readVfs } from '@/lib/api/vfs';
import type { VfsReadOut } from '@/lib/api/vfs';

export interface VfsFullState {
  /** The fetched body (null until a successful load). */
  content: string | null;
  /** True when the backend truncated the response at the 256 KB cap. */
  truncated: boolean;
  loading: boolean;
  /** Readable error message (null when none). */
  error: string | null;
  /** Trigger the fetch (idempotent-ish; re-fetches on each call). */
  load: () => void;
}

export function useVfsFull(
  wfId: string | undefined,
  path: string | undefined,
): VfsFullState {
  const [content, setContent] = useState<string | null>(null);
  const [truncated, setTruncated] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    if (!path) {
      setError('No file path for this output.');
      return;
    }
    setLoading(true);
    setError(null);
    readVfs({ path, wf_id: wfId })
      .then((out: VfsReadOut) => {
        setContent(out.content ?? null);
        setTruncated(!!out.truncated);
      })
      .catch((e: unknown) => {
        setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        setLoading(false);
      });
  }, [wfId, path]);

  return { content, truncated, loading, error, load };
}
