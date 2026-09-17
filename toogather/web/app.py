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
from datetime import date
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from toogather import __version__, db, repo, security, workspace
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
                       csrf: str = Form("")):
    """
    Create a project and furnish it.

    Anyone may create one: the creator becomes its owner, and from there the
    project's own roles decide who can do what. Seeding happens in the same
    request so the project is never briefly empty.
    """
    user = require_user(request)
    check_csrf(request, csrf)
    if not name.strip():
        flash(request, "Give the project a name first.", "error")
        return RedirectResponse("/", status_code=303)

    project = repo.create_project(name, description, str(user["id"]))
    workspace.seed_project(str(project["id"]), project["name"], str(user["id"]))
    repo.audit("project.create", user_id=str(user["id"]), project_id=str(project["id"]))
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
    )


@app.post("/documents/{document_id}")
def save_document(request: Request, document_id: uuid.UUID,
                  title: str = Form(...), body: str = Form(""), csrf: str = Form("")):
    user = require_user(request)
    check_csrf(request, csrf)
    document, project, _role = load_document(document_id, user, Role.MEMBER.value)
    if len(body) > MAX_DOC_BODY:
        raise StarletteHTTPException(413, "That document is too large to save.")

    repo.update_document(str(document_id), title.strip()[:MAX_TITLE] or "Untitled",
                         body, str(user["id"]))
    flash(request, "Saved.")
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
# 8. Project settings, users, API tokens
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
    if len(content.encode("utf-8")) > MAX_UPLOAD_BYTES:
        raise StarletteHTTPException(413, "content is larger than 2 MB.")
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
