"""
Project types: the shape a project starts out with.

A construction project and a software project do not keep the same things in
the same drawers. "Code Context" means nothing on a building site, and
"Drawings and specifications" means nothing in a web app. So a project is
created from a type, and the type decides three things:

  * which folders exist,
  * what the charter asks the team to write down,
  * what SKILL.md tells an AI agent about working here.

Adding a type is a change to this file and nothing else. `template_kind` in
the database is free text, so no migration is needed - which is the point.

The seeded text is written as questions, not filler. A heading with a real
question under it invites an answer; a page of placeholder prose invites
someone to close the tab.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FolderSpec:
    """One folder a new project of this type starts with."""

    name: str
    slug: str
    kind: str          # stable identifier, used for the sidebar icon
    icon: str          # a single glyph; the sidebar has no image assets
    description: str   # shown on the folder page
    read_when: str     # the left column of the "where to look" table in SKILL.md


@dataclass(frozen=True)
class ProjectType:
    kind: str
    label: str
    tagline: str                    # one line, shown in the project-type picker
    folders: tuple[FolderSpec, ...]
    achieve_prompt: str             # under "What we are trying to achieve"
    done_examples: tuple[str, ...]  # checklist items under "What done looks like"
    scope_prompt: str               # under "What is explicitly out of scope"
    agent_intro: str                # opening line of SKILL.md
    agent_rules: tuple[str, ...]    # example rules, shown as prompts


# ---------------------------------------------------------------------
# The types themselves.
# ---------------------------------------------------------------------

SOFTWARE = ProjectType(
    kind="software",
    label="Software",
    tagline="A system being built or maintained: code, deployments, technical decisions.",
    folders=(
        FolderSpec(
            "Code Context", "code-context", "code_context", "◈",
            "How the system is put together: modules, data flow, why it is shaped this way.",
            "How the system fits together",
        ),
        FolderSpec(
            "Code Documentation", "code-documentation", "code_documentation", "◉",
            "How to use it: setup, APIs, commands, examples.",
            "How to use or run it",
        ),
        FolderSpec(
            "Technical", "technical", "technical", "⚙",
            "Infrastructure, deployment, environments, operational runbooks.",
            "Infrastructure and deployment",
        ),
        FolderSpec(
            "Minutes of Meeting", "mom", "mom", "\U0001f5d3",
            "What was discussed and agreed, meeting by meeting.",
            "What was decided, and when",
        ),
    ),
    achieve_prompt="_One paragraph. If someone reads only this, what should they understand?_",
    done_examples=(
        "_A feature working end to end, in production_",
        "_A number that has to move: latency, error rate, hours saved_",
    ),
    scope_prompt=(
        "_Naming these now saves an argument later, and stops scope creep being deniable._"
    ),
    agent_intro="_Two or three sentences. Enough that an agent stops guessing._",
    agent_rules=(
        '_e.g. "Migrations only move forward: never edit an applied file."_',
        '_e.g. "All user-facing text is plain English, no jargon."_',
    ),
)

CONSTRUCTION = ProjectType(
    kind="construction",
    label="Construction",
    tagline="A build: drawings, site progress, permits, variations, and who signed off on what.",
    folders=(
        FolderSpec(
            "Drawings and Specifications", "drawings", "drawings", "▤",
            "Current drawings and specifications, and which revision is being built to.",
            "What is being built, and to which revision",
        ),
        FolderSpec(
            "Site Reports", "site-reports", "site_reports", "▱",
            "Daily and weekly progress from site: work done, weather, manpower, blockers.",
            "What actually happened on site",
        ),
        FolderSpec(
            "Permits and Compliance", "permits", "permits", "⚖",
            "Permits, inspections, certificates, and the conditions attached to them.",
            "What is approved and what is still pending",
        ),
        FolderSpec(
            "Variations and Cost", "variations", "variations", "◨",
            "Variation orders, claims, and the cost impact of each change.",
            "What a change cost and who approved it",
        ),
        FolderSpec(
            "Minutes of Meeting", "mom", "mom", "\U0001f5d3",
            "Site meetings and client meetings: what was discussed and agreed.",
            "What was decided, and when",
        ),
    ),
    achieve_prompt="_What is being built, where, and by when? One paragraph._",
    done_examples=(
        "_Handover accepted by the client with no outstanding defects_",
        "_Every permit closed out and the certificates filed_",
    ),
    scope_prompt=(
        "_Work the client may assume is included but is not. "
        "Write it down before it is claimed._"
    ),
    agent_intro="_What is being built and at what stage. Enough that an agent stops guessing._",
    agent_rules=(
        '_e.g. "Only drawings marked FOR CONSTRUCTION are current. IFA drawings are not."_',
        '_e.g. "No variation is real until it has a signed VO number."_',
    ),
)

AGENCY = ProjectType(
    kind="agency",
    label="Agency or consulting",
    tagline="Client work: scope, deliverables, approvals, and what was promised in which meeting.",
    folders=(
        FolderSpec(
            "Scope and Contract", "scope", "scope", "▦",
            "What was sold: the statement of work, the deliverables, and the terms.",
            "What was actually promised to the client",
        ),
        FolderSpec(
            "Client Context", "client-context", "client_context", "◈",
            "How the client works: their systems, their people, their constraints.",
            "Background on the client",
        ),
        FolderSpec(
            "Deliverables", "deliverables", "deliverables", "◉",
            "The work itself, and which version the client has approved.",
            "What has been delivered and approved",
        ),
        FolderSpec(
            "Minutes of Meeting", "mom", "mom", "\U0001f5d3",
            "Client calls and internal reviews: what was discussed and agreed.",
            "What was decided, and when",
        ),
    ),
    achieve_prompt="_What does the client get, and what changes for them once they have it?_",
    done_examples=(
        "_Final deliverable signed off by the named approver_",
        "_Handover done, and the client can run it without us_",
    ),
    scope_prompt="_Requests that will be a new engagement rather than part of this one._",
    agent_intro="_Who the client is and what this engagement covers._",
    agent_rules=(
        '_e.g. "Nothing goes to the client without an internal review first."_',
        '_e.g. "Only the named approver can sign off a deliverable."_',
    ),
)

EVENTS = ProjectType(
    kind="events",
    label="Event",
    tagline="A date that cannot move: run sheet, vendors, budget, and the decisions behind them.",
    folders=(
        FolderSpec(
            "Run Sheet", "run-sheet", "run_sheet", "◷",
            "The schedule on the day, minute by minute, and who owns each slot.",
            "What happens when, on the day",
        ),
        FolderSpec(
            "Vendors and Contracts", "vendors", "vendors", "◨",
            "Suppliers, what each one is committed to, and by when.",
            "Who is supplying what",
        ),
        FolderSpec(
            "Budget", "budget", "budget", "▤",
            "What was budgeted, what is committed, and what has actually been spent.",
            "Where the money is going",
        ),
        FolderSpec(
            "Minutes of Meeting", "mom", "mom", "\U0001f5d3",
            "Planning meetings: what was discussed and agreed.",
            "What was decided, and when",
        ),
    ),
    achieve_prompt="_What is the event, for whom, on what date, and what does a good one look like?_",
    done_examples=(
        "_The event ran on the day with no item left unowned_",
        "_Every vendor paid and reconciled against the budget_",
    ),
    scope_prompt="_What this event is not. Useful when someone asks to bolt on a second audience._",
    agent_intro="_What the event is, the date, and how much of it is already locked._",
    agent_rules=(
        '_e.g. "The date does not move. Anything that cannot make it is cut, not delayed."_',
        '_e.g. "A vendor is not booked until there is a signed contract."_',
    ),
)

ALL_TYPES: tuple[ProjectType, ...] = (SOFTWARE, CONSTRUCTION, AGENCY, EVENTS)
DEFAULT_TYPE = SOFTWARE

_BY_KIND = {t.kind: t for t in ALL_TYPES}

# Every folder icon any type defines, so the sidebar can draw a folder from
# its stored `kind` without knowing which type created it.
_ICONS = {f.kind: f.icon for t in ALL_TYPES for f in t.folders}
CUSTOM_FOLDER_ICON = "▪"


def get(kind: str | None) -> ProjectType:
    """
    The type with this kind, falling back to the default.

    Falling back rather than raising matters: a project created by a future
    version, or from a type someone later removed from this file, must still
    open.
    """
    return _BY_KIND.get(kind or "", DEFAULT_TYPE)


def folder_icon(kind: str | None) -> str:
    """The sidebar glyph for a folder kind. Unknown kinds get the custom mark."""
    return _ICONS.get(kind or "", CUSTOM_FOLDER_ICON)
