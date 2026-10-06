import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useParams } from 'react-router';
import { useTranslation } from 'react-i18next';
import { AllCommunityModule, ModuleRegistry, themeQuartz, type ColDef, type IDatasource, type ICellRendererParams } from 'ag-grid-community';
import { AgGridReact } from 'ag-grid-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { getTask, taskResultRequest, type ResultPage } from '@/lib/api/tasks';

ModuleRegistry.registerModules([AllCommunityModule]);
const fields = ['index', 'status', 'execution_id', 'input', 'mapped_input', 'output', 'error', 'execution_time'];
const display = (value: unknown) => value == null ? '' : typeof value === 'object' ? JSON.stringify(value) : String(value);
export function TaskResultsPage() {
  const { taskId = '' } = useParams();
  const { t, i18n } = useTranslation();
  const task = useQuery({ queryKey: ['task', taskId], queryFn: () => getTask(taskId) });
  const payload = task.data?.payload as { name?: string; workflow_snapshot?: { name?: string } } | undefined;
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState('');
  const [status, setStatus] = useState('');
  const [hidden, setHidden] = useState<string[]>(['mapped_input']);
  const [summary, setSummary] = useState<Omit<ResultPage, 'rows'> | null>(null);
  const [error, setError] = useState('');
  const [detail, setDetail] = useState<Record<string, unknown> | null>(null);
  const columns = useMemo<ColDef<Record<string, unknown>>[]>(() => fields.map(field => ({
    field, headerName: field === 'execution_id' ? 'Trace ID' : t(`evaluation.column.${field}`, field),
    hide: hidden.includes(field), pinned: field === 'index' ? 'left' : undefined,
    width: field === 'index' ? 100 : field === 'execution_id' ? 270 : 200,
    sortable: true, resizable: true, filter: field === 'status' ? false : 'agTextColumnFilter',
    filterParams: { filterOptions: ['contains'], maxNumConditions: 1, debounceMs: 400 },
    valueFormatter: params => display(params.value),
    tooltipValueGetter: params => display(params.value),
    ...(field === 'status' ? { cellRenderer: (params: ICellRendererParams<Record<string, unknown>>) => <span className={`rounded-full px-2 py-1 text-xs ${params.value === 'success' ? 'bg-emerald-50 text-emerald-700' : params.value === 'error' ? 'bg-red-50 text-red-700' : 'bg-muted text-muted-foreground'}`}>{t(`evaluation.${String(params.value)}`, String(params.value ?? ''))}</span> } : {}),
    ...(field === 'execution_id' ? { cellRenderer: (params: ICellRendererParams<Record<string, unknown>>) => typeof params.value === 'string'
      ? <Link className="font-mono text-primary underline underline-offset-2" to={`/workflow-executions/${encodeURIComponent(params.value)}`}>{params.value}</Link> : '—' } : {}),
  })), [hidden, t]);
  const datasource = useMemo<IDatasource>(() => {
    const lifetime = new AbortController();
    return {
      destroy: () => { lifetime.abort(); },
      getRows: params => {
        const filters: Record<string, string> = {};
        for (const [key, model] of Object.entries(params.filterModel ?? {})) {
          const value = model as { filter?: string };
          if (value.filter) filters[key] = value.filter;
        }
        const sort = params.sortModel[0];
        void taskResultRequest<ResultPage>(taskId, 'results/query', 'POST', {
          offset: params.startRow, limit: params.endRow - params.startRow,
          search: query, row_status: status, filters,
          sort: sort?.colId ?? 'index', descending: sort?.sort === 'desc',
        }).then(page => {
          if (lifetime.signal.aborted) return;
          setSummary(page); setError(''); params.successCallback(page.rows, page.filtered);
        }).catch((failure: Error) => { if (!lifetime.signal.aborted) { setError(failure.message); params.failCallback(); } });
      },
    };
  }, [taskId, query, status]);
  return <div className="flex h-full min-h-0 flex-col gap-4 p-4 md:p-6">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div><Link to={`/tasks/${taskId}`} className="text-sm text-muted-foreground hover:underline">← {t('evaluation.back', 'Back to task')}</Link>
        <h1 className="mt-2 text-xl font-semibold">{t('evaluation.results', 'Inference results')}</h1><p className="text-sm text-muted-foreground">{payload?.name || payload?.workflow_snapshot?.name || task.data?.workflow_id}</p><p className="font-mono text-xs text-muted-foreground">{taskId}</p></div>
      <div className="flex items-center gap-2">{task.data?.access.capabilities.includes('export') && <Button variant="outline" asChild><a href={`/api/v1/tasks/${taskId}/download?format=jsonl`}>{t('evaluation.downloadJsonl')}</a></Button>}<Button variant="outline" asChild><Link to={`/tasks/${taskId}?tab=evaluation`}>{t('evaluation.title', 'Evaluation')}</Link></Button></div>
    </div>
    {summary && <div className="flex flex-wrap gap-4 text-sm"><span>{t('evaluation.total', 'Total')}: <strong>{summary.total}</strong></span><span>{t('evaluation.success', 'Success')}: <strong>{summary.counts.success}</strong></span><span>{t('evaluation.errors', 'Errors')}: <strong>{summary.counts.error}</strong></span><span>{t('evaluation.filtered', 'Matching')}: <strong>{summary.filtered}</strong></span>{summary.partial && <span className="text-amber-600">{t('evaluation.partial', 'Partial results')}</span>}</div>}
    <div className="flex flex-wrap items-center gap-3">
      <form className="flex min-w-64 flex-1 gap-2" onSubmit={e => { e.preventDefault(); setQuery(search); }}>
        <Input value={search} onChange={e => setSearch(e.target.value)} placeholder={t('evaluation.searchPlaceholder', 'Search all result fields…')} aria-label={t('evaluation.search', 'Search results')} />
        <Button variant="outline">{t('evaluation.search', 'Search results')}</Button>
      </form>
      <select className="h-9 rounded-md border bg-background px-3 text-sm" value={status} onChange={e => setStatus(e.target.value)} aria-label={t('evaluation.column.status', 'Status')}>
        <option value="">{t('evaluation.allStatuses', 'All statuses')}</option>{['success', 'error', 'cancelled', 'not_started'].map(s => <option key={s} value={s}>{t(`evaluation.${s}`, s)}</option>)}
      </select>
      <details className="relative text-sm"><summary className="cursor-pointer">{t('evaluation.columns', 'Columns')}</summary><div className="absolute right-0 z-20 mt-2 w-48 space-y-2 rounded-md border bg-background p-3 shadow-md">{fields.map(field => <label key={field} className="flex gap-2"><input type="checkbox" checked={!hidden.includes(field)} onChange={e => setHidden(current => e.target.checked ? current.filter(f => f !== field) : [...current, field])} />{field === 'execution_id' ? 'Trace ID' : t(`evaluation.column.${field}`, field)}</label>)}</div></details>
    </div>
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
    <p className="text-xs text-muted-foreground">{t('evaluation.rowHint', 'Double-click a row to inspect its full input and output. Trace ID opens the execution canvas.')}</p>
    <div className="min-h-[420px] flex-1 overflow-hidden rounded-lg border">
      <AgGridReact key={i18n.language} theme={themeQuartz} columnDefs={columns} rowModelType="infinite" datasource={datasource}
        cacheBlockSize={100} maxBlocksInCache={5} rowHeight={44} suppressMultiSort
        onRowDoubleClicked={event => event.data && setDetail(event.data)}
        localeText={i18n.language.startsWith('zh') ? { loadingOoo: '加载中…', noRowsToShow: '暂无结果', filterOoo: '筛选…', contains: '包含', pinColumn: '固定列', pinLeft: '固定到左侧', pinRight: '固定到右侧', noPin: '取消固定' } : undefined} />
    </div>
    {detail && <dialog open className="fixed inset-y-0 right-0 left-auto z-40 m-0 h-full max-h-none w-full max-w-xl overflow-auto border-l bg-background p-6 shadow-xl">
      <div className="mb-4 flex justify-between"><h2 className="font-semibold">{t('evaluation.sample', 'Sample details')}</h2><Button variant="outline" onClick={() => setDetail(null)}>{t('evaluation.close', 'Close')}</Button></div>
      <pre className="whitespace-pre-wrap break-all text-xs">{JSON.stringify(detail, null, 2)}</pre>
    </dialog>}
  </div>;
}
