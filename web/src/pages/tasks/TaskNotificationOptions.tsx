import { useTranslation } from 'react-i18next';
import { Input } from '@/components/ui/input';

export type TaskNotifications = { enabled: boolean; on: string[]; email: string };
export const emptyNotifications = (): TaskNotifications => ({ enabled: false, on: [], email: '' });
export const notificationsValid = (value: TaskNotifications) => !value.enabled || value.email.trim().length <= 254 && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value.email.trim());

export function TaskNotificationOptions({ value, onChange, disabled = false }: {
  value: TaskNotifications; onChange: (value: TaskNotifications) => void; disabled?: boolean;
}) {
  const { t } = useTranslation();
  return <fieldset disabled={disabled} className="rounded-lg border bg-background p-3 space-y-3">
    <legend className="px-1 text-sm font-medium">{t('tasks.scheduled.notifications', 'Notifications')}</legend>
    <div className="flex flex-wrap gap-4 text-sm">
      {(['succeeded', 'failed'] as const).map(event => <label key={event} className="flex items-center gap-2">
        <input type="checkbox" checked={value.on.includes(event)} onChange={e => {
          const on = e.target.checked ? [...value.on, event] : value.on.filter(item => item !== event);
          onChange({ ...value, on, enabled: on.length > 0 });
        }} />
        {event === 'succeeded' ? t('tasks.notifications.success', 'Notify on success') : t('tasks.notifications.failure', 'Notify on failure')}
      </label>)}
    </div>
    {value.enabled && <label className="block space-y-2 text-sm">
      <span>{t('tasks.notifications.email', 'Notification recipient email')}</span>
      <Input type="email" maxLength={254} required value={value.email} placeholder="user@example.com"
        onChange={e => onChange({ ...value, email: e.target.value })} />
    </label>}
  </fieldset>;
}
