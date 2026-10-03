import { ScheduleTimeError, zonedWallClockToIso } from '@/lib/schedule-time';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import { ChevronDown } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { DateTimePicker } from '@/components/ui/date-time-picker';
import { Select, SelectContent, SelectGroup, SelectItem, SelectLabel, SelectTrigger, SelectValue } from '@/components/ui/select';
import { SearchSelect, type SearchSelectOption } from '@/components/ui/search-select';
import { createScheduledRun, type ScheduledRunResponse } from '@/lib/api/tasks';
import { useWorkspaceList } from '@/lib/api/queries/workflows';
import { TaskWorkflowVersion } from './TaskWorkflowVersion';
import { useTaskWorkflowVersion } from './useTaskWorkflowVersion';
import { getStartNodeFields } from '@/lib/workflow/start-node';
import { TIMEZONE_GROUPS } from '@/lib/timezone-list';
import { describeCronExpression, scheduleLocale } from '@/lib/cron-description';

function workflowOptions(
  workflows: Array<{ wf_id: string; workflow_name?: string | null; description?: string | null }>,
): SearchSelectOption[] {
  return workflows.map((wf) => ({
    value: wf.wf_id,
    label: wf.workflow_name || wf.wf_id,
    meta: wf.wf_id,
    description: wf.description ?? '',
    keywords: [wf.workflow_name ?? '', wf.wf_id, wf.description ?? ''],
  }));
}

function parsePresetValue(raw: string, type: string): unknown {
  const value = raw.trim();
  if (value === '') return '';
  const lower = type.toLowerCase();
  if (lower.includes('int') || lower.includes('float') || lower.includes('number')) {
    const n = Number(value);
    if (!Number.isFinite(n) || (lower.includes('int') && !Number.isInteger(n))) throw new Error('Invalid numeric input.');
    return n;
  }
  if (lower.includes('bool')) {
    if (/^(true|yes|1)$/i.test(value)) return true;
    if (/^(false|no|0)$/i.test(value)) return false;
    throw new Error('Invalid boolean input.');
  }
  if (lower.includes('list') || lower.includes('array') || lower.includes('object') || lower.includes('dict')) {
    try {
      const parsed = JSON.parse(value);
      if (lower.includes('array') || lower.includes('list')) {
        if (!Array.isArray(parsed)) throw new Error('Expected an array.');
      } else if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('Expected an object.');
      return parsed;
    } catch {
      throw new Error('Invalid JSON input.');
    }
  }
  return raw;
}

type ScheduleFrequency = 'hourly' | 'daily' | 'weekly' | 'monthly' | 'custom';

function splitTime(value: string): { hour: number; minute: number } {
  const [rawHour, rawMinute] = value.split(':');
  return {
    hour: Math.min(23, Math.max(0, Number(rawHour) || 0)),
    minute: Math.min(59, Math.max(0, Number(rawMinute) || 0)),
  };
}

function scheduleCron({
  frequency,
  time,
  hourlyMinute,
  weekday,
  monthday,
  customCron,
}: {
  frequency: ScheduleFrequency;
  time: string;
  hourlyMinute: number;
  weekday: number;
  monthday: number;
  customCron: string;
}): string {
  const { hour, minute } = splitTime(time);
  if (frequency === 'hourly') return `${hourlyMinute} * * * *`;
  if (frequency === 'daily') return `${minute} ${hour} * * *`;
  if (frequency === 'weekly') return `${minute} ${hour} * * ${weekday}`;
  if (frequency === 'monthly') return `${minute} ${hour} ${monthday} * *`;
  return customCron.trim();
}

export function ScheduledRunCreatePanel({
  onCancel,
  onCreated,
  context,
}: {
  onCancel: () => void;
  onCreated: (taskId: string, response: ScheduledRunResponse) => void;
  context?: { workflowId: string; version: string; workflow: Record<string, unknown>; name?: string; dirty: boolean; initialInputs?: Record<string, unknown>; prepare: () => Promise<string> };
}) {
  const { t, i18n } = useTranslation();
  const workflowsQuery = useWorkspaceList(200, 0);
  const workflows = useMemo(() => workflowsQuery.data?.items ?? [], [workflowsQuery.data?.items]);
  const workflowSelectOptions = useMemo(() => workflowOptions(workflows), [workflows]);
  const [selectedWorkflowId, setSelectedWorkflowId] = useState('');
  const effectiveWorkflowId = context?.workflowId || selectedWorkflowId || workflows[0]?.wf_id || '';
  const [customName, setCustomName] = useState<string | null>(null);
  const [scheduleMode, setScheduleMode] = useState<'once' | 'calendar' | 'interval'>('calendar');
  const [frequency, setFrequency] = useState<ScheduleFrequency>('daily');
  const [time, setTime] = useState('09:00');
  const [hourlyMinute, setHourlyMinute] = useState(0);
  const [weekday, setWeekday] = useState(1);
  const [monthday, setMonthday] = useState(1);
  const [customCron, setCustomCron] = useState('0 9 * * *');
  const [intervalValue, setIntervalValue] = useState(1);
  const [intervalUnit, setIntervalUnit] = useState<'minutes' | 'hours' | 'days'>('hours');
  const [timezone, setScheduleTimezone] = useState(
    Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC',
  );
  const [runAt, setRunAt] = useState('');
  const [startAt, setStartAt] = useState('');
  const [endAt, setEndAt] = useState('');
  const [enabled, setEnabled] = useState(true);
  const [mountEnabled, setMountEnabled] = useState(false);
  const [notifySuccess, setNotifySuccess] = useState(false);
  const [notifyFailure, setNotifyFailure] = useState(true);
  const [inputValues, setInputValues] = useState<Record<string, string>>(() => Object.fromEntries(
    Object.entries(context?.initialInputs ?? {}).map(([key, value]) => [key, typeof value === 'string' ? value : JSON.stringify(value) ?? '']),
  ));
  const [inputResetNotice, setInputResetNotice] = useState(false);
  const submitting = useRef(false);
  const cronExpr = scheduleCron({
    frequency, time, hourlyMinute, weekday, monthday, customCron,
  });
  const intervalSeconds = intervalValue * (
    intervalUnit === 'minutes' ? 60 : intervalUnit === 'hours' ? 3_600 : 86_400
  );

  const selectedWorkflow = workflows.find((wf) => wf.wf_id === effectiveWorkflowId);
  const workflowName = selectedWorkflow?.workflow_name || context?.name || effectiveWorkflowId;
  const name = customName ?? (context ? `${workflowName} ${t('inspector.tab.schedule', 'Schedule')}` :
    selectedWorkflow
      ? `${selectedWorkflow.workflow_name || selectedWorkflow.wf_id} schedule`
      : 'Scheduled run'
  );
  const versionSelection = useTaskWorkflowVersion(effectiveWorkflowId, context?.version);
  const snapshotQuery = versionSelection.snapshot;
  const workflowSnapshot = snapshotQuery.data?.workflow as Record<string, unknown> | null | undefined;
  const fields = useMemo(() => getStartNodeFields(context?.workflow ?? workflowSnapshot), [context?.workflow, workflowSnapshot]);

  const createMutation = useMutation({
    mutationFn: async () => {
      const selectedRunAt = scheduleMode === "once" ? zonedWallClockToIso(runAt, timezone) : null;
      if (scheduleMode === "once" && (!selectedRunAt || Date.parse(selectedRunAt) <= Date.now())) throw new Error(t("tasks.scheduled.futureRequired", "Choose a future date and time."));
      const input_preset: Record<string, unknown> = {};
      for (const field of fields) {
        try {
          input_preset[field.name] = parsePresetValue(inputValues[field.name] ?? '', field.type);
        } catch {
          throw new Error(t('tasks.scheduled.invalidInput', 'Check input {{field}} ({{type}}).', { field: field.name, type: field.type }));
        }
      }
      const version = context ? await context.prepare() : versionSelection.frozenTarget?.version;
      if (!version) throw new Error(t("tasks.scheduled.versionRequired", "Choose a saved workflow version."));
      return createScheduledRun({
        name,
        workflow_id: effectiveWorkflowId,
        version,
        enabled,
        schedule_type: scheduleMode === 'calendar' ? 'cron' : scheduleMode === 'once' ? 'once' : 'interval',
        run_at: selectedRunAt,
        interval_seconds: scheduleMode === 'interval' ? intervalSeconds : null,
        cron_expr: scheduleMode === 'calendar' ? cronExpr : null,
        timezone,
        start_at: scheduleMode === "once" ? null : zonedWallClockToIso(startAt, timezone),
        end_at: scheduleMode === "once" ? null : zonedWallClockToIso(endAt, timezone),
        input_preset,
        mount_enabled: mountEnabled,
        notification_policy: {
          enabled: notifySuccess || notifyFailure,
          on: [
            ...(notifySuccess ? ['succeeded'] : []),
            ...(notifyFailure ? ['failed'] : []),
          ],
          channels: ['in_app'],
          include_detail_link: true,
        },
      });
    },
    onSuccess: (data) => {
      toast.success(t('tasks.scheduled.created', 'Scheduled run created'));
      onCreated(data.task.id, data);
    },
    onError: (e) => {
      toast.error(
        `${t('tasks.scheduled.createFailed', 'Create scheduled run failed')}: ${
          e instanceof ScheduleTimeError ? t(`scheduleTime.${e.code}`, e.message) : e instanceof Error ? e.message : String(e)
        }`,
      );
    },
    onSettled: () => { submitting.current = false; },
  });

  const signature = JSON.stringify([effectiveWorkflowId, context?.version ?? versionSelection.selector, fields]);
  const previousSignature = useRef(signature);
  useEffect(() => {
    if (previousSignature.current !== signature) {
      previousSignature.current = signature;
      if (!submitting.current) {
        setInputValues({});
        setInputResetNotice(true);
      }
    }
  }, [signature]);

  return (
    <section className="flex min-h-0 flex-1 flex-col overflow-hidden border border-edge-structural bg-surface-work" data-testid="task-scheduled-create-panel">
      <div className="shrink-0 border-b bg-surface-sunken/70 px-4 py-3">
        <div>
          <div className="text-base font-semibold">
            {t('tasks.new.scheduledTitle', 'Scheduled run setup')}
          </div>
          <p className="mt-1 text-sm text-muted-foreground">
            {t('tasks.new.scheduledDesc', 'Run one workflow on a simple schedule with fixed preset input.')}
          </p>
        </div>
      </div>

      <div className={`page-scroll-region grid flex-1 content-start gap-4 p-4 ${context ? "" : "lg:grid-cols-[320px_minmax(0,1fr)]"}`} data-role="task-create-scroll-region">
        <aside className="space-y-3">
          <div className="rounded-lg border bg-background p-3">
            <label className="text-sm font-medium" htmlFor="task-scheduled-name">
              {t('tasks.scheduled.name', 'Name')}
            </label>
            <Input
              id="task-scheduled-name"
              className="mt-2"
              value={name}
              onChange={(event) => setCustomName(event.target.value)}
            />
          </div>

          <div className="rounded-lg border bg-background p-3">
            <label className="text-sm font-medium" htmlFor="task-scheduled-workflow">
              {t('tasks.new.workflow', 'Workflow')}
            </label>
            {context ? <div className="mt-2 space-y-2 text-sm"><p>{workflowName} · {context.version}</p>
              <p className="text-xs text-muted-foreground">{t('tasks.version.scheduleHint', 'Each execution uses this fixed saved version until you explicitly change it.')}</p></div> : <>
              <SearchSelect
                value={effectiveWorkflowId}
                options={workflowSelectOptions}
                onValueChange={setSelectedWorkflowId}
                placeholder={t('tasks.new.selectWorkflow', 'Select a workflow to configure the batch task.')}
                searchPlaceholder={t('tasks.new.searchWorkflow', 'Search workflow name, ID, or description')}
                emptyText={t('tasks.new.noWorkflowMatches', 'No workflows match your search.')}
                disabled={workflowsQuery.isLoading || workflows.length === 0}
                className="mt-2"
                triggerClassName="w-full"
              />
            <TaskWorkflowVersion selection={versionSelection} />
            </>}
            {selectedWorkflow && !context && (
              <div className="mt-3 rounded-md bg-surface-sunken p-2 text-xs text-muted-foreground">
                <div className="truncate font-medium text-foreground">
                  {selectedWorkflow.workflow_name}
                </div>
                <div className="truncate font-mono">{selectedWorkflow.wf_id}</div>
              </div>
            )}
          </div>

          <div className="rounded-lg border bg-background p-3">
            <div className="text-sm font-medium">{t('tasks.scheduled.timing', 'Timing')}</div>
            <Select
              value={scheduleMode}
              onValueChange={(value) => setScheduleMode(value as 'once' | 'calendar' | 'interval')}
            >
              <SelectTrigger className="mt-3 w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="once">{t("tasks.scheduled.fixedTime", "Fixed time — once")}</SelectItem>
                <SelectItem value="calendar">{t('tasks.scheduled.calendarSchedule', 'At a specific time')}</SelectItem>
                <SelectItem value="interval">{t('tasks.scheduled.intervalSchedule', 'At a fixed interval')}</SelectItem>
              </SelectContent>
            </Select>

            {scheduleMode === 'once' ? (
              <div className="mt-3 grid gap-2 text-sm">
                <span>{t('tasks.scheduled.runAt', 'Run once at')}</span>
                <DateTimePicker value={runAt} onChange={setRunAt} disabled={createMutation.isPending} />
                <span className="text-xs text-muted-foreground">{runAt ? t('tasks.scheduled.oncePreview', 'Runs once at {{time}} · {{timezone}}.', { time: runAt.replace('T', ' '), timezone }) : t('tasks.scheduled.selectTime', 'Select year, month, day, hour, minute and second.')}</span>
              </div>
            ) : scheduleMode === 'calendar' ? (
              <div className="mt-3 grid gap-3">
                <Select value={frequency} onValueChange={(value) => setFrequency(value as ScheduleFrequency)}>
                  <SelectTrigger className="w-full" aria-label={t('tasks.scheduled.frequency', 'Frequency')}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="hourly">{t('tasks.scheduled.hourly', 'Hourly')}</SelectItem>
                    <SelectItem value="daily">{t('tasks.scheduled.daily', 'Daily')}</SelectItem>
                    <SelectItem value="weekly">{t('tasks.scheduled.weekly', 'Weekly')}</SelectItem>
                    <SelectItem value="monthly">{t('tasks.scheduled.monthly', 'Monthly')}</SelectItem>
                    <SelectItem value="custom">{t('tasks.scheduled.customCron', 'Custom cron')}</SelectItem>
                  </SelectContent>
                </Select>
                {frequency === 'hourly' ? (
                  <label className="grid gap-1.5 text-xs text-muted-foreground">
                    {t('tasks.scheduled.minuteOfHour', 'Minute of the hour')}
                    <Input type="number" min={0} max={59} value={hourlyMinute} onChange={(event) => setHourlyMinute(Math.min(59, Math.max(0, Number(event.target.value))))} />
                  </label>
                ) : frequency === 'custom' ? (
                  <label className="grid gap-1.5 text-xs text-muted-foreground">
                    {t('tasks.scheduled.cronExpression', 'Cron expression')}
                    <Input className="font-mono" value={customCron} onChange={(event) => setCustomCron(event.target.value)} placeholder="0 9 * * *" />
                    <span>
                      {t('tasks.scheduled.schedulePreview', 'Schedule preview: {{schedule}}', {
                        schedule: describeCronExpression(customCron, scheduleLocale(i18n.resolvedLanguage)).text,
                      })}
                    </span>
                  </label>
                ) : (
                  <>
                    {frequency === 'weekly' ? (
                      <Select value={String(weekday)} onValueChange={(value) => setWeekday(Number(value))}>
                        <SelectTrigger aria-label={t('tasks.scheduled.dayOfWeek', 'Day of week')}><SelectValue /></SelectTrigger>
                        <SelectContent>
                          {[
                            { value: 1, label: 'Monday' }, { value: 2, label: 'Tuesday' },
                            { value: 3, label: 'Wednesday' }, { value: 4, label: 'Thursday' },
                            { value: 5, label: 'Friday' }, { value: 6, label: 'Saturday' },
                            { value: 0, label: 'Sunday' },
                          ].map(({ value, label }) => (
                            <SelectItem key={value} value={String(value)}>
                              {t(`tasks.scheduled.weekday.${value}`, label)}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    ) : null}
                    {frequency === 'monthly' ? (
                      <label className="grid gap-1.5 text-xs text-muted-foreground">
                        {t('tasks.scheduled.dayOfMonth', 'Day of month')}
                        <Input type="number" min={1} max={31} value={monthday} onChange={(event) => setMonthday(Math.min(31, Math.max(1, Number(event.target.value))))} />
                      </label>
                    ) : null}
                    <label className="grid gap-1.5 text-xs text-muted-foreground">
                      {t('tasks.scheduled.runTime', 'Run time')}
                      <Input type="time" step={60} value={time} onChange={(event) => setTime(event.target.value)} />
                    </label>
                  </>
                )}
              </div>
            ) : (
              <div className="mt-3 grid grid-cols-[minmax(0,1fr)_minmax(8rem,0.8fr)] gap-2">
                <Input type="number" min={1} value={intervalValue} onChange={(event) => setIntervalValue(Math.max(1, Number(event.target.value)))} aria-label={t('tasks.scheduled.intervalValue', 'Interval value')} />
                <Select value={intervalUnit} onValueChange={(value) => setIntervalUnit(value as typeof intervalUnit)}>
                  <SelectTrigger aria-label={t('tasks.scheduled.intervalUnit', 'Interval unit')}><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="minutes">{t('tasks.scheduled.minutes', 'Minutes')}</SelectItem>
                    <SelectItem value="hours">{t('tasks.scheduled.hours', 'Hours')}</SelectItem>
                    <SelectItem value="days">{t('tasks.scheduled.days', 'Days')}</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            )}

            <label className="mt-3 grid gap-1.5 text-xs text-muted-foreground">
              {t('tasks.scheduled.timezone', 'Timezone')}
              <Select value={timezone} onValueChange={setScheduleTimezone}>
                <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                <SelectContent>
                  {TIMEZONE_GROUPS.map((group) => (
                    <SelectGroup key={group.region}>
                      <SelectLabel>{group.region}</SelectLabel>
                      {group.zones.map((zone) => <SelectItem key={zone.value} value={zone.value}>{zone.label}</SelectItem>)}
                    </SelectGroup>
                  ))}
                </SelectContent>
              </Select>
            </label>

            {scheduleMode !== "once" && <details className="group mt-3 rounded-md border border-edge-subtle bg-surface-sunken/45">
              <summary className="flex cursor-pointer list-none items-center justify-between px-3 py-2 text-xs font-medium text-content-secondary">
                {t('tasks.scheduled.timeframe', 'Start and end')}
                <ChevronDown className="h-3.5 w-3.5 transition-transform group-open:rotate-180" />
              </summary>
              <div className="grid gap-3 border-t border-edge-subtle px-3 py-3">
                <label className="grid gap-1.5 text-xs text-muted-foreground">
                  {t('tasks.scheduled.startAt', 'Start at (optional)')}
                  <Input type="datetime-local" value={startAt} onChange={(event) => setStartAt(event.target.value)} />
                </label>
                <label className="grid gap-1.5 text-xs text-muted-foreground">
                  {t('tasks.scheduled.endAt', 'End at (optional)')}
                  <Input type="datetime-local" value={endAt} min={startAt || undefined} onChange={(event) => setEndAt(event.target.value)} />
                </label>
              </div>
            </details>}

            <div className="mt-3 rounded-md border border-edge-subtle bg-surface-sunken/45 px-3 py-2 text-xs leading-5 text-muted-foreground">
              {t('tasks.scheduled.overlapHint', 'Each occurrence runs independently, even while a previous run is active. Runs may queue when capacity is unavailable.')}
            </div>
            <label className="mt-3 flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={enabled}
                onChange={(event) => setEnabled(event.target.checked)}
              />
              {t('tasks.scheduled.enabled', 'Enable after creation')}
            </label>
            <label className="mt-2 flex items-start gap-2 text-sm">
              <input
                type="checkbox"
                checked={mountEnabled}
                onChange={(event) => setMountEnabled(event.target.checked)}
                className="mt-0.5"
              />
              <span>
                <span className="block">{t('tasks.scheduled.mountUserStorage', 'Mount user storage')}</span>
                <span className="block text-xs text-muted-foreground">
                  {t('tasks.scheduled.mountUserStorageHint', 'Allow each run to access files under /mount.')}
                </span>
              </span>
            </label>
          </div>
        </aside>

        <div className="space-y-4">
          <div className="rounded-lg border bg-background p-3">
            <div className="text-sm font-medium">
              {t('tasks.scheduled.inputPreset', 'Workflow input preset')}
            </div>
            {inputResetNotice && <p role="status" className="mt-2 text-xs text-muted-foreground">{t('tasks.scheduled.inputsReset', 'Workflow version or inputs changed. Review and fill in the inputs again.')}</p>}
            <p className="mt-1 text-xs text-muted-foreground">
              {t('tasks.scheduled.inputHint', 'Dynamic values should be computed inside the workflow. These values are reused for every scheduled run.')}
            </p>
            {snapshotQuery.isLoading && effectiveWorkflowId ? (
              <div className="mt-4 text-sm text-muted-foreground">
                {t('tasks.new.loadingWorkflow', 'Loading workflow...')}
              </div>
            ) : fields.length === 0 ? (
              <div className="mt-4 rounded-md border border-dashed p-4 text-sm text-muted-foreground">
                {t('tasks.scheduled.noFields', 'No StartNode input fields found.')}
              </div>
            ) : (
              <div className="mt-3 grid gap-3">
                {fields.map((field) => (
                  <label key={field.name} className="grid gap-1 text-sm">
                    <span className="flex items-center justify-between gap-2">
                      <span className="font-medium">{field.name}</span>
                      <span className="font-mono text-xs text-muted-foreground">{field.type}</span>
                    </span>
                    <textarea
                      value={inputValues[field.name] ?? ''}
                      onChange={(event) =>
                        setInputValues((prev) => ({ ...prev, [field.name]: event.target.value }))
                      }
                      className="min-h-20 rounded-md border bg-background px-3 py-2 font-mono text-xs"
                    />
                  </label>
                ))}
              </div>
            )}
          </div>

          <div className="rounded-lg border bg-background p-3">
            <div className="text-sm font-medium">
              {t('tasks.scheduled.notifications', 'Notifications')}
            </div>
            <div className="mt-3 flex flex-wrap gap-4 text-sm">
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={notifyFailure}
                  onChange={(event) => setNotifyFailure(event.target.checked)}
                />
                {t('tasks.scheduled.notifyFailure', 'Failure')}
              </label>
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={notifySuccess}
                  onChange={(event) => setNotifySuccess(event.target.checked)}
                />
                {t('tasks.scheduled.notifySuccess', 'Success')}
              </label>
            </div>
            <p className="mt-2 text-xs text-muted-foreground">
              {t('tasks.scheduled.notificationHint', 'Notifications will include a link back to the execution detail.')}
            </p>
          </div>

        </div>
      </div>
      <div className="flex shrink-0 justify-end gap-2 border-t bg-surface-raised px-4 py-3">
        <Button variant="outline" onClick={onCancel}>
          {t('common.cancel', 'Cancel')}
        </Button>
        <Button
          onClick={() => { if (!submitting.current) { submitting.current = true; createMutation.mutate(); } }}
          disabled={!effectiveWorkflowId || (context ? !context.version : !versionSelection.selector || versionSelection.loading || versionSelection.error || snapshotQuery.isLoading || snapshotQuery.isError) || createMutation.isPending}
        >
          {context?.dirty ? t('tasks.scheduled.saveCreate', 'Save and create scheduled task') : enabled ? t('tasks.scheduled.createEnable', 'Create and enable') : t('tasks.scheduled.createTask', 'Create task')}
        </Button>
      </div>
    </section>
  );
}
