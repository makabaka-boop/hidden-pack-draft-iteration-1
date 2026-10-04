"""Draft game state machine + async manager.

Rules: 4 players, 5 rounds. Round N each player holds a pack of (6-N) cards
and secretly picks one. When all 4 picks are in — or the round deadline
passes (missing picks are auto-filled by a stable rule) — all picks of the
round are revealed simultaneously and the leftover cards pass to the next
seat. After round 5 the game is finished.

Hidden-information invariant: the ONLY function that produces client-bound
state is `Game.snapshot_for(seat)`. It never includes other players' current
packs or the current round's unrevealed picks. Error messages raised as
`GameError` carry a code and a generic message with no card data.
"""
from __future__ import annotations

import asyncio
import json
import random
import secrets
import time
from dataclasses import dataclass

from .cards import ALL_CARD_IDS, card_public
from .store import EventStore

SEAT_COUNT = 4
PACK_SIZE = 5
ROUNDS = PACK_SIZE  # pack sizes per round: 5, 4, 3, 2, 1
PASS_DIRECTION = 1  # leftovers pass to seat (s + 1) % SEAT_COUNT


class GameError(Exception):
    """Client-visible error. `message` must never contain hidden card data."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass
class Player:
    seat: int
    name: str
    token: str


@dataclass
class PickResult:
    card: str
    auto: bool
    already: bool  # True if this was an idempotent replay of a recorded pick
    resolved: bool  # True if this pick completed the round


def stable_auto_pick(pack: list[str]) -> str:
    """Deterministic timeout rule: smallest card id in the pack.

    Stable = depends only on pack contents, never on timing or order of
    arrival, so a timeout always produces the same, reproducible pick.
    """
    return min(pack)


class Game:
    def __init__(self, game_id: str, timeout_seconds: float, store: EventStore):
        self.id = game_id
        self.timeout_seconds = timeout_seconds
        self.store = store
        self.status = "lobby"  # lobby | active | finished
        self.players: dict[int, Player] = {}
        self.connected: set[int] = set()
        self.round_no = 0
        self.packs: dict[int, list[str]] = {}  # SECRET: current pack per seat
        self.picks: dict[int, str] = {}  # SECRET until reveal
        self.auto_seats: set[int] = set()
        self.deadline: float | None = None
        self.revealed: list[dict] = []  # public: {"round", "picks", "auto"}
        self.final_picks: dict[int, list[str]] = {}

    # -- lobby ----------------------------------------------------------

    def add_player(self, name: str) -> Player:
        if self.status != "lobby":
            raise GameError("game_already_started", "this game has already started")
        if len(self.players) >= SEAT_COUNT:
            raise GameError("game_full", "this game already has 4 players")
        name = (name or "").strip()[:24] or f"Player {len(self.players) + 1}"
        seat = next(s for s in range(SEAT_COUNT) if s not in self.players)
        player = Player(seat=seat, name=name, token=secrets.token_urlsafe(16))
        self.players[seat] = player
        self.store.add_player(self.id, seat, name, player.token)
        self.store.append_event(self.id, "player_joined", {"seat": seat, "name": name})
        if len(self.players) == SEAT_COUNT:
            self._start()
        return player

    def _start(self) -> None:
        seed = secrets.randbits(64)
        deck = ALL_CARD_IDS[:]
        random.Random(seed).shuffle(deck)
        self.packs = {s: deck[s * PACK_SIZE : (s + 1) * PACK_SIZE] for s in range(SEAT_COUNT)}
        self.final_picks = {s: [] for s in range(SEAT_COUNT)}
        self.status = "active"
        self.store.append_event(self.id, "game_started", {"seed": seed})
        self._begin_round(1, time.time())

    def _begin_round(self, round_no: int, now: float) -> None:
        self.round_no = round_no
        self.deadline = now + self.timeout_seconds
        self.store.append_event(
            self.id,
            "round_started",
            {"round": round_no, "packs": self.packs, "deadline": self.deadline},
        )

    # -- picks ------------------------------------------------------------

    def submit_pick(self, seat: int, card_id: str, round_no: int, now: float) -> PickResult:
        """Record a pick. Idempotent: a seat's first recorded pick for a round
        wins; repeats (any card, even after the round moved on or the game
        finished) return the recorded pick with already=True."""
        if self.status == "active" and round_no == self.round_no:
            if seat in self.picks:
                return PickResult(card=self.picks[seat], auto=seat in self.auto_seats,
                                  already=True, resolved=False)
            if card_id not in self.packs.get(seat, []):
                raise GameError("card_not_in_pack", "that card is not in your current pack")
            self._record_pick(seat, card_id, auto=False)
            resolved = self._maybe_resolve(now)
            return PickResult(card=card_id, auto=False, already=False, resolved=resolved)
        # Late or duplicate submission for a round that already moved on:
        # acknowledge the recorded pick idempotently instead of failing.
        recorded = self._pick_in_round(seat, round_no)
        if recorded is not None:
            return PickResult(card=recorded, auto=False, already=True, resolved=False)
        if self.status != "active":
            raise GameError("game_not_active", "the game is not accepting picks")
        raise GameError("round_not_active", "that round is not accepting picks")

    def force_timeout(self, round_no: int, now: float) -> bool:
        """Auto-pick for everyone who has not picked, then resolve. Returns
        True if the round was resolved by this call. No-op if the round moved
        on already (e.g. a manual pick won the race)."""
        if self.status != "active" or round_no != self.round_no:
            return False
        if len(self.picks) >= SEAT_COUNT:
            return False
        for seat in range(SEAT_COUNT):
            if seat not in self.picks:
                self._record_pick(seat, stable_auto_pick(self.packs[seat]), auto=True)
        return self._maybe_resolve(now)

    def _record_pick(self, seat: int, card_id: str, auto: bool) -> None:
        self.picks[seat] = card_id
        if auto:
            self.auto_seats.add(seat)
        self.store.record_pick(self.id, self.round_no, seat, card_id, auto)

    def _maybe_resolve(self, now: float) -> bool:
        if len(self.picks) < SEAT_COUNT:
            return False
        picks = dict(self.picks)
        auto = sorted(self.auto_seats)
        self.store.append_event(
            self.id, "round_revealed", {"round": self.round_no, "picks": picks, "auto": auto}
        )
        self.revealed.append({"round": self.round_no, "picks": picks, "auto": auto})
        for seat, card in picks.items():
            self.final_picks[seat].append(card)
        self.picks = {}
        self.auto_seats = set()
        if self.round_no >= ROUNDS:
            self.status = "finished"
            self.deadline = None
            self.store.append_event(self.id, "game_finished", {"final": self.final_picks})
        else:
            leftover = {
                s: [c for c in pack if c != picks[s]] for s, pack in self.packs.items()
            }
            self.packs = {
                s: leftover[(s - PASS_DIRECTION) % SEAT_COUNT] for s in range(SEAT_COUNT)
            }
            self._begin_round(self.round_no + 1, now)
        return True

    def _pick_in_round(self, seat: int, round_no: int) -> str | None:
        for rec in self.revealed:
            if rec["round"] == round_no:
                return rec["picks"].get(seat)
        return None

    # -- client-visible state (the ONLY serialization allowed to clients) --

    def snapshot_for(self, seat: int, now: float) -> dict:
        pending = self.status == "active" and seat not in self.picks
        return {
            "type": "snapshot",
            "game_id": self.id,
            "status": self.status,
            "you": {"seat": seat, "name": self.players[seat].name},
            "players": [
                {
                    "seat": s,
                    "name": p.name,
                    "connected": s in self.connected,
                    "submitted": self.status == "active" and s in self.picks,
                }
                for s, p in sorted(self.players.items())
            ],
            "round": self.round_no,
            "rounds_total": ROUNDS,
            "your_pack": [card_public(c) for c in self.packs.get(seat, [])] if pending else [],
            "your_pick": card_public(self.picks[seat]) if seat in self.picks else None,
            "your_pick_auto": seat in self.auto_seats,
            "your_picks_all": [card_public(c) for c in self.final_picks.get(seat, [])],
            "revealed": [
                {
                    "round": r["round"],
                    "picks": {str(s): card_public(c) for s, c in r["picks"].items()},
                    "auto": r["auto"],
                }
                for r in self.revealed
            ],
            "deadline": self.deadline if self.status == "active" else None,
            "server_now": now,
            "timeout_seconds": self.timeout_seconds,
        }

    def public_replay(self) -> dict:
        """Final pick results, rebuilt from public (revealed) state only."""
        return {
            "game_id": self.id,
            "status": self.status,
            "players": {str(s): p.name for s, p in sorted(self.players.items())},
            "rounds": [
                {
                    "round": r["round"],
                    "picks": {str(s): card_public(c) for s, c in r["picks"].items()},
                    "auto": r["auto"],
                }
                for r in self.revealed
            ],
            "final_picks": {
                str(s): [card_public(c) for c in cards]
                for s, cards in sorted(self.final_picks.items())
            },
        }

    # -- recovery ---------------------------------------------------------

    @classmethod
    def recover(cls, store: EventStore, game_row, player_rows, event_rows) -> "Game":
        game = cls(game_row["game_id"], game_row["timeout_seconds"], store)
        for row in player_rows:
            game.players[row["seat"]] = Player(row["seat"], row["name"], row["token"])
        for ev in event_rows:
            type_ = ev["type"]
            payload = json.loads(ev["payload"])
            if type_ == "game_started":
                game.status = "active"
                game.final_picks = {s: [] for s in game.players}
            elif type_ == "round_started":
                game.round_no = payload["round"]
                game.packs = {int(s): list(cards) for s, cards in payload["packs"].items()}
                game.deadline = payload["deadline"]
            elif type_ == "pick_submitted":
                if payload["round"] == game.round_no:
                    game.picks[payload["seat"]] = payload["card"]
                    if payload["auto"]:
                        game.auto_seats.add(payload["seat"])
            elif type_ == "round_revealed":
                picks = {int(s): c for s, c in payload["picks"].items()}
                game.revealed.append(
                    {"round": payload["round"], "picks": picks, "auto": payload["auto"]}
                )
                for seat, card in picks.items():
                    game.final_picks[seat].append(card)
                game.picks = {}
                game.auto_seats = set()
            elif type_ == "game_finished":
                game.status = "finished"
                game.deadline = None
        return game


class GameManager:
    """Owns all games, serializes mutations per game, pushes personalized
    snapshots over WebSocket, and drives the round clock.

    clock_mode "auto": a timer task auto-resolves rounds at their deadline.
    clock_mode "manual": rounds only resolve via force_timeout() — a
    controllable clock for tests and demos.
    """

    def __init__(self, store: EventStore, round_timeout: float = 30.0, clock_mode: str = "auto"):
        self.store = store
        self.round_timeout = round_timeout
        self.clock_mode = clock_mode
        self.games: dict[str, Game] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._conns: dict[str, dict[int, set]] = {}
        self._timers: dict[tuple[str, int], asyncio.Task] = {}

    # -- lifecycle --------------------------------------------------------

    def recover(self) -> None:
        for row in self.store.list_games():
            game = Game.recover(
                self.store,
                row,
                self.store.load_players(row["game_id"]),
                self.store.load_events(row["game_id"]),
            )
            self.games[game.id] = game
            if game.status == "active":
                # Resume the pending round; if the deadline passed while the
                # server was down, the timer fires immediately (delay 0).
                self._arm_timer(game)

    async def shutdown(self) -> None:
        for task in self._timers.values():
            task.cancel()
        if self._timers:
            await asyncio.gather(*self._timers.values(), return_exceptions=True)

    def _lock_for(self, game_id: str) -> asyncio.Lock:
        if game_id not in self._locks:
            self._locks[game_id] = asyncio.Lock()
        return self._locks[game_id]

    # -- games ------------------------------------------------------------

    def create_game(self) -> str:
        game_id = secrets.token_hex(4)
        self.store.create_game(game_id, self.round_timeout)
        self.games[game_id] = Game(game_id, self.round_timeout, self.store)
        return game_id

    def join_game(self, game_id: str, name: str) -> Player:
        game = self._get(game_id)
        player = game.add_player(name)
        self._arm_timer(game)  # arms the round-1 clock if this join started the game
        return player

    def authenticate(self, game_id: str, token: str) -> int | None:
        game = self.games.get(game_id)
        if not game:
            return None
        for seat, player in game.players.items():
            if secrets.compare_digest(player.token, token):
                return seat
        return None

    def _get(self, game_id: str) -> Game:
        game = self.games.get(game_id)
        if game is None:
            raise GameError("game_not_found", "unknown game id")
        return game

    # -- clock --------------------------------------------------------------

    def _arm_timer(self, game: Game) -> None:
        if self.clock_mode != "auto" or game.status != "active" or game.deadline is None:
            return
        key = (game.id, game.round_no)
        delay = max(0.0, game.deadline - time.time())
        self._timers[key] = asyncio.create_task(self._timeout_task(game.id, game.round_no, delay))

    async def _timeout_task(self, game_id: str, round_no: int, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            await self.force_timeout(game_id, round_no)
        except asyncio.CancelledError:
            pass

    async def force_timeout(self, game_id: str, round_no: int | None = None) -> bool:
        async with self._lock_for(game_id):
            game = self._get(game_id)
            changed = game.force_timeout(
                game.round_no if round_no is None else round_no, time.time()
            )
            if changed:
                self._arm_timer(game)
                snapshots = self._snapshots(game)
            else:
                snapshots = None
        if snapshots:
            await self._dispatch(game_id, snapshots)
        return changed

    # -- picks --------------------------------------------------------------

    async def submit_pick(self, game_id: str, seat: int, card_id: str, round_no: int) -> PickResult:
        async with self._lock_for(game_id):
            game = self._get(game_id)
            result = game.submit_pick(seat, card_id, round_no, time.time())
            snapshots = None
            if not result.already:
                if result.resolved:
                    self._arm_timer(game)
                snapshots = self._snapshots(game)
        if snapshots:
            await self._dispatch(game_id, snapshots)
        return result

    # -- connections --------------------------------------------------------

    async def connect(self, game_id: str, seat: int, ws) -> None:
        async with self._lock_for(game_id):
            game = self._get(game_id)
            game.connected.add(seat)
            self._conns.setdefault(game_id, {}).setdefault(seat, set()).add(ws)
            snapshots = self._snapshots(game)
        await self._dispatch(game_id, snapshots)

    async def disconnect(self, game_id: str, seat: int, ws) -> None:
        async with self._lock_for(game_id):
            game = self.games.get(game_id)
            if game is None:
                return
            self._conns.get(game_id, {}).get(seat, set()).discard(ws)
            if not self._conns.get(game_id, {}).get(seat):
                game.connected.discard(seat)
            snapshots = self._snapshots(game)
        await self._dispatch(game_id, snapshots)

    def _snapshots(self, game: Game) -> dict[int, dict]:
        now = time.time()
        return {seat: game.snapshot_for(seat, now) for seat in game.players}

    async def _dispatch(self, game_id: str, snapshots: dict[int, dict]) -> None:
        for seat, payload in snapshots.items():
            for ws in list(self._conns.get(game_id, {}).get(seat, ())):
                try:
                    await ws.send_text(json.dumps(payload))
                except Exception:
                    self._conns.get(game_id, {}).get(seat, set()).discard(ws)
