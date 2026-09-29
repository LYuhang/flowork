import * as React from 'react';
import { cva, type VariantProps } from 'class-variance-authority';
import { Circle, CircleAlert, CircleCheck, CircleX, LoaderCircle } from 'lucide-react';

import { cn } from '@/lib/utils';

export type SemanticStatus =
  | 'neutral'
  | 'info'
  | 'running'
  | 'success'
  | 'warning'
  | 'danger';

const statusColor = {
  neutral: 'text-content-tertiary',
  info: 'text-state-info',
  running: 'text-state-running',
  success: 'text-state-success',
  warning: 'text-state-warning',
  danger: 'text-state-danger',
} satisfies Record<SemanticStatus, string>;

const statusBadgeVariants = cva(
  'inline-flex min-h-6 shrink-0 whitespace-nowrap items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-medium',
  {
    variants: {
      status: {
        neutral: 'border-edge-structural bg-surface-sunken text-content-secondary',
        info: 'border-state-info/25 bg-state-info/10 text-state-info',
        running: 'border-state-running/25 bg-state-running/10 text-content-primary',
        success: 'border-state-success/25 bg-state-success/10 text-content-primary',
        warning: 'border-state-warning/30 bg-state-warning/10 text-state-warning',
        danger: 'border-state-danger/25 bg-state-danger/10 text-state-danger',
      },
    },
    defaultVariants: { status: 'neutral' },
  },
);

export interface StatusDotProps extends React.HTMLAttributes<HTMLSpanElement> {
  status?: SemanticStatus;
  pulse?: boolean;
}

export function StatusDot({
  status = 'neutral',
  pulse = false,
  className,
  'aria-label': ariaLabel,
  ...props
}: StatusDotProps) {
  return (
    <span
      className={cn('relative inline-flex h-2 w-2 shrink-0', className)}
      aria-hidden={ariaLabel ? undefined : true}
      aria-label={ariaLabel}
      {...props}
    >
      {pulse && status === 'running' ? (
        <span className={cn('absolute inset-0 rounded-full bg-current opacity-30 motion-safe:animate-pulse', statusColor[status])} />
      ) : null}
      <span className={cn('relative h-2 w-2 rounded-full bg-current', statusColor[status])} />
    </span>
  );
}

export interface StatusBadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof statusBadgeVariants> {
  showDot?: boolean;
}

export function StatusBadge({ status = 'neutral', showDot = true, className, children, ...props }: StatusBadgeProps) {
  const semanticStatus = status ?? 'neutral';
  return (
    <span className={cn(statusBadgeVariants({ status: semanticStatus }), className)} {...props}>
      {showDot ? <StatusDot status={semanticStatus} pulse={semanticStatus === 'running'} /> : null}
      {children}
    </span>
  );
}

const stateIcons = {
  neutral: Circle,
  info: Circle,
  running: LoaderCircle,
  success: CircleCheck,
  warning: CircleAlert,
  danger: CircleX,
} satisfies Record<SemanticStatus, typeof Circle>;

export interface ProgressStateProps extends React.HTMLAttributes<HTMLDivElement> {
  status?: SemanticStatus;
  label: React.ReactNode;
  progressLabel?: string;
  detail?: React.ReactNode;
  value?: number;
  max?: number;
}

export function ProgressState({
  status = 'neutral',
  label,
  progressLabel,
  detail,
  value,
  max = 100,
  className,
  ...props
}: ProgressStateProps) {
  const Icon = stateIcons[status];
  const labelId = React.useId();
  const percent = typeof value === 'number' && max > 0
    ? Math.min(100, Math.max(0, (value / max) * 100))
    : null;
  return (
    <div className={cn('space-y-2', className)} {...props}>
      <div className="flex min-h-6 items-center gap-2 text-sm">
        <Icon
          className={cn('h-4 w-4 shrink-0', statusColor[status], status === 'running' && 'motion-safe:animate-spin')}
          aria-hidden="true"
        />
        <span id={labelId} className="min-w-0 flex-1 font-medium">{label}</span>
        {detail ? <span className="text-xs text-muted-foreground">{detail}</span> : null}
      </div>
      {percent !== null ? (
        <div
          className="h-1.5 overflow-hidden rounded-full bg-surface-sunken"
          role="progressbar"
          aria-label={progressLabel}
          aria-labelledby={progressLabel ? undefined : labelId}
          aria-valuemin={0}
          aria-valuemax={max}
          aria-valuenow={value}
        >
          <div
            className={cn('h-full rounded-full bg-current transition-[width] duration-feedback', statusColor[status])}
            style={{ width: `${percent}%` }}
          />
        </div>
      ) : null}
    </div>
  );
}
