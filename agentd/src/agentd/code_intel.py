"""Semantic code intelligence (Phase H3, ADR-023).

Symbolic repository understanding for the agents:

- **Symbol extraction** — classes, functions, and methods per file, with
  line numbers and signatures. Python is parsed with the stdlib ``ast``
  (always available); other languages are parsed with **Tree-sitter** when
  the optional grammars are installed (``pip install 'agentd[intel]'``) —
  the index degrades gracefully to the languages it can parse.
- **Dependency graph** — import edges resolved to repository files, so the
  agents (and humans) can see hotspots: the modules everything leans on.
- **Persistence** — ``<origin>/.agent/code-index/`` holds ``symbols.json``
  (the symbol cache, content-hash keyed for incremental refresh) and
  ``graph.json`` (the repository graph). The index is refreshed at
  ``prepare_run`` and reused across runs; unchanged files are never
  re-parsed.

Consumers: the Planner (repository map injected into its prompt), and the
Coder / Debugger / Reviewer via the read-only ``code_symbols`` tool.
"""

from __future__ import annotations

import ast
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from agentd.config import CodeIntelConfig
from agentd.logging_setup import get_logger

log = get_logger("code_intel")

INDEX_DIRNAME = "code-index"
SYMBOLS_FILE = "symbols.json"
GRAPH_FILE = "graph.json"
INDEX_VERSION = 1

#: Optional Tree-sitter grammars per suffix (module name, ts language fn).
_TREESITTER_LANGUAGES: dict[str, str] = {
    ".js": "tree_sitter_javascript",
    ".jsx": "tree_sitter_javascript",
    ".ts": "tree_sitter_typescript",
    ".tsx": "tree_sitter_typescript",
    ".go": "tree_sitter_go",
    ".rs": "tree_sitter_rust",
}

_TS_SYMBOL_NODES = {
    "function_declaration": "function",
    "generator_function_declaration": "function",
    "class_declaration": "class",
    "method_definition": "method",
    "function_item": "function",       # rust
    "struct_item": "class",            # rust
    "impl_item": "class",              # rust
    "method_declaration": "method",    # go
    "type_declaration": "class",       # go
}


@dataclass
class CodeIndex:
    """The in-memory view of the persisted index."""

    root: str = ""
    built_at: str = ""
    #: relpath → {"hash", "language", "symbols": [...], "imports": [...]}
    files: dict[str, dict] = field(default_factory=dict)

    # ── queries ──────────────────────────────────────────────────────────────

    @property
    def symbol_count(self) -> int:
        return sum(len(e.get("symbols", [])) for e in self.files.values())

    def find(self, query: str, limit: int = 20) -> list[dict]:
        """Symbols whose name contains ``query`` (case-insensitive)."""
        needle = query.lower()
        matches: list[dict] = []
        for path, entry in sorted(self.files.items()):
            for sym in entry.get("symbols", []):
                if needle in sym["name"].lower():
                    matches.append({**sym, "file": path})
                    if len(matches) >= limit:
                        return matches
        return matches

    def dependency_edges(self) -> list[tuple[str, str]]:
        """Import edges (src file → dst file), resolved within the repo."""
        modules = self._module_map()
        edges: list[tuple[str, str]] = []
        for path, entry in self.files.items():
            for imp in entry.get("imports", []):
                dst = self._resolve_import(imp, modules)
                if dst is not None and dst != path:
                    edges.append((path, dst))
        return edges

    def most_imported(self, top: int = 8) -> list[tuple[str, int]]:
        counts: dict[str, int] = {}
        for _, dst in self.dependency_edges():
            counts[dst] = counts.get(dst, 0) + 1
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top]

    def _module_map(self) -> dict[str, str]:
        """Dotted module name → file path (Python layout)."""
        modules: dict[str, str] = {}
        for path in self.files:
            p = Path(path)
            if p.suffix != ".py":
                continue
            parts = list(p.with_suffix("").parts)
            if parts and parts[-1] == "__init__":
                parts = parts[:-1]
            if not parts:
                continue
            dotted = ".".join(parts)
            modules[dotted] = path
            # src-layout convenience: also register without a leading src/.
            if parts[0] == "src" and len(parts) > 1:
                modules.setdefault(".".join(parts[1:]), path)
        return modules

    @staticmethod
    def _resolve_import(imp: str, modules: dict[str, str]) -> str | None:
        candidate = imp
        while candidate:
            if candidate in modules:
                return modules[candidate]
            candidate = candidate.rpartition(".")[0]
        return None


# ── extraction ───────────────────────────────────────────────────────────────


def _extract_python(text: str) -> tuple[list[dict], list[str]]:
    symbols: list[dict] = []
    imports: list[str] = []
    tree = ast.parse(text)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(_py_symbol(node, "function"))
        elif isinstance(node, ast.ClassDef):
            symbols.append({"name": node.name, "kind": "class",
                            "line": node.lineno, "signature": f"class {node.name}"})
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    sym = _py_symbol(child, "method")
                    sym["name"] = f"{node.name}.{child.name}"
                    symbols.append(sym)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imports.append(node.module)
    return symbols, sorted(set(imports))


def _py_symbol(node: ast.FunctionDef | ast.AsyncFunctionDef, kind: str) -> dict:
    args = [a.arg for a in node.args.args]
    return {
        "name": node.name,
        "kind": kind,
        "line": node.lineno,
        "signature": f"def {node.name}({', '.join(args)})",
    }


def _treesitter_parser(suffix: str):
    """A (parser, language_module) for the suffix, or None when the optional
    grammar is not installed."""
    module_name = _TREESITTER_LANGUAGES.get(suffix)
    if module_name is None:
        return None
    try:
        import importlib

        import tree_sitter

        lang_module = importlib.import_module(module_name)
        get_language = getattr(lang_module, "language", None)
        if get_language is None and suffix in (".ts", ".tsx"):
            get_language = getattr(lang_module, "language_typescript", None)
        if get_language is None:
            return None
        parser = tree_sitter.Parser(tree_sitter.Language(get_language()))
        return parser
    except Exception:  # noqa: BLE001 — optional dependency, degrade silently
        return None


def _extract_treesitter(parser, text: str) -> tuple[list[dict], list[str]]:
    data = text.encode("utf-8", errors="replace")
    tree = parser.parse(data)
    symbols: list[dict] = []
    imports: list[str] = []

    def walk(node) -> None:
        kind = _TS_SYMBOL_NODES.get(node.type)
        if kind is not None:
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                name = data[name_node.start_byte:name_node.end_byte].decode(
                    "utf-8", errors="replace")
                symbols.append({
                    "name": name, "kind": kind,
                    "line": node.start_point[0] + 1,
                    "signature": f"{kind} {name}",
                })
        if node.type in ("import_statement", "use_declaration", "import_declaration"):
            snippet = data[node.start_byte:node.end_byte].decode(
                "utf-8", errors="replace")
            imports.append(snippet.strip()[:200])
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return symbols, imports


# ── build / refresh / persist ────────────────────────────────────────────────


def _iter_source_files(root: Path, cfg: CodeIntelConfig):
    exclude = set(cfg.exclude_dirs)
    suffixes = {".py"} | set(_TREESITTER_LANGUAGES)
    count = 0
    for path in sorted(root.rglob("*")):
        if count >= cfg.max_files:
            log.warning("code index capped at %d files", cfg.max_files)
            return
        if not path.is_file() or path.suffix not in suffixes:
            continue
        if any(part in exclude for part in path.relative_to(root).parts):
            continue
        try:
            if path.stat().st_size > cfg.max_file_bytes:
                continue
        except OSError:
            continue
        count += 1
        yield path


def _parse_file(path: Path) -> dict | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    suffix = path.suffix
    if suffix == ".py":
        try:
            symbols, imports = _extract_python(text)
        except SyntaxError:
            symbols, imports = [], []
        language = "python"
    else:
        parser = _treesitter_parser(suffix)
        if parser is None:
            return None  # grammar not installed — skip the file entirely
        symbols, imports = _extract_treesitter(parser, text)
        language = suffix.lstrip(".")
    return {"language": language, "symbols": symbols, "imports": imports}


def _hash_text(path: Path) -> str:
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def load_index(origin_root: Path) -> CodeIndex | None:
    """The persisted index, or None when absent/unreadable."""
    path = Path(origin_root) / ".agent" / INDEX_DIRNAME / SYMBOLS_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != INDEX_VERSION:
            return None
        return CodeIndex(root=data.get("root", ""),
                         built_at=data.get("built_at", ""),
                         files=data.get("files", {}))
    except (OSError, json.JSONDecodeError):
        return None


def refresh_index(
    cfg: CodeIntelConfig,
    workspace_root: Path,
    origin_root: Path,
    journal=None,
    persist: bool = True,
) -> CodeIndex:
    """Incrementally (re)build the index for the workspace content and
    persist it under the origin repo's ``.agent/code-index/``
    (``persist=False`` for traceless pipelines like ``plan``)."""
    workspace_root = Path(workspace_root)
    prior = load_index(origin_root)
    prior_files = prior.files if prior is not None else {}

    files: dict[str, dict] = {}
    parsed = reused = 0
    for path in _iter_source_files(workspace_root, cfg):
        rel = path.relative_to(workspace_root).as_posix()
        digest = _hash_text(path)
        old = prior_files.get(rel)
        if old is not None and old.get("hash") == digest:
            files[rel] = old
            reused += 1
            continue
        entry = _parse_file(path)
        if entry is None:
            continue
        entry["hash"] = digest
        files[rel] = entry
        parsed += 1

    index = CodeIndex(
        root=str(workspace_root),
        built_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        files=files,
    )
    if persist:
        _persist(index, Path(origin_root))
    if journal is not None:
        journal.append("CODE_INDEX", files=len(files), symbols=index.symbol_count,
                       parsed=parsed, reused=reused)
    log.info("code index: %d file(s), %d symbol(s) (%d parsed, %d cached)",
             len(files), index.symbol_count, parsed, reused)
    return index


def _persist(index: CodeIndex, origin_root: Path) -> None:
    index_dir = origin_root / ".agent" / INDEX_DIRNAME
    try:
        index_dir.mkdir(parents=True, exist_ok=True)
        symbols_doc = {
            "version": INDEX_VERSION,
            "root": index.root,
            "built_at": index.built_at,
            "files": index.files,
        }
        graph_doc = {
            "version": INDEX_VERSION,
            "built_at": index.built_at,
            "edges": [list(e) for e in index.dependency_edges()],
            "most_imported": [list(m) for m in index.most_imported()],
        }
        for name, doc in ((SYMBOLS_FILE, symbols_doc), (GRAPH_FILE, graph_doc)):
            tmp = index_dir / (name + ".tmp")
            tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
            tmp.replace(index_dir / name)  # atomic on POSIX and Windows
    except OSError as exc:  # persistence must never fail a run
        log.warning("code index persist failed: %s", exc)


# ── prompt injection ─────────────────────────────────────────────────────────


def render_repo_map(index: CodeIndex, max_chars: int = 2500) -> str:
    """A compact repository map for agent prompts (Planner)."""
    if not index.files:
        return ""
    lines = [
        f"Repository map ({len(index.files)} source file(s), "
        f"{index.symbol_count} symbol(s)):"
    ]
    hotspots = index.most_imported(top=6)
    if hotspots:
        hot = ", ".join(f"{path} (imported by {n})" for path, n in hotspots)
        lines.append(f"- dependency hotspots: {hot}")
    for path, entry in sorted(index.files.items()):
        symbols = entry.get("symbols", [])
        if not symbols:
            continue
        names = [s["name"] for s in symbols[:6]]
        extra = f" (+{len(symbols) - 6} more)" if len(symbols) > 6 else ""
        lines.append(f"- {path}: {', '.join(names)}{extra}")
        if sum(len(line) + 1 for line in lines) > max_chars:
            lines.append("- ... (map truncated)")
            break
    text = "\n".join(lines)
    return text[:max_chars]
