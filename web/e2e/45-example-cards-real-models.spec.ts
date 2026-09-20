import fs from 'node:fs';

import { expect, test, type BrowserContext, type Page } from '@playwright/test';

const API_BASE = process.env.VIBECANVAS_API_BASE ?? 'http://localhost:8000';
const APP_ORIGIN = process.env.VIBECANVAS_E2E_ORIGIN
  ?? `http://${process.env.VIBECANVAS_E2E_HOST ?? 'localhost'}:${process.env.VIBECANVAS_WEB_PORT ?? '9001'}`;
const SESSION_FILE = process.env.FLOWORK_E2E_EXISTING_SESSION_FILE;
const MESSAGE_PATH = /\/api\/v1\/chat-scopes\/([^/]+)\/chats\/([^/]+)\/messages$/;

interface ExistingSession {
  session: string;
  csrf: string;
}

interface ModelCapability {
  id: string;
  label: string;
  api_source?: string | null;
  provider_model_id?: string | null;
}

interface ExampleCase {
  id: string;
  tab: string;
  command: string;
  expectedTools: readonly (string | readonly string[])[];
}

const EXAMPLES: readonly ExampleCase[] = [
  { id: 'office:presentation', tab: 'Office', command: '/document', expectedTools: ['review_document', 'render_document_feedback', 'render_interactive'] },
  { id: 'office:report', tab: 'Office', command: '/document', expectedTools: ['review_document', 'render_document_feedback', 'render_interactive'] },
  { id: 'office:spreadsheet', tab: 'Office', command: '/document', expectedTools: ['review_document', 'render_document_feedback', 'render_interactive'] },
  { id: 'diagram:architecture', tab: 'Diagram', command: '/diagram', expectedTools: ['save_drawio_file', 'render_interactive'] },
  { id: 'diagram:process', tab: 'Diagram', command: '/diagram', expectedTools: ['save_drawio_file', 'render_interactive'] },
  { id: 'diagram:sequence', tab: 'Diagram', command: '/diagram', expectedTools: ['save_drawio_file', 'render_interactive'] },
  { id: 'workflow:feedback', tab: 'Workflow', command: '/workflow', expectedTools: ['create_workflow', 'check_workflow', 'update_canvas'] },
  { id: 'workflow:research', tab: 'Workflow', command: '/workflow', expectedTools: ['create_workflow', 'check_workflow', 'update_canvas'] },
  { id: 'workflow:invoices', tab: 'Workflow', command: '/workflow', expectedTools: ['create_workflow', 'check_workflow', 'update_canvas'] },
  { id: 'operations:batch', tab: 'Tasks and deployments', command: '/workflow', expectedTools: ['batch_execute'] },
  { id: 'operations:schedule', tab: 'Tasks and deployments', command: '/task', expectedTools: ['task_create_scheduled_run'] },
  { id: 'operations:deploy', tab: 'Tasks and deployments', command: '/deployment', expectedTools: [['deployment_create', 'deployment_update']] },
  { id: 'knowledge:create', tab: 'Knowledge', command: '/knowledge', expectedTools: ['knowledge_create'] },
  { id: 'knowledge:explore', tab: 'Knowledge', command: '/knowledge', expectedTools: [['knowledge_list', 'knowledge_search'], 'knowledge_get'] },
  { id: 'knowledge:update', tab: 'Knowledge', command: '/knowledge', expectedTools: [['knowledge_update', 'knowledge_create']] },
];

function sessionMaterial(): ExistingSession {
  if (!SESSION_FILE) throw new Error('FLOWORK_E2E_EXISTING_SESSION_FILE is required');
  const material = JSON.parse(fs.readFileSync(SESSION_FILE, 'utf8')) as ExistingSession;
  if (!material.session || !material.csrf) throw new Error('existing Session is incomplete');
  return material;
}

async function api(
  session: ExistingSession,
  path: string,
  init: RequestInit = {},
  acceptFailure = false,
): Promise<Response> {
  const method = (init.method ?? 'GET').toUpperCase();
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      Cookie: `vibecanvas-web-session=${session.session}; vibecanvas-web-csrf=${session.csrf}`,
      Origin: APP_ORIGIN,
      ...(method === 'GET' || method === 'HEAD' ? {} : { 'X-CSRF-Token': session.csrf }),
      ...init.headers,
    },
  });
  if (!response.ok && !acceptFailure) {
    throw new Error(`${method} ${path} failed: ${response.status} ${await response.text()}`);
  }
  return response;
}

async function seed(context: BrowserContext, session: ExistingSession) {
  await context.addCookies([
    { name: 'vibecanvas-web-session', value: session.session, url: APP_ORIGIN },
    { name: 'vibecanvas-web-csrf', value: session.csrf, url: APP_ORIGIN },
  ]);
  await context.addInitScript(() => window.localStorage.setItem('vibecanvas.locale', 'en'));
}

function rows(payload: unknown): Array<Record<string, unknown>> {
  if (Array.isArray(payload)) return payload as Array<Record<string, unknown>>;
  if (payload && typeof payload === 'object' && Array.isArray((payload as { items?: unknown }).items)) {
    return (payload as { items: Array<Record<string, unknown>> }).items;
  }
  return [];
}

function rowId(row: Record<string, unknown>): string {
  return String(row.id ?? row.wf_id ?? row.task_id ?? row.deployment_id ?? '');
}

interface StreamToolEvent {
  type: 'tool_start' | 'tool_end';
  name: string;
  status?: string;
}

function streamToolEvents(stream: string): StreamToolEvent[] {
  const events: StreamToolEvent[] = [];
  for (const block of stream.split(/\r?\n\r?\n/u)) {
    const data = block.split(/\r?\n/u)
      .filter((line) => line.startsWith('data:'))
      .map((line) => line.slice(5).trimStart())
      .join('\n');
    if (!data) continue;
    try {
      const event = JSON.parse(data) as Record<string, unknown>;
      const payload = (
        event.payload && typeof event.payload === 'object'
          ? event.payload
          : event
      ) as Record<string, unknown>;
      if (payload.type !== 'tool_start' && payload.type !== 'tool_end') continue;
      events.push({
        type: payload.type,
        name: String(payload.name ?? ''),
        status: payload.status == null ? undefined : String(payload.status),
      });
    } catch {
      // Keepalive and terminal SSE frames need no tool projection.
    }
  }
  return events;
}

function terminalStreamEvent(stream: string): 'done' | 'error' | null {
  const names = [...stream.matchAll(/^event:\s*(done|error)\s*$/gmu)];
  return (names.at(-1)?.[1] as 'done' | 'error' | undefined) ?? null;
}

function unrecoveredToolErrors(events: StreamToolEvent[]): StreamToolEvent[] {
  return events.filter((event, index) => (
    event.type === 'tool_end'
    && event.status === 'error'
    && !events.slice(index + 1).some((candidate) => (
      candidate.type === 'tool_end'
      && candidate.status === 'done'
    ))
  ));
}

function latestDurableEventId(stream: string, fallback: number): number {
  return [...stream.matchAll(/^id:\s*(\d+)\s*$/gmu)].reduce(
    (latest, match) => Math.max(latest, Number(match[1])),
    fallback,
  );
}

async function replayTurnToTerminal(
  session: ExistingSession,
  chatId: string,
  turnId: string,
): Promise<string> {
  const chunks: string[] = [];
  let cursor = 0;
  for (let attempt = 0; attempt < 20; attempt += 1) {
    const replay = await api(
      session,
      `/api/v1/chats/${encodeURIComponent(chatId)}`
        + `/turns/${encodeURIComponent(turnId)}/stream`,
      { headers: cursor > 0 ? { 'Last-Event-ID': String(cursor) } : {} },
    );
    const chunk = await replay.text();
    chunks.push(chunk);
    cursor = latestDurableEventId(chunk, cursor);
    const combined = chunks.join('\n\n');
    if (terminalStreamEvent(combined)) return combined;
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 250));
  }
  throw new Error(`Turn ${turnId} replay closed repeatedly without a terminal event`);
}

async function ids(session: ExistingSession, path: string): Promise<Set<string>> {
  const response = await api(session, path, {}, true);
  if (!response.ok) return new Set();
  return new Set(rows(await response.json()).map(rowId).filter(Boolean));
}

async function selectModel(page: Page, model: ModelCapability) {
  const picker = page.locator('[data-role="chat-model-select"]');
  await expect(picker).toBeEnabled({ timeout: 30_000 });
  await picker.click();
  await page.locator(
    `[data-role="chat-model-source-option"][data-model-source="${model.api_source}"]`,
  ).click();
  if (model.api_source === 'openrouter_oauth') {
    await page.getByPlaceholder(/Search models, providers, or free/i)
      .fill(model.provider_model_id ?? model.id);
  }
  const option = page.locator(
    `[data-role="chat-model-option"][data-model-id="${model.id}"]`,
  );
  await expect(option).toHaveCount(1);
  await option.click();
}

test.skip(!SESSION_FILE, 'requires the user-authorized existing account Session');
test.setTimeout(1_800_000);

for (const source of ['chatgpt_account', 'openrouter_oauth'] as const) {
  test.describe.serial(`Empty Chat examples through Codex and ${source}`, () => {
    const session = SESSION_FILE ? sessionMaterial() : null;
    let model: ModelCapability | undefined;
    const chats = new Array<{ scopeId: string; chatId: string; turnId: string }>();
    const baseline = new Map<string, Set<string>>();

    test.beforeAll(async () => {
      if (!session) return;
      await api(session, '/api/v1/agent-runtime/settings', {
        method: 'PUT',
        body: JSON.stringify({ default_runtime_type: 'codex' }),
      });
      const capabilities = await api(session, '/api/v1/agent-runtime/capabilities')
        .then((response) => response.json()) as {
          models: ModelCapability[];
          default_model_id?: string | null;
        };
      model = source === 'chatgpt_account'
        ? capabilities.models.find((candidate) => (
          candidate.api_source === source && candidate.id === capabilities.default_model_id
        )) ?? capabilities.models.find((candidate) => candidate.api_source === source)
        : capabilities.models.find((candidate) => (
          candidate.api_source === source && candidate.provider_model_id === 'stealth/ox-alpha'
        ));
      if (!model) throw new Error(`${source} model is unavailable`);
      for (const path of [
        '/api/v1/workflows',
        '/api/v1/kb',
        '/api/v1/tasks?task_type=scheduled_run&limit=100',
        '/api/v1/deployments?limit=100&offset=0',
      ]) baseline.set(path, await ids(session, path));
    });

    test.afterAll(async () => {
      if (!session) return;
      for (const chat of chats) {
        await api(
          session,
          `/api/v1/chats/${encodeURIComponent(chat.chatId)}`
            + `/turns/${encodeURIComponent(chat.turnId)}/cancel`,
          { method: 'POST' },
          true,
        );
        for (let attempt = 0; attempt < 20; attempt += 1) {
          const response = await api(
            session,
            `/api/v1/chat-scopes/${encodeURIComponent(chat.scopeId)}`
              + `/chats/${encodeURIComponent(chat.chatId)}`,
            { method: 'DELETE' },
            true,
          );
          if (response.ok || response.status === 404) break;
          await new Promise((resolvePromise) => setTimeout(resolvePromise, 250));
        }
      }
      const cleanup: Array<{ list: string; remove: (id: string) => string }> = [
        { list: '/api/v1/deployments?limit=100&offset=0', remove: (id) => `/api/v1/deployments/${encodeURIComponent(id)}` },
        { list: '/api/v1/tasks?task_type=scheduled_run&limit=100', remove: (id) => `/api/v1/tasks/scheduled-runs/${encodeURIComponent(id)}` },
        { list: '/api/v1/kb', remove: (id) => `/api/v1/kb/${encodeURIComponent(id)}` },
        { list: '/api/v1/workflows', remove: (id) => `/api/v1/workflows/${encodeURIComponent(id)}` },
      ];
      for (const resource of cleanup) {
        const before = baseline.get(resource.list) ?? new Set<string>();
        for (const id of await ids(session, resource.list)) {
          if (!before.has(id)) await api(session, resource.remove(id), { method: 'DELETE' }, true);
        }
      }
    });

    test.beforeEach(async ({ context }) => {
      if (session) await seed(context, session);
    });

    for (const example of EXAMPLES) {
      test(`${source} ${example.id} runs the exact card prompt`, async ({ page }) => {
        if (!session || !model) return;
        await page.goto('/chat', { waitUntil: 'domcontentloaded' });
        await expect(page.locator('[data-role="agent-composer-input"]')).toBeVisible({
          timeout: 30_000,
        });
        await page.locator('[data-action="chat-new"]').click();
        await expect(page.locator('[data-role="agent-composer-input"]')).toBeEnabled({
          timeout: 30_000,
        });
        await expect(page.locator('[data-role="empty-chat-examples"]')).toBeVisible({
          timeout: 30_000,
        });
        await selectModel(page, model);
        await page.getByRole('tab', { name: example.tab, exact: true }).click();
        await page.locator(`[data-example-id="${example.id}"]`).click();

        const composer = page.locator('[data-role="agent-composer-input"]');
        const prompt = await composer.inputValue();
        expect(prompt).toMatch(new RegExp(`^${example.command.replace('/', '\\/')}\\s`));
        const [messageResponse] = await Promise.all([
          page.waitForResponse((response) => (
            response.request().method() === 'POST'
            && MESSAGE_PATH.test(new URL(response.url()).pathname)
          ), { timeout: 30_000 }),
          page.locator('[data-action="agent-composer-send"]').click(),
        ]);
        expect(messageResponse.ok()).toBe(true);
        const match = new URL(messageResponse.url()).pathname.match(MESSAGE_PATH);
        const scopeId = match?.[1];
        const chatId = match?.[2];
        const turnId = messageResponse.headers()['x-turn-id'];
        expect(scopeId).toBeTruthy();
        expect(chatId).toBeTruthy();
        expect(turnId).toMatch(/^t_/);
        chats.push({ scopeId: scopeId!, chatId: chatId!, turnId: turnId! });

        const stream = await replayTurnToTerminal(session, chatId!, turnId!);
        const error = [...stream.matchAll(/^event: error\ndata: (.+)$/gm)].at(-1)?.[1];
        if (error) throw new Error(`${source} ${example.id} failed: ${error}`);
        expect(stream).not.toContain('Defaulting to fallback metadata');
        const toolEvents = streamToolEvents(stream);
        expect(
          unrecoveredToolErrors(toolEvents),
          'every failed tool attempt must be followed by a successful recovery tool call',
        ).toEqual([]);
        const completedTools = new Set(toolEvents
          .filter((event) => event.type === 'tool_end' && event.status === 'done')
          .map((event) => event.name));
        const missingTools = example.expectedTools.filter((requirement) => (
          typeof requirement === 'string'
            ? !completedTools.has(requirement)
            : !requirement.some((name) => completedTools.has(name))
        ));
        expect(
          missingTools,
          `expected every required tool or alternative group to complete: ${example.expectedTools
            .map((requirement) => (
              typeof requirement === 'string' ? requirement : `(${requirement.join(' or ')})`
            ))
            .join(', ')}`,
        ).toEqual([]);
        await expect(page.locator('[data-role="agent-thinking"]')).toHaveCount(0, {
          timeout: 900_000,
        });
        // The transcript intentionally compacts long tool sequences into
        // activity groups. Durable SSE is the authoritative tool contract;
        // the browser assertion here only requires the completed answer to be
        // visibly present after that projection has settled.
        await expect(page.locator('[data-message-role="assistant"]').last()).toBeVisible();
      });
    }
  });
}

test.describe.serial('Example Card conversation continuity after sandbox release', () => {
  const session = SESSION_FILE ? sessionMaterial() : null;
  let model: ModelCapability | undefined;
  let scopeId = '';
  let chatId = '';
  let workflowId = '';
  let workflowBaseline = new Set<string>();

  test.beforeAll(async () => {
    if (!session) return;
    await api(session, '/api/v1/agent-runtime/settings', {
      method: 'PUT',
      body: JSON.stringify({ default_runtime_type: 'codex' }),
    });
    const capabilities = await api(session, '/api/v1/agent-runtime/capabilities')
      .then((response) => response.json()) as { models: ModelCapability[] };
    model = capabilities.models.find((candidate) => (
      candidate.api_source === 'openrouter_oauth'
      && candidate.provider_model_id === 'stealth/ox-alpha'
    ));
    if (!model) throw new Error('OpenRouter Ox Alpha is unavailable');
    workflowBaseline = await ids(session, '/api/v1/workflows');
  });

  test.afterAll(async () => {
    if (!session) return;
    if (chatId && scopeId) {
      await api(
        session,
        `/api/v1/chat-scopes/${encodeURIComponent(scopeId)}`
          + `/chats/${encodeURIComponent(chatId)}`,
        { method: 'DELETE' },
        true,
      );
    }
    if (workflowId) {
      await api(
        session,
        `/api/v1/workflows/${encodeURIComponent(workflowId)}`,
        { method: 'DELETE' },
        true,
      );
    }
  });

  test.beforeEach(async ({ context }) => {
    if (session) await seed(context, session);
  });

  test('continues the exact workflow created from the Example Card', async ({ page }) => {
    if (!session || !model) return;
    await page.goto('/chat', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('[data-role="agent-composer-input"]')).toBeEnabled({
      timeout: 30_000,
    });
    await page.locator('[data-action="chat-new"]').click();
    await expect(page.locator('[data-role="empty-chat-examples"]')).toBeVisible({
      timeout: 30_000,
    });
    await selectModel(page, model);
    await page.getByRole('tab', { name: 'Workflow', exact: true }).click();
    await page.locator('[data-example-id="workflow:feedback"]').click();

    const composer = page.locator('[data-role="agent-composer-input"]');
    const cardPrompt = await composer.inputValue();
    expect(cardPrompt).toMatch(/^\/workflow Build a compact customer-feedback workflow/u);
    const [firstResponse] = await Promise.all([
      page.waitForResponse((response) => (
        response.request().method() === 'POST'
        && MESSAGE_PATH.test(new URL(response.url()).pathname)
      ), { timeout: 30_000 }),
      page.locator('[data-action="agent-composer-send"]').click(),
    ]);
    expect(firstResponse.ok()).toBe(true);
    const firstMatch = new URL(firstResponse.url()).pathname.match(MESSAGE_PATH);
    scopeId = firstMatch?.[1] ?? '';
    chatId = firstMatch?.[2] ?? '';
    const firstTurnId = firstResponse.headers()['x-turn-id'];
    expect(scopeId).toBeTruthy();
    expect(chatId).toBeTruthy();
    expect(firstTurnId).toMatch(/^t_/);
    const firstStream = await replayTurnToTerminal(session, chatId, firstTurnId);
    expect(firstStream).not.toMatch(/^event: error$/mu);
    const firstTools = streamToolEvents(firstStream);
    expect(firstTools).toEqual(expect.arrayContaining([
      expect.objectContaining({ type: 'tool_end', name: 'create_workflow', status: 'done' }),
      expect.objectContaining({ type: 'tool_end', name: 'check_workflow', status: 'done' }),
      expect.objectContaining({ type: 'tool_end', name: 'update_canvas', status: 'done' }),
    ]));
    expect(firstTools.some((event) => event.name === 'run_workflow')).toBe(false);
    expect(firstTools.some((event) => event.type === 'tool_end' && event.status === 'error'))
      .toBe(false);
    await expect(page.locator('[data-role="agent-thinking"]')).toHaveCount(0, {
      timeout: 900_000,
    });
    await expect(page.locator(
      '[data-role="tool-call"][data-tool-status="error"]',
    )).toHaveCount(0);

    const workflowsAfterFirst = await ids(session, '/api/v1/workflows');
    const created = [...workflowsAfterFirst].filter((id) => !workflowBaseline.has(id));
    expect(created).toHaveLength(1);
    [workflowId] = created;

    const released = await api(
      session,
      `/api/v1/chats/sandbox?chat_id=${encodeURIComponent(chatId)}`,
      { method: 'DELETE' },
    ).then((response) => response.json()) as { status: string };
    expect(released.status).toBe('closed');

    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.locator(`button[data-chat-id="${chatId}"]`).click();
    await expect(composer).toBeEnabled({ timeout: 60_000 });
    const followUp = [
      'Continue the customer-feedback workflow you created in the previous Turn.',
      'Add an explicit PII-redaction step immediately before response drafting,',
      'then validate the updated workflow. Update the same workflow; do not create another one.',
      'In your final reply, state the original node-count constraint from my first message.',
    ].join(' ');
    await composer.fill(followUp);
    const [secondResponse] = await Promise.all([
      page.waitForResponse((response) => (
        response.request().method() === 'POST'
        && MESSAGE_PATH.test(new URL(response.url()).pathname)
      ), { timeout: 30_000 }),
      page.locator('[data-action="agent-composer-send"]').click(),
    ]);
    expect(secondResponse.ok()).toBe(true);
    const secondTurnId = secondResponse.headers()['x-turn-id'];
    expect(secondTurnId).toMatch(/^t_/);
    const secondStream = await replayTurnToTerminal(session, chatId, secondTurnId);
    expect(secondStream).not.toMatch(/^event: error$/mu);
    const secondTools = streamToolEvents(secondStream);
    expect(secondTools.some((event) => event.name === 'create_workflow')).toBe(false);
    expect(secondTools).toEqual(expect.arrayContaining([
      expect.objectContaining({ type: 'tool_end', name: 'get_workflow', status: 'done' }),
      expect.objectContaining({ type: 'tool_end', name: 'check_workflow', status: 'done' }),
      expect.objectContaining({ type: 'tool_end', name: 'update_canvas', status: 'done' }),
    ]));
    expect(secondTools.some((event) => event.type === 'tool_end' && event.status === 'error'))
      .toBe(false);
    await expect(page.locator('[data-role="agent-thinking"]')).toHaveCount(0, {
      timeout: 900_000,
    });
    await expect(page.locator('[data-message-role="assistant"]').last()).toContainText(
      /PII|redact/i,
      { timeout: 60_000 },
    );
    await expect(page.locator('[data-message-role="assistant"]').last()).toContainText(
      /5\s*[–-]\s*7/u,
      { timeout: 60_000 },
    );
    expect(
      [...await ids(session, '/api/v1/workflows')]
        .filter((id) => !workflowBaseline.has(id)),
    ).toEqual([workflowId]);

    const history = await api(
      session,
      `/api/v1/chat-scopes/${encodeURIComponent(scopeId)}`
        + `/chats/${encodeURIComponent(chatId)}/messages?limit=500&tail=true`,
    ).then((response) => response.json()) as {
      items: Array<{ role: string; content: string }>;
    };
    expect(history.items.some((item) => item.role === 'user' && item.content === cardPrompt))
      .toBe(true);
    expect(history.items.some((item) => item.role === 'user' && item.content === followUp))
      .toBe(true);

    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.locator(`button[data-chat-id="${chatId}"]`).click();
    await expect(page.locator('[data-message-role="assistant"]').last()).toContainText(
      /PII|redact/i,
      { timeout: 30_000 },
    );
    await expect(page.locator('[data-message-role="assistant"]').last()).toContainText(
      /5\s*[–-]\s*7/u,
      { timeout: 30_000 },
    );
  });
});
