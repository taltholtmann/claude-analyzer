# 🔍 Memory & Compliance Analyzer

**See what your coding agent actually did — which `CLAUDE.md` / `AGENTS.md` it loaded, and whether it followed them.**

A local tool that reads **Claude Code** and **OpenAI Codex** session transcripts and answers the questions other transcript viewers don't:

- 🧠 **Memory injection** — which (nested) `CLAUDE.md` / `AGENTS.md` were loaded into context, *from which directory*, and *at what point* in the run.
- ✅ **Instruction compliance** — were the “read/follow file X” rules in those files actually honored? (e.g. *“Read README.md before editing this subtree”*) — with a traffic-light verdict and evidence.
- ⏱️ **A real chronological timeline** — prompts, tool calls, shell commands, **skill** uses, and memory injections woven together in the order they happened, with collapsible output.
- 📊 **Session overview** — tokens (incl. cache-hit rate), estimated USD cost, tool-call breakdown, thinking blocks, subagents, skills.

Use it three ways: a **web UI**, an **MCP server** (so any agent session can analyze itself), or a **CLI** (Markdown/JSON for pipelines).

> ⚠️ **Draft / beta — not extensively tested.** Built in a few sessions as a proof of concept. No automated test suite; transcript formats are reverse-engineered and may change; compliance heuristics are best-effort (expect occasional false positives/negatives). Run it locally, skim the code, and treat results as indicative — not authoritative. Issues & PRs welcome.

---

## 🚀 Install — just ask your agent

The fastest way: paste this into **Claude Code** (or Codex) and let it do the setup.

```
Install the claude-analyzer tool for me.
1. Clone https://github.com/taltholtmann/claude-analyzer into ~/code (or my usual code dir).
2. Read its README.
3. Create a Python venv and install requirements.
4. Register its MCP server with Claude Code at user scope so any session can use it.
5. Optionally start the web UI via docker compose and tell me the URL.
Report back what you set up and how I use it.
```

That’s it — the agent has everything it needs in this README to finish the setup.

---

## 🛠️ Manual install

```bash
git clone https://github.com/taltholtmann/claude-analyzer.git ~/code/claude-analyzer
cd ~/code/claude-analyzer
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Then pick the surface(s) you want below.

---

## 1. Web UI

```bash
docker compose up -d --build      # → http://localhost:8420
# stop: docker compose down
```

Pick a project + session in the sidebar (sorted by last activity) → the timeline, compliance, and overview render instantly. The URL keeps your selection, so a refresh stays put and links are shareable.

Mounted **read-only**: `~/.claude/projects`, `~/.codex/sessions`, and `~/code` (the last is used to check whether referenced files exist on disk).

> If your repos don’t live under `~/code`, edit the `${HOME}/code` volume + `HOST_CODE_DIR` in `docker-compose.yml` to point at your code root — otherwise compliance can’t find the referenced files and reports them as `n/a`. (The CLI and MCP server use real paths and aren’t affected.)

No Docker? Run it directly:
```bash
.venv/bin/python app/server.py   # http://localhost:8420
```

## 2. MCP server — let any agent session analyze itself

Exposes the analysis as tools so a running agent can introspect its own (or any past) session live — *“did I follow the AGENTS.md instructions in my last session?”*

```bash
claude mcp add --scope user claude-analyzer -- \
  ~/code/claude-analyzer/.venv/bin/python ~/code/claude-analyzer/app/mcp_server.py
```

Tools: `list_projects`, `list_sessions`, `analyze_session`, `analyze_latest`. They return meta, stats (skills/tools/subagents/tokens), cost, **compliance**, and **injected_memory**; the large timeline is omitted unless `include_timeline=true`. Verify with `claude mcp list` or `/mcp`.

Equivalent JSON (`.mcp.json`, project scope):
```json
{ "mcpServers": { "claude-analyzer": {
  "command": "/abs/path/claude-analyzer/.venv/bin/python",
  "args": ["/abs/path/claude-analyzer/app/mcp_server.py"]
}}}
```

> **Trust model:** local single-user stdio server. Any session you register it with can read the content of all your other sessions (prompts, commands, injected memory). Fine for introspecting your own work; don’t register it on a shared home directory.

## 3. CLI — headless report

```bash
.venv/bin/python app/cli.py <transcript.jsonl>          # Markdown report (source auto-detected)
.venv/bin/python app/cli.py <transcript.jsonl> --json   # full result object
.venv/bin/python app/cli.py --list                      # all projects + sessions
```

The Markdown report leads with compliance + memory and is built to drop straight into an LLM context. Also available over HTTP while the web UI runs: `curl localhost:8420/api/projects/<project>/sessions/<id> | jq .compliance`.

---

## What it detects, per agent

| | Claude Code | OpenAI Codex |
|---|---|---|
| Transcripts | `~/.claude/projects/**/*.jsonl` | `~/.codex/sessions/**/rollout-*.jsonl` |
| Memory files | nested `CLAUDE.md` / `AGENTS.md` (injected `nested_memory` attachments) | `AGENTS.md` (injected `# AGENTS.md instructions for <dir>` messages) |
| Skill use | `Skill` tool calls | reading a `skills/<name>/SKILL.md` |
| Cost estimate | ✅ (Claude model pricing) | — (OpenAI models → shown as n/a) |

Both sources are normalized to **one schema** and run through the **same** analysis. A `claude` / `codex` badge marks the source.

## How compliance is judged

From each injected memory file, “read/follow `<file>`” directives are extracted (verbs: read/follow/see/consult/review/check). Each is rated:

- **satisfied** — the target was read (Read tool **or** `cat`/`head`/… via shell), or is itself loaded as memory. For *“before editing”* directives, additionally: before the first edit.
- **partial** — read only *after* the edit, or only a same-named file in a *different* directory was read.
- **conditional** — scoped to a context (*“For PHP files, …”*, *“Tests:”*); only relevant if that area was touched — not hard-judged.
- **not satisfied** — the target exists in the repo but was never read.

## Scope — and what this is *not*

For **generic** transcript viewing (full timeline, token charts, multi-provider) mature tools already exist — e.g. [`claude-code-history-viewer`](https://github.com/jhlee0409/claude-code-history-viewer). This tool deliberately stays in the **memory + compliance niche** they don’t cover; that generic view is here only as a secondary, collapsed “Raw details” section.

---

## Architecture

```
app/
├── server.py              # Flask: JSON API + serves the web UI
├── mcp_server.py          # MCP (stdio) server — same analysis as tools for other sessions
├── sources.py             # unified project/session discovery + routing (Claude + Codex)
├── parser.py              # Claude reader + shared finalize/compliance/stats core
├── codex.py               # Codex rollout reader → normalizes to the shared schema
├── pricing.py             # token → USD cost estimate (Claude models)
├── cli.py                 # headless Markdown/JSON report
└── templates/index.html   # single-page web UI (vanilla JS, no CDN, everything inline)
Dockerfile · docker-compose.yml · requirements.txt · LICENSE
```

Stack: Python stdlib + Flask + the `mcp` SDK, vanilla JS, no build step. Adding a third agent = one new reader module that normalizes into the shared schema via `parser.finalize()`.

### HTTP API

| Endpoint | returns |
|---|---|
| `GET /api/projects` | all projects + session counts |
| `GET /api/projects/<project>/sessions` | sessions (newest first) |
| `GET /api/projects/<project>/sessions/<id>` | full analysis JSON |

## Known follow-ups

Good first contributions, not blockers:
- Extract the shared core (`finalize` / `_compliance` / directive helpers) from `parser.py` into a neutral `core.py`.
- Type the reader→`finalize` boundary with a `TypedDict`.
- Add a third reader (e.g. Gemini CLI).
- Codex skill detection is heuristic (SKILL.md reads); Claude’s is exact (Skill tool).
- `pricing.py` rates are hardcoded — refresh periodically.

## License

MIT — see [LICENSE](LICENSE).
