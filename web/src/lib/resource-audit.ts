export interface ResourceAudit {
  skills: Array<{ id: string; name: string; revision_hash?: string }>;
  mcp_servers: Array<{ id: string; name: string; tools_fingerprint?: string }>;
  tools: Array<{ name: string; status: string; skill_paths?: string[]; exit_code?: number }>;
  truncated: boolean;
}
export function parseResourceAudit(value: unknown): ResourceAudit | undefined {
  if (!value || typeof value !== 'object') return undefined;
  const v = value as Record<string, unknown>;
  if (![v.skills, v.mcp_servers, v.tools].every(Array.isArray)) return undefined;
  const refs = (items: unknown[]) => items.filter((x): x is Record<string, unknown> => !!x && typeof x === 'object')
    .filter(x => typeof x.id === 'string' && typeof x.name === 'string').map(x => ({
      id: x.id as string, name: x.name as string,
      revision_hash: typeof x.revision_hash === 'string' ? x.revision_hash : undefined,
      tools_fingerprint: typeof x.tools_fingerprint === 'string' ? x.tools_fingerprint : undefined,
    }));
  return { skills: refs(v.skills as unknown[]), mcp_servers: refs(v.mcp_servers as unknown[]),
    tools: (v.tools as unknown[]).filter((x): x is Record<string, unknown> => !!x && typeof x === 'object')
      .filter(x => typeof x.name === 'string' && typeof x.status === 'string').slice(0, 256).map(x => ({
        name: x.name as string, status: x.status as string,
        skill_paths: Array.isArray(x.skill_paths) ? x.skill_paths.filter((p): p is string => typeof p === 'string') : [],
        exit_code: typeof x.exit_code === 'number' ? x.exit_code : undefined,
      })), truncated: v.truncated === true };
}
