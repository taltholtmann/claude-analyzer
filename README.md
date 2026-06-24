# Memory & Compliance Analyzer (Claude Code + Codex)

> ⚠️ **Draft / beta — not extensively tested.** This is an early proof-of-concept built
> in a single session. It works on the transcripts it was developed against, but it has
> no automated test suite, the transcript formats are reverse-engineered and may change,
> and the compliance heuristics are best-effort (expect false positives/negatives). Run
> it locally, read the code before trusting the output, and treat results as indicative,
> not authoritative. Issues and PRs welcome.

A local web tool with a **sharp focus**: it answers two questions that no other
transcript viewer does:

1. **Memory injection** — Which (nested) `CLAUDE.md` / `AGENTS.md` files were loaded into
   context, and from which directory? Shown as a **directory cascade** (indented by depth),
   with each file's content.
2. **Instruction compliance** — Were the "read/follow file X" instructions declared in
   injected memory files actually honored (e.g. *"Read README.md before editing this
   subtree"*, *"follow `../Core/AGENTS.md`"*)? Traffic-light status **satisfied / partial /
   not satisfied / conditional**, each with evidence.

It also reports a **session overview** (duration, API turns, tool-call breakdown with
errors, thinking blocks, subagents, **skills used**, MCP calls), **token usage** (incl.
cache hit rate) and an **estimated USD cost** for Claude models. Skill invocations are
detected per source — Claude `Skill` tool calls, and Codex reading a `skills/<name>/SKILL.md`
— and are woven into the timeline.

## Supported agents

| Agent | Transcripts | Memory file |
|---|---|---|
| **Claude Code** | `~/.claude/projects/**/*.jsonl` | nested `CLAUDE.md` / `AGENTS.md` (injected as `nested_memory` attachments) |
| **OpenAI Codex CLI** | `~/.codex/sessions/**/rollout-*.jsonl` | `AGENTS.md` (injected as `# AGENTS.md instructions for <dir>` user messages) |

Both are normalized to **one schema** and run through the **same** compliance/memory
analysis. In the UI a `codex` / `claude` badge marks the source. Cost estimation only
applies to Claude models (Codex uses OpenAI models → cost shown as n/a).

Web UI: pick a project + session by clicking.

## Scope — when to use this vs. something else

For **generic** transcript viewing (full timeline, token charts, tool-call details,
multi-provider incl. Codex/Gemini/Antigravity) mature tools already exist — most notably
**[`claude-code-history-viewer`](https://github.com/jhlee0409/claude-code-history-viewer) (CCHV)**,
plus `claude-code-trace`, `claude-code-log`, `simonw/claude-code-transcripts`.

This tool deliberately does **not** expand the generic part (it is present only as a
secondary, collapsed "Activity" section). Its unique value is the **memory/compliance
niche** — exactly what the others do not show. CCHV & co. are the recommended complement
for everything else.

> Also complementary to Arize/Phoenix tracing: Phoenix streams the live run to a collector
> but sees **no** injected memory files (only tool calls / files). This analyzer works
> directly on the transcripts and surfaces the injection itself.

## Compliance logic (short)

From every injected memory file, "read/follow `<file>`" directives are extracted
(verbs: read/follow/see/consult/review/check). Each directive is judged as:

- **satisfied** — the target file was read (Read tool **or** `cat`/`head`/… via Bash), or
  is itself loaded as memory (nested-injected / root memory). For *"before editing"*
  directives, additionally: before the first edit.
- **partial** — read only *after* the edit, or only a same-named file in a *different*
  directory was read (not the requested one).
- **conditional** — directive carries a condition (*"For PHP files, …"*); only relevant if
  the condition held — not hard-judged.
- **not satisfied** — the target file exists in the repo but was never read.

## Run (Docker)

```bash
cd ~/code/claude-analyzer
docker compose up -d --build
# open the UI:
open http://localhost:8420
```

Mounted read-only:
- `~/.claude/projects` → Claude Code transcripts
- `~/.codex/sessions` → Codex CLI transcripts
- `~/code` → for file/README existence checks (compliance)

Stop: `docker compose down`.

## Run (without Docker, local)

```bash
cd ~/code/claude-analyzer
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
PROJECTS_ROOT="$HOME/.claude/projects" \
  HOST_CODE_DIR="$HOME/code" MOUNT_CODE_DIR="$HOME/code" \
  python app/server.py
# http://localhost:8420
```

## Headless / AI consumption (no UI)

The tool is fully usable without the browser — for agents, CI, and scripts:

**HTTP JSON API** (same data the UI uses):
```bash
curl localhost:8420/api/projects
curl "localhost:8420/api/projects/<project>/sessions"
curl "localhost:8420/api/projects/<project>/sessions/<id>" | jq '.compliance, .injected_memory, .cost'
```

**CLI** — point it at any transcript file (source auto-detected), get a Markdown report
(compliance + memory first) or the full JSON:
```bash
python app/cli.py ~/.claude/projects/<proj>/<id>.jsonl            # Markdown report
python app/cli.py ~/.codex/sessions/2026/06/19/rollout-*.jsonl    # Codex works too
python app/cli.py <transcript.jsonl> --json                       # full result object
python app/cli.py --list                                          # all projects + sessions
```
The Markdown report is designed to be dropped straight into an LLM context.

## Architecture

```
app/
├── server.py              # Flask: JSON API + serves the web UI
├── sources.py             # unified project/session discovery + routing (Claude + Codex)
├── parser.py              # Claude reader + shared finalize/compliance/stats core
├── codex.py               # Codex rollout reader → normalizes to the shared schema
├── pricing.py             # token → USD cost estimate (Claude models)
├── cli.py                 # headless Markdown/JSON report
└── templates/index.html   # single-page web UI (vanilla JS, no CDN, everything inline)
Dockerfile · docker-compose.yml · requirements.txt · LICENSE
```

### API (for scripting)

| Endpoint | returns |
|---|---|
| `GET /api/projects` | all project dirs + session count |
| `GET /api/projects/<project>/sessions` | sessions of a project (newest first) |
| `GET /api/projects/<project>/sessions/<id>` | full analysis as JSON |

Example:
```bash
curl localhost:8420/api/projects
curl "localhost:8420/api/projects/<project>/sessions/<id>" | jq '.compliance, .injected_memory'
```

Response fields: `meta` (incl. `source`), `stats` (counts: api_turns, tool_calls/errors,
tool_breakdown, thinking_blocks, subagents + subagent_list, skills + skill_list, mcp_calls,
cache_hit_rate, …), `tokens`, `cost` (USD estimate or `null`), `compliance` (checked directives),
`injected_memory` (nested memory with `dir`/`text`/`file_directives`), `files`, `commands`,
`timeline`.

## Known follow-ups (from the code review)

Applied: collision-safe Codex project ids, rollout-scan caching, loopback-only
binding, path-containment checks, conditional-directive heuristic, several bug fixes.

Deferred (good first contributions, not blockers):
- Extract the shared analysis core (`finalize` / `_compliance` / directive helpers)
  out of `parser.py` into a neutral `core.py`; today `codex.py` imports `parser`'s
  underscore-prefixed helpers.
- Type the reader→`finalize` boundary with a `TypedDict`/dataclass instead of 15 kwargs.
- Move `detect_source()` into `sources.py` so a third reader (e.g. Gemini) registers
  its format in one place.
- `pricing.py` rates are hardcoded — note a "last verified" date and refresh periodically.

## How the transcript fields are interpreted

- **Tool calls:** `assistant` message → `content[]` with `type:"tool_use"` (name, input).
- **Results:** the following `user` message → `tool_result` (matched by `tool_use_id`,
  `is_error`).
- **Injected memory:** `type:"attachment"` with `attachment.type == "nested_memory"`
  (path in `displayPath`, content in `content.content`).
- **Repo path:** taken from the session's `cwd` field (not guessed from the dir name).
- **Tokens:** `message.usage` (input/output/cache_read/cache_creation).

**Codex** (`response_item` / `event_msg`): tool calls are `function_call` (shell →
`Shell`, `apply_patch` → edits); results are `function_call_output` (`exited with code N`
→ error); thinking is `reasoning`; tokens come from the cumulative `token_count` event;
injected memory is the `# AGENTS.md instructions for <dir>` user message.

> **Compliance heuristic:** a "read X" directive is treated as **conditional** (not a
> miss) when it is scoped to a context — `for …` / `when …` / `if …`, or an area marker
> like `PHP/server code:` / `Tests:` — since it only applies when that area is touched.
> Unconditional "read X before editing" directives are checked strictly.
