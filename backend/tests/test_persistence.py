"""Persistence: the event log survives restarts — pending rounds resume,
recorded picks are preserved, and final results replay after recovery."""
from __future__ import annotations

import asyncio

import pytest

from .conftest import (
    ServerFixture,
    force_timeout,
    get_replay,
    get_state,
    load_truth,
    start_game,
    ws_connect,
)


async def test_restart_recovers_pending_round(tmp_path):
    fix = ServerFixture(str(tmp_path / "g.db"), round_timeout=30.0, clock_mode="manual")
    srv = await fix.start()
    game_id, players = await start_game(srv.http)

    # Round 1: seats 0 and 1 pick, then the server goes down mid-round.
    c0 = await ws_connect(srv, game_id, 0, players[0]["token"])
    c1 = await ws_connect(srv, game_id, 1, players[1]["token"])
    snap0 = await c0.latest_snapshot(round=1)
    snap1 = await c1.latest_snapshot(round=1)
    pick0 = snap0["your_pack"][1]["id"]
    pick1 = snap1["your_pack"][3]["id"]
    await c0.send({"type": "pick", "card": pick0, "round": 1})
    await c1.send({"type": "pick", "card": pick1, "round": 1})
    await asyncio.sleep(0.1)
    await fix.stop()

    # Restart on the same database: pending round is restored exactly.
    srv = await fix.restart()
    state0 = await get_state(srv.http, game_id, players[0]["token"])
    assert state0["status"] == "active" and state0["round"] == 1
    assert state0["your_pick"]["id"] == pick0
    assert state0["your_pack"] == []  # already picked: no pack shown
    state2 = await get_state(srv.http, game_id, players[2]["token"])
    assert state2["your_pick"] is None
    assert len(state2["your_pack"]) == 5  # still pending, decision intact

    # The game can be played to completion after the restart.
    truth = load_truth(fix.db_path, game_id)
    assert truth["picks"][1] == {0: pick0, 1: pick1}
    await force_timeout(srv.http, game_id)  # auto-picks seats 2,3 -> reveal r1
    for round_no in range(2, 6):
        await force_timeout(srv.http, game_id)
    replay = await get_replay(srv.http, game_id)
    assert replay["status"] == "finished"
    assert len(replay["rounds"]) == 5
    assert replay["rounds"][0]["picks"]["0"]["id"] == pick0
    assert replay["rounds"][0]["picks"]["1"]["id"] == pick1
    assert replay["rounds"][0]["auto"] == [2, 3]
    await fix.stop()


async def test_restart_replays_final_results(tmp_path):
    fix = ServerFixture(str(tmp_path / "g.db"), round_timeout=30.0, clock_mode="manual")
    srv = await fix.start()
    game_id, players = await start_game(srv.http)
    for _ in range(5):
        await force_timeout(srv.http, game_id)  # auto-pick everything
    before = await get_replay(srv.http, game_id)
    assert before["status"] == "finished"
    await fix.stop()

    srv = await fix.restart()
    after = await get_replay(srv.http, game_id)
    assert after == before  # final pick results replay identically
    state = await get_state(srv.http, game_id, players[0]["token"])
    assert state["status"] == "finished"
    assert len(state["your_picks_all"]) == 5
    await fix.stop()


async def test_auto_clock_resolves_overdue_round_after_restart(tmp_path):
    """If the deadline passed while the server was down, the recovered game
    resolves the pending round immediately (auto clock)."""
    fix = ServerFixture(str(tmp_path / "g.db"), round_timeout=0.2, clock_mode="auto")
    srv = await fix.start()
    game_id, players = await start_game(srv.http)
    await fix.stop()  # go down before/around the deadline

    srv = await fix.restart()
    # No client action: the armed timer must resolve round 1 by itself.
    for _ in range(100):
        state = await get_state(srv.http, game_id, players[0]["token"])
        if state["revealed"]:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("round 1 was not auto-resolved after restart")
    assert state["revealed"][0]["auto"] == [0, 1, 2, 3]
    await fix.stop()
