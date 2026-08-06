"""Executes the golden dataset against the live answer service and persists the run."""

from deflect_common.llm.base import LLMClient
from deflect_common.schemas import AnswerRequest, AnswerResponse, SearchRequest

from evals.answer_client import AnswerClient
from evals.dataset import GoldenItem
from evals.judge import judge_answer
from evals.metrics import hit_at_k, mrr
from evals.models import EvalResult


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _aggregate(results: list[EvalResult]) -> dict:
    answerable = [r for r in results if not r.expected_escalate]
    judged = [r for r in results if r.faithfulness is not None]
    escalated = [r for r in results if r.escalated]
    should_escalate = [r for r in results if r.expected_escalate]

    return {
        "hit_at_5": _mean([r.hit_at_5 for r in answerable]),
        "mrr": _mean([r.mrr for r in answerable]),
        "faithfulness": _mean([r.faithfulness for r in judged]),
        "answer_relevance": _mean([r.answer_relevance for r in judged]),
        "context_relevance": _mean([r.context_relevance for r in judged]),
        # Of the answers it refused, how many deserved refusing.
        "escalation_precision": _mean([float(r.expected_escalate) for r in escalated]),
        # Of the questions it should have refused, how many it actually did.
        "escalation_recall": _mean([float(r.escalated) for r in should_escalate]),
        "deflection_rate": _mean([float(not r.escalated) for r in results]),
        # The headline number: answerable questions answered without refusing.
        "answered_rate": _mean([float(not r.escalated) for r in answerable]),
    }


async def score_item(
    item: GoldenItem,
    answer_client: AnswerClient,
    judge_client: LLMClient,
    search: SearchRequest | None,
) -> tuple[EvalResult, AnswerResponse]:
    """Answer and score one item, returning an unattached row and the raw outcome.

    It takes no session on purpose: the worker owns persistence, and a function that both
    scored and wrote could not be retried without deciding what to do about a
    half-written result.

    The outcome comes back alongside the row because a run's provenance -- the prompt
    version, the model, the gate thresholds actually in force -- is only knowable once
    something has answered, and the run row is created before that.
    """
    request = AnswerRequest(
        question=item.question,
        search=search.model_copy(update={"query": item.question}) if search else None,
    )
    outcome = await answer_client.answer(request)

    # Judging a refusal wastes tokens: there is no answer to score, and the escalation
    # metrics already capture whether refusing was correct.
    scores = (
        None
        if outcome.escalated
        else await judge_answer(judge_client, item, outcome.answer, outcome.hits)
    )

    sources = [hit.source_path for hit in outcome.hits]
    result = EvalResult(
        item_id=item.id,
        question=item.question,
        answer=outcome.answer,
        escalated=outcome.escalated,
        expected_escalate=item.should_escalate,
        retrieved_sources=sources,
        hit_at_5=hit_at_k(sources, item.expected_sources, k=5),
        mrr=mrr(sources, item.expected_sources),
        faithfulness=scores.faithfulness if scores else None,
        answer_relevance=scores.answer_relevance if scores else None,
        context_relevance=scores.context_relevance if scores else None,
        rationale=scores.rationale if scores else None,
    )
    return result, outcome
