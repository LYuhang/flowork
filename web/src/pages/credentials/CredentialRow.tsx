/** One credential row. Provider keys are write-only and never revealable. */
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { CircleCheck, CircleX, LoaderCircle, Pencil, PlugZap, Trash2 } from 'lucide-react';

import { useFormatDateTime } from '@/lib/timezone';
import { formatNumber } from '@/lib/format/number';
import {
  testLlmCredentialConnection,
  type CredentialConnectionTest,
  type CredentialPublic,
} from '@/lib/api/llm-credentials';
import { ResourceIcon } from '@/components/presentation/ResourceIcon';

interface CredentialRowProps {
  cred: CredentialPublic;
  onEdit: (cred: CredentialPublic) => void;
  onDelete: (cred: CredentialPublic) => void;
}

const MASK = '••••••••';

export function CredentialRow({ cred, onEdit, onDelete }: CredentialRowProps) {
  const { t } = useTranslation();
  const formatTime = useFormatDateTime();
  const capabilities = new Set(cred.access?.capabilities ?? []);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<CredentialConnectionTest | null>(null);

  const testConnection = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      setTestResult(await testLlmCredentialConnection(cred.id));
    } catch {
      setTestResult({ ok: false, outcome: 'unreachable', latency_ms: 0, upstream_status: null });
    } finally {
      setTesting(false);
    }
  };

  return (
    <tr className="border-b last:border-b-0 hover:bg-muted/30">
      <td className="px-3 py-2">
        <div className="flex min-w-0 items-start gap-2.5">
          <ResourceIcon kind="credential" size="sm" />
          <div className="min-w-0">
            <div className="truncate font-medium">{cred.name}</div>
            {cred.description && (
              <div className="mt-0.5 max-w-[280px] truncate text-xs text-muted-foreground">
                {cred.description}
              </div>
            )}
          </div>
        </div>
      </td>
      <td className="px-3 py-2">
        <span className="inline-flex items-center rounded-full bg-secondary px-2 py-0.5 text-xs font-medium text-secondary-foreground">
          {cred.provider}
        </span>
      </td>
      <td className="px-3 py-2">
        <div className="flex items-center gap-2">
          <code className="font-mono text-xs text-muted-foreground">{MASK}</code>
          <span className="text-xs text-muted-foreground">
            {t('credentials.stored', 'Stored · write-only')}
          </span>
        </div>
      </td>
      <td className="px-3 py-2 text-xs text-muted-foreground">
        {cred.model_context_tokens
          ? formatNumber(cred.model_context_tokens)
          : t('credentials.context_default', 'Default')}
      </td>
      <td className="px-3 py-2 text-muted-foreground">
        {formatTime(cred.updated_at)}
      </td>
      <td className="px-3 py-2">
        <div className="flex flex-wrap items-center gap-1">
          {capabilities.has('manage_secret') ? <button
            type="button"
            onClick={() => void testConnection()}
            disabled={testing}
            title={t('credentials.testConnection', 'Test connection')}
            aria-label={t('credentials.testConnection', 'Test connection')}
            className="grid size-10 place-items-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:cursor-wait disabled:opacity-60"
          >
            {testing ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <PlugZap className="h-4 w-4" />}
          </button> : null}
          {capabilities.has('manage_secret') ? <button
            type="button"
            onClick={() => onEdit(cred)}
            title={t('credentials.edit', 'Edit')}
            aria-label={t('credentials.edit', 'Edit')}
            className="grid size-10 place-items-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
          >
            <Pencil className="h-4 w-4" />
          </button> : null}
          {capabilities.has('delete') ? <button
            type="button"
            onClick={() => onDelete(cred)}
            title={t('credentials.delete', 'Delete')}
            aria-label={t('credentials.delete', 'Delete')}
            className="grid size-10 place-items-center rounded-md text-muted-foreground transition-colors hover:bg-destructive/10 hover:text-destructive"
          >
            <Trash2 className="h-4 w-4" />
          </button> : null}
          <span className="ml-1 min-w-24 text-xs text-muted-foreground" aria-live="polite">
            {testResult ? (
              <span className={testResult.ok ? 'text-state-success' : 'text-destructive'}>
                {testResult.ok
                  ? <><CircleCheck className="mr-1 inline size-3.5" />{t('credentials.connected', 'Connected')} · {testResult.latency_ms} ms</>
                  : <><CircleX className="mr-1 inline size-3.5" />{t(`credentials.test.${testResult.outcome}`, testResult.outcome.replaceAll('_', ' '))}</>}
              </span>
            ) : null}
          </span>
        </div>
      </td>
    </tr>
  );
}
