## 2025-02-12 - Markdown instance is not thread-safe
**Learning:** In Python, the `markdown` library's `Markdown` parser instance is not thread-safe. To optimize performance in multithreaded environments (like Flask), store reusable `Markdown` instances in `threading.local()` rather than instantiating them globally or recreating them per request.
**Action:** Add thread-local caching for Markdown instances in web interfaces where markdown rendering is heavily used.
