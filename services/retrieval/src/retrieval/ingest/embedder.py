"""Local embedding model. Kept local so re-ingesting during ablation costs nothing."""

from functools import lru_cache

from fastembed import TextEmbedding

from retrieval.config import get_settings


@lru_cache
def _model() -> TextEmbedding:
    return TextEmbedding(model_name=get_settings().embedding_model)


def embed_texts(texts: list[str]) -> list[list[float]]:
    batch_size = get_settings().embedding_batch_size
    return [vector.tolist() for vector in _model().embed(texts, batch_size=batch_size)]


def embed_query(text: str) -> list[float]:
    # bge models are trained with an asymmetric query prefix; omitting it measurably
    # degrades retrieval, so the query path must not reuse embed_texts directly.
    return embed_texts([f"Represent this sentence for searching relevant passages: {text}"])[0]
