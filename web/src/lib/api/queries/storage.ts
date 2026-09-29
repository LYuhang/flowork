import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  deleteStorage,
  listStorage,
  mkdirStorage,
  readStorage,
  renameStorage,
  uploadStorageFile,
  writeStorageContent,
} from '@/lib/api/storage';

export const storageKeys = {
  all: ['storage'] as const,
  list: (path: string, search: string, sort: string, cursor: string | null) =>
    ['storage', 'list', path, search, sort, cursor] as const,
  content: (path: string | null) => ['storage', 'content', path] as const,
};

export function useStorageList(args: {
  path: string;
  search: string;
  sort: string;
  cursor: string | null;
}) {
  return useQuery({
    queryKey: storageKeys.list(args.path, args.search, args.sort, args.cursor),
    queryFn: () =>
      listStorage({
        path: args.path,
        search: args.search,
        sort: args.sort,
        cursor: args.cursor,
        limit: 100,
      }),
  });
}

export function useStorageContent(path: string | null) {
  return useQuery({
    queryKey: storageKeys.content(path),
    queryFn: () => readStorage(path as string),
    enabled: !!path,
  });
}

export function useUploadStorageFile(path: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (file: File) => uploadStorageFile(path, file),
    onSuccess: () => void qc.invalidateQueries({ queryKey: storageKeys.all }),
  });
}

export function useMkdirStorage() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (path: string) => mkdirStorage(path),
    onSuccess: () => void qc.invalidateQueries({ queryKey: storageKeys.all }),
  });
}

export function useDeleteStorage() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (path: string) => deleteStorage(path),
    onSuccess: () => void qc.invalidateQueries({ queryKey: storageKeys.all }),
  });
}

export function useRenameStorage() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (args: { old_path: string; new_path: string }) => renameStorage(args),
    onSuccess: () => void qc.invalidateQueries({ queryKey: storageKeys.all }),
  });
}

export function useWriteStorageContent() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (args: { path: string; content: string; content_type?: string }) =>
      writeStorageContent(args),
    onSuccess: async (data, vars) => {
      // A completed save is authoritative. Keep the editor visible, and prevent
      // an older in-flight read from replacing the newly saved content.
      await qc.cancelQueries({ queryKey: storageKeys.content(vars.path), exact: true });
      qc.setQueryData(storageKeys.content(vars.path), {
        path: data.path,
        content: vars.content,
        content_type: data.content_type,
        size_bytes: data.size_bytes,
        truncated: false,
      });
      void qc.invalidateQueries({ queryKey: ['storage', 'list'] });
    },
  });
}
