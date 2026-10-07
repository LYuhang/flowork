import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { MetricsResponse } from '@/lib/api/deployments';
import { useFormatDateTime, useTimezone } from '@/lib/timezone';
import { formatNumber } from '@/lib/format/number';

type Metric = 'calls' | 'errors' | 'qps' | 'error_rate' | 'latency_p95';
const left = 52, right = 346, top = 18, bottom = 162;

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

export function DeploymentMetricChart({ data, metric, label, unit, color, description }: {
  data: MetricsResponse; metric: Metric; label: string; unit: string; color: string; description?: string;
}) {
  const { t, i18n } = useTranslation();
  const timezone = useTimezone();
  const formatTime = useFormatDateTime();
  const [selected, setSelected] = useState<string | null>(null);
  const model = metricGeometry(data, metric);
  const active = model.points.find(point => point.ts === selected) ?? model.points.at(-1);
  const number = (value: number) => formatNumber(value, { maximumFractionDigits: 3 });
  const timeAxis = new Intl.DateTimeFormat(i18n.resolvedLanguage ?? 'en', {
    timeZone: timezone,
    ...(model.end - model.start > 48 * 3600000 ? { month: '2-digit', day: '2-digit' } : { hour: '2-digit', minute: '2-digit', hour12: false }),
  });
  return <figure className="min-w-0 rounded-lg border border-edge-subtle bg-surface-raised p-3" data-metric={metric}>
    <figcaption className="flex items-baseline justify-between gap-2">
      <span className="text-sm font-semibold">{label}</span>
      <span className="text-xs text-content-secondary">{t('deployments.metrics.max', 'Max')} {model.points.length ? number(model.max) : '—'} {unit}</span>
    </figcaption>
    {description && <p className="mt-2 text-xs leading-relaxed text-content-secondary">{description}</p>}
    <p className="mt-2 flex justify-between gap-2 text-xs text-content-secondary"><span>{unit}</span><span>{timezone}</span></p>
    <svg viewBox="0 0 360 204" className="w-full" role="img" aria-label={`${label}; maximum ${number(model.max)} ${unit}`}
      onPointerMove={event => {
        const bounds = event.currentTarget.getBoundingClientRect();
        const x = (event.clientX - bounds.left) / bounds.width * 360;
        const nearest = model.points.reduce<typeof active>((best, point) => !best || Math.abs(point.x - x) < Math.abs(best.x - x) ? point : best, undefined);
        if (nearest) setSelected(nearest.ts);
      }} onPointerLeave={() => setSelected(null)}>
      {model.ticks.map(value => {
        const y = bottom - value / model.ceiling * (bottom - top);
        return <g key={value}>
          <line x1={left} x2={right} y1={y} y2={y} stroke="currentColor" className="text-edge-subtle" />
          <text x={left - 8} y={y + 4} textAnchor="end" fontSize="12" fill="currentColor" className="text-content-secondary">{formatNumber(value, { notation: 'compact', maximumFractionDigits: 3 })}</text>
        </g>;
      })}
      <line x1={left} x2={left} y1={top} y2={bottom} stroke="currentColor" className="text-edge-structural" />
      {[0, 0.5, 1].map(fraction => <text key={fraction} x={left + fraction * (right - left)} y={186}
        textAnchor={fraction === 0 ? 'start' : fraction === 1 ? 'end' : 'middle'} fontSize="12" fill="currentColor" className="text-content-secondary">
        {timeAxis.format(new Date(model.start + fraction * (model.end - model.start)))}
      </text>)}
      <path d={model.path} fill="none" stroke="currentColor" strokeWidth="2" className={color} />
      {selected && active && <line x1={active.x} x2={active.x} y1={top} y2={bottom} stroke="currentColor" strokeDasharray="3 3" className="text-content-tertiary" />}
      {model.points.map(point => <circle key={point.ts} cx={point.x} cy={point.y} r={point.ts === selected ? 5 : 3}
        tabIndex={0} fill="currentColor" className={`${color} cursor-pointer outline-focus`}
        onFocus={() => setSelected(point.ts)} onBlur={() => setSelected(null)} onClick={() => setSelected(point.ts)}
        aria-label={`${formatTime(point.ts)}: ${number(point.value)} ${unit}`}>
        <title>{formatTime(point.ts)}: {number(point.value)} {unit}</title>
      </circle>)}
    </svg>
    <div className="min-h-12 border-t border-edge-subtle pt-2 text-xs" data-slot="metric-reading">
      {active ? <><span className="block text-content-secondary">{formatTime(active.ts)}</span>
        <span className={`block font-semibold tabular-nums ${color}`}>{number(active.value)} {unit}</span></>
        : <span className="text-content-secondary">{t('deployments.metrics.noSamples', 'No samples in this range')}</span>}
    </div>
  </figure>;
}
