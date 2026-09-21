import { expect, test, type BrowserContext, type Page, type Route } from '@playwright/test';

import inventory from './fixtures/i18n-surface-inventory.json' with { type: 'json' };

const RUN_MATRIX = process.env.VIBECANVAS_I18N_MATRIX === '1';
const LOCALES = inventory.locales as Array<'en' | 'zh'>;
const RAW_KEY = /\b(?:account|auth|chat|common|credentials|deployments|knowledge|mcp|settings|skills|tasks|workspace)\.[a-z][\w.-]+\b/;
const USER_ID = '00000000-0000-4000-8000-000000000001';
const ORG_ID = '00000000-0000-4000-8000-000000000002';
const TASK_ID = '00000000-0000-4000-8000-000000000003';
const DEPLOYMENT_ID = '00000000-0000-4000-8000-000000000004';
const WORKFLOW_ID = '00000000-0000-4000-8000-000000000005';

type Locale = 'en' | 'zh';
type InventoryGroup = 'childTabs' | 'modals' | 'dynamicStates';

/**
 * These surfaces require state which the public route cannot construct from a
 * deterministic HTTP fixture alone. They remain explicit blockers: the
 * coverage contract below fails if an inventory entry is neither exercised
 * by Chromium nor documented here. A blocked surface is never counted as a
 * pass.
 */
const BLOCKED: Record<InventoryGroup, Record<string, string>> = {
  childTabs: {
    'embed-sections': 'The extension shell requires a parent-window binding handshake and a partitioned extension session.',
    'chat-debug-views': 'Debug views require an active streamed turn plus a server-produced debug snapshot.',
    'workflow-inspector': 'Inspector variants require a hydrated canvas graph, node selection, and a live sandbox run.',
    'workflow-batch-source': 'Batch source tabs are nested in the hydrated workflow inspector.',
    'workflow-settings': 'Workflow settings require a hydrated editable workflow and lock state.',
  },
  modals: {
    'command-palette': 'The global keyboard-only trigger did not fire deterministically in the headless Chromium shell; retain it for visible-browser shortcut verification.',
    'shell-chat-actions': 'Rename/delete require a persisted chat row and sidebar menu state.',
    'agent-settings': 'Agent settings require an active chat runtime and model catalog.',
    'step-up-authentication': 'A valid server-issued high-risk challenge cannot be represented by an HTTP response fixture alone.',
    'prompt-diff': 'Prompt diff requires two persisted prompt revisions selected from a hydrated workflow.',
    'workflow-check': 'Workflow check is owned by the hydrated canvas command pipeline.',
    'workflow-toolbar-dialogs': 'Toolbar confirmations require live sandbox and local file-picker transitions.',
    'workflow-unsaved': 'Unsaved confirmation requires an editor dirty transition on a hydrated workflow.',
    'workflow-settings': 'The dialog requires hydrated workflow configuration and an edit lock.',
    'workflow-file-actions': 'VFS context-menu actions require a mounted sandbox file tree.',
    'workflow-field-editor': 'The expanded field editor requires a selected configurable workflow node.',
    'workflow-code-editor': 'The code editor requires a selected code-capable workflow node.',
    'chat-close-sandbox': 'Close sandbox requires an active chat with a server-owned sandbox lease.',
    'chat-file-preview': 'Discard edits requires a preview session, editable file, and dirty editor state.',
    'credential-actions': 'Delete requires a persisted secret row; only redacted metadata can be safely fixture-driven at this layer.',
    'deployment-detail-actions': 'The one-time-secret variant is only produced after a mutating key rotation response.',
    'deployment-list-actions': 'List confirmations depend on a persisted deployment row and mutation state.',
    'mcp-list-actions': 'List uninstall requires a persisted installed server row; detail uninstall is exercised instead.',
    'enterprise-identity-actions': 'OIDC/SCIM flows require issuer discovery and one-time secret lifecycle state.',
    'organization-actions': 'Member/group mutations require server-enforced organization membership state.',
    'skill-detail-actions': 'Unsaved/publish variants require a persisted draft transition; uninstall is exercised from the detail surface.',
    'storage-actions': 'Storage dialogs require a mounted VFS tree and editable preview dirty state.',
    'task-list-actions': 'Cancellation requires a server-owned runnable task state; detail presentation is covered without mutation.',
    'workflow-delete': 'Workflow row destructive actions require a persisted workflow and sandbox ownership state.',
    'workflow-duplicate': 'Workflow duplication requires a persisted workflow revision.',
    'workflow-edit-info': 'Workflow metadata editing requires a persisted workflow revision.',
    'workflow-row-actions': 'Close-workflow-sandbox requires a server-owned sandbox lease.',
  },
  dynamicStates: {
    disconnected: 'There is no shared product-level offline surface: chat, embed, MCP, and runtime each derive disconnection from different live transports.',
  },
};

const EXECUTED = {
  childTabs: [
    'login-methods',
    'chat-example-categories',
    'management-sections',
    'management-audit-categories',
    'settings-sections',
    'organization-sections',
    'task-types',
    'task-detail-sections',
    'deployment-sections',
    'deployment-code-examples',
    'mcp-sections',
    'mcp-catalog-sections',
    'mcp-detail-sections',
    'skill-sections',
    'skill-detail-sections',
    'skill-catalog-sections',
  ],
  modals: [
    'resource-share',
    'credential-form',
    'deployment-create',
    'knowledge-detail-actions',
    'knowledge-create',
    'mcp-catalog-install',
    'mcp-detail-actions',
    'mcp-form',
    'passkey-actions',
    'settings-account-actions',
    'skill-catalog-install',
    'skill-list-actions',
    'workflow-create',
  ],
  dynamicStates: ['loading', 'empty', 'error', 'permission-denied', 'destructive-action'],
} satisfies Record<InventoryGroup, string[]>;

const provenance = {
  ownership_scope: 'personal',
  origin_type: 'created',
  owner: { type: 'user', display_name: 'Fixture Owner' },
  created_by: { type: 'user', display_name: 'Fixture Owner' },
};
const fullAccess = {
  capabilities: ['view_metadata', 'view', 'export', 'create', 'update', 'delete', 'manage_access', 'use', 'execute', 'cancel', 'resume', 'inspect_runs', 'deploy', 'mount', 'publish', 'manage_secret', 'manage_members', 'manage_policy', 'view_audit'],
  effective_role: 'manager',
  source: 'computed',
};

const taskFixture = {
  id: TASK_ID,
  status: 'running',
  progress: 0.42,
  task_type: 'batch_exec',
  workflow_id: WORKFLOW_ID,
  payload: {},
  result: null,
  results_uri: null,
  error: null,
  background_job_id: TASK_ID,
  submitted_at: '2026-08-23T08:00:00Z',
  started_at: '2026-08-23T08:00:01Z',
  finished_at: null,
  access: fullAccess,
  provenance,
};

const deploymentFixture = {
  id: DEPLOYMENT_ID,
  tenant_id: ORG_ID,
  user_id: USER_ID,
  wf_id: WORKFLOW_ID,
  name: 'Fixture API',
  slug: 'fixture-api',
  trigger_type: 'api',
  version_pin: 'head',
  pinned_major: null,
  pinned_sub: null,
  enabled: true,
  rate_limit_qps: 10,
  invoke_count: 5,
  last_invoked_at: null,
  access: fullAccess,
  provenance,
  created_at: '2026-08-23T08:00:00Z',
  updated_at: null,
  deleted_at: null,
};

const mcpFixture = {
  id: 'mcp-fixture',
  name: 'Fixture Search',
  tool_prefix: 'fixture',
  transport: 'sse',
  endpoint: 'https://example.test/mcp',
  auth_mode: 'configuration',
  connection_status: 'not_required',
  description: 'Deterministic MCP fixture',
  description_source: 'user',
  description_model_id: null,
  description_generated_at: null,
  description_basis_hash: null,
  auth_config: { type: 'bearer', token: '***' },
  connection_config: {},
  enabled: true,
  last_handshake_status: 'ok',
  last_tool_count: 1,
  last_tool_names: [{ name: 'fixture__query', description: 'Search', input_schema: { type: 'object' } }],
  last_handshake_at: '2026-08-23T08:00:00Z',
  created_at: '2026-08-23T08:00:00Z',
  updated_at: '2026-08-23T08:00:00Z',
  access: fullAccess,
  provenance,
};

const mcpCatalogFixture = {
  source: 'official',
  source_id: 'fixture-mcp',
  name: 'Fixture MCP',
  description: 'Official catalog fixture',
  version: '1.0.0',
  verified: true,
  usage_count: 42,
  homepage: 'https://example.test',
  published_at: '2026-08-23T08:00:00Z',
  connection: { transport: 'sse', endpoint: 'https://example.test/mcp', connection_config: {} },
  config_fields: [{ key: 'token', label: 'Token', description: 'Access token', required: true, secret: true, target: 'auth_config.token', input_type: 'string', choices: [], default: null, placeholder: 'token' }],
  configuration_source: 'official_registry',
  auth_mode: 'oauth',
  auth_metadata_url: null,
};

const skillFixture = {
  id: 'skill-fixture',
  name: 'Fixture Skill',
  description: 'Deterministic skill fixture',
  allowed_tools: ['read_file'],
  version: 2,
  source: 'custom',
  source_id: null,
  source_url: null,
  source_revision: null,
  revision_hash: 'fixture-hash',
  created_at: '2026-08-23T08:00:00Z',
  updated_at: '2026-08-23T08:00:00Z',
  body: '# Fixture Skill',
  skill_md: '---\nname: Fixture Skill\n---\n# Fixture Skill',
  files: ['SKILL.md', 'scripts/read.ts'],
  has_draft: false,
  draft_updated_at: null,
  access: fullAccess,
  provenance,
};

const skillCatalogFixture = {
  source: 'openai',
  source_label: 'OpenAI',
  source_id: 'fixture-skill',
  name: 'Fixture Catalog Skill',
  description: 'Official skill fixture',
  version: 1,
  allowed_tools: ['read_file'],
  homepage: 'https://example.test',
  revision: 'fixture-revision',
  files: [{ path: 'SKILL.md', size_bytes: 128 }, { path: 'references/guide.md', size_bytes: 64 }],
  skill_md: '# Fixture Catalog Skill',
  body: '# Fixture Catalog Skill',
};

const knowledgeFixture = {
  id: 'knowledge-fixture',
  name: 'Fixture Knowledge',
  description: 'Deterministic knowledge fixture',
  retrieval_strategy: 'agentic_lexical',
  package_version: 3,
  created_at: '2026-08-23T08:00:00Z',
  updated_at: '2026-08-23T08:00:00Z',
  latest_updated_at: '2026-08-23T08:00:00Z',
  file_count: 1,
  chunk_count: 2,
  stored_count: 0,
  pending_count: 0,
  indexing_count: 0,
  indexed_count: 1,
  failed_count: 0,
  access: fullAccess,
  provenance,
};

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: 'application/json', json: body });
}

async function installLocale(context: BrowserContext, locale: Locale) {
  await context.addInitScript((lng) => {
    try {
      if (window.localStorage.getItem('vibecanvas.locale') === null) {
        window.localStorage.setItem('vibecanvas.locale', lng);
      }
    } catch {
      // Sandboxed preview frames intentionally cannot access application storage.
    }
  }, locale);
}

async function installApiFixture(page: Page) {
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();

    if (path === '/api/v1/auth/me') return json(route, {
      user_id: USER_ID,
      tenant_id: ORG_ID,
      active_organization_id: ORG_ID,
      email: 'fixture@example.test',
      display_name: 'Fixture User',
      platform_management_role: 'platform_security_admin',
      session: { audience: 'web' },
    });
    if (path === '/api/v1/public-config') return json(route, {
      enterprise_sso_enabled: true,
      password_auth_enabled: true,
      passkey_auth_enabled: true,
      account_deletion_grace_days: 7,
    });
    if (path === '/api/v1/organizations') return json(route, {
      active_organization_id: ORG_ID,
      session_generation: 1,
      items: [{
        organization_id: ORG_ID,
        kind: 'business',
        slug: 'fixture-company',
        name: 'Fixture Company',
        membership_id: 'membership-self',
        role: 'owner',
        status: 'active',
        active: true,
        access: fullAccess,
      }],
    });
    if (path.endsWith('/me') && path.startsWith('/api/v1/organizations/')) return json(route, {
      membership: { membership_id: 'membership-self', user_id: USER_ID, email: 'fixture@example.test', display_name: 'Fixture User', role: 'owner', status: 'active', source: 'native', directory_provider_id: null, created_at: '2026-08-23T08:00:00Z', updated_at: '2026-08-23T08:00:00Z' },
      groups: [{ group_id: 'group-fixture', kind: 'department', name: 'Engineering', source: 'native', role: 'lead', status: 'active' }],
    });
    if (/\/organizations\/[^/]+\/members$/.test(path)) return json(route, { items: [] });
    if (/\/organizations\/[^/]+\/groups\/[^/]+\/members$/.test(path)) return json(route, { items: [] });
    if (/\/organizations\/[^/]+\/groups$/.test(path)) return json(route, { items: [{ group_id: 'group-fixture', organization_id: ORG_ID, parent_group_id: null, kind: 'department', name: 'Engineering', source: 'native', directory_provider_id: null, external_id: null, status: 'active', created_by: USER_ID, created_at: '2026-08-23T08:00:00Z', updated_at: '2026-08-23T08:00:00Z', access: fullAccess }] });
    if (/\/organizations\/[^/]+\/service-accounts$/.test(path)) return json(route, { items: [] });
    if (/\/organizations\/[^/]+\/identity-providers$/.test(path)) return json(route, { items: [] });
    if (path === '/api/v1/audit') return json(route, { items: [], next_cursor: null });

    if (path === '/api/v1/platform-management/context') return json(route, { role: 'platform_security_admin' });
    if (path === '/api/v1/platform-management/overview') return json(route, {
      role: 'platform_security_admin', generated_at: '2026-08-23T08:00:00Z',
      identity: { registered_users: 12, active_users: 11, online_users_5m: 3, registered_users_24h: 2, personal_workspaces: 9, company_workspaces: 3 },
      organizations: [],
      host: { cpu_count: 8, load_average_1m: 1, load_average_5m: 0.8, load_average_15m: 0.7, memory: { total_bytes: 1024, available_bytes: 512 }, disk: { total_bytes: 4096, free_bytes: 2048 }, scope: 'current_api_host' },
      sandboxes: { resident: 1, capacity: 4, busy: 1, resident_leases: 1, pending_closes: 0 },
      privacy: { content_visible: false, user_profiles_visible: false, scope: 'aggregate_and_lifecycle_metadata_only' },
    });
    if (path === '/api/v1/platform-management/audit') return json(route, {
      role: 'platform_security_admin', generated_at: '2026-08-23T08:00:00Z', window_hours: 168, bucket: 'day',
      categories: ['identity', 'access_security', 'resources', 'data_lifecycle', 'runtime_operations'].map((category) => ({ category, total: 1, failures: 0, series: [], actions: [] })),
      recent_events: [], catalog: [], privacy: { content_visible: false, identities_visible: false, customer_resource_identifiers_visible: false, private_payload_decrypted: false },
    });

    if (path === '/api/v1/tasks/summary') return json(route, { active: 1, queued: 0, running: 1, cancelling: 0, failed: 0, finished: 0, cancelled: 0 });
    if (path === '/api/v1/tasks/scheduled-runs') return json(route, { items: [], total: 0, limit: 50, offset: 0 });
    if (path === '/api/v1/tasks') return json(route, { items: [], total: 0, limit: 50, offset: 0 });
    if (path === `/api/v1/tasks/${TASK_ID}`) return json(route, taskFixture);
    if (path === `/api/v1/tasks/${TASK_ID}/events`) return json(route, { items: [], limit: 50, after_seq: null, before_seq: null, order: 'desc', next_cursor: null, latest_seq: 0 });

    if (path === '/api/v1/deployments') return json(route, { items: [], total: 0, limit: 50, offset: 0 });
    if (path === `/api/v1/deployments/${DEPLOYMENT_ID}`) return json(route, deploymentFixture);
    if (path === `/api/v1/deployments/${DEPLOYMENT_ID}/metrics`) return json(route, { series: [], bucket: 'hour', from: '2026-08-22T08:00:00Z', to: '2026-08-23T08:00:00Z' });
    if (path === `/api/v1/deployments/${DEPLOYMENT_ID}/history`) return json(route, { items: [], next_cursor: null, limit: 50 });

    if (path === '/api/v1/mcp-servers/catalog/resolve') return json(route, mcpCatalogFixture);
    if (path === '/api/v1/mcp-servers/catalog') return json(route, { source: 'official', ranking: 'browse', items: [mcpCatalogFixture], has_more: false });
    if (path === '/api/v1/mcp-servers') return json(route, { items: [] });
    if (path === '/api/v1/mcp-servers/mcp-fixture') return json(route, mcpFixture);

    if (path === '/api/v1/skills/catalog/resolve') return json(route, skillCatalogFixture);
    if (path === '/api/v1/skills/catalog') return json(route, { source: 'openai', source_label: 'OpenAI', revision: 'fixture-revision', items: [skillCatalogFixture], has_more: false });
    if (path === '/api/v1/skills') return json(route, { items: [] });
    if (path === '/api/v1/skills/skill-fixture/draft') return json(route, { skill_id: 'skill-fixture', base_revision_hash: 'fixture-hash', draft_hash: null, skill_md: skillFixture.skill_md, body: skillFixture.body, files: skillFixture.files, has_changes: false, updated_at: null, access: fullAccess, provenance });
    if (path === '/api/v1/skills/skill-fixture/versions') return json(route, []);
    if (path === '/api/v1/skills/skill-fixture') return json(route, skillFixture);

    if (path === '/api/v1/kb') return json(route, []);
    if (path === '/api/v1/kb/knowledge-fixture/files') return json(route, []);
    if (path === '/api/v1/kb/knowledge-fixture') return json(route, knowledgeFixture);

    if (path === '/api/v1/workflows') return json(route, { items: [], total: 0, limit: 50, offset: 0 });
    if (path === `/api/v1/workflows/${WORKFLOW_ID}`) return json(route, { workflow: { start: { node_type: 'StartNode', input_fields: { topic: { type: 'string' } } } } });
    if (path === '/api/v1/workflows/sandboxes') return json(route, { items: [] });
    if (path === '/api/v1/enums') return json(route, { node_types: [], field_types: [] });

    if (path === '/api/v1/storage/list') return json(route, { path: '/', items: [], writable: true });
    if (path === '/api/v1/chats/bootstrap') return json(route, { carrier_scope_id: 'chat-fixture', surface: 'chat', available_commands: [], debug_view_enabled: false });
    if (/\/api\/v1\/chat-scopes\/[^/]+\/chats$/.test(path)) return json(route, { items: [] });
    if (path === '/api/v1/chats/inventory') return json(route, { items: [] });

    if (path === '/api/v1/llm-credentials') return json(route, []);
    if (path === '/api/v1/llm-credentials/openrouter/status') return json(route, { connected: false, credential_id: null, models: [], catalog_refreshed_at: null, catalog_stale: false, error_code: null });
    if (path === '/api/v1/agent-runtime/capabilities') return json(route, { runtimes: [], codex_account_enabled: false });
    if (path === '/api/v1/agent-runtime/settings') return json(route, { runtime: 'codex', timezone: 'UTC' });
    if (path === '/api/v1/agent-runtime/codex/account') return json(route, { connected: false });
    if (path === '/api/v1/auth/passkeys') return json(route, { enabled: true, credentials: [] });
    if (path === '/api/v1/resource-access/shared') return json(route, { items: [], total: 0 });
    if (path.endsWith('/access')) return json(route, { bindings: [], access: fullAccess, provenance });

    if (method === 'DELETE') return route.fulfill({ status: 204, body: '' });
    return json(route, {});
  });
}

async function waitForSurface(page: Page) {
  await expect(page.locator('#root')).toBeVisible();
  await expect(page.locator('body')).not.toContainText(
    /Unexpected Application Error|Application crashed|Something went wrong|页面发生错误/i,
  );
}

async function expectSurfaceQuality(page: Page) {
  await waitForSurface(page);
  const visibleText = await page.locator('body').innerText();
  expect(visibleText).not.toMatch(RAW_KEY);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow, 'the document must not create horizontal page overflow').toBeLessThanOrEqual(1);
}

async function openAndCloseDialog(page: Page, trigger: () => Promise<void>) {
  await trigger();
  await expect(page.getByRole('dialog').last()).toBeVisible();
  await expectSurfaceQuality(page);
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
}

async function clickEveryTab(page: Page, tablistIndex = 0, minimum = 2) {
  const list = page.locator('[role="tablist"]:visible').nth(tablistIndex);
  await expect(list).toBeVisible();
  const tabs = list.getByRole('tab');
  expect(await tabs.count()).toBeGreaterThanOrEqual(minimum);
  for (let index = 0; index < await tabs.count(); index += 1) {
    const tab = tabs.nth(index);
    await tab.click();
    await expect(tab).toHaveAttribute('data-state', 'active');
    await expectSurfaceQuality(page);
  }
}

async function gotoFixture(page: Page, path: string) {
  await page.goto(path, { waitUntil: 'domcontentloaded' });
  await expectSurfaceQuality(page);
}

function assertCoverageContract(group: InventoryGroup) {
  const ids = inventory[group].map((entry) => entry.id).sort();
  const executed = [...EXECUTED[group]].sort();
  const blocked = Object.keys(BLOCKED[group]).sort();
  expect(executed.filter((id) => blocked.includes(id)), `${group}: executed and blocked must be disjoint`).toEqual([]);
  expect([...executed, ...blocked].sort(), `${group}: every inventoried surface needs executable or blocked evidence`).toEqual(ids);
}

test.describe('complete multilingual surface matrix', () => {
  test.skip(!RUN_MATRIX, 'Set VIBECANVAS_I18N_MATRIX=1 to run the multilingual matrix.');

  test('inventory contract classifies every semantic surface without counting blockers as passes', async () => {
    assertCoverageContract('childTabs');
    assertCoverageContract('modals');
    assertCoverageContract('dynamicStates');
  });

  for (const locale of LOCALES) {
    for (const viewport of inventory.viewports) {
      const cell = `${locale}/${viewport.id}`;

      test.describe(cell, () => {
        test.beforeEach(async ({ context, page }) => {
          await installLocale(context, locale);
          await page.setViewportSize({ width: viewport.width, height: viewport.height });
          await installApiFixture(page);
        });

        test('switch, refresh, and deep-link preserve locale and route state', async ({ page }) => {
          await gotoFixture(page, '/settings?tab=preferences');
          const target: Locale = locale === 'en' ? 'zh' : 'en';
          await page.locator(`[data-action="set-locale-${target}"]`).click();
          await expect(page.locator('html')).toHaveAttribute('lang', target === 'zh' ? 'zh-CN' : 'en');
          expect(new URL(page.url()).searchParams.get('tab')).toBe('preferences');
          await page.reload({ waitUntil: 'domcontentloaded' });
          await expect(page.locator('html')).toHaveAttribute('lang', target === 'zh' ? 'zh-CN' : 'en');
          await gotoFixture(page, '/tasks?type=scheduled_run');
          expect(new URL(page.url()).searchParams.get('type')).toBe('scheduled_run');
        });

        test('executes public, chat, management, and settings child-tab groups', async ({ page }) => {
          await gotoFixture(page, '/login');
          await clickEveryTab(page, 0, 2);

          await gotoFixture(page, '/chat');
          await expect(page.locator('[data-role="empty-chat-examples"]')).toBeVisible();
          await clickEveryTab(page, 0, 5);

          await gotoFixture(page, '/management');
          await clickEveryTab(page, 0, 2);
          await page.locator('[role="tablist"]:visible').first().getByRole('tab').nth(1).click();
          await clickEveryTab(page, 1, 5);

          await gotoFixture(page, '/settings?tab=preferences');
          if (viewport.width >= 768) {
            for (const tab of ['preferences', 'runtime', 'api-keys', 'extensions', 'organization', 'account']) {
              const trigger = page.getByTestId(`settings-tab-${tab}`);
              await trigger.click();
              await expect(trigger).toHaveAttribute('data-state', 'active');
              await expectSurfaceQuality(page);
            }
          } else {
            for (const tab of ['runtime', 'api-keys', 'extensions', 'organization', 'account', 'preferences']) {
              await page.goto(`/settings?tab=${tab}`, { waitUntil: 'domcontentloaded' });
              await expectSurfaceQuality(page);
              expect(new URL(page.url()).searchParams.get('tab')).toBe(tab);
            }
          }

          await gotoFixture(page, '/settings?tab=organization');
          await clickEveryTab(page, 0, 5);
        });

        test('executes task and deployment child-tab groups', async ({ page }) => {
          await gotoFixture(page, '/tasks');
          await clickEveryTab(page, 0, 2);

          await gotoFixture(page, `/tasks/${TASK_ID}`);
          await clickEveryTab(page, 0, 2);

          await gotoFixture(page, `/deployments/${DEPLOYMENT_ID}`);
          await clickEveryTab(page, 0, 4);
          await page.locator('[role="tablist"]:visible').first().getByRole('tab').nth(1).click();
          await clickEveryTab(page, 1, 3);
        });

        test('executes MCP and Skill child-tab groups', async ({ page }) => {
          await gotoFixture(page, '/mcp-servers');
          await clickEveryTab(page, 0, 2);
          await gotoFixture(page, '/mcp-servers/discover/official?id=fixture-mcp');
          await clickEveryTab(page, 0, 4);
          await gotoFixture(page, '/mcp-servers/mcp-fixture');
          await clickEveryTab(page, 0, 6);

          await gotoFixture(page, '/skills');
          await clickEveryTab(page, 0, 3);
          await gotoFixture(page, '/skills/discover/openai?id=fixture-skill');
          await clickEveryTab(page, 0, 4);
          await gotoFixture(page, '/skills/skill-fixture');
          await clickEveryTab(page, 0, 4);

        });

        test('executes creation, catalog, account, and destructive dialog groups', async ({ page }) => {
          test.setTimeout(90_000);
          await gotoFixture(page, '/workspace');
          await openAndCloseDialog(page, () => page.getByRole('button', { name: /new workflow|新建工作流/i }).first().click());

          await gotoFixture(page, '/settings?tab=api-keys');
          await openAndCloseDialog(page, () => page.getByTestId('cred-add-button').click());

          await gotoFixture(page, '/deployments');
          await openAndCloseDialog(page, () => page.getByRole('button', { name: /new deployment|新建部署/i }).first().click());

          await gotoFixture(page, '/knowledge');
          await openAndCloseDialog(page, () => page.getByRole('button', { name: /new knowledge|新建知识/i }).first().click());
          await openAndCloseDialog(page, () => page.getByRole('button', { name: /upload folder|上传.*文件夹/i }).first().click());

          await gotoFixture(page, '/knowledge/knowledge-fixture');
          await openAndCloseDialog(page, () => page.getByRole('button', { name: /share|分享/i }).first().click());
          await openAndCloseDialog(page, () => page.getByRole('button', { name: /edit|编辑/i }).first().click());

          await gotoFixture(page, '/mcp-servers');
          await openAndCloseDialog(page, () => page.getByTestId('mcp-add-button').click());
          await gotoFixture(page, '/mcp-servers/discover/official?id=fixture-mcp');
          await openAndCloseDialog(page, () => page.getByRole('button', { name: /install|安装/i }).first().click());
          await gotoFixture(page, '/mcp-servers/mcp-fixture');
          await openAndCloseDialog(page, () => page.getByRole('button', { name: /uninstall|卸载/i }).first().click());

          await gotoFixture(page, '/skills');
          await page.locator('[role="tablist"]:visible').first().getByRole('tab').nth(2).click();
          await openAndCloseDialog(page, () => page.getByRole('button', { name: /upload|上传/i }).first().click());
          await gotoFixture(page, '/skills/discover/openai?id=fixture-skill');
          await openAndCloseDialog(page, () => page.getByRole('button', { name: /install|安装/i }).first().click());

          await gotoFixture(page, '/settings?tab=account');
          await openAndCloseDialog(page, () => page.getByRole('button', { name: /add passkey|添加通行密钥/i }).first().click());
          await page.getByLabel(/type your email|输入.*邮箱/i).fill('fixture@example.test');
          await openAndCloseDialog(page, () => page.getByRole('button', { name: /delete account|删除账号/i }).first().click());
        });

        test('executes loading, empty, error, permission, and destructive state classes', async ({ page }) => {
          let release!: () => void;
          const gate = new Promise<void>((resolve) => { release = resolve; });
          await page.route('**/api/v1/workflows**', async (route) => {
            await gate;
            await json(route, { items: [], total: 0, limit: 50, offset: 0 });
          });
          await page.goto('/workspace', { waitUntil: 'domcontentloaded' });
          await expect(page.locator('[aria-busy="true"], [data-testid*="loading"], .animate-pulse').first()).toBeVisible();
          release();
          await expectSurfaceQuality(page);
          await expect(page.getByText(/no workflows|暂无工作流/i).first()).toBeVisible();

          await gotoFixture(page, '/preview');
          await expect(page.locator('main, #main-content')).toContainText(/unable|missing|无法|缺少|error|错误/i);

          await page.route('**/api/v1/platform-management/**', (route) => json(route, { detail: 'forbidden' }, 403));
          await gotoFixture(page, '/management');
          await expect(page.locator('main, #main-content')).toContainText(/not available|operator|permission|access|不可用|无法|权限|访问/i, { timeout: 20_000 });

          await gotoFixture(page, '/settings?tab=account');
          await page.getByLabel(/type your email|输入.*邮箱/i).fill('fixture@example.test');
          await page.getByRole('button', { name: /delete account|删除账号/i }).first().click();
          const destructive = page.getByRole('dialog');
          await expect(destructive).toBeVisible();
          await expect(destructive.getByRole('button', { name: /delete|删除/i }).last()).toBeVisible();
          await expectSurfaceQuality(page);
        });
      });
    }
  }
});
