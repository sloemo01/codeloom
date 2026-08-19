# codeloom user guide

A practical walkthrough of using codeloom for everyday agent work.

## Quickstart

```bash
# macOS / Linux
curl -O https://raw.githubusercontent.com/sloemo01/codeloom/main/codeloom.py
chmod +x codeloom.py
./codeloom /path/to/repo

# Windows (PowerShell)
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/sloemo01/codeloom/main/codeloom.py" -OutFile codeloom.py
python codeloom.py C:\path\to\repo

# Or install via pip
pip install codeloom
codeloom /path/to/repo
```

## The agent workflow

The most effective way to use codeloom with an AI coding agent:

### 1. Give the agent the map first

```bash
codeloom --install-agents .
# writes AGENTS.md so every future session auto-loads the map
```

### 2. Orient to a task

```bash
codeloom --task "fix the login bug" .
# ranked reading list — which files matter for THIS task

codeloom --pack "add retry logic" .
# single-shot context: reading order + impact + symbols, all in one file
```

### 3. Understand before editing

```bash
codeloom --impact auth/login.py .
# what breaks if I change this?

codeloom --cross --focus auth/login.py .
# the real call path across files

codeloom --get-symbol verify .
# summary-first: signature + docstring + call graph (~10 tokens)
```

### 4. Read the exact code

```bash
codeloom --get-symbol verify --full .
# full source + what it calls + what calls it
```

## MCP setup (Claude Code / Cursor / Codex)

```json
{
  "codeloom": {
    "command": "python3",
    "args": ["/path/to/codeloom-mcp.py"]
  }
}
```

Then the agent can call `codeloom_ask` with plain English:
- "what matters for fixing the login bug"
- "what breaks if I change auth.py"
- "where is the Agent class"
- "what calls what across files"

## Framework-aware

```bash
codeloom --framework .
# framework: FastAPI
# ## Entry points
# ## Routes / views / models
# ## Config
# ## Conventions
```

## Scale

```bash
codeloom --index .          # build persistent knowledge graph (symbols + call/import edges)
codeloom --get-symbol X .   # loads from index in milliseconds
codeloom --deadcode .       # loads call edges from the graph — no re-parse
codeloom --index-status .   # is the index fresh?
```

## Multi-language precision

```bash
codeloom --install-grammars --yes   # one command, 25 languages
```

## Security

```bash
codeloom --verify codeloom.py   # confirm a downloaded copy is official
```

## Troubleshooting

- **"module not found"** — use a path, dir, or dotted name that exists in the repo
- **Slow on huge repos** — run `codeloom --index` first, then queries are fast
- **Want tree-sitter precision** — `codeloom --install-grammars --yes`
- **`--trace` needs `--force`** — it executes code; run in an isolated sandbox
