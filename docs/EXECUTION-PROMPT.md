# Execution Prompt — lore Professionalisierung (2026-06-24)

> Generiert via /scope nach 3-Agent-Research (Architektur-Map, Roadmap-Doc-Scan, Competitor-Research).
> User-Entscheidung: **Voll umsetzen alle 4 Achsen** · **Redis nur bei echtem Mehrwert (kein Postgres)**.
> In frischer `lore`-Session als ersten Prompt einfügen.

---

Du bist CEO/Tech-Lead von "lore" (`/home/dnames/projects/lab/ai/lore`) — cross-tool
AI-Session-Archiv + Shared-Memory, Flask+SQLite+MCP, Package `lore`, Produkt=Lore,
Datadir `~/.lore`, v2.3.0, Remote GitHub `DnaMes/lore` (Forgejo ist tot — nur GitHub).
Docker = 1 Service, gunicorn `--workers 1` (RELOAD_JOBS in-memory, NICHT auf >1 erhöhen
ohne persistenten JobStore).

**Auftrag:** Aus dem aktuell "gevibe-coded" wirkenden Tool ein professionelles machen.
**Modus: VOLL UMSETZEN — alle 4 Achsen** (nicht nur reviewen). Branch first, Tests grün, verify.

**Gates:** systematic-debugging vor Bugfix · verification-before-completion vor "fertig"
(echte Outputs zeigen, kein "sollte gehen") · requesting-code-review nach Code-Änderungen.
Tests: `.venv/bin/python -m pytest tests/` (Coverage-Gate 80%). Conventional Commits.
NICHT auf main committen ohne Freigabe. Stage gezielt, nie `git add -A`. Stale Docs im
selben Change mitziehen. No silent failures.

## ── ACHSE 1: DATEN-VOLLSTÄNDIGKEIT (Priorität #1, hart verifizieren) ──
a) **FIX `claude.py:217-219`** — Tool-Result-Truncation auf 500 Zeichen entfernt Daten.
   Volle Tool-Results in DB speichern (content TEXT darf groß sein); Truncation NUR im
   Web-Default-View mit "Expand"-Toggle, nicht in der Quelle. Migration falls nötig.
   ⚠️ Größencheck: index.json/SQLite kann stark wachsen (aktuell ~78 MB) — messen.
b) **Verifiziere ALLE 11 Extractors gegen REALE Daten**: pro Tool Sessions auf Platte vs.
   importiert zählen (`lore export --all`, dann `~/.lore/index` inspizieren). Lücken-Report.
   Claude besonders: `~/.claude/projects/*.jsonl` + `*/subagents/*.jsonl` — kommt alles an?
c) **MIN_USER_PROMPTS=3** (`base.py`): konfigurierbar machen + dokumentieren; messen wie
   viele Sessions dadurch wegfallen (Zahl liefern). Default ggf. auf 2 senken / Opt-out.
d) **Extractor-Exceptions in Job-Metadata surfacen** (#1e) statt `logger.debug` — keine
   stillen Drops mehr.

## ── ACHSE 2: UX / SESSION-RENDERING (Priorität #2) ──
ui-designer/frontend-Agent lässt `web_formatting.py` + `web_templates.py` auditieren.
Umsetzen (impactreichstes zuerst):
- **Visual Tool Cards**: Tool-Calls als kollabierbare Karten (Name, Args syntax-highlighted,
  Result); Datei-Edits als Diff. **Raw-JSON-Toggle** pro Message.
- **2-Fidelity-View**: clean prose default + "Detailliert/Forensik"-Schalter (volle Tool-
  Outputs, MCP-Responses) — passt zu Achse-1a.
- **Message-Taxonomie sichtbar**: user/assistant/tool/thinking/error getrennt, Rollen-Badges,
  Timestamps. Tool-Burst-Folding. Duplicate-Folding (×N).
- Markdown/Code-Highlight härten. Threading Subagent↔Parent sichtbar.
⚠️ KEINE XSS-Regression (nh3 zwei-Pass MUSS bleiben — `test_web_formatters.py`).
⚠️ Web-HTML liegt als 2036-LOC-Python-String in `web_templates.py` — evtl. erst nach
   Jinja extrahieren (#25c) bevor großer Umbau, sonst riskant.

## ── ACHSE 3: PROFESSIONALISIERUNG / OPTIONEN ──
- **P0 schließen**: #1b (`_new`-Module umbenennen + pyproject scripts), #1d (`safe_copy_db`
  /tmp-Leak try/finally), CODE_OF_CONDUCT.md, README-Screenshot/GIF + 3-Command-Quickstart.
- **Neue Optionen (Competitor-Lücken = Differenzierung):**
  * **Tags / Bookmarks / Favoriten / Annotation** pro Session (kann KEIN Competitor) —
    Schema + Web-UI + MCP-Expose.
  * **Hybrid-Search** FTS5 + optional Vektoren (memory_embeddings Migration 10 teils da) —
    3-Layer-Retrieval search→timeline→get_by_id (~10x weniger MCP-Token). Von Memory auf
    das Session-Archiv selbst erweitern.
  * **Resume-Button** pro Claude-Session: `claude --resume <id>` mit korrektem cwd.
  * **Token/Cost-Dashboard** (#34, meistgewünscht) falls Zeit.
- **Redis/DB — ENTSCHIEDEN: nur bei echtem Mehrwert.** SQLite+WAL reicht aktuell (lokal,
  Single-Worker). Redis NUR falls persistenter JobStore + workers>1 (#45) gewollt — dann
  optional hinter Flag, mit Begründung im Commit. **Postgres NICHT** (Over-Engineering,
  #1a wurde bewusst entfernt). Default = nicht einbauen, außer #45 wird aktiv angegangen.

## ── ACHSE 4: HÄRTUNG / HYGIENE ──
- **Stale Docs fixen**: CLAUDE.md (ruff statt black/isort/flake8; kein postgres/redis),
  AGENTS.md (lore-Namen), README Docker-Sektion (kein db+redis), docs/ROADMAP +
  SESSION-HANDOFF (post-rename, GitHub statt Forgejo).
- **Test-Lücken**: Extractoren ohne Coverage (antigravity/copilot/cursor/vscode) +
  parametrisierter 11-Extractor-Contract-Test (#26/#27).

## VORGEHEN
1. Lies `HANDOFF.md` (neueste Wahrheit) + `docs/SESSION-HANDOFF.md` + `docs/ROADMAP.md`.
   Multi-agent REVIEW NICHT neu starten (`docs/REVIEW.md` existiert). Daten-Vollständigkeit
   IST neu zu prüfen.
2. Plan mode: priorisierten Plan vorlegen (P0 Daten+Security zuerst → UX → Optionen → Hygiene).
3. Achsenweise umsetzen, jede Achse Tests grün + verify (echte App-Outputs/Screenshots via
   browser-qa oder playwright für UX).
4. Findings, die NICHT diese Session umgesetzt werden → GitHub-Issues:
   `gh issue create -R DnaMes/lore -t "..." -b "..."` (Bulk: `tools/create_issues.sh github
   DnaMes lore` — Forgejo-Pfad NICHT nutzen).
5. `HANDOFF.md` + Docs am Ende aktualisieren.

**Fan out Subagents** für parallele unabhängige Arbeit (Extractor-Verify ∥ UX-Audit ∥
Doc-Hygiene). Route günstig wo möglich (`delegate --type explore/review`), Opus nur für
Architektur/harte Bugs.

---
## Bestätigte Findings aus Research (Kontext)
- **claude.py:217-219** truncated Tool-Results 500 chars → echter Datenverlust (Achse 1a).
- MIN_USER_PROMPTS=3 verwirft Sessions <3 User-Msgs komplett.
- Subagent-Import live (46→368 Sessions, commit f549b71) aber Titel ohne Parent-Bezug.
- Docker bereits sauber (1 Service, kein postgres/redis — #1a erledigt).
- Forgejo gelöscht → nur GitHub DnaMes/lore.
- Stale: CLAUDE.md/AGENTS.md/README/docs widersprechen aktuellem Stand.
- Competitor-Lücken (Differenzierung): Tags/Bookmarks (niemand), semantic search übers
  Archiv (niemand), Archive+Memory+MCP kombiniert (niemand).
