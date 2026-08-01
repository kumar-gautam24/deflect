"""Scripted client so tests exercise the real pipeline without spending tokens."""

from deflect_common.llm.base import Completion


class FakeClient:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []

    async def complete(self, prompt: str, schema: dict | None = None) -> Completion:
        self.prompts.append(prompt)
        assert self._responses, "FakeClient exhausted: more calls than scripted responses"
        text = self._responses.pop(0)
        return Completion(
            text=text,
            input_tokens=len(prompt.split()),
            output_tokens=max(len(text.split()), 1),
            model="fake",
        )
