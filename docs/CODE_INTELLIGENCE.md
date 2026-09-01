# Local-EZAI — Code Intelligence

Semantic repository understanding for the agents (Phase H3, ADR-023):
instead of grepping blindly, the platform maintains a **symbolic index**
of every repository it works on and injects a structural map into
planning.

## 1. What is extracted

| Artifact | Content |
|---|---|
| **Symbols** | classes, functions, and methods per file — name, kind, line, signature |
| **Dependency graph** | import edges resolved to repository files, plus "hotspots" (most-imported modules) |

Parsers:

- **Python** — stdlib `ast` (always available, exact).
- **JS / TS / Go / Rust** — **Tree-sitter**, when the optional grammars are
  installed: `pip install 'agentd[intel]'`. Without them the index simply
  covers the languages it can parse — nothing breaks.

## 2. Where the index lives

```
<repo>/.agent/code-index/
├── symbols.json    # per-file symbol cache, content-hash keyed
└── graph.json      # import edges + most-imported hotspots
```

- Refreshed automatically at the start of every run; **incremental** —
  unchanged files (by content hash) are never re-parsed; journaled as
  `CODE_INDEX` (files, symbols, parsed, reused).
- Machine-managed: never staged or committed by agents (like memory), and
  safe to delete at any time (it rebuilds on the next run).
- `local-ezai plan` builds its index **in memory only** — a plan stays
  traceless.

## 3. How the agents use it

| Agent | Integration |
|---|---|
| **Planner** | a compact **repository map** (file → key symbols, dependency hotspots) is injected into its prompt, so plans reference real modules instead of guesses |
| **Coding Agent** | `code_symbols` tool: exact symbol → `file:line` lookup before editing |
| **Debug Agent** | `code_symbols` to trace a failing name to its definition |
| **Reviewer** | `code_symbols` to verify a diff's references against the real structure |

The `code_symbols` tool (read-only, tier T0):

```
code_symbols(query="Customer")
→ pkg/mod.py:4 class Customer — class Customer
  pkg/mod.py:5 method Customer.save — def save(self, db)
```

## 4. Configuration

```yaml
code_intel:
  enabled: true          # default on; disable per global config if unwanted
  max_files: 2000        # index size cap
  max_file_bytes: 300000 # skip generated monsters
  exclude_dirs: [.git, .agent, __pycache__, node_modules, .venv, venv,
                 dist, build, .mypy_cache, .ruff_cache, .pytest_cache]
  map_max_chars: 2500    # prompt budget for the repository map
```

## 5. Optional: vector retrieval (Qdrant)

The persisted index is deliberately plain JSON — greppable, diffable, and
dependency-free. Embedding the symbol corpus into the stack's existing
Qdrant (`code-<repo>` collections, served by embed-server :8001) is the
planned next step (roadmap N3′) and will layer *semantic similarity*
search on top of the *symbolic* index without replacing it.
