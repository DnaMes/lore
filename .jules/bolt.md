## 2024-10-26 - Thread-safe Markdown Parsing Caching
**Learning:** The Python `markdown` library's `Markdown` parser instance is not thread-safe and instantiating it per-request in a multithreaded environment (like Flask) adds significant overhead (rendering many snippets is ~3x slower without caching).
**Action:** Store reusable `Markdown` instances using `threading.local()` and call `.reset().convert(text)` for multithreaded server applications to avoid high instantiation costs while maintaining thread safety.
