## 2024-06-25 - Thread-safe Markdown Parser caching
**Learning:** Python-Markdown's `Markdown` class is inherently stateful and not thread-safe. Repeatedly instantiating `markdown.markdown()` (which initializes the parser with extensions every time) creates significant overhead in multithreaded web environments like Flask.
**Action:** Use `threading.local()` to cache a single parser instance per thread, and properly invoke `.reset()` before reusing it to avoid state leakage (e.g. footnote counters) across renders.
