# UMBAU.md — Session-Rendering UX-Überarbeitung (lore)

> Detaillierter UX-Umbau für die Session-/Chat-Darstellung. Generiert via ui-designer-Agent
> + Code-Inspektion (2026-06-24). Begleitdokument zu `docs/EXECUTION-PROMPT.md` (Achse 2).
> Ziel: aus dem "billig"/gevibe-coded wirkenden Transcript einen forensischen, lesbaren
> Conversation-Reader machen — ohne SPA-Framework, server-rendered HTML + Tailwind +
> highlight.js (vendored) + minimal Vanilla-JS (Progressive Enhancement).

---

## 0. WURZEL-PROBLEM (zuerst lesen — erklärt warum es "billig" wirkt)

`web_formatting.py:_format_message_content_cached` rendert Tool-Calls **NICHT aus dem
strukturierten `UnifiedMessage.tool_calls`-Feld**, sondern parst sie per **Regex aus dem
bereits zu Text formatierten `content`-String** zurück:

```python
# web_formatting.py:148-165 — Tool-Calls/Results werden aus Strings re-geparst:
r"\[Tool Result\]\s*\n(.*?)(?=\n\[Tool:|\n\[Tool Result\]|\Z)"  # → format_tool_result

r"\[Tool:\s*([^\]]+)\]\s*(.*?)(?=\n\[Tool:|\n\[Tool Result\]|\Z)"  # → format_tool_display
```

Das ist die Wurzel:
- Strukturierte Daten (args als dict, result als object, status, duration, file-path) gehen
  beim Extractor-Schritt in einen flachen String verloren (`[Tool: Read]\n...`).
- `claude.py:217-219` truncated Results auf 500 Zeichen BEVOR sie hier ankommen (Achse 1a).
- `format_tool_calls()` (strukturiert, web_formatting.py:195) EXISTIERT, wird im Haupt-Flow
  aber nicht genutzt — die Message-Anzeige geht über den String-Pfad.

**Konsequenz für den Umbau:** Erst die **Prep-Layer** bauen (siehe §11), die
`UnifiedMessage` → View-Structs konvertiert (Tool-Icon, Result-Typ erkannt, Diff-Hunks
vorberechnet, Tokens formatiert). Erst dann lohnt sich UI-Politur. Sonst poliert man auf
einem kaputten Fundament. **Das ist Vorbedingung für Phase 2+.**

---

## 1. DESIGN-PRINZIPIEN (treiben jede Entscheidung)

1. **Prose-first, Forensik on-demand.** Default liest sich wie sauberes Gespräch. Tool-Lärm
   default kollabiert. Detail ein Klick weg — nie gelöscht.
2. **Dichte ohne Clutter.** Sessions sind lang (hunderte Messages, riesige Tool-Outputs).
   Jede Komponente kollabierbar, Seite bleibt navigierbar.
3. **Eine Akzent-Farbe pro semantischem Typ.** Leser erkennt Message-Typ an Farbe+Icon in
   <200ms, vor dem Lesen.
4. **Alles kopierbar, alles verlinkbar.** Jede Message + jeder Tool-Call: stabiler Anker +
   Copy-Button. Es ist ein forensisches Tool.
5. **Progressive Enhancement.** Seite ohne JS voll lesbar (`<details>`/`<summary>` für
   Collapse). JS upgradet: Fidelity-Toggle, Keyboard-Nav, Sticky-TOC-Scrollspy, Copy-Buttons.

---

## 2. LAYOUT

Drei Zonen: sticky **Meta-Header**, zweispaltiger **Body** (TOC-Rail + Conversation-Spalte),
dünne **Footer-Statusleiste**. Conversation-Spalte max `820px` (Lesbarkeit), Seite
full-width damit TOC in der Gutter sitzt.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ META BAR (sticky top, h-14)                                                    │
│ ◀ Back │ ⬡ Claude Code │ "Refactor auth middleware"        [Clean|Detailed]   │
│         feat/auth-mw · a1b2c3d · ~/proj/api    14:32→15:08 · 47 msgs · 38k tok │
│                                                          [⧉ Raw] [↻ Resume] [⌘K]│
└──────────────────────────────────────────────────────────────────────────────┘
┌───────────────┬────────────────────────────────────────────────────┬─────────┐
│ TOC RAIL      │  CONVERSATION COLUMN (max-w-[820px], mx-auto)       │ (gutter)│
│ (sticky,w-64, │  ┌──────────────────────────────────────────────┐  │         │
│  hidden <lg)  │  │ 👤 USER · 14:32                          ⧉ #1 │  │         │
│  ● User #1    │  └──────────────────────────────────────────────┘  │         │
│  ○ Asst #2    │  ┌──────────────────────────────────────────────┐  │         │
│    ▸ 💭 think  │  │ ✦ ASSISTANT · sonnet-4 · 14:33   ↑1.2k ↓0.4k │  │         │
│    ▸ 🔧 Read×3 │  │ ▸ 💭 Thinking (3 paragraphs)                  │  │         │
│  ● User #6    │  │ I'll start by reading the current middleware… │  │         │
│  [Jump:▾]     │  │ ┌──────────────────────────────────────────┐ │  │         │
│  ──────       │  │ │▸🔧 Read  middleware/auth.py     ✓  ⧉ ⌄  │ │  │         │
│  Todos (3/5)  │  │ └──────────────────────────────────────────┘ │  │         │
└───────────────┴────────────────────────────────────────────────────┴─────────┘
┌──────────────────────────────────────────────────────────────────────────────┐
│ FOOTER (h-8): 47 messages · 12 tool calls · 38,402 tokens · ⌘K shortcuts      │
└──────────────────────────────────────────────────────────────────────────────┘
```

**Responsive:** `≥lg` TOC sichtbar (sticky w-64), Conversation zentriert. `<lg` TOC versteckt
→ floating "☰ Contents"-Button (unten rechts) öffnet Slide-Over. Meta-Bar klappt Git/Project
in Disclosure. Conversation nie >820px (Extra-Platz = leere Gutter, Text NICHT verbreitern).

**Meta-Bar (links→rechts):** Back · Tool-Badge (Vendor-Icon+Name) · Titel (truncate) ·
**Fidelity-Toggle** `[Clean|Detailed]` · Sub-Zeile (`text-xs muted`:
`git_branch · commit(short) · project(~/abbrev) · created→updated · N msgs · ~Ntok`) ·
rechts: `⧉ Raw` (Session-Raw-JSON) · `↻ Resume` · `⌘K`.

---

## 3. MESSAGE-KOMPONENTEN

Geteilte Anatomie jeder Message: **4px farbiger Left-Border** (= semantischer Hue),
**Icon/Avatar-Chip**, **Header-Meta-Zeile**, **Body**, **Hover-Toolbar** (oben rechts,
`group-hover`). Jede Message = Anker `id="m-{index}"`. Rhythmus: Messages `space-y-4`,
Tools innerhalb Assistant `space-y-2`.

**Header-Meta-Zeile:** `[icon] ROLE_BADGE · model? · timestamp   [hover: ⧉copy {}raw ⌄collapse] #anchor`

### 3.1 USER
- Hue **Blau**. `border-l-2 border-blue-500`, `bg-blue-50/60 dark:bg-blue-950/30`,
  `border border-blue-100 dark:border-blue-900/40 rounded-lg p-4`. Icon 👤 in blauem Chip.
- Meta: `USER`-Badge + Timestamp. Kein Model/Tokens.
- Body: gerenderte Markdown (sans), Code-Fences highlighted.
- Nicht default-kollabierbar (User-Prompts = Navigations-Anker). >40 Zeilen: "show more"-Clamp.

### 3.2 ASSISTANT
- Hue **Slate/neutral** (die "Default-Stimme"). `border-l-2 border-slate-300 dark:border-slate-600`,
  `bg-white dark:bg-slate-900`, `border border-slate-200 dark:border-slate-800 rounded-lg p-4`.
  Etwas prominenter als User. Icon ✦/Vendor-Spark.
- Meta: `ASSISTANT`-Badge · **Model-Chip** (`sonnet-4.5`) · Timestamp · **Token-Chips**
  `↑{input} ↓{output}`. Tokens leer → Chips weglassen.
- Body-Reihenfolge in Card: 1) Thinking (`<details>`, §3.3) 2) Prose `content` (Markdown)
  3) Tool-Calls (§4, gefoldet §6).
- Card selbst nicht kollabierbar; Sub-Teile (Thinking, Tools) schon.

### 3.3 THINKING / reasoning
- Hue **Violett** (signalisiert "intern, kein Output"). `<details>` in Assistant-Card,
  **default kollabiert**.
  - `<summary>`: `▸ 💭 Thinking · {n} paragraphs / {n} words` — `text-violet-600 dark:text-violet-400`,
    italic, `text-sm`. Word-/Para-Count im Summary = Leser schätzt Gewicht vor Expand.
  - Body: `bg-violet-50/50 dark:bg-violet-950/20`, `border-l-2 border-violet-400`, muted
    prose, `p-3 rounded`.
- Native `<details>` (no-JS). Copy auf Hover. In **Detailed** auto-expanded (§5).

### 3.4 TOOL CALL → §4 (Centerpiece).

### 3.5 TOOL RESULT (standalone `role:tool` Message ohne Parent-Call)
- **Bevorzugt:** Result liegt in `tool_calls[i].result` → im Result-Bereich der Ursprungs-Card
  rendern (§4, kanonisches Threading).
- **Orphan-Fallback:** `role:tool` ohne Parent → kompakte Card, Hue **Amber** (Tool-Familie),
  Header `↳ TOOL RESULT · {tool}? · {status}`, Body = Result-Renderer (§4.4). `ml-6` eingerückt
  + dünne Connector-Linie zur vorigen Assistant-Message.

### 3.6 ERROR
- Hue **Rot**. `bg-red-50 dark:bg-red-950/30`, `border border-red-300 dark:border-red-800`,
  `border-l-2 border-red-500`, Icon ⚠.
- Trigger: Tool-Call `result.status == error`/nonzero-exit, oder Message error-flagged. Tool-Card
  im Error-State: rot nur auf Header+Result-Band (nicht ganze Assistant-Card).
- Meta: `ERROR`-Badge, Exit-Code/Error-Type falls da.
- **Default expanded** (Errors sucht man). Copy prominent. NIE in Tool-Burst folden (§6).

### 3.7 SYSTEM / INFO
- Hue **Grau**, niedrigster Kontrast — ambient. Full-width dünne Rule mit zentriertem Pill:
  `── system: context compacted ──`. Keine Card/Avatar. `text-xs muted`,
  `bg-slate-100 dark:bg-slate-800/50` Pill.
- Für: Session-Start, Context-Compaction, Model-Switch, Resume-Point, Env-Notes.
- Klick→Expand falls Payload. Werden TOC-Divider.

---

## 4. TOOL-CALL-CARD (Centerpiece)

Card pro Eintrag in `tool_calls[]`. Familie-Hue **Amber** (Erfolg) / **Rot** (Error).
`<details>` für no-JS-Collapse, JS-upgraded.

### 4.1 Collapsed (default)
```
┌────────────────────────────────────────────────────────────────────┐
│ ▸ 🔧 Read   middleware/auth.py                    ✓ 0.4s   ⧉  {}  ⌄ │
└────────────────────────────────────────────────────────────────────┘
```
Header (`<summary>`, 1 Zeile, `h-9 px-3`, `bg-amber-50/60 dark:bg-amber-950/20`,
`border border-amber-200/60 dark:border-amber-900/40 rounded`):
- **Caret** ▸/▾ (CSS-Rotation auf `[open]`).
- **Tool-Icon** per Name: Read=📄 Write=✎ Edit=✎± Bash=▮_ Grep=🔍 Glob=* WebFetch=🌐
  Task/subagent=⛬ default=🔧. **Single Python Icon-Map-Dict.**
- **Tool-Name** (mono, semibold).
- **Primary-Arg-Summary** (der EINE wichtige Arg): file-path bei Read/Write/Edit, command
  bei Bash (truncated), pattern bei Grep. Mono, muted, truncate. **Macht collapsed Card
  nützlich** — man sieht WAS es tat ohne Expand.
- **Status-Chip:** ✓ grün / ⚠ err rot / … running + Duration falls da.
- **Hover:** ⧉ copy (`tool(args) → result`-Block), {} raw-JSON-Toggle, Caret.

### 4.2 Expanded
```
│ ▾ 🔧 Read   middleware/auth.py                    ✓ 0.4s   ⧉  {}  ⌄ │
├────────────────────────────────────────────────────────────────────┤
│ ARGS                                                            ⧉   │  ← muted label band
│   file_path: "middleware/auth.py"                                   │  ← key/val ODER code
│   offset: 0   limit: 120                                            │
│ RESULT                                                         ⧉   │  ← muted label band
│   1 │ from flask import request                                     │  ← result renderer
│   2 │ from .db import get_user                                      │
│   … │ … (118 more lines)                          [show all ⌄]      │
└────────────────────────────────────────────────────────────────────┘
```

### 4.3 ARGS-Rendering (nach Shape)
- **≤4 skalare Args, kurz:** aligned **key/value-Table** (mono keys, getintete values). Best
  für Read/Grep/Glob.
- **Ein dominanter String-Arg** (`Bash.command`, `Edit.old/new_string`, `Write.content`):
  **syntax-highlighted Code-Block** (highlight.js, Sprache aus Tool+Extension). Gelabelt.
- **Nested/komplex:** pretty-JSON, highlight.js `json`.
- **Truncation:** Args-Block clampt bei ~16 Zeilen + Fade + `[show all]`. Voller Content IMMER
  im DOM (find+copy), nur visuell via `max-height`+overflow geclampt.

### 4.4 RESULT-Rendering — Dispatch nach Tool/Result-Typ (höchster Politur-Wert)

| Result-Typ | Erkennung | Renderer |
|---|---|---|
| **File-Read** | Tool `Read`, oder Text mit Zeilennummern | Line-numbered Code-Block, highlight.js by ext. Gutter-Linenum (`tabular-nums`, muted). Clamp lange Files (§9). |
| **Edit/Write-Diff** | Tool `Edit`/`Write`/`MultiEdit`, old→new | **Diff-Renderer** (§5). |
| **Shell-Output** | Tool `Bash` | Terminal-Block: mono, `bg-slate-900 text-slate-100` (BEIDE Themes), whitespace preserved, Exit-Code-Chip. ANSI server-seitig strippen, Text behalten. |
| **Search** | `Grep`/`Glob` | Liste `path:line: match`, path bold, match mono. Group by file. Header `12 matches in 4 files`. |
| **Structured/JSON** | result dict/list | Pretty-JSON, highlight.js, collapsible nodes wenn tief. |
| **Plain-Text** | Fallback | mono pre, whitespace preserved, clamp. |
| **Empty** | kein result/"" | muted `(no output)` / `(exit 0, empty)`. |
| **Error** | status error/nonzero | Rotes Result-Band, mono Error-Text, Exit-Code-Chip. Default expanded. |

Jeder Result-Block: eigener Copy-Button + eigener clamp/expand.

### 4.5 Collapsed-vs-Expanded-Regeln
- **Default collapsed.** Ausnahme: einzelner `Edit`/`Write` in **Detailed** default-expanded
  (Diff ist der Punkt). Errors immer default-expanded.
- Collapsed zeigt IMMER Primary-Arg-Summary + Status — nie nacktes "Tool: Read".
- State persistiert per-Card via JS in `sessionStorage` (key=session+card-idx). No-JS:
  `<details>`-Default.

---

## 5. DIFF-RENDERING (Edit/Write/MultiEdit)

Unified-Diff (nicht side-by-side — passt in 820px; side-by-side = Stretch-Goal).
```
┌────────────────────────────────────────────────────────────────────┐
│ ✎± middleware/auth.py                          +4 −2     ⧉  collapse│  ← file header
├────────────────────────────────────────────────────────────────────┤
│  41 41   def wrap_auth(handler):                                     │  ← context
│  42    - │     user = get_user(request.token)                       │  ← removed: rot bg, −
│     42 + │     token = request.headers.get("Authorization")         │  ← added: grün bg, +
│     43 + │     user = get_user(token)                               │
│  43 44   │     if not user:                                         │  ← context
│             ⋯ 38 unchanged lines hidden ⋯              [expand ⌄]    │  ← collapsed hunk
└────────────────────────────────────────────────────────────────────┘
```
Regeln:
- **File-Header** mit `+adds −dels` Count-Chips (grün/rot).
- **Zwei-Spalten-Linenum** (old|new), `tabular-nums`, muted; removed blankt new-col, added
  blankt old-col.
- **Line-BGs:** added `bg-green-100 dark:bg-green-950/40` + `border-l-2 border-green-500`;
  removed `bg-red-100 dark:bg-red-950/40` + `border-l-2 border-red-500`; context kein bg.
- **Marker** `+`/`−`/` ` in fixer Gutter-Spalte.
- **Syntax-Highlight über Diff-BG:** hljs aufs Code-Text-Span, Diff-BG auf Row. v1 darf
  Token-Highlight im Diff überspringen — Diff-BG + mono ist schon riesiger Sprung.
- **Lange Hunks:** 3 Zeilen Kontext um jede Änderung; Runs >6 unchanged → `⋯ N unchanged ⋯
  [expand]`.
- **MultiEdit:** mehrere Hunks unter einem File-Header.
- **Write (neu):** alle Zeilen grün, Header `+N new file`.
- **Diff erzeugen:** speichert Tool nur `old_string`/`new_string` (Edit) statt echtem Diff →
  server-seitig `difflib.unified_diff` rechnen (in Prep-Layer §11), strukturierte Hunks an
  Renderer geben. ⚠️ **Datenmodell-Check nötig:** trägt `tool_calls[i].result` echten
  unified-diff oder nur old/new? (siehe §11 — vor Implementierung verifizieren).

---

## 6. FIDELITY-TOGGLE (Clean ↔ Detailed/Forensic)

Segmented-Control in Meta-Bar `[Clean|Detailed]`. Toggelt Klasse auf `<body>`
(`fidelity-clean`/`fidelity-detailed`) — **pure CSS** zeigt/versteckt. Kein Re-Render, instant.

| Element | Clean (default) | Detailed/Forensic |
|---|---|---|
| Assistant-Prose / User | shown | shown |
| Thinking | collapsed summary | **auto-expanded** |
| Tool-Cards | **collapsed**, Primary-Arg sichtbar | collapsed Header aber **Edit/Write-Diffs auto-expanded**, Result-Preview-Zeile |
| Tool-Burst-Folding | **on** (§7) | **off** (alle Calls gelistet) |
| Duplicate-Folding | on | off |
| Token-Chips | compact | + per-Message Cost-Estimate |
| Raw-JSON-Toggles | hidden (Hover-Menu) | **always-visible `{}`** |
| Timestamps | `HH:MM` | full `HH:MM:SS` + relativ |
| System/Info-Pills | nur major | alle (inkl. Env-Notes) |
| Empty Tool-Outputs | hidden | `(no output)` |

- **Persistenz:** `localStorage` `lore.fidelity`, apply VOR paint (inline-Script in `<head>`
  gegen Flash).
- **Per-Message-Override:** individuelles Expand/Collapse gewinnt über globalen Mode (Toggle
  reißt offene Cards nicht zu).

---

## 7. FOLDING

### 7.1 Tool-Burst-Folding
Assistant-Turn feuert **≥3 aufeinanderfolgende Tool-Calls** → alle außer **erstem+letztem**
in eine Fold-Strip:
```
┌─ ⛬ 5 tool calls (Read×3, Grep, Bash) ───────────────────  show all ┐
└──────────────────────────────────────────────────────────────────┘
```
- Strip zeigt **Inhalts-Summary** (Tool-Name-Counts), nicht nur Count.
- Klick → expandiert Burst inline.
- **NIE Error-Card in Burst folden** — rausziehen + expanded zeigen.
- Auto-Fold nur in **Clean**. Off in Detailed.

### 7.2 Duplicate-Folding
Identische adjacent Messages/Results (gleiche role + content-hash) → eine mit `×N`-Badge:
`Asst: "Reading file…" ×3`. Hover/Klick expandiert. Hash über (role, content, tool_name, args)
server-seitig, `data-dup-count`.

### 7.3 Fold-Affordance (geteilt)
Dashed top+bottom border, `bg-slate-50 dark:bg-slate-800/40`, `text-xs muted`, full-width.
Links Summary+Icon, rechts `show all ⌄`. Folds = ein TOC-Node.

---

## 8. TYPOGRAFIE & FARBE

**Type-Scale (Tailwind):**
- Body-Prose: `text-[15px] leading-relaxed`, sans (system-UI / Inter vendored).
- Header/Badges: `text-xs font-medium uppercase tracking-wide`.
- Code/mono: `text-[13px] leading-snug`, mono (`ui-monospace,"JetBrains Mono","Fira Code",monospace`).
  Mono für: Tool-Namen, Args, alle Result-Bodies, Linenum, file-paths, git-hashes, Token-Chips.
- Sans für: User/Assistant/Thinking-Prose, UI-Chrome, Badges.
- Linenum & Tokens: `tabular-nums`.

**Semantik-Farbsystem (ein Hue/Typ, Light+Dark getunt):**

| Typ | Hue | Light | Dark | Einsatz |
|---|---|---|---|---|
| User | **Blau** | blue-500/50 | blue-400/950 | border, chip, icon |
| Assistant | **Slate** | slate-300/white | slate-600/900 | Default-Stimme, bewusst leise |
| Thinking | **Violett** | violet-400/50 | violet-400/950 | "intern" |
| Tool (call/result) | **Amber** | amber-500/50 | amber-400/950 | "Maschinen-Aktion" |
| Diff added | **Grün** | green-500/100 | green-500/950 | |
| Diff removed | **Rot** | red-500/100 | red-500/950 | |
| Error | **Rot** | red-500/50 | red-500/950 | |
| System/Info | **Grau** | slate-400/100 | slate-500/800 | ambient, niedrigster Kontrast |

Regeln:
- Jeder Hue als: 4px **Left-Border** (voll sat), **Icon-Chip** (voll sat auf getintetem bg),
  subtiler **Card-Tint** (`/50–/60` light, `/20–/30` dark). NIE Card mit sat Farbe fluten.
- **WCAG AA:** Text immer auf near-white/near-black; Farbe nur für Border/Chips/Tints, nie
  alleiniges Signal (immer Icon+Text-Badge → colorblind-safe).
- **Token-Chips:** pill `bg-slate-100 dark:bg-slate-800 text-xs tabular-nums`, ↑input ↓output,
  Cost-Chip (Detailed) `$0.012`.
- **Tool-Badge Meta-Bar:** per-Vendor-Akzent (Claude=orange, Cursor=blau, Codex=teal,
  Aider=grün, Gemini=indigo) — nur Vendor-Chip, kämpft nicht mit Message-Taxonomie.

---

## 9. NAVIGATION

### 9.1 Sticky TOC-Rail
- Listet **User-Messages** (Primär-Anker) + major System-Marker; Assistant-Turns nested unter
  vorigem User-Msg als `○`-Children mit Tool-Summary (`💭`, `🔧×3`).
- Entry = `[icon] truncated first line · #n`. Klick → smooth-scroll `#m-{i}`.
- **Scroll-Spy:** IntersectionObserver highlightet aktuelle TOC-Entry (~30 Zeilen Vanilla-JS).
- Header `Jump ▾` Dropdown-Filter: All / Errors only / Tool calls / User turns.
- **Todos-Panel** unten gepinnt (`3/5 done`, Checklist).

### 9.2 In-Page-Find
- Native Ctrl/⌘-F funktioniert weil **aller Content (auch geclampt/collapsed) im DOM bleibt**
  — clamps via `max-height`+overflow, collapses via `<details>` + JS-Hook der `<details>`
  auto-`open`t bei Match (`:target`/selection). "⊕ expand all"-Button für Pre-Expand.
- Optional custom-Find (`/`) — native = v1-Baseline, nicht drauf blockieren.

### 9.3 Keyboard-Shortcuts (Vanilla-JS, `⌘K`-Palette listet sie)
| Key | Aktion |
|---|---|
| `j`/`k` | next/prev Message |
| `o` | toggle focused Card |
| `t` | toggle Fidelity |
| `e`/`E` | expand all / collapse all |
| `g g`/`G` | top/bottom |
| `/` | find |
| `c` | copy focused Message |
| `⌘K` | Command-Palette (jump, toggle, copy-link, resume, raw) |
| `?` | Cheatsheet-Overlay |

### 9.4 Anker / Deep-Links
Jede Message `id="m-{i}"`, jede Tool-Card `id="m-{i}-t-{j}"`. Copy-Link auf Hover → URL mit
Fragment. On-load: Ziel-Element auto-expand.

---

## 10. EMPTY / EDGE-STATES

- **Sehr langer Tool-Output / File-Read:** clamp ~24 Zeilen (`max-h-96 overflow-hidden`+Fade)
  + `[show all N lines ⌄]`. Voller Text im DOM (find/copy). >2000 Zeilen: nur head+tail +
  `… N lines omitted, [load full]` (`<details>`, server-rendered collapsed; bei riesigem
  Payload hinter `?full=1`-Raw-View).
- **Binary/Image:** by mime/ext. Image → inline `<img>` Thumb (max-h-64, Klick→Lightbox).
  Anderes Binary → Chip `🗎 binary · {size} · {mime}` + Download/Raw, kein Body-Dump.
- **Source-truncated** (Model-Limit / Log-truncated): Original + `⚠ truncated by {tool}`-Badge
  → nicht mit unserem Clamp verwechseln.
- **Empty Tool-Result:** `(no output)` muted; Bash `(exit 0, no output)`.
- **Empty Session:** zentrierter Empty-State — Tool-Icon, "No messages", Meta, ↻Resume + ⧉Raw.
- **Missing Metadata:** nie leere Chips. Kein Model → Chip weg. Keine Tokens → weg. Kein Git → weg.
- **Malformed/unknown role:** Fallback neutrale graue "info"-Card, raw role als Badge, body als
  Text — Render nie crashen.
- **Huge Sessions (500+):** alle server-rendern (lokal, fine), aber highlight.js per-Card lazy
  bei erstem Expand (collapsed Bodies nicht beim Load highlighten) → schneller First-Paint.

---

## 11. IMPLEMENTIERUNG (server-rendered Constraints) — VORBEDINGUNG

### 11.0 Prep-Layer (MUSS zuerst, sonst poliert man Kaputtes)
Neuen `ai_history/interfaces/session_view_prep.py` einführen:
`prepare_session_view(session) -> list[ViewMessage]`. Konvertiert `UnifiedMessage` →
View-Structs:
- Tool-Icon aufgelöst (Icon-Map-Dict).
- **Result-Typ erkannt** (Read/Bash/Edit/Grep/JSON/plain/error — §4.4).
- **Diff-Hunks vorberechnet** (`difflib`, §5).
- Tokens formatiert, Dup-Hashes (§7.2), Burst-Groupings (§7.1).
- **Tool-Calls aus `UnifiedMessage.tool_calls` (STRUKTUR), nicht aus String-Regex** (§0).

`web_templates.py` bleibt dumm — emittiert nur HTML aus Structs. Isoliert die messy Logik vom
2723-LOC-Template-File.

### 11.1 Weitere Regeln
- **Collapse = `<details>`/`<summary>`** überall möglich → free no-JS-Verhalten; JS nur für
  Persistenz + Keyboard + Scroll-Spy.
- **Fidelity = body-class + CSS** → keine conditional server-Branches pro Element.
- **highlight.js lazy:** `hljs.highlightElement` on `<details>` `toggle`-Event pro Card.
- **Copy-Buttons:** EIN delegierter Click-Listener auf Conversation-Root, liest
  `data-copy-target`/`data-copy-text`.
- **Icon-Map + Hue-Map:** je ein Python-Dict, von Prep-Layer geteilt; dokumentiert →
  neue Tools degradieren graceful zu Defaults.

### 11.2 XSS-Regression-Schutz (NICHT brechen)
- nh3 zwei-Pass-Sanitization in `web_formatting.py` MUSS bleiben (`test_web_formatters.py`).
- Neue Tags für Tool-Cards/Diffs (`details`, `summary`, `div`, `span` mit `class`) sind schon
  in `SANITIZE_TAGS`/`SANITIZE_ATTRS` — bei neuen Attributen (`data-*`) prüfen ob nh3 sie
  durchlässt; ggf. structured rendern statt im sanitisierten String.

---

## 12. PRIORISIERTER ROLLOUT (Impact vs Effort)

**Phase 0 — Fundament (Vorbedingung):**
0. **Prep-Layer** §11.0 + Achse-1a-Fix (volle Tool-Results in DB) → strukturierte Daten da.

**Phase 1 — "Stop looking cheap" (höchster Impact, low Effort):**
1. **Semantik-Farbsystem + per-Typ-Cards** (§3, §8) — Borders, Icon-Chips, Tints, Badges.
   Pure CSS/Markup. Killt allein "billig".
2. **Tool-Card collapsed** (§4.1) — Icon + Primary-Arg + Status, als `<details>`.
3. **Token+Model-Chips** Assistant (§3.2).
4. **Typografie-Pass** (mono/sans-Split, Type-Scale).

**Phase 2 — "Forensic power" (high Impact, medium Effort):**
5. **Tool-Card expanded ARGS+RESULT-Renderer** mit Dispatch-Table (§4.3-4.4).
6. **Diff-Rendering** Edit/Write (§5) — größtes "Wow". Server-side `difflib`.
7. **Fidelity-Toggle** (§6) — CSS-Klassen-Flip. Cheap, high delight.
8. **Copy-Buttons + stabile Anker + Copy-Link** (§3-Toolbar, §9.4).

**Phase 3 — "Navigable at scale":**
9. **Sticky-TOC + Scroll-Spy** (§9.1).
10. **Tool-Burst + Duplicate-Folding** (§7).
11. **Clamp/Expand lange Outputs** + Edge-States (§10).

**Phase 4 — "Pro polish":**
12. Keyboard-Shortcuts + ⌘K-Palette (§9.3).
13. Raw-JSON per-Message + Session-Raw-View (§3).
14. Resume-Button, Subagent↔Parent-Threading, custom Find.

> **Für Demo zuerst:** Phase 0 → Phase 1 → Items 5-7. Konvertiert "vibe-coded" zu "echtes
> Produkt" ohne Framework, nur kleine Vanilla-JS-Toggles.

---

## 13. RELEVANTE DATEIEN
- `ai_history/interfaces/web_formatting.py` (225 LOC) — String-Regex-Tool-Parsing (§0-Wurzel),
  nh3-Sanitization. `format_tool_calls()` (strukturiert) existiert ungenutzt.
- `ai_history/interfaces/web_templates.py` (2723 LOC) — HTML-als-Python-Strings. Ziel-Datei.
- `ai_history/core/models.py` — `UnifiedMessage` hat schon `tool_calls`, `reasoning`, `tokens`,
  `model` als Felder → Prep-Layer kann sie nutzen.
- `ai_history/utils/text_processing.py` — `format_thinking`, `format_tool_display`,
  `format_tool_result` (die String-Formatter).
- **NEU:** `ai_history/interfaces/session_view_prep.py` (Prep-Layer, §11.0).
- `tests/test_web_formatters.py` — XSS-Regression-Gate.

⚠️ **Vor Implementierung verifizieren** (Datenmodell-Gap): trägt `UnifiedMessage.tool_calls[i]`
das `result`/`output` strukturiert UND vollständig (nach Achse-1a-Fix), oder muss der Extractor
erst nachgebessert werden? `claude.py` baut tool_calls auf — prüfen ob args+result als Objekte
oder nur als String landen.
