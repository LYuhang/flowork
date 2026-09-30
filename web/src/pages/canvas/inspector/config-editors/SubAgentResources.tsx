import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ExternalLink, RefreshCw, X } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { useSkills } from '@/lib/api/queries/skills';
import { useMcpServers } from '@/lib/api/queries/mcp-servers';

export interface SkillReference { id: string; name: string }
export interface McpReference { id: string; name: string }

export function SubAgentResources({ skills, servers, readOnly, onSkills, onServers }: {
  skills: SkillReference[]; servers: McpReference[]; readOnly?: boolean;
  onSkills: (value: SkillReference[]) => void; onServers: (value: McpReference[]) => void;
}) {
  const { t } = useTranslation();
  const skillQuery = useSkills({ refreshOnReturn: true });
  const mcpQuery = useMcpServers({ refreshOnReturn: true });
  const [skillSearch, setSkillSearch] = useState('');
  const [mcpSearch, setMcpSearch] = useState('');
  const includes = (query: string, name: string, description: string) => `${name} ${description}`.toLocaleLowerCase().includes(query.toLocaleLowerCase());
  return <div className="space-y-4" data-testid="subagent-resources">
    <section className="space-y-2 rounded-lg border border-edge-subtle p-3">
      <div className="flex items-center justify-between gap-2"><h3 className="text-sm font-medium">Skills · {skills.length}</h3><Button variant="ghost" size="icon-sm" aria-label={t('subagent.resources.refreshSkills', 'Refresh Skills')} onClick={() => void skillQuery.refetch()} disabled={skillQuery.isFetching}><RefreshCw /></Button></div>
      <p className="text-xs text-muted-foreground">{t('subagent.resources.skillHint', 'Selected instructions are introduced in the prompt. Each run uses the latest published content. Other authorized Skills remain readable in the shared, read-only folder.')}</p>
      {skills.map(ref => {
        const current = skillQuery.data?.find(item => item.id === ref.id);
        const unavailable = skillQuery.isSuccess && (!current?.revision_hash || !current.access.capabilities.includes('use'));
        return <div key={ref.id} className="rounded-md bg-surface-sunken p-2 text-xs">
          <div className="flex min-w-0 items-center gap-2"><span className="min-w-0 flex-1 break-words">{current?.name ?? ref.name}</span><a href={`/skills/${encodeURIComponent(ref.id)}`} target="_blank" rel="noreferrer" aria-label={`${t('subagent.resources.details', 'Resource details')}: ${ref.name}`}><ExternalLink className="size-3.5" /></a><Button variant="ghost" size="icon-sm" disabled={readOnly} aria-label={`${t('remove', 'Remove')}: ${ref.name}`} onClick={() => onSkills(skills.filter(item => item.id !== ref.id))}><X /></Button></div>
          {unavailable && <p role="alert" className="text-destructive">{t('subagent.resources.unavailable', 'Resource missing or unavailable. Remove it or restore access before running.')}</p>}
        </div>;
      })}
      <Input aria-label={t('subagent.resources.searchSkills', 'Search Skills')} placeholder={t('subagent.resources.searchSkills', 'Search Skills')} value={skillSearch} onChange={event => setSkillSearch(event.target.value)} className="h-8 text-xs" />
      {skillQuery.isPending ? <p role="status" className="text-xs">{t('loading', 'Loading…')}</p> : skillQuery.isError ? <p role="alert" className="text-xs text-destructive">{t('subagent.resources.loadError', 'Could not load resources. Use Refresh to retry.')}</p> : <div className="max-h-56 space-y-1 overflow-y-auto">
        {(skillQuery.data ?? []).filter(item => includes(skillSearch, item.name, item.description)).map(item => {
          const selected = skills.some(ref => ref.id === item.id);
          const usable = item.access.capabilities.includes('use') && !!item.revision_hash;
          return <label key={item.id} className="flex cursor-pointer items-start gap-2 rounded-md p-2 hover:bg-muted"><input type="checkbox" className="mt-0.5 size-4 shrink-0 accent-primary" checked={selected} disabled={readOnly || (!selected && !usable)} onChange={event => onSkills(event.target.checked ? [...skills, { id: item.id, name: item.name }] : skills.filter(ref => ref.id !== item.id))} /><span className="min-w-0 text-xs"><span className="block break-words font-medium">{item.name} · v{item.version}</span><span className="line-clamp-2 break-words text-muted-foreground">{item.description}</span>{!usable && <span className="block text-muted-foreground">{t('subagent.resources.cannotUse', 'Not available for execution')}</span>}</span></label>;
        })}
        {skillQuery.data?.length === 0 && <p className="text-xs text-muted-foreground">{t('subagent.resources.noSkills', 'No installed Skills. Add one on the Skills page.')}</p>}
      </div>}
    </section>
    <section className="space-y-2 rounded-lg border border-edge-subtle p-3">
      <div className="flex items-center justify-between gap-2"><h3 className="text-sm font-medium">MCP · {servers.length}</h3><Button variant="ghost" size="icon-sm" aria-label={t('subagent.resources.refreshMcp', 'Refresh MCP servers')} onClick={() => void mcpQuery.refetch()} disabled={mcpQuery.isFetching}><RefreshCw /></Button></div>
      <p className="text-xs text-muted-foreground">{t('subagent.resources.mcpHint', 'Only selected servers provide tools to this node. Credentials stay on the platform.')}</p>
      {servers.map(ref => {
        const current = mcpQuery.data?.find(item => item.id === ref.id);
        const unavailable = mcpQuery.isSuccess && (!current?.enabled || !current.access?.capabilities.includes('use'));
        return <div key={ref.id} className="rounded-md bg-surface-sunken p-2 text-xs"><div className="flex min-w-0 items-center gap-2"><span className="min-w-0 flex-1 break-words">{current?.name ?? ref.name}</span><a href={`/mcp-servers/${encodeURIComponent(ref.id)}`} target="_blank" rel="noreferrer" aria-label={`${t('subagent.resources.details', 'Resource details')}: ${ref.name}`}><ExternalLink className="size-3.5" /></a><Button variant="ghost" size="icon-sm" disabled={readOnly} aria-label={`${t('remove', 'Remove')}: ${ref.name}`} onClick={() => onServers(servers.filter(item => item.id !== ref.id))}><X /></Button></div>{unavailable && <p role="alert" className="text-destructive">{t('subagent.resources.unavailable', 'Resource missing or unavailable. Remove it or restore access before running.')}</p>}</div>;
      })}
      <Input aria-label={t('subagent.resources.searchMcp', 'Search MCP servers')} placeholder={t('subagent.resources.searchMcp', 'Search MCP servers')} value={mcpSearch} onChange={event => setMcpSearch(event.target.value)} className="h-8 text-xs" />
      {mcpQuery.isPending ? <p role="status" className="text-xs">{t('loading', 'Loading…')}</p> : mcpQuery.isError ? <p role="alert" className="text-xs text-destructive">{t('subagent.resources.loadError', 'Could not load resources. Use Refresh to retry.')}</p> : <div className="max-h-64 space-y-2 overflow-y-auto">
        {(mcpQuery.data ?? []).filter(item => includes(mcpSearch, item.name, item.description ?? '')).map(item => {
          const selected = servers.some(ref => ref.id === item.id);
          const usable = item.enabled && item.access?.capabilities.includes('use');
          return <div key={item.id} className="rounded-md border border-edge-subtle p-2"><label className="flex cursor-pointer items-start gap-2"><input type="checkbox" className="mt-0.5 size-4 shrink-0 accent-primary" checked={selected} disabled={readOnly || (!selected && !usable)} onChange={event => onServers(event.target.checked ? [...servers, { id: item.id, name: item.name }] : servers.filter(ref => ref.id !== item.id))} /><span className="min-w-0 text-xs"><span className="block break-words font-medium">{item.name}</span><span className="line-clamp-2 break-words text-muted-foreground">{item.description}</span><span className="block text-muted-foreground">{item.transport} · {t('subagent.resources.toolCount', { count: item.last_tool_count ?? 0 })} · {item.last_handshake_status ?? '—'}</span></span></label><details className="mt-2 text-xs"><summary className="cursor-pointer text-muted-foreground">{t('subagent.resources.tools', 'Tool definitions')}</summary>{(item.last_tool_names ?? []).map(tool => <div key={tool.name} className="mt-2 min-w-0"><strong className="break-all">{tool.name}</strong><p className="break-words text-muted-foreground">{tool.description}</p><pre className="max-h-40 overflow-auto whitespace-pre-wrap break-words rounded bg-muted p-2">{JSON.stringify(tool.input_schema ?? {}, null, 2)}</pre></div>)}</details></div>;
        })}
        {mcpQuery.data?.length === 0 && <p className="text-xs text-muted-foreground">{t('subagent.resources.noMcp', 'No MCP servers. Add one on the MCP page.')}</p>}
      </div>}
    </section>
  </div>;
}
