export class ScheduleTimeError extends Error {
  readonly code: string;
  constructor(code: string, message: string) { super(message); this.code = code; }
}

/** Resolve a local wall clock in an explicit IANA zone, retaining seconds.
 * Missing and repeated DST times require correction instead of silent shifting.
 */
export function zonedWallClockToIso(value: string, timezone: string): string | null {
  if (!value) return null;
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(value);
  if (!match) throw new ScheduleTimeError('incomplete', 'Enter a complete date and time.');
  const [, y, mo, d, h, mi, sec = '00'] = match;
  const target = Date.UTC(+y, +mo - 1, +d, +h, +mi, +sec);
  const expected = `${y}-${mo}-${d}T${h}:${mi}:${sec}`;
  if (new Date(target).toISOString().slice(0, 19) !== expected) throw new ScheduleTimeError('invalid', 'Invalid date or time.');
  const formatter = new Intl.DateTimeFormat('en-CA', {
    timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
  });
  const wall = (instant: number) => {
    const parts = Object.fromEntries(formatter.formatToParts(new Date(instant)).map(p => [p.type, p.value]));
    return Date.UTC(+parts.year, +parts.month - 1, +parts.day, +parts.hour, +parts.minute, +parts.second);
  };
  const offsets = new Set<number>();
  for (let hours = -48; hours <= 48; hours += 6) {
    const sample = target + hours * 3_600_000;
    offsets.add(wall(sample) - sample);
  }
  const matches = [...offsets].map(offset => target - offset).filter(instant => wall(instant) === target);
  if (matches.length === 0) throw new ScheduleTimeError('nonexistent', 'This local time does not exist in the selected timezone.');
  if (matches.length > 1) throw new ScheduleTimeError('ambiguous', 'This local time occurs twice. Choose UTC or a different time to make the instant explicit.');
  return new Date(matches[0]).toISOString();
}
