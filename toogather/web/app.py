"""
The TooGather web application and REST API.

Layout of this file (search for the banner comments):
  1. Setup: settings, database, templates, middleware
  2. Helpers: current user, CSRF, flash messages, project access
  3. First-run setup and login
  4. Projects: list, create, overview, upload, add event
  5. Review and event detail
  6. Project settings, users, API tokens
  7. REST API (used by the MCP bridge and scripts)
  8. Entry point

Security model in one paragraph: people sign in with email + password and get
a signed session cookie. Every form POST must carry a CSRF token. Every
project page checks the user's role in that project; a project the user
cannot access returns 404 so its existence is not revealed. API calls use
personal tokens and are written to the audit log.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from toogather import __version__, db, repo, security
from toogather.config import load_settings, require_secure_secret
from toogather.models import (
    EVENT_TYPE_LABELS,
    EventStatus,
    EventType,
    Role,
    can_change_status,
)
from toogather.services import project_brief, project_findings

log = logging.getLogger("toogather.web")

# =====================================================================
# 1. Setup
# =====================================================================

settings = load_settings()
HERE = Path(__file__).parent

MAX_UPLOAD_BYTES = 2 * 1024 * 1024       # 2 MB is plenty for text notes and transcripts
ALLOWED_UPLOAD_SUFFIXES = {".txt", ".md", ".vtt", ".srt", ".csv"}
ROLE_RANK = {Role.VIEWER.value: 0, Role.MEMBER.value: 1, Role.OWNER.value: 2}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Runs once when the web server starts and once when it stops."""
    require_secure_secret(settings)
    db.init_pool(settings.database_url)
    applied = db.run_migrations()
    if applied:
        log.info("Applied migrations: %s", ", ".join(applied))
    yield
    db.close_pool()


app = FastAPI(title="TooGather", version=__version__, lifespan=lifespan,
              docs_url=None, redoc_url=None)  # interactive docs off by default: less to expose
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key or "not-used-startup-will-refuse",
    session_cookie="toogather_session",
    max_age=60 * 60 * 12,          # sign in again after 12 hours
    same_site="lax",               # blocks most cross-site form attacks, together with CSRF tokens
    https_only=settings.cookie_secure,
)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals.update(
    EVENT_TYPE_LABELS=EVENT_TYPE_LABELS,
    EVENT_TYPES=[t.value for t in EventType],
    EVENT_STATUSES=[s.value for s in EventStatus],
    version=__version__,
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Headers that make common browser attacks harder."""
    response: Response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; "
        "frame-ancestors 'none'; form-action 'self'",
    )
    return response


# =====================================================================
# 2. Helpers
# =====================================================================


class Redirect(Exception):
    """Raise to send the browser somewhere else, e.g. to the login page."""

    def __init__(self, url: str):
        self.url = url


@app.exception_handler(Redirect)
async def _handle_redirect(_request: Request, exc: Redirect):
    return RedirectResponse(exc.url, status_code=303)


@app.exception_handler(StarletteHTTPException)
async def _handle_http_error(request: Request, exc: StarletteHTTPException):
    """JSON errors for the API, a friendly page for people."""
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    return templates.TemplateResponse(
        request, "error.html",
        {"status": exc.status_code, "message": exc.detail, **_base_context(request)},
        status_code=exc.status_code,
    )


def current_user(request: Request) -> dict | None:
    user_id = request.session.get("user_id")
    return repo.get_user(user_id) if user_id else None


def require_user(request: Request) -> dict:
    user = current_user(request)
    if user is None:
        if repo.count_users() == 0:
            raise Redirect("/setup")
        raise Redirect("/login")
    return user


def csrf_token(request: Request) -> str:
    """One CSRF token per session, created on first use."""
    if "csrf" not in request.session:
        request.session["csrf"] = security.new_csrf_token()
    return request.session["csrf"]


def check_csrf(request: Request, form_token: str | None) -> None:
    """Every state-changing browser request must prove it came from our own page."""
    received = form_token or request.headers.get("X-CSRF-Token")
    if not security.csrf_matches(request.session.get("csrf"), received):
        raise StarletteHTTPException(400, "This form expired. Reload the page and try again.")


def flash(request: Request, message: str, kind: str = "info") -> None:
    request.session.setdefault("flash", []).append({"kind": kind, "message": message})


def _base_context(request: Request) -> dict:
    """Values every template can use."""
    return {
        "user": current_user(request),
        "csrf_token": csrf_token(request),
        "flashes": request.session.pop("flash", []),
    }


def render(request: Request, template: str, status_code: int = 200, **context) -> HTMLResponse:
    return templates.TemplateResponse(
        request, template, {**_base_context(request), **context}, status_code=status_code
    )


def load_project(project_id: uuid.UUID, user: dict, min_role: str = Role.VIEWER.value):
    """
    Return (project, role) if the user may access the project at `min_role`.

    No access at all -> 404 (do not reveal that the project exists).
    Access but too low a role -> 403 with a clear message.
    """
    project = repo.get_project(str(project_id))
    role = repo.get_role(str(project_id), user) if project else None
    if project is None or role is None:
        raise StarletteHTTPException(404, "Project not found.")
    if ROLE_RANK[role] < ROLE_RANK[min_role]:
        raise StarletteHTTPException(403, f"This needs the {min_role} role in this project.")
    return project, role


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


# Very small in-memory login throttle: 5 failed attempts per email+IP locks
# that pair for 5 minutes. Good enough for an office install; put a reverse
# proxy with rate limiting in front for internet-facing installs.
_failed_logins: dict[str, list[float]] = defaultdict(list)
LOGIN_MAX_FAILURES = 5
LOGIN_LOCK_SECONDS = 300


def _login_key(request: Request, email: str) -> str:
    client = request.client.host if request.client else "unknown"
    return f"{email.strip().lower()}|{client}"


def _login_locked(key: str) -> bool:
    now = time.monotonic()
    _failed_logins[key] = [t for t in _failed_logins[key] if now - t < LOGIN_LOCK_SECONDS]
    return len(_failed_logins[key]) >= LOGIN_MAX_FAILURES


# =====================================================================
# 3. First-run setup and login
# =====================================================================


@app.get("/health")
def health():
    """For Docker health checks and quick troubleshooting. Reveals no data."""
    try:
        db.fetch_one("SELECT 1 AS ok")
        database = "ok"
    except Exception:
        database = "unreachable"
    return {
        "status": "ok" if database == "ok" else "degraded",
        "version": __version__,
        "database": database,
        "ai_extraction_configured": settings.llm_configured,
        "email_configured": settings.smtp_configured,
    }


@app.get("/setup")
def setup_page(request: Request):
    if repo.count_users() > 0:
        raise Redirect("/login")
    return render(request, "setup.html")


@app.post("/setup")
def setup_submit(request: Request, display_name: str = Form(...), email: str = Form(...),
                 password: str = Form(...), csrf: str = Form("")):
    check_csrf(request, csrf)
    # Checked again here so nobody can create a second admin through /setup.
    if repo.count_users() > 0:
        raise Redirect("/login")
    problem = security.password_problem(password)
    if problem:
        return render(request, "setup.html", status_code=400, error=problem,
                      display_name=display_name, email=email)
    user = repo.create_user(email, display_name, security.hash_password(password), is_admin=True)
    request.session.clear()
    request.session["user_id"] = str(user["id"])
    flash(request, "Your admin account is ready. Create your first project.")
    return RedirectResponse("/projects/new", status_code=303)


@app.get("/login")
def login_page(request: Request):
    if repo.count_users() == 0:
        raise Redirect("/setup")
    return render(request, "login.html")


@app.post("/login")
def login_submit(request: Request, email: str = Form(...), password: str = Form(...),
                 csrf: str = Form("")):
    check_csrf(request, csrf)
    key = _login_key(request, email)
    if _login_locked(key):
        return render(request, "login.html", status_code=429, email=email,
                      error="Too many failed attempts. Try again in 5 minutes.")
    user = repo.get_user_by_email(email)
    if user is None or not security.verify_password(user["password_hash"], password):
        _failed_logins[key].append(time.monotonic())
        # Same message for unknown email and wrong password: do not reveal which.
        return render(request, "login.html", status_code=401, email=email,
                      error="Email or password is incorrect.")
    _failed_logins.pop(key, None)
    request.session.clear()   # new session on login prevents session fixation
    request.session["user_id"] = str(user["id"])
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(request: Request, csrf: str = Form("")):
    check_csrf(request, csrf)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# =====================================================================
# 4. Projects
# =====================================================================


@app.get("/")
def projects_page(request: Request):
    user = require_user(request)
    return render(request, "projects.html", projects=repo.list_projects_for_user(user))


@app.get("/projects/new")
def new_project_page(request: Request):
    user = require_user(request)
    if not user["is_admin"]:
        raise StarletteHTTPException(403, "Only admins can create projects.")
    return render(request, "project_new.html")


@app.post("/projects/new")
def new_project_submit(request: Request, name: str = Form(...), description: str = Form(""),
                       csrf: str = Form("")):
    user = require_user(request)
    check_csrf(request, csrf)
    if not user["is_admin"]:
        raise StarletteHTTPException(403, "Only admins can create projects.")
    if not name.strip():
        return render(request, "project_new.html", status_code=400, error="Give the project a name.")
    project = repo.create_project(name, description, str(user["id"]))
    flash(request, "Project created. Upload meeting notes or add the first decision.")
    return RedirectResponse(f"/projects/{project['id']}", status_code=303)


@app.get("/projects/{project_id}")
def project_page(request: Request, project_id: uuid.UUID):
    user = require_user(request)
    project, role = load_project(project_id, user)
    pid = str(project_id)
    events = repo.events_for_drift(pid)
    findings = project_findings(settings, pid, events)
    return render(
        request, "project.html",
        project=project, role=role,
        brief=project_brief(settings, pid),
        findings=findings,
        counts=repo.count_events_by_status(pid),
        sources=repo.list_sources(pid, limit=8),
        can_edit=ROLE_RANK[role] >= ROLE_RANK[Role.MEMBER.value],
        today=settings.today().isoformat(),
    )


@app.post("/projects/{project_id}/upload")
async def upload_source(request: Request, project_id: uuid.UUID,
                        file: UploadFile | None = File(None), pasted_text: str = Form(""),
                        title: str = Form(""), csrf: str = Form("")):
    user = require_user(request)
    check_csrf(request, csrf)
    load_project(project_id, user, Role.MEMBER.value)
    target = f"/projects/{project_id}"

    if file is not None and file.filename:
        suffix = Path(file.filename).suffix.lower()
        if suffix not in ALLOWED_UPLOAD_SUFFIXES:
            flash(request, f"Upload a text file ({', '.join(sorted(ALLOWED_UPLOAD_SUFFIXES))}). "
                           "For Word or PDF, copy the text and paste it instead.", "error")
            return RedirectResponse(target, status_code=303)
        raw = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(raw) > MAX_UPLOAD_BYTES:
            flash(request, "That file is larger than 2 MB. Split it into smaller parts.", "error")
            return RedirectResponse(target, status_code=303)
        # utf-8-sig removes the byte-order mark some Windows editors add.
        content = raw.decode("utf-8-sig", errors="replace")
        filename = Path(file.filename).name
    elif pasted_text.strip():
        content = pasted_text
        filename = title.strip() or f"Pasted notes {settings.today().isoformat()}"
    else:
        flash(request, "Choose a file or paste some text first.", "error")
        return RedirectResponse(target, status_code=303)

    repo.create_source(str(project_id), filename, content, str(user["id"]), kind="upload")
    flash(request, f"“{filename}” was added. Proposals will appear in Review shortly.")
    return RedirectResponse(target, status_code=303)


@app.post("/projects/{project_id}/events")
def add_event(request: Request, project_id: uuid.UUID, type: str = Form(...),
              summary: str = Form(...), detail: str = Form(""), owner: str = Form(""),
              due_date: str = Form(""), source_ref: str = Form(""), csrf: str = Form("")):
    user = require_user(request)
    check_csrf(request, csrf)
    load_project(project_id, user, Role.MEMBER.value)
    if type not in {t.value for t in EventType} or not summary.strip():
        flash(request, "Choose a type and write a one-line summary.", "error")
    else:
        repo.create_event(
            project_id=str(project_id), type_=type, summary=summary, detail=detail,
            owner=owner.strip() or None, due_date=parse_date(due_date), source_ref=source_ref,
            status=EventStatus.CONFIRMED.value, proposed_by="person", created_by=str(user["id"]),
        )
        flash(request, f"{EVENT_TYPE_LABELS[type]} recorded.")
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.get("/projects/{project_id}/events")
def events_page(request: Request, project_id: uuid.UUID, q: str = "", type: str = "",
                status: str = ""):
    user = require_user(request)
    project, role = load_project(project_id, user)
    # Ignore unknown filter values instead of passing them to SQL.
    type_ = type if type in {t.value for t in EventType} else ""
    status_ = status if status in {s.value for s in EventStatus} else ""
    events = repo.search_events(str(project_id), q, type_, status_, limit=100)
    return render(request, "events.html", project=project, role=role, events=events,
                  q=q, type_filter=type_, status_filter=status_)


# =====================================================================
# 5. Review and event detail
# =====================================================================


@app.get("/projects/{project_id}/review")
def review_page(request: Request, project_id: uuid.UUID):
    user = require_user(request)
    project, role = load_project(project_id, user)
    proposals = repo.search_events(str(project_id), status=EventStatus.PROPOSED.value, limit=200)
    return render(request, "review.html", project=project, role=role, events=proposals,
                  can_edit=ROLE_RANK[role] >= ROLE_RANK[Role.MEMBER.value])


@app.post("/events/{event_id}/status")
def change_status(request: Request, event_id: uuid.UUID, new_status: str = Form(...),
                  csrf: str = Form("")):
    user = require_user(request)
    check_csrf(request, csrf)
    event = repo.get_event(str(event_id))
    if event is None:
        raise StarletteHTTPException(404, "Event not found.")
    load_project(event["project_id"], user, Role.MEMBER.value)

    if not can_change_status(event["status"], new_status):
        raise StarletteHTTPException(
            400, f"An event cannot move from {event['status']} to {new_status}.")
    updated = repo.change_event_status(str(event_id), new_status, str(user["id"]))

    # HTMX requests get just the updated row; normal form posts go back a page.
    if request.headers.get("HX-Request"):
        return render(request, "_event_row.html", e=updated, can_edit=True, compact=True)
    # Redirect to a page we build ourselves, never to a URL taken from a header.
    return RedirectResponse(f"/events/{event_id}", status_code=303)


@app.get("/events/{event_id}")
def event_page(request: Request, event_id: uuid.UUID):
    user = require_user(request)
    event = repo.get_event(str(event_id))
    if event is None:
        raise StarletteHTTPException(404, "Event not found.")
    project, role = load_project(event["project_id"], user)
    pid = str(project["id"])
    decisions = [d for d in repo.search_events(pid, type_=EventType.DECISION.value, limit=200)
                 if str(d["id"]) != str(event_id)]
    related = repo.get_event(str(event["related_id"])) if event["related_id"] else None
    source = repo.get_source(str(event["source_id"])) if event["source_id"] else None
    return render(
        request, "event.html", project=project, role=role, e=event,
        history=repo.event_history(str(event_id)), decisions=decisions, related=related,
        source=source, can_edit=ROLE_RANK[role] >= ROLE_RANK[Role.MEMBER.value],
    )


@app.post("/events/{event_id}/link")
def link_event(request: Request, event_id: uuid.UUID, related_id: str = Form(""),
               csrf: str = Form("")):
    """Link an event (usually a change) to the decision it implements."""
    user = require_user(request)
    check_csrf(request, csrf)
    event = repo.get_event(str(event_id))
    if event is None:
        raise StarletteHTTPException(404, "Event not found.")
    load_project(event["project_id"], user, Role.MEMBER.value)

    target = repo.get_event(related_id) if related_id else None
    # The linked event must exist and belong to the same project.
    if related_id and (target is None or target["project_id"] != event["project_id"]):
        raise StarletteHTTPException(400, "Choose an event from the same project.")
    db.execute("UPDATE events SET related_id = %s, updated_at = now() WHERE id = %s",
               (related_id or None, str(event_id)))
    flash(request, "Link saved." if related_id else "Link removed.")
    return RedirectResponse(f"/events/{event_id}", status_code=303)


@app.post("/events/{event_id}/supersede")
def supersede(request: Request, event_id: uuid.UUID, replaced_by_id: str = Form(...),
              csrf: str = Form("")):
    """Mark this event as replaced by a newer decision."""
    user = require_user(request)
    check_csrf(request, csrf)
    event = repo.get_event(str(event_id))
    newer = repo.get_event(replaced_by_id) if replaced_by_id else None
    if event is None or newer is None or newer["project_id"] != event["project_id"]:
        raise StarletteHTTPException(400, "Choose a newer event from the same project.")
    load_project(event["project_id"], user, Role.MEMBER.value)
    if not can_change_status(event["status"], EventStatus.SUPERSEDED.value):
        raise StarletteHTTPException(400, "Only confirmed events can be superseded.")
    repo.supersede_event(str(event_id), newer, str(user["id"]))
    flash(request, "Marked as superseded.")
    return RedirectResponse(f"/events/{event_id}", status_code=303)


# =====================================================================
# 6. Project settings, users, API tokens
# =====================================================================


@app.get("/projects/{project_id}/settings")
def settings_page(request: Request, project_id: uuid.UUID):
    user = require_user(request)
    project, role = load_project(project_id, user, Role.OWNER.value)
    return render(request, "settings.html", project=project, role=role,
                  members=repo.list_members(str(project_id)), all_users=repo.list_users(),
                  llm_configured=settings.llm_configured)


@app.post("/projects/{project_id}/settings/ai")
def settings_ai(request: Request, project_id: uuid.UUID, enabled: str = Form("off"),
                csrf: str = Form("")):
    user = require_user(request)
    check_csrf(request, csrf)
    load_project(project_id, user, Role.OWNER.value)
    turned_on = enabled == "on"
    repo.set_ai_extraction(str(project_id), turned_on)
    repo.audit("project.ai_extraction", user_id=str(user["id"]), project_id=str(project_id),
               detail={"enabled": turned_on})
    flash(request, "AI extraction is on." if turned_on else "AI extraction is off.")
    return RedirectResponse(f"/projects/{project_id}/settings", status_code=303)


@app.post("/projects/{project_id}/members")
def settings_members(request: Request, project_id: uuid.UUID, user_id: str = Form(...),
                     role: str = Form(...), action: str = Form("save"), csrf: str = Form("")):
    user = require_user(request)
    check_csrf(request, csrf)
    load_project(project_id, user, Role.OWNER.value)
    pid = str(project_id)
    target = f"/projects/{project_id}/settings"
    existing = {str(m["id"]): m["role"] for m in repo.list_members(pid)}

    # Never leave a project without an owner.
    losing_owner = existing.get(user_id) == Role.OWNER.value and (
        action == "remove" or role != Role.OWNER.value)
    if losing_owner and repo.count_owners(pid) <= 1:
        flash(request, "A project needs at least one owner. Add another owner first.", "error")
        return RedirectResponse(target, status_code=303)

    if action == "remove":
        repo.remove_member(pid, user_id)
        flash(request, "Member removed.")
    elif role in ROLE_RANK and repo.get_user(user_id):
        repo.upsert_member(pid, user_id, role)
        flash(request, "Member saved.")
    else:
        flash(request, "Choose a person and a role.", "error")
    repo.audit("project.members", user_id=str(user["id"]), project_id=pid,
               detail={"target_user": user_id, "role": role, "action": action})
    return RedirectResponse(target, status_code=303)


@app.get("/admin/users")
def users_page(request: Request):
    user = require_user(request)
    if not user["is_admin"]:
        raise StarletteHTTPException(403, "Only admins can manage people.")
    return render(request, "users.html", users=repo.list_users())


@app.post("/admin/users")
def users_create(request: Request, display_name: str = Form(...), email: str = Form(...),
                 password: str = Form(...), is_admin: str = Form("off"), csrf: str = Form("")):
    user = require_user(request)
    check_csrf(request, csrf)
    if not user["is_admin"]:
        raise StarletteHTTPException(403, "Only admins can manage people.")
    problem = security.password_problem(password)
    if problem:
        flash(request, problem, "error")
    elif repo.get_user_by_email(email):
        flash(request, "Someone with that email already exists.", "error")
    else:
        repo.create_user(email, display_name, security.hash_password(password),
                         is_admin=is_admin == "on")
        repo.audit("admin.user_created", user_id=str(user["id"]), detail={"email": email})
        flash(request, f"Account created for {email}. Share the password with them privately.")
    return RedirectResponse("/admin/users", status_code=303)


@app.get("/account/tokens")
def tokens_page(request: Request):
    user = require_user(request)
    new_token = request.session.pop("new_token", None)   # shown exactly once
    return render(request, "tokens.html", tokens=repo.list_tokens(str(user["id"])),
                  new_token=new_token, base_url=settings.base_url)


@app.post("/account/tokens")
def tokens_create(request: Request, name: str = Form(...), csrf: str = Form("")):
    user = require_user(request)
    check_csrf(request, csrf)
    plain, token_hash = security.new_api_token()
    repo.create_token(str(user["id"]), name or "Unnamed token", token_hash)
    request.session["new_token"] = plain
    return RedirectResponse("/account/tokens", status_code=303)


@app.post("/account/tokens/{token_id}/revoke")
def tokens_revoke(request: Request, token_id: uuid.UUID, csrf: str = Form("")):
    user = require_user(request)
    check_csrf(request, csrf)
    repo.revoke_token(str(token_id), str(user["id"]))
    flash(request, "Token revoked. Anything using it will stop working.")
    return RedirectResponse("/account/tokens", status_code=303)


# =====================================================================
# 7. REST API
# =====================================================================


def api_user(request: Request) -> dict:
    """Authenticate an API call from its `Authorization: Bearer tg_...` header."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise StarletteHTTPException(401, "Missing API token.")
    user = repo.user_for_token(security.hash_api_token(header.removeprefix("Bearer ").strip()))
    if user is None:
        raise StarletteHTTPException(401, "Invalid or revoked API token.")
    return user


def _jsonable_event(e: dict) -> dict:
    return {
        "id": str(e["id"]),
        "type": e["type"],
        "summary": e["summary"],
        "detail": e["detail"],
        "owner": e["owner"],
        "status": e["status"],
        "due_date": e["due_date"].isoformat() if e["due_date"] else None,
        "source": e.get("source_filename") or e["source_ref"] or None,
        "recorded_at": e["created_at"].isoformat(),
        "url": f"{settings.base_url}/events/{e['id']}",
    }


@app.get("/api/v1/projects")
def api_projects(request: Request):
    user = api_user(request)
    projects = repo.list_projects_for_user(user)
    repo.audit("api.list_projects", user_id=str(user["id"]), token_id=str(user["token_id"]))
    return {"projects": [
        {"id": str(p["id"]), "name": p["name"], "description": p["description"],
         "your_role": p["my_role"]} for p in projects
    ]}


@app.get("/api/v1/projects/{project_id}/events")
def api_events(request: Request, project_id: uuid.UUID, q: str = "", type: str = "",
               status: str = "", limit: int = 25):
    user = api_user(request)
    load_project(project_id, user)
    type_ = type if type in {t.value for t in EventType} else ""
    status_ = status if status in {s.value for s in EventStatus} else ""
    events = repo.search_events(str(project_id), q, type_, status_, limit=max(1, min(limit, 100)))
    repo.audit("api.search_events", user_id=str(user["id"]), token_id=str(user["token_id"]),
               project_id=str(project_id),
               detail={"q": q, "type": type_, "status": status_, "results": len(events)})
    return {"events": [_jsonable_event(e) for e in events]}


@app.get("/api/v1/projects/{project_id}/brief")
def api_brief(request: Request, project_id: uuid.UUID):
    user = api_user(request)
    project, _ = load_project(project_id, user)
    repo.audit("api.project_brief", user_id=str(user["id"]), token_id=str(user["token_id"]),
               project_id=str(project_id))
    return {"project": {"id": str(project["id"]), "name": project["name"]},
            "brief": project_brief(settings, str(project_id))}


@app.post("/api/v1/projects/{project_id}/sources")
async def api_add_source(request: Request, project_id: uuid.UUID):
    """
    Submit text (meeting notes, a commit summary, an agent's note) for review.
    Body: {"title": "...", "content": "..."}. It goes through the same
    AI-proposal-then-human-review path as a browser upload.
    """
    user = api_user(request)
    load_project(project_id, user, Role.MEMBER.value)
    payload = await request.json()
    title = str(payload.get("title") or "").strip()[:200] or f"API note {settings.today().isoformat()}"
    content = str(payload.get("content") or "")
    if not content.strip():
        raise StarletteHTTPException(400, "content is required.")
    if len(content.encode("utf-8")) > MAX_UPLOAD_BYTES:
        raise StarletteHTTPException(413, "content is larger than 2 MB.")
    source = repo.create_source(str(project_id), title, content, str(user["id"]), kind="api")
    repo.audit("api.add_source", user_id=str(user["id"]), token_id=str(user["token_id"]),
               project_id=str(project_id), detail={"title": title, "chars": len(content)})
    return {"source_id": str(source["id"]), "status": source["status"],
            "note": "Proposals will appear in the Review screen for a person to confirm."}


# =====================================================================
# 8. Entry point
# =====================================================================


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run("toogather.web.app:app", host="0.0.0.0", port=8080, proxy_headers=True)


if __name__ == "__main__":
    main()
