"""SQLite persistence layer.

The event log (`events`) is the source of truth: in-memory game state is
rebuilt by replaying it after a restart. `picks` mirrors pick events with a
UNIQUE(game_id, round, seat) constraint as a defense-in-depth backstop
against double-recorded picks. All writes are serialized by a lock and each
public method commits its own transaction, so a crash leaves the log
consistent.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    game_id         TEXT PRIMARY KEY,
    created_at      REAL NOT NULL,
    timeout_seconds REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS players (
    game_id TEXT NOT NULL,
    seat    INTEGER NOT NULL,
    name    TEXT NOT NULL,
    token   TEXT NOT NULL,
    PRIMARY KEY (game_id, seat)
);
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id    TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    type       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE (game_id, seq)
);
CREATE TABLE IF NOT EXISTS picks (
    game_id  TEXT NOT NULL,
    round    INTEGER NOT NULL,
    seat     INTEGER NOT NULL,
    card     TEXT NOT NULL,
    auto     INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (game_id, round, seat)
);
"""


class RevisionConflictError(Exception):
    """The stored pick's revision did not match the caller's expectation
    (or the pick is automatic / missing). Defense-in-depth backstop behind
    the in-memory guards in Game.change_pick."""


class EventStore:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)
            # Migration for databases created before picks.revision existed.
            cols = {row[1] for row in self._conn.execute("PRAGMA table_info(picks)")}
            if "revision" not in cols:
                self._conn.execute(
                    "ALTER TABLE picks ADD COLUMN revision INTEGER NOT NULL DEFAULT 1"
                )

    # -- writes ---------------------------------------------------------

    def create_game(self, game_id: str, timeout_seconds: float) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO games (game_id, created_at, timeout_seconds) VALUES (?,?,?)",
                (game_id, time.time(), timeout_seconds),
            )

    def add_player(self, game_id: str, seat: int, name: str, token: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO players (game_id, seat, name, token) VALUES (?,?,?,?)",
                (game_id, seat, name, token),
            )

    def _append_locked(self, game_id: str, type_: str, payload: dict) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM events WHERE game_id = ?",
            (game_id,),
        ).fetchone()
        seq = row["seq"]
        self._conn.execute(
            "INSERT INTO events (game_id, seq, type, payload, created_at) VALUES (?,?,?,?,?)",
            (game_id, seq, type_, json.dumps(payload), time.time()),
        )
        return seq

    def append_event(self, game_id: str, type_: str, payload: dict) -> int:
        with self._lock, self._conn:
            return self._append_locked(game_id, type_, payload)

    def record_pick(self, game_id: str, round_no: int, seat: int, card: str, auto: bool) -> None:
        """Insert the pick row and its event atomically.

        Raises sqlite3.IntegrityError if a pick already exists for
        (game_id, round, seat) — the primary backstop against double picks.
        """
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO picks (game_id, round, seat, card, auto, revision)"
                " VALUES (?,?,?,?,?,1)",
                (game_id, round_no, seat, card, int(auto)),
            )
            self._append_locked(
                game_id,
                "pick_submitted",
                {"round": round_no, "seat": seat, "card": card, "auto": auto},
            )

    def change_pick(
        self, game_id: str, round_no: int, seat: int, card: str, expected_revision: int
    ) -> int:
        """Re-point a recorded manual pick at a new card, bump its revision,
        and append the immutable pick_changed event — all in one transaction.

        The WHERE clause re-checks revision/auto as a backstop to the
        in-memory guards; a mismatch raises RevisionConflictError and rolls
        the whole transaction back (no update, no event). Returns the new
        revision.
        """
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE picks SET card = ?, revision = revision + 1 "
                "WHERE game_id = ? AND round = ? AND seat = ? AND auto = 0 AND revision = ?",
                (card, game_id, round_no, seat, expected_revision),
            )
            if cur.rowcount != 1:
                raise RevisionConflictError(
                    f"pick for game {game_id} round {round_no} seat {seat} "
                    f"is not at revision {expected_revision} or is automatic"
                )
            new_revision = expected_revision + 1
            self._append_locked(
                game_id,
                "pick_changed",
                {
                    "round": round_no,
                    "seat": seat,
                    "card": card,
                    "revision": new_revision,
                },
            )
            return new_revision

    # -- reads ----------------------------------------------------------

    def list_games(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM games ORDER BY created_at"
            ).fetchall()

    def load_players(self, game_id: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM players WHERE game_id = ? ORDER BY seat", (game_id,)
            ).fetchall()

    def load_events(self, game_id: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM events WHERE game_id = ? ORDER BY seq", (game_id,)
            ).fetchall()

    def count_events(self, game_id: str, type_: str | None = None) -> int:
        with self._lock:
            if type_ is None:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM events WHERE game_id = ?", (game_id,)
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM events WHERE game_id = ? AND type = ?",
                    (game_id, type_),
                ).fetchone()
            return row["n"]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
