import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { renderVfsContent } from '@/pages/canvas/explorer/renderers';
import type { VfsReadOut } from '@/lib/api/vfs';

const base: VfsReadOut = {
  path: '/data/x_1.jsonl', content_type: 'table/jsonl', content: '',
  size_bytes: 0, truncated: false, wf_version: null, stale: false,
};

describe('renderVfsContent', () => {
  it('renders table/jsonl as a <table> with rows', () => {
    render(renderVfsContent({ ...base, content: '{"a":1,"b":2}\n{"a":3,"b":4}' }));
    expect(screen.getByRole('table')).toBeInTheDocument();
    expect(screen.getByText('3')).toBeInTheDocument();
  });

  it('renders application/json pretty (no error)', () => {
    render(renderVfsContent({ ...base, content_type: 'application/json', content: '{"k":1}' }));
    expect(screen.getByText(/"k": 1/)).toBeInTheDocument();
  });

  it('renders text/html as ESCAPED source, never injected', () => {
    const { container } = render(renderVfsContent({
      ...base, content_type: 'text/html', content: '<script>alert(1)</script>',
    }));
    expect(container.querySelector('script')).toBeNull();
    expect(screen.getByText(/<script>alert\(1\)<\/script>/)).toBeInTheDocument();
  });

  it('falls back for unknown content types (never throws)', () => {
    render(renderVfsContent({ ...base, content_type: 'application/x-weird', content: 'blob' }));
    expect(screen.getByText(/no formatted view/i)).toBeInTheDocument();
  });

  it('normalizes content_type case', () => {
    render(renderVfsContent({ ...base, content_type: 'TABLE/JSONL', content: '{"a":1}' }));
    expect(screen.getByRole('table')).toBeInTheDocument();
  });

  it.each(['image/png', 'application/octet-stream', 'table/jsonl', 'text/x-uri'])('handles absent inline content for %s', (content_type) => {
    render(renderVfsContent({ ...base, path: '/mount/file', content_type, content: null, size_bytes: 101935 }));
    expect(screen.getByText('This file has no text preview.')).toBeInTheDocument();
  });

  it('handles omitted content and keeps empty text files valid', () => {
    const { rerender } = render(renderVfsContent({ ...base, content: undefined }));
    expect(screen.getByText('This file has no text preview.')).toBeInTheDocument();
    rerender(renderVfsContent({ ...base, path: '/mount/empty.txt', content_type: 'text/plain', content: '' }));
    expect(screen.queryByText('This file has no text preview.')).toBeNull();
  });
});
