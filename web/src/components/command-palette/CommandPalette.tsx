/**
 * Cmd+K command palette.
 *
 * Reads `commandPaletteOpen` from `useUIStore` so any code (toolbar
 * button, global shortcut handler, future onboarding tour) can pop the
 * palette by flipping a single flag. Render is gated by the shadcn
 * `CommandDialog` which itself wraps Radix Dialog + cmdk — so focus
 * trap, scroll lock, and Esc-to-close are all handled for us.
 *
 * Action handlers receive a live `ActionCtx` built from `useNavigate`
 * and `useParams` rather than captured at module load: when the user
 * triggers an action the navigate ref is current and `wfId` reflects
 * whichever workflow route is active.
 */
import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate, useParams } from 'react-router';
import {
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandShortcut,
} from '@/components/ui/command';
import { useUIStore } from '@/stores/ui';
import { ACTIONS, type Action, type ActionCtx } from './actions';

const GROUP_LABEL_KEYS: Record<Action['group'], string> = {
  navigate: 'commandPalette.group.navigate',
  workflow: 'commandPalette.group.workflow',
  view: 'commandPalette.group.view',
};

/** Stable group order — matches the visual hierarchy a power user expects. */
const GROUP_ORDER: Action['group'][] = ['navigate', 'workflow', 'view'];

export function CommandPalette() {
  const { t } = useTranslation();
  const open = useUIStore((s) => s.commandPaletteOpen);
  const setOpen = useUIStore((s) => s.setCommandPaletteOpen);
  const navigate = useNavigate();
  const params = useParams<{ wfId?: string }>();

  const ctx: ActionCtx = useMemo(
    () => ({ navigate, wfId: params.wfId ?? null }),
    [navigate, params.wfId],
  );

  // Bucket actions by group while preserving the registry's intra-group
  // order. Computed once per `open` toggle — cheap, but memoised anyway
  // because `useMemo` here also reads as documentation.
  const grouped = useMemo(() => {
    const buckets: Record<Action['group'], Action[]> = {
      navigate: [],
      workflow: [],
      view: [],
    };
    for (const action of ACTIONS) {
      if (action.group === 'workflow' && !ctx.wfId) continue;
      buckets[action.group].push(action);
    }
    return buckets;
  }, [ctx.wfId]);

  const handleSelect = (action: Action) => {
    setOpen(false);
    // Defer to next tick so the dialog has a chance to unmount before
    // the action runs — otherwise focus may bounce when an action
    // triggers a navigation or toast.
    queueMicrotask(() => action.handler(ctx));
  };

  return (
    <CommandDialog open={open} onOpenChange={setOpen} title={t('commandPalette.open')}>
      <CommandInput aria-label={t('commandPalette.placeholder')} placeholder={t('commandPalette.placeholder')} />
      <CommandList>
        <CommandEmpty>{t('commandPalette.empty')}</CommandEmpty>
        {GROUP_ORDER.map((group) => {
          const items = grouped[group];
          if (items.length === 0) return null;
          return (
            <CommandGroup key={group} heading={t(GROUP_LABEL_KEYS[group])}>
              {items.map((action) => {
                const label = t(action.labelKey);
                return (
                  <CommandItem
                    key={action.id}
                    value={`${action.id} ${label}`}
                    onSelect={() => handleSelect(action)}
                  >
                    {label}
                    {action.shortcut ? (
                      <CommandShortcut>{action.shortcut}</CommandShortcut>
                    ) : null}
                  </CommandItem>
                );
              })}
            </CommandGroup>
          );
        })}
      </CommandList>
    </CommandDialog>
  );
}
