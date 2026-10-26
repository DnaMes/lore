# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 2.x     | Yes       |
| < 2.0   | No        |

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Reporting: open a [GitHub private security advisory](https://github.com/DnaMes/lore/security/advisories/new) (preferred), or by emailing the maintainers directly.

Include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

You will receive a response within 72 hours. If confirmed, a patch will be released as soon as possible.

## Hardening

- **Host allowlist**: The web UI allows `localhost`, `127.0.0.1`, `[::1]`, the bind host, and hosts from `LORE_PUBLIC_ORIGINS`. `LORE_ALLOWED_HOSTS` adds hosts. Its `*` value disables validation with a warning. Other hosts receive HTTP 400. `lore-web` warns on stderr when a non-loopback bind has neither host nor public-origin configuration.
- **Origin check**: Web requests using `POST`, `PUT`, `PATCH`, or `DELETE` must be same-origin when they carry an `Origin` header. Origins from `LORE_PUBLIC_ORIGINS` are also accepted with `TRUSTED_PROXY` enabled. Rejected origins receive HTTP 403.
- **Cloud opt-in**: Cloud AI providers require `--allow-cloud` or `LORE_ALLOW_CLOUD=1`. This applies to every command that can send session text to a non-local provider. The values `true` and `yes` are also accepted.
- **Prune confinement**: `lore prune` deletes exports only inside the export root. It refuses paths and symlink targets outside that root.
- **Memory dedupe**: `lore memory dedupe` is a dry run by default; `--apply` writes changes. It never merges across `kind`, `scope_project`, or `scope_tool`. Undo logs record the store identity, and restore refuses a log from another store.

## Scope

This tool runs **locally** — it reads AI tool config directories on your machine and serves a local web UI. Data leaves the machine only when a provider is used on purpose (see [docs/PRIVACY.md](docs/PRIVACY.md) for the list). The default bind is `127.0.0.1` (see [docs/WEB-UI.md](docs/WEB-UI.md)).

Known limitations:
- The web UI has no built-in authentication. If you expose it publicly, use a reverse proxy with Basic Auth or similar.
- The default Docker stack publishes the UI on loopback only, has no login, and keeps its data in a named volume. The sources overlay mounts your AI tool directories read-only. Before any networked deployment, put an authenticating reverse proxy in front; see [docs/DOCKER.md](docs/DOCKER.md).
