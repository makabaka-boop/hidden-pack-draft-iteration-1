"""Concurrency: duplicate clicks, simultaneous submissions, timeout races,
and reconnect idempotency — everything counts exactly once."""
from __future__ import annotations

import asyncio
import sqlite3

import httpx

from .conftest import (
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
        "SELECT seat, card, auto FROM picks WHERE game_id = ? AND round = ? ORDER BY seat",
        (game_id, round_no),
    ).fetchall()
    conn.close()
    return rows


async def test_duplicate_clicks_count_once(server):
    srv = server.srv
    game_id, players = await start_game(srv.http)
    client = await ws_connect(srv, game_id, 0, players[0]["token"])
    snap = await client.latest_snapshot(round=1)
    card = snap["your_pack"][0]["id"]

    # 20 identical clicks as fast as possible (double-click storms).
    for _ in range(20):
        await client.send({"type": "pick", "card": card, "round": 1, "client_msg_id": "dup"})
    await client.until(
        lambda m: m.get("type") == "pick_ack"
        and sum(1 for x in client.messages if x.get("type") == "pick_ack") >= 20
    )
    acks = [m for m in client.messages if m.get("type") == "pick_ack"]
    assert len(acks) == 20
    assert sum(1 for a in acks if a["already"] is False) == 1
    assert all(a["card"] == card for a in acks)

    # Follow-up clicks with DIFFERENT cards are still idempotent no-ops.
    other = snap["your_pack"][1]["id"]
    await client.send({"type": "pick", "card": other, "round": 1})
    ack = await client.until(lambda m: m.get("type") == "pick_ack" and m.get("already") is True)
    assert ack["card"] == card  # the first recorded pick stands

    assert pick_rows(server.db_path, game_id, 1) == [(0, card, 0)]
    assert count_events(server.db_path, game_id, "pick_submitted", round_no=1) == 1
    await client.ws.close()


async def test_concurrent_submissions_all_players_single_reveal(server):
    srv = server.srv
    game_id, players = await start_game(srv.http)
    clients = [await ws_connect(srv, game_id, p["seat"], p["token"]) for p in players]
    snaps = [await c.latest_snapshot(round=1) for c in clients]

    # All four submit at the same instant.
    await asyncio.gather(
        *(c.send({"type": "pick", "card": s["your_pack"][0]["id"], "round": 1})
          for c, s in zip(clients, snaps))
    )
    for c in clients:
        await c.latest_snapshot(round=2)

    # Exactly one pick per seat, exactly one reveal, packs passed correctly.
    rows = pick_rows(server.db_path, game_id, 1)
    assert len(rows) == 4 and [r[0] for r in rows] == [0, 1, 2, 3]
    assert count_events(server.db_path, game_id, "round_revealed", round_no=1) == 1
    truth = load_truth(server.db_path, game_id)
    for seat in range(4):
        assert len(truth["packs"][2][seat]) == 4
    for c in clients:
        await c.ws.close()


async def test_timeout_vs_manual_submit_race(server):
    """Every round: 3 players pick, then the 4th pick races the clock.
    The round must resolve exactly once, with exactly 4 recorded picks."""
    srv = server.srv
    game_id, players = await start_game(srv.http)
    clients = [await ws_connect(srv, game_id, p["seat"], p["token"]) for p in players]

    for round_no in range(1, 6):
        snaps = [await c.latest_snapshot(round=round_no) for c in clients]
        for i in range(3):
            await clients[i].send(
                {"type": "pick", "card": snaps[i]["your_pack"][0]["id"], "round": round_no}
            )
        await asyncio.sleep(0.05)  # let the three picks land

        async def manual_pick():
            await clients[3].send(
                {"type": "pick", "card": snaps[3]["your_pack"][0]["id"], "round": round_no}
            )

        async def timeout():
            async with httpx.AsyncClient() as http:
                await http.post(f"{srv.http}/api/games/{game_id}/timeout")

        await asyncio.gather(manual_pick(), timeout())

        for c in clients:
            await c.until(
                lambda m: m.get("type") == "snapshot"
                and (m.get("status") == "finished" or m.get("round") == round_no + 1)
            )
        # Whatever won the race: exactly 4 picks, exactly 1 reveal.
        assert len(pick_rows(server.db_path, game_id, round_no)) == 4
        assert count_events(server.db_path, game_id, "round_revealed", round_no=round_no) == 1

    truth = load_truth(server.db_path, game_id)
    for round_no in range(1, 6):
        assert len(truth["picks"][round_no]) == 4
    for c in clients:
        await c.ws.close()


async def test_reconnect_resubmit_is_idempotent(server):
    srv = server.srv
    game_id, players = await start_game(srv.http)
    client = await ws_connect(srv, game_id, 0, players[0]["token"])
    snap = await client.latest_snapshot(round=1)
    card = snap["your_pack"][0]["id"]
    await client.send({"type": "pick", "card": card, "round": 1})
    await client.until(lambda m: m.get("type") == "pick_ack")
    await client.ws.close()

    # Reconnect: the snapshot must show the recorded pick, not the pack.
    client2 = await ws_connect(srv, game_id, 0, players[0]["token"])
    snap2 = await client2.latest_snapshot(round=1)
    assert snap2["your_pick"]["id"] == card
    assert snap2["your_pack"] == []

    # Resubmitting a different card after reconnect changes nothing.
    await client2.send({"type": "pick", "card": "c99", "round": 1})
    ack = await client2.until(lambda m: m.get("type") == "pick_ack")
    assert ack["already"] is True and ack["card"] == card
    assert pick_rows(server.db_path, game_id, 1) == [(0, card, 0)]
    await client2.ws.close()


async def test_late_submission_after_round_resolved(server):
    srv = server.srv
    game_id, players = await start_game(srv.http)
    clients = [await ws_connect(srv, game_id, p["seat"], p["token"]) for p in players]
    snaps = [await c.latest_snapshot(round=1) for c in clients]
    chosen = snaps[0]["your_pack"][0]["id"]
    await clients[0].send({"type": "pick", "card": chosen, "round": 1})
    await clients[0].until(lambda m: m.get("type") == "pick_ack")  # consume first ack
    await force_timeout(srv.http, game_id)  # resolves round 1
    await clients[0].latest_snapshot(round=2)

    # A stale round-1 message arrives late: idempotent ack, no state change.
    await clients[0].send({"type": "pick", "card": snaps[0]["your_pack"][1]["id"],
                           "round": 1, "client_msg_id": "late"})
    ack = await clients[0].until(
        lambda m: m.get("type") == "pick_ack" and m.get("client_msg_id") == "late"
    )
    assert ack["already"] is True and ack["card"] == chosen
    assert pick_rows(server.db_path, game_id, 1)[0] == (0, chosen, 0)
    assert count_events(server.db_path, game_id, "round_revealed", round_no=1) == 1
    for c in clients:
        await c.ws.close()


async def test_http_state_reconnect_matches_ws(server):
    srv = server.srv
    game_id, players = await start_game(srv.http)
    state = await get_state(srv.http, game_id, players[2]["token"])
    assert state["you"]["seat"] == 2
    assert len(state["your_pack"]) == 5
    assert state["round"] == 1
