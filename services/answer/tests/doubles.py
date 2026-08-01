"""Test doubles for the services this one depends on."""

from deflect_common.schemas import Hit, SearchRequest, SearchResponse


class FakeRetrieval:
    """Stands in for the retrieval service.

    The service boundary is what makes this possible: the answer service's tests no
    longer need a vector database or an embedding model, only the wire schema.
    """

    def __init__(self, hits: list[Hit]) -> None:
        self._hits = hits
        self.requests: list[SearchRequest] = []

    async def search(self, request: SearchRequest) -> SearchResponse:
        self.requests.append(request)
        return SearchResponse(hits=self._hits)


def hit(chunk_id: int, text: str, score: float, source_path: str = "deps.md") -> Hit:
    return Hit(
        chunk_id=chunk_id,
        document_id=1,
        source_path=source_path,
        heading_path=f"Dependencies > {chunk_id}",
        text=text,
        score=score,
    )
