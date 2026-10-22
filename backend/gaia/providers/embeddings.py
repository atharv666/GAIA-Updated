"""Text embedding providers for the knowledge store.

Every stored chunk records ``embed_model`` (provider:model:dim). Vector search
only compares vectors from the same model, and ``POST /api/v1/ingest/reindex``
re-embeds the store after a model change.
"""

from __future__ import annotations

import hashlib
import logging
import re
from abc import ABC, abstractmethod

import httpx
import numpy as np

from ..config import DEFAULT_MODELS, Settings
from .llm import LLMError, _Retryable, with_retries

log = logging.getLogger(__name__)


def l2_normalize(vec) -> list[float]:
    arr = np.asarray(vec, dtype=np.float64)
    norm = np.linalg.norm(arr)
    return (arr / norm if norm > 0 else arr).tolist()


class Embedder(ABC):
    provider = "base"

    def __init__(self, model: str, dim: int):
        self.model = model
        self.dim = dim

    @property
    def model_id(self) -> str:
        return f"{self.provider}:{self.model}:{self.dim}"

    @abstractmethod
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    @abstractmethod
    def embed_query(self, text: str) -> list[float]: ...


class GeminiEmbedder(Embedder):
    provider = "gemini"
    batch_size = 100

    def __init__(self, model: str, dim: int, api_key: str):
        super().__init__(model, dim)
        from google import genai
        from google.genai import types

        self._types = types
        self._client = genai.Client(api_key=api_key)

    def _embed(self, texts: list[str], task_type: str) -> list[list[float]]:
        from google.genai import errors

        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i : i + self.batch_size]

            def run(batch=batch):
                try:
                    return self._client.models.embed_content(
                        model=self.model,
                        contents=batch,
                        config=self._types.EmbedContentConfig(task_type=task_type, output_dimensionality=self.dim),
                    )
                except errors.APIError as exc:
                    if exc.code in (429, 500, 502, 503, 504):
                        raise _Retryable(str(exc)) from exc
                    raise LLMError(f"Gemini embedding error {exc.code}: {exc.message}") from exc

            resp = with_retries(run)
            out.extend(l2_normalize(e.values) for e in resp.embeddings)
        return out

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, "RETRIEVAL_DOCUMENT") if texts else []

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], "RETRIEVAL_QUERY")[0]


class OpenAICompatEmbedder(Embedder):
    provider = "openai_compat"
    batch_size = 96

    def __init__(self, model: str, dim: int, base_url: str, api_key: str):
        super().__init__(model, dim)
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"), headers={"Authorization": f"Bearer {api_key}"}, timeout=60
        )

    def _embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i : i + self.batch_size]

            def run(batch=batch):
                try:
                    r = self._http.post("/embeddings", json={"model": self.model, "input": batch, "dimensions": self.dim})
                except httpx.TransportError as exc:
                    raise _Retryable(str(exc)) from exc
                if r.status_code in (429, 500, 502, 503):
                    raise _Retryable(f"HTTP {r.status_code}")
                if r.status_code >= 400:
                    raise LLMError(f"Embedding HTTP {r.status_code}: {r.text[:300]}")
                return r.json()

            data = with_retries(run)
            rows = sorted(data["data"], key=lambda d: d["index"])
            out.extend(l2_normalize(row["embedding"]) for row in rows)
        return out

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts) if texts else []

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text])[0]


_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "a an the and or of to in on at for from by with is are was were be been it its this that "
    "what how when which who do does did my me i you your our we there than then as".split()
)


def _stem(tok: str) -> str:
    for suffix in ("ities", "ity", "ings", "ing", "ies", "ed", "es", "s"):
        if tok.endswith(suffix) and len(tok) - len(suffix) >= 3:
            return tok[: -len(suffix)]
    return tok


def tokenize(text: str) -> list[str]:
    return [_stem(t) for t in _TOKEN_RE.findall(text.lower()) if t not in _STOP]


class HashingEmbedder(Embedder):
    """Deterministic, offline feature-hashing embedder.

    Not semantic like a neural model, but lets the full system (and the test
    suite) run without any API key. Words, word prefixes and bigrams are
    hashed into ``dim`` signed buckets.
    """

    provider = "hashing"

    def __init__(self, dim: int):
        super().__init__("hashing-v1", dim)

    def _vec(self, text: str) -> list[float]:
        vec = np.zeros(self.dim)
        toks = tokenize(text)
        feats: list[tuple[str, float]] = [(t, 1.0) for t in toks]
        feats += [(f"p:{t[:5]}", 0.5) for t in toks if len(t) > 5]
        feats += [(f"b:{a}_{b}", 0.7) for a, b in zip(toks, toks[1:])]
        for feat, weight in feats:
            h = int.from_bytes(hashlib.blake2b(feat.encode(), digest_size=8).digest(), "little")
            vec[h % self.dim] += weight if (h >> 63) & 1 else -weight
        return l2_normalize(vec)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


def build_embedder(settings: Settings) -> Embedder:
    provider = settings.embedding_provider
    if provider == "auto":
        provider = "gemini" if settings.gemini_api_key else "hashing"
        if provider == "hashing":
            log.warning(
                "No GEMINI_API_KEY: using the offline hashing embedder. Retrieval works but is "
                "keyword-like; set GEMINI_API_KEY for semantic embeddings."
            )
    if provider == "gemini":
        if not settings.gemini_api_key:
            raise RuntimeError("EMBEDDING_PROVIDER=gemini requires GEMINI_API_KEY")
        return GeminiEmbedder(
            settings.embedding_model or DEFAULT_MODELS[("gemini", "embedding")],
            settings.embedding_dim,
            settings.gemini_api_key,
        )
    if provider == "openai_compat":
        if not settings.openai_compat_api_key:
            raise RuntimeError("EMBEDDING_PROVIDER=openai_compat requires OPENAI_COMPAT_API_KEY")
        return OpenAICompatEmbedder(
            settings.embedding_model or DEFAULT_MODELS[("openai_compat", "embedding")],
            settings.embedding_dim,
            settings.openai_compat_base_url,
            settings.openai_compat_api_key,
        )
    return HashingEmbedder(settings.embedding_dim)
