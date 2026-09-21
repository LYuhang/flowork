import { spawn } from 'node:child_process';
import { openSync } from 'node:fs';
import { readFile, writeFile } from 'node:fs/promises';
import { expect, test, type BrowserContext, type Page } from '@playwright/test';

const API_BASE = process.env.VIBECANVAS_API_BASE ?? 'http://127.0.0.1:8000';
const APP_ORIGIN = process.env.VIBECANVAS_E2E_ORIGIN
  ?? `http://${process.env.VIBECANVAS_E2E_HOST ?? '127.0.0.1'}:${process.env.VIBECANVAS_WEB_PORT ?? '5173'}`;
const MESSAGE_PATH = /\/api\/v1\/chat-scopes\/([^/]+)\/chats\/([^/]+)\/messages$/;
const NATIVE_RUN_DIR = '/tmp/vibecanvas-native';
const PYTHON = process.env.VIBECANVAS_PYTHON
  ?? 'python3';

type BackgroundJob = {
  job_id: string;
  title: string;
  status: string;
  result: Record<string, unknown>;
  error?: Record<string, unknown>;
  delivery_status?: 'pending' | 'delivered';
  delivery_batch_id?: string | null;
  delivered_at?: string | null;
};

test.setTimeout(600_000);

const sessionCookies = new Map<string, string>();
let accountEmail = '';
const accountPassword = 'pw-123456';

function rememberResponseCookies(response: Response) {
  const values = response.headers.getSetCookie();
  for (const value of values) {
    const pair = value.split(';', 1)[0];
    const separator = pair.indexOf('=');
    if (separator <= 0) continue;
    sessionCookies.set(pair.slice(0, separator), pair.slice(separator + 1));
  }
}

function cookieHeader() {
  return [...sessionCookies.entries()].map(([name, value]) => `${name}=${value}`).join('; ');
}

function csrfToken() {
  const entry = [...sessionCookies.entries()].find(([name]) => (
    name.endsWith('vibecanvas-web-csrf')
  ));
  return entry?.[1] ?? '';
}

function rememberBrowserCookies(cookies: Awaited<ReturnType<BrowserContext['cookies']>>) {
  sessionCookies.clear();
  for (const cookie of cookies) {
    if (cookie.name.includes('vibecanvas-web-')) {
      sessionCookies.set(cookie.name, cookie.value);
    }
  }
}

async function api(path: string, init: RequestInit = {}, allowError = false) {
  const method = (init.method ?? 'GET').toUpperCase();
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      Cookie: cookieHeader(),
      Origin: APP_ORIGIN,
      ...(method === 'GET' || method === 'HEAD' ? {} : { 'X-CSRF-Token': csrfToken() }),
      ...init.headers,
    },
  });
  rememberResponseCookies(response);
  if (!allowError && !response.ok) {
    throw new Error(`${init.method ?? 'GET'} ${path} failed: ${response.status} ${await response.text()}`);
  }
  return response;
}

test.beforeAll(async () => {
  accountEmail = `background_${Date.now()}_${Math.random().toString(36).slice(2, 10)}@example.com`;
  const registration = await fetch(`${API_BASE}/api/v1/auth/register`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Origin: APP_ORIGIN },
    body: JSON.stringify({
      email: accountEmail,
      username: accountEmail.split('@')[0],
      password: accountPassword,
    }),
  });
  if (!registration.ok) {
    throw new Error(`register failed: ${registration.status} ${await registration.text()}`);
  }
  rememberResponseCookies(registration);
  await api('/api/v1/agent-runtime/settings', {
    method: 'PUT',
    body: JSON.stringify({ default_runtime_type: 'langchain' }),
  });
});

test.beforeEach(async ({ context }: { context: BrowserContext }) => {
  await context.addCookies([...sessionCookies.entries()].map(([name, value]) => ({
    name,
    value,
    url: API_BASE,
  })));
  await context.addInitScript(() => {
    try {
      window.localStorage.setItem('vibecanvas.locale', 'en');
    } catch {
      // Opaque Preview frames intentionally cannot access Web Storage.
    }
  });
});

async function sendPrompt(page: Page, prompt: string) {
  const composer = page.locator('[data-role="agent-composer-input"]');
  for (let attempt = 0; attempt < 4; attempt += 1) {
    await expect(composer).toBeEnabled({ timeout: 60_000 });
    await composer.fill(prompt);
    const responsePromise = page.waitForResponse((candidate) => (
      candidate.request().method() === 'POST'
      && MESSAGE_PATH.test(new URL(candidate.url()).pathname)
    ), { timeout: 15_000 }).catch(() => null);
    await page.locator('[data-action="agent-composer-send"]').click();
    const response = await responsePromise;
    if (response === null || response.status() === 409) {
      // A server-originated delivery Turn may have won the Chat claim between
      // UI frames. The product keeps this draft; wait for that same Turn to
      // finish and submit the user message, never the background job, again.
      await page.waitForTimeout(1_000);
      continue;
    }
    expect(response.ok()).toBe(true);
    const match = new URL(response.url()).pathname.match(MESSAGE_PATH);
    expect(match).not.toBeNull();
    return { scopeId: match![1], chatId: match![2] };
  }
  throw new Error('message was not durably accepted after the active Turn finished');
}

async function listJobs(scopeId: string, chatId: string): Promise<BackgroundJob[]> {
  const response = await api(
    `/api/v1/chat-scopes/${encodeURIComponent(scopeId)}/chats/${encodeURIComponent(chatId)}`
    + '/background-jobs?status=all&limit=100',
    {},
    true,
  );
  // The message POST is observable just before its asynchronous Turn has
  // registered the draft Chat. Treat that short fixture race as "no jobs yet".
  if (response.status === 404) return [];
  if (!response.ok) {
    throw new Error(`list background jobs failed: ${response.status} ${await response.text()}`);
  }
  return response.json() as Promise<BackgroundJob[]>;
}

async function waitForProcessExit(pid: number) {
  const deadline = Date.now() + 15_000;
  while (Date.now() < deadline) {
    try {
      process.kill(pid, 0);
    } catch {
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  process.kill(pid, 'SIGKILL');
}

async function stopManaged(name: 'api' | 'worker') {
  const pid = Number((await readFile(`${NATIVE_RUN_DIR}/${name}.pid`, 'utf8')).trim());
  process.kill(pid, 'SIGTERM');
  await waitForProcessExit(pid);
}

async function startManaged(
  name: 'api' | 'worker',
  args: string[],
) {
  const logFd = openSync(`${NATIVE_RUN_DIR}/${name}.log`, 'a');
  const child = spawn(`${NATIVE_RUN_DIR}/run.sh`, args, {
    detached: true,
    stdio: ['ignore', logFd, logFd],
  });
  child.unref();
  await writeFile(`${NATIVE_RUN_DIR}/${name}.pid`, `${child.pid}\n`);
}

async function restartNativeControlPlane() {
  await Promise.all([stopManaged('api'), stopManaged('worker')]);
  await new Promise<void>((resolve) => {
    const shutdown = spawn('redis-cli', ['-p', '6379', 'shutdown', 'nosave'], {
      stdio: 'ignore',
    });
    shutdown.on('exit', () => resolve());
    shutdown.on('error', () => resolve());
  });
  await new Promise<void>((resolve, reject) => {
    const redis = spawn('redis-server', [
      '--daemonize', 'yes',
      '--port', '6379',
      '--dir', '/tmp',
    ], { stdio: 'ignore' });
    redis.on('exit', (code) => code === 0 ? resolve() : reject(new Error(`redis restart failed: ${code}`)));
    redis.on('error', reject);
  });
  await startManaged('api', [
    PYTHON,
    '-m',
    'uvicorn',
    'vibecanvas_api.app:build_app',
    '--factory',
    '--host',
    '127.0.0.1',
    '--port',
    '8000',
  ]);
  await expect.poll(async () => {
    try {
      return (await fetch(`${API_BASE}/healthz`)).ok;
    } catch {
      return false;
    }
  }, { timeout: 120_000, intervals: [200, 500, 1_000, 2_000] }).toBe(true);
  await startManaged('worker', [
    PYTHON,
    '-m',
    'vibecanvas_api.background_worker',
  ]);
}

test('LangChain background subagents persist, batch-deliver, survive reconnect/login, and cancel from Preview', async ({
  page,
  context,
}) => {
  await page.goto('/chat');
  await expect(page.locator('[data-role="agent-composer-input"]')).toBeVisible({
    timeout: 20_000,
  });
  await page.locator('[data-action="chat-new"]').click();

  const firstPrompt = [
    'Call the subagent tool exactly twice with background_run=true and max_iterations=5.',
    'First use title "Background acceptance alpha" and this complete prompt:',
    '"Do not call any non-terminal tool. Immediately call set_output with result Background alpha marker."',
    'Second use title "Background acceptance beta" and this complete prompt:',
    '"Do not call any non-terminal tool. Immediately call set_output with result Background beta marker."',
    'After both jobs are accepted, call bash exactly once with command "sleep 30" so the foreground Turn remains busy.',
    'Do not start any more background jobs.',
  ].join(' ');
  const { scopeId, chatId } = await sendPrompt(page, firstPrompt);

  try {
    await expect.poll(
      async () => (await listJobs(scopeId, chatId)).filter(
        (job) => job.title.startsWith('Background acceptance '),
      ).length,
      { timeout: 180_000, intervals: [1_000, 2_000, 3_000] },
    ).toBe(2);

    const backgroundButton = page.locator('[data-action="chat-background-jobs"]');
    await expect(backgroundButton).toBeEnabled({ timeout: 30_000 });
    await backgroundButton.click();
    const pane = page.locator('[data-role="chat-preview-pane"]');
    await expect(pane.getByText('Background acceptance alpha', { exact: true })).toBeVisible({
      timeout: 30_000,
    });
    await expect(pane.getByText('Background acceptance beta', { exact: true })).toBeVisible({
      timeout: 30_000,
    });

    await expect.poll(async () => {
      const jobs = await listJobs(scopeId, chatId);
      return jobs
        .filter((job) => job.title.startsWith('Background acceptance '))
        .map((job) => job.status)
        .sort();
    }, {
      timeout: 240_000,
      intervals: [1_000, 2_000, 3_000],
    }).toEqual(['completed', 'completed']);

    const completed = (await listJobs(scopeId, chatId)).filter(
      (job) => job.title.startsWith('Background acceptance '),
    );
    expect(completed.map((job) => JSON.stringify(job.result)).join('\n')).toContain(
      'Background alpha marker',
    );
    expect(completed.map((job) => JSON.stringify(job.result)).join('\n')).toContain(
      'Background beta marker',
    );

    const activity = page.locator('[data-role="background-job-activity"]').last();
    await expect(activity).toBeVisible({ timeout: 240_000 });
    await activity.click();
    await expect(pane.getByText('Delivery batch', { exact: false })).toBeVisible({
      timeout: 30_000,
    });
    await expect.poll(async () => (
      (await listJobs(scopeId, chatId))
        .filter((job) => job.title.startsWith('Background acceptance '))
        .every((job) => !!job.delivered_at && !!job.delivery_batch_id)
    ), { timeout: 60_000 }).toBe(true);

    let cancelledJobId = '';
    if (process.env.VIBECANVAS_RESTART_CONTROL_PLANE_E2E === '1') {
      const interruptedPrompt = [
        'Call the subagent tool exactly twice with background_run=true and max_iterations=20.',
        'First use title "Background acceptance interrupted" and this complete prompt:',
        '"You must call bash with command sleep 180 && echo INTERRUPTED_DONE and timeout_s=300.',
        'Do not call set_output until that exact stdout returns; then set_output to Interrupted task unexpectedly finished."',
        'Second use title "Background acceptance cancellable" and this complete prompt:',
        '"You must call bash with command sleep 180 && echo CANCELLABLE_DONE and timeout_s=300.',
        'Do not call set_output until that exact stdout returns; then set_output to Cancellable task unexpectedly finished."',
        'Create exactly those two background jobs now, then create no additional job.',
      ].join(' ');
      await sendPrompt(page, interruptedPrompt);
      let interrupted: BackgroundJob | undefined;
      let cancellable: BackgroundJob | undefined;
      await expect.poll(async () => {
        const jobs = await listJobs(scopeId, chatId);
        interrupted = jobs.find(
          (job) => job.title === 'Background acceptance interrupted',
        );
        cancellable = jobs.find(
          (job) => job.title === 'Background acceptance cancellable',
        );
        return [interrupted?.status ?? 'missing', cancellable?.status ?? 'missing'];
      }, {
        timeout: 180_000,
        intervals: [500, 1_000, 2_000],
      }).toEqual(['running', 'running']);
      expect(interrupted).toBeDefined();
      expect(cancellable).toBeDefined();
      cancelledJobId = cancellable!.job_id;

      const heldClose = await api(
        `/api/v1/chats/sandbox?chat_id=${encodeURIComponent(chatId)}`,
        { method: 'DELETE' },
        true,
      );
      expect(heldClose.status).toBe(409);
      const heldDetail = (await heldClose.json()) as {
        detail?: { code?: string; job_ids?: string[] };
      };
      expect(heldDetail.detail?.code).toBe('sandbox_held_by_background_jobs');
      expect(heldDetail.detail?.job_ids).toContain(interrupted!.job_id);

      const viewAll = pane.getByRole('button', { name: 'View all', exact: true });
      if (await viewAll.isVisible()) await viewAll.click();
      await expect(pane.getByText(
        'Background acceptance cancellable',
        { exact: true },
      )).toBeVisible({ timeout: 30_000 });
      await pane.getByRole('button', {
        name: `Cancel ${cancellable!.job_id}`,
      }).click();
      await expect(pane.getByText('Cancel this task?', { exact: true })).toBeVisible();
      await pane.getByRole('button', { name: 'Cancel task', exact: true }).click();
      await expect.poll(async () => (
        (await listJobs(scopeId, chatId)).find(
          (job) => job.job_id === cancellable!.job_id,
        )?.status
      ), {
        timeout: 90_000,
        intervals: [500, 1_000, 2_000],
      }).toBe('cancelled');
      await restartNativeControlPlane();
      await expect.poll(async () => (
        (await listJobs(scopeId, chatId))
          .filter((job) => job.title.startsWith('Background acceptance '))
          .map((job) => `${job.status}:${job.delivered_at ? 'delivered' : 'pending'}`)
          .sort()
      ), { timeout: 60_000 }).toEqual([
        'cancelled:delivered',
        'completed:delivered',
        'completed:delivered',
        'failed:delivered',
      ]);
      const afterReplacement = (await listJobs(scopeId, chatId)).find(
        (job) => job.job_id === interrupted!.job_id,
      );
      expect(afterReplacement?.status).toBe('failed');
      // A graceful cancellation can persist executor_shutdown. If the
      // replacement process observes the expired lease first, the stricter
      // state-unknown result is correct: never replay work whose side effects
      // cannot be proven absent. A model/tool failure may also win the race
      // before process replacement; it remains a valid, explicit terminal
      // result as long as its diagnostic message is preserved.
      expect([
        'executor_shutdown',
        'executor_disconnected_state_unknown',
        'subagent_failed',
      ]).toContain(afterReplacement?.error?.code);
      expect(String(afterReplacement?.error?.message ?? '')).not.toHaveLength(0);
      expect((await listJobs(scopeId, chatId)).filter(
        (job) => job.title === 'Background acceptance interrupted',
      )).toHaveLength(1);
      await expect(page.locator('[data-role="agent-composer-input"]')).toBeEnabled({
        timeout: 120_000,
      });
      await expect(page.getByText(/Agent init error/)).toHaveCount(0);

      // Terminal-but-unsubmitted results hold the sandbox; once the new API
      // has durably delivered the failure Turn, releasing it is allowed.
      const releasedClose = await api(
        `/api/v1/chats/sandbox?chat_id=${encodeURIComponent(chatId)}`,
        { method: 'DELETE' },
        true,
      );
      expect(releasedClose.status).toBe(200);
    }

    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.locator('[data-role="background-job-activity"]').last()).toBeVisible({
      timeout: 180_000,
    });
    await expect(page.locator('[data-role="agent-composer-input"]')).toBeEnabled({
      timeout: 180_000,
    });
    await page.locator('[data-action="chat-background-jobs"]').click();
    const restoredPane = page.locator('[data-role="chat-preview-pane"]');
    await expect(restoredPane.getByText(
      'Background acceptance alpha',
      { exact: true },
    )).toHaveCount(0);
    await restoredPane.getByRole('button', { name: 'All', exact: true }).click();
    await expect(restoredPane.getByText(
      'Background acceptance alpha',
      { exact: true },
    )).toBeVisible({ timeout: 30_000 });

    // The result list is durable and not tied to the current browser transport.
    // Let background polling fail while offline, reconnect, and then revoke the
    // entire login session before signing back into the same account.
    await context.setOffline(true);
    await page.waitForTimeout(2_500);
    await context.setOffline(false);
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.locator('[data-role="background-job-activity"]').last()).toBeVisible({
      timeout: 60_000,
    });
    await page.locator('[data-action="open-user-menu"]').click();
    await page.locator('[data-action="logout"]').click();
    await expect(page).toHaveURL(/\/login$/, { timeout: 15_000 });
    await page.locator('#login-email').fill(accountEmail);
    await page.locator('#login-password').fill(accountPassword);
    await page.getByRole('button', { name: /sign in|登录/i }).click();
    await expect(page).toHaveURL(/\/chat$/, { timeout: 20_000 });
    rememberBrowserCookies(await context.cookies(API_BASE));
    expect(cookieHeader()).toContain('vibecanvas-web-session=');
    await expect(page.locator('[data-role="background-job-activity"]').last()).toBeVisible({
      timeout: 60_000,
    });
    await page.locator('[data-action="chat-background-jobs"]').click();
    const reloginPane = page.locator('[data-role="chat-preview-pane"]');
    await reloginPane.getByRole('button', { name: 'All', exact: true }).click();
    await expect(reloginPane.getByText(
      'Background acceptance alpha',
      { exact: true },
    )).toBeVisible({ timeout: 30_000 });
    if (cancelledJobId) {
      const restoredCancelled = (await listJobs(scopeId, chatId)).find(
        (job) => job.job_id === cancelledJobId,
      );
      expect(restoredCancelled?.status).toBe('cancelled');
      expect(restoredCancelled?.delivery_status).toBe('delivered');
    }
  } finally {
    await api(
      `/api/v1/chat-scopes/${encodeURIComponent(scopeId)}/chats/${encodeURIComponent(chatId)}`,
      { method: 'DELETE' },
      true,
    );
  }
});
