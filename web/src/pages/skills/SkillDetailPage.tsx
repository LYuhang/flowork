import { ResourceAccessBadge } from '@/components/resources/ResourceAccessBadge';
import { useCallback, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate, useParams, useSearchParams } from 'react-router';
import { toast } from 'sonner';
import { BookOpenText, ExternalLink, GitCommitHorizontal, Pencil, Share2, Trash2 } from 'lucide-react';
import { ResourceShareDialog } from '@/components/modals/ResourceShareDialog';
import { CopyButton } from '@/components/ui/copy-button';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useSetSkillInstalled, useDeleteSkill, usePublishSkillVersion, useSkill, useSkillDraft, useSkillVersion, useSkillVersions } from '@/lib/api/queries/skills';
import { getSkillFile, getSkillVersionFile, getSkillDraftFile, writeSkillDraftFile } from '@/lib/api/skills';
import { SkillFileBrowser } from '@/pages/skills/SkillFileBrowser';
import { StatusBadge } from '@/components/ui/status';
import { useFormatDateTime } from '@/lib/timezone';
import { EntityDetailShell } from '@/components/layout/entity-detail-shell';
import { ResourceProvenanceLine } from '@/components/resources/ResourceProvenanceLine';
import { DetailSummary } from '@/components/layout/detail-summary';
import { SectionBlock } from '@/components/layout/section-block';
import { ActionableError } from '@/components/presentation/ActionableError';

export function SkillDetailPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const formatTime = useFormatDateTime();
  const [params, setParams] = useSearchParams();
  const { id } = useParams();
  const query = useSkill(id);
  const revision = params.get('revision') ?? undefined;
  const versions = useSkillVersions(id);
  const historical = useSkillVersion(id, revision);
  const canEdit = query.data?.source === 'custom' && query.data.access.capabilities.includes('update');
  const draft = useSkillDraft(id, !!canEdit);
  const remove = useDeleteSkill();
  const installation = useSetSkillInstalled();
  const publish = usePublishSkillVersion();
  const editing = !!canEdit && !revision && params.get('edit') === '1';
  const [shareOpen, setShareOpen] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [versionDialog, setVersionDialog] = useState(false);
  const [nextVersion, setNextVersion] = useState('');
  const [publishHash, setPublishHash] = useState('');
  // Old Instructions/Requirements links retain their content in Files/Overview.
  const requestedTab = params.get('tab');
  const tab = requestedTab === 'overview' || requestedTab === 'requirements' ? 'overview' : 'files';
  const loadFile = useCallback((path: string) => editing ? getSkillDraftFile(id!, path)
    : revision ? getSkillVersionFile(id!, revision, path) : getSkillFile(id!, path), [editing, id, revision]);
  const selectFile = useCallback((path: string) => {
    const next = new URLSearchParams(params); next.set('tab', 'files'); next.set('file', path); setParams(next, {replace:true});
  }, [params, setParams]);
  const failure = query.isError ? query : revision && historical.isError ? historical : null;
  if (query.isLoading || (revision && historical.isLoading)) return <div className="empty-state">{t('skills.loading', 'Loading…')}</div>;
  if (failure || !query.data || (revision && !historical.data)) return <ActionableError
    title={t('skills.not_found', 'This Skill no longer exists.')}
    actionLabel={t('retry', 'Retry')} onAction={() => void (failure ?? query).refetch()} />;
  const skill = query.data;
  const viewed = historical.data ?? skill;
  const token = draft.data?.draft_hash ?? draft.data?.base_revision_hash ?? '';
  const files = editing && draft.data ? draft.data.files : viewed.files;
  const changeFile = async (path: string, file?: File, create = false) => {
    await writeSkillDraftFile(skill.id, path, token, file, create);
    await Promise.all([draft.refetch(), query.refetch()]);
  };
  const setEditing = (value: boolean) => {
    const next = new URLSearchParams(params); next.set('tab', 'files');
    if (value) {next.set('edit', '1'); next.delete('revision');} else next.delete('edit');
    setParams(next, {replace:true});
  };
  const publishVersion = async () => {
    const version = Number(nextVersion);
    if (!Number.isSafeInteger(version) || version <= skill.version) {
      toast.error(t('skills.custom.version_invalid', 'Version must be a whole number greater than the published version.')); return;
    }
    try {
      await publish.mutateAsync({id:skill.id,version,expectedHash:publishHash}); setVersionDialog(false); setEditing(false);
      toast.success(t('skills.custom.version_created', 'New Skill version created'));
    } catch (reason) { toast.error(reason instanceof Error ? reason.message : String(reason)); }
  };
  const sourceName = skill.source === 'custom' ? t('skills.source.custom', 'Custom') : skill.source || '—';
  return <EntityDetailShell resourceKind="skill" backTo="/skills" backLabel={t('skills.back', 'Skills')}
    title={viewed.name} description={viewed.description} icon={BookOpenText}
    status={editing ? <StatusBadge status="warning">{t('files.manage.draft', 'Draft')}</StatusBadge> : revision ? <StatusBadge status="neutral">{t('skills.custom.historical', 'Historical version')}</StatusBadge> : undefined}
    metadata={<><span>{sourceName}</span><span>{t('skills.files_count', {count:files.length,defaultValue:'{{count}} Files'})}</span><ResourceAccessBadge access={skill.access} /><ResourceProvenanceLine provenance={viewed.provenance} /></>}
    actions={<>
      <Select value={revision ?? 'latest'} disabled={editing || publish.isPending} onValueChange={value => {
        const next = new URLSearchParams(params); if (value === 'latest') next.delete('revision'); else next.set('revision', value);
        next.delete('edit'); next.delete('file'); setParams(next, {replace:true});
      }}>
        <SelectTrigger className="h-9 w-44" aria-label={t('skills.custom.select_version', 'Select version')}><SelectValue /></SelectTrigger>
        <SelectContent><SelectItem value="latest">{t('skills.custom.latest_version', {version:skill.version,defaultValue:'Latest · v{{version}}'})}</SelectItem>
          {(versions.data ?? []).filter(v => !v.is_latest).map(v => <SelectItem key={v.revision_id} value={v.revision_id}>v{v.version}</SelectItem>)}
        </SelectContent>
      </Select>
      {skill.source_url && <Button variant="outline" size="sm" asChild><a href={skill.source_url} target="_blank" rel="noreferrer"><ExternalLink />{t('skills.catalog.source', 'Source')}</a></Button>}
      {skill.source === 'custom' && <>
        <Button variant="outline" size="sm" disabled={!canEdit || !!revision || !draft.data || publish.isPending} onClick={() => setEditing(!editing)}><Pencil />{editing ? t('files.manage.finishEditing', 'Finish editing') : t('skills.edit', 'Edit')}</Button>
        {editing && <Button size="sm" disabled={!draft.data?.has_changes || publish.isPending} onClick={() => {setNextVersion(String(skill.version+1)); setPublishHash(token); setVersionDialog(true);}}><GitCommitHorizontal />{t('skills.custom.new_version', 'New version')}</Button>}
      </>}
      {skill.source === 'custom' && (!canEdit || !!revision) && <span className="text-xs text-muted-foreground">{t('resourceAccess.editUnavailable')}</span>}
      {skill.source === 'custom' && skill.access.capabilities.includes('manage_access') && <Button variant="outline" size="sm" onClick={() => setShareOpen(true)}><Share2 />{t('share.share', 'Share')}</Button>}
      {skill.access.capabilities.includes('use') && <Button variant="outline" size="sm" disabled={installation.isPending} onClick={async () => {
        try { await installation.mutateAsync({ id: skill.id, installed: !skill.installed }); }
        catch (reason) { toast.error(reason instanceof Error ? reason.message : String(reason)); }
      }}>{skill.installed ? t('skills.uninstall', 'Uninstall') : t('skills.install', 'Install')}</Button>}
      {skill.access.capabilities.includes('delete') && <Button variant="outline" size="sm" className="text-destructive" onClick={() => setConfirmDelete(true)}><Trash2 />{t('skills.deleteResource', 'Delete Skill')}</Button>}
    </>}
  >
    <Tabs value={tab} onValueChange={value => {const next = new URLSearchParams(params); next.set('tab',value); setParams(next,{replace:true});}} className="flex min-h-0 flex-1 flex-col gap-4">
      <TabsList variant="underline" className="w-full justify-start"><TabsTrigger value="files">{t('skills.detail.tab.files', 'Files')}</TabsTrigger><TabsTrigger value="overview">{t('skills.detail.tab.overview', 'Overview')}</TabsTrigger></TabsList>
      <TabsContent value="files" className="mt-0 min-h-0 flex-1">
        {editing && <p className="mb-3 text-xs text-muted-foreground">{t('files.manage.skillSaved')}</p>}
        <SkillFileBrowser persistKey={`${skill.id}:${editing ? token : viewed.revision_hash ?? revision ?? skill.version}`}
          files={files} skillMd={editing && draft.data ? draft.data.skill_md : viewed.skill_md} loadFile={loadFile}
          selectedPath={params.get('file') ?? undefined} onSelectedPathChange={selectFile}
          operations={editing && draft.data ? {loadFile, saveFile:(path,file,create) => changeFile(path,file,create), deleteFile:path => changeFile(path)} : undefined}
          labels={{files:t('skills.detail.files.bundle','Package Files'),loading:t('skills.loading','Loading…'),failed:t('skills.detail.files.failed','Could Not Load File'),binary:t('files.preview.unavailable','Preview unavailable')}} />
      </TabsContent>
      <TabsContent value="overview" className="mt-0">
        <SectionBlock title={t('skills.detail.packageDetails', 'Package details')}>
          <DetailSummary items={[
            {label:t('skills.detail.id','Skill ID'),value:<span className="flex items-center gap-2"><code className="break-all text-xs">{skill.id}</code><CopyButton value={skill.id} /></span>,wide:true},
            {label:t('skills.detail.created','Installed'),value:formatTime(skill.created_at)},
            {label:t('skills.detail.updated','Updated'),value:formatTime(revision ? historical.data?.created_at : skill.updated_at)},
            {label:t('skills.tools_title','Allowed tools'),value:viewed.allowed_tools.length ? viewed.allowed_tools.join(', ') : t('skills.no_tools_short','None declared'),wide:true},
          ]} />
        </SectionBlock>
      </TabsContent>
    </Tabs>
    <ResourceShareDialog open={shareOpen && skill.access.capabilities.includes('manage_access')} onOpenChange={setShareOpen} resourceKind="skill_installation"
      resourceId={skill.id} resourceName={skill.name} effectiveRole={skill.access.effective_role} accessSource={skill.access.source} />
    <Dialog open={versionDialog && editing} onOpenChange={setVersionDialog}><DialogContent><DialogHeader><DialogTitle>{t('skills.custom.new_version','New version')}</DialogTitle><DialogDescription>{t('files.manage.publishSkillHint')}</DialogDescription></DialogHeader>
      <Input type="number" aria-label={t('skills.detail.version','Version')} min={skill.version+1} step={1} value={nextVersion} onChange={e => setNextVersion(e.target.value)} />
      <DialogFooter><Button variant="outline" disabled={publish.isPending} onClick={() => setVersionDialog(false)}>{t('cancel','Cancel')}</Button><Button disabled={publish.isPending} onClick={() => void publishVersion()}>{t('files.manage.publish','Publish')}</Button></DialogFooter>
    </DialogContent></Dialog>
    <Dialog open={confirmDelete && skill.access.capabilities.includes('delete')} onOpenChange={setConfirmDelete}><DialogContent><DialogHeader><DialogTitle>{t('skills.deleteResourceTitle','Delete this Skill?')}</DialogTitle><DialogDescription>{t('skills.deleteResourceHint','This deletes the resource for all recipients. To remove only your installation, use Uninstall.')}</DialogDescription></DialogHeader><DialogFooter>
      <Button variant="outline" disabled={remove.isPending} onClick={() => setConfirmDelete(false)}>{t('cancel','Cancel')}</Button><Button variant="destructive" disabled={remove.isPending} onClick={async () => {try {await remove.mutateAsync(skill.id); navigate('/skills');} catch(reason) {toast.error(reason instanceof Error ? reason.message : String(reason));}}}>{t('skills.deleteResource','Delete Skill')}</Button>
    </DialogFooter></DialogContent></Dialog>
  </EntityDetailShell>;
}
