"""HTTP client for the answer service.

This is the point of the split that most matters. In the monolith the eval harness
called the answer function directly, which guaranteed evals and production shared a
code path only because both were in one process. Here the eval service calls the same
endpoint a real client calls, so the guarantee survives the network boundary.
"""

import httpx
from deflect_common.logging import request_id
from deflect_common.observability import HEADER
from deflect_common.schemas import AnswerRequest, AnswerResponse
from fastapi import HTTPException


class AnswerClient:
    def __init__(self, base_url: str, token: str, timeout: float = 120.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    async def answer(self, request: AnswerRequest) -> AnswerResponse:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                headers = {"Authorization": f"Bearer {self._token}"}
                # Without this the id stops at the service boundary and the trace breaks
                # exactly where a multi-service bug is hardest to follow.
                if request_id.get():
                    headers[HEADER] = request_id.get()
                response = await client.post(
                    f"{self._base_url}/answer",
                    json=request.model_dump(),
                    headers=headers,
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as cause:
            # Matches RetrievalClient: a rejected credential is a misconfiguration and
            # will not heal, so it is not disguised as a transient outage.
            if cause.response.status_code == 401:
                raise HTTPException(
                    status_code=500, detail="answer rejected this service's credential"
                ) from cause
            raise HTTPException(
                status_code=503, detail=f"answer service unavailable: {cause}"
            ) from cause
        except httpx.HTTPError as cause:
            raise HTTPException(
                status_code=503, detail=f"answer service unavailable: {cause}"
            ) from cause

        return AnswerResponse.model_validate(response.json())
