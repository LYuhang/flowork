import { parseWorkflowFocus } from '@/lib/preview/workflow-reference';
import { PreviewOriginProvider } from '@/lib/preview/PreviewOriginProvider';
import { previewOriginFromSearch, originChatHref } from '@/lib/preview/context-origin';
import { lazy, Suspense, useEffect, useMemo } from 'react';
import { ArrowLeft, FileText, Network, X } from 'lucide-react';
import { useNavigate, useSearchParams } from 'react-router';
import { useTranslation } from 'react-i18next';

import { AsyncState } from '@/components/ui/async-state';
import { Button } from '@/components/ui/button';
import { previewReturnPath, standalonePreviewTarget, standaloneWorkflowPreviewTarget } from '@/lib/preview/standalone-preview';
import { ChatFilePreview } from '@/pages/chat/preview/ChatFilePreview';

const WorkflowPreviewRenderer = lazy(() => import('@/pages/chat/preview/WorkflowPreviewRenderer').then((m) => ({ default: m.WorkflowPreviewRenderer })));

function fileName(path: string): string {
  return path.split('/').filter(Boolean).at(-1) || path;
}

export function StandalonePreviewPage() {
  const { t } = useTranslation();
  const [search] = useSearchParams();
  const navigate = useNavigate();
  const origin = useMemo(() => previewOriginFromSearch(search), [search]);
  const returnTo = previewReturnPath(search.get('returnTo'));
  const leavePreview = () => {
    if (returnTo) navigate(returnTo, { replace: true });
    else if (window.history.state?.idx > 0) navigate(-1);
    else if (origin) window.location.assign(originChatHref(origin.chatId));
    else navigate('/', { replace: true });
  };
  const target = useMemo(() => standalonePreviewTarget(search), [search]);
  const workflow = useMemo(() => standaloneWorkflowPreviewTarget(search), [search]);
  const requestedPage = Number(search.get('page'));
  const initialPage = Number.isSafeInteger(requestedPage) && requestedPage > 0 ? requestedPage : undefined;
  const name = workflow ? `${workflow.workflowId} · ${workflow.version}` : target ? fileName(target.fileRef.path) : '';

  useEffect(() => {
    if (!name) return;
    const previous = document.title;
    document.title = `${name} · ${t('preview.standalone.title', 'Preview')}`;
    return () => {
      document.title = previous;
    };
  }, [name, t]);

  if (!target && !workflow) {
    return (
      <main className="grid min-h-dvh place-items-center bg-surface-app p-6">
        <AsyncState
          kind="error"
          title={t('preview.standalone.invalidTitle', 'Unable to open Preview')}
          description={t(
            'preview.standalone.invalidDescription',
            'This Preview link is incomplete or invalid. Open the file again from its conversation.',
          )}
          className="w-full max-w-lg"
        />
      </main>
    );
  }

  return (
    <PreviewOriginProvider origin={origin}>
    <main className="flex h-dvh min-h-0 flex-col bg-surface-work" data-page="standalone-preview">
      <header className="flex h-12 shrink-0 items-center gap-3 border-b border-edge-structural bg-surface-raised px-3 sm:px-4">
        <Button variant="ghost" size="icon" aria-label={t('preview.standalone.back', 'Back')} onClick={leavePreview}>
          <ArrowLeft className="h-4 w-4" />
        </Button>
        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-accent-subtle text-accent-strong">
          {workflow ? <Network className="h-4 w-4" aria-hidden="true" /> : <FileText className="h-4 w-4" aria-hidden="true" />}
        </span>
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-sm font-semibold text-content-primary">{name}</h1>
          <p className="text-xs text-muted-foreground">
            {t('preview.standalone.title', 'Preview')}
          </p>
        </div>
        {origin ? <Button asChild variant="outline" size="sm"><a href={originChatHref(origin.chatId)}>
          {t('preview.reference.returnChat', 'Return to conversation')}
        </a></Button> : null}
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label={t('preview.standalone.close', 'Close Preview')}
          title={t('preview.standalone.close', 'Close Preview')}
          onClick={leavePreview}
        >
          <X className="h-4 w-4" />
        </Button>
      </header>
      <section className="min-h-0 flex-1" aria-label={t('preview.standalone.title', 'Preview')}>
        {workflow ? (
          <Suspense fallback={<AsyncState kind="loading" title={t('chat.preview.loadingWorkflow', 'Loading workflow...')} />}>
            <WorkflowPreviewRenderer key={`${workflow.workflowId}:${workflow.version}`} {...workflow} focus={parseWorkflowFocus(search.get('focus'))} allowOpenInNewPage={false} inspectorPlacement="right" />
          </Suspense>
        ) : target ? <ChatFilePreview
          fileRef={target.fileRef}
          fileType={target.fileType}
          expectedRevision={search.get('expectedRevision')}
          initialPage={initialPage}
          allowOpenInNewPage={false}
        /> : null}
      </section>
    </main>
    </PreviewOriginProvider>
  );
}
