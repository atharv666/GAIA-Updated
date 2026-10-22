"""Provider-agnostic LLM / VLM interface.

* ``GeminiLLM``        - Google Gemini via the google-genai SDK (native JSON schema output).
* ``OpenAICompatLLM``  - any OpenAI-compatible /chat/completions API: Groq, xAI Grok,
                         OpenAI, OpenRouter, Together, a local vLLM/Ollama server ...
* ``UnavailableLLM``   - placeholder when no key is configured; pipelines degrade gracefully.

All pipelines only use ``complete`` (free text) and ``structured`` (validated
Pydantic output), so switching providers is a configuration change.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Literal, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from ..config import DEFAULT_MODELS, Settings

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)
R = TypeVar("R")


class LLMError(RuntimeError):
    """The provider failed or returned unusable output."""


class LLMUnavailable(LLMError):
    """No provider is configured for this role."""


@dataclass(frozen=True)
class ChatTurn:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class ImagePart:
    data: bytes
    mime_type: str = "image/jpeg"


class _Retryable(Exception):
    pass


def with_retries(fn: Callable[[], R], *, attempts: int = 3, base_delay: float = 1.5) -> R:
    for attempt in range(attempts):
        try:
            return fn()
        except _Retryable as exc:
            if attempt == attempts - 1:
                raise LLMError(str(exc.__cause__ or exc)) from exc
            delay = base_delay * (2**attempt)
            log.warning("Transient provider error (%s); retrying in %.1fs", exc.__cause__ or exc, delay)
            time.sleep(delay)
    raise AssertionError("unreachable")


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def extract_json(text: str) -> str:
    """Strip markdown fences / prose around a JSON object."""
    cleaned = _FENCE_RE.sub("", text.strip()).strip()
    if cleaned.startswith("{"):
        return cleaned
    start, end = cleaned.find("{"), cleaned.rfind("}")
    return cleaned[start : end + 1] if start != -1 and end > start else cleaned


class LLM(ABC):
    provider = "base"

    def __init__(self, model: str):
        self.model = model

    @property
    def available(self) -> bool:
        return True

    @property
    def model_id(self) -> str:
        return f"{self.provider}:{self.model}"

    @abstractmethod
    def _generate(
        self,
        *,
        system: str,
        turns: Sequence[ChatTurn],
        images: Sequence[ImagePart],
        temperature: float,
        max_tokens: int,
        json_mode: bool,
    ) -> str: ...

    def complete(
        self,
        *,
        system: str,
        turns: Sequence[ChatTurn],
        images: Sequence[ImagePart] = (),
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> str:
        return self._generate(
            system=system, turns=turns, images=images, temperature=temperature,
            max_tokens=max_tokens, json_mode=False,
        )

    def structured(
        self,
        *,
        system: str,
        turns: Sequence[ChatTurn],
        schema: type[T],
        images: Sequence[ImagePart] = (),
        temperature: float = 0.1,
        max_tokens: int = 4096,
    ) -> T:
        """Generate JSON that validates against ``schema`` (one self-repair attempt)."""
        schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        sys = (
            f"{system}\n\nReturn ONLY a JSON object (no markdown, no prose) that validates "
            f"against this JSON Schema:\n{schema_json}"
        )
        raw = self._generate(
            system=sys, turns=turns, images=images, temperature=temperature,
            max_tokens=max_tokens, json_mode=True,
        )
        try:
            return schema.model_validate_json(extract_json(raw))
        except ValidationError as exc:
            log.info("Structured output invalid, attempting repair: %s", exc.errors()[:2])
            repair = [
                *turns,
                ChatTurn("assistant", raw),
                ChatTurn(
                    "user",
                    f"The JSON above is invalid: {exc.errors()[:3]}. "
                    "Return only the corrected JSON object.",
                ),
            ]
            raw = self._generate(
                system=sys, turns=repair, images=images, temperature=0.0,
                max_tokens=max_tokens, json_mode=True,
            )
            try:
                return schema.model_validate_json(extract_json(raw))
            except ValidationError as exc2:
                raise LLMError(f"{self.model_id} returned invalid structured output: {exc2}") from exc2


    def stream_structured(
        self,
        *,
        system: str,
        turns: Sequence[ChatTurn],
        schema: type[T],
        images: Sequence[ImagePart] = (),
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> Iterator[str]:
        """Yield the JSON for ``schema`` as text chunks as the model produces it.

        Providers with native streaming override this. The default produces the whole object
        with ``structured`` and yields it as a single chunk, so callers never need to care.
        """
        yield self.structured(
            system=system, turns=turns, schema=schema, images=images,
            temperature=temperature, max_tokens=max_tokens,
        ).model_dump_json()


class UnavailableLLM(LLM):
    provider = "none"

    def __init__(self, reason: str):
        super().__init__("none")
        self.reason = reason

    @property
    def available(self) -> bool:
        return False

    def _generate(self, **_) -> str:
        raise LLMUnavailable(self.reason)

    def structured(self, **_):  # type: ignore[override]
        raise LLMUnavailable(self.reason)


class GeminiLLM(LLM):
    provider = "gemini"

    def __init__(self, model: str, api_key: str, *, thinking_budget: int | None = None, timeout: float = 90):
        super().__init__(model)
        from google import genai
        from google.genai import types

        self._types = types
        self._client = genai.Client(
            api_key=api_key, http_options=types.HttpOptions(timeout=int(timeout * 1000))
        )
        self._thinking_budget = thinking_budget

    def _contents(self, turns: Sequence[ChatTurn], images: Sequence[ImagePart]):
        types = self._types
        contents = []
        for i, turn in enumerate(turns):
            parts = [types.Part.from_text(text=turn.content)]
            if images and i == len(turns) - 1 and turn.role == "user":
                parts = [types.Part.from_bytes(data=im.data, mime_type=im.mime_type) for im in images] + parts
            contents.append(types.Content(role="user" if turn.role == "user" else "model", parts=parts))
        return contents

    def _config(self, system: str, temperature: float, max_tokens: int, **extra):
        cfg = dict(
            system_instruction=system,
            temperature=temperature,
            max_output_tokens=max_tokens,
            # No tools are passed; disabling AFC also silences the SDK's per-call warning.
            automatic_function_calling=self._types.AutomaticFunctionCallingConfig(disable=True),
            **extra,
        )
        if self._thinking_budget is not None:
            cfg["thinking_config"] = self._types.ThinkingConfig(thinking_budget=self._thinking_budget)
        return self._types.GenerateContentConfig(**cfg)

    def _call(self, contents, config):
        from google.genai import errors

        def run():
            try:
                return self._client.models.generate_content(model=self.model, contents=contents, config=config)
            except errors.APIError as exc:
                if exc.code in (429, 500, 502, 503, 504):
                    raise _Retryable(str(exc)) from exc
                raise LLMError(f"Gemini error {exc.code}: {exc.message}") from exc
            except httpx.TransportError as exc:
                raise _Retryable(str(exc)) from exc

        return with_retries(run)

    @staticmethod
    def _text(resp) -> str:
        text = resp.text
        if not text:
            reason = None
            if resp.candidates:
                reason = resp.candidates[0].finish_reason
            feedback = getattr(resp, "prompt_feedback", None)
            raise LLMError(f"Gemini returned no text (finish_reason={reason}, feedback={feedback})")
        return text

    def _generate(self, *, system, turns, images, temperature, max_tokens, json_mode) -> str:
        # Gemini 2.5 counts thinking tokens against max_output_tokens: keep headroom.
        extra = {"response_mime_type": "application/json"} if json_mode else {}
        config = self._config(system, temperature, max(max_tokens, 8192), **extra)
        return self._text(self._call(self._contents(turns, images), config))

    def structured(self, *, system, turns, schema, images=(), temperature=0.1, max_tokens=4096):
        try:
            config = self._config(
                system, temperature, max(max_tokens, 8192),
                response_mime_type="application/json", response_schema=schema,
            )
        except Exception as exc:  # schema not expressible in Gemini's subset
            log.info("Falling back to prompt-level schema for %s: %s", schema.__name__, exc)
            return super().structured(
                system=system, turns=turns, schema=schema, images=images,
                temperature=temperature, max_tokens=max_tokens,
            )
        resp = self._call(self._contents(turns, images), config)
        if isinstance(resp.parsed, schema):
            return resp.parsed
        try:
            return schema.model_validate_json(extract_json(self._text(resp)))
        except (ValidationError, LLMError):
            return super().structured(
                system=system, turns=turns, schema=schema, images=images,
                temperature=temperature, max_tokens=max_tokens,
            )


    def stream_structured(
        self, *, system, turns, schema, images=(), temperature=0.2, max_tokens=4096
    ) -> Iterator[str]:
        from google.genai import errors

        try:
            config = self._config(
                system, temperature, max(max_tokens, 8192),
                response_mime_type="application/json", response_schema=schema,
            )
        except Exception:  # schema not expressible in Gemini's subset
            yield from super().stream_structured(
                system=system, turns=turns, schema=schema, images=images,
                temperature=temperature, max_tokens=max_tokens,
            )
            return
        contents = self._contents(turns, images)
        attempts = 3
        for attempt in range(attempts):
            emitted = False
            try:
                for chunk in self._client.models.generate_content_stream(
                    model=self.model, contents=contents, config=config
                ):
                    text = chunk.text
                    if text:
                        emitted = True
                        yield text
                return
            except errors.APIError as exc:
                retryable = exc.code in (429, 500, 502, 503, 504)
                if emitted or not retryable or attempt == attempts - 1:
                    raise LLMError(f"Gemini error {exc.code}: {exc.message}") from exc
            except httpx.TransportError as exc:
                if emitted or attempt == attempts - 1:
                    raise LLMError(str(exc)) from exc
            time.sleep(1.5 * (2**attempt))


class OpenAICompatLLM(LLM):
    provider = "openai_compat"

    def __init__(self, model: str, base_url: str, api_key: str, *, timeout: float = 90, reasoning_effort: str | None = None):
        super().__init__(model)
        self.base_url = base_url.rstrip("/")
        self.reasoning_effort = reasoning_effort
        self._http = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )

    @property
    def model_id(self) -> str:
        host = httpx.URL(self.base_url).host
        return f"{self.provider}({host}):{self.model}"

    def _messages(self, system: str, turns: Sequence[ChatTurn], images: Sequence[ImagePart]) -> list[dict]:
        messages: list[dict] = [{"role": "system", "content": system}]
        for i, turn in enumerate(turns):
            if images and i == len(turns) - 1 and turn.role == "user":
                content: list[dict] | str = [{"type": "text", "text": turn.content}] + [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{im.mime_type};base64,{base64.b64encode(im.data).decode()}"},
                    }
                    for im in images
                ]
            else:
                content = turn.content
            messages.append({"role": turn.role, "content": content})
        return messages

    def _generate(self, *, system, turns, images, temperature, max_tokens, json_mode) -> str:
        body: dict = {
            "model": self.model,
            "messages": self._messages(system, turns, images),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort

        def run() -> dict:
            try:
                r = self._http.post("/chat/completions", json=body)
            except httpx.TransportError as exc:
                raise _Retryable(str(exc)) from exc
            if r.status_code in (429, 500, 502, 503, 504):
                raise _Retryable(f"HTTP {r.status_code}: {r.text[:300]}")
            if r.status_code >= 400:
                raise LLMError(f"{self.model_id} HTTP {r.status_code}: {r.text[:500]}")
            return r.json()

        data = with_retries(run)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Unexpected response from {self.model_id}: {str(data)[:300]}") from exc
        # Some hosts inline a reasoning model's thinking as <think>...</think>; keep only the answer.
        content = _THINK_RE.sub("", content or "").strip()
        if not content:
            raise LLMError(f"{self.model_id} returned an empty message")
        return content

    def stream_structured(
        self, *, system, turns, schema, images=(), temperature=0.2, max_tokens=4096
    ) -> Iterator[str]:
        schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        sys = (
            f"{system}\n\nReturn ONLY a JSON object (no markdown, no prose) that validates "
            f"against this JSON Schema, writing the \"answer\" property first:\n{schema_json}"
        )
        body: dict = {
            "model": self.model,
            "messages": self._messages(sys, turns, images),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
            "response_format": {"type": "json_object"},
        }
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        try:
            with self._http.stream("POST", "/chat/completions", json=body) as r:
                if r.status_code >= 400:
                    r.read()
                    raise LLMError(f"{self.model_id} HTTP {r.status_code}: {r.text[:500]}")
                for line in r.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        return
                    try:
                        delta = json.loads(payload)["choices"][0].get("delta", {}).get("content")
                    except (ValueError, KeyError, IndexError, TypeError):
                        continue
                    if delta:
                        yield delta
        except httpx.TransportError as exc:
            raise LLMError(f"{self.model_id} stream failed: {exc}") from exc


def build_llm(settings: Settings, role: Literal["chat", "vision"]) -> LLM:
    provider = settings.llm_provider if role == "chat" else settings.vlm_provider
    model = settings.llm_model if role == "chat" else settings.vlm_model
    if provider == "auto":
        if settings.gemini_api_key:
            provider = "gemini"
        elif settings.openai_compat_api_key:
            provider = "openai_compat"
        else:
            provider = "none"
    if provider == "gemini":
        if not settings.gemini_api_key:
            return UnavailableLLM("GEMINI_API_KEY is not set in backend/.env")
        return GeminiLLM(
            model or DEFAULT_MODELS[("gemini", role)],
            settings.gemini_api_key,
            thinking_budget=settings.gemini_thinking_budget,
            timeout=settings.llm_timeout_seconds,
        )
    if provider == "openai_compat":
        if not settings.openai_compat_api_key:
            return UnavailableLLM("OPENAI_COMPAT_API_KEY is not set in backend/.env")
        return OpenAICompatLLM(
            model or DEFAULT_MODELS[("openai_compat", role)],
            settings.openai_compat_base_url,
            settings.openai_compat_api_key,
            timeout=settings.llm_timeout_seconds,
            reasoning_effort=settings.openai_compat_reasoning_effort,
        )
    what = "chat LLM" if role == "chat" else "vision-language model"
    return UnavailableLLM(
        f"No {what} configured. Set GEMINI_API_KEY (or OPENAI_COMPAT_API_KEY for Groq / xAI Grok) "
        "in backend/.env"
    )
