export type TaskNotifications = { enabled: boolean; on: string[]; email: string };
export const emptyNotifications = (): TaskNotifications => ({ enabled: false, on: [], email: '' });
export const notificationsValid = (value: TaskNotifications) => !value.enabled || value.email.trim().length <= 254 && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value.email.trim());

