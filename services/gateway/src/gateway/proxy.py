"""Carrying a request to an upstream and its response back.

The response is streamed rather than read. A gateway that buffers turns a token-by-token
answer into a thirty-second wait, and the failure is invisible in any test that only
checks the final body.
"""

import httpx
from deflect_common.logging import request_id
from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from gateway.policy import Policy
from gateway.routes import Route

# Defined by RFC 9110 as meaningful only for a single connection. Relaying them corrupts
# connection handling in ways that surface under load rather than in a test.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

# Never relayed from a caller. The gateway writes its own; accepting an inbound copy would
# let a client author a header some upstream might one day be taught to trust. Nothing
# trusts them today, and this is what keeps that true by construction.
_NEVER_FROM_CLIENT = ("x-forwarded-", "x-deflect-", "x-real-ip")


def clean_request_headers(headers) -> dict[str, str]:
    """The caller's headers, minus what must not travel.

    Authorization is deliberately kept: the upstream re-resolves the caller itself, which
    is the property that makes bypassing the gateway a non-event rather than a breach.
    """
    return {
        key: value
        for key, value in dict(headers).items()
        if (lowered := key.lower()) not in _HOP_BY_HOP
        and lowered != "host"
        and lowered != "content-length"
        and not lowered.startswith(_NEVER_FROM_CLIENT)
    }


def _response_headers(upstream: httpx.Response) -> dict[str, str]:
    return {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in _HOP_BY_HOP and key.lower() != "content-length"
    }


async def forward(
    route: Route, request: Request, base_url: str, client: httpx.AsyncClient
) -> StreamingResponse:
    """Send this request to `base_url` and stream the answer back.

    Timeouts are split: connect and write are short because a healthy upstream on a
    private network answers in milliseconds, while read is per-route because /ask
    legitimately takes far longer than /traces. A streaming route gets no read timeout at
    all -- a long gap between SSE frames is the normal shape of a slow answer, not a
    failure -- and is bounded by the client going away instead.
    """
    timeout = httpx.Timeout(
        connect=Policy.CONNECT_TIMEOUT,
        write=Policy.WRITE_TIMEOUT,
        read=None if route.stream else route.timeout,
        pool=Policy.CONNECT_TIMEOUT,
    )

    headers = clean_request_headers(request.headers)
    # RequestIdMiddleware puts the id on a contextvar and on the RESPONSE, never on the
    # inbound request, so a caller-supplied id survives clean_request_headers unchanged but
    # a MINTED one -- the common case, every request the caller did not already tag -- had
    # nothing to carry it here. Setting it from the contextvar covers both: for a
    # caller-supplied id this is the same value already present, and for a minted one it is
    # the only place that value exists before this call.
    current_id = request_id.get()
    if current_id:
        headers["X-Request-ID"] = current_id

    upstream_request = client.build_request(
        request.method,
        f"{base_url}{request.url.path}",
        headers=headers,
        params=request.query_params,
        content=await request.body(),
        timeout=timeout,
    )

    try:
        upstream = await client.send(upstream_request, stream=True)
    except httpx.TimeoutException as exc:
        # 504 rather than 502: the difference between "did not answer in time" and "could
        # not be reached" is the first thing anyone debugging this will want.
        raise HTTPException(504, f"{route.upstream} did not answer in time") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"{route.upstream} could not be reached") from exc

    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        headers=_response_headers(upstream),
        # Closing the upstream response is what returns its connection to the pool. Without
        # this the pool leaks one connection per request and the gateway stalls under load.
        background=BackgroundTask(upstream.aclose),
    )
