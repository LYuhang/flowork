import { expect, test, type BrowserContext, type Page } from '@playwright/test';
import { mkdir, rm, writeFile } from 'node:fs/promises';

import { E2ECookieSession } from './cookie-session';
import {
  provisionRealRuntime,
  selectRuntimeModel,
  type RealRuntimeName,
  type RealRuntimeProfile,
} from './real-runtime-profile';

const MESSAGE_PATH = /\/api\/v1\/chat-scopes\/([^/]+)\/chats\/([^/]+)\/messages$/;

test.setTimeout(900_000);

for (const runtime of ['langchain', 'codex'] as const satisfies readonly RealRuntimeName[]) {
  test.describe(`${runtime} real Knowledge package journey`, () => {
    const session = new E2ECookieSession();
    const unique = `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    const kbName = `Knowledge acceptance ${runtime} ${unique}`;
    const identifier = `FLOWORK_KNOWLEDGE_E2E_${runtime.toUpperCase()}_${unique}`;
    let profile: RealRuntimeProfile | undefined;
    let kbId = '';
    let chatId = '';

    test.beforeAll(async () => {
      await session.register(`knowledge-real-${runtime}`);
      profile = await provisionRealRuntime(session, runtime);
    });

    test.afterAll(async () => {
      try {
        if (chatId) {
          const bootstrap = await session.api('/api/v1/chats/bootstrap', {}, true);
          if (bootstrap.ok) {
            const scope = await bootstrap.json() as { carrier_scope_id: string };
            await session.api(
              `/api/v1/chat-scopes/${encodeURIComponent(scope.carrier_scope_id)}`
                + `/chats/${encodeURIComponent(chatId)}`,
              { method: 'DELETE' },
              true,
            );
          }
        }
        if (kbId) {
          await session.api(`/api/v1/kb/${encodeURIComponent(kbId)}`, {
            method: 'DELETE',
          }, true);
        }
      } finally {
        profile?.cleanup();
      }
    });

    test.beforeEach(async ({ context }: { context: BrowserContext }) => {
      await session.seed(context, 'en');
    });

    async function chooseAutomaticApproval(page: Page) {
      const options = page.locator('[data-role="chat-composer-options-toggle"]');
      await expect(options).toBeVisible({ timeout: 20_000 });
      await options.click();
      await expect(page.locator('[data-role="chat-approval-mode-select"]')).toHaveCount(0);
    }

    test(`creates, reopens, and retrieves a hierarchical package through ${runtime}`, async ({
      page,
    }, testInfo) => {
      if (!profile) throw new Error(`${runtime} Runtime profile was not provisioned`);
      const browserErrors: string[] = [];
      page.on('pageerror', (error) => browserErrors.push(error.message));
      page.on('console', (message) => {
        if (message.type() === 'error') browserErrors.push(`console: ${message.text()}`);
      });
      page.on('requestfailed', (request) => {
        const errorText = request.failure()?.errorText ?? '';
        // Document navigation intentionally supersedes outstanding React Query
        // reads. Browser lifecycle aborts are not application transport errors.
        if (!errorText.includes('ERR_ABORTED')) {
          browserErrors.push(`${request.method()} ${request.url()}: ${errorText}`);
        }
      });

      await page.goto('/knowledge');
      await expect(page.getByRole('heading', { name: 'Knowledge', exact: true })).toBeVisible({
        timeout: 30_000,
      });
      const packageRoot = testInfo.outputPath('complete-knowledge-folder');
      await mkdir(`${packageRoot}/notes`, { recursive: true });
      await writeFile(`${packageRoot}/README.md`, [
        '# Real browser package acceptance',
        '',
        'This root README describes the complete Knowledge folder.',
      ].join('\n'));
      await writeFile(`${packageRoot}/notes/acceptance.md`, [
        '# Acceptance fact',
        '',
        `The exact verification identifier is ${identifier}.`,
        'Return this identifier verbatim when it is requested.',
      ].join('\n'));

      await page.getByRole('button', { name: 'Upload folder' }).click();
      await expect(page.getByRole('dialog', { name: 'Upload a knowledge folder' })).toBeVisible();
      await page.getByTestId('knowledge-import-folder-input').setInputFiles(packageRoot);
      await expect(page.getByText('2 files selected')).toBeVisible();
      await page.getByLabel('Name').fill(kbName);
      await page.getByLabel('Description').fill('Real browser package acceptance');
      const [importResponse] = await Promise.all([
        page.waitForResponse((response) => (
          response.request().method() === 'POST'
            && new URL(response.url()).pathname === '/api/v1/kb/import'
        ), { timeout: 30_000 }),
        page.getByRole('button', { name: 'Import knowledge' }).click(),
      ]);
      const detailPayload = await importResponse.json() as { id?: string; name?: string };
      expect(importResponse.ok()).toBe(true);
      kbId = detailPayload.id ?? page.url().match(/\/knowledge\/([^/?#]+)$/)?.[1] ?? '';
      if (!kbId) throw new Error('Knowledge detail route did not expose a package id');
      expect(detailPayload.name).toBe(kbName);

      await expect(page.locator('header').getByRole('heading', {
        name: kbName,
        exact: true,
      }), [
        `Knowledge detail stayed stale after import HTTP ${importResponse.status()}`,
        `payload.id=${detailPayload.id ?? 'missing'}`,
        `payload.name_matches=${detailPayload.name === kbName}`,
        `browser_errors=${browserErrors.join(' | ') || 'none'}`,
      ].join('; ')).toBeVisible({ timeout: 30_000 });
      const sourceTree = page.getByRole('tree', { name: 'Files' });
      await expect(sourceTree).toBeVisible();
      await expect(sourceTree.getByRole('treeitem', { name: /README\.md/i })).toBeVisible();
      await expect(sourceTree.getByRole('treeitem', { name: /^notes$/i })).toBeVisible();
      await expect(sourceTree.getByRole('treeitem', { name: /acceptance\.md/i })).toBeVisible();
      await sourceTree.click({ button: 'right', position: { x: 240, y: 300 } });
      await expect(page.getByRole('menuitem', { name: 'Upload files' })).toBeVisible();
      await expect(page.getByRole('menuitem', { name: 'Upload folder' })).toBeVisible();
      await page.keyboard.press('Escape');
      await sourceTree.getByRole('treeitem', { name: /^notes$/i }).click({ button: 'right' });
      await expect(page.getByRole('menuitem', { name: 'Upload files here' })).toBeVisible();
      await expect(page.getByRole('menuitem', { name: 'Upload folder here' })).toBeVisible();
      await expect(page.getByRole('menuitem', { name: 'Delete folder' })).toBeVisible();
      await page.keyboard.press('Escape');
      await sourceTree.getByRole('treeitem', { name: /acceptance\.md/i }).click();
      await expect(page.getByText(identifier, { exact: false }).last()).toBeVisible({
        timeout: 30_000,
      });

      // A fresh document load must reconstruct the package tree from the
      // durable package snapshot rather than retaining a flat client index.
      await page.reload({ waitUntil: 'domcontentloaded' });
      await expect(page.getByRole('tree', { name: 'Files' })
        .getByRole('treeitem', { name: /README\.md/i })).toBeVisible();
      await expect(page.getByRole('tree', { name: 'Files' })
        .getByRole('treeitem', { name: /acceptance\.md/i })).toBeVisible();
      await page.screenshot({
        path: testInfo.outputPath(`${runtime}-knowledge-package-reopened.png`),
        fullPage: true,
      });

      await page.goto('/chat');
      const composer = page.locator('[data-role="agent-composer-input"]');
      await expect(composer).toBeVisible({ timeout: 30_000 });
      await page.locator('[data-action="chat-new"]').click();
      await selectRuntimeModel(page, profile);
      await chooseAutomaticApproval(page);
      const prompt = [
        '/knowledge',
        'First call knowledge_list. Find the Knowledge package named',
        `"${kbName}", then call knowledge_get to materialize its complete directory.`,
        `Call knowledge_search with the exact query "${identifier}". Read the`,
        'materialized root README.md and acceptance.md with ordinary filesystem tools',
        'before answering. Do not treat the derived search index as the package source.',
        `After those tools succeed, reply exactly KNOWLEDGE_CHAT_OK ${identifier}`,
      ].join(' ');
      await composer.fill(prompt);
      const [messageResponse] = await Promise.all([
        page.waitForResponse((response) => (
          response.request().method() === 'POST'
            && MESSAGE_PATH.test(new URL(response.url()).pathname)
        ), { timeout: 30_000 }),
        page.locator('[data-action="agent-composer-send"]').click(),
      ]);
      expect(messageResponse.ok()).toBe(true);
      chatId = new URL(messageResponse.url()).pathname.match(MESSAGE_PATH)?.[2] ?? '';
      await expect(
        page.locator('[data-message-role="assistant"]').filter({
          hasText: `KNOWLEDGE_CHAT_OK ${identifier}`,
        }).last(),
      ).toBeVisible({ timeout: 360_000 });
      const activities = page.locator('[data-tool-activity="true"]');
      for (let index = 0; index < await activities.count(); index += 1) {
        const toggle = activities.nth(index).locator('[data-action="tool-activity-toggle"]');
        if (await toggle.getAttribute('aria-expanded') !== 'true') await toggle.click();
      }
      for (const toolName of ['knowledge_list', 'knowledge_get', 'knowledge_search']) {
        await expect(page.locator(
          `[data-role="tool-call"][data-tool-name="${toolName}"][data-tool-status="done"]`,
        )).toHaveCount(1, { timeout: 30_000 });
      }
      await expect(page.locator('[data-role="agent-thinking"]')).toHaveCount(0, {
        timeout: 30_000,
      });
      await expect(composer).toBeEnabled({ timeout: 30_000 });
      await page.screenshot({
        path: testInfo.outputPath(`${runtime}-knowledge-agent-chain.png`),
        fullPage: true,
      });

      await page.reload({ waitUntil: 'domcontentloaded' });
      await expect(page.locator('[data-message-role="user"]').filter({ hasText: prompt }))
        .toHaveCount(1);
      expect(browserErrors).toEqual([]);
      await rm(packageRoot, { recursive: true, force: true });
    });
  });
}
