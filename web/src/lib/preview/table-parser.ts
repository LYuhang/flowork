import type { PreviewErrorInfo } from './protocol';

export const PREVIEW_TABLE_MAX_ROWS = 50_000;
export const PREVIEW_TABLE_MAX_COLUMNS = 200;
export const PREVIEW_WORKBOOK_MAX_SHEETS = 20;

export interface ParsedPreviewTable {
  columns: string[];
  rows: string[][];
  /** One-based source positions of retained CSV/TSV records, with the header first. */
  recordRows?: number[];
}

export class PreviewTableError extends Error {
  readonly details: PreviewErrorInfo;

  constructor(code: string, params: PreviewErrorInfo['params'] = {}) {
    super(code);
    this.name = 'PreviewTableError';
    this.details = { code, params };
  }
}

function checkColumns(actual: number): void {
  if (actual > PREVIEW_TABLE_MAX_COLUMNS) {
    throw new PreviewTableError('too_many_columns', {
      actual,
      limit: PREVIEW_TABLE_MAX_COLUMNS,
    });
  }
}

function checkRows(actual: number): void {
  if (actual > PREVIEW_TABLE_MAX_ROWS) {
    throw new PreviewTableError('too_many_rows', {
      actual,
      limit: PREVIEW_TABLE_MAX_ROWS,
    });
  }
}

function parseDelimited(data: string, delimiter: ',' | '\t'): ParsedPreviewTable {
  const records: string[][] = [];
  const recordRows: number[] = [];
  let recordNumber = 0;
  let row: string[] = [];
  let cell = '';
  let quoted = false;

  const finishRow = () => {
    recordNumber += 1;
    row.push(cell);
    cell = '';
    if (row.length > 1 || row[0] !== '') {
      checkColumns(row.length);
      records.push(row);
      recordRows.push(recordNumber);
      if (records.length > 1) checkRows(records.length - 1);
    }
    row = [];
  };

  for (let index = 0; index < data.length; index += 1) {
    const character = data[index];
    if (quoted) {
      if (character === '"') {
        if (data[index + 1] === '"') {
          cell += '"';
          index += 1;
        } else {
          quoted = false;
        }
      } else {
        cell += character;
      }
      continue;
    }
    if (character === '"' && cell.length === 0) {
      quoted = true;
    } else if (character === delimiter) {
      row.push(cell);
      cell = '';
      checkColumns(row.length);
    } else if (character === '\n' || character === '\r') {
      if (character === '\r' && data[index + 1] === '\n') index += 1;
      finishRow();
    } else {
      cell += character;
    }
  }

  if (quoted) throw new PreviewTableError('invalid_file');
  if (cell.length > 0 || row.length > 0) finishRow();
  if (records.length === 0) return { columns: ['value'], rows: [] };

  const columns = records[0].map((value, index) => value || `Column ${index + 1}`);
  checkColumns(columns.length);
  return {
    columns,
    recordRows,
    rows: records.slice(1).map((record) =>
      columns.map((_column, index) => record[index] ?? ''),
    ),
  };
}

function parseJsonLines(data: string): ParsedPreviewTable {
  const records: Record<string, unknown>[] = [];
  const columns: string[] = [];
  const seen = new Set<string>();

  for (const raw of data.split(/\r?\n/)) {
    if (!raw.trim()) continue;
    let value: unknown;
    try {
      value = JSON.parse(raw);
    } catch {
      throw new PreviewTableError('invalid_file');
    }
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
      throw new PreviewTableError('invalid_file');
    }
    const record = value as Record<string, unknown>;
    for (const key of Object.keys(record)) {
      if (!seen.has(key)) {
        seen.add(key);
        columns.push(key);
        checkColumns(columns.length);
      }
    }
    records.push(record);
    checkRows(records.length);
  }

  const resolvedColumns = columns.length ? columns : ['value'];
  return {
    columns: resolvedColumns,
    rows: records.map((record) =>
      resolvedColumns.map((key) => {
        const value = record[key];
        if (value === null || value === undefined) return '';
        if (typeof value === 'string') return value;
        if (typeof value === 'object') return JSON.stringify(value);
        return String(value);
      }),
    ),
  };
}

export function parsePreviewTable(
  data: string,
  detectedType: string,
): ParsedPreviewTable {
  if (detectedType === 'jsonl') return parseJsonLines(data);
  return parseDelimited(data, detectedType === 'tsv' ? '\t' : ',');
}
