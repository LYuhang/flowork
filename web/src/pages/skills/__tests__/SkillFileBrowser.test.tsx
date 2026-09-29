import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { SkillFileBrowser } from '@/pages/skills/SkillFileBrowser';

describe('<SkillFileBrowser>', () => {
  it('renders nested files as an explorer and loads file content in the detail pane', async () => {
    const user = userEvent.setup();
    const loadFile = vi.fn(async (path: string) => new Blob(
      [path === 'scripts/helpers/format.py' ? 'def format_value(value):\n    return str(value)' : ''],
      { type: 'text/x-python' },
    ));

    render(
      <SkillFileBrowser
        files={['scripts/helpers/format.py', 'references/guide.md']}
        skillMd={'# Example Skill\n\nInstructions.'}
        loadFile={loadFile}
        labels={{ files: 'Package Files', loading: 'Loading File…', failed: 'Could Not Load File', binary: 'No Preview' }}
      />,
    );

    expect(screen.getByRole('tree', { name: 'Package Files' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'scripts' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'helpers' })).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'format.py' }));

    expect(loadFile).toHaveBeenCalledWith('scripts/helpers/format.py');
    expect(await screen.findByText(/def format_value\(value\):/)).toBeInTheDocument();
    expect(screen.getByText('3')).toBeInTheDocument();
  });

  it('supports roving tree keyboard navigation and reports the selected path', async () => {
    const user = userEvent.setup();
    const onSelectedPathChange = vi.fn();
    const loadFile = vi.fn(async () => new Blob(['print("ok")'], { type: 'text/x-python' }));

    render(
      <SkillFileBrowser
        files={['scripts/helpers/format.py']}
        skillMd="# Skill"
        loadFile={loadFile}
        onSelectedPathChange={onSelectedPathChange}
        labels={{ files: 'Package Files', loading: 'Loading File…', failed: 'Could Not Load File', binary: 'No Preview' }}
      />,
    );

    const scripts = screen.getByRole('button', { name: 'scripts' });
    scripts.focus();
    await user.keyboard('{ArrowRight}{ArrowRight}{ArrowRight}{Enter}');

    expect(loadFile).toHaveBeenCalledWith('scripts/helpers/format.py');
    expect(onSelectedPathChange).toHaveBeenCalledWith('scripts/helpers/format.py');
    expect(screen.getByRole('treeitem', { selected: true })).toHaveTextContent('format.py');
  });

  it('does not restore a stale controlled path while URL state catches up', async () => {
    const user = userEvent.setup();
    const loadFile = vi.fn(async () => new Blob(['logo'], { type: 'image/png' }));

    render(
      <SkillFileBrowser
        files={['assets/logo.png']}
        skillMd="# Skill"
        loadFile={loadFile}
        selectedPath="SKILL.md"
        onSelectedPathChange={() => undefined}
        labels={{ files: 'Package Files', loading: 'Loading File…', failed: 'Could Not Load File', binary: 'No Preview' }}
      />,
    );

    await user.click(screen.getByRole('button', { name: 'logo.png' }));

    expect(await screen.findByRole('heading', { name: 'logo.png' })).toBeInTheDocument();
    expect(screen.getByRole('treeitem', { selected: true })).toHaveTextContent('logo.png');
  });

  it('loads a non-SKILL file selected by a direct URL on first render', async () => {
    const loadFile = vi.fn(async () => new Blob(['theme: light'], { type: 'text/yaml' }));

    render(
      <SkillFileBrowser
        files={['agents/openai.yaml']}
        skillMd="# Skill"
        loadFile={loadFile}
        selectedPath="agents/openai.yaml"
        onSelectedPathChange={() => undefined}
        labels={{ files: 'Package Files', loading: 'Loading File…', failed: 'Could Not Load File', binary: 'No Preview' }}
      />,
    );

    expect(await screen.findByText('theme: light')).toBeInTheDocument();
    expect(loadFile).toHaveBeenCalledWith('agents/openai.yaml');
  });
  it('restores SKILL.md text after previewing an image', async () => {
    const user = userEvent.setup();
    render(<SkillFileBrowser files={['image.png']} skillMd="# Skill instructions"
      loadFile={async () => new Blob(['image'], { type: 'image/png' })}
      labels={{ files: 'Files', loading: 'Loading', failed: 'Failed', binary: 'Binary' }} />);
    await user.click(screen.getByRole('button', { name: 'image.png' }));
    expect(await screen.findByRole('img', { name: 'image.png' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'SKILL.md' }));
    expect(await screen.findByRole('heading', { name: 'Skill instructions' })).toBeInTheDocument();
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
  });

  it('does not reuse cached file content from another revision', async () => {
    const user = userEvent.setup();
    const labels = { files: 'Files', loading: 'Loading', failed: 'Failed', binary: 'Binary' };
    const first = vi.fn(async () => new Blob(['# Old guide'], { type: 'text/markdown' }));
    const second = vi.fn(async () => new Blob(['# New guide'], { type: 'text/markdown' }));
    const { rerender } = render(<SkillFileBrowser persistKey="skill:revision-one"
      files={['guide.md']} skillMd="# Skill" loadFile={first} labels={labels} />);
    await user.click(screen.getByRole('button', { name: 'guide.md' }));
    expect(await screen.findByRole('heading', { name: 'Old guide' })).toBeInTheDocument();
    rerender(<SkillFileBrowser persistKey="skill:revision-two" files={['guide.md']}
      skillMd="# Skill" loadFile={second} labels={labels} />);
    await user.click(screen.getByRole('button', { name: 'guide.md' }));
    expect(await screen.findByRole('heading', { name: 'New guide' })).toBeInTheDocument();
    expect(second).toHaveBeenCalledOnce();
  });

});
