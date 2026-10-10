import { applicationGridTheme } from '@/lib/presentation/grid-theme';
import { PreviewToolbar } from './PreviewToolbar';
import { useEffect, useMemo, useState } from 'react';
import {
  AllCommunityModule,
  ModuleRegistry,
  type ColDef,
} from 'ag-grid-community';
import { AgGridReact } from 'ag-grid-react';
import { useTranslation } from 'react-i18next';

import {
  fileRefKey,
  type PreviewDescriptorV1,
  type PreviewErrorInfo,
} from '@/lib/preview/protocol';
import {
  parsePreviewTable,
  PreviewTableError,
} from '@/lib/preview/table-parser';
import { PreviewErrorState } from './PreviewErrorState';
import type { PreviewRendererProps } from './renderer-types';

ModuleRegistry.registerModules([AllCommunityModule]);

type GridRow = Record<string, string>;

interface TableData {
  columns: string[];
  rows: GridRow[];
}

type TableLoadState =
  | { key: string; status: 'loading' }
  | { key: string; status: 'ready'; table: TableData }
  | { key: string; status: 'error'; error: PreviewErrorInfo };

function rowsToGrid(columns: string[], rows: string[][]): GridRow[] {
  return rows.map((row) => Object.fromEntries(
    columns.map((_name, index) => [`c${index}`, row[index] ?? '']),
  ));
}

async function loadStructuredTable(
  source: {
    detectedType: PreviewDescriptorV1['detectedType'];
    inlineText?: string;
    url?: string;
  },
  signal: AbortSignal,
): Promise<TableData> {
  let content = source.inlineText;
  if (typeof content !== 'string') {
    const url = source.url;
    if (!url) throw new PreviewTableError('content_unavailable');
    const response = await fetch(url, { signal });
    if (!response.ok) throw new PreviewTableError('content_unavailable');
    content = await response.text();
  }
  const parsed = parsePreviewTable(content, source.detectedType);
  return {
    columns: parsed.columns,
    rows: rowsToGrid(parsed.columns, parsed.rows),
  };
}

export function StructuredTablePreview({ descriptor, loadAllowed }: PreviewRendererProps) {
  const { t } = useTranslation();
  const loadKey = `${fileRefKey(descriptor.fileRef)}:${descriptor.revision}`;
  const loadSource = useMemo(() => ({
    detectedType: descriptor.detectedType,
    inlineText: descriptor.content?.inlineText ?? undefined,
    url: descriptor.content?.url ?? undefined,
  }), [descriptor.content?.inlineText, descriptor.content?.url, descriptor.detectedType]);
  const [loadState, setLoadState] = useState<TableLoadState>({
    key: loadKey,
    status: 'loading',
  });
  const currentLoadState: TableLoadState = loadState.key === loadKey
    ? loadState
    : { key: loadKey, status: 'loading' };

  useEffect(() => {
    if (!loadAllowed) return;
    const controller = new AbortController();
    void loadStructuredTable(loadSource, controller.signal).then((table) => {
      setLoadState({ key: loadKey, status: 'ready', table });
    }).catch((reason) => {
      if (!controller.signal.aborted) {
        setLoadState({
          key: loadKey,
          status: 'error',
          error: reason instanceof PreviewTableError
            ? reason.details
            : { code: 'render_failed', params: {} },
        });
      }
    });
    return () => controller.abort();
  }, [loadAllowed, loadKey, loadSource]);

  const table = currentLoadState.status === 'ready' ? currentLoadState.table : null;
  const columnDefs = useMemo<ColDef<GridRow>[]>(() => (table?.columns ?? []).map(
    (name, index) => ({
      field: `c${index}`,
      headerName: name,
      sortable: true,
      filter: true,
      resizable: true,
      minWidth: 110,
    }),
  ), [table?.columns]);

  if (!loadAllowed) return null;
  if (currentLoadState.status === 'loading') {
    return (
      <div className="p-4 text-sm text-muted-foreground">
        {t('preview.table.loading', 'Loading table…')}
      </div>
    );
  }
  if (currentLoadState.status === 'error') {
    return <PreviewErrorState descriptor={descriptor} error={currentLoadState.error} />;
  }
  if (!table) {
    return (
      <PreviewErrorState
        descriptor={descriptor}
        error={{ code: 'invalid_file', params: {} }}
      />
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PreviewToolbar>
        <span className="text-xs text-muted-foreground">
          {t('preview.table.readOnly', 'Read only')}
        </span>
      </PreviewToolbar>
      <div className="min-h-0 flex-1">
        <AgGridReact<GridRow>
          theme={applicationGridTheme}
          rowData={table.rows}
          columnDefs={columnDefs}
          defaultColDef={{ flex: 1 }}
          animateRows={false}
        />
      </div>
    </div>
  );
}
