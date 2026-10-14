import { describe, expect, it } from 'vitest';

import {
  parsePreviewTable,
  PREVIEW_TABLE_MAX_COLUMNS,
  PREVIEW_TABLE_MAX_ROWS,
  PREVIEW_WORKBOOK_MAX_SHEETS,
  PreviewTableError,
} from '../table-parser';

describe('Preview table parser', () => {
  it('keeps the reviewed fixed browser capacity envelope', () => {
    expect(PREVIEW_TABLE_MAX_ROWS).toBe(50_000);
    expect(PREVIEW_TABLE_MAX_COLUMNS).toBe(200);
    expect(PREVIEW_WORKBOOK_MAX_SHEETS).toBe(20);
  });

  it('parses quoted delimiters and logical rows with embedded newlines', () => {
    const parsed = parsePreviewTable(
      'name,comment\nAlice,"first line\nsecond line"\nBob,"a,b"\n',
      'csv',
    );

    expect(parsed.columns).toEqual(['name', 'comment']);
    expect(parsed.rows).toEqual([
      ['Alice', 'first line\nsecond line'],
      ['Bob', 'a,b'],
    ]);
  });

  it('retains original record positions after skipping blank records', () => {
    const parsed = parsePreviewTable('name,value\nFirst,"line 1\nline 2"\n\nSecond,2', 'csv');
    expect(parsed.recordRows).toEqual([1, 2, 4]);
    expect(parsed.rows[1]).toEqual(['Second', '2']);
  });

  it('rejects a table after the configured row limit', () => {
    const source = [
      'name',
      ...Array.from({ length: PREVIEW_TABLE_MAX_ROWS + 1 }, () => 'value'),
    ].join('\n');

    expect(() => parsePreviewTable(source, 'csv')).toThrowError(
      expect.objectContaining({
        details: {
          code: 'too_many_rows',
          params: {
            actual: PREVIEW_TABLE_MAX_ROWS + 1,
            limit: PREVIEW_TABLE_MAX_ROWS,
          },
        },
      }) as PreviewTableError,
    );
  });

  it('rejects too many columns and malformed quoted content', () => {
    const columns = Array.from(
      { length: PREVIEW_TABLE_MAX_COLUMNS + 1 },
      (_value, index) => `c${index}`,
    ).join(',');
    expect(() => parsePreviewTable(columns, 'csv')).toThrowError(
      expect.objectContaining({
        details: expect.objectContaining({ code: 'too_many_columns' }),
      }) as PreviewTableError,
    );
    expect(() => parsePreviewTable('name,comment\nAlice,"unfinished', 'csv')).toThrowError(
      expect.objectContaining({
        details: { code: 'invalid_file', params: {} },
      }) as PreviewTableError,
    );
  });

  it('requires every JSONL row to be an object', () => {
    expect(() => parsePreviewTable('{"name":"Alice"}\nnot-json\n', 'jsonl')).toThrowError(
      expect.objectContaining({
        details: { code: 'invalid_file', params: {} },
      }) as PreviewTableError,
    );
  });
});
