/**
 * Maintained visual-review harness — drives the real built frontend (vite preview
 * on :5173) with Playwright + full API route-stubbing. No backend needed.
 * Run:  node scripts/screenshots.mjs
 */
import { chromium } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { mkdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const OUT = resolve(__dirname, '..', 'screenshots');
mkdirSync(OUT, { recursive: true });

const BASE = 'http://127.0.0.1:5173';
const WF_ID = 'wf_demo';
const now = Math.floor(Date.now() / 1000);

const LOCALE = process.env.LOCALE ?? 'en';
const THEME = process.env.THEME ?? 'light';
const ZH = LOCALE === 'zh';
const PREFIX = `${ZH ? 'zh' : 'en'}-${THEME}-`;
const L = (zh, en) => (ZH ? zh : en);

// ---- canned payloads -------------------------------------------------------
const ME = { user_id: 'user_demo', tenant_id: 'tenant_demo', email: 'demo@example.com' };

const META = {
  wf_id: WF_ID,
  workflow_name: L('客户反馈分析流程', 'Customer Feedback Analysis'),
  description: L('读取反馈、用大模型分类、汇总输出', 'Read feedback, classify with an LLM, summarize'),
  active_v: 1,
  active_sv: 0,
  updated_at: now,
  created_at: now - 86400,
  tags: ['demo', L('示例', 'sample')],
  access: { capabilities: ['view', 'run', 'update', 'delete'], effective_role: 'owner', source: 'visual-review-fixture' },
};

const WORKSPACE_LIST = {
  items: [
    META,
    {
      wf_id: 'wf_2',
      workflow_name: L('商品文案生成器', 'Product Copy Generator'),
      description: L('根据卖点批量生成营销文案', 'Batch-generate marketing copy from selling points'),
      active_v: 3,
      active_sv: 2,
      updated_at: now - 3600,
      created_at: now - 200000,
      tags: [L('营销', 'marketing')],
    },
    {
      wf_id: 'wf_3',
      workflow_name: L('空白工作流', 'Blank Workflow'),
      description: '',
      active_v: 1,
      active_sv: 0,
      updated_at: now - 7200,
      created_at: now - 7200,
      tags: [],
    },
  ],
  total: 3,
  limit: 50,
  offset: 0,
};

// flat-dict workflow keyed by node_id + reserved __meta__
const WORKFLOW = {
  __meta__: { workflow_name: META.workflow_name, version: 'v1.sv0' },
  node_1: {
    node_id: 'node_1',
    node_name: 'Start',
    node_type: 'StartNode',
    node_description: L('工作流入口', 'Workflow entry'),
    input_fields: {},
    output_fields: { user_feedback: { type: 'string', description: L('原始反馈文本', 'raw feedback text') } },
    node_config: {},
    children: ['node_2'],
    __attributes__: { x: 40, y: 160 },
  },
  node_2: {
    node_id: 'node_2',
    node_name: 'Classify',
    node_type: 'PromptNode',
    node_description: L('用大模型对反馈做情感分类', 'Classify sentiment with an LLM'),
    input_fields: {
      feedback: { type: 'string', value: '', reference: 'Start.user_feedback' },
    },
    output_fields: { sentiment: { type: 'string', description: L('正面/负面/中性', 'positive/negative/neutral') } },
    node_config: { model: 'gpt-4o', prompt: L('请判断以下反馈的情感倾向：{feedback}', 'Classify the sentiment of: {feedback}') },
    children: ['node_3'],
    __attributes__: { x: 380, y: 160 },
  },
  node_3: {
    node_id: 'node_3',
    node_name: 'End',
    node_type: 'EndNode',
    node_description: L('输出分类结果', 'Emit classification result'),
    input_fields: {
      result: { type: 'string', value: '', reference: 'Classify.sentiment' },
    },
    output_fields: {},
    node_config: {},
    children: [],
    __attributes__: { x: 720, y: 160 },
  },
};

const SNAPSHOT = { workflow: WORKFLOW, meta: META };

const VERSIONS = {
  versions: [
    { major: 1, sub: 0, v: 1, sv: 0, version_str: 'v1.sv0', version_id: 'ver_1', message: L('初始版本', 'initial version'), timestamp: now - 86400 },
    { major: 1, sub: 1, v: 1, sv: 1, version_str: 'v1.sv1', version_id: 'ver_2', message: L('调整节点位置', 'tweak node positions'), timestamp: now - 80000 },
    { major: 2, sub: 0, v: 2, sv: 0, version_str: 'v2.sv0', version_id: 'ver_3', message: L('新增分类节点', 'add classify node'), timestamp: now - 3600 },
  ],
};

const VFS_LIST = {
  entries: [
    { path: '/mount/cells_1.jsonl', kind: 'artifact', content_type: 'table/jsonl', abstract: L('反馈样本表（5行）', 'feedback sample table (5 rows)'), size_bytes: 412, wf_version: 'v2.sv0', last_access: now - 100, stale: false, capabilities: ['read', 'download', 'copy_path'] },
    { path: '/mount/counts_1.json', kind: 'artifact', content_type: 'application/json', abstract: L('各类别计数', 'per-category counts'), size_bytes: 96, wf_version: 'v2.sv0', last_access: now - 90, stale: false, capabilities: ['read', 'download', 'copy_path'] },
    { path: '/mount/schema_1.json', kind: 'artifact', content_type: 'application/json', abstract: L('工作流结构快照', 'workflow structure snapshot'), size_bytes: 1024, wf_version: 'v1.sv1', last_access: now - 5000, stale: true, capabilities: ['read', 'download', 'copy_path'] },
    { path: '/mount/exec_1.json', kind: 'artifact', content_type: 'application/json', abstract: L('上次执行记录', 'last execution record'), size_bytes: 256, wf_version: 'v2.sv0', last_access: now - 50, stale: false, capabilities: ['read', 'download', 'copy_path'] },
  ],
  root_capabilities: { data: ['upload'], mount: ['upload'] },
};

const VFS_CONTENT = {
  '/mount/cells_1.jsonl': {
    path: '/mount/cells_1.jsonl',
    content_type: 'table/jsonl',
    content: (ZH ? [
      '{"id": 1, "feedback": "物流很快，包装精美", "sentiment": "正面"}',
      '{"id": 2, "feedback": "客服回复太慢了", "sentiment": "负面"}',
      '{"id": 3, "feedback": "价格还能再便宜点", "sentiment": "中性"}',
      '{"id": 4, "feedback": "质量超出预期，会回购", "sentiment": "正面"}',
      '{"id": 5, "feedback": "收到时有点磕碰", "sentiment": "负面"}',
    ] : [
      '{"id": 1, "feedback": "Fast shipping, lovely packaging", "sentiment": "positive"}',
      '{"id": 2, "feedback": "Support replied too slowly", "sentiment": "negative"}',
      '{"id": 3, "feedback": "Wish it were a bit cheaper", "sentiment": "neutral"}',
      '{"id": 4, "feedback": "Quality beat expectations, will rebuy", "sentiment": "positive"}',
      '{"id": 5, "feedback": "Arrived slightly dented", "sentiment": "negative"}',
    ]).join('\n'),
    size_bytes: 412,
    truncated: false,
    wf_version: 'v2.sv0',
    stale: false,
  },
  '/mount/counts_1.json': {
    path: '/mount/counts_1.json',
    content_type: 'application/json',
    content: JSON.stringify(ZH ? { 正面: 2, 负面: 2, 中性: 1 } : { positive: 2, negative: 2, neutral: 1 }, null, 2),
    size_bytes: 96,
    truncated: false,
    wf_version: 'v2.sv0',
    stale: false,
  },
  '/mount/schema_1.json': {
    path: '/mount/schema_1.json',
    content_type: 'application/json',
    content: JSON.stringify({ nodes: 3, edges: 2 }, null, 2),
    size_bytes: 1024,
    truncated: false,
    wf_version: 'v1.sv1',
    stale: true,
  },
  '/mount/exec_1.json': {
    path: '/mount/exec_1.json',
    content_type: 'application/json',
    content: JSON.stringify({ status: 'finished', duration_ms: 1830 }, null, 2),
    size_bytes: 256,
    truncated: false,
    wf_version: 'v2.sv0',
    stale: false,
  },
};

// ---- route handler ---------------------------------------------------------
function json(route, body, status = 200) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

async function installStubs(page) {
  // Knowledge routes use the same versioned API namespace as other resources.
  await page.route(/\/api\/v1\/kb(?:\/[^?#]*)?(?:\?.*)?$/, (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/v1/kb') return json(route, []);
    return json(route, { detail: 'not found' }, 404);
  });
  await page.route('**/api/v1/**', (route) => {
    const url = new URL(route.request().url());
    const p = url.pathname;
    const q = url.searchParams;
    if (process.env.DEBUG_VISUAL_REQUESTS === '1' && p.includes('/vfs')) {
      console.log('VFS request', route.request().method(), p, url.search);
    }

    if (p.endsWith('/auth/me')) return json(route, ME);
    if (p === '/api/v1/agent-runtime/settings') return json(route, {
      default_runtime_type: 'langchain',
      available_runtime_types: ['langchain', 'codex'],
    });
    if (p === '/api/v1/agent-runtime/capabilities') return json(route, {
      protocol_version: 1,
      runtime_type: 'langchain',
      runtime_available: true,
      authenticated: true,
      source: 'visual-review-fixture',
      models: [],
      default_model_id: null,
      error_code: null,
    });
    if (p === '/api/v1/organizations') return json(route, {
      items: [{
        organization_id: ME.tenant_id,
        kind: 'personal',
        slug: 'demo',
        name: L('演示空间', 'Demo workspace'),
        membership_id: 'membership_demo',
        role: 'owner',
        status: 'active',
        active: true,
        access: { capabilities: ['view', 'create', 'update', 'delete'], effective_role: 'owner', source: 'computed' },
      }],
      active_organization_id: ME.tenant_id,
      session_generation: 1,
    });
    if (p.endsWith('/chats/bootstrap')) return json(route, {
      carrier_scope_id: 'scope_demo',
      surface: q.get('surface') || 'chat',
      available_commands: ['/task', '/deployment', '/knowledge', '/workflow', '/browser'],
      debug_view_enabled: true,
    });
    if (p.endsWith('/chats/workspace')) return json(route, {
      workspace_scope_id: 'workspace_demo',
      mount_scope_id: 'workspace_demo',
      chat_id: q.get('chat_id') || 'chat_demo',
    });
    if (/\/chat-scopes\/[^/]+\/chats$/.test(p)) return json(route, {
      items: [], total: 0, limit: 50, offset: 0,
    });
    if (/\/chat-scopes\/[^/]+\/active-runs$/.test(p)) return json(route, []);
    if (/\/chat-scopes\/[^/]+\/chats\/[^/]+\/messages$/.test(p)) return json(route, {
      items: [], total: 0, limit: 200, offset: 0,
    });
    if (p.includes('/sandbox-status')) return json(route, { items: [] });
    if (p === '/api/v1/llm-credentials') return json(route, []);
    if (p === '/api/v1/mcp-servers') return json(route, { items: [] });
    if (p === '/api/v1/storage/list') return json(route, {
      path: q.get('path') || '/',
      items: [],
      next_cursor: null,
      total_estimate: 0,
      readonly: false,
    });
    if (/\/chat-scopes\/[^/]+\/chats\/[^/]+\/state$/.test(p)) return json(route, {
      todo_items: [], background_jobs: [], active_modes: [], mcp_server_ids: [], mcp_config_revision: 0,
    });
    if (p.endsWith('/vfs/content')) {
      const path = q.get('path') ?? '';
      const c = VFS_CONTENT[path];
      return c ? json(route, c) : json(route, { detail: 'not found' }, 404);
    }
    if (p.endsWith('/vfs')) return json(route, VFS_LIST);
    if (p.match(/\/workflows\/[^/]+\/workspace$/)) return json(route, {
      workflow_scope_id: WF_ID,
      mount_scope_id: 'mount_demo',
    });
    if (p.match(/\/workflows\/[^/]+\/versions$/)) return json(route, VERSIONS);
    if (p.match(/\/workflows\/[^/]+\/chats$/)) return json(route, { items: [], total: 0, limit: 50, offset: 0 });
    if (p.match(/\/workflows\/[^/]+\/chats\//)) return json(route, { messages: [] });
    if (p.match(/\/workflows\/[^/]+$/) && !p.endsWith('/workflows')) return json(route, SNAPSHOT);
    if (p.endsWith('/workflows')) return json(route, WORKSPACE_LIST);
    if (p.includes('/templates')) return json(route, { items: [
      { template_id: 'tpl_1', name: L('情感分类', 'Sentiment Classifier'), node_type: 'PromptNode', description: L('用大模型判断文本情感', 'Classify text sentiment with an LLM'), tags: ['nlp'] },
      { template_id: 'tpl_2', name: L('摘要生成', 'Summarizer'), node_type: 'PromptNode', description: L('生成简短摘要', 'Generate a short summary'), tags: ['nlp'] },
      { template_id: 'tpl_3', name: L('CSV 解析', 'CSV Parser'), node_type: 'CodeNode', description: L('解析 CSV 为结构化数据', 'Parse CSV into rows'), tags: ['data'] },
    ], total: 3, limit: 50, offset: 0 });
    if (p.includes('/deployments')) return json(route, { items: [], total: 0, limit: 50, offset: 0 });
    if (p.includes('/tasks')) return json(route, { items: [], total: 0, limit: 50, offset: 0 });
    // benign empty 200 so nothing 500s/hangs
    return json(route, {});
  });
}

// ---- capture ---------------------------------------------------------------
const shots = [];
async function assertSidebarUtilityAlignment(page) {
  const sidebar = page.locator('[data-testid="app-sidebar"]');
  if (await sidebar.count() === 0 || !(await sidebar.isVisible())) return;
  const issues = [];

  const settings = sidebar.locator('a[href="/settings"]').first();
  if (await settings.count()) {
    const iconBox = await settings.locator('svg').first().boundingBox();
    const labelBox = await settings.locator(':scope > span').first().boundingBox();
    if (iconBox && labelBox) {
      const iconCenter = iconBox.y + iconBox.height / 2;
      const labelCenter = labelBox.y + labelBox.height / 2;
      if (Math.abs(iconCenter - labelCenter) > 1) {
        issues.push(`Settings is off baseline by ${Math.abs(iconCenter - labelCenter).toFixed(1)}px (icon=${JSON.stringify(iconBox)}, label=${JSON.stringify(labelBox)})`);
      }
    }
  }

  const organizationBox = await sidebar.locator('[data-testid="organization-switcher"]').boundingBox();
  const userBox = await sidebar.locator('[data-action="open-user-menu"]').boundingBox();
  if (organizationBox && userBox) {
    const organizationCenter = organizationBox.y + organizationBox.height / 2;
    const userCenter = userBox.y + userBox.height / 2;
    if (Math.abs(organizationCenter - userCenter) > 1) {
      issues.push(`workspace and user controls are off baseline by ${Math.abs(organizationCenter - userCenter).toFixed(1)}px`);
    }
  }
  if (issues.length) throw new Error(`sidebar utilities: ${issues.join('; ')}`);
}

async function assertResponsiveShell(page) {
  const viewport = page.viewportSize();
  if (!viewport || viewport.width >= 768) return;
  const pathname = new URL(page.url()).pathname;
  if (!pathname.startsWith('/workflow/')) {
    const desktopSidebar = page.locator('[data-testid="app-sidebar"]').first();
    if (await desktopSidebar.count() && await desktopSidebar.isVisible()) {
      throw new Error('desktop sidebar is visible in a narrow viewport');
    }
    const mobileHeader = page.locator('[data-testid="mobile-app-header"]');
    if (!(await mobileHeader.isVisible())) {
      throw new Error('mobile navigation header is not visible');
    }
  }
  const overflow = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
  if (overflow.scrollWidth > overflow.clientWidth + 1) {
    throw new Error(`horizontal page overflow: ${overflow.scrollWidth}px > ${overflow.clientWidth}px`);
  }
  if (pathname === '/chat') {
    const composer = page.locator('[data-role="agent-composer-input"]').first();
    const box = await composer.boundingBox();
    if (!box || box.x < 0 || box.x + box.width > viewport.width + 1 || box.width < 240) {
      throw new Error(`Chat composer does not fit the mobile viewport: ${JSON.stringify(box)}`);
    }
  }
}

async function shoot(page, name, label) {
  try {
    await page.waitForLoadState('networkidle').catch(() => {});
    await page.waitForTimeout(900);
    const bodyText = await page.locator('body').innerText();
    if (/Something went wrong|Unexpected Application Error|Application crashed/i.test(bodyText)) {
      throw new Error('route rendered the application error boundary');
    }
    await assertSidebarUtilityAlignment(page);
    await assertResponsiveShell(page);
    if (process.env.AXE === '1') {
      const results = await new AxeBuilder({ page })
        .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
        .exclude('.react-flow__renderer')
        .exclude('.react-flow__attribution')
        .analyze();
      const severe = results.violations.filter((violation) =>
        violation.impact === 'serious' || violation.impact === 'critical');
      if (severe.length > 0) {
        console.error(`AXE ${name}`, JSON.stringify(severe.map((violation) => ({
          id: violation.id,
          help: violation.help,
          nodes: violation.nodes.map((node) => ({
            target: node.target,
            html: node.html,
            failureSummary: node.failureSummary,
          })),
        })), null, 2));
        throw new Error(`axe serious/critical violations: ${severe.map((violation) => violation.id).join(', ')}`);
      }
    }
    const file = resolve(OUT, PREFIX + name);
    await page.screenshot({ path: file, fullPage: false });
    shots.push(`OK  ${PREFIX + name}  — ${label}`);
  } catch (err) {
    shots.push(`FAIL ${name}  — ${label}: ${err.message}`);
  }
}

const run = async () => {
  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  await context.addInitScript(({ lng, theme }) => {
    window.localStorage.setItem('vibecanvas.token', 'demo-token');
    window.localStorage.setItem('vibecanvas.locale', lng);
    window.localStorage.setItem('theme', theme);
  }, { lng: LOCALE, theme: THEME });

  const page = await context.newPage();
  page.setDefaultNavigationTimeout(60_000);
  await installStubs(page);
  page.on('console', (m) => { if (m.type() === 'error') console.log('PAGE ERR:', m.text()); });
  page.on('pageerror', (e) => console.log('PAGE CRASH:', e.message));

  // a. workspace
  try {
    await page.goto(`${BASE}/workspace`, { waitUntil: 'domcontentloaded' });
    await shoot(page, '01-workspace.png', 'workflow list');
  } catch (e) { shots.push(`FAIL 01-workspace.png: ${e.message}`); }

  // Every management route is rendered by the maintained harness so Shell,
  // spacing and empty/error contracts can be reviewed without a backend.
  for (const [name, path, label] of [
    ['05-chat.png', '/chat', 'Chat workbench'],
    ['06-tasks.png', '/tasks', 'Task center'],
    ['07-deployments.png', '/deployments', 'Deployments'],
    ['08-credentials.png', '/credentials', 'Credentials'],
    ['09-mcp.png', '/mcp-servers', 'MCP management'],
    ['10-skills.png', '/skills', 'Skills management'],
    ['11-knowledge.png', '/knowledge', 'Knowledge management'],
    ['12-storage.png', '/storage', 'Storage'],
    ['13-settings.png', '/settings', 'Settings'],
  ]) {
    try {
      await page.goto(`${BASE}${path}`, { waitUntil: 'domcontentloaded' });
      await shoot(page, name, label);
    } catch (e) { shots.push(`FAIL ${name}: ${e.message}`); }
  }

  // b. canvas
  try {
    await page.goto(`${BASE}/workflow/${WF_ID}`, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(1200);
    // Re-center so all three nodes are visible (default fitView lands before
    // the inspector overlay settles). The xyflow Controls "fit view" button.
    await page.locator('.react-flow__controls-fitview').click({ timeout: 3000 }).catch(() => {});
    await page.waitForTimeout(600);
    await shoot(page, '02-canvas.png', 'canvas + nodes + inspector (explorer collapsed)');
  } catch (e) { shots.push(`FAIL 02-canvas.png: ${e.message}`); }

  // c. explorer open
  try {
    await page.locator('[data-action="files"]').click({ timeout: 5000 });
    await page.waitForTimeout(3000);
    await shoot(page, '03-explorer-open.png', 'left Explorer: Versions + Files (stale marker)');
  } catch (e) { shots.push(`FAIL 03-explorer-open.png: ${e.message}`); }

  // d. file modal (table)
  try {
    await page.getByRole('treeitem', { name: /mount/i }).click({ timeout: 5000 });
    await page.getByText('cells_1.jsonl', { exact: true }).first().dblclick({ timeout: 15000 });
    await page.waitForTimeout(900);
    await shoot(page, '04-file-modal.png', 'file content modal with rendered JSONL table');
  } catch (e) { shots.push(`FAIL 04-file-modal.png: ${e.message}`); }
  // close modal
  await page.keyboard.press('Escape').catch(() => {});

  // Representative 390px captures cover responsive shell/content behavior;
  // desktop captures above continue to cover every maintained route.
  if (process.env.INCLUDE_MOBILE === '1') {
    const mobile = await browser.newContext({ viewport: { width: 390, height: 844 } });
    await mobile.addInitScript(({ lng, theme }) => {
      window.localStorage.setItem('vibecanvas.token', 'demo-token');
      window.localStorage.setItem('vibecanvas.locale', lng);
      window.localStorage.setItem('theme', theme);
    }, { lng: LOCALE, theme: THEME });
    const mobilePage = await mobile.newPage();
    mobilePage.setDefaultNavigationTimeout(60_000);
    await installStubs(mobilePage);
    for (const [name, path, label] of [
      ['20-mobile-workspace.png', '/workspace', '390px workflow list'],
      ['21-mobile-chat.png', '/chat', '390px Chat'],
      ['22-mobile-knowledge.png', '/knowledge', '390px Knowledge'],
      ['23-mobile-canvas.png', `/workflow/${WF_ID}`, '390px canvas'],
      ['24-mobile-storage.png', '/storage', '390px Storage'],
      ['25-mobile-settings.png', '/settings', '390px Settings'],
      ['26-mobile-tasks.png', '/tasks', '390px Tasks'],
    ]) {
      try {
        await mobilePage.goto(`${BASE}${path}`, { waitUntil: 'domcontentloaded' });
        await shoot(mobilePage, name, label);
      } catch (e) { shots.push(`FAIL ${name}: ${e.message}`); }
    }
    await mobile.close();
  }

  // e. login (no token)
  try {
    const ctx2 = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    await ctx2.addInitScript(({ lng, theme }) => {
      window.localStorage.removeItem('vibecanvas.token');
      window.localStorage.setItem('vibecanvas.locale', lng);
      window.localStorage.setItem('theme', theme);
    }, { lng: LOCALE, theme: THEME });
    const p2 = await ctx2.newPage();
    await installStubs(p2);
    await p2.goto(`${BASE}/login`, { waitUntil: 'domcontentloaded' });
    await shoot(p2, '00-login.png', 'login screen (no token)');
    await ctx2.close();
  } catch (e) { shots.push(`FAIL 00-login.png: ${e.message}`); }

  await browser.close();
  console.log('\n=== SCREENSHOT RESULTS ===');
  for (const s of shots) console.log(s);
  if (shots.some((s) => s.startsWith('FAIL'))) {
    throw new Error('One or more visual review captures failed');
  }
};

run().then(() => process.exit(0)).catch((e) => { console.error(e); process.exit(1); });
