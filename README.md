# TooGather

**Shared project memory for teams and their AI agents.**

Every project team loses track of the same things: a decision made three meetings ago, a promise someone forgot, a change nobody told the project manager about. TooGather keeps those in one place, points out what has been forgotten, and lets AI agents answer questions from the team's confirmed memory instead of guessing.

It works for any kind of project: software and ERP implementations, agencies, construction, events, consulting.

> Status: **v0.1, early.** It works end to end and is ready for a pilot on a real project. Expect rough edges and breaking changes before 1.0.

## What it does

TooGather tracks five kinds of project knowledge:

| Type | Example |
|---|---|
| Decision | "We use batch numbering for finished goods." |
| Commitment | "Budi sends the chart of accounts mapping by 10 September." |
| Change | "The stock aging report query was modified to include consignment stock." |
| Risk | "About 800 duplicate customer records, nobody assigned to clean them." |
| Open question | "Who approves credit limit overrides?" |

The workflow:

1. **Capture.** Upload meeting notes, transcripts, or chat exports. Or record an event by hand.
2. **Propose.** If you allow it for the project, an AI model reads the notes and suggests events.
3. **Confirm.** A person confirms or rejects each suggestion. Only confirmed events count as project memory.
4. **Notice drift.** TooGather flags overdue commitments, changes not linked to any decision, risks without an owner, questions left unanswered, and suggestions nobody reviewed.
5. **Use it anywhere.** Browse it in the web app, receive a weekly email summary, or let an AI agent (Claude, Cursor, and others) query it through MCP.

## Principles

- **The AI proposes, people decide.** AI output never becomes project truth without human confirmation.
- **Every fact has a source.** Events keep a link to the notes they came from, and a history of every status change.
- **Private by default.** Self-hosted. AI extraction is off for every new project until an owner turns it on, because some clients do not allow their documents to be sent to an AI service. You can also use a local model with Ollama so nothing leaves your network.
- **Plain rules for drift.** Drift detection is simple, explainable code, not AI.

## Quick start

You need [Docker](https://docs.docker.com/get-docker/) with Docker Compose. On Windows, Docker Desktop needs WSL2; [Rancher Desktop](https://rancherdesktop.io/) and [Podman Desktop](https://podman-desktop.io/) are free alternatives.

```bash
git clone https://github.com/<your-account>/TooGather.git
cd TooGather
cp .env.example .env
```

Edit `.env` and set at least:

- `POSTGRES_PASSWORD`: any long random string
- `SECRET_KEY`: generate one with `python -c "import secrets; print(secrets.token_urlsafe(48))"`
- `BASE_URL`: how teammates will reach the server, e.g. `http://192.168.1.50:8080`
- `TIMEZONE`: e.g. `Asia/Jakarta`

Then start it:

```bash
docker compose up -d
```

Open `http://localhost:8080` (or your `BASE_URL`). Type your name once and you are in — there is no password to set up. Name the project you are working on and TooGather creates it with four folders, a charter to fill in, and a `SKILL.md` for your AI agents.

To try it quickly, create a project and upload `sample_data/sample-mom-weekly-sync.md`.

### A project is a workspace

Each project holds four folders out of the box, plus any you add:

| Folder | What belongs in it |
|---|---|
| Code Context | How the system is put together: modules, data flow, why it is shaped this way. |
| Code Documentation | How to use it: setup, APIs, commands, examples. |
| Technical | Infrastructure, deployment, environments, runbooks. |
| Minutes of Meeting | What was discussed and agreed, meeting by meeting. |

Two documents sit outside the folders and belong to the project itself:

- **Project charter** — what you are trying to achieve, what "done" looks like, what is out of scope.
- **`SKILL.md`** — what an AI agent should read before working on this project. Download it from the project page and drop it next to your code, or point an agent at `/projects/<id>/skill.md`.

Everything is Markdown, and it is stored as Markdown, so it survives a copy‑paste into a repo.

### Inviting your team

Open **Team** in any project you own, pick a role, and create an invite link. Whoever opens it joins at that role.

| Role | Can |
|---|---|
| Owner | Everything a member can, plus managing people, invites and settings. |
| Member | Create folders and documents, upload notes, confirm or reject proposals. |
| Viewer | Read everything, change nothing. |

A project always keeps at least one owner: the server refuses the change that would leave it with none.

### Upgrading

```bash
git pull
docker compose up -d --build
```

Database migrations run automatically on startup.

## Running it for a team

TooGather is designed as **one shared server** that the whole team uses. It is not meant to run separately on every laptop, because separate copies drift apart, which is the problem TooGather exists to solve.

For a team that mostly works in one office:

1. Run it on an always-on machine: a small office PC or server with 8 to 16 GB of RAM is enough when using a cloud AI service.
2. Give that machine a fixed IP address on your network (a DHCP reservation in your router).
3. Teammates open `http://<that-address>:8080` in a browser. Nothing else to install.

**Do not expose TooGather directly to the internet by forwarding a router port.** For access from home, use a private network such as Tailscale, NetBird, or your company VPN, with your IT team's approval. See [SECURITY.md](SECURITY.md).

## AI extraction

Set these in `.env` (any OpenAI-compatible endpoint works):

```bash
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=sk-...
LLM_MODEL=gpt-4o-mini
```

Then turn it on per project under **Settings → AI extraction**.

**Fully local with Ollama:** install [Ollama](https://ollama.com), pull a model, and set `LLM_BASE_URL=http://host.docker.internal:11434/v1` with no API key. On Linux, add `extra_hosts: ["host.docker.internal:host-gateway"]` to the `worker` service in `docker-compose.yml`. Local models need capable hardware: plan on 16 GB of RAM or more, and expect extraction to be slower than a cloud service.

Without AI configured, TooGather still works: uploads are stored and events are recorded by hand.

## Connect an AI agent (MCP)

TooGather includes a small bridge that lets MCP-compatible agents read project memory as you, with your permissions.

1. In the web app, open **API tokens** and create a token.
2. On the computer running your agent:

   ```bash
   pip install "toogather[mcp] @ git+https://github.com/<your-account>/TooGather.git"
   ```

3. Add this to your agent's MCP configuration (for Claude Desktop, `claude_desktop_config.json`):

   ```json
   {
     "mcpServers": {
       "toogather": {
         "command": "toogather-mcp",
         "env": {
           "TOOGATHER_URL": "http://192.168.1.50:8080",
           "TOOGATHER_TOKEN": "tg_your_token_here"
         }
       }
     }
   }
   ```

The agent gets four tools:

| Tool | What it does |
|---|---|
| `list_projects` | Projects you can access |
| `search_events` | Search a project's decisions, commitments, changes, risks, and questions |
| `get_project_brief` | Where the project stands, including what needs attention |
| `submit_note` | Send notes for review. They do not become memory until a person confirms them. |

Every call is recorded in the audit log. Revoke the token in the web app to cut the agent off immediately.

## REST API

All endpoints need `Authorization: Bearer <token>`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/projects` | List accessible projects |
| GET | `/api/v1/projects/{id}/events?q=&type=&status=&limit=` | Search events |
| GET | `/api/v1/projects/{id}/brief` | Project brief with attention items |
| POST | `/api/v1/projects/{id}/sources` | Submit `{"title", "content"}` for review |

Useful for scripts, for example sending a summary of recent git commits after each deployment.

## Roles

| Role | Can do |
|---|---|
| Admin | Create projects and accounts; acts as owner on every project |
| Owner | Manage project members and the AI setting, plus everything a member can do |
| Member | Upload notes, record events, confirm or reject suggestions |
| Viewer | Read only |

## Backups

```bash
age-keygen -o toogather-backup-key.txt      # once; store the key file somewhere safe
scripts/backup.sh <public-key-from-that-file>
```

This writes an encrypted file under `backups/`. Copy it off the server regularly. Restore instructions are at the top of `scripts/backup.sh`.

## Current limitations

Honest list, so you can decide whether it fits:

- Uploads are text only (`.txt`, `.md`, `.vtt`, `.srt`, `.csv`). For Word or PDF, paste the text.
- **There are no passwords.** Anyone who can reach the server can claim any name and join any project whose invite link they hold. That is deliberate for a team tool on a trusted network — run it on your LAN or behind a VPN, and put real authentication in front of it before exposing it to the internet.
- Documents are edited as raw Markdown in a textarea. There is no rich-text editor, and no live preview yet.
- Documents have no version history. Events do; documents are last-write-wins, so two people editing the same page at once will overwrite each other.
- AI provider settings apply to the whole installation. Per-project providers are planned.
- No automatic connectors yet (git, email, WhatsApp). Use the upload form or the API.
- Search is keyword full-text search, not semantic search.

See [ROADMAP.md](ROADMAP.md) for what comes next.

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md): how it is built and why
- [SECURITY.md](SECURITY.md): security model, safe deployment, and reporting vulnerabilities
- [CONTRIBUTING.md](CONTRIBUTING.md): development setup and how to contribute
- [ROADMAP.md](ROADMAP.md): plans and explicit non-goals
- [CHANGELOG.md](CHANGELOG.md): release history

## License

Apache License 2.0. See [LICENSE](LICENSE).

TooGather bundles [htmx](https://htmx.org) (0BSD license) so the web app works on networks without internet access.
