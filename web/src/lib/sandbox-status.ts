export function formatSandboxTtl(ttlSeconds: number | null | undefined): string | null {
  if (typeof ttlSeconds !== 'number' || !Number.isFinite(ttlSeconds)) return null;
  const seconds = Math.max(0, Math.ceil(ttlSeconds));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.ceil(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return remainder ? `${hours}h ${remainder}m` : `${hours}h`;
}

export interface SandboxTiming {
  idle_elapsed_s?: number | null;
  ttl_s?: number | null;
  ttl_paused?: boolean;
  ttl_remaining_s?: number | null;
}

export type SandboxLifecycleStatus =
  | 'idle'
  | 'running'
  | 'hibernating'
  | 'hibernated'
  | 'restoring'
  | 'releasing'
  | 'snapshot_failed'
  | 'closed';

export interface SandboxResourceStatus {
  workspace_projection: 'materialized' | 'released';
  vfs_mount: 'attached' | 'detached' | 'unknown';
  runtime_volume: 'attached' | 'detached' | 'not_required' | 'unknown';
  runtime_process: 'resident' | 'stopped' | 'unknown';
  authentication:
    | 'account_bound'
    | 'turn_capability_active'
    | 'detached'
    | 'unknown';
  network: 'connected' | 'disconnected' | 'unknown';
  snapshot_kind: 'baseline' | 'session_hibernation' | null;
  runtime_type: string | null;
  lifecycle_generation: number | null;
  lifecycle_state: string;
}

/** Derive display time from the positive idle clock and phase TTL. */
export function sandboxTtlRemaining(timing: SandboxTiming | null | undefined): number | null {
  if (!timing || timing.ttl_paused) return null;
  if (
    typeof timing.ttl_s === 'number'
    && Number.isFinite(timing.ttl_s)
    && typeof timing.idle_elapsed_s === 'number'
    && Number.isFinite(timing.idle_elapsed_s)
  ) {
    return Math.max(0, timing.ttl_s - timing.idle_elapsed_s);
  }
  return typeof timing.ttl_remaining_s === 'number' && Number.isFinite(timing.ttl_remaining_s)
    ? Math.max(0, timing.ttl_remaining_s)
    : null;
}
