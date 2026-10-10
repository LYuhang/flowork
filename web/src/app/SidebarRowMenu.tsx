import { Fragment, type ReactNode } from 'react';
import { MoreHorizontal, type LucideIcon } from 'lucide-react';
import { ContextMenu, ContextMenuContent, ContextMenuItem, ContextMenuSeparator, ContextMenuTrigger } from '@/components/ui/context-menu';
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/dropdown-menu';

export interface SidebarRowAction {
  label: string;
  icon: LucideIcon;
  onSelect: () => void;
  disabled?: boolean;
  destructive?: boolean;
}

/** Both entry points use the same actions and confirmation handlers. */
export function SidebarRowMenu({ label, actions, children }: {
  label: string;
  actions: SidebarRowAction[];
  children: ReactNode;
}) {
  const items = (context: boolean) => {
    const Item = context ? ContextMenuItem : DropdownMenuItem;
    const Separator = context ? ContextMenuSeparator : DropdownMenuSeparator;
    return actions.map(({ label, icon: Icon, onSelect, disabled, destructive }) => (
      <Fragment key={label}>
        {destructive && <Separator />}
        <Item disabled={disabled} onSelect={onSelect} className={destructive ? 'text-destructive focus:text-destructive' : undefined}>
          <Icon className="mr-2 size-4" />{label}
        </Item>
      </Fragment>
    ));
  };
  return (
    <ContextMenu>
      <ContextMenuTrigger asChild>
        <div className="relative">
          {children}
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button type="button" aria-label={label} title={label}
                className="absolute right-1 top-1/2 grid size-7 -translate-y-1/2 place-items-center rounded-md text-muted-foreground hover:bg-surface-raised hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring [@media(pointer:coarse)]:size-9">
                <MoreHorizontal className="size-4" />
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-48">{items(false)}</DropdownMenuContent>
          </DropdownMenu>
        </div>
      </ContextMenuTrigger>
      <ContextMenuContent className="w-48">{items(true)}</ContextMenuContent>
    </ContextMenu>
  );
}
