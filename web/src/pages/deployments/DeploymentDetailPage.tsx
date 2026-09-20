import { useCallback, useMemo, useRef, useState } from 'react';
import { Link, useParams, useSearchParams } from 'react-router';
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import {
  Code2,
  KeyRound,
  Pencil,
  Play,
  RefreshCw,
  Rocket,
  Share2,
  ShieldCheck,
} from 'lucide-react';

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
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from '@/components/ui/tabs';
import { Textarea } from '@/components/ui/textarea';
import {
  getDeployment,
  getHistory,
  getMetrics,
  patchDeployment,
  rotateKey,
  testInvoke,
  type Deployment,
  type HistoryItem,
  type MetricsPoint,
} from '@/lib/api/deployments';
import { useFormatDateTime } from '@/lib/timezone';
import { EntityDetailShell } from '@/components/layout/entity-detail-shell';
import { SectionBlock } from '@/components/layout/section-block';
import { DetailSummary } from '@/components/layout/detail-summary';
import { OperationalSummary } from '@/components/layout/operational-summary';
import {
  IncrementalLogLoader,
  LogHistoryControls,
} from '@/components/logs/log-history-controls';
import { resolveLogRange, type LogRangeValue, type LogSortOrder } from '@/lib/log-history';
import { ResourceShareDialog } from '@/components/modals/ResourceShareDialog';
import { ResourceProvenanceLine } from '@/components/resources/ResourceProvenanceLine';
import { CopyButton } from '@/components/ui/copy-button';
import { StatusBadge } from '@/components/ui/status';
import { formatNumber } from '@/lib/format/number';
import { ActionableError } from '@/components/presentation/ActionableError';
import { resolveApiUrl } from '@/lib/base-path';
import { OneTimeSecretField } from '@/pages/deployments/OneTimeSecretField';
import { useWorkflow } from '@/lib/api/queries/workflow';
import { getStartNodeFields, type StartNodeField } from '@/lib/workflow/start-node';
import type { TFunction } from 'i18next';

type TabKey = 'overview' | 'usage' | 'activity' | 'settings';
type CodeLanguage = 'curl' | 'python' | 'javascript';

function deploymentDetailTab(value: string | null): TabKey {
  if (value === 'usage' || value === 'code' || value === 'test') return 'usage';
  if (value === 'activity' || value === 'runs' || value === 'monitoring') return 'activity';
  if (value === 'settings' || value === 'config' || value === 'security') return 'settings';
  return 'overview';
}

function endpointFor(dep: Deployment): string {
  return dep.trigger_type === 'webhook'
    ? `/api/v1/deployments/${dep.slug}/webhook`
    : `/api/v1/deployments/${dep.slug}/invoke`;
}

function last24HoursRange() {
  const to = new Date();
  const from = new Date(to.getTime() - 24 * 3600 * 1000);
  return { from: from.toISOString(), to: to.toISOString() };
}

function triggerLabel(dep: Deployment, t: TFunction): string {
  if (dep.trigger_type === 'api') return 'API';
  return t('deployments.type.webhook', 'Webhook');
}

function OverviewTab({
  dep,
  latestMetric,
  canUpdate,
}: {
  dep: Deployment;
  latestMetric: MetricsPoint | null;
  canUpdate: boolean;
}) {
  const { t } = useTranslation();
  const formatTime = useFormatDateTime();
  const errorRate =
    latestMetric && latestMetric.calls > 0
      ? `${Math.round((latestMetric.errors / latestMetric.calls) * 1000) / 10}%`
      : '0%';

  return (
    <div>
      <OperationalSummary
        label={t('deployments.detail.operationalSummary', 'Deployment health summary')}
        className="mt-5"
        items={[
          {
            label: t('deployments.detail.calls', 'Calls'),
            value: formatNumber(dep.invoke_count ?? 0),
            hint: t('deployments.detail.totalCalls', 'Total recorded API, webhook, and test invocations.'),
            tone: 'info',
          },
          {
            label: t('deployments.detail.errorRate', 'Error rate'),
            value: errorRate,
            hint: t('deployments.detail.errorRateHint', 'Errors divided by calls in the latest metrics bucket.'),
            tone: latestMetric && latestMetric.errors > 0 ? 'danger' : 'success',
          },
          {
            label: t('deployments.detail.p95Latency', 'P95 latency'),
            value: latestMetric?.latency_p95 == null ? '—' : `${latestMetric.latency_p95.toFixed(0)} ms`,
            hint: t('deployments.detail.p95Hint', '95th percentile latency in the latest metrics bucket.'),
            tone: latestMetric?.latency_p95 == null ? 'neutral' : 'warning',
          },
          {
            label: t('deployments.detail.lastInvoked', 'Last invoked'),
            value: formatTime(dep.last_invoked_at),
            hint: t('deployments.detail.lastInvokedHint', 'Most recent request handled by this deployment.'),
          },
        ]}
      />
      <BasicInfoSection dep={dep} canUpdate={canUpdate} />
    </div>
  );
}

function BasicInfoSection({ dep, canUpdate }: { dep: Deployment; canUpdate: boolean }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [name, setName] = useState(dep.name);
  const [editing, setEditing] = useState(false);
  const dirty = name.trim() !== dep.name;
  const patchMutation = useMutation({
    mutationFn: () =>
      patchDeployment(dep.id, {
        name: name.trim(),
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['deployment', dep.id] });
      void qc.invalidateQueries({ queryKey: ['deployments'] });
      setEditing(false);
      toast.success(t('deployments.detail.saved', 'Saved'));
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : String(e)),
  });

  return (
    <SectionBlock
      variant="plain"
      className="max-w-4xl"
      title={t('deployments.detail.basic', 'Basic information')}
      description={t('deployments.detail.basicDescription', 'Identity and workflow routing for this deployment.')}
      actions={canUpdate && !editing ? (
        <Button variant="ghost" size="sm" onClick={() => {
          setName(dep.name);
          setEditing(true);
        }}>
          <Pencil className="mr-2 h-4 w-4" aria-hidden="true" />
          {t('deployments.detail.editBasic', 'Edit')}
        </Button>
      ) : null}
    >
      {editing ? (
        <div className="max-w-xl space-y-4">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="dep-name">{t('deployments.create.fields.name', 'Name')}</Label>
            <Input
              id="dep-name"
              name="deployment-name"
              autoComplete="off"
              value={name}
              onChange={(event) => setName(event.target.value)}
            />
          </div>
          <div className="flex flex-wrap justify-end gap-2">
            <Button
              variant="outline"
              onClick={() => {
                setName(dep.name);
                setEditing(false);
              }}
            >
              {t('common.cancel', 'Cancel')}
            </Button>
            <Button onClick={() => patchMutation.mutate()} disabled={!dirty || !name.trim() || patchMutation.isPending}>
              {patchMutation.isPending
                ? t('common.saving', 'Saving…')
                : t('deployments.detail.saveBasic', 'Save basic information')}
            </Button>
          </div>
        </div>
      ) : (
        <DetailSummary
          className="max-w-3xl gap-y-5"
          items={[
            { label: t('deployments.create.fields.name', 'Name'), value: dep.name },
            { label: t('deployments.create.fields.slug', 'Slug'), value: <span className="font-mono text-xs" translate="no">{dep.slug}</span> },
            { label: t('deployments.create.fields.wfId', 'Workflow ID'), value: <span className="font-mono text-xs" translate="no">{dep.wf_id}</span> },
            { label: t('deployments.create.fields.triggerType', 'Trigger type'), value: triggerLabel(dep, t) },
          ]}
        />
      )}
    </SectionBlock>
  );
}

function ConfigTab({ dep }: { dep: Deployment }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [rateQps, setRateQps] = useState<number>(dep.rate_limit_qps);
  const [enabled, setEnabled] = useState(dep.enabled);

  const dirty = rateQps !== dep.rate_limit_qps || enabled !== dep.enabled;
  const patchMutation = useMutation({
    mutationFn: () => patchDeployment(dep.id, { rate_limit_qps: rateQps, enabled }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['deployment', dep.id] });
      void qc.invalidateQueries({ queryKey: ['deployments'] });
      toast.success(t('deployments.detail.saved', 'Saved'));
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : String(e)),
  });

  return (
    <div className="space-y-5">

      <SectionBlock
        title={t('deployments.detail.trafficControl', 'Traffic and runtime controls')}
        description={t('deployments.detail.trafficControlHelp', 'Control whether this deployment accepts traffic and how many requests per second it allows. Requests above the limit receive HTTP 429.')}
        contentClassName="grid gap-4 md:grid-cols-2"
      >
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="dep-qps">{t('deployments.create.fields.rateLimitQps', 'Rate limit (QPS)')}</Label>
          <Input
            id="dep-qps"
            name="rate-limit-qps"
            type="number"
            min={0}
            value={rateQps}
            onChange={(event) => setRateQps(Number(event.target.value) || 0)}
          />
        </div>
          <label className="flex items-center gap-2 md:self-end md:pb-2">
            <Switch id="dep-enabled" checked={enabled} onCheckedChange={setEnabled} />
            <span className="text-sm">{t('deployments.col.enabled', 'Enabled')}</span>
          </label>
      </SectionBlock>

      <div className="flex justify-end">
        <Button
          onClick={() => patchMutation.mutate()}
          disabled={!dirty || patchMutation.isPending}
        >
          {patchMutation.isPending
            ? t('common.saving', 'Saving…')
            : t('deployments.detail.save', 'Save changes')}
        </Button>
      </div>
    </div>
  );
}

function exampleValue(field: StartNodeField): unknown {
  switch (field.type.toLowerCase()) {
    case 'integer':
    case 'number':
    case 'float':
      return 0;
    case 'boolean':
    case 'bool':
      return true;
    case 'array':
    case 'list':
      return [];
    case 'object':
    case 'dict':
      return {};
    default:
      return `<${field.name}>`;
  }
}

function workflowExampleInputs(fields: StartNodeField[]): Record<string, unknown> {
  return Object.fromEntries(fields.map((field) => [field.name, exampleValue(field)]));
}

function deploymentCodeExamples(
  dep: Deployment,
  exampleInputs: Record<string, unknown>,
): Record<CodeLanguage, string> {
  const endpointPath = endpointFor(dep);
  if (!endpointPath) {
    return { curl: '', python: '', javascript: '' };
  }
  const endpoint = resolveApiUrl(endpointPath);
  const payload = JSON.stringify(exampleInputs);
  if (dep.trigger_type === 'webhook') {
    return {
      curl: [
        `payload='${payload}'`,
        'timestamp="$(date +%s)"',
        'signature="$(printf \'%s\' "${timestamp}.${payload}" | openssl dgst -sha256 -hmac "${FLOWORK_WEBHOOK_SECRET}" | awk \'{print $2}\')"',
        '',
        `curl --request POST '${endpoint}' \\`,
        "  --header 'Content-Type: application/json' \\",
        '  --header "X-Vibecanvas-Timestamp: ${timestamp}" \\',
        '  --header "X-Vibecanvas-Signature: sha256=${signature}" \\',
        '  --data "${payload}"',
      ].join('\n'),
      python: [
        'import hashlib',
        'import hmac',
        'import json',
        'import os',
        'import time',
        '',
        'import requests',
        '',
        `url = ${JSON.stringify(endpoint)}`,
        `payload = json.dumps(json.loads(${JSON.stringify(payload)}), separators=(",", ":"))`,
        'timestamp = str(int(time.time()))',
        'secret = os.environ["FLOWORK_WEBHOOK_SECRET"].encode()',
        'signature = hmac.new(',
        '    secret, f"{timestamp}.{payload}".encode(), hashlib.sha256',
        ').hexdigest()',
        'response = requests.post(',
        '    url,',
        '    data=payload,',
        '    headers={',
        '        "Content-Type": "application/json",',
        '        "X-Vibecanvas-Timestamp": timestamp,',
        '        "X-Vibecanvas-Signature": f"sha256={signature}",',
        '    },',
        '    timeout=60,',
        ')',
        'response.raise_for_status()',
        'print(response.json())',
      ].join('\n'),
      javascript: [
        "import { createHmac } from 'node:crypto';",
        '',
        `const url = ${JSON.stringify(endpoint)};`,
        `const payload = JSON.stringify(${payload});`,
        'const timestamp = Math.floor(Date.now() / 1000).toString();',
        "const secret = process.env.FLOWORK_WEBHOOK_SECRET;",
        "if (!secret) throw new Error('FLOWORK_WEBHOOK_SECRET is required');",
        "const signature = createHmac('sha256', secret)",
        "  .update(`${timestamp}.${payload}`)",
        "  .digest('hex');",
        'const response = await fetch(url, {',
        "  method: 'POST',",
        '  headers: {',
        "    'Content-Type': 'application/json',",
        "    'X-Vibecanvas-Timestamp': timestamp,",
        "    'X-Vibecanvas-Signature': `sha256=${signature}` ,",
        '  },',
        '  body: payload,',
        '});',
        'if (!response.ok) throw new Error(`${response.status} ${await response.text()}`);',
        'console.log(await response.json());',
      ].join('\n'),
    };
  }

  return {
    curl: [
      `curl --request POST '${endpoint}' \\`,
      "  --header 'Content-Type: application/json' \\",
      "  --header 'Authorization: Bearer ${FLOWORK_API_KEY}' \\",
      `  --data '${payload}'`,
    ].join('\n'),
    python: [
      'import os',
      'import json',
      'import requests',
      '',
      `response = requests.post(${JSON.stringify(endpoint)},`,
      '    headers={"Authorization": f"Bearer {os.environ[\'FLOWORK_API_KEY\']}"},',
      `    json=json.loads(${JSON.stringify(payload)}),`,
      '    timeout=60,',
      ')',
      'response.raise_for_status()',
      'print(response.json())',
    ].join('\n'),
    javascript: [
      `const response = await fetch(${JSON.stringify(endpoint)}, {`,
      "  method: 'POST',",
      '  headers: {',
      "    'Content-Type': 'application/json',",
      "    Authorization: `Bearer ${process.env.FLOWORK_API_KEY}` ,",
      '  },',
      `  body: JSON.stringify(${payload}),`,
      '});',
      'if (!response.ok) throw new Error(`${response.status} ${await response.text()}`);',
      'console.log(await response.json());',
    ].join('\n'),
  };
}

function CodeExamplesTab({
  dep,
  exampleInputs,
}: {
  dep: Deployment;
  exampleInputs: Record<string, unknown>;
}) {
  const { t } = useTranslation();
  const [language, setLanguage] = useState<CodeLanguage>('curl');
  const examples = useMemo(
    () => deploymentCodeExamples(dep, exampleInputs),
    [dep, exampleInputs],
  );
  const code = examples[language];
  return (
    <section className="border-y border-edge-subtle py-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <Code2 className="size-4 text-focus" aria-hidden="true" />
            <h2 className="text-sm font-semibold">
              {t('deployments.code.title', 'Call this deployment')}
            </h2>
          </div>
          <p className="mt-1 text-xs text-muted-foreground">
            {t(
              'deployments.code.placeholderHint',
              'Examples use environment-variable placeholders and never include your secret value.',
            )}
          </p>
        </div>
        <CopyButton
          value={code}
          label={t('deployments.code.copy', 'Copy code example')}
          copiedLabel={t('deployments.actions.copied', 'Copied')}
        />
      </div>
      <Tabs value={language} onValueChange={(value) => setLanguage(value as CodeLanguage)} className="mt-4">
        <TabsList aria-label={t('deployments.code.language', 'Code language')}>
          <TabsTrigger value="curl">cURL</TabsTrigger>
          <TabsTrigger value="python">Python</TabsTrigger>
          <TabsTrigger value="javascript">JavaScript</TabsTrigger>
        </TabsList>
        {(['curl', 'python', 'javascript'] as const).map((value) => (
          <TabsContent key={value} value={value} className="mt-3">
            <pre className="app-scrollbar max-h-[32rem] overflow-auto rounded-lg bg-surface-sunken p-4 font-mono text-xs leading-5 text-foreground" data-testid={`deployment-code-${value}`}>
              <code>{examples[value]}</code>
            </pre>
          </TabsContent>
        ))}
      </Tabs>
    </section>
  );
}

function UsageEndpoint({ dep }: { dep: Deployment }) {
  const { t } = useTranslation();
  const endpointPath = endpointFor(dep);
  const endpoint = resolveApiUrl(endpointPath);
  return (
    <section className="border-b border-edge-subtle pb-5">
      <h2 className="text-sm font-semibold">
        {t('deployments.detail.endpoint', 'Endpoint')}
      </h2>
      <p className="mt-1 text-xs text-muted-foreground">
        {t('deployments.detail.endpointHelp', 'Use this address from your application or the examples below.')}
      </p>
      <div className="mt-3 flex min-w-0 items-center gap-2 rounded-md border border-edge-subtle bg-surface-sunken/35 px-3 py-2">
        <code className="min-w-0 flex-1 select-text truncate font-mono text-xs" title={endpoint}>
          {endpoint}
        </code>
        <CopyButton value={endpoint} label={t('deployments.actions.copyEndpoint', 'Copy endpoint')} />
      </div>
    </section>
  );
}

function RunsTab({ depId, active }: { depId: string; active: boolean }) {
  const { t } = useTranslation();
  const formatTime = useFormatDateTime();
  const [statusFilter, setStatusFilter] = useState('all');
  const [runSearch, setRunSearch] = useState('');
  const [logRange, setLogRange] = useState<LogRangeValue>({ range: 'all', from: '', to: '' });
  const [logOrder, setLogOrder] = useState<LogSortOrder>('desc');
  const scrollRegionRef = useRef<HTMLDivElement>(null);
  const logBounds = useMemo(() => resolveLogRange(logRange), [logRange]);
  const query = useInfiniteQuery({
    queryKey: ['deployment-history', depId, statusFilter, logRange, logOrder],
    queryFn: ({ pageParam }) => getHistory(depId, {
      limit: 50,
      cursor: pageParam ?? undefined,
      status: statusFilter === 'all' ? undefined : [statusFilter],
      order: logOrder,
      ...logBounds,
    }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    placeholderData: (previousData) => previousData,
    enabled: active,
    refetchOnWindowFocus: false,
  });
  const rows: HistoryItem[] = useMemo(
    () => {
      const byId = new Map<string, HistoryItem>();
      for (const row of query.data?.pages.flatMap((page) => page.items) ?? []) {
        if (!byId.has(row.id)) byId.set(row.id, row);
      }
      return [...byId.values()];
    },
    [query.data?.pages],
  );
  const visibleRows = useMemo(() => {
    const needle = runSearch.trim().toLocaleLowerCase();
    return rows.filter((row) => {
      if (!needle) return true;
      return [row.id, row.source, row.error, row.status]
        .filter(Boolean)
        .some((value) => String(value).toLocaleLowerCase().includes(needle));
    });
  }, [rows, runSearch]);
  const { fetchNextPage, hasNextPage, isFetchingNextPage } = query;
  const loadMore = useCallback(() => {
    if (hasNextPage && !isFetchingNextPage) void fetchNextPage();
  }, [fetchNextPage, hasNextPage, isFetchingNextPage]);

  if (!active) return null;
  if (query.isLoading) {
    return <div className="empty-state">{t('tasks.loading', 'Loading…')}</div>;
  }
  if (query.isError) {
    return (
      <ActionableError
        title={t('deployments.detail.historyError', 'Failed to load runs.')}
        description={t('deployments.detail.historyErrorHint', 'Check the connection and try loading the latest requests again.')}
        actionLabel={t('retry', 'Retry')}
        onAction={() => void query.refetch()}
        technicalDetails={query.error instanceof Error ? query.error.message : undefined}
      />
    );
  }
  return (
    <div className="space-y-3">
      <LogHistoryControls
        value={logRange}
        order={logOrder}
        onValueChange={setLogRange}
        onOrderChange={setLogOrder}
      >
        <div className="min-w-52 flex-1">
          <Label htmlFor="deployment-run-search">{t('deployments.detail.searchRuns', 'Search runs')}</Label>
          <Input
            id="deployment-run-search"
            className="mt-1.5 h-9"
            value={runSearch}
            onChange={(event) => setRunSearch(event.target.value)}
            placeholder={t('deployments.detail.searchRunsPlaceholder', 'Request ID, source, or error')}
          />
        </div>
        <div className="w-44">
          <Label>{t('tasks.col.status', 'Status')}</Label>
          <Select value={statusFilter} onValueChange={setStatusFilter}>
            <SelectTrigger
              className="mt-1.5 h-9"
              aria-label={t('tasks.col.status', 'Status')}
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">{t('common.all', 'All statuses')}</SelectItem>
              {['queued', 'running', 'succeeded', 'failed', 'cancelled'].map((status) => (
                <SelectItem key={status} value={status}>
                  {t(`tasks.executionStatus.${status}`, status)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <span className="pb-2 text-xs text-content-tertiary">
          {t('logs.loadedVisible', '{{visible}} visible · {{loaded}} loaded', {
            visible: visibleRows.length,
            loaded: rows.length,
          })}
        </span>
      </LogHistoryControls>
      <div
        ref={scrollRegionRef}
        role="region"
        tabIndex={0}
        aria-label={t('deployments.detail.runHistoryRegion', 'Deployment run history')}
        className="app-scrollbar h-96 max-h-[48vh] min-h-64 overflow-auto overscroll-contain rounded-lg border border-edge-subtle bg-surface-base focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
        data-role="deployment-run-log-scroll-region"
      >
      {rows.length === 0 ? (
        <div className="empty-state min-h-full">
          <div className="empty-state-title">{t('deployments.detail.noHistory', 'No runs yet.')}</div>
          <div className="empty-state-copy">
            {t('deployments.detail.noHistoryHint', 'API, webhook, and test invocations will appear here after the first request.')}
          </div>
        </div>
      ) : <>
      <div className="hidden min-w-[58rem] sm:block">
      <table className="w-full text-sm">
        <thead className="sticky top-0 z-10 border-b bg-surface-sunken text-left text-xs font-medium text-muted-foreground shadow-[0_1px_0_var(--color-edge-subtle)]">
          <tr>
            <th className="px-4 py-3 font-medium">{t('deployments.detail.requestId', 'Request id')}</th>
            <th className="px-4 py-3 font-medium">{t('deployments.detail.source', 'Source')}</th>
            <th className="px-4 py-3 font-medium">{t('tasks.col.status', 'Status')}</th>
            <th className="px-4 py-3 font-medium">{t('tasks.col.submitted', 'Started')}</th>
            <th className="px-4 py-3 font-medium">{t('deployments.detail.finished', 'Finished')}</th>
            <th className="px-4 py-3 text-right font-medium">{t('deployments.detail.latency', 'Latency')}</th>
            <th className="px-4 py-3 font-medium">{t('deployments.detail.errorCol', 'Error')}</th>
          </tr>
        </thead>
        <tbody>
          {visibleRows.map((row) => (
            <tr key={row.id} className="border-b last:border-b-0">
              <td className="px-4 py-3 font-mono text-xs">{row.id}</td>
              <td className="px-4 py-3 text-muted-foreground">
                {row.source ? t(`deployments.source.${row.source}`, row.source) : '-'}
              </td>
              <td className="px-4 py-3">{t(`tasks.executionStatus.${row.status}`, row.status)}</td>
              <td className="px-4 py-3 text-muted-foreground">{formatTime(row.started_at ?? row.submitted_at)}</td>
              <td className="px-4 py-3 text-muted-foreground">{formatTime(row.finished_at)}</td>
              <td className="px-4 py-3 text-right tabular-nums text-muted-foreground">
                {row.latency_ms == null ? '-' : `${row.latency_ms.toFixed(0)} ms`}
              </td>
              <td className="max-w-[260px] truncate px-4 py-3 text-xs text-destructive" title={row.error ?? ''}>
                {row.error ?? '-'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {visibleRows.length === 0 ? (
        <div className="px-4 py-8 text-center text-sm text-content-tertiary">
          {t('deployments.detail.noMatchingRuns', 'No runs match these filters.')}
        </div>
      ) : null}
      </div>
      <div className="divide-y divide-edge-subtle px-4 sm:hidden">
        {visibleRows.map((row) => (
          <article key={row.id} className="space-y-3 py-4">
            <div className="flex min-w-0 items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="truncate font-mono text-xs font-medium" title={row.id}>{row.id}</div>
                <div className="mt-1 text-xs text-content-tertiary">
                  {row.source ? t(`deployments.source.${row.source}`, row.source) : '-'}
                </div>
              </div>
              <StatusBadge status={row.status === 'succeeded' ? 'success' : row.status === 'failed' ? 'danger' : 'neutral'}>
                {t(`tasks.executionStatus.${row.status}`, row.status)}
              </StatusBadge>
            </div>
            <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-xs">
              <div>
                <dt className="text-content-tertiary">{t('tasks.col.submitted', 'Started')}</dt>
                <dd className="mt-0.5 text-muted-foreground">{formatTime(row.started_at ?? row.submitted_at)}</dd>
              </div>
              <div>
                <dt className="text-content-tertiary">{t('deployments.detail.finished', 'Finished')}</dt>
                <dd className="mt-0.5 text-muted-foreground">{formatTime(row.finished_at)}</dd>
              </div>
              <div>
                <dt className="text-content-tertiary">{t('deployments.detail.latency', 'Latency')}</dt>
                <dd className="mt-0.5 tabular-nums text-muted-foreground">
                  {row.latency_ms == null ? '-' : `${row.latency_ms.toFixed(0)} ms`}
                </dd>
              </div>
            </dl>
            {row.error ? (
              <p className="rounded-md bg-destructive/5 px-3 py-2 text-xs text-destructive">{row.error}</p>
            ) : null}
          </article>
        ))}
        {visibleRows.length === 0 ? (
          <div className="px-4 py-8 text-center text-sm text-content-tertiary">
            {t('deployments.detail.noMatchingRuns', 'No runs match these filters.')}
          </div>
        ) : null}
      </div>
      </>}
      <IncrementalLogLoader
        hasMore={Boolean(query.hasNextPage)}
        loading={query.isFetchingNextPage}
        onLoadMore={loadMore}
        order={logOrder}
        rootRef={scrollRegionRef}
      />
      </div>
    </div>
  );
}

function MetricLineChart({
  data,
  metric,
  label,
  unit,
  color,
  dash,
  formatTime,
}: {
  data: MetricsPoint[];
  metric: 'calls' | 'errors' | 'latency_p95';
  label: string;
  unit: string;
  color: string;
  dash?: string;
  formatTime: (value?: string | null) => string;
}) {
  const values = data.map((row) => metric === 'latency_p95' ? row.latency_p95 ?? 0 : row[metric]);
  const max = Math.max(1, ...values);
  const points = values.map((value, index) => ({
    value,
    x: values.length <= 1 ? 0 : (index / (values.length - 1)) * 100,
    y: 44 - (value / max) * 40,
    ts: data[index]!.ts,
  }));
  const path = points.map((point, index) => `${index === 0 ? 'M' : 'L'} ${point.x.toFixed(2)} ${point.y.toFixed(2)}`).join(' ');
  return (
    <figure className="min-w-0 border-t border-edge-subtle pt-3 first:border-t-0 first:pt-0 md:border-l md:border-t-0 md:pl-4 md:pt-0 md:first:border-l-0 md:first:pl-0">
      <figcaption className="mb-2 flex items-baseline justify-between gap-3">
        <span className="text-sm font-medium">{label}</span>
        <span className="text-xs tabular-nums text-muted-foreground">{formatNumber(max)} {unit}</span>
      </figcaption>
      <svg viewBox="0 0 100 48" className="h-28 w-full overflow-visible" role="img" aria-label={`${label}; maximum ${max} ${unit}`}>
        <path d="M 0 4 H 100 M 0 24 H 100 M 0 44 H 100" fill="none" stroke="currentColor" strokeWidth="0.6" className="text-edge-subtle" vectorEffect="non-scaling-stroke" />
        <path d={path} fill="none" stroke="currentColor" strokeWidth="1.8" strokeDasharray={dash} className={color} vectorEffect="non-scaling-stroke" />
        {points.map((point) => (
          <circle
            key={point.ts}
            cx={point.x}
            cy={point.y}
            r="1.5"
            tabIndex={0}
            className={color}
            fill="currentColor"
            aria-label={`${formatTime(point.ts)}: ${point.value} ${unit}`}
          >
            <title>{formatTime(point.ts)}: {point.value} {unit}</title>
          </circle>
        ))}
      </svg>
    </figure>
  );
}

function MonitoringTab({ depId, active, onTest }: { depId: string; active: boolean; onTest: () => void }) {
  const { t } = useTranslation();
  const formatTime = useFormatDateTime();
  const query = useQuery({
    queryKey: ['deployment-metrics', depId, 'last-24-hours'],
    queryFn: () => getMetrics(depId, { ...last24HoursRange(), bucket: 'hour' }),
    enabled: active,
    refetchOnWindowFocus: false,
    refetchInterval: active ? 10_000 : false,
  });
  const series = query.data?.series ?? [];

  if (!active) return null;
  if (query.isLoading) return <div className="empty-state">{t('tasks.loading', 'Loading…')}</div>;
  if (query.isError) {
    return (
      <ActionableError
        title={t('deployments.detail.metricsError', 'Failed to load metrics.')}
        description={t('deployments.detail.metricsErrorHint', 'Check the connection and reload the last 24 hours of metrics.')}
        actionLabel={t('retry', 'Retry')}
        onAction={() => void query.refetch()}
        technicalDetails={query.error instanceof Error ? query.error.message : undefined}
      />
    );
  }

  return (
    <div className="space-y-4">
      <section className="border-y border-edge-subtle py-4">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h2 className="text-sm font-semibold">{t('deployments.detail.metricsChart', 'Requests, errors, and latency')}</h2>
            <p className="mt-1 text-xs text-muted-foreground">
              {t('deployments.detail.metricsWindow', 'Last 24 hours, hourly buckets')}
            </p>
          </div>
        </div>
        {series.length === 0 ? (
          <div className="empty-state">
            <div className="empty-state-title">{t('deployments.detail.noMetrics', 'No metrics in this window.')}</div>
            <div className="empty-state-copy">
              {t('deployments.detail.noMetricsHint', 'Metrics appear after an API, webhook, or Test request. Run a test to verify the deployment and generate its first data point.')}
            </div>
            <Button size="sm" variant="outline" onClick={onTest}>
              <Play className="mr-2 h-4 w-4" aria-hidden="true" />
              {t('deployments.detail.runTest', 'Run a test')}
            </Button>
          </div>
        ) : (
          <div className="mt-4 grid gap-4 md:grid-cols-3">
            <MetricLineChart data={series} metric="calls" label={t('deployments.detail.requests', 'Requests')} unit={t('deployments.detail.callsUnit', 'calls')} color="text-state-info" formatTime={formatTime} />
            <MetricLineChart data={series} metric="errors" label={t('deployments.detail.errors', 'Errors')} unit={t('deployments.detail.errorsUnit', 'errors')} color="text-state-danger" dash="4 2" formatTime={formatTime} />
            <MetricLineChart data={series} metric="latency_p95" label={t('deployments.detail.p95Latency', 'P95 latency')} unit="ms" color="text-state-warning" dash="1.5 1.5" formatTime={formatTime} />
          </div>
        )}
      </section>
      {series.length > 0 && (
        <div className="overflow-x-auto border-y border-edge-subtle">
          <table className="w-full text-sm">
            <thead className="border-b bg-surface-sunken text-left text-xs font-medium text-muted-foreground">
              <tr>
                <th className="px-4 py-3 font-medium">{t('deployments.detail.bucket', 'Bucket')}</th>
                <th className="px-4 py-3 text-right font-medium">{t('deployments.detail.calls', 'Calls')}</th>
                <th className="px-4 py-3 text-right font-medium">{t('deployments.detail.errors', 'Errors')}</th>
                <th className="px-4 py-3 text-right font-medium">P50</th>
                <th className="px-4 py-3 text-right font-medium">P95</th>
              </tr>
            </thead>
            <tbody>
              {series.map((row) => (
                <tr key={row.ts} className="border-b last:border-b-0">
                  <td className="px-4 py-3 font-mono text-xs">{formatTime(row.ts)}</td>
                  <td className="px-4 py-3 text-right tabular-nums">{row.calls}</td>
                  <td className="px-4 py-3 text-right tabular-nums text-destructive">{row.errors}</td>
                  <td className="px-4 py-3 text-right tabular-nums">{row.latency_p50 == null ? '-' : row.latency_p50.toFixed(1)}</td>
                  <td className="px-4 py-3 text-right tabular-nums">{row.latency_p95 == null ? '-' : row.latency_p95.toFixed(1)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function TestTab({
  depId,
  exampleInputs,
}: {
  depId: string;
  exampleInputs: Record<string, unknown>;
}) {
  const { t } = useTranslation();
  const [inputsText, setInputsText] = useState(JSON.stringify(exampleInputs, null, 2));
  const [output, setOutput] = useState<string | null>(null);
  const [responseStatus, setResponseStatus] = useState<'idle' | 'success' | 'error'>('idle');
  const [parseError, setParseError] = useState<string | null>(null);
  const mutation = useMutation({
    mutationFn: (inputs: unknown) => testInvoke(depId, inputs),
    onSuccess: (resp) => {
      setResponseStatus('success');
      setOutput(JSON.stringify(resp, null, 2));
    },
    onError: (e) => {
      const message = e instanceof Error ? e.message : String(e);
      setResponseStatus('error');
      setOutput(message);
      toast.error(message);
    },
  });
  const onRun = () => {
    try {
      const parsed = JSON.parse(inputsText);
      setParseError(null);
      mutation.mutate(parsed);
    } catch (error) {
      setParseError(
        `${t('deployments.detail.testInvalidJson', 'Inputs are not valid JSON.')} ${
          error instanceof Error ? error.message : String(error)
        }`,
      );
    }
  };
  return (
    <div className="space-y-4">
      <SectionBlock
        title={t('deployments.detail.testRequest', 'Request')}
        description={t('deployments.detail.testRequestHelp', 'Edit the example generated from the workflow Start node, then send it to this deployment.')}
      >
        <Label htmlFor="dep-test-inputs">{t('deployments.detail.testInputs', 'Inputs (JSON)')}</Label>
        <Textarea
          id="dep-test-inputs"
          rows={8}
          value={inputsText}
          onChange={(event) => setInputsText(event.target.value)}
          className="mt-2 font-mono text-xs"
        />
        {parseError && <div className="mt-2 text-xs text-destructive">{parseError}</div>}
        <div className="mt-3">
          <Button onClick={onRun} disabled={mutation.isPending}>
            <Play className="mr-2 h-4 w-4" aria-hidden="true" />
            {t('deployments.testInvoke.run', 'Run')}
          </Button>
        </div>
      </SectionBlock>
        <SectionBlock
          title={t('deployments.detail.testResponse', 'Response')}
          description={responseStatus === 'idle'
            ? t('deployments.detail.testResponsePending', 'Run the request to see the HTTP result and workflow output here.')
            : responseStatus === 'success'
              ? t('deployments.detail.testResponseSuccess', 'The deployment accepted the request successfully.')
              : t('deployments.detail.testResponseError', 'The request failed. Review the message below, update the request or deployment, and try again.')}
        >
          {output ? (
          <pre className="mt-2 max-h-96 overflow-auto rounded-md border bg-muted/40 p-3 font-mono text-xs">
            {output}
          </pre>
          ) : (
            <div className="rounded-md border border-dashed border-edge-subtle px-4 py-8 text-center text-sm text-content-tertiary">
              {t('deployments.detail.noTestResponse', 'No response yet')}
            </div>
          )}
        </SectionBlock>
    </div>
  );
}

function SecurityTab({ dep }: { dep: Deployment }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [rotated, setRotated] = useState<string | null>(null);
  const [rotateConfirmOpen, setRotateConfirmOpen] = useState(false);
  const [secretSaved, setSecretSaved] = useState(false);
  const rotateMutation = useMutation({
    mutationFn: () => rotateKey(dep.id),
    onSuccess: (resp) => {
      setRotateConfirmOpen(false);
      setSecretSaved(false);
      setRotated(resp.api_key);
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : String(e)),
  });

  return (
    <div className="space-y-4">
      <SectionBlock title={t('deployments.detail.securityModel', 'Access control')}>
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <div className="flex items-center gap-2">
              <ShieldCheck className="h-4 w-4 text-state-success" aria-hidden="true" />
            </div>
            <p className="mt-2 text-sm text-muted-foreground">
              {dep.trigger_type === 'api'
                ? t('deployments.detail.apiKeyHint', 'This API deployment uses a bearer key. The key is only shown at creation or rotation.')
                : t('deployments.detail.webhookHint', 'This webhook deployment uses an HMAC signing secret shown only at creation.')}
            </p>
          </div>
          {dep.trigger_type === 'api' && (
            <Button variant="outline" onClick={() => setRotateConfirmOpen(true)} disabled={rotateMutation.isPending}>
              <KeyRound className="mr-2 h-4 w-4" aria-hidden="true" />
              {t('deployments.detail.rotateKey', 'Rotate API key')}
            </Button>
          )}
        </div>
        <div className="mt-4 max-w-xl">
          <Label>{dep.trigger_type === 'api'
            ? t('deployments.create.apiKey', 'API Key')
            : t('deployments.create.hmacSecret', 'HMAC Secret')}</Label>
          <Input
            type="password"
            value="credential-is-not-retrievable"
            readOnly
            disabled
            className="mt-2 font-mono"
            aria-label={t('deployments.secret.stored', 'Stored credential')}
          />
          <p className="mt-2 text-xs text-muted-foreground">
            {t(
              'deployments.secret.notRetrievable',
              'The existing credential cannot be retrieved. Rotate it to receive a new one-time value.',
            )}
          </p>
        </div>
      </SectionBlock>

      <Dialog open={rotateConfirmOpen} onOpenChange={setRotateConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('deployments.detail.rotateConfirmTitle', 'Rotate API key?')}</DialogTitle>
            <DialogDescription>
              {t('deployments.detail.rotateConfirmDescription', 'The current API key will stop working immediately. Update every client with the new key after rotation.')}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setRotateConfirmOpen(false)} disabled={rotateMutation.isPending}>
              {t('common_cancel', 'Cancel')}
            </Button>
            <Button variant="destructive" onClick={() => rotateMutation.mutate()} disabled={rotateMutation.isPending}>
              {rotateMutation.isPending
                ? t('deployments.detail.rotatingKey', 'Rotating…')
                : t('deployments.detail.rotateKey', 'Rotate API key')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={!!rotated}
        onOpenChange={(open) => {
          if (!open && secretSaved) {
            setRotated(null);
            setSecretSaved(false);
            void qc.invalidateQueries({ queryKey: ['deployment', dep.id] });
          }
        }}
      >
        <DialogContent closeDisabled={!secretSaved}>
          <DialogHeader>
            <DialogTitle>{t('deployments.detail.rotateTitle', 'New API key')}</DialogTitle>
            <DialogDescription>
              {t(
                'deployments.create.oneTimeSecret',
                'This secret is shown only once. Copy it now - you cannot retrieve it later.',
              )}
            </DialogDescription>
          </DialogHeader>
          {rotated ? (
            <div className="space-y-4">
              <OneTimeSecretField
                value={rotated}
                label={t('deployments.create.apiKey', 'API Key')}
                testId="rotated-key"
              />
              <label className="flex cursor-pointer items-start gap-2 rounded-md border border-edge-subtle p-3 text-sm">
                <input
                  type="checkbox"
                  checked={secretSaved}
                  onChange={(event) => setSecretSaved(event.target.checked)}
                  className="mt-0.5 size-4 accent-primary"
                />
                <span>{t('deployments.detail.secretSavedConfirmation', 'I saved the new API key in a secure place.')}</span>
              </label>
            </div>
          ) : null}
          <DialogFooter>
            <Button
              disabled={!secretSaved}
              onClick={() => {
                setRotated(null);
                setSecretSaved(false);
                void qc.invalidateQueries({ queryKey: ['deployment', dep.id] });
              }}
            >
              {t('deployments.create.close', 'Close')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

export function DeploymentDetailPage() {
  const { t } = useTranslation();
  const formatTime = useFormatDateTime();
  const { depId } = useParams<{ depId: string }>();
  const [searchParams, setSearchParams] = useSearchParams();
  const [shareOpen, setShareOpen] = useState(false);
  const requestedTab = searchParams.get('tab');
  const tab = deploymentDetailTab(requestedTab);
  const setTab = (nextTab: TabKey) => {
    const next = new URLSearchParams(searchParams);
    next.set('tab', nextTab);
    setSearchParams(next, { replace: true });
  };
  const query = useQuery({
    queryKey: ['deployment', depId],
    queryFn: () => getDeployment(depId!),
    enabled: !!depId,
    refetchOnWindowFocus: false,
  });
  const metricsQuery = useQuery({
    queryKey: ['deployment-metrics', depId, 'last-24-hours', 'summary'],
    queryFn: () => getMetrics(depId!, { ...last24HoursRange(), bucket: 'hour' }),
    enabled: !!depId && !!query.data?.access?.capabilities.includes('inspect_runs'),
    refetchOnWindowFocus: false,
    refetchInterval: 15_000,
  });
  const workflowQuery = useWorkflow(query.data?.wf_id ?? '');
  const workflowSnapshot = workflowQuery.data?.workflow as Record<string, unknown> | null | undefined;
  const exampleInputs = useMemo(
    () => workflowExampleInputs(getStartNodeFields(workflowSnapshot)),
    [workflowSnapshot],
  );

  if (!depId) {
    return <div className="flex-1 p-6 text-sm text-muted-foreground">{t('deployments.detail.missingId', 'Missing deployment id in URL')}</div>;
  }
  if (query.isLoading) {
    return <div className="flex-1 p-6 text-sm text-muted-foreground">{t('tasks.loading', 'Loading…')}</div>;
  }
  if (query.isError || !query.data) {
    return (
      <div className="flex-1 p-6">
        <ActionableError
          title={t('deployments.detail.loadError', 'Failed to load this deployment.')}
          description={t('deployments.detail.loadErrorHint', 'Return to the deployment list or try loading this deployment again.')}
          actionLabel={t('retry', 'Retry')}
          onAction={() => void query.refetch()}
          technicalDetails={query.error instanceof Error ? query.error.message : String(query.error ?? '')}
          technicalDetailsLabel={t('common.technicalDetails', 'Technical details')}
        />
        <Link to="/deployments" className="mt-4 inline-flex text-sm text-primary underline-offset-4 hover:underline">
          {t('deployments.detail.backToList', 'Back to deployments')}
        </Link>
      </div>
    );
  }

  const dep = query.data;
  const capabilities = new Set(dep.access?.capabilities ?? []);
  const canUpdate = capabilities.has('update');
  const canInspectRuns = capabilities.has('inspect_runs');
  const canExecute = capabilities.has('execute');
  const canManageSecret = capabilities.has('manage_secret');
  const allowedTab = tab === 'activity'
    ? canInspectRuns
    : tab === 'settings'
      ? canUpdate || canManageSecret
      : true;
  const activeTab: TabKey = allowedTab ? tab : 'overview';
  const latestMetric = metricsQuery.data?.series.at(-1) ?? null;
  const versionLabel = dep.version_pin === 'head'
    ? t('deployments.detail.latestVersion', 'Latest version')
    : `v${dep.pinned_major ?? 0}.sv${dep.pinned_sub ?? 0}`;
  const healthStatus = latestMetric && latestMetric.calls > 0 && latestMetric.errors > 0
    ? 'warning'
    : dep.enabled
      ? 'success'
      : 'neutral';

  return (
    <EntityDetailShell
      resourceKind="deployment"
      backTo="/deployments"
      backLabel={t('deployments.detail.backToList', 'Back to deployments')}
      title={dep.name}
      icon={Rocket}
      status={<div className="flex flex-wrap items-center gap-2">
        <StatusBadge status={dep.enabled ? 'success' : 'neutral'}>
          {dep.enabled ? t('deployments.status.active', 'Active') : t('deployments.status.disabled', 'Disabled')}
        </StatusBadge>
        <StatusBadge status={healthStatus}>
          {latestMetric && latestMetric.errors > 0
            ? t('deployments.detail.healthAttention', 'Health needs attention')
            : dep.enabled
              ? t('deployments.detail.healthy', 'Healthy')
              : t('deployments.detail.inactive', 'Inactive')}
        </StatusBadge>
      </div>}
      metadata={<>
        <span>{triggerLabel(dep, t)}</span>
        <span>{versionLabel}</span>
        <span>{t('deployments.detail.updated', 'Updated')}: {formatTime(dep.updated_at ?? dep.created_at)}</span>
        <ResourceProvenanceLine provenance={dep.provenance} />
      </>}
      actions={<>
            <Button
              variant="outline"
              size="sm"
              onClick={() => void Promise.all([
                query.refetch(),
                metricsQuery.refetch(),
              ])}
            >
              <RefreshCw className="mr-2 h-4 w-4" aria-hidden="true" />
              {t('refresh', 'Refresh')}
            </Button>
            {canExecute ? (
              <Button size="sm" onClick={() => setTab('usage')}>
                <Play className="mr-2 h-4 w-4" aria-hidden="true" />
                {t('deployments.detail.testAction', 'Test')}
              </Button>
            ) : null}
            {capabilities.has('manage_access') ? (
              <Button variant="outline" size="sm" onClick={() => setShareOpen(true)}>
                <Share2 className="mr-2 h-4 w-4" aria-hidden="true" />
                {t('deployments.action.share', 'Share deployment')}
              </Button>
            ) : null}
          </>}
    >

        <Tabs value={activeTab} onValueChange={(value) => setTab(value as TabKey)}>
          <TabsList variant="underline" className="chat-scrollbar flex h-auto w-full justify-start overflow-x-auto border-b border-edge-subtle">
            <TabsTrigger value="overview" className="shrink-0">{t('deployments.detail.tabs.overview', 'Overview')}</TabsTrigger>
            <TabsTrigger value="usage" className="shrink-0">
              {t('deployments.detail.tabs.usage', 'Usage')}
            </TabsTrigger>
            {canInspectRuns ? <TabsTrigger value="activity" className="shrink-0">{t('deployments.detail.tabs.activity', 'Activity')}</TabsTrigger> : null}
            {canUpdate || canManageSecret ? <TabsTrigger value="settings" className="shrink-0">{t('deployments.detail.tabs.settings', 'Settings')}</TabsTrigger> : null}
          </TabsList>
          <TabsContent value="overview">
            <OverviewTab
              dep={dep}
              latestMetric={latestMetric}
              canUpdate={canUpdate}
            />
          </TabsContent>
          <TabsContent value="usage" className="space-y-8">
            <UsageEndpoint dep={dep} />
            <CodeExamplesTab dep={dep} exampleInputs={exampleInputs} />
            {canExecute ? (
              <TestTab
                key={JSON.stringify(exampleInputs)}
                depId={depId}
                exampleInputs={exampleInputs}
              />
            ) : null}
          </TabsContent>
          <TabsContent value="activity" className="space-y-8">
            <section className="space-y-3">
              <h2 className="text-sm font-semibold">{t('deployments.detail.recentRuns', 'Recent runs')}</h2>
              <RunsTab depId={depId} active={activeTab === 'activity'} />
            </section>
            <MonitoringTab depId={depId} active={activeTab === 'activity'} onTest={() => setTab('usage')} />
          </TabsContent>
          <TabsContent value="settings" className="space-y-8">
            {canUpdate ? <ConfigTab dep={dep} /> : null}
            {canManageSecret ? <SecurityTab dep={dep} /> : null}
          </TabsContent>
        </Tabs>
        <ResourceShareDialog
          open={shareOpen}
          onOpenChange={setShareOpen}
          resourceKind="deployment"
          resourceId={dep.id}
          resourceName={dep.name}
          effectiveRole={dep.access?.effective_role}
          accessSource={dep.access?.source}
        />
    </EntityDetailShell>
  );
}
