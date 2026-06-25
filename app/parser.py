"""
Parsing and analysis logic for Claude Code session transcripts (.jsonl).

A session lives at ~/.claude/projects/<project-dir>/<session-id>.jsonl. Each line is
a JSON object with fields such as `type`, `timestamp`, `cwd`, `message`, `attachment`.
This module reads the transcript and builds a structured, chronological analysis with
a focus on injected memory files and instruction compliance.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from datetime import datetime
from typing import Any

import pricing

FILE_TOOLS = {"Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "NotebookRead"}
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
# Bash programs that display a file's *contents* (no grep/sed/awk — those
# search/transform but do not read the file for comprehension).
READ_CMDS = ("cat", "head", "tail", "less", "more", "bat", "view", "open")


# --------------------------------------------------------------------------- #
# Project / session discovery
# --------------------------------------------------------------------------- #
def list_projects(root: str) -> list[dict]:
    """All project directories under <root> with their session count."""
    out = []
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        pdir = os.path.join(root, name)
        if not os.path.isdir(pdir):
            continue
        sessions = [f for f in os.listdir(pdir) if f.endswith(".jsonl")]
        if not sessions:
            continue
        out.append({
            "dir": name,
            "label": _decode_project_dir(name),
            "session_count": len(sessions),
        })
    return out


def _decode_project_dir(name: str) -> str:
    """`-home-user-code-myrepo` -> readable hint (best effort)."""
    return name.lstrip("-").replace("-", "/")


def list_sessions(root: str, project_dir: str) -> list[dict]:
    """Sessions of a project, newest first, with short metadata."""
    pdir = os.path.join(root, project_dir)
    out = []
    if not os.path.isdir(pdir):
        return out
    for f in os.listdir(pdir):
        if not f.endswith(".jsonl"):
            continue
        path = os.path.join(pdir, f)
        try:
            st = os.stat(path)
        except OSError:
            continue
        meta = _quick_meta(path)
        out.append({
            "id": f[:-6],
            "size_kb": round(st.st_size / 1024),
            "mtime": st.st_mtime,
            "mtime_str": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
            "first_prompt": meta["first_prompt"],
            "git_branch": meta["git_branch"],
            "cwd": meta["cwd"],
        })
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def _quick_meta(path: str) -> dict:
    """First user message + cwd/branch without processing the whole file."""
    first_prompt, cwd, branch = "", "", ""
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = cwd or o.get("cwd", "")
                branch = branch or o.get("gitBranch", "")
                if not first_prompt and o.get("type") == "user":
                    txt = _user_text(o)
                    if txt and not txt.startswith("<"):
                        first_prompt = txt[:140]
                if first_prompt and cwd:
                    break
    except OSError:
        pass
    return {"first_prompt": first_prompt, "cwd": cwd, "git_branch": branch}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _user_text(o: dict) -> str:
    msg = o.get("message")
    if not isinstance(msg, dict):
        return ""
    c = msg.get("content")
    if isinstance(c, str):
        return c.strip()
    if isinstance(c, list):
        parts = [b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"]
        return " ".join(p for p in parts if p).strip()
    return ""


_CMD_RE = re.compile(r"<command-name>(.*?)</command-name>", re.DOTALL)
_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)
_SKIP_PREFIXES = ("<local-command-caveat", "<local-command-stdout",
                  "<system-reminder", "<command-message")


def _emit_prompt(text: str, full: bool = False):
    """Classify a user prompt: real prompt, slash command, or noise (None)."""
    t = (text or "").strip()
    if not t:
        return None
    mcmd = _CMD_RE.search(t)
    if mcmd:
        args = _ARGS_RE.search(t)
        label = (mcmd.group(1).strip() + " " + (args.group(1).strip() if args else "")).strip()
        return {"kind": "command", "text": "⌘ " + label}
    if t.startswith(_SKIP_PREFIXES):
        return None
    return {"kind": "prompt", "text": t if full else _short(t, 600)}


def _short(s: str, n: int = 200) -> str:
    """One-line label: newlines collapsed (for targets/commands/headlines)."""
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[:n] + "…"


def _clip(s: str, n: int = 2000) -> str:
    """Multi-line content: newlines preserved, capped (for tool results / text)."""
    s = (s or "").strip()
    return s if len(s) <= n else s[:n].rstrip() + "\n…"


def _rel(path: str, cwd: str) -> str:
    if path and cwd and path.startswith(cwd):
        return path[len(cwd):].lstrip("/")
    return path


def _hms(ts: str) -> str:
    return ts[11:19] if len(ts) >= 19 else ts


# --------------------------------------------------------------------------- #
# Main analysis
# --------------------------------------------------------------------------- #
def analyze(session_path: str, host_code: str = "", mount_code: str = "",
            full: bool = False) -> dict:
    """Full analysis of a session transcript. With full=True, timeline text fields
    are NOT clipped (used by the MCP get_session_text tool to return full content)."""
    clip = (lambda s, n=None: (s or "")) if full else _clip
    lines = []
    with open(session_path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                try:
                    lines.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    cwd = next((o.get("cwd") for o in lines if o.get("cwd")), "") or ""
    branch = next((o.get("gitBranch") for o in lines if o.get("gitBranch")), "") or ""
    version = next((o.get("version") for o in lines if o.get("version")), "") or ""

    timeline: list[dict] = []
    tool_calls: dict[str, dict] = {}          # tool_use_id -> call info
    files_read: list[str] = []
    files_edited: list[str] = []
    read_ev: list[tuple] = []                 # (seq, relpath) Read tool
    edit_ev: list[tuple] = []                 # (seq, relpath) Edit/Write
    commands: list[dict] = []
    injected: list[dict] = []
    models: set[str] = set()
    tok = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    ts_all: list[str] = []
    seq = 0
    api_turns = 0
    thinking_blocks = 0

    for o in lines:
        typ = o.get("type")
        ts = o.get("timestamp", "")
        if ts:
            ts_all.append(ts)

        # ---- injected nested memory files ---------------------------------
        if typ == "attachment":
            a = o.get("attachment", {})
            if isinstance(a, dict) and a.get("type") == "nested_memory":
                inner = a.get("content", {})
                text = inner.get("content", "") if isinstance(inner, dict) else ""
                mtype = inner.get("type", "") if isinstance(inner, dict) else ""
                disp = a.get("displayPath") or _rel(a.get("path", ""), cwd)
                mdir = os.path.dirname(disp)
                injected.append({
                    "path": disp,
                    "dir": mdir,
                    "name": os.path.basename(disp),
                    "abs": a.get("path", ""),
                    "mtype": mtype,
                    "text": text,
                    "chars": len(text),
                    "ts": _hms(ts),
                    "directives": _directives(text),
                    "file_directives": _extract_file_directives(text, mdir),
                })
                seq += 1
                timeline.append({"seq": seq, "ts": _hms(ts), "kind": "memory",
                                 "path": disp, "mtype": mtype, "chars": len(text),
                                 "text": clip(text, 4000)})
            continue

        msg = o.get("message")
        if not isinstance(msg, dict):
            continue

        # ---- token usage --------------------------------------------------
        usage = msg.get("usage")
        if isinstance(usage, dict):
            tok["input"] += usage.get("input_tokens", 0) or 0
            tok["output"] += usage.get("output_tokens", 0) or 0
            tok["cache_read"] += usage.get("cache_read_input_tokens", 0) or 0
            tok["cache_write"] += usage.get("cache_creation_input_tokens", 0) or 0
        if msg.get("model"):
            models.add(msg["model"])

        content = msg.get("content")

        # ---- user prompt / tool results -----------------------------------
        if typ == "user":
            if isinstance(content, str) and content.strip():
                pe = _emit_prompt(content, full)
                if pe:
                    seq += 1
                    timeline.append({"seq": seq, "ts": _hms(ts), **pe})
            elif isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text" and b.get("text", "").strip():
                        pe = _emit_prompt(b["text"], full)
                        if pe:
                            seq += 1
                            timeline.append({"seq": seq, "ts": _hms(ts), **pe})
                    elif b.get("type") == "tool_result":
                        tid = b.get("tool_use_id")
                        is_err = bool(b.get("is_error"))
                        out_txt = _result_text(b.get("content"))
                        call = tool_calls.get(tid)
                        if call is not None:
                            call["error"] = is_err
                            call["result"] = clip(out_txt, 2000)
            continue

        # ---- assistant: text / thinking / tool use ------------------------
        if typ == "assistant" and isinstance(content, list):
            api_turns += 1
            for b in content:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt in ("thinking", "redacted_thinking"):
                    thinking_blocks += 1
                if bt == "text" and b.get("text", "").strip():
                    seq += 1
                    timeline.append({"seq": seq, "ts": _hms(ts), "kind": "assistant",
                                     "text": clip(b["text"], 1500)})
                elif bt == "tool_use":
                    seq += 1
                    name = b.get("name", "?")
                    inp = b.get("input", {}) or {}
                    target, detail = _tool_target(name, inp, cwd)
                    ev = {"seq": seq, "ts": _hms(ts), "kind": "tool",
                          "tool": name, "target": target, "detail": detail,
                          "error": None, "result": ""}
                    if name == "Skill":  # skill invocation — input.skill holds the name
                        sk = inp.get("skill") or inp.get("command") or ""
                        if sk:
                            ev["skill"] = sk
                            ev["target"] = sk
                    timeline.append(ev)
                    if b.get("id"):
                        tool_calls[b["id"]] = ev

                    # aggregations
                    fp = inp.get("file_path") or inp.get("notebook_path")
                    if name in FILE_TOOLS and fp:
                        rel = _rel(fp, cwd)
                        if name in EDIT_TOOLS:
                            files_edited.append(rel)
                            edit_ev.append((seq, rel))
                        else:
                            files_read.append(rel)
                            read_ev.append((seq, rel))
                    if name == "Bash" and inp.get("command"):
                        commands.append({"seq": seq, "ts": _hms(ts),
                                         "command": inp["command"]})
            continue

    return finalize(
        source="claude", session_id=os.path.basename(session_path)[:-6],
        cwd=cwd, branch=branch, version=version, models=models,
        timeline=timeline, read_ev=read_ev, edit_ev=edit_ev, commands=commands,
        injected=injected, tok=tok, api_turns=api_turns,
        thinking_blocks=thinking_blocks, ts_all=ts_all,
        files_read=files_read, files_edited=files_edited,
        host_code=host_code, mount_code=mount_code,
    )


def finalize(*, source: str, session_id: str, cwd: str, branch: str, version: str,
             models: set[str], timeline: list[dict],
             read_ev: list[tuple[int, str]], edit_ev: list[tuple[int, str]],
             commands: list[dict], injected: list[dict], tok: dict[str, int],
             api_turns: int, thinking_blocks: int, ts_all: list[str],
             files_read: list[str], files_edited: list[str],
             host_code: str, mount_code: str) -> dict:
    """Build the canonical result dict from collected primitives.

    Shared by the Claude reader (above) and the Codex reader (codex.py) so both
    produce the exact same schema, compliance logic, and stats.
    """
    bash_ev = [(c["seq"], c["command"]) for c in commands]
    compliance = _compliance(injected, read_ev, bash_ev, edit_ev,
                             cwd, host_code, mount_code)

    tool_counts: Counter = Counter()
    tool_errors: Counter = Counter()
    for e in timeline:
        if e["kind"] == "tool":
            tool_counts[e["tool"]] += 1
            if e.get("error"):
                tool_errors[e["tool"]] += 1
    tool_breakdown = [
        {"tool": name, "count": cnt, "errors": tool_errors.get(name, 0)}
        for name, cnt in tool_counts.most_common()
    ]
    subagents = [
        {"label": e.get("target", ""), "type": e.get("detail", "")}
        for e in timeline if e["kind"] == "tool" and e["tool"] == "Task"
    ]
    skill_counts = Counter(e["skill"] for e in timeline if e.get("skill"))
    skill_list = [{"name": n, "count": c} for n, c in skill_counts.most_common()]
    total_input = tok["input"] + tok["cache_read"] + tok["cache_write"]
    stats = {
        "prompts": sum(1 for e in timeline if e["kind"] == "prompt"),
        "slash_commands": sum(1 for e in timeline if e["kind"] == "command"),
        "assistant_msgs": sum(1 for e in timeline if e["kind"] == "assistant"),
        "api_turns": api_turns,
        "thinking_blocks": thinking_blocks,
        "tool_calls": sum(tool_counts.values()),
        "tool_errors": sum(tool_errors.values()),
        "tool_breakdown": tool_breakdown,
        "bash_commands": len(commands),
        "subagents": len(subagents),
        "subagent_list": subagents,
        "skills": sum(skill_counts.values()),
        "skill_list": skill_list,
        "mcp_calls": sum(v for k, v in tool_counts.items() if k.startswith("mcp__")),
        "files_read": len(_counted(files_read)),
        "files_edited": len(_counted(files_edited)),
        "memory_files": len(injected),
        "total_input_tokens": total_input,
        "cache_hit_rate": round(tok["cache_read"] / total_input, 3) if total_input else 0,
    }

    return {
        "meta": {
            "source": source,
            "id": session_id,
            "cwd": cwd,
            "git_branch": branch,
            "version": version,
            "models": sorted(models),
            "start": _hms(ts_all[0]) if ts_all else "",
            "end": _hms(ts_all[-1]) if ts_all else "",
            "duration": _duration(ts_all),
            "events": len(timeline),
        },
        "tokens": tok,
        "cost": pricing.estimate_cost(tok, sorted(models)),
        "stats": stats,
        "timeline": timeline,
        "files": {"read": _counted(files_read), "edited": _counted(files_edited)},
        "commands": commands,
        "injected_memory": injected,
        "compliance": compliance,
    }


def _tool_target(name: str, inp: dict, cwd: str) -> tuple[str, str]:
    """(short target, detail) per tool, for the timeline view."""
    if name == "Bash":
        return _short(inp.get("command", ""), 160), inp.get("description", "")
    for key in ("file_path", "notebook_path", "path"):
        if inp.get(key):
            return _rel(inp[key], cwd), ""
    if name in ("Grep", "Glob"):
        return inp.get("pattern", ""), inp.get("path", "")
    if name == "Task":
        return inp.get("description", ""), inp.get("subagent_type", "")
    if name == "TodoWrite":
        todos = inp.get("todos", [])
        return f"{len(todos)} todos", ""
    if name.startswith("mcp__"):
        return name.split("__", 2)[-1], ""
    # fallback: first string argument
    for v in inp.values():
        if isinstance(v, str) and v:
            return _short(v, 120), ""
    return "", ""


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif isinstance(b, str):
                parts.append(b)
        return " ".join(parts)
    return ""


def _counted(items: list[str]) -> list[dict]:
    seen: dict[str, int] = {}
    order: list[str] = []
    for it in items:
        if it not in seen:
            seen[it] = 0
            order.append(it)
        seen[it] += 1
    return [{"path": p, "count": seen[p]} for p in order]


def _duration(ts_all: list[str]) -> str:
    if len(ts_all) < 2:
        return ""
    try:
        a = datetime.fromisoformat(ts_all[0].replace("Z", "+00:00"))
        b = datetime.fromisoformat(ts_all[-1].replace("Z", "+00:00"))
        secs = int((b - a).total_seconds())
        m, s = divmod(secs, 60)
        return f"{m}m {s}s" if m else f"{s}s"
    except ValueError:
        return ""


# --------------------------------------------------------------------------- #
# Directive extraction & compliance
# --------------------------------------------------------------------------- #
_DIRECTIVE_RE = re.compile(
    r"^\s*[-*]?\s*((?:Always|Never|Read|Follow|Use|Run|Check|Do not|Don't|Avoid|Ensure|Prefer|Make sure)\b.*)",
    re.IGNORECASE,
)


def _directives(text: str) -> list[str]:
    """Extract imperative instruction lines from a memory file (display only)."""
    out = []
    for line in (text or "").splitlines():
        m = _DIRECTIVE_RE.match(line)
        if m:
            out.append(m.group(1).strip())
    return out[:12]


# --- generalized "read/follow file X" directives --------------------------- #
_READ_VERB = re.compile(
    r"\b(read|follow|see|consult|refer to|review|check)\b", re.IGNORECASE)
_FILE_TOKEN = re.compile(
    r"`([^`]+)`|([A-Za-z0-9_./\-]+\.(?:md|markdown|txt|ya?ml|json))", re.IGNORECASE)
_BARE = re.compile(r"\b(README|AGENTS|CLAUDE)\b")


def _extract_file_directives(text: str, mem_dir: str) -> list[dict]:
    """
    Directives of the form "Read/Follow/See <file> ..." from a memory file.
    Returns checkable directives with the target path resolved relative to the repo.
    """
    out, seen = [], set()
    for line in (text or "").splitlines():
        if not _READ_VERB.search(line):
            continue
        low = line.lower()
        tokens = []
        for m in _FILE_TOKEN.finditer(line):
            tok = (m.group(1) or m.group(2) or "").strip().strip("`")
            if tok and "." in os.path.basename(tok):
                tokens.append(tok)
        if not tokens:  # bare README/AGENTS/CLAUDE without extension
            tokens = [b + ".md" for b in _BARE.findall(line)]
        for tok in tokens:
            target = _resolve_target(tok, mem_dir, low)
            if target in seen:
                continue
            seen.add(target)
            out.append({
                "raw": line.strip()[:160],
                "target": target,
                "name": os.path.basename(target),
                "before_edit": ("before" in low and "edit" in low) or "first" in low,
                "conditional": _is_conditional(low),
            })
    return out


# a directive is conditional when it only applies to a context (a file type, an
# area of the codebase, an explicit "for/when/if") rather than unconditionally.
# anchored to the start of the line so a mid-sentence colon (e.g. "... server: ...")
# doesn't falsely mark an unconditional directive as conditional.
_COND_RE = re.compile(
    r"^\s*[-*]?\s*("
    r"(for |when |if )|"
    r"[\w/+ ]*\b(code|files?|tests?|frontend|backend|storefront|administration|server|client)\b\s*:"
    r")",
    re.IGNORECASE,
)


def _is_conditional(line_low: str) -> bool:
    return bool(_COND_RE.search(line_low))


def _resolve_target(tok: str, mem_dir: str, line_low: str) -> str:
    tok = tok.strip().strip("`./")
    if "root" in line_low and "/" not in tok:
        return os.path.basename(tok)          # repo root
    return os.path.normpath(os.path.join(mem_dir, tok)) if mem_dir else tok


def _compliance(injected, reads, bashes, edits,
                cwd, host_code, mount_code) -> list[dict]:
    """
    For each injected memory file, check its "read file X" directives against actual
    behavior: X counts as satisfied if X was read (Read tool) or opened via Bash
    (cat/head/...), or is itself an injected memory file. `before_edit` directives
    additionally require: before the first edit.
    """
    injected_paths = {m["path"] for m in injected}
    first_edit = edits[0][0] if edits else None
    results = []
    seen = set()

    for m in injected:
        for fd in m["file_directives"]:
            key = (m["path"], fd["target"])
            if key in seen:
                continue
            seen.add(key)
            tgt, name = fd["target"], fd["name"]

            # 1) already in context (injected as memory / root memory)?
            loaded, why = _loaded_memory(tgt, injected_paths)
            if loaded:
                results.append(_mk(m, fd, "ok", why))
                continue

            # 2) conditional directive ("For PHP files, follow ...") — not hard-checkable
            if fd["conditional"]:
                results.append(_mk(m, fd, "conditional",
                                   'conditional ("for/when ...") — only relevant if the condition held'))
                continue

            # 3) this exact file read (Read tool or cat/head/...)?
            read_seq, how = _find_read(tgt, name, reads, bashes)
            if read_seq is not None:
                if fd["before_edit"] and first_edit is not None and read_seq > first_edit:
                    results.append(_mk(m, fd, "partial", f"read only AFTER the first edit ({how})"))
                else:
                    results.append(_mk(m, fd, "ok", how))
                continue

            # 4) not read — does the target exist & are there related reads?
            on_disk = _exists(tgt, cwd, host_code, mount_code)
            related = _find_related(name, reads, bashes)
            if related:
                results.append(_mk(m, fd, "partial",
                                   f"same-named file read elsewhere ({related}), but not this one"))
            elif on_disk:
                results.append(_mk(m, fd, "missing", f"{tgt} exists but was never read"))
            else:
                results.append(_mk(m, fd, "na", f"{tgt} not found in repo"))
    return results


def _mk(m, fd, status, note):
    return {"memory_file": m["path"], "directive": fd["raw"],
            "target": fd["target"], "before_edit": fd["before_edit"],
            "status": status, "note": note}


def _loaded_memory(tgt, injected_paths):
    """Memory file that is in context anyway: nested-injected or root memory."""
    if tgt in injected_paths:
        return True, f"injected as memory ({tgt})"
    if os.path.basename(tgt) in ("AGENTS.md", "CLAUDE.md") and "/" not in tgt:
        return True, "root memory (loaded automatically)"
    return False, ""


def _bash_reads(c: str, name: str, tgt: str | None) -> bool:
    """
    True if the Bash command displays the file via cat/head/... (token-exact).
    With `tgt` (relative path) the exact path is required; without `tgt` the
    file name alone is enough (used for "same-named file elsewhere").
    """
    toks = [t.strip("'\"") for t in c.replace("|", " ").replace(";", " ").replace("&", " ").split()]
    if not any(t in READ_CMDS for t in toks):
        return False
    for t in toks:
        if tgt:
            # exact, or the path ends at a separator before the target (avoid
            # "eslint-config.json".endswith("config.json") false positives)
            if t == tgt or t.endswith("/" + tgt):
                return True
        elif t == name or t.endswith("/" + name):
            return True
    return False


def _find_read(tgt, name, reads, bashes):
    for s, p in reads:
        if p == tgt or (os.path.basename(p) == name and os.path.dirname(p) == os.path.dirname(tgt)):
            return s, f"Read {p}"
    for s, c in bashes:
        if _bash_reads(c, name, tgt):
            return s, f"Bash: {_short(c, 70)}"
    return None, ""


def _find_related(name, reads, bashes):
    for _, p in reads:
        if os.path.basename(p) == name:
            return p
    for _, c in bashes:
        if _bash_reads(c, name, None):
            return _short(c, 60)
    return ""


def _exists(relpath, cwd, host_code, mount_code) -> bool:
    base = cwd
    if host_code and mount_code and cwd.startswith(host_code):
        base = mount_code + cwd[len(host_code):]
    # `relpath` and `cwd` come from transcript content — confine the probe to
    # the repo so a crafted "../../etc/passwd" can't be used as a file oracle.
    full = os.path.realpath(os.path.join(base, relpath))
    if not full.startswith(os.path.realpath(base) + os.sep):
        return False
    return os.path.isfile(full)
