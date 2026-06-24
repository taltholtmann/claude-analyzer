"""
Unified discovery + routing across transcript sources (Claude Code, Codex CLI).

Both sources are presented through one project/session API. A project carries a
`source` tag; sessions and analysis are routed to the matching reader.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
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


def latest_session(project: str = "") -> tuple[str, str] | None:
    """(project_dir, session_id) of the most recently active session, or None.

    Within `project` if given, else across all projects. Cheap: list_sessions is
    already sorted newest-first, so only each project's head is compared.
    """
    best = None
    projects = [{"dir": project}] if project else list_projects()
    for p in projects:
        sess = list_sessions(p["dir"])
        if sess and (best is None or sess[0]["mtime"] > best[0]):
            best = (sess[0]["mtime"], p["dir"], sess[0]["id"])
    return (best[1], best[2]) if best else None


def analyze(project: str, session: str) -> dict | None:
    try:
        result = None
        if project.startswith(_CODEX_PREFIX):
            for r in _codex_rollouts():
                if r["id"] == session and _codex_project_id(r["cwd"]) == project:
                    result = CX.analyze_codex(r["path"], HOST_CODE, MOUNT_CODE)
                    break
        else:
            path = os.path.join(CLAUDE_ROOT, project, f"{session}.jsonl")
            # containment: the resolved file must stay inside CLAUDE_ROOT
            if os.path.realpath(path).startswith(os.path.realpath(CLAUDE_ROOT) + os.sep) \
                    and os.path.isfile(path):
                result = P.analyze(path, HOST_CODE, MOUNT_CODE)
        if result is not None:
            ic = _initial_context(result["meta"], project, result["injected_memory"])
            result["initial_context"] = ic
            if ic:
                # weave the start-of-session memory into the timeline (first), so it
                # renders consistently with the nested, on-demand memory events
                ts0 = result["meta"].get("start", "")
                intro = [{"seq": 0, "ts": ts0, "kind": "memory", "path": x["path"],
                          "mtype": x["scope"], "chars": x["chars"], "initial": True}
                         for x in ic]
                result["timeline"] = intro + result.get("timeline", [])
                result["meta"]["events"] = len(result["timeline"])
        return result
    except OSError:
        return None


def _map_cwd(path: str) -> str:
    if HOST_CODE and MOUNT_CODE and path.startswith(HOST_CODE):
        return MOUNT_CODE + path[len(HOST_CODE):]
    return path


def _read_full(real_path: str) -> str | None:
    try:
        with open(real_path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _clip(text: str, n: int = 4000) -> str:
    return text if len(text) <= n else text[:n].rstrip() + "\n…"


def _import_allowed(key: str, base_dir: str) -> bool:
    """
    Confine `@`-import targets: only within the importing file's own subtree, or
    within the memory homes (~/.claude, ~/.codex). Blocks traversal and exfiltration
    of unrelated files (`@/etc/passwd`, `@~/.ssh/id_rsa`, `@../../.env`) whose content
    would otherwise be returned over the API / MCP.
    """
    roots = [os.path.realpath(base_dir),
             os.path.realpath(os.path.dirname(CLAUDE_ROOT)),
             os.path.realpath(os.path.dirname(CODEX_ROOT))]
    return any(r and (key == r or key.startswith(r + os.sep)) for r in roots)


def _initial_context(meta: dict, project: str, injected: list) -> list[dict]:
    """
    Best-effort reconstruction of the context files loaded at session START that
    Claude Code does NOT persist in the transcript (user `~/.claude/CLAUDE.md`, the
    project auto-memory `MEMORY.md`, and the repo-root CLAUDE.md/AGENTS.md). Read
    from disk now, so it reflects current file state — flagged as reconstructed.
    Codex already persists its AGENTS.md as injected memory, so this is Claude-only.
    """
    if meta.get("source") != "claude":
        return []
    cwd = meta.get("cwd", "")
    base = _map_cwd(cwd) if cwd else ""
    home_claude = os.path.dirname(CLAUDE_ROOT)  # e.g. ~/.claude
    injected_paths = {m["path"] for m in injected}
    candidates = [
        ("User memory", "~/.claude/CLAUDE.md", os.path.join(home_claude, "CLAUDE.md"), None),
        ("Auto-memory", "MEMORY.md", os.path.join(CLAUDE_ROOT, project, "memory", "MEMORY.md"), None),
        ("Project root", "CLAUDE.md", os.path.join(base, "CLAUDE.md") if base else "", "CLAUDE.md"),
        ("Project root", "AGENTS.md", os.path.join(base, "AGENTS.md") if base else "", "AGENTS.md"),
    ]
    out, seen, pending = [], set(), []
    for scope, label, real, rel in candidates:
        if not real or (rel and rel in injected_paths):
            continue
        full = _read_full(real)
        if full is None:
            continue
        seen.add(os.path.realpath(real))
        # scan the FULL file for imports; clip only what we display, report true size
        out.append({"scope": scope, "path": label, "chars": len(full), "text": _clip(full)})
        pending.append((full, os.path.dirname(real)))
    # resolve @-imports (Claude pulls the imported file's content into context)
    for full, base_dir in pending:
        _resolve_imports(full, base_dir, seen, out)
    return out


# `@path` in a CLAUDE.md/AGENTS.md imports that file's content (recursively).
# Match only when preceded by start/space and the target looks like a path
# (has an extension or a slash) — excludes emails (foo@bar) and @mentions.
_IMPORT_RE = re.compile(r"(?:^|\s)@([^\s`]+)")


def _resolve_imports(text: str, base_dir: str, seen: set, out: list, depth: int = 0) -> None:
    if depth > 5:
        return
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for m in _IMPORT_RE.finditer(line):
            raw = m.group(1).rstrip(".,;:)]}")
            if not raw or not ("/" in raw or re.search(r"\.\w+$", raw)):
                continue  # not a file-ish import (skip @mentions / @params)
            if raw.startswith("~"):
                real = os.path.expanduser(raw)
            elif os.path.isabs(raw):
                real = _map_cwd(raw)
            else:
                real = os.path.normpath(os.path.join(base_dir, raw))
            key = os.path.realpath(real)
            if key in seen or not _import_allowed(key, base_dir):
                continue
            seen.add(key)
            full = _read_full(real)
            if full is None:
                continue
            out.append({"scope": "Import (@)", "path": raw, "chars": len(full), "text": _clip(full)})
            _resolve_imports(full, os.path.dirname(real), seen, out, depth + 1)
