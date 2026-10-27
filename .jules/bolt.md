## 2025-02-09 - Markdown formatting optimization
**Learning:** `_format_message_content_cached` in `lore/interfaces/web_formatting.py` is called "cached" but it isn't actually using any cache (like `functools.lru_cache`).
**Action:** Adding `@functools.lru_cache(maxsize=128)` significantly speeds it up, because the same message contents are rendered many times across a single request when displaying long conversation histories.
