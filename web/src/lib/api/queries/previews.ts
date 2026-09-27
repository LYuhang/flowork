import {
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query';
import { useCallback } from 'react';

import { resolvePreview, writePreviewFile } from '@/lib/api/previews';
import { usePreviewFileEvents } from '@/lib/api/sse/preview-file-events';
import {
  fileRefKey,
  type FileRefV1,
  type PreviewDescriptorV1,
} from '@/lib/preview/protocol';

export const previewDescriptorQueryKey = (fileRef: FileRefV1) =>
  ['preview', 'descriptor', fileRefKey(fileRef)] as const;

export function usePreviewDescriptor(fileRef: FileRefV1) {
  const queryClient = useQueryClient();
  const queryKey = previewDescriptorQueryKey(fileRef);
  const queryKeyToken = fileRefKey(fileRef);
  const reconcile = useCallback((event: { revision: string | null }) => {
    const current = queryClient.getQueryData<PreviewDescriptorV1>(queryKey);
    // A successful Save has already installed this exact revision and content
    // locally. Ignore its eventual durable VFS event in this Preview instance;
    // another browser instance still receives and applies the same event.
    if (event.revision && current?.revision === event.revision) return;
    void queryClient.invalidateQueries({ queryKey, exact: true });
    // queryKeyToken is the stable identity represented by queryKey.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [queryClient, queryKeyToken]);
  const query = useQuery({
    queryKey,
    queryFn: ({ signal }) => resolvePreview(fileRef, signal),
    // Descriptor media URLs are signed for five minutes. Mark the descriptor
    // stale before that boundary so returning to an already-open Preview tab
    // refreshes its URL instead of attempting an expired one.
    staleTime: 4 * 60 * 1000,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    retry: false,
  });
  // A legacy line citation may resolve to a different literal filename.
  // Observe edits to the actual resource, not the citation string.
  usePreviewFileEvents(query.data?.fileRef ?? fileRef, reconcile);
  return query;
}

export function useWritePreviewFile(fileRef: FileRefV1) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (args: {
      expectedRevision: string;
      contentType: string;
      content: string;
    }) => writePreviewFile({ fileRef, ...args }),
    onSuccess: (result, variables) => {
      queryClient.setQueryData<PreviewDescriptorV1>(
        previewDescriptorQueryKey(fileRef),
        (current) => current
          ? {
              ...current,
              revision: result.revision,
              sizeBytes: result.sizeBytes,
              contentType: result.contentType,
              content: {
                ...current.content,
                inlineText: variables.content,
                truncated: false,
                rangeSupported: current.content?.rangeSupported ?? false,
              },
            }
          : current,
      );
    },
  });
}
