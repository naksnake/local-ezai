import pytest

from agentd.tools.filesystem import FsEdit, FsGlob, FsLs, FsRead, FsWrite
from agentd.workspace import PathEscapeError


def test_read_numbered(inplace_ws):
    result = FsRead().run(inplace_ws, path="calculator.py")
    assert result.ok
    assert result.output.startswith("1\tdef add(a, b):")


def test_read_offset_limit(inplace_ws):
    result = FsRead().run(inplace_ws, path="calculator.py", offset=2, limit=1)
    assert result.ok
    assert result.output == "2\t    return a - b"


def test_read_missing(inplace_ws):
    result = FsRead().run(inplace_ws, path="nope.py")
    assert not result.ok
    assert "not found" in result.error


def test_write_creates_dirs(inplace_ws):
    result = FsWrite().run(inplace_ws, path="pkg/sub/new.py", content="x = 1\n")
    assert result.ok
    assert (inplace_ws.root / "pkg/sub/new.py").read_text() == "x = 1\n"


def test_edit_unique(inplace_ws):
    result = FsEdit().run(
        inplace_ws, path="calculator.py", old_string="return a - b",
        new_string="return a + b",
    )
    assert result.ok
    assert "return a + b" in (inplace_ws.root / "calculator.py").read_text()


def test_edit_not_found(inplace_ws):
    result = FsEdit().run(
        inplace_ws, path="calculator.py", old_string="does not exist",
        new_string="x",
    )
    assert not result.ok
    assert "not found" in result.error


def test_edit_ambiguous_requires_replace_all(inplace_ws):
    (inplace_ws.root / "dup.txt").write_text("aaa\naaa\n")
    tool = FsEdit()
    result = tool.run(inplace_ws, path="dup.txt", old_string="aaa", new_string="bbb")
    assert not result.ok
    assert "occurs 2 times" in result.error
    result = tool.run(
        inplace_ws, path="dup.txt", old_string="aaa", new_string="bbb",
        replace_all=True,
    )
    assert result.ok
    assert (inplace_ws.root / "dup.txt").read_text() == "bbb\nbbb\n"


def test_edit_noop_rejected(inplace_ws):
    result = FsEdit().run(
        inplace_ws, path="calculator.py", old_string="x", new_string="x"
    )
    assert not result.ok


def test_ls(inplace_ws):
    result = FsLs().run(inplace_ws)
    assert result.ok
    assert "calculator.py" in result.output
    assert ".git" not in result.output


def test_glob_skips_git_internals(inplace_ws):
    result = FsGlob().run(inplace_ws, pattern="**/*")
    assert result.ok
    assert "calculator.py" in result.output
    assert ".git" not in result.output


def test_path_escape_raises(inplace_ws):
    with pytest.raises(PathEscapeError):
        FsRead().run(inplace_ws, path="../../etc/passwd")


def test_registry_converts_escape_to_error(registry, inplace_ws):
    result = registry.execute(
        "fs_read", {"path": "../secret"}, inplace_ws, agent="test",
    )
    assert not result.ok
    assert "escapes workspace" in result.error
