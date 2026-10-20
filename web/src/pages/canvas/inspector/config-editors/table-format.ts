/**
 * Shared table-format helpers for the Table Read/Write config editors.
 *
 * Mirrors the engine's extension-to-format map for local structured files.
 * Explicit formats take precedence; only `auto` uses the path suffix.
 */

/** Engine `file_format` enum (table_read.py / table_write.py CONFIG_SCHEMA). */
export const TABLE_FORMATS = ['auto', 'csv', 'jsonl', 'excel'] as const;
export type TableFormat = (typeof TABLE_FORMATS)[number];

/** Default when nothing is stored and the suffix is unknown. */
export const FALLBACK_FORMAT: TableFormat = 'auto';

/** Choices offered for explicit format selection, regardless of path suffix. */
export const MANUAL_FORMATS: TableFormat[] = ['auto', 'csv', 'jsonl', 'excel'];

/** Extension → concrete format (matches engine `_EXT_FORMAT`). */
const EXT_FORMAT: Record<string, Exclude<TableFormat, 'auto'>> = {
  '.csv': 'csv',
  '.jsonl': 'jsonl',
  '.ndjson': 'jsonl',
  '.json': 'jsonl',
  '.xlsx': 'excel',
  '.xls': 'excel',
};

/**
 * Derive a concrete format from a file path's extension, or `null` when the
 * suffix is unknown / the path is empty (caller then falls back to manual).
 * Tolerant of `{{var}}` placeholders embedded in the path — it only inspects
 * the trailing extension.
 */
export function deriveTableFormat(
  filePath: string,
): Exclude<TableFormat, 'auto'> | null {
  if (!filePath) return null;
  const lower = filePath.toLowerCase();
  const dot = lower.lastIndexOf('.');
  if (dot < 0) return null;
  const ext = lower.slice(dot);
  return EXT_FORMAT[ext] ?? null;
}
