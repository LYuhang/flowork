import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { DebugMessageContent } from '../DebugMessageContent';

vi.mock('react-i18next', async (importOriginal) => ({
  ...(await importOriginal<typeof import('react-i18next')>()), useTranslation: () => ({ t: (_key: string, fallback: string) => fallback }) }));

describe('Database message expansion', () => {
  it('updates content without freezing its original prop', () => {
    const { rerender } = render(<DebugMessageContent content="partial" />);
    rerender(<DebugMessageContent content="partial then completed" />);
    expect(screen.getByText('partial then completed')).toBeInTheDocument();
  });
  it('folds long content independently without fetching sidecar files', () => {
    const full = 'A'.repeat(1200) + 'EXPANDED-MARKER' + 'Z'.repeat(10000);
    const { container } = render(<div>
      <DebugMessageContent content={full} />
      <DebugMessageContent content="Next round remains visible" />
    </div>);
    expect(container.textContent).not.toContain('EXPANDED-MARKER');
    fireEvent.click(screen.getByRole('button', { name: 'Show more' }));
    expect(container.textContent).toContain('EXPANDED-MARKER');
    expect(screen.getByText('Next round remains visible')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Collapse' }));
    expect(container.textContent).not.toContain('EXPANDED-MARKER');
  });
});
