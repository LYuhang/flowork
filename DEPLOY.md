# Production Deployment

This guide defines the production release and deployment boundary for Flowork.
For local evaluation or development, use the
[installation guide](docs/installation.md).

> [!WARNING]
> The default Compose stack is a local environment. Do not expose it directly
> to the Internet or treat locally built images as a production release.

## Current deployment status

Flowork is alpha software. The repository implements a verifiable release
pipeline, a production evidence gate, a digest-only release overlay in
[`docker-compose.release.yml`](docker-compose.release.yml), and a fail-closed
runtime security profile. It does not yet provide a turnkey production
infrastructure stack.

The current release overlay also inherits fixed internal PostgreSQL and Valkey
connection strings from the local Compose file. Those values do not satisfy the
production profile, which requires verified database TLS and authenticated
`rediss://` connections. Because Compose service-level `environment` values
take precedence over an env file, external production data services cannot
currently be supplied through `VIBECANVAS_ENV_FILE` alone.

As a result, [`production_release.sh`](scripts/deploy/production_release.sh)
can verify release artifacts and evidence, but the checked-in Compose files are
not yet an end-to-end production deployment. Do not bypass the runtime security
checks or weaken them to make the local topology start. A supported production
deployment requires a reviewed release overlay that explicitly supplies the
external database and Valkey URLs through the verified release entry point.

| Capability | Current status |
| --- | --- |
| Build, scan, and publish release images | Implemented in GitHub Actions |
| Generate provenance and SPDX SBOM attestations | Implemented |
| Verify independently reviewed production evidence | Implemented |
| Require digest-pinned API and Web images | Implemented |
| Validate the production security profile at process startup | Implemented |
| Configure compliant external PostgreSQL and Valkey through the release overlay | Not yet implemented |
| Automatically provision TLS, KMS, S3, audit, backup, and monitoring services | Operator responsibility |
| Distribute sandbox ownership across multiple `sandboxd` instances | Not implemented |

## Production architecture

A production environment must provide the following infrastructure outside the
application containers:

| Area | Requirement |
| --- | --- |
| **Public edge** | HTTPS termination, explicit host and origin allowlists, trusted proxy configuration, and firewall policy |
| **PostgreSQL** | TLS certificate verification, non-default application and maintenance roles, automated backups, and tested restoration |
| **Valkey** | Authenticated TLS using `rediss://`, private networking, and appropriate persistence for broker workloads |
| **OpenFGA** | Authenticated HTTPS endpoint with a pinned store, authorization model, and reviewed model digest |
| **Object storage** | S3-compatible storage using SSE-KMS, lifecycle policy, backup coverage, and workload identity |
| **KMS and secrets** | Managed KMS, workload identity, secret rotation, and no static cloud access keys in containers |
| **File scanning** | A capacity-tested ClamAV daemon available through a local Unix socket |
| **Audit and observability** | Immutable HTTPS audit export, private metrics and tracing, alerting, and defined retention |
| **Email** | SMTP credentials retrieved from a managed secret reference rather than plaintext environment values |
| **Sandbox host** | Rootful gVisor support, controlled egress, sufficient storage, and successful checkpoint/restore validation |

The application validates these assumptions before a production API or worker
starts. The executable requirements are defined in the
[production security profile](api/src/vibecanvas_api/security_profile.py).

### Sandbox ownership

In the single-node topology, one `sandboxd` instance manages all gVisor
sessions through a private Unix socket. It is the only application service that
requires elevated container privileges; the Web, API, workers, databases, and
migration jobs remain unprivileged.

All requests for the same Chat or Workflow scope must reach the same
`sandboxd`. Round-robin routing across independent daemons is not supported
because distributed session ownership and lease transfer are not implemented.
A future multi-node deployment must use deterministic sharding or sticky
routing to one daemon per scope.

For the underlying lifecycle and network model, see
[Sandbox lifecycle](docs/architecture.md#sandbox-lifecycle) and
[Network boundaries](docs/architecture.md#network-boundaries).

## Release artifacts

Pushing a semantic version tag matching `v*` triggers
[`release-images.yml`](.github/workflows/release-images.yml). The workflow
requires the tagged commit to belong to the default branch and runs in the
protected `production-release` GitHub environment.

For each release, the workflow:

1. builds the API, Web, and Engine images from the tagged commit;
2. generates Syft SBOMs;
3. rejects High or Critical vulnerabilities according to the reviewed policy;
4. publishes immutable commit-tagged images to GitHub Container Registry;
5. records the resulting image digests;
6. creates GitHub OIDC build-provenance and SPDX SBOM attestations; and
7. verifies the attestations before completing the job.

The workflow also builds, tests, packages, and attests the Chrome extension.
After every image and extension gate passes, it attaches a public release
bundle to the matching GitHub Release. The bundle contains the installable
extension ZIP, an immutable image manifest, consolidated security reports, and
one SHA-256 checksum file. If the tag does not have a Release yet, the workflow
creates a draft so that the notes can be reviewed before publication.

The production Compose deployment uses the API and Web image digests recorded
in the manifest. The separate Engine image is a release artifact but is not a
service in the current Compose topology. GitHub Actions artifacts remain
available for retained build evidence; user-facing downloads come from the
GitHub Release and are not tied to the Actions retention period.

Configure required reviewers for the `production-release` environment. The
person approving a release should not be the sole author of its production
evidence.

## Production evidence

Production promotion requires a JSON manifest that binds operational evidence
to one repository, commit, semantic version tag, and production environment.
Start from
[`production-evidence-manifest.example.json`](docs/production-evidence-manifest.example.json),
but keep the completed manifest and evidence records under the access and
retention policy of the deployment organization.

Every required gate must:

- have status `passed`;
- identify an accountable owner;
- identify an independent reviewer different from the owner;
- include a timezone-aware verification timestamp; and
- reference at least one immutable evidence artifact.

Evidence URIs must use `https`, `s3`, `gs`, or `oci`. Each artifact must include
a SHA-256 digest or another immutable identifier, and it must not contain
credentials, query parameters, or fragments. Evidence older than the configured
limit is rejected; the maximum accepted limit is 180 days.

The required gates cover TLS and proxies, KMS workload identity, OpenFGA,
immutable audit export, backup restoration, key rotation, historical credential
rotation, enterprise identity, two-person privileged access, ClamAV capacity,
release attestations, extension release, and authorization rollback.

The schema and validation rules are implemented in
[`verify_production_evidence.py`](scripts/security/verify_production_evidence.py).
The separately dispatched
[`production-evidence.yml`](.github/workflows/production-evidence.yml) workflow
verifies that the manifest is tracked on the default branch and exactly matches
the release being promoted.

Do not include passwords, tokens, API keys, private keys, or raw customer data
in an evidence manifest.

## Prepare a release

### Deployment host prerequisites

The deployment host needs:

- Docker Engine with Compose v2;
- Python 3;
- GitHub CLI (`gh`) for attestation verification;
- registry access to pull the selected image digests; and
- access to the reviewed evidence manifest and production environment file.

The GitHub CLI must be able to verify attestations for the release repository.
The Docker daemon must be able to pull both images by digest.

### Environment file

Store the production environment file outside the repository with mode `0600`.
Do not derive it from the local `.env` generated by `local_server.sh`. The
production file must satisfy the complete security profile, including:

- an exact HTTPS public URL, CORS list, WebAuthn origin, and trusted proxy CIDRs;
- TLS-verified, non-default PostgreSQL application and maintenance DSNs;
- an authenticated `rediss://` URL;
- stable signing, browser-token, and content-lookup keys;
- an authenticated and pinned production OpenFGA service;
- AWS KMS and S3 workload identities with no static cloud credentials;
- S3 server-side encryption using the configured KMS key;
- managed SMTP credentials;
- an immutable audit export destination;
- verified encrypted backups and the purge worker;
- distributed authentication rate limiting and WebAuthn step-up;
- a local ClamAV Unix socket;
- an independent Sandbox Service using a private Unix socket or mTLS gRPC; and
- production debug and shared test-user features disabled.

Runtime API and worker processes must not receive schema-owner credentials.
Database migrations run as a separate one-shot workload with a dedicated
migration identity.

The complete configuration vocabulary and comments are available in
[`.env.example`](.env.example). The runtime security validator remains the
source of truth when documentation and code differ.

### Release metadata

Set the exact release identity and digest-pinned images:

```bash
export VIBECANVAS_API_IMAGE='ghcr.io/owner/repository-api@sha256:...'
export VIBECANVAS_WEB_IMAGE='ghcr.io/owner/repository-web@sha256:...'
export RELEASE_REPOSITORY='owner/repository'
export RELEASE_SHA='0123456789abcdef0123456789abcdef01234567'
export RELEASE_REF='refs/tags/v1.0.0'
export PRODUCTION_EVIDENCE_MANIFEST='/secure/flowork/production-evidence.json'
export VIBECANVAS_ENV_FILE='/secure/flowork/production.env'
```

`RELEASE_SHA` must be the full lowercase 40-character commit SHA.
`RELEASE_REF` must be an exact semantic version tag under `refs/tags/`. Image
references must use `@sha256:` digests; mutable tags are rejected.

Keep production secrets inside the protected environment file or a secret
manager. Do not place secret values directly in these shell exports.

## Verify the release

Run the release gate without changing the running deployment:

```bash
./scripts/deploy/production_release.sh verify
```

The command:

1. validates all required release metadata;
2. verifies provenance and SPDX attestations for the API and Web images;
3. verifies that production evidence matches the repository, commit, and tag;
   and
4. validates the merged Compose configuration.

A successful result prints `production_release_gate=pass`. This command does
not start the application and therefore does not replace the API and worker
startup security validation.

The script currently supports only `verify` and `up`. There is no `config`
subcommand.

## Deploy

After the release gate passes and the release overlay limitation described
above has been resolved in a reviewed change, the intended deployment command
is:

```bash
./scripts/deploy/production_release.sh up
```

The script repeats every verification step, then runs Compose with
`--no-build --pull always --wait`. Production images are pulled by digest and
are never built on the deployment host.

Internally, the verified entry point combines `docker-compose.yml` with
`docker-compose.release.yml` and executes the equivalent of:

```bash
docker compose up -d --no-build --pull always --wait
```

Do not replace this entry point with `docker compose up --build`, and do not
manually remove a failing production security check.

## Post-deployment verification

After deployment:

1. confirm that every service is healthy and running the reviewed digest;
2. verify the public health endpoint through the HTTPS reverse proxy;
3. test registration or login with a dedicated non-production identity;
4. verify both an allowed action and an authorization denial;
5. execute a representative Chat turn and Workflow inside gVisor;
6. confirm that audit events reach the immutable sink;
7. confirm that metrics, traces, and alerts are active;
8. verify that the current extension package matches the release; and
9. retain the manifest, image digests, source SHA, and deployment logs.

Run these checks with test identities and synthetic data. Do not use real
customer content in deployment smoke tests.

## Upgrade

Treat every upgrade as a new release promotion:

1. back up the database, object storage, runtime state, and configuration;
2. review the new release notes and database migrations;
3. build and attest a new semantic version tag;
4. collect fresh evidence for controls affected by the change;
5. verify the new image digests and evidence manifest;
6. deploy through `production_release.sh up`; and
7. repeat the post-deployment checks.

Do not upgrade a production host with `git pull` or by rebuilding an image from
a source checkout. The deployed source identity must remain traceable to the
reviewed tag, commit, digest, attestations, and evidence.

## Rollback

Application rollback means redeploying a previously verified API and Web image
pair with the matching repository, commit, tag, and evidence. Do not rebuild an
old checkout on the production host.

Database migrations and authorization-model changes require separate rollback
plans. A previous application image may not be compatible with a newer schema.
Before each release, choose and test one of the following strategies:

- roll forward with a corrective release;
- restore the database and object store to a consistent pre-release point; or
- run a reviewed backward migration when the release explicitly supports it.

The evidence manifest includes an authorization canary and rollback gate. Keep
the previous OpenFGA model identifier available until the new model has passed
production verification.

## Backup and recovery

A recoverable deployment protects all authoritative state:

- PostgreSQL application data and runtime checkpoint data;
- S3 objects and their version or lifecycle history;
- OpenFGA data and the pinned authorization model configuration;
- runtime volumes that contain SDK-specific Chat state;
- production environment metadata and secret-manager references; and
- KMS keys and policies required to decrypt retained data.

gVisor hibernation snapshots are a performance cache, not an authoritative data
store, and do not need to be part of application recovery.

Test restoration in an isolated environment on a defined schedule. A restore
test should verify database consistency, object readability, KMS access,
authorization decisions, Agent Runtime resume, and one sandboxed Workflow run.
Record the result as immutable evidence for the `backup_restore` gate.

Backup retention must be documented alongside account-erasure policy because
the application cannot remove data from an operator-managed backup before that
backup expires. See [Security and data lifecycle](docs/security-and-data-lifecycle.md).

## Security incidents

Keep public endpoints, metrics, OpenFGA, databases, Valkey, `sandboxd`, and
administrative interfaces within their intended network boundaries. Rotate
affected credentials and preserve audit evidence when a control fails.

Report suspected product vulnerabilities through the private process in
[`SECURITY.md`](SECURITY.md), not through a public issue.
