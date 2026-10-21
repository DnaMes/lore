## 2024-05-24 - Markdown Parsing
**Learning:** In multithreaded environments (like Flask), storing reusable `Markdown` instances in `threading.local()` optimizes performance rather than instantiating them globally or recreating them per request.
**Action:** Use thread-local storage for markdown parser instances when optimizing markdown parsing performance.
## 2024-05-24 - SQLite Extension Loading in CI
**Learning:** Some Python `sqlite3` builds lack extension loading support (e.g., in CI environments like GitHub Actions). When testing optional SQLite extensions (like `sqlite-vec`), conditionally skip tests using `pytest.mark.skipif` by explicitly checking `hasattr(sqlite3.Connection, "enable_load_extension")` to avoid CI failures.
**Action:** When working on SQLite extension tests, always ensure skip conditions check for `enable_load_extension`.
