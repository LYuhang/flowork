import { useId, useState } from 'react';
import { CalendarDays, ChevronLeft, ChevronRight } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from './button';
import { Input } from './input';
import { Popover, PopoverContent, PopoverTrigger } from './popover';

const pad = (value: number) => String(value).padStart(2, '0');

/** A wall-clock selector: zone resolution belongs to the shared schedule form. */
export function DateTimePicker({ value, onChange, disabled }: {
  value: string; onChange: (value: string) => void; disabled?: boolean;
}) {
  const { t, i18n } = useTranslation();
  const id = useId();
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState('');
  const [month, setMonth] = useState(() => new Date().getMonth());
  const [year, setYear] = useState(() => new Date().getFullYear());
  const [time, setTime] = useState([0, 0, 0]);
  const locale = i18n.resolvedLanguage || 'en';
  const setVisibleMonth = (offset: number) => {
    const date = new Date(year, month + offset, 1);
    setYear(date.getFullYear()); setMonth(date.getMonth());
  };
  const changeOpen = (next: boolean) => {
    if (next) {
      const date = value.split('T')[0];
      const parts = date ? date.split('-').map(Number) : [];
      const now = new Date();
      setSelected(date || '');
      setYear(parts[0] || now.getFullYear()); setMonth(parts[1] ? parts[1] - 1 : now.getMonth());
      setTime((value.split('T')[1] || '00:00:00').split(':').map(Number).concat([0, 0, 0]).slice(0, 3));
    }
    setOpen(next);
  };
  const firstDay = new Date(year, month, 1).getDay();
  const days = new Date(year, month + 1, 0).getDate();
  return <div className="flex gap-1.5">
    <Input aria-label={t('tasks.scheduled.runAt', 'Run once at')} type="text" placeholder={t('dateTime.placeholder')}
      value={value.replace('T', ' ')} disabled={disabled} onChange={event => onChange(event.target.value.replace(' ', 'T'))} />
    <Popover open={open} onOpenChange={changeOpen}>
      <PopoverTrigger asChild>
        <Button type="button" variant="outline" size="icon" disabled={disabled}
          aria-label={t('dateTime.open', 'Choose date and time')}><CalendarDays className="h-4 w-4" /></Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-[320px] space-y-3" aria-label={t('dateTime.open', 'Choose date and time')}>
        <div className="flex items-center gap-2">
          <Button type="button" variant="ghost" size="icon" onClick={() => setVisibleMonth(-1)} aria-label={t('dateTime.previous', 'Previous month')}><ChevronLeft className="h-4 w-4" /></Button>
          <Input key={year} type="number" min={1900} max={9999} defaultValue={year} aria-label={t('dateTime.year', 'Year')}
            onBlur={event => { const n = Number(event.target.value); if (Number.isInteger(n) && n >= 1900 && n <= 9999) setYear(n); else event.target.value = String(year); }} className="w-24" />
          <select value={month} onChange={event => setMonth(Number(event.target.value))}
            aria-label={t('dateTime.month', 'Month')} className="h-9 min-w-0 flex-1 rounded-md border bg-background px-1 text-sm">
            {Array.from({ length: 12 }, (_, m) => <option key={m} value={m}>{new Intl.DateTimeFormat(locale, { month: 'short' }).format(new Date(2026, m, 1))}</option>)}
          </select>
          <Button type="button" variant="ghost" size="icon" onClick={() => setVisibleMonth(1)} aria-label={t('dateTime.next', 'Next month')}><ChevronRight className="h-4 w-4" /></Button>
        </div>
        <div className="grid grid-cols-7 gap-1">
          {Array.from({ length: 7 }, (_, day) => <span key={`weekday-${day}`} className="text-center text-xs text-muted-foreground">{new Intl.DateTimeFormat(locale, { weekday: 'narrow' }).format(new Date(2026, 0, 4 + day))}</span>)}
          {Array.from({ length: firstDay }, (_, day) => <span key={`space-${day}`} />)}
          {Array.from({ length: days }, (_, i) => {
            const day = i + 1;
            const date = `${year}-${pad(month + 1)}-${pad(day)}`;
            return <Button key={date} type="button" variant={selected === date ? 'default' : 'ghost'}
              className="h-8 p-0" aria-label={date} aria-pressed={selected === date}
              onClick={() => setSelected(date)}>{day}</Button>;
          })}
        </div>
        <div className="grid grid-cols-3 gap-2 border-t pt-3">
          {['hour', 'minute', 'second'].map((part, index) => <label key={part} htmlFor={`${id}-${part}`} className="space-y-1 text-xs text-muted-foreground">
            <span>{t(`dateTime.${part}`, part)}</span>
            <Input id={`${id}-${part}`} type="number" min={0} max={index === 0 ? 23 : 59} value={time[index]}
              onChange={event => { const n = Number(event.target.value); if (Number.isInteger(n) && n >= 0 && n <= (index === 0 ? 23 : 59)) setTime(old => old.map((v, i) => i === index ? n : v)); }} />
          </label>)}
        </div>
        <Button type="button" className="w-full" disabled={!selected} onClick={() => {
          onChange(`${selected}T${time.map(pad).join(':')}`); setOpen(false);
        }}>{t('common.confirm', 'Confirm')}</Button>
      </PopoverContent>
    </Popover>
  </div>;
}
