"""Semantic code intelligence (Phase H3, ADR-023): symbol extraction,
dependency graph, persisted incremental index, repo-map rendering, and the
code_symbols tool."""

import json

from agentd.code_intel import (
    CodeIndex,
    load_index,
    refresh_index,
    render_repo_map,
)
from agentd.config import CodeIntelConfig
from agentd.journal import Journal
from agentd.tools.search import CodeSymbols
from agentd.workspace import Workspace


def make_tree(root):
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "mod.py").write_text(
        "import os\n\n\n"
        "class Customer:\n"
        "    def save(self, db):\n"
        "        return db\n\n\n"
        "def load_customer(cid):\n"
        "    return cid\n",
        encoding="utf-8",
    )
    (root / "app.py").write_text(
        "from pkg.mod import Customer\n\n\n"
        "def main():\n"
        "    return Customer()\n",
        encoding="utf-8",
    )
    (root / "node_modules").mkdir()
    (root / "node_modules" / "junk.py").write_text("def hidden(): pass\n",
                                                   encoding="utf-8")


def test_symbols_functions_classes_extracted(tmp_path):
    make_tree(tmp_path)
    index = refresh_index(CodeIntelConfig(), tmp_path, tmp_path, persist=False)

    names = {(s["file"], s["kind"], s["name"]) for s in index.find("", limit=100)}
    assert ("pkg/mod.py", "class", "Customer") in names
    assert ("pkg/mod.py", "method", "Customer.save") in names
    assert ("pkg/mod.py", "function", "load_customer") in names
    assert ("app.py", "function", "main") in names
    # excluded directory never indexed
    assert not any(f.startswith("node_modules") for f, _, _ in names)

    sym = index.find("load_customer")[0]
    assert sym["line"] == 9
    assert sym["signature"] == "def load_customer(cid)"


def test_dependency_graph_resolves_repo_imports(tmp_path):
    make_tree(tmp_path)
    index = refresh_index(CodeIntelConfig(), tmp_path, tmp_path, persist=False)
    assert ("app.py", "pkg/mod.py") in index.dependency_edges()
    assert index.most_imported()[0][0] == "pkg/mod.py"


def test_index_persisted_and_incremental(tmp_path):
    make_tree(tmp_path)
    journal = Journal(tmp_path / "run1")
    refresh_index(CodeIntelConfig(), tmp_path, tmp_path, journal=journal)

    index_dir = tmp_path / ".agent" / "code-index"
    symbols = json.loads((index_dir / "symbols.json").read_text())
    graph = json.loads((index_dir / "graph.json").read_text())
    assert "app.py" in symbols["files"]
    assert ["app.py", "pkg/mod.py"] in graph["edges"]

    # second refresh: everything served from the content-hash cache
    journal2 = Journal(tmp_path / "run2")
    refresh_index(CodeIntelConfig(), tmp_path, tmp_path, journal=journal2)
    event = [json.loads(line)
             for line in (tmp_path / "run2" / "journal.jsonl").read_text().splitlines()
             if '"CODE_INDEX"' in line][-1]
    assert event["payload"]["parsed"] == 0
    assert event["payload"]["reused"] == len(symbols["files"])

    # a changed file is re-parsed, the rest stays cached
    (tmp_path / "app.py").write_text("def main2():\n    return 1\n",
                                     encoding="utf-8")
    index = refresh_index(CodeIntelConfig(), tmp_path, tmp_path)
    assert index.find("main2")
    assert load_index(tmp_path).find("main2")


def test_syntax_error_file_does_not_crash(tmp_path):
    (tmp_path / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    index = refresh_index(CodeIntelConfig(), tmp_path, tmp_path, persist=False)
    assert "broken.py" in index.files
    assert index.files["broken.py"]["symbols"] == []


def test_repo_map_rendering(tmp_path):
    make_tree(tmp_path)
    index = refresh_index(CodeIntelConfig(), tmp_path, tmp_path, persist=False)
    text = render_repo_map(index)
    assert text.startswith("Repository map")
    assert "dependency hotspots: pkg/mod.py (imported by 1)" in text
    assert "pkg/mod.py: Customer, Customer.save, load_customer" in text
    # budget respected
    assert len(render_repo_map(index, max_chars=120)) <= 120
    assert render_repo_map(CodeIndex()) == ""


def test_code_symbols_tool(tmp_path):
    make_tree(tmp_path)
    ws = Workspace(root=tmp_path, repo_path=tmp_path, branch="main",
                   mode="in-place")
    tool = CodeSymbols()
    missing = tool.run(ws, query="Customer")
    assert not missing.ok and "not available" in missing.error

    ws.code_index = refresh_index(CodeIntelConfig(), tmp_path, tmp_path,
                                  persist=False)
    hit = tool.run(ws, query="Customer")
    assert hit.ok
    assert "pkg/mod.py:4 class Customer" in hit.output
    none = tool.run(ws, query="zzz_nope")
    assert none.ok and "no symbols match" in none.output


def test_planner_prompt_carries_repo_map(config, tmp_repo):
    """Integration seam: the Planner sees the repository map (ADR-023)."""
    from agentd.llm import ScriptedLLM
    from agentd.runner import plan_only
    from tests.conftest import planner_response

    llm = ScriptedLLM([planner_response()])
    plan_only(config, tmp_repo, "fix the add bug", llm=llm, run_id="ci-plan")
    prompt = llm.calls[0]["messages"][1]["content"]
    assert "Repository map" in prompt
    assert "calculator.py" in prompt
    assert "code_symbols" in prompt
    # traceless: the index was built in memory only
    assert not (tmp_repo / ".agent").exists()
