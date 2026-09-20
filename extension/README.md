# Flowork Browser Extension

The Browser Extension brings Flowork Chat into Chrome's side panel and allows
an Agent to inspect or operate the browser tab connected by the user. It reuses
the main Web application's embedded Chat interface, while the extension owns
the browser-specific transport, tab session, and Chrome DevTools Protocol
(CDP) execution boundary.

The extension uses Chrome Manifest V3 and requires Chrome 116 or later. For a
product-level overview, begin with the repository [README](../README.md).

## How it works

Browser Chat is implemented by several cooperating components:

1. The [side-panel shell](src/sidepanel.ts) loads the Web application's
   `/embed/chat` route in an allowlisted iframe.
2. The Web application reuses the authenticated Chat UI and requests a
   short-lived capability for the browser connection.
3. An [offscreen document](src/offscreen.ts) owns the WebSocket so the transport
   can survive normal Manifest V3 service-worker suspension.
4. The [service worker](src/service-worker.ts) validates command envelopes,
   manages the controlled tab session, and executes fixed CDP operations.
5. The [Dynamic Island content script](src/island/content.ts) displays browser
   control status, narration, and highlights on the page.
6. Command results return through the WebSocket as structured observations and
   become part of the Browser Chat turn.

The main Web application and Browser surface keep separate Chat histories. A
browser-control session is bound to one Browser Chat and one side-panel window;
another window cannot silently take ownership of that active session.

## Installation

Each Flowork deployment packages an extension configured for its own public
application URL. Download that build from the main application:

1. open **Settings → Extensions**;
2. select **Download extension**;
3. extract the ZIP to a permanent folder;
4. open `chrome://extensions` and enable **Developer mode**;
5. select **Load unpacked** and choose the extracted folder; and
6. pin Flowork in the toolbar, then open its side panel.

Chrome cannot load the ZIP directly. To update the extension, download the new
package, replace the extracted files, and select **Reload** on
`chrome://extensions`.

The application origin is compiled into the extension. If the Flowork URL,
protocol, port, or reverse-proxy prefix changes, rebuild the deployment and
download the matching extension again.

## Browser command scope

Use `/browser` in the extension side panel to activate browser-control context:

```text
/browser compare the plans on this page and summarize the differences
```

The command attaches the Browser Chat to the pinned official Playwright MCP
running inside that Chat's sandbox. Its reviewed tools cover navigation, page
snapshots, screenshots, tab management, form input, dialogs, file upload, and
other explicit browser actions. The active command context remains attached to
the Browser Chat, so follow-up requests can continue the same task without
repeating all prior context.

`/browser` is intentionally available only on the extension's Browser surface.
Entering it in the main application Chat returns a notice directing the user to
the side panel; it does not hand the conversation to the extension or begin
browser control in the background.

Chrome displays its native debugging indicator while a tab is attached. Ending
the browser session releases the controlled tabs and clears the corresponding
session projection.

## Authentication and transport

When the main application is already signed in, it sends the extension a
short-lived, single-use exchange code. The side-panel iframe redeems that code
for its own Web Session; the main application's HttpOnly session cookie is
never copied into extension storage. A user can also sign in directly from the
side panel when no reusable application session is available.

The authenticated Web session mints a separate, short-lived browser capability
for `/api/v1/browser/ws`. The extension sends that capability through the
WebSocket subprotocol rather than a URL query parameter. Browser identity,
tenant scope, origin, and token claims are validated by the API before the
connection is accepted.

Session identity and controlled-tab references use `chrome.storage.session` so
they can be restored after a service-worker restart without becoming permanent
device credentials. The offscreen document maintains the live WebSocket and
reconnects only through the scoped authentication flow.

## Permissions and security boundary

The source [`manifest.json`](manifest.json) declares the Chrome capabilities
required by the runtime:

| Capability | Why it is required |
| --- | --- |
| `sidePanel` | Hosts the embedded Browser Chat |
| `debugger` | Executes the fixed CDP operations used to read and control an attached tab |
| `tabs` | Identifies, observes, switches, and closes tabs within the controlled session |
| `offscreen` | Keeps the authenticated WebSocket alive outside the visible panel |
| `storage` | Retains session-scoped connection and controlled-tab state across worker restarts |
| `scripting` | Recovers the application bridge when the extension is installed or reloaded after an allowlisted app tab is already open |
| Content script on `<all_urls>` | Displays the isolated Dynamic Island feedback surface on whichever page the user controls |

The content script runs only in the top frame, in Chrome's isolated world, and
the extension exposes no web-accessible resources. It does not provide the
backend with arbitrary JavaScript execution.

Browser semantics come from the pinned official Playwright MCP, not from a
second Flowork command vocabulary. The extension implements only the fixed CDP
transport allow-list in
[`relay-executor.ts`](src/playwright/relay-executor.ts) and the upstream-derived
browser target model in
[`browser-model.ts`](src/playwright/browser-model.ts). It scopes every request
to tabs in the user-approved side-panel window and rejects unknown relay
commands. Unrestricted Playwright page evaluation and remote-code tools are not
exposed to the Agent. Any new relay command or Chrome permission requires
matching boundary tests and a manual privacy and security review before
release.

Production builds generate `externally_connectable` and `host_permissions`
from exact configured Flowork Web bases. The service worker performs an
additional runtime origin and path-prefix check before accepting messages from
the Web application. The Browser Extension should therefore be rebuilt for
each deployment boundary rather than distributed with a wildcard application
origin.

## Development and testing

Prepare the repository environment using the
[development guide](../docs/development.md). Build the extension from the
repository root with the exact Web origin used to open Flowork:

```bash
VITE_WEB_BASE=http://localhost:9001 \
VITE_EXTENSION_ALLOWED_ORIGINS=http://localhost:9001 \
pnpm --dir extension build
```

Load the generated `extension/dist/` directory as an unpacked extension. For
watched development builds, run:

```bash
pnpm --dir extension dev
```

Chrome does not automatically reload an unpacked extension when `dist/`
changes; select **Reload** on `chrome://extensions` after each rebuild.

Run the package tests and a production build with:

```bash
pnpm --dir extension test
pnpm --dir extension build
```

The unit suite covers command parity, origin validation, WebSocket
authentication, service-worker recovery, CDP dispatch, tab ownership, content
script boundaries, and the no-remote-code invariant. Browser-level extension
journeys are documented in the Web [E2E testing guide](../web/e2e/README.md).

## Deployment configuration

[`vite.config.ts`](vite.config.ts) generates the final Manifest V3 allowlists
and copies the static extension assets into `dist/`.

| Setting | Purpose |
| --- | --- |
| `VITE_WEB_BASE` | Canonical Web application base loaded by the side-panel iframe |
| `VITE_EXTENSION_ALLOWED_ORIGINS` | Comma-separated exact Web bases permitted to communicate with the extension; must include `VITE_WEB_BASE` |
| `VITE_WS_BASE` | Optional WebSocket base when it differs from the Web application base |

These values are public deployment coordinates, not secrets. HTTP and HTTPS
origins, IP literals, explicit ports, and reverse-proxy path prefixes are
supported. Credentials, query strings, and fragments are rejected. Use exact
origins: the runtime sender check does not trust wildcard origins.

The production Web image builds and packages the matching extension as
`/downloads/flowork-extension.zip`. The extension ID must remain aligned
with the API identity and the Web server's `/embed/*` framing policy; the full
deployment wiring is defined in [`docker-compose.yml`](../docker-compose.yml)
and [`web/Dockerfile`](../web/Dockerfile).

## Source map

| Path | Responsibility |
| --- | --- |
| [`src/sidepanel.ts`](src/sidepanel.ts) | Side-panel iframe host and Web/extension message bridge |
| [`src/service-worker.ts`](src/service-worker.ts) | Browser session ownership, Playwright relay lifecycle, and extension message routing |
| [`src/offscreen.ts`](src/offscreen.ts) | Persistent authenticated WebSocket transport |
| [`src/playwright/`](src/playwright/) | Window-scoped CDP relay, upstream-derived target model, and boundary tests |
| [`src/island/content.ts`](src/island/content.ts) | Isolated in-page control status and feedback surface |
| [`src/shared/config.ts`](src/shared/config.ts) | Compiled Web/WS coordinates and runtime sender validation |
| [`src/shared/envelope.ts`](src/shared/envelope.ts) | Lifecycle and authenticated Playwright relay envelope |
| [`src/shared/ws-client.ts`](src/shared/ws-client.ts) | WebSocket lifecycle and reconnect behavior |
| [`manifest.json`](manifest.json) | Source Manifest V3 permissions and entry points |
| [`vite.config.ts`](vite.config.ts) | Build entries, generated allowlists, icons, and final manifest |

## License

Apache-2.0. See [`LICENSE`](../LICENSE).
