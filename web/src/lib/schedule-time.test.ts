import { describe, expect, it } from 'vitest';
import { zonedWallClockToIso } from './schedule-time';

describe('schedule wall clocks', () => {
  it('retains seconds and uses the selected zone', () => {
    expect(zonedWallClockToIso('2026-10-05T09:30:17', 'Asia/Shanghai')).toBe('2026-10-05T01:30:17.000Z');
  });
  it('accepts minute precision with zero seconds', () => {
    expect(zonedWallClockToIso('2026-10-05T09:30', 'UTC')).toBe('2026-10-05T09:30:00.000Z');
  });
  it('rejects invalid dates and missing DST times', () => {
    expect(() => zonedWallClockToIso('2026-02-30T09:00:00', 'UTC')).toThrow();
    expect(() => zonedWallClockToIso('2026-03-08T02:30:00', 'America/New_York')).toThrow(/does not exist/);
  });
  it('rejects ambiguous DST times rather than picking an occurrence silently', () => {
    expect(() => zonedWallClockToIso('2026-11-01T01:30:00', 'America/New_York')).toThrow(/twice/);
  });
});
