"""A fake upstream, served in-process.

The gateway is given an httpx client whose transport is this app, so proxy behaviour is
exercised for real -- routing, headers, status, streaming -- without a socket.
"""

import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


def build_upstream() -> FastAPI:
    app = FastAPI()
    app.state.seen_headers = {}
    app.state.release = asyncio.Event()

    @app.api_route("/echo", methods=["GET", "POST"])
    async def echo(request: Request) -> JSONResponse:
        app.state.seen_headers = dict(request.headers)
        body = await request.body()
        return JSONResponse(
            {
                "path": request.url.path,
                "query": str(request.url.query),
                "body": body.decode(),
            }
        )

    @app.get("/slow-stream")
    async def slow_stream() -> StreamingResponse:
        """Emits one frame, then blocks until the test releases it.

        Gating on an event rather than sleeping is what makes the no-buffering test
        deterministic: if the gateway buffered, the first frame could not arrive before
        the second is even produced.
        """

        async def frames():
            yield b"data: first\n\n"
            await app.state.release.wait()
            yield b"data: second\n\n"

        return StreamingResponse(frames(), media_type="text/event-stream")

    @app.get("/boom")
    async def boom() -> JSONResponse:
        return JSONResponse({"detail": "upstream said no"}, status_code=418)

    return app
