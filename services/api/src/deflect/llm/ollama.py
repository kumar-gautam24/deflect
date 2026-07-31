import httpx

from deflect.config import get_settings
from deflect.llm.base import Completion


class OllamaClient:
    def __init__(self, model: str) -> None:
        self._base_url = get_settings().ollama_base_url
        self._model = model

    async def complete(self, prompt: str, schema: dict | None = None) -> Completion:
        payload: dict = {
            "model": self._model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.0},
        }
        if schema is not None:
            payload["format"] = schema

        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(f"{self._base_url}/api/generate", json=payload)
            response.raise_for_status()
            body = response.json()

        return Completion(
            text=body["response"],
            input_tokens=body["prompt_eval_count"],
            output_tokens=body["eval_count"],
            model=self._model,
        )
