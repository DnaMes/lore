
## 2025-02-12 - Reusing Markdown parser in multithreaded environments
**Learning:** The `markdown.Markdown` class in python is not thread-safe. Recreating it on every call causes unnecessary overhead especially when parsing long messages and large texts. Using a thread local cache and resetting it correctly provides a significant performance gain.
**Action:** Use `threading.local()` to cache `markdown.Markdown` instances and use `.reset().convert(...)` on them instead of `markdown.markdown(...)` for thread safety and performance.
