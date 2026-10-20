/** TableReadNode config: explicit formats override automatic extension detection. */
import { useTranslation } from 'react-i18next';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { CommitOnBlurInput, CommitOnBlurNumber } from '@/pages/canvas/inspector/CommitOnBlur';
import type { NodeConfigEditorProps } from './types';
import { deriveTableFormat, FALLBACK_FORMAT, MANUAL_FORMATS } from './table-format';

const MODES = ['batch', 'stream'];

export function TableReadNodeEditor({
  config,
  readOnly,
  onChange,
}: NodeConfigEditorProps) {
  const { t } = useTranslation();
  const filePath =
    typeof config.file_path === 'string' ? (config.file_path as string) : '';
  const storedFormat =
    typeof config.file_format === 'string'
      ? (config.file_format as string)
      : FALLBACK_FORMAT;
  const mode =
    typeof config.mode === 'string' ? (config.mode as string) : 'batch';
  const sheetName =
    typeof config.sheet_name === 'string' ? (config.sheet_name as string) : '';
  const offset =
    typeof config.offset === 'number' ? (config.offset as number) : 0;
  const limit =
    typeof config.limit === 'number' ? (config.limit as number) : 0;

  // Only automatic format selection follows the file extension.
  const derived = deriveTableFormat(filePath);
  const effectiveFormat = storedFormat === 'auto' ? derived : storedFormat;
  const isExcel = effectiveFormat === 'excel';

  // Editing one field preserves other authored settings.
  function commit(patch: Record<string, unknown>) {
    const next: Record<string, unknown> = { ...config, ...patch };
    onChange(next);
  }

  // offset / limit are optional (engine default 0 = skip none / read all).
  // Only persist the key when it has a positive value, otherwise drop it so
  // the saved node_config stays minimal.
  function commitNum(key: 'offset' | 'limit', next: number) {
    const value = Number.isFinite(next) && next > 0 ? Math.floor(next) : 0;
    const patch: Record<string, unknown> = { ...config };
    if (value > 0) {
      patch[key] = value;
    } else {
      delete patch[key];
    }
    onChange(patch);
  }

  const modeHint =
    mode === 'stream'
      ? t(
          'inspector.config.table.modeStreamHint',
          'stream: reserved for future lazy iteration — not yet implemented; running it raises an error.',
        )
      : t(
          'inspector.config.table.modeBatchHint',
          'batch: read every row into memory and return them as a list (use offset / limit for large files).',
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
            'inspector.config.table.filePathPlaceholderRead',
            '/run/{{date}}/input.csv',
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

      <div className="space-y-1">
        <Label className="text-xs">
          {t('inspector.config.table.modeLabel', 'Read mode')}
        </Label>
        <Select
          value={mode}
          onValueChange={(next) => commit({ mode: next })}
          disabled={readOnly}
        >
          <SelectTrigger className="h-8 text-xs" data-testid="cfg-table-mode-select">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {MODES.map((m) => (
              <SelectItem
                key={m}
                value={m}
                disabled={m === 'stream'}
                className="text-xs"
              >
                {m === 'stream' ? `${m} (coming soon)` : m}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <p className="text-xs text-muted-foreground" data-testid="cfg-table-mode-hint">
          {modeHint}
        </p>
      </div>

      <div className="grid grid-cols-2 gap-2">
        <div className="space-y-1">
          <Label className="text-xs">
            {t('inspector.config.table.offsetLabel', 'offset')}
          </Label>
          <CommitOnBlurNumber
            kind="int"
            min={0}
            step={1}
            value={offset}
            onCommit={(next) => commitNum('offset', next)}
            disabled={readOnly}
            className="h-8 text-xs"
            data-testid="cfg-table-offset"
          />
          <p className="text-xs leading-tight text-muted-foreground">
            {t('inspector.config.table.offsetHint', 'Skip the first N rows (0 = none).')}
          </p>
        </div>
        <div className="space-y-1">
          <Label className="text-xs">
            {t('inspector.config.table.limitLabel', 'limit')}
          </Label>
          <CommitOnBlurNumber
            kind="int"
            min={0}
            step={1}
            value={limit}
            onCommit={(next) => commitNum('limit', next)}
            disabled={readOnly}
            className="h-8 text-xs"
            data-testid="cfg-table-limit"
          />
          <p className="text-xs leading-tight text-muted-foreground">
            {t('inspector.config.table.limitHint', 'Max rows to read (0 = all).')}
          </p>
        </div>
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
    </div>
  );
}
