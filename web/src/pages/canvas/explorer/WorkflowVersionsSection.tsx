import { Link } from 'react-router';
import { useTranslation } from 'react-i18next';
import { useWorkflowVersions } from '@/lib/api/queries/workflow';

interface VersionRow {
  major: number;
  sub: number;
  version_str?: string;
}

export interface WorkflowVersionsSectionProps {
  wfId: string;
  activeMajor: number | null;
  activeSub: number | null;
  /** When viewing a pinned historical version, its major — that row reads
   *  "current" instead of the HEAD (the canvas is showing it). Null on the
   *  live route, where the HEAD (`activeMajor`) is current. */
  viewedMajor?: number | null;
}

export function WorkflowVersionsSection({ wfId, activeMajor, viewedMajor }: WorkflowVersionsSectionProps) {
  const { t } = useTranslation();
  const q = useWorkflowVersions(wfId);

  if (q.isLoading) return <div className="px-3 py-2 text-xs text-muted-foreground">{t('vfs.loading', 'Loading…')}</div>;
  if (q.isError) return <div className="px-3 py-2 text-xs text-destructive">{t('vfs.versions_error', 'Failed to load versions.')}</div>;

  const sorted = [...((q.data as { versions?: VersionRow[] })?.versions ?? [])].sort(
    (a, b) => b.major - a.major || b.sub - a.sub,
  );
  // ONE entry per MAJOR version, showing its LATEST (highest) sv (user model:
  // a major = a "file"; Save bumps sv and updates that file's entry in place).
  // Sorted major-desc/sub-desc, so the first row of each major IS its latest sv.
  const seenMajor = new Set<number>();
  const versions = sorted.filter((e) => {
    if (seenMajor.has(e.major)) return false;
    seenMajor.add(e.major);
    return true;
  });
  if (versions.length === 0) return <div className="px-3 py-2 text-xs text-muted-foreground">{t('vfs.no_versions', 'No versions yet.')}</div>;

  return (
    <ul className="space-y-0.5">
      {versions.map((e) => {
        const vKey = `v${e.major}.sv${e.sub}`;
        // Current = the major the canvas is SHOWING: the pinned viewed major when
        // browsing history, else the active HEAD major (its latest sv is the row).
        const currentMajor = viewedMajor ?? activeMajor;
        const isCurrent = e.major === currentMajor;
        return (
          <li key={vKey}>
            <Link
              to={`/workflow/${wfId}/version/v${e.major}`}
              className="flex w-full items-center gap-2 rounded px-3 py-1 text-left text-xs hover:bg-muted"
            >
              <span className="font-mono">{vKey}</span>
              {isCurrent && (
                <span className="rounded bg-primary/10 px-1 text-xs text-primary">
                  {t('vfs.current', 'current')}
                </span>
              )}
            </Link>
          </li>
        );
      })}
    </ul>
  );
}
