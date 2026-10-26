# Install

Installation guide and package options for Lore.

## Requirements

- Python: Python 3.11 or newer (specified by `requires-python = ">=3.11"` in `pyproject.toml`).
- Operating System:
  - Linux: Verified by the maintainer.
  - macOS: NOT VERIFIED.
  - Windows: NOT VERIFIED.

## Package Distribution

The package name `lore` on PyPI is registered to an unrelated machine learning framework (see [PyPI lore](https://pypi.org/project/lore/)). Because of this namespace collision, Lore must be installed directly from its Git repository rather than from PyPI.

### Recommended: pipx

Using `pipx` installs Lore into an isolated environment and exposes its command-line entry points:

```bash
pipx install git+https://github.com/DnaMes/lore
```

### Alternative: uv tool

If you use `uv`, install Lore via `uv tool`:

```bash
uv tool install git+https://github.com/DnaMes/lore
```

### Alternative: Virtual Environment

To install into a dedicated Python virtual environment:

```bash
python3 -m venv ~/.venvs/lore && ~/.venvs/lore/bin/pip install git+https://github.com/DnaMes/lore
```

You can then add `~/.venvs/lore/bin` to your `PATH` or invoke the binaries directly.

### Alternative: Editable Clone

To contribute to or develop Lore:

```bash
git clone https://github.com/DnaMes/lore && cd lore && pip install -e .
```

## Optional Extras

Lore has two optional extras, both defined in `pyproject.toml`. The former `watch` extra is gone: `lore watch` polls the filesystem on its own (`--interval`, default 60 seconds) and needs no extra package.

| Extra | Adds | Without it |
| --- | --- | --- |
| `semantic` | `fastembed>=0.3`, `sqlite-vec>=0.1.9` | Keyword-only search. When installed, enables semantic vector search and KNN similarity queries over session embeddings (`bge-small-en-v1.5`), fusing vector scores with FTS5 BM25 keyword matches via Reciprocal Rank Fusion (RRF). Without this extra, `sqlite_vec_available()` returns false and search falls back strictly to FTS5 keyword search. |
| `dev` | `pytest>=8,<10`, `pytest-cov>=5`, `coverage>=7`, `ruff>=0.6`, `mypy>=1.8`, `fastembed>=0.3`, `sqlite-vec>=0.1.9` | Development and testing dependencies. Only required for contributors running test suites, coverage reports, linters, and type checkers. |

### Installing Extras

To add the `semantic` extra to a `pipx`-managed installation:

```bash
pipx inject lore fastembed sqlite-vec
```

Or install with the extra from the Git repository in one step:

```bash
pipx install "lore[semantic] @ git+https://github.com/DnaMes/lore"
```

*Note: the `pipx` commands in this section were NOT VERIFIED by running them in this environment.*

## Core Dependencies

The core runtime dependencies specified in `pyproject.toml` are:

- `flask` (`>=3.0.0,<4.0.0`): Web application server and API endpoints.
- `markdown` (`>=3.5.0,<4.0.0`): Markdown rendering for exported transcripts and UI views.
- `nh3` (`>=0.2.0`): HTML sanitization for safe rendering of user prompts and assistant outputs.
- `google-generativeai` (`>=0.3.0,<1.0.0`): Client library for Gemini LLM integrations.
- `google-auth` (`>=2.0.0,<3.0.0`): Authentication library for Google Cloud and Gemini credentials.

## Verification

After installation, verify that the CLI binary is available:

```bash
lore --version
```

Next, check tool detection across your local system:

```bash
lore check
```

The `lore check` command is read-only. It inspects configured directories for supported coding assistants (such as Claude Code, Cursor, Gemini, and OpenCode) and displays their status.

## Upgrading

To upgrade a `pipx`-installed version to the latest repository commit:

```bash
pipx upgrade lore
```

Alternatively, re-run the `pipx install` or `pip install` command with the repository URL. Existing databases, exported sessions, and cache files stored under `~/.lore` are preserved during upgrades.

## Uninstalling

Use the command that matches how you installed Lore:

```bash
pipx uninstall lore                  # pipx install
uv tool uninstall lore               # uv tool install
~/.venvs/lore/bin/pip uninstall lore # virtual environment (adjust the path)
```

Uninstalling the package removes the CLI binaries and package files. Your archived sessions, vector indices, and configuration in `~/.lore` are retained. To remove all archived data, delete `~/.lore` manually (this action is irreversible):

```bash
rm -rf ~/.lore
```

## Troubleshooting

1. `lore: command not found`
   Ensure the directory where pipx or your virtual environment installs binaries (e.g. `~/.local/bin`) is present in your shell `PATH`. Run `pipx ensurepath` to update your shell configuration automatically.

2. A tool shows "Not found" in `lore check`
   Lore looks for default session and database directories (for example `~/.claude` or `~/.config/Cursor`). If a tool is listed as "Not found", its configuration directory does not exist on the machine or the tool has not yet created local sessions.

3. Claude sessions missing
   If expected Claude Code sessions do not appear, verify retention periods or export directories. Older sessions may have been pruned or archived outside default storage (see [./PRIVACY.md](./PRIVACY.md)).

4. Empty search results
   If queries return no results, verify that session data has been indexed. Run `lore export --all` or `lore sync <tool>` to populate the SQLite search database.

5. Port 5000 busy
   The web interface defaults to port 5000. If port 5000 is occupied by another process, start the server on a different port using `--port` (for example, `lore-web --port 5001`) or set the `LORE_WEB_PORT` environment variable.

6. Semantic search unavailable
   If semantic vector search does not return results or degrades to keyword matching, the `fastembed` and `sqlite-vec` libraries are missing. Run `pipx inject lore fastembed sqlite-vec`, or install the `semantic` extra from the Git repository as shown above. Do not use `pip install "lore[semantic]"`: PyPI `lore` is a different project.

## Related Documentation

- [./CLI.md](./CLI.md) - Command-line interface usage and options
- [./CONFIGURATION.md](./CONFIGURATION.md) - Environment variables and configuration options
- [./PRIVACY.md](./PRIVACY.md) - Data storage, retention, and privacy scope
- [./DOCKER.md](./DOCKER.md) - Running Lore in a Docker container
