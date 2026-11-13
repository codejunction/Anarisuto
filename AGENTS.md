# AGENTS.md

Guidance for coding agents working in this repository.

## What this is

Analyst is an evidence-first research harness on CrewAI. A question goes through three agents
in sequence: a researcher (knowledge-base search, then MCP data sources), an analyst, and a
report writer. Every tool result gets an evidence id (`E1`, `E2`, …) that the report must cite.
A FastAPI server exposes the harness and a single-page web app manages it.

## Layout

| Path | What |
|---|---|
| `analyst/harness.py` | `Harness`: config and extension loading, MCP connections, `research()`, `catalog()`, the evidence ledger (`Run`) |
| `analyst/kb.py` | Knowledge base on Chroma: chunking, file and URL ingestion, search with priority sources |
| `analyst/server.py` | HTTP API, server-sent events for runs, management endpoints |
| `analyst/static/` | Web app: `index.html`, `app.css`, `app.js` (no build step, no framework) |
| `analyst/__main__.py` | CLI: `serve`, `ingest`, `ask`, `catalog` |
| `assistants/`, `agents/`, `rules/`, `skills/` | Markdown with optional YAML frontmatter, loaded at runtime |
| `plugins/*.py` | Python with `register(harness)`, loaded at runtime |
| `examples/` | Demo MCP server (`demo_mcp.py`) and documents (`docs/`) |
| `tests/` | `test_harness.py` and a tiny MCP server used by it |
| `config.yaml` | Model, embedder, data directory, MCP sources |

## Commands

```
uv sync                                 # .venv, locked dependencies, pytest, `analyst` command
uv run analyst serve                    # http://127.0.0.1:8000
uv run analyst ingest examples/docs
uv run pytest -k scripted               # fast, needs nothing external
uv run pytest -k local                  # real crew on Ollama; several minutes
uv run python plugins/fiscal.py         # plugin self-check
```

Add dependencies with `uv add <package>` (or `uv add --dev`) so `uv.lock` stays in step.
`tests/conftest.py` turns off CrewAI tracing and telemetry for test runs.

## Conventions

- Keep it small. Prefer the standard library and the dependencies already listed in
  `pyproject.toml`. Deliberate shortcuts carry a `ponytail:` comment naming the limit.
- The web app builds DOM with the `el()` helper; strings become text nodes. Only the report
  is rendered from markdown, and it goes through DOMPurify. Do not add `innerHTML` elsewhere.
- Every colour, radius and shadow comes from the CSS custom properties at the top of
  `app.css`. Dark values exist twice (system preference and `data-theme="dark"`); change both.
- Run events are `{"type": "step", "kind": ...}` with kinds `stage`, `thought`, `say`, `call`,
  `result`, `skill`, `ask`, `reply`, `output`. They are saved with each turn and replayed.
- A broken plugin or an unreachable MCP server must be reported, never crash the harness.
- Management endpoints can run code. They stay behind the `admin` dependency in `server.py`.

## Before finishing a change

1. `uv run pytest -k scripted` passes.
2. `node --check analyst/static/app.js` passes after front-end edits.
3. Front-end changes are checked in the browser in both light and dark themes.
