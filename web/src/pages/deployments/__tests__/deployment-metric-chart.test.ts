import { describe, expect, it } from 'vitest';
import { metricGeometry } from '../deployment-metric-geometry';
import type { MetricsResponse } from '@/lib/api/deployments';

const dataset: MetricsResponse = {
  from: '2026-10-07T00:00:00Z', to: '2026-10-07T06:00:00Z', bucket: 'hour',
  series: [
    { ts: '2026-10-07T01:00:00Z', calls: 1, errors: 0, qps: 1/60, error_rate: null, latency_p50: null, latency_p95: null },
    { ts: '2026-10-07T02:00:00Z', calls: 2, errors: 0, qps: 2/60, error_rate: 0, latency_p50: 5, latency_p95: 10 },
    { ts: '2026-10-07T05:00:00Z', calls: 3, errors: 1, qps: 3/60, error_rate: 33.3, latency_p50: 8, latency_p95: 20 },
  ],
};
describe('deployment metric geometry', () => {
  it('preserves fractional rates and excludes absent error rates', () => {
    expect(metricGeometry(dataset, 'qps').points[0]!.value).toBeCloseTo(1/60);
    expect(metricGeometry(dataset, 'error_rate').points.map(p => p.value)).toEqual([0, 33.3]);
  });
  it('spaces buckets by time and does not bridge unrecorded hours', () => {
    const model = metricGeometry(dataset, 'calls');
    expect(model.points[2]!.x - model.points[1]!.x).toBeCloseTo(3 * (model.points[1]!.x - model.points[0]!.x));
    expect(model.path.match(/M/g)).toHaveLength(2);
    expect(model.path.match(/L/g)).toHaveLength(1);
  });
  it('does not turn missing latency into a zero measurement', () => {
    const model = metricGeometry(dataset, 'latency_p95');
    expect(model.points.map(point => point.value)).toEqual([10, 20]);
  });
  it('keeps zero-only count axes finite and integer-valued', () => {
    const model = metricGeometry({ ...dataset, series: dataset.series.map(row => ({ ...row, errors: 0 })) }, 'errors');
    expect(model.ticks).toEqual([0, 1]);
    expect(model.points.every(point => point.y === 162)).toBe(true);
    expect(model.path).not.toMatch(/NaN|Infinity/);
  });
  it('sorts timestamps without mutating input and excludes out-of-range samples', () => {
    const input = { ...dataset, series: [...dataset.series].reverse() };
    expect(metricGeometry(input, 'calls').points[0]!.ts).toBe(dataset.series[0]!.ts);
    expect(input.series[0]!.ts).toBe(dataset.series[2]!.ts);
    expect(metricGeometry({ ...dataset, to: '2026-10-07T03:00:00Z' }, 'calls').points).toHaveLength(2);
  });
});
