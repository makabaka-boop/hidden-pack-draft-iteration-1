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
