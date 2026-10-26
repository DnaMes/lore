# Contributing to Lore

> The product/CLI is **Lore** (`lore`), but the Python import package is
> `lore` — the import name was deliberately not renamed. Paths like
> `lore/...` below refer to the code package, not the product name.

## Development Setup

```bash
git clone https://github.com/DnaMes/lore.git
cd lore
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
```

The `dev` extra includes pytest, ruff, mypy, fastembed and sqlite-vec.

## Running Tests

```bash
# All tests (pyproject addopts includes coverage; fail_under 80)
pytest tests/

# Single file
pytest tests/test_extractors_contract.py

# Pattern match
pytest tests/ -k "opencode"

# With output
pytest tests/ -v --tb=short
```

## Lint & Format

```bash
ruff format . && ruff check --fix .
mypy lore/ --ignore-missing-imports
bandit -r lore/
```

Or run all pre-commit hooks at once:

```bash
pre-commit run --all-files
```

## Adding a New AI Tool Extractor

1. Create `lore/extractors/<toolname>.py`
2. Inherit `BaseExtractor` from `lore.extractors.base`
3. Implement:
   - `tool` property → return the correct `Tool` enum value
   - `extract_sessions()` → `Iterator[UnifiedSession]`
   - `is_available()` → check if the tool's data directory exists
4. Register in `lore/extractors/factory.py` (`get_all_extractors()` list)
5. Add the tool to `Tool` enum in `lore/core/models.py` if it's new
6. Add a contract test in `tests/test_extractors_contract.py`

See existing extractors (e.g. `claude.py`, `gemini.py`) for patterns. Use `safe_copy_db()` from `BaseExtractor` when reading SQLite files.

## Pull Request Guidelines

- One logical change per PR
- Tests must pass: `pytest tests/`
- Pre-commit hooks must pass
- Follow [Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`
- Do not add cloud dependencies — this project is local-first by design
- Do not hardcode absolute paths; use `Path.home()` / `Path.expanduser()`

## Documentation

User docs live in [README.md](README.md) and `docs/`. Start with [docs/CONFIGURATION.md](docs/CONFIGURATION.md) and [docs/CLI.md](docs/CLI.md). Keep documentation in sync with behavior in the same change.

## Releasing

The version is defined in exactly one place: `__version__` in
`lore/__init__.py` (`pyproject.toml` reads it dynamically). To cut a
release:

1. Bump `__version__` in `lore/__init__.py` (semantic versioning).
2. Move the [Unreleased] entries in CHANGELOG.md into a new [X.Y.Z] - YYYY-MM-DD section and leave an empty [Unreleased] above it.
3. Commit: `docs: changelog for X.Y.Z` together with the version bump.
4. Tag the commit: `git tag -a vX.Y.Z -m "Lore X.Y.Z — <summary>"`.
5. Push the branch and the tag: `git push <remote> main vX.Y.Z`.
6. Optionally build + publish the wheel: `python -m build`.

The web UI (sidebar footer), `lore --version`, and
`/api/build-info` all surface the version automatically.

## Architecture

See [AGENTS.md](AGENTS.md) for a detailed architecture overview.
