import { expect, test, type BrowserContext, type Page } from '@playwright/test';

import { E2ECookieSession } from './cookie-session';

const MESSAGE_PATH = /\/api\/v1\/chat-scopes\/([^/]+)\/chats\/([^/]+)\/messages$/;

test.setTimeout(360_000);

const session = new E2ECookieSession();

async function api(path: string, init: RequestInit = {}) {
  return session.api(path, init);
}

test.beforeAll(async () => {
  console.log('[interactive-e2e] registering user');
  await session.register('interactive-e2e');
  await api('/api/v1/agent-runtime/settings', {
    method: 'PUT',
    body: JSON.stringify({ default_runtime_type: 'langchain' }),
  });
  console.log('[interactive-e2e] runtime configured');
});

test.beforeEach(async ({ context }: { context: BrowserContext }) => {
  await session.seed(context, 'en');
});

test('LangChain renders a durable HTML file, saves to VFS, and continues through a hidden control Turn', async ({ page }: { page: Page }) => {
  page.on('request', (request) => {
    if (request.method() === 'POST') {
      console.log(`[interactive-e2e] POST ${new URL(request.url()).pathname}`);
    }
  });
  page.on('requestfailed', (request) => {
    console.log(
      `[interactive-e2e] request failed ${request.method()} ${new URL(request.url()).pathname}: `
      + `${request.failure()?.errorText ?? 'unknown'}`,
    );
  });
  page.on('pageerror', (error) => {
    console.log(`[interactive-e2e] page error: ${error.message}`);
  });
  console.log('[interactive-e2e] opening Chat');
  await page.goto('/chat');
  await expect(page.locator('[data-role="agent-composer-input"]')).toBeVisible({
    timeout: 15_000,
  });
  const newChat = page.locator('[data-action="chat-new"]');
  await expect(newChat).toBeVisible({ timeout: 15_000 });
  await newChat.click({ timeout: 10_000 });
  console.log('[interactive-e2e] new Chat selected');

  const prompt = [
    'Create /data/acceptance/index.html, then call render_preview exactly once',
    'with type="file", source="/data/acceptance/index.html", title="Acceptance Gate", and require_human_confirm=true.',
    'The saved HTML file must contain:',
    '1. a button id="save" labeled "Save annotation" that, on a real click, PUTs',
    '{"accepted":true,"label":"verified"} as application/json to /data/acceptance/labels.json;',
    '2. a status element id="status" changed to "Saved" only after fetch succeeds;',
    '3. an anchor id="open-result" href="/data/acceptance/labels.json" labeled "Open saved result".',
    'Use flat arguments, not a nested view, and do not emit prose after the Preview call.',
  ].join(' ');

  const composer = page.locator('[data-role="agent-composer-input"]');
  await expect(composer).toBeEnabled({ timeout: 20_000 });
  await composer.fill(prompt);
  const send = page.locator('[data-action="agent-composer-send"]');
  await expect(send).toBeEnabled({ timeout: 10_000 });
  await page.waitForTimeout(250);
  console.log('[interactive-e2e] sending prompt');
  const [request] = await Promise.all([
    page.waitForRequest((candidate) => (
      candidate.method() === 'POST'
      && MESSAGE_PATH.test(new URL(candidate.url()).pathname)
    ), { timeout: 30_000 }),
    send.click({ timeout: 10_000 }),
  ]);
  console.log('[interactive-e2e] Turn accepted');
  const match = new URL(request.url()).pathname.match(MESSAGE_PATH);
  expect(match).not.toBeNull();
  const [, scopeId, chatId] = match!;

  try {
    const card = page.locator('[data-role="interactive-artifact"]').filter({
      hasText: 'Acceptance Gate',
    }).last();
    await expect(card.locator('[data-action="interactive-submit"]')).toBeVisible({
      timeout: 180_000,
    });
    await expect(page.locator('[data-action="agent-composer-send"]')).toBeVisible({
      timeout: 20_000,
    });

    // The pending gate is reconstructed from durable state, not an in-memory
    // suspended Runtime stack.
    await page.reload({ waitUntil: 'domcontentloaded' });
    const restored = page.locator('[data-role="interactive-artifact"]').filter({
      hasText: 'Acceptance Gate',
    }).last();
    await expect(restored.locator('[data-action="interactive-submit"]')).toBeVisible({
      timeout: 30_000,
    });
    await expect(page.locator('[data-message-role="user"]').filter({ hasText: prompt })).toHaveCount(1);

    const frame = restored.frameLocator('iframe');
    await frame.locator('#save').click({ timeout: 20_000 });
    await expect(frame.locator('#status')).toHaveText('Saved', { timeout: 20_000 });

    // Local absolute paths remain natural inside Agent HTML and open the same
    // Preview shell after the file has been durably written.
    await frame.locator('#open-result').click();
    await expect(page.getByText('labels.json', { exact: true }).last()).toBeVisible({
      timeout: 30_000,
    });
    await expect(page.getByText(/"accepted"\s*:\s*true/).last()).toBeVisible({
      timeout: 30_000,
    });

    await restored.locator('[data-action="interactive-submit"]').click();
    const continued = restored.locator(
      '[data-action="interactive-submit"][data-state="continued"]',
    );
    await expect(continued).toHaveText(/Continued/, {
      timeout: 30_000,
    });
    await expect(continued).toBeDisabled();
    await expect(page.locator('[data-message-role="user"]').filter({ hasText: prompt })).toHaveCount(1);

    // Wait for the independent follow-up Turn to settle, then verify that the
    // product transcript never exposes the hidden control Human message.
    await expect(page.locator('[data-action="agent-composer-send"]')).toBeVisible({
      timeout: 150_000,
    });
    // The disabled state is reconstructed from hitl_requests.status, not kept
    // only in React state. A new document must not revive Continue.
    await page.reload({ waitUntil: 'domcontentloaded' });
    const persisted = page.locator('[data-role="interactive-artifact"]').filter({
      hasText: 'Acceptance Gate',
    }).last().locator('[data-action="interactive-submit"][data-state="continued"]');
    await expect(persisted).toBeDisabled({ timeout: 30_000 });
    const history = await api(
      `/api/v1/chat-scopes/${encodeURIComponent(scopeId)}/chats/${encodeURIComponent(chatId)}/messages?limit=200`,
    ).then((response) => response.json()) as {
      items: Array<{ role: string; content: string }>;
    };
    expect(history.items.filter((item) => item.role === 'user').map((item) => item.content)).toEqual([prompt]);
  } finally {
    await api(
      `/api/v1/chat-scopes/${encodeURIComponent(scopeId)}/chats/${encodeURIComponent(chatId)}`,
      { method: 'DELETE' },
    );
  }
});
