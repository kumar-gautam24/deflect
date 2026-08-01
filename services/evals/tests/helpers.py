"""Row builders shared by the route tests."""

from evals.models import EvalResult, EvalRun


def make_run(sha: str, faithfulness: float) -> EvalRun:
    return EvalRun(
        git_sha=sha,
        prompt_version="answer_v1",
        judge_version="judge_v1",
        model="fake",
        retrieval_config={},
        thresholds={},
        item_count=1,
        metrics={"faithfulness": faithfulness},
    )


def make_result(run_id: int, item_id: str, faithfulness: float | None) -> EvalResult:
    return EvalResult(
        run_id=run_id,
        item_id=item_id,
        question="q",
        answer="a",
        escalated=False,
        expected_escalate=False,
        retrieved_sources=["deps.md"],
        hit_at_5=1.0,
        mrr=1.0,
        faithfulness=faithfulness,
        answer_relevance=1.0,
        context_relevance=1.0,
        rationale="ok",
    )
