"""Store-level backstop: the picks table rejects double picks even if the
in-memory guards were ever bypassed."""
from __future__ import annotations

import sqlite3

import pytest

from app.store import EventStore, RevisionConflictError


def pick_row(db_path: str, game_id: str, round_no: int, seat: int) -> tuple:
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT card, auto, revision FROM picks WHERE game_id = ? AND round = ? AND seat = ?",
        (game_id, round_no, seat),
    ).fetchone()
    conn.close()
    return row


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


def test_change_pick_atomic_and_revision_backstop(tmp_path):
    db = str(tmp_path / "s.db")
    store = EventStore(db)
    store.create_game("g1", 30.0)
    store.record_pick("g1", 1, 0, "c01", auto=False)

    # One transaction: row updated, revision bumped, event appended.
    assert store.change_pick("g1", 1, 0, "c02", expected_revision=1) == 2
    assert pick_row(db, "g1", 1, 0) == ("c02", 0, 2)
    assert store.count_events("g1", "pick_changed") == 1

    # Stale revision: the whole transaction rolls back — no update, no event.
    with pytest.raises(RevisionConflictError):
        store.change_pick("g1", 1, 0, "c03", expected_revision=1)
    assert pick_row(db, "g1", 1, 0) == ("c02", 0, 2)
    assert store.count_events("g1", "pick_changed") == 1

    # Auto picks are locked at the store level too.
    store.record_pick("g1", 1, 1, "c04", auto=True)
    with pytest.raises(RevisionConflictError):
        store.change_pick("g1", 1, 1, "c05", expected_revision=1)
    assert pick_row(db, "g1", 1, 1) == ("c04", 1, 1)

    # No recorded pick at all.
    with pytest.raises(RevisionConflictError):
        store.change_pick("g1", 1, 2, "c05", expected_revision=1)
    store.close()


def test_revision_column_migration(tmp_path):
    """Databases created before picks.revision existed are upgraded in place."""
    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE games (
            game_id TEXT PRIMARY KEY, created_at REAL NOT NULL,
            timeout_seconds REAL NOT NULL
        );
        CREATE TABLE players (
            game_id TEXT NOT NULL, seat INTEGER NOT NULL,
            name TEXT NOT NULL, token TEXT NOT NULL, PRIMARY KEY (game_id, seat)
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, game_id TEXT NOT NULL,
            seq INTEGER NOT NULL, type TEXT NOT NULL, payload TEXT NOT NULL,
            created_at REAL NOT NULL, UNIQUE (game_id, seq)
        );
        CREATE TABLE picks (
            game_id TEXT NOT NULL, round INTEGER NOT NULL, seat INTEGER NOT NULL,
            card TEXT NOT NULL, auto INTEGER NOT NULL,
            PRIMARY KEY (game_id, round, seat)
        );
        """
    )
    conn.execute("INSERT INTO picks VALUES ('g1', 1, 0, 'c01', 0)")
    conn.commit()
    conn.close()

    store = EventStore(db)  # opening migrates: legacy rows get revision 1
    assert store.change_pick("g1", 1, 0, "c02", expected_revision=1) == 2
    assert pick_row(db, "g1", 1, 0) == ("c02", 0, 2)
    store.close()
