"""
The TooGather web application and REST API.

Layout of this file (search for the banner comments):
  1. Setup: settings, database, templates, middleware
  2. Helpers: current user, CSRF, flash messages, project access
  3. Joining: name on first visit, invite links
  4. Projects: home, create, overview, upload, add event
  5. Folders and documents
  6. Team and roles
  7. Review and event detail
  8. Project settings, users, API tokens
 8b. Connectors
 8c. Your own account
  9. REST API (used by the MCP bridge and scripts)
 10. Entry point

Security model in one paragraph: there is no password. A visitor types a
display name once and gets a signed session cookie that identifies them from
then on; that identity is what roles attach to. Every form POST must carry a
CSRF token. Every project page checks the user's role in that project, and a
project the user cannot access returns 404 so its existence is not revealed.
API calls use personal tokens and are written to the audit log.

What that trades away, stated plainly: anyone who reaches this server can
claim any name and join any project they hold an invite link for. That is the
intended model for a team tool on a trusted network. Do not expose this
directly to the internet without putting authentication in front of it.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from toogather import (
    __version__,
    connectors,
    crypto,
    db,
    importers,
    project_types,
    repo,
    security,
    workspace,
)
from toogather.config import load_settings, require_secure_secret
from toogather.models import (
    EVENT_TYPE_LABELS,
    EventStatus,
    EventType,
    Role,
    can_change_status,
)
from toogather.services import project_brief, project_findings
from toogather.web.markdown import first_paragraph, render_markdown

log = logging.getLogger("toogather.web")

# =====================================================================
# 1. Setup
# =====================================================================

settings = load_settings()
HERE = Path(__file__).parent

# A PDF or a Word file is much larger than the text inside it, so the limit on
# what may be uploaded is larger than the limit on the text that comes out.
MAX_UPLOAD_BYTES = 8 * 1024 * 1024       # 8 MB: a long PDF with a text layer fits
MAX_TEXT_BYTES = 2 * 1024 * 1024         # 2 MB of text is a very long document indeed
ROLE_RANK = {Role.VIEWER.value: 0, Role.MEMBER.value: 1, Role.OWNER.value: 2}

# How often a connector may check, offered in the setup form. Anything faster
# than a quarter of an hour is polling, not scheduling.
CONNECTOR_INTERVALS = ((15, "Every 15 minutes"), (60, "Hourly"),
                       (360, "Every 6 hours"), (1440, "Daily"))


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
    PROJECT_TYPES=project_types.ALL_TYPES,
    # The sidebar draws a folder from its stored kind, which the project type
    # chose. Exposing the lookup keeps that mapping out of the template.
    folder_icon=project_types.folder_icon,
    version=__version__,
)
templates.env.filters["markdown"] = render_markdown
templates.env.filters["preview"] = first_paragraph


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
    """Raise to send the browser somewhere else, e.g. to the name prompt."""

    def __init__(self, url: str):
        self.url = url


@app.exception_handler(Redirect)
async def _handle_redirect(_request: Request, exc: Redirect):
    return RedirectResponse(exc.url, status_code=303)


@app.exception_handler(StarletteHTTPException)
async def _handle_http_error(request: Request, exc: StarletteHTTPException):
    """
    JSON errors for machines, a friendly page for people.

    /hooks/ counts as a machine: it is called by a CI job or a script, and an
    HTML error page tells whoever configured it nothing they can read in a log.
    """
    if request.url.path.startswith(("/api/", "/hooks/")):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    return templates.TemplateResponse(
        request, "error.html",
        {"status": exc.status_code, "message": exc.detail, **_base_context(request)},
        status_code=exc.status_code,
    )


def current_user(request: Request) -> dict | None:
    """
    Who is making this request, or None.

    A person an admin has switched off is treated as signed out, so
    deactivating someone takes effect on their next request rather than
    whenever their cookie happens to expire.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    user = repo.get_user(user_id)
    return user if user and user["is_active"] else None


def require_user(request: Request) -> dict:
    """
    The visitor, or a redirect to the one-time name prompt.

    `next` carries where they were heading so an invite link still lands on
    the right project after they introduce themselves.
    """
    user = current_user(request)
    if user is None:
        raise Redirect(f"/welcome?next={quote(request.url.path, safe='')}")
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
    """
    Values every template can use.

    `nav_projects` feeds the sidebar, which is on every signed-in page, so it
    is fetched here rather than remembered by each route.
    """
    user = current_user(request)
    return {
        "user": user,
        "csrf_token": csrf_token(request),
        "flashes": request.session.pop("flash", []),
        "nav_projects": repo.list_projects_for_user(user) if user else [],
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


def parse_timestamp(value: str | None) -> datetime | None:
    """
    Read back the `updated_at` a form was rendered with.

    Used for the "did somebody else save while I was typing?" check, so an
    unreadable value returns None, which means "do not check" rather than
    "the check failed". A tampered field can only give up a safeguard the
    tamperer already had, never overwrite something they could not have.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _looks_like_uuid(value: str) -> bool:
    """
    Is this worth handing to PostgreSQL as a uuid?

    Bulk actions take ids from checkboxes. Filtering them here means one
    mistyped value cannot make the whole query raise.
    """
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


# =====================================================================
# 3. Joining
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
        # Which connectors this build has, so an operator can tell a missing
        # connector from a misconfigured one without reading the logs.
        "connectors": (sorted(c.kind for c in connectors.available())
                       if settings.connectors_enabled else []),
    }


MAX_DISPLAY_NAME = 60


def safe_next(target: str | None) -> str:
    """
    Where to send someone after they introduce themselves.

    Only same-site paths are allowed. Anything absolute, protocol-relative, or
    empty falls back to the home page, so a crafted ?next= cannot bounce a
    visitor to another site.
    """
    if not target or not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


@app.get("/welcome")
def welcome_page(request: Request, next: str = "/"):
    """Asked once. Returning visitors have a cookie and never see this."""
    if current_user(request) is not None:
        raise Redirect(safe_next(next))
    return render(request, "welcome.html", next=safe_next(next))


@app.post("/welcome")
def welcome_submit(request: Request, display_name: str = Form(...),
                   next: str = Form("/"), csrf: str = Form("")):
    check_csrf(request, csrf)
    destination = safe_next(next)
    name = display_name.strip()
    if not name:
        return render(request, "welcome.html", status_code=400, next=destination,
                      error="Type a name so your team knows who did what.")
    if len(name) > MAX_DISPLAY_NAME:
        return render(request, "welcome.html", status_code=400, next=destination,
                      error=f"Keep it under {MAX_DISPLAY_NAME} characters.",
                      display_name=name)

    # The first person through the door administers the install. After that,
    # everyone is an ordinary user and gets their rights from project roles.
    is_first = repo.count_users() == 0
    user = repo.create_named_user(name, is_admin=is_first)
    request.session.clear()      # a fresh session prevents session fixation
    request.session["user_id"] = str(user["id"])
    return RedirectResponse(destination, status_code=303)


@app.post("/logout")
def logout(request: Request, csrf: str = Form("")):
    """
    Forget this browser.

    The user row stays: their name is attached to events and documents they
    created, and deleting it would blank out that history.
    """
    check_csrf(request, csrf)
    request.session.clear()
    return RedirectResponse("/welcome", status_code=303)


@app.get("/join/{code}")
def join_project(request: Request, code: str):
    """
    Open an invite link: join the project at the role the link carries.

    Someone without a session is sent to /welcome first and comes back here.
    """
    user = require_user(request)
    invite = repo.get_invite_by_code(code)
    problem = repo.invite_problem(invite)
    if problem:
        raise StarletteHTTPException(404, problem)

    project = repo.get_project(str(invite["project_id"]))
    if project is None:
        raise StarletteHTTPException(404, "That project no longer exists.")

    existing = repo.get_role(str(invite["project_id"]), user)
    if existing is not None:
        flash(request, f"You are already in {project['name']} as {existing}.")
    else:
        repo.accept_invite(invite, str(user["id"]))
        repo.audit("invite.accept", user_id=str(user["id"]),
                   project_id=str(invite["project_id"]), detail={"role": invite["role"]})
        flash(request, f"You joined {project['name']} as {invite['role']}.")
    return RedirectResponse(f"/projects/{invite['project_id']}", status_code=303)


# =====================================================================
# 4. Projects
# =====================================================================


@app.get("/")
def home_page(request: Request):
    """
    The landing page: one question and a box to answer it in.

    Naming the thing you are building is how a project starts here, so the
    composer is the first thing on the page rather than a list of forms.
    """
    user = require_user(request)
    return render(request, "home.html", projects=repo.list_projects_for_user(user))


@app.post("/projects/new")
def new_project_submit(request: Request, name: str = Form(...), description: str = Form(""),
                       template_kind: str = Form("software"), csrf: str = Form("")):
    """
    Create a project and furnish it.

    Anyone may create one: the creator becomes its owner, and from there the
    project's own roles decide who can do what. Seeding happens in the same
    request so the project is never briefly empty.

    The project type decides which folders appear and what the charter asks.
    An unknown type falls back to the default rather than being refused: the
    picker is a convenience, not a gate.
    """
    user = require_user(request)
    check_csrf(request, csrf)
    if not name.strip():
        flash(request, "Give the project a name first.", "error")
        return RedirectResponse("/", status_code=303)

    ptype = project_types.get(template_kind)
    project = repo.create_project(name, description, str(user["id"]), ptype.kind)
    workspace.seed_project(str(project["id"]), project["name"], str(user["id"]), ptype.kind)
    repo.audit("project.create", user_id=str(user["id"]), project_id=str(project["id"]),
               detail={"template_kind": ptype.kind})
    flash(request, f"{project['name']} is ready. Start with the charter.")
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
        folders=repo.list_folders(pid),
        project_type=project_types.get(project.get("template_kind")),
        accepted_uploads=", ".join(importers.accepted_suffixes()),
        upload_accept=",".join(importers.accepted_suffixes()),
        charter=repo.get_special_document(pid, "charter"),
        skill_doc=repo.get_special_document(pid, "skill"),
        recent_docs=repo.recent_documents(pid),
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
        raw = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(raw) > MAX_UPLOAD_BYTES:
            flash(request, f"That file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB. "
                           "Split it into smaller parts.", "error")
            return RedirectResponse(target, status_code=303)
        # The importers decide what a file type means: a .docx is unzipped, a
        # .pdf is read page by page, a WhatsApp export is turned into a
        # transcript. This route only moves bytes and reports the result.
        try:
            imported = importers.read_file(file.filename, raw)
        except (importers.UnsupportedFile, importers.ImportProblem) as exc:
            flash(request, str(exc), "error")
            return RedirectResponse(target, status_code=303)
        content, filename = imported.text, Path(file.filename).name
    elif pasted_text.strip():
        imported = importers.read_text(pasted_text)
        content = imported.text
        filename = title.strip() or f"Pasted notes {settings.today().isoformat()}"
    else:
        flash(request, "Choose a file or paste some text first.", "error")
        return RedirectResponse(target, status_code=303)

    # A 200-page PDF can hold more text than any note needs to be. Cut it here
    # rather than at the database, so the person is told it happened.
    truncated = False
    if len(content.encode("utf-8")) > MAX_TEXT_BYTES:
        content = content.encode("utf-8")[:MAX_TEXT_BYTES].decode("utf-8", errors="ignore")
        truncated = True

    repo.create_source(str(project_id), filename, content, str(user["id"]), kind="upload")
    message = f"“{filename}” was added. Proposals will appear in Review shortly."
    if imported.note:
        message = f"{imported.note} {message}"
    if truncated:
        message += (f" It was longer than {MAX_TEXT_BYTES // (1024 * 1024)} MB of text, "
                    "so only the beginning was kept.")
    flash(request, message)
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
                  folders=repo.list_folders(str(project_id)),
                  q=q, type_filter=type_, status_filter=status_)




# =====================================================================
# 5. Folders and documents
# =====================================================================

MAX_DOC_BODY = 400_000       # ~400 KB of Markdown; far past any real page
MAX_TITLE = 200
MAX_FOLDER_NAME = 80


def load_document(document_id: uuid.UUID, user: dict, min_role: str = Role.VIEWER.value):
    """
    Return (document, project, role) after checking access on the document's
    own project. Going through the project keeps one permission rule.
    """
    document = repo.get_document(str(document_id))
    if document is None:
        raise StarletteHTTPException(404, "Document not found.")
    project, role = load_project(document["project_id"], user, min_role)
    return document, project, role


@app.get("/projects/{project_id}/folders/{folder_id}")
def folder_page(request: Request, project_id: uuid.UUID, folder_id: uuid.UUID):
    user = require_user(request)
    project, role = load_project(project_id, user)
    folder = repo.get_folder(str(folder_id))
    if folder is None or str(folder["project_id"]) != str(project_id):
        raise StarletteHTTPException(404, "Folder not found.")
    return render(
        request, "folder.html",
        project=project, role=role, folder=folder,
        documents=repo.list_documents(str(folder_id)),
        folders=repo.list_folders(str(project_id)),
        can_edit=ROLE_RANK[role] >= ROLE_RANK[Role.MEMBER.value],
    )


@app.post("/projects/{project_id}/folders")
def create_folder_submit(request: Request, project_id: uuid.UUID,
                         name: str = Form(...), description: str = Form(""),
                         csrf: str = Form("")):
    """Adding a drawer needs the member role; the four defaults come for free."""
    user = require_user(request)
    check_csrf(request, csrf)
    load_project(project_id, user, Role.MEMBER.value)
    clean = name.strip()[:MAX_FOLDER_NAME]
    if not clean:
        flash(request, "Give the folder a name.", "error")
        return RedirectResponse(f"/projects/{project_id}", status_code=303)

    folder = repo.create_folder(
        project_id=str(project_id),
        name=clean,
        slug=workspace.unique_slug(str(project_id), clean),
        kind="custom",
        description=description.strip(),
        position=repo.next_folder_position(str(project_id)),
        created_by=str(user["id"]),
    )
    flash(request, f"Added {folder['name']}.")
    return RedirectResponse(f"/projects/{project_id}/folders/{folder['id']}", status_code=303)


@app.post("/projects/{project_id}/folders/{folder_id}/delete")
def delete_folder_submit(request: Request, project_id: uuid.UUID, folder_id: uuid.UUID,
                         csrf: str = Form("")):
    """
    Only an owner may delete a folder, because its documents go with it.
    The four seeded folders are refused: removing them would break the
    structure SKILL.md tells an agent to expect.
    """
    user = require_user(request)
    check_csrf(request, csrf)
    load_project(project_id, user, Role.OWNER.value)
    folder = repo.get_folder(str(folder_id))
    if folder is None or str(folder["project_id"]) != str(project_id):
        raise StarletteHTTPException(404, "Folder not found.")
    if folder["kind"] != "custom":
        raise StarletteHTTPException(403, "The four standard folders cannot be deleted.")

    repo.delete_folder(str(folder_id))
    flash(request, f"Deleted {folder['name']} and everything in it.")
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/projects/{project_id}/documents")
def create_document_submit(request: Request, project_id: uuid.UUID,
                           folder_id: str = Form(...), title: str = Form(""),
                           csrf: str = Form("")):
    user = require_user(request)
    check_csrf(request, csrf)
    load_project(project_id, user, Role.MEMBER.value)
    folder = repo.get_folder(folder_id)
    if folder is None or str(folder["project_id"]) != str(project_id):
        raise StarletteHTTPException(404, "Folder not found.")

    clean = title.strip()[:MAX_TITLE] or "Untitled"
    document = repo.create_document(
        project_id=str(project_id), folder_id=folder_id, title=clean,
        body=f"# {clean}\n\n", doc_kind="note", created_by=str(user["id"]),
    )
    return RedirectResponse(f"/documents/{document['id']}?edit=1", status_code=303)


@app.get("/documents/{document_id}")
def document_page(request: Request, document_id: uuid.UUID, edit: int = 0):
    user = require_user(request)
    document, project, role = load_document(document_id, user)
    can_edit = ROLE_RANK[role] >= ROLE_RANK[Role.MEMBER.value]
    return render(
        request, "document.html",
        project=project, role=role, document=document,
        folder=repo.get_folder(str(document["folder_id"])) if document["folder_id"] else None,
        folders=repo.list_folders(str(project["id"])),
        can_edit=can_edit,
        editing=bool(edit) and can_edit,
        version_count=repo.count_document_versions(str(document_id)),
    )


@app.post("/documents/{document_id}")
def save_document(request: Request, document_id: uuid.UUID,
                  title: str = Form(...), body: str = Form(""),
                  expected_updated_at: str = Form(""), csrf: str = Form("")):
    """
    Save a document, refusing to overwrite an edit made while this one was open.

    The form carries the `updated_at` it was rendered from. If the stored value
    has moved on, somebody else saved in the meantime and this save is refused:
    the editor comes back with both versions and the person decides. Before
    this, the later save simply won and the earlier one was gone with no trace.
    """
    user = require_user(request)
    check_csrf(request, csrf)
    document, project, _role = load_document(document_id, user, Role.MEMBER.value)
    if len(body) > MAX_DOC_BODY:
        raise StarletteHTTPException(413, "That document is too large to save.")

    clean_title = title.strip()[:MAX_TITLE] or "Untitled"
    saved = repo.update_document(
        str(document_id), clean_title, body, str(user["id"]),
        expected_updated_at=parse_timestamp(expected_updated_at),
    )
    if not saved:
        current = repo.get_document(str(document_id))
        return render(
            request, "document.html", status_code=409,
            project=project, role=repo.get_role(str(project["id"]), user),
            document=current,
            folder=(repo.get_folder(str(current["folder_id"]))
                    if current["folder_id"] else None),
            folders=repo.list_folders(str(project["id"])),
            can_edit=True, editing=True,
            version_count=repo.count_document_versions(str(document_id)),
            # What this person wrote is kept in the box; nothing is lost while
            # they decide what to do with it.
            draft_title=clean_title, draft_body=body,
            conflict=True,
        )

    flash(request, "Saved.")
    return RedirectResponse(f"/documents/{document_id}", status_code=303)


@app.get("/documents/{document_id}/history")
def document_history(request: Request, document_id: uuid.UUID):
    """Every earlier version of a document, newest first."""
    user = require_user(request)
    document, project, role = load_document(document_id, user)
    return render(
        request, "document_history.html",
        project=project, role=role, document=document,
        folders=repo.list_folders(str(project["id"])),
        versions=repo.list_document_versions(str(document_id)),
        can_edit=ROLE_RANK[role] >= ROLE_RANK[Role.MEMBER.value],
    )


@app.get("/documents/{document_id}/history/{version_id}")
def document_version_page(request: Request, document_id: uuid.UUID, version_id: int):
    user = require_user(request)
    document, project, role = load_document(document_id, user)
    version = repo.get_document_version(str(version_id), str(document_id))
    if version is None:
        raise StarletteHTTPException(404, "That version does not exist.")
    return render(
        request, "document_version.html",
        project=project, role=role, document=document, version=version,
        folders=repo.list_folders(str(project["id"])),
        can_edit=ROLE_RANK[role] >= ROLE_RANK[Role.MEMBER.value],
    )


@app.post("/documents/{document_id}/history/{version_id}/restore")
def restore_document_version(request: Request, document_id: uuid.UUID, version_id: int,
                             csrf: str = Form("")):
    """
    Put an old version back.

    This is an ordinary save, not a rewind: the version being replaced is
    itself kept, so restoring can be undone the same way.
    """
    user = require_user(request)
    check_csrf(request, csrf)
    document, project, _role = load_document(document_id, user, Role.MEMBER.value)
    version = repo.get_document_version(str(version_id), str(document_id))
    if version is None:
        raise StarletteHTTPException(404, "That version does not exist.")

    repo.update_document(str(document_id), version["title"], version["body"],
                         str(user["id"]))
    repo.audit("document.restore", user_id=str(user["id"]), project_id=str(project["id"]),
               detail={"document": str(document_id), "version": version_id})
    flash(request, "Restored. The version you replaced is still in the history.")
    return RedirectResponse(f"/documents/{document_id}", status_code=303)


@app.post("/documents/{document_id}/delete")
def delete_document_submit(request: Request, document_id: uuid.UUID, csrf: str = Form("")):
    """
    The charter and SKILL.md cannot be deleted: every project is expected to
    have exactly one of each, and the UI has nowhere to recreate them.
    """
    user = require_user(request)
    check_csrf(request, csrf)
    document, project, _role = load_document(document_id, user, Role.MEMBER.value)
    if document["doc_kind"] != "note":
        raise StarletteHTTPException(403, "The charter and SKILL.md cannot be deleted.")

    folder_id = document["folder_id"]
    repo.delete_document(str(document_id))
    flash(request, "Document deleted.")
    target = (f"/projects/{project['id']}/folders/{folder_id}" if folder_id
              else f"/projects/{project['id']}")
    return RedirectResponse(target, status_code=303)


@app.get("/projects/{project_id}/skill.md")
def download_skill(request: Request, project_id: uuid.UUID):
    """
    Serve SKILL.md as a plain file, so an agent can fetch it directly and a
    person can save it into their repo next to the code it describes.
    """
    user = require_user(request)
    project, _role = load_project(project_id, user)
    document = repo.get_special_document(str(project_id), "skill")
    if document is None:
        raise StarletteHTTPException(404, "This project has no SKILL.md.")
    return Response(
        document["body"],
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="SKILL.md"'},
    )


# =====================================================================
# 6. Team and roles
# =====================================================================


@app.get("/projects/{project_id}/team")
def team_page(request: Request, project_id: uuid.UUID):
    """Everyone in the project can see who else is in it and at what role."""
    user = require_user(request)
    project, role = load_project(project_id, user)
    is_owner = ROLE_RANK[role] >= ROLE_RANK[Role.OWNER.value]
    return render(
        request, "team.html",
        project=project, role=role, is_owner=is_owner,
        members=repo.list_members(str(project_id)),
        invites=repo.list_invites(str(project_id)) if is_owner else [],
        folders=repo.list_folders(str(project_id)),
        roles=[r.value for r in Role],
        base_url=settings.base_url.rstrip("/"),
    )


@app.post("/projects/{project_id}/invites")
def create_invite_submit(request: Request, project_id: uuid.UUID,
                         role: str = Form(...), label: str = Form(""),
                         max_uses: str = Form(""), csrf: str = Form("")):
    user = require_user(request)
    check_csrf(request, csrf)
    load_project(project_id, user, Role.OWNER.value)
    if role not in ROLE_RANK:
        raise StarletteHTTPException(400, "Unknown role.")

    # Blank means an unlimited link; anything unparseable is treated as blank
    # rather than rejected, since this is a convenience field.
    try:
        uses = int(max_uses) if max_uses.strip() else None
    except ValueError:
        uses = None
    if uses is not None and uses < 1:
        uses = None

    invite = repo.create_invite(str(project_id), workspace.new_invite_code(),
                                role, label, uses, str(user["id"]))
    repo.audit("invite.create", user_id=str(user["id"]), project_id=str(project_id),
               detail={"role": role})
    flash(request, f"Invite link ready for the {invite['role']} role.")
    return RedirectResponse(f"/projects/{project_id}/team", status_code=303)


@app.post("/projects/{project_id}/invites/{invite_id}/revoke")
def revoke_invite_submit(request: Request, project_id: uuid.UUID, invite_id: uuid.UUID,
                         csrf: str = Form("")):
    user = require_user(request)
    check_csrf(request, csrf)
    load_project(project_id, user, Role.OWNER.value)
    repo.revoke_invite(str(invite_id), str(project_id))
    flash(request, "Invite revoked. The link no longer works.")
    return RedirectResponse(f"/projects/{project_id}/team", status_code=303)


@app.post("/projects/{project_id}/members/{member_id}/role")
def change_member_role(request: Request, project_id: uuid.UUID, member_id: uuid.UUID,
                       role: str = Form(...), csrf: str = Form("")):
    """
    Change someone's role, or remove them with role='remove'.

    A project must keep at least one owner, otherwise nobody could ever manage
    it again. Both the demote and the remove path check that.
    """
    user = require_user(request)
    check_csrf(request, csrf)
    load_project(project_id, user, Role.OWNER.value)

    target_id = str(member_id)
    current = repo.get_role(str(project_id), {"id": target_id, "is_admin": False})
    if current is None:
        raise StarletteHTTPException(404, "That person is not in this project.")

    losing_an_owner = current == Role.OWNER.value and role != Role.OWNER.value
    if losing_an_owner and repo.count_owners(str(project_id)) <= 1:
        raise StarletteHTTPException(
            400, "This is the only owner. Make someone else an owner first.")

    if role == "remove":
        repo.remove_member(str(project_id), target_id)
        flash(request, "Removed from the project.")
    elif role in ROLE_RANK:
        repo.upsert_member(str(project_id), target_id, role)
        flash(request, f"Role changed to {role}.")
    else:
        raise StarletteHTTPException(400, "Unknown role.")

    repo.audit("member.role", user_id=str(user["id"]), project_id=str(project_id),
               detail={"target": target_id, "role": role})
    return RedirectResponse(f"/projects/{project_id}/team", status_code=303)
# =====================================================================
# 7. Review and event detail
# =====================================================================


@app.get("/projects/{project_id}/review")
def review_page(request: Request, project_id: uuid.UUID):
    user = require_user(request)
    project, role = load_project(project_id, user)
    proposals = repo.search_events(str(project_id), status=EventStatus.PROPOSED.value, limit=200)
    return render(request, "review.html", project=project, role=role, events=proposals,
                  folders=repo.list_folders(str(project_id)),
                  can_edit=ROLE_RANK[role] >= ROLE_RANK[Role.MEMBER.value])


@app.post("/projects/{project_id}/review/bulk")
async def review_bulk(request: Request, project_id: uuid.UUID):
    """
    Confirm or reject several proposals at once.

    A review queue holding forty suggestions from one meeting is not reviewed
    one button at a time; it is abandoned. The ids come from checkboxes, so
    they are caller-supplied and every one is checked against this project
    before anything moves - a crafted form cannot reach into another project.

    An id that cannot legally make the move is skipped rather than failing the
    whole batch, and the count of each outcome is reported.
    """
    user = require_user(request)
    form = await request.form()
    check_csrf(request, str(form.get("csrf", "")))
    load_project(project_id, user, Role.MEMBER.value)
    target = f"/projects/{project_id}/review"

    action = str(form.get("action", ""))
    new_status = {"confirm": EventStatus.CONFIRMED.value,
                  "reject": EventStatus.REJECTED.value}.get(action)
    if new_status is None:
        raise StarletteHTTPException(400, "Choose confirm or reject.")

    chosen = [value for value in form.getlist("event_ids") if _looks_like_uuid(str(value))]
    if not chosen:
        flash(request, "Tick the ones you want to act on first.", "error")
        return RedirectResponse(target, status_code=303)

    moved = skipped = 0
    for row in repo.events_for_bulk_review(str(project_id), [str(c) for c in chosen]):
        if can_change_status(row["status"], new_status):
            repo.change_event_status(str(row["id"]), new_status, str(user["id"]),
                                     note="bulk review")
            moved += 1
        else:
            skipped += 1

    repo.audit("event.bulk_status", user_id=str(user["id"]), project_id=str(project_id),
               detail={"action": action, "moved": moved, "skipped": skipped})
    word = "Confirmed" if action == "confirm" else "Rejected"
    message = f"{word} {moved}."
    if skipped:
        message += f" {skipped} could not move from where they were and were left alone."
    flash(request, message)
    return RedirectResponse(target, status_code=303)


@app.get("/projects/{project_id}/sources")
def sources_page(request: Request, project_id: uuid.UUID, kind: str = ""):
    """
    Everything brought into this project, and where each piece came from.

    Worth its own page once connectors exist: "did a person put this here, or
    did it arrive on its own?" is the first question asked of a proposal
    nobody recognises.
    """
    user = require_user(request)
    project, role = load_project(project_id, user)
    return render(
        request, "sources.html",
        project=project, role=role,
        folders=repo.list_folders(str(project_id)),
        sources=repo.list_sources(str(project_id), limit=200, kind=kind),
        counts=repo.count_sources_by_kind(str(project_id)),
        kind_filter=kind if kind in ("upload", "api", "connector") else "",
    )


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
def event_page(request: Request, event_id: uuid.UUID, edit: int = 0):
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
    can_edit = ROLE_RANK[role] >= ROLE_RANK[Role.MEMBER.value]
    return render(
        request, "event.html", project=project, role=role, e=event,
        history=repo.event_history(str(event_id)), decisions=decisions, related=related,
        source=source, folders=repo.list_folders(pid),
        revisions=repo.event_revisions(str(event_id)),
        can_edit=can_edit, editing=bool(edit) and can_edit,
    )


@app.post("/events/{event_id}/edit")
def edit_event(request: Request, event_id: uuid.UUID, summary: str = Form(...),
               detail: str = Form(""), owner: str = Form(""), due_date: str = Form(""),
               source_ref: str = Form(""), csrf: str = Form("")):
    """
    Correct what an event says.

    Only the words change. The status, the links and the source stay as they
    are, so fixing a typo in a confirmed decision does not re-open it, and the
    previous wording is kept so the correction is visible rather than silent.
    """
    user = require_user(request)
    check_csrf(request, csrf)
    event = repo.get_event(str(event_id))
    if event is None:
        raise StarletteHTTPException(404, "Event not found.")
    project, _role = load_project(event["project_id"], user, Role.MEMBER.value)

    if not summary.strip():
        flash(request, "An event needs a one-line summary.", "error")
        return RedirectResponse(f"/events/{event_id}?edit=1", status_code=303)

    repo.update_event_text(
        str(event_id),
        summary=summary[:300], detail=detail[:4000],
        owner=owner.strip()[:120] or None, due_date=parse_date(due_date),
        source_ref=source_ref.strip()[:300], edited_by=str(user["id"]),
    )
    repo.audit("event.edit", user_id=str(user["id"]), project_id=str(project["id"]),
               detail={"event": str(event_id)})
    flash(request, "Saved. What it said before is in the history below.")
    return RedirectResponse(f"/events/{event_id}", status_code=303)


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
# 8. Project settings, users, API tokens
# =====================================================================


@app.get("/projects/{project_id}/settings")
def settings_page(request: Request, project_id: uuid.UUID):
    user = require_user(request)
    project, role = load_project(project_id, user, Role.OWNER.value)
    override = repo.get_ai_settings(str(project_id))
    return render(
        request, "settings.html", project=project, role=role,
        members=repo.list_members(str(project_id)), all_users=repo.list_users(),
        folders=repo.list_folders(str(project_id)),
        project_type=project_types.get(project.get("template_kind")),
        llm_configured=settings.llm_configured,
        server_model=settings.llm_model,
        ai_override=override,
        # Never the key itself, only whether one is stored. The page has no
        # reason to know it, and a page that does not have it cannot leak it.
        ai_key_stored=bool(override and override["api_key_encrypted"]),
    )


@app.post("/projects/{project_id}/settings/provider")
def settings_provider(request: Request, project_id: uuid.UUID,
                      base_url: str = Form(""), model: str = Form(""),
                      api_key: str = Form(""), clear_key: str = Form(""),
                      csrf: str = Form("")):
    """
    Point one project at its own AI provider.

    The key is encrypted before it is stored. An empty key field means "keep
    the one already saved", so the form can be submitted without the page ever
    having held the key; clearing it is an explicit checkbox instead.
    """
    user = require_user(request)
    check_csrf(request, csrf)
    load_project(project_id, user, Role.OWNER.value)
    target = f"/projects/{project_id}/settings"

    if not base_url.strip() or not model.strip():
        flash(request, "An endpoint and a model are both needed. Leave them empty and "
                       "use “Use the server’s provider” to go back to the default.",
              "error")
        return RedirectResponse(target, status_code=303)
    if not base_url.strip().lower().startswith(("http://", "https://")):
        flash(request, "The endpoint must be an http or https URL.", "error")
        return RedirectResponse(target, status_code=303)

    if clear_key == "on":
        stored_key: str | None = ""
    elif api_key.strip():
        stored_key = crypto.encrypt_secret(settings.secret_key, api_key.strip())
    else:
        stored_key = None                      # leave whatever is saved alone

    repo.save_ai_settings(str(project_id), base_url.strip(), model.strip(),
                          stored_key, str(user["id"]))
    # The endpoint and model are recorded; the key never is.
    repo.audit("project.ai_provider", user_id=str(user["id"]), project_id=str(project_id),
               detail={"base_url": base_url.strip(), "model": model.strip()})
    flash(request, f"This project now uses {model.strip()} at {base_url.strip()}.")
    return RedirectResponse(target, status_code=303)


@app.post("/projects/{project_id}/settings/provider/clear")
def settings_provider_clear(request: Request, project_id: uuid.UUID, csrf: str = Form("")):
    """Go back to the server's AI provider. The stored key is deleted with the row."""
    user = require_user(request)
    check_csrf(request, csrf)
    load_project(project_id, user, Role.OWNER.value)
    repo.clear_ai_settings(str(project_id))
    repo.audit("project.ai_provider_clear", user_id=str(user["id"]), project_id=str(project_id))
    flash(request, "This project uses the server’s AI provider again. "
                   "Its own API key has been deleted.")
    return RedirectResponse(f"/projects/{project_id}/settings", status_code=303)


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


def require_admin(request: Request) -> dict:
    """The server administrator, or 403."""
    user = require_user(request)
    if not user["is_admin"]:
        raise StarletteHTTPException(403, "Only admins can do that.")
    return user


@app.get("/admin/projects")
def admin_projects_page(request: Request):
    """
    Every project on the server, with counts but not contents.

    An admin is not automatically on any team. This page exists so they can
    still see what the server holds, and let themselves into a project when
    they genuinely need to - which is recorded.
    """
    user = require_admin(request)
    mine = {str(p["id"]) for p in repo.list_projects_for_user(user)}
    return render(request, "admin_projects.html",
                  projects=repo.list_all_projects(), my_project_ids=mine)


@app.post("/admin/projects/{project_id}/join")
def admin_join_project(request: Request, project_id: uuid.UUID, csrf: str = Form("")):
    """
    Let an admin into a project as owner, and write it down.

    Whoever runs the server can read the database anyway, so this is not a
    new power. What it adds is a trace: the audit log records that an admin
    gave themselves access, and when.
    """
    user = require_admin(request)
    check_csrf(request, csrf)
    project = repo.get_project(str(project_id))
    if project is None:
        raise StarletteHTTPException(404, "Project not found.")

    if repo.get_role(str(project_id), user) is not None:
        flash(request, f"You are already in {project['name']}.")
    else:
        repo.upsert_member(str(project_id), str(user["id"]), Role.OWNER.value)
        repo.audit("admin.join_project", user_id=str(user["id"]),
                   project_id=str(project_id), detail={"granted": Role.OWNER.value})
        flash(request, f"You added yourself to {project['name']} as owner. "
                       "This has been recorded in the audit log.", "warning")
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.get("/admin/users")
def users_page(request: Request):
    require_admin(request)          # called for the check, not for the person
    return render(request, "users.html", users=repo.list_users())


@app.post("/admin/users/{target_id}")
def users_update(request: Request, target_id: uuid.UUID, action: str = Form(...),
                 csrf: str = Form("")):
    """
    Change what one person may do on this server.

    There is nothing to "create" here: people add themselves by opening the
    app and typing a name. What an admin can do is promote someone to admin,
    or switch a leaver off.
    """
    user = require_admin(request)
    check_csrf(request, csrf)
    target = repo.get_user(str(target_id))
    if target is None:
        raise StarletteHTTPException(404, "No such person.")

    # Do not let the last admin remove their own last route back in.
    removing_last_admin = (
        action in {"revoke_admin", "deactivate"}
        and target["is_admin"] and repo.count_admins() <= 1
    )
    if removing_last_admin:
        flash(request, "This is the only admin. Make someone else an admin first.", "error")
        return RedirectResponse("/admin/users", status_code=303)

    if action == "make_admin":
        repo.set_user_admin(str(target_id), True)
        flash(request, f"{target['display_name']} is now an admin.")
    elif action == "revoke_admin":
        repo.set_user_admin(str(target_id), False)
        flash(request, f"{target['display_name']} is no longer an admin.")
    elif action == "deactivate":
        repo.set_user_active(str(target_id), False)
        flash(request, f"{target['display_name']} has been switched off.")
    elif action == "reactivate":
        repo.set_user_active(str(target_id), True)
        flash(request, f"{target['display_name']} is active again.")
    else:
        raise StarletteHTTPException(400, "Unknown action.")

    repo.audit("admin.user_update", user_id=str(user["id"]),
               detail={"target": str(target_id), "action": action})
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
# 8b. Connectors
#
# A connector is a scheduled way of bringing material in. It is owner-only:
# it holds a credential, and it decides what lands in the project's review
# queue. The worker does the actual running; these routes only configure it
# and show what happened.
# =====================================================================


def _load_connector(project_id: uuid.UUID, connector_id: uuid.UUID) -> dict:
    """One connector, checked against its project so an id cannot cross over."""
    row = repo.get_connector(str(connector_id))
    if row is None or str(row["project_id"]) != str(project_id):
        raise StarletteHTTPException(404, "Connector not found.")
    return row


def _read_connector_form(connector, form) -> tuple[dict, str | None, str | None]:
    """
    Read a connector's own fields out of a submitted form.

    Returns (config, secret, problem). A `secret` of None means "keep whatever
    is stored": the form is never sent the existing secret, so an empty box
    cannot be told apart from an unchanged one without the explicit
    "forget it" checkbox.
    """
    config: dict[str, str] = {}
    secret: str | None = None
    for spec in connector.fields:
        value = str(form.get(spec.name, "")).strip()
        if not spec.secret:
            config[spec.name] = value[:500]
        elif form.get(f"clear_{spec.name}") == "on":
            secret = ""
        elif value:
            secret = value
    return config, secret, connector.config_problem(config)


def _interval(raw: str) -> int:
    """Pick a schedule from the form, refusing anything not on the menu."""
    allowed = {minutes for minutes, _ in CONNECTOR_INTERVALS}
    try:
        chosen = int(raw)
    except (TypeError, ValueError):
        return 60
    return chosen if chosen in allowed else 60


@app.get("/projects/{project_id}/connectors")
def connectors_page(request: Request, project_id: uuid.UUID):
    user = require_user(request)
    project, role = load_project(project_id, user, Role.OWNER.value)
    return render(
        request, "connectors.html",
        project=project, role=role,
        folders=repo.list_folders(str(project_id)),
        rows=repo.list_connectors(str(project_id)),
        available=connectors.available(),
        # A connector whose class is no longer installed still has a row; the
        # page needs the definition to draw its fields, so look each one up.
        definition=connectors.get,
        intervals=CONNECTOR_INTERVALS,
        connectors_enabled=settings.connectors_enabled,
        ai_on=project["ai_extraction_enabled"],
        base_url=settings.base_url.rstrip("/"),
    )


@app.post("/projects/{project_id}/connectors")
async def create_connector_submit(request: Request, project_id: uuid.UUID):
    """
    Add a connector. Its fields are defined by the connector class, not by
    this route, so the form is read as a whole rather than declared here.
    """
    user = require_user(request)
    form = await request.form()
    check_csrf(request, str(form.get("csrf", "")))
    load_project(project_id, user, Role.OWNER.value)
    target = f"/projects/{project_id}/connectors"

    connector = connectors.get(str(form.get("kind", "")))
    if connector is None:
        flash(request, "This server does not have that connector.", "error")
        return RedirectResponse(target, status_code=303)

    config, secret, problem = _read_connector_form(connector, form)
    if problem:
        flash(request, problem, "error")
        return RedirectResponse(target, status_code=303)

    name = str(form.get("name", "")).strip()[:120] or connector.label
    if connector.inbound:
        # Nothing to schedule. What it gets instead is a URL, and the code in
        # that URL is the whole of its authentication - so it is made here,
        # from the same generator as an invite code, never from the form.
        row = repo.create_inbound_connector(
            project_id=str(project_id), kind=connector.kind, name=name,
            config=config, code=workspace.new_invite_code(), created_by=str(user["id"]),
        )
    else:
        row = repo.create_connector(
            project_id=str(project_id), kind=connector.kind, name=name,
            config=config,
            secret_encrypted=crypto.encrypt_secret(settings.secret_key, secret or ""),
            run_every_minutes=_interval(str(form.get("run_every_minutes", "60"))),
            created_by=str(user["id"]),
        )
    # The config is recorded because it is not secret; the token is not.
    repo.audit("connector.create", user_id=str(user["id"]), project_id=str(project_id),
               detail={"kind": connector.kind, "config": config})
    if connector.inbound:
        flash(request, f"{row['name']} added. Copy its URL below and give it to "
                       "whatever will be posting.")
    else:
        flash(request, f"{row['name']} added. The first check runs within a minute.")
    return RedirectResponse(target, status_code=303)


@app.post("/projects/{project_id}/connectors/{connector_id}")
async def update_connector_submit(request: Request, project_id: uuid.UUID,
                                  connector_id: uuid.UUID):
    user = require_user(request)
    form = await request.form()
    check_csrf(request, str(form.get("csrf", "")))
    load_project(project_id, user, Role.OWNER.value)
    row = _load_connector(project_id, connector_id)
    target = f"/projects/{project_id}/connectors"

    connector = connectors.get(row["kind"])
    if connector is None:
        flash(request, "This server no longer has that connector, so it cannot be "
                       "changed. Delete it, or reinstall the connector.", "error")
        return RedirectResponse(target, status_code=303)

    config, secret, problem = _read_connector_form(connector, form)
    if problem:
        flash(request, problem, "error")
        return RedirectResponse(target, status_code=303)

    repo.update_connector(
        connector_id=str(connector_id), project_id=str(project_id),
        name=str(form.get("name", "")).strip()[:120] or row["name"],
        config=config,
        secret_encrypted=(None if secret is None
                          else crypto.encrypt_secret(settings.secret_key, secret)),
        run_every_minutes=_interval(str(form.get("run_every_minutes", "60"))),
        enabled=form.get("enabled") == "on",
    )
    repo.audit("connector.update", user_id=str(user["id"]), project_id=str(project_id),
               detail={"connector": str(connector_id), "config": config})
    flash(request, "Connector saved.")
    return RedirectResponse(target, status_code=303)


@app.post("/projects/{project_id}/connectors/{connector_id}/run")
def run_connector_submit(request: Request, project_id: uuid.UUID, connector_id: uuid.UUID,
                         csrf: str = Form("")):
    """
    Ask for a check now.

    This does not run anything here: a web request must not wait on a network
    fetch of unknown length. It marks the connector due, and the worker picks
    it up on its next pass, within about half a minute.
    """
    user = require_user(request)
    check_csrf(request, csrf)
    load_project(project_id, user, Role.OWNER.value)
    _load_connector(project_id, connector_id)
    repo.run_connector_now(str(connector_id), str(project_id))
    flash(request, "Queued. The worker will check within a minute; reload to see the result.")
    return RedirectResponse(f"/projects/{project_id}/connectors", status_code=303)


@app.post("/projects/{project_id}/connectors/{connector_id}/delete")
def delete_connector_submit(request: Request, project_id: uuid.UUID, connector_id: uuid.UUID,
                            csrf: str = Form("")):
    user = require_user(request)
    check_csrf(request, csrf)
    load_project(project_id, user, Role.OWNER.value)
    row = _load_connector(project_id, connector_id)
    repo.delete_connector(str(connector_id), str(project_id))
    repo.audit("connector.delete", user_id=str(user["id"]), project_id=str(project_id),
               detail={"kind": row["kind"], "name": row["name"]})
    flash(request, f"{row['name']} removed. What it already brought in is still here.")
    return RedirectResponse(f"/projects/{project_id}/connectors", status_code=303)


@app.post("/hooks/{code}")
async def receive_webhook(request: Request, code: str):
    """
    Accept a note posted by something that is not a person.

    This is the one route with no session and no CSRF token, because the
    caller is a machine holding a secret in its URL rather than a browser
    submitting a form. Three things follow from that, and all three are
    deliberate:

      * The code is the entire authentication. It is long and random, it is
        revoked by deleting the connector, and it grants exactly one thing:
        the right to add material to one project, for a person to review.
      * A bad code gets the same 404 as an unknown path. Telling a caller that
        a code exists but is disabled would let someone probe for live ones.
      * Nothing here can create an event. The posted note lands in the review
        queue like any upload, so the worst a leaked code buys is noise that a
        person rejects - not a false entry in a team's memory.
    """
    if not settings.connectors_enabled:
        raise StarletteHTTPException(404, "Not found.")

    row = repo.get_connector_by_code(code)
    connector = connectors.get(row["kind"]) if row else None
    if row is None or connector is None or not connector.inbound:
        raise StarletteHTTPException(404, "Not found.")

    body = await request.body()
    try:
        result = connector.receive(connectors.Delivery(
            content_type=request.headers.get("content-type", ""),
            body=body,
            config=dict(row["config"] or {}),
            project_id=str(row["project_id"]),
            connector_id=str(row["id"]),
        ))
    except connectors.ConnectorError as exc:
        # A readable 400: whoever configured the caller has to be able to see
        # what was wrong with what it sent.
        raise StarletteHTTPException(400, str(exc)) from exc

    stored = 0
    for item in result.items:
        # No external_id: a webhook has no stable identifier for what it sends,
        # and the same deploy summary may legitimately be posted twice. The
        # unique index only covers a non-empty one, so each delivery is its own
        # source rather than being refused as a duplicate.
        repo.create_inbound_source(
            project_id=str(row["project_id"]), connector_id=str(row["id"]),
            filename=item.title, content=item.content,
        )
        stored += 1
    repo.record_inbound(str(row["id"]), result.note or f"Received {stored} note(s).")
    repo.audit("connector.receive", project_id=str(row["project_id"]),
               detail={"connector": str(row["id"]), "items": stored})
    return {"ok": True, "stored": stored,
            "note": "It will appear in the project's Review screen for a person to confirm."}


# =====================================================================
# 8c. Your own account
# =====================================================================


@app.get("/account")
def account_page(request: Request):
    """
    What a person can change about themselves: their name, and whether their
    account is still in use.
    """
    user = require_user(request)
    return render(request, "account.html",
                  sole_owner_of=repo.projects_solely_owned_by(str(user["id"])),
                  is_last_admin=user["is_admin"] and repo.count_admins() <= 1)


@app.post("/account/name")
def account_rename(request: Request, display_name: str = Form(...), csrf: str = Form("")):
    """
    Change your display name.

    The user row is not replaced, so everything already attached to this
    person - events, documents, project roles - simply shows the new name.
    """
    user = require_user(request)
    check_csrf(request, csrf)
    name = display_name.strip()
    if not name:
        flash(request, "A name cannot be empty.", "error")
    elif len(name) > MAX_DISPLAY_NAME:
        flash(request, f"Keep it under {MAX_DISPLAY_NAME} characters.", "error")
    else:
        repo.set_display_name(str(user["id"]), name)
        flash(request, f"You are now shown as {name}.")
    return RedirectResponse("/account", status_code=303)


@app.post("/account/deactivate")
def account_deactivate(request: Request, csrf: str = Form(""), confirm: str = Form("")):
    """
    Switch your own account off and sign out.

    The row stays, so your name stays on what you wrote; you simply stop being
    able to use it. An admin can switch it back on.

    Two things are refused, because both would leave the server stuck: being
    the only owner of a project, and being the only admin. Each names exactly
    what to fix first rather than saying "not allowed".
    """
    user = require_user(request)
    check_csrf(request, csrf)
    if confirm != "deactivate":
        flash(request, "Type deactivate to confirm.", "error")
        return RedirectResponse("/account", status_code=303)

    orphaned = repo.projects_solely_owned_by(str(user["id"]))
    if orphaned:
        names = ", ".join(p["name"] for p in orphaned)
        flash(request, f"You are the only owner of {names}. Make someone else an owner "
                       "first, or nobody will be able to manage it.", "error")
        return RedirectResponse("/account", status_code=303)
    if user["is_admin"] and repo.count_admins() <= 1:
        flash(request, "You are the only admin on this server. Make someone else an "
                       "admin first.", "error")
        return RedirectResponse("/account", status_code=303)

    repo.set_user_active(str(user["id"]), False)
    repo.audit("account.deactivate", user_id=str(user["id"]))
    request.session.clear()
    return RedirectResponse("/welcome", status_code=303)


# =====================================================================
# 9. REST API
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
    if len(content.encode("utf-8")) > MAX_TEXT_BYTES:
        raise StarletteHTTPException(
            413, f"content is larger than {MAX_TEXT_BYTES // (1024 * 1024)} MB.")
    source = repo.create_source(str(project_id), title, content, str(user["id"]), kind="api")
    repo.audit("api.add_source", user_id=str(user["id"]), token_id=str(user["token_id"]),
               project_id=str(project_id), detail={"title": title, "chars": len(content)})
    return {"source_id": str(source["id"]), "status": source["status"],
            "note": "Proposals will appear in the Review screen for a person to confirm."}


# =====================================================================
# 10. Entry point
# =====================================================================


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run("toogather.web.app:app", host="0.0.0.0", port=8080, proxy_headers=True)


if __name__ == "__main__":
    main()
