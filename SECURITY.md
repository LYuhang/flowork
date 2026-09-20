# Security Policy

Flowork treats authentication, tenant isolation, sandbox execution, secrets,
and user data as security boundaries. This policy explains which versions
receive fixes, how to report a suspected vulnerability privately, and what to
expect during coordinated disclosure.

For the implemented protection model and account-erasure behavior, see
[Security and Data Lifecycle](docs/security-and-data-lifecycle.md). Operators
should also review the [Production Deployment Guide](DEPLOY.md) before placing
Flowork in a security-sensitive environment.

## Supported versions

Flowork is alpha software and does not yet maintain multiple supported release
lines.

| Version | Security support |
| --- | --- |
| Latest commit on the default branch | Supported |
| Earlier commits, tags, forks, and modified distributions | No backport commitment |

Security fixes are developed against the current default branch. Until a
stable release policy is published, deployments must evaluate and adopt the
latest applicable fixes rather than assuming an older tag remains supported.

## Report a vulnerability privately

Do not disclose a suspected vulnerability through a public issue, discussion,
pull request, commit message, or social-media post.

Until a dedicated security mailbox is published, use **GitHub Private
Vulnerability Reporting** from this repository's **Security** tab. If the
private reporting control is unavailable, open a non-sensitive issue requesting
a private contact method without including any vulnerability details.

Use the ordinary issue tracker for non-sensitive defects that do not expose a
security or privacy boundary.

## What to report

Examples of security-relevant reports include:

- authentication, Session, CSRF, passkey, account-recovery, or privilege-escalation
  failures;
- cross-user or cross-Organization authorization and data-isolation failures;
- sandbox escapes or unintended access to host files, credentials, processes,
  services, or network destinations;
- exposure of secrets, personal data, user-provided content, runtime state, or
  audit context through APIs, logs, browser assets, previews, or generated
  artifacts;
- unsafe file upload, archive extraction, path handling, content rendering, or
  Browser Extension behavior;
- account-deletion behavior that leaves active identity, authorization, or
  personal-tenant data recoverable;
- release, dependency, build, provenance, or update behavior that could deliver
  untrusted code; and
- prompt- or content-driven behavior that crosses an enforced authorization,
  sandbox, secret, or data boundary.

The following normally belong in a public issue rather than a private security
report:

- model quality, unexpected model output, or prompt-following problems that do
  not cross a security boundary;
- availability or performance problems without a security impact;
- reports that only expose documented local-development defaults after those
  defaults have been intentionally published to an untrusted network;
- automated scanner output without a vulnerable Flowork execution path or
  affected configuration; and
- vulnerabilities that exist only in an independently modified fork.

When uncertain whether public disclosure is safe, use the private reporting
channel.

## What to include

A useful report contains enough information to reproduce and assess the issue
without using real user data:

- a concise description of the violated security boundary;
- the affected commit, tag, component, and deployment mode;
- required configuration and environmental assumptions;
- deterministic reproduction steps or a minimal proof of concept;
- observed and expected behavior;
- the expected confidentiality, integrity, or availability impact;
- sanitized logs, requests, screenshots, or stack traces where relevant;
- whether the issue has been disclosed to anyone else; and
- a suggested mitigation or fix, if available.

Remove credentials, Session values, personal data, internal hostnames, and
unrelated production information before attaching evidence.

## Researcher conduct

Security testing must minimize harm:

- Test only accounts, data, systems, and deployments owned by the researcher or
  explicitly authorized for testing.
- Stop after obtaining the minimum evidence required to demonstrate impact.
- Do not retain access, establish persistence, extract unrelated data, degrade
  service for other users, or perform destructive tests.
- Do not use social engineering, credential stuffing, denial-of-service
  traffic, or attacks against third-party services.
- Delete locally retained sensitive evidence after coordinated handling no
  longer requires it.

These rules do not grant authorization to test infrastructure operated by
another party. Self-hosted operators define access to their own deployments.

## Handling and disclosure

Maintainers will use the private report to:

1. confirm the affected component and reproduction conditions;
2. assess impact, exploitability, and supported configurations;
3. develop and validate a fix or mitigation;
4. coordinate the timing and content of public disclosure; and
5. publish the applicable code, release, or security advisory when ready.

Response and remediation time depend on severity, reproducibility, and release
complexity; this alpha project does not currently publish a fixed response-time
service level. Reporters should keep technical details private until disclosure
has been coordinated. Credit may be included in an advisory when requested and
when doing so does not reveal private information.

## Deployment responsibility

The local Docker Compose and native-development configurations are not
production security profiles. Self-hosting operators remain responsible for
TLS termination, firewall and network policy, trusted proxy configuration,
identity-provider security, secret and KMS management, database and object
storage controls, backups, monitoring, retention, incident response, and timely
updates.

The repository does not yet provide a turnkey production infrastructure stack.
Do not bypass a startup security check or expose development defaults to the
Internet to work around that limitation. Current production constraints and
operator requirements are documented in
[Production Deployment](DEPLOY.md#current-deployment-status).

## Automated security controls

Repository security gates cover dependency auditing, container SBOM and
vulnerability evaluation, upload scanning, Browser Extension boundaries,
OpenFGA model and live authorization checks, and secret scanning across the
worktree and Git history. Their executable definitions are maintained in
[`security.yml`](.github/workflows/security.yml).

Tagged release artifacts are built, scanned, published by immutable digest,
and supplied with provenance and SBOM attestations through
[`release-images.yml`](.github/workflows/release-images.yml). These automated
controls reduce risk but do not replace security review, responsible deployment,
or private vulnerability reporting.
