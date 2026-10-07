import { useSearchParams } from 'react-router';

export type SortDirection = 'asc' | 'desc';
export function useResourceSort<T extends string>(fields: readonly T[], defaultField: T) {
  const [params, setParams] = useSearchParams();
  const requested = params.get('sort_by');
  const field = fields.includes(requested as T) ? requested as T : defaultField;
  const direction: SortDirection = params.get('sort_order') === 'asc' ? 'asc' : 'desc';
  const setValue = (value: string) => {
    const [nextField, nextDirection] = value.split(':');
    const next = new URLSearchParams(params);
    next.set('sort_by', nextField);
    next.set('sort_order', nextDirection);
    next.delete('page');
    next.delete('offset');
    setParams(next, { replace: true });
  };
  return { field, direction, value: `${field}:${direction}`, setValue };
}

/** Complete inventories only: order before rendering, with missing dates last. */
export function compareResourceValues(a: string | null | undefined, b: string | null | undefined, direction: SortDirection, name = false): number {
  const left = name ? a : a ? Date.parse(a) : undefined;
  const right = name ? b : b ? Date.parse(b) : undefined;
  const missing = (v: string | number | null | undefined) => v == null || v === '' || typeof v === 'number' && !Number.isFinite(v);
  if (missing(left) || missing(right)) return Number(missing(left)) - Number(missing(right));
  const order = name ? String(left).localeCompare(String(right)) : Number(left) - Number(right);
  return direction === 'asc' ? order : -order;
}
