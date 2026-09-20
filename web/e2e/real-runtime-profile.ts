import {
  chmodSync,
  copyFileSync,
  existsSync,
  mkdirSync,
  rmSync,
} from 'node:fs';
import { homedir } from 'node:os';
import { join, posix, resolve, sep } from 'node:path';
import { randomUUID } from 'node:crypto';
import { spawnSync } from 'node:child_process';

import { expect, type Page } from '@playwright/test';

import { E2ECookieSession } from './cookie-session';

export type RealRuntimeName = 'langchain' | 'codex';

type RuntimeCapabilities = {
  runtime_type: RealRuntimeName;
  runtime_available: boolean;
  authenticated: boolean | null;
  default_model_id: string | null;
  error_code: string | null;
  models: Array<{
    id: string;
    label: string;
    provider?: string | null;
    api_source?: string | null;
  }>;
};

export type RealRuntimeProfile = {
  runtime: RealRuntimeName;
  authProfile: 'langchain_model' | 'chatgpt_account' | 'managed_api' | 'personal_api';
  modelOptionLabel: string | null;
  modelId: string;
  modelSourceId: string;
  cleanup: () => void;
};

function modelSourceId(model: RuntimeCapabilities['models'][number]) {
  if (model.api_source) return model.api_source;
  if (model.id.startsWith('codex:account:')) return 'chatgpt_account';
  if (model.id.startsWith('langchain:openrouter:')
    || model.id.startsWith('codex:openrouter:')) return 'openrouter_oauth';
  if (model.id === 'langchain:default' || model.id.startsWith('codex:managed:')) {
    return 'managed_api';
  }
  return 'manual';
}

function nativeRuntimeVolumeRoot() {
  return resolve(
    process.env.AGENT_RUNTIME_ROOT
      ?? join(homedir(), '.vibecanvas', 'agent-runtime'),
  );
}

function assertSafeDockerSegment(value: string, label: string) {
  if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(value)) {
    throw new Error(`invalid ${label} path segment for Codex identity staging`);
  }
}

function assertSafeContainerName(value: string) {
  if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(value)) {
    throw new Error('invalid Docker container name for Codex identity staging');
  }
}

function dockerExec(container: string, script: string, args: string[] = []) {
  return spawnSync(
    'docker.exe',
    ['exec', '-u', '0', container, 'sh', '-c', script, 'flowork-e2e', ...args],
    {
      encoding: 'utf8',
      maxBuffer: 1024 * 1024,
      timeout: 30_000,
    },
  );
}

function dockerRuntimeVolumeRoot(container: string) {
  const result = dockerExec(container, 'printf %s "$AGENT_RUNTIME_ROOT"');
  if (result.error || result.status !== 0) return null;
  const value = String(result.stdout ?? '').trim();
  return value.startsWith('/') && posix.normalize(value) === value ? value : null;
}

function dockerPathExists(container: string, path: string) {
  return dockerExec(container, 'test -e "$1"', [path]).status === 0;
}

function stageDockerCodexAccountIdentity(
  container: string,
  tenantId: string,
  userId: string,
  root: string,
) {
  assertSafeContainerName(container);
  assertSafeDockerSegment(tenantId, 'tenant');
  assertSafeDockerSegment(userId, 'user');
  const accountRoot = posix.join(root, tenantId, userId, 'codex-account-v1');
  const accountHome = posix.join(accountRoot, '.codex');
  const destination = posix.join(accountHome, 'auth.json');
  if (dockerPathExists(container, destination)) {
    console.log(`[real-runtime] codex_identity=existing location=docker:${destination}`);
    return () => undefined;
  }
  const source = join(homedir(), '.codex', 'auth.json');
  if (!existsSync(source)) return null;
  const temporary = `/tmp/flowork-e2e-codex-auth-${randomUUID()}.json`;
  const copied = spawnSync(
    'docker.exe',
    ['cp', '--', source, `${container}:${temporary}`],
    { encoding: 'utf8', maxBuffer: 1024 * 1024, timeout: 30_000 },
  );
  if (copied.error || copied.status !== 0) {
    throw new Error('could not copy Codex identity into the opted-in Docker container');
  }
  const staged = dockerExec(
    container,
    [
      'set -eu',
      'mkdir -p "$1"',
      'chown 10001:10001 "$2" "$1"',
      'chmod 0700 "$2" "$1"',
      'install -o 10001 -g 10001 -m 0600 "$3" "$4"',
      'rm -f -- "$3"',
    ].join('; '),
    [accountHome, accountRoot, temporary, destination],
  );
  if (staged.error || staged.status !== 0) {
    dockerExec(container, 'rm -f -- "$1"', [temporary]);
    throw new Error('could not install Codex identity into the Docker Runtime volume');
  }
  console.log(`[real-runtime] codex_identity=staged location=docker:${destination}`);
  return () => {
    dockerExec(container, 'rm -rf -- "$1"', [accountRoot]);
  };
}

function stageNativeCodexAccountIdentity(tenantId: string, userId: string) {
  const root = nativeRuntimeVolumeRoot();
  const accountRoot = resolve(root, tenantId, userId, 'codex-account-v1');
  if (!accountRoot.startsWith(`${root}${sep}`)) {
    throw new Error('refusing to create Codex identity outside AGENT_RUNTIME_ROOT');
  }
  const accountHome = join(accountRoot, '.codex');
  const destination = join(accountHome, 'auth.json');
  // A reusable acceptance user may already own a connected account. Do not
  // overwrite or later delete that identity merely to run this fixture.
  if (existsSync(destination)) {
    console.log(`[real-runtime] codex_identity=existing location=native:${destination}`);
    return () => undefined;
  }

  const source = join(homedir(), '.codex', 'auth.json');
  if (!existsSync(source)) return null;
  const accountRootExisted = existsSync(accountRoot);
  const accountHomeExisted = existsSync(accountHome);
  mkdirSync(accountHome, { recursive: true, mode: 0o700 });
  if (!accountHomeExisted) chmodSync(accountHome, 0o700);
  copyFileSync(source, destination);
  chmodSync(destination, 0o600);
  console.log(`[real-runtime] codex_identity=staged location=native:${destination}`);
  return () => {
    rmSync(destination, { force: true });
    if (!accountRootExisted) {
      rmSync(accountRoot, { recursive: true, force: true });
    } else if (!accountHomeExisted) {
      rmSync(accountHome, { recursive: true, force: true });
    }
  };
}

function preferredModel(capabilities: RuntimeCapabilities) {
  if (capabilities.runtime_type === 'codex') {
    // Exercise the connected ChatGPT account when present. Managed/personal
    // API models remain a valid explicit fallback and still use host-brokered
    // capabilities; no credential value enters this fixture or its reports.
    return capabilities.models.find((model) => model.provider === 'chatgpt')
      ?? capabilities.models.find((model) => model.id.startsWith('codex:managed:'))
      ?? capabilities.models.find((model) => model.id.startsWith('codex:credential:'))
      ?? capabilities.models[0];
  }
  return capabilities.models.find((model) => model.id === capabilities.default_model_id)
    ?? capabilities.models[0];
}

export async function provisionRealRuntime(
  session: E2ECookieSession,
  runtime: RealRuntimeName,
): Promise<RealRuntimeProfile> {
  const settings = await session.api('/api/v1/agent-runtime/settings', {
    method: 'PUT',
    body: JSON.stringify({ default_runtime_type: runtime }),
    signal: AbortSignal.timeout(60_000),
  }).then((response) => response.json()) as {
    codex_auth_methods?: Array<'chatgpt' | 'managed_api' | 'personal_api'>;
  };

  const cleanupStagedIdentities: Array<() => void> = [];
  if (runtime === 'codex' && settings.codex_auth_methods?.includes('chatgpt')) {
    const me = await session.api('/api/v1/auth/me')
      .then((response) => response.json()) as { tenant_id: string; user_id: string };
    const dockerContainer = process.env.FLOWORK_E2E_DOCKER_ACCOUNT_CONTAINER;
    const dockerRoot = dockerContainer
      ? dockerRuntimeVolumeRoot(dockerContainer)
      : null;
    if (dockerContainer && !dockerRoot) {
      throw new Error('opted-in Docker container does not expose AGENT_RUNTIME_ROOT');
    }
    const cleanup = dockerContainer && dockerRoot
      ? stageDockerCodexAccountIdentity(
          dockerContainer,
          me.tenant_id,
          me.user_id,
          dockerRoot,
        )
      : stageNativeCodexAccountIdentity(me.tenant_id, me.user_id);
    if (cleanup) cleanupStagedIdentities.push(cleanup);
  }

  try {
    const capabilities = await session.api('/api/v1/agent-runtime/capabilities', {
      signal: AbortSignal.timeout(120_000),
    }).then((response) => response.json()) as RuntimeCapabilities;
    if (capabilities.runtime_type !== runtime) {
      throw new Error(
        `requested ${runtime} but capability discovery returned ${capabilities.runtime_type}`,
      );
    }
    if (!capabilities.runtime_available) {
      throw new Error(`${runtime} Runtime is unavailable: ${capabilities.error_code ?? 'unknown'}`);
    }
    if (capabilities.authenticated !== true) {
      throw new Error(`${runtime} Runtime has no authenticated model profile`);
    }
    const model = preferredModel(capabilities);
    if (!model) throw new Error(`${runtime} Runtime exposes no selectable model`);
    const modelOptionLabel = capabilities.default_model_id === model.id
      ? null
      : `${model.label}${model.provider ? ` (${model.provider})` : ''}`;
    const authProfile = runtime === 'langchain'
      ? 'langchain_model'
      : model.provider === 'chatgpt'
        ? 'chatgpt_account'
        : model.id.startsWith('codex:managed:')
          ? 'managed_api'
          : 'personal_api';
    console.log(`[real-runtime] runtime=${runtime} auth_profile=${authProfile}`);
    return {
      runtime,
      authProfile,
      modelOptionLabel,
      modelId: model.id,
      modelSourceId: modelSourceId(model),
      cleanup: () => {
        for (const cleanup of cleanupStagedIdentities) {
          cleanup();
        }
      },
    };
  } catch (error) {
    for (const cleanup of cleanupStagedIdentities) {
      cleanup();
    }
    throw error;
  }
}

export async function selectRuntimeModel(
  page: Page,
  profile: RealRuntimeProfile,
) {
  if (!profile.modelOptionLabel) return;
  await page.locator('[data-role="chat-model-select"]').click();
  const source = page.locator(
    `[data-role="chat-model-source-option"][data-model-source="${profile.modelSourceId}"]`,
  );
  await expect(source).toBeVisible({ timeout: 30_000 });
  await source.click();
  const option = page.locator(
    `[data-role="chat-model-option"][data-model-id="${profile.modelId}"]`,
  );
  await expect(option).toBeVisible({ timeout: 30_000 });
  await option.click();
}

export async function loadCompleteChatHistory(page: Page, chatId: string) {
  const loadEarlier = page.locator('[data-role="agent-history-load-older"]');
  await expect(page.locator('[data-role="agent-history-loading"]'))
    .toHaveCount(0, { timeout: 30_000 });
  await expect.poll(async () => (
    await loadEarlier.isVisible().catch(() => false)
      || await page.locator('[data-chat-render-key]').count() > 0
  ), { timeout: 30_000 }).toBe(true);
  for (let pageIndex = 0; pageIndex < 50; pageIndex += 1) {
    if (!await loadEarlier.isVisible().catch(() => false)) return;
    const olderPage = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return response.request().method() === 'GET'
        && url.pathname.endsWith(`/chats/${chatId}/messages`)
        && (url.searchParams.has('before_turn_id') || !url.searchParams.has('tail'));
    }, { timeout: 30_000 });
    // The visible button is the deterministic accessibility contract. The
    // product also auto-loads at the scroll boundary, but synthetic scroll
    // events are intentionally not used as an E2E synchronization primitive.
    await loadEarlier.click();
    await olderPage;
    await expect(page.locator('[data-role="agent-history-loading-older"]'))
      .toHaveCount(0, { timeout: 30_000 });
  }
  throw new Error('Chat history still reports older messages after 50 pages');
}
