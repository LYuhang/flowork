import { useEffect, useState } from 'react';
import { Menu } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useLocation } from 'react-router';

import { AppSidebar } from '@/app/AppSidebar';
import { Button } from '@/components/ui/button';
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from '@/components/ui/sheet';

const SECTION_LABELS: Record<string, { key: string; fallback: string }> = {
  chat: { key: 'nav.chat', fallback: 'Chat' },
  workspace: { key: 'nav.workspace', fallback: 'Workflows' },
  tasks: { key: 'nav.tasks', fallback: 'Tasks' },
  deployments: { key: 'nav.deployments', fallback: 'Deployments' },
  credentials: { key: 'nav.credentials', fallback: 'API Keys' },
  'mcp-servers': { key: 'nav.mcpServers', fallback: 'MCP Servers' },
  skills: { key: 'nav.skills', fallback: 'Skills' },
  knowledge: { key: 'nav.knowledge', fallback: 'Knowledge' },
  storage: { key: 'nav.storage', fallback: 'Storage' },
  settings: { key: 'nav.settings', fallback: 'Settings' },
};

export function MobileAppHeader() {
  const { t } = useTranslation();
  const location = useLocation();
  const [open, setOpen] = useState(false);
  const section = location.pathname.split('/').filter(Boolean)[0] ?? 'chat';
  const label = SECTION_LABELS[section] ?? { key: 'ws_title', fallback: 'Flowork' };

  const [locationKey, setLocationKey] = useState(location.key);
  if (locationKey !== location.key) {
    setLocationKey(location.key);
    setOpen(false);
  }

  useEffect(() => {
    const desktop = window.matchMedia('(min-width: 768px)');
    const closeOnDesktop = () => { if (desktop.matches) setOpen(false); };
    desktop.addEventListener('change', closeOnDesktop);
    return () => desktop.removeEventListener('change', closeOnDesktop);
  }, []);

  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <header
        className="surface-topbar flex h-12 shrink-0 items-center gap-2 px-2 md:hidden"
        data-testid="mobile-app-header"
      >
        <SheetTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="h-11 w-11"
            aria-label={t('nav.open', 'Open navigation')}
            aria-expanded={open}
          >
            <Menu className="h-4 w-4" aria-hidden="true" />
          </Button>
        </SheetTrigger>
        <div className="min-w-0 flex-1 truncate text-sm font-semibold">
          {t(label.key, label.fallback)}
        </div>
      </header>
      <SheetContent
        side="left"
        className="w-[min(88vw,320px)] max-w-none supports-[height:100dvh]:h-dvh overflow-hidden p-0 sm:max-w-none"
        data-testid="mobile-nav-drawer"
      >
        <SheetHeader className="sr-only">
          <SheetTitle>{t('nav.primary', 'Navigation')}</SheetTitle>
          <SheetDescription>
            {t('nav.mobileDescription', 'Navigate Flowork and manage your workspace.')}
          </SheetDescription>
        </SheetHeader>
        <AppSidebar mobile onNavigate={() => setOpen(false)} />
      </SheetContent>
    </Sheet>
  );
}
