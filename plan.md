1. **Optimize markdown parsing in `lore/interfaces/web_formatting.py` and `lore/interfaces/web.py`**
   - The memory states: "In Python, the `markdown` library's `Markdown` parser instance is not thread-safe. To optimize performance in multithreaded environments (like Flask), store reusable `Markdown` instances in `threading.local()` and always call `.reset().convert(text)` rather than instantiating them globally or recreating them per request."
   - Replace `markdown.markdown(content, extensions=["fenced_code", "tables", "nl2br"])` with a thread-local parser instance.
   - Use `threading.local()` to store the parser.

2. **Complete pre-commit steps to ensure proper testing, verification, review, and reflection are done.**
   - Run linter and tests (`pnpm lint`, `pnpm test`, or their Python equivalents).

3. **Submit the PR**
   - Create a PR with title `⚡ Bolt: [performance improvement]` and the required description format.
