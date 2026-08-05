"""HTTP client for the retrieval service.

Retrieval being unavailable is a new failure mode that did not exist when this code
lived in one process. It is surfaced as a 503 rather than swallowed, because an answer
built on no context is exactly what the escalation gate exists to prevent.
"""

import httpx
from deflect_common.logging import request_id
from deflect_common.observability import HEADER
from deflect_common.schemas import SearchRequest, SearchResponse
from fastapi import HTTPException


class RetrievalClient:
    def __init__(self, base_url: str, token: str, timeout: float = 30.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    async def search(self, request: SearchRequest) -> SearchResponse:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                headers = {"Authorization": f"Bearer {self._token}"}
                # Without this the id stops at the service boundary and the trace breaks
                # exactly where a multi-service bug is hardest to follow.
                if request_id.get():
                    headers[HEADER] = request_id.get()
                response = await client.post(
                    f"{self._base_url}/search",
                    json=request.model_dump(),
                    headers=headers,
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as cause:
            # A 401 from retrieval is a misconfigured deployment, not an outage. Reporting
            # it as 503 would tell an operator to wait for a service that will never
            # recover on its own.
            if cause.response.status_code == 401:
                raise HTTPException(
                    status_code=500, detail="retrieval rejected this service's credential"
                ) from cause
            raise HTTPException(
                status_code=503, detail=f"retrieval service unavailable: {cause}"
            ) from cause
        except httpx.HTTPError as cause:
            raise HTTPException(
                status_code=503, detail=f"retrieval service unavailable: {cause}"
            ) from cause

        return SearchResponse.model_validate(response.json())
