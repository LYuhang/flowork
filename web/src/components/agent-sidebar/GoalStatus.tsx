import { useEffect, useState } from 'react';
import { Target, Pause, Play, Pencil, Trash2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';

import type { ChatGoal } from '@/lib/api/queries/chats';

export function GoalStatus({ goal, streaming, disabled, onStop, onCommand }: {
  goal: ChatGoal | null | undefined;
  streaming: boolean;
  disabled: boolean;
  onStop: () => void;
  onCommand: (command: string) => Promise<unknown>;
}) {
  const { t } = useTranslation();
  const [now, setNow] = useState(() => Date.now());
  const [editing, setEditing] = useState(false);
  const [objective, setObjective] = useState('');
  const [pending, setPending] = useState(false);
  useEffect(() => {
    if (goal?.status !== 'active' || !streaming) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [goal?.status, streaming]);
  if (!goal) return null;
  const seconds = Math.max(0, Math.floor(goal.timeUsedSeconds + (
    goal.status === 'active' && streaming ? Math.max(0, now / 1000 - goal.updatedAt) : 0
  )));
  const elapsed = [Math.floor(seconds / 3600), Math.floor(seconds % 3600 / 60), seconds % 60]
    .map((part, index) => index ? String(part).padStart(2, '0') : String(part)).join(':');
  const send = async (command: string) => {
    if (pending || streaming || disabled) return;
    setPending(true);
    try { await onCommand(command); setEditing(false); } finally { setPending(false); }
  };
  const controlsDisabled = disabled || pending || streaming;
  return <div className="flex justify-center px-2" data-role="goal-status">
    <Popover>
      <PopoverTrigger asChild>
        <button type="button" className="inline-flex max-w-full items-center gap-2 rounded-full border border-edge-subtle bg-surface-raised px-3 py-1.5 text-xs shadow-raised hover:bg-accent" aria-label={t('goal.details')}>
          <Target className="size-4 shrink-0" />
          <span className="truncate">{t(`goal.state.${goal.status}`)}</span>
          <span className="shrink-0 tabular-nums text-muted-foreground">{elapsed}</span>
        </button>
      </PopoverTrigger>
      <PopoverContent side="top" align="center" className="w-[min(28rem,calc(100vw-2rem))] rounded-2xl p-3">
        {editing ? <textarea className="min-h-28 w-full rounded-lg border bg-background p-2 text-sm" value={objective} maxLength={4000} onChange={event => setObjective(event.target.value)} aria-label={t('goal.objective')} />
          : <p className="max-h-60 overflow-y-auto whitespace-pre-wrap break-words text-sm">{goal.objective}</p>}
        <div className="mt-3 flex items-center justify-end gap-1">
          {editing ? <Button size="sm" disabled={controlsDisabled || !objective.trim()} onClick={() => void send(`/goal:edit ${objective.trim()}`)}>{t('save')}</Button>
            : <Button variant="ghost" size="icon" disabled={controlsDisabled} aria-label={t('goal.edit')} onClick={() => { setObjective(goal.objective); setEditing(true); }}><Pencil className="size-4" /></Button>}
          {streaming ? <Button variant="ghost" size="icon" disabled={disabled} aria-label={t('goal.pause')} onClick={onStop}><Pause className="size-4" /></Button>
            : goal.status !== 'complete' && <Button variant="ghost" size="icon" disabled={controlsDisabled} aria-label={t('goal.resume')} onClick={() => void send('/goal:resume')}><Play className="size-4" /></Button>}
          <Button variant="ghost" size="icon" disabled={controlsDisabled} aria-label={t('goal.clear')} onClick={() => void send('/goal:clear')}><Trash2 className="size-4" /></Button>
        </div>
      </PopoverContent>
    </Popover>
  </div>;
}
