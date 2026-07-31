import json

import pytest

from deflect.llm.base import Completion, get_client
from deflect.llm.fake import FakeClient


async def test_fake_client_returns_scripted_responses_in_order():
    client = FakeClient(["first", "second"])

    assert (await client.complete("a")).text == "first"
    assert (await client.complete("b")).text == "second"


async def test_fake_client_records_prompts_for_assertions():
    client = FakeClient(["ok"])

    await client.complete("what is Depends")

    assert client.prompts == ["what is Depends"]


async def test_fake_client_raises_when_the_script_runs_out():
    client = FakeClient(["only"])
    await client.complete("a")

    with pytest.raises(AssertionError):
        await client.complete("b")


async def test_completion_reports_token_counts():
    completion = await FakeClient([json.dumps({"answer": "x"})]).complete("q")

    assert isinstance(completion, Completion)
    assert completion.output_tokens > 0


def test_unknown_provider_is_rejected():
    with pytest.raises(ValueError, match="unknown provider"):
        get_client(provider="anthropic")
