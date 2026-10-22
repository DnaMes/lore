## 2024-10-23 - Accessibility of dynamically injected DOM elements
**Learning:** Elements injected dynamically via JS (like the resume modal copy button or toast notifications) often miss ARIA labels, as they bypass standard template-level accessibility checks.
**Action:** When auditing template files for accessibility, also inspect `<script>` blocks where HTML strings are constructed, ensuring interactive elements injected client-side have appropriate ARIA attributes.
