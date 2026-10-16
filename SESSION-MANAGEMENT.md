# AI Session Management

**Nahtlos zwischen AI-Tools wechseln mit vollem Kontext**

## 🎯 Problem gelöst

- ❌ **Vorher**: Claude Code Limit erreicht → Kontext verloren, manuell kopieren
- ✅ **Jetzt**: `ai-session switch gemini` → Weiterarbeiten mit vollem Kontext

---

## 🚀 Quick Start

### 1. Einfacher Wechsel zwischen Tools

```bash
# Liste alle Sessions
ai-session list

# Zu Gemini wechseln (großes Context Window)
ai-session switch gemini

# Zu Codex wechseln (Code Review)
ai-session switch codex

# Automatisch bestes Tool wählen
ai-session continue
```

### 2. In Claude Code (MCP Integration)

```
# Wenn Rate Limit erreicht:
"Use the switch_to_tool MCP tool to continue in Gemini"

# Oder automatisch:
"Use continue_session to auto-select best tool"

# Sessions anzeigen:
"Use list_active_sessions to show all sessions"
```

---

## 🛠️ Features

### ✅ Was funktioniert

1. **CLI Command**: `ai-session` für direkten Tool-Wechsel
2. **MCP Integration**: Automatischer Context-Transfer via Claude Code
3. **Web UI**: Alle Sessions auf http://localhost:5000 sehen
4. **Multi-Tool Support**: Claude, Gemini, Codex, Cursor, VSCode Copilot

### 🔄 Workflow

```
Claude Code
   ↓ (Rate Limit)
   ├─→ ai-session switch gemini  → Gemini CLI
   ├─→ ai-session switch codex   → Codex CLI
   └─→ ai-session continue       → Auto-Select
```

---

## 📋 Verfügbare Commands

| Command | Beschreibung | Beispiel |
|---------|-------------|----------|
| `ai-session list` | Zeige alle Sessions für aktuelles Projekt | - |
| `ai-session switch <tool>` | Wechsel zu Tool mit Kontext | `ai-session switch gemini` |
| `ai-session continue` | Auto-Select bestes Tool | - |

### Unterstützte Tools

- **claude** - Claude Code (default)
- **gemini** - Gemini CLI (1M+ Token Context!)
- **codex** - Codex CLI (Code Review Spezialist)
- **cursor** - Cursor Composer (manuell)
- **vscode** - VSCode Copilot (manuell)

---

## 🔌 MCP Tools

In Claude Code verfügbar (automatisch geladen):

### `switch_to_tool`

Wechsel zu anderem AI Tool mit vollem Kontext.

**Parameter:**
- `tool` (required): gemini, codex, cursor, vscode, claude
- `max_messages` (optional): Anzahl Messages im Kontext (default: 15)

**Beispiel:**
```
Use switch_to_tool with tool="gemini" to continue this session in Gemini CLI
```

### `continue_session`

Automatisch bestes verfügbares Tool wählen basierend auf Rate Limits.

**Parameter:** Keine

**Beispiel:**
```
Use continue_session to auto-select best tool
```

### `list_active_sessions`

Liste alle Sessions für aktuelles Projekt.

**Parameter:** Keine

**Beispiel:**
```
Use list_active_sessions to see all sessions
```

---

## 🌐 Web UI

Starte die Web UI:

```bash
lore-web
```

Öffne: http://localhost:5000

**Features:**
- Alle Sessions über alle Tools sehen
- Conversations durchsuchen
- Sessions filtern nach Tool
- Markdown Rendering mit Code Highlighting

---

## 🏗️ Wie es funktioniert

### 1. Session Tracking

`lore` sammelt automatisch Sessions von:
- Claude Code (`~/.claude/projects/`)
- Gemini CLI (`~/.gemini/tmp/`)
- Codex CLI (`~/.codex/sessions/`)
- Cursor (`~/.config/Cursor/`)
- VSCode Copilot (`~/.config/Code/`)

### 2. Context Transfer

Beim Wechsel:
1. Letzte N Messages aus **allen** Tools für Projekt laden
2. Als Markdown formatieren
3. An neues Tool übergeben
4. Neue Session startet mit vollem Kontext

### 3. Auto-Selection

`ai-session continue` prüft:
1. Ist Claude Code verfügbar? → Claude
2. Sonst: Ist Gemini installiert? → Gemini
3. Sonst: Ist Codex installiert? → Codex

---

## 🔧 Installation

### Voraussetzungen

```bash
# AI Tools (mindestens eins)
npm install -g @google/generative-ai-cli  # Gemini
cargo install codex-cli                    # Codex
# Claude Code bereits installiert

# CCManager (optional, für Worktree Management)
npm install -g ccmanager
```

### ai-session CLI

```bash
pip install -e .
ai-session list
```

### MCP Server

In `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "lore": {
      "command": "lore-mcp"
    }
  }
}
```

Claude Code neu starten, dann verfügbar als MCP Tools.

---

## 📖 Beispiel Workflow

### Szenario: Claude Code Limit erreicht

```bash
# Terminal 1: Claude Code
$ claude

User: Implement feature X
Assistant: [arbeitet... 200k Tokens erreicht]
"I've hit the context limit. Use switch_to_tool with tool='gemini' to continue."

# Claude nutzt MCP Tool automatisch:
[Tool: switch_to_tool]
tool: gemini
max_messages: 20

# Terminal 2: Gemini CLI startet automatisch
$ gemini-cli

# Kontext ist bereits geladen!
User: [letzten 20 Messages aus Claude + Gemini Sessions]
...
Continue implementing feature X