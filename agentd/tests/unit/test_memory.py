"""MemoryStore — SQLite persistence, lazy creation, queries, export,
similarity, and prompt renderers."""

import json

import pytest

from agentd.memory import (
    ALL_KINDS,
    KIND_ARCHITECTURE,
    KIND_FAILED_FIX,
    KIND_IMPLEMENTATION,
    KIND_RULE,
    KIND_STYLE,
    KIND_SUCCESSFUL_FIX,
    MemoryStore,
    approaches_similar,
    find_repeated_approach,
    render_debugger_context,
    render_planner_context,
)

SIG = "test[0]|assertion|AssertionError|"


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / ".agent")
    yield s
    s.close()


def seed_fixes(store):
    store.record(KIND_FAILED_FIX, "wrong operator fix", "tried multiplication",
                 run_id="r1", error_signature=SIG, category="assertion",
                 data={"approach": "change the operator to multiplication"})
    store.record(KIND_SUCCESSFUL_FIX, "addition fix", "used a + b as the goal states",
                 run_id="r2", error_signature=SIG, category="assertion",
                 data={"approach": "use addition in add()"})


# ── lazy creation ─────────────────────────────────────────────────────────────


def test_reads_on_missing_db_create_nothing(store):
    assert store.recent() == []
    assert store.fixes_for_signature(SIG) == []
    assert store.search("anything") == []
    assert store.count() == 0
    assert not store.exists
    assert not store.agent_dir.exists()  # zero traces


def test_first_write_creates_db(store):
    store.record(KIND_RULE, "no prints", "never commit print() debugging")
    assert store.exists
    assert store.db_path.name == "memory.db"
    assert store.count() == 1


# ── record / query ────────────────────────────────────────────────────────────


def test_record_rejects_unknown_kind(store):
    with pytest.raises(ValueError, match="unknown memory kind"):
        store.record("gossip", "t", "c")


def test_all_six_kinds_persist(store):
    for kind in ALL_KINDS:
        store.record(kind, f"title {kind}", f"content {kind}")
    assert store.count() == 6
    for kind in ALL_KINDS:
        assert store.count(kind) == 1
        assert store.recent([kind])[0].kind == kind


def test_recent_orders_newest_first_and_limits(store):
    for i in range(5):
        store.record(KIND_RULE, f"rule {i}", "c")
    records = store.recent([KIND_RULE], limit=3)
    assert [r.title for r in records] == ["rule 4", "rule 3", "rule 2"]


def test_fixes_for_signature_exact_match(store):
    seed_fixes(store)
    store.record(KIND_FAILED_FIX, "other", "different failure",
                 error_signature="other|sig")
    records = store.fixes_for_signature(SIG)
    assert len(records) == 2
    assert {r.kind for r in records} == {KIND_FAILED_FIX, KIND_SUCCESSFUL_FIX}
    assert store.fixes_for_signature("") == []


def test_fixes_for_category(store):
    seed_fixes(store)
    assert len(store.fixes_for_category("assertion")) == 2
    assert store.fixes_for_category("browser") == []


def test_search_tokens_any_match_case_insensitive(store):
    store.record(KIND_IMPLEMENTATION, "Fixed pagination bug",
                 "the Customer list paged wrong", run_id="r1")
    store.record(KIND_IMPLEMENTATION, "added logging", "structured logs")
    hits = store.search("customer PAGINATION problem")
    assert len(hits) == 1
    assert hits[0].title == "Fixed pagination bug"
    assert store.search("") == []


def test_roundtrip_files_and_data(store):
    store.record(KIND_SUCCESSFUL_FIX, "t", "c", files=["a.py", "b.py"],
                 data={"approach": "x", "n": 3})
    record = store.recent()[0]
    assert record.files == ["a.py", "b.py"]
    assert record.data == {"approach": "x", "n": 3}


# ── lessons_learned.json export ──────────────────────────────────────────────


def test_export_lessons_structure(store):
    seed_fixes(store)
    store.record(KIND_RULE, "pin deps", "always pin dependency versions")
    store.record(KIND_STYLE, "line length", "100 chars max")
    store.record(KIND_ARCHITECTURE, "sqlite memory", "memory uses sqlite")
    store.record(KIND_IMPLEMENTATION, "run one", "status: completed", run_id="r1")

    path = store.export_lessons()
    assert path.name == "lessons_learned.json"
    data = json.loads(path.read_text())
    assert data["total_memories"] == 6
    assert data["project_rules"][0]["title"] == "pin deps"
    assert data["coding_styles"][0]["title"] == "line length"
    assert data["architecture_decisions"][0]["title"] == "sqlite memory"
    assert len(data["lessons"]) == 2  # failed + successful fixes
    assert data["implementation_history"][0]["run_id"] == "r1"
    assert data["updated_at"]


# ── similarity / repeat detection ────────────────────────────────────────────


def test_approaches_similar():
    assert approaches_similar("change the operator to multiplication",
                              "change the operator to multiplication")
    assert approaches_similar("Change the operator to multiplication!",
                              "change operator to multiplication")
    assert not approaches_similar("change the operator to multiplication",
                                  "rewrite the parser to handle unicode")
    assert not approaches_similar("", "something")


def test_find_repeated_approach_only_matches_failed(store):
    seed_fixes(store)
    repeated = find_repeated_approach(
        store, [SIG], "change the operator to multiplication")
    assert repeated is not None and repeated.kind == KIND_FAILED_FIX
    # a successful approach is not a "mistake to avoid"
    assert find_repeated_approach(store, [SIG], "use addition in add()") is None
    # different approach → no repeat
    assert find_repeated_approach(store, [SIG], "guard against None inputs") is None
    # different signature → no repeat
    assert find_repeated_approach(
        store, ["other|sig"], "change the operator to multiplication") is None


# ── prompt renderers ──────────────────────────────────────────────────────────


def test_render_planner_context(store):
    store.record(KIND_RULE, "pin deps", "always pin dependency versions")
    store.record(KIND_STYLE, "naming", "snake_case everywhere")
    store.record(KIND_IMPLEMENTATION, "fixed the calculator addition bug",
                 "status: completed", run_id="r1")
    text = render_planner_context(store, "improve the calculator addition")
    assert "Project rules" in text
    assert "pin deps" in text
    assert "Coding styles" in text
    assert "Past lessons relevant to this request" in text  # 'calculator' token hit
    assert "Recent implementation history" in text


def test_render_planner_context_empty_store(store):
    assert render_planner_context(store, "anything") == ""


def test_render_debugger_context_lists_failed_first(store):
    seed_fixes(store)
    text = render_debugger_context(store, [SIG], ["assertion"])
    assert "ALREADY FAILED" in text
    assert "change the operator to multiplication" in text
    assert "previously SUCCEEDED" in text
    assert "use addition in add()" in text
    assert text.index("ALREADY FAILED") < text.index("previously SUCCEEDED")


def test_render_debugger_context_empty(store):
    assert render_debugger_context(store, [SIG], ["assertion"]) == ""
