"""
The weekly digest: a short email to project owners listing what needs
attention. This is how TooGather reaches people who never open the app.

The wording is intentionally calm and factual ("needs attention", not
"incident" or "failure"), because the digest often goes to managers.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from toogather.config import Settings

log = logging.getLogger("toogather.digest")

_SECTION_TITLES = [
    ("open_commitments", "Open commitments"),
    ("open_risks", "Open risks"),
    ("open_questions", "Open questions"),
    ("decisions", "Recent decisions"),
]


def render_digest_text(project: dict, brief: dict, base_url: str) -> str:
    """Build a plain-text digest. Plain text reads well in every email client."""
    lines = [
        f"TooGather weekly summary: {project['name']}",
        "",
    ]

    attention = brief["attention"]
    if attention:
        lines.append(f"Needs attention ({len(attention)})")
        for item in attention[:10]:
            lines.append(f"  - {item['summary']}: {item['message']}")
        if len(attention) > 10:
            lines.append(f"  ...and {len(attention) - 10} more in the app.")
    else:
        lines.append("Nothing needs attention this week.")
    lines.append("")

    if brief["awaiting_review"]:
        lines.append(f"{brief['awaiting_review']} AI proposal(s) are waiting for review.")
        lines.append("")

    for key, title in _SECTION_TITLES:
        items = brief[key][:5]
        if not items:
            continue
        lines.append(title)
        for item in items:
            extra = []
            if item.get("owner"):
                extra.append(item["owner"])
            if item.get("due_date"):
                extra.append(f"due {item['due_date']}")
            suffix = f" ({', '.join(extra)})" if extra else ""
            lines.append(f"  - {item['summary']}{suffix}")
        lines.append("")

    lines.append(f"Open the project: {base_url}/projects/{project['id']}")
    return "\n".join(lines)


def send_email(settings: Settings, to_addresses: list[str], subject: str, body: str) -> None:
    """Send one plain-text email. Raises on failure so the caller can log it."""
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = ", ".join(to_addresses)
    message["Subject"] = subject
    message.set_content(body)

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
        if settings.smtp_starttls:
            smtp.starttls()
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(message)
