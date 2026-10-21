"""Transport and the Groq-backed provider.

Deliberately speaks the OpenAI-compatible wire protocol over plain httpx rather
than pinning the `groq` SDK: no SDK-version drift, full control over retries and
timeouts, and no reliance on SDK-only fields for usage accounting.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Protocol

import httpx

from . import config
from .schema import Chapter, Plan


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    elapsed: float = 0.0

    def merge(self, other: Usage) -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.elapsed += other.elapsed

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def tokens_per_second(self) -> float:
        if self.elapsed <= 0:
            return 0.0
        return self.completion_tokens / self.elapsed

    def to_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "elapsed": round(self.elapsed, 2),
            "tokens_per_second": round(self.tokens_per_second, 1),
        }


class LLMError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after


class Provider(Protocol):
    """Semantic interface the pipeline depends on."""

    name: str
    architect_model: str
    writer_model: str
    editor_model: str

    async def plan(
        self, *, topic: str, audience: str, tone: str, length, notes: str = ""
    ) -> tuple[dict, Usage]:
        ...

    def write_chapter(
        self, *, plan: Plan, chapter: Chapter, part_title: str, bible: str, style: str = "",
        pov: str = "", voice_sample: str = "",
    ) -> AsyncIterator[dict]:
        ...

    async def rewrite_chunk(
        self, *, chunk: str, findings: str, rules: str, chapter: Chapter
    ) -> tuple[str, Usage]:
        ...

    async def regenerate_chapter(
        self, *, plan: Plan, chapter: Chapter, part_title: str, bible: str,
        current_text: str, instruction: str, style: str = "", pov: str = "",
        voice_sample: str = "",
    ) -> tuple[str, Usage]:
        ...

    async def summarize(self, *, chapter: Chapter, text: str) -> tuple[dict, Usage]:
        ...


# --------------------------------------------------------------------------- #
# Groq / OpenAI-compatible provider
# --------------------------------------------------------------------------- #
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
MAX_ATTEMPTS = 4


def _backoff(attempt: int, retry_after: float | None) -> float:
    if retry_after is not None:
        # Respect the provider's minimum delay (Groq may report a token-window
        # reset around 24 seconds); only cap pathological values at one hour.
        return min(max(retry_after, 0.1), 3_600.0)
    # Jitter only: this never needs to be unpredictable, just spread out.
    return min(0.8 * (2 ** (attempt - 1)) + random.uniform(0, 0.4), 12.0)  # noqa: S311


def _retry_after_seconds(headers, message: str) -> float | None:
    """Read Retry-After or Groq's human-readable reset hint."""
    raw = (headers or {}).get("retry-after", "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            try:
                reset_at = parsedate_to_datetime(raw)
                if reset_at.tzinfo is None:
                    reset_at = reset_at.replace(tzinfo=UTC)
                return max(0.0, (reset_at - datetime.now(UTC)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    match = re.search(r"(?:try again in|retry after)\s+(\d+(?:\.\d+)?)\s*(?:s|sec(?:ond)?s?)", message, re.I)
    return float(match.group(1)) if match else None


class GroqProvider:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        timeout: float = 120.0,
        architect_model: str | None = None,
        writer_model: str | None = None,
        editor_model: str | None = None,
    ):
        if not api_key:
            raise LLMError("no API key supplied")
        self._api_key = api_key
        self._base_url = (base_url or config.GROQ_BASE_URL).rstrip("/")
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15.0))
        self.name = "groq"
        self.architect_model = architect_model or config.ARCHITECT_MODEL
        self.writer_model = writer_model or config.WRITER_MODEL
        self.editor_model = editor_model or config.EDITOR_MODEL

    async def aclose(self) -> None:
        await self._client.aclose()

    async def list_models(self) -> list[dict]:
        """Return the models this Groq key can currently access."""
        response = await self._client.get(f"{self._base_url}/models", headers=self._headers)
        if response.status_code >= 400:
            raise self._error_from(response.status_code, response.content, response.headers)
        try:
            records = response.json().get("data", [])
        except (ValueError, AttributeError) as exc:
            raise LLMError("Groq returned an invalid model list") from exc
        models = []
        for record in records:
            if not isinstance(record, dict) or not record.get("id"):
                continue
            models.append({
                "id": str(record["id"]),
                "owned_by": record.get("owned_by"),
                "context_window": record.get("context_window"),
                "max_completion_tokens": record.get("max_completion_tokens"),
                "active": record.get("active"),
            })
        return models

    # -- low level ---------------------------------------------------------- #
    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _payload(self, messages, *, model, max_tokens, temperature, json_mode, stream) -> dict:
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _error_from(self, status: int, body: bytes, headers=None) -> LLMError:
        try:
            data = json.loads(body)
            message = data.get("error", {}).get("message") or data.get("message") or body[:300]
        except (json.JSONDecodeError, AttributeError, TypeError):
            message = body[:300].decode("utf-8", "replace")
        message = str(message)
        hint = ""
        if status == 401:
            hint = " — check the API key"
        elif status == 404:
            hint = " — the model ID may have been retired; see console.groq.com/docs/deprecations"
        return LLMError(
            f"Groq returned {status}: {message}{hint}",
            status=status,
            retryable=status in RETRYABLE_STATUS,
            retry_after=_retry_after_seconds(headers, message),
        )

    async def _complete_text(
        self, messages, *, model, max_tokens, temperature=0.2
    ) -> tuple[str, Usage]:
        payload = self._payload(
            messages, model=model, max_tokens=max_tokens, temperature=temperature,
            json_mode=False, stream=False,
        )
        last: LLMError | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            started = time.perf_counter()
            try:
                resp = await self._client.post(
                    f"{self._base_url}/chat/completions", headers=self._headers, json=payload
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = LLMError(f"network error talking to Groq: {exc}", retryable=True)
            else:
                if resp.status_code >= 400:
                    last = self._error_from(resp.status_code, resp.content, resp.headers)
                    if not last.retryable:
                        raise last
                else:
                    data = resp.json()
                    raw_usage = data.get("usage") or {}
                    choice = (data.get("choices") or [{}])[0]
                    content = ((choice.get("message") or {}).get("content") or "").strip()
                    usage = Usage(
                        prompt_tokens=int(raw_usage.get("prompt_tokens") or 0),
                        completion_tokens=int(raw_usage.get("completion_tokens") or 0),
                        elapsed=time.perf_counter() - started,
                    )
                    if not content:
                        raise LLMError("Groq returned an empty rewrite")
                    return content, usage
            if attempt < MAX_ATTEMPTS:
                await asyncio.sleep(_backoff(attempt, last.retry_after if last else None))
        raise last or LLMError("Groq request failed")

    async def _complete_json(self, messages, *, model, max_tokens, temperature=0.3) -> tuple[dict, Usage]:
        payload = self._payload(
            messages, model=model, max_tokens=max_tokens, temperature=temperature,
            json_mode=True, stream=False,
        )
        last: LLMError | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            started = time.perf_counter()
            try:
                resp = await self._client.post(
                    f"{self._base_url}/chat/completions", headers=self._headers, json=payload
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = LLMError(f"network error talking to Groq: {exc}", retryable=True)
            else:
                if resp.status_code >= 400:
                    last = self._error_from(resp.status_code, resp.content, resp.headers)
                    if not last.retryable:
                        raise last
                else:
                    data = resp.json()
                    raw_usage = data.get("usage") or {}
                    choice = (data.get("choices") or [{}])[0]
                    content = (choice.get("message") or {}).get("content") or ""
                    usage = Usage(
                        prompt_tokens=int(raw_usage.get("prompt_tokens") or 0),
                        completion_tokens=int(raw_usage.get("completion_tokens") or 0),
                        elapsed=time.perf_counter() - started,
                    )
                    if not content.strip():
                        raise LLMError("Groq returned an empty completion")
                    try:
                        return json.loads(content), usage
                    except json.JSONDecodeError:
                        # Models occasionally wrap JSON in a fence despite json_mode.
                        cleaned = content.strip().removeprefix("```json").removeprefix("```")
                        cleaned = cleaned.removesuffix("```").strip()
                        try:
                            return json.loads(cleaned), usage
                        except json.JSONDecodeError as exc:
                            raise LLMError(f"model did not return valid JSON: {exc}") from exc
            if attempt < MAX_ATTEMPTS:
                await asyncio.sleep(_backoff(attempt, last.retry_after if last else None))
        raise last or LLMError("Groq request failed")

    async def _stream_text(
        self, messages, *, model, max_tokens, temperature=0.7
    ) -> AsyncIterator[dict]:
        """Yield {'type':'delta'|'usage'|'finish'} events, retrying only before first token."""
        payload = self._payload(
            messages, model=model, max_tokens=max_tokens, temperature=temperature,
            json_mode=False, stream=True,
        )
        last: LLMError | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            emitted = False
            started = time.perf_counter()
            try:
                async with self._client.stream(
                    "POST", f"{self._base_url}/chat/completions",
                    headers=self._headers, json=payload,
                ) as resp:
                    if resp.status_code >= 400:
                        body = await resp.aread()
                        raise self._error_from(resp.status_code, body, resp.headers)
                    finish: str | None = None
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        usage = chunk.get("usage") or (chunk.get("x_groq") or {}).get("usage")
                        if usage:
                            yield {
                                "type": "usage",
                                "usage": Usage(
                                    prompt_tokens=int(usage.get("prompt_tokens") or 0),
                                    completion_tokens=int(usage.get("completion_tokens") or 0),
                                    elapsed=time.perf_counter() - started,
                                ),
                            }
                        for choice in chunk.get("choices") or []:
                            text = (choice.get("delta") or {}).get("content") or ""
                            if text:
                                emitted = True
                                yield {"type": "delta", "text": text}
                            finish = choice.get("finish_reason") or finish
                    yield {"type": "finish", "reason": finish}
                    return
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = LLMError(f"network error talking to Groq: {exc}", retryable=True)
            except LLMError as exc:
                last = exc
                if not exc.retryable:
                    raise
            if emitted:
                # Partial output has already streamed: restarting would duplicate text.
                raise last or LLMError("stream failed mid-chapter")
            if attempt < MAX_ATTEMPTS:
                await asyncio.sleep(_backoff(attempt, last.retry_after if last else None))
        raise last or LLMError("Groq stream failed")

    # -- pipeline interface ------------------------------------------------- #
    async def plan(
        self, *, topic: str, audience: str, tone: str, length, notes: str = ""
    ) -> tuple[dict, Usage]:
        from . import prompts

        return await self._complete_json(
            prompts.plan_messages(
                topic=topic, audience=audience, tone=tone, length=length, notes=notes
            ),
            model=self.architect_model,
            max_tokens=config.ARCHITECT_MAX_TOKENS,
        )

    async def repair_plan(self, messages, error: str) -> tuple[dict, Usage]:
        from . import prompts

        return await self._complete_json(
            prompts.plan_repair_messages(messages, error),
            model=self.architect_model,
            max_tokens=config.ARCHITECT_MAX_TOKENS,
            temperature=0.1,
        )

    async def write_chapter(
        self, *, plan: Plan, chapter: Chapter, part_title: str, bible: str, style: str = "",
        pov: str = "", voice_sample: str = "",
    ) -> AsyncIterator[dict]:
        from . import prompts

        buffer = ""
        usage = Usage()
        for attempt in range(config.MAX_CONTINUATIONS + 1):
            messages = prompts.chapter_messages(
                plan=plan, chapter=chapter, part_title=part_title, bible=bible,
                style=style, continuation=attempt > 0, pov=pov, voice_sample=voice_sample,
            )
            if attempt > 0 and buffer:
                messages[-1]["content"] += "\n\nAlready written (do not repeat):\n" + buffer[-1200:]
            finish = None
            async for event in self._stream_text(
                messages,
                model=self.writer_model,
                max_tokens=config.writer_tokens_for(chapter.words),
            ):
                if event["type"] == "delta":
                    buffer += event["text"]
                    yield event
                elif event["type"] == "usage":
                    usage.merge(event["usage"])
                elif event["type"] == "finish":
                    finish = event.get("reason")
            if finish != "length":
                break
        yield {"type": "usage", "usage": usage}

    async def regenerate_chapter(
        self,
        *,
        plan: Plan,
        chapter: Chapter,
        part_title: str,
        bible: str,
        current_text: str,
        instruction: str,
        style: str = "",
        pov: str = "",
        voice_sample: str = "",
    ) -> tuple[str, Usage]:
        from . import prompts

        messages = prompts.chapter_regeneration_messages(
            plan=plan,
            chapter=chapter,
            part_title=part_title,
            bible=bible,
            current_text=current_text,
            instruction=instruction,
            style=style,
            pov=pov,
            voice_sample=voice_sample,
        )
        return await self._complete_text(
            messages,
            model=self.writer_model,
            max_tokens=config.writer_tokens_for(chapter.words),
            temperature=0.45,
        )

    async def rewrite_chunk(
        self, *, chunk: str, findings: str, rules: str, chapter: Chapter
    ) -> tuple[str, Usage]:
        from . import prompts

        messages = prompts.stylist_messages(
            chunk=chunk, findings_text=findings, rules_text=rules, chapter=chapter
        )
        return await self._complete_text(
            messages,
            model=self.editor_model,
            max_tokens=config.writer_tokens_for(len(chunk.split())),
            temperature=0.2,
        )

    async def summarize(self, *, chapter: Chapter, text: str) -> tuple[dict, Usage]:
        from . import prompts

        return await self._complete_json(
            prompts.summary_messages(chapter=chapter, text=text),
            model=self.editor_model,
            max_tokens=config.EDITOR_MAX_TOKENS,
            temperature=0.2,
        )
