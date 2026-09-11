# Lore — Agent Guidelines

Local-first AI chat history manager for Claude Code, Cursor, VSCode Copilot, Gemini CLI, Warp, Codex, OpenCode. Product/CLI = **Lore**; the Python import package stays `lore`.

## Project Structure

```
lore/
├── lore_cli.py           # CLI entry point (`lore`)
├── lore_session_cli.py           # Session switching CLI (`lore-session`)
├── lore/
│   ├── cli/web.py              # Web UI entry point (`lore-web`)
│   ├── cli/mcp.py              # MCP server entry point (`lore-mcp`)
│   ├── extractors/            # Tool-specific data extractors
│   ├── interfaces/            # Web UI (web_templates.py, web.py)
│   ├── exporters/             # Markdown/JSON export
│   ├── search/               # SQLite FTS search
│   ├── core/models.py        # UnifiedSession, Role, Tool enums
│   └── utils/                # Utilities
├── tests/                     # pytest suite
├── pyproject.toml
└── docker-compose.yml
```

## Build, Install & Run Commands

```bash
# Install
pip install -e . && pre-commit install

# Lint & Type Check — ruff replaces black/isort/flake8
ruff format . && ruff check --fix .
mypy lore/ --ignore-missing-imports
bandit -r lore/

# Run Tests
pytest tests/                              # All tests
pytest tests/test_extractors_contract.py    # Single file
pytest tests/ -k "gemini"                  # Pattern match
pytest tests/ -v --tb=short               # Verbose

# Docker
docker compose build app && docker compose up -d app
docker compose logs -f app

# CLI
lore check && lore list --since 7d
lore search "query" --context 3
lore export --all
lore-web  # http://localhost:5000
```

## Code Style Guidelines

### Imports (order matters)

1. Standard library (`os`, `sys`, `json`, `re`, etc.)
2. Third-party (`flask`, `markdown`, etc.)
3. Local (`lore.core`, `lore.utils`, etc.)
4. **Alphabetical within groups, explicit only - no `from X import *`**

### Formatting

- **4-space indentation**, **100 char line limit**
- 2 blank lines between top-level, 1 inside methods
- `"""docstrings"""` for all classes and public functions

### Types

```python
def extract_sessions(self) -> Iterator[UnifiedSession]: ...

@dataclass
class UnifiedMessage:
    role: Role
    content: str
    timestamp: datetime
    metadata: Dict[str, Any] = field(default_factory=dict)

class Tool(Enum):
    CLAUDE_CODE = "claude-code"
    CURSOR = "cursor"
```

### Naming

| Type                | Convention   | Example                |
| ------------------- | ------------ | ---------------------- |
| functions/variables | snake_case   | `extract_sessions()`   |
| Classes/Enums       | PascalCase   | `ClaudeCodeExtractor`  |
| Constants           | UPPER_CASE   | `MAX_RETRIES`          |
| Private methods     | \_snake_case | `_normalize_session()` |

### Error Handling

```python
# CORRECT - specific exceptions
try:
    sessions = extractor.extract_sessions()
except sqlite3.OperationalError as e:
    print(f"Error: Database locked: {e}", file=sys.stderr)
    return []
except FileNotFoundError:
    print(f"Warning: {tool.value} not found", file=sys.stderr)
    return []

# WRONG - never bare except
except:  # NEVER
    pass
```

## Architecture Patterns

### Extractors

- Inherit `BaseExtractor`, implement `tool` property + `extract_sessions()`
- Return `Iterator[UnifiedSession]`
- Implement `is_available()` to check tool presence

### Web UI Theming (web_templates.py)

- CSS variables per theme in `html[data-theme="X"]` blocks
- **Tailwind dark mode uses `.dark` class on `<html>`**
- JavaScript `setTheme()` adds/removes `.dark` for dark themes only:

```javascript
const DARK_THEMES = [
  "catppuccin",
  "dracula",
  "nord",
  "monokai",
  "github",
  "tokyo",
];
// Light theme does NOT get .dark class
```

### CLI Commands

- argparse, one function per subcommand
- stdout for data, stderr for errors
- Exit codes: 0 success, 1 error

## What NOT to Do

- ❌ Hardcode absolute paths → use `Path.expanduser()`
- ❌ Commit secrets → use environment variables
- ❌ Break backward compatibility → users depend on export formats
- ❌ Add cloud dependencies → local-first philosophy
- ❌ `from X import *` → explicit imports only
- ❌ Bare `except:` → catch specific exceptions
- ❌ Rename the `lore/` package → import name is deliberately kept; edit inside it

## Debugging

```bash
# Test extractor
python3 -c "from lore.extractors.claude import ClaudeCodeExtractor; print(list(ClaudeCodeExtractor().extract_sessions()))"

# Check tools
lore check

# Debug logging
AI_HISTORY_DEBUG=1 lore list

# Docker debugging
docker compose logs -f app
docker exec -it lore-app bash
```

<!-- BEGIN obsidian-link (generated — do not edit inside this block) -->
## Knowledge base

Documentation for this project lives in the Obsidian vault — that is the
place to look things up, and the place to write lasting notes:

- **This project:** `~/ObsidianVault/2-Projects/ai-stack/lore/`
- **Where does what live:** `~/ObsidianVault/Maps/BIBLIOTHEK.md`
- **Start here (human):** `~/ObsidianVault/START.md`
- **Homelab wiki:** https://wiki.home.erdlabs.com

Session logs are filed automatically to `2-Projects/ai-stack/lore/Sessions/` via the
`.obsidian-doc` marker in this directory — you do not need to write them.

Conventions before editing the vault: skill `obsidian-keeper`.
<!-- END obsidian-link -->

<!-- parachute:begin -->
## Session handoff (context-parachute)

**2026-09-11 — issue-backlog sweep complete, all work pushed.** 26 of 32 open issues
closed across 13 commits on `main` (== `github/main` @ `2ac30bd`): a11y (labels/ARIA/
focus/contrast), Tailwind precompiled CSS replacing the JIT runtime, CSP-safe delegated
event wiring, memoized index lookups + direct single-session reads, opencode two-pass
extraction (425 MB peak → bounded), CLI dispatch table, exception hygiene. Suite
**1164 passed / 10 skipped** (run with isolated `HOME=/tmp/opencode/lore-test-home`),
ruff clean, mypy at pre-existing baseline.

**Immediate next step:** manual browser pass for #106 click-flows (zero-CSP-error
check), then the #119 Blueprint refactor and #112 v2-store upsert redesign — both need
scoped sessions; #92/#48/#33/#29 await maintainer decisions.

Full context, decisions + gotchas: `HANDOFF.md`. Ready-to-paste prompts:
`.parachute/continue.md` (any agent) · `.parachute/continue-claude.md` (fresh Claude
session after `/clear`).

generated by context-parachute vunknown
<!-- parachute:end -->
