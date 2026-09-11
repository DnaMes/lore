/** Tailwind build config for lore's precompiled UI stylesheet (#129).
 *
 * The Play-CDN JIT runtime (`tailwind-3.4.16.min.js`) was replaced by a
 * build-time compiled sheet (issue #129). Rebuild after template changes:
 *
 *     scripts/build_tailwind.sh
 *
 * Content scanning covers every template (including their inline <script>
 * blocks — the search panel and toast build Tailwind utilities in JS) plus
 * the Python modules that emit HTML fragments. The theme extension mirrors
 * the previous inline `tailwind.config` byte-for-byte.
 */
module.exports = {
  darkMode: "class",
  content: [
    "./lore/templates/**/*.html",
    "./lore/interfaces/*.py",
    "./lore/utils/*.py",
  ],
  theme: {
    extend: {
      colors: {
        bg: "#09090b",
        surface: "#121214",
        border: "#1f1f23",
        accent: "#3b82f6",
      },
    },
  },
  plugins: [],
};
