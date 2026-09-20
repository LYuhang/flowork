import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { expect, test, type BrowserContext, type Page } from '@playwright/test';

import { E2ECookieSession } from './cookie-session';
import {
  provisionRealRuntime,
  selectRuntimeModel,
  type RealRuntimeName,
  type RealRuntimeProfile,
} from './real-runtime-profile';

const MESSAGE_PATH = /\/api\/v1\/chat-scopes\/([^/]+)\/chats\/([^/]+)\/messages$/;

const MCP_SOURCE = `
from mcp.server.fastmcp import FastMCP

server = FastMCP("Flowork real acceptance")

@server.tool()
def echo_acceptance(value: str) -> str:
    """Return a deterministic acceptance marker with the supplied value."""
    return "MCP_REAL_OK:" + value

server.run(transport="stdio")
`;

test.setTimeout(900_000);

for (const runtime of ['langchain', 'codex'] as const satisfies readonly RealRuntimeName[]) {
  test.describe(`${runtime} real custom MCP and Skill`, () => {
    const session = new E2ECookieSession();
    const unique = `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    const safeUnique = unique.toLowerCase().replace(/[^a-z0-9]/g, '').slice(-20);
    const mcpName = `Real MCP ${runtime} ${unique}`;
    const mcpPrefix = `real_${runtime}_${safeUnique}`;
    const skillName = `real-skill-${runtime}-${unique.toLowerCase().replace(/_/g, '-')}`;
    const skillMarker = `SKILL_REAL_OK_${runtime.toUpperCase()}_${safeUnique.toUpperCase()}`;
    const chatIds = new Set<string>();
    let tempDir = '';
    let mcpId = '';
    let skillId = '';
    let profile: RealRuntimeProfile | undefined;

    test.beforeAll(async () => {
      test.setTimeout(180_000);
      await session.register(`mcp-skill-real-${runtime}`);
      profile = await provisionRealRuntime(session, runtime);

      const mcpResponse = await session.api('/api/v1/mcp-servers', {
        method: 'POST',
        body: JSON.stringify({
          name: mcpName,
          tool_prefix: mcpPrefix,
          transport: 'stdio',
          endpoint: '/usr/local/bin/python',
          description: `Deterministic ${runtime} real E2E MCP server.`,
          connection_config: {
            command: '/usr/local/bin/python',
            args: ['-c', MCP_SOURCE],
          },
          auth_config: { type: 'none' },
        }),
      });
      mcpId = ((await mcpResponse.json()) as { id: string }).id;

      tempDir = mkdtempSync(join(tmpdir(), `flowork-${runtime}-mcp-skill-`));
      const skillMd = `---
name: ${skillName}
description: Read this playbook when asked for its private real-runtime acceptance token.
version: 1
allowed-tools: []
---

# Real progressive-disclosure acceptance

When this Skill is explicitly requested, return the exact acceptance token below:

${skillMarker}

Do not reveal the token unless this SKILL.md has been read for the current task.
`;
      const skillSource = join(tempDir, 'SKILL.md');
      const skillZip = join(tempDir, 'skill.zip');
      writeFileSync(skillSource, skillMd, 'utf8');
      execFileSync(
        process.env.VIBECANVAS_PYTHON ?? 'python3',
        [
          '-c',
          'import sys,zipfile; z=zipfile.ZipFile(sys.argv[1],"w",zipfile.ZIP_DEFLATED); z.write(sys.argv[2],"SKILL.md"); z.close()',
          skillZip,
          skillSource,
        ],
      );
      const archive = new Uint8Array(readFileSync(skillZip));
      const form = new FormData();
      form.append('bundle', new Blob([archive], { type: 'application/zip' }), 'skill.zip');
      const skillResponse = await session.form('/api/v1/skills/custom', form);
      skillId = ((await skillResponse.json()) as { id: string }).id;
    });

    test.afterAll(async () => {
      test.setTimeout(180_000);
      const runtimeProfile = profile;
      profile = undefined;
      runtimeProfile?.cleanup();
      try {
        const bootstrap = await session.api('/api/v1/chats/bootstrap', {
          signal: AbortSignal.timeout(30_000),
        }, true);
        if (bootstrap.ok) {
          const scope = await bootstrap.json() as { carrier_scope_id: string };
          for (const chatId of chatIds) {
            await session.api(
              `/api/v1/chat-scopes/${encodeURIComponent(scope.carrier_scope_id)}`
                + `/chats/${encodeURIComponent(chatId)}`,
              {
                method: 'DELETE',
                signal: AbortSignal.timeout(30_000),
              },
              true,
            );
          }
        }
        if (skillId) {
          await session.api(`/api/v1/skills/${encodeURIComponent(skillId)}`, {
            method: 'DELETE',
            signal: AbortSignal.timeout(30_000),
          }, true);
        }
        if (mcpId) {
          await session.api(`/api/v1/mcp-servers/${encodeURIComponent(mcpId)}`, {
            method: 'DELETE',
            signal: AbortSignal.timeout(30_000),
          }, true);
        }
      } finally {
        if (tempDir) rmSync(tempDir, { recursive: true, force: true });
      }
    });

    test.beforeEach(async ({ context }: { context: BrowserContext }) => {
      await session.seed(context, 'en');
    });

    async function openNewChat(page: Page) {
      if (!profile) throw new Error(`${runtime} Runtime profile was not provisioned`);
      await page.goto('/chat');
      await expect(page.locator('[data-role="agent-composer-input"]')).toBeVisible({
        timeout: 30_000,
      });
      await page.locator('[data-action="chat-new"]').click();
      await selectRuntimeModel(page, profile);
    }

    async function sendAndWait(page: Page, prompt: string, marker: string) {
      const composer = page.locator('[data-role="agent-composer-input"]');
      await composer.fill(prompt);
      await expect(composer).toHaveValue(prompt);
      await expect(page.locator('[data-action="agent-composer-send"]')).toBeEnabled({
        timeout: 30_000,
      });
      const [response] = await Promise.all([
        page.waitForResponse((candidate) => (
          candidate.request().method() === 'POST'
            && MESSAGE_PATH.test(new URL(candidate.url()).pathname)
        ), { timeout: 30_000 }),
        page.locator('[data-action="agent-composer-send"]').click(),
      ]);
      if (!response.ok()) {
        throw new Error(`Agent Turn rejected: ${response.status()} ${await response.text()}`);
      }
      const chatId = new URL(response.url()).pathname.match(MESSAGE_PATH)?.[2];
      if (chatId) chatIds.add(chatId);
      await expect(
        page.locator('[data-message-role="assistant"]').filter({ hasText: marker }).last(),
      ).toBeVisible({ timeout: 360_000 });
    }

    test(`installs and invokes a real MCP and Skill through ${runtime}`, async ({ page }) => {
      if (!mcpId || !skillId) throw new Error('MCP or Skill setup did not return an id');
      await page.goto(`/mcp-servers/${encodeURIComponent(mcpId)}`);
      await expect(page.getByText(mcpName, { exact: true }).first()).toBeVisible({
        timeout: 60_000,
      });
      const toolsTab = page.getByRole('tab', { name: /Tools 1/i });
      await expect(toolsTab).toBeVisible({ timeout: 60_000 });
      await toolsTab.click();
      await expect(page.getByText('echo_acceptance', { exact: true }).first()).toBeVisible({
        timeout: 60_000,
      });

      await openNewChat(page);
      await page.locator('[data-role="chat-composer-options-toggle"]').click();
      const mcpPicker = page.locator('[data-role="chat-mcp-picker"]');
      await expect(mcpPicker).toBeVisible({ timeout: 30_000 });
      await mcpPicker.click();
      const mcpOption = page.getByText(mcpName, { exact: true }).last();
      await expect(mcpOption).toBeVisible({ timeout: 30_000 });
      await mcpOption.click();
      await expect(page.locator('[data-role="chat-mcp-picker"]')).toContainText('1 MCP');
      await page.keyboard.press('Escape');
      await sendAndWait(
        page,
        `Call ${mcpPrefix}__echo_acceptance exactly once with value "browser". `
          + 'Only after that custom MCP tool succeeds, reply with its complete result.',
        'MCP_REAL_OK:browser',
      );
      const mcpActivity = page.locator('[data-tool-activity="true"]')
        .filter({ hasText: mcpPrefix }).last();
      await expect(mcpActivity).toBeVisible({ timeout: 60_000 });
      const mcpActivityToggle = mcpActivity.locator('[data-action="tool-activity-toggle"]');
      if (await mcpActivityToggle.getAttribute('aria-expanded') !== 'true') {
        await mcpActivityToggle.click();
      }
      await expect(mcpActivity.locator(
        `[data-role="tool-call"][data-tool-name="${mcpPrefix}__echo_acceptance"]`
          + '[data-tool-status="done"]',
      )).toHaveCount(1, { timeout: 60_000 });

      // The token is intentionally absent from the user prompt and catalog
      // description. Observing it proves the installed SKILL.md was disclosed
      // inside the selected Runtime rather than guessed from the request.
      await page.locator('[data-action="chat-new"]').click();
      if (!profile) throw new Error(`${runtime} Runtime profile was not provisioned`);
      await selectRuntimeModel(page, profile);
      await sendAndWait(
        page,
        `Use the installed Skill named ${skillName}. Read its SKILL.md, follow its `
          + 'playbook, and return the private acceptance token it defines. Do not guess.',
        skillMarker,
      );

      await page.goto('/skills');
      await expect(page.getByText(skillName, { exact: true }).first()).toBeVisible({
        timeout: 60_000,
      });
    });
  });
}
