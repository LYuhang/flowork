/**
 * Opt-in headed-Chromium gate for Flowork's Playwright extension relay.
 *
 * Run after `pnpm --dir extension build`:
 *   VIBECANVAS_EXTENSION_E2E=1 xvfb-run -a pnpm exec playwright test \
 *     e2e/16-extension-playwright-relay.spec.ts --workers=1
 *
 * This deliberately speaks standard CDP through PLAYWRIGHT_RELAY_FRAME. The
 * retired RUN_COMMAND protocol is separately asserted to be absent.
 */
import { createServer, type Server } from 'node:http';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  chromium,
  expect,
  test,
  type BrowserContext,
  type Page,
  type Worker,
} from '@playwright/test';

const RUN_EXTENSION_E2E = process.env.VIBECANVAS_EXTENSION_E2E === '1';
const HERE = dirname(fileURLToPath(import.meta.url));
const EXTENSION_PATH = resolve(HERE, '../../extension/dist');
const CHANNEL = 'chat:playwright-relay-e2e';
const SESSION_ID = 'browser-session-playwright-relay-e2e';
const SESSION_GENERATION = 1;

async function runtimeMessage<T>(page: Page, message: unknown): Promise<T> {
  return page.evaluate(async (payload) => {
    return await (globalThis as unknown as {
      chrome: { runtime: { sendMessage: (value: unknown) => Promise<unknown> } };
    }).chrome.runtime.sendMessage(payload);
  }, message) as T;
}

let sequence = 0;
async function relay(
  panel: Page,
  action: 'initialize' | 'request' | 'close',
  request?: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  const raw = await runtimeMessage<string>(panel, {
    type: 'PLAYWRIGHT_RELAY_FRAME',
    env: {
      v: 1,
      kind: 'playwright_relay',
      id: `relay-${++sequence}`,
      channel: CHANNEL,
      transport: 'extension-e2e',
      data: {
        action,
        browser_session_id: SESSION_ID,
        session_generation: SESSION_GENERATION,
        request,
      },
    },
  });
  expect(typeof raw).toBe('string');
  return JSON.parse(raw).data?.message ?? {};
}

test.describe('real MV3 Playwright relay', () => {
  test.skip(!RUN_EXTENSION_E2E, 'Set VIBECANVAS_EXTENSION_E2E=1 and use headed Chrome.');
  test.describe.configure({ mode: 'serial' });

  let server: Server;
  let baseUrl = '';
  let context: BrowserContext;
  let worker: Worker;
  let panel: Page;
  let target: Page;
  let profileDir = '';

  test.beforeAll(async () => {
    server = createServer((_request, response) => {
      response.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
      response.end('<!doctype html><title>Playwright Relay Fixture</title><main><h1>PLAYWRIGHT_RELAY_OK</h1><button id="action">Run</button><output id="result"></output><script>action.onclick=()=>result.textContent="ACTION_OK"</script></main>');
    });
    await new Promise<void>((resolveListen, rejectListen) => {
      server.once('error', rejectListen);
      server.listen(0, '127.0.0.1', resolveListen);
    });
    const address = server.address();
    if (!address || typeof address === 'string') throw new Error('fixture server did not bind');
    baseUrl = `http://127.0.0.1:${address.port}`;

    profileDir = await mkdtemp(resolve(tmpdir(), 'flowork-playwright-relay-'));
    context = await chromium.launchPersistentContext(profileDir, {
      headless: false,
      args: [
        `--disable-extensions-except=${EXTENSION_PATH}`,
        `--load-extension=${EXTENSION_PATH}`,
        '--no-first-run',
        '--no-default-browser-check',
      ],
    });
    worker = context.serviceWorkers()[0] ?? await context.waitForEvent('serviceworker');
    target = context.pages()[0] ?? await context.newPage();
    await target.goto(baseUrl);
    const extensionId = new URL(worker.url()).host;
    panel = await context.newPage();
    await panel.goto(`chrome-extension://${extensionId}/sidepanel.html`);
    const topology = await worker.evaluate(async (url) => {
      const tabs = await (globalThis as unknown as {
        chrome: { tabs: { query: (query: object) => Promise<Array<{ id?: number; windowId: number; url?: string }>> } };
      }).chrome.tabs.query({});
      const tab = tabs.find((candidate) => candidate.url === url);
      if (tab?.id === undefined) throw new Error('target tab not found');
      return { tabId: tab.id, windowId: tab.windowId };
    }, baseUrl + '/');
    await runtimeMessage(panel, {
      type: 'SIDEPANEL_WINDOW',
      windowId: topology.windowId,
      panelContextId: 'playwright-relay-e2e-panel',
    });
    await target.bringToFront();
  });

  test.afterAll(async () => {
    await context?.close();
    await new Promise<void>((resolveClose) => server?.close(() => resolveClose()));
    if (profileDir) await rm(profileDir, { recursive: true, force: true });
  });

  test('controls a real visible page only through standard CDP relay frames', async () => {
    test.setTimeout(60_000);
    await panel.evaluate(() => {
      const scope = globalThis as unknown as {
        __floworkSessionEvents?: Array<Record<string, unknown>>;
        chrome: {
          runtime: {
            onMessage: {
              addListener: (listener: (message: Record<string, unknown>) => void) => void;
            };
          };
        };
      };
      scope.__floworkSessionEvents = [];
      scope.chrome.runtime.onMessage.addListener((message) => {
        if (message?.type === 'BROWSER_SESSION_CHANGED') {
          scope.__floworkSessionEvents?.push(message);
        }
      });
    });
    expect(await relay(panel, 'initialize')).toMatchObject({
      result: { initialized: true, tabs: 1 },
    });
    await expect.poll(async () => panel.evaluate(() => {
      const events = (globalThis as unknown as {
        __floworkSessionEvents?: Array<Record<string, unknown>>;
      }).__floworkSessionEvents ?? [];
      return events.some((event) => event.status === 'attached');
    })).toBe(true);
    expect(await relay(panel, 'request', {
      id: 1,
      method: 'Target.setAutoAttach',
      params: {},
    })).toMatchObject({ id: 1, result: {} });

    const title = await relay(panel, 'request', {
      id: 2,
      method: 'Runtime.evaluate',
      params: { expression: 'document.title', returnByValue: true },
    });
    expect(title).toMatchObject({
      id: 2,
      result: { result: { value: 'Playwright Relay Fixture' } },
    });

    const action = await relay(panel, 'request', {
      id: 3,
      method: 'Runtime.evaluate',
      params: {
        expression: 'document.querySelector("#action").click(); document.querySelector("#result").textContent',
        returnByValue: true,
      },
    });
    expect(action).toMatchObject({ id: 3, result: { result: { value: 'ACTION_OK' } } });
    await expect(target.locator('#result')).toHaveText('ACTION_OK');

    const screenshot = await relay(panel, 'request', {
      id: 4,
      method: 'Page.captureScreenshot',
      params: { format: 'png' },
    });
    const data = ((screenshot.result as Record<string, unknown>)?.data ?? '') as string;
    expect(data.length).toBeGreaterThan(1_000);

    // Cancelling one Agent Turn must not tear down the longer-lived browser
    // lease. The next Turn can continue through the same relay immediately.
    expect(await runtimeMessage(panel, {
      type: 'BROWSER_TURN_CANCELLED',
      turn_id: 'extension-e2e-cancelled-turn',
    })).toEqual({ ok: true });
    const afterCancel = await relay(panel, 'request', {
      id: 5,
      method: 'Runtime.evaluate',
      params: { expression: 'document.title', returnByValue: true },
    });
    expect(afterCancel).toMatchObject({
      id: 5,
      result: { result: { value: 'Playwright Relay Fixture' } },
    });

    expect(await relay(panel, 'close')).toMatchObject({ result: { closed: true } });
  });

  test('reinitializes the official CDP relay after a controller reconnect', async () => {
    expect(await relay(panel, 'initialize')).toMatchObject({
      result: { initialized: true, tabs: 1 },
    });
    // A new official Playwright connection performs its Target handshake
    // again; the extension advertises tabs but does not attach them before the
    // controller requests auto-attach.
    expect(await relay(panel, 'request', {
      id: 6,
      method: 'Target.setAutoAttach',
      params: {},
    })).toMatchObject({ id: 6, result: {} });
    const title = await relay(panel, 'request', {
      id: 7,
      method: 'Runtime.evaluate',
      params: { expression: 'document.title', returnByValue: true },
    });
    expect(title).toMatchObject({
      id: 7,
      result: { result: { value: 'Playwright Relay Fixture' } },
    });
    expect(await relay(panel, 'close')).toMatchObject({ result: { closed: true } });
  });

  test('does not register the removed custom command entry point', async () => {
    const result = await runtimeMessage<unknown>(panel, {
      type: 'RUN_COMMAND',
      env: { kind: 'command' },
    });
    expect(result).toBeUndefined();
  });
});
