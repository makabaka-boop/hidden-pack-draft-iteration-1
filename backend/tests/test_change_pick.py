"""Change-pick (改选): a manually submitted pick can be privately re-selected
while its round is still pending. Covers validation, revision concurrency,
races against the completing submission and the clock (per-game lock
arbitration), restart recovery, and the hidden-information boundary."""
from __future__ import annotations

import asyncio
import json
import sqlite3

import httpx

from .conftest import (
    CARD_RE,
    ServerFixture,
    count_events,
    force_timeout,
    get_state,
    load_truth,
    start_game,
    ws_connect,
)


def pick_rows(db_path: str, game_id: str, round_no: int) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT seat, card, auto, revision FROM picks"
        " WHERE game_id = ? AND round = ? ORDER BY seat",
        (game_id, round_no),
    ).fetchall()
    conn.close()
    return rows


def pick_row(db_path: str, game_id: str, round_no: int, seat: int) -> tuple:
    rows = [r for r in pick_rows(db_path, game_id, round_no) if r[0] == seat]
    assert len(rows) == 1
    card, auto, revision = rows[0][1], rows[0][2], rows[0][3]
    return (card, auto, revision)


def tokens_in(payload: dict) -> set[str]:
    return set(CARD_RE.findall(json.dumps(payload)))


async def send_change(client, **kw) -> dict:
    """Send a change_pick request and wait for its ack or error."""
    await client.send({"type": "change_pick", **kw})
    return await client.until(
        lambda m: m.get("type") in ("change_ack", "error")
        and m.get("client_msg_id") == kw.get("client_msg_id")
    )


async def expect_change_error(client, code: str, **kw) -> dict:
    msg = await send_change(client, **kw)
    assert msg["type"] == "error" and msg["code"] == code, msg
    assert tokens_in(msg) == set()  # errors never carry card data
    return msg


async def test_change_pick_success_updates_everything(server):
    srv = server.srv
    game_id, players = await start_game(srv.http)
    clients = [await ws_connect(srv, game_id, p["seat"], p["token"]) for p in players]
    snaps = [await c.latest_snapshot(round=1) for c in clients]
    old_card = snaps[0]["your_pack"][0]["id"]
    new_card = snaps[0]["your_pack"][2]["id"]

    await clients[0].send({"type": "pick", "card": old_card, "round": 1})
    await clients[0].until(lambda m: m.get("type") == "pick_ack" and not m["already"])
    assert pick_row(server.db_path, game_id, 1, 0) == (old_card, 0, 1)

    await clients[0].send({"type": "change_pick", "card": new_card, "round": 1,
                           "expected_revision": 1, "client_msg_id": "chg-1"})
    ack = await clients[0].until(lambda m: m.get("client_msg_id") == "chg-1")
    assert ack["type"] == "change_ack"
    assert ack["card"] == new_card and ack["revision"] == 2

    # The changer's refreshed snapshot shows the new pick and the original pack.
    snap0 = await clients[0].until(
        lambda m: m.get("type") == "snapshot" and m.get("your_pick_revision") == 2
    )
    assert snap0["your_pick"]["id"] == new_card
    assert len(snap0["your_original_pack"]) == 5

    # One persistent commit: row updated, revision bumped, event appended.
    assert pick_row(server.db_path, game_id, 1, 0) == (new_card, 0, 2)
    assert count_events(server.db_path, game_id, "pick_changed", round_no=1) == 1
    assert count_events(server.db_path, game_id, "pick_submitted", round_no=1) == 1

    # The original submit endpoint stays idempotent — and now acknowledges
    # the current (changed) card.
    await clients[0].send({"type": "pick", "card": old_card, "round": 1})
    ack2 = await clients[0].until(lambda m: m.get("type") == "pick_ack" and m["already"])
    assert ack2["card"] == new_card
    assert count_events(server.db_path, game_id, "pick_submitted", round_no=1) == 1

    # Everyone else submits; the reveal and the pass both use the NEW card.
    for i in (1, 2, 3):
        await clients[i].send({"type": "pick", "card": snaps[i]["your_pack"][0]["id"], "round": 1})
    snap = await clients[0].latest_snapshot(round=2)
    assert snap["revealed"][0]["picks"]["0"]["id"] == new_card
    truth = load_truth(server.db_path, game_id)
    assert sorted(truth["packs"][2][1]) == sorted(
        c for c in truth["packs"][1][0] if c != new_card
    )
    assert old_card in truth["packs"][2][1]  # the changed-away card passes on
    for c in clients:
        await c.ws.close()


async def test_change_pick_validation_errors(server):
    srv = server.srv
    game_id, players = await start_game(srv.http)
    clients = [await ws_connect(srv, game_id, p["seat"], p["token"]) for p in players]
    snaps = [await c.latest_snapshot(round=1) for c in clients]
    truth = load_truth(server.db_path, game_id)
    foreign_card = truth["packs"][1][2][0]  # seat 2's card — not in seat 0's pack

    # Nothing submitted yet -> nothing to change.
    await expect_change_error(clients[1], "not_submitted", card="c01", round=1,
                              expected_revision=1, client_msg_id="e1")

    card_a, card_b, card_c = (snaps[0]["your_pack"][i]["id"] for i in (0, 1, 2))
    await clients[0].send({"type": "pick", "card": card_a, "round": 1})
    await clients[0].until(lambda m: m.get("type") == "pick_ack")

    # The new card must come from the seat's ORIGINAL pack for the round.
    await expect_change_error(clients[0], "card_not_in_pack", card=foreign_card, round=1,
                              expected_revision=1, client_msg_id="e2")
    # The round must be the current, still-pending one.
    await expect_change_error(clients[0], "round_not_active", card=card_b, round=2,
                              expected_revision=1, client_msg_id="e3")
    # The expected revision must match the current one.
    await expect_change_error(clients[0], "revision_conflict", card=card_b, round=1,
                              expected_revision=7, client_msg_id="e4")

    # A valid change bumps the revision; a stale expectation then fails.
    await clients[0].send({"type": "change_pick", "card": card_b, "round": 1,
                           "expected_revision": 1, "client_msg_id": "ok1"})
    ack = await clients[0].until(lambda m: m.get("client_msg_id") == "ok1")
    assert ack["type"] == "change_ack" and ack["revision"] == 2
    await expect_change_error(clients[0], "revision_conflict", card=card_c, round=1,
                              expected_revision=1, client_msg_id="e5")
    await clients[0].send({"type": "change_pick", "card": card_c, "round": 1,
                           "expected_revision": 2, "client_msg_id": "ok2"})
    ack = await clients[0].until(lambda m: m.get("client_msg_id") == "ok2")
    assert ack["type"] == "change_ack" and ack["revision"] == 3 and ack["card"] == card_c

    assert pick_row(server.db_path, game_id, 1, 0) == (card_c, 0, 3)
    assert count_events(server.db_path, game_id, "pick_changed", round_no=1) == 2

    # Once the game is over there is nothing to change.
    for _ in range(5):
        await force_timeout(srv.http, game_id)
    await expect_change_error(clients[0], "game_not_active", card=card_a, round=5,
                              expected_revision=3, client_msg_id="e6")
    for c in clients:
        await c.ws.close()


async def test_timeout_auto_pick_cannot_be_changed(server):
    srv = server.srv
    game_id, players = await start_game(srv.http)
    truth = load_truth(server.db_path, game_id)
    auto_card = min(truth["packs"][1][0])  # stable rule: smallest id

    # Seat 0 never submits; the clock auto-picks and the round resolves.
    result = await force_timeout(srv.http, game_id)
    assert result["resolved"] is True
    assert pick_row(server.db_path, game_id, 1, 0) == (auto_card, 1, 1)

    client = await ws_connect(srv, game_id, 0, players[0]["token"])
    snap = await client.latest_snapshot(round=2)
    assert snap["revealed"][0]["picks"]["0"]["id"] == auto_card
    assert 0 in snap["revealed"][0]["auto"]

    # Trying to "fix" the auto-pick after the fact fails and changes nothing.
    await expect_change_error(client, "round_not_active", card="c20", round=1,
                              expected_revision=1, client_msg_id="no")
    assert pick_row(server.db_path, game_id, 1, 0) == (auto_card, 1, 1)
    assert count_events(server.db_path, game_id, "pick_changed") == 0
    await client.ws.close()


async def test_change_committed_before_fourth_pick_is_revealed(server):
    """Change wins the lock before the 4th submission: the reveal and the
    pass both use the new card."""
    srv = server.srv
    game_id, players = await start_game(srv.http)
    clients = [await ws_connect(srv, game_id, p["seat"], p["token"]) for p in players]
    snaps = [await c.latest_snapshot(round=1) for c in clients]
    for i in (0, 1, 2):
        await clients[i].send({"type": "pick", "card": snaps[i]["your_pack"][0]["id"], "round": 1})
    await clients[0].until(lambda m: m.get("type") == "pick_ack")

    old_card = snaps[0]["your_pack"][0]["id"]
    new_card = snaps[0]["your_pack"][1]["id"]
    await clients[0].send({"type": "change_pick", "card": new_card, "round": 1,
                           "expected_revision": 1, "client_msg_id": "chg"})
    ack = await clients[0].until(lambda m: m.get("client_msg_id") == "chg")
    assert ack["type"] == "change_ack" and ack["revision"] == 2

    await clients[3].send({"type": "pick", "card": snaps[3]["your_pack"][0]["id"], "round": 1})
    snap = await clients[3].latest_snapshot(round=2)
    assert snap["revealed"][0]["picks"]["0"]["id"] == new_card
    truth = load_truth(server.db_path, game_id)
    assert sorted(truth["packs"][2][1]) == sorted(
        c for c in truth["packs"][1][0] if c != new_card
    )
    assert old_card in truth["packs"][2][1]
    for c in clients:
        await c.ws.close()


async def test_reveal_committed_before_change_rejects_it(server):
    """Reveal wins the lock first: the change fails and neither the pass nor
    the public result is altered."""
    srv = server.srv
    game_id, players = await start_game(srv.http)
    clients = [await ws_connect(srv, game_id, p["seat"], p["token"]) for p in players]
    snaps = [await c.latest_snapshot(round=1) for c in clients]
    old_card = snaps[0]["your_pack"][0]["id"]
    for i in range(4):
        await clients[i].send({"type": "pick", "card": snaps[i]["your_pack"][0]["id"], "round": 1})
    snap = await clients[0].latest_snapshot(round=2)
    assert snap["revealed"][0]["picks"]["0"]["id"] == old_card

    await expect_change_error(clients[0], "round_not_active",
                              card=snaps[0]["your_pack"][1]["id"], round=1,
                              expected_revision=1, client_msg_id="late-chg")
    assert pick_row(server.db_path, game_id, 1, 0) == (old_card, 0, 1)
    assert count_events(server.db_path, game_id, "pick_changed") == 0
    truth = load_truth(server.db_path, game_id)
    assert sorted(truth["packs"][2][1]) == sorted(
        c for c in truth["packs"][1][0] if c != old_card
    )
    for c in clients:
        await c.ws.close()


async def test_change_vs_fourth_submission_race(server):
    """The change and the completing submission fire at the same instant;
    the per-game lock serializes them into exactly one coherent outcome."""
    srv = server.srv
    game_id, players = await start_game(srv.http)
    clients = [await ws_connect(srv, game_id, p["seat"], p["token"]) for p in players]
    snaps = [await c.latest_snapshot(round=1) for c in clients]
    for i in (0, 1, 2):
        await clients[i].send({"type": "pick", "card": snaps[i]["your_pack"][0]["id"], "round": 1})
    # The three picks have landed before the race starts.
    await clients[3].until(
        lambda m: m.get("type") == "snapshot"
        and sum(p["submitted"] for p in m["players"]) == 3
    )
    old_card = snaps[0]["your_pack"][0]["id"]
    new_card = snaps[0]["your_pack"][1]["id"]

    start = asyncio.Event()  # concurrency barrier: both fire at once

    async def changer():
        await start.wait()
        await clients[0].send({"type": "change_pick", "card": new_card, "round": 1,
                               "expected_revision": 1, "client_msg_id": "race-chg"})

    async def fourth():
        await start.wait()
        await clients[3].send({"type": "pick", "card": snaps[3]["your_pack"][0]["id"], "round": 1})

    tasks = [asyncio.create_task(changer()), asyncio.create_task(fourth())]
    start.set()
    await asyncio.gather(*tasks)

    verdict = await clients[0].until(lambda m: m.get("client_msg_id") == "race-chg")
    snap = await clients[0].latest_snapshot(round=2)
    revealed_card = snap["revealed"][0]["picks"]["0"]["id"]
    if verdict["type"] == "change_ack":
        # Change committed first: the reveal uses the new card.
        assert verdict["revision"] == 2
        assert revealed_card == new_card
        assert pick_row(server.db_path, game_id, 1, 0) == (new_card, 0, 2)
        assert count_events(server.db_path, game_id, "pick_changed", round_no=1) == 1
    else:
        # Reveal committed first: the change failed without side effects.
        assert verdict["type"] == "error" and verdict["code"] == "round_not_active"
        assert revealed_card == old_card
        assert pick_row(server.db_path, game_id, 1, 0) == (old_card, 0, 1)
        assert count_events(server.db_path, game_id, "pick_changed", round_no=1) == 0
    # Either way: exactly one reveal, exactly four picks, coherent result.
    assert count_events(server.db_path, game_id, "round_revealed", round_no=1) == 1
    rows = pick_rows(server.db_path, game_id, 1)
    assert len(rows) == 4
    assert dict(snap["revealed"][0]["picks"].items()).keys() == {"0", "1", "2", "3"}
    for c in clients:
        await c.ws.close()


async def test_change_vs_timeout_race(server):
    """Change races the controllable clock; the lock settles it exactly once."""
    srv = server.srv
    game_id, players = await start_game(srv.http)
    clients = [await ws_connect(srv, game_id, p["seat"], p["token"]) for p in players]
    snaps = [await c.latest_snapshot(round=1) for c in clients]
    for i in (0, 1, 2):
        await clients[i].send({"type": "pick", "card": snaps[i]["your_pack"][0]["id"], "round": 1})
    await clients[3].until(
        lambda m: m.get("type") == "snapshot"
        and sum(p["submitted"] for p in m["players"]) == 3
    )
    old_card = snaps[0]["your_pack"][0]["id"]
    new_card = snaps[0]["your_pack"][1]["id"]

    start = asyncio.Event()

    async def changer():
        await start.wait()
        await clients[0].send({"type": "change_pick", "card": new_card, "round": 1,
                               "expected_revision": 1, "client_msg_id": "race-chg"})

    async def clock():
        await start.wait()
        async with httpx.AsyncClient() as http:
            await http.post(f"{srv.http}/api/games/{game_id}/timeout")

    tasks = [asyncio.create_task(changer()), asyncio.create_task(clock())]
    start.set()
    await asyncio.gather(*tasks)

    verdict = await clients[0].until(lambda m: m.get("client_msg_id") == "race-chg")
    snap = await clients[0].latest_snapshot(round=2)
    revealed = snap["revealed"][0]
    assert revealed["auto"] == [3]  # only the missing seat was auto-filled
    if verdict["type"] == "change_ack":
        assert revealed["picks"]["0"]["id"] == new_card
        assert pick_row(server.db_path, game_id, 1, 0) == (new_card, 0, 2)
    else:
        assert verdict["type"] == "error" and verdict["code"] == "round_not_active"
        assert revealed["picks"]["0"]["id"] == old_card
        assert pick_row(server.db_path, game_id, 1, 0) == (old_card, 0, 1)
    assert count_events(server.db_path, game_id, "round_revealed", round_no=1) == 1
    assert len(pick_rows(server.db_path, game_id, 1)) == 4
    for c in clients:
        await c.ws.close()


async def test_restart_recovers_changed_pick(tmp_path):
    """A changed pick survives a restart: replay restores the latest card
    and its revision, and the game can be played on from there."""
    fix = ServerFixture(str(tmp_path / "g.db"), round_timeout=30.0, clock_mode="manual")
    srv = await fix.start()
    game_id, players = await start_game(srv.http)
    c0 = await ws_connect(srv, game_id, 0, players[0]["token"])
    snap = await c0.latest_snapshot(round=1)
    old_card = snap["your_pack"][0]["id"]
    new_card = snap["your_pack"][1]["id"]
    pack_ids = [c["id"] for c in snap["your_pack"]]
    await c0.send({"type": "pick", "card": old_card, "round": 1})
    await c0.until(lambda m: m.get("type") == "pick_ack")
    await c0.send({"type": "change_pick", "card": new_card, "round": 1,
                   "expected_revision": 1, "client_msg_id": "chg"})
    ack = await c0.until(lambda m: m.get("client_msg_id") == "chg")
    assert ack["type"] == "change_ack" and ack["revision"] == 2
    await fix.stop()

    # Restart on the same database: the pending pick is the CHANGED one.
    srv = await fix.restart()
    state = await get_state(srv.http, game_id, players[0]["token"])
    assert state["status"] == "active" and state["round"] == 1
    assert state["your_pick"]["id"] == new_card
    assert state["your_pick_revision"] == 2
    assert [c["id"] for c in state["your_original_pack"]] == pack_ids

    # The restored revision gates further changes (stale ones are rejected).
    c0 = await ws_connect(srv, game_id, 0, players[0]["token"])
    await c0.latest_snapshot(round=1)
    third = next(i for i in pack_ids if i != new_card)
    await expect_change_error(c0, "revision_conflict", card=third, round=1,
                              expected_revision=1, client_msg_id="stale")
    await c0.send({"type": "change_pick", "card": third, "round": 1,
                   "expected_revision": 2, "client_msg_id": "chg2"})
    ack = await c0.until(lambda m: m.get("client_msg_id") == "chg2")
    assert ack["type"] == "change_ack" and ack["revision"] == 3

    # And the reveal after recovery uses the latest pick.
    await force_timeout(srv.http, game_id)
    truth = load_truth(fix.db_path, game_id)
    assert truth["picks"][1][0] == third
    assert pick_row(fix.db_path, game_id, 1, 0) == (third, 0, 3)
    await c0.ws.close()
    await fix.stop()


async def test_change_pick_hidden_information(server):
    """A change is invisible to the other seats: no WS message, no snapshot
    field, and no error response may reveal the old or the new card."""
    srv = server.srv
    game_id, players = await start_game(srv.http)
    clients = [await ws_connect(srv, game_id, p["seat"], p["token"]) for p in players]
    snaps = [await c.latest_snapshot(round=1) for c in clients]
    truth = load_truth(server.db_path, game_id)
    packs1 = truth["packs"][1]

    old_card = snaps[0]["your_pack"][0]["id"]
    new_card = snaps[0]["your_pack"][2]["id"]
    await clients[0].send({"type": "pick", "card": old_card, "round": 1})
    await clients[0].until(lambda m: m.get("type") == "pick_ack")
    # Everyone sees seat 0's "submitted" flag; freeze their inboxes there.
    for c in clients[1:]:
        await c.until(
            lambda m: m.get("type") == "snapshot" and m["players"][0]["submitted"]
        )
    baseline = {c.seat: len(c.messages) for c in clients[1:]}

    await clients[0].send({"type": "change_pick", "card": new_card, "round": 1,
                           "expected_revision": 1, "client_msg_id": "chg"})
    ack = await clients[0].until(lambda m: m.get("client_msg_id") == "chg")
    assert ack["type"] == "change_ack"

    # The other seats receive NOTHING — not even a hint that a change happened.
    for c in clients[1:]:
        await c.drain(settle=0.3)
        assert len(c.messages) == baseline[c.seat]
        assert old_card not in c.card_tokens_seen()
        assert new_card not in c.card_tokens_seen()

    # Their HTTP reconnect snapshots are equally blind.
    for p in players[1:]:
        state = await get_state(srv.http, game_id, p["token"])
        assert old_card not in tokens_in(state)
        assert new_card not in tokens_in(state)

    # Error responses carry no card data either.
    foreign = packs1[3][0]
    await clients[0].send({"type": "change_pick", "card": foreign, "round": 1,
                           "expected_revision": 2, "client_msg_id": "bad"})
    err = await clients[0].until(lambda m: m.get("client_msg_id") == "bad")
    assert err["type"] == "error" and err["code"] == "card_not_in_pack"
    assert tokens_in(err) == set()

    # The changer's OWN view shows the original pack and the current pick.
    state0 = await get_state(srv.http, game_id, players[0]["token"])
    assert {c["id"] for c in state0["your_original_pack"]} == set(packs1[0])
    assert state0["your_pick"]["id"] == new_card
    assert state0["your_pick_revision"] == 2

    # Reveal: the new card becomes public; the changed-away card only
    # reaches seat 1 through the pass — seats 2 and 3 never learn it.
    await force_timeout(srv.http, game_id)
    for c in clients:
        await c.latest_snapshot(round=2)
    truth = load_truth(server.db_path, game_id)
    assert truth["picks"][1][0] == new_card
    assert old_card in truth["packs"][2][1]  # passed to seat 1
    for c in (clients[2], clients[3]):
        assert old_card not in c.card_tokens_seen()
        assert new_card in c.card_tokens_seen()  # publicly revealed
    for c in clients:
        await c.ws.close()
