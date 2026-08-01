"""Test doubles for the services this one depends on."""

from deflect_common.schemas import AnswerRequest, AnswerResponse, Hit


def hit(chunk_id: int = 1, source_path: str = "deps.md", score: float = 6.0) -> Hit:
    return Hit(
        chunk_id=chunk_id,
        document_id=1,
        source_path=source_path,
        heading_path="Dependencies",
        text="Use Depends to declare a dependency.",
        score=score,
    )


def response(answer: str, escalated: bool, **overrides) -> AnswerResponse:
    base = dict(
        trace_id=1,
        answer=answer,
        citations=[],
        escalated=escalated,
        reason="ungrounded_answer" if escalated else None,
        top_score=6.0,
        margin=5.0,
        hits=[hit()],
        input_tokens=10,
        output_tokens=5,
        cost_usd=0.0,
        model="fake",
        prompt_version="answer_v1",
        latency_ms=12,
        min_top_score=2.0,
        min_margin=0.0,
    )
    return AnswerResponse(**(base | overrides))


class FakeAnswer:
    """Stands in for the answer service.

    The eval service's tests need no vector database, no embedding model and no
    provider key: the contract is the whole dependency.
    """

    def __init__(self, responses: list[AnswerResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[AnswerRequest] = []

    async def answer(self, request: AnswerRequest) -> AnswerResponse:
        self.requests.append(request)
        assert self._responses, "FakeAnswer exhausted: more calls than scripted responses"
        return self._responses.pop(0)
