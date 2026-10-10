import { ChevronDown } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuRadioGroup,
  DropdownMenuRadioItem, DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { useComposerPreferences } from '@/stores/composer-preferences';

export function SendShortcutPreference() {
  const { t } = useTranslation();
  const shortcut = useComposerPreferences((state) => state.sendShortcut);
  const setShortcut = useComposerPreferences((state) => state.setSendShortcut);
  const modifier = /Mac|iPhone|iPad/.test(navigator.platform) ? '⌘' : 'Ctrl';
  const modifierLabel = t('composer.shortcut.modifier', { modifier });
  const enterLabel = t('composer.shortcut.enter');

  return (
    <div className="flex flex-wrap items-center gap-x-2 text-xs text-content-tertiary">
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button type="button" className="inline-flex items-center gap-1 rounded py-1 hover:text-content-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            aria-label={t('composer.shortcut.choose')}>
            {shortcut === 'enter' ? enterLabel : modifierLabel}
            <ChevronDown className="h-3 w-3" aria-hidden="true" />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start">
          <DropdownMenuRadioGroup value={shortcut} onValueChange={(value) => {
            if (value === 'enter' || value === 'modifier-enter') setShortcut(value);
          }}>
            <DropdownMenuRadioItem value="modifier-enter">{modifierLabel}</DropdownMenuRadioItem>
            <DropdownMenuRadioItem value="enter">{enterLabel}</DropdownMenuRadioItem>
          </DropdownMenuRadioGroup>
        </DropdownMenuContent>
      </DropdownMenu>
      <span>{t('composer.shortcut.newline')}</span>
    </div>
  );
}
