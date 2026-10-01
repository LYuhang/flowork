import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { useApprovalDecision, type ExecutionApproval, type ExecutionDetail } from '@/lib/api/queries/workflow-history';
import { useExecutionHistory } from '../ExecutionHistoryContext';

export function HumanApprovalPanel({ nodeId, instruction }: { nodeId: string; instruction: string }) {
  const history = useExecutionHistory();
  if (!history) return null;
  const node = history.detail.workflow[nodeId];
  const config = node && typeof node === 'object' && 'node_config' in node ? node.node_config : null;
  const frozen = config && typeof config === 'object' && 'instruction' in config ? config.instruction : null;
  const reviewInstruction = typeof frozen === 'string' ? frozen : instruction;
  const approvals = history.detail.approvals.filter((approval) => approval.node_id === nodeId);
  const pending = approvals.filter((approval) => ['pending', 'decision_requested'].includes(approval.status));
  const visible = pending.length ? pending : approvals.slice(-1);
  return <>{visible.map((approval) => <ApprovalCard key={approval.id} detail={history.detail} approval={approval} instruction={reviewInstruction} />)}</>;
}

function ApprovalCard({ detail, approval, instruction }: { detail: ExecutionDetail; approval: ExecutionApproval; instruction: string }) {
  const { t } = useTranslation();
  const [now, setNow] = useState(Date.now);
  const decision = useApprovalDecision(detail.id);
  const pending = ['pending', 'decision_requested'].includes(approval.status);
  useEffect(() => {
    if (!pending) return;
    const timer = window.setInterval(() => setNow(Date.now()), 250);
    return () => window.clearInterval(timer);
  }, [pending]);
  // Relative server time avoids depending on the reviewer's wall clock.
  const deadline = detail.received_at + Date.parse(approval.deadline) - Date.parse(detail.server_time);
  const seconds = Math.max(0, Math.ceil((deadline - now) / 1000));
  const time = `${Math.floor(seconds / 3600).toString().padStart(2, '0')}:${Math.floor(seconds % 3600 / 60).toString().padStart(2, '0')}:${(seconds % 60).toString().padStart(2, '0')}`;
  const disabled = !approval.can_decide || seconds === 0 || decision.isPending;
  return (
    <div className="nodrag nopan nowheel mt-1 w-56 space-y-2 rounded-md border border-edge-structural bg-surface-raised p-2.5 text-xs"
      data-approval-id={approval.id} onDoubleClick={(event) => event.stopPropagation()}>
      <p className="whitespace-pre-wrap break-words text-content-primary">{instruction}</p>
      {pending ? <>
        <p className="font-mono text-content-secondary" role="timer">{seconds > 0 ? t('approval.remaining', { time }) : t('approval.expiring')}</p>
        {approval.status === 'decision_requested' ? <p>{t('approval.status.decision_requested')}</p> : <>
          <div className="flex flex-wrap gap-2">
            <Button size="sm" disabled={disabled} onClick={() => decision.mutate({ approvalId: approval.id, approved: true })}>{t('approval.approve')}</Button>
            <Button size="sm" variant="outline" disabled={disabled} onClick={() => decision.mutate({ approvalId: approval.id, approved: false })}>{t('approval.reject')}</Button>
          </div>
          {!approval.can_decide && <p className="text-content-secondary">{t('approval.waitingForAssignee')}</p>}
        </>}
      </> : <p>{t(`approval.status.${approval.status}`)}</p>}
      {decision.isError && <p role="alert" className="text-state-danger">{t('approval.submitFailed')}</p>}
    </div>
  );
}
