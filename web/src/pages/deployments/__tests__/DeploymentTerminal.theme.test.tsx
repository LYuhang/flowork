import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import type { Deployment } from '@/lib/api/deployments';
import { terminalThemes } from '@/lib/presentation/terminal-theme';
import { DeploymentTerminal } from '../DeploymentTerminal';

const mock = vi.hoisted(() => ({ theme: 'light', terminals: [] as Array<{ options: { theme: unknown }; dispose: ReturnType<typeof vi.fn> }> }));
vi.mock('next-themes', () => ({ useTheme: () => ({ resolvedTheme: mock.theme }) }));
vi.mock('@xterm/xterm', () => ({ Terminal: class {
  options; dispose = vi.fn();
  constructor(options: { theme: unknown }) { this.options = options; mock.terminals.push(this); }
  loadAddon() {} open() {} onData() { return { dispose: vi.fn() }; }
} }));
vi.mock('@xterm/addon-fit', () => ({ FitAddon: class { fit() {} } }));
afterEach(() => { mock.theme = 'light'; mock.terminals.length = 0; document.documentElement.classList.remove('dark'); vi.unstubAllGlobals(); });

it('changes the existing terminal palette without reconnecting or disposing the session', async () => {
  const sockets = vi.fn();
  vi.stubGlobal('WebSocket', class { static OPEN = 1; close = vi.fn(); constructor(url: string) { sockets(url); } });
  const dep = { id: 'dep-theme', enabled: true, active_revision_id: 'revision-1' } as Deployment;
  const view = render(<DeploymentTerminal dep={dep} />);
  fireEvent.click(screen.getByRole('button', { name: /connect|连接/i }));
  await waitFor(() => expect(sockets).toHaveBeenCalledTimes(1));
  const terminal = mock.terminals[0];
  expect(terminal.options.theme).toEqual(terminalThemes.light);
  mock.theme = 'dark'; document.documentElement.classList.add('dark');
  view.rerender(<DeploymentTerminal dep={dep} />);
  expect(terminal.options.theme).toEqual(terminalThemes.dark);
  mock.theme = 'light'; document.documentElement.classList.remove('dark');
  view.rerender(<DeploymentTerminal dep={dep} />);
  expect(terminal.options.theme).toEqual(terminalThemes.light);
  expect(sockets).toHaveBeenCalledTimes(1);
  expect(mock.terminals).toHaveLength(1);
  expect(terminal.dispose).not.toHaveBeenCalled();
  view.unmount();
  expect(terminal.dispose).toHaveBeenCalledOnce();
});
