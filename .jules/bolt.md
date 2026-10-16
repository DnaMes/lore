## 2024-10-24 - Markdown Parsing Performance in Flask
**Learning:** The Python `markdown` library's `Markdown` instance is not thread-safe. While instantiating it per request is a performance bottleneck, using a global instance in a multithreaded environment (like Flask) causes race conditions and corrupted output.
**Action:** Use `threading.local()` to cache and reuse the `Markdown` instance per thread, ensuring safety while still providing significant performance gains. Always remember to call `.reset()` after conversion to prevent state leakage.
