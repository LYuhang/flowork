/**
 * Opt-in acceptance for the real MV3 side-panel document at Chrome's typical
 * narrow width. This covers the cold-login path, browser-only Chat history,
 * a real agent turn, the compact Settings surface, and light/dark layouts.
 *
 * Build the extension for the target web origin first, then run under Xvfb:
 *   VIBECANVAS_EXTENSION_SIDEPANEL_E2E=1 pnpm exec playwright test \
 *     e2e/40-extension-sidepanel-narrow.spec.ts --workers=1
 */
import { mkdir, mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium, expect, test, type BrowserContext, type Frame, type Page } from '@playwright/test';

const RUN = process.env.VIBECANVAS_EXTENSION_SIDEPANEL_E2E === '1';
const HERE = dirname(fileURLToPath(import.meta.url));
const EXTENSION_PATH = resolve(HERE, '../../extension/dist');
const EXTENSION_ID = 'mkfldhmlgdbpmhplaphhcfcdcoaakcik';
const SCREENSHOT_DIR = resolve(HERE, '../screenshots/extension-sidepanel-2026-08-07');
const APP_URL = process.env.VIBECANVAS_APP_ORIGIN ?? 'http://127.0.0.1:9001';

async function embedFrame(page: Page): Promise<Frame> {
  await expect.poll(() => page.frames().some((frame) => frame.url().includes('/embed/chat'))).toBe(true);
  const frame = page.frames().find((candidate) => candidate.url().includes('/embed/chat'));
  if (!frame) throw new Error('The side-panel embed frame did not load');
  return frame;
}

test.describe('real narrow extension side panel', () => {
  test.skip(!RUN, 'Set VIBECANVAS_EXTENSION_SIDEPANEL_E2E=1 to run the real extension acceptance.');
  test.describe.configure({ mode: 'serial', timeout: 240_000 });

  let context: BrowserContext;
  let page: Page;

  test.beforeAll(async () => {
    test.setTimeout(180_000);
    await mkdir(SCREENSHOT_DIR, { recursive: true });
    const profileDir = await mkdtemp(resolve(tmpdir(), 'flowork-sidepanel-'));
    context = await chromium.launchPersistentContext(profileDir, {
      headless: false,
      ignoreHTTPSErrors: true,
      viewport: { width: 430, height: 900 },
      args: [
        `--disable-extensions-except=${EXTENSION_PATH}`,
        `--load-extension=${EXTENSION_PATH}`,
        '--no-proxy-server',
        '--no-first-run',
        '--no-default-browser-check',
      ],
    });
    // Authenticate in the main app before opening the side panel. This is the
    // real account-sharing path: the web session itself never enters extension
    // storage; the extension redeems a one-time exchange code into its
    // partitioned HttpOnly cookie.
    const appPage = context.pages()[0] ?? await context.newPage();
    await appPage.goto(APP_URL);
    const email = appPage.locator('#login-email');
    await expect(email).toBeVisible({ timeout: 45_000 });
    await email.fill('test@test.local');
    await appPage.locator('input[type="password"]').fill('test');
    await appPage.locator('button[type="submit"]').click();
    await expect(appPage.locator('#login-email')).toHaveCount(0, { timeout: 45_000 });
    const externalMessagingAvailable = await appPage.evaluate(() => {
      const runtime = (globalThis as typeof globalThis & {
        chrome?: { runtime?: { sendMessage?: unknown } };
      }).chrome?.runtime;
      return typeof runtime?.sendMessage === 'function';
    });
    expect(externalMessagingAvailable).toBe(true);
    // Request a fresh code after the authenticated shell has settled. The
    // extension performs the same MAIN-world event when a side panel opens
    // later or after an extension update.
    await appPage.evaluate(() => {
      document.dispatchEvent(new CustomEvent('flowork:extension-auth-refresh'));
    });
    await appPage.waitForTimeout(1_000);
    const meStatus = await appPage.evaluate(async () => (
      await fetch('/api/v1/auth/me', { credentials: 'include' })
    ).status);
    expect(meStatus).toBe(200);

    page = await context.newPage();
    await page.setViewportSize({ width: 430, height: 900 });
    await page.goto(`chrome-extension://${EXTENSION_ID}/sidepanel.html`);
  });

  test.afterAll(async () => {
    await context?.close();
  });

  test('reuses the main-app login and supports a real browser Chat turn', async () => {
    const frame = await embedFrame(page);
    const composer = frame.locator('[data-role="agent-composer-input"]');
    await expect(composer).toBeVisible({ timeout: 45_000 });
    await expect(frame.locator('#login-email')).toHaveCount(0);
    // Use the lightweight LangChain runtime for the side-panel transport
    // acceptance. Codex startup/MCP health is covered by its dedicated gates
    // and must not turn this narrow-layout test into a sandbox soak test.
    await frame.locator('[data-action="embed-tab-settings"]').click();
    const runtimeSelect = frame.getByRole('combobox', { name: /默认 Agent Runtime|Default Agent runtime/ });
    await runtimeSelect.click();
    const langchainOption = frame.getByRole('option', { name: 'LangChain' });
    if (await langchainOption.count()) await langchainOption.click();
    await frame.locator('[data-action="embed-tab-chat"]').click();
    // The shared development account can have a deliberately interrupted
    // browser Turn from another acceptance. Start a new browser-only Chat so
    // this test measures a fresh send instead of correctly resuming that Turn.
    const previousChatId = await composer.getAttribute('data-chat-id');
    await frame.locator('[data-action="agent-sidebar-new-chat"]').click();
    if (previousChatId) {
      await expect(composer).not.toHaveAttribute('data-chat-id', previousChatId);
    } else {
      // Compatibility with a server build from before the diagnostic Chat id
      // attribute existed; the final production build always takes the branch
      // above.
      await frame.waitForTimeout(750);
    }
    await expect(composer).toHaveAttribute('data-history-ready', 'true');
    await page.screenshot({ path: resolve(SCREENSHOT_DIR, '01-chat-empty-light.png') });

    await composer.fill('请只回复：侧边栏交互正常');
    await frame.locator('[data-action="agent-composer-send"]').click();
    // Draft clearing is tied to the backend's onAccepted acknowledgement. It
    // is a stable assertion; a transient thinking row may legitimately appear
    // and disappear between two Playwright animation frames on a fast model.
    await expect(composer).toHaveValue('', { timeout: 30_000 });
    await expect(
      frame.locator('[data-message-content-rail="assistant"] [data-role="markdown"]'),
    ).toContainText(
      '侧边栏交互正常',
      { timeout: 180_000 },
    );
    await expect(frame.locator('[data-action="agent-composer-send"]')).toBeVisible();
    await expect(frame.locator('[data-action="diagram-open-preview"]')).toHaveCount(0);
    await expect(frame.locator('[data-action="interactive-open-artifact-preview"]')).toHaveCount(0);
    await page.screenshot({ path: resolve(SCREENSHOT_DIR, '02-chat-reply-light.png') });

    await frame.locator('[data-action="agent-sidebar-history"]').click();
    const history = frame.locator('[role="menu"]');
    await expect(history).toBeVisible();
    const box = await history.boundingBox();
    expect(box?.x ?? -1).toBeGreaterThanOrEqual(8);
    expect((box?.x ?? 0) + (box?.width ?? 1)).toBeLessThanOrEqual(422);
    await page.screenshot({ path: resolve(SCREENSHOT_DIR, '03-history-menu-light.png') });
    await history.press('Escape');
    await expect(frame.locator('[role="menu"]')).toHaveCount(0);
  });

  test('keeps compact Settings readable in light and dark themes', async () => {
    const frame = await embedFrame(page);
    await frame.locator('[data-action="embed-tab-settings"]').click();
    await expect(frame.locator('[data-testid="embed-settings-runtime"]')).toBeVisible();
    await expect(frame.locator('[role="menu"]')).toHaveCount(0);
    await page.screenshot({ path: resolve(SCREENSHOT_DIR, '04-settings-light.png') });

    await frame.locator('[data-action="toggle-theme"]').click();
    await frame.getByRole('menuitem', { name: /深色|Dark/ }).click();
    await expect.poll(() => frame.locator('html').getAttribute('class')).toContain('dark');
    await expect(frame.locator('[role="menu"]')).toHaveCount(0);
    await page.screenshot({ path: resolve(SCREENSHOT_DIR, '05-settings-dark.png') });

    const horizontalOverflow = await frame.locator('body').evaluate(
      (body) => body.scrollWidth - body.clientWidth,
    );
    expect(horizontalOverflow).toBeLessThanOrEqual(1);

    // Leave the shared local test account on its original default.
    const runtimeSelect = frame.getByRole('combobox', { name: /默认 Agent Runtime|Default Agent runtime/ });
    await runtimeSelect.click();
    const codexOption = frame.getByRole('option', { name: 'Codex' });
    if (await codexOption.count()) await codexOption.click();
  });

  test('also supports a standalone side-panel login', async () => {
    const profileDir = await mkdtemp(resolve(tmpdir(), 'flowork-sidepanel-standalone-'));
    const standalone = await chromium.launchPersistentContext(profileDir, {
      headless: false,
      ignoreHTTPSErrors: true,
      viewport: { width: 430, height: 900 },
      args: [
        `--disable-extensions-except=${EXTENSION_PATH}`,
        `--load-extension=${EXTENSION_PATH}`,
        '--no-proxy-server',
        '--no-first-run',
        '--no-default-browser-check',
      ],
    });
    try {
      const standalonePage = standalone.pages()[0] ?? await standalone.newPage();
      await standalonePage.goto(`chrome-extension://${EXTENSION_ID}/sidepanel.html`);
      const frame = await embedFrame(standalonePage);
      await expect(frame.locator('#embed-login-email')).toBeVisible({ timeout: 45_000 });
      await frame.locator('#embed-login-email').fill('test@test.local');
      await frame.locator('input[type="password"]').fill('test');
      await frame.locator('button[type="submit"]').click();
      await expect(frame.locator('[data-role="agent-composer-input"]')).toBeVisible({ timeout: 45_000 });
      await standalonePage.screenshot({ path: resolve(SCREENSHOT_DIR, '06-standalone-login.png') });
    } finally {
      await standalone.close();
    }
  });
});
