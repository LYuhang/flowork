export type ScheduleLocale = 'en' | 'zh';

export interface CronDescription {
  text: string;
  valid: boolean;
}

export function scheduleLocale(language: string | undefined): ScheduleLocale {
  return language?.startsWith('zh') ? 'zh' : 'en';
}

const EN_WEEKDAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
const ZH_WEEKDAYS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];

function normalizeWeekday(value: number): number | null {
  if (value === 7) return 0;
  return value >= 0 && value <= 6 ? value : null;
}

function parseNumber(value: string, min: number, max: number): number | null {
  if (!/^\d+$/.test(value)) return null;
  const number = Number(value);
  return number >= min && number <= max ? number : null;
}

function formatTime(hour: number, minute: number, locale: ScheduleLocale): string {
  const hhmm = `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`;
  if (locale === 'zh') return hhmm;
  const suffix = hour < 12 ? 'AM' : 'PM';
  const displayHour = hour % 12 || 12;
  return `${String(displayHour).padStart(2, '0')}:${String(minute).padStart(2, '0')} ${suffix}`;
}

function parseWeekdayField(field: string): number[] | null {
  if (field === '*') return [];
  const values: number[] = [];
  for (const part of field.split(',')) {
    if (part.includes('-')) {
      const [startText, endText, ...rest] = part.split('-');
      if (rest.length > 0) return null;
      const start = parseNumber(startText, 0, 7);
      const end = parseNumber(endText, 0, 7);
      if (start === null || end === null || start > end) return null;
      for (let value = start; value <= end; value += 1) {
        const normalized = normalizeWeekday(value);
        if (normalized !== null) values.push(normalized);
      }
    } else {
      const value = parseNumber(part, 0, 7);
      const normalized = value === null ? null : normalizeWeekday(value);
      if (normalized === null) return null;
      values.push(normalized);
    }
  }
  return [...new Set(values)];
}

function formatWeekdays(days: number[], locale: ScheduleLocale): string | null {
  if (days.length === 0) return locale === 'zh' ? '每天' : 'every day';
  if (days.length === 5 && days.every((day, index) => day === index + 1)) {
    return locale === 'zh' ? '每周一至周五' : 'every Monday through Friday';
  }
  const names = locale === 'zh' ? ZH_WEEKDAYS : EN_WEEKDAYS;
  if (days.length === 1) return locale === 'zh' ? `每${names[days[0]]}` : `every ${names[days[0]]}`;
  const labels = days.map((day) => names[day]);
  if (locale === 'zh') return `每${labels.join('、')}`;
  return `every ${labels.slice(0, -1).join(', ')} and ${labels.at(-1)}`;
}

/**
 * Converts the common five-field Cron forms used by Flowork into copy that a
 * non-technical user can scan. The raw expression remains available beside
 * this description, so unsupported advanced syntax is never misrepresented.
 */
export function describeCronExpression(expression: string | null | undefined, locale: ScheduleLocale): CronDescription {
  const fallback = locale === 'zh' ? '自定义计划' : 'Custom schedule';
  const fields = expression?.trim().split(/\s+/) ?? [];
  if (fields.length !== 5) return { text: fallback, valid: false };

  const [minuteField, hourField, dayOfMonthField, monthField, weekdayField] = fields;
  const minute = parseNumber(minuteField, 0, 59);
  const hour = parseNumber(hourField, 0, 23);
  const dayOfMonth = parseNumber(dayOfMonthField, 1, 31);
  const month = parseNumber(monthField, 1, 12);
  const weekdays = parseWeekdayField(weekdayField);

  const minuteStep = minuteField.match(/^\*\/(\d+)$/);
  if (minuteStep && hourField === '*' && dayOfMonthField === '*' && monthField === '*' && weekdayField === '*') {
    const count = parseNumber(minuteStep[1], 1, 59);
    if (count !== null) {
      return {
        text: locale === 'zh' ? `每 ${count} 分钟` : `Every ${count} minutes`,
        valid: true,
      };
    }
  }

  const hourStep = hourField.match(/^\*\/(\d+)$/);
  if (minute === 0 && hourStep && dayOfMonthField === '*' && monthField === '*' && weekdayField === '*') {
    const count = parseNumber(hourStep[1], 1, 23);
    if (count !== null) {
      return {
        text: locale === 'zh' ? `每 ${count} 小时` : `Every ${count} hours`,
        valid: true,
      };
    }
  }

  if (minute !== null && hourField === '*' && dayOfMonthField === '*' && monthField === '*' && weekdayField === '*') {
    return {
      text: locale === 'zh' ? `每小时的第 ${minute} 分钟` : `At minute ${minute} of every hour`,
      valid: true,
    };
  }

  if (minute === null || hour === null || weekdays === null) {
    return { text: fallback, valid: false };
  }

  const time = formatTime(hour, minute, locale);
  if (dayOfMonthField === '*' && monthField === '*') {
    const recurrence = formatWeekdays(weekdays, locale);
    if (recurrence !== null) {
      return {
        text: locale === 'zh' ? `${recurrence} ${time}` : `${time}, ${recurrence}`,
        valid: true,
      };
    }
  }

  if (dayOfMonth !== null && monthField === '*' && weekdayField === '*') {
    return {
      text: locale === 'zh' ? `每月 ${dayOfMonth} 日 ${time}` : `${time} on day ${dayOfMonth} of every month`,
      valid: true,
    };
  }

  if (dayOfMonth !== null && month !== null && weekdayField === '*') {
    return {
      text: locale === 'zh' ? `每年 ${month} 月 ${dayOfMonth} 日 ${time}` : `${time} on ${month}/${dayOfMonth} every year`,
      valid: true,
    };
  }

  return { text: fallback, valid: false };
}
