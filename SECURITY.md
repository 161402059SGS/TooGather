# Security

TooGather stores decisions, commitments, and meeting notes that are often confidential. This document describes how it protects that data, how to deploy it safely, and how to report a vulnerability.

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Use GitHub's private vulnerability reporting: on the repository page, open **Security → Report a vulnerability**. Include steps to reproduce and the version or commit you tested.

This is a small, volunteer-maintained project. We aim to acknowledge reports within 7 days and will credit reporters in the changelog unless you prefer otherwise.

## Supported versions

Until 1.0, only the latest release receives security fixes.

## What TooGather does

**Accounts and sessions**
- Passwords are hashed with argon2. Minimum length is 10 characters.
- Sessions use signed cookies (`HttpOnly`, `SameSite=Lax`, `Secure` when `COOKIE_SECURE=true`) and expire after 12 hours.
- The app refuses to start if `SECRET_KEY` is missing or shorter than 32 characters.
- A new session is issued at login to prevent session fixation.
- Repeated failed logins for the same email and address are locked for 5 minutes.
- The same error is shown for an unknown email and a wrong password.

**Requests**
- Every state-changing browser request requires a CSRF token.
- Responses send a strict Content-Security-Policy (scripts and styles only from the app itself), `X-Frame-Options: DENY`, and `X-Content-Type-Options: nosniff`.
- All SQL uses parameters; user input is never formatted into queries.
- Uploads are limited to 2 MB and to text file types.

**Access control**
- Every project page and API call checks the user's role in that project.
- A project the user cannot access returns 404, so its existence is not revealed.
- API tokens are random, stored only as SHA-256 hashes, shown once, and revocable.
- API calls and administrative changes are recorded in `audit_log`.

**AI and agents**
- AI extraction is off for every new project; an owner must enable it.
- Document text is treated as untrusted data, and AI output is validated before storage.
- AI suggestions never become confirmed memory without a person.
- The MCP bridge uses the user's own token, so agents cannot exceed that user's permissions.

**Deployment defaults**
- Containers run as a non-root user.
- The database port is not published outside the Docker network.

## Deploying safely

1. **Change every secret** in `.env` (`POSTGRES_PASSWORD`, `SECRET_KEY`) and never commit `.env`.
2. **Do not expose TooGather to the internet by forwarding a router port.** For remote access use a private network (Tailscale, NetBird, WireGuard, or your company VPN) approved by your IT team.
3. **If you must serve it publicly**, put it behind a reverse proxy with HTTPS (for example Caddy), set `COOKIE_SECURE=true`, add rate limiting at the proxy, and keep the server patched.
4. **Back up regularly and off the machine** with `scripts/backup.sh`, which encrypts backups with `age`. Test a restore at least once.
5. **Check client agreements** before enabling AI extraction on a project. With a cloud AI service, document text leaves your network. Use a local Ollama model when that is not acceptable.
6. **Protect AI provider keys.** They live in `.env` on the server and are never shown in the web app.
7. **Remove access promptly** when people leave a project: remove them from the project and revoke their API tokens.

## Known limitations

These are tracked in the roadmap. Consider them when deciding how to deploy.

- The login throttle is kept in memory and resets on restart; it does not coordinate across several web processes.
- There is no two-factor authentication yet. Keep TooGather on a private network until it exists.
- There is no self-service password reset; an admin creates accounts and must share initial passwords privately.
- AI provider settings are installation-wide rather than per project.
- Deleting a project removes its events and sources from the database, but copies may remain in existing backups until those backups expire.
- The audit log records API calls and administrative actions, not every page view in the web app.
