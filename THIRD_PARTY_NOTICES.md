# Third-party notices

Flowork's original source code is licensed under the Apache License 2.0. The
project also uses third-party packages that remain governed by their own
licenses. Nothing in Flowork's `LICENSE` file replaces those terms.

The committed Python and pnpm lockfiles identify exact package versions. Release
images additionally carry SPDX SBOM attestations produced by the release
workflow. License texts and copyright notices distributed inside upstream
packages must be preserved when redistributing those packages or derived binary
artifacts.

Notable license choices in the current dependency graph include:

- `@playwright/mcp` and its pinned `playwright` / `playwright-core` runtime are
  distributed under Apache-2.0. The browser extension's CDP browser model and
  relay data plane are adapted from Microsoft Playwright commit
  `680e5ad5894a54bba9e4ed8a311fd2aee388137d`; the adapted source files retain
  the Microsoft copyright and Apache-2.0 notice. Flowork changes only the
  transport, browser-window scope, and extension integration; the official
  Playwright MCP continues to own locator, snapshot, waiting, and action
  semantics.
- The Docker stack uses Valkey, a Redis-protocol-compatible datastore released
  under the BSD 3-Clause license, instead of Redis releases under RSALv2/SSPLv1.
- `elkjs` is available under `EPL-2.0 OR GPL-3.0-or-later`; Flowork uses it
  under EPL-2.0.
- `@drawio/mcp` is the official draw.io MCP published by JGraph Ltd under
  Apache-2.0. Flowork runs the pinned package unchanged inside each activated
  Chat sandbox and adds only file, preview, and quality-feedback adapters.
  The package carries draw.io's vendored Libavoid routing assets and their
  upstream notices.
- `jszip` is available under `MIT OR GPL-3.0-or-later`; Flowork uses it under
  the MIT license.
- `axe-core`, `lightningcss`, `certifi`, `orjson`, and `tqdm` include MPL-2.0
  terms, alone or together with permissive alternatives.
- `psycopg`, `psycopg-binary`, and `psycopg-pool` are unmodified Python
  dependencies under LGPL-3.0-only.
- `pytest-postgresql` and `mirakuru` are development/test dependencies and are
  not part of the production runtime image.

Before publishing a binary distribution, review the generated SBOM and license
inventory for that exact release. Dependency updates can change this list even
when application code is unchanged.
