/**
 * Opt-in production-route visual invariant matrix.
 *
 * Run with `VIBECANVAS_VISUAL_MATRIX=1`. The matrix treats the requested
 * width as physical browser width and divides it by the zoom factor to model
 * the CSS viewport Chrome exposes after browser zoom. This catches the layout
 * and breakpoint failures that matter without relying on browser-chrome
 * keyboard shortcuts, which Playwright cannot control deterministically.
 *
 * `VIBECANVAS_VISUAL_CASE=1440-100` can select one width/zoom cell while
 * iterating locally. The unfiltered gate covers 5 widths × 4 zoom levels ×
 * 2 locales × 2 themes × every production route. The 200% cell is the
 * accessibility zoom gate required by the visual-system audit.
 */
import { expect, test, type Page, type TestInfo } from '@playwright/test';
import {
  createWorkflow,
  deleteWorkflow,
  seedAuthAndLocale,
} from './fixtures';

const RUN_MATRIX = process.env.VIBECANVAS_VISUAL_MATRIX === '1';
const CASE_FILTER = process.env.VIBECANVAS_VISUAL_CASE;
// 560 px is the product's required narrow desktop/side-by-side acceptance
// width. Keep the 390 px shell-only gate below for true mobile behavior.
const WIDTHS = [560, 1024, 1280, 1440, 1920] as const;
const ZOOMS = [80, 100, 125, 200] as const;
const THEMES = ['light', 'dark'] as const;
const LOCALES = ['en', 'zh'] as const;
const MISSING_UUID = '00000000-0000-4000-8000-000000000001';
const SYSTEM_THEME_ROUTE_IDS = new Set([
  'chat',
  'standalone-preview-error',
  'storage',
  'workflow',
  'settings',
  'login',
  'embed-chat',
]);

interface RouteFixture {
  id: string;
  path: (workflowId: string) => string;
  screenshot?: boolean;
  /** Detail routes deliberately exercise their durable not-found state. */
  expectedNotFound?: boolean;
}

const ROUTES: readonly RouteFixture[] = [
  { id: 'root', path: () => '/' },
  { id: 'chat', path: () => '/chat', screenshot: true },
  { id: 'shared-chat-error', path: () => '/share/missing', screenshot: true, expectedNotFound: true },
  { id: 'standalone-preview-error', path: () => '/preview', screenshot: true },
  { id: 'workspace', path: () => '/workspace', screenshot: true },
  {
    id: 'management',
    path: () => '/management',
    screenshot: true,
    expectedNotFound: true,
  },
  { id: 'tasks', path: () => '/tasks', screenshot: true },
  {
    id: 'task-detail-error',
    path: () => `/tasks/${MISSING_UUID}`,
    screenshot: true,
    expectedNotFound: true,
  },
  { id: 'deployments', path: () => '/deployments', screenshot: true },
  {
    id: 'deployment-detail-error',
    path: () => `/deployments/${MISSING_UUID}`,
    screenshot: true,
    expectedNotFound: true,
  },
  { id: 'credentials', path: () => '/credentials', screenshot: true },
  { id: 'mcp-servers', path: () => '/mcp-servers', screenshot: true },
  {
    id: 'mcp-catalog-detail-error',
    path: () => '/mcp-servers/discover/e2e-missing-source',
    screenshot: true,
    expectedNotFound: true,
  },
  {
    id: 'mcp-detail-error',
    path: () => `/mcp-servers/${MISSING_UUID}`,
    expectedNotFound: true,
  },
  { id: 'skills', path: () => '/skills', screenshot: true },
  {
    id: 'skill-catalog-detail-error',
    path: () => '/skills/discover/e2e-missing-source',
    screenshot: true,
    expectedNotFound: true,
  },
  {
    id: 'skill-detail-error',
    path: () => `/skills/${MISSING_UUID}`,
    expectedNotFound: true,
  },
  { id: 'storage', path: () => '/storage', screenshot: true },
  { id: 'knowledge', path: () => '/knowledge', screenshot: true },
  {
    id: 'knowledge-detail-error',
    path: () => `/knowledge/${MISSING_UUID}`,
    screenshot: true,
    expectedNotFound: true,
  },
  { id: 'workflow', path: (workflowId) => `/workflow/${workflowId}`, screenshot: true },
  {
    id: 'workflow-version',
    path: (workflowId) => `/workflow/${workflowId}/version/v1.sv0`,
  },
  { id: 'settings', path: () => '/settings', screenshot: true },
  {
    id: 'openrouter-callback-error',
    path: () => '/settings/openrouter/callback/e2e-state?error=access_denied',
  },
  { id: 'login', path: () => '/login', screenshot: true },
  { id: 'signup', path: () => '/signup' },
  { id: 'reset-password', path: () => '/reset-password' },
  { id: 'embed-chat', path: () => '/embed/chat', screenshot: true },
];

function ignoredExpectedConsoleError(message: string, route: RouteFixture): boolean {
  return (
    route.expectedNotFound === true &&
    /failed to load resource.*(?:4\d\d|not found|unprocessable)/i.test(message)
  );
}

async function setAppearance(page: Page, theme: string, locale: string) {
  await page.goto('/login');
  await page.evaluate(
    ([nextTheme, nextLocale]) => {
      localStorage.setItem('theme', nextTheme);
      localStorage.setItem('vibecanvas.locale', nextLocale);
    },
    [theme, locale] as const,
  );
}

async function waitForRouteSurface(page: Page) {
  await expect(page.locator('#root')).toBeVisible();
  const busy = page.locator('[aria-busy="true"]');
  if (await busy.count()) {
    await expect(busy.first()).toBeHidden({ timeout: 10_000 });
  }
  await expect(page.locator('body')).not.toContainText(
    /Unexpected Application Error|Application crashed/i,
  );
}

async function assertRuntimeVisualInvariants(
  page: Page,
  route: RouteFixture,
  locale: string,
  theme: string,
) {
  await expect(page.locator('html'), `${route.id}: document language`).toHaveAttribute(
    'lang',
    locale === 'zh' ? 'zh-CN' : 'en',
  );
  await expect(page.locator('html'), `${route.id}: resolved theme`).toHaveClass(
    new RegExp(`(?:^|\\s)${theme}(?:\\s|$)`),
  );

  const audit = await page.evaluate(() => {
    const root = document.documentElement;
    const visible = (element: Element) => {
      const node = element as HTMLElement;
      const rect = node.getBoundingClientRect();
      const style = getComputedStyle(node);
      const visuallyHidden =
        style.clipPath !== 'none' ||
        style.clip !== 'auto' ||
        (style.position === 'absolute' &&
          rect.width <= 1 &&
          rect.height <= 1 &&
          style.overflow === 'hidden');
      return (
        rect.width > 0 &&
        rect.height > 0 &&
        rect.bottom > 0 &&
        rect.right > 0 &&
        rect.top < innerHeight &&
        rect.left < innerWidth &&
        style.visibility !== 'hidden' &&
        style.display !== 'none' &&
        !visuallyHidden
      );
    };
    const selector = [
      'button',
      'a',
      'input',
      'select',
      'textarea',
      'label',
      '[role="tab"]',
      '[role="menuitem"]',
      '[role="treeitem"]',
    ].join(',');
    const functional = Array.from(document.querySelectorAll(selector)).filter(visible);
    const smallText = functional
      .filter((element) => {
        const text = (element.textContent ?? '').trim();
        return text.length > 0 && Number.parseFloat(getComputedStyle(element).fontSize) < 12;
      })
      .slice(0, 8)
      .map((element) => ({
        tag: element.tagName.toLowerCase(),
        text: (element.textContent ?? '').trim().slice(0, 80),
        size: getComputedStyle(element).fontSize,
      }));
    const undersizedControls = functional
      .filter((element) => element.matches('button, input, select, textarea, [role="tab"]'))
      .filter((element) => {
        const rect = element.getBoundingClientRect();
        return rect.width < 24 || rect.height < 24;
      })
      .slice(0, 8)
      .map((element) => ({
        tag: element.tagName.toLowerCase(),
        label:
          element.getAttribute('aria-label') ??
          (element.textContent ?? '').trim().slice(0, 80),
        rect: {
          width: Math.round(element.getBoundingClientRect().width),
          height: Math.round(element.getBoundingClientRect().height),
        },
      }));
    return {
      horizontalOverflow: root.scrollWidth - root.clientWidth,
      smallText,
      undersizedControls,
    };
  });

  expect(
    audit.horizontalOverflow,
    `${route.id}: document must not overflow horizontally`,
  ).toBeLessThanOrEqual(1);
  expect(audit.smallText, `${route.id}: functional text must be at least 12px`).toEqual([]);
  expect(
    audit.undersizedControls,
    `${route.id}: visible controls must meet the WCAG 24px minimum target`,
  ).toEqual([]);
}

async function attachRepresentativeScreenshot(
  page: Page,
  testInfo: TestInfo,
  route: RouteFixture,
  locale: string,
  theme: string,
) {
  if (!route.screenshot) return;
  await testInfo.attach(`${route.id}-${locale}-${theme}`, {
    body: await page.screenshot({ animations: 'disabled', fullPage: false }),
    contentType: 'image/png',
  });
}

test.describe('production route visual matrix', () => {
  test.skip(!RUN_MATRIX, 'Set VIBECANVAS_VISUAL_MATRIX=1 to run the exhaustive matrix.');

  let workflowId: string;

  test.beforeAll(async () => {
    workflowId = await createWorkflow(`visual-matrix-${Date.now()}`);
  });

  test.afterAll(async () => {
    if (workflowId) await deleteWorkflow(workflowId);
  });

  test.beforeEach(async ({ context }) => {
    await seedAuthAndLocale(context, 'en');
  });

  for (const physicalWidth of WIDTHS) {
    for (const zoomPercent of ZOOMS) {
      const caseId = `${physicalWidth}-${zoomPercent}`;
      test(`${physicalWidth}px at ${zoomPercent}% zoom`, async ({ page }, testInfo) => {
        test.skip(Boolean(CASE_FILTER) && CASE_FILTER !== caseId, `Filtered to ${CASE_FILTER}`);
        test.setTimeout(8 * 60_000);
        const zoom = zoomPercent / 100;
        await page.setViewportSize({
          width: Math.round(physicalWidth / zoom),
          height: Math.round(900 / zoom),
        });

        const consoleErrors: string[] = [];
        const responseErrors: string[] = [];
        page.on('console', (message) => {
          if (message.type() === 'error') consoleErrors.push(message.text());
        });
        page.on('pageerror', (error) => consoleErrors.push(error.message));
        page.on('response', (response) => {
          if (response.status() >= 400) {
            responseErrors.push(`${response.status()} ${response.request().method()} ${response.url()}`);
          }
        });

        for (const locale of LOCALES) {
          for (const theme of THEMES) {
            await setAppearance(page, theme, locale);
            for (const route of ROUTES) {
              consoleErrors.length = 0;
              responseErrors.length = 0;
              await page.goto(route.path(workflowId), { waitUntil: 'domcontentloaded' });
              await waitForRouteSurface(page);
              // Keep late detail-route responses attached to the route that
              // initiated them. Without this boundary, an expected 404 can
              // finish after the next navigation and be misreported there.
              await page.waitForLoadState('networkidle', { timeout: 3_000 }).catch(() => {});
              await assertRuntimeVisualInvariants(page, route, locale, theme);

              const unexpectedErrors = consoleErrors.filter(
                (message) => !ignoredExpectedConsoleError(message, route),
              );
              expect(
                unexpectedErrors,
                `${route.id}: uncaught browser console/page errors; HTTP failures: ${responseErrors.join(', ') || 'none'}`,
              ).toEqual([]);

              if (!route.expectedNotFound) {
                expect(responseErrors, `${route.id}: unexpected HTTP failures`).toEqual([]);
              }

              if (physicalWidth === 1440 && zoomPercent === 100) {
                await attachRepresentativeScreenshot(
                  page,
                  testInfo,
                  route,
                  locale,
                  theme,
                );
              }
            }
          }
        }
      });
    }
  }

  test('system theme at 200% zoom', async ({ page }) => {
    test.skip(Boolean(CASE_FILTER) && CASE_FILTER !== '1440-200', `Filtered to ${CASE_FILTER}`);
    test.setTimeout(2 * 60_000);
    await page.emulateMedia({ colorScheme: 'dark', reducedMotion: 'reduce' });
    await page.setViewportSize({ width: 720, height: 450 });

    for (const locale of LOCALES) {
      await setAppearance(page, 'system', locale);
      for (const route of ROUTES.filter((item) => SYSTEM_THEME_ROUTE_IDS.has(item.id))) {
        await page.goto(route.path(workflowId), { waitUntil: 'domcontentloaded' });
        await waitForRouteSurface(page);
        await assertRuntimeVisualInvariants(page, route, locale, 'dark');
        const transition = page.locator('.route-transition');
        if (await transition.count()) {
          await expect(transition).toHaveCSS('animation-name', 'none');
        }
      }
    }
  });

  test('mobile shell and primary routes at 390px', async ({ page }) => {
    test.skip(Boolean(CASE_FILTER) && CASE_FILTER !== '390-100', `Filtered to ${CASE_FILTER}`);
    await page.setViewportSize({ width: 390, height: 844 });
    await setAppearance(page, 'light', 'en');
    for (const route of ROUTES.filter((item) =>
      ['chat', 'workspace', 'tasks', 'knowledge', 'settings', 'login', 'embed-chat'].includes(item.id),
    )) {
      await page.goto(route.path(workflowId), { waitUntil: 'domcontentloaded' });
      await waitForRouteSurface(page);
      await assertRuntimeVisualInvariants(page, route, 'en', 'light');
    }
  });

  test('keyboard skip link, focus return and forced-colors semantics', async ({ page }) => {
    test.skip(Boolean(CASE_FILTER) && CASE_FILTER !== 'keyboard', `Filtered to ${CASE_FILTER}`);
    await page.emulateMedia({ forcedColors: 'active', reducedMotion: 'reduce' });
    await page.setViewportSize({ width: 1280, height: 800 });
    await setAppearance(page, 'light', 'en');
    await page.goto('/workspace', { waitUntil: 'domcontentloaded' });
    await waitForRouteSurface(page);
    await page.keyboard.press('Tab');
    const skip = page.getByRole('link', { name: /skip to main content/i });
    await expect(skip).toBeFocused();
    await page.keyboard.press('Enter');
    await expect(page.locator('#main-content')).toBeFocused();
    await page.getByRole('link', { name: /^Tasks?$/i }).first().click();
    await expect(page).toHaveURL(/\/tasks$/);
    await waitForRouteSurface(page);
    await expect(page.locator('#main-content')).toBeFocused();
    await expect(page.locator('body')).not.toContainText(/Unexpected Application Error/i);
  });
});
