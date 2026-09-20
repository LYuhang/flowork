# Flowork Web

The Web package is Flowork's browser application. It presents the Chat,
Workflow, Task, Deployment, Browser, resource-management, and administration
experiences, and translates user interactions into typed API requests and
real-time execution updates.

The application is a React and TypeScript single-page application built with
Vite. React Router defines the product surfaces, TanStack Query manages server
state, Zustand owns interaction and streaming state, and React Flow powers the
Workflow canvas.

For an end-user overview, begin with the repository [README](../README.md). This
document is intended for contributors working on the Web package.

## Responsibilities and boundaries

The Web application owns:

- authenticated navigation and route-level product experiences;
- Workflow editing, Chat interaction, execution progress, and file previews;
- typed REST requests and browser-side session/CSRF handling;
- live Chat, Workflow, Task, background-job, and preview updates over SSE;
- client-side drafts, selection, layout, theme, and language preferences; and
- the embedded Chat surface displayed by the Browser Extension.

The Web package does not execute workflows or agents, enforce tenant
authorization, or serve as durable storage. Those responsibilities belong to
the [`api/`](../api/) and [`engine/`](../engine/) packages. Browser-tab control
is implemented by the separate [`extension/`](../extension/) package.

## Application structure

The route tree is defined in [`router.tsx`](src/app/router.tsx). Every page is
loaded on demand so the Workflow editor, Chat renderers, management tools, and
file-preview libraries do not enter unrelated route bundles.

| Surface | Primary routes | Purpose |
| --- | --- | --- |
| Authentication | `/login`, `/signup`, `/reset-password` | Public account access and recovery |
| Chat | `/chat` | Agent conversations, tool activity, generated files, diagrams, and previews |
| Workflow | `/workspace`, `/workflow/:wfId` | Workflow library, visual editing, validation, execution, and version review |
| Operations | `/tasks`, `/deployments` | Durable task progress and deployed workflow management |
| Resources | `/knowledge`, `/storage`, `/skills`, `/mcp-servers`, `/credentials` | Knowledge, files, integrations, skills, and model connections |
| Administration | `/management`, `/settings` | Platform administration and user/device preferences |
| Extension embed | `/embed/chat` | Reduced Chat shell framed by the Browser Extension side panel |

Public authentication pages and the extension embed use dedicated layouts.
All ordinary product routes are protected by the authenticated application
shell in [`AppLayout.tsx`](src/app/AppLayout.tsx).

## Data access and real-time state

The frontend uses separate paths for canonical server state and live execution
state:

1. [`client.ts`](src/lib/api/client.ts) creates an `openapi-fetch` client from
   the committed API schema.
2. Query and mutation modules under
   [`lib/api/`](src/lib/api/) integrate that client with TanStack Query.
3. Streaming modules under [`lib/api/sse/`](src/lib/api/sse/) process POST-based
   SSE connections, replay cursors, terminal events, and reconnect behavior.
4. Stores under [`stores/`](src/stores/) hold transient editor, selection, and
   active-stream state; durable records are reconciled back into the query
   cache and backend history.

The shared transport in
[`session-fetch.ts`](src/lib/api/session-fetch.ts) sends the HttpOnly session
cookie, attaches the double-submit CSRF header to unsafe requests, and handles
step-up authentication when required. The primary session credential is never
stored in JavaScript-accessible state.

API response types are generated in
[`schema.d.ts`](src/lib/api/schema.d.ts). SSE event types are maintained
separately because streaming wire formats are not represented completely by the
OpenAPI request/response schema.

## Development

Prepare dependencies and supporting services with the repository
[development guide](../docs/development.md), then start the full stack with Vite
hot module replacement:

```bash
WEB_MODE=dev ./launch.sh start
```

The application is available at <http://localhost:9001> by default. The launcher
overrides Vite's standalone port (`5173`) with the public development port and
proxies `/api`, the Browser WebSocket, and `/healthz` to the API on
`127.0.0.1:8000`.

After the backend services are already running, the Web process can also be
started directly from the repository root:

```bash
pnpm --dir web dev
```

Use the package manager version declared in [`package.json`](package.json) and
keep [`pnpm-lock.yaml`](pnpm-lock.yaml) synchronized with dependency changes.

## Validation

Run the checks relevant to the changed surface from the repository root:

```bash
pnpm --dir web lint
pnpm --dir web lint:visual
pnpm --dir web lint:routes
pnpm --dir web lint:locales
pnpm --dir web lint:retired-ui
pnpm --dir web test
pnpm --dir web test:i18n:components
pnpm --dir web build
pnpm --dir web lint:bundle
```

`build` performs TypeScript checking, creates the production bundle, and audits
deployment-path portability. Unit and component tests run through Vitest,
Testing Library, and MSW. `lint:locales` requires the English and Simplified
Chinese catalogs to contain the same non-empty key set with matching
interpolation parameters. It also checks the maintained route/child-tab/modal/
dynamic-state inventory and rejects new high-confidence hard-coded interface
copy. Intentional technical identifiers and acknowledged migration debt must be
recorded explicitly in [`i18n-copy-allowlist.json`](scripts/i18n-copy-allowlist.json)
with a reason; stale entries fail the audit.

Browser end-to-end tests are organized by product journey and do not all share
the same runtime prerequisites. Run the specifications affected by a change;
the complete `pnpm --dir web test:e2e` collection also includes scenarios that
require credentials, a Browser Extension build, or explicit opt-in variables.
See the [E2E testing guide](e2e/README.md) for supported commands and fixtures.

`test:i18n:components` runs the maintained component-spec matrix for localized
tabs, dialogs, empty/error states, resource surfaces, and locale-sensitive
schedule and time presentation. The spec list lives beside the browser surface
inventory so a review can distinguish static, component, and visible-browser
evidence without relying on an undocumented local command.

The opt-in multilingual browser gate verifies in-place language switching,
locale persistence through refresh and deep links, automatable nested tabs and
dialogs, and the application's date, number, and schedule formatters:

```bash
pnpm --dir web test:e2e:i18n
```

Its machine-readable coverage contract is
[`i18n-surface-inventory.json`](e2e/fixtures/i18n-surface-inventory.json).
Entries marked `visible-browser` still require human review in a visible Edge
session at both 1440 px and 560 px; the automated gate is a regression check,
not visual evidence of translation readability.

## Generated API contract

The committed [`openapi.json`](openapi.json) snapshot allows deterministic Web
builds without a live backend. When the public API contract changes, start the
local API and update both the snapshot and generated TypeScript definitions:

```bash
pnpm --dir web codegen:snapshot
```

Do not edit `openapi.json` or `schema.d.ts` by hand. Commit both generated files
with the backend schema change. Further details are available in
[Generated contracts](../docs/development.md#generated-contracts).

## Deployment model

One production build can run at the origin root or under a reverse-proxy path
prefix. [`base-path.ts`](src/lib/base-path.ts) resolves one deployment
coordinate for routing, REST, SSE, signed media, and extension-facing URLs. It
uses, in order, host-injected runtime configuration, fixed build-time values,
or the path inferred from the emitted application assets.

The relevant public build settings are:

| Setting | Purpose |
| --- | --- |
| `VITE_APP_BASE_PATH` | Fixed application mount path when runtime inference is not used |
| `VITE_API_BASE` | Same-origin path or absolute API base; an empty value uses the application origin and mount path |
| `WEB_ALLOWED_HOSTS` | Exact hosts or reviewed suffixes accepted by the Vite development and preview servers |

Variables prefixed with `VITE_` are embedded in browser assets and must never
contain secrets.

The production image serves the SPA through nginx, forwards `/api/` without
buffering SSE, preserves the Browser Extension WebSocket upgrade, applies
browser security headers, and publishes the deployment-specific extension ZIP.
Build it from the repository root:

```bash
docker build -f web/Dockerfile -t flowork-web:dev .
```

The image expects the API and extension-origin settings supplied by the full
service topology; it is not a complete deployment by itself. Use the
[installation guide](../docs/installation.md) for a local stack and
[`DEPLOY.md`](../DEPLOY.md) for production requirements.

## Source map

| Path | Responsibility |
| --- | --- |
| [`src/main.tsx`](src/main.tsx) | Browser bootstrap, shared transport initialization, and root providers |
| [`src/app/`](src/app/) | Router, application shell, providers, keyboard behavior, and error boundaries |
| [`src/pages/`](src/pages/) | Route-level product surfaces |
| [`src/components/`](src/components/) | Shared product components, interaction surfaces, and UI primitives |
| [`src/lib/api/`](src/lib/api/) | Typed API client, queries, mutations, and SSE transports |
| [`src/stores/`](src/stores/) | Client-side editor, session projection, and streaming state |
| [`src/lib/preview/`](src/lib/preview/) | Interactive artifact, diagram, and file-preview protocols |
| [`src/lib/i18n/`](src/lib/i18n/) | English and Chinese localization resources |
| [`e2e/`](e2e/) | Playwright product journeys and full-stack acceptance tests |
| [`scripts/`](scripts/) | Code generation, visual audits, bundle checks, and browser verification |
| [`vite.config.ts`](vite.config.ts) | Development proxy, portable assets, CSP generation, and build configuration |
| [`nginx.conf`](nginx.conf) | Production SPA, API/SSE proxy, WebSocket, framing, and security-header policy |

## License

Apache-2.0. See [`LICENSE`](../LICENSE).
