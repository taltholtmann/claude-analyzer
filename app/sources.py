"""
Unified discovery + routing across transcript sources (Claude Code, Codex CLI).

Both sources are presented through one project/session API. A project carries a
`source` tag; sessions and analysis are routed to the matching reader.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime

import parser as P
import codex as CX

CLAUDE_ROOT = os.environ.get("PROJECTS_ROOT", os.path.expanduser("~/.claude/projects"))
CODEX_ROOT = os.environ.get("CODEX_ROOT", os.path.expanduser("~/.codex/sessions"))
HOST_CODE = os.environ.get("HOST_CODE_DIR", "")
MOUNT_CODE = os.environ.get("MOUNT_CODE_DIR", "")

_CODEX_PREFIX = "codex--"
_CACHE_TTL = 30  # seconds — local single-user tool; covers a projects→session click


def _codex_project_id(cwd: str) -> str:
    """Collision-safe, URL-safe project id for a Codex cwd (label kept separately)."""
    return _CODEX_PREFIX + hashlib.sha1(cwd.encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# Codex rollout index (cached — _codex_rollouts is hit 3x per page load)
# --------------------------------------------------------------------------- #
_rollout_cache: tuple[float, list[dict]] | None = None


def _codex_rollouts() -> list[dict]:
    global _rollout_cache
    if _rollout_cache and time.time() - _rollout_cache[0] < _CACHE_TTL:
        return _rollout_cache[1]
    out = _scan_codex_rollouts()
    _rollout_cache = (time.time(), out)
    return out


def _scan_codex_rollouts() -> list[dict]:
    """All Codex rollouts with cwd + light metadata (reads only the file head)."""
    out = []
    if not os.path.isdir(CODEX_ROOT):
        return out
    for root, _dirs, files in os.walk(CODEX_ROOT):
        for f in files:
            if not (f.startswith("rollout-") and f.endswith(".jsonl")):
                continue
            path = os.path.join(root, f)
            cwd, prompt = "", ""
            try:
                st = os.stat(path)
                with open(path, encoding="utf-8") as fh:
                    for i, line in enumerate(fh):
                        if i > 60:
                            break
                        try:
                            o = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        p = o.get("payload", {}) or {}
                        if o.get("type") == "session_meta":
                            cwd = p.get("cwd", cwd) or cwd
                        elif not prompt and o.get("type") == "event_msg" \
                                and p.get("type") == "user_message":
                            prompt = (p.get("message") or "")[:140]
            except OSError:
                continue
            out.append({"path": path, "id": f[:-6], "cwd": cwd, "mtime": st.st_mtime,
                        "size_kb": round(st.st_size / 1024), "first_prompt": prompt})
    return out


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def list_projects() -> list[dict]:
    projects = [dict(p, source="claude") for p in P.list_projects(CLAUDE_ROOT)]
    by_cwd: dict[str, int] = {}
    for r in _codex_rollouts():
        by_cwd[r["cwd"]] = by_cwd.get(r["cwd"], 0) + 1
    for cwd, n in sorted(by_cwd.items()):
        projects.append({"dir": _codex_project_id(cwd), "label": cwd or "(unknown)",
                         "session_count": n, "source": "codex"})
    return projects


def list_sessions(project: str) -> list[dict]:
    if project.startswith(_CODEX_PREFIX):
        out = []
        for r in _codex_rollouts():
            if _codex_project_id(r["cwd"]) != project:
                continue
            out.append({
                "id": r["id"], "size_kb": r["size_kb"], "mtime": r["mtime"],
                "mtime_str": datetime.fromtimestamp(r["mtime"]).strftime("%Y-%m-%d %H:%M"),
                "first_prompt": r["first_prompt"], "git_branch": "", "cwd": r["cwd"],
            })
        out.sort(key=lambda x: x["mtime"], reverse=True)
        return out
    return P.list_sessions(CLAUDE_ROOT, project)


def analyze(project: str, session: str) -> dict | None:
    try:
        if project.startswith(_CODEX_PREFIX):
            for r in _codex_rollouts():
                if r["id"] == session and _codex_project_id(r["cwd"]) == project:
                    return CX.analyze_codex(r["path"], HOST_CODE, MOUNT_CODE)
            return None
        path = os.path.join(CLAUDE_ROOT, project, f"{session}.jsonl")
        # containment: the resolved file must stay inside CLAUDE_ROOT
        if not os.path.realpath(path).startswith(os.path.realpath(CLAUDE_ROOT) + os.sep):
            return None
        if not os.path.isfile(path):
            return None
        return P.analyze(path, HOST_CODE, MOUNT_CODE)
    except OSError:
        return None
