"""Store-level backstop: the picks table rejects double picks even if the
in-memory guards were ever bypassed."""
from __future__ import annotations

import sqlite3

import pytest

from app.store import EventStore


def test_picks_unique_backstop(tmp_path):
    store = EventStore(str(tmp_path / "s.db"))
    store.create_game("g1", 30.0)
    store.record_pick("g1", 1, 0, "c01", auto=False)
    with pytest.raises(sqlite3.IntegrityError):
        store.record_pick("g1", 1, 0, "c02", auto=False)
    # The failed insert rolled back its event too.
    assert store.count_events("g1", "pick_submitted") == 1
    store.close()


def test_event_log_roundtrip(tmp_path):
    store = EventStore(str(tmp_path / "s.db"))
    store.create_game("g1", 30.0)
    store.add_player("g1", 0, "Ann", "tok")
    store.append_event("g1", "player_joined", {"seat": 0, "name": "Ann"})
    store.append_event("g1", "game_started", {"seed": 42})
    events = store.load_events("g1")
    assert [e["type"] for e in events] == ["player_joined", "game_started"]
    assert [e["seq"] for e in events] == [1, 2]
    store.close()


def test_change_pick_backstop(tmp_path):
    """The picks table rejects stale-revision and auto-pick changes even if
    the in-memory guards were bypassed; the failed update appends no event."""
    store = EventStore(str(tmp_path / "s.db"))
    store.create_game("g1", 30.0)
    store.record_pick("g1", 1, 0, "c01", auto=False)

    # Happy path: update + revision bump + event, all in one commit.
    assert store.change_pick("g1", 1, 0, "c02", expected_revision=1) == 2
    assert store.count_events("g1", "pick_changed") == 1

    # Stale expected revision: rejected, no event appended.
    with pytest.raises(sqlite3.IntegrityError):
        store.change_pick("g1", 1, 0, "c03", expected_revision=1)
    assert store.count_events("g1", "pick_changed") == 1

    # Auto-picked rows are locked against changes.
    store.record_pick("g1", 1, 1, "c05", auto=True)
    with pytest.raises(sqlite3.IntegrityError):
        store.change_pick("g1", 1, 1, "c06", expected_revision=1)
    assert store.count_events("g1", "pick_changed") == 1
    store.close()


def test_revision_column_migrates_existing_db(tmp_path):
    """A database created before the revision column existed is upgraded."""
    path = str(tmp_path / "s.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE picks (game_id TEXT NOT NULL, round INTEGER NOT NULL,"
        " seat INTEGER NOT NULL, card TEXT NOT NULL, auto INTEGER NOT NULL,"
        " PRIMARY KEY (game_id, round, seat))"
    )
    conn.execute(
        "INSERT INTO picks VALUES ('g1', 1, 0, 'c01', 0)"
    )
    conn.commit()
    conn.close()

    store = EventStore(path)  # migration runs on open
    assert store.change_pick("g1", 1, 0, "c02", expected_revision=1) == 2
    store.close()
