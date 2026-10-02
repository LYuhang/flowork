import CodeMirror from '@uiw/react-codemirror';
import { python } from '@codemirror/lang-python';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import type { EvaluationConfig } from '@/lib/api/tasks';

export const EVALUATION_TEMPLATE = `def evaluate(results: list[dict]) -> dict:
    total = len(results)
    succeeded = sum(1 for row in results if row.get("status") == "success")
    return {
        "total": total,
        "execution_success_rate": succeeded / total if total else None,
    }
`;

export function EvaluationEditor({ value, onChange, disabled = false, alwaysOpen = false }: {
  value: EvaluationConfig; onChange: (value: EvaluationConfig) => void; disabled?: boolean; alwaysOpen?: boolean;
}) {
  const { t } = useTranslation();
  return <section className="space-y-3">
    <label className="flex items-center gap-2 text-sm">
      <input type="checkbox" checked={value.enabled} disabled={disabled}
        onChange={e => onChange({ ...value, enabled: e.target.checked })} />
      {t('evaluation.afterInference', 'Evaluate after inference')}
    </label>
    {(alwaysOpen || value.enabled) && <>
      <div className="flex items-center justify-between gap-3">
        <p className="text-xs text-muted-foreground">{t('evaluation.contract', 'evaluate(results) receives all result dictionaries and returns a metrics dictionary. Approved standard library only; no third-party libraries.')}</p>
        <Button type="button" variant="outline" size="sm" disabled={disabled} onClick={() => onChange({ ...value, script: EVALUATION_TEMPLATE })}>
          {t('evaluation.useTemplate', 'Use template')}
        </Button>
      </div>
      <div className="overflow-hidden rounded-md border">
        <CodeMirror value={value.script} onChange={script => onChange({ ...value, script })}
          extensions={[python()]} placeholder={EVALUATION_TEMPLATE} height="220px" editable={!disabled}
          aria-label={t('evaluation.script', 'Evaluation script')} />
      </div>
    </>}
  </section>;
}
