import { useRef, useState } from 'react';
import { useUIStore } from '@/stores/ui';
import { useNavigate } from 'react-router';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { GitBranch, Loader2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog';
import { createChatBranch, previewChatBranch } from '@/lib/api/chat-branches';
import { fetchChatWorkspace } from '@/lib/api/queries/chats';

export function ChatBranchDialog({ chatId, turnId, excerpt }: { chatId: string; turnId: string; excerpt: string }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState('');
  const [busy, setBusy] = useState(false);
  const [attemptId, setAttemptId] = useState<string | null>(null);
  const [uncertain, setUncertain] = useState(false);
  const [notFound, setNotFound] = useState(false);
  const submitting = useRef(false);
  const preview = useQuery({
    queryKey: ['chat-branch-preview', chatId, turnId],
    queryFn: () => previewChatBranch(chatId, turnId), enabled: open, retry: false, staleTime: 0,
  });
  const goToBranch = (id: string) => {
    void queryClient.invalidateQueries({ queryKey: ['chats'] });
    setOpen(false);
    if (preview.data?.scope_id && !preview.data.scope_id.startsWith('__')) {
      useUIStore.getState().setActiveChatId('chat', id);
    } else {
      navigate(`/chat/open/${encodeURIComponent(id)}`);
    }
  };
  const create = async () => {
    if (submitting.current || attemptId || !preview.data) return;
    submitting.current = true;
    setBusy(true);
    const id = crypto.randomUUID();
    setAttemptId(id);
    try {
      const result = await createChatBranch(chatId, { chat_id: id, turn_id: turnId, name: name.trim() });
      goToBranch(result.chat_id);
    } catch {
      setUncertain(true);
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  };
  const checkCreatedBranch = async () => {
    if (!attemptId || submitting.current) return;
    submitting.current = true;
    setBusy(true);
    setNotFound(false);
    try {
      await fetchChatWorkspace(attemptId);
      goToBranch(attemptId);
    } catch {
      setNotFound(true);
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  };
  return <Dialog open={open} onOpenChange={(value) => { if (!busy) setOpen(value); }}>
    <DialogTrigger asChild>
      <button type="button" aria-label={t('chat.branch.open')} title={t('chat.branch.open')}
        className="inline-flex h-8 w-8 items-center justify-center rounded-md hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
        <GitBranch className="h-3.5 w-3.5" />
      </button>
    </DialogTrigger>
    <DialogContent className="max-w-lg">
      <DialogHeader><DialogTitle>{t('chat.branch.open')}</DialogTitle>
        <DialogDescription>{t('chat.branch.description')}</DialogDescription></DialogHeader>
      <blockquote className="line-clamp-3 border-l-2 border-edge-structural pl-3 text-sm text-muted-foreground">{excerpt}</blockquote>
      <p className="text-sm">{t('chat.branch.files')}</p>
      {preview.isPending || preview.isFetching ? <p role="status">{t('chat.branch.loading')}</p> : preview.isError ?
        <p role="alert" className="text-sm text-destructive">{t('chat.branch.unavailable')}</p> : <>
        <dl className="space-y-2 text-xs text-muted-foreground">
          <div><dt className="font-medium">{t('chat.branch.source')}</dt><dd className="break-all">{chatId}</dd></div>
          <div><dt className="font-medium">{t('chat.branch.project')}</dt><dd className="break-all">{preview.data.project_id}</dd></div>
        </dl>
        {!!preview.data.unavailable_files?.length && <div role="status" className="space-y-1 text-sm text-muted-foreground">
          <p>{t('chat.branch.unavailableFiles')}</p>
          <ul className="list-disc pl-5">{preview.data.unavailable_files.map((file, index) => <li key={index} className="break-all">{file}</li>)}</ul>
        </div>}
        <label className="space-y-2 text-sm"><span>{t('chat.branch.name')}</span>
          <Input value={name} onChange={(event) => setName(event.target.value)} maxLength={200} disabled={busy || !!attemptId} />
        </label>
        {uncertain ? <div className="space-y-2 text-sm" role="alert">
          <p>{t('chat.branch.uncertain')}</p>
          <code className="block break-all text-xs">{attemptId}</code>
          {notFound && <p>{t('chat.branch.notFound')}</p>}
          <Button variant="outline" disabled={busy} onClick={() => void checkCreatedBranch()}>{t('chat.branch.check')}</Button>
        </div> : <Button disabled={busy || !!attemptId || !name.trim()} onClick={() => void create()}>
          {busy && <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" />}{t('chat.branch.create')}
        </Button>}
      </>}
    </DialogContent>
  </Dialog>;
}
