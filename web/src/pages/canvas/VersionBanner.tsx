/**
 * Read-only mode banner — mounted by `CanvasPage` above the toolbar when
 * the `:vKey` route param resolves to a pinned historical version.
 *
 * Two actions:
 *   - **Fork from this version** — POST `/major-versions` with the
 *     currently-rendered draft (already seeded into `useWorkflowEditStore`
 *     by `CanvasPage`'s load effect). On success, navigate to
 *     `/workflow/{wfId}` so the user lands on the new editable HEAD.
 *   - **Return to latest** — navigate to `/workflow/{wfId}` without
 *     mutating anything.
 *
 * The yellow tint follows shadcn's destructive/warning convention; we keep
 * the layout to a single flex row to avoid stealing vertical space from
 * the canvas.
 */
import { useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { useNewMajorVersion } from '@/lib/api/mutations/workflow-ops';
import { useWorkflowEditStore } from '@/stores/workflow-edit';

export interface VersionBannerProps {
  wfId: string;
  vKey: string;
}

export function VersionBanner({ wfId, vKey }: VersionBannerProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const draft = useWorkflowEditStore((s) => s.draft);
  const newMajor = useNewMajorVersion(wfId);

  const onFork = async () => {
    if (!draft) return;
    try {
      await newMajor.mutateAsync(draft);
      // The forked draft is now committed as the NEW latest major, so re-baseline
      // it to clean — otherwise (when forking an EDITED historical version) the
      // draft stays dirty and the unsaved-changes guard blocks this navigation
      // with a spurious prompt. Then land on the bare latest route: that IS the
      // new fork (new HEAD), and being latest it shows no banner.
      useWorkflowEditStore.getState().markSaved();
      navigate(`/workflow/${wfId}`);
    } catch {
      // Toast already raised by the mutation's onError handler.
    }
  };

  const onReturnLatest = () => {
    navigate(`/workflow/${wfId}`);
  };

  return (
    <div className="flex items-center gap-3 border-b border-state-warning/35 bg-state-warning/10 px-4 py-2 text-sm text-content-primary" role="status">
      <span className="font-medium text-state-warning">
        {t('version_banner.readOnly', {
          vKey,
          defaultValue: 'Historical {{vKey}} · Read-only. Fork to edit as a new major.',
        })}
      </span>
      <div className="flex-1" />
      <Button
        variant="outline"
        size="sm"
        data-action="version-fork"
        disabled={!draft || newMajor.isPending}
        onClick={() => void onFork()}
      >
        {t('version_banner.fork', 'Fork as new major')}
      </Button>
      <Button
        variant="ghost"
        size="sm"
        data-action="version-return-latest"
        onClick={onReturnLatest}
      >
        {t('version_banner.return_latest', 'Return to latest')}
      </Button>
    </div>
  );
}
