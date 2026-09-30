import { describe, expect, it } from 'vitest';
import { textSelectionFromOffsets } from './text-reference';
describe('source selection coordinates', () => {
  it('preserves CRLF and indentation, excluding the unselected next line', () => {
    expect(textSelectionFromOffsets('a\r\n  b\r\nc', 3, 8)).toEqual({ text: '  b\r\n', startLine: 2, endLine: 2 });
  });
  it('handles reverse and empty selections', () => {
    expect(textSelectionFromOffsets('abc\ndef', 7, 4)).toEqual({ text: 'def', startLine: 2, endLine: 2 });
    expect(textSelectionFromOffsets('abc', 1, 1)).toBeNull();
  });
});
