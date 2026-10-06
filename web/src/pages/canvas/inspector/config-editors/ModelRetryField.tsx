import { useTranslation } from 'react-i18next';
import { Label } from '@/components/ui/label';
import { CommitOnBlurNumber } from '@/pages/canvas/inspector/CommitOnBlur';

export function ModelRetryField({ value, readOnly, onChange }: {
  value: unknown; readOnly: boolean; onChange: (value: number) => void;
}) {
  const { t } = useTranslation();
  return <div className="space-y-1.5">
    <Label htmlFor="model-retry" className="text-xs font-medium">{t('model_retry.label')}</Label>
    <CommitOnBlurNumber id="model-retry" kind="int" min={0} max={10} step={1}
      value={typeof value === 'number' ? value : 0} onCommit={onChange}
      disabled={readOnly} className="h-8 text-xs" data-testid="cfg-model-retry" />
    <p className="text-xs text-muted-foreground">{t('model_retry.hint')}</p>
  </div>;
}
