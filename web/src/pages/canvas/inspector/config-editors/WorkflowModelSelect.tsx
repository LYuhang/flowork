import { useTranslation } from 'react-i18next';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Button } from '@/components/ui/button';
import { useWorkflowModels } from '@/lib/api/queries/llm-credentials';

/** Same server-authorized manual API catalog as CLI/check/execution. */
export function WorkflowModelSelect({ value, onChange, readOnly, testId }: {
  value: string;
  onChange: (value: string) => void;
  readOnly?: boolean;
  testId: string;
}) {
  const { t } = useTranslation();
  const query = useWorkflowModels();
  const models = query.data?.models ?? {};
  const names = Object.keys(models);
  const unavailable = !!value && !Object.hasOwn(models, value);
  const ready = !query.isPending && !query.isError;
  return (
    <div className="space-y-1.5">
      <Select value={value} disabled={readOnly || !ready || names.length === 0}
        onValueChange={(next) => { if (ready && Object.hasOwn(models, next)) onChange(next); }}>
        <SelectTrigger className="h-8 text-xs" data-testid={testId} aria-label={t('workflow_model_picker.label', 'Model API')}>
          <SelectValue placeholder={query.isPending
            ? t('workflow_model_picker.loading', 'Loading model APIs…')
            : t('workflow_model_picker.select', 'Select a manually added API')} />
        </SelectTrigger>
        <SelectContent>
          {unavailable && <SelectItem disabled value={value} className="text-xs">{value} ({t('workflow_model_picker.unavailable', 'unavailable')})</SelectItem>}
          {names.map((name) => <SelectItem key={name} value={name} className="text-xs">{name} ({models[name].provider})</SelectItem>)}
        </SelectContent>
      </Select>
      {query.isError ? <div role="alert" className="text-xs">
        {t('workflow_model_picker.error', 'Could not load model APIs. Selection is unavailable.')}
        <Button variant="link" size="sm" onClick={() => void query.refetch()}>{t('workflow_model_picker.retry', 'Retry')}</Button>
      </div> : ready && names.length === 0 ? <p role="status" className="text-xs">
        {t('workflow_model_picker.empty', 'Add a model API in API Management first. OpenRouter account connections cannot be used in workflows.')}
      </p> : ready && unavailable ? <p role="status" className="text-xs">
        {t('workflow_model_picker.replace', 'The saved model is unavailable for workflows. Select a manually added API; the saved value has not been changed.')}
      </p> : null}
    </div>
  );
}
