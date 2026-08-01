"""HTTP client for the answer service.

This is the point of the split that most matters. In the monolith the eval harness
called the answer function directly, which guaranteed evals and production shared a
code path only because both were in one process. Here the eval service calls the same
endpoint a real client calls, so the guarantee survives the network boundary.
"""

import httpx
from deflect_common.schemas import AnswerRequest, AnswerResponse
from fastapi import HTTPException


class AnswerClient:
    def __init__(self, base_url: str, timeout: float = 120.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def answer(self, request: AnswerRequest) -> AnswerResponse:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base_url}/answer", json=request.model_dump()
                )
                response.raise_for_status()
        except httpx.HTTPError as cause:
            # Matches RetrievalClient: a dependency being unreachable is reported as
            # such rather than surfacing as an opaque 500 partway through a run.
            raise HTTPException(
                status_code=503, detail=f"answer service unavailable: {cause}"
            ) from cause

        return AnswerResponse.model_validate(response.json())
