## 2026-10-18 - [Python Markdown Thread-Safety]
**Learning:** In Python, the `markdown` library's `Markdown` parser instance is not thread-safe. Instantiating it globally or recreating it per request in multithreaded environments (like Flask) causes significant performance overhead.
**Action:** Use `threading.local()` to store reusable `Markdown` instances and call `md.reset()` and `md.convert(text)` to safely improve rendering performance across threads.
