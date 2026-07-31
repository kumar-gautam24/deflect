import os
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from deflect.evals.dataset import load_dataset
from deflect.models import Document

GOLDEN = Path(__file__).parents[3] / "evals" / "golden.yaml"


def test_loads_items_with_all_fields(tmp_path):
    path = tmp_path / "golden.yaml"
    path.write_text(
        "- id: q1\n"
        "  question: How do I declare a dependency?\n"
        "  ideal_answer: Use Depends.\n"
        "  expected_sources: [tutorial/dependencies/index.md]\n"
        "  should_escalate: false\n"
    )

    items = load_dataset(path)

    assert len(items) == 1
    assert items[0].id == "q1"
    assert items[0].expected_sources == ["tutorial/dependencies/index.md"]
    assert items[0].should_escalate is False


def test_unanswerable_items_need_no_expected_sources(tmp_path):
    path = tmp_path / "golden.yaml"
    path.write_text(
        "- id: q2\n"
        "  question: What is the FastAPI pricing?\n"
        "  ideal_answer: Not covered by the documentation.\n"
        "  should_escalate: true\n"
    )

    assert load_dataset(path)[0].expected_sources == []


def test_answerable_item_without_expected_sources_is_rejected(tmp_path):
    path = tmp_path / "golden.yaml"
    path.write_text(
        "- id: q3\n  question: q\n  ideal_answer: a\n  should_escalate: false\n"
    )

    with pytest.raises(ValueError, match="q3"):
        load_dataset(path)


def test_escalating_item_naming_sources_is_rejected(tmp_path):
    path = tmp_path / "golden.yaml"
    path.write_text(
        "- id: q4\n"
        "  question: q\n"
        "  ideal_answer: a\n"
        "  expected_sources: [x.md]\n"
        "  should_escalate: true\n"
    )

    with pytest.raises(ValueError, match="q4"):
        load_dataset(path)


def test_duplicate_ids_are_rejected(tmp_path):
    path = tmp_path / "golden.yaml"
    path.write_text(
        "- {id: q1, question: a, ideal_answer: a,"
        " expected_sources: [x.md], should_escalate: false}\n"
        "- {id: q1, question: b, ideal_answer: b,"
        " expected_sources: [y.md], should_escalate: false}\n"
    )

    with pytest.raises(ValueError, match="duplicate"):
        load_dataset(path)


def test_the_real_dataset_loads_and_covers_the_refusal_path():
    items = load_dataset(GOLDEN)

    assert len(items) >= 80
    assert sum(1 for i in items if i.should_escalate) >= 15


async def test_every_expected_source_exists_in_the_ingested_corpus():
    """A typo in a source path would look like a permanent retrieval regression.

    Checked against the corpus database rather than the test database: the test
    database is deliberately kept empty so retrieval tests can assert ranking.
    """
    url = os.environ.get(
        "CORPUS_DATABASE_URL", "postgresql+asyncpg://deflect:deflect@localhost:5432/deflect"
    )
    engine = create_async_engine(url)
    try:
        async with AsyncSession(engine) as db:
            known = set((await db.execute(select(Document.source_path))).scalars().all())
    finally:
        await engine.dispose()

    assert known, f"no corpus ingested at {url}; run scripts/ingest.py first"

    referenced = {s for item in load_dataset(GOLDEN) for s in item.expected_sources}
    assert referenced <= known, f"unknown source paths: {sorted(referenced - known)}"
