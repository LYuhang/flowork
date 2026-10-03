import { useState } from 'react';
import { Link, useLocation } from 'react-router';
import { ChevronRight } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useWorkflowVersions } from '@/lib/api/queries/workflow';

interface VersionRow { major: number; sub: number }
export interface WorkflowVersionsSectionProps {
  wfId: string;
  activeMajor: number | null;
  activeSub: number | null;
  viewedMajor?: number | null;
}

export function WorkflowVersionsSection({ wfId, activeMajor, viewedMajor }: WorkflowVersionsSectionProps) {
  const { t } = useTranslation();
  const { pathname } = useLocation();
  const q = useWorkflowVersions(wfId);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const currentMajor = viewedMajor ?? activeMajor;
  if (q.isLoading) return <div className="px-3 py-2 text-xs text-muted-foreground">{t('vfs.loading', 'Loading…')}</div>;
  if (q.isError) return <div className="px-3 py-2 text-xs text-destructive">{t('vfs.versions_error', 'Failed to load versions.')}</div>;
  const groups = new Map<number, VersionRow[]>();
  const sorted = [...((q.data as { versions?: VersionRow[] })?.versions ?? [])].sort((a, b) => b.major - a.major || b.sub - a.sub);
  for (const row of sorted) groups.set(row.major, [...(groups.get(row.major) ?? []), row]);
  if (!groups.size) return <div className="px-3 py-2 text-xs text-muted-foreground">{t('vfs.no_versions', 'No versions yet.')}</div>;
  return (
    <ul className="space-y-0.5" data-testid="workflow-version-tree">
      {[...groups].map(([major, versions]) => {
        const key = `${wfId}:${major}`;
        const open = expanded[key] ?? major === currentMajor;
        const majorPath = `/workflow/${wfId}/version/v${major}`;
        return (
          <li key={key}>
            <div className="flex items-center rounded hover:bg-muted">
              <button type="button" className="flex h-7 w-7 shrink-0 items-center justify-center rounded hover:bg-muted focus-visible:outline focus-visible:outline-2"
                aria-label={open ? t('vfs.collapse_version', { version: `v${major}`, defaultValue: 'Collapse {{version}}' }) : t('vfs.expand_version', { version: `v${major}`, defaultValue: 'Expand {{version}}' })}
                aria-expanded={open} aria-controls={`versions-${wfId}-${major}`}
                onClick={() => setExpanded((old) => ({ ...old, [key]: !open }))}>
                <ChevronRight aria-hidden="true" className={`h-3.5 w-3.5 transition-transform ${open ? 'rotate-90' : ''}`} />
              </button>
              <Link to={majorPath} aria-current={pathname === majorPath ? 'page' : undefined}
                className="flex min-w-0 flex-1 items-center gap-2 py-1 pr-3 text-xs">
                <span className="font-mono">v{major}</span>
                {major === currentMajor && <span className="rounded bg-primary/10 px-1 text-xs text-primary">{t('vfs.current', 'current')}</span>}
              </Link>
            </div>
            {open && <ul id={`versions-${wfId}-${major}`} className="ml-3.5 space-y-0.5 border-l border-border pl-3">
              {versions.map(({ sub }) => {
                const version = `v${major}.sv${sub}`;
                const path = `/workflow/${wfId}/version/${version}`;
                return <li key={sub}><Link to={`${path}?snapshot=1`} aria-current={pathname === path ? 'page' : undefined}
                  className="block rounded px-2 py-1 font-mono text-xs text-muted-foreground hover:bg-muted aria-[current=page]:bg-primary/10 aria-[current=page]:text-primary">{version}</Link></li>;
              })}
            </ul>}
          </li>
        );
      })}
    </ul>
  );
}
