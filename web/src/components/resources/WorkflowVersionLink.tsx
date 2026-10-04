import { Link, useLocation } from 'react-router';
import { useTranslation } from 'react-i18next';
import { GitBranch } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { workflowVersionHref } from '@/lib/workflow/version-link';

export function WorkflowVersionLink({ workflowId, version, kind = 'snapshot', inline = false }: {
  workflowId: string | null | undefined;
  version: unknown;
  kind?: 'snapshot' | 'configured' | 'execution' | 'serving';
  inline?: boolean;
}) {
  const { t } = useTranslation();
  const location = useLocation();
  const target = workflowVersionHref(workflowId, version);
  const returnTo = location.pathname + location.search + location.hash;
  const href = target ? `${target}&returnTo=${encodeURIComponent(returnTo)}` : null;
  const label = {
    snapshot: t('workflowVersionLink.snapshot', 'View workflow version'),
    configured: t('workflowVersionLink.configured', 'View configured version'),
    execution: t('workflowVersionLink.execution', 'View selected run version'),
    serving: t('workflowVersionLink.serving', 'View serving version'),
  }[kind];
  if (!href) return <span className="text-xs text-muted-foreground" role="status">
    {t('workflowVersionLink.unavailable', 'Exact workflow version is not yet available.')}
  </span>;
  if (inline) return <Link to={href} className="rounded font-mono text-primary underline underline-offset-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    title={t('workflowVersionLink.readOnly', 'Open the exact saved workflow version in a read-only canvas')} translate="no">{String(version)}</Link>;
  return <Button asChild variant="outline" size="sm">
    <Link to={href} title={t('workflowVersionLink.readOnly', 'Open the exact saved workflow version in a read-only canvas')}>
      <GitBranch className="size-4" aria-hidden="true" />
      {label} <span className="font-mono" translate="no">{String(version)}</span>
    </Link>
  </Button>;
}
