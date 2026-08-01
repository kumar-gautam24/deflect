"""Shared helper for the analysis scripts.

They talk to the retrieval service over HTTP like any other client, so what they
measure is what the deployed pipeline actually returns.
"""

import os
from pathlib import Path

import httpx
from deflect_common.schemas import Hit, SearchRequest

RETRIEVAL_URL = os.environ.get("RETRIEVAL_URL", "http://localhost:8001")
DATASET = Path(os.environ.get("DATASET_PATH", Path(__file__).parents[3] / "evals" / "golden.yaml"))


async def search(client: httpx.AsyncClient, query: str, **overrides) -> list[Hit]:
    request = SearchRequest(query=query, **overrides)
    response = await client.post(f"{RETRIEVAL_URL}/search", json=request.model_dump())
    response.raise_for_status()
    return [Hit.model_validate(h) for h in response.json()["hits"]]
