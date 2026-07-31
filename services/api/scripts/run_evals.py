"""Runs the golden dataset through the live answer path and stores the run."""

import argparse
import asyncio
import subprocess
from pathlib import Path

from deflect.config import get_settings
from deflect.db import SessionFactory
from deflect.evals.dataset import load_dataset
from deflect.evals.runner import run_evals
from deflect.llm.base import get_client
from deflect.retrieval.pipeline import RetrievalConfig
from deflect.routes.ask import THRESHOLDS

DEFAULT_DATASET = Path(__file__).parents[3] / "evals" / "golden.yaml"


async def main(dataset: Path, limit: int | None, fail_under: float | None) -> None:
    items = load_dataset(dataset)[:limit]
    settings = get_settings()
    git_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()

    async with SessionFactory() as session:
        run = await run_evals(
            session,
            items,
            get_client(model=settings.generation_model),
            get_client(model=settings.judge_model),
            RetrievalConfig(),
            THRESHOLDS,
            git_sha,
        )
        await session.commit()
        metrics = dict(run.metrics)
        run_id = run.id

    print(f"run {run_id} over {len(items)} items at {git_sha[:7]}")
    for name, value in sorted(metrics.items()):
        print(f"  {name}: {value:.3f}")

    if fail_under is not None and metrics["faithfulness"] < fail_under:
        raise SystemExit(
            f"faithfulness {metrics['faithfulness']:.3f} below threshold {fail_under}"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--fail-under", type=float, default=None)
    args = parser.parse_args()
    asyncio.run(main(args.dataset, args.limit, args.fail_under))
