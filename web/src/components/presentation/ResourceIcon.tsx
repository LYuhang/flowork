import type { HTMLAttributes } from 'react';

import { resourceVisual, type ResourceKind } from '@/lib/presentation/resource-visuals';
import { cn } from '@/lib/utils';

const sizes = {
  sm: { shell: 'size-7 rounded-md', icon: 'size-3.5' },
  md: { shell: 'size-9 rounded-lg', icon: 'size-4' },
  lg: { shell: 'size-11 rounded-xl', icon: 'size-5' },
} as const;

export function ResourceIcon({
  kind,
  size = 'md',
  variant = 'badge',
  label,
  className,
  ...props
}: Omit<HTMLAttributes<HTMLSpanElement>, 'children'> & {
  kind: ResourceKind;
  size?: keyof typeof sizes;
  variant?: 'badge' | 'plain';
  label?: string;
}) {
  const visual = resourceVisual(kind);
  const Icon = visual.icon;
  return (
    <span
      className={cn(
        'grid shrink-0 place-items-center',
        sizes[size].shell,
        visual.foregroundClass,
        variant === 'badge' && ['ring-1 ring-inset', visual.surfaceClass],
        className,
      )}
      role={label ? 'img' : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      {...props}
    >
      <Icon className={sizes[size].icon} />
    </span>
  );
}
