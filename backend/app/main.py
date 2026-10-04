"""FastAPI entrypoint: HTTP API + WebSocket + static frontend hosting."""
from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .game import GameError, GameManager
from .store import EventStore

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


class JoinRequest(BaseModel):
    name: str = Field(default="", max_length=64)


def create_app(
    db_path: str = "draft.db",
    *,
    round_timeout: float = 30.0,
    clock_mode: str = "auto",  # "auto" | "manual" (controllable clock)
    serve_static: bool = True,
) -> FastAPI:
    store = EventStore(db_path)
    manager = GameManager(store, round_timeout=round_timeout, clock_mode=clock_mode)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        manager.recover()  # rebuild pending games from the event log
        yield
        await manager.shutdown()
        store.close()

    app = FastAPI(title="Draft Game", lifespan=lifespan)
    app.state.manager = manager

    def err(status: int, exc: GameError) -> JSONResponse:
        return JSONResponse({"error": exc.code, "message": str(exc)}, status_code=status)

    # -- HTTP API ---------------------------------------------------------

    @app.post("/api/games")
    async def create_game():
        return {"game_id": manager.create_game(), "timeout_seconds": manager.round_timeout}

    @app.post("/api/games/{game_id}/join")
    async def join_game(game_id: str, req: JoinRequest):
        try:
            player = manager.join_game(game_id, req.name)
        except GameError as exc:
            return err(409, exc)
        return {"game_id": game_id, "seat": player.seat, "name": player.name, "token": player.token}

    @app.get("/api/games/{game_id}/state")
    async def game_state(game_id: str, token: str = Query(default="")):
        """Reconnect snapshot — built by the same filter as WS snapshots, so
        it can never leak other players' hidden cards."""
        seat = manager.authenticate(game_id, token)
        if seat is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return manager.games[game_id].snapshot_for(seat, time.time())

    @app.post("/api/games/{game_id}/timeout")
    async def force_timeout(game_id: str):
        """Controllable clock: resolve the current round now, auto-picking
        (stable rule) for everyone who has not submitted."""
        try:
            changed = await manager.force_timeout(game_id)
        except GameError as exc:
            return err(404, exc)
        return {"resolved": changed}

    @app.get("/api/games/{game_id}/replay")
    async def replay(game_id: str):
        """Public replay of every revealed round and the final picks."""
        game = manager.games.get(game_id)
        if game is None:
            return JSONResponse({"error": "game_not_found"}, status_code=404)
        return game.public_replay()

    # -- WebSocket ----------------------------------------------------------

    @app.websocket("/ws/{game_id}")
    async def ws_endpoint(websocket: WebSocket, game_id: str, token: str = Query(default="")):
        seat = manager.authenticate(game_id, token)
        if seat is None:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        await manager.connect(game_id, seat, websocket)
        try:
            async for raw in websocket.iter_text():
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await websocket.send_text(json.dumps({"type": "error", "code": "bad_json"}))
                    continue
                await handle_message(websocket, manager, game_id, seat, msg)
        except WebSocketDisconnect:
            pass
        finally:
            await manager.disconnect(game_id, seat, websocket)

    # -- static frontend (built React app) ---------------------------------

    if serve_static and FRONTEND_DIST.is_dir():
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="static")

    return app


async def handle_message(websocket: WebSocket, manager: GameManager, game_id: str, seat: int, msg: dict) -> None:
    msg_type = msg.get("type")
    if msg_type == "pick":
        try:
            result = await manager.submit_pick(
                game_id,
                seat,
                str(msg.get("card", "")),
                int(msg.get("round", -1)),
            )
        except (GameError, ValueError, TypeError) as exc:
            code = exc.code if isinstance(exc, GameError) else "bad_request"
            await websocket.send_text(json.dumps({
                "type": "error",
                "code": code,
                "client_msg_id": msg.get("client_msg_id"),
            }))
            return
        await websocket.send_text(json.dumps({
            "type": "pick_ack",
            "round": msg.get("round"),
            "card": result.card,
            "auto": result.auto,
            "already": result.already,
            "client_msg_id": msg.get("client_msg_id"),
        }))
    elif msg_type == "change_pick":
        try:
            result = await manager.change_pick(
                game_id,
                seat,
                str(msg.get("card", "")),
                int(msg.get("round", -1)),
                int(msg.get("revision", -1)),
            )
        except (GameError, ValueError, TypeError) as exc:
            code = exc.code if isinstance(exc, GameError) else "bad_request"
            await websocket.send_text(json.dumps({
                "type": "error",
                "code": code,
                "client_msg_id": msg.get("client_msg_id"),
            }))
            return
        await websocket.send_text(json.dumps({
            "type": "change_pick_ack",
            "round": msg.get("round"),
            "card": result.card,
            "revision": result.revision,
            "already": result.already,
            "client_msg_id": msg.get("client_msg_id"),
        }))
    elif msg_type == "ping":
        await websocket.send_text(json.dumps({"type": "pong"}))
    else:
        await websocket.send_text(json.dumps({"type": "error", "code": "unknown_message_type"}))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=8000)
