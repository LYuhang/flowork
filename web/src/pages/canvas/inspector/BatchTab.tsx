/**
 * Inspector workflow Batch tab.
 *
 * Folds the retired `RunBatchModal` into an inline tab AND adds a
 * workflow-scoped task list:
 *
 *   1. SUBMIT — the input-source selector (upload / VFS path),
 *      a minimal in-browser CSV/tabular parse, the column-mapping grid, and
 *      Submit (`submitBatch` → durable background queue). Stable
 *      testids preserved for the e2e (`batch-source-selector`, `batch-submit`,
 *      the per-source/per-mapping ids). The submitted batch is a background
 *      job on the Task Center channel — on success we toast + reset.
 *
 *   2. THIS WORKFLOW'S TASKS — a workflow-scoped lens onto `tasks`
 *      (`useWorkflowTasks` → `listTasks({ workflow_id })`): status + submitted
 *      time per batch run. Clicking a row opens an INLINE progress view that
 *      reuses the Task Center's `useTaskStream` SSE + polled `getTask`. A
 *      "View in Task Center" link hands off to the cross-workflow `/tasks`.
 *      A no-batch-yet empty state when the list is empty.
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';
import { ChevronLeft } from 'lucide-react';

import {
  submitBatch,
  getTask,
  type BatchOutputSpec,
  type SubmitBatchBody,
  type Task,
  type TaskStatus,
} from '@/lib/api/tasks';
import { useWorkflowTasks } from '@/lib/api/queries/tasks';
import { useTaskStream } from '@/lib/api/sse/run-task-stream';
import { readVfs } from '@/lib/api/vfs';
import { parseExcel } from '@/lib/batch/excel';
import { parseTabular } from '@/lib/api/queries/data-files';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { SearchSelect, type SearchSelectOption } from '@/components/ui/search-select';
import { getStartNodeFields } from '@/lib/workflow/start-node';
import { outputFieldCandidates } from '@/lib/workflow/output-fields';
import { loadBatchConfig, saveBatchConfig } from '@/lib/batch/config-store';
import {
  BatchOutputColumns,
} from '@/pages/canvas/inspector/BatchOutputColumns';
import {
  defaultColumnsState,
  toWireColumns,
  type BatchColumnsState,
} from '@/pages/canvas/inspector/batch-output-columns-model';
import { useFormatDateTime } from '@/lib/timezone';
import { useWorkflowEditStore } from '@/stores/workflow-edit';
import { useCommitWorkflow } from '@/lib/api/mutations/workflow-ops';
import { saveBeforeRun } from '@/lib/workflow/save-before-run';
import type { components } from '@/lib/api/schema';
import { StatusBadge, type SemanticStatus } from '@/components/ui/status';
import { parseCsv, type ParsedCsv } from './batch-csv';

/**
 * Minimal CSV parser — first row = header, comma-split, no quoting/escape.
 * Mirrors the engine's permissive `batch_exec` input contract (rows of string
 * cells). Ported verbatim from the retired `RunBatchModal` so the existing
 * parse tests + the column-mapping flow stay byte-identical.
 */
/** Does a path name an Excel workbook? Selects the Excel writer server-side and
 *  reveals the output sheet-name field in the UI. */
function isExcelPath(path: string): boolean {
  return /\.xlsx?$/i.test(path.trim());
}

/** The batch input-source the user is feeding rows from. */
type BatchSource = 'upload' | 'data';

const TERMINAL: TaskStatus[] = ['finished', 'finished_with_errors', 'failed', 'interrupted', 'cancelled'];
const NONE_VALUE = '__none__';

function taskStatusTone(s: TaskStatus): SemanticStatus {
  switch (s) {
    case 'queued':
      return 'neutral';
    case 'running':
    case 'resuming':
      return 'running';
    case 'finished':
      return 'success';
    case 'finished_with_errors':
    case 'interrupted':
    case 'cancelling':
      return 'warning';
    case 'failed':
      return 'danger';
    case 'cancelled':
      return 'neutral';
    case 'enabled':
      return 'success';
    case 'paused':
      return 'neutral';
  }
}

export interface BatchTabProps {
  wfId: string;
  workflow?: Record<string, unknown> | null;
  showTaskList?: boolean;
  active?: boolean;
  onSubmitted?: (taskId: string) => void;
}

export function BatchTab({
  wfId,
  workflow,
  showTaskList = true,
  active = true,
  onSubmitted,
}: BatchTabProps) {
  const { t } = useTranslation();
  // Render UTC timestamps in the user's chosen timezone (reactive).
  const fmtTime = useFormatDateTime();
  const navigate = useNavigate();
  const draft = useWorkflowEditStore((s) => s.draft);
  const dirty = useWorkflowEditStore((s) => s.dirty);
  const workflowForBatch = workflow ?? draft;
  const startNodeFields = getStartNodeFields(workflowForBatch);

  // Like the interactive Run path: the batch task loads the COMMITTED version
  // by wfId, so unsaved canvas edits must be committed first or the batch runs
  // the last-saved workflow. `useCommitWorkflow` toasts on save failure.
  const commit = useCommitWorkflow(wfId);

  // The inline task-progress drill-down: when set, the tab swaps the
  // submit+list view for a single task's live progress.
  const [openTaskId, setOpenTaskId] = useState<string | null>(null);
  // Drives the hidden file input from our own i18n button.
  const fileInputRef = useRef<HTMLInputElement>(null);

  // ── Submit state (folded RunBatchModal) ────────────────────────────────
  const [source, setSource] = useState<BatchSource>('upload');
  const [columns, setColumns] = useState<string[]>([]);
  const [rows, setRows] = useState<Record<string, string>[]>([]);
  // mapping[workflow_field] = csv_column. We flip the direction at submit.
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const [fileName, setFileName] = useState<string>('');
  // Excel input: the loaded workbook bytes + its sheet names + the chosen read
  // sheet. Kept so changing the sheet re-parses without re-uploading. Empty
  // sheetNames means the current upload isn't an Excel file.
  const [excelBuf, setExcelBuf] = useState<ArrayBuffer | null>(null);
  const [inputSheetNames, setInputSheetNames] = useState<string[]>([]);
  const [inputSheet, setInputSheet] = useState<string>('');
  // Optional results output destination (v1: a durable /data VFS path). Blank →
  // results stay only in the task's downloadable copy. The path's extension
  // picks the file format server-side; an Excel path also takes a sheet name.
  // The INFERENCE config (output columns / location / concurrency) is restored
  // from the per-workflow saved config so re-entering the tab reuses the last
  // setup; the DATA SOURCE is not persisted (per-run input). Lazy initializers
  // read localStorage once on mount.
  const saved = loadBatchConfig(wfId);
  const [outputPath, setOutputPath] = useState<string>(() => saved?.outputPath ?? '');
  const [outputSheet, setOutputSheet] = useState<string>(() => saved?.outputSheet ?? '');
  // Rows to run in parallel (thread pool; server clamps to 1..16).
  const [concurrency, setConcurrency] = useState<number>(() => saved?.concurrency ?? 1);
  const [dataPath, setDataPath] = useState<string>('');
  const [dataLoading, setDataLoading] = useState(false);
  const [dataError, setDataError] = useState<string>('');
  // Output-table columns: the table is EXACTLY these columns, in order. Starts
  // with compact fixed metadata columns; the user appends node-output columns.
  const [columnsState, setColumnsState] = useState<BatchColumnsState>(
    () => saved?.columns ?? defaultColumnsState(),
  );

  // Persist the inference config (per workflow) on every change.
  useEffect(() => {
    saveBatchConfig(wfId, { outputPath, outputSheet, concurrency, columns: columnsState });
  }, [wfId, outputPath, outputSheet, concurrency, columnsState]);
  // Every node's output fields — the source dropdown for a field column.
  const outputCandidates = outputFieldCandidates(workflowForBatch);
  const inputSheetOptions = useMemo<SearchSelectOption[]>(
    () => inputSheetNames.map((sheet) => ({ value: sheet, label: sheet, keywords: [sheet] })),
    [inputSheetNames],
  );
  const inputColumnOptions = useMemo<SearchSelectOption[]>(
    () => columns.map((column) => ({ value: column, label: column, keywords: [column] })),
    [columns],
  );

  // RightInspector keeps this form mounted so an uploaded file is not lost
  // when the user checks Run. Pause the 5 s task polling while the hidden tab
  // is inactive; hidden work must not compete with the canvas main thread.
  const tasksQuery = useWorkflowTasks(wfId, active && showTaskList);
  const qc = useQueryClient();

  function clearParsed() {
    setColumns([]);
    setRows([]);
    setMapping({});
  }

  function clearExcel() {
    setExcelBuf(null);
    setInputSheetNames([]);
    setInputSheet('');
  }

  // Post-submit reset clears only the DATA SOURCE (the per-run input). The
  // inference config (output columns / location / concurrency) is deliberately
  // kept so the next run reuses it — see the persisted config above.
  function reset() {
    setSource('upload');
    clearParsed();
    setFileName('');
    clearExcel();
    setDataPath('');
    setDataLoading(false);
    setDataError('');
  }

  /** Shared: apply a parsed table + auto-map same-name columns. */
  function applyParsed(parsed: ParsedCsv) {
    setColumns(parsed.columns);
    setRows(parsed.rows);
    const auto: Record<string, string> = {};
    for (const field of startNodeFields) {
      if (parsed.columns.includes(field.name)) auto[field.name] = field.name;
    }
    setMapping(auto);
  }

  async function onFileChange(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    if (!f) return;
    setFileName(f.name);
    const ext = f.name.toLowerCase().split('.').pop() ?? '';
    // Excel: parse the workbook, keep the bytes so the read-sheet picker can
    // re-parse other sheets, and read the first sheet by default.
    if (ext === 'xlsx' || ext === 'xls') {
      const buf = await f.arrayBuffer();
      setExcelBuf(buf);
      const parsed = await parseExcel(buf);
      setInputSheetNames(parsed.sheetNames);
      setInputSheet(parsed.sheetNames[0] ?? '');
      applyParsed({ columns: parsed.columns, rows: parsed.rows });
      return;
    }
    // Non-Excel → clear any prior Excel state, parse as CSV/TSV/JSONL (browsers
    // leave `f.type` blank for .tsv/.jsonl, so dispatch on the extension).
    setExcelBuf(null);
    setInputSheetNames([]);
    setInputSheet('');
    const text = await f.text();
    const contentType =
      ext === 'tsv' ? 'table/tsv' : ext === 'jsonl' ? 'table/jsonl' : 'table/csv';
    applyParsed(parseTabular(text, contentType, parseCsv));
  }

  /** Re-parse the chosen sheet of the already-loaded Excel workbook. */
  async function onInputSheetChange(sheet: string) {
    setInputSheet(sheet);
    if (!excelBuf) return;
    const parsed = await parseExcel(excelBuf, sheet);
    applyParsed({ columns: parsed.columns, rows: parsed.rows });
  }

  async function onLoadDataPath() {
    const path = dataPath.trim();
    setDataError('');
    if (!path) {
      clearParsed();
      return;
    }
    if (!path.startsWith('/data/') && !path.startsWith('/mount/')) {
      setDataError(
        t('canvas.batch.dataPathInvalid', 'Enter a file path under /data or /mount.'),
      );
      clearParsed();
      return;
    }
    setDataLoading(true);
    try {
      const out = await readVfs({ path, wf_id: wfId });
      if (out.content == null) {
        setDataError(
          t('canvas.batch.dataUnreadable', 'This file has no inline content to parse.'),
        );
        clearParsed();
        return;
      }
      applyParsed(parseTabular(out.content, out.content_type, parseCsv));
    } catch (err) {
      setDataError(err instanceof Error ? err.message : String(err));
      clearParsed();
    } finally {
      setDataLoading(false);
    }
  }

  function switchSource(next: BatchSource) {
    if (next === source) return;
    setSource(next);
    clearParsed();
    setFileName('');
    clearExcel();
    setDataPath('');
    setDataError('');
  }

  const mutation = useMutation({
    mutationFn: (body: SubmitBatchBody) => submitBatch(wfId, body),
    onSuccess: ({ task_id }) => {
      reset();
      // Refresh the in-tab task list so the just-submitted run appears.
      void qc.invalidateQueries({ queryKey: ['tasks', 'workflow', wfId] });
      void qc.invalidateQueries({ queryKey: ['tasks'] });
      if (onSubmitted) {
        onSubmitted(task_id);
        return;
      }
      toast.success(t('canvas.batch.started', 'Batch task started'), {
        description: t(
          'canvas.batch.startedDesc',
          'Running in the Task Center — you can keep editing while it runs.',
        ),
        action: {
          label: t('canvas.batch.viewTask', 'View task'),
          onClick: () => navigate(`/tasks/${task_id}`),
        },
      });
    },
    onError: (err) => {
      toast.error(
        t('canvas.batch.submitFailed', 'Submit failed: {{msg}}', {
          msg: err instanceof Error ? err.message : String(err),
        }),
      );
    },
  });

  async function onSubmit() {
    if (!rows.length) return;
    // Flip mapping direction: UI keys by workflow_field, backend expects
    // {csv_column: workflow_field}.
    const column_mapping: Record<string, string> = {};
    for (const [field, col] of Object.entries(mapping)) {
      if (col) column_mapping[col] = field;
    }
    // Optional output destination — only sent when the user typed a path. An
    // Excel path (.xlsx/.xls) also carries the sheet name to write into.
    const trimmedOutput = outputPath.trim();
    const trimmedSheet = outputSheet.trim();
    const output: BatchOutputSpec | null = trimmedOutput
      ? {
          type: 'vfs_data',
          path: trimmedOutput,
          ...(isExcelPath(trimmedOutput) && trimmedSheet
            ? { sheet_name: trimmedSheet }
            : {}),
        }
      : null;
    // Save-if-dirty FIRST so the batch runs the user's current canvas, not the
    // last-saved version. A clean draft skips the save; a rejected save (its
    // own toast already fired) aborts before the batch is submitted.
    await saveBeforeRun({
      dirty: workflow == null && dirty && draft != null,
      draft: workflowForBatch,
      save: (wf) =>
        commit.mutateAsync(
          wf as components['schemas']['CommitRequest']['workflow'],
        ),
      run: () =>
        mutation.mutateAsync({
          data_source: { rows },
          column_mapping,
          output,
          concurrency,
          output_columns: toWireColumns(columnsState),
        }),
    }).catch(() => {
      // mutation.onError / commit.onError already toasted; nothing more to do.
    });
  }

  // ── Inline task progress drill-down ─────────────────────────────────────
  if (openTaskId) {
    return (
      <TaskProgressInline
        taskId={openTaskId}
        onBack={() => setOpenTaskId(null)}
      />
    );
  }

  const tasks = tasksQuery.data?.items ?? [];

  return (
    <div className="space-y-4" data-testid="batch-tab">
      <section>
        <span className="text-sm font-medium">
          {t('canvas.batch.source', 'Input source')}
        </span>
        <div
          role="tablist"
          className="mt-2 flex gap-4 border-b border-edge-subtle"
          data-testid="batch-source-selector"
        >
          <button
            type="button"
            role="tab"
            aria-selected={source === 'upload'}
            data-testid="batch-source-upload"
            onClick={() => switchSource('upload')}
            className={`min-h-9 flex-1 border-b-2 px-2 py-1.5 text-sm transition-colors duration-feedback ${
              source === 'upload' ? 'border-focus font-medium text-foreground' : 'border-transparent text-muted-foreground hover:text-foreground'
            }`}
          >
            {t('canvas.batch.sourceUpload', 'Upload file')}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={source === 'data'}
            data-testid="batch-source-data"
            onClick={() => switchSource('data')}
            className={`min-h-9 flex-1 border-b-2 px-2 py-1.5 text-sm transition-colors duration-feedback ${
              source === 'data' ? 'border-focus font-medium text-foreground' : 'border-transparent text-muted-foreground hover:text-foreground'
            }`}
          >
            {t('canvas.batch.sourceData', 'From path')}
          </button>
        </div>
      </section>

      {source === 'upload' && (
        // A plain <div>, NOT a <label>: a wrapping <label> forwards clicks from
        // its whole area (the caption + padding) to the file input, so clicking
        // AROUND the native button also popped the file dialog. Only the input's
        // own button should trigger it.
        <section className="border-t border-edge-subtle pt-3">
          <span className="text-ui font-medium">
            {t('canvas.batch.tabularFile', 'Tabular file (CSV / TSV / JSONL / Excel)')}
          </span>
          {/* Hidden native input + our OWN button/label so the text follows the
              app language (the native file input renders browser-locale
              "Choose file / No file chosen", which ignores the i18n toggle). */}
          <input
            ref={fileInputRef}
            type="file"
            accept=".csv,.tsv,.jsonl,.xlsx,.xls,text/csv,text/tab-separated-values,application/jsonl,application/x-jsonlines,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/vnd.ms-excel"
            onChange={onFileChange}
            data-testid="batch-csv-input"
            className="hidden"
          />
          <div className="mt-2 flex items-center gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => fileInputRef.current?.click()}
              data-testid="batch-csv-choose"
            >
              {t('canvas.batch.chooseFile', 'Choose file')}
            </Button>
            <span className="min-w-0 flex-1 truncate text-meta">
              {fileName || t('canvas.batch.noFileChosen', 'No file chosen')}
            </span>
          </div>
          {inputSheetNames.length > 0 && (
            <label className="mt-2 flex flex-col gap-1">
              <span className="text-ui font-medium">
                {t('canvas.batch.inputSheet', 'Sheet to read')}
              </span>
              <SearchSelect
                value={inputSheet}
                options={inputSheetOptions}
                onValueChange={(value) => void onInputSheetChange(value)}
                placeholder={t('canvas.batch.inputSheetPlaceholder', 'Select a sheet')}
                searchPlaceholder={t('canvas.batch.searchSheet', 'Search sheets')}
                emptyText={t('canvas.batch.noSheetMatches', 'No sheets match your search.')}
                triggerClassName="h-8 text-ui"
                triggerTestId="batch-input-sheet"
              />
            </label>
          )}
          {fileName && (
            <span className="mt-2 block text-meta">
              {t('canvas.batch.parsed', 'Parsed {{count}} rows, {{columns}} columns', {
                count: rows.length,
                columns: columns.length,
              })}
            </span>
          )}
        </section>
      )}

      {source === 'data' && (
        <section className="border-t border-edge-subtle pt-3">
          <span className="text-ui font-medium">
            {t('canvas.batch.dataFile', 'Input file path')}
          </span>
          <div className="mt-2 flex gap-2">
            <Input
              type="text"
              value={dataPath}
              onChange={(e) => setDataPath(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') void onLoadDataPath();
              }}
              placeholder="/data/rows.csv or /mount/rows.csv"
              className="min-w-0 flex-1 font-mono text-ui"
              data-testid="batch-data-picker"
            />
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => void onLoadDataPath()}
              disabled={dataLoading}
              data-testid="batch-data-load"
            >
              {dataLoading
                ? t('canvas.batch.loadingData', 'Loading...')
                : t('canvas.batch.loadDataPath', 'Load')}
            </Button>
          </div>
          <span className="text-meta">
            {t(
              'canvas.batch.dataPathHint',
              'CSV / TSV / JSONL text files are supported under /data or /mount.',
            )}
          </span>
          {dataLoading && (
            <span className="text-meta">
              {t('canvas.batch.loadingData', 'Loading…')}
            </span>
          )}
          {dataError && <span className="text-meta text-destructive">{dataError}</span>}
          {!dataLoading && !dataError && dataPath && (
            <span className="text-meta">
              {t('canvas.batch.parsed', 'Parsed {{count}} rows, {{columns}} columns', {
                count: rows.length,
                columns: columns.length,
              })}
            </span>
          )}
        </section>
      )}

      {columns.length > 0 && startNodeFields.length > 0 && (
        <section className="border-t border-edge-subtle pt-3">
          <h3 className="mb-2 text-ui font-medium">
            {t('canvas.batch.mapping', 'Column mapping')}
          </h3>
          <div className="grid grid-cols-1 gap-2">
            {startNodeFields.map((f) => (
              <label key={f.name} className="flex items-center gap-2 text-ui">
                <span className="w-32 truncate font-mono text-xs">{f.name}</span>
                <SearchSelect
                  value={mapping[f.name] || NONE_VALUE}
                  options={[
                    {
                      value: NONE_VALUE,
                      label: t('canvas.batch.noMapping', '— pick a column —'),
                      keywords: ['none'],
                    },
                    ...inputColumnOptions,
                  ]}
                  onValueChange={(value) =>
                    setMapping((m) => ({ ...m, [f.name]: value === NONE_VALUE ? '' : value }))
                  }
                  placeholder={t('canvas.batch.noMapping', '— pick a column —')}
                  searchPlaceholder={t('canvas.batch.searchColumn', 'Search columns')}
                  emptyText={t('canvas.batch.noColumnMatches', 'No columns match your search.')}
                  className="min-w-0 flex-1"
                  triggerClassName="h-8 text-ui"
                  triggerTestId={`mapping-${f.name}`}
                />
              </label>
            ))}
          </div>
        </section>
      )}

      {/* Output columns — the output table is exactly these, in order. Always
          shown (not gated on a parse) so it's configurable while preparing data. */}
      <section className="border-t border-edge-subtle pt-3">
        <BatchOutputColumns
          state={columnsState}
          onChange={setColumnsState}
          candidates={outputCandidates}
        />
      </section>

      {/* Output location is ALWAYS shown in the submit view (not gated on a
          successful parse) so it's discoverable before/while preparing data —
          it's optional, blank = results only in the downloadable copy. */}
      <section className="space-y-1 border-t border-edge-subtle pt-3">
        <span className="text-ui font-medium">
          {t('canvas.batch.outputLocation', 'Output location')}
        </span>
        <Input
          type="text"
          value={outputPath}
          onChange={(e) => setOutputPath(e.target.value)}
          placeholder="/data/results.csv"
          data-testid="batch-output-path"
          className="w-full font-mono text-ui"
        />
        <p className="text-meta">
          {t(
            'canvas.batch.outputHint',
            'Optional — write the results table to a path under /data in the Agent sandbox. The file extension picks the format (.csv / .tsv / .jsonl / .xlsx). Leave blank to keep results only in the task’s downloadable file.',
          )}
        </p>
        {isExcelPath(outputPath) && (
          <div className="mt-2 space-y-1">
            <span className="text-ui font-medium">
              {t('canvas.batch.outputSheet', 'Sheet name')}
            </span>
            <Input
              type="text"
              value={outputSheet}
              onChange={(e) => setOutputSheet(e.target.value)}
              placeholder="Sheet1"
              data-testid="batch-output-sheet"
              className="w-full text-ui"
            />
            <p className="text-meta">
              {t(
                'canvas.batch.outputSheetHint',
                'The sheet to write into the Excel file. Defaults to Sheet1.',
              )}
            </p>
          </div>
        )}
      </section>

      <section className="space-y-1 border-t border-edge-subtle pt-3">
        <span className="text-ui font-medium">
          {t('canvas.batch.concurrency', 'Parallel rows')}
        </span>
        <Input
          type="number"
          min={1}
          max={16}
          value={concurrency}
          onChange={(e) => {
            const n = Math.round(Number(e.target.value));
            setConcurrency(Number.isFinite(n) ? Math.min(16, Math.max(1, n)) : 1);
          }}
          data-testid="batch-concurrency"
          className="w-24 text-ui"
        />
        <p className="text-meta">
          {t(
            'canvas.batch.concurrencyHint',
            'How many rows run at the same time (1–16). Higher is faster but uses more resources.',
          )}
        </p>
        <p className="text-meta">
          {t(
            'canvas.batch.concurrencyOrderNote',
            'Parallel rows may interleave writes to shared files; for ordered results, emit them from the End node’s output fields — the batch aggregates per-row by input-row index, so the downloadable table / output file is always in input-row order regardless of concurrency.',
          )}
        </p>
      </section>

      <Button
        className="w-full"
        onClick={() => void onSubmit()}
        disabled={!rows.length || mutation.isPending || commit.isPending}
        data-testid="batch-submit"
      >
        {t('canvas.batch.runOnRows', 'Run on {{count}} rows', { count: rows.length })}
      </Button>

      {showTaskList && (
        <section className="border-t border-edge-subtle pt-3">
          <div className="mb-2 flex items-center justify-between">
            <h3 className="text-ui font-medium">
              {t('canvas.batch.tasksTitle', 'This workflow’s batch runs')}
            </h3>
            <Link
              to="/tasks"
              className="text-meta text-primary underline-offset-4 hover:underline"
              data-testid="batch-view-task-center"
            >
              {t('canvas.batch.viewInTaskCenter', 'View in Task Center')}
            </Link>
          </div>

          {tasksQuery.isLoading ? (
            <p className="text-meta">
              {t('tasks.loading', 'Loading…')}
            </p>
          ) : tasks.length === 0 ? (
            <p className="text-meta" data-testid="batch-no-tasks">
              {t('canvas.batch.noBatchYet', 'No batch runs yet for this workflow.')}
            </p>
          ) : (
            <ul className="flex flex-col gap-1" data-testid="batch-task-list">
              {tasks.map((task) => (
                <li key={task.id} className="border-b border-edge-subtle last:border-b-0">
                  <button
                    type="button"
                    onClick={() => setOpenTaskId(task.id)}
                    className="interactive-row flex min-h-10 w-full items-center justify-between gap-2 px-2 py-1.5 text-left"
                    data-testid="batch-task-row"
                    data-task-id={task.id}
                  >
                    <span className="font-mono text-xs text-muted-foreground">
                      {task.id.slice(0, 8)}…
                    </span>
                    <span className="flex items-center gap-2">
                      <span className="text-xs text-muted-foreground">
                        {fmtTime(task.submitted_at)}
                      </span>
                      <StatusBadge status={taskStatusTone(task.status)} data-testid="batch-task-status">
                        {t(`tasks.status.${task.status}`, task.status)}
                      </StatusBadge>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}
    </div>
  );
}

/**
 * Inline single-task progress — reuses the Task Center's two channels (polled
 * `getTask` for the canonical status snapshot + `useTaskStream` SSE for the
 * live event log) inside the Inspector, so the user watches a batch without
 * leaving the canvas. A back affordance returns to the list; a deep link hands
 * off to the full Task Center detail page.
 */
function TaskProgressInline({
  taskId,
  onBack,
}: {
  taskId: string;
  onBack: () => void;
}) {
  const { t } = useTranslation();
  const stream = useTaskStream(taskId);
  const taskQuery = useQuery<Task>({
    queryKey: ['task', taskId],
    queryFn: () => getTask(taskId),
    enabled: !!taskId,
    refetchInterval: (q) => {
      const data = q.state.data as Task | undefined;
      if (!data) return 5_000;
      return TERMINAL.includes(data.status) ? false : 5_000;
    },
    refetchOnWindowFocus: false,
  });

  const task = taskQuery.data;
  const pct = task ? Math.round((task.progress ?? 0) * 100) : 0;

  return (
    <div className="space-y-3" data-testid="batch-task-progress" data-task-id={taskId}>
      <div className="flex items-center justify-between">
        <button
          type="button"
          onClick={onBack}
          className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
          data-testid="batch-task-back"
        >
          <ChevronLeft className="h-3 w-3" />
          {t('taskDetail.backToList', 'Back to tasks')}
        </button>
        <Link
          to={`/tasks/${taskId}`}
          className="text-xs text-primary underline-offset-4 hover:underline"
          data-testid="batch-task-open-detail"
        >
          {t('canvas.batch.viewInTaskCenter', 'View in Task Center')}
        </Link>
      </div>

      {taskQuery.isLoading ? (
        <p className="text-xs text-muted-foreground">{t('tasks.loading', 'Loading…')}</p>
      ) : !task ? (
        <p className="text-xs text-destructive">
          {t('taskDetail.loadError', 'Failed to load this task.')}
        </p>
      ) : (
        <>
          <div className="flex items-center justify-between text-sm">
            <StatusBadge status={taskStatusTone(task.status)} data-testid="batch-task-detail-status">
              {t(`tasks.status.${task.status}`, task.status)}
            </StatusBadge>
            <span className="tabular-nums text-xs text-muted-foreground">{pct}%</span>
          </div>
          <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
            <div
              className={`h-full transition-[width] duration-feedback ${
                task.status === 'failed'
                  ? 'bg-state-danger'
                  : task.status === 'finished'
                    ? 'bg-state-success'
                    : 'bg-state-running'
              }`}
              style={{ width: `${pct}%` }}
            />
          </div>

          <div className="flex items-center justify-between text-xs text-muted-foreground">
            <span>{t('taskDetail.events', 'Events')}</span>
            <span>
              {stream.done
                ? t('taskDetail.streamClosed', 'Stream closed')
                : t('taskDetail.streamLive', 'Live')}
            </span>
          </div>
          {stream.events.length === 0 ? (
            <p className="text-xs text-muted-foreground">
              {t('taskDetail.noEvents', 'No events yet.')}
            </p>
          ) : (
            <ol className="max-h-48 overflow-auto rounded border text-xs">
              {stream.events.map((f) => (
                <li
                  key={f.id}
                  className="flex gap-2 border-b px-2 py-1 last:border-b-0"
                >
                  <span className="w-16 shrink-0 font-mono text-muted-foreground">
                    {f.event_type}
                  </span>
                  <span className="flex-1 break-all font-mono">
                    {f.payload == null
                      ? ''
                      : typeof f.payload === 'string'
                        ? f.payload
                        : JSON.stringify(f.payload)}
                  </span>
                </li>
              ))}
            </ol>
          )}
        </>
      )}
    </div>
  );
}
