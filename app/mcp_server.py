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
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

import sources

mcp = FastMCP("claude-analyzer")


def _strip(result: dict, include_timeline: bool) -> dict:
    """Drop the (large) timeline unless explicitly requested, to keep responses lean."""
    if result and not include_timeline:
        result = {k: v for k, v in result.items() if k != "timeline"}
    return result


@mcp.tool()
def list_projects() -> list[dict]:
    """List all analyzable projects across Claude Code and Codex transcripts.

    Returns one entry per project with: dir (the id to pass to other tools),
    label (the repo path), source ("claude"|"codex"), and session_count.
    """
    return sources.list_projects()


@mcp.tool()
def list_sessions(project: str, limit: int = 20) -> list[dict]:
    """List recent sessions of a project (newest first).

    `project` is the `dir` value from list_projects. Returns id, mtime_str,
    first_prompt, git_branch, cwd — use the `id` with analyze_session.
    """
    return sources.list_sessions(project)[:max(1, limit)]


@mcp.tool()
def analyze_session(project: str, session_id: str, include_timeline: bool = False) -> dict:
    """Full behavior analysis of one session.

    Returns: meta (source/cwd/model/duration), stats (tool_calls + breakdown,
    skills + skill_list, subagents, thinking, cache_hit_rate, …), cost (USD est.
    for Claude models), compliance (were injected "read/follow file X" directives
    honored — status satisfied/partial/not-satisfied/conditional with evidence),
    injected_memory (which nested CLAUDE.md/AGENTS.md loaded, from which directory),
    files, commands. Set include_timeline=true for the full chronological event
    stream (prompts, tool calls, memory injections, skill uses) — large.
    """
    result = sources.analyze(project, session_id)
    if result is None:
        return {"error": f"session not found: {project}/{session_id}"}
    return _strip(result, include_timeline)


@mcp.tool()
def analyze_latest(project: str = "", include_timeline: bool = False) -> dict:
    """Analyze the most recently active session.

    Within `project` if given, otherwise the most recent session across all
    projects. Handy for "analyze my last session". Same shape as analyze_session,
    plus the resolved project/session ids under `_resolved`.
    """
    candidates = []
    projects = [{"dir": project}] if project else sources.list_projects()
    for p in projects:
        for s in sources.list_sessions(p["dir"]):
            candidates.append((s["mtime"], p["dir"], s["id"]))
    if not candidates:
        return {"error": "no sessions found"}
    _, pdir, sid = max(candidates, key=lambda c: c[0])
    result = sources.analyze(pdir, sid)
    if result is None:
        return {"error": f"session not found: {pdir}/{sid}"}
    out = _strip(result, include_timeline)
    out["_resolved"] = {"project": pdir, "session_id": sid}
    return out


if __name__ == "__main__":
    mcp.run()
