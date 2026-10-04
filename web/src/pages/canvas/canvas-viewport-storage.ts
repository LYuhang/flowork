import type { Viewport } from '@xyflow/react';

export function readViewport(key: string): Viewport | null {
  try {
    const value = JSON.parse(localStorage.getItem(key) ?? 'null');
    return value && [value.x, value.y, value.zoom].every(Number.isFinite) && value.zoom >= 0.1 && value.zoom <= 2 ? value : null;
  } catch { return null; }
}
