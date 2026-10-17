# GitHub Copilot Instructions — lore

Local-first AI chat history manager. See AGENTS.md for full developer guidelines.

## Architecture

- `lore/extractors/` — one class per AI tool, all inherit `BaseExtractor`
- `lore/interfaces/web.py` — Flask routes; data layer in `web_data.py`; jobs in `web_jobs.py`
- `lore/interfaces/web_templates.py` — all HTML/CSS/JS as Python strings (Tailwind CDN, no build step)
- `~/.lore/` — all persistent output (index.json, SQLite FTS, exported markdown)

## Critical Constraints

- Never hardcode paths: use `Path.home()` or `Path.expanduser()`
- Gunicorn must use `--workers 1` — `RELOAD_JOBS` dict is in-memory (multi-worker = 404 on job status)
- After writing to `index.json`, call `clear_index_cache()` from `web_data`
- 100-char line limit; imports: stdlib → third-party → local, alphabetical, explicit only
- No bare `except:` — catch specific exceptions

## Adding a New Extractor

1. `lore/extractors/<name>.py` — class inheriting `BaseExtractor`
2. Implement `tool` (returns `Tool` enum), `extract_sessions()` (returns `Iterator[UnifiedSession]`), `is_available()`
3. Register in `lore/extractors/factory.py`
4. Add contract test in `tests/test_extractors_contract.py`
