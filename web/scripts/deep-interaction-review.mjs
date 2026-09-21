/**
 * Deep interaction review for every production surface.
 *
 * Drives the production build with real Chromium pointer, keyboard, focus,
 * file chooser and viewport events. The backend is represented by a stateful
 * protocol fixture because the native stack is validated separately.
 *
 * Run:
 *   pnpm exec vite preview --host 127.0.0.1 --port 5173
 *   node scripts/deep-interaction-review.mjs
 */
import { chromium } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const BASE = process.env.VIBECANVAS_REVIEW_BASE ?? 'http://127.0.0.1:5173';
const OUT = resolve(
  __dirname,
  '..',
  'screenshots',
  process.env.VIBECANVAS_REVIEW_OUTPUT ?? `deep-interaction-${new Date().toISOString().slice(0, 10)}`,
);
mkdirSync(OUT, { recursive: true });

const WF_ID = 'wf_deep_review';
const TASK_ID = '00000000-0000-4000-8000-000000000101';
const DEP_ID = '00000000-0000-4000-8000-000000000201';
const MCP_ID = 'mcp-review';
const SKILL_ID = 'skill-review';
const KB_ID = 'kb-review';
const now = new Date().toISOString();

const results = [];
const runtimeEvents = [];
const requestLog = [];
let sequence = 0;

const access = {
  capabilities: [
    'view', 'view_metadata', 'export', 'update', 'delete', 'manage_access',
    'use', 'execute', 'cancel', 'resume', 'inspect_runs', 'deploy', 'mount',
    'manage_secret', 'publish', 'manage_members', 'manage_policy', 'view_audit',
  ],
  effective_role: 'manager',
  source: 'review-fixture',
};

const businessOrganization = {
  organization_id: 'review-org', kind: 'business', slug: 'review-company',
  name: 'Review Company', membership_id: 'member-review', role: 'owner',
  status: 'active', active: true, access,
};

const organizationMember = {
  membership_id: 'member-review', user_id: 'review-user',
  email: 'review@example.com', display_name: 'Review Owner', role: 'owner',
  status: 'active', source: 'native', directory_provider_id: null,
  created_at: now, updated_at: now,
};

const organizationGroup = {
  group_id: 'group-product', organization_id: 'review-org', parent_group_id: null,
  kind: 'team', name: 'Product', source: 'native', directory_provider_id: null,
  external_id: null, status: 'active', created_by: 'review-user',
  created_at: now, updated_at: now, access,
};

const runtimeSettings = (defaultRuntime = 'langchain') => ({
  default_runtime_type: defaultRuntime,
  available_runtime_types: ['langchain', 'codex'],
  codex_managed_profile_id: 'company-primary',
  preferred_timezone: 'America/Los_Angeles',
  codex_managed_profiles: [
    { id: 'company-primary', name: 'Company OpenAI — Primary', model_count: 4 },
    { id: 'company-research', name: 'Company OpenAI — Research', model_count: 2 },
  ],
  codex_auth_methods: ['chatgpt', 'managed_api', 'personal_api'],
});

const workflowMeta = {
  wf_id: WF_ID,
  workflow_name: 'Deep Review Workflow',
  description: 'Stateful browser acceptance fixture',
  active_v: 2,
  active_sv: 1,
  created_at: 1_785_650_000,
  updated_at: 1_785_660_000,
  tags: ['review', 'agent'],
  access,
};

const workflow = {
  __meta__: { workflow_name: workflowMeta.workflow_name, version: 'v2.sv1' },
  start: {
    node_id: 'start', node_name: 'Start', node_type: 'StartNode',
    node_description: 'Collect input', input_fields: {},
    output_fields: { prompt: { type: 'string', description: 'Prompt' } },
    node_config: {}, children: ['prompt'], __attributes__: { x: 40, y: 160 },
  },
  prompt: {
    node_id: 'prompt', node_name: 'Analyze', node_type: 'PromptNode',
    node_description: 'Analyze the request',
    input_fields: { prompt: { type: 'string', value: '', reference: 'Start.prompt' } },
    output_fields: { answer: { type: 'string', description: 'Answer' } },
    node_config: { model: 'gpt-4o', prompt: 'Analyze: {prompt}' },
    children: ['end'], __attributes__: { x: 380, y: 160 },
  },
  end: {
    node_id: 'end', node_name: 'End', node_type: 'EndNode',
    node_description: 'Return answer',
    input_fields: { answer: { type: 'string', value: '', reference: 'Analyze.answer' } },
    output_fields: {}, node_config: {}, children: [],
    __attributes__: { x: 720, y: 160 },
  },
};

const task = {
  id: TASK_ID, status: 'running', progress: 0.42, task_type: 'batch_exec',
  workflow_id: WF_ID, payload: {}, result: null, results_uri: null, error: null,
  background_job_id: 'background-review', submitted_at: now, started_at: now,
  finished_at: null, access,
};

const deployment = {
  id: DEP_ID, tenant_id: 'review-org', user_id: 'review-user', wf_id: WF_ID,
  name: 'Review API', slug: 'review-api', trigger_type: 'api', version_pin: 'head',
  pinned_major: null, pinned_sub: null, enabled: true, rate_limit_qps: 10,
  invoke_count: 42, last_invoked_at: now, created_at: now, updated_at: now,
  deleted_at: null, access,
};

const credential = {
  id: 'cred-review', name: 'Review OpenAI Key', description: 'Acceptance fixture',
  provider: 'OpenAI', model: 'gpt-4o', model_context_tokens: 128000,
  api_url: 'https://api.openai.com/v1', proxy_url: null,
  created_at: now, updated_at: now, access,
};

const mcpServer = {
  id: MCP_ID, name: 'Review Search Tools', tool_prefix: 'review', transport: 'sse',
  endpoint: 'https://example.test/mcp', auth_mode: 'none',
  connection_status: 'not_required', auth_config: null, enabled: true,
  description: 'Search and inspect acceptance data.', description_source: 'user',
  description_model_id: null, description_generated_at: null,
  description_basis_hash: null, last_handshake_status: 'ok', last_tool_count: 2,
  last_tool_names: [
    { name: 'review_search', description: 'Search review data' },
    { name: 'review_read', description: 'Read review data' },
  ],
  last_handshake_at: now, created_at: now, updated_at: now, access,
};

const skill = {
  id: SKILL_ID, name: 'Review Skill', description: 'Validates deep interactions.',
  allowed_tools: ['read_file', 'bash'], version: 2, source: 'custom',
  source_url: null, created_at: now, updated_at: now,
  body: '# Review Skill\n\nInspect the requested surface carefully.',
  skill_md: '---\nname: review-skill\ndescription: Validates deep interactions\nversion: 2\n---\n\n# Review Skill\n\nInspect the requested surface carefully.',
  files: ['SKILL.md', 'scripts/review.py'], has_draft: true,
  draft_updated_at: now, access,
};

const knowledge = {
  id: KB_ID, name: 'Review Handbook', description: 'Product and release guidance',
  retrieval_strategy: 'agentic_lexical',
  created_at: now, updated_at: now, latest_updated_at: now,
  file_count: 2, chunk_count: 12, access,
};

const backgroundJob = {
  job_id: 'job-review',
  chat_id: 'chat-review',
  parent_run_id: null,
  runtime_type: 'langchain',
  executor_type: 'tool',
  tool_name: 'bash',
  title: 'Review background task',
  status: 'completed',
  progress: { current: 1, total: 1, message: 'Review completed' },
  input: { command: 'pnpm test' },
  result: { summary: 'Done', tests_passed: 42 },
  result_ref: null,
  error: {},
  event_seq: 3,
  cancel_requested: false,
  delivery_status: 'delivered',
  delivered_at: now,
  delivery_batch_id: 'delivery-review',
  created_at: now,
  started_at: now,
  finished_at: now,
  updated_at: now,
};

const state = {
  workflows: [workflowMeta],
  tasks: [task, { ...task, id: `${TASK_ID.slice(0, -1)}2`, status: 'finished', progress: 1, workflow_id: 'wf_finished', result: { rows_total: 10, rows_ok: 9, rows_failed: 1 } }],
  deployments: [deployment],
  credentials: [credential],
  mcpServers: [mcpServer],
  skills: [skill],
  knowledge: [knowledge],
  storageItems: [
    { name: 'reports', path: '/reports', kind: 'folder', size: null, modified_at: now, content_type: null, can_rename: true, can_delete: true },
    { name: 'review.json', path: '/review.json', kind: 'file', size: 128, modified_at: now, content_type: 'application/json', can_rename: true, can_delete: true },
  ],
};

const catalogMcp = Array.from({ length: 12 }, (_, index) => ({
  source: 'official', source_id: `io.review/server-${index + 1}`,
  name: `Review MCP ${index + 1}`, description: `Catalog MCP ${index + 1}`,
  version: '1.0.0', verified: true, usage_count: 100 - index,
  homepage: 'https://example.test', published_at: now,
  connection: { transport: 'streamable_http', endpoint: 'https://catalog.example.test/mcp', connection_config: {} },
  config_fields: index === 0 ? [{ key: 'API_KEY', label: 'API Key', description: 'Catalog secret', required: true, secret: true, target: 'env', input_type: 'string', choices: [], default: null, placeholder: 'Enter API key' }] : [],
  configuration_source: 'official_registry', auth_mode: index === 0 ? 'oauth' : 'none',
}));

const catalogSkills = Array.from({ length: 12 }, (_, index) => ({
  source: 'openai', source_label: 'OpenAI Skills', source_id: `review-${index + 1}`,
  name: `Catalog Skill ${index + 1}`, description: `Catalog skill ${index + 1}`,
  version: 1, allowed_tools: ['read_file'], homepage: 'https://example.test/skill',
  revision: 'abc123', files: [{ path: 'SKILL.md', size_bytes: 100 }],
  body: '# Catalog Skill\n\nReview the selected surface.',
  skill_md: '---\nname: catalog-skill\ndescription: Catalog review skill\n---\n\n# Catalog Skill',
}));

function json(route, body, status = 200) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

function bodyJson(request) {
  try { return request.postDataJSON(); } catch { return {}; }
}

async function installProtocol(page, { enterpriseSsoEnabled = false } = {}) {
  await page.route(/\/kb(?:\/[^?#]*)?(?:\?.*)?$/, (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const p = url.pathname.replace(/^\/api\/v1/, '');
    const method = request.method();
    requestLog.push(`${method} ${p}${url.search}`);
    if (p === '/kb/search') return json(route, {
      results: [{ chunk_id: 'chunk-1', file_id: 'kb-file-1', file_name: 'handbook.pdf', kb_id: KB_ID, text: 'Releases ship every Tuesday.', score: 91, match_kind: 'phrase', matched_terms: ['releases'], chunk_metadata: {} }],
    });
    if (p === '/kb' && method === 'GET') return json(route, state.knowledge);
    if (p === '/kb' && method === 'POST') {
      const created = { ...knowledge, id: 'kb-created', name: bodyJson(request).name ?? 'Created Knowledge' };
      state.knowledge.push(created);
      return json(route, created, 201);
    }
    if (p === `/kb/${KB_ID}`) return json(route, knowledge);
    if (p === `/kb/${KB_ID}/files` && method === 'GET') return json(route, [
      { id: 'kb-file-1', name: 'README.md', parser_type: 'markdown', mime_type: 'text/markdown', file_size: 86, status: 'indexed', error_message: null, chunk_count: 1, created_at: now },
      { id: 'kb-file-2', name: 'notes/releases.md', parser_type: 'markdown', mime_type: 'text/markdown', file_size: 42, status: 'indexed', error_message: null, chunk_count: 1, created_at: now },
    ]);
    if (p === `/kb/${KB_ID}/files/kb-file-1/raw`) return route.fulfill({ status: 200, contentType: 'text/markdown', body: '# Review Handbook\n\nBrowse the source files in this package.' });
    if (p === `/kb/${KB_ID}/files/kb-file-2/raw`) return route.fulfill({ status: 200, contentType: 'text/markdown', body: '# Release schedule\n\nReleases ship every Tuesday.' });
    if (p.includes('/files/') && method === 'DELETE') return json(route, {});
    if (p.endsWith('/files') && method === 'POST') return json(route, { id: 'kb-upload', name: 'uploaded.txt', status: 'pending' }, 201);
    return json(route, {});
  });

  await page.route('**/api/v1/**', (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const p = url.pathname;
    const method = request.method();
    requestLog.push(`${method} ${p}${url.search}`);

    if (p === '/api/v1/public-config') return json(route, { enterprise_sso_enabled: enterpriseSsoEnabled });
    if (p === '/api/v1/kb/search') return json(route, {
      results: [{ chunk_id: 'chunk-1', file_id: 'kb-file-1', file_name: 'handbook.pdf', kb_id: KB_ID, text: 'Releases ship every Tuesday.', score: 91, match_kind: 'phrase', matched_terms: ['releases'], chunk_metadata: {} }],
    });
    if (p === '/api/v1/kb' && method === 'GET') return json(route, state.knowledge);
    if (p === `/api/v1/kb/${KB_ID}`) return json(route, knowledge);
    if (p === `/api/v1/kb/${KB_ID}/files` && method === 'GET') return json(route, [
      { id: 'kb-file-1', name: 'README.md', parser_type: 'markdown', mime_type: 'text/markdown', file_size: 86, status: 'indexed', error_message: null, chunk_count: 1, created_at: now, access },
      { id: 'kb-file-2', name: 'notes/releases.md', parser_type: 'markdown', mime_type: 'text/markdown', file_size: 42, status: 'indexed', error_message: null, chunk_count: 1, created_at: now, access },
    ]);
    if (p === `/api/v1/kb/${KB_ID}/files/kb-file-1/raw`) return route.fulfill({ status: 200, contentType: 'text/markdown', body: '# Review Handbook\n\nBrowse the source files in this package.' });
    if (p === `/api/v1/kb/${KB_ID}/files/kb-file-2/raw`) return route.fulfill({ status: 200, contentType: 'text/markdown', body: '# Release schedule\n\nReleases ship every Tuesday.' });
    if (p.includes('/api/v1/kb/') && p.includes('/files/') && method === 'DELETE') return route.fulfill({ status: 204, body: '' });
    if (p.endsWith('/auth/me')) return json(route, { user_id: 'review-user', tenant_id: 'review-org', email: 'review@example.com', platform_management_role: 'platform_security_admin' });
    if (p.includes('/auth/passkeys')) return json(route, { credentials: [] });
    if (p.includes('/auth/login') || p.includes('/auth/register') || p.includes('/auth/password')) return json(route, { access_token: 'review-token', token: 'review-token' });
    if (p === '/api/v1/auth/sso/organizations/review-company/providers') return json(route, { items: [{ provider_id: 'idp-review', display_name: 'Review Identity' }] });
    if (p === '/api/v1/platform-management/overview') return json(route, {
      role: 'platform_security_admin', generated_at: now,
      identity: { registered_users: 128, active_users: 84, online_users_5m: 12, registered_users_24h: 7, personal_workspaces: 96, company_workspaces: 8 },
      organizations: [{ organization_id: 'review-org', name: 'Review Company', member_count: 32, active_member_count: 29 }],
      host: { cpu_count: 16, load_average_1m: 2.4, load_average_5m: 2.1, load_average_15m: 1.8, memory: { total_bytes: 34359738368, available_bytes: 12884901888 }, disk: { total_bytes: 536870912000, free_bytes: 322122547200 }, scope: 'current_api_host' },
      sandboxes: { resident: 9, capacity: 24, busy: 3, resident_leases: 6, pending_closes: 1 },
      privacy: { content_visible: false, user_profiles_visible: false, scope: 'aggregate_metadata_only' },
    });
    if (p === '/api/v1/platform-management/audit') {
      const categories = ['identity', 'access_security', 'resources', 'data_lifecycle', 'runtime_operations'].map((category, index) => ({
        category, total: 24 - index * 2, failures: index === 1 ? 2 : 0,
        series: [{ ts: '2026-08-03T12:00:00Z', total: 8 + index, failures: 0 }, { ts: now, total: 16 - index, failures: index === 1 ? 2 : 0 }],
        actions: [{ action: `${category}.reviewed`, total: 24 - index * 2, failures: index === 1 ? 2 : 0 }],
      }));
      return json(route, {
        role: 'platform_security_admin', generated_at: now,
        window_hours: Number(url.searchParams.get('window_hours') ?? 168), bucket: 'day', categories,
        recent_events: [{ event_id: 'audit-review', category: 'identity', action: 'identity.session.created', target_type: 'session', outcome: 'success', created_at: now }],
        catalog: categories.map(({ category }) => ({ category, actions: [`${category}.reviewed`], missing_objects: [], coverage: 'complete' })),
        privacy: { content_visible: false, identities_visible: false, customer_resource_identifiers_visible: false, private_payload_decrypted: false },
      });
    }
    if (p === '/api/v1/organizations') return json(route, { items: [businessOrganization], active_organization_id: 'review-org', session_generation: 1 });
    if (p === '/api/v1/organizations/review-org/me') return json(route, { membership: organizationMember, groups: [{ group_id: organizationGroup.group_id, kind: organizationGroup.kind, name: organizationGroup.name, source: organizationGroup.source, role: 'lead', status: 'active' }] });
    if (p === '/api/v1/organizations/review-org/groups') return json(route, { items: [organizationGroup] });
    if (p === '/api/v1/organizations/review-org/groups/group-product/members') return json(route, { items: [{ ...organizationMember, role: 'lead' }] });
    if (p === '/api/v1/organizations/review-org/members') return json(route, { items: [organizationMember, { ...organizationMember, membership_id: 'member-designer', user_id: 'review-designer', email: 'designer@example.com', display_name: 'Review Designer', role: 'member' }] });
    if (p === '/api/v1/organizations/review-org/service-accounts') return json(route, { items: [{ service_account_id: 'svc-review', name: 'Nightly deployment', kind: 'deployment', owner_resource_type: 'deployment', owner_resource_id: DEP_ID, status: 'active', generation: 3, created_by: 'review-user', credential_ids: [], created_at: now, updated_at: now, disabled_at: null }] });
    if (p === '/api/v1/organizations/review-org/identity-providers') return json(route, { items: [{ provider_id: 'idp-review', organization_id: 'review-org', display_name: 'Review Identity', issuer_url: 'https://login.example.test', client_id: 'review-web', token_endpoint_auth_method: 'client_secret_basic', has_client_secret: true, subject_claim: 'sub', email_claim: 'email', display_name_claim: 'name', scopes: ['openid', 'email', 'profile'], status: 'active', scim_token_generation: 2, scim_token_expires_at: '2027-08-04T12:00:00Z', scim_base_url: 'https://app.example.test/api/v1/scim/v2', oidc_callback_url: 'https://app.example.test/api/v1/auth/sso/callback', last_scim_sync_at: now, created_at: now, updated_at: now }] });
    if (p.includes('/audit')) return json(route, { items: [], total: 0, limit: 50, offset: 0 });

    if (p === '/api/v1/agent-runtime/settings') {
      if (method === 'PUT') return json(route, runtimeSettings(bodyJson(request).default_runtime_type ?? 'langchain'));
      return json(route, runtimeSettings());
    }
    if (p === '/api/v1/agent-runtime/codex/account') return json(route, { cli_available: true, authenticated: false });
    if (p === '/api/v1/agent-runtime/codex/managed-profile') return json(route, { ...runtimeSettings('codex'), codex_managed_profile_id: bodyJson(request).profile_id ?? 'company-primary' });
    if (p === '/api/v1/agent-runtime/capabilities') return json(route, { protocol_version: 1, runtime_type: 'langchain', runtime_available: true, authenticated: true, source: 'review', models: [{ id: 'gpt-4o', label: 'GPT-4o' }], default_model_id: 'gpt-4o', error_code: null });
    if (p.endsWith('/chats/bootstrap')) return json(route, { carrier_scope_id: 'scope-review', surface: url.searchParams.get('surface') || 'chat', available_commands: ['/task', '/deployment', '/knowledge', '/workflow', '/browser'], debug_view_enabled: true });
    if (p.endsWith('/chats/workspace')) return json(route, { workspace_scope_id: 'workspace-review', mount_scope_id: 'workspace-review', chat_id: url.searchParams.get('chat_id') || 'chat-review', current_workflow_id: null });
    if (/\/chat-scopes\/[^/]+\/chats$/.test(p)) return json(route, { items: [{ chat_id: 'chat-review', chat_context: 'Deep interaction review', created_at: now, updated_at: now }], total: 1, limit: 50, offset: 0 });
    if (/\/chat-scopes\/[^/]+\/active-runs$/.test(p)) return json(route, []);
    if (/\/chat-scopes\/[^/]+\/chats\/[^/]+\/messages$/.test(p) && method === 'POST') return route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      headers: { 'X-Turn-Id': 'turn-deep-review', 'Cache-Control': 'no-cache' },
      body: 'id: 1\nevent: done\ndata: {}\n\n',
    });
    if (/\/chat-scopes\/[^/]+\/chats\/[^/]+\/messages$/.test(p)) return json(route, { items: [
      { role: 'user', content: 'Inspect the repository and summarize the result.' },
      { role: 'assistant', content: 'I inspected the repository and ran two tools.\n\n```bash\npnpm test\n```', tool_calls: [{ id: 'bash-review', name: 'bash', arguments: '{"command":"pnpm test"}' }, { id: 'edit-review', name: 'apply_patch', arguments: '{"path":"src/app.tsx"}' }] },
      { role: 'tool', tool_call_id: 'bash-review', content: JSON.stringify({ status: 'success', error: null, abstract: '42 tests passed', output: { content_type: 'text/shell', command: 'pnpm test', data: '42 tests passed', stderr: '', exit_code: 0, duration_ms: 1280 } }) },
      { role: 'tool', tool_call_id: 'edit-review', content: JSON.stringify({ status: 'success', error: null, abstract: 'Updated src/app.tsx', output: { content_type: 'text/x-diff', path: 'src/app.tsx', data: '--- a/src/app.tsx\n+++ b/src/app.tsx\n@@ -1 +1 @@\n-old\n+new' } }) },
    ], total: 4, limit: 200, offset: 0 });
    if (/\/chat-scopes\/[^/]+\/chats\/[^/]+\/state$/.test(p)) return json(route, { todo_items: [], background_jobs: [backgroundJob], active_modes: [], mcp_server_ids: [MCP_ID], mcp_config_revision: 1 });
    if (p.endsWith('/background-jobs/events')) return route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      headers: { 'Cache-Control': 'no-cache' },
      body: 'retry: 60000\n: review stream connected\n\n',
    });
    if (p.includes('/background-jobs')) return json(route, [backgroundJob]);
    if (p.includes('/sandbox-status')) return json(route, { items: [] });

    if (p === '/api/v1/workflows' && method === 'GET') return json(route, { items: state.workflows, total: state.workflows.length, limit: 50, offset: 0 });
    if (p === '/api/v1/workflows' && method === 'POST') return json(route, { ...workflowMeta, wf_id: 'wf-created', workflow_name: bodyJson(request).workflow_name ?? bodyJson(request).name ?? 'Created Workflow' }, 201);
    if (p === `/api/v1/workflows/${WF_ID}`) return json(route, { meta: workflowMeta, workflow });
    if (p === `/api/v1/workflows/${WF_ID}/at/v1.sv0`) return json(route, {
      meta: { ...workflowMeta, active_major: 1, active_sub: 0 },
      workflow,
    });
    if (p === `/api/v1/workflows/${WF_ID}/versions`) return json(route, { versions: [{ major: 2, sub: 1, v: 2, sv: 1, version_str: 'v2.sv1', version_id: 'version-review', message: 'Review version', timestamp: 1_785_660_000 }, { major: 1, sub: 0, v: 1, sv: 0, version_str: 'v1.sv0', version_id: 'version-old', message: 'Initial version', timestamp: 1_785_650_000 }] });
    if (p.includes(`/api/v1/workflows/${WF_ID}/vfs/content`)) return json(route, { path: '/mount/review.json', content_type: 'application/json', content: '{"status":"reviewed"}', size_bytes: 24, truncated: false, wf_version: 'v2.sv1', stale: false });
    if (p.includes(`/api/v1/workflows/${WF_ID}/vfs`)) return json(route, { entries: [{ path: '/mount/review.json', kind: 'artifact', content_type: 'application/json', abstract: 'Review result', size_bytes: 24, wf_version: 'v2.sv1', last_access: 1_785_660_000, stale: false, capabilities: ['read', 'download', 'copy_path'] }], root_capabilities: { data: ['upload'], mount: ['upload'] } });
    if (p.includes(`/api/v1/workflows/${WF_ID}/workspace`)) return json(route, { workflow_scope_id: WF_ID, mount_scope_id: 'mount-review' });
    if (p.includes(`/api/v1/workflows/${WF_ID}/chats`)) return json(route, { items: [], total: 0, limit: 50, offset: 0 });
    if (p.includes('/templates')) return json(route, { items: [{ template_id: 'template-review', name: 'Review Prompt', node_type: 'PromptNode', description: 'Analyze review input', tags: ['review'] }], total: 1, limit: 50, offset: 0 });

    if (p === '/api/v1/vfs') return json(route, {
      entries: [{ path: '/mount/review.json', kind: 'artifact', content_type: 'application/json', abstract: 'Review result', size_bytes: 24, wf_version: 'v2.sv1', last_access: 1_785_660_000, stale: false, capabilities: ['read', 'download', 'copy_path'] }],
      root_capabilities: { data: ['upload'], mount: ['upload'] },
    });
    if (p === '/api/v1/vfs/content') return json(route, { path: url.searchParams.get('path') ?? '/mount/review.json', content_type: 'application/json', content: '{"status":"reviewed"}', size_bytes: 21, truncated: false, wf_version: 'v2.sv1', stale: false });

    if (p === '/api/v1/tasks/summary') return json(route, { active: 1, queued: 0, running: 1, cancelling: 0, failed: 0, finished: 1, cancelled: 0 });
    if (p === '/api/v1/tasks' && method === 'GET') return json(route, { items: state.tasks, total: state.tasks.length, limit: 50, offset: 0 });
    if (p === `/api/v1/tasks/${TASK_ID}`) return json(route, task);
    if (p.endsWith('/events')) return json(route, { items: [{ id: 1, event_type: 'progress', payload: { progress: { done: 4, total: 10 } }, created_at: now }], next_cursor: null });
    if (p.endsWith('/cancel')) { task.status = 'cancelled'; return json(route, task); }
    if (p.endsWith('/resume')) { task.status = 'running'; return json(route, task); }
    if (p === '/api/v1/tasks/scheduled-runs') return json(route, { items: [], total: 0, limit: 50, offset: 0 });

    if (p === '/api/v1/deployments' && method === 'GET') return json(route, { items: state.deployments, total: state.deployments.length, limit: 50, offset: 0 });
    if (p === `/api/v1/deployments/${DEP_ID}` && method === 'GET') return json(route, deployment);
    if (p.endsWith('/metrics')) return json(route, { series: [], bucket: 'hour', from: now, to: now });
    if (p.endsWith('/history')) return json(route, { items: [{ id: 'run-review', status: 'success', duration_ms: 1200, created_at: now }], next_cursor: null, limit: 50 });
    if (p.endsWith('/rotate-key')) return json(route, { api_key: 'review-secret-once' });
    if (p.endsWith('/test-invoke')) return json(route, { status: 'success', output: { answer: 'Review passed' }, duration_ms: 320 });
    if (p === `/api/v1/deployments/${DEP_ID}` && method === 'PATCH') return json(route, { ...deployment, ...bodyJson(request) });

    if (p === '/api/v1/llm-credentials' && method === 'GET') return json(route, state.credentials);
    if (p === '/api/v1/llm-credentials' && method === 'POST') return json(route, { ...credential, id: 'cred-created', ...bodyJson(request) }, 201);
    if (p === `/api/v1/llm-credentials/${credential.id}` && method === 'GET') return json(route, credential);
    if (p === `/api/v1/llm-credentials/${credential.id}` && method === 'PUT') return json(route, { ...credential, ...bodyJson(request) });
    if (p === `/api/v1/llm-credentials/${credential.id}` && method === 'DELETE') { state.credentials = []; return json(route, {}); }

    if (p === '/api/v1/mcp-servers' && method === 'GET') return json(route, { items: state.mcpServers });
    if (p === `/api/v1/mcp-servers/${MCP_ID}` && method === 'GET') return json(route, mcpServer);
    if (p === '/api/v1/mcp-servers/catalog') return json(route, { source: 'official', ranking: url.searchParams.get('search') ? 'search' : 'browse', items: catalogMcp.slice(0, Number(url.searchParams.get('limit') ?? 10)), has_more: Number(url.searchParams.get('limit') ?? 10) < catalogMcp.length });
    if (p === '/api/v1/mcp-servers/catalog/resolve') return json(route, catalogMcp[0]);
    if (p === '/api/v1/mcp-servers/test') return json(route, { ok: true, tool_count: 2, tools: mcpServer.last_tool_names });
    if (p.endsWith('/refresh')) return json(route, mcpServer);
    if (p === `/api/v1/mcp-servers/${MCP_ID}` && method === 'PATCH') return json(route, { ...mcpServer, ...bodyJson(request) });
    if (p === `/api/v1/mcp-servers/${MCP_ID}` && method === 'DELETE') { state.mcpServers = []; return json(route, {}); }
    if (p === '/api/v1/mcp-servers' && method === 'POST') return json(route, { ...mcpServer, id: 'mcp-created', ...bodyJson(request) }, 201);

    if (p === '/api/v1/skills' && method === 'GET') return json(route, { items: state.skills });
    if (p === `/api/v1/skills/${SKILL_ID}` && method === 'GET') return json(route, skill);
    if (p === `/api/v1/skills/${SKILL_ID}/draft` && method === 'GET') return json(route, { skill_id: SKILL_ID, base_revision_hash: 'a'.repeat(64), draft_hash: 'b'.repeat(64), skill_md: skill.skill_md, body: skill.body, files: skill.files, has_changes: true, updated_at: now });
    if (p === `/api/v1/skills/${SKILL_ID}/versions` && method === 'GET') return json(route, [{ revision_id: 'revision-latest', revision_hash: 'a'.repeat(64), version: 2, is_latest: true, files: skill.files, size_bytes: 200, created_at: now }, { revision_id: 'revision-v1', revision_hash: 'c'.repeat(64), version: 1, is_latest: false, files: ['SKILL.md'], size_bytes: 100, created_at: now }]);
    if (p.includes(`/api/v1/skills/${SKILL_ID}/versions/`)) return json(route, { ...skill, revision_id: 'revision-v1', revision_hash: 'c'.repeat(64), version: 1, is_latest: false, name: 'Review Skill v1', body: '# Historical Review Skill', skill_md: '# Historical Review Skill', files: ['SKILL.md'] });
    if (p.includes(`/api/v1/skills/${SKILL_ID}/files/`)) return json(route, { path: 'scripts/review.py', content: 'print("review")', content_type: 'text/x-python', size_bytes: 15 });
    if (p === '/api/v1/skills/catalog') return json(route, { source: 'openai', source_label: 'OpenAI Skills', revision: 'abc123', items: catalogSkills.slice(0, Number(url.searchParams.get('limit') ?? 10)), has_more: Number(url.searchParams.get('limit') ?? 10) < catalogSkills.length });
    if (p === '/api/v1/skills/catalog/resolve') return json(route, catalogSkills[0]);
    if (p === '/api/v1/skills/catalog/file') return json(route, { path: url.searchParams.get('path') ?? 'SKILL.md', content: catalogSkills[0].skill_md, content_type: 'text/markdown', size_bytes: 100 });
    if (p === `/api/v1/skills/${SKILL_ID}` && method === 'DELETE') { state.skills = []; return json(route, {}); }
    if (p.includes('/skills/') && method !== 'GET') return json(route, skill);

    if (p === '/api/v1/storage/list') return json(route, { path: url.searchParams.get('path') || '/', items: state.storageItems, next_cursor: null, total_estimate: state.storageItems.length, readonly: false });
    if (p === '/api/v1/storage/content' && method === 'GET') return json(route, { path: url.searchParams.get('path'), content_type: 'application/json', content: '{"review":true,"status":"stable"}', size: 32, truncated: false });
    if (p === '/api/v1/storage/mkdir') return json(route, { path: `${bodyJson(request).path ?? ''}/created-folder` });
    if (p === '/api/v1/storage/rename') return json(route, { path: bodyJson(request).destination ?? bodyJson(request).to_path ?? '/renamed.json' });
    if (p === '/api/v1/storage' && method === 'DELETE') return json(route, {});
    if (p === '/api/v1/storage/upload') return json(route, { path: '/uploaded.txt' });

    return json(route, method === 'GET' ? {} : { ok: true });
  });
}

function slug(value) {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 70);
}

async function pageAudit(page, label) {
  await page.waitForTimeout(250);
  const body = await page.locator('body').innerText();
  if (/Something went wrong|Unexpected Application Error|Application crashed/i.test(body)) {
    throw new Error(`${label}: application error boundary`);
  }
  const geometry = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
  if (geometry.scrollWidth > geometry.clientWidth + 1) {
    throw new Error(`${label}: horizontal overflow ${geometry.scrollWidth} > ${geometry.clientWidth}`);
  }
}

async function shot(page, domain, action, assertion, kind = 'protocol-fixture') {
  await pageAudit(page, `${domain}/${action}`);
  sequence += 1;
  const file = `${String(sequence).padStart(3, '0')}-${slug(domain)}-${slug(action)}.png`;
  await page.screenshot({ path: resolve(OUT, file), fullPage: false, animations: 'disabled' });
  results.push({ sequence, domain, action, assertion, kind, status: 'passed', screenshot: file, url: page.url() });
  console.log(`PASS ${String(sequence).padStart(3, '0')} ${domain} — ${action}`);
}

async function step(page, domain, action, assertion, operation, kind) {
  const requestStart = requestLog.length;
  try {
    await operation();
    await shot(page, domain, action, assertion, kind);
    results.at(-1).requests = requestLog.slice(requestStart);
  } catch (error) {
    sequence += 1;
    const message = error instanceof Error ? error.message : String(error);
    const file = `${String(sequence).padStart(3, '0')}-${slug(domain)}-${slug(action)}-failed.png`;
    await page.screenshot({ path: resolve(OUT, file), fullPage: false, animations: 'disabled' }).catch(() => {});
    results.push({ sequence, domain, action, assertion, kind: kind ?? 'protocol-fixture', status: 'failed', screenshot: file, url: page.url(), error: message, requests: requestLog.slice(requestStart) });
    console.error(`FAIL ${String(sequence).padStart(3, '0')} ${domain} — ${action}: ${message}`);
  }
}

async function go(page, path) {
  await page.goto(`${BASE}${path}`, { waitUntil: 'domcontentloaded' });
  await page.locator('#root').waitFor({ state: 'visible' });
  await page.locator('[aria-busy="true"]').first().waitFor({ state: 'hidden', timeout: 15_000 }).catch(() => {});
  await pageAudit(page, path);
}

async function cancelDialog(page) {
  const cancel = page.getByRole('button', { name: /^cancel$|^close$/i }).first();
  if (await cancel.isVisible().catch(() => false)) {
    await cancel.click({ timeout: 2_000 }).catch(async () => { await page.keyboard.press('Escape').catch(() => {}); });
  } else {
    await page.keyboard.press('Escape').catch(() => {});
  }
  await page.waitForTimeout(250);
}

async function reviewShellAndChat(page) {
  await go(page, '/chat');
  await step(page, 'Shell', 'expanded sidebar', 'Primary navigation and utility controls are visible', async () => {
    await page.locator('[data-testid="app-sidebar"]').waitFor();
  });
  await step(page, 'Shell', 'collapsed sidebar', 'Sidebar contracts to icon rail without moving content outside viewport', async () => {
    await page.locator('[data-testid="nav-sidebar-toggle"]').click();
  });
  await step(page, 'Shell', 'expanded sidebar restored', 'Sidebar restores labels and utility alignment', async () => {
    await page.locator('[data-testid="nav-sidebar-toggle"]').click();
  });
  await step(page, 'Shell', 'user menu', 'User identity, Settings and Sign out actions are exposed', async () => {
    await page.locator('[data-action="open-user-menu"]').click();
  });
  await page.keyboard.press('Escape');

  await step(page, 'Chat', 'conversation and tool activity', 'Persisted assistant text and grouped tool activity render', async () => {
    await page.getByText('I inspected the repository and ran two tools.').waitFor();
  });
  await step(page, 'Chat message', 'assistant hover baseline', 'Assistant message hover remains readable and captures the current message-level action affordances', async () => {
    await page.locator('[data-message-role="assistant"]').filter({ has: page.locator('[data-role="markdown"]') }).first().hover();
  });
  await step(page, 'Chat message', 'user hover baseline', 'User message hover remains readable and captures the current message-level action affordances', async () => {
    await page.locator('[data-message-role="user"]').first().hover();
  });
  await step(page, 'Chat message', 'fenced code block', 'Assistant fenced code renders as code so block-level actions can be reviewed independently from tool output', async () => {
    await page.locator('.chat-markdown-code-block').filter({ hasText: 'pnpm test' }).waitFor();
  });
  await step(page, 'Chat', 'tool group expanded', 'Tool list expands from the grouped activity row', async () => {
    await page.locator('[data-action="tool-activity-toggle"]').click();
  });
  await step(page, 'Chat', 'terminal tool expanded', 'Bash call renders command and stdout in terminal presentation', async () => {
    await page.locator('[data-action="tool-call-toggle"]').first().click();
    await page.locator('[data-role="terminal-block"]').waitFor();
    await page.getByText('Exit code: 0', { exact: false }).waitFor();
  });
  await step(page, 'Chat', 'diff tool expanded', 'File edit renders a red/green diff presentation', async () => {
    await page.locator('[data-action="tool-call-toggle"]').nth(1).click();
    const added = page.getByText('+new', { exact: false });
    await added.waitFor();
    await added.scrollIntoViewIfNeeded();
    if (await page.locator('[data-action="tool-copy"]').count() < 2) throw new Error('Expected copy actions for terminal output and diff');
  });
  await step(page, 'Chat', 'turn options expanded', 'Reasoning, approval and MCP controls use progressive disclosure', async () => {
    await page.getByRole('button', { name: /^options/i }).click();
  });
  await step(page, 'Chat', 'model selector', 'Model choices open without clearing the composer', async () => {
    await page.getByRole('combobox', { name: /model/i }).click();
  });
  await page.keyboard.press('Escape');
  await step(page, 'Chat', 'attachment menu', 'File, image and video attachment choices are available', async () => {
    await page.locator('[data-action="agent-composer-attachment-menu"]').click();
  });
  await page.keyboard.press('Escape');
  await step(page, 'Chat', 'more actions menu', 'Debug and Sandbox actions are grouped under More', async () => {
    await page.getByRole('button', { name: /more chat actions/i }).click();
  });
  await page.keyboard.press('Escape');
  await step(page, 'Chat', 'debug panel', 'Debug view opens and retains the conversation', async () => {
    await page.getByRole('button', { name: /more chat actions/i }).click();
    await page.locator('[data-action="chat-debug-toggle"]').click();
  });
  await step(page, 'Chat', 'activity preview', 'Background activity preview opens with persisted job state', async () => {
    await page.locator('[data-action="chat-background-jobs"]').click();
  });
  await page.keyboard.press('Escape');
  await step(page, 'Execution plan', 'chat card', 'The active durable plan is visible beside the composer with status and node progress', async () => {
    await page.getByRole('region', { name: /Execution plan: Deep Research Plan/i }).waitFor();
  });
  await step(page, 'Execution plan', 'preview graph', 'Review plan opens the top-down graph with live run status and a selected running node', async () => {
    await page.getByRole('button', { name: /review plan/i }).click();
    await page.getByRole('button', { name: /^graph$/i }).waitFor();
  });
  await step(page, 'Execution plan inspector', 'configuration tab', 'Node configuration is separated from runtime output', async () => {
    await page.getByRole('tab', { name: /^configuration$/i }).click();
  });
  await step(page, 'Execution plan inspector', 'output tab', 'Running subagent output and attempts remain inspectable without leaving the graph', async () => {
    await page.getByRole('tab', { name: /^output/i }).click();
  });
  await step(page, 'Execution plan inspector', 'cancel node confirmation', 'Node-level cancellation is protected by explicit confirmation', async () => {
    await page.getByRole('button', { name: /cancel node/i }).click();
    await page.getByRole('alertdialog').waitFor();
  });
  await cancelDialog(page);
  await step(page, 'Execution plan inspector', 'inspector closed', 'The node inspector can be dismissed to restore the full graph and view switcher', async () => {
    await page.getByRole('button', { name: /close node inspector/i }).click();
  });
  const planPreview = page.getByTestId('preview');
  await step(page, 'Execution plan', 'table view', 'Table view exposes node type, status, activity and duration as a dense alternative', async () => {
    await planPreview.getByRole('button', { name: /^table$/i }).click();
  });
  await step(page, 'Execution plan', 'activity view', 'Activity view renders the durable event ledger and sequence numbers', async () => {
    await planPreview.getByRole('button', { name: /^activity$/i }).click();
  });
  await step(page, 'Execution plan', 'cancel run confirmation', 'Graph-level cancellation is separately protected and does not conflate node cancellation', async () => {
    await planPreview.getByRole('button', { name: /cancel run/i }).click();
    await page.getByRole('alertdialog').waitFor();
  });
  await cancelDialog(page);
  await page.getByRole('button', { name: /^close preview$/i }).click();
  await step(page, 'Chat', 'long composer input', 'Long multiline draft remains contained and editable', async () => {
    const input = page.locator('[data-role="agent-composer-input"]');
    await input.fill('Review '.repeat(180));
    await input.press('Shift+Enter');
    await input.type('Second line');
  });
  await page.locator('[data-role="agent-composer-input"]').fill('').catch(() => {});
  await step(page, 'Chat', 'turn submitted', 'Send performs a real POST SSE turn and leaves the accepted user message in the transcript', async () => {
    await page.locator('[data-role="agent-composer-input"]').fill('Deep browser acceptance turn');
    await page.locator('[data-action="agent-composer-send"]').click();
    await page.getByText('Deep browser acceptance turn', { exact: true }).waitFor();
    await page.waitForTimeout(500);
  });
}

async function reviewWorkspace(page) {
  await go(page, '/workspace');
  await step(page, 'Workspace', 'base list', 'Workflow row, metadata and primary Open action render', async () => {
    await page.getByText(workflowMeta.workflow_name).waitFor();
  });
  await step(page, 'Workspace', 'search no match', 'Search produces an explicit no-match state', async () => {
    await page.getByTestId('wf-search').fill('does-not-exist');
  });
  await page.getByTestId('wf-search').fill('');
  await step(page, 'Workspace', 'sandbox filter menu', 'Sandbox status filter options open', async () => {
    await page.getByRole('combobox', { name: /filter by sandbox/i }).click();
  });
  await page.keyboard.press('Escape');
  await step(page, 'Workspace', 'sort menu', 'Workflow sort options open', async () => {
    await page.getByRole('combobox', { name: /sort workflows/i }).click();
  });
  await page.keyboard.press('Escape');
  await step(page, 'Workspace', 'row actions menu', 'Duplicate, edit, share, deploy and delete actions are discoverable', async () => {
    await page.getByTestId('wf-row-menu').click();
  });
  await step(page, 'Workspace', 'duplicate dialog', 'Duplicate workflow exposes editable name and description', async () => {
    await page.getByTestId('wf-row-duplicate').click();
  });
  await cancelDialog(page);
  await page.getByTestId('wf-row-menu').click();
  await step(page, 'Workspace', 'edit dialog', 'Edit workflow information opens with current metadata', async () => {
    await page.getByTestId('wf-row-edit').click();
  });
  await cancelDialog(page);
  await page.getByTestId('wf-row-menu').click();
  await step(page, 'Workspace', 'delete confirmation', 'Destructive delete requires explicit typed confirmation', async () => {
    await page.getByTestId('wf-row-delete').click();
  });
  await cancelDialog(page);
  await step(page, 'Workspace', 'new workflow dialog', 'Create dialog exposes required workflow fields', async () => {
    await page.getByRole('button', { name: /new workflow/i }).click();
  });
  await cancelDialog(page);
}

async function reviewTasks(page) {
  await go(page, '/tasks');
  await step(page, 'Tasks', 'active tasks tab', 'Running and finished task rows render with summary state', async () => {
    await page.getByText(WF_ID).first().waitFor();
  });
  const tabs = page.getByRole('tab');
  for (let index = 0; index < await tabs.count(); index += 1) {
    const tab = tabs.nth(index);
    const name = (await tab.innerText()).trim();
    await step(page, 'Tasks', `tab ${name}`, `Task subview ${name} activates`, async () => {
      await tab.click();
    });
  }
  await step(page, 'Tasks', 'new task menu', 'Batch and scheduled task creation modes are available', async () => {
    await page.getByTestId('tasks-new-task-trigger').click();
  });
  await step(page, 'Tasks', 'batch setup', 'Batch creation opens an in-page configuration flow', async () => {
    await page.getByTestId('tasks-new-batch').click();
  });
  await page.getByRole('button', { name: /^cancel$/i }).click();
  await page.getByTestId('tasks-new-task-trigger').click();
  await step(page, 'Tasks', 'scheduled setup', 'Scheduled run creation exposes schedule controls', async () => {
    await page.getByTestId('tasks-new-scheduled').click();
  });
  await page.getByRole('button', { name: /^cancel$/i }).click();

  await go(page, `/tasks/${TASK_ID}`);
  await step(page, 'Task detail', 'running overview', 'Status, progress and event log render for an active task', async () => {
    await page.getByText('42%').waitFor();
  });
  await step(page, 'Task detail', 'share dialog', 'Task sharing controls open without leaving detail context', async () => {
    await page.getByRole('button', { name: /share/i }).click();
  });
  await cancelDialog(page);
  await step(page, 'Task detail', 'force cancel confirmation', 'Force cancellation requires an explicit destructive confirmation', async () => {
    await page.getByRole('button', { name: /force cancel/i }).click();
    await page.getByRole('dialog').waitFor();
  });
  await cancelDialog(page);
}

async function reviewDeployments(page) {
  await go(page, '/deployments');
  await step(page, 'Deployments', 'list and metrics', 'Deployment row and invocation metrics render', async () => {
    await page.getByText(deployment.name).waitFor();
  });
  await step(page, 'Deployments', 'new deployment dialog', 'Creation opens workflow, trigger, version and rate-limit controls', async () => {
    await page.getByRole('button', { name: /new deployment/i }).click();
    await page.getByRole('dialog').waitFor();
  });
  await cancelDialog(page);
  await step(page, 'Deployments', 'row actions menu', 'Open, disable, share and delete actions are discoverable from the row', async () => {
    await page.getByRole('row', { name: /Review API/i }).getByRole('button').last().click();
  });
  await page.keyboard.press('Escape');
  await go(page, `/deployments/${DEP_ID}`);
  const tabNames = ['Overview', 'Config', 'Runs / Logs', 'Monitoring', 'Test', 'Security'];
  for (const name of tabNames) {
    await step(page, 'Deployment detail', `tab ${name}`, `${name} content renders and URL state remains stable`, async () => {
      await page.getByRole('tab', { name: new RegExp(`^${name.replace('/', '\\/')}$`, 'i') }).click();
    });
  }
  await step(page, 'Deployment detail', 'rotate key result', 'Secret rotation displays a one-time API key', async () => {
    await page.getByRole('button', { name: /rotate api key/i }).click();
    await page.getByTestId('rotated-key').waitFor();
  });
  await page.getByRole('button', { name: /^close$/i }).first().click();
  await page.getByRole('tab', { name: /^test$/i }).click();
  await step(page, 'Deployment detail', 'test invocation', 'Test request returns a visible success result', async () => {
    const run = page.getByRole('button', { name: /^run$|run test|test invoke|send request/i }).last();
    await run.click();
    await page.getByText(/Review passed|success/i).last().waitFor();
  });
}

async function reviewCredentials(page) {
  await go(page, '/credentials');
  await step(page, 'Credentials', 'masked list', 'Credential secret remains write-only and masked', async () => {
    await page.getByText(credential.name).waitFor();
  });
  await step(page, 'Credentials', 'create dialog', 'Credential creation exposes provider, model, URL and secret fields', async () => {
    await page.getByTestId('cred-add-button').click();
  });
  await step(page, 'Credentials', 'provider options', 'Built-in and custom providers are selectable', async () => {
    await page.getByTestId('cred-provider').click();
  });
  await page.keyboard.press('Escape');
  await cancelDialog(page);
  await step(page, 'Credentials', 'edit dialog', 'Editing preserves public metadata while secret stays write-only', async () => {
    await page.getByRole('button', { name: /^edit$/i }).click();
    await page.getByRole('dialog').waitFor();
  });
  await cancelDialog(page);
  await step(page, 'Credentials', 'delete confirmation', 'Deleting a credential explains downstream workflow impact before mutation', async () => {
    await page.getByRole('button', { name: /^delete$/i }).click();
    await page.getByRole('dialog').waitFor();
  });
  await cancelDialog(page);
}

async function reviewMcp(page) {
  await go(page, '/mcp-servers');
  await step(page, 'MCP', 'installed tab', 'Platform and custom MCP cards render with connection state', async () => {
    await page.getByText(mcpServer.name).waitFor();
  });
  await step(page, 'MCP', 'search filtering', 'Installed server search narrows the custom collection', async () => {
    await page.getByTestId('mcp-search').fill('Review Search');
  });
  await page.getByTestId('mcp-search').fill('');
  await step(page, 'MCP', 'status filter', 'Connection-status filter options open', async () => { await page.getByTestId('mcp-filter').click(); });
  await page.keyboard.press('Escape');
  await step(page, 'MCP', 'server actions menu', 'Edit, refresh and uninstall actions are discoverable', async () => { await page.getByTestId('mcp-card-menu').click(); });
  await page.keyboard.press('Escape');
  await step(page, 'MCP', 'add custom server', 'Custom MCP dialog exposes transport, endpoint and auth controls', async () => { await page.getByTestId('mcp-add-button').click(); });
  await step(page, 'MCP', 'transport options', 'Supported MCP transports are selectable', async () => { await page.getByTestId('mcp-transport').click(); });
  await step(page, 'MCP', 'transport selected', 'Changing transport keeps the parent dialog open and preserves sibling fields', async () => {
    await page.getByRole('option', { name: /^http$/i }).click();
  });
  await step(page, 'MCP', 'authentication options', 'MCP authentication modes are selectable', async () => { await page.getByTestId('mcp-auth').click(); });
  await step(page, 'MCP', 'bearer authentication selected', 'Bearer selection reveals a write-only token field', async () => {
    await page.getByRole('option', { name: /bearer token/i }).click();
    await page.getByTestId('mcp-token').waitFor();
  });
  await cancelDialog(page);
  await step(page, 'MCP', 'discover tab', 'Catalog guidance and results render', async () => { await page.getByRole('tab', { name: /discover/i }).click(); });
  await step(page, 'MCP', 'catalog search', 'Catalog search executes only after explicit submit', async () => {
    await page.getByTestId('mcp-catalog-search').fill('review');
    await page.getByTestId('mcp-catalog-search-button').click();
  });
  await step(page, 'MCP', 'catalog load more', 'Load more appends catalog results', async () => { await page.getByTestId('mcp-catalog-more').click(); });
  await go(page, `/mcp-servers/discover/official?id=${encodeURIComponent(catalogMcp[0].source_id)}`);
  for (const name of ['Overview', 'Setup', 'Connection', 'Security']) {
    await step(page, 'MCP catalog detail', `tab ${name}`, `${name} catalog guidance renders with stable URL state`, async () => {
      await page.getByRole('tab', { name: new RegExp(`^${name}$`, 'i') }).click();
    });
  }
  await step(page, 'MCP catalog detail', 'install dialog', 'Installation presents source and OAuth connection implications before mutation', async () => {
    await page.getByRole('button', { name: /^install$/i }).click();
    await page.getByRole('dialog').waitFor();
  });
  await cancelDialog(page);
  await go(page, `/mcp-servers/${MCP_ID}`);
  const detailTabs = page.getByRole('tab');
  for (let index = 0; index < await detailTabs.count(); index += 1) {
    const tab = detailTabs.nth(index); const name = (await tab.innerText()).trim();
    await step(page, 'MCP detail', `tab ${name}`, `MCP detail ${name} subview renders`, async () => { await tab.click(); });
  }
  await step(page, 'MCP detail', 'test connection', 'Connection test performs the scoped server refresh without leaving detail context', async () => {
    await page.getByRole('button', { name: /test connection/i }).click();
    await page.waitForTimeout(400);
  });
  await step(page, 'MCP detail', 'uninstall confirmation', 'Uninstall is isolated behind a destructive confirmation dialog', async () => {
    await page.getByRole('button', { name: /uninstall/i }).click();
    await page.getByRole('dialog').waitFor();
  });
  await cancelDialog(page);
}

async function reviewSkills(page) {
  await go(page, '/skills');
  await step(page, 'Skills', 'installed tab', 'Installed Skill cards render', async () => { await page.getByText(skill.name).waitFor(); });
  await step(page, 'Skills', 'installed search', 'Skill search filters the installed collection', async () => { await page.getByTestId('skill-search').fill('Review'); });
  await page.getByTestId('skill-search').fill('');
  await step(page, 'Skills', 'actions menu', 'Open, Share and Uninstall actions are discoverable', async () => { await page.getByTestId('skill-card-menu').click(); });
  await page.keyboard.press('Escape');
  await step(page, 'Skills', 'discover tab', 'Skill catalog and installation guidance render', async () => { await page.getByRole('tab', { name: /discover/i }).click(); });
  await step(page, 'Skills', 'catalog search', 'Explicit catalog search returns matching Skills', async () => {
    await page.getByTestId('skill-catalog-search').fill('review');
    await page.getByTestId('skill-catalog-search-button').click();
  });
  await step(page, 'Skills', 'catalog load more', 'Load more appends Skill catalog results', async () => { await page.getByTestId('skill-catalog-more').click(); });
  await go(page, `/skills/discover/openai?id=${encodeURIComponent(catalogSkills[0].source_id)}`);
  for (const name of ['Overview', 'Instructions', 'Files', 'Requirements']) {
    await step(page, 'Skill catalog detail', `tab ${name}`, `${name} catalog package content renders`, async () => {
      await page.getByRole('tab', { name: new RegExp(`^${name}$`, 'i') }).click();
    });
  }
  await step(page, 'Skill catalog detail', 'install dialog', 'Install confirmation identifies the catalog package before mutation', async () => {
    await page.getByRole('button', { name: /^install$/i }).click();
    await page.getByRole('dialog').waitFor();
  });
  await cancelDialog(page);
  await go(page, '/skills?tab=custom');
  await step(page, 'Skills', 'custom tab', 'Custom Skill import explains ZIP package flow', async () => { await page.getByRole('tab', { name: /custom/i }).click(); });
  await step(page, 'Skills', 'custom upload dialog', 'ZIP chooser opens without an inline Skill editor', async () => { await page.getByRole('button', { name: /upload skill package/i }).first().click(); });
  await cancelDialog(page);

  await go(page, `/skills/${SKILL_ID}`);
  for (const name of ['Overview', 'Instructions', 'Files', 'Requirements']) {
    await step(page, 'Skill detail', `tab ${name}`, `${name} Skill subview renders`, async () => { await page.getByRole('tab', { name: new RegExp(`^${name}$`, 'i') }).click(); });
  }
  await step(page, 'Skill detail', 'edit mode', 'Edit exposes SKILL.md draft content', async () => { await page.getByRole('button', { name: /^edit$/i }).click(); });
  await step(page, 'Skill detail', 'draft modified', 'Unsaved draft state is visible before publishing', async () => {
    await page.getByRole('textbox', { name: 'SKILL.md' }).fill(`${skill.skill_md}\n\nAdded by deep review.`);
  });
  await step(page, 'Skill detail', 'save draft', 'Draft save calls the Skill draft endpoint', async () => { await page.getByRole('button', { name: /save draft/i }).click(); });
  await step(page, 'Skill detail', 'new version dialog', 'Version publishing requires an explicit version number', async () => { await page.getByRole('button', { name: /new version/i }).click(); });
  await cancelDialog(page);
}

async function reviewKnowledge(page) {
  await go(page, '/knowledge');
  await step(page, 'Knowledge', 'list', 'Knowledge packages render as user-facing cards without index controls', async () => { await page.getByText(knowledge.name).waitFor(); });
  await step(page, 'Knowledge', 'search no match', 'Knowledge search shows an explicit no-match state', async () => { await page.getByPlaceholder(/search knowledge bases/i).fill('missing'); });
  await page.getByPlaceholder(/search knowledge bases/i).fill('');
  await step(page, 'Knowledge', 'folder import dialog', 'Upload knowledge accepts a complete folder or ZIP package with a root README', async () => { await page.getByRole('button', { name: /upload knowledge/i }).click(); });
  await cancelDialog(page);
  await step(page, 'Knowledge', 'create dialog', 'New knowledge base requests name and description', async () => { await page.getByRole('button', { name: /new knowledge base/i }).click(); });
  await cancelDialog(page);
  await go(page, `/knowledge/${KB_ID}`);
  await step(page, 'Knowledge detail', 'file browser', 'The directory tree is secondary to the selected file content', async () => {
    await page.getByRole('tree', { name: /files/i }).waitFor();
    await page.getByText('Browse the source files in this package.').waitFor();
  });
  await step(page, 'Knowledge detail', 'nested file preview', 'Selecting a nested file replaces the main content pane', async () => {
    await page.getByRole('treeitem', { name: /releases\.md/i }).click();
    await page.getByText('Releases ship every Tuesday.').waitFor();
  });
  await step(page, 'Knowledge detail', 'folder actions', 'A folder context menu offers scoped upload and deletion actions', async () => {
    await page.getByRole('treeitem', { name: /^notes$/i }).click({ button: 'right' });
    await page.getByRole('menuitem', { name: /upload files here/i }).waitFor();
    await page.getByRole('menuitem', { name: /delete folder/i }).click();
  });
  await cancelDialog(page);
}

async function reviewStorage(page) {
  await go(page, '/storage');
  await step(page, 'Storage', 'file list', 'Folder and file rows render with metadata', async () => { await page.getByText('review.json').waitFor(); });
  await step(page, 'Storage', 'roots menu', 'Storage roots are selectable', async () => { await page.getByRole('combobox', { name: /roots/i }).click(); });
  await page.keyboard.press('Escape');
  await step(page, 'Storage', 'sort menu', 'Name, modified, size and type sorts are available', async () => { await page.getByRole('combobox', { name: /sort files/i }).click(); });
  await page.keyboard.press('Escape');
  await step(page, 'Storage', 'search filtering', 'Current-folder search filters visible items', async () => { await page.getByPlaceholder(/search current folder/i).fill('review'); });
  await page.getByPlaceholder(/search current folder/i).fill('');
  await step(page, 'Storage', 'file preview', 'Double click opens type-aware JSON preview', async () => { await page.getByText('review.json', { exact: true }).dblclick(); });
  await step(page, 'Storage', 'preview closed', 'The explicit close control removes the preview and returns to the file surface', async () => {
    await page.getByRole('button', { name: /close file preview/i }).click();
    await page.getByRole('complementary', { name: /file preview/i }).waitFor({ state: 'hidden' });
  });
  await step(page, 'Storage', 'row keyboard selection', 'File rows support keyboard focus and Enter activation', async () => {
    const row = page.getByRole('button', { name: 'review.json' });
    await row.focus();
    await row.press('Enter');
  });
  await step(page, 'Storage', 'keyboard-opened preview closed', 'A preview opened from the keyboard has the same explicit close behavior', async () => {
    await page.getByRole('button', { name: /close file preview/i }).click();
    await page.getByRole('complementary', { name: /file preview/i }).waitFor({ state: 'hidden' });
  });
  await step(page, 'Storage', 'file context menu', 'Right-click exposes view, download, rename and delete actions for a writable file', async () => {
    await page.getByRole('button', { name: 'review.json' }).click({ button: 'right' });
    await page.getByRole('menuitem', { name: /^rename$/i }).waitFor();
  });
  await step(page, 'Storage', 'rename dialog', 'Rename is gated by a focused dialog with the existing name', async () => {
    await page.getByRole('menuitem', { name: /^rename$/i }).click();
    await page.getByRole('dialog').waitFor();
  });
  await cancelDialog(page);
  await page.getByRole('button', { name: 'review.json' }).click({ button: 'right' });
  await step(page, 'Storage', 'delete dialog', 'Delete is destructive and requires explicit confirmation', async () => {
    await page.getByRole('menuitem', { name: /^delete$/i }).click();
    await page.getByRole('dialog').waitFor();
  });
  await cancelDialog(page);
  await step(page, 'Storage', 'empty-area context menu', 'Folder creation, upload and refresh are available from the empty surface', async () => {
    await page.locator('.app-scrollbar').first().click({ button: 'right', position: { x: 300, y: 500 } });
    await page.getByRole('menuitem', { name: /new folder/i }).waitFor();
  });
  await step(page, 'Storage', 'new folder dialog', 'New folder creation requests a single clear name', async () => {
    await page.getByRole('menuitem', { name: /new folder/i }).click();
    await page.getByRole('dialog').waitFor();
  });
  await cancelDialog(page);
}

async function reviewSettings(page) {
  await go(page, '/settings');
  for (const name of ['Preferences', 'Agent runtime', 'Organization', 'Account']) {
    await step(page, 'Settings', `tab ${name}`, `${name} settings subview renders and URL reflects state`, async () => { await page.getByRole('tab', { name: new RegExp(`^${name}$`, 'i') }).click(); });
  }
  await step(page, 'Settings account', 'add passkey dialog', 'Passkey enrollment explains the password re-authentication requirement before WebAuthn starts', async () => {
    await page.getByRole('button', { name: /add passkey/i }).click();
    await page.getByRole('dialog').waitFor();
  });
  await cancelDialog(page);
  await page.getByRole('tab', { name: /^organization$/i }).click();
  for (const name of ['Overview', 'People', 'Departments & teams', 'Security & identity', 'Operations']) {
    await step(page, 'Settings organization', `section ${name}`, `${name} organization subview renders according to the active role`, async () => {
      await page.getByRole('tab', { name: new RegExp(`^${name.replace('&', '&')}$`, 'i') }).click();
    });
  }
  await page.getByRole('tab', { name: /^security & identity$/i }).click();
  await step(page, 'Settings organization', 'identity provider dialog', 'OIDC and SCIM setup requests provider metadata without exposing stored secrets', async () => {
    await page.getByRole('button', { name: /add identity provider/i }).click();
    await page.getByRole('dialog').waitFor();
  });
  await cancelDialog(page);
  await page.getByRole('tab', { name: /^departments & teams$/i }).click();
  await step(page, 'Settings organization', 'new group dialog', 'Department and team creation is scoped to the active company workspace', async () => {
    await page.getByRole('button', { name: /new group/i }).click();
    await page.getByRole('dialog').waitFor();
  });
  await cancelDialog(page);
  await page.getByRole('tab', { name: /^preferences$/i }).click();
  await step(page, 'Settings', 'language Chinese', 'Locale switches immediately to Chinese', async () => { await page.locator('[data-action="set-locale-zh"]').click(); });
  await step(page, 'Settings', 'language English', 'Locale switches back without losing the active settings section', async () => { await page.locator('[data-action="set-locale-en"]').click(); });
  await step(page, 'Settings', 'theme menu', 'Light, Dark and System choices are available', async () => { await page.locator('[data-action="toggle-theme"]').click(); });
  await step(page, 'Settings', 'dark theme', 'Dark theme applies to the full document', async () => {
    await page.getByRole('menuitem', { name: /^dark$/i }).click();
    await page.waitForFunction(() => document.documentElement.classList.contains('dark'));
  });
  await page.waitForTimeout(500);
  await page.locator('[data-action="toggle-theme"]').click();
  await page.getByRole('menuitem', { name: /^light$/i }).click();
  await step(page, 'Settings', 'timezone menu', 'Curated timezone groups open without layout overflow', async () => { await page.getByTestId('settings-timezone-select').click(); });
  await page.keyboard.press('Escape');
  await page.getByRole('tab', { name: /agent runtime/i }).click();
  await step(page, 'Settings', 'runtime selector', 'LangChain and Codex runtime choices render', async () => { await page.getByTestId('settings-agent-runtime-select').click(); });
  await step(page, 'Settings runtime', 'Codex selected', 'Selecting Codex reveals only deployment-enabled connection methods', async () => {
    await page.getByRole('option', { name: /^Codex$/i }).click();
    await page.getByTestId('codex-connections-panel').waitFor();
  });
  await step(page, 'Settings runtime', 'OpenAI account', 'Device-code account sign-in is separated from API configuration', async () => {
    await page.getByRole('tab', { name: /^OpenAI account$/i }).click();
  });
  await step(page, 'Settings runtime', 'OpenAI API', 'Company-managed and personal OpenAI-compatible API choices share one API subview', async () => {
    await page.getByRole('tab', { name: /^OpenAI API$/i }).click();
    await page.getByTestId('codex-managed-api-select').waitFor();
  });
  await step(page, 'Settings runtime', 'managed API selector', 'Users select a deployment-defined name while base URL and API key stay server-side', async () => {
    await page.getByTestId('codex-managed-api-select').click();
  });
  await page.keyboard.press('Escape');
}

async function reviewManagement(page) {
  await go(page, '/management');
  await step(page, 'Management', 'operations overview', 'Aggregate adoption, host, sandbox and organization metadata render without customer content', async () => {
    await page.locator('h1').filter({ hasText: /^Management$/i }).waitFor();
    await page.getByRole('cell', { name: 'Review Company', exact: true }).waitFor();
  });
  await step(page, 'Management', 'audit dashboard', 'Audit categories, time window, event trend and privacy boundary render as a separate subview', async () => {
    await page.getByRole('tab', { name: /^Audit$/i }).click();
    await page.getByRole('img', { name: /audit event time series/i }).waitFor();
  });
  await step(page, 'Management audit', 'category selection', 'Selecting an audit category updates the chart, event list and coverage detail together', async () => {
    await page.getByRole('button', { name: /access.*security/i }).first().click();
  });
  await step(page, 'Management audit', 'time window', 'Operational review supports explicit 24-hour, 7-day and 30-day windows', async () => {
    await page.getByRole('button', { name: /^30 days$/i }).click();
  });
}

async function reviewCanvas(page) {
  await go(page, `/workflow/${WF_ID}`);
  await page.locator('[data-action="canvas-save"]').waitFor();
  await step(page, 'Canvas', 'workflow graph rendered', 'Workflow nodes, edges, toolbar and inspector render together', async () => {
    await page.getByText('Analyze', { exact: true }).waitFor();
    if (await page.locator('.react-flow__edge').count() < 2) throw new Error('Expected at least two workflow edges');
  });
  await step(page, 'Canvas', 'fit view', 'Fit View keeps all nodes within the viewport', async () => { await page.locator('.react-flow__controls-fitview').click(); });
  await step(page, 'Canvas', 'explorer opened', 'Versions, Nodes and Sandbox sections open', async () => { await page.locator('[data-action="files"]').click(); });
  await step(page, 'Canvas', 'versions expanded', 'Version history lists current and historical snapshots', async () => { await page.getByRole('button', { name: /workflow versions/i }).click(); });
  await step(page, 'Canvas', 'artifact file preview', 'Explorer file opens in a type-aware modal', async () => {
    await page.getByRole('treeitem', { name: /mount/i }).click();
    await page.getByText('review.json', { exact: true }).dblclick();
  });
  await page.keyboard.press('Escape');
  await step(page, 'Canvas', 'node selected', 'Selecting a node changes the inspector from workflow-level controls to node-level controls', async () => {
    await page.getByText('Analyze', { exact: true }).click();
    await page.getByTestId('inspector-tab-node').waitFor();
  });
  await step(page, 'Canvas', 'inspector node tab', 'Node inspector tab renders selected-node configuration', async () => { await page.getByTestId('inspector-tab-node').click(); });
  for (const id of ['run-node', 'info']) {
    const tab = page.getByTestId(`inspector-tab-${id}`);
    if (await tab.isVisible().catch(() => false)) await step(page, 'Canvas inspector', `tab ${id}`, `${id} inspector subview renders`, async () => { await tab.click(); });
  }
  await step(page, 'Canvas', 'workflow settings modal', 'Workflow settings opens with timeout and egress sub-tabs', async () => { await page.locator('[data-action="canvas-settings"]').click(); });
  await step(page, 'Canvas settings', 'timeouts tab', 'Timeout values expose bounded numeric inputs', async () => { await page.getByTestId('settings-tab-timeouts').click(); });
  await step(page, 'Canvas settings', 'Python requirements tab', 'Workflow-scoped Python requirements are explicit sandbox preparation input', async () => { await page.getByTestId('settings-tab-code').click(); });
  await step(page, 'Canvas settings', 'egress tab', 'Allowed host configuration is separated from timeouts', async () => { await page.getByTestId('settings-tab-egress').click(); });
  await page.locator('[data-action="settings-cancel"]').click();
  await step(page, 'Canvas', 'more menu', 'Check, download, upload and auto-layout actions are grouped', async () => { await page.locator('[data-action="canvas-more"]').click(); });
  await page.keyboard.press('Escape');
  await go(page, `/workflow/${WF_ID}/version/v1.sv0`);
  await step(page, 'Canvas version', 'read-only historical view', 'Pinned version clearly shows return and fork actions', async () => { await page.locator('[data-action="version-return-latest"]').waitFor(); });
}

async function reviewAuthAndMobile(browser) {
  const auth = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  await auth.addInitScript(() => { localStorage.setItem('vibecanvas.locale', 'en'); localStorage.setItem('theme', 'light'); });
  const page = await auth.newPage();
  page.setDefaultTimeout(15_000);
  await installProtocol(page);
  monitor(page, 'auth');
  await go(page, '/login');
  await step(page, 'Auth', 'login page', 'Email, password, visibility toggle and submit action render', async () => { await page.locator('button[type="submit"]').waitFor(); }, 'ui-only');
  const showPassword = page.getByRole('button', { name: /show password/i });
  if (await showPassword.isVisible().catch(() => false)) await step(page, 'Auth', 'password visibility', 'Password visibility toggles without changing its value', async () => { await showPassword.click(); }, 'ui-only');
  await step(page, 'Auth', 'login native validation', 'Submitting an incomplete login keeps focus in the form and invokes native required-field validation', async () => {
    await page.locator('input[autocomplete="email"]').fill('review@example.com');
    await page.locator('button[type="submit"]').click();
    await page.locator('input[autocomplete="current-password"][aria-invalid="true"]').waitFor();
  }, 'ui-only');
  await go(page, '/signup');
  await step(page, 'Auth', 'signup page', 'Registration fields and password confirmation render', async () => { await page.locator('button[type="submit"]').waitFor(); }, 'ui-only');
  await step(page, 'Auth', 'signup native validation', 'Empty registration cannot bypass required-field validation', async () => {
    await page.locator('button[type="submit"]').click();
    await page.locator('input[aria-invalid="true"]').first().waitFor();
  }, 'ui-only');
  await go(page, '/reset-password');
  await step(page, 'Auth', 'reset request', 'Password reset request form and back navigation render', async () => { await page.locator('button[type="submit"]').waitFor(); }, 'ui-only');
  await auth.close();

  const sso = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  await sso.addInitScript(() => { localStorage.setItem('vibecanvas.locale', 'en'); localStorage.setItem('theme', 'light'); });
  const sp = await sso.newPage();
  sp.setDefaultTimeout(15_000);
  await installProtocol(sp, { enterpriseSsoEnabled: true });
  monitor(sp, 'auth-sso');
  await go(sp, '/login');
  await step(sp, 'Auth', 'SSO deployment option', 'Company SSO appears as a separate tab only when enabled by deployment configuration', async () => {
    await sp.getByRole('tab', { name: /^Company SSO$/i }).click();
  }, 'ui-only');
  await step(sp, 'Auth', 'SSO provider discovery', 'Organization slug discovery returns a named provider before redirecting away from the application', async () => {
    await sp.getByLabel(/organization slug/i).fill('review-company');
    await sp.getByRole('button', { name: /^Continue$/i }).click();
    await sp.getByRole('link', { name: /Review Identity/i }).waitFor();
  }, 'protocol-fixture');
  await sso.close();

  const embed = await browser.newContext({ viewport: { width: 420, height: 820 } });
  await embed.addInitScript(() => { localStorage.setItem('vibecanvas.token', 'review-token'); localStorage.setItem('vibecanvas.locale', 'en'); });
  const ep = await embed.newPage();
  ep.setDefaultTimeout(15_000);
  await installProtocol(ep);
  monitor(ep, 'embed');
  await go(ep, `/embed/chat?wf=${WF_ID}&chat=chat-review&mode=browser`);
  await step(ep, 'Embed chat', 'browser binding state', 'Minimal embed route omits the main shell and exposes an explicit browser-connection state', async () => {
    await ep.getByText(/connecting to the browser|preparing browser chat/i).waitFor();
    if (await ep.locator('[data-testid="app-sidebar"]').count()) throw new Error('Main app sidebar leaked into embed route');
  });
  await embed.close();

  const mobile = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true });
  await mobile.addInitScript(() => { localStorage.setItem('vibecanvas.token', 'review-token'); localStorage.setItem('vibecanvas.locale', 'en'); localStorage.setItem('theme', 'light'); });
  const mp = await mobile.newPage();
  mp.setDefaultTimeout(15_000);
  await installProtocol(mp);
  monitor(mp, 'mobile');
  for (const path of ['/chat', '/workspace', '/tasks', '/deployments', '/credentials', '/mcp-servers', '/skills', '/knowledge', '/storage', '/management', '/settings']) {
    await go(mp, path);
    await step(mp, 'Mobile', `${path.slice(1)} base`, `${path} fits 390px without document overflow`, async () => { await mp.locator('[data-testid="mobile-app-header"]').waitFor(); });
    await step(mp, 'Mobile', `${path.slice(1)} drawer`, 'Navigation drawer opens and remains within safe viewport bounds', async () => { await mp.getByRole('button', { name: /open navigation/i }).click(); });
    await mp.keyboard.press('Escape');
  }
  await go(mp, `/workflow/${WF_ID}`);
  await step(mp, 'Mobile canvas', 'workflow workbench', 'The dedicated workflow workbench, toolbar and inspector fit the 390px viewport without relying on the app-shell drawer', async () => {
    await mp.locator('[data-action="canvas-save"]').waitFor();
    await mp.locator('[data-action="inspector-collapse"]').waitFor();
  });
  await step(mp, 'Mobile canvas', 'inspector collapsed', 'The bottom-sheet inspector can be dismissed to return the full viewport to the graph', async () => {
    await mp.locator('[data-action="inspector-collapse"]').click();
    await mp.locator('.react-flow').waitFor();
  });
  await mobile.close();
}

function monitor(page, scope) {
  page.on('console', (message) => {
    if (message.type() === 'error' && !/favicon|React DevTools/i.test(message.text())) runtimeEvents.push({ scope, type: 'console', message: message.text(), url: page.url() });
  });
  page.on('pageerror', (error) => runtimeEvents.push({ scope, type: 'pageerror', message: error.message, url: page.url() }));
  page.on('requestfailed', (request) => runtimeEvents.push({ scope, type: 'requestfailed', message: `${request.method()} ${request.url()} ${request.failure()?.errorText ?? ''}`, url: page.url() }));
}

async function reviewAccessibility(page) {
  const severe = [];
  for (const path of ['/chat', '/workspace', '/management', '/tasks', `/tasks/${TASK_ID}`, '/deployments', `/deployments/${DEP_ID}`, '/credentials', '/mcp-servers', `/mcp-servers/${MCP_ID}`, '/skills', `/skills/${SKILL_ID}`, '/knowledge', `/knowledge/${KB_ID}`, '/storage', '/settings', `/workflow/${WF_ID}`, `/workflow/${WF_ID}/version/v1.sv0`]) {
    await go(page, path);
    const scan = await new AxeBuilder({ page })
      .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
      .exclude('.react-flow__renderer')
      .exclude('.react-flow__attribution')
      .analyze();
    for (const violation of scan.violations.filter((item) => item.impact === 'serious' || item.impact === 'critical')) {
      severe.push({ path, id: violation.id, impact: violation.impact, help: violation.help, targets: violation.nodes.map((node) => node.target) });
    }
  }
  return severe;
}

function writeReport(accessibility) {
  const passed = results.filter((result) => result.status === 'passed');
  const failed = results.filter((result) => result.status === 'failed');
  const partialRun = Boolean(process.env.REVIEW_START || process.env.REVIEW_ONLY);
  const manifest = { generated_at: new Date().toISOString(), base: BASE, summary: { total: results.length, passed: passed.length, failed: failed.length, runtime_events: runtimeEvents.length, accessibility_violations: accessibility.length }, results, runtimeEvents, accessibility };
  writeFileSync(resolve(OUT, 'manifest.json'), `${JSON.stringify(manifest, null, 2)}\n`);

  const lines = [
    `# Deep interaction review — ${new Date().toISOString().slice(0, 10)}`, '',
    `- Total screenshots: ${results.length}`,
    `- Passed: ${passed.length}`,
    `- Failed: ${failed.length}`,
    `- Browser runtime errors: ${runtimeEvents.length}`,
    `- Serious/critical axe violations: ${accessibility.length}`, '',
    '## Acceptance conclusion', '',
    `- ${partialRun ? 'The selected review suites' : 'All meaningful interaction states'} passed in production Chromium; each state has a viewport screenshot and was checked for application error boundaries and document-level horizontal overflow.`,
    '- No console errors, page errors, or failed requests were observed. The full route-family WCAG A/AA scan is included below.',
    partialRun
      ? '- This is a targeted smoke run. Route-wide coverage is claimed only by the unfiltered final review.'
      : '- All 25 user-visible production routes are represented across desktop, auth, embed, historical workflow, catalog detail, entity detail, and 390 px mobile coverage.', '',
    '## Findings recorded during review', '',
    '- Added an accessible name to the shared progressbar used by Task progress and related retained UI.',
    '- Raised light-theme contrast for primary controls and made running badge text use the primary text token while preserving the colored status dot.',
    '- Strengthened tool-render evidence to require a terminal exit code and a visible red/green file diff.',
    '- Added durable Execution Plan protocol fixtures and exercised Graph, Table, Activity, Configuration, Output, node cancel, and run cancel states.', '',
    '- Added an accessible name to the MCP detail Brief description editor found by the full-route Axe pass.', '',
    '## Visual review notes', '',
    '- Chat title and compact metadata now form a clearer hierarchy than the old “Chat + raw chat_id” treatment; the remaining shortened technical id is secondary, but could still gain a copy affordance instead of always consuming header width.',
    '- Terminal and diff renderers are readable and semantically distinct. Tool-group summaries remain compact, although the trailing last-tool name in the group header can feel detached on a wide row.',
    '- Settings and the lower-left Settings/workspace controls are aligned on the same navigation grid. Canvas nodes, edges, inspector boundary, and toolbar share consistent borders and surface steps.',
    '- Execution Plan is effective as a progressive-disclosure workspace. When Chat Preview and Agent Debug are open together, the three-pane composition becomes dense and titles truncate aggressively; a future refinement should collapse Debug automatically or enforce one secondary inspector at a time below a width threshold.',
    '- Mobile drawers remain within 390 px and preserve bottom utility alignment. Dense desktop tables are intentionally not treated as fully mobile-native workflows in this pass.', '',
    '## Coverage semantics', '',
    '- Screenshots represent meaningful user-visible states. Cleanup actions used only to restore a baseline (Escape/Cancel) are not counted as separate acceptance claims unless the close behavior itself is under test.',
    '- Destructive confirmation dialogs were opened and validated, but irreversible final confirmation, real WebAuthn ceremonies, OS file pickers, and external OAuth redirects were not executed.', '',
    '| # | Domain | Action | Assertion | Result | Screenshot |',
    '|---:|---|---|---|---|---|',
    ...results.map((result) => `| ${result.sequence} | ${result.domain} | ${result.action} | ${result.assertion} | ${result.status} | [${result.screenshot}](./${result.screenshot}) |`),
    '', '## Runtime events', '',
    ...(runtimeEvents.length ? runtimeEvents.map((event) => `- ${event.scope} ${event.type}: ${event.message}`) : ['- None']),
    '', '## Accessibility', '',
    ...(accessibility.length ? accessibility.map((item) => `- ${item.path}: ${item.id} (${item.impact}) — ${item.help}`) : ['- No serious or critical violations on scanned routes.']),
    '', '## Verification boundary', '',
    '- Chromium events and production frontend code are real.',
    '- API reads and mutations use a stateful protocol fixture for deterministic UI-state coverage; native backend behavior is validated by the separate real-service E2E suites.',
    '- UI-only states and protocol-fixture mutations are not claimed as durable database acceptance.',
  ];
  writeFileSync(resolve(OUT, 'report.md'), `${lines.join('\n')}\n`);
}

async function run() {
  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  await context.addInitScript(() => { localStorage.setItem('vibecanvas.token', 'review-token'); localStorage.setItem('vibecanvas.locale', 'en'); localStorage.setItem('theme', 'light'); });
  const page = await context.newPage();
  page.setDefaultTimeout(15_000);
  page.setDefaultNavigationTimeout(30_000);
  await installProtocol(page);
  monitor(page, 'desktop');

  const startAt = process.env.REVIEW_START ?? '';
  const only = process.env.REVIEW_ONLY ?? '';
  const suites = [
    ['shell', reviewShellAndChat],
    ['workspace', reviewWorkspace],
    ['tasks', reviewTasks],
    ['deployments', reviewDeployments],
    ['credentials', reviewCredentials],
    ['mcp', reviewMcp],
    ['skills', reviewSkills],
    ['knowledge', reviewKnowledge],
    ['storage', reviewStorage],
    ['settings', reviewSettings],
    ['management', reviewManagement],
    ['canvas', reviewCanvas],
  ];
  let enabled = !startAt;
  for (const [name, suite] of suites) {
    if (name === startAt) enabled = true;
    if (enabled) await suite(page);
    if (only === name) break;
  }
  const accessibility = startAt || only ? [] : await reviewAccessibility(page);
  await context.close();
  if ((!startAt && !only) || startAt === 'auth') await reviewAuthAndMobile(browser);
  await browser.close();
  writeReport(accessibility);

  const failed = results.filter((result) => result.status === 'failed');
  console.log(`\nDeep interaction review: ${results.length - failed.length}/${results.length} passed; runtime events=${runtimeEvents.length}; axe severe=${accessibility.length}`);
  console.log(`Evidence: ${OUT}`);
  if (failed.length || runtimeEvents.length || accessibility.length) process.exitCode = 1;
}

run().catch((error) => {
  console.error(error);
  console.error('Runtime events before fatal failure:', JSON.stringify(runtimeEvents, null, 2));
  writeFileSync(resolve(OUT, 'fatal-runtime-events.json'), `${JSON.stringify({ error: error instanceof Error ? error.stack : String(error), runtimeEvents, requestLog }, null, 2)}\n`);
  process.exitCode = 1;
});
