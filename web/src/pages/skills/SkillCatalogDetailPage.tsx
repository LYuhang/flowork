import { useCallback, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router';
import { ArrowLeft, BookOpenText, ExternalLink, ShieldCheck } from 'lucide-react';

import { Markdown } from '@/components/agent-sidebar/Markdown';
import { EntityDetailShell } from '@/components/layout/entity-detail-shell';
import { DetailSummary } from '@/components/layout/detail-summary';
import { SectionBlock } from '@/components/layout/section-block';
import { Button } from '@/components/ui/button';
import { StatusBadge } from '@/components/ui/status';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useSkillCatalogItem, useSkills } from '@/lib/api/queries/skills';
import { getCatalogSkillFile } from '@/lib/api/skills';
import type { SkillCatalogSource } from '@/lib/api/skills';
import { SkillCatalogInstallDialog } from '@/pages/skills/SkillCatalogInstallDialog';
import { SkillFileBrowser } from '@/pages/skills/SkillFileBrowser';
import { ActionableError } from '@/components/presentation/ActionableError';

export function SkillCatalogDetailPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { source: rawSource } = useParams();
  const [params, setParams] = useSearchParams();
  const source = rawSource === 'openai' || rawSource === 'anthropic' ? rawSource : undefined;
  const sourceId = params.get('id') ?? undefined;
  const query = useSkillCatalogItem(source, sourceId);
  const installedQuery = useSkills();
  const [installOpen, setInstallOpen] = useState(false);
  const loadFile = useCallback(
    (path: string) => getCatalogSkillFile(source as SkillCatalogSource, sourceId as string, path),
    [source, sourceId],
  );
  const selectFileInUrl = useCallback((path: string) => {
    const next = new URLSearchParams(params);
    next.set('tab', 'files');
    next.set('file', path);
    setParams(next, { replace: true });
  }, [params, setParams]);

  const back = <Link to={`/skills?tab=discover${source ? `&source=${source}` : ''}`} className="inline-flex items-center gap-1 text-sm text-primary hover:underline"><ArrowLeft className="h-4 w-4" />{t('skills.back', 'Back')}</Link>;
  if (!source || !sourceId) return <div className="page-shell page-shell-contained"><div className="page-content max-w-6xl">{back}<div className="empty-state">{t('skills.catalog.invalid', 'Invalid Skill Catalog Link.')}</div></div></div>;
  if (query.isLoading) return <div className="page-shell page-shell-contained"><div className="page-content max-w-6xl">{back}<div className="empty-state">{t('skills.loading', 'Loading…')}</div></div></div>;
  if (query.isError || !query.data) return <div className="page-shell page-shell-contained"><div className="page-content max-w-6xl">{back}<ActionableError className="mt-4" title={t('skills.load_error', 'Failed To Load Skill.')} description={t('skills.catalog.load_error_hint', 'Check the catalog connection, then load this skill again.')} actionLabel={t('retry', 'Retry')} onAction={() => void query.refetch()} technicalDetails={query.error instanceof Error ? query.error.message : undefined} technicalDetailsLabel={t('common.technicalDetails', 'Technical details')} /></div></div>;

  const skill = query.data;
  const installed = installedQuery.data?.find((row) => row.source === skill.source && row.source_id === skill.source_id);
  const files = skill.files.map((file) => file.path);
  const requestedTab = params.get('tab');
  const activeTab = ['overview', 'instructions', 'files', 'requirements'].includes(requestedTab ?? '') ? requestedTab! : 'overview';
  const selectedFile = params.get('file') ?? undefined;
  return (
    <>
      <EntityDetailShell
        resourceKind="skill"
        backTo={`/skills?tab=discover${source ? `&source=${source}` : ''}`}
        backLabel={t('skills.back', 'Back')}
        title={skill.name}
        description={skill.source_label}
        icon={BookOpenText}
        status={<StatusBadge status="success">{t('skills.verified', 'Verified Source')}</StatusBadge>}
        metadata={
          <>
            <span>v{skill.version}</span>
            <span>{t('skills.files_count', { count: files.length, defaultValue: '{{count}} Files' })}</span>
          </>
        }
        actions={
          <>
            <Button variant="outline" size="sm" asChild><a href={skill.homepage} target="_blank" rel="noreferrer"><ExternalLink />{t('skills.catalog.source', 'Source')}</a></Button>
            {installed ? <Button size="sm" variant="outline" onClick={() => navigate(`/skills/${installed.id}`)}>{t('skills.installed_button', 'Installed')}</Button> : <Button size="sm" onClick={() => setInstallOpen(true)}>{t('skills.catalog.install', 'Install')}</Button>}
          </>
        }
      >

        <Tabs value={activeTab} onValueChange={(tab) => { const next = new URLSearchParams(params); next.set('tab', tab); setParams(next, { replace: true }); }} className="flex min-h-0 flex-1 flex-col gap-4">
          <TabsList variant="underline" className="chat-scrollbar h-auto w-full shrink-0 justify-start overflow-x-auto">
            <TabsTrigger value="overview">{t('skills.detail.tab.overview', 'Overview')}</TabsTrigger>
            <TabsTrigger value="instructions">{t('skills.detail.tab.instructions', 'Instructions')}</TabsTrigger>
            <TabsTrigger value="files">{t('skills.detail.tab.files', 'Files')}</TabsTrigger>
            <TabsTrigger value="requirements">{t('skills.detail.tab.requirements', 'Requirements')}</TabsTrigger>
          </TabsList>
          <TabsContent value="overview" className="page-scroll-region mt-0 min-h-0 w-full flex-1 pr-2">
            <SectionBlock title={t('skills.detail.about', 'About this Skill')}>
              <p className="text-sm leading-6 text-muted-foreground">{skill.description}</p>
            </SectionBlock>
            <SectionBlock className="mt-4" title={t('skills.detail.packageDetails', 'Package details')}>
              <DetailSummary items={[
                { label: t('skills.detail.source', 'Source'), value: skill.source_label },
                { label: t('skills.detail.version', 'Version'), value: `v${skill.version}` },
                { label: t('skills.detail.package_id', 'Package ID'), value: <code className="text-xs">{skill.source_id}</code> },
                { label: t('skills.detail.revision', 'Revision'), value: <code className="text-xs">{skill.revision.slice(0, 12)}</code> },
              ]} />
            </SectionBlock>
          </TabsContent>
          <TabsContent value="instructions" className="page-scroll-region mt-0 min-h-0 w-full flex-1 pr-2"><SectionBlock title="SKILL.md"><Markdown className="min-w-0 text-sm leading-6">{skill.body ?? ''}</Markdown></SectionBlock></TabsContent>
          <TabsContent value="files" className="mt-0 min-h-0 flex-1 overflow-hidden"><SkillFileBrowser persistKey={`${skill.source}:${skill.source_id}:${skill.revision}`} files={files} skillMd={skill.skill_md ?? ''} loadFile={loadFile} selectedPath={selectedFile} onSelectedPathChange={selectFileInUrl} labels={{ files: t('skills.detail.files.bundle', 'Package Files'), loading: t('skills.detail.files.loading', 'Loading File…'), failed: t('skills.detail.files.failed', 'Could Not Load File'), binary: t('skills.detail.files.binary', 'Binary File Preview Is Not Available.') }} /></TabsContent>
          <TabsContent value="requirements" className="page-scroll-region mt-0 min-h-0 w-full flex-1 space-y-5 pr-2">
            <SectionBlock
              title={t('skills.detail.validation.title', 'SKILL.md validated')}
              description={t('skills.detail.validation.catalog', 'The catalog package contains valid name, description, and instruction metadata.')}
              icon={<ShieldCheck className="size-4 text-state-success" aria-hidden="true" />}
            >
              <h3 className="text-xs font-medium uppercase tracking-[0.06em] text-content-tertiary">{t('skills.tools_title', 'Allowed tools')}</h3>
              <ToolList tools={skill.allowed_tools} empty={t('skills.no_tools', 'No tool requirements declared.')} />
            </SectionBlock>
          </TabsContent>
        </Tabs>
      </EntityDetailShell>
      <SkillCatalogInstallDialog open={installOpen} onOpenChange={setInstallOpen} skill={skill} onInstalled={(row) => navigate(`/skills/${row.id}`)} />
    </>
  );
}

function ToolList({ tools, empty }: { tools: string[]; empty: string }) { return tools.length ? <div className="mt-3 flex flex-wrap gap-2">{tools.map((tool) => <span key={tool} className="rounded bg-secondary px-2 py-1 font-mono text-xs text-secondary-foreground">{tool}</span>)}</div> : <p className="mt-2 text-sm text-muted-foreground">{empty}</p>; }
