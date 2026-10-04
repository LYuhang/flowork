import { ResourceAccessBadge } from '@/components/resources/ResourceAccessBadge';
import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  BookOpen,
  Pencil,
  RefreshCw,
  Share2,
  Trash2,
} from 'lucide-react';
import { useNavigate, useParams } from 'react-router';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { SectionBlock } from '@/components/layout/section-block';
import { DetailSummary } from '@/components/layout/detail-summary';
import { CopyButton } from '@/components/ui/copy-button';
import { useFormatDateTime } from '@/lib/timezone';
import { EntityDetailShell } from '@/components/layout/entity-detail-shell';
import { ResourceShareDialog } from '@/components/modals/ResourceShareDialog';
import { ResourceProvenanceLine } from '@/components/resources/ResourceProvenanceLine';
import { AsyncState } from '@/components/ui/async-state';
import { Button } from '@/components/ui/button';
import { ConfirmationDialog } from '@/components/ui/confirmation-dialog';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import {
  deleteKb,
  getKb,
  listKbFiles,
  updateKb,
  getKnowledgeDraft, getKnowledgeVersions, getKnowledgeVersion, getKnowledgeVersionFile, writeKnowledgeDraftFile, publishKnowledgeDraft,
  type KbFile,
} from '@/lib/api/kb';
import { KnowledgeSourceExplorer } from '@/pages/knowledge/KnowledgeSourceExplorer';

function errorState(error: unknown): 'permission' | 'error' {
  const message = error instanceof Error ? error.message : String(error ?? '');
  return /(?:\b403\b|forbidden|permission)/i.test(message) ? 'permission' : 'error';
}

export function KnowledgeDetailPage() {
  const { kbId = '' } = useParams<{ kbId: string }>();
  const navigate = useNavigate();
  const { t } = useTranslation();
  const client = useQueryClient();
  const formatTime = useFormatDateTime();
  const [tab, setTab] = useState('files');
  const [editingRequested, setEditing] = useState(false);
  const [selectedVersion, setSelectedVersion] = useState('latest');
  const [publishOpen, setPublishOpen] = useState(false);
  const [publishHash, setPublishHash] = useState('');
  const [shareOpen, setShareOpen] = useState(false);
  const [editOpen, setEditOpen] = useState(false);
  const [editName, setEditName] = useState('');
  const [editHash, setEditHash] = useState('');
  const [editDescription, setEditDescription] = useState('');
  const [deleteKbOpen, setDeleteKbOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<{
    files: KbFile[];
    label: string;
    folder: boolean;
  } | null>(null);

  const detail = useQuery({
    queryKey: ['knowledge-base', kbId],
    queryFn: () => getKb(kbId),
    enabled: Boolean(kbId),
    // Refresh permissions independently of file/content changes.
    refetchInterval: 15_000,
    refetchOnWindowFocus: 'always',
  });
  const files = useQuery({
    queryKey: ['knowledge-files', kbId],
    queryFn: () => listKbFiles(kbId),
    enabled: Boolean(kbId),
  });

  const canUpdate = !!detail.data?.access.capabilities.includes('update');
  const editing = editingRequested && canUpdate && selectedVersion === 'latest';
  const draft = useQuery({queryKey: ['knowledge-draft', kbId], queryFn: () => getKnowledgeDraft(kbId), enabled: canUpdate});
  const versions = useQuery({queryKey: ['knowledge-versions', kbId], queryFn: () => getKnowledgeVersions(kbId), enabled: !!detail.data});
  const number = selectedVersion === 'latest' ? detail.data?.package_version : Number(selectedVersion);
  const snapshot = useQuery({queryKey: ['knowledge-version', kbId, number], queryFn: () => getKnowledgeVersion(kbId, number!), enabled: !!number && !editing});
  const shown = editing ? draft : snapshot;
  const shownFiles: KbFile[] = (shown.data?.files ?? []).map(path => ({
    ...(files.data?.find(file => file.name === path) ?? {}), id: path, name: path,
  } as KbFile));
  const loadFile = (path: string) => getKnowledgeVersionFile(kbId, editing ? 0 : number!, path);
  const refresh = async () => {
    const current = await detail.refetch();
    await Promise.all([files.refetch(), versions.refetch(), snapshot.refetch(),
      ...(current.data?.access.capabilities.includes('update') ? [draft.refetch()] : []),
    ]);
  };
  const applyFile = async (path: string, file?: File, create = false) => {
    if (!draft.data) throw new Error('Draft unavailable');
    const next = await writeKnowledgeDraftFile(kbId, path, draft.data.content_hash, file, create);
    client.setQueryData(['knowledge-draft', kbId], next);
  };
  const upload = useMutation({
    mutationFn: async (items: Array<{file: File; path: string}>) => {
      let token = draft.data!.content_hash;
      try {
        for (const item of items) {
          const next = await writeKnowledgeDraftFile(kbId, item.path, token, item.file, true);
          token = next.content_hash;
          client.setQueryData(['knowledge-draft', kbId], next);
        }
      } finally { await draft.refetch(); }
    },
    onSuccess: () => toast.success(t('files.manage.skillSaved')),
    onError: reason => toast.error(reason instanceof Error ? reason.message : String(reason)),
  });
  const removeFile = useMutation({
    mutationFn: async (target: {files: KbFile[]}) => {
      let token = draft.data!.content_hash;
      try {
        for (const file of target.files) {
          const next = await writeKnowledgeDraftFile(kbId, file.name, token);
          token = next.content_hash;
          client.setQueryData(['knowledge-draft', kbId], next);
        }
      } finally { await draft.refetch(); }
    },
    onSuccess: () => {setDeleteTarget(null); toast.success(t('files.manage.skillSaved'));},
    onError: reason => toast.error(reason instanceof Error ? reason.message : String(reason)),
  });
  const publish = useMutation({
    mutationFn: () => publishKnowledgeDraft(kbId, publishHash),
    onSuccess: async () => {
      setPublishOpen(false); setEditing(false); setSelectedVersion('latest');
      await client.invalidateQueries({queryKey: ['knowledge-version', kbId]});
      await refresh();
      toast.success(t('files.manage.published', 'New version published'));
    },
    onError: reason => toast.error(reason instanceof Error ? reason.message : String(reason)),
  });
  const removeKnowledgeBase = useMutation({
    mutationFn: () => deleteKb(kbId),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ['knowledge-bases'] });
      toast.success(t('knowledge.deleted', 'Knowledge base deleted'));
      navigate('/knowledge', { replace: true });
    },
    onError: (reason) => toast.error(reason instanceof Error ? reason.message : String(reason)),
  });
  const editMetadata = useMutation({
    mutationFn: () => updateKb(kbId, {
      expected_hash: editHash,
      name: editName.trim(),
      description: editDescription.trim(),
    }),
    onSuccess: async (result) => {
      client.setQueryData(['knowledge-draft', kbId], result);
      await draft.refetch();
      setEditOpen(false);
      toast.success(t('files.manage.draftSaved', 'Draft saved'));
    },
    onError: (reason) => toast.error(reason instanceof Error ? reason.message : String(reason)),
  });

  if (detail.isLoading) {
    return <AsyncState kind="loading" className="m-6" title={t('knowledge.loadingDetail', 'Loading knowledge base…')} />;
  }
  if (detail.isError || !detail.data) {
    const kind = errorState(detail.error);
    return (
      <AsyncState
        kind={kind}
        className="m-6"
        title={kind === 'permission'
          ? t('knowledge.forbiddenDetail', 'You do not have access to this knowledge base')
          : t('knowledge.detailFailed', 'Could not load this knowledge base')}
        description={kind === 'permission'
          ? t('knowledge.forbiddenDetailHint', 'Ask the knowledge owner for access.')
          : t('knowledge.detailFailedHint', 'Check the connection and try loading this knowledge base again.')}
        technicalDetails={detail.error instanceof Error ? detail.error.message : undefined}
        technicalDetailsLabel={t('common.technicalDetails', 'Technical details')}
        actionLabel={kind === 'error' ? t('retry', 'Retry') : undefined}
        onAction={kind === 'error' ? () => void detail.refetch() : undefined}
      />
    );
  }

  const canShare = detail.data.access.capabilities.includes('manage_access');
  const canDelete = detail.data.access.capabilities.includes('delete');

  return (
    <>
      <EntityDetailShell
        resourceKind="knowledge"
        backTo="/knowledge"
        backLabel={t('knowledge.back', 'Knowledge')}
        title={shown.data?.name ?? detail.data.name}
        description={(shown.data?.description ?? (selectedVersion === 'latest' ? detail.data.description : '')) || t('knowledge.noDescription', 'No description')}
        icon={BookOpen}
        metadata={(
          <>
            <span>{shown.data?.files.length ?? detail.data.file_count} {t('knowledge.files', 'files')}</span>
            {editing && <span>{t('files.manage.draft', 'Draft')}</span>}
            <ResourceAccessBadge access={detail.data.access} /><ResourceProvenanceLine provenance={detail.data.provenance} />
          </>
        )}
        actions={(
          <>
            <Select value={selectedVersion} disabled={editing || publish.isPending} onValueChange={setSelectedVersion}>
              <SelectTrigger className="h-9 w-44" aria-label={t('skills.custom.select_version', 'Select version')}><SelectValue /></SelectTrigger>
              <SelectContent><SelectItem value="latest">{t('skills.custom.latest_version', {version: detail.data.package_version, defaultValue: 'Latest · v{{version}}'})}</SelectItem>
                {(versions.data ?? []).filter(v => v.version !== detail.data.package_version).map(v => <SelectItem key={v.version} value={String(v.version)}>v{v.version}</SelectItem>)}
              </SelectContent>
            </Select>
            <Button variant="outline" size="sm" disabled={!canUpdate || selectedVersion !== 'latest' || !draft.data || publish.isPending} onClick={() => {setEditing(!editing); setTab('files');}}>{editing ? t('files.manage.finishEditing', 'Finish editing') : t('edit', 'Edit')}</Button>
            {editing && <Button size="sm" disabled={!draft.data?.has_changes || publish.isPending} onClick={() => {setPublishHash(draft.data!.content_hash); setPublishOpen(true);}}>{t('skills.custom.new_version', 'New version')}</Button>}
            {(!canUpdate || selectedVersion !== 'latest') && <span className="text-xs text-muted-foreground">{t('resourceAccess.editUnavailable')}</span>}
            <Button variant="outline" size="sm" onClick={() => void refresh()}>
              <RefreshCw className="h-4 w-4" />{t('refresh', 'Refresh')}
            </Button>
            {canShare ? (
              <Button variant="outline" size="sm" onClick={() => setShareOpen(true)}>
                <Share2 className="h-4 w-4" />{t('share.share', 'Share')}
              </Button>
            ) : null}
            {canDelete ? (
              <Button
                variant="outline"
                size="sm"
                className="text-destructive hover:text-destructive"
                onClick={() => setDeleteKbOpen(true)}
              >
                <Trash2 className="h-4 w-4" />{t('knowledge.delete', 'Delete')}
              </Button>
            ) : null}
          </>
        )}
      >
        <Tabs value={tab} onValueChange={setTab} className="flex min-h-0 flex-1 flex-col">
          <TabsList variant="underline" className="w-full justify-start">
            <TabsTrigger value="files">{t('knowledge.sourceFiles', 'Files')}</TabsTrigger>
            <TabsTrigger value="overview">{t('skills.detail.tab.overview', 'Overview')}</TabsTrigger>
          </TabsList>
          <TabsContent value="overview">
            <SectionBlock title={t('skills.detail.packageDetails', 'Package details')} actions={<>            {canUpdate && editing ? (
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  setEditHash(draft.data!.content_hash);
                  setEditName(draft.data?.name ?? detail.data.name);
                  setEditDescription(draft.data?.description ?? detail.data.description ?? '');
                  setEditOpen(true);
                }}
              >
                <Pencil className="h-4 w-4" />{t('knowledge.editTitle', 'Edit Knowledge details')}
              </Button>
            ) : null}
</>}>
              <DetailSummary items={[
                {label: 'Knowledge ID', value: <span className="flex items-center gap-2"><code className="break-all text-xs">{kbId}</code><CopyButton value={kbId} /></span>, wide:true},
                {label:t('skills.detail.created', 'Created'), value:formatTime(detail.data.created_at)},
                {label:t('skills.detail.updated', 'Updated'), value:formatTime(detail.data.updated_at)},
                {label:t('knowledge.indexedFiles', 'Indexed files'), value:files.data?.filter(file => file.status === 'indexed').length ?? '—'},
                {label:t('knowledge.pendingFiles', 'Pending indexing'), value:files.data?.filter(file => ['pending','indexing'].includes(file.status)).length ?? '—'},
                {label:t('files.manage.indexErrors','Indexing failures'), value:files.data?.filter(file => file.status === 'failed').length ?? '—'},
              ]} />
              {files.data?.filter(file => file.status === 'failed').map(file => <p key={file.id} className="mt-3 break-words text-xs text-destructive">{file.name}: {file.error_message}</p>)}
              <p className="mt-4 text-xs text-muted-foreground">{t('files.manage.historyStart')}</p>
            </SectionBlock>
          </TabsContent>
          <TabsContent value="files" className="min-h-0 flex-1">
            {editing && <p className="mb-3 text-xs text-muted-foreground">{t('files.manage.knowledgeSaved')}</p>}
            {shown.isLoading && <AsyncState kind="loading" title={t('knowledge.loadingFiles', 'Loading files…')} />}
            {shown.isError && <AsyncState kind="error" title={t('knowledge.filesFailed', 'Could not load files')} actionLabel={t('retry', 'Retry')} onAction={() => void shown.refetch()} />}
            {shown.data && <KnowledgeSourceExplorer
              kbId={kbId} files={shownFiles} canUpdate={canUpdate && editing}
              revisionKey={shown.data.content_hash} loadFile={loadFile}
              operations={canUpdate && editing ? {loadFile, saveFile:(path,file,create) => applyFile(path,file,create), deleteFile:path => applyFile(path)} : undefined}
              uploading={upload.isPending} deleting={removeFile.isPending}
              onUpload={items => upload.mutate(items)}
              onDeleteFiles={(targetFiles, folderPath) => setDeleteTarget({files:targetFiles,label:folderPath,folder:true})}
              onDelete={file => setDeleteTarget({files:[file],label:file.name,folder:false})}
            />}
          </TabsContent>
        </Tabs>
        <ConfirmationDialog open={publishOpen && editing} onOpenChange={setPublishOpen} confirmVariant="default"
          title={t('files.manage.publishTitle', {version:detail.data.package_version+1})}
          description={t('files.manage.publishKnowledgeHint')}
          confirmLabel={t('files.manage.publish', 'Publish')} cancelLabel={t('cancel', 'Cancel')}
          pending={publish.isPending} onConfirm={() => publish.mutate()} />

        <ConfirmationDialog
          open={deleteKbOpen && canDelete}
          onOpenChange={(open) => {
            if (!removeKnowledgeBase.isPending) setDeleteKbOpen(open);
          }}
          title={t('knowledge.deleteTitle', 'Delete this knowledge base?')}
          description={t(
            'knowledge.deleteDescription',
            '{{name}} and all files in this knowledge folder will be permanently deleted.',
            { name: detail.data.name },
          )}
          confirmLabel={removeKnowledgeBase.isPending ? t('deleting', 'Deleting…') : t('delete', 'Delete')}
          cancelLabel={t('cancel', 'Cancel')}
          pending={removeKnowledgeBase.isPending}
          onConfirm={() => removeKnowledgeBase.mutate()}
        />

        <Dialog open={editOpen && canUpdate} onOpenChange={setEditOpen}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>{t('knowledge.editTitle', 'Edit Knowledge details')}</DialogTitle>
              <DialogDescription>{t('knowledge.editDraftHint', 'Saved to the README draft. Publish a new version to apply these changes.')}</DialogDescription>
            </DialogHeader>
            <div className="space-y-4">
              <div className="space-y-1.5">
                <Label htmlFor="knowledge-edit-name">{t('name', 'Name')}</Label>
                <Input id="knowledge-edit-name" value={editName} onChange={(event) => setEditName(event.target.value)} />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="knowledge-edit-description">{t('description', 'Description')}</Label>
                <Textarea id="knowledge-edit-description" value={editDescription} onChange={(event) => setEditDescription(event.target.value)} />
              </div>
            </div>
            <DialogFooter>
              <Button variant="outline" onClick={() => setEditOpen(false)}>{t('cancel', 'Cancel')}</Button>
              <Button disabled={!editName.trim() || editMetadata.isPending} onClick={() => editMetadata.mutate()}>
                {editMetadata.isPending ? t('saving', 'Saving…') : t('save', 'Save')}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>

        <ConfirmationDialog
          open={deleteTarget !== null && canUpdate}
          onOpenChange={(open) => {
            if (!open && !removeFile.isPending) setDeleteTarget(null);
          }}
          title={deleteTarget?.folder ? t('knowledge.deleteFolderTitle', 'Delete folder?') : t('knowledge.deleteFileTitle', 'Delete file?')}
          description={deleteTarget?.folder
            ? t('files.manage.deleteDraftFolder', '{{name}} and its {{count}} files will be removed from the draft. Published versions are unchanged.', { name: deleteTarget.label, count: deleteTarget.files.length })
            : t('files.manage.deleteDraftFile', '{{name}} will be removed from the draft. Published versions are unchanged.', { name: deleteTarget?.label ?? '' })}
          confirmLabel={removeFile.isPending ? t('deleting', 'Deleting…') : t('delete', 'Delete')}
          cancelLabel={t('cancel', 'Cancel')}
          pending={removeFile.isPending}
          onConfirm={() => {
            if (deleteTarget) removeFile.mutate(deleteTarget);
          }}
        />
      </EntityDetailShell>

      <ResourceShareDialog
        open={shareOpen && canShare}
        onOpenChange={setShareOpen}
        resourceKind="knowledge_base"
        resourceId={detail.data.id}
        resourceName={detail.data.name}
        effectiveRole={detail.data.access.effective_role}
        accessSource={detail.data.access.source}
      />
    </>
  );
}
