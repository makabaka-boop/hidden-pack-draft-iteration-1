"""Hidden-information boundary: no client message, reconnect snapshot, or
error response may ever reveal another player's current pack or unrevealed
pick. Ground truth (secret packs/picks) is read from the server-private
event log and checked against everything each client actually received."""
from __future__ import annotations

import json

import httpx

from .conftest import CARD_RE, get_state, load_truth, start_game, ws_connect


def tokens_in(payload: dict) -> set[str]:
    return set(CARD_RE.findall(json.dumps(payload)))


def assert_only_allowed(client, allowed: set[str], phase: str) -> None:
    seen = client.card_tokens_seen()
    leaked = seen - allowed
    assert not leaked, f"[{phase}] seat {client.seat} saw forbidden cards: {leaked}"


async def test_hidden_information_boundary(server):
    srv = server.srv
    game_id, players = await start_game(srv.http)
    clients = [await ws_connect(srv, game_id, p["seat"], p["token"]) for p in players]
    snaps = [await c.latest_snapshot(round=1) for c in clients]

    truth = load_truth(server.db_path, game_id)
    packs1 = truth["packs"][1]

    # Phase 1 — nobody has picked: each client may only know its own pack.
    for c in clients:
        assert_only_allowed(c, set(packs1[c.seat]), "round1-pristine")
        assert len(snaps[c.seat]["your_pack"]) == 5

    # Phase 2 — seats 0 and 1 pick secretly. Their picks must not reach 2/3,
    # and must not reach each other either.
    pick0 = snaps[0]["your_pack"][0]["id"]
    pick1 = snaps[1]["your_pack"][2]["id"]
    await clients[0].send({"type": "pick", "card": pick0, "round": 1})
    await clients[1].send({"type": "pick", "card": pick1, "round": 1})
    for c in clients:
        await c.drain(settle=0.3)

    assert_only_allowed(clients[2], set(packs1[2]), "round1-partial->seat2")
    assert_only_allowed(clients[3], set(packs1[3]), "round1-partial->seat3")
    assert pick0 not in clients[2].card_tokens_seen()
    assert pick0 not in clients[3].card_tokens_seen()
    assert pick1 not in clients[3].card_tokens_seen()
    assert pick1 not in clients[0].card_tokens_seen()
    assert pick0 not in clients[1].card_tokens_seen()
    # Submission STATUS is public meta-information; card contents are not.
    snap = await clients[2].until(
        lambda m: m.get("type") == "snapshot"
        and sum(p["submitted"] for p in m["players"]) == 2
    )
    assert len(snap["your_pack"]) == 5  # seat 2 hasn't picked: still sees own pack

    # Phase 3 — everyone picks: simultaneous reveal. Now round-1 picks are
    # public, but round-2 packs of OTHERS must stay hidden.
    await clients[2].send({"type": "pick", "card": snaps[2]["your_pack"][0]["id"], "round": 1})
    await clients[3].send({"type": "pick", "card": snaps[3]["your_pack"][0]["id"], "round": 1})
    revealed_picks = set()
    for c in clients:
        snap = await c.latest_snapshot(round=2)
        assert len(snap["revealed"]) == 1
        assert len(snap["your_pack"]) == 4
        revealed_picks.update(card["id"] for card in snap["revealed"][0]["picks"].values())
    assert len(revealed_picks) == 4

    truth = load_truth(server.db_path, game_id)
    packs2 = truth["packs"][2]
    for c in clients:
        # Authoritative boundary: c may only ever see its own packs (past and
        # current) plus publicly revealed picks. Note c's neighbor's round-2
        # pack legitimately contains cards c saw in its own round-1 pack —
        # that is inherent to drafting, not a server leak.
        allowed = set(packs1[c.seat]) | set(packs2[c.seat]) | revealed_picks
        assert_only_allowed(c, allowed, "round2-start")

    # Phase 4 — reconnect mid-round-2 (fresh WS + HTTP snapshot): same boundary.
    await clients[1].ws.close()
    reconnected = await ws_connect(srv, game_id, 1, players[1]["token"])
    await reconnected.latest_snapshot(round=2)
    allowed1 = set(packs1[1]) | set(packs2[1]) | revealed_picks
    assert_only_allowed(reconnected, allowed1, "ws-reconnect")
    http_state = await get_state(srv.http, game_id, players[1]["token"])
    assert tokens_in(http_state) <= allowed1

    # Phase 5 — error paths carry no card data at all.
    foreign_card = next(iter(set(packs2[3]) - revealed_picks))
    await clients[2].send({"type": "pick", "card": foreign_card, "round": 2})
    err = await clients[2].until(lambda m: m.get("type") == "error")
    assert err["code"] == "card_not_in_pack"
    assert tokens_in(err) == set()
    await clients[2].send({"type": "pick", "card": "c99", "round": 2})
    errors = lambda: [m for m in clients[2].messages if m.get("type") == "error"]
    await clients[2].until(lambda m: m.get("type") == "error" and len(errors()) >= 2)
    assert tokens_in(errors()[-1]) == set()

    # Bad token: no snapshot, no cards.
    async with httpx.AsyncClient() as http:
        r = await http.get(f"{srv.http}/api/games/{game_id}/state", params={"token": "wrong"})
    assert r.status_code == 401
    assert tokens_in(r.json()) == set()

    for c in clients:
        if c is not clients[1]:
            await c.ws.close()
    await reconnected.ws.close()


async def test_replay_endpoint_is_public_only(server):
    """The replay endpoint exposes revealed picks — never pack contents."""
    srv = server.srv
    game_id, players = await start_game(srv.http)
    async with httpx.AsyncClient() as http:
        r = await http.get(f"{srv.http}/api/games/{game_id}/replay")
    assert r.status_code == 200
    body = r.json()
    assert body["rounds"] == []  # nothing revealed yet
    assert CARD_RE.findall(json.dumps(body)) == []  # and no card ids leak
