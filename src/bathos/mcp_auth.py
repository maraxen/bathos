"""Lightweight shared-secret token check for bathos's MCP write-verb tools.

## Debt #619 context

The MCP anchor write seam (`anchor_insert`/`figure_entry_register`/
`attestation_register`/etc. in `bathos.mcp`) previously accepted writes from
ANY MCP caller with no auth at all — flagged by the cross-boundary gate spike,
which wrote a real row into the shared `~/.bth/catalog/bathos.db` from an
external session.

## Why a token check, not full RBAC

`bathos.mcp.mcp_server()` runs FastMCP's default **stdio** transport
(`app.run()` with no `transport=`/`host=`/`port=` override — see that
function's docstring, "Entry point for MCP server (stdio transport)").
Registration (`bathos.export.register_mcp` / `_mcp_entry`) writes a
`{"command": ...}` argv entry into the caller's MCP config (`~/.claude.json`,
`.mcp.json`) — never a `url` key. That means every real caller (Claude Code,
or any other MCP client) spawns `bth-mcp` as a **subprocess it owns**; the
primary access boundary is already "can this OS user exec this binary",
enforced by the filesystem/process permissions of whoever can invoke
`bth-mcp` — not application-layer auth. A full multi-tenant auth system would
be over-engineering for that trust model.

This token check exists as **defense-in-depth**: if the server process is
ever misconfigured to listen on a network interface, or fronted by a proxy
that turns stdio into a shared network endpoint, a network-reachable caller
still cannot write into the catalog without first reading a local, 0600-mode
token file that only the local OS user (i.e. someone already inside the
stdio trust boundary) can read. It is not, and is not meant to be, a
substitute for real bearer-token auth in a genuinely multi-tenant/networked
deployment.

## Stdio is exempt (debt #2841)

Agent sessions run under an access guard that will not let them read
`~/.bth/mcp_token`, so with the check always on, no agent could call any
write tool (`run`, `campaign_add`, `claim_register`, ...), while the
equivalent `bth` CLI commands needed no token at all. Over stdio the
caller already owns the server process, which is exactly the trust boundary
above, so `bth-mcp`'s stdio entry point calls `mark_stdio_transport()` and
the check is skipped. It still applies when the tools are served any other
way (nothing marks the transport), and `BTH_MCP_REQUIRE_TOKEN=1` forces it
back on even over stdio.
"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path


class McpAuthError(Exception):
    """Raised when a write-verb MCP tool call carries a missing/invalid token."""


_stdio_transport = False


def mark_stdio_transport() -> None:
    """Record that this process serves MCP over stdio (see module docstring)."""
    global _stdio_transport
    _stdio_transport = True


def token_required() -> bool:
    """Whether write-verb tools must carry a valid token in this process.

    False only over stdio, unless `BTH_MCP_REQUIRE_TOKEN=1` forces the check.
    """
    value = os.environ.get("BTH_MCP_REQUIRE_TOKEN", "").strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value not in ("", "0", "false", "no", "off"):
        # An unrecognised value is an attempt to opt in; fail closed.
        return True
    return not _stdio_transport


def token_path() -> Path:
    """Resolve the local MCP token file path.

    Honors BTH_MCP_TOKEN_PATH for tests/overrides; defaults to ~/.bth/mcp_token.
    """
    override = os.environ.get("BTH_MCP_TOKEN_PATH")
    if override:
        return Path(override)
    return Path.home() / ".bth" / "mcp_token"


def get_or_create_token() -> str:
    """Return the local shared-secret MCP token, creating it on first use.

    Created with mode 0600 (owner read/write only) so only the local OS user
    can read it — see module docstring for why that's the relevant boundary.
    """
    path = token_path()
    if path.exists():
        existing = path.read_text().strip()
        if existing:
            return existing
    path.parent.mkdir(parents=True, exist_ok=True)
    new_token = secrets.token_hex(32)
    path.write_text(new_token)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return new_token


def check_token(supplied: str | None) -> bool:
    """Constant-time compare a supplied token against the local token file.

    Creates the token file (get_or_create_token) if it doesn't exist yet, so
    an empty/missing supplied token always fails against a real secret rather
    than against an empty expected value.
    """
    expected = get_or_create_token()
    if not supplied:
        return False
    return secrets.compare_digest(supplied, expected)
