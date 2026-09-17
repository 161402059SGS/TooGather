"""
TooGather MCP bridge.

This lets AI agents that speak the Model Context Protocol (Claude Desktop,
Claude Code, Cursor, Nakama, and others) use a team's project memory.

How it works: the agent starts this small program on the user's own computer
(stdio transport). The program forwards every tool call to the TooGather
server's REST API using the user's personal API token.

Why a bridge instead of giving the agent database access:
  * The server checks permissions on every call. The agent can see exactly
    what its user can see, and nothing more.
  * Every call is written to the server's audit log.
  * Revoking the token in the web app cuts the agent off immediately.

Configuration (environment variables):
  TOOGATHER_URL    e.g. http://192.168.1.50:8080
  TOOGATHER_TOKEN  a token created under "API tokens" in the web app

Install and run:  pip install "toogather[mcp]"  then  toogather-mcp
"""

from __future__ import annotations

import json
import os
import sys

import httpx

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:  # pragma: no cover - explained to the user at startup
    MCPServer = None  # type: ignore[assignment,misc]

REQUEST_TIMEOUT_SECONDS = 30


def _client() -> httpx.Client:
    """An HTTP client pre-configured with the server address and token."""
    base_url = os.environ.get("TOOGATHER_URL", "").rstrip("/")
    token = os.environ.get("TOOGATHER_TOKEN", "")
    if not base_url or not token:
        raise RuntimeError("Set TOOGATHER_URL and TOOGATHER_TOKEN for the TooGather MCP bridge.")
    return httpx.Client(
        base_url=base_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )


def _request(method: str, path: str, **kwargs) -> dict:
    """Call the TooGather API and return JSON, turning errors into readable messages."""
    try:
        with _client() as client:
            response = client.request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        return {"error": f"Could not reach the TooGather server: {exc}"}
    try:
        data = response.json()
    except ValueError:
        data = {"error": response.text[:300]}
    if response.status_code >= 400:
        return {"error": data.get("error", f"HTTP {response.status_code}")}
    return data


def build_server():
    if MCPServer is None:
        sys.exit('The MCP bridge needs the "mcp" package. Install with: pip install "toogather[mcp]"')

    server = MCPServer(
        name="TooGather",
        instructions=(
            "TooGather holds a team's confirmed project memory: decisions, commitments, "
            "changes, risks, and open questions. Use list_projects to find a project id. "
            "When you answer from TooGather, mention the source and date of each fact, and say "
            "clearly when something is only 'proposed' (not yet confirmed by a person). "
            "If TooGather has no record of something, say so instead of guessing."
        ),
    )

    @server.tool()
    def list_projects() -> str:
        """List the projects you can access, with their ids and your role."""
        return json.dumps(_request("GET", "/api/v1/projects"), ensure_ascii=False)

    @server.tool()
    def search_events(project_id: str, query: str = "", type: str = "", status: str = "",
                      limit: int = 25) -> str:
        """
        Search a project's memory.

        project_id: id from list_projects.
        query: words to search for, e.g. "batch numbering". Empty lists the latest events.
        type: optional filter: decision, commitment, change, risk, or question.
        status: optional filter: proposed, confirmed, done, superseded, rejected.
                Leave empty to include everything except rejected.
        limit: maximum results (1-100).
        """
        params = {"q": query, "type": type, "status": status, "limit": limit}
        return json.dumps(
            _request("GET", f"/api/v1/projects/{project_id}/events", params=params),
            ensure_ascii=False,
        )

    @server.tool()
    def get_project_brief(project_id: str) -> str:
        """
        Get where a project stands: recent decisions, open commitments, risks, questions,
        and items that need attention (overdue commitments, unlinked changes, and so on).
        Good starting point before answering any question about a project.
        """
        return json.dumps(_request("GET", f"/api/v1/projects/{project_id}/brief"),
                          ensure_ascii=False)

    @server.tool()
    def submit_note(project_id: str, title: str, content: str) -> str:
        """
        Send a note (for example a meeting summary) to the project for review.
        It does NOT become project memory directly: people confirm or reject the
        suggested events in TooGather's Review screen. Requires the member role.
        """
        return json.dumps(
            _request("POST", f"/api/v1/projects/{project_id}/sources",
                     json={"title": title, "content": content}),
            ensure_ascii=False,
        )

    return server


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
