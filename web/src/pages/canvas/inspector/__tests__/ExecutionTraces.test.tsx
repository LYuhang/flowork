import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { ExecutionTraces } from '../ExecutionTraces';

describe('ExecutionTraces', () => {
  it('mounts message bodies only after opening both levels', () => {
    const { container } = render(<ExecutionTraces messages={[{ role: 'tool', content: 'actual tool result' }]} />);
    expect(screen.queryByText(/actual tool result/)).toBeNull();
    const outer = container.querySelector('details')!;
    outer.open = true;
    fireEvent(outer, new Event('toggle'));
    expect(screen.getByText('1. tool')).toBeTruthy();
    expect(screen.queryByText(/actual tool result/)).toBeNull();
    const inner = outer.querySelector('details')!;
    inner.open = true;
    fireEvent(inner, new Event('toggle'));
    expect(screen.getByText(/actual tool result/)).toBeTruthy();
  });
});
