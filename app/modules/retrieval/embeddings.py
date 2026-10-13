"""Embedding provider abstraction (decision A5: Ollama behind an adapter).

The application only ever talks to :class:`EmbeddingProvider`; the two
shipped adapters are:

- :class:`OllamaEmbeddingProvider` — the production HTTP adapter for
  Ollama's ``/api/embeddings`` endpoint (one prompt per request).
- :class:`FakeEmbeddingProvider` — a deterministic, dependency-free
  bag-of-tokens vectorizer for tests and offline development.

Every failure is reported as :class:`EmbeddingError` so callers (the
ingest pipeline) can map it to a ``failed`` document status without
parsing HTTP exceptions.
"""

import math
import zlib
from typing import Protocol, runtime_checkable

import httpx

from app.core.config import Settings


class EmbeddingError(Exception):
    """Raised when an embedding backend fails or returns unusable output."""


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Async batch text embedder with a known output dimension."""

    @property
    def dimension(self) -> int: ...

    async def embed_texts(self, texts: list[str]) -> list[list[float]]: ...


class FakeEmbeddingProvider:
    """Deterministic bag-of-tokens embedder for tests and offline dev.

    Each whitespace token is hashed (CRC32, stable across processes, unlike
    ``hash()``) into one of ``dimension`` buckets; the bucket counter is
    L2-normalized. Texts sharing tokens therefore share buckets and score a
    higher cosine similarity than disjoint texts — enough structure for
    ranking assertions without any model or network.
    """

    def __init__(self, dimension: int = 768) -> None:
        if dimension < 1:
            raise ValueError(f"dimension must be >= 1, got {dimension}")
        self._dimension = dimension

    @property
    def dimension(self) -> int:
        return self._dimension

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self._dimension
        for token in text.lower().split():
            vector[zlib.crc32(token.encode("utf-8")) % self._dimension] += 1.0
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return vector
        return [value / norm for value in vector]


class OllamaEmbeddingProvider:
    """HTTP adapter for Ollama's ``POST {base_url}/api/embeddings``.

    Ollama embeds one prompt per request, so a batch is sent as a
    sequential loop (ingest batches are chunk-sized, not page-sized).
    ``transport`` is a test seam for ``httpx.MockTransport``; production
    uses the default transport.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        dimension: int,
        timeout: float,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url
        self._model = model
        self._dimension = dimension
        self._timeout = timeout
        self._transport = transport

    @property
    def dimension(self) -> int:
        return self._dimension

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        async with httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            transport=self._transport,
        ) as client:
            for text in texts:
                try:
                    response = await client.post(
                        "/api/embeddings",
                        json={"model": self._model, "prompt": text},
                    )
                    response.raise_for_status()
                    payload = response.json()
                    embedding = payload["embedding"]
                    vectors.append([float(value) for value in embedding])
                except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                    raise EmbeddingError(f"ollama embeddings request failed: {exc}") from exc
        return vectors


def build_embedding_provider(settings: Settings) -> EmbeddingProvider:
    """Construct the adapter named by ``settings.embedding_provider``."""
    if settings.embedding_provider == "fake":
        return FakeEmbeddingProvider(dimension=settings.embedding_dim)
    if settings.embedding_provider == "ollama":
        return OllamaEmbeddingProvider(
            base_url=settings.ollama_base_url,
            model=settings.embedding_model,
            dimension=settings.embedding_dim,
            timeout=settings.embedding_timeout_seconds,
        )
    raise ValueError(
        f"unknown embedding_provider {settings.embedding_provider!r}; expected 'ollama' or 'fake'"
    )
