# Analyst

A research harness on CrewAI that answers data questions with evidence. A researcher agent looks
up what the data means in a knowledge base, queries your sources over MCP, then an analyst and a
report writer turn the results into a cited report. Every tool result gets an id (`[E3]`) and the
report must cite those ids; citations that match no result are flagged.

## Run

```
uv venv --python 3.12 && uv pip install -e ".[dev,local]"
ollama pull qwen3:4b && ollama pull embeddinggemma:300m   # or set a hosted model, see below
python -m analyst ingest examples/docs   # the example documents
python -m analyst serve                  # http://127.0.0.1:8000
python -m analyst ask "Which region grew fastest last quarter?"
python -m analyst catalog demo_sales     # an agent explores a source and writes notes about it
pytest -k scripted                       # end-to-end test, scripted model, needs nothing
```

## Examples

The repository ships a complete worked setup around a made-up sales database, so you can see
how the same question comes out differently depending on the assistant, skills and documents.

| Section | Examples |
|---|---|
| Source | `demo_sales` (`examples/demo_mcp.py`): customers, products, orders, targets; read-only |
| Documents | `examples/docs/`: data dictionary, metric definitions, fiscal calendar, known data issues, regional teams |
| Assistants | `sales` (full report), `finance_brief` (brief), `ops_table` (table), `default` |
| Skills | `period_comparison`, `target_attainment`, `data_quality_check` |
| Rules | `definitions_first`, `money_and_periods` |
| Plugins | `fiscal` (`fiscal_period` tool: dates to fiscal quarters and back), `example` (`today`) |
| Sub-agent | `sql_specialist` |

The data has traps the documents explain: east refunds before 1 July 2025 are missing, west
opened on 1 October 2025, the fiscal year starts in April, and "revenue" means net of refunds.
Questions to try with each assistant:

- What was revenue by region last quarter?
- Which regions hit their target in FY2027-Q1?
- How did the refund rate change between FY2026-Q1 and FY2027-Q1?
- Which product category grew fastest year over year?

## Managing it in the browser

Everything below can be added, edited and removed in the web app; it writes the same files.

| Page | What | Stored in |
|---|---|---|
| Sources | MCP servers (local command or remote URL), tool allowlist, reconnect, catalog | `mcp:` in `config.yaml` |
| Assistants | name, description, domain, answer format (report, brief, table), instructions, sources it may query, reference documents searched first | `assistants/*.md` |
| Library: Sub-agents | specialists the researcher delegates to | `agents/*.md` |
| Library: Rules | always-on instructions for every agent | `rules/*.md` |
| Library: Skills | instructions the researcher loads on demand | `skills/*.md` |
| Library: Plugins | `register(harness)`: tools, file loaders, sources | `plugins/*.py` |
| Documents | upload files, add a web page, remove | `data/` |
| Settings | model and endpoint | `config.yaml` |

During a question the researcher can stop and ask you something; the question appears in the
conversation with a reply box.

Each turn shows its working notes beside the report: every stage, the model's reasoning text
(when the endpoint returns it, as Ollama does for qwen3), each tool call with its input and
result, and what each stage handed to the next. They are saved with the conversation.

Management endpoints can run code, so they answer only requests from the same machine. To manage
from elsewhere set `ANALYST_ADMIN_TOKEN` on the server and enter it under Settings. Behind a
reverse proxy every request looks local, so set the token there.

## Local models

`config.yaml` ships pointing at Ollama: `llm: openai/qwen3:4b` with
`llm_base_url: http://localhost:11434/v1`, and `embed_model: embeddinggemma:300m`. A question
takes several minutes on a 4B model. For a hosted model set `llm: anthropic/claude-opus-5-5`
(with `ANTHROPIC_API_KEY`) and remove `llm_base_url`, here or under Settings. Remove
`embed_model` to use Chroma's built-in embedder; each embedder has its own collection, so
switching means ingesting again. `pytest -k local` runs the real crew on Ollama.

An MCP source with `command: python` runs with the interpreter that runs the harness.

## Ingestion

- **Documents**: `.pdf` and any text file (markdown, SQL, CSV, JSON). Upload in the chat page or use
  `ingest`. Re-ingesting a file replaces its old chunks. Add formats with a plugin loader.
- **Sources**: `catalog <server>` saves one note per table, collection, report or page, so later
  questions can find the right object with `kb_search`.

## Limits

- Read-only is enforced by a keyword check on SQL-looking arguments and by the `tools:` allowlist.
  Both are easy to get around. Give the MCP servers read-only credentials.
- One model for all agents unless a sub-agent sets `llm:`.
- The chat endpoints have no authentication. Keep the server on localhost or behind your own proxy.
