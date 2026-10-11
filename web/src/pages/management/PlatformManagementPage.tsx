import { useQuery } from '@tanstack/react-query';
import {
  Activity,
  Building2,
  Cpu,
  Database,
  FileClock,
  HardDrive,
  KeyRound,
  LockKeyhole,
  PackageSearch,
  ShieldCheck,
  UserRound,
  UsersRound,
  Workflow,
} from 'lucide-react';
import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';

import { StatusBadge } from '@/components/ui/status';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import {
  getPlatformAuditReport,
  getPlatformManagementOverview,
  type PlatformAuditCategory,
  type PlatformAuditReport,
  type PlatformManagementOverview,
} from '@/lib/api/platform-management';
import { cn } from '@/lib/utils';
import { ActionableError } from '@/components/presentation/ActionableError';
import { Button } from '@/components/ui/button';

const AUDIT_CATEGORIES: PlatformAuditCategory[] = [
  'identity',
  'access_security',
  'resources',
  'data_lifecycle',
  'runtime_operations',
];

const AUDIT_CATEGORY_ICONS = {
  identity: UsersRound,
  access_security: KeyRound,
  resources: PackageSearch,
  data_lifecycle: FileClock,
  runtime_operations: Workflow,
} satisfies Record<PlatformAuditCategory, typeof UsersRound>;

function formatBytes(value: number | null): string {
  if (value === null) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let amount = value;
  let index = 0;
  while (amount >= 1024 && index < units.length - 1) {
    amount /= 1024;
    index += 1;
  }
  return `${amount.toFixed(index > 1 ? 1 : 0)} ${units[index]}`;
}

function formatTimestamp(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value));
}

function MetricCard({
  label,
  value,
  detail,
  icon: Icon,
}: {
  label: string;
  value: string | number;
  detail: string;
  icon: typeof UsersRound;
}) {
  return (
    <article className="rounded-xl border border-edge-subtle bg-surface-raised p-4">
      <div className="flex items-start justify-between gap-3">
        <p className="text-sm text-muted-foreground">{label}</p>
        <span className="grid size-8 place-items-center rounded-xl bg-primary/[0.08] text-primary"><Icon className="size-4" /></span>
      </div>
      <p className="mt-3 text-2xl font-semibold tracking-tight text-content-primary">{value}</p>
      <p className="mt-1 text-xs text-muted-foreground">{detail}</p>
    </article>
  );
}

function OperationsOverview({ data }: { data: PlatformManagementOverview }) {
  const { t } = useTranslation();
  const memoryUsed = data.host.memory.total_bytes !== null && data.host.memory.available_bytes !== null
    ? data.host.memory.total_bytes - data.host.memory.available_bytes
    : null;

  return (
    <div className="space-y-6">
      <section>
        <div className="mb-3 flex items-center gap-2"><UsersRound className="size-4 text-primary" /><h2 className="text-sm font-semibold">{t('management.adoption', 'Accounts & workspaces')}</h2></div>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <MetricCard icon={UsersRound} label={t('management.registeredUsers', 'Registered users')} value={data.identity.registered_users} detail={t('management.newUsers24h', '+{{count}} in the last 24 hours', { count: data.identity.registered_users_24h })} />
          <MetricCard icon={UserRound} label={t('management.onlineUsers', 'Online now')} value={data.identity.online_users_5m} detail={t('management.onlineWindow', 'Active web session in the last 5 minutes')} />
          <MetricCard icon={Building2} label={t('management.companies', 'Companies')} value={data.identity.company_workspaces} detail={t('management.companyDetail', 'Business workspaces')} />
          <MetricCard icon={UserRound} label={t('management.personalUsers', 'Personal users')} value={data.identity.personal_workspaces} detail={t('management.personalDetail', 'Personal workspaces')} />
        </div>
      </section>

      <section>
        <div className="mb-3 flex items-center gap-2"><Cpu className="size-4 text-primary" /><h2 className="text-sm font-semibold">{t('management.infrastructure', 'Infrastructure')}</h2></div>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <MetricCard icon={Cpu} label={t('management.hostLoad', 'Host load')} value={data.host.load_average_1m} detail={t('management.cpuCount', '{{count}} logical CPUs · current API host', { count: data.host.cpu_count })} />
          <MetricCard icon={Database} label={t('management.memory', 'Memory used')} value={formatBytes(memoryUsed)} detail={`${formatBytes(data.host.memory.available_bytes)} ${t('management.available', 'available')}`} />
          <MetricCard icon={HardDrive} label={t('management.disk', 'Disk free')} value={formatBytes(data.host.disk.free_bytes)} detail={`${formatBytes(data.host.disk.total_bytes)} ${t('management.total', 'total')}`} />
          <MetricCard icon={LockKeyhole} label={t('management.sandboxes', 'Resident sandboxes')} value={`${data.sandboxes.resident} / ${data.sandboxes.capacity}`} detail={t('management.sandboxDetail', '{{busy}} busy · this API worker', { busy: data.sandboxes.busy })} />
        </div>
      </section>

      <section className="min-h-0">
        <div className="mb-3 flex items-center gap-2"><Building2 className="size-4 text-primary" /><h2 className="text-sm font-semibold">{t('management.organizationDirectory', 'Organization directory')}</h2></div>
        <div className="overflow-hidden rounded-xl border border-edge-subtle bg-surface-raised">
          <div className="max-h-[26rem] overflow-y-auto overscroll-contain">
            <table className="w-full min-w-[620px] text-left text-sm">
              <thead className="sticky top-0 z-10 border-b border-edge-subtle bg-surface-sunken text-xs text-muted-foreground">
                <tr><th className="px-4 py-2.5 font-medium">{t('organization.name', 'Organization')}</th><th className="px-4 py-2.5 font-medium">{t('management.members', 'Members')}</th><th className="px-4 py-2.5 font-medium">{t('management.activeMembers', 'Active')}</th><th className="px-4 py-2.5 font-medium">{t('management.identifier', 'Identifier')}</th></tr>
              </thead>
              <tbody className="divide-y divide-edge-subtle">
                {data.organizations.map((organization) => (
                  <tr key={organization.organization_id}>
                    <td className="px-4 py-3 font-medium">{organization.name}</td>
                    <td className="px-4 py-3">{organization.member_count}</td>
                    <td className="px-4 py-3">{organization.active_member_count}</td>
                    <td className="max-w-64 truncate px-4 py-3 font-mono text-xs text-muted-foreground">{organization.organization_id}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {data.organizations.length === 0 ? <p className="p-8 text-center text-sm text-muted-foreground">{t('management.noCompanies', 'No company workspaces yet.')}</p> : null}
          </div>
        </div>
      </section>
    </div>
  );
}

function MiniSparkline({ points }: { points: Array<{ total: number; failures: number }> }) {
  const values = points.map((point) => point.total);
  const max = Math.max(1, ...values);
  const path = values.map((value, index) => {
    const x = values.length < 2 ? 50 : (index / (values.length - 1)) * 100;
    const y = 24 - (value / max) * 20;
    return `${index === 0 ? 'M' : 'L'} ${x.toFixed(2)} ${y.toFixed(2)}`;
  }).join(' ');
  return <svg viewBox="0 0 100 28" aria-hidden="true" className="h-7 w-20"><path d={path || 'M 0 24 L 100 24'} fill="none" stroke="currentColor" strokeWidth="2" vectorEffect="non-scaling-stroke" /></svg>;
}

function AuditTrendChart({ category }: { category: PlatformAuditReport['categories'][number] }) {
  const { t } = useTranslation();
  const max = Math.max(1, ...category.series.map((point) => point.total));
  const points = category.series.map((point, index) => ({
    ...point,
    x: category.series.length < 2 ? 50 : (index / (category.series.length - 1)) * 100,
    totalY: 48 - (point.total / max) * 40,
    failureY: 48 - (point.failures / max) * 40,
  }));
  const totalPath = points.map((point, index) => `${index === 0 ? 'M' : 'L'} ${point.x.toFixed(2)} ${point.totalY.toFixed(2)}`).join(' ');
  const failurePath = points.map((point, index) => `${index === 0 ? 'M' : 'L'} ${point.x.toFixed(2)} ${point.failureY.toFixed(2)}`).join(' ');

  return (
    <figure className="rounded-xl border border-edge-subtle bg-surface-raised p-4">
      <figcaption className="flex flex-wrap items-start justify-between gap-3">
        <div><h3 className="text-sm font-semibold">{t('management.audit.eventTrend', 'Event trend')}</h3><p className="mt-1 text-xs text-muted-foreground">{t('management.audit.trendHint', 'Total activity and failed operations in the selected window')}</p></div>
        <div className="flex items-center gap-4 text-xs text-muted-foreground"><span className="flex items-center gap-1.5"><i className="size-2 rounded-full bg-primary" />{t('management.audit.total', 'Total')}</span><span className="flex items-center gap-1.5"><i className="size-2 rounded-full bg-destructive" />{t('management.audit.failures', 'Failures')}</span></div>
      </figcaption>
      {points.length === 0 ? (
        <div className="grid h-52 place-items-center text-sm text-muted-foreground">{t('management.audit.noEvents', 'No audit events in this window.')}</div>
      ) : (
        <svg viewBox="0 0 100 54" role="img" aria-label={t('management.audit.chartLabel', 'Audit event time series')} className="mt-5 h-52 w-full overflow-visible">
          <path d="M 0 8 H 100 M 0 28 H 100 M 0 48 H 100" fill="none" stroke="currentColor" strokeWidth="0.5" className="text-edge-subtle" vectorEffect="non-scaling-stroke" />
          <path d={`${totalPath} L ${points.at(-1)?.x ?? 100} 48 L ${points[0]?.x ?? 0} 48 Z`} fill="currentColor" className="text-primary/10" />
          <path d={totalPath} fill="none" stroke="currentColor" strokeWidth="1.7" className="text-primary" vectorEffect="non-scaling-stroke" />
          <path d={failurePath} fill="none" stroke="currentColor" strokeWidth="1.4" strokeDasharray="3 2" className="text-destructive" vectorEffect="non-scaling-stroke" />
          {points.map((point) => <circle key={point.ts} cx={point.x} cy={point.totalY} r="1.4" fill="currentColor" className="text-primary"><title>{formatTimestamp(point.ts)} · {point.total}</title></circle>)}
        </svg>
      )}
    </figure>
  );
}

function AuditDashboard() {
  const { t } = useTranslation();
  const [windowHours, setWindowHours] = useState(168);
  const [selectedCategory, setSelectedCategory] = useState<PlatformAuditCategory>('identity');
  const report = useQuery({
    queryKey: ['platform-management-audit', windowHours],
    queryFn: () => getPlatformAuditReport(windowHours),
    refetchInterval: 30_000,
    retry: false,
  });
  const category = report.data?.categories.find((item) => item.category === selectedCategory);
  const catalog = report.data?.catalog.find((item) => item.category === selectedCategory);
  const events = useMemo(() => report.data?.recent_events.filter((event) => event.category === selectedCategory) ?? [], [report.data, selectedCategory]);

  if (report.isPending) return <div className="rounded-xl border border-edge-subtle p-10 text-center text-sm text-muted-foreground">{t('common.loading', 'Loading…')}</div>;
  if (!report.data || !category) return <div className="rounded-xl border border-destructive/20 bg-destructive/5 p-6 text-sm text-destructive">{t('management.audit.unavailable', 'Audit telemetry is not available.')}</div>;

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div><h2 className="text-sm font-semibold">{t('management.audit.title', 'Platform audit')}</h2><p className="mt-1 text-xs text-muted-foreground">{t('management.audit.subtitle', 'Immutable security and lifecycle event trends. Customer content and identities remain hidden.')}</p></div>
        <div className="inline-flex rounded-lg bg-surface-sunken p-1" aria-label={t('management.audit.timeWindow', 'Time window')}>
          {[24, 168, 720].map((hours) => <button type="button" key={hours} onClick={() => setWindowHours(hours)} className={cn('rounded-md px-3 py-1.5 text-xs font-medium text-muted-foreground transition-colors', windowHours === hours && 'bg-surface-raised text-content-primary shadow-sm')}>{hours === 24 ? t('management.audit.hours24', '24h') : hours === 168 ? t('management.audit.days7', '7 days') : t('management.audit.days30', '30 days')}</button>)}
        </div>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
        {report.data.categories.map((item) => {
          const Icon = AUDIT_CATEGORY_ICONS[item.category];
          const selected = item.category === selectedCategory;
          return <button type="button" key={item.category} aria-pressed={selected} onClick={() => setSelectedCategory(item.category)} className={cn('rounded-xl border bg-surface-raised p-4 text-left transition-[border-color,background-color,box-shadow] hover:border-primary/30', selected ? 'border-primary/40 bg-primary/[0.035] shadow-sm' : 'border-edge-subtle')}>
            <div className="flex items-center justify-between gap-2 text-primary"><span className="grid size-8 place-items-center rounded-lg bg-primary/[0.08]"><Icon className="size-4" /></span><MiniSparkline points={item.series} /></div>
            <p className="mt-3 text-xl font-semibold tabular-nums">{item.total}</p>
            <p className="mt-0.5 truncate text-xs font-medium text-content-secondary">{t(`management.audit.categories.${item.category}`, item.category)}</p>
            <p className={cn('mt-1 text-xs', item.failures > 0 ? 'text-destructive' : 'text-muted-foreground')}>{t('management.audit.failureCount', '{{count}} failed', { count: item.failures })}</p>
          </button>;
        })}
      </div>

      <Tabs value={selectedCategory} onValueChange={(value) => setSelectedCategory(value as PlatformAuditCategory)}>
        <TabsList variant="underline" className="w-full overflow-x-auto">
          {AUDIT_CATEGORIES.map((item) => <TabsTrigger key={item} value={item}>{t(`management.audit.categories.${item}`, item)}</TabsTrigger>)}
        </TabsList>
        {AUDIT_CATEGORIES.map((item) => <TabsContent key={item} value={item} className="mt-4 space-y-4">
          {item === selectedCategory ? <>
            <AuditTrendChart category={category} />
            <div className="grid gap-4 lg:grid-cols-[minmax(0,1.35fr)_minmax(280px,.65fr)]">
              <section className="overflow-hidden rounded-xl border border-edge-subtle bg-surface-raised">
                <div className="border-b border-edge-subtle px-4 py-3"><h3 className="text-sm font-semibold">{t('management.audit.recentEvents', 'Recent events')}</h3><p className="mt-1 text-xs text-muted-foreground">{t('management.audit.recentHint', 'Identifiers and encrypted private payloads are excluded from this platform view.')}</p></div>
                <div className="max-h-80 overflow-auto overscroll-contain">
                  <table className="w-full min-w-[560px] text-left text-sm"><thead className="sticky top-0 bg-surface-sunken text-xs text-muted-foreground"><tr><th className="px-4 py-2 font-medium">{t('management.audit.time', 'Time')}</th><th className="px-4 py-2 font-medium">{t('management.audit.action', 'Action')}</th><th className="px-4 py-2 font-medium">{t('management.audit.object', 'Object')}</th><th className="px-4 py-2 font-medium">{t('management.audit.outcome', 'Outcome')}</th></tr></thead>
                    <tbody className="divide-y divide-edge-subtle">{events.map((event) => <tr key={event.event_id}><td className="whitespace-nowrap px-4 py-2.5 text-xs text-muted-foreground">{formatTimestamp(event.created_at)}</td><td className="px-4 py-2.5 font-mono text-xs">{event.action}</td><td className="px-4 py-2.5 text-xs text-muted-foreground">{event.target_type ?? '—'}</td><td className="px-4 py-2.5"><StatusBadge status={event.outcome === 'success' ? 'success' : 'danger'}>{event.outcome}</StatusBadge></td></tr>)}</tbody></table>
                  {events.length === 0 ? <p className="p-8 text-center text-sm text-muted-foreground">{t('management.audit.noEvents', 'No audit events in this window.')}</p> : null}
                </div>
              </section>
              <section className="rounded-xl border border-edge-subtle bg-surface-raised p-4">
                <div className="flex items-center justify-between gap-3"><h3 className="text-sm font-semibold">{t('management.audit.coverage', 'Audit coverage')}</h3><StatusBadge status={catalog?.coverage === 'complete' ? 'success' : 'warning'}>{catalog?.coverage ?? 'partial'}</StatusBadge></div>
                <p className="mt-1 text-xs leading-5 text-muted-foreground">{t('management.audit.coverageHint', 'Current immutable event types and objects that still rely on operational telemetry.')}</p>
                <div className="mt-4 max-h-36 space-y-1 overflow-y-auto font-mono text-xs text-content-secondary">{catalog?.actions.map((action) => <div key={action} className="rounded-md bg-surface-sunken px-2 py-1.5">{action}</div>)}</div>
                {(catalog?.missing_objects.length ?? 0) > 0 ? <div className="mt-4"><p className="text-xs font-medium text-muted-foreground">{t('management.audit.notImmutableYet', 'Operational only · immutable events pending')}</p><div className="mt-2 flex flex-wrap gap-1.5">{catalog?.missing_objects.map((object) => <span key={object} className="rounded-md border border-edge-subtle bg-surface-sunken px-2 py-1 font-mono text-xs text-muted-foreground">{object}</span>)}</div></div> : null}
              </section>
            </div>
          </> : null}
        </TabsContent>)}
      </Tabs>

      <section className="flex items-start gap-3 rounded-xl border border-primary/15 bg-primary/[0.045] p-4"><ShieldCheck className="mt-0.5 size-5 shrink-0 text-primary" /><div><h3 className="text-sm font-semibold">{t('management.audit.privacyTitle', 'Metadata-only operator view')}</h3><p className="mt-1 text-sm leading-6 text-muted-foreground">{t('management.audit.privacyDetail', 'No customer content, names, email addresses, tenant IDs, target IDs, IP addresses, user agents, or decrypted audit payloads are returned by this endpoint.')}</p></div></section>
    </div>
  );
}

export function PlatformManagementPage() {
  const { t } = useTranslation();
  const overview = useQuery({
    queryKey: ['platform-management-overview'],
    queryFn: getPlatformManagementOverview,
    refetchInterval: 30_000,
    retry: false,
  });

  if (overview.isPending) return <div className="page-shell"><div className="page-content"><div className="rounded-xl border border-edge-subtle p-10 text-center text-sm text-muted-foreground">{t('common.loading', 'Loading…')}</div></div></div>;
  if (!overview.data) return (
    <div className="page-shell">
      <div className="page-content max-w-3xl">
        <ActionableError
          title={t('management.unavailable', 'Platform management is not available for this account.')}
          description={t('management.unavailableHint', 'This area is limited to platform operators. You can manage your preferences and account in Settings.')}
          actionLabel={t('retry', 'Retry')}
          onAction={() => void overview.refetch()}
          technicalDetails={overview.error instanceof Error ? overview.error.message : undefined}
        />
        <Button asChild variant="outline" className="mt-4">
          <Link to="/settings">{t('management.openSettings', 'Open settings')}</Link>
        </Button>
      </div>
    </div>
  );

  return (
    <div className="page-shell">
      <div className="page-content w-full max-w-6xl gap-6">
        <header className="flex flex-wrap items-start justify-between gap-4 border-b border-edge-subtle pb-5">
          <div><p className="text-xs font-medium uppercase tracking-[0.12em] text-muted-foreground">{t('management.controlPlane', 'Platform control plane')}</p><h1 className="mt-1 text-title">{t('management.title', 'Management')}</h1><p className="mt-1 max-w-3xl text-sm leading-6 text-muted-foreground">{t('management.subtitle', 'Operational health and identity-lifecycle metadata. Customer conversations, files, prompts, credentials, and runtime content are excluded.')}</p></div>
          <StatusBadge status="success">{t(`management.roles.${overview.data.role}`, overview.data.role)}</StatusBadge>
        </header>

        <Tabs defaultValue="overview">
          <TabsList variant="underline"><TabsTrigger value="overview" className="gap-2"><Activity className="size-4" />{t('management.tabs.overview', 'Overview')}</TabsTrigger><TabsTrigger value="audit" className="gap-2"><ShieldCheck className="size-4" />{t('management.tabs.audit', 'Audit')}</TabsTrigger></TabsList>
          <TabsContent value="overview" className="mt-5"><OperationsOverview data={overview.data} /></TabsContent>
          <TabsContent value="audit" className="mt-5"><AuditDashboard /></TabsContent>
        </Tabs>

        <section className="flex items-start gap-3 rounded-xl border border-primary/15 bg-primary/[0.045] p-4"><ShieldCheck className="mt-0.5 size-5 shrink-0 text-primary" /><div><h2 className="text-sm font-semibold">{t('management.privacyBoundary', 'Customer privacy boundary')}</h2><p className="mt-1 text-sm leading-6 text-muted-foreground">{t('management.privacyBoundaryDetail', 'This view contains aggregate metrics and identity lifecycle metadata only. Access to a specific customer resource requires a separate, time-limited support request, independent approval, strong authentication, customer notification, and a complete audit trail.')}</p></div></section>
      </div>
    </div>
  );
}
