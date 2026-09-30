/**
 * Vitest global setup.
 *
 * Runs once per test file before any test code. Two responsibilities:
 *
 * 1. Extend `expect` with jest-dom matchers (`toBeInTheDocument`,
 *    `toHaveTextContent`, etc). The `/vitest` subpath is the modern v6+
 *    integration — it auto-augments the `vitest` module's `Assertion`
 *    type, so no `<reference>` lines needed in test files.
 *
 * 2. MSW server lifecycle:
 *      - `beforeAll`  → start the interceptor with the shared handlers.
 *      - `afterEach`  → reset handlers so per-test `server.use(...)`
 *        overrides don't leak into the next test.
 *      - `afterAll`   → close cleanly so vitest's process can exit.
 *    `onUnhandledRequest: 'error'` matches the MSW v2 production-style
 *    setup — any un-mocked fetch is treated as a test bug (better than
 *    silently hitting the network).
 *
 * jsdom polyfills (`ResizeObserver`, `matchMedia`) are added below since
 * Radix UI components and next-themes touch them at mount time and jsdom
 * does not implement them by default.
 */
import '@testing-library/jest-dom/vitest';
import { afterAll, afterEach, beforeAll, beforeEach, vi } from 'vitest';
import { cleanup } from '@testing-library/react';
import { server, resetDraftFixtures } from '@/__tests__/msw-handlers';
import { CONTEXT_DRAFT_RESET_EVENT } from '@/lib/chat/context-draft';
import i18n from 'i18next';

// With `test.isolate: false` (see vitest.config.ts), RTL would otherwise
// leak DOM trees from earlier tests into the next test's `screen.*`
// queries (auto-cleanup only fires when Vitest detects a unique scope per
// test). Explicit per-test cleanup makes the contract deterministic
// regardless of pool config.
function resetBrowserPreferences() {
  resetDraftFixtures();
  if (typeof window !== 'undefined') window.dispatchEvent(new Event(CONTEXT_DRAFT_RESET_EVENT));
  if (typeof localStorage === 'undefined') return;
  localStorage.clear();
  // Seed the product's canonical key before each test module imports the app
  // i18n bootstrap. This keeps jsdom deterministic while remaining safe for
  // pure Node-environment suites (for example the Excel parser tests).
  localStorage.setItem('vibecanvas.locale', 'en');
  if (typeof sessionStorage !== 'undefined') sessionStorage.clear();
  if (typeof document !== 'undefined') document.documentElement.lang = 'en';
  if (i18n.isInitialized) void i18n.changeLanguage('en');
}

resetBrowserPreferences();
beforeEach(resetBrowserPreferences);

afterEach(() => {
  cleanup();
  resetBrowserPreferences();
});

// ---------------------------------------------------------------------------
// jsdom shims for browser APIs Radix UI + next-themes assume exist.
// ---------------------------------------------------------------------------

// `matchMedia` — next-themes reads `prefers-color-scheme` at boot.
if (typeof window !== 'undefined' && !window.matchMedia) {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }),
  });
}

// `ResizeObserver` — Radix UI Popover / Select observe trigger geometry.
class MockResizeObserver {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}
if (typeof globalThis !== 'undefined' && !('ResizeObserver' in globalThis)) {
  (globalThis as unknown as { ResizeObserver: typeof MockResizeObserver }).ResizeObserver =
    MockResizeObserver;
}

// Pointer capture — Radix Select uses these methods during pointer-driven
// option selection; jsdom exposes PointerEvent but not the Element capture
// methods, which otherwise turns an ordinary combobox test into an uncaught
// TypeError and contaminates every test that follows it.
if (typeof Element !== 'undefined') {
  if (!Element.prototype.hasPointerCapture) {
    Element.prototype.hasPointerCapture = () => false;
  }
  if (!Element.prototype.setPointerCapture) {
    Element.prototype.setPointerCapture = () => undefined;
  }
  if (!Element.prototype.releasePointerCapture) {
    Element.prototype.releasePointerCapture = () => undefined;
  }
  if (!Element.prototype.scrollIntoView) {
    Element.prototype.scrollIntoView = () => undefined;
  }
}

// `TransformStream` polyfill lives in `setup-polyfills.ts` (run BEFORE
// this file via vitest.config.ts `setupFiles` order) — must execute
// before MSW's `brotli-decompress` import-time call.

// ---------------------------------------------------------------------------
// MSW lifecycle.
// ---------------------------------------------------------------------------
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());
