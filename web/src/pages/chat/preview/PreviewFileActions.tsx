import { Download, ExternalLink, MoreHorizontal, RefreshCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem } from '@/components/ui/dropdown-menu';
import { Tooltip, TooltipTrigger, TooltipContent, TooltipProvider } from '@/components/ui/tooltip';

export function PreviewFileActions({ openHref, downloadHref, filename, onRefresh }: {
  openHref?: string; downloadHref?: string; filename: string; onRefresh: () => void;
}) {
  const { t } = useTranslation();
  const actions = [
    ...(openHref ? [{ label: t('preview.action.openInNewPage', 'Open in new page'), Icon: ExternalLink, href: openHref, download: false }] : []),
    ...(downloadHref ? [{ label: t('preview.action.download', 'Download'), Icon: Download, href: downloadHref, download: true }] : []),
    { label: t('preview.action.refresh', 'Refresh preview'), Icon: RefreshCw, href: undefined, download: false },
  ];
  return <TooltipProvider delayDuration={250}>
    <div className="preview-file-actions-expanded shrink-0 items-center gap-1">
      {actions.map(({ label, Icon, href, download }) => <Tooltip key={label}>
        <TooltipTrigger asChild>{href ? <Button asChild size="icon-sm" variant="ghost">
          <a href={href} target={download ? undefined : '_blank'} rel="noopener noreferrer" download={download ? filename : undefined} aria-label={label}><Icon className="h-3.5 w-3.5" /></a>
        </Button> : <Button size="icon-sm" variant="ghost" aria-label={label} onClick={onRefresh}><Icon className="h-3.5 w-3.5" /></Button>}</TooltipTrigger>
        <TooltipContent>{label}</TooltipContent>
      </Tooltip>)}
    </div>
    <DropdownMenu><DropdownMenuTrigger asChild>
      <Button className="preview-file-actions-more shrink-0" variant="ghost" size="icon-sm" aria-label={t('common.more', 'More')} title={t('common.more', 'More')}><MoreHorizontal className="h-4 w-4" /></Button>
    </DropdownMenuTrigger><DropdownMenuContent align="end">
      {actions.map(({ label, Icon, href, download }) => href ? <DropdownMenuItem key={label} asChild>
        <a href={href} target={download ? undefined : '_blank'} rel="noopener noreferrer" download={download ? filename : undefined}><Icon />{label}</a>
      </DropdownMenuItem> : <DropdownMenuItem key={label} onSelect={onRefresh}><Icon />{label}</DropdownMenuItem>)}
    </DropdownMenuContent></DropdownMenu>
  </TooltipProvider>;
}
