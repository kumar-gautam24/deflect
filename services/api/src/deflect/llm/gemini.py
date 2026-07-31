from google import genai
from google.genai import types

from deflect.config import get_settings
from deflect.llm.base import Completion


class GeminiClient:
    def __init__(self, model: str) -> None:
        self._client = genai.Client(api_key=get_settings().gemini_api_key)
        self._model = model

    async def complete(self, prompt: str, schema: dict | None = None) -> Completion:
        # Temperature zero because both callers are graded: the answer path is scored
        # by the eval harness and the judge path produces the scores. Sampling noise
        # would show up as run-to-run metric drift with no code change behind it.
        config = types.GenerateContentConfig(temperature=0.0)
        if schema is not None:
            config.response_mime_type = "application/json"
            config.response_schema = schema

        response = await self._client.aio.models.generate_content(
            model=self._model, contents=prompt, config=config
        )
        usage = response.usage_metadata
        return Completion(
            text=response.text,
            input_tokens=usage.prompt_token_count,
            output_tokens=usage.candidates_token_count,
            model=self._model,
        )
