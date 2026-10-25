## 2025-02-24 - [Thread-safe Markdown parsing in Flask]
**Learning:** The `markdown.markdown()` function creates a new parser instance on every call, which is a performance bottleneck in multithreaded environments like Flask when rendering many messages. The `Markdown` parser instance is not thread-safe.
**Action:** Store reusable `Markdown` instances in `threading.local()` and always call `.reset().convert(text)` rather than instantiating them globally or recreating them per request.
