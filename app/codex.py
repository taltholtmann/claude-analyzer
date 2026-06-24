"""
Reader for OpenAI Codex CLI rollout transcripts (~/.codex/sessions/**/rollout-*.jsonl).

Codex uses the OpenAI Responses format. Each line is `{timestamp, type, payload}`:
  - session_meta   : payload.id / cwd / cli_version
  - turn_context   : payload.model / cwd / effort
  - event_msg      : payload.type == "token_count" carries cumulative token usage
  - response_item  : payload.type in {message, function_call, function_call_output, reasoning}

Memory model: Codex injects each AGENTS.md as a *user* message whose text starts
with `# AGENTS.md instructions for <dir>`. We treat those as injected memory — the
direct analog of Claude's nested_memory attachments — so the same compliance logic
applies. This module normalizes a rollout into the primitives `parser.finalize`
expects, producing the identical result schema.
"""
from __future__ import annotations

import json
import os
import re

import parser as P

# header that prefixes a Codex-injected AGENTS.md user message
_AGENTS_HDR = re.compile(r"^#\s*AGENTS\.md instructions for (.+?)\s*$", re.MULTILINE)
# shell programs that read a file's content (Codex commonly uses `sed -n`/`cat`)
_READ_BINS = ("cat", "sed", "head", "tail", "less", "more", "bat", "view", "nl")
_SHELL_CALLS = ("exec_command", "shell", "local_shell", "container.exec", "bash")


def analyze_codex(path: str, host_code: str = "", mount_code: str = "") -> dict:
    lines = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                try:
                    lines.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    cwd, version = "", ""
    models: set[str] = set()
    timeline: list[dict] = []
    read_ev: list[tuple] = []
    edit_ev: list[tuple] = []
    commands: list[dict] = []
    injected: list[dict] = []
    files_read: list[str] = []
    files_edited: list[str] = []
    tok = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    calls: dict[str, dict] = {}
    ts_all: list[str] = []
    api_turns = 0
    thinking_blocks = 0
    seq = 0

    for o in lines:
        typ = o.get("type")
        p = o.get("payload", {}) or {}
        ts = o.get("timestamp", "")
        if ts:
            ts_all.append(ts)

        if typ == "session_meta":
            cwd = p.get("cwd", cwd) or cwd
            version = p.get("cli_version", version) or version
            continue
        if typ == "turn_context":
            if p.get("model"):
                models.add(p["model"])
            cwd = cwd or p.get("cwd", "")
            continue
        if typ == "event_msg":
            if p.get("type") == "token_count":
                # cumulative — overwrite each time so we end on the final totals
                info = (p.get("info") or {}).get("total_token_usage") or {}
                cached = info.get("cached_input_tokens", 0) or 0
                tok["cache_read"] = cached
                tok["input"] = max(0, (info.get("input_tokens", 0) or 0) - cached)
                tok["output"] = (info.get("output_tokens", 0) or 0) + \
                                (info.get("reasoning_output_tokens", 0) or 0)
            continue
        if typ != "response_item":
            continue

        st = p.get("type")
        if st == "message":
            role = p.get("role")
            txt = " ".join(b.get("text", "") for b in p.get("content", [])
                           if isinstance(b, dict)).strip()
            if not txt:
                continue
            if role == "user":
                m = _AGENTS_HDR.match(txt)
                if m:
                    mdir = P._rel(m.group(1), cwd)
                    disp = os.path.join(mdir, "AGENTS.md") if mdir else "AGENTS.md"
                    injected.append({
                        "path": disp, "dir": mdir, "name": "AGENTS.md",
                        "mtype": "AGENTS.md", "text": txt, "chars": len(txt),
                        "ts": P._hms(ts), "directives": P._directives(txt),
                        "file_directives": P._extract_file_directives(txt, mdir),
                    })
                    seq += 1
                    timeline.append({"seq": seq, "ts": P._hms(ts), "kind": "memory",
                                     "path": disp, "mtype": "AGENTS.md", "chars": len(txt)})
                elif not txt.startswith("<"):  # skip env/instruction XML blocks
                    seq += 1
                    timeline.append({"seq": seq, "ts": P._hms(ts), "kind": "prompt",
                                     "text": P._short(txt, 600)})
            elif role == "assistant":
                seq += 1
                api_turns += 1
                timeline.append({"seq": seq, "ts": P._hms(ts), "kind": "assistant",
                                 "text": P._clip(txt, 1500)})
            # developer / system messages are injected instructions — skip
            continue

        if st == "function_call":
            seq += 1
            name = p.get("name", "?")
            try:
                args = json.loads(p.get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                args = {}
            ev = {"seq": seq, "ts": P._hms(ts), "kind": "tool",
                  "tool": name, "target": "", "detail": "", "error": None, "result": ""}
            if name in _SHELL_CALLS:
                cmd = args.get("cmd") or args.get("command") or ""
                if isinstance(cmd, list):
                    cmd = " ".join(cmd)
                ev["tool"] = "Shell"
                ev["target"] = P._short(cmd, 160)
                commands.append({"seq": seq, "ts": P._hms(ts), "command": cmd})
                rp = _read_path(cmd, cwd)
                if rp:
                    read_ev.append((seq, rp))
                    files_read.append(rp)
                # skill usage: Codex "uses" a skill by reading its SKILL.md
                sk = re.search(r"skills/([\w./-]+?)/SKILL\.md", cmd)
                if sk:
                    ev["skill"] = sk.group(1)
            elif name == "apply_patch":
                paths = _patch_paths(args, cwd)
                ev["tool"] = "apply_patch"
                ev["target"] = ", ".join(paths)
                for fp in paths:
                    edit_ev.append((seq, fp))
                    files_edited.append(fp)
            else:
                ev["target"] = P._short(json.dumps(args), 120)
            if p.get("call_id"):
                calls[p["call_id"]] = ev
            timeline.append(ev)
            continue

        if st == "function_call_output":
            ev = calls.get(p.get("call_id"))
            out = p.get("output", "")
            if isinstance(out, dict):
                out = json.dumps(out)
            if ev is not None:
                m = re.search(r"exited with code (\d+)", out or "")
                ev["error"] = bool(m and m.group(1) != "0")
                ev["result"] = P._clip(out, 2000)
            continue

        if st == "reasoning":
            thinking_blocks += 1
            continue

    return P.finalize(
        source="codex", session_id=os.path.basename(path)[:-6],
        cwd=cwd, branch="", version=version, models=models,
        timeline=timeline, read_ev=read_ev, edit_ev=edit_ev, commands=commands,
        injected=injected, tok=tok, api_turns=api_turns,
        thinking_blocks=thinking_blocks, ts_all=ts_all,
        files_read=files_read, files_edited=files_edited,
        host_code=host_code, mount_code=mount_code,
    )


def _read_path(cmd: str, cwd: str) -> str | None:
    """If a shell command reads a file (cat/sed/head/…), return its repo-relative path."""
    toks = [t.strip("'\";|&") for t in cmd.replace("|", " ").split()]
    if not any(t in _READ_BINS for t in toks):
        return None
    # last token that looks like a path/file
    for t in reversed(toks):
        if ("/" in t or "." in t) and not t.startswith("-") and "=" not in t:
            return P._rel(t, cwd)
    return None


def _patch_paths(args: dict, cwd: str) -> list[str]:
    """Extract file paths from an apply_patch payload."""
    blob = args.get("input") or args.get("patch") or json.dumps(args)
    out = []
    for m in re.finditer(r"\*\*\* (?:Add|Update|Delete) File: (.+)", blob):
        out.append(P._rel(m.group(1).strip(), cwd))
    return out or ["(patch)"]
