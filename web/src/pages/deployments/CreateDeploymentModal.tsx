/**
 * `CreateDeploymentModal` — single-form create flow (Deployments T14).
 *
 * Spec §10.2 sketches a wizard (workflow → trigger → runtime/security →
 * review). This component keeps the existing dialog surface while supporting
 * API endpoints and webhooks.
 *
 * Two-phase modal:
 *   1. **Form phase** — collect user-facing fields, derive the backend slug
 *      from the deployment/workflow name, and submit.
 *   2. **Success phase** — display the one-shot plaintext credential
 *      (`api_key` for api / `hmac_secret` for webhook) with a "Copy"
 *      button and an explicit "This is shown only once" warning. Closing
 *      the success panel calls `onCreated()` so the parent list refetches.
 *
 * The success phase is deliberately a SEPARATE render path (not a toast)
 * because:
 *   * The secret must remain visible until the user actively dismisses it.
 *   * The "Copy to clipboard" affordance needs a stable anchor.
 *   * A toast would auto-dismiss and risk the user losing the only copy.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import { Code2, Webhook } from 'lucide-react';

import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Switch } from '@/components/ui/switch';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { SearchSelect, type SearchSelectOption } from '@/components/ui/search-select';
import {
  createDeployment,
  type CreateDeploymentBody,
  type CreateDeploymentResponse,
} from '@/lib/api/deployments';
import { useWorkspaceList } from '@/lib/api/queries/workflows';
import { useWorkflowVersions } from '@/lib/api/queries/workflow';
import { OneTimeSecretField } from '@/pages/deployments/OneTimeSecretField';
import { cn } from '@/lib/utils';

interface WorkflowVersionOption {
  major: number;
  sub: number;
}

interface CreateDeploymentModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated?: () => void;
  initialWorkflowId?: string;
  initialName?: string;
}

const TRIGGER_TYPES = [
  { type: 'api', icon: Code2, tone: 'text-state-info', label: 'API', help: 'Call from an application' },
  { type: 'webhook', icon: Webhook, tone: 'text-focus', label: 'Webhook', help: 'Receive signed events' },
] as const;

function defaultBody(initialWorkflowId = '', initialName = ''): CreateDeploymentBody {
  return {
    wf_id: initialWorkflowId,
    name: initialName,
    slug: '',
    trigger_type: 'api',
    version_pin: 'specific',
    rate_limit_qps: 10,
    mount_enabled: false,
    enabled: true,
  };
}

function slugifyDeploymentName(value: string): string {
  const slug = value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 63);
  return slug || 'deployment';
}

export function CreateDeploymentModal({
  open,
  onOpenChange,
  onCreated,
  initialWorkflowId = '',
  initialName = '',
}: CreateDeploymentModalProps) {
  const { t } = useTranslation();
  const workflowsQuery = useWorkspaceList(200, 0, open);
  const workflows = useMemo(() => workflowsQuery.data?.items ?? [], [workflowsQuery.data?.items]);
  const [body, setBody] = useState<CreateDeploymentBody>(() =>
    defaultBody(initialWorkflowId, initialName),
  );
  const [result, setResult] = useState<CreateDeploymentResponse | null>(null);
  const [dialogElement, setDialogElement] = useState<HTMLDivElement | null>(null);
  const captureDialogElement = useCallback((node: HTMLDivElement | null) => {
    setDialogElement((current) => current === node ? current : node);
  }, []);

  // Reset state every time the modal re-opens — `useEffect` keyed on
  // `open` clears stale form input + the success panel when the user
  // closes mid-flow and re-opens.
  useEffect(() => {
    if (open) queueMicrotask(() => {
      setBody(defaultBody(initialWorkflowId, initialName));
      setResult(null);
    });
  }, [initialName, initialWorkflowId, open]);

  const workflowOptions = useMemo<SearchSelectOption[]>(() => {
    const options = workflows.map((wf) => ({
      value: wf.wf_id,
      label: wf.workflow_name || wf.wf_id,
      meta: wf.wf_id,
      description: wf.description ?? '',
      keywords: [wf.workflow_name ?? '', wf.wf_id, wf.description ?? ''],
    }));
    if (body.wf_id && !options.some((option) => option.value === body.wf_id)) {
      options.unshift({
        value: body.wf_id,
        label: body.wf_id,
        meta: body.wf_id,
        description: t('deployments.create.currentWorkflow', 'Current workflow'),
        keywords: [body.wf_id],
      });
    }
    return options;
  }, [body.wf_id, t, workflows]);

  const versionsQuery = useWorkflowVersions(
    open ? body.wf_id || undefined : undefined,
  );
  const versions = useMemo<WorkflowVersionOption[]>(() => {
    const raw = (versionsQuery.data as { versions?: unknown[] } | undefined)?.versions ?? [];
    return raw
      .flatMap((item) => {
        if (!item || typeof item !== 'object') return [];
        const row = item as Record<string, unknown>;
        return typeof row.major === 'number' && typeof row.sub === 'number'
          ? [{ major: row.major, sub: row.sub }]
          : [];
      })
      .sort((left, right) => right.major - left.major || right.sub - left.sub);
  }, [versionsQuery.data]);
  const majorOptions = useMemo(
    () => [...new Set(versions.map((version) => version.major))],
    [versions],
  );
  const selectedMajor = body.version_pin !== 'head'
    ? (majorOptions.includes(body.pinned_major as number)
        ? body.pinned_major
        : majorOptions[0])
    : undefined;
  const subOptions = useMemo(
    () => versions
      .filter((version) => version.major === selectedMajor)
      .map((version) => version.sub),
    [selectedMajor, versions],
  );
  const selectedSub = body.version_pin === 'specific'
    ? (subOptions.includes(body.pinned_sub as number) ? body.pinned_sub : subOptions[0])
    : undefined;

  const createMutation = useMutation({
    mutationFn: (b: CreateDeploymentBody) => createDeployment(b),
    onSuccess: (resp) => {
      setResult(resp);
      // We do NOT call onCreated() here — defer until the user closes
      // the success panel so they don't lose the secret to an aggressive
      // list refetch unmounting this dialog.
    },
    onError: (e) => {
      toast.error(
        e instanceof Error ? e.message : String(e),
      );
    },
  });

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (
      selectedMajor === undefined || (body.version_pin === 'specific' && selectedSub === undefined)
    ) {
      toast.error(t('deployments.create.selectVersion', 'Select a workflow version.'));
      return;
    }
    const selectedWorkflow = workflows.find((workflow) => workflow.wf_id === body.wf_id);
    const payload: CreateDeploymentBody = {
      ...body,
      ...(body.version_pin === 'specific'
        ? { pinned_major: selectedMajor, pinned_sub: selectedSub }
        : { pinned_major: selectedMajor }),
      slug: slugifyDeploymentName(
        body.name || selectedWorkflow?.workflow_name || body.wf_id,
      ),
    };
    if (payload.version_pin !== 'specific') {
      delete payload.pinned_sub;
    }
    createMutation.mutate(payload);
  };

  const handleDismiss = () => {
    if (result) {
      onCreated?.();
    }
    onOpenChange(false);
  };

  const renderSuccess = (resp: CreateDeploymentResponse) => {
    const secretLabel =
      resp.api_key !== undefined
        ? t('deployments.create.apiKey', 'API Key')
        : resp.hmac_secret !== undefined
        ? t('deployments.create.hmacSecret', 'HMAC Secret')
        : null;
    const secretValue = resp.api_key ?? resp.hmac_secret ?? '';
    const url = resp.endpoint_url ?? resp.webhook_url ?? null;

    return (
      <div className="flex flex-col gap-4">
        {secretLabel ? (
          <div className="rounded-md border border-state-warning/40 bg-state-warning/10 p-3 text-sm text-state-warning">
            {t(
              'deployments.create.oneTimeSecret',
              'This secret is shown only once. Copy it now — you cannot retrieve it later.',
            )}
          </div>
        ) : null}
        <div className="flex flex-col gap-1">
          <span className="text-xs text-muted-foreground">
            {t('deployments.create.id', 'Deployment ID')}
          </span>
          <span className="break-all font-mono text-xs">{resp.id}</span>
        </div>
        {secretLabel && (
          <OneTimeSecretField value={secretValue} label={secretLabel} />
        )}
        {url && (
          <div className="flex flex-col gap-1">
            <span className="text-xs text-muted-foreground">
              {t('deployments.create.endpoint', 'Endpoint')}
            </span>
            <code className="break-all rounded border bg-muted/50 px-2 py-1 font-mono text-xs">
              {url}
            </code>
          </div>
        )}
      </div>
    );
  };

  return (
    <Dialog open={open} onOpenChange={(o) => !o && handleDismiss()}>
      <DialogContent ref={captureDialogElement} className="max-h-[90vh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>
            {result
              ? t('deployments.create.createdHeader', 'Deployment created')
              : t('deployments.create.title', 'New deployment')}
          </DialogTitle>
          {!result && (
            <DialogDescription>
              {t(
                'deployments.create.desc',
                'Choose how this workflow should receive requests.',
              )}
            </DialogDescription>
          )}
        </DialogHeader>

        {result ? (
          <>
            {renderSuccess(result)}
            <DialogFooter>
              <Button type="button" onClick={handleDismiss}>
                {t('deployments.create.close', 'Close')}
              </Button>
            </DialogFooter>
          </>
        ) : (
          <form onSubmit={handleSubmit} className="flex flex-col gap-4">
            <div className="flex flex-col gap-1">
              <Label htmlFor="dep-name">
                {t('deployments.create.fields.name', 'Name')}
              </Label>
              <Input
                id="dep-name"
                name="deployment-name"
                autoComplete="off"
                required
                value={body.name}
                onChange={(e) =>
                  setBody((b) => ({ ...b, name: e.target.value }))
                }
              />
            </div>

            <div className="flex flex-col gap-1">
              <Label htmlFor="dep-wf">
                {t('deployments.create.fields.workflow', 'Workflow')}
              </Label>
              <SearchSelect
                id="dep-wf"
                value={body.wf_id}
                options={workflowOptions}
                onValueChange={(value) => {
                  const selected = workflows.find((wf) => wf.wf_id === value);
                  const label = selected?.workflow_name || value;
                  setBody((b) => ({
                    ...b,
                    wf_id: value,
                    name: b.name.trim() ? b.name : `${label} API`,
                    pinned_major: undefined,
                    pinned_sub: undefined,
                  }));
                }}
                placeholder={t('deployments.create.selectWorkflow', 'Select a workflow')}
                searchPlaceholder={t('deployments.create.searchWorkflow', 'Search workflow name, ID, or description')}
                emptyText={t('deployments.create.noWorkflowMatches', 'No workflows match your search.')}
                disabled={workflowsQuery.isLoading || workflowOptions.length === 0}
                portalContainer={dialogElement}
              />
              {workflowsQuery.isLoading && (
                <span className="text-xs text-muted-foreground">
                  {t('workspace_loading', 'Loading workflows...')}
                </span>
              )}
            </div>

            <fieldset className="flex flex-col gap-2">
              <legend className="text-sm font-medium">
                {t(
                  'deployments.create.fields.triggerType',
                  'Trigger type',
                )}
              </legend>
              <div className="grid gap-2 sm:grid-cols-2">
                {TRIGGER_TYPES.map(({ type, icon: Icon, tone, label, help }) => (
                  <label
                    key={type}
                    className={cn(
                      'group relative flex cursor-pointer flex-col gap-2 rounded-lg border border-edge-subtle bg-surface-work p-3 transition-[border-color,background-color,box-shadow]',
                      'hover:border-edge-strong hover:bg-surface-sunken/50 focus-within:ring-2 focus-within:ring-focus/40',
                      body.trigger_type === type && 'border-focus bg-focus/5 shadow-sm',
                    )}
                  >
                    <input
                      type="radio"
                      name="trigger_type"
                      value={type}
                      checked={body.trigger_type === type}
                      className="absolute inset-0 z-10 h-full w-full cursor-pointer opacity-0"
                      onChange={() =>
                        setBody((current) => ({ ...current, trigger_type: type }))
                      }
                    />
                    <Icon className={cn('h-5 w-5', tone)} aria-hidden="true" />
                    <span className="text-sm font-medium">{t(`deployments.type.${type}`, label)}</span>
                    <span className="text-xs leading-4 text-muted-foreground">
                      {t(`deployments.create.triggerHelp.${type}`, help)}
                    </span>
                  </label>
                ))}
              </div>
            </fieldset>

            <fieldset className="flex flex-col gap-2">
              <legend className="text-sm font-medium">
                {t(
                  'deployments.create.fields.versionPin',
                  'Version pin',
                )}
              </legend>
              {body.version_pin !== 'head' && (
                <div className="grid grid-cols-2 gap-2">
                  <div className="flex flex-col gap-1">
                    <Label>{t('deployments.create.fields.majorVersion', 'Major version')}</Label>
                    <Select
                      value={selectedMajor === undefined ? undefined : String(selectedMajor)}
                      onValueChange={(value) => {
                        const major = Number(value);
                        const nextSub = versions.find((version) => version.major === major)?.sub;
                        setBody((current) => ({
                          ...current,
                          pinned_major: major,
                          pinned_sub: nextSub,
                        }));
                      }}
                      disabled={versionsQuery.isLoading || majorOptions.length === 0}
                    >
                      <SelectTrigger aria-label={t('deployments.create.fields.majorVersion', 'Major version')}>
                        <SelectValue placeholder={t('deployments.create.selectMajor', 'Select major')} />
                      </SelectTrigger>
                      <SelectContent>
                        {majorOptions.map((major) => (
                          <SelectItem key={major} value={String(major)}>v{major}</SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                  {body.version_pin === 'specific' && <div className="flex flex-col gap-1">
                    <Label>{t('deployments.create.fields.subVersion', 'Sub version')}</Label>
                    <Select
                      value={selectedSub === undefined ? undefined : String(selectedSub)}
                      onValueChange={(value) =>
                        setBody((current) => ({ ...current, pinned_sub: Number(value) }))
                      }
                      disabled={versionsQuery.isLoading || subOptions.length === 0}
                    >
                      <SelectTrigger aria-label={t('deployments.create.fields.subVersion', 'Sub version')}>
                        <SelectValue placeholder={t('deployments.create.selectSub', 'Select sub')} />
                      </SelectTrigger>
                      <SelectContent>
                        {subOptions.map((sub) => (
                          <SelectItem key={sub} value={String(sub)}>sv{sub}</SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>}
                  {versionsQuery.isError && <p role="alert" className="col-span-2 text-sm text-destructive">{t('tasks.version.error', 'Could not load workflow versions.')}</p>}
                  {!versionsQuery.isLoading && !versionsQuery.isError && versions.length === 0 && (
                    <p className="col-span-2 text-xs text-muted-foreground">
                      {t('deployments.create.noVersions', 'No saved versions are available for this workflow.')}
                    </p>
                  )}
                </div>
              )}
            </fieldset>

            <div className="flex flex-col gap-1">
              <Label htmlFor="dep-qps">
                {t(
                  'deployments.create.fields.rateLimitQps',
                  'Rate limit (QPS)',
                )}
              </Label>
              <Input
                id="dep-qps"
                name="rate-limit-qps"
                type="number"
                min={0}
                value={body.rate_limit_qps ?? 10}
                onChange={(e) =>
                  setBody((b) => ({
                    ...b,
                    rate_limit_qps: Number(e.target.value) || 0,
                  }))
                }
              />
            </div>

            <label className="flex items-center gap-2">
              <Switch checked={body.mount_enabled ?? false} onCheckedChange={(checked) => setBody((b) => ({ ...b, mount_enabled: checked }))} />
              <span>{t('deployments.mount', 'Mount user storage (/mount)')}</span>
            </label>
            <p className="text-xs text-muted-foreground">{t('deployments.mountHelp', 'Shares authorized user storage, not Chat files. Each accepted call keeps its version and mount settings.')}</p>
            <label className="flex items-center gap-2">
              <Switch checked={body.enabled ?? true} onCheckedChange={(checked) => setBody((b) => ({ ...b, enabled: checked }))} />
              <span>{t('deployments.col.enabled', 'Enabled')}</span>
            </label>
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => onOpenChange(false)}
                disabled={createMutation.isPending}
              >
                {t('deployments.create.cancel', 'Cancel')}
              </Button>
              <Button type="submit" disabled={createMutation.isPending}>
                {createMutation.isPending
                  ? t('deployments.create.creating', 'Creating…')
                  : t('deployments.create.submit', 'Create')}
              </Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}
