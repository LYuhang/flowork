# Contributing to Flowork

Flowork welcomes focused bug fixes, documentation improvements, tests,
security hardening, and features that fit the project's architecture. This
guide defines the contribution and review requirements; environment setup and
detailed commands are maintained separately in the
[Development Guide](docs/development.md).

Participation in the project is governed by the
[Code of Conduct](CODE_OF_CONDUCT.md). Security vulnerabilities follow a
private disclosure process and must not be reported through a public issue or
pull request.

## Before starting

- Search existing issues and pull requests to avoid duplicated work.
- Open an issue before a large feature, breaking API change, cross-package
  refactor, data migration, or security-boundary redesign. Early agreement on
  scope prevents incompatible implementations.
- Keep a contribution focused on one problem. Separate unrelated formatting,
  cleanup, and behavior changes.
- Never include credentials, private data, production exports, private model
  prompts, or internal platform URLs.
- Write public documentation, code comments, identifiers, and user-facing
  source strings in English. Product translations belong in the locale files.

Small bug fixes, tests, and documentation corrections do not require a design
proposal when their intent and compatibility are clear.

## Development workflow

1. Prepare the supported repository-local toolchain using the
   [Development Guide](docs/development.md#prepare-the-environment).
2. Create a focused branch from the current default branch.
3. Add or update tests alongside behavior changes.
4. Run the checks appropriate to every affected package.
5. Update documentation, migrations, and generated contracts in the same
   change when public behavior or persisted data changes.
6. Open a pull request that explains the problem, solution, risk, and
   verification.

The complete local stack, package commands, test prerequisites, migrations,
and code-generation procedures are documented in the
[Development Guide](docs/development.md). Browser journeys and specialized
acceptance gates are documented in the
[Browser End-to-End Testing Guide](web/e2e/README.md).

## Project boundaries

Preserve the dependency direction described in the
[Architecture Guide](docs/architecture.md):

| Area | Owns | Contribution expectations |
| --- | --- | --- |
| [`engine/`](engine/) | Framework-independent Workflow models, nodes, validation, and execution | Must not depend on FastAPI, the API package, Web application, or deployment-specific state |
| [`api/`](api/) | Public API, Agent Runtime orchestration, persistence, authorization, workers, and sandbox control | Changes at trust or persistence boundaries require explicit schemas, authorization checks, and focused tests |
| [`web/`](web/) | Browser application, product routes, client state, and previews | Keep server contracts in generated or typed API layers; preserve accessibility and deployment-path behavior |
| [`extension/`](extension/) | Chrome MV3 side panel and controlled-tab integration | Permission, origin, authentication, and message-contract changes require boundary tests and a security review |

Avoid introducing a second implementation of an existing cross-package
contract. Shared behavior should have one authoritative owner and typed or
generated consumers.

## Validate the change

Run checks in proportion to the affected behavior rather than invoking every
specialized acceptance suite for every pull request.

| Change | Minimum expected validation |
| --- | --- |
| Documentation only | Verify links, examples, Markdown formatting, and consistency with the referenced source |
| Engine | Focused Engine tests, full Engine suite, and Python correctness checks |
| API or worker | Focused API tests, full API suite, Python correctness checks, and dependency verification when imports change |
| Database model or storage behavior | API tests against the current migration head plus migration-specific upgrade and data-contract coverage |
| Web application | Focused Vitest coverage, lint, relevant UI audits, and a production build |
| Browser Extension | Extension unit tests and an extension build; run the real MV3 gate when browser permissions or runtime behavior changes |
| User-visible browser flow | Relevant Playwright specification paths; use the dedicated wrapper for visual, Diagram, or Extension acceptance |
| Authentication, authorization, secrets, uploads, sandboxing, or supply chain | Relevant unit/integration tests and the corresponding security gate or verification script |

The authoritative CI command set is defined in
[`ci.yml`](.github/workflows/ci.yml). Security checks are defined in
[`security.yml`](.github/workflows/security.yml). A local skip caused by a
missing kernel capability, external service, browser, or model credential must
be reported in the pull request; it must not be presented as successful
coverage.

## Keep related artifacts synchronized

### Dependencies and lockfiles

Dependency updates must be reproducible and reviewable:

- Update the owning `pyproject.toml` or `package.json` together with its input
  and lock files.
- Regenerate Python lock files using the exact command recorded at the top of
  each generated file. Do not edit resolved lock files by hand.
- Keep `requirements-runtime.txt`, `requirements-build.txt`,
  `requirements-sandbox.txt`, and their Engine counterparts consistent with
  their declared inputs.
- Update `pnpm-lock.yaml` in the same change as a JavaScript dependency and use
  frozen installs outside intentional dependency updates.
- Run `scripts/verify_dependency_locks.py` and `pip check` after changing the
  Python dependency graph.

Do not weaken a production pin, hash, or audit threshold solely to make a gate
pass. Explain the reason, compatibility impact, and security review for every
dependency exception.

### Database changes

Persisted schema changes require a new Alembic revision under
[`api/alembic/versions/`](api/alembic/versions/). Do not rewrite a migration
that may already have been applied; add a forward revision instead. Include
tests for the resulting current-head schema and for any data invariant the
migration establishes.

Backfills, encryption cutovers, and destructive transformations require an
explicit operational path and failure/retry behavior. Runtime application code
must not acquire migration privileges or silently create production tables.

### Public and generated contracts

Commit contract sources and generated consumers together:

- API schema changes include [`web/openapi.json`](web/openapi.json) and
  [`schema.d.ts`](web/src/lib/api/schema.d.ts).
- Interactive-view schema changes include
  [`interactive-view-schema.generated.json`](web/src/components/agent-sidebar/tool-render/interactive-view-schema.generated.json).
- Sandbox service changes include the source
  [`sandbox_service.proto`](api/src/vibecanvas_api/services/sandbox/proto/sandbox_service.proto)
  and both generated Python bindings:
  [`sandbox_service_pb2.py`](api/src/vibecanvas_api/services/sandbox/proto/sandbox_service_pb2.py)
  and
  [`sandbox_service_pb2_grpc.py`](api/src/vibecanvas_api/services/sandbox/proto/sandbox_service_pb2_grpc.py).

Generated files must not be edited to conceal a mismatch with their source.
When a public API changes, update all affected callers, tests, and documentation
in the same pull request or provide a documented compatibility path.

### Documentation and configuration

Update public documentation when a change affects installation, configuration,
permissions, user-visible behavior, API contracts, data lifecycle, or
deployment operations. Examples must use public names and safe placeholder
values.

Variables prefixed with `VITE_` are embedded in browser assets and must never
contain secrets. New environment variables require a documented default,
purpose, validation rule, and deployment scope.

## Code expectations

- Prefer explicit schemas and typed interfaces at trust boundaries.
- Enforce authentication, authorization, tenant isolation, validation, and
  sandbox policy in the owning server-side layer.
- Do not bypass a security control or convert a required failure into a silent
  fallback to make a test pass.
- Keep secrets server-side and redact sensitive values from logs, errors,
  fixtures, screenshots, and test artifacts.
- Preserve existing public behavior unless the change explicitly defines and
  documents a compatibility break.
- Add comments for non-obvious constraints and design decisions, not for
  self-evident syntax or temporary implementation history.
- Avoid unrelated refactoring, generated-file churn, or formatting changes in
  a focused contribution.

## Pull request requirements

A pull request should make the review decision clear without requiring the
reviewer to reconstruct the change from the diff. Include:

- the problem and intended outcome;
- the implementation scope and affected packages;
- user-visible, API, configuration, and data-model changes;
- compatibility, security, migration, and rollback considerations where
  applicable;
- the exact checks run and their results, including skipped or unavailable
  gates; and
- screenshots or recordings for material UI changes when they improve review.

Keep commits reviewable and ensure the final branch contains no debug output,
local environment files, credentials, generated evidence that is not intended
for source control, or unrelated changes.

## Commit style

Use a short imperative subject. Conventional Commit prefixes are encouraged:

```text
feat: add workflow export validation
fix(api): reject expired browser capabilities
docs: clarify native installation
test(engine): cover parallel branch failure
```

## Security issues

Do not open a public issue or pull request for a suspected vulnerability.
Follow the private reporting instructions in [`SECURITY.md`](SECURITY.md).

## License

By contributing, contributors agree that their work is licensed under the
[Apache License 2.0](LICENSE).
