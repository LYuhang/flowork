import { act, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it } from 'vitest';

import { CommandPalette } from '@/components/command-palette/CommandPalette';
import i18n from '@/lib/i18n';
import { useUIStore } from '@/stores/ui';


function renderOpenPalette() {
  useUIStore.setState({ commandPaletteOpen: true });
  render(
    <MemoryRouter initialEntries={['/chat']}>
      <CommandPalette />
    </MemoryRouter>,
  );
  return within(screen.getByRole('dialog'));
}


afterEach(() => {
  useUIStore.setState({ commandPaletteOpen: false });
});


describe('CommandPalette localization', () => {
  it('names the dialog and restores focus to an external launcher after Escape', async () => {
    await i18n.changeLanguage('en');
    useUIStore.setState({ commandPaletteOpen: false });
    render(<MemoryRouter><button onClick={() => useUIStore.getState().setCommandPaletteOpen(true)}>Open palette</button><CommandPalette /></MemoryRouter>);
    const launcher = screen.getByRole('button', { name: 'Open palette' });
    await userEvent.click(launcher);
    expect(screen.getByRole('dialog', { name: 'Search Flowork' })).toBeVisible();
    await userEvent.keyboard('{Escape}');
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)); });
    expect(launcher).toHaveFocus();
  });

  it('renders the English navigation vocabulary', async () => {
    await i18n.changeLanguage('en');
    const palette = renderOpenPalette();

    expect(palette.getByText('Navigate')).toBeInTheDocument();
    expect(palette.getByText('Go to workspace')).toBeInTheDocument();
    expect(palette.getByText('Go to tasks')).toBeInTheDocument();
    expect(palette.getByText('Go to deployments')).toBeInTheDocument();
    expect(palette.getByText('Settings')).toBeInTheDocument();
  });

  it('renders the Simplified Chinese navigation vocabulary without stale English', async () => {
    await i18n.changeLanguage('zh');
    const palette = renderOpenPalette();

    expect(palette.getByText('导航')).toBeInTheDocument();
    expect(palette.getByText('前往工作流')).toBeInTheDocument();
    expect(palette.getByText('前往任务')).toBeInTheDocument();
    expect(palette.getByText('前往工作流部署')).toBeInTheDocument();
    expect(palette.getByText('设置')).toBeInTheDocument();
    expect(palette.queryByText('Navigate')).not.toBeInTheDocument();
    expect(palette.queryByText('Go to workspace')).not.toBeInTheDocument();
  });
});
