"""Shared test infrastructure: real uvicorn server in a task, WS client helper,
and direct SQLite access to the server-private event log (ground truth)."""
from __future__ import annotations

import asyncio
import json
import re
import socket
import sqlite3

import httpx
import pytest
import uvicorn
from websockets.asyncio.client import connect

from app.main import create_app

CARD_RE = re.compile(r"\bc\d{2}\b")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class RunningServer:
    def __init__(self, app):
        self.port = free_port()
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error")
        self.server = uvicorn.Server(config)
        self.http = f"http://127.0.0.1:{self.port}"
        self.ws = f"ws://127.0.0.1:{self.port}"

    async def start(self):
        self.task = asyncio.create_task(self.server.serve())
        for _ in range(250):
            if self.server.started:
                return
            await asyncio.sleep(0.02)
        raise RuntimeError("server did not start")

    async def stop(self):
        self.server.should_exit = True
        await asyncio.wait_for(self.task, timeout=10)


class ServerFixture:
    """A running server plus its db path, restartable on the same db."""

    def __init__(self, db_path: str, **app_kwargs):
        self.db_path = db_path
        self.app_kwargs = app_kwargs
        self.srv: RunningServer | None = None

    async def start(self):
        app = create_app(self.db_path, serve_static=False, **self.app_kwargs)
        self.srv = RunningServer(app)
        await self.srv.start()
        return self.srv

    async def stop(self):
        if self.srv:
            await self.srv.stop()
            self.srv = None

    async def restart(self):
        await self.stop()
        return await self.start()


@pytest.fixture
async def server(tmp_path):
    """Manual-clock server (rounds resolve only via POST /timeout)."""
    fix = ServerFixture(str(tmp_path / "test.db"), round_timeout=30.0, clock_mode="manual")
    await fix.start()
    yield fix
    await fix.stop()


# -- HTTP helpers -----------------------------------------------------------


async def create_game(http: str) -> str:
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{http}/api/games")
    assert r.status_code == 200, r.text
    return r.json()["game_id"]


async def join(http: str, game_id: str, name: str) -> dict:
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{http}/api/games/{game_id}/join", json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()


async def force_timeout(http: str, game_id: str) -> dict:
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{http}/api/games/{game_id}/timeout")
    assert r.status_code == 200, r.text
    return r.json()


async def get_state(http: str, game_id: str, token: str) -> dict:
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{http}/api/games/{game_id}/state", params={"token": token})
    assert r.status_code == 200, r.text
    return r.json()


async def get_replay(http: str, game_id: str) -> dict:
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{http}/api/games/{game_id}/replay")
    assert r.status_code == 200, r.text
    return r.json()


async def start_game(http: str) -> tuple[str, list[dict]]:
    """Create a game and join 4 players (auto-starts on the 4th join)."""
    game_id = await create_game(http)
    players = [await join(http, game_id, f"P{i}") for i in range(4)]
    return game_id, players


# -- WebSocket client helper ---------------------------------------------------


class WSClient:
    def __init__(self, ws, seat: int):
        self.ws = ws
        self.seat = seat
        self.messages: list[dict] = []  # everything ever received, in order

    async def recv(self, timeout: float = 5.0) -> dict:
        msg = json.loads(await asyncio.wait_for(self.ws.recv(), timeout))
        self.messages.append(msg)
        return msg

    async def send(self, obj: dict) -> None:
        await self.ws.send(json.dumps(obj))

    async def until(self, pred, timeout: float = 5.0) -> dict:
        """Return the first message matching pred, buffering the rest."""
        for msg in self.messages:
            if pred(msg):
                return msg
        async def loop():
            while True:
                msg = await self.recv()
                if pred(msg):
                    return msg
        return await asyncio.wait_for(loop(), timeout)

    async def latest_snapshot(self, timeout: float = 5.0, **conds) -> dict:
        def pred(m):
            if m.get("type") != "snapshot":
                return False
            return all(m.get(k) == v for k, v in conds.items())
        return await self.until(pred, timeout)

    async def drain(self, settle: float = 0.1) -> None:
        """Buffer all messages that arrive within the settle window."""
        try:
            while True:
                await self.recv(timeout=settle)
        except asyncio.TimeoutError:
            pass

    def card_tokens_seen(self) -> set[str]:
        tokens: set[str] = set()
        for msg in self.messages:
            tokens.update(CARD_RE.findall(json.dumps(msg)))
        return tokens


async def ws_connect(srv: RunningServer, game_id: str, seat: int, token: str) -> WSClient:
    ws = await connect(f"{srv.ws}/ws/{game_id}?token={token}")
    return WSClient(ws, seat)


# -- server-private ground truth (read straight from the event log) -----------


def load_truth(db_path: str, game_id: str) -> dict:
    """Replay the event log from SQLite: packs/picks per round, incl. secrets."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT type, payload FROM events WHERE game_id = ? ORDER BY seq", (game_id,)
    ).fetchall()
    conn.close()
    truth: dict = {"packs": {}, "picks": {}, "revealed": set(), "events": rows}
    for row in rows:
        payload = json.loads(row["payload"])
        if row["type"] == "round_started":
            truth["packs"][payload["round"]] = {
                int(s): list(cards) for s, cards in payload["packs"].items()
            }
        elif row["type"] == "pick_submitted":
            truth["picks"].setdefault(payload["round"], {})[payload["seat"]] = payload["card"]
        elif row["type"] == "pick_changed":
            truth["picks"].setdefault(payload["round"], {})[payload["seat"]] = payload["card"]
        elif row["type"] == "round_revealed":
            truth["revealed"].add(payload["round"])
    return truth


def count_events(db_path: str, game_id: str, type_: str, round_no: int | None = None) -> int:
    conn = sqlite3.connect(db_path)
    n = 0
    for (payload,) in conn.execute(
        "SELECT payload FROM events WHERE game_id = ? AND type = ?", (game_id, type_)
    ):
        data = json.loads(payload)
        if round_no is None or data.get("round") == round_no:
            n += 1
    conn.close()
    return n
