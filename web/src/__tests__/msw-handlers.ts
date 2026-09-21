/**
 * MSW (Mock Service Worker) handler registry for vitest.
 *
 * One module-scope `server` per test process — created here, listened
 * to in `setup-tests.ts`. Tests opt into different mock states via
 * `server.use(...)` for per-test overrides; the defaults below cover
 * the happy path for the small handful of endpoints exercised in the
 * T21 unit/integration smoke tests.
 *
 * MSW v2 API note: handlers are built from the `http` factory (the old
 * `rest.*` API was removed in v2). `HttpResponse.json` is the canonical
 * way to return a typed JSON body; we keep the bodies aligned with the
 * generated `components['schemas']` so the integration tests exercise
 * the real openapi-fetch happy path.
 *
 * Why a tiny default set: T21 ships infrastructure. T22+ will grow this
 * file as more flows get coverage. Keeping the default minimal also
 * keeps the unhandled-request error meaningful — any new flow forces a
 * conscious decision to mock its endpoint.
 */
import { setupServer } from 'msw/node';
import { http, HttpResponse } from 'msw';
import type { components } from '@/lib/api/schema';

type Page<T> = { items: T[]; total: number; limit: number; offset: number };
type WorkflowMetaOut = components['schemas']['WorkflowMetaOut'];

const emptyPage = <T,>(): Page<T> => ({
  items: [],
  total: 0,
  limit: 50,
  offset: 0,
});

/**
 * Build a `WorkflowMetaOut` with sensible defaults. Tests override
 * individual fields without having to spell out the full shape every
 * time.
 */
export const fixtureWorkflow = (
  overrides: Partial<WorkflowMetaOut> = {},
): WorkflowMetaOut => ({
  wf_id: 'wf_test_1',
  workflow_name: 'Test Workflow',
  description: 'a workflow for tests',
  active_v: 1,
  active_sv: 0,
  updated_at: 1_700_000_000,
  created_at: 1_700_000_000,
  tags: [],
  access: {
    capabilities: ['view_metadata', 'view', 'export', 'update', 'delete', 'manage_access', 'use', 'execute', 'cancel', 'inspect_runs', 'deploy', 'mount'],
    effective_role: 'manager',
    source: 'computed',
  },
  provenance: {
    ownership_scope: 'personal',
    origin_type: 'created',
    owner: { type: 'user', display_name: 'Test user' },
    created_by: { type: 'user', display_name: 'Test user' },
  },
  ...overrides,
});

export const handlers = [
  http.get('*/api/v1/me', () =>
    HttpResponse.json({ username: 'test-user' }),
  ),

  http.get('*/api/v1/version', () =>
    HttpResponse.json({ engine_version: '0.0.0-test', api_version: '0.0.0-test' }),
  ),

  http.get('*/api/v1/agent-runtime/capabilities', () =>
    HttpResponse.json({
      protocol_version: 2,
      runtime_type: 'codex',
      runtime_available: true,
      authenticated: null,
      source: 'test-default',
      models: [],
      default_model_id: null,
      error_code: null,
      bound_agent_settings: null,
    }),
  ),

  http.get('*/api/v1/mcp-servers', () =>
    HttpResponse.json({ items: [] }),
  ),

  http.get('*/api/v1/workflows', () =>
    HttpResponse.json(emptyPage<WorkflowMetaOut>()),
  ),

  http.get('*/api/v1/workflows/:wfId', ({ params }) =>
    HttpResponse.json({
      meta: fixtureWorkflow({ wf_id: String(params.wfId) }),
      workflow: { __meta__: { workflow_name: 'Test Workflow' } },
    }),
  ),

  http.get('*/api/v1/chat-scopes/:scopeId/chats', () =>
    HttpResponse.json({ items: [], total: 0, limit: 50, offset: 0 }),
  ),

  http.get('*/api/v1/chat-scopes/:scopeId/chats/:chatId/messages', () =>
    HttpResponse.json({ items: [], total: 0, limit: 200, offset: 0 }),
  ),
  http.get('*/api/v1/chats/workspace', ({ request }) => {
    const chatId = new URL(request.url).searchParams.get('chat_id') || 'chat-test';
    return HttpResponse.json({
      chat_id: chatId,
      workspace_scope_id: `__chatws_test_${chatId}`,
    });
  }),
  http.get('*/api/v1/interactive-artifacts/:artifactId', () =>
    new HttpResponse(null, { status: 404 }),
  ),

  http.post('*/api/v1/interactive-artifacts/:artifactId/resource-session', ({ params }) =>
    HttpResponse.json({
      artifact_id: String(params.artifactId),
      resource_mounts: [
        { path_prefix: '/mount/', root_url: '/api/v1/vfs/resources/test-mount-token/' },
        { path_prefix: '/', root_url: '/api/v1/vfs/resources/test-token/' },
      ],
      base_url: '/api/v1/vfs/resources/test-token/data/',
      expires_in: 3600,
      draft_debounce_ms: 600,
    }),
  ),
];

export const server = setupServer(...handlers);
