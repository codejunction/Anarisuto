@AGENTS.md

## Claude Code notes

- Restart the server after Python changes; static files are served with `Cache-Control: no-cache`,
  so a reload is enough for front-end edits.
- The harness writes to `config.yaml` when settings or sources change in the web app, and
  rewrites it without comments.
- `data/` holds the knowledge base and conversations. It is not committed; remove it to start fresh.
