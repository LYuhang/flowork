import { useId, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import * as Popover from '@radix-ui/react-popover';
import { ExternalLink, RefreshCw, X } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { useSkills } from '@/lib/api/queries/skills';
import { useMcpServers } from '@/lib/api/queries/mcp-servers';

export interface SkillReference { id: string; name: string }
export interface McpReference { id: string; name: string }
interface Candidate extends SkillReference { description: string; usable: boolean; meta?: string }

function ResourcePicker({ title, searchLabel, refreshLabel, selected, candidates, path, readOnly, pending, failed, success, refreshing, refresh, onChange, hint }: {
  title: string; searchLabel: string; refreshLabel: string; selected: SkillReference[];
  candidates: Candidate[]; path: string; readOnly?: boolean; pending: boolean; failed: boolean;
  success: boolean; refreshing?: boolean; refresh: () => void; onChange: (value: SkillReference[]) => void; hint: string;
}) {
  const { t } = useTranslation();
  const id = useId();
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState('');
  const [limit, setLimit] = useState(50);
  const byId = useMemo(() => new Map(candidates.map(item => [item.id, item])), [candidates]);
  const selectedIds = new Set(selected.map(item => item.id));
  const filtered = useMemo(() => {
    const query = search.trim().toLocaleLowerCase();
    return candidates.filter(item => `${item.name} ${item.description}`.toLocaleLowerCase().includes(query));
  }, [candidates, search]);
  const details = (item: SkillReference) => <a href={`${path}/${encodeURIComponent(item.id)}`} target="_blank" rel="noreferrer"
    className="inline-flex size-7 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    aria-label={`${t('subagent.resources.details', 'Resource details')}: ${item.name}`}><ExternalLink className="size-3.5" /></a>;
  return <section className="space-y-2 rounded-lg border border-edge-subtle p-3">
    <div className="flex items-center justify-between gap-2"><h3 className="text-sm font-medium">{title}</h3>
      <Button variant="ghost" size="icon-sm" aria-label={refreshLabel} onClick={refresh} disabled={refreshing}><RefreshCw /></Button>
    </div>
    <Popover.Root open={open && !readOnly} onOpenChange={value => { setOpen(value); if (!value) { setSearch(''); setLimit(50); } }}>
      <Popover.Anchor asChild><Input id={id} aria-label={searchLabel} placeholder={searchLabel} disabled={readOnly}
        aria-expanded={open && !readOnly} aria-controls={`${id}-candidates`} aria-haspopup="dialog"
        value={search} onFocus={() => setOpen(true)} onClick={() => setOpen(true)}
        onKeyDown={event => { if (event.key === 'ArrowDown') { event.preventDefault(); setOpen(true); requestAnimationFrame(() => document.getElementById(`${id}-candidates`)?.querySelector<HTMLInputElement>('input:not(:disabled)')?.focus()); } if (event.key === 'Escape') setOpen(false); }}
        onChange={event => { setSearch(event.target.value); setLimit(50); setOpen(true); }} className="h-8 text-xs" /></Popover.Anchor>
      <Popover.Portal><Popover.Content id={`${id}-candidates`} aria-label={searchLabel} sideOffset={6} align="start" collisionPadding={12}
        className="z-50 w-[var(--radix-popover-trigger-width)] min-w-64 max-w-[calc(100vw-24px)] rounded-xl border bg-popover p-1.5 text-popover-foreground shadow-lg"
        style={{ width: 'var(--radix-popover-trigger-width)', maxHeight: 'min(360px, var(--radix-popover-content-available-height))', overflowY: 'auto' }}
        onOpenAutoFocus={event => event.preventDefault()} onCloseAutoFocus={event => event.preventDefault()}
        onInteractOutside={event => { if (event.target === document.getElementById(id)) event.preventDefault(); }}>
        {pending ? <p role="status" className="p-2 text-xs">{t('loading', 'Loading…')}</p>
          : failed ? <p role="alert" className="p-2 text-xs text-destructive">{t('subagent.resources.loadError', 'Could not load resources. Use Refresh to retry.')}</p>
          : <>
            {filtered.slice(0, limit).map(item => <div key={item.id} className="flex items-center gap-1 rounded-lg px-1 hover:bg-muted">
              <label className="flex min-w-0 flex-1 cursor-pointer items-start gap-2 py-2 pl-1">
                <input type="checkbox" className="mt-0.5 size-4 shrink-0 accent-primary" checked={selectedIds.has(item.id)} disabled={!selectedIds.has(item.id) && !item.usable}
                  onChange={event => onChange(event.target.checked ? [...selected, { id: item.id, name: item.name }] : selected.filter(ref => ref.id !== item.id))} />
                <span className="min-w-0 text-xs"><span className="block break-words font-medium">{item.name}</span>
                  <span className="line-clamp-2 break-words text-muted-foreground">{item.description}</span>
                  {item.meta && <span className="block text-muted-foreground">{item.meta}</span>}
                  {!item.usable && <span className="block text-muted-foreground">{t('subagent.resources.cannotUse', 'Not available for execution')}</span>}
                </span>
              </label>{details(item)}
            </div>)}
            {!filtered.length && <p className="p-2 text-xs text-muted-foreground">{t('subagent.resources.noMatches', 'No matching resources.')}</p>}
            {filtered.length > limit && <Button variant="ghost" size="sm" className="w-full" onClick={() => setLimit(value => value + 50)}>{t('subagent.resources.more', 'Show more')}</Button>}
          </>}
      </Popover.Content></Popover.Portal>
    </Popover.Root>
    {selected.map(ref => {
      const current = byId.get(ref.id);
      return <div key={ref.id} className="rounded-md bg-surface-sunken px-2 py-1 text-xs">
        <div className="flex min-w-0 items-center gap-1"><span className="min-w-0 flex-1 break-words">{current?.name ?? ref.name}</span>{details(current ?? ref)}
          {!readOnly && <Button variant="ghost" size="icon-sm" aria-label={`${t('remove', 'Remove')}: ${ref.name}`} onClick={() => onChange(selected.filter(item => item.id !== ref.id))}><X /></Button>}
        </div>
        {success && !current?.usable && <p role="alert" className="pb-1 text-destructive">{t('subagent.resources.unavailable', 'Resource missing or unavailable. Remove it or restore access before running.')}</p>}
      </div>;
    })}
    {failed && !open && <p role="alert" className="text-xs text-destructive">{t('subagent.resources.loadError', 'Could not load resources. Use Refresh to retry.')}</p>}
    <p className="text-xs leading-relaxed text-muted-foreground">{t('subagent.resources.selectedCount', { count: selected.length })} · {hint}</p>
  </section>;
}

export function SubAgentResources({ skills, servers, readOnly, onSkills, onServers }: {
  skills: SkillReference[]; servers: McpReference[]; readOnly?: boolean;
  onSkills: (value: SkillReference[]) => void; onServers: (value: McpReference[]) => void;
}) {
  const { t } = useTranslation();
  const skillQuery = useSkills({ refreshOnReturn: true });
  const mcpQuery = useMcpServers({ refreshOnReturn: true });
  return <div className="space-y-3" data-testid="subagent-resources">
    <ResourcePicker title="Skills" searchLabel={t('subagent.resources.searchSkills', 'Search Skills')} refreshLabel={t('subagent.resources.refreshSkills', 'Refresh Skills')}
      selected={skills} candidates={(skillQuery.data ?? []).map(item => ({ id: item.id, name: item.name, description: item.description, usable: !!item.revision_hash && item.access.capabilities.includes('use') }))}
      path="/skills" readOnly={readOnly} pending={skillQuery.isPending} failed={skillQuery.isError} success={skillQuery.isSuccess} refreshing={skillQuery.isFetching} refresh={() => void skillQuery.refetch()} onChange={onSkills}
      hint={t('subagent.resources.skillCompactHint', 'Uses the latest published instructions.')} />
    <ResourcePicker title="MCP" searchLabel={t('subagent.resources.searchMcp', 'Search MCP servers')} refreshLabel={t('subagent.resources.refreshMcp', 'Refresh MCP servers')}
      selected={servers} candidates={(mcpQuery.data ?? []).map(item => ({ id: item.id, name: item.name, description: item.description ?? '', usable: !!item.enabled && !!item.access?.capabilities.includes('use'), meta: `${item.transport} · ${t('subagent.resources.toolCount', { count: item.last_tool_count ?? 0 })}` }))}
      path="/mcp-servers" readOnly={readOnly} pending={mcpQuery.isPending} failed={mcpQuery.isError} success={mcpQuery.isSuccess} refreshing={mcpQuery.isFetching} refresh={() => void mcpQuery.refetch()} onChange={onServers}
      hint={t('subagent.resources.mcpCompactHint', 'Only selected servers provide tools.')} />
  </div>;
}
