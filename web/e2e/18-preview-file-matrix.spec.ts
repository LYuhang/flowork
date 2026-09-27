import { execFile } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import { promisify } from 'node:util';
import { expect, test, type BrowserContext, type Page } from '@playwright/test';

import {
  e2eSessionHeaders,
  registerE2EUserToken,
  seedTokenAndLocale,
} from './fixtures';
import { findAccessibilityNode, readAccessibilityTree } from './accessibility-tree';

const execFileAsync = promisify(execFile);
const API_BASE = process.env.VIBECANVAS_API_BASE ?? 'http://127.0.0.1:8000';
const PYTHON = process.env.VIBECANVAS_PYTHON
  ?? 'python3';
const MESSAGE_PATH = /\/api\/v1\/chat-scopes\/([^/]+)\/chats\/([^/]+)\/messages$/;
const SAMPLE_NAMES = [
  'acceptance.pdf',
  'acceptance.docx',
  'acceptance.pptx',
  'acceptance.xlsx',
  'acceptance.csv',
  'acceptance.tsv',
  'acceptance.jsonl',
  'acceptance.txt',
  'acceptance.rtf',
] as const;
const CONTENT_TYPES: Record<(typeof SAMPLE_NAMES)[number], string> = {
  'acceptance.pdf': 'application/pdf',
  'acceptance.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  'acceptance.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  'acceptance.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  'acceptance.csv': 'text/csv',
  'acceptance.tsv': 'text/tab-separated-values',
  'acceptance.jsonl': 'application/x-ndjson',
  'acceptance.txt': 'text/plain',
  'acceptance.rtf': 'application/rtf',
};

test.setTimeout(480_000);

let token = '';

async function api(path: string, init: RequestInit = {}, allowError = false) {
  const headers = new Headers(e2eSessionHeaders(token));
  new Headers(init.headers).forEach((value, key) => headers.set(key, value));
  if (init.body instanceof FormData) headers.delete('Content-Type');
  const response = await fetch(`${API_BASE}${path}`, { ...init, headers });
  if (!allowError && !response.ok) {
    throw new Error(`${init.method ?? 'GET'} ${path} failed: ${response.status} ${await response.text()}`);
  }
  return response;
}

test.beforeAll(async () => {
  token = await registerE2EUserToken();
  await api('/api/v1/agent-runtime/settings', {
    method: 'PUT',
    body: JSON.stringify({ default_runtime_type: 'langchain' }),
  });
});

test.beforeEach(async ({ context }: { context: BrowserContext }) => {
  await seedTokenAndLocale(context, token, 'en');
});

async function uploadSamples(
  workspaceScopeId: string,
  sampleDir: string,
): Promise<void> {
  for (const name of SAMPLE_NAMES) {
    const data = await readFile(`${sampleDir}/${name}`);
    await api('/api/v1/vfs/bytes', {
      method: 'PUT',
      body: JSON.stringify({
        wf_id: workspaceScopeId,
        path: `/data/preview-acceptance/${name}`,
        data_b64: data.toString('base64'),
        content_type: CONTENT_TYPES[name],
      }),
    });
  }
}

async function openFromIndex(
  page: Page,
  frame: ReturnType<Page['frameLocator']>,
  id: string,
  name: string,
): Promise<ReturnType<Page['locator']>> {
  await frame.locator(`#${id}`).click();
  const pane = page.locator('[data-role="chat-preview-pane"]');
  await expect(pane).toHaveAccessibleName(/Preview|预览/);
  await expect(pane.getByText(name, { exact: true }).last()).toBeVisible({
    timeout: 30_000,
  });
  return pane;
}

async function confirmOverwrite(page: Page, pane: ReturnType<Page['locator']>) {
  await pane.getByRole('button', { name: 'Overwrite', exact: true }).click();
  const dialog = page.getByRole('alertdialog', { name: 'Overwrite' });
  await expect(dialog).toBeVisible();
  await dialog.getByRole('button', { name: 'Overwrite', exact: true }).click();
}

test('Preview renders real office, PDF, and table files and protects text revisions', async ({
  page,
}, testInfo) => {
  const sampleDir = testInfo.outputPath('preview-samples');
  await execFileAsync(PYTHON, [
    'e2e/fixtures/generate_preview_acceptance_samples.py',
    sampleDir,
  ], {
    cwd: process.cwd(),
    timeout: 90_000,
  });

  await page.goto('/chat');
  await expect(page.locator('[data-role="agent-composer-input"]')).toBeVisible({
    timeout: 20_000,
  });
  await page.locator('[data-action="chat-new"]').click();

  const links = SAMPLE_NAMES.map((name) => {
    const id = name.split('.')[1];
    return `<a id="open-${id}" href="/data/preview-acceptance/${name}">${name}</a>`;
  }).join(' ');
  const prompt = [
    'Create /data/preview-acceptance/index.html with this exact HTML body:',
    `<main>${links}</main>.`,
    'Then call render_preview exactly once with type="file", source="/data/preview-acceptance/index.html",',
    'title="Preview Acceptance Index", and require_human_confirm=false.',
    'Use flat arguments, not a nested view, and do not emit prose after the Preview call.',
  ].join(' ');
  const composer = page.locator('[data-role="agent-composer-input"]');
  await composer.fill(prompt);
  const [messageResponse] = await Promise.all([
    page.waitForResponse((response) => (
      response.request().method() === 'POST'
      && MESSAGE_PATH.test(new URL(response.url()).pathname)
      && response.ok()
    ), { timeout: 30_000 }),
    page.locator('[data-action="agent-composer-send"]').click(),
  ]);
  const match = new URL(messageResponse.url()).pathname.match(MESSAGE_PATH);
  expect(match).not.toBeNull();
  const [, scopeId, chatId] = match!;
  const workspace = await api(
    `/api/v1/chats/workspace?chat_id=${encodeURIComponent(chatId)}`,
  ).then((response) => response.json()) as { workspace_scope_id: string };

  try {
    await uploadSamples(workspace.workspace_scope_id, sampleDir);
    const card = page.locator('[data-role="interactive-artifact"]').filter({
      hasText: 'Preview Acceptance Index',
    }).last();
    await expect(card).toBeVisible({ timeout: 180_000 });
    const frame = card.frameLocator('iframe');

    let pane = await openFromIndex(page, frame, 'open-pdf', 'acceptance.pdf');
    expect(
      findAccessibilityNode(await readAccessibilityTree(page), 'complementary', 'Preview'),
    ).toBeDefined();
    await expect(pane.getByText('1 / 1', { exact: true })).toBeVisible({
      timeout: 60_000,
    });
    await expect(pane.locator('canvas')).toBeVisible();

    pane = await openFromIndex(page, frame, 'open-docx', 'acceptance.docx');
    await expect(pane.getByText('DOCX acceptance marker', { exact: true })).toBeVisible({
      timeout: 60_000,
    });

    pane = await openFromIndex(page, frame, 'open-pptx', 'acceptance.pptx');
    await expect(pane.getByText('PPTX acceptance marker', { exact: true })).toBeVisible({
      timeout: 60_000,
    });

    for (const [id, name, marker] of [
      ['open-xlsx', 'acceptance.xlsx', 'XLSX acceptance marker'],
      ['open-csv', 'acceptance.csv', 'CSV acceptance marker'],
      ['open-tsv', 'acceptance.tsv', 'TSV acceptance marker'],
      ['open-jsonl', 'acceptance.jsonl', 'JSONL acceptance marker'],
    ] as const) {
      pane = await openFromIndex(page, frame, id, name);
      await expect(pane.getByText(marker, { exact: true })).toBeVisible({
        timeout: 60_000,
      });
    }

    pane = await openFromIndex(page, frame, 'open-txt', 'acceptance.txt');
    await expect(pane.getByText(/Text acceptance marker/)).toBeVisible({
      timeout: 30_000,
    });
    await pane.getByRole('button', { name: 'Edit', exact: true }).click();
    const editor = pane.getByRole('textbox', { name: 'acceptance.txt source' });
    await editor.fill('Text acceptance marker\nSaved in browser\n');
    await pane.getByRole('button', { name: 'Save', exact: true }).click();
    const editButton = pane.getByRole('button', { name: 'Edit', exact: true });
    const conflictAlert = pane.getByRole('alert').filter({
      hasText: 'changed after editing began',
    });
    await Promise.race([
      editButton.waitFor({ state: 'visible', timeout: 30_000 }),
      conflictAlert.waitFor({ state: 'visible', timeout: 30_000 }),
    ]);
    if (await conflictAlert.isVisible()) {
      await confirmOverwrite(page, pane);
      await expect(editButton).toBeVisible({ timeout: 30_000 });
    }

    await editButton.click();
    await editor.fill('Text acceptance marker\nBrowser wins conflict\n');
    await api('/api/v1/vfs/bytes', {
      method: 'PUT',
      body: JSON.stringify({
        wf_id: workspace.workspace_scope_id,
        path: '/data/preview-acceptance/acceptance.txt',
        data_b64: Buffer.from('\uFEFFAgent changed the file\r\n', 'utf8').toString('base64'),
        content_type: 'text/plain',
      }),
    });
    await expect(conflictAlert).toBeVisible({
      timeout: 15_000,
    });
    await confirmOverwrite(page, pane);
    await expect(editButton).toBeVisible({
      timeout: 30_000,
    });
    const saved = await api(
      `/api/v1/vfs/content?wf_id=${encodeURIComponent(workspace.workspace_scope_id)}`
      + '&path=%2Fdata%2Fpreview-acceptance%2Facceptance.txt',
    ).then((response) => response.json()) as { content: string };
    expect(saved.content).toContain('Browser wins conflict');
    expect(saved.content.startsWith('\uFEFF')).toBe(true);
    expect(saved.content).toContain('\r\n');

    pane = await openFromIndex(page, frame, 'open-rtf', 'acceptance.rtf');
    await expect(pane.getByText(
      'This file type is not supported in Preview.',
      { exact: true },
    )).toBeVisible({
      timeout: 30_000,
    });
    await expect(pane.getByRole('link', { name: 'Download' }).last()).toBeVisible();

    // Error states are part of the normal Preview lifecycle, so they must
    // remain useful after a locale switch and Chat remount instead of falling
    // back to a hard-coded English renderer error.
    await page.goto('/settings');
    await page.locator('[data-action="set-locale-zh"]').click();
    await page.goBack({ waitUntil: 'domcontentloaded' });
    const restoredCard = page.locator('[data-role="interactive-artifact"]').filter({
      hasText: 'Preview Acceptance Index',
    }).last();
    await expect(restoredCard).toBeVisible({ timeout: 60_000 });
    const restoredFrame = restoredCard.frameLocator('iframe');
    pane = await openFromIndex(page, restoredFrame, 'open-rtf', 'acceptance.rtf');
    await expect(pane.getByText(
      'Preview 暂不支持该文件类型。',
      { exact: true },
    )).toBeVisible({
      timeout: 30_000,
    });
    await expect(pane.getByRole('link', { name: '下载' }).last()).toBeVisible();
    await pane.getByRole('button', {
      name: /^(?:Close preview|关闭预览)$/,
    }).click();
    await expect(page.locator('[data-action="chat-preview-toggle"]')).toBeFocused();
  } finally {
    await api(
      `/api/v1/vfs?wf_id=${encodeURIComponent(workspace.workspace_scope_id)}`
      + '&path=%2Fdata%2Fpreview-acceptance',
      { method: 'DELETE' },
      true,
    );
    await api(
      `/api/v1/chat-scopes/${encodeURIComponent(scopeId)}/chats/${encodeURIComponent(chatId)}`,
      { method: 'DELETE' },
      true,
    );
  }
});
