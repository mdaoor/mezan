import asyncio

import jwt
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from mizan.core import models as m
from mizan.core.security import decode_token

router = APIRouter()


@router.websocket("/ws")
async def ws(websocket: WebSocket, token: str = ""):
    c = websocket.app.state.c
    try:
        claims = decode_token(c.settings.secret, token)
    except jwt.PyJWTError:
        await websocket.close(code=4401)
        return
    async with c.session() as db:
        u = await db.get(m.User, claims["sub"])
    if u is None:
        await websocket.close(code=4401)
        return
    user = {"id": u.id, "name": u.name}
    await websocket.accept()
    q: asyncio.Queue = asyncio.Queue(maxsize=2000)
    topics: set[str] = set()
    present_in: set[str] = set()

    async def pump():
        while True:
            await websocket.send_json(await q.get())

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            msg = await websocket.receive_json()
            if "subscribe" in msg:
                t = msg["subscribe"]
                if t not in topics:
                    topics.add(t)
                    c.hub.subscribe(t, q)
                if t.startswith("dataset:"):
                    q.put_nowait({"topic": t, "type": "presence", "users": list(c.hub.presence[t[8:]].values())})
            elif "unsubscribe" in msg:
                topics.discard(msg["unsubscribe"])
                c.hub.unsubscribe(msg["unsubscribe"], q)
            elif "presence" in msg:
                ds, seg = msg["presence"].get("dataset"), msg["presence"].get("segment")
                if ds:
                    present_in.add(ds)
                    users = c.hub.set_presence(ds, user, seg)
                    await c.hub.publish(f"dataset:{ds}", {"type": "presence", "users": users})
            elif msg.get("ping"):
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        pump_task.cancel()
        for t in topics:
            c.hub.unsubscribe(t, q)
        for ds in present_in:
            users = c.hub.set_presence(ds, user, None)
            await c.hub.publish(f"dataset:{ds}", {"type": "presence", "users": users})
