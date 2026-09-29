/**
 * Single workflow row rendered in the workspace table.
 *
 * Columns: NAME (links to the canvas), LATEST VERSION (`v{major}.sv{sub}`),
 * UPDATED time, and a trailing ACTIONS cell with Open / Duplicate / Delete.
 * Primary actions (Open, Duplicate) are surfaced as buttons; the secondary
 * Edit-info + Delete actions live in a kebab menu so the row stays uncluttered
 * for non-technical users. Dialog/duplicate state is owned by the parent so the
 * row stays render-only.
 */
import { Link } from 'react-router';
import {
  MoreHorizontal,
  Pencil,
  Trash2,
  Copy,
  ExternalLink,
  Rocket,
  Share2,
} from 'lucide-react';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useFormatDateTime } from '@/lib/timezone';
import { Button } from '@/components/ui/button';
import { CopyButton } from '@/components/ui/copy-button';
import { StatusDot } from '@/components/ui/status';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import {
  useCloseWorkflowSandbox,
  type WorkflowSandboxStatus,
} from '@/lib/api/queries/workflow-sandbox';
import type { components } from '@/lib/api/schema';
import type { ResourceAccess } from '@/lib/api/organizations';
import { ResourceShareDialog } from '@/components/modals/ResourceShareDialog';
import { ResourceIcon } from '@/components/presentation/ResourceIcon';
import { ResourceProvenanceLine } from '@/components/resources/ResourceProvenanceLine';

type WorkflowMetaOut = components['schemas']['WorkflowMetaOut'];

export interface WorkflowRowProps {
  wf: WorkflowMetaOut;
  onEdit: (wf: WorkflowMetaOut) => void;
  onDelete: (wf: WorkflowMetaOut) => void;
  onDuplicate: (wf: WorkflowMetaOut) => void;
  duplicating: boolean;
  sandboxStatus?: WorkflowSandboxStatus;
}

export function WorkflowRow({
  wf,
  onEdit,
  onDelete,
  onDuplicate,
  duplicating,
  sandboxStatus,
}: WorkflowRowProps) {
  const { t } = useTranslation();
  // `updated_at` is epoch *seconds* (backend convention). Render in the user's
  // chosen timezone via the shared formatter (re-renders when the zone changes).
  const formatDateTime = useFormatDateTime();
  const title = wf.workflow_name || wf.wf_id;
  const [closeSandboxOpen, setCloseSandboxOpen] = useState(false);
  const [shareOpen, setShareOpen] = useState(false);
  const access = (wf as WorkflowMetaOut & { access?: ResourceAccess }).access;
  const capabilities = new Set(access?.capabilities ?? []);
  // Missing access is a deny, not a compatibility fallback. Every protected
  // resource DTO must carry the backend-computed OpenFGA projection.
  const canView = capabilities.has('view');
  const canUpdate = capabilities.has('update');
  const canDelete = capabilities.has('delete');
  const canDuplicate = capabilities.has('export');
  const canDeploy = capabilities.has('deploy');
  const canManageAccess = capabilities.has('manage_access');
  const canCancel = capabilities.has('cancel');
  const closeSandbox = useCloseWorkflowSandbox(wf.wf_id);
  const sandboxState = sandboxStatus?.status ?? 'idle';
  const sandboxRunning = sandboxState === 'running';
  const sandboxAllocated = [
    'running',
    'hibernating',
    'hibernated',
    'restoring',
    'releasing',
    'snapshot_failed',
  ].includes(sandboxState);
  const sandboxExecuting = (sandboxStatus?.active_execution_ids?.length ?? 0) > 0;
  const sandboxLabel = sandboxRunning
    ? sandboxExecuting
      ? t('workflow.sandbox.executing', 'Executing')
      : t('workflow.sandbox.running', 'Sandbox running')
    : sandboxState === 'hibernating'
      ? t('workflow.sandbox.hibernating', 'Creating snapshot')
    : sandboxState === 'restoring'
      ? t('workflow.sandbox.restoring', 'Restoring sandbox')
    : sandboxState === 'releasing'
      ? t('workflow.sandbox.releasing', 'Releasing sandbox')
    : sandboxState === 'hibernated'
      ? t('workflow.sandbox.hibernated', 'Sandbox hibernated')
    : sandboxState === 'snapshot_failed'
      ? t('workflow.sandbox.snapshot_failed', 'Snapshot failed')
    : sandboxState === 'closed'
      ? t('workflow.sandbox.closed', 'Sandbox closed')
      : t('workflow.sandbox.idle', 'Sandbox idle');
  const sandboxTone = sandboxExecuting ? 'running' : sandboxRunning ? 'success' : 'neutral';
  return (
    <>
      <tr
        className={`interactive-row group border-b ${closeSandboxOpen ? 'interactive-selected' : ''}`}
        data-testid="wf-row"
        data-wf-id={wf.wf_id}
      >
      <td data-col="name" className="py-3 pl-4 pr-3">
        <div className="flex min-w-0 items-start gap-3">
          <ResourceIcon kind="workflow" size="sm" className="mt-0.5" />
          <div className="min-w-0 flex-1">
        {canView ? (
          <Link
            to={`/workflow/${wf.wf_id}`}
            className="block truncate font-medium hover:underline focus-visible:underline focus-visible:outline-none"
            title={title}
          >
            {title}
          </Link>
        ) : (
          <span
            className="block truncate font-medium"
            title={title}
            data-testid="wf-row-metadata-only"
          >
            {title}
          </span>
        )}
        {wf.description ? (
          <p className="truncate text-meta" title={wf.description}>
            {wf.description}
          </p>
        ) : null}
        <ResourceProvenanceLine
          provenance={wf.provenance}
          className="mt-0.5 flex max-w-full"
        />
        <div className="mt-0.5 flex items-center gap-1">
          <code
            className="truncate font-mono text-meta"
            title={wf.wf_id}
            data-testid="wf-row-id"
          >
            {wf.wf_id}
          </code>
          <span
            onClick={(e) => {
              e.stopPropagation();
              e.preventDefault();
            }}
          >
            <CopyButton
              data-testid="wf-row-copy-id"
              value={wf.wf_id}
              label={t('copy_wf_id', 'Copy workflow ID')}
              copiedLabel={t('wf_id_copied', 'Workflow ID copied')}
            />
          </span>
        </div>
          </div>
        </div>
      </td>
      <td data-col="version" data-label={t('col_version', 'Version')} className="whitespace-nowrap px-3 py-3">
        <span className="inline-flex rounded-full border bg-background px-2 py-0.5 text-meta">
          v{wf.active_v}.sv{wf.active_sv}
        </span>
      </td>
      <td data-col="sandbox" data-label={t('workspace.col_sandbox', 'Sandbox')} className="whitespace-nowrap px-3 py-3">
        <button
          type="button"
          className="inline-flex items-center gap-2 rounded-md px-2 py-1 text-meta transition-colors hover:bg-muted hover:text-foreground disabled:hover:bg-transparent disabled:hover:text-muted-foreground"
          title={sandboxAllocated
            ? t('workflow.sandbox.close', 'Close workflow sandbox')
            : sandboxLabel}
          disabled={!sandboxAllocated || !canCancel}
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
            if (sandboxAllocated) setCloseSandboxOpen(true);
          }}
        >
          <StatusDot status={sandboxTone} pulse={sandboxExecuting} />
          <span>{sandboxLabel}</span>
        </button>
      </td>
      <td data-col="updated" data-label={t('col_updated', 'Updated')} className="hidden whitespace-nowrap px-3 py-3 text-sm text-muted-foreground xl:table-cell">
        {formatDateTime(wf.updated_at)}
      </td>
      <td data-col="actions" className="py-3 pl-3 pr-4">
        <div className="flex items-center justify-end gap-1">
          {canView ? (
            <Button asChild variant="outline" size="sm" data-testid="wf-row-open">
              <Link to={`/workflow/${wf.wf_id}`}>
                <ExternalLink />
                {t('open_wf', 'Open')}
              </Link>
            </Button>
          ) : (
            <Button
              variant="outline"
              size="sm"
              data-testid="wf-row-open"
              disabled
              title={t(
                'workflow.metadataOnly',
                'This role can review metadata but cannot open workflow content.',
              )}
            >
              <ExternalLink />
              {t('open_wf', 'Open')}
            </Button>
          )}
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                data-testid="wf-row-menu"
                aria-label={t('open_actions_menu', {
                  name: title,
                  defaultValue: 'Open actions menu for {{name}}',
                })}
              >
                <MoreHorizontal />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem
                disabled={duplicating || !canDuplicate}
                onSelect={() => onDuplicate(wf)}
                data-testid="wf-row-duplicate"
              >
                <Copy />
                {duplicating
                  ? t('duplicating_wf', 'Duplicating…')
                  : t('duplicate_wf', 'Duplicate')}
              </DropdownMenuItem>
              {canUpdate ? (
                <DropdownMenuItem
                  onSelect={() => onEdit(wf)}
                  data-testid="wf-row-edit"
                >
                  <Pencil />
                  {t('edit_info', 'Edit info')}
                </DropdownMenuItem>
              ) : null}
              {canManageAccess ? (
                <DropdownMenuItem
                  onSelect={() => setShareOpen(true)}
                  data-testid="wf-row-share"
                >
                  <Share2 />
                  {t('share.title', 'Share workflow')}
                </DropdownMenuItem>
              ) : null}
              {canDeploy ? (
                <DropdownMenuItem asChild>
                  <Link
                    data-testid="wf-row-deploy"
                    to={`/deployments?create=1&workflow_id=${encodeURIComponent(wf.wf_id)}&workflow_name=${encodeURIComponent(title)}`}
                  >
                    <Rocket />
                    {t('deployments.actions.deployWorkflow', 'Deploy')}
                  </Link>
                </DropdownMenuItem>
              ) : null}
              {canDelete ? (
                <DropdownMenuItem
                  onSelect={() => onDelete(wf)}
                  className="text-destructive focus:bg-destructive/10 focus:text-destructive"
                  data-testid="wf-row-delete"
                >
                  <Trash2 />
                  {t('delete_wf', 'Delete')}
                </DropdownMenuItem>
              ) : null}
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </td>
      </tr>
      <Dialog open={closeSandboxOpen} onOpenChange={setCloseSandboxOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('workflow.sandbox.closeTitle', 'Close sandbox?')}</DialogTitle>
            <DialogDescription>
              {t(
                'workflow.sandbox.closeDescription',
                'This stops any running execution and releases the active sandbox for "{{name}}". Stored workflow files remain available.',
                { name: title },
              )}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setCloseSandboxOpen(false)}
            >
              {t('cancel', 'Cancel')}
            </Button>
            <Button
              type="button"
              variant="destructive"
              disabled={closeSandbox.isPending}
              onClick={async () => {
                await closeSandbox.mutateAsync();
                setCloseSandboxOpen(false);
              }}
            >
              {closeSandbox.isPending
                ? t('workflow.sandbox.closing', 'Closing…')
                : t('workflow.sandbox.closeAction', 'Close sandbox')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <ResourceShareDialog
        open={shareOpen}
        onOpenChange={setShareOpen}
        resourceKind="workflow"
        resourceId={wf.wf_id}
        resourceName={title}
        effectiveRole={access?.effective_role}
        accessSource={access?.source}
      />
    </>
  );
}
