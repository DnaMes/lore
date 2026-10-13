# AI History LLM Integration

This module provides LLM-powered features for analyzing and processing your AI coding sessions.

## Features

### 1. `lore analyze` - Statistics & Insights

Generate AI-powered statistics and insights about your coding sessions.

```bash
# Basic stats (no LLM required)
lore analyze --no-llm

# AI-powered insights (requires Gemini API key)
lore analyze

# Use specific model
lore analyze --model gemini-2.0-flash

# Save to specific file
lore analyze --output ~/my-stats.json
```

### 2. `lore knowledge` - Knowledge Extraction

Extract structured knowledge from your sessions using LLM.

```bash
# Extract knowledge from last 50 sessions
lore knowledge

# Extract from specific tool
lore knowledge --tool opencode

# Limit to 20 sessions
lore knowledge --limit 20
```

The conversation text of each session is sent to the configured provider (the
provider is named before anything is sent). Results are stored per session in
`session_knowledge`; sessions unchanged since the last run are skipped, and
deleted sessions are excluded. `--tool` also scopes the printed output. The command
exits 1 when every provider call fails.

### 3. `lore format` - Session Formatting

Format sessions with AI-generated summaries and tags.

```bash
# Format last 10 sessions
lore format

# Format specific session
lore format --session-id abc123

# Format sessions from specific tool
lore format --tool claude-code --limit 20
```

## Setup

### Authentication Methods

Choose ONE of the following authentication methods:

#### Method 1: API Key (Recommended - Free Tier Available)

1. Get your FREE API key from [Google AI Studio](https://aistudio.google.com/app/apikey)
2. Set environment variable:

```bash
# Add to ~/.bashrc or ~/.zshrc
export GEMINI_API_KEY="your-api-key-here"
```

**Note:** The free tier includes 15 requests per minute and 1 million tokens per day.

#### Method 2: OAuth2 (Google AI Pro Subscription)

**Important:** OAuth tokens from Gemini CLI won't work directly because they lack the Generative Language API scope. You need to create your own OAuth credentials:

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project or select existing
3. Enable the "Generative Language API"
4. Go to APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID
5. Select "Desktop application"
6. Use [OAuth Playground](https://developers.google.com/oauthplayground/) to get refresh token:
   - Click the gear icon ⚙️ → Use your own OAuth credentials
   - Enter your Client ID and Client Secret
   - Select scope: `https://www.googleapis.com/auth/generative-language`
   - Authorize and exchange code for tokens
   - Copy the `refresh_token`

```bash
export GOOGLE_CLIENT_ID="your-client-id"
export GOOGLE_CLIENT_SECRET="your-client-secret"
export GOOGLE_REFRESH_TOKEN="your-refresh-token"
```

#### Method 3: Application Default Credentials (ADC)

If you have `gcloud` CLI installed and have enabled the Generative Language API:

```bash
# Login with gcloud
gcloud auth application-default login

# The tool will automatically use ADC
```

### Ollama (Local LLM)

1. Install Ollama: https://ollama.ai
2. Pull a model: `ollama pull llama3.2`
3. Use with `--provider ollama`:

```bash
lore analyze --provider ollama --model llama3.2
```

#### Method 2: OAuth2 (Google AI Pro Subscription)

If you have a Google AI Pro subscription and OAuth tokens:

```bash
# Set OAuth credentials
export GOOGLE_CLIENT_ID="your-client-id"
export GOOGLE_CLIENT_SECRET="your-client-secret"
export GOOGLE_REFRESH_TOKEN="your-refresh-token"
```

To obtain OAuth tokens:

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create OAuth 2.0 credentials (Desktop app)
3. Use OAuth playground to get refresh token:
   - Go to https://developers.google.com/oauthplayground/
   - Select "Gemini API" scopes
   - Authorize and exchange code for tokens
   - Copy the refresh_token

#### Method 3: Application Default Credentials (ADC)

If you have `gcloud` CLI installed:

```bash
# Login with gcloud
gcloud auth application-default login

# The tool will automatically use ADC
```

### Ollama (Local LLM)

1. Install Ollama: https://ollama.ai
2. Pull a model: `ollama pull llama3.2`
3. Use with `--provider ollama`:

```bash
lore analyze --provider ollama --model llama3.2
```

## Configuration

Create `~/.ai-history/config.json`:

### API Key Method

```json
{
  "llm": {
    "provider": "gemini",
    "model": "gemini-2.0-flash",
    "api_key": "${GEMINI_API_KEY}",
    "cache_enabled": true
  }
}
```

### OAuth Method

```json
{
  "llm": {
    "provider": "gemini",
    "model": "gemini-2.0-flash",
    "oauth_client_id": "${GOOGLE_CLIENT_ID}",
    "oauth_client_secret": "${GOOGLE_CLIENT_SECRET}",
    "oauth_refresh_token": "${GOOGLE_REFRESH_TOKEN}",
    "cache_enabled": true
  }
}
```

### ADC Method

```json
{
  "llm": {
    "provider": "gemini",
    "model": "gemini-2.0-flash",
    "use_adc": true,
    "cache_enabled": true
  }
}
```

## Output Locations

- **Stats**: `~/.ai-history/stats/stats.json`
- **Knowledge**: `~/.ai-history/knowledge/knowledge_base.json`
- **Formatted**: `~/.ai-history/formatted/*.md`

## Caching

LLM responses are cached in `~/.ai-history/llm_cache/` to:

- Reduce API costs
- Speed up repeated queries
- Enable offline access to previous results

## Cost Optimization

- Use `--no-llm` for basic stats (free)
- Use `gemini-2.0-flash` (fast and cheap)
- Cache is enabled by default
- Limit sessions with `--limit` flag
