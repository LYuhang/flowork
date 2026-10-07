import type { MetricsResponse } from '@/lib/api/deployments';

export type Metric = 'calls' | 'errors' | 'qps' | 'error_rate' | 'latency_p95';
export const left = 52, right = 346, top = 18, bottom = 162;

/** Missing latency and unrecorded buckets remain gaps, never fabricated zeros. */
export function metricGeometry(data: MetricsResponse, metric: Metric) {
  const bucketMs = data.bucket === 'minute' ? 60000 : data.bucket === 'day' ? 86400000 : 3600000;
  const rows = [...data.series].sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts));
  const start = Math.floor(Date.parse(data.from) / bucketMs) * bucketMs;
  const end = Math.max(start + bucketMs, Date.parse(data.to));
  const valid = rows.filter(row => row[metric] != null && Number.isFinite(row[metric])
    && Date.parse(row.ts) >= start && Date.parse(row.ts) <= end);
  const max = Math.max(0, ...valid.map(row => row[metric]!));
  const rawStep = (max > 0 ? max : 1) / 3;
  const magnitude = 10 ** Math.floor(Math.log10(rawStep));
  const step = Math.max(metric === 'calls' || metric === 'errors' ? 1 : 0,
    ([1, 2, 5, 10].find(value => value * magnitude >= rawStep) ?? 10) * magnitude);
  const ceiling = Math.max(step, Math.ceil(max / step) * step);
  const ticks = Array.from({ length: Math.round(ceiling / step) + 1 }, (_, index) => index * step);
  const points = valid.map(row => ({ ts: row.ts, value: row[metric]!,
    x: left + (Date.parse(row.ts) - start) / (end - start) * (right - left),
    y: bottom - row[metric]! / ceiling * (bottom - top),
  }));
  const path = points.map((point, index) => {
    const previous = points[index - 1];
    const connected = previous && Date.parse(point.ts) - Date.parse(previous.ts) <= bucketMs * 1.01;
    return `${connected ? 'L' : 'M'} ${point.x} ${point.y}`;
  }).join(' ');
  return { start, end, max, ceiling, ticks, points, path };
}

