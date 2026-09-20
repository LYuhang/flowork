import fs from 'node:fs';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';

import {
  chromium,
  expect,
  test,
  type BrowserContext,
  type Frame,
  type Locator,
  type Page,
} from '@playwright/test';

const API_BASE = process.env.VIBECANVAS_API_BASE ?? 'http://localhost:8000';
const APP_ORIGIN = process.env.VIBECANVAS_E2E_ORIGIN
  ?? `http://${process.env.VIBECANVAS_E2E_HOST ?? 'localhost'}:${process.env.VIBECANVAS_WEB_PORT ?? '9001'}`;
const SESSION_FILE = process.env.FLOWORK_E2E_EXISTING_SESSION_FILE;
const RECOVERY_CHAT_ID = process.env.FLOWORK_E2E_HISTORY_RECOVERY_CHAT_ID;
const MESSAGE_PATH = /\/api\/v1\/chat-scopes\/([^/]+)\/chats\/([^/]+)\/messages$/;
const EXTENSION_PATH = fileURLToPath(new URL('../../extension/dist/', import.meta.url));
const EXTENSION_ID = 'mkfldhmlgdbpmhplaphhcfcdcoaakcik';

interface ExistingSession {
  session: string;
  csrf: string;
  session_id: string;
  user_id: string;
  organization_id: string;
}

function sessionMaterial(): ExistingSession {
  if (!SESSION_FILE) throw new Error('FLOWORK_E2E_EXISTING_SESSION_FILE is required');
  const material = JSON.parse(fs.readFileSync(SESSION_FILE, 'utf8')) as ExistingSession;
  for (const field of ['session', 'csrf', 'session_id', 'user_id', 'organization_id'] as const) {
    if (!material[field]) throw new Error(`existing Session is missing ${field}`);
  }
  return material;
}

async function api(
  session: ExistingSession,
  path: string,
  init: RequestInit = {},
  acceptedStatuses: readonly number[] = [],
): Promise<Response> {
  const method = (init.method ?? 'GET').toUpperCase();
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      Cookie: `vibecanvas-web-session=${session.session}; vibecanvas-web-csrf=${session.csrf}`,
      Origin: APP_ORIGIN,
      ...(method === 'GET' || method === 'HEAD'
        ? {}
        : { 'X-CSRF-Token': session.csrf }),
      ...init.headers,
    },
  });
  if (!response.ok && !acceptedStatuses.includes(response.status)) {
    throw new Error(`${method} ${path} failed: ${response.status} ${await response.text()}`);
  }
  return response;
}

async function seed(context: BrowserContext, session: ExistingSession) {
  await context.addCookies([
    { name: 'vibecanvas-web-session', value: session.session, url: APP_ORIGIN },
    { name: 'vibecanvas-web-csrf', value: session.csrf, url: APP_ORIGIN },
  ]);
  await context.addInitScript(() => {
    window.localStorage.setItem('vibecanvas.locale', 'en');
  });
}

async function selectOxAlpha(page: Page) {
  const picker = page.locator('[data-role="chat-model-select"]');
  await expect(picker).toBeEnabled({ timeout: 30_000 });
  await picker.click();
  await page.locator(
    '[data-role="chat-model-source-option"][data-model-source="openrouter_oauth"]',
  ).click();
  const search = page.getByPlaceholder(/Search models, providers, or free/i);
  await search.fill('stealth/ox-alpha');
  const option = page.locator('[data-role="chat-model-option"]').filter({
    hasText: 'stealth/ox-alpha',
  });
  await expect(option).toHaveCount(1);
  await option.click();

  await page.locator('[data-role="chat-composer-options-toggle"]').click();
  const effort = page.locator('[data-role="chat-reasoning-effort-select"]');
  await expect(effort).toBeEnabled();
  await effort.click();
  await page.getByRole('option', { name: 'Maximum' }).click();
}

async function selectSurfaceModel(
  surface: { locator(selector: string): Locator },
  source: string,
  modelId: string,
  searchQuery?: string,
) {
  const picker = surface.locator('[data-role="chat-model-select"]');
  await expect(picker).toBeEnabled({ timeout: 30_000 });
  await picker.click({ timeout: 30_000 });
  const sourceOption = surface.locator(
    `[data-role="chat-model-source-option"][data-model-source="${source}"]`,
  );
  await expect(sourceOption).toBeVisible({ timeout: 30_000 });
  await sourceOption.click({ timeout: 30_000 });
  if (searchQuery) {
    const search = surface.locator('input[placeholder*="Search models"]');
    await expect(search).toBeVisible({ timeout: 30_000 });
    await search.fill(searchQuery);
  }
  const modelOption = surface.locator(
    `[data-role="chat-model-option"][data-model-id="${modelId}"]`,
  );
  await expect(modelOption).toBeVisible({ timeout: 30_000 });
  await modelOption.click({ timeout: 30_000 });
  await expect(picker).toHaveAttribute('title', /\S/u, { timeout: 30_000 });
}

async function closeCompactOptions(surface: Frame) {
  const sheet = surface.locator('[data-role="chat-composer-options-sheet"]');
  if (await sheet.isVisible()) {
    await sheet.getByRole('button', { name: /close/i }).click();
    await expect(sheet).toHaveCount(0);
  }
}

async function runFrameTurn(
  panel: Page,
  frame: Frame,
  session: ExistingSession,
  prompt: string,
  expected: string,
) {
  const composer = frame.locator('[data-role="agent-composer-input"]');
  await expect(composer).toBeEnabled({ timeout: 30_000 });
  await composer.fill(prompt);
  const [messageResponse] = await Promise.all([
    panel.waitForResponse((response) => (
      response.request().method() === 'POST'
      && MESSAGE_PATH.test(new URL(response.url()).pathname)
    ), { timeout: 30_000 }),
    frame.locator('[data-action="agent-composer-send"]').click(),
  ]);
  expect(messageResponse.ok()).toBe(true);
  const match = new URL(messageResponse.url()).pathname.match(MESSAGE_PATH);
  const scopeId = match?.[1];
  const chatId = match?.[2];
  expect(scopeId).toBeTruthy();
  const turnId = messageResponse.headers()['x-turn-id'];
  expect(chatId).toBeTruthy();
  expect(turnId).toMatch(/^t_/);
  const replay = await api(
    session,
    `/api/v1/chats/${encodeURIComponent(chatId!)}/turns/${encodeURIComponent(turnId!)}/stream`,
  );
  const stream = await replay.text();
  const error = [...stream.matchAll(/^event: error\ndata: (.+)$/gm)].at(-1)?.[1];
  if (error) throw new Error(`Side-panel Codex Turn failed: ${error}`);
  expect(stream).not.toContain('Defaulting to fallback metadata');
  await expect(frame.locator('[data-role="agent-thinking"]')).toHaveCount(0, {
    timeout: 240_000,
  });
  await expect(frame.locator('[data-message-role="assistant"]').last())
    .toContainText(expected);
  return { scopeId: scopeId!, chatId: chatId!, turnId: turnId!, stream };
}

async function runTurn(
  page: Page,
  session: ExistingSession,
  prompt: string,
) {
  const composer = page.locator('[data-role="agent-composer-input"]');
  await expect(composer).toBeEnabled({ timeout: 30_000 });
  await composer.fill(prompt);
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
  expect(scopeId).toBeTruthy();
  const turnId = messageResponse.headers()['x-turn-id'];
  expect(chatId).toBeTruthy();
  expect(turnId).toMatch(/^t_/);
  const replay = await api(
    session,
    `/api/v1/chats/${encodeURIComponent(chatId!)}/turns/${encodeURIComponent(turnId!)}/stream`,
  );
  const stream = await replay.text();
  const error = [...stream.matchAll(/^event: error\ndata: (.+)$/gm)].at(-1)?.[1];
  if (error) throw new Error(`OpenRouter Codex Turn failed: ${error}`);
  expect(stream).not.toContain(`Model metadata for \`stealth/ox-alpha\` not found`);
  expect(stream).not.toContain('Defaulting to fallback metadata');
  await expect(page.locator('[data-role="agent-thinking"]')).toHaveCount(0, {
    timeout: 180_000,
  });
  await expect(page.locator('[data-action="agent-composer-send"]')).toBeVisible();
  return { scopeId: scopeId!, chatId: chatId!, turnId: turnId!, stream };
}

test.describe('OpenRouter OAuth model through Codex', () => {
  test.skip(!SESSION_FILE, 'requires an explicitly authorized existing account Session');
  test.setTimeout(600_000);
  const session = SESSION_FILE ? sessionMaterial() : null;

  test.beforeAll(async () => {
    if (!session) return;
    await api(session, '/api/v1/agent-runtime/settings', {
      method: 'PUT',
      body: JSON.stringify({ default_runtime_type: 'codex' }),
    });
  });

  test.beforeEach(async ({ context }) => {
    if (session) await seed(context, session);
  });

  test('locks an OpenRouter Chat to its connection across text, tools, and reload', async ({ page }) => {
    if (!session) return;
    const capabilitiesResponse = await api(session, '/api/v1/agent-runtime/capabilities');
    const capabilities = await capabilitiesResponse.json() as {
      models: Array<{ id: string; api_source?: string | null }>;
      default_model_id?: string | null;
    };
    const accountModel = capabilities.models.find((model) => (
      model.api_source === 'chatgpt_account'
      && model.id === capabilities.default_model_id
    )) ?? capabilities.models.find((model) => model.api_source === 'chatgpt_account');
    expect(accountModel).toBeTruthy();
    let first: Awaited<ReturnType<typeof runTurn>> | null = null;
    try {
      await page.goto('/chat', { waitUntil: 'domcontentloaded' });
      await expect(page.locator('[data-role="agent-composer-input"]')).toBeVisible({
        timeout: 30_000,
      });
      await expect(page.locator('[data-role="agent-composer-input"]')).toBeEnabled({
        timeout: 30_000,
      });
      await page.locator('[data-action="chat-new"]').click();
      await expect(page.locator('[data-role="empty-chat-examples"]')).toBeVisible({
        timeout: 30_000,
      });
      await selectOxAlpha(page);

      const marker = `OPENROUTER_OX_${Date.now()}`;
      first = await runTurn(
        page,
        session,
        `Reply with exactly this token and no other text: ${marker}`,
      );
      await expect(page.locator('[data-message-role="assistant"]').last())
        .toContainText(marker);

      const lockedCapabilities = await api(
        session,
        `/api/v1/agent-runtime/capabilities?chat_id=${encodeURIComponent(first.chatId)}`,
      ).then((response) => response.json()) as {
        models: Array<{ id: string; api_source?: string | null }>;
      };
      expect(lockedCapabilities.models.length).toBeGreaterThan(0);
      expect(lockedCapabilities.models.every(
        (model) => model.api_source === 'openrouter_oauth',
      )).toBe(true);

      const rejected = await api(
        session,
        `/api/v1/chat-scopes/${encodeURIComponent(first.scopeId)}`
          + `/chats/${encodeURIComponent(first.chatId)}/messages`,
        {
          method: 'POST',
          body: JSON.stringify({
            role: 'user',
            content: 'This cross-connection Turn must not run.',
            agent_settings: { model_id: accountModel!.id },
          }),
        },
        [409],
      );
      expect(rejected.status).toBe(409);
      expect((await rejected.json() as { detail: { code: string } }).detail.code)
        .toBe('runtime_connection_locked');

      const second = await runTurn(
        page,
        session,
        'Use a local filesystem tool to create hello.txt containing exactly '
          + 'OPENROUTER_TOOL_OK, then read it back and report the exact content.',
      );
      expect(second.chatId).toBe(first.chatId);
      await page.getByRole('button', { name: /tools used/i }).last().click();
      await expect(page.locator('[data-role="tool-call"]').last()).toHaveAttribute(
        'data-tool-status',
        'done',
        { timeout: 60_000 },
      );
      await expect(page.locator('[data-message-role="assistant"]').last())
        .toContainText('OPENROUTER_TOOL_OK');
      await expect(page.getByText(/Model metadata for .* not found/i)).toHaveCount(0);

      await page.reload({ waitUntil: 'domcontentloaded' });
      await page.locator(`button[data-chat-id="${first.chatId}"]`).click();
      await expect(page.locator('[data-role="chat-model-select"]')).toContainText('Ox Alpha');
      await page.locator('[data-role="chat-model-select"]').click();
      await expect(page.locator(
        '[data-role="chat-model-source-option"][data-model-source="openrouter_oauth"]',
      )).toHaveCount(1);
      await expect(page.locator(
        '[data-role="chat-model-source-option"]:not([data-model-source="openrouter_oauth"])',
      )).toHaveCount(0);
      await page.keyboard.press('Escape');
      await page.locator('[data-role="chat-composer-options-toggle"]').click();
      await expect(page.locator('[data-role="chat-reasoning-effort-select"]'))
        .toContainText('Maximum');
      await expect(page.locator('[data-message-role="assistant"]').last())
        .toContainText('OPENROUTER_TOOL_OK');
    } finally {
      if (first) {
        await api(
          session,
          `/api/v1/chat-scopes/${encodeURIComponent(first.scopeId)}`
            + `/chats/${encodeURIComponent(first.chatId)}`,
          { method: 'DELETE' },
          [404],
        );
      }
    }
  });

  test('reconstructs an existing Chat when its native Runtime history is incomplete', async ({ page }) => {
    test.skip(
      !RECOVERY_CHAT_ID,
      'set FLOWORK_E2E_HISTORY_RECOVERY_CHAT_ID to an explicitly authorized recovery fixture',
    );
    if (!session || !RECOVERY_CHAT_ID) return;
    const bootstrap = await api(
      session,
      '/api/v1/chats/bootstrap?surface=chat',
    ).then((response) => response.json()) as { carrier_scope_id: string };
    const history = await api(
      session,
      `/api/v1/chat-scopes/${encodeURIComponent(bootstrap.carrier_scope_id)}`
        + `/chats/${encodeURIComponent(RECOVERY_CHAT_ID)}/messages?limit=500&tail=true`,
    ).then((response) => response.json()) as {
      items: Array<{ role: string; content: string }>;
    };
    const earliestUserText = history.items.find((item) => item.role === 'user')?.content.trim();
    expect(earliestUserText).toBeTruthy();
    const expectedCommand = earliestUserText!.match(/^\/\S+/u)?.[0] ?? '';
    const expectedContentCharacter = Array.from(
      earliestUserText!
        .slice(expectedCommand.length)
        .trim()
        .replace(/^[请帮我]+/u, ''),
    )[0] ?? '';
    expect(expectedCommand).toBeTruthy();
    expect(expectedContentCharacter).toBeTruthy();

    await page.goto('/chat', { waitUntil: 'domcontentloaded' });
    await page.locator(`button[data-chat-id="${RECOVERY_CHAT_ID}"]`).click();
    await expect(page.locator('[data-role="agent-composer-input"]')).toBeEnabled({
      timeout: 30_000,
    });
    const recovered = await runTurn(
      page,
      session,
      'This is a conversation-recovery diagnostic. Inspect the earliest user message '
        + 'in this Chat. Reply with its slash-command token, one space, and the first '
        + 'content character after optional polite words such as 请 or 帮我. Do not use '
        + 'tools, add punctuation, or ask me to repeat it.',
    );
    expect(recovered.chatId).toBe(RECOVERY_CHAT_ID);
    const expectedAnswer = `${expectedCommand} ${expectedContentCharacter}`;
    await expect(page.locator('[data-message-role="assistant"]').last()).toHaveText(
      expectedAnswer,
      { timeout: 60_000 },
    );

    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.locator(`button[data-chat-id="${RECOVERY_CHAT_ID}"]`).click();
    await expect(page.locator('[data-message-role="assistant"]').last()).toHaveText(
      expectedAnswer,
      { timeout: 30_000 },
    );
  });

  test('enforces the same connection lock and same-account model switch in the real side panel', async () => {
    if (!session) return;
    const capabilitiesResponse = await api(session, '/api/v1/agent-runtime/capabilities');
    const capabilities = await capabilitiesResponse.json() as {
      models: Array<{
        id: string;
        label: string;
        api_source?: string | null;
        provider_model_id?: string | null;
        supported_reasoning_efforts?: Array<{ id: string }>;
      }>;
      default_model_id?: string | null;
    };
    const accountModel = capabilities.models.find((model) => (
      model.api_source === 'chatgpt_account'
      && model.id === capabilities.default_model_id
    )) ?? capabilities.models.find((model) => model.api_source === 'chatgpt_account');
    const alternateAccountModel = capabilities.models.find((model) => (
      model.api_source === 'chatgpt_account' && model.id !== accountModel?.id
    ));
    const oxModel = capabilities.models.find((model) => (
      model.api_source === 'openrouter_oauth'
      && model.provider_model_id === 'stealth/ox-alpha'
    ));
    expect(accountModel).toBeTruthy();
    expect(alternateAccountModel).toBeTruthy();
    expect(oxModel).toBeTruthy();
    expect(oxModel?.supported_reasoning_efforts?.map((option) => option.id))
      .toContain('max');
    const bootstrapResponse = await api(
      session,
      '/api/v1/chats/bootstrap?surface=browser',
    );
    const scopeId = String((await bootstrapResponse.json() as {
      carrier_scope_id: string;
    }).carrier_scope_id);

    const profileDir = await mkdtemp(`${tmpdir()}/flowork-openrouter-sidepanel-`);
    const context = await chromium.launchPersistentContext(profileDir, {
      headless: false,
      viewport: { width: 430, height: 900 },
      args: [
        `--disable-extensions-except=${EXTENSION_PATH}`,
        `--load-extension=${EXTENSION_PATH}`,
        '--no-proxy-server',
        '--no-first-run',
        '--no-default-browser-check',
      ],
    });
    const chatIds: string[] = [];
    try {
      await seed(context, session);
      const app = context.pages()[0] ?? await context.newPage();
      await app.goto(`${APP_ORIGIN}/chat`, { waitUntil: 'domcontentloaded' });
      await expect(app.locator('[data-role="agent-composer-input"]')).toBeVisible({
        timeout: 45_000,
      });
      await expect.poll(() => app.evaluate(() => (
        typeof (globalThis as typeof globalThis & {
          chrome?: { runtime?: { sendMessage?: unknown } };
        }).chrome?.runtime?.sendMessage === 'function'
      ))).toBe(true);
      await app.evaluate(() => {
        document.dispatchEvent(new CustomEvent('flowork:extension-auth-refresh'));
      });
      await app.waitForTimeout(750);

      const panel = await context.newPage();
      await panel.goto(`chrome-extension://${EXTENSION_ID}/sidepanel.html`, {
        waitUntil: 'domcontentloaded',
        timeout: 45_000,
      });
      await expect.poll(() => panel.frames().some((candidate) => (
        candidate.url().includes('/embed/chat')
      ))).toBe(true);
      let frame = panel.frames().find((candidate) => candidate.url().includes('/embed/chat'))!;
      await expect(frame.locator('[data-role="agent-composer-input"]')).toBeVisible({
        timeout: 45_000,
      });
      await expect(frame.locator('[data-role="agent-composer-input"]')).toBeEnabled({
        timeout: 45_000,
      });
      await frame.locator('[data-action="agent-sidebar-new-chat"]').click();
      await panel.bringToFront();

      await selectSurfaceModel(
        frame,
        'openrouter_oauth',
        oxModel!.id,
        oxModel!.provider_model_id ?? undefined,
      );
      await frame.locator('[data-role="chat-composer-options-toggle"]').click();
      const effort = frame.locator('[data-role="chat-reasoning-effort-select"]');
      await effort.click();
      await frame.getByRole('option', { name: 'Maximum' }).click();
      await closeCompactOptions(frame);
      const ox = await runFrameTurn(
        panel,
        frame,
        session,
        'Reply with exactly SIDEPANEL_OPENROUTER_OK and no other text.',
        'SIDEPANEL_OPENROUTER_OK',
      );
      chatIds.push(ox.chatId);

      await panel.reload({ waitUntil: 'domcontentloaded' });
      await expect.poll(() => panel.frames().some((candidate) => (
        candidate.url().includes('/embed/chat')
      ))).toBe(true);
      frame = panel.frames().find((candidate) => candidate.url().includes('/embed/chat'))!;
      await expect(frame.locator('[data-role="chat-model-select"]')).toContainText(
        oxModel!.label,
        { timeout: 45_000 },
      );
      await frame.locator('[data-role="chat-composer-options-toggle"]').click();
      await expect(frame.locator('[data-role="chat-reasoning-effort-select"]'))
        .toContainText('Maximum');
      await closeCompactOptions(frame);
      await expect(frame.locator('[data-message-content-rail="assistant"]').filter({
        hasText: 'SIDEPANEL_OPENROUTER_OK',
      })).toHaveCount(1);
      await frame.locator('[data-role="chat-model-select"]').click();
      await expect(frame.locator(
        '[data-role="chat-model-source-option"][data-model-source="openrouter_oauth"]',
      )).toHaveCount(1);
      await expect(frame.locator(
        '[data-role="chat-model-source-option"]:not([data-model-source="openrouter_oauth"])',
      )).toHaveCount(0);
      await frame.locator('body').press('Escape');

      await frame.locator('[data-action="agent-sidebar-new-chat"]').click();
      await selectSurfaceModel(frame, 'chatgpt_account', accountModel!.id);
      const account = await runFrameTurn(
        panel,
        frame,
        session,
        'Reply with exactly SIDEPANEL_ACCOUNT_OK and no other text.',
        'SIDEPANEL_ACCOUNT_OK',
      );
      chatIds.push(account.chatId);

      await selectSurfaceModel(frame, 'chatgpt_account', alternateAccountModel!.id);
      const switched = await runFrameTurn(
        panel,
        frame,
        session,
        'Reply with exactly SIDEPANEL_ACCOUNT_MODEL_SWITCH_OK and no other text.',
        'SIDEPANEL_ACCOUNT_MODEL_SWITCH_OK',
      );
      expect(switched.chatId).toBe(account.chatId);

      const accountCapabilities = await api(
        session,
        `/api/v1/agent-runtime/capabilities?chat_id=${encodeURIComponent(account.chatId)}`,
      ).then((response) => response.json()) as {
        models: Array<{ api_source?: string | null }>;
      };
      expect(accountCapabilities.models.length).toBeGreaterThan(1);
      expect(accountCapabilities.models.every(
        (model) => model.api_source === 'chatgpt_account',
      )).toBe(true);
    } finally {
      await context.close();
      for (const chatId of chatIds) {
        await api(
          session,
          `/api/v1/chat-scopes/${encodeURIComponent(scopeId)}/chats/${encodeURIComponent(chatId)}`,
          { method: 'DELETE' },
          [404],
        );
      }
      await rm(profileDir, { recursive: true, force: true });
    }
  });
});
