import { render, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { Markdown } from '../Markdown';
import { normalizeMathDelimiters } from '../markdown-math';

describe('chat math', () => {
  it('normalizes TeX delimiters while preserving code examples', () => {
    const code = String.raw`\(x\)`;
    expect(normalizeMathDelimiters(`Inline ${code} and \`${code}\`\n\n\`\`\`python\n${code}\n\`\`\``))
      .toBe(`Inline $x$ and \`${code}\`\n\n\`\`\`python\n${code}\n\`\`\``);
    expect(normalizeMathDelimiters(String.raw`\[\frac{1}{2}\]`)).toBe('\n$$\n\\frac{1}{2}\n$$\n');
    expect(normalizeMathDelimiters(String.raw`unfinished \(x`)).toBe(String.raw`unfinished \(x`);
  });
  it('renders inline and display formulas without converting code', async () => {
    const {container} = render(<Markdown>{'Inline $x^2$\n\n$$\n\\frac{1}{2}\n$$\n\n`$code$`'}</Markdown>);
    await waitFor(() => expect(container.querySelectorAll('.katex')).toHaveLength(2));
    expect(container.querySelectorAll('.katex-display')).toHaveLength(1);
    expect(container.querySelector('code')?.textContent).toBe('$code$');
    expect(container.querySelector('annotation')?.textContent).toBe('x^2');
  });
  it('handles invalid streamed formulas and does not enable trusted commands', async () => {
    const {container,rerender} = render(<Markdown streaming>{'$\\frac{$'}</Markdown>);
    await waitFor(() => expect(container.querySelector('.katex-error')).toBeTruthy());
    rerender(<Markdown>{String.raw`$\href{javascript:alert(1)}{click}$`}</Markdown>);
    await waitFor(() => expect(container.querySelector('.katex')).toBeTruthy());
    expect(container.querySelector('a')).toBeNull();
  });
});
