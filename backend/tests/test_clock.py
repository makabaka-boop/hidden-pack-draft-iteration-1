"""Auto clock: rounds resolve at their deadline without any client action,
including round 1 (armed when the 4th player joins)."""
from __future__ import annotations

import asyncio

import pytest

from .conftest import ServerFixture, get_state, start_game


async def test_auto_clock_starts_on_fourth_join(tmp_path):
    fix = ServerFixture(str(tmp_path / "g.db"), round_timeout=0.3, clock_mode="auto")
    srv = await fix.start()
    game_id, players = await start_game(srv.http)
    # Nobody picks anything, ever — the clock must drive the whole game.
    for _ in range(200):
        state = await get_state(srv.http, game_id, players[0]["token"])
        if state["status"] == "finished":
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail("game did not finish on the auto clock")
    assert len(state["revealed"]) == 5
    assert all(len(r["auto"]) == 4 for r in state["revealed"])
    await fix.stop()
