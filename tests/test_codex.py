"""新版 Codex 名称字段、旧版数据库与写入失败的隔离回归测试。"""

import sqlite3

import pytest

from rename.adapters.codex import CodexAdapter, _codex_root, _find_state_db


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    return tmp_path


def make_db(root, *, modern=True, milliseconds=True, optional=True):
    db = root / "state_5.sqlite"
    columns = "id TEXT PRIMARY KEY, title TEXT, rollout_path TEXT, updated_at INTEGER"
    if modern:
        columns += ", name TEXT"
    if milliseconds:
        columns += ", updated_at_ms INTEGER"
    if optional:
        columns += ", cwd TEXT, archived INTEGER, first_user_message TEXT"
    with sqlite3.connect(db) as con:
        con.execute(f"CREATE TABLE threads ({columns})")
        con.execute(
            "INSERT INTO threads (id, title, rollout_path, updated_at) VALUES (?,?,?,?)",
            ("test-thread", "Legacy title", "", 100),
        )
    return db


@pytest.mark.parametrize("name, expected", [
    ("User-facing name", "User-facing name"),
    (None, "Legacy title"),
    ("", "Legacy title"),
    ("   ", "Legacy title"),
])
def test_modern_name_read_and_write(codex_home, name, expected):
    db = make_db(codex_home)
    with sqlite3.connect(db) as con:
        con.execute("UPDATE threads SET name=?, updated_at_ms=100123", (name,))
    adapter = CodexAdapter()
    session, = adapter.discover(100.123)
    assert session.title == expected
    assert session.last_active == 100.123
    assert adapter.discover(100.124) == []

    adapter.set_title(session, "修复 Codex 显示名称")
    assert adapter.discover(0)[0].title == "修复 Codex 显示名称"
    with sqlite3.connect(db) as con:
        assert con.execute(
            "SELECT name,title,updated_at,updated_at_ms FROM threads"
        ).fetchone() == ("修复 Codex 显示名称", "修复 Codex 显示名称", 100, 100123)


@pytest.mark.parametrize("milliseconds", [True, False])
@pytest.mark.parametrize("optional", [True, False])
def test_legacy_and_null_milliseconds(codex_home, milliseconds, optional):
    make_db(codex_home, modern=False, milliseconds=milliseconds, optional=optional)
    adapter = CodexAdapter()
    session, = adapter.discover(100)
    assert session.last_active == 100
    assert adapter.discover(100.001) == []
    adapter.set_title(session, "Compatible title")
    assert adapter.discover(0)[0].title == "Compatible title"


def test_archived_filter_survives_missing_optional_column(codex_home):
    db = make_db(codex_home, optional=False)
    with sqlite3.connect(db) as con:
        con.execute("ALTER TABLE threads ADD COLUMN archived INTEGER")
        con.execute("UPDATE threads SET archived=1")
    assert CodexAdapter().discover(0) == []


def test_missing_thread_is_not_reported_as_success(codex_home):
    db = make_db(codex_home)
    adapter = CodexAdapter()
    session, = adapter.discover(0)
    with sqlite3.connect(db) as con:
        con.execute("DELETE FROM threads")
    with pytest.raises(ValueError, match="Codex 会话不存在"):
        adapter.set_title(session, "Must not succeed")


def test_failed_write_preserves_both_fields(codex_home):
    db = make_db(codex_home)
    adapter = CodexAdapter()
    session, = adapter.discover(0)
    with sqlite3.connect(db) as con:
        con.execute(
            "CREATE TRIGGER reject_title BEFORE UPDATE ON threads "
            "BEGIN SELECT RAISE(ABORT, 'write rejected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="write rejected"):
        adapter.set_title(session, "Rejected")
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT name,title FROM threads").fetchone() == (None, "Legacy title")


def test_codex_home_and_numeric_database_version(codex_home):
    assert _codex_root() == codex_home
    assert not CodexAdapter().available()
    for name in ("state.sqlite", "state_5.sqlite", "state_10.sqlite"):
        (codex_home / name).touch()
    assert _find_state_db() == codex_home / "state_10.sqlite"
