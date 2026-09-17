# Contributing to TooGather

Thank you for helping. TooGather is small on purpose, so the most valuable contributions are ones that keep it simple, reliable, and easy to self-host.

## Before you start

- **Bugs:** open an issue with steps to reproduce, what you expected, and what happened.
- **Features:** open an issue to discuss before writing code. Check [ROADMAP.md](ROADMAP.md) first, including the **non-goals**; proposals that conflict with them will likely be declined, and it is better to find out before you spend time.
- **Security issues:** follow [SECURITY.md](SECURITY.md). Do not open a public issue.

Good first contributions: new drift rules with tests, documentation fixes, accessibility improvements, clearer error messages, and deployment guides for specific environments.

## Development setup

You need Python 3.12+ and PostgreSQL 14+ (or Docker).

```bash
# 1. Start a database (or use a local PostgreSQL)
docker run -d --name toogather-dev-db -p 5432:5432 \
  -e POSTGRES_DB=toogather -e POSTGRES_USER=toogather -e POSTGRES_PASSWORD=devpass \
  postgres:16-alpine

# 2. Install TooGather in editable mode with development tools
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[mcp,dev]"

# 3. Configure
export DATABASE_URL=postgresql://toogather:devpass@localhost:5432/toogather
export SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(48))")
export TIMEZONE=Asia/Jakarta

# 4. Run the web app and, in a second terminal, the worker
uvicorn toogather.web.app:app --reload --port 8080
python -m toogather.worker
```

Open http://localhost:8080.

## Checks before opening a pull request

```bash
ruff check .
pytest
```

CI runs both, plus a secret scan.

## Code guidelines

- **Readability first.** Someone new should understand a function without running it. Comment the *why*, especially for security decisions and non-obvious SQL.
- **Keep SQL in `repo.py`** and always use parameters (`%s`), never string formatting with user input.
- **Drift rules are pure functions** in `drift.py`, with tests. No database or network calls inside rules.
- **Schema changes are new migration files.** Never edit a migration that has been released.
- **Check access on every new route** with `load_project()` and the minimum role needed. API routes also need an `audit()` entry.
- **No new services or heavy dependencies** without discussion. Every extra component is something self-hosters must run.
- **Never commit real client data.** Examples and tests use fictional names only.
- **Interface text** is short, plain, and in sentence case. Say what happens ("Confirm", "Record event"), not how it works internally.

## Pull requests

- Keep each pull request focused on one change.
- Explain what changed and why, and how you tested it.
- Add or update tests and documentation where behavior changes.
- Add a line to the "Unreleased" section of [CHANGELOG.md](CHANGELOG.md).

By contributing, you agree that your contributions are licensed under the Apache License 2.0.
