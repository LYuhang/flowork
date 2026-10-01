import { useId } from 'react';
import { useTranslation } from 'react-i18next';
import { Label } from '@/components/ui/label';
import { CommitOnBlurInput, CommitOnBlurNumber, CommitOnBlurTextarea } from '../CommitOnBlur';
import type { NodeConfigEditorProps } from './types';

export function HumanApprovalNodeEditor({ config, readOnly, onChange }: NodeConfigEditorProps) {
  const { t } = useTranslation();
  const id = useId();
  return (
    <div className="space-y-3">
      <div className="space-y-1.5">
        <Label htmlFor={`${id}-instruction`}>{t('approval.instruction')}</Label>
        <CommitOnBlurTextarea
          id={`${id}-instruction`}
          value={typeof config.instruction === 'string' ? config.instruction : ''}
          onCommit={(instruction) => onChange({ ...config, instruction })}
          disabled={readOnly}
          maxLength={4000}
          rows={4}
        />
      </div>
      <div className="space-y-1.5">
        <Label htmlFor={`${id}-email`}>{t('approval.approverEmail')}</Label>
        <CommitOnBlurInput
          id={`${id}-email`}
          type="email"
          value={typeof config.approver_email === 'string' ? config.approver_email : ''}
          onCommit={(approver_email) => onChange({ ...config, approver_email: approver_email.trim() })}
          disabled={readOnly}
          maxLength={254}
          aria-describedby={`${id}-email-hint`}
        />
        <p id={`${id}-email-hint`} className="text-xs text-content-secondary">{t('approval.approverHint')}</p>
      </div>
      <div className="space-y-1.5">
        <Label htmlFor={`${id}-timeout`}>{t('approval.timeoutSeconds')}</Label>
        <CommitOnBlurNumber
          id={`${id}-timeout`}
          value={typeof config.timeout_seconds === 'number' ? config.timeout_seconds : 3600}
          onCommit={(timeout_seconds) => onChange({ ...config, timeout_seconds: Math.max(1, timeout_seconds) })}
          disabled={readOnly}
          kind="int"
          min={1}
          step={1}
        />
      </div>
      <p className="text-xs text-content-secondary">{t('approval.outputHint')}</p>
    </div>
  );
}
