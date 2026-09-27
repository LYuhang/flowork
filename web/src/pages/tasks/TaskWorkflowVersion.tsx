import { useTranslation } from 'react-i18next';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import type { useTaskWorkflowVersion } from './useTaskWorkflowVersion';

export function TaskWorkflowVersion({ selection, batch = false }: {
  selection: ReturnType<typeof useTaskWorkflowVersion>;
  batch?: boolean;
}) {
  const { t } = useTranslation();
  return <div className="mt-3 space-y-2">
    <div className="text-sm font-medium">{t('tasks.version.label', 'Workflow version')}</div>
    <Select value={selection.selector} onValueChange={selection.select} disabled={selection.loading || selection.error}>
      <SelectTrigger aria-label={t('tasks.version.label', 'Workflow version')}><SelectValue /></SelectTrigger>
      <SelectContent>{selection.choices.map(value => <SelectItem key={value} value={value}>
        {value.includes('.sv') ? value : t('tasks.version.latestMajor', '{{major}} · latest saved', { major: value })}
      </SelectItem>)}</SelectContent>
    </Select>
    <p className="text-xs text-muted-foreground">{batch
      ? t('tasks.version.batchHint', 'The submitted batch keeps one saved version, including when resumed.')
      : t('tasks.version.scheduleHint', 'A major follows its latest saved version per execution; a full version stays fixed.')}</p>
    {selection.error && <p role="alert" className="text-xs text-destructive">{t('tasks.version.error', 'Could not load workflow versions.')}</p>}
  </div>;
}
