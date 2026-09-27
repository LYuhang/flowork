import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { getApiBase } from '@/lib/base-path';
import { useAuthStore } from '@/stores/auth';
import type { InteractiveArtifact } from './interactive-artifact-contract';

type Decision = { status: string; interaction_result_json?: { selected_ids?: string[]; status?: string } };
type Option = { value: string; label: string; description?: string };

function headers() {
  const token = useAuthStore.getState().token;
  return { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) };
}

/** A live tool's input, not a new Chat message or a file-transfer approval. */
export function ChoicesCard({ artifact }: { artifact: InteractiveArtifact }) {
  const { t } = useTranslation();
  const groupId = useId();
  const question = (artifact.props?.questions as { multiple?: boolean; options: Option[] }[])[0];
  const options = question.options;
  const storageKey = `flowork:choice-draft:${artifact.artifact_id}`;
  const [draft, setDraft] = useState<string[]>(() => {
    try {
      const saved = JSON.parse(sessionStorage.getItem(storageKey) || 'null');
      if (Array.isArray(saved)) return saved.filter((id) => options.some((o) => o.value === id));
    } catch { /* Storage can be disabled. The server draft remains available. */ }
    const saved = artifact.widget_state?.selected_ids;
    return Array.isArray(saved) ? saved.filter((id): id is string => typeof id === 'string') : [];
  });
  const initialDecision = artifact.interaction_state?.status
    ? { status: artifact.interaction_state.status, interaction_result_json: artifact.interaction_state.result as Decision['interaction_result_json'] }
    : null;
  const [decision, setDecision] = useState<Decision | null>(initialDecision);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const submitting = useRef(false);
  const terminal = useRef(Boolean(initialDecision && initialDecision.status !== 'pending'));
  const draftWrites = useRef(Promise.resolve());
  const hitlId = artifact.hitl_request_id;
  const endpoint = `${getApiBase()}/api/v1/hitl-requests/${encodeURIComponent(hitlId || '')}`;
  const frozen = Boolean(decision && decision.status !== 'pending');
  const selected = frozen ? decision?.interaction_result_json?.selected_ids || [] : draft;

  const accept = useCallback((value: Decision) => {
    if (terminal.current && value.status === 'pending') return;
    if (value.status !== 'pending') {
      terminal.current = true;
      try { sessionStorage.removeItem(storageKey); } catch { /* Optional local draft. */ }
    }
    setDecision(value);
  }, [storageKey]);

  useEffect(() => {
    if (artifact.interaction_state?.status && artifact.interaction_state.status !== 'pending') {
      accept({ status: artifact.interaction_state.status,
        interaction_result_json: artifact.interaction_state.result as Decision['interaction_result_json'] });
    }
  }, [artifact.interaction_state, accept]);

  useEffect(() => {
    if (!hitlId) return;
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      try {
        const response = await fetch(endpoint, { headers: headers(), signal: abort.signal });
        if (response.ok && !abort.signal.aborted) accept(await response.json() as Decision);
      } catch { /* Keep the draft; a temporary disconnect does not cancel input. */ }
      if (!abort.signal.aborted && !terminal.current) timer = setTimeout(() => void refresh(), 3000);
    };
    void refresh();
    return () => { abort.abort(); clearTimeout(timer); };
  }, [endpoint, hitlId, accept]);

  const choose = (id: string) => {
    if (busy || frozen) return;
    const next = question.multiple
      ? draft.includes(id) ? draft.filter((value) => value !== id) : [...draft, id]
      : [id];
    setDraft(next);
    try { sessionStorage.setItem(storageKey, JSON.stringify(next)); } catch { /* Optional local draft. */ }
    // Serialize draft writes so a slower old choice cannot replace a newer one.
    // Confirmation remains a separate atomic operation; a draft is never consent.
    draftWrites.current = draftWrites.current.then(async () => {
      if (terminal.current || !artifact.artifact_id) return;
      await fetch(`${getApiBase()}/api/v1/interactive-artifacts/${encodeURIComponent(artifact.artifact_id)}/state`, {
        method: 'PUT', headers: headers(), signal: AbortSignal.timeout(15000),
        body: JSON.stringify({ state: { selected_ids: next } }),
      });
    }).catch(() => { /* The session draft survives offline/refresh. */ });
  };

  const submit = async (action: 'submit' | 'cancel') => {
    if (submitting.current || frozen || !hitlId || (action === 'submit' && !draft.length)) return;
    submitting.current = true;
    setBusy(true);
    setError(false);
    try {
      // Reconcile before retrying an uncertain POST. Never assume a timeout means
      // the first confirmation failed, or overwrite another tab's choice.
      const current = await fetch(endpoint, { headers: headers(), signal: AbortSignal.timeout(15000) });
      if (!current.ok) throw new Error('Choice state unavailable');
      const saved = await current.json() as Decision;
      accept(saved);
      if (saved.status !== 'pending') return;
      const response = await fetch(`${endpoint}/decision`, {
        method: 'POST', headers: headers(), signal: AbortSignal.timeout(15000),
        body: JSON.stringify({ decision: action, decision_payload: { widget_state: { selected_ids: draft } } }),
      });
      if (!response.ok) throw new Error('Choice was not accepted');
      accept(await response.json() as Decision);
    } catch {
      setError(true);
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  };

  const expired = decision?.interaction_result_json?.status === 'expired';
  return (
    <section className="min-w-0 overflow-hidden rounded-lg border border-edge-structural bg-surface-raised" data-role="choices-card">
      <div className="space-y-1 border-b border-edge-subtle p-3">
        <h3 id={`${groupId}-title`} className="break-words text-sm font-semibold">{artifact.title}</h3>
        {artifact.props?.message ? <p className="break-words text-xs text-muted-foreground">{String(artifact.props.message)}</p> : null}
      </div>
      <fieldset disabled={busy || frozen} aria-labelledby={`${groupId}-title`} className="max-h-72 space-y-2 overflow-y-auto p-3">
        {options.map((option) => (
          <label key={option.value} className={`flex min-h-10 min-w-0 items-start gap-2 rounded-md border px-3 py-2 text-sm ${selected.includes(option.value) ? 'border-primary bg-primary/5' : 'border-edge-subtle'} ${busy || frozen ? '' : 'cursor-pointer hover:bg-surface-hover'}`}>
            <input className="mt-1 shrink-0 accent-primary" name={groupId} type={question.multiple ? 'checkbox' : 'radio'}
              checked={selected.includes(option.value)} onChange={() => choose(option.value)} />
            <span className="min-w-0 [overflow-wrap:anywhere]">
              <span className="block">{option.label}</span>
              {option.description ? <span className="mt-0.5 block text-xs text-muted-foreground">{option.description}</span> : null}
            </span>
          </label>
        ))}
      </fieldset>
      <div className="flex flex-wrap items-center gap-2 border-t border-edge-subtle p-3" aria-live="polite">
        {frozen ? <span className="text-xs text-muted-foreground">{decision?.status === 'submitted'
          ? t('tool.choices.confirmed', 'Selection confirmed') : expired
            ? t('tool.choices.expired', 'This tool call ended. Selection is no longer available.')
            : t('tool.choices.cancelled', 'Selection cancelled')}</span> : <>
          <span className="mr-auto text-xs text-muted-foreground">{t('tool.choices.count', '{{count}} selected', { count: draft.length })}</span>
          <button type="button" onClick={() => void submit('cancel')} disabled={busy || !hitlId}
            className="min-h-8 rounded-md border border-edge-structural px-3 text-xs disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">{t('common.cancel', 'Cancel')}</button>
          <button type="button" onClick={() => void submit('submit')} disabled={busy || !hitlId || !draft.length}
            className="min-h-8 rounded-md bg-primary px-3 text-xs text-primary-foreground disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">{busy ? t('common.saving', 'Saving...') : t('tool.choices.confirm', 'Confirm selection')}</button>
        </>}
        {error && !frozen ? <p role="alert" className="w-full text-xs text-destructive">{t('tool.choices.unknown', 'Confirmation could not be verified. Your selection is preserved; retry to check its status.')}</p> : null}
      </div>
    </section>
  );
}
