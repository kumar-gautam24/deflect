"""HTTP client for the retrieval service.

Retrieval being unavailable is a new failure mode that did not exist when this code
lived in one process. It is surfaced as a 503 rather than swallowed, because an answer
built on no context is exactly what the escalation gate exists to prevent.
"""

import httpx
from deflect_common.schemas import SearchRequest, SearchResponse
from fastapi import HTTPException


class RetrievalClient:
    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def search(self, request: SearchRequest) -> SearchResponse:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base_url}/search", json=request.model_dump()
                )
                response.raise_for_status()
        except httpx.HTTPError as cause:
            raise HTTPException(
                status_code=503, detail=f"retrieval service unavailable: {cause}"
            ) from cause

        return SearchResponse.model_validate(response.json())
