import { useTranslation } from 'react-i18next';

import { AppIcon } from '@/app/AppIcon';

export function AppLogo() {
  const { t } = useTranslation();
  const brand = t('ws_title', 'Flowork');

  return (
    <span
      data-testid="app-logo"
      aria-label={brand}
      className="flex select-none items-center gap-2.5 text-xl font-semibold leading-7 text-foreground"
    >
      <AppIcon
        alt=""
        aria-hidden="true"
        className="size-8 shrink-0 rounded-[9px]"
      />
      <span>{brand}</span>
    </span>
  );
}
