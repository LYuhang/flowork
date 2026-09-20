# Browser End-to-End Testing

The Playwright suite validates Flowork through real browser interactions and
public API contracts. It covers focused product journeys, Workflow authoring,
accessibility, Agent Runtime integration, and the Browser Extension. Unit and
component tests remain in Vitest and are documented in the repository
[development guide](../../docs/development.md).

## Test architecture

[`playwright.config.ts`](../playwright.config.ts) defines the shared browser
configuration. Tests run with one worker because several full-stack scenarios
coordinate durable jobs, Runtime processes, or shared development services. CI
retries a failed test twice; local runs do not retry automatically. Screenshots
are retained on failure and traces are captured on the first retry.

Unless `VIBECANVAS_SKIP_WEB_SERVER=1` is set, Playwright starts or, during a
local run, reuses:

- the FastAPI process on `127.0.0.1:8000`; and
- the Vite development server on `127.0.0.1:5173`.

These managed processes do not provide PostgreSQL, Redis, OpenFGA, Celery, or
the sandbox service. Focused route tests can use this mode when those
dependencies are already available through the supplied environment. Tests
that exercise execution, Tasks, Deployments, Agent Runtimes, or sandbox state
should run against the complete native stack.

### Authentication and fixture isolation

Browser tests use the same Secure Cookie and CSRF flow as the application.
Fixtures register a fresh user, retain the HttpOnly Session only in the
Playwright process, and seed cookies into the browser context. No development
token is required, and Session credentials are not copied into Web Storage or
an `Authorization` header.

The shared fixture implementations are:

- [`fixtures.ts`](fixtures.ts) for focused product journeys;
- [`cookie-session.ts`](cookie-session.ts) for long-running Runtime scenarios;
  and
- [`acceptance/fixtures.ts`](acceptance/fixtures.ts) for Workflow acceptance.

Each specification owns its data setup and cleanup. A test must remain
independently runnable and must not rely on resources created by another file.

## Test suites

The numbered filenames indicate areas of coverage, not one mandatory execution
sequence.

| Suite | Paths | Scope | Typical requirements |
| --- | --- | --- | --- |
| Product journeys | `01-*` through `09-*`, plus `13-*`, `25-*`, `26-*`, `35-*`, and `36-*` | Authentication, Chat shell, Workflow CRUD and execution, Settings, MCP, Storage, navigation, and accessibility | API, Web, and PostgreSQL; Workflow execution requires the full stack, while live Chat also requires a model |
| Workflow acceptance | `acceptance/*.spec.ts` | Canvas authoring, inspector editing, persistence, validation, execution, keyboard behavior, JSON import/export, batch runs, and visual evidence | Complete native stack; most graphs are deterministic and do not use a model |
| Full-stack integration | `17-*` through `24-*`, `27-*` through `34-*`, `38-*`, `39-*`, and `42-*` | Interactive artifacts, background work, Knowledge, MCP and Skills, slash commands, Runtime reuse and resume, Codex resilience, and real Workflow execution | Complete native stack plus the model or Runtime credentials required by the selected spec |
| Visual matrix | [`15-route-visual-matrix.spec.ts`](15-route-visual-matrix.spec.ts) | Production routes across viewports, zoom levels, locales, themes, keyboard, and forced-colors states | Explicit opt-in flag and a complete application stack |
| Browser Extension | [`16-extension-runtime.spec.ts`](16-extension-runtime.spec.ts), [`40-extension-sidepanel-narrow.spec.ts`](40-extension-sidepanel-narrow.spec.ts) | Real unpacked MV3 service worker, controlled tabs, side panel, authentication handoff, and narrow layouts | Built extension, headed Chromium, and Xvfb or another display server |
| Diagram acceptance | Manual visible-browser matrix | Real Codex `/diagram` creation, refinement, Preview and export with pixel-level review | Complete native stack, connected Codex account, visible Windows browser, and evidence under `output/playwright/` |

The specification source is authoritative for credentials, feature flags, and
external services required by that scenario. Real-runtime tests deliberately
fail when their claimed Runtime or model is unavailable; they are not general
UI smoke tests.

`42-codex-resilience-conformance.spec.ts` is the focused Codex release gate for
streaming terminals, usage replay, Stop/Retry, persisted history, and recovery
after a sanitized tool failure. Native Codex compaction is deliberately not
counted by default because the platform does not own its threshold. Run that
pressure case only with a model/profile whose threshold is known:

The test provisions an isolated user and stages the host Codex identity into
`AGENT_RUNTIME_ROOT`. The Playwright process, API, and sandbox service must see
that same Runtime root. For the Docker Compose named volume, opt in to the
shared staging helper explicitly:

```bash
FLOWORK_E2E_DOCKER_ACCOUNT_CONTAINER=flowork-sandboxd-1 \
pnpm --dir web exec playwright test e2e/42-codex-resilience-conformance.spec.ts
```

A browser account already connected for another user does not authenticate the
isolated E2E user.

```bash
VIBECANVAS_CODEX_COMPACTION_E2E=1 \
VIBECANVAS_CODEX_COMPACTION_TURNS=12 \
VIBECANVAS_CODEX_COMPACTION_CHARS_PER_TURN=24000 \
pnpm --dir web exec playwright test e2e/42-codex-resilience-conformance.spec.ts
```

## Prepare the environment

From the repository root, prepare the supported native development environment
and install the Playwright Chromium binary:

```bash
./scripts/bootstrap_native_linux.sh --prepare-only
pnpm --dir web exec playwright install chromium
```

Start the complete local stack:

```bash
./launch.sh start
```

For tests that reuse this stack, export the browser coordinates once in the
same shell:

```bash
export VIBECANVAS_SKIP_WEB_SERVER=1
export VIBECANVAS_WEB_PORT=9001
export VIBECANVAS_PYTHON="$PWD/.venv/bin/python"
```

The Web application then resolves to `http://127.0.0.1:9001` and direct API
fixture calls resolve to `http://127.0.0.1:8000`.

## Run focused tests

Run the specifications affected by a change instead of collecting every
specialized gate:

```bash
pnpm --dir web exec playwright test e2e/01-login-and-workspace.spec.ts
pnpm --dir web exec playwright test \
  e2e/02-create-workflow.spec.ts \
  e2e/05-delete-with-confirmation.spec.ts
```

Select a test by title or open a headed browser while debugging:

```bash
pnpm --dir web exec playwright test e2e/07-auth.spec.ts --grep "logout"
pnpm --dir web exec playwright test e2e/07-auth.spec.ts --headed
pnpm --dir web exec playwright test e2e/07-auth.spec.ts --debug
```

Run the deterministic Workflow acceptance directory with:

```bash
pnpm --dir web exec playwright test e2e/acceptance
```

`pnpm --dir web test:e2e` is the umbrella package command and collects all
specifications. It is not a zero-configuration smoke command: several files
require real Runtime credentials or explicit opt-in state. Use selected paths
for normal development and the dedicated commands below for specialized gates.

## Run specialized gates

### Multilingual surface matrix

The multilingual gate exercises language changes without route replacement,
locale persistence, nested tabs and creation dialogs in English and Simplified
Chinese, and locale-aware date, number, and schedule formatting. Its companion
inventory records the additional fixture-backed states that remain part of the
visible-browser acceptance pass.

```bash
pnpm --dir web test:e2e:i18n
```

Keep [`fixtures/i18n-surface-inventory.json`](fixtures/i18n-surface-inventory.json)
in sync whenever a route gains or removes a child tab, modal, or dynamic state.
The static `pnpm --dir web lint:locales` gate enforces the inventory shape and
prevents unreviewed hard-coded interface copy from entering the baseline.
Run the inventory's focused component matrix separately:

```bash
pnpm --dir web test:i18n:components
```

The inventory also guards the source-level list of product tab and dialog
implementations. Adding a new `TabsTrigger`, custom `role="tab"`, or direct
`DialogContent` without recording its localized acceptance surface fails the
static gate.

### Visual route matrix

The visual matrix is skipped unless explicitly enabled. Run the complete matrix
or select one width and zoom combination:

```bash
VIBECANVAS_VISUAL_MATRIX=1 \
pnpm --dir web exec playwright test e2e/15-route-visual-matrix.spec.ts

VIBECANVAS_VISUAL_MATRIX=1 \
VIBECANVAS_VISUAL_CASE=1440-100 \
pnpm --dir web exec playwright test e2e/15-route-visual-matrix.spec.ts
```

### Diagram acceptance

Diagram quality acceptance is intentionally not a mocked Playwright
specification. Use a visible Windows browser, a real Codex-backed Chat and
natural-language `/diagram` requests; inspect the rendered pixels and continue
the same conversation until each result is ready to use. The current matrix
and evidence requirements are documented in
`docs/internal/diagram-drawio-mcp-implementation-and-acceptance-2026-08-16.md`.
Store screenshots and verdicts below `output/playwright/`.

### OpenRouter through Codex

The OpenRouter gate is intentionally opt-in because it uses an already
authorized account and makes real model requests. It never registers a test
user or copies the provider key into Playwright. Supply a mode-`0600` JSON file
containing an existing Flowork Secure Cookie Session and its CSRF value, then
run the gate against the complete local stack:

```bash
FLOWORK_E2E_EXISTING_SESSION_FILE=/absolute/path/existing-session.json \
VIBECANVAS_SKIP_WEB_SERVER=1 \
VIBECANVAS_WEB_PORT=9001 \
VIBECANVAS_API_BASE=http://localhost:8000 \
VIBECANVAS_E2E_HOST=localhost \
pnpm --dir web exec playwright test \
  e2e/44-openrouter-codex-real.spec.ts --workers=1
```

The scenario selects Codex, OpenRouter, `stealth/ox-alpha`, and Maximum
reasoning; completes a normal response and a local filesystem tool loop; then
reloads the Chat and verifies the model, reasoning effort, history, and tool
result. Revoke the temporary Flowork Session immediately after the acceptance
run. The OpenRouter credential remains encrypted on the Host and must never be
placed in this file, browser storage, screenshots, or test output.

### Browser Extension runtime

Build the unpacked extension before either extension gate. For the side-panel
gate, bind the build to the same exact origin used by the test:

```bash
VITE_WEB_BASE=http://localhost:9001 \
VITE_EXTENSION_ALLOWED_ORIGINS=http://localhost:9001 \
pnpm --dir extension build
```

The MV3 runtime gate owns a local fixture server and validates extension
internals without starting unrelated API and Web processes:

```bash
VIBECANVAS_EXTENSION_E2E=1 \
VIBECANVAS_SKIP_WEB_SERVER=1 \
xvfb-run -a pnpm --dir web exec playwright test \
  e2e/16-extension-runtime.spec.ts --workers=1
```

The side-panel acceptance gate connects the built extension to a running
Flowork application. The extension must be built for the same exact origin,
and the account and Agent Runtime expected by the specification must be
available:

```bash
VIBECANVAS_EXTENSION_SIDEPANEL_E2E=1 \
VIBECANVAS_APP_ORIGIN=http://localhost:9001 \
xvfb-run -a pnpm --dir web exec playwright test \
  e2e/40-extension-sidepanel-narrow.spec.ts --workers=1
```

## Environment reference

| Variable | Purpose |
| --- | --- |
| `VIBECANVAS_SKIP_WEB_SERVER=1` | Reuse externally managed API and Web processes instead of starting them from Playwright |
| `VIBECANVAS_E2E_HOST` | Host used by the Playwright-managed API and Web servers; defaults to `127.0.0.1` |
| `VIBECANVAS_API_PORT` | API port used by Playwright and shared fixtures; defaults to `8000` |
| `VIBECANVAS_WEB_PORT` | Web port used by Playwright and shared fixtures; defaults to `5173` |
| `VIBECANVAS_API_BASE` | Override the complete direct-API base URL used by fixtures |
| `VIBECANVAS_WEB_ORIGIN` / `VIBECANVAS_E2E_ORIGIN` | Override the browser origin used for cookies, CSRF validation, and direct API calls |
| `VIBECANVAS_PYTHON` | Python executable used by Playwright-managed API processes and Python-backed fixtures |
| `VIBECANVAS_E2E_USE_TEST_USER=1` | Use the explicitly enabled local test account instead of registering a disposable user |
| `AGENT_API_KEY` | Enables the legacy live Chat streaming specification in `03-chat-stream.spec.ts` |

Specialized flags such as `VIBECANVAS_VISUAL_MATRIX`,
`VIBECANVAS_EXTENSION_E2E`, and
`VIBECANVAS_EXTENSION_SIDEPANEL_E2E` belong only to their corresponding gates.

## Authoring guidelines

- Prefer stable `data-action`, `data-role`, accessible role, and accessible name
  selectors over presentation classes or translated text.
- Use API fixtures for setup and cleanup; drive the browser through the behavior
  the test is intended to validate.
- Register a distinct user or create uniquely named resources when state is
  durable. Clean up mutations in `afterAll` or `try/finally` where practical.
- Wait for observable UI, network, or durable-state transitions. Fixed delays
  should be reserved for boundaries that do not expose a deterministic signal.
- Keep Secure Cookie material inside the fixture layer. Do not introduce bearer
  development tokens or copy Session values into browser storage.
- Make optional external dependencies explicit with a named opt-in flag and a
  clear skip reason. Required acceptance behavior should fail with a diagnostic
  message instead of silently passing through a skip.
- Store diagnostic output through Playwright's `testInfo.outputPath` or
  attachment APIs unless a specification intentionally owns a reviewed evidence
  directory.

## Troubleshooting

Start with the service health and logs when a full-stack test cannot reach a
terminal state:

```bash
./launch.sh status
./launch.sh logs
```

If Playwright reports a port collision, either stop the existing process or set
`VIBECANVAS_SKIP_WEB_SERVER=1` and point the test at that process. If a test
opens the correct page but fixture API calls fail CSRF validation, verify that
the Web port and origin variables describe the same origin used by the browser.

Failed-test artifacts are written under `web/test-results/`. Workflow and
extension walkthrough specifications may additionally write reviewed evidence
under `web/screenshots/`; their source comments define the exact output.
