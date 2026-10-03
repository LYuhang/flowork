import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { useState } from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { MemoryRouter } from 'react-router';
import i18n from 'i18next';

// Single home for the Explorer section component tests. Mocks react-router /
// queries.workflow / api.vfs here ONCE — a second sibling file mocking the same
// modules would collide under vitest isolate=false.

vi.mock('@/lib/api/queries/workflow', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api/queries/workflow')>();
  return {
    ...actual,
    // VfsExplorer self-fetches the active version pointer via useWorkflow.
    useWorkflow: () => ({
      data: { meta: { active_v: 1, active_sv: 0 } },
      isLoading: false,
      isError: false,
    }),
    useWorkflowVersions: () => ({
      data: {
        versions: [
          { major: 1, sub: 0, v: 1, sv: 0 },
          { major: 2, sub: 0, v: 2, sv: 0 },
          { major: 1, sub: 1, v: 1, sv: 1 },
        ],
      },
      isLoading: false,
      isError: false,
    }),
  };
});

vi.mock('@/lib/api/queries/workflow-workspace', () => ({
  useWorkflowWorkspaceIdentity: () => ({
    data: { workflow_scope_id: 'wf1', mount_scope_id: '__mount_user' },
    isLoading: false,
    isError: false,
  }),
}));

// Every override is delegated to a `globalThis.__mock*` cell so the VfsItemMenu
// sibling test (which also mocks this module) installs the SAME factory — under
// isolate=false the LAST factory registered wins the slot for ALL files, so both
// must be behaviorally identical. The cells default to this file's behavior and
// are (re)installed in beforeEach. `signVfs` is delegated too so the sibling can
// stage its own download spy.
vi.mock('@/lib/api/vfs', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api/vfs')>();
  return {
    ...actual,

    signVfs: (...a: unknown[]) => ((globalThis as any).__mockSignVfs ?? (async () => ({ url: 'about:blank' })))(...a),

    listVfs: (...a: unknown[]) => ((globalThis as any).__mockListVfs ?? (async () => ({ entries: [], root_capabilities: {} })))(...a),

    readVfs: (...a: unknown[]) => ((globalThis as any).__mockReadVfs ?? (async () => ({ path: '', content_type: 'text/plain', content: '', size_bytes: 0, truncated: false, wf_version: null, stale: false })))(...a),

    readVfsRun: (...a: unknown[]) => ((globalThis as any).__mockReadVfsRun ?? (async () => ({ path: '', content_type: 'application/json', content: '{}', size_bytes: 2, truncated: false, wf_version: null, stale: false })))(...a),

    listVfsRun: (...a: unknown[]) => ((globalThis as any).__mockListVfsRun ?? (async () => ({ entries: [] })))(...a),

    uploadVfsFile: (...a: unknown[]) => ((globalThis as any).__mockUploadVfsFile ?? (async () => ({ path: '', size_bytes: 0, content_type: '', replaced: false })))(...a),
    // Delegated so the VfsItemMenu sibling test (which exercises these) installs
    // the SAME factory — under isolate=false the LAST factory wins for ALL files.

    deleteVfs: (...a: unknown[]) => ((globalThis as any).__mockDeleteVfs ?? (async () => ({ deleted: 1 })))(...a),

    renameVfs: (...a: unknown[]) => ((globalThis as any).__mockRenameVfs ?? (async () => ({ path: '' })))(...a),
  };
});

// The upload spy lives in a global cell (a vi.fn so calls can be asserted). The
// default resolves a fresh-upload result; per-test overrides via `mockUpload`.
const uploadSpy = vi.fn((..._args: unknown[]) =>
  Promise.resolve({ path: '/mount/x.csv', size_bytes: 1, content_type: 'text/csv', replaced: false }),
);
let mockUpload: (...args: unknown[]) => Promise<unknown> = async () => ({
  path: '/mount/x.csv',
  size_bytes: 1,
  content_type: 'text/csv',
  replaced: false,
});

// Mutable cell the mocked `listVfs` reads — the default keeps the original
// single `/data/cells_1.jsonl` entry; upload tests stage their own listings.
const DEFAULT_LIST_ENTRIES = [
  {
    path: '/data/cells_1.jsonl',
    kind: 'artifact',
    content_type: 'table/jsonl',
    abstract: '',
    size_bytes: 10,
    wf_version: 'v0.sv0',
    last_access: 0,
    stale: true,
    capabilities: ['read', 'download', 'copy_path', 'rename', 'delete'],
  },
];
let mockListEntries: unknown[] = DEFAULT_LIST_ENTRIES;

// Mutable cell the mocked `listVfsRun` reads, so each WORKFLOW_SANDBOX test can
// stage its own run-tier entries without re-mocking the module.
let mockRunEntries: {
  path: string;
  content_type: string;
  size_bytes: number;
  capabilities: string[];
}[] = [];
const listVfsRunSpy = vi.fn();

// VfsExplorer (mounted below) transitively pulls in NodesSection. Under
// vitest isolate=false the module
// graph is shared across files, so the LAST factory registered for a module
// wins the slot for ALL consumers in the run — if this file's factory won, a
// static one would clobber the sibling tests' mocks (the
// [[feedback_vitest_isolate_false]] pitfall). To stay behaviorally identical no
// matter which file wins, we install the SAME delegating factories the sibling
// tests use: each reads its per-test behavior from a `globalThis.__mock*` cell,
// Workflow /run is keyed by wfId.
vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));
vi.mock('@/stores/workflow-edit', () => ({

  useWorkflowEditStore: Object.assign((sel: (s: { addNode: () => void }) => unknown) => sel({ addNode: (globalThis as any).__mockAddNode ?? (() => {}) }), {

    getState: () => ({ addNode: (globalThis as any).__mockAddNode ?? (() => {}) }),
  }),
}));
vi.mock('@/pages/canvas/CanvasViewportContext', () => ({
  useCanvasViewport: () =>

    ((globalThis as any).__mockUseCanvasViewport ?? (() => ({ viewportCenterFlowPos: () => ({ x: 42, y: 7 }) })))(),
}));

import { WorkflowVersionsSection } from '@/pages/canvas/explorer/WorkflowVersionsSection';
import { VfsFilesSection } from '@/pages/canvas/explorer/VfsFilesSection';
import { VfsExplorer } from '@/pages/canvas/explorer/VfsExplorer';
import { VfsRunSection } from '@/pages/canvas/explorer/VfsRunSection';
import { useUIStore } from '@/stores/ui';

const testI18n = i18n.createInstance();
void testI18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: {} } },
  interpolation: { escapeValue: false },
});

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <I18nextProvider i18n={testI18n}>
        <MemoryRouter>{ui}</MemoryRouter>
      </I18nextProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
  uploadSpy.mockClear();
  listVfsRunSpy.mockClear();
  mockRunEntries = [];
  mockListEntries = DEFAULT_LIST_ENTRIES;
  mockUpload = async () => ({
    path: '/mount/x.csv',
    size_bytes: 1,
    content_type: 'text/csv',
    replaced: false,
  });
  useUIStore.setState({ explorerOpen: true, activeChatIds: { chat: null, browser: null } });
  // Install this file's @/lib/api/vfs behavior into the shared global cells the
  // delegating factory reads (so a sibling test that won the module slot still
  // routes to THIS file's mocks during its own tests).

  (globalThis as any).__mockSignVfs = async () => ({ url: 'about:blank' });

  (globalThis as any).__mockListVfs = async () => ({
    entries: mockListEntries,
    root_capabilities: {
      mount: ['upload', 'create_folder', 'rename', 'delete'],
      data: ['upload', 'create_folder', 'rename', 'delete'],
    },
  });

  (globalThis as any).__mockReadVfs = async () => ({
    path: '/data/cells_1.jsonl',
    content_type: 'table/jsonl',
    content: '{"a":1}',
    size_bytes: 7,
    truncated: false,
    wf_version: 'v0.sv0',
    stale: true,
  });

  (globalThis as any).__mockListVfsRun = async (...args: unknown[]) => {
    listVfsRunSpy(...args);
    return { entries: mockRunEntries };
  };

  (globalThis as any).__mockUploadVfsFile = (...a: unknown[]) => {
    void uploadSpy(...a);
    return mockUpload(...a);
  };
  // Reset shared cross-file mock cells to THIS file's defaults so a sibling
  // test (run earlier in the shared graph) can't leak its delegate in.

  (globalThis as any).__mockAddNode = vi.fn();

  (globalThis as any).__mockUseAgentPlans = (_c: string | null) => ({ data: undefined, isLoading: false, isError: false });

  (globalThis as any).__mockUseAgentPlan = (r: string | null) => ({ data: undefined, isSuccess: !!r });
});

describe('WorkflowVersionsSection', () => {
  it('separates following a major from opening a fixed subversion', () => {
    wrap(<WorkflowVersionsSection wfId="wf1" activeMajor={1} activeSub={0} />);
    expect(screen.getByText('v1').closest('a')).toHaveAttribute('href', '/workflow/wf1/version/v1');
    expect(screen.getByText('v1.sv1').closest('a')).toHaveAttribute('href', '/workflow/wf1/version/v1.sv1?snapshot=1');
    expect(screen.getByText('v1.sv0')).toBeInTheDocument();
    expect(screen.queryByText('v2.sv0')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Expand v2' }));
    expect(screen.getByText('v2.sv0')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Collapse v1' }));
    expect(screen.queryByText('v1.sv1')).not.toBeInTheDocument();
    expect(screen.getByText('v1').closest('a')).toHaveAttribute('href', '/workflow/wf1/version/v1');
  });

});

describe('VfsExplorer section labels', () => {
  it('renders the ExplorerBlock headers (route-fed props)', () => {
    wrap(<VfsExplorer wfId="wf1" readOnly={false} />);
    for (const title of ['Workflow Versions', 'Sandbox']) {
      expect(screen.getByText(title)).toBeInTheDocument();
    }
    expect(screen.queryByText('Workflow Sandbox')).not.toBeInTheDocument();
    expect(screen.queryByText('Agent Sandbox')).not.toBeInTheDocument();
    expect(screen.queryByText('Agent Workflows')).not.toBeInTheDocument();
  });

  it('the Sandbox block is expanded by default and shows the mount and run roots', async () => {
    wrap(<VfsExplorer wfId="wf1" readOnly={false} />);
    const header = screen.getByRole('button', { name: /^Sandbox/i });
    expect(header).toHaveAttribute('aria-expanded', 'true');
    expect(await screen.findByRole('treeitem', { name: /^mount/ })).toBeInTheDocument();
    expect(screen.queryByRole('treeitem', { name: /^store/ })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^run/i })).toBeInTheDocument();
    expect(
      screen.getAllByRole('treeitem').filter((item) => item.getAttribute('aria-selected') === 'true'),
    ).toHaveLength(1);
  });

  it('other blocks are collapsed by default and toggle open on header click', () => {
    wrap(<VfsExplorer wfId="wf1" readOnly={false} />);
    const header = screen.getByRole('button', { name: /^Workflow Versions/i });
    expect(header).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(header);
    expect(header).toHaveAttribute('aria-expanded', 'true');
    expect(header).toHaveClass('bg-surface-sunken/70');
    expect(header.nextElementSibling).toHaveAttribute('data-role', 'explorer-block-content');
  });

  it('renders nothing when the explorer is collapsed', () => {
    useUIStore.setState({ explorerOpen: false });
    const { container } = wrap(<VfsExplorer wfId="wf1" readOnly={false} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe('VfsFilesSection tree', () => {
  it('shows the canonical folders collapsed by default; expands data → file + stale + onOpen', async () => {
    const onOpen = vi.fn();
    wrap(<VfsFilesSection wfId="wf1" open onOpenFile={onOpen} />);
    // Canonical folders render (after the list query resolves); files hidden until expand.
    for (const f of ['mount', 'data', 'memory', 'logs']) {
      expect(await screen.findByRole('treeitem', { name: new RegExp(`^${f}`) })).toBeInTheDocument();
    }
    expect(screen.queryByText('cells_1.jsonl')).toBeNull();
    // The /data folder TOGGLE button's accessible name starts with "data";
    // upload moved into the right-click menu (no sibling upload button), so the
    // prefix match is unambiguous.
    fireEvent.keyDown(screen.getByRole('treeitem', { name: /^data/ }), { key: 'ArrowRight' });
    await waitFor(() => expect(screen.getByText('cells_1.jsonl')).toBeInTheDocument());
    expect(screen.getByLabelText(/stale/i)).toBeInTheDocument();
    fireEvent.doubleClick(screen.getByText('cells_1.jsonl'));
    expect(onOpen).toHaveBeenCalledWith('/data/cells_1.jsonl');
  });

  it('shares one selected row across separately queried VFS scopes', async () => {
    function ComposedTree() {
      const [selectionKey, setSelectionKey] = useState<string | null>(null);
      return (
        <>
          <VfsFilesSection
            wfId="chat-workspace"
            open
            roots={['data']}
            selectionKey={selectionKey}
            onSelectionKeyChange={setSelectionKey}
            onOpenFile={vi.fn()}
          />
          <VfsFilesSection
            wfId="user-mount"
            open
            roots={['mount']}
            selectionKey={selectionKey}
            onSelectionKeyChange={setSelectionKey}
            defaultSelectFirst={false}
            onOpenFile={vi.fn()}
          />
        </>
      );
    }

    wrap(<ComposedTree />);
    const data = await screen.findByRole('treeitem', { name: /^data/ });
    const mount = await screen.findByRole('treeitem', { name: /^mount/ });

    expect(data).toHaveAttribute('aria-selected', 'true');
    expect(mount).toHaveAttribute('aria-selected', 'false');

    fireEvent.click(mount);

    expect(data).toHaveAttribute('aria-selected', 'false');
    expect(mount).toHaveAttribute('aria-selected', 'true');
  });
});

describe('VfsFilesSection /mount + /data upload (right-click menu)', () => {
  // Upload now lives in each user-writable root folder's right-click menu
  // ("Upload file…"); the hidden <input type=file> is rendered once per
  // writable folder (/mount, /data) in canonical order.
  const fileInputs = () =>
    Array.from(document.querySelectorAll('input[type="file"]')) as HTMLInputElement[];

  it('renders an upload input for /mount and /data only (not /memory)', async () => {
    wrap(<VfsFilesSection wfId="wf1" open onOpenFile={vi.fn()} />);
    await waitFor(() => expect(fileInputs()).toHaveLength(2));
  });

  it('uploads a new-name file directly to /mount (no confirm)', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    wrap(<VfsFilesSection wfId="wf1" open onOpenFile={vi.fn()} />);
    await waitFor(() => expect(fileInputs()).toHaveLength(2));

    const file = new File(['a,b'], 'fresh.csv', { type: 'text/csv' });
    fireEvent.change(fileInputs()[0], { target: { files: [file] } });

    await waitFor(() => expect(uploadSpy).toHaveBeenCalledWith('wf1', file, 'mount'));
    expect(confirmSpy).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });

  it('uploads to /data via the /data input (folder threaded through)', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    wrap(<VfsFilesSection wfId="wf1" open onOpenFile={vi.fn()} />);
    await waitFor(() => expect(fileInputs()).toHaveLength(2));

    // The /data input is second in canonical order.
    const file = new File(['x'], 'note.csv', { type: 'text/csv' });
    fireEvent.change(fileInputs()[1], { target: { files: [file] } });

    await waitFor(() => expect(uploadSpy).toHaveBeenCalledWith('wf1', file, 'data'));
    confirmSpy.mockRestore();
  });

  it('uses the shared overwrite dialog when the /mount path already exists', async () => {
    mockListEntries = [
      {
        path: '/mount/dup.csv',
        kind: 'artifact',
        content_type: 'text/csv',
        abstract: '',
        size_bytes: 5,
        wf_version: null,
        last_access: 0,
        stale: false,
        capabilities: ['read', 'download', 'copy_path', 'rename', 'delete'],
      },
    ];
    wrap(<VfsFilesSection wfId="wf1" open onOpenFile={vi.fn()} />);
    await waitFor(() => expect(fileInputs()).toHaveLength(2));

    const file = new File(['x'], 'dup.csv', { type: 'text/csv' });
    fireEvent.change(fileInputs()[0], { target: { files: [file] } });

    expect(await screen.findByRole('heading', { name: 'Replace existing file?' })).toBeInTheDocument();
    expect(screen.getByText('dup.csv')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    // Cancelled → no upload.
    expect(uploadSpy).not.toHaveBeenCalled();
  });
});

describe('VfsRunSection (Workflow Run)', () => {
  // The section header lives in VfsExplorer ("WORKFLOW_SANDBOX"); VfsRunSection
  // renders an always-present top-level `run` folder (expanded by default) so
  // the structure is perceivable even before a run.
  it('shows the empty state inside the /run folder before files exist', async () => {
    wrap(<VfsRunSection wfId="wf1" onOpenFile={vi.fn()} />);
    expect(screen.getByRole('button', { name: /^run/i })).toBeInTheDocument();
    expect(await screen.findByText(/This run produced no files/i)).toBeInTheDocument();
    expect(listVfsRunSpy).toHaveBeenCalledWith('wf1');
  });

  it('builds a run-tier tree from workflow-id entries; clicking a file opens it', async () => {
    mockRunEntries = [{
      path: '/out/report.csv',
      content_type: 'text/csv',
      size_bytes: 42,
      capabilities: ['read', 'download', 'copy_path'],
    }];
    const onOpen = vi.fn();
    wrap(<VfsRunSection wfId="wf1" onOpenFile={onOpen} />);
    // Run-tier folders are whatever the run wrote ('out'), NOT the fixed TOP set.
    const outFolder = await screen.findByRole('button', { name: /^out/ });
    fireEvent.click(outFolder);
    await waitFor(() => expect(screen.getByText('report.csv')).toBeInTheDocument());
    fireEvent.doubleClick(screen.getByText('report.csv'));
    expect(onOpen).toHaveBeenCalledWith('/out/report.csv', 'wf1');
    expect(listVfsRunSpy).toHaveBeenCalledWith('wf1');
  });

});
