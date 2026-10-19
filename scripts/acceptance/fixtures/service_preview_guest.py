import asyncio
import os

import uvicorn
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from vibecanvas_api.services.agent_runtime.sandbox_entry import serve_turns
from vibecanvas_engine.sandbox_bus import connect_bus

app = FastAPI()


@app.get("/")
async def root():
    return HTMLResponse("""<!doctype html><html><head><link rel="stylesheet" href="/style.css"></head><body>
<h1>Sandbox live service</h1><div id="api"></div><div id="sse"></div><div id="ws"></div><div id="count">0</div>
<button id="post">Send POST</button><div id="posted"></div><img src="/icon.svg">
<script type="module" src="/app.js"></script></body></html>""")


@app.get("/style.css")
async def style():
    return Response("h1 { color: rgb(0, 100, 200); }", media_type="text/css")


@app.get("/icon.svg")
async def icon():
    return Response(
        '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24"><rect width="24" height="24" fill="green"/></svg>',
        media_type="image/svg+xml",
    )


@app.get("/app.js")
async def js():
    return Response(
        """fetch('/api?value=hello').then(r=>r.json()).then(v=>document.querySelector('#api').textContent=v.value);
new EventSource('/events').onmessage=e=>document.querySelector('#sse').textContent=e.data;
const ws=new WebSocket(location.origin.replace('https:', 'wss:')+'/socket');ws.onopen=()=>ws.send('live');ws.onmessage=e=>document.querySelector('#ws').textContent=e.data;
setInterval(()=>document.querySelector('#count').textContent=Number(document.querySelector('#count').textContent)+1,200);
document.querySelector('#post').onclick=()=>fetch('/post',{method:'POST',body:'from browser'}).then(r=>r.text()).then(t=>document.querySelector('#posted').textContent=t);
""",
        media_type="text/javascript",
    )


@app.get("/api")
async def api(value: str):
    return {"value": value}


@app.post("/post")
async def post(request: __import__("fastapi").Request):
    return Response(await request.body())


@app.get("/events")
async def events():
    async def output():
        for i in range(5):
            yield f"data: event {i}\n\n"
            await asyncio.sleep(0.2)

    return StreamingResponse(output(), media_type="text/event-stream")


@app.websocket("/socket")
async def socket(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            await ws.send_text("echo " + await ws.receive_text())
    except Exception:
        pass


async def main():
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=3100, log_level="warning"))
    task = asyncio.create_task(server.serve())
    channel = await connect_bus(os.environ["VC_BUS_SOCK"])
    try:
        await serve_turns(channel)
    finally:
        server.should_exit = True
        await task
        await channel.close()


asyncio.run(main())
