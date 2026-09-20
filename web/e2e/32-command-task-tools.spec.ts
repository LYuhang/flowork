import { execFileSync } from 'node:child_process';

import { expect, test, type BrowserContext, type Page } from '@playwright/test';

import { E2ECookieSession } from './cookie-session';
import {
  loadCompleteChatHistory,
  provisionRealRuntime,
  selectRuntimeModel,
  type RealRuntimeName,
  type RealRuntimeProfile,
} from './real-runtime-profile';
type TaskRow = {
  id: string;
  status: string;
  task_type: string;
  payload?: Record<string, unknown>;
  result?: Record<string, unknown> | null;
};

const MESSAGE_PATH = /\/api\/v1\/chat-scopes\/([^/]+)\/chats\/([^/]+)\/messages$/;
const PREFIX = 'command-tools-20260803-task';
const MINIMAL_WORKFLOW = {
  node_1: {
    node_id: 'node_1',
    node_type: 'StartNode',
    node_name: '__start__',
    node_description: '',
    input_fields: { x: { type: 'integer', value: 0, reference: '' } },
    output_fields: { x: { type: 'integer', description: '' } },
    node_config: {},
    children: ['node_2'],
    __attributes__: { x: 0, y: 0 },
  },
  node_2: {
    node_id: 'node_2',
    node_type: 'EndNode',
    node_name: '__end__',
    node_description: '',
    input_fields: { y: { type: 'integer', value: 0, reference: '__start__.x' } },
    output_fields: { y: { type: 'integer', description: '' } },
    node_config: {},
    children: [],
    __attributes__: { x: 240, y: 0 },
  },
};

test.setTimeout(2_400_000);

async function eventually<T>(
  load: () => Promise<T>,
  accept: (value: T) => boolean,
  timeout = 180_000,
): Promise<T> {
  const deadline = Date.now() + timeout;
  let latest = await load();
  while (!accept(latest) && Date.now() < deadline) {
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 1_000));
    latest = await load();
  }
  if (!accept(latest)) throw new Error(`condition not reached; latest=${JSON.stringify(latest)}`);
  return latest;
}

for (const runtime of ['langchain', 'codex'] as const satisfies readonly RealRuntimeName[]) {
  test.describe(`${runtime} /task every tool`, () => {
    const session = new E2ECookieSession();
    const unique = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const workflowName = `${PREFIX}-workflow-${runtime}-${unique}`;
    const scheduleName = `${PREFIX}-schedule-${runtime}-${unique}`;
    const updatedScheduleName = `${scheduleName}-updated`;
    let runtimeProfile: RealRuntimeProfile | null = null;
    let workflowId = '';
    let tenantId = '';
    let scheduleTaskId = '';
    let batchTaskId = '';
    let chatId = '';

    async function task(taskId: string): Promise<TaskRow> {
      return session.api(`/api/v1/tasks/${encodeURIComponent(taskId)}`)
        .then((response) => response.json()) as Promise<TaskRow>;
    }

    test.beforeAll(async () => {
      console.log(`[${runtime}-task] registering disposable user`);
      await session.register(`command-task-${runtime}`);
      runtimeProfile = await provisionRealRuntime(session, runtime);
      const me = await session.api('/api/v1/auth/me').then((response) => response.json()) as {
        tenant_id: string;
        user_id: string;
      };
      tenantId = me.tenant_id;

      console.log(`[${runtime}-task] seeding a durable executable workflow`);
      const created = await session.api('/api/v1/workflows', {
        method: 'POST',
        body: JSON.stringify({ name: workflowName, description: 'Task tool acceptance', tags: [] }),
      }).then((response) => response.json()) as { wf_id: string };
      workflowId = created.wf_id;
      await session.api(`/api/v1/workflows/${encodeURIComponent(workflowId)}/commits`, {
        method: 'POST',
        body: JSON.stringify({ workflow: MINIMAL_WORKFLOW, note: 'task acceptance fixture' }),
      });
    });

    test.afterAll(async () => {
      if (batchTaskId) {
        const response = await session.api(
          `/api/v1/tasks/${encodeURIComponent(batchTaskId)}`,
          {},
          true,
        );
        if (response.ok) {
          const current = await response.json() as TaskRow;
          if (['queued', 'running', 'resuming'].includes(current.status)) {
            await session.api(`/api/v1/tasks/${encodeURIComponent(batchTaskId)}/cancel`, {
              method: 'POST',
              body: JSON.stringify({ mode: 'force' }),
            }, true);
          }
        }
      }
      if (scheduleTaskId) {
        await session.api(
          `/api/v1/tasks/scheduled-runs/${encodeURIComponent(scheduleTaskId)}`,
          { method: 'DELETE' },
          true,
        );
      }
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
      if (workflowId) {
        await session.api(`/api/v1/workflows/${encodeURIComponent(workflowId)}`, {
          method: 'DELETE',
        }, true);
      }
      runtimeProfile?.cleanup();
    });

    test.beforeEach(async ({ context }: { context: BrowserContext }) => {
      await session.seed(context, 'en');
    });

    async function openChat(page: Page) {
      await page.goto('/chat');
      await expect(page.locator('[data-role="agent-composer-input"]')).toBeVisible({
        timeout: 30_000,
      });
      await page.locator('[data-action="chat-new"]').click();
      if (!runtimeProfile) throw new Error(`${runtime} Runtime profile was not provisioned`);
      await selectRuntimeModel(page, runtimeProfile);
      await page.locator('[data-role="chat-composer-options-toggle"]').click();
      await expect(page.locator('[data-role="chat-approval-mode-select"]')).toHaveCount(0);
      console.log(`[${runtime}-task] composer ready`);
    }

    function makeCompletedBatchResumableFixture(taskId: string) {
      // The real batch produces the encrypted checkpoint first. We only move
      // its public lifecycle columns back to queued so task_cancel exercises
      // the deterministic queued branch; task_resume then consumes the real
      // checkpoint and dispatches an actual worker execution.
      const code = [
        'import asyncio, os, uuid',
        'from vibecanvas_api.storage.db import session_scope',
        'from vibecanvas_api.storage.repo_tasks import TasksRepo',
        'async def main():',
        '    async with session_scope(tenant_id=os.environ["E2E_TENANT_ID"]) as session:',
        '        await TasksRepo(session).update_status(',
        '            uuid.UUID(os.environ["E2E_TASK_ID"]),',
        '            status="queued", finished_at=None, progress=0,',
        '        )',
        'asyncio.run(main())',
      ].join('\n');
      const dockerSandboxContainer = process.env.FLOWORK_E2E_DOCKER_ACCOUNT_CONTAINER;
      if (dockerSandboxContainer) {
        const dockerApiContainer = process.env.FLOWORK_E2E_DOCKER_API_CONTAINER
          ?? dockerSandboxContainer.replace(/-sandboxd(-\d+)?$/, '-api$1');
        if (dockerApiContainer === dockerSandboxContainer) {
          throw new Error(
            'set FLOWORK_E2E_DOCKER_API_CONTAINER when the sandbox container name '
              + 'does not follow the <project>-sandboxd-<index> convention',
          );
        }
        execFileSync('docker.exe', [
          'exec',
          '-e', `E2E_TASK_ID=${taskId}`,
          '-e', `E2E_TENANT_ID=${tenantId}`,
          dockerApiContainer,
          'python', '-c', code,
        ], {
          stdio: ['ignore', 'ignore', 'pipe'],
        });
        return;
      }
      execFileSync('bash', [
        '-lc',
        'set -a; source /tmp/vibecanvas-native/.env.native; '
          + '"$VIBECANVAS_PYTHON" -c "$E2E_FIXTURE_CODE"',
      ], {
        env: {
          ...process.env,
          E2E_FIXTURE_CODE: code,
          E2E_TASK_ID: taskId,
          E2E_TENANT_ID: tenantId,
        },
        stdio: ['ignore', 'ignore', 'pipe'],
      });
    }

    async function invoke(
      page: Page,
      toolName: string,
      instruction: string,
      marker: string,
      timeout = 360_000,
    ) {
      console.log(`[${runtime}-task] invoking ${toolName}`);
      const matchingActivities = page.locator('[data-tool-activity="true"]').filter({
        hasText: toolName,
      });
      const before = await matchingActivities.count();
      console.log(`[${runtime}-task] ${toolName} prior activities=${before}`);
      await page.locator('[data-role="agent-composer-input"]').fill(
        `/task ${instruction} After that tool succeeds, reply exactly ${marker}.`,
      );
      console.log(`[${runtime}-task] sending ${toolName}`);
      const [response] = await Promise.all([
        page.waitForResponse((candidate) => (
          candidate.request().method() === 'POST'
          && MESSAGE_PATH.test(new URL(candidate.url()).pathname)
        ), { timeout: 30_000 }),
        page.locator('[data-action="agent-composer-send"]').click(),
      ]);
      expect(response.ok()).toBe(true);
      console.log(`[${runtime}-task] accepted ${toolName}`);
      const match = new URL(response.url()).pathname.match(MESSAGE_PATH);
      expect(match).not.toBeNull();
      chatId ||= match![2];
      await expect(matchingActivities).toHaveCount(before + 1, { timeout: 60_000 });
      const activity = matchingActivities.last();
      const activityToggle = activity.locator('[data-action="tool-activity-toggle"]');
      if (await activityToggle.getAttribute('aria-expanded') !== 'true') await activityToggle.click();
      const calls = activity.locator(`[data-role="tool-call"][data-tool-name="${toolName}"]`);
      await expect(calls).toHaveCount(1, { timeout: 30_000 });
      const call = calls.first();
      await expect(call).toHaveAttribute('data-tool-status', /^(done|error)$/, { timeout });
      console.log(`[${runtime}-task] terminal card for ${toolName}`);
      if (await call.getAttribute('data-tool-status') === 'error') {
        const callToggle = call.locator('[data-action="tool-call-toggle"]');
        if (await callToggle.getAttribute('aria-expanded') !== 'true') await callToggle.click();
        throw new Error(
          `${toolName} failed through ${runtime}: `
            + (await call.locator('[data-role="tool-output"]').innerText()).trim(),
        );
      }
      await expect(
        page.locator('[data-message-role="assistant"]').filter({ hasText: marker }).last(),
      ).toBeVisible({ timeout });
      await expect(page.locator('[data-action="agent-composer-send"]')).toBeVisible({
        timeout: 60_000,
      });
    }

    test(`invokes all seven /task tools through ${runtime}`, async ({ page }) => {
      await openChat(page);

      await invoke(
        page,
        'task_create_scheduled_run',
        `Call task_create_scheduled_run exactly once with name "${scheduleName}", `
          + `workflow_id "${workflowId}", schedule_type "interval", interval_seconds 3600, `
          + 'timezone "UTC", enabled false, empty input_preset, mount_enabled false, and an '
          + 'empty notification_policy. Do not call another command tool.',
        'TASK_CREATE_OK',
      );
      const scheduled = await session.api(
        '/api/v1/tasks?task_type=scheduled_run&limit=100',
      ).then((response) => response.json()) as { items: TaskRow[] };
      const createdSchedule = scheduled.items.find((item) => item.payload?.name === scheduleName);
      expect(createdSchedule).toBeTruthy();
      scheduleTaskId = createdSchedule!.id;
      expect(createdSchedule!.status).toBe('paused');

      await invoke(
        page,
        'task_list',
        'Call task_list exactly once with task_type ["scheduled_run"], limit 10 and offset 0. '
          + 'Do not call another command tool.',
        'TASK_LIST_OK',
      );

      await invoke(
        page,
        'task_get',
        `Call task_get exactly once with task_id "${scheduleTaskId}". `
          + 'Do not call another command tool.',
        'TASK_GET_OK',
      );

      await invoke(
        page,
        'task_collect_diagnostics',
        `Call task_collect_diagnostics exactly once with task_id "${scheduleTaskId}", `
          + 'event_limit 20 and execution_limit 20. Do not call another command tool.',
        'TASK_DIAGNOSTICS_OK',
      );

      await invoke(
        page,
        'task_update_scheduled_run',
        `Call task_update_scheduled_run exactly once with task_id "${scheduleTaskId}", `
          + `name "${updatedScheduleName}", interval_seconds 7200, timezone "Asia/Shanghai", `
          + 'and enabled false. Do not call task_get or another command tool.',
        'TASK_UPDATE_OK',
      );
      const updated = await session.api(
        `/api/v1/tasks/scheduled-runs/${encodeURIComponent(scheduleTaskId)}`,
      ).then((response) => response.json()) as {
        task: TaskRow;
        schedule: { name: string; interval_seconds: number; timezone: string; enabled: boolean };
      };
      expect(updated.schedule).toMatchObject({
        name: updatedScheduleName,
        interval_seconds: 7200,
        timezone: 'Asia/Shanghai',
        enabled: false,
      });

      console.log(`[${runtime}-task] creating a real durable batch checkpoint for cancel/resume`);
      const batchRows = [{ x: 1 }, { x: 2 }];
      const batch = await session.api(`/api/v1/workflows/${encodeURIComponent(workflowId)}/batch`, {
        method: 'POST',
        body: JSON.stringify({
          data_source: { rows: batchRows },
          column_mapping: { x: 'x' },
          concurrency: 1,
        }),
      }).then((response) => response.json()) as { task_id: string };
      batchTaskId = batch.task_id;
      const completed = await eventually(
        () => task(batchTaskId),
        (value) => value.status === 'finished',
        180_000,
      );
      expect((completed.result?.artifact_uris as Record<string, unknown> | undefined)?.jsonl)
        .toBeTruthy();
      makeCompletedBatchResumableFixture(batchTaskId);
      expect((await task(batchTaskId)).status).toBe('queued');

      if (runtime === 'codex') {
        await invoke(
          page,
          'task_get',
          `Call task_get exactly once with task_id "${batchTaskId}" to inspect the exact `
            + 'queued batch before the next Turn cancels it. Do not call another command tool.',
          'TASK_BATCH_INSPECT_OK',
        );
      }

      await invoke(
        page,
        'task_cancel',
        `The exact batch Task was already inspected and is queued. Call task_cancel exactly once `
          + `with task_id "${batchTaskId}" and mode "soft". Do not call another command tool.`,
        'TASK_CANCEL_OK',
      );
      const resumable = await eventually(
        () => task(batchTaskId),
        (value) => (
          value.status === 'cancelled'
          && Boolean((value.result?.artifact_uris as Record<string, unknown> | undefined)?.jsonl)
        ),
        30_000,
      );
      expect(resumable.status).not.toBe('finished');

      await invoke(
        page,
        'task_resume',
        `Call task_resume exactly once with task_id "${batchTaskId}". `
          + 'Do not call task_get or another command tool.',
        'TASK_RESUME_OK',
      );
      const events = await eventually(
        () => session.api(`/api/v1/tasks/${encodeURIComponent(batchTaskId)}/events?limit=200`)
          .then(async (response) => {
            const payload = await response.json() as {
              items: Array<{ payload?: { action?: string } }>;
            };
            return payload.items;
          }),
        (items) => items.some((item) => item.payload?.action === 'batch.resume_started'),
        180_000,
      );
      expect(events.some((item) => item.payload?.action === 'batch.resume_started')).toBe(true);
      const afterResume = await task(batchTaskId);
      if (['queued', 'running', 'resuming'].includes(afterResume.status)) {
        await session.api(`/api/v1/tasks/${encodeURIComponent(batchTaskId)}/cancel`, {
          method: 'POST',
          body: JSON.stringify({ mode: 'force' }),
        }, true);
      }

      await invoke(
        page,
        'task_delete_scheduled_run',
        `Call task_delete_scheduled_run exactly once with task_id "${scheduleTaskId}". `
          + 'Do not call task_get or another command tool.',
        'TASK_DELETE_OK',
      );
      expect((await session.api(
        `/api/v1/tasks/scheduled-runs/${encodeURIComponent(scheduleTaskId)}`,
        {},
        true,
      )).status).toBe(404);
      scheduleTaskId = '';

      await page.reload({ waitUntil: 'domcontentloaded' });
      await page.locator(`button[data-chat-id="${chatId}"]`).click();
      await loadCompleteChatHistory(page, chatId);
      const activities = page.locator('[data-tool-activity="true"]');
      await expect(activities.first()).toBeVisible({ timeout: 60_000 });
      for (let index = 0; index < await activities.count(); index += 1) {
        const toggle = activities.nth(index).locator('[data-action="tool-activity-toggle"]');
        if (await toggle.getAttribute('aria-expanded') !== 'true') await toggle.click();
      }
      for (const toolName of [
        'task_create_scheduled_run',
        'task_list',
        'task_get',
        'task_collect_diagnostics',
        'task_update_scheduled_run',
        'task_cancel',
        'task_resume',
        'task_delete_scheduled_run',
      ]) {
        await expect(page.locator(
          `[data-role="tool-call"][data-tool-name="${toolName}"][data-tool-status="done"]`,
        )).toHaveCount(
          runtime === 'codex' && toolName === 'task_get' ? 2 : 1,
          { timeout: 30_000 },
        );
      }
    });
  });
}
