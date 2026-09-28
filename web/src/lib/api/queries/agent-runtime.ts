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
  options?: { enabled?: boolean; projectId?: string | null },
) {
  return useQuery({
    queryKey: options?.projectId
      ? [...runtimeCapabilitiesKey(chatId), options.projectId]
      : runtimeCapabilitiesKey(chatId),
    queryFn: () => getAgentRuntimeCapabilities(chatId, options?.projectId),
    enabled: options?.enabled ?? true,
    staleTime: 30_000,
    // Global Settings may be changed in another tab. Existing chats remain
    // protected by their Project Runtime binding. A new Chat inherits its
    // Project source/model; only an unbound Project uses the global default.
    refetchOnWindowFocus: 'always',
  });
}
