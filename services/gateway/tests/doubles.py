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
    app.state.seen_paths = []
    app.state.release = asyncio.Event()

    @app.middleware("http")
    async def record(request: Request, call_next):
        """Record EVERY request, not only the ones this double has a route for.

        Recording inside a handler can only ever see paths the double defines, so a test
        asserting "the upstream was never called" would pass identically when the upstream
        WAS called on a path it happens not to serve -- which is exactly the regression
        those tests exist to catch.
        """
        app.state.seen_paths.append(request.url.path)
        app.state.seen_headers = dict(request.headers)
        return await call_next(request)

    @app.api_route("/echo", methods=["GET", "POST"])
    async def echo(request: Request) -> JSONResponse:
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

    @app.get("/slow")
    async def slow() -> JSONResponse:
        """Sleeps past any route timeout the 504 test uses, over a real socket -- so the
        timeout that fires is a genuine read timeout rather than one inferred from reading
        proxy.py. Short enough to keep the suite fast; the route's own timeout is tinier
        still, so the gateway never actually waits out the full sleep."""
        await asyncio.sleep(0.5)
        return JSONResponse({"path": "/slow"})

    return app
