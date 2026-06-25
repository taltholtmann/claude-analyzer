#!/usr/bin/env python3
"""
Headless report for a Claude Code or Codex transcript — for agents and pipelines
that consume the analysis without the web UI.

Usage:
  python cli.py <transcript.jsonl> [--json]   # analyze one file (source auto-detected)
  python cli.py --list                        # list all projects + sessions (JSON)

Default output is Markdown (overview + memory first); --json emits the full
result object (the same schema the HTTP API returns).
"""
from __future__ import annotations

import json
import os
import sys

import parser as P
import codex as CX
import sources


def detect_source(path: str) -> str:
    """Sniff the first lines: Codex rollouts carry session_meta/response_item."""
    with open(path, encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if i > 5:
                break
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = obj.get("type", "") if isinstance(obj, dict) else ""
            if t in ("session_meta", "response_item", "turn_context", "event_msg"):
                return "codex"
            if t in ("user", "assistant", "attachment", "system"):
                return "claude"
    return "claude"


def analyze_file(path: str) -> dict:
    # on-host run: transcript cwd == real path, so no host/mount mapping needed
    if detect_source(path) == "codex":
        return CX.analyze_codex(path)
    result = P.analyze(path)
    # attach reconstructed initial context, like sources.analyze() does for the API
    project = os.path.basename(os.path.dirname(path))
    result["initial_context"] = sources._initial_context(
        result["meta"], project, result["injected_memory"])
    return result


def to_markdown(d: dict) -> str:
    m, s, t = d["meta"], d["stats"], d["tokens"]
    L = []
    L.append(f"# Session {m['id']}  ({m['source']})")
    L.append(f"- cwd: `{m['cwd']}`" + (f" · branch: `{m['git_branch']}`" if m['git_branch'] else ""))
    L.append(f"- model: {', '.join(m['models']) or '—'} · {m['start']}–{m['end']} ({m['duration'] or '–'})")
    if d.get("cost"):
        L.append(f"- est. cost: **${d['cost']['usd']}** ({d['cost']['note']})")
    L.append("")
    L.append("## Overview")
    L.append(f"- api_turns={s['api_turns']} · tool_calls={s['tool_calls']} "
             f"(errors={s['tool_errors']}) · thinking={s['thinking_blocks']} · "
             f"subagents={s['subagents']} · skills={s.get('skills', 0)} · mcp_calls={s['mcp_calls']}")
    L.append(f"- files read={s['files_read']} · edited={s['files_edited']} · "
             f"bash={s['bash_commands']} · memory_files={s['memory_files']}")
    L.append(f"- tokens: output={t['output']:,} input={t['input']:,} "
             f"cache_read={t['cache_read']:,} cache_write={t['cache_write']:,} "
             f"(cache hit {round(s['cache_hit_rate']*100)}%)")
    breakdown = ", ".join(
        f"{b['tool']}×{b['count']}" + (f"(⚠{b['errors']})" if b['errors'] else "")
        for b in s["tool_breakdown"]) or "—"
    L.append(f"- tool breakdown: {breakdown}")
    L.append("")

    if d.get("initial_context"):
        L.append("## Initial context (loaded at session start, reconstructed from disk)")
        for ic in d["initial_context"]:
            L.append(f"- [{ic['scope']}] `{ic['path']}` ({ic['chars']} chars)")
        L.append("")

    L.append("## Injected memory (directory cascade)")
    if not d["injected_memory"]:
        L.append("_No nested CLAUDE.md/AGENTS.md injected._")
    else:
        for mem in sorted(d["injected_memory"], key=lambda x: x["path"]):
            L.append(f"- `{mem['path']}` ({mem['mtype']}, {mem['chars']} chars)")
    L.append("")

    if s.get("skill_list"):
        L.append("## Skills used")
        for sk in s["skill_list"]:
            L.append(f"- {sk['name']}" + (f" ×{sk['count']}" if sk['count'] > 1 else ""))
        L.append("")
    if s["subagent_list"]:
        L.append("## Subagents")
        for sa in s["subagent_list"]:
            L.append(f"- {sa['label']}" + (f" ({sa['type']})" if sa['type'] else ""))
        L.append("")
    return "\n".join(L)


def main() -> None:
    args = sys.argv[1:]
    if "--list" in args:
        out = []
        for proj in sources.list_projects():
            out.append({**proj, "sessions": [s["id"] for s in sources.list_sessions(proj["dir"])]})
        print(json.dumps(out, indent=2))
        return
    if not args:
        print(__doc__)
        sys.exit(1)
    path = args[0]
    if not os.path.isfile(path):
        sys.exit(f"not a file: {path}")
    try:
        result = analyze_file(path)
    except OSError as e:
        sys.exit(f"cannot read {path}: {e}")
    if "--json" in args:
        print(json.dumps(result, indent=2))
    else:
        print(to_markdown(result))


if __name__ == "__main__":
    main()
