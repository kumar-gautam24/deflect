import os
from pathlib import Path

import httpx
import pytest

from evals.dataset import load_dataset

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

    Checked against the retrieval service, which owns the corpus. This service can
    no longer read that table, so the boundary is crossed the same way production
    crosses it.
    """
    url = os.environ.get("RETRIEVAL_URL", "http://localhost:8001")
    # Deliberately not SERVICE_TOKEN: conftest.py assigns that before import, so a value
    # passed in under that name never survives to here. This is the credential of the
    # live retrieval service being checked against, which is a different thing from the
    # token this service builds its own guards with.
    token = os.environ.get("CORPUS_CHECK_TOKEN", "")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"{url}/documents", headers={"Authorization": f"Bearer {token}"}
            )
            response.raise_for_status()
    except httpx.HTTPError as cause:
        # Skipping keeps the unit suite runnable without the whole stack, but a
        # check that can silently vanish is worthless. CI sets this variable so an
        # unreachable service fails the build instead of quietly passing.
        if os.environ.get("REQUIRE_CORPUS_CHECK"):
            raise AssertionError(f"retrieval service required but unreachable: {cause}") from cause
        pytest.skip(f"retrieval service not reachable at {url}: {cause}")

    known = set(response.json()["source_paths"])
    assert known, f"no corpus ingested at {url}"

    referenced = {s for item in load_dataset(GOLDEN) for s in item.expected_sources}
    assert referenced <= known, f"unknown source paths: {sorted(referenced - known)}"
