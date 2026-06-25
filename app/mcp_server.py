#!/usr/bin/env python3
"""
MCP server exposing the transcript analysis to other Claude Code / Codex sessions.

Lets an agent introspect agent behavior: which (nested) CLAUDE.md / AGENTS.md were
injected, whether "read/follow file X" instructions were honored, which skills/tools
ran, token usage, and the chronological timeline — for any local Claude Code or Codex
session.

Run:  python app/mcp_server.py   (stdio)
Register:  claude mcp add --scope user claude-analyzer -- python /abs/path/app/mcp_server.py
Reads transcripts from ~/.claude/projects and ~/.codex/sessions (override via
PROJECTS_ROOT / CODEX_ROOT env vars).

Trust model: this is a LOCAL, single-user stdio server. Any agent session you register
it with can read the full content of all your other sessions — prompts, commands, and
the text of injected CLAUDE.md/AGENTS.md files. Don't register it if other users share
your home directory, or if you routinely paste secrets directly into prompts.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

import sources

mcp = FastMCP("claude-analyzer")

_MAX_SESSIONS = 200


def _safe(name: str) -> str | None:
    """Reject path separators / traversal in a project or session id (None = invalid)."""
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    return name


def _strip(result: dict, include_timeline: bool) -> dict:
    """Drop the (large) timeline unless explicitly requested, to keep responses lean."""
    if include_timeline:
        return result
    return {k: v for k, v in result.items() if k != "timeline"}


@mcp.tool()
def list_projects() -> list[dict]:
    """List all analyzable projects across Claude Code and Codex transcripts.

    Returns one entry per project with: dir (the id to pass to other tools),
    label (the repo path), source ("claude"|"codex"), and session_count.
    """
    return sources.list_projects()


@mcp.tool()
def list_sessions(project: str, limit: int = 20) -> list[dict] | dict:
    """List recent sessions of a project (newest first).

    `project` is the `dir` value from list_projects. Returns id, mtime_str,
    first_prompt, git_branch, cwd — use the `id` with analyze_session.
    """
    if _safe(project) is None:
        return {"error": "invalid project id"}
    return sources.list_sessions(project)[:max(0, min(limit, _MAX_SESSIONS))]


@mcp.tool()
def analyze_session(project: str, session_id: str, include_timeline: bool = False) -> dict:
    """Full behavior analysis of one session.

    Returns: meta (source/cwd/model/duration), stats (tool_calls + breakdown,
    skills + skill_list, subagents, thinking, cache_hit_rate, …), cost (USD est.
    for Claude models), compliance (were injected "read/follow file X" directives
    honored — status satisfied/partial/not-satisfied/conditional with evidence),
    injected_memory (which nested CLAUDE.md/AGENTS.md loaded, from which directory),
    files, commands. Set include_timeline=true for the full chronological event
    stream (prompts, tool calls, memory injections, skill uses) — note its text is
    CLIPPED and the stream can be large; for full untruncated content use
    get_session_text.
    """
    if _safe(project) is None or _safe(session_id) is None:
        return {"error": "invalid project or session id"}
    result = sources.analyze(project, session_id)
    if result is None:
        return {"error": f"session not found: {project}/{session_id}"}
    return _strip(result, include_timeline)


@mcp.tool()
def analyze_latest(project: str = "", cwd: str = "", include_timeline: bool = False) -> dict:
    """Analyze the most recently active session.

    Scoped to `cwd` (a repo path — e.g. your current working directory) or to
    `project` if given, otherwise the most recent session across all projects.
    Handy: analyze_latest(cwd="/path/to/repo") to introspect your own last run.
    Same shape as analyze_session, plus the resolved ids under `_resolved`.
    """
    if project and _safe(project) is None:
        return {"error": "invalid project id"}
    found = sources.latest_session(project, cwd)
    if not found:
        return {"error": "no sessions found"}
    pdir, sid = found
    result = sources.analyze(pdir, sid)
    if result is None:
        return {"error": f"session not found: {pdir}/{sid}"}
    out = _strip(result, include_timeline)
    out["_resolved"] = {"project": pdir, "session_id": sid}
    return out


@mcp.tool()
def get_session_text(project: str, session_id: str, seq: int | None = None,
                     kind: str = "", offset: int = 0, limit: int = 20) -> dict:
    """Full, UNTRUNCATED text from a session.

    The analyze_* tools clip text and omit the timeline; use this to read actual
    content (full assistant answers, full tool output, full prompts/memory).
    - seq: return just the one timeline event with this seq (its full text). Use a
      seq seen in analyze_session(include_timeline=true).
    - kind: filter to 'prompt' | 'assistant' | 'tool' | 'memory' | 'command'
      (e.g. kind='assistant' for the model's full answers). Ignored when seq is set.
    - offset / limit: paginate (limit default 20, max 50) to stay within token
      limits. Returns {total, returned, offset, next_offset, events:[...]}.
    Tip: a single very large event (e.g. a huge tool output) may still exceed the
    response limit — that's the genuine full content; narrow with seq + lower limit.
    """
    if _safe(project) is None or _safe(session_id) is None:
        return {"error": "invalid project or session id"}
    result = sources.analyze(project, session_id, full=True)
    if result is None:
        return {"error": f"session not found: {project}/{session_id}"}
    tl = result.get("timeline", [])
    if seq is not None:
        ev = next((e for e in tl if e.get("seq") == seq), None)
        return ev or {"error": f"no event with seq {seq}"}
    if kind:
        tl = [e for e in tl if e.get("kind") == kind]
    limit = max(1, min(limit, 50))
    offset = max(0, offset)
    page = tl[offset:offset + limit]
    nxt = offset + limit if offset + limit < len(tl) else None
    return {"total": len(tl), "returned": len(page), "offset": offset,
            "next_offset": nxt, "events": page}


@mcp.tool()
def compliance_summary(project: str = "", cwd: str = "", max_sessions: int = 20) -> dict:
    """Cross-session instruction compliance — the "are our AGENTS.md/CLAUDE.md rules
    actually followed over time?" view.

    Across the most recent sessions (scoped to `cwd` or `project`, else all), it
    aggregates every injected "read/follow file X" directive: how often it was
    satisfied vs partial/missing/conditional, most-violated first, with example
    session ids. Use to find guidance that agents repeatedly ignore. `max_sessions`
    is capped at 50. Returns {sessions_analyzed, directives:[...]}.
    """
    if project and _safe(project) is None:
        return {"error": "invalid project id"}
    return sources.compliance_overview(project, cwd, max_sessions)


if __name__ == "__main__":
    mcp.run()
