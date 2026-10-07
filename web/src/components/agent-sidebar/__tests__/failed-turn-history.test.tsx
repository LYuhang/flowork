import { render, screen } from '@testing-library/react';
import { expect, it } from 'vitest';
import { MessageItem } from '../MessageItem';
import { mergeChunks } from '../types';

it('shows a durable failure on its user message and updates the memoized bubble', () => {
  const input = { id: 'request-1', role: 'user', content: 'Make a report' };
  const initial = mergeChunks([input])[0];
  const view = render(<MessageItem message={initial} />);
  expect(screen.queryByRole('status')).not.toBeInTheDocument();
  const failed = mergeChunks([{ ...input, meta: { turn_error: {
    code: 'authorization_unavailable', message: 'authorization_unavailable',
  } } }])[0];
  view.rerender(<MessageItem message={failed} />);
  expect(screen.getByRole('status')).toHaveTextContent('This request failed.');
  expect(screen.getByRole('status')).toHaveTextContent('Authorization is temporarily unavailable');
  expect(screen.getAllByText('Make a report')).toHaveLength(1);
  view.unmount();
  render(<MessageItem message={failed} />);
  expect(screen.getByRole('status')).toHaveTextContent('This request failed.');
});
