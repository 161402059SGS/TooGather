# Security

TooGather stores decisions, commitments, and meeting notes that are often confidential. This document describes how it protects that data, how to deploy it safely, and how to report a vulnerability.

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Use GitHub's private vulnerability reporting: on the repository page, open **Security → Report a vulnerability**. Include steps to reproduce and the version or commit you tested.

This is a small, volunteer-maintained project. We aim to acknowledge reports within 7 days and will credit reporters in the changelog unless you prefer otherwise.

## Supported versions

Until 1.0, only the latest release receives security fixes.

## What TooGather does

**Identity and sessions**

Read this part first, because it is the biggest thing to know about deploying
TooGather.

**There are no passwords.** A visitor types a display name once and is
remembered by a signed session cookie. That identity is what project roles
attach to, and those roles are enforced on every request. What it means in
practice: *anyone who can reach this server can claim any name, and can join
any project whose invite link they hold.* TooGather is a tool for a team on a
trusted network. Put real authentication in front of it before it is reachable
from anywhere else.

Given that model:

- Sessions use signed cookies (`HttpOnly`, `SameSite=Lax`, `Secure` when `COOKIE_SECURE=true`) and expire after 12 hours.
- A fresh session is issued when someone introduces themselves, which prevents session fixation.
- `SECRET_KEY` is generated on first start and kept in the data volume if you do not set one. If you do set one, the app refuses to start when it is shorter than 32 characters, because that key signs every session.
- An invite link is a capability to join one project at one role. Treat it like a password: it can be labelled, use-limited, and revoked, and revoking it takes effect immediately.
- Someone who is switched off - by an admin, or by themselves - is treated as signed out on their very next request, not whenever their cookie happens to expire.

**Requests**
- Every state-changing browser request requires a CSRF token.
- Responses send a strict Content-Security-Policy (scripts and styles only from the app itself), `X-Frame-Options: DENY`, and `X-Content-Type-Options: nosniff`.
- All SQL uses parameters; user input is never formatted into queries.
- Uploads are limited to 8 MB per file, and to 2 MB of text once read out of it.
- Document Markdown is rendered with raw HTML disabled and link URLs validated, so pasted `<script>` and `javascript:` links are shown as text rather than executed.

**Access control**
- Every project page and API call checks the user's role in that project.
- A project the user cannot access returns 404, so its existence is not revealed.
- API tokens are random, stored only as SHA-256 hashes, shown once, and revocable.
- API calls and administrative changes are recorded in `audit_log`.

**Stored secrets**
- A project's AI provider key and a connector's access token are encrypted before they are stored, with a key derived from `SECRET_KEY` by HKDF. A database dump, a stolen backup, or a read-only SQL user sees ciphertext only.
- This does **not** protect against anyone who can already read the server's environment or run code on it: they have `SECRET_KEY` and therefore the keys. Nothing short of an external key service would change that, and that is not something a small self-hosted install should have to run.
- Stored secrets are write-only in the web app. A page is told whether a key is set, never what it is, so no version of a page can leak one.
- If `SECRET_KEY` is replaced, stored secrets cannot be read back. The app treats them as unset and asks for them again rather than failing.

**Connectors**
- A connector is configured by a project owner and runs in the worker, never in a web request.
- `git` is run with an argument list, never a shell string. Repository URLs are restricted to https, http and ssh: git's `ext::` transport runs an arbitrary command, and a local path would let a project owner read any repository on the server. Branch names are checked against a character allow-list.
- An access token is percent-encoded into the URL, so a token containing `@` or `/` cannot change which host is contacted, and it is removed from every message that gets stored or shown.
- Git prompting is disabled, so a repository that wants credentials fails immediately instead of hanging a worker.
- A connector can only create material for review. It cannot write a confirmed event.
- `CONNECTORS_ENABLED=false` turns the whole mechanism off for the server, whatever any project has configured.

**Uploaded documents**
- A `.docx` part's uncompressed size is checked before it is read, so a small file cannot expand into gigabytes of memory.
- A `.docx` whose XML declares a DTD is refused. Word never writes one, and accepting one is how an XML parser is talked into expanding entities until the process dies.

**AI and agents**
- AI extraction is off for every new project; an owner must enable it.
- Document text is treated as untrusted data, and AI output is validated before storage.
- AI suggestions never become confirmed memory without a person.
- The MCP bridge uses the user's own token, so agents cannot exceed that user's permissions.

**Deployment defaults**
- Containers run as a non-root user.
- The database port is not published outside the Docker network.

## Deploying safely

1. **Set `POSTGRES_PASSWORD`** to something long and random, and never commit `.env`. Leave `SECRET_KEY` empty unless several machines serve the same install; one is generated on first start and kept in the data volume.
2. **Do not expose TooGather to the internet by forwarding a router port.** For remote access use a private network (Tailscale, NetBird, WireGuard, or your company VPN) approved by your IT team.
3. **If you must serve it publicly**, put it behind a reverse proxy with HTTPS (for example Caddy), set `COOKIE_SECURE=true`, add rate limiting at the proxy, and keep the server patched.
4. **Back up regularly and off the machine** with `scripts/backup.sh`, which encrypts backups with `age`. Test a restore at least once.
5. **Check client agreements** before enabling AI extraction on a project. With a cloud AI service, document text leaves your network. Use a local Ollama model when that is not acceptable, either for the whole server or for the one project that needs it.
6. **Protect AI provider keys.** The server's key lives in `.env`. A project's own key is encrypted in the database and is never shown in the web app.
7. **Use a read-only deploy key or token** for a Git connector. It only needs to read commits.
8. **Remove access promptly** when people leave: remove them from each project, revoke their API tokens, and switch their account off under **People**. Revoke any invite link they were given.

## Known limitations

These are tracked in the roadmap. Consider them when deciding how to deploy.

- **There is no authentication at all**, in the sense that word usually carries. See "Identity and sessions" above. Keep TooGather on a private network. Two-factor authentication is not on the roadmap until there is a first factor for it to strengthen.
- Anyone holding an invite link can join that project. Links are revocable, but they are not tied to a person.
- Stored secrets are only as protected as `SECRET_KEY`. Anyone who can read the server's environment can read them.
- A Git connector clones the repository onto the server. Anyone who can read that server's disk can read the repository.
- Deleting a project removes its events and sources from the database, but copies may remain in existing backups until those backups expire.
- The audit log records API calls and administrative actions, not every page view in the web app.
