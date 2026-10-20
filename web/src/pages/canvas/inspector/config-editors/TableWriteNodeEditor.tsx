/** TableWriteNode config: explicit formats override automatic extension detection. */
import { useTranslation } from 'react-i18next';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { CommitOnBlurInput } from '@/pages/canvas/inspector/CommitOnBlur';
import type { NodeConfigEditorProps } from './types';
import { deriveTableFormat, FALLBACK_FORMAT, MANUAL_FORMATS } from './table-format';

const WRITE_MODES = ['overwrite', 'append'];

/** Sentinel for "no explicit data field — auto-detect" (Radix Select forbids ''). */
const DATA_AUTO = '__auto__';

export function TableWriteNodeEditor({
  config,
  readOnly,
  onChange,
  inputFields = {},
}: NodeConfigEditorProps) {
  const { t } = useTranslation();
  // The data source for the write: only object/list input fields can be written
  // as a table (an object → one row; a list → one row per item).
  const dataWrite =
    typeof config.data_write === 'string' ? (config.data_write as string) : '';
  const dataCandidates = Object.entries(inputFields)
    .filter(([, f]) => f?.type === 'object' || f?.type === 'array')
    .map(([name]) => name);
  const filePath =
    typeof config.file_path === 'string' ? (config.file_path as string) : '';
  const storedFormat =
    typeof config.file_format === 'string'
      ? (config.file_format as string)
      : FALLBACK_FORMAT;
  const sheetName =
    typeof config.sheet_name === 'string' ? (config.sheet_name as string) : '';
  const writeMode =
    typeof config.write_mode === 'string'
      ? (config.write_mode as string)
      : 'overwrite';

  const derived = deriveTableFormat(filePath);
  const effectiveFormat = storedFormat === 'auto' ? derived : storedFormat;
  const isExcel = effectiveFormat === 'excel';

  function commit(patch: Record<string, unknown>) {
    const next: Record<string, unknown> = { ...config, ...patch };
    onChange(next);
  }

  const writeModeHint =
    writeMode === 'append'
      ? t(
          'inspector.config.table.writeModeAppendHint',
          'append: add rows to the end of an existing file.',
        )
      : t(
          'inspector.config.table.writeModeOverwriteHint',
          'overwrite: create or replace the file.',
        );

  return (
    <div className="space-y-3">
      <div className="space-y-1">
        <Label className="text-xs">
          {t('inspector.config.table.filePathLabel', 'file_path')}{' '}
          <span className="text-muted-foreground font-normal">
            {t('inspector.config.table.filePathHint', '(supports {{}} variables)')}
          </span>
        </Label>
        <CommitOnBlurInput
          value={filePath}
          onCommit={(next) => commit({ file_path: next })}
          disabled={readOnly}
          placeholder={t(
            'inspector.config.table.filePathPlaceholderWrite',
            '/run/{{date}}/output.csv',
          )}
          className="h-8 text-xs"
          data-testid="cfg-table-file-path"
        />
      </div>

      <div className="space-y-1">
        <Label className="text-xs">
          {t('inspector.config.table.formatLabel', 'Format')}
        </Label>
        <Select
          value={storedFormat}
          onValueChange={(next) => commit({ file_format: next })}
          disabled={readOnly}
        >
          <SelectTrigger className="h-8 text-xs" data-testid="cfg-table-format-select">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {MANUAL_FORMATS.map((f) => (
              <SelectItem key={f} value={f} className="text-xs">
                {f}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        {storedFormat === 'auto' && derived && (
          <p className="text-xs text-muted-foreground" data-testid="cfg-table-format-derived">{derived}</p>
        )}
        <p className="text-xs text-muted-foreground">
            {t(
              'inspector.config.table.formatAuto',
              'Auto detects the file extension. An explicit format takes precedence.',
            )}
        </p>
      </div>

      {isExcel || sheetName ? (
        <div className="space-y-1">
          <Label className="text-xs">
            {t('inspector.config.table.sheetLabel', 'sheet_name')}{' '}
            <span className="text-muted-foreground font-normal" data-testid="cfg-table-sheet-hint">
              {t('inspector.config.table.filePathHint', '(supports {{}} variables)')}
            </span>
          </Label>
          <CommitOnBlurInput
            value={sheetName}
            onCommit={(next) => commit({ sheet_name: next })}
            disabled={readOnly}
            placeholder="data_{{date}}"
            className="h-8 text-xs"
            data-testid="cfg-table-sheet-name"
          />
        </div>
      ) : null}

      <div className="space-y-1">
        <Label className="text-xs">
          data_write{' '}
          <span className="text-muted-foreground font-normal">
            {t(
              'inspector.config.table.dataWriteOnlyHint',
              '(object / list data only)',
            )}
          </span>
        </Label>
        <Select
          value={dataWrite || DATA_AUTO}
          onValueChange={(next) =>
            commit({ data_write: next === DATA_AUTO ? '' : next })
          }
          disabled={readOnly}
        >
          <SelectTrigger className="h-8 text-xs" data-testid="cfg-table-data-write-select">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={DATA_AUTO} className="text-xs">
              {t('inspector.config.table.dataWriteAuto', '(auto-detect)')}
            </SelectItem>
            {/* Keep a stale/renamed selection visible so it isn't silently lost. */}
            {dataWrite && !dataCandidates.includes(dataWrite) && (
              <SelectItem value={dataWrite} className="text-xs">
                {dataWrite} (?)
              </SelectItem>
            )}
            {dataCandidates.map((n) => (
              <SelectItem key={n} value={n} className="text-xs">
                {n}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <p className="text-xs text-muted-foreground" data-testid="cfg-table-data-write-hint">
          {t(
            'inspector.config.table.dataWriteHint',
            'object → writes one row (table format); list → writes one row per item, using the first item as the table schema.',
          )}
        </p>
      </div>

      <div className="space-y-1">
        <Label className="text-xs">
          {t('inspector.config.table.writeModeLabel', 'write_mode')}
        </Label>
        <Select
          value={writeMode}
          onValueChange={(next) => commit({ write_mode: next })}
          disabled={readOnly}
        >
          <SelectTrigger className="h-8 text-xs" data-testid="cfg-table-write-mode-select">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {WRITE_MODES.map((m) => (
              <SelectItem key={m} value={m} className="text-xs">
                {m}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <p className="text-xs text-muted-foreground" data-testid="cfg-table-write-mode-hint">
          {writeModeHint}
        </p>
      </div>
    </div>
  );
}
