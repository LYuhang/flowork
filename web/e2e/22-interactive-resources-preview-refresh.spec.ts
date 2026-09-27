import { expect, test, type BrowserContext, type Page } from '@playwright/test';

import {
  e2eSessionHeaders,
  registerE2EUserToken,
  seedTokenAndLocale,
} from './fixtures';

const API_BASE = process.env.VIBECANVAS_API_BASE ?? 'http://127.0.0.1:8000';
const MESSAGE_PATH = /\/api\/v1\/chat-scopes\/([^/]+)\/chats\/([^/]+)\/messages$/;

test.setTimeout(600_000);

let token = '';

async function api(path: string, init: RequestInit = {}, allowError = false) {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      ...e2eSessionHeaders(token),
      ...init.headers,
    },
  });
  if (!allowError && !response.ok) {
    throw new Error(`${init.method ?? 'GET'} ${path} failed: ${response.status} ${await response.text()}`);
  }
  return response;
}

async function sendPrompt(page: Page, prompt: string) {
  const composer = page.locator('[data-role="agent-composer-input"]');
  await expect(composer).toBeEnabled({ timeout: 30_000 });
  await composer.fill(prompt);
  const [response] = await Promise.all([
    page.waitForResponse((candidate) => (
      candidate.request().method() === 'POST'
      && MESSAGE_PATH.test(new URL(candidate.url()).pathname)
      && candidate.ok()
    ), { timeout: 30_000 }),
    page.locator('[data-action="agent-composer-send"]').click(),
  ]);
  const match = new URL(response.url()).pathname.match(MESSAGE_PATH);
  expect(match).not.toBeNull();
  return { scopeId: match![1], chatId: match![2] };
}

async function writeText(
  workspaceScopeId: string,
  path: string,
  content: string,
  contentType: string,
) {
  const deadline = Date.now() + 60_000;
  while (true) {
    const response = await api('/api/v1/vfs/content', {
      method: 'PUT',
      body: JSON.stringify({
        wf_id: workspaceScopeId,
        path,
        content,
        content_type: contentType,
      }),
    }, true);
    if (response.ok) return;
    const body = await response.text();
    if (response.status !== 404 || !body.includes('vfs_scope_not_found') || Date.now() >= deadline) {
      throw new Error(`PUT /api/v1/vfs/content failed: ${response.status} ${body}`);
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
}

async function writeBytes(
  workspaceScopeId: string,
  path: string,
  content: Buffer,
  contentType: string,
) {
  const deadline = Date.now() + 60_000;
  while (true) {
    const response = await api('/api/v1/vfs/bytes', {
      method: 'PUT',
      body: JSON.stringify({
        wf_id: workspaceScopeId,
        path,
        data_b64: content.toString('base64'),
        content_type: contentType,
      }),
    }, true);
    if (response.ok) return;
    const body = await response.text();
    if (response.status !== 404 || !body.includes('vfs_scope_not_found') || Date.now() >= deadline) {
      throw new Error(`PUT /api/v1/vfs/bytes failed: ${response.status} ${body}`);
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
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

test('dynamic Interactive resources survive save failures; Preview follows VFS events and samples ranged files', async ({
  page,
}: {
  page: Page;
}) => {
  await page.goto('/chat');
  await expect(page.locator('[data-role="agent-composer-input"]')).toBeVisible({
    timeout: 20_000,
  });
  await page.locator('[data-action="chat-new"]').click();
  const publicImageUrl = new URL('/favicon.svg', page.url()).href;

  const html = [
    '<main>',
    '<input id="label" name="label" value="draft-label">',
    '<div id="json-status">loading</div>',
    '<div id="local-status">loading</div>',
    '<div id="public-status">loading</div>',
    '<div id="save-status">idle</div>',
    '<div id="images"></div>',
    '<img id="missing" src="/data/interactive-acceptance/missing.png">',
    '<button id="bad-save">Save invalid path</button>',
    '<button id="good-save">Save annotation</button>',
    '<a id="open-result" href="/data/interactive-acceptance/annotation.json">Open annotation</a>',
    '<a id="open-large" href="/data/interactive-acceptance/large.csv">Open large table</a>',
    '<script>',
    "fetch('/data/interactive-acceptance/items.jsonl').then(r=>{if(!r.ok)throw new Error('HTTP '+r.status);return r.text()}).then(t=>{",
    "document.querySelector('#json-status').textContent='rows:'+t.trim().split(String.fromCharCode(10)).length;",
    "}).catch(e=>document.querySelector('#json-status').textContent='error:'+e.message);",
    "const local=new Image(); local.id='local-image';",
    "local.onload=()=>document.querySelector('#local-status').textContent='loaded';",
    "local.onerror=()=>document.querySelector('#local-status').textContent='failed';",
    "local.src='/data/interactive-acceptance/local.png'; document.querySelector('#images').append(local);",
    "const pub=new Image(); pub.id='public-image';",
    "pub.onload=()=>document.querySelector('#public-status').textContent='loaded';",
    "pub.onerror=()=>document.querySelector('#public-status').textContent='failed';",
    `pub.src=${JSON.stringify(publicImageUrl)};`,
    "document.querySelector('#images').append(pub);",
    "document.querySelector('#bad-save').onclick=async()=>{",
    "const r=await fetch('/mount/not-writable.json',{method:'PUT',headers:{'Content-Type':'application/json'},body:'{}'});",
    "document.querySelector('#save-status').textContent='failed:'+r.status;",
    '};',
    "document.querySelector('#good-save').onclick=async()=>{",
    "const label=document.querySelector('#label').value;",
    "const r=await fetch('/data/interactive-acceptance/annotation.json',{method:'PUT',",
    "headers:{'Content-Type':'application/json'},body:JSON.stringify({label,accepted:true})});",
    "document.querySelector('#save-status').textContent=r.ok?'saved':'failed:'+r.status;",
    '};',
    '</script>',
    '</main>',
  ].join('');
  const prompt = [
    'Create /data/interactive-acceptance/index.html using this exact HTML without changing it:',
    html,
    'Then call render_preview exactly once with type="file", source="/data/interactive-acceptance/index.html",',
    'title="Dynamic Resource Acceptance", and require_human_confirm=true.',
    'Use flat arguments, not a nested view, and do not emit prose after the Preview call.',
  ].join(' ');
  const { scopeId, chatId } = await sendPrompt(page, prompt);
  const workspace = await api(
    `/api/v1/chats/workspace?chat_id=${encodeURIComponent(chatId)}`,
  ).then((response) => response.json()) as { workspace_scope_id: string };

  try {
    await writeText(
      workspace.workspace_scope_id,
      '/data/interactive-acceptance/items.jsonl',
      '{"id":1,"label":"first"}\n{"id":2,"label":"second"}\n',
      'table/jsonl',
    );
    await writeBytes(
      workspace.workspace_scope_id,
      '/data/interactive-acceptance/local.png',
      Buffer.from(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=',
        'base64',
      ),
      'image/png',
    );
    const largeCsv = `id,label\n${'1,large-range-marker\n'.repeat(600_000)}`;
    expect(Buffer.byteLength(largeCsv)).toBeGreaterThan(10 * 1024 * 1024);
    await writeText(
      workspace.workspace_scope_id,
      '/data/interactive-acceptance/large.csv',
      largeCsv,
      'text/csv',
    );

    let card = page.locator('[data-role="interactive-artifact"]').filter({
      hasText: 'Dynamic Resource Acceptance',
    }).last();
    await expect(card.locator('[data-action="interactive-submit"]')).toBeVisible({
      timeout: 180_000,
    });
    const pending = await api(
      `/api/v1/chats/${encodeURIComponent(chatId)}/hitl-requests`,
    ).then((response) => response.json()) as Array<{ artifact_id?: string }>;
    expect(pending).toHaveLength(1);
    const artifactId = pending[0].artifact_id;
    expect(artifactId).toBeTruthy();
    const resourceSession = await api(
      `/api/v1/interactive-artifacts/${encodeURIComponent(artifactId!)}/resource-session`,
      { method: 'POST' },
    ).then((response) => response.json()) as {
      resource_mounts: Array<{ path_prefix: string; root_url: string }>;
    };
    const workspaceMount = resourceSession.resource_mounts.find(
      (mount) => mount.path_prefix === '/',
    );
    expect(workspaceMount).toBeTruthy();
    const directResource = await fetch(
      `${API_BASE}${workspaceMount!.root_url}data/interactive-acceptance/items.jsonl`,
    );
    expect(directResource.status).toBe(200);
    expect(await directResource.text()).toContain('"id":2');

    let frame = card.frameLocator('iframe');
    await expect(frame.locator('#json-status')).toHaveText('rows:2', { timeout: 30_000 });
    await expect(frame.locator('#local-status')).toHaveText('loaded', { timeout: 30_000 });
    // Interactive HTML is intentionally a no-egress renderer: only data/blob
    // and this artifact's opaque VFS resource roots are present in CSP. A
    // normal URL must therefore fail while the local VFS image succeeds.
    await expect(frame.locator('#public-status')).toHaveText('failed', { timeout: 30_000 });

    await frame.locator('#label').fill('annotated-after-failure');
    await frame.locator('#bad-save').click();
    await expect(frame.locator('#save-status')).toHaveText('failed:403', { timeout: 20_000 });
    await expect(frame.locator('#label')).toHaveValue('annotated-after-failure');
    await expect(card.getByText(/needs attention/i)).toBeVisible({ timeout: 20_000 });
    await expect.poll(async () => {
      const stored = await api(
        `/api/v1/interactive-artifacts/${encodeURIComponent(artifactId!)}`,
      ).then((response) => response.json()) as {
        artifact?: { widget_state?: { fields?: Record<string, unknown> } };
      };
      return stored.artifact?.widget_state?.fields?.label;
    }, { timeout: 10_000, intervals: [100, 250, 500] }).toBe('annotated-after-failure');

    // This is an explicit recovery acceptance (the user refreshes/relogs in),
    // not the normal Preview update mechanism. The draft and diagnostic must
    // survive that lifecycle boundary.
    await page.reload({ waitUntil: 'domcontentloaded' });
    card = page.locator('[data-role="interactive-artifact"]').filter({
      hasText: 'Dynamic Resource Acceptance',
    }).last();
    await expect(card.locator('[data-action="interactive-submit"]')).toBeVisible({
      timeout: 60_000,
    });
    frame = card.frameLocator('iframe');
    await expect(frame.locator('#label')).toHaveValue('annotated-after-failure', {
      timeout: 30_000,
    });
    let annotationResolveRequests = 0;
    page.on('request', (request) => {
      if (
        request.method() !== 'POST'
        || new URL(request.url()).pathname !== '/api/v1/previews/resolve'
      ) return;
      try {
        const body = request.postDataJSON() as {
          fileRef?: { path?: string };
        };
        if (
          body.fileRef?.path
          === '/data/interactive-acceptance/annotation.json'
        ) {
          annotationResolveRequests += 1;
        }
      } catch {
        // Ignore unrelated/non-JSON traffic.
      }
    });
    await frame.locator('#good-save').click();
    await expect(frame.locator('#save-status')).toHaveText('saved', { timeout: 20_000 });
    await frame.locator('#open-result').click();

    const pane = page.locator('[data-role="chat-preview-pane"]');
    await expect(pane.getByText('annotation.json', { exact: true }).last()).toBeVisible({
      timeout: 30_000,
    });
    await expect(pane.getByText(/annotated-after-failure/).last()).toBeVisible({
      timeout: 30_000,
    });
    // Once the initial resolve/subscribe reconciliation settles, remaining
    // idle for longer than the removed 3-second interval must do no request.
    await page.waitForTimeout(500);
    const idleResolveCount = annotationResolveRequests;
    await page.waitForTimeout(3_500);
    expect(annotationResolveRequests).toBe(idleResolveCount);

    await card.locator('[data-action="interactive-submit"]').click();
    await expect(card.getByText('Continued', { exact: true })).toBeVisible({
      timeout: 30_000,
    });
    await expect(page.locator('[data-action="agent-composer-send"]')).toBeVisible({
      timeout: 180_000,
    });

    // A real Agent write to the same natural path must update the already-open
    // stable Preview tab without the user closing/reopening it.
    await sendPrompt(page, [
      'Call bash exactly once with this exact command and do not call another tool:',
      "printf '%s' '{\"label\":\"agent-overwrite\",\"accepted\":true}' > /data/interactive-acceptance/annotation.json",
      'Do not explain the result.',
    ].join(' '));
    await expect(page.locator('[data-action="agent-composer-send"]')).toBeVisible({
      timeout: 180_000,
    });
    await expect(pane.getByText(/agent-overwrite/).last()).toBeVisible({
      timeout: 30_000,
    });
    await expect(pane.getByText('annotation.json', { exact: true })).toHaveCount(1);
    // The event may update the open renderer directly or invalidate+resolve;
    // both are valid. Assert the user-visible update above and only require
    // that either strategy settles without falling back to interval polling.
    const changedResolveCount = annotationResolveRequests;
    await page.waitForTimeout(3_500);
    expect(annotationResolveRequests).toBe(changedResolveCount);

    await frame.locator('#open-large').click();
    await expect(pane.getByText('large.csv', { exact: true }).last()).toBeVisible({
      timeout: 30_000,
    });
    await expect(pane.getByText(
      /Preview supports files up to 10 MB for this format/,
    )).toBeVisible({
      timeout: 30_000,
    });
    await expect(pane.getByRole('link', { name: 'Download' }).last()).toBeVisible();
    await pane.getByRole('button', { name: 'Close preview item', exact: true }).first().click();
    await expect(pane.getByText('annotation.json', { exact: true }).last()).toBeVisible({
      timeout: 20_000,
    });
  } finally {
    await api(
      `/api/v1/vfs?wf_id=${encodeURIComponent(workspace.workspace_scope_id)}`
      + '&path=%2Fdata%2Finteractive-acceptance',
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
