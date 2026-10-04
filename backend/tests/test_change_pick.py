"""Change-pick (改选): a manually submitted pick may be privately switched to
another card of the same original pack while the round is still unrevealed.

Covers: happy path + revision bumps, validation errors (no card data leaks),
idempotent retries, races against the 4th submission and the timeout reveal
(decided by the per-game lock, synchronized here with asyncio.Barrier),
restart recovery of the latest pending pick, auto-pick lockout, and the
hidden-information boundary (other players only ever learn "submitted").
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from app.game import Game, GameError
from app.store import EventStore

from .conftest import (
    CARD_RE,
    ServerFixture,
    count_events,
    force_timeout,
    get_replay,
    get_state,
    load_truth,
    start_game,
    ws_connect,
)


def pick_row(db_path: str, game_id: str, round_no: int, seat: int) -> tuple:
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT card, auto, revision FROM picks WHERE game_id = ? AND round = ? AND seat = ?",
        (game_id, round_no, seat),
    ).fetchone()
    conn.close()
    return row


def tokens_in(payload) -> set[str]:
    return set(CARD_RE.findall(json.dumps(payload)))


async def submit(client, card: str, round_no: int) -> None:
    await client.send({"type": "pick", "card": card, "round": round_no})
    await client.until(lambda m: m.get("type") == "pick_ack" and m.get("already") is False)


async def change(client, card: str, round_no: int, revision: int, msg_id: str) -> dict:
    """Send a change_pick and return the ack or error that answers it."""
    await client.send(
        {
            "type": "change_pick",
            "card": card,
            "round": round_no,
            "revision": revision,
            "client_msg_id": msg_id,
        }
    )
    return await client.until(
        lambda m: m.get("client_msg_id") == msg_id
        and m.get("type") in ("change_pick_ack", "error")
    )


async def test_change_pick_happy_path(server):
    srv = server.srv
    game_id, players = await start_game(srv.http)
    clients = [await ws_connect(srv, game_id, p["seat"], p["token"]) for p in players]
    snaps = [await c.latest_snapshot(round=1) for c in clients]

    pack0 = [c["id"] for c in snaps[0]["your_pack"]]
    first, second = pack0[0], pack0[2]
    await submit(clients[0], first, 1)
    assert snaps[0]["your_pick"] is None  # baseline: not submitted in first snap

    # Seat 0 reconsiders and privately switches to another card of its pack.
    ack = await change(clients[0], second, 1, revision=1, msg_id="chg1")
    assert ack["type"] == "change_pick_ack"
    assert ack["card"] == second and ack["revision"] == 2 and ack["already"] is False

    # The changer's own snapshot reflects the new pick and revision, and the
    # original pack stays visible to the owner for further changes.
    snap = await clients[0].until(
        lambda m: m.get("type") == "snapshot" and m.get("your_pick_revision") == 2
    )
    assert snap["your_pick"]["id"] == second
    assert snap["can_change_pick"] is True
    assert sorted(c["id"] for c in snap["your_pack"]) == sorted(pack0)

    # One persistent commit: pick row updated, revision bumped, event appended.
    assert pick_row(server.db_path, game_id, 1, 0) == (second, 0, 2)
    assert count_events(server.db_path, game_id, "pick_submitted", round_no=1) == 1
    assert count_events(server.db_path, game_id, "pick_changed", round_no=1) == 1

    # A second change chains on the new revision.
    ack2 = await change(clients[0], first, 1, revision=2, msg_id="chg2")
    assert ack2["type"] == "change_pick_ack" and ack2["revision"] == 3
    assert pick_row(server.db_path, game_id, 1, 0) == (first, 0, 3)

    # Everyone else submits; the reveal must use the LATEST card, and the
    # leftovers passed on must be computed from it.
    for i in (1, 2, 3):
        await clients[i].send(
            {"type": "pick", "card": snaps[i]["your_pack"][0]["id"], "round": 1}
        )
    revealed = None
    for c in clients:
        snap = await c.latest_snapshot(round=2)
        revealed = snap["revealed"][0]
    assert revealed["picks"]["0"]["id"] == first
    truth = load_truth(server.db_path, game_id)
    assert truth["picks"][1][0] == first
    assert truth["revisions"][(1, 0)] == 3
    # Passing: seat 1 receives seat 0's leftovers without the changed pick.
    assert sorted(truth["packs"][2][1]) == sorted(c for c in truth["packs"][1][0] if c != first)

    for c in clients:
        await c.ws.close()


async def test_change_pick_validation_errors_carry_no_card_data(server):
    srv = server.srv
    game_id, players = await start_game(srv.http)
    clients = [await ws_connect(srv, game_id, p["seat"], p["token"]) for p in players]
    snaps = [await c.latest_snapshot(round=1) for c in clients]
    pack0 = [c["id"] for c in snaps[0]["your_pack"]]
    pack1 = [c["id"] for c in snaps[1]["your_pack"]]

    # Not submitted yet: nothing to change.
    rep = await change(clients[0], pack0[0], 1, revision=1, msg_id="e1")
    assert rep["type"] == "error" and rep["code"] == "no_pick_to_change"

    await submit(clients[0], pack0[0], 1)

    # Card from someone else's pack / unknown card.
    rep = await change(clients[0], pack1[0], 1, revision=1, msg_id="e2")
    assert rep["type"] == "error" and rep["code"] == "card_not_in_pack"
    rep = await change(clients[0], "c99", 1, revision=1, msg_id="e3")
    assert rep["type"] == "error" and rep["code"] == "card_not_in_pack"

    # Stale expected revision.
    rep = await change(clients[0], pack0[1], 1, revision=7, msg_id="e4")
    assert rep["type"] == "error" and rep["code"] == "revision_conflict"

    # Changing to the card already picked is a no-op request.
    rep = await change(clients[0], pack0[0], 1, revision=1, msg_id="e5")
    assert rep["type"] == "error" and rep["code"] == "no_change"

    # A round that is not the current one.
    rep = await change(clients[0], pack0[1], 3, revision=1, msg_id="e6")
    assert rep["type"] == "error" and rep["code"] == "round_not_active"

    # None of the error responses may contain any card id.
    errors = [m for m in clients[0].messages if m.get("type") == "error"]
    assert len(errors) == 6
    for e in errors:
        assert tokens_in(e) == set()

    # After the reveal the window is closed: change fails and changes nothing.
    for i in (1, 2, 3):
        await clients[i].send(
            {"type": "pick", "card": snaps[i]["your_pack"][0]["id"], "round": 1}
        )
    await clients[0].latest_snapshot(round=2)
    rep = await change(clients[0], pack0[1], 1, revision=1, msg_id="e7")
    assert rep["type"] == "error" and rep["code"] == "round_not_active"
    assert tokens_in(rep) == set()
    assert pick_row(server.db_path, game_id, 1, 0) == (pack0[0], 0, 1)
    assert count_events(server.db_path, game_id, "pick_changed") == 0

    for c in clients:
        await c.ws.close()


async def test_change_pick_retry_is_idempotent(server):
    """A lost ack leads the client to retry the exact same change; the retry
    must be acknowledged without recording a second change."""
    srv = server.srv
    game_id, players = await start_game(srv.http)
    client = await ws_connect(srv, game_id, 0, players[0]["token"])
    snap = await client.latest_snapshot(round=1)
    pack = [c["id"] for c in snap["your_pack"]]
    await submit(client, pack[0], 1)

    ack = await change(client, pack[1], 1, revision=1, msg_id="once")
    assert ack["type"] == "change_pick_ack" and ack["already"] is False

    retry = await change(client, pack[1], 1, revision=1, msg_id="retry")
    assert retry["type"] == "change_pick_ack"
    assert retry["already"] is True and retry["revision"] == 2

    assert pick_row(server.db_path, game_id, 1, 0) == (pack[1], 0, 2)
    assert count_events(server.db_path, game_id, "pick_changed", round_no=1) == 1
    await client.ws.close()


async def test_change_then_fourth_pick_reveals_new_card(server):
    """Deterministic order: the change commits first, so the simultaneous
    reveal (triggered by the 4th submission) uses the new card."""
    srv = server.srv
    game_id, players = await start_game(srv.http)
    clients = [await ws_connect(srv, game_id, p["seat"], p["token"]) for p in players]
    snaps = [await c.latest_snapshot(round=1) for c in clients]
    pack0 = [c["id"] for c in snaps[0]["your_pack"]]

    await submit(clients[0], pack0[0], 1)
    for i in (1, 2):
        await clients[i].send(
            {"type": "pick", "card": snaps[i]["your_pack"][0]["id"], "round": 1}
        )
    ack = await change(clients[0], pack0[1], 1, revision=1, msg_id="win")
    assert ack["type"] == "change_pick_ack"

    await clients[3].send(
        {"type": "pick", "card": snaps[3]["your_pack"][0]["id"], "round": 1}
    )
    snap = await clients[3].latest_snapshot(round=2)
    assert snap["revealed"][0]["picks"]["0"]["id"] == pack0[1]
    for c in clients:
        await c.ws.close()


async def test_reveal_then_change_fails_and_changes_nothing(server):
    """Deterministic order: the 4th submission reveals first, so the late
    change fails and neither passing nor the public result is altered."""
    srv = server.srv
    game_id, players = await start_game(srv.http)
    clients = [await ws_connect(srv, game_id, p["seat"], p["token"]) for p in players]
    snaps = [await c.latest_snapshot(round=1) for c in clients]
    pack0 = [c["id"] for c in snaps[0]["your_pack"]]

    await submit(clients[0], pack0[0], 1)
    for i in (1, 2, 3):
        await clients[i].send(
            {"type": "pick", "card": snaps[i]["your_pack"][0]["id"], "round": 1}
        )
    snap = await clients[0].latest_snapshot(round=2)
    assert snap["revealed"][0]["picks"]["0"]["id"] == pack0[0]

    rep = await change(clients[0], pack0[1], 1, revision=1, msg_id="late")
    assert rep["type"] == "error" and rep["code"] == "round_not_active"

    truth = load_truth(server.db_path, game_id)
    assert truth["picks"][1][0] == pack0[0]
    assert count_events(server.db_path, game_id, "pick_changed") == 0
    # Passing was computed from the ORIGINAL pick.
    assert sorted(truth["packs"][2][1]) == sorted(
        c for c in truth["packs"][1][0] if c != pack0[0]
    )
    for c in clients:
        await c.ws.close()


async def test_change_vs_fourth_pick_race(server):
    """Barrier-synchronized race: the change and the 4th submission are fired
    at the same instant. The per-game lock decides; either way the outcome
    must be consistent — reveal uses the new card iff the change committed,
    exactly one reveal, exactly one pick row per seat. Staggered iterations
    pin down each lock order; unstaggered ones accept either."""
    srv = server.srv
    # stagger: None = truly simultaneous, "change"/"reveal" = that side gets
    # a head start and MUST win the lock.
    for stagger in (None, None, "change", "reveal"):
        game_id, players = await start_game(srv.http)
        clients = [await ws_connect(srv, game_id, p["seat"], p["token"]) for p in players]
        snaps = [await c.latest_snapshot(round=1) for c in clients]
        pack0 = [c["id"] for c in snaps[0]["your_pack"]]
        await submit(clients[0], pack0[0], 1)
        for i in (1, 2):
            await clients[i].send(
                {"type": "pick", "card": snaps[i]["your_pack"][0]["id"], "round": 1}
            )
        await asyncio.sleep(0.05)  # let the three picks land before the race

        barrier = asyncio.Barrier(3)

        async def racer_change():
            await barrier.wait()
            if stagger == "reveal":
                await asyncio.sleep(0.1)
            await clients[0].send(
                {"type": "change_pick", "card": pack0[1], "round": 1,
                 "revision": 1, "client_msg_id": "race"}
            )

        async def racer_fourth():
            await barrier.wait()
            if stagger == "change":
                await asyncio.sleep(0.1)
            await clients[3].send(
                {"type": "pick", "card": snaps[3]["your_pack"][0]["id"], "round": 1}
            )

        async def racer_go():
            await barrier.wait()

        await asyncio.gather(racer_change(), racer_fourth(), racer_go())

        reply = await clients[0].until(
            lambda m: m.get("client_msg_id") == "race"
            and m.get("type") in ("change_pick_ack", "error")
        )
        snap = await clients[0].latest_snapshot(round=2)
        revealed_card = snap["revealed"][0]["picks"]["0"]["id"]

        assert count_events(server.db_path, game_id, "round_revealed", round_no=1) == 1
        if reply["type"] == "change_pick_ack":
            assert stagger != "reveal"  # a staggered reveal must win the lock
            assert revealed_card == pack0[1]
            assert pick_row(server.db_path, game_id, 1, 0) == (pack0[1], 0, 2)
            assert count_events(server.db_path, game_id, "pick_changed", round_no=1) == 1
        else:
            assert stagger != "change"  # a staggered change must win the lock
            assert reply["code"] == "round_not_active"
            assert revealed_card == pack0[0]
            assert pick_row(server.db_path, game_id, 1, 0) == (pack0[0], 0, 1)
            assert count_events(server.db_path, game_id, "pick_changed") == 0
        for c in clients:
            await c.ws.close()


async def test_change_vs_timeout_race(server):
    """Same race against the controllable clock: change vs timeout reveal.
    If the timeout commits first the change fails; if the change commits
    first the timeout reveal uses the new card."""
    srv = server.srv
    for stagger in (None, None, "change", "reveal"):
        game_id, players = await start_game(srv.http)
        client0 = await ws_connect(srv, game_id, 0, players[0]["token"])
        snap = await client0.latest_snapshot(round=1)
        pack0 = [c["id"] for c in snap["your_pack"]]
        await submit(client0, pack0[0], 1)

        barrier = asyncio.Barrier(3)

        async def racer_change():
            await barrier.wait()
            if stagger == "reveal":
                await asyncio.sleep(0.1)
            await client0.send(
                {"type": "change_pick", "card": pack0[1], "round": 1,
                 "revision": 1, "client_msg_id": "race"}
            )

        async def racer_timeout():
            await barrier.wait()
            if stagger == "change":
                await asyncio.sleep(0.1)
            await force_timeout(srv.http, game_id)

        async def racer_go():
            await barrier.wait()

        await asyncio.gather(racer_change(), racer_timeout(), racer_go())

        reply = await client0.until(
            lambda m: m.get("client_msg_id") == "race"
            and m.get("type") in ("change_pick_ack", "error")
        )
        snap = await client0.latest_snapshot(round=2)
        revealed_card = snap["revealed"][0]["picks"]["0"]["id"]
        assert count_events(server.db_path, game_id, "round_revealed", round_no=1) == 1
        if reply["type"] == "change_pick_ack":
            assert stagger != "reveal"
            assert revealed_card == pack0[1]
        else:
            assert stagger != "change"
            assert reply["code"] == "round_not_active"
            assert revealed_card == pack0[0]
        await client0.ws.close()


async def test_timeout_reveal_then_change_fails(server):
    """Deterministic: the clock reveals the round, a later change is rejected
    and the public result keeps the original card."""
    srv = server.srv
    game_id, players = await start_game(srv.http)
    client0 = await ws_connect(srv, game_id, 0, players[0]["token"])
    snap = await client0.latest_snapshot(round=1)
    pack0 = [c["id"] for c in snap["your_pack"]]
    await submit(client0, pack0[0], 1)

    await force_timeout(srv.http, game_id)  # auto-picks 1..3, reveals round 1
    snap = await client0.latest_snapshot(round=2)
    assert snap["revealed"][0]["picks"]["0"]["id"] == pack0[0]

    rep = await change(client0, pack0[1], 1, revision=1, msg_id="too-late")
    assert rep["type"] == "error" and rep["code"] == "round_not_active"
    assert pick_row(server.db_path, game_id, 1, 0) == (pack0[0], 0, 1)
    assert count_events(server.db_path, game_id, "pick_changed") == 0
    await client0.ws.close()


async def test_change_pick_survives_restart(tmp_path):
    """Restart with a changed pick pending: replay restores the LATEST card
    and its revision, and the post-restart reveal uses it."""
    fix = ServerFixture(str(tmp_path / "g.db"), round_timeout=30.0, clock_mode="manual")
    srv = await fix.start()
    game_id, players = await start_game(srv.http)
    client0 = await ws_connect(srv, game_id, 0, players[0]["token"])
    snap = await client0.latest_snapshot(round=1)
    pack0 = [c["id"] for c in snap["your_pack"]]
    await submit(client0, pack0[0], 1)
    ack = await change(client0, pack0[3], 1, revision=1, msg_id="pre-restart")
    assert ack["type"] == "change_pick_ack" and ack["revision"] == 2
    await fix.stop()

    srv = await fix.restart()
    state = await get_state(srv.http, game_id, players[0]["token"])
    assert state["status"] == "active" and state["round"] == 1
    assert state["your_pick"]["id"] == pack0[3]
    assert state["your_pick_revision"] == 2
    assert state["can_change_pick"] is True
    assert sorted(c["id"] for c in state["your_pack"]) == sorted(pack0)

    # The recovered game reveals with the changed card (timeout auto-picks
    # the rest), and further changes still chain on the recovered revision.
    await force_timeout(srv.http, game_id)
    replay = await get_replay(srv.http, game_id)
    assert replay["rounds"][0]["picks"]["0"]["id"] == pack0[3]
    truth = load_truth(fix.db_path, game_id)
    assert truth["picks"][1][0] == pack0[3]
    assert truth["revisions"][(1, 0)] == 2
    await fix.stop()


async def test_change_pick_hidden_information(server):
    """A change is private: other players get no message at all, their HTTP
    snapshots only ever say "submitted", and nothing leaks the old or the
    new card. The changer sees their own pack and current pick."""
    srv = server.srv
    game_id, players = await start_game(srv.http)
    clients = [await ws_connect(srv, game_id, p["seat"], p["token"]) for p in players]
    snaps = [await c.latest_snapshot(round=1) for c in clients]
    truth = load_truth(server.db_path, game_id)
    packs1 = truth["packs"][1]
    pack0 = [c["id"] for c in snaps[0]["your_pack"]]

    await submit(clients[0], pack0[0], 1)
    # Everyone observes the submission status update, then goes quiet.
    for c in clients[1:]:
        await c.until(
            lambda m: m.get("type") == "snapshot" and m["players"][0]["submitted"]
        )
    for c in clients:
        await c.drain(settle=0.2)
    baseline = {c.seat: len(c.messages) for c in clients[1:]}

    ack = await change(clients[0], pack0[1], 1, revision=1, msg_id="secret")
    assert ack["type"] == "change_pick_ack"
    await clients[0].until(
        lambda m: m.get("type") == "snapshot" and m.get("your_pick_revision") == 2
    )
    for c in clients[1:]:
        await c.drain(settle=0.3)

    # Other players received NOTHING about the change.
    for c in clients[1:]:
        assert len(c.messages) == baseline[c.seat]
        assert pack0[0] not in c.card_tokens_seen()
        assert pack0[1] not in c.card_tokens_seen()

    # Their HTTP reconnect snapshots say only "submitted" — no cards of seat 0.
    for p in players[1:]:
        state = await get_state(srv.http, game_id, p["token"])
        assert state["players"][0]["submitted"] is True
        allowed = set(packs1[p["seat"]])
        assert tokens_in(state) <= allowed

    # The changer's own view: original pack + current pick, nothing hidden.
    state0 = await get_state(srv.http, game_id, players[0]["token"])
    assert state0["your_pick"]["id"] == pack0[1]
    assert sorted(c["id"] for c in state0["your_pack"]) == sorted(pack0)

    for c in clients:
        await c.ws.close()


def test_auto_pick_cannot_be_changed(tmp_path):
    """Timeout auto-picks are locked. A pending auto pick only exists inside
    the resolving call in normal play, so this drives the state machine
    directly (white-box) to cover the guard."""
    store = EventStore(str(tmp_path / "g.db"))
    store.create_game("g1", 30.0)
    game = Game("g1", 30.0, store)
    for i in range(4):
        game.add_player(f"P{i}")
    pack0 = list(game.packs[0])
    game._record_pick(0, pack0[0], auto=True)  # what force_timeout records
    with pytest.raises(GameError) as exc:
        game.change_pick(0, pack0[1], 1, expected_revision=1)
    assert exc.value.code == "auto_pick_locked"
    assert game.picks[0] == pack0[0]
    assert store.count_events("g1", "pick_changed") == 0
    store.close()
