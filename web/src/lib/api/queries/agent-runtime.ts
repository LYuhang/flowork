import { useQuery } from '@tanstack/react-query';

import { getAgentRuntimeCapabilities } from '@/lib/api/agent-runtime';

export const runtimeCapabilitiesPrefix = [
  'agent-runtime',
  'capabilities',
] as const;

export const codexAccountUsageQueryKey = [
  'agent-runtime',
  'codex',
  'account-usage',
] as const;

export const runtimeCapabilitiesKey = (chatId?: string | null) => [
  ...runtimeCapabilitiesPrefix,
  chatId ?? 'new-chat',
] as const;

export function useAgentRuntimeCapabilities(
  chatId?: string | null,
  options?: { enabled?: boolean; projectId?: string | null; persisted?: boolean },
) {
  // An existing Chat already resolves its Project on the server. Adding the
  // asynchronously loaded project id changes the key mid-request and fetches
  // the exact same capabilities twice on refresh. Drafts still need a Project
  // key: their selected connection/model can change before materialization.
  const projectId = options?.persisted ? undefined : options?.projectId;
  return useQuery({
    queryKey: projectId
      ? [...runtimeCapabilitiesKey(chatId), projectId]
      : runtimeCapabilitiesKey(chatId),
    queryFn: () => getAgentRuntimeCapabilities(chatId, projectId),
    enabled: options?.enabled ?? true,
    staleTime: 30_000,
    // Global Settings may be changed in another tab. Existing chats remain
    // protected by their Project Runtime binding. A new Chat inherits its
    // Project source/model; only an unbound Project uses the global default.
    refetchOnWindowFocus: 'always',
  });
}
