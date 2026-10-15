## 2026-10-15 - Added ARIA labels to form elements without explicit text labels
**Learning:** Some form elements like <select> and <input type="date"> or <input type="checkbox"> lacked explicit <label> associations for screen readers. Using aria-label directly on these elements provides an accessible name.
**Action:** Always ensure that form inputs have either an associated <label> or an aria-label attribute for accessibility.
