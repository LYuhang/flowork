import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Undo2 } from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { ConfirmationDialog } from '@/components/ui/confirmation-dialog';

export function DiscardPackageDraft({ expectedHash, disabled, onDiscard }: {
  expectedHash: string;
  disabled?: boolean;
  onDiscard: (expectedHash: string) => Promise<void>;
}) {
  const { t } = useTranslation();
  const [confirmedHash, setConfirmedHash] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  return <>
    <Button variant="outline" size="sm" disabled={disabled || pending}
      onClick={() => setConfirmedHash(expectedHash)}><Undo2 />{t('files.manage.discardDraft')}</Button>
    <ConfirmationDialog open={confirmedHash !== null} onOpenChange={open => { if (!open) setConfirmedHash(null); }}
      title={t('files.manage.discardDraft')} description={t('files.manage.discardDraftHint')}
      confirmLabel={t('files.manage.discardDraft')} cancelLabel={t('cancel', 'Cancel')} pending={pending}
      onConfirm={async () => {
        if (confirmedHash === null) return;
        setPending(true);
        try {
          await onDiscard(confirmedHash);
          setConfirmedHash(null);
          toast.success(t('files.manage.draftDiscarded'));
        } catch (error) { toast.error(error instanceof Error ? error.message : String(error)); }
        finally { setPending(false); }
      }} />
  </>;
}
