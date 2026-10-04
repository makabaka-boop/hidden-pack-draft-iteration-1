"""Happy-path flow, stable auto-pick rule, and public replay."""
from __future__ import annotations

from .conftest import (
    WSClient,
    count_events,
    force_timeout,
    get_replay,
    load_truth,
    start_game,
    ws_connect,
)


async def play_round(clients: list[WSClient], round_no: int) -> None:
    """Every client picks the first card of its current pack."""
    for c in clients:
        snap = await c.latest_snapshot(round=round_no, status="active")
        assert len(snap["your_pack"]) == 6 - round_no
        await c.send({"type": "pick", "card": snap["your_pack"][0]["id"], "round": round_no})
    for c in clients:
        await c.until(
            lambda m: m.get("type") == "snapshot"
            and (m.get("status") == "finished" or m.get("round") == round_no + 1)
        )


async def test_full_game_completes(server):
    srv = server.srv
    game_id, players = await start_game(srv.http)
    clients = [await ws_connect(srv, game_id, p["seat"], p["token"]) for p in players]

    for round_no in range(1, 6):
        await play_round(clients, round_no)

    finals = {}
    for c in clients:
        snap = await c.latest_snapshot(status="finished")
        assert len(snap["revealed"]) == 5
        assert len(snap["your_picks_all"]) == 5
        finals[snap["you"]["seat"]] = [card["id"] for card in snap["your_picks_all"]]

    # All 20 cards were drafted exactly once across the 4 players.
    all_picks = [card for picks in finals.values() for card in picks]
    assert len(all_picks) == 20 and len(set(all_picks)) == 20

    # Public replay matches what clients saw.
    replay = await get_replay(srv.http, game_id)
    assert replay["status"] == "finished"
    assert len(replay["rounds"]) == 5
    for seat, picks in replay["final_picks"].items():
        assert [c["id"] for c in picks] == finals[int(seat)]

    for c in clients:
        await c.ws.close()


async def test_reveal_only_after_all_four_submitted(server):
    srv = server.srv
    game_id, players = await start_game(srv.http)
    clients = [await ws_connect(srv, game_id, p["seat"], p["token"]) for p in players]
    snaps = [await c.latest_snapshot(round=1) for c in clients]

    # Three players pick: no reveal may happen yet.
    for i in range(3):
        await clients[i].send({"type": "pick", "card": snaps[i]["your_pack"][0]["id"], "round": 1})
    await clients[3].drain(settle=0.3)
    assert all(m.get("round") != 2 for m in clients[3].messages if m["type"] == "snapshot")
    assert count_events(server.db_path, game_id, "round_revealed") == 0

    # The fourth pick triggers the simultaneous reveal for everyone.
    await clients[3].send({"type": "pick", "card": snaps[3]["your_pack"][0]["id"], "round": 1})
    for c in clients:
        snap = await c.latest_snapshot(round=2)
        assert len(snap["revealed"]) == 1
        assert len(snap["revealed"][0]["picks"]) == 4
    assert count_events(server.db_path, game_id, "round_revealed", round_no=1) == 1

    for c in clients:
        await c.ws.close()


async def test_timeout_auto_pick_is_stable(server):
    srv = server.srv
    game_id, players = await start_game(srv.http)
    truth = load_truth(server.db_path, game_id)

    # Nobody picks; the controllable clock fires.
    result = await force_timeout(srv.http, game_id)
    assert result["resolved"] is True

    truth = load_truth(server.db_path, game_id)
    picks = truth["picks"][1]
    assert len(picks) == 4
    # Stable rule: smallest card id of each pack — reproducible, timing-free.
    for seat in range(4):
        assert picks[seat] == min(truth["packs"][1][seat])

    replay = await get_replay(srv.http, game_id)
    assert replay["rounds"][0]["auto"] == [0, 1, 2, 3]


async def test_leftovers_pass_to_next_seat(server):
    srv = server.srv
    game_id, players = await start_game(srv.http)
    clients = [await ws_connect(srv, game_id, p["seat"], p["token"]) for p in players]
    await play_round(clients, 1)

    truth = load_truth(server.db_path, game_id)
    picks = truth["picks"][1]
    for seat in range(4):
        # Seat s receives the leftovers of seat (s - 1) mod 4.
        source = (seat - 1) % 4
        expected = [c for c in truth["packs"][1][source] if c != picks[source]]
        assert sorted(truth["packs"][2][seat]) == sorted(expected)
        assert len(truth["packs"][2][seat]) == 4

    for c in clients:
        await c.ws.close()
