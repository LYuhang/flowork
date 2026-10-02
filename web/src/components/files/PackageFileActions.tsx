import { useRef, useState } from 'react';
import { useDirtyNavigationGuard } from '@/lib/navigation/use-dirty-navigation-guard';
import { MoreHorizontal } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/dropdown-menu';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { resolveFileCapability } from '@/lib/files/capabilities';

export type PackageFileOperations = {
  loadFile: (path: string) => Promise<Blob>;
  saveFile: (path: string, file: File, create: boolean) => Promise<void>;
  deleteFile: (path: string) => Promise<void>;
};

/** Mutations stay with the package owner; the dialog captures its opening revision. */
export function PackageFileActions({ path, mimeType, required = false, operations, savedHint }: {
  path: string;
  mimeType?: string | null;
  required?: boolean;
  operations: PackageFileOperations;
  savedHint: string;
}) {
  const { t } = useTranslation();
  const [mode, setMode] = useState<'edit' | 'create' | 'upload' | 'replace' | 'delete' | null>(null);
  const [name, setName] = useState(path);
  const [value, setValue] = useState('');
  const [original, setOriginal] = useState('');
  const [upload, setUpload] = useState<File | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [discard, setDiscard] = useState(false);
  const [readable, setReadable] = useState(false);
  const captured = useRef(operations);
  const dirty = value !== original || upload !== null || ((mode === 'create' || mode === 'upload') && name !== '');
  const blocker = useDirtyNavigationGuard(!!mode && dirty);
  const message = (reason: unknown) => {
    const raw = reason instanceof Error ? reason.message : String(reason);
    return /package_file_changed/.test(raw) ? t('files.manage.conflict')
      : /package_path_exists/.test(raw) ? t('files.manage.pathExists') : raw;
  };
  const begin = async (next: NonNullable<typeof mode>) => {
    captured.current = operations;
    setReadable(next !== 'edit');
    setMode(next); setError(null); setUpload(null); setValue(''); setOriginal('');
    setName(next === 'create' || next === 'upload' ? '' : path);
    if (next !== 'edit') return;
    setPending(true);
    try {
      const blob = await captured.current.loadFile(path);
      if (blob.size > 2 * 1024 * 1024) throw new Error(t('files.manage.tooLarge'));
      const text = new TextDecoder('utf-8', { fatal: true }).decode(await blob.arrayBuffer());
      if (text.includes('\0')) throw new Error(t('files.manage.notText'));
      setValue(text); setOriginal(text); setReadable(true);
    } catch (reason) { setError(message(reason)); }
    finally { setPending(false); }
  };
  const close = () => {
    if (pending) return;
    if (dirty && mode !== 'delete') setDiscard(true);
    else setMode(null);
  };
  const submit = async () => {
    if (!mode) return;
    setPending(true); setError(null);
    try {
      if (mode === 'delete') await captured.current.deleteFile(name);
      else {
        const file = mode === 'upload' || mode === 'replace' ? upload : new File([value], name, { type: mode === 'edit' ? mimeType || 'text/plain' : 'text/plain' });
        if (!file) return;
        await captured.current.saveFile(name.trim(), file, mode === 'create' || mode === 'upload');
      }
      setMode(null); toast.success(savedHint);
    } catch (reason) { setError(message(reason)); }
    finally { setPending(false); }
  };
  const textMode = mode === 'edit' || mode === 'create';
  return <>
    <DropdownMenu>
      <DropdownMenuTrigger asChild><Button variant="ghost" size="icon" aria-label={t('files.manage.actions')}><MoreHorizontal className="size-4" /></Button></DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuItem disabled={!resolveFileCapability(path, mimeType).source} onSelect={() => void begin('edit')}>{t('files.manage.edit')}</DropdownMenuItem>
        <DropdownMenuItem onSelect={() => void begin('replace')}>{t('files.manage.replace')}</DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => void begin('create')}>{t('files.manage.create')}</DropdownMenuItem>
        <DropdownMenuItem onSelect={() => void begin('upload')}>{t('files.manage.upload')}</DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem disabled={required} className="text-destructive" onSelect={() => void begin('delete')}>{t('files.manage.delete')}</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
    <Dialog open={mode !== null} onOpenChange={open => { if (!open) close(); }}>
      <DialogContent className={textMode ? 'sm:max-w-4xl' : 'sm:max-w-lg'} closeDisabled={pending}>
        <DialogHeader>
          <DialogTitle>{t(`files.manage.${mode ?? 'edit'}`)}</DialogTitle>
          <DialogDescription>{mode === 'delete' ? t('files.manage.deleteHint', { path: name }) : savedHint}</DialogDescription>
        </DialogHeader>
        {mode !== 'delete' && <div className="space-y-3 min-w-0">
          <div className="space-y-1.5"><Label htmlFor="package-file-path">{t('files.manage.path')}</Label>
            <Input id="package-file-path" value={name} disabled={pending || mode === 'edit' || mode === 'replace'} placeholder="references/notes.md" onChange={e => setName(e.target.value)} />
          </div>
          {textMode ? <div className="space-y-1.5"><Label htmlFor="package-file-content">{t('files.manage.content')}</Label>
            <Textarea id="package-file-content" spellCheck={false} className="h-[50vh] min-h-48 resize-y font-mono text-xs leading-5" value={value} disabled={pending || !readable} onChange={e => setValue(e.target.value)} />
          </div> : <Input type="file" aria-label={t('files.manage.chooseFile')} disabled={pending} onChange={e => {
            const file = e.target.files?.[0] ?? null; setUpload(file);
            if (mode === 'upload' && file) setName(file.name);
          }} />}
        </div>}
        {error && <p role="alert" className="whitespace-pre-wrap break-words text-sm text-destructive">{error}</p>}
        <DialogFooter>
          <Button variant="outline" disabled={pending} onClick={close}>{t('cancel', 'Cancel')}</Button>
          <Button variant={mode === 'delete' ? 'destructive' : 'default'} disabled={pending || !name.trim() || (mode === 'edit' && (!readable || value === original)) || ((mode === 'upload' || mode === 'replace') && !upload)} onClick={() => void submit()}>{pending ? t('saving', 'Saving…') : mode === 'delete' ? t('delete', 'Delete') : t('save', 'Save')}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
    <Dialog open={discard || blocker.state === 'blocked'} onOpenChange={open => {setDiscard(open); if (!open && blocker.state === 'blocked') blocker.reset();}}><DialogContent><DialogHeader><DialogTitle>{t('files.manage.discard')}</DialogTitle><DialogDescription>{t('files.manage.discardHint')}</DialogDescription></DialogHeader><DialogFooter>
      <Button variant="outline" onClick={() => {setDiscard(false); if (blocker.state === 'blocked') blocker.reset();}}>{t('files.manage.keepEditing')}</Button>
      <Button variant="destructive" onClick={() => { setDiscard(false); setMode(null); if (blocker.state === 'blocked') blocker.proceed(); }}>{t('files.manage.discard')}</Button>
    </DialogFooter></DialogContent></Dialog>
  </>;
}
