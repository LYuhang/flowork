import { expect, test, type BrowserContext, type Page } from '@playwright/test';

import { E2ECookieSession } from './cookie-session';

const MESSAGE_PATH = /\/api\/v1\/chat-scopes\/([^/]+)\/chats\/([^/]+)\/messages$/;

test.setTimeout(420_000);

const session = new E2ECookieSession();

async function api(path: string, init: RequestInit = {}, allowError = false) {
  return session.api(path, init, allowError);
}

test.beforeAll(async () => {
  // Codex model calls use the host Runtime Model Broker; no user auth cache is
  // copied into the Chat sandbox.
  await session.register('codex-interactive-e2e');
  await api('/api/v1/agent-runtime/settings', {
    method: 'PUT',
    body: JSON.stringify({ default_runtime_type: 'codex' }),
  });
  const capabilities = await api('/api/v1/agent-runtime/capabilities').then((response) => response.json()) as {
    runtime_available: boolean;
    error_code: string | null;
  };
  expect(capabilities.runtime_available).toBe(true);
  expect(capabilities.error_code).toBeNull();
});

test.beforeEach(async ({ context }: { context: BrowserContext }) => {
  await session.seed(context, 'en');
});

test('Codex calls Platform MCP, saves through a Previewed HTML file, and continues as a new hidden Turn', async ({
  page,
}: {
  page: Page;
}) => {
  await page.goto('/chat');
  await expect(page.locator('[data-role="agent-composer-input"]')).toBeVisible({
    timeout: 20_000,
  });
  await page.locator('[data-action="chat-new"]').click({ timeout: 10_000 });
  const prompt = [
    'Create /data/codex-acceptance/index.html, then call render_preview exactly once',
    'with type="file", source="/data/codex-acceptance/index.html", title="Codex Acceptance Gate",',
    'and require_human_confirm=true. The saved HTML file must contain an input',
    'id="note" name="note" with value="codex-draft";',
    'a button id="bad-save" labeled "Test failed save" that PUTs {} to /mount/not-writable.json',
    'and changes an element id="bad-status" to "Failed 403" when the response is 403;',
    'a button id="save" labeled "Save Codex result" that PUTs',
    '{"runtime":"codex","accepted":true} as application/json to /data/codex-acceptance/result.json;',
    'a status element id="status" changed to "Saved" only after the write succeeds;',
    'and an anchor id="open-result" href="/data/codex-acceptance/result.json" labeled "Open Codex result".',
    'Use flat arguments, not a nested view, and do not emit prose after the Preview call.',
  ].join(' ');
  const composer = page.locator('[data-role="agent-composer-input"]');
  await composer.fill(prompt);
  const [messageResponse] = await Promise.all([
    page.waitForResponse((response) => (
      response.request().method() === 'POST'
      && MESSAGE_PATH.test(new URL(response.url()).pathname)
    ), { timeout: 30_000 }),
    page.waitForRequest((request) => (
      request.method() === 'POST'
      && MESSAGE_PATH.test(new URL(request.url()).pathname)
    ), { timeout: 30_000 }),
    page.locator('[data-action="agent-composer-send"]').click(),
  ]);
  const messageRequest = messageResponse.request();
  const match = new URL(messageRequest.url()).pathname.match(MESSAGE_PATH);
  expect(match).not.toBeNull();
  const [, scopeId, chatId] = match!;
  const turnId = messageResponse.headers()['x-turn-id'];
  expect(turnId).toMatch(/^t_/);
  const initialStream = api(
    `/api/v1/chats/${encodeURIComponent(chatId)}/turns/${encodeURIComponent(turnId!)}`
    + '/stream',
  ).then((response) => response.text());

  try {
    const card = page.locator('[data-role="interactive-artifact"]').filter({
      hasText: 'Codex Acceptance Gate',
    }).last();
    const stream = await initialStream;
    try {
      await expect(card.locator('[data-action="interactive-submit"]')).toBeVisible({
        timeout: 30_000,
      });
    } catch (error) {
      // Surface the durable Runtime error instead of leaving a generic locator
      // timeout as the only acceptance evidence.
      const terminal = [...stream.matchAll(/^event: error\ndata: (.+)$/gm)]
        .map((item) => item[1])
        .at(-1);
      const evidence = stream
        .split('\n\n')
        .filter((frame) => /event: (TOOL_EVENT|error|done)/.test(frame))
        .slice(-8)
        .join('\n\n')
        .slice(-6_000);
      throw new Error(`Codex Runtime did not render the card: ${
        (terminal ?? evidence) || stream.slice(-12_000) || 'no terminal event'
      }`, {
        cause: error,
      });
    }

    await page.reload({ waitUntil: 'domcontentloaded' });
    const restored = page.locator('[data-role="interactive-artifact"]').filter({
      hasText: 'Codex Acceptance Gate',
    }).last();
    await expect(restored.locator('[data-action="interactive-submit"]')).toBeVisible({
      timeout: 60_000,
    });
    await expect(page.locator('[data-message-role="user"]').filter({ hasText: prompt })).toHaveCount(1);

    const frame = restored.frameLocator('iframe');
    await frame.locator('#note').fill('codex-draft-preserved');
    await frame.locator('#bad-save').click({ timeout: 20_000 });
    await expect(frame.locator('#bad-status')).toHaveText('Failed 403', { timeout: 20_000 });
    await expect(frame.locator('#note')).toHaveValue('codex-draft-preserved');
    await expect(restored.getByText(/needs attention/i)).toBeVisible({ timeout: 20_000 });
    await frame.locator('#save').click({ timeout: 20_000 });
    await expect(frame.locator('#status')).toHaveText('Saved', { timeout: 20_000 });
    await frame.locator('#open-result').click();
    const pane = page.locator('[data-role="chat-preview-pane"]');
    await expect(pane.getByText('result.json', { exact: true }).last()).toBeVisible({
      timeout: 30_000,
    });
    await expect(pane.getByText(/"runtime"\s*:\s*"codex"/).last()).toBeVisible({
      timeout: 30_000,
    });

    await restored.locator('[data-action="interactive-submit"]').click();
    await expect(restored.getByText('Continued', { exact: true })).toBeVisible({
      timeout: 30_000,
    });
    await expect(page.locator('[data-action="agent-composer-send"]')).toBeVisible({
      timeout: 180_000,
    });
    const history = await api(
      `/api/v1/chat-scopes/${encodeURIComponent(scopeId)}/chats/${encodeURIComponent(chatId)}`
      + '/messages?limit=200',
    ).then((response) => response.json()) as {
      items: Array<{ role: string; content: string }>;
    };
    expect(history.items.filter((item) => item.role === 'user').map((item) => item.content)).toEqual([prompt]);
  } finally {
    if (process.env.VIBECANVAS_KEEP_E2E_CHAT !== '1') {
      await api(
        `/api/v1/chat-scopes/${encodeURIComponent(scopeId)}/chats/${encodeURIComponent(chatId)}`,
        { method: 'DELETE' },
        true,
      );
    } else {
      console.log(`[codex-e2e] preserved chat=${chatId} turn=${turnId}`);
    }
  }
});
