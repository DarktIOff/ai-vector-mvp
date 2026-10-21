from __future__ import annotations

import asyncio
import hashlib
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from openai import (
    APIConnectionError,
    APIStatusError,
    AsyncOpenAI,
    DefaultAsyncHttpxClient,
)

from .config import Settings


@dataclass(frozen=True)
class ProviderCapabilities:
    streaming: bool
    structured_output: bool
    data_policy: str


class LLMProviderError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class CircuitBreaker:
    failure_threshold: int = 3
    reset_after_seconds: float = 60.0
    failures: int = 0
    opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self.opened_at is None:
            return False
        if time.monotonic() - self.opened_at >= self.reset_after_seconds:
            self.failures = 0
            self.opened_at = None
            return False
        return True

    def success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= self.failure_threshold:
            self.opened_at = time.monotonic()


class LLMProvider(ABC):
    name: str
    model: str

    @abstractmethod
    async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        raise NotImplementedError

    @abstractmethod
    async def stream(
        self, messages: list[dict[str, str]], **kwargs: Any
    ) -> AsyncIterator[str]:
        raise NotImplementedError

    @abstractmethod
    async def healthcheck(self) -> dict:
        raise NotImplementedError

    @abstractmethod
    def capabilities(self) -> ProviderCapabilities:
        raise NotImplementedError


class DeterministicProvider(LLMProvider):
    name = "deterministic"
    model = "rules-v1"

    async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        return messages[-1]["content"] if messages else ""

    async def stream(
        self, messages: list[dict[str, str]], **kwargs: Any
    ) -> AsyncIterator[str]:
        text = await self.complete(messages, **kwargs)
        for word in text.split(" "):
            yield word + " "
            await asyncio.sleep(0)

    async def healthcheck(self) -> dict:
        return {
            "status": "available",
            "provider": self.name,
            "model": self.model,
            "fallback": True,
        }

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(True, True, "none")


class OpenAICompatibleProvider(LLMProvider):
    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str,
        model: str,
        timeout: int,
        max_retries: int,
        data_policy: str,
        trust_env: bool = True,
    ):
        self.name = name
        self.model = model
        self._configured = bool(api_key)
        self._client = (
            AsyncOpenAI(
                base_url=base_url,
                api_key=api_key,
                timeout=timeout,
                # Retry policy belongs to this provider wrapper.  The OpenAI
                # client otherwise performs two hidden retries even when a
                # certification scenario explicitly requires zero retries.
                max_retries=0,
                http_client=DefaultAsyncHttpxClient(
                    timeout=timeout,
                    trust_env=trust_env,
                ),
            )
            if api_key
            else None
        )
        self._data_policy = data_policy
        self._max_retries = max(0, max_retries)
        self._breaker = CircuitBreaker()
        self.last_usage: dict[str, int] = {}
        self.last_fallback = False

    async def _remote_completion(
        self,
        messages: list[dict[str, str]],
        *,
        stream: bool,
        **kwargs: Any,
    ):
        if self._client is None:
            raise LLMProviderError("not_configured", "Провайдер не настроен")
        if self._breaker.is_open:
            raise LLMProviderError(
                "circuit_open",
                "Провайдер временно отключён после повторяющихся ошибок",
            )
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    stream=stream,
                    **kwargs,
                )
                self._breaker.success()
                return response
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                self._breaker.failure()
                if attempt >= self._max_retries or self._breaker.is_open:
                    break
                await asyncio.sleep(min(0.25 * (2**attempt), 2.0))
        code = "provider_unavailable"
        message = f"Провайдер {self.name} недоступен после повторов"
        if isinstance(last_error, APIStatusError):
            if last_error.status_code == 401:
                code = "invalid_api_key"
                message = "API-ключ провайдера отклонён"
            elif last_error.status_code == 429:
                body = last_error.body if isinstance(last_error.body, dict) else {}
                error = body.get("error", body)
                api_code = error.get("code") if isinstance(error, dict) else None
                code = (
                    "insufficient_balance"
                    if str(api_code) == "1113"
                    else "rate_limited"
                )
                message = (
                    "Недостаточно средств или пакета запросов у провайдера"
                    if code == "insufficient_balance"
                    else "Провайдер ограничил частоту запросов"
                )
            elif last_error.status_code == 404:
                code = "model_or_endpoint_not_found"
                message = "Модель или адрес API не найдены"
            elif last_error.status_code in {400, 422}:
                code = "invalid_request"
                message = "Провайдер отклонил параметры запроса"
        elif isinstance(last_error, APIConnectionError):
            code = "connection_error"
            message = "Не удалось установить соединение с провайдером"
        raise LLMProviderError(code, message) from last_error

    async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        allow_fallback = bool(kwargs.pop("allow_fallback", True))
        self.last_fallback = False
        try:
            response = await self._remote_completion(
                messages, stream=False, **kwargs
            )
            usage = getattr(response, "usage", None)
            if usage is not None:
                self.last_usage = {
                    "prompt_tokens": int(
                        getattr(usage, "prompt_tokens", 0) or 0
                    ),
                    "completion_tokens": int(
                        getattr(usage, "completion_tokens", 0) or 0
                    ),
                    "total_tokens": int(
                        getattr(usage, "total_tokens", 0) or 0
                    ),
                }
            content = response.choices[0].message.content
            if not isinstance(content, str) or not content.strip():
                raise LLMProviderError(
                    "invalid_response", "Провайдер вернул пустой ответ"
                )
            return content
        except (
            LLMProviderError,
            AttributeError,
            IndexError,
            TypeError,
        ) as exc:
            normalized = (
                exc
                if isinstance(exc, LLMProviderError)
                else LLMProviderError(
                    "invalid_response", "Некорректная структура ответа провайдера"
                )
            )
            if not allow_fallback:
                raise normalized from exc
            self.last_fallback = True
            return await DeterministicProvider().complete(messages)

    async def stream(
        self, messages: list[dict[str, str]], **kwargs: Any
    ) -> AsyncIterator[str]:
        allow_fallback = bool(kwargs.pop("allow_fallback", True))
        self.last_fallback = False
        emitted = False
        try:
            response = await self._remote_completion(
                messages, stream=True, **kwargs
            )
            async for chunk in response:
                text = chunk.choices[0].delta.content
                if text:
                    emitted = True
                    yield text
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    self.last_usage = {
                        "prompt_tokens": int(
                            getattr(usage, "prompt_tokens", 0) or 0
                        ),
                        "completion_tokens": int(
                            getattr(usage, "completion_tokens", 0) or 0
                        ),
                        "total_tokens": int(
                            getattr(usage, "total_tokens", 0) or 0
                        ),
                    }
        except (
            LLMProviderError,
            AttributeError,
            IndexError,
            TypeError,
        ) as exc:
            normalized = (
                exc
                if isinstance(exc, LLMProviderError)
                else LLMProviderError(
                    "invalid_response", "Некорректная структура ответа провайдера"
                )
            )
            if emitted or not allow_fallback:
                raise normalized from exc
            self.last_fallback = True
            async for text in DeterministicProvider().stream(messages):
                yield text

    async def healthcheck(self) -> dict:
        if not self._configured:
            return {
                "status": "not_configured",
                "provider": self.name,
                "model": self.model,
                "fallback": True,
            }
        try:
            await self.complete(
                [
                    {
                        "role": "user",
                        "content": "Ответь одним словом: доступен",
                    }
                ],
                max_tokens=8,
                allow_fallback=False,
            )
            return {
                "status": "available",
                "provider": self.name,
                "model": self.model,
                "fallback": False,
                "circuit_open": self._breaker.is_open,
            }
        except LLMProviderError as exc:
            return {
                "status": "unavailable",
                "provider": self.name,
                "model": self.model,
                "fallback": True,
                "circuit_open": self._breaker.is_open,
                "error_code": exc.code,
            }

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(True, True, self._data_policy)


class ZAIProvider(OpenAICompatibleProvider):
    def __init__(self, settings: Settings):
        super().__init__(
            name="zai",
            base_url=settings.zai_base_url,
            api_key=settings.zai_api_key,
            model=settings.llm_query_model,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
            data_policy=settings.llm_data_policy,
        )


class GnivcQwenProvider(OpenAICompatibleProvider):
    _NO_THINK_SUFFIX = "\n\n/no_think"
    _NO_THINK_EXTRA_BODY = {
        "chat_template_kwargs": {"enable_thinking": False}
    }

    def __init__(self, settings: Settings, *, model: str | None = None):
        super().__init__(
            name="gnivc_qwen",
            base_url=settings.gnivc_base_url,
            api_key=settings.gnivc_api_key,
            model=model or settings.gnivc_query_model,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
            data_policy=settings.llm_data_policy,
            trust_env=False,
        )

    @classmethod
    def _normalized_messages(
        cls, messages: list[dict[str, str]]
    ) -> list[dict[str, str]]:
        normalized = [dict(message) for message in messages]
        for index in range(len(normalized) - 1, -1, -1):
            if normalized[index].get("role") != "user":
                continue
            content = normalized[index].get("content", "")
            if "/no_think" not in content:
                normalized[index]["content"] = content + cls._NO_THINK_SUFFIX
            break
        return normalized

    @classmethod
    def _normalized_kwargs(cls, kwargs: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(kwargs)
        # GPUStack Qwen is reliable when JSON is requested in the prompt. Some
        # deployments reject OpenAI's response_format even though the generated
        # JSON itself is valid.
        normalized.pop("response_format", None)
        if float(normalized.get("temperature", 0.0) or 0.0) <= 0:
            normalized["temperature"] = 0.2
        normalized.setdefault("top_p", 1)
        # Z.AI and Qwen expose different thinking controls. Never forward the
        # Z.AI-specific body to the local GPUStack endpoint.
        normalized["extra_body"] = cls._NO_THINK_EXTRA_BODY
        return normalized

    async def complete(
        self, messages: list[dict[str, str]], **kwargs: Any
    ) -> str:
        return await super().complete(
            self._normalized_messages(messages),
            **self._normalized_kwargs(kwargs),
        )

    async def stream(
        self, messages: list[dict[str, str]], **kwargs: Any
    ) -> AsyncIterator[str]:
        async for chunk in super().stream(
            self._normalized_messages(messages),
            **self._normalized_kwargs(kwargs),
        ):
            yield chunk

    async def healthcheck(self) -> dict:
        if not self._configured:
            return await super().healthcheck()
        try:
            await self.complete(
                [
                    {
                        "role": "user",
                        "content": 'Верни строго JSON: {"ok": true}.',
                    }
                ],
                max_tokens=1024,
                allow_fallback=False,
            )
            return {
                "status": "available",
                "provider": self.name,
                "model": self.model,
                "fallback": False,
                "circuit_open": self._breaker.is_open,
            }
        except LLMProviderError as exc:
            return {
                "status": "unavailable",
                "provider": self.name,
                "model": self.model,
                "fallback": True,
                "circuit_open": self._breaker.is_open,
                "error_code": exc.code,
            }


class GnivcGigaChatProvider(OpenAICompatibleProvider):
    """GigaChat served by the same GNIVC OpenAI-compatible gateway as Qwen."""

    def __init__(self, settings: Settings):
        super().__init__(
            name="gnivc_gigachat",
            base_url=settings.gnivc_base_url,
            api_key=settings.gnivc_api_key,
            model=settings.report_synthesis_model,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
            data_policy=settings.llm_data_policy,
            trust_env=False,
        )

    @staticmethod
    def _normalized_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(kwargs)
        normalized.pop("extra_body", None)
        if float(normalized.get("temperature", 0.0) or 0.0) <= 0:
            normalized["temperature"] = 0.2
        normalized.setdefault("top_p", 1)
        return normalized

    async def complete(
        self, messages: list[dict[str, str]], **kwargs: Any
    ) -> str:
        return await super().complete(
            messages,
            **self._normalized_kwargs(kwargs),
        )

    async def stream(
        self, messages: list[dict[str, str]], **kwargs: Any
    ) -> AsyncIterator[str]:
        async for chunk in super().stream(
            messages,
            **self._normalized_kwargs(kwargs),
        ):
            yield chunk


_PROVIDER_CACHE: dict[tuple[Any, ...], LLMProvider] = {}
_NARRATIVE_PROVIDER_CACHE: dict[tuple[Any, ...], LLMProvider] = {}
_REPORT_SYNTHESIS_PROVIDER_CACHE: dict[tuple[Any, ...], LLMProvider] = {}


def _secret_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else ""


def _provider_cache_key(
    settings: Settings,
    *,
    narrative: bool = False,
) -> tuple[Any, ...]:
    provider = settings.llm_provider
    if provider == "zai":
        return (
            provider,
            settings.zai_base_url,
            _secret_fingerprint(settings.zai_api_key),
            (
                settings.llm_narrative_model
                if narrative
                else settings.llm_query_model
            ),
            settings.llm_timeout_seconds,
            settings.llm_max_retries,
            settings.llm_data_policy,
        )
    if provider in {"gnivc", "gnivc_qwen", "qwen"}:
        return (
            "gnivc_qwen",
            settings.gnivc_base_url,
            _secret_fingerprint(settings.gnivc_api_key),
            (
                settings.gnivc_narrative_model
                if narrative
                else settings.gnivc_query_model
            ),
            settings.llm_timeout_seconds,
            settings.llm_max_retries,
            settings.llm_data_policy,
        )
    return ("deterministic",)


def configured_provider(settings: Settings) -> LLMProvider:
    # Analysis runs create equivalent Settings copies to force retry=0.  Keying
    # by object identity leaked one AsyncOpenAI/httpx client per scenario and
    # exhausted the backend during a long certification run.  Equivalent
    # configurations must share one connection pool and circuit breaker.
    cache_key = _provider_cache_key(settings)
    if cache_key in _PROVIDER_CACHE:
        return _PROVIDER_CACHE[cache_key]
    if settings.llm_provider == "zai":
        provider: LLMProvider = ZAIProvider(settings)
    elif settings.llm_provider in {"gnivc", "gnivc_qwen", "qwen"}:
        provider = GnivcQwenProvider(settings)
    else:
        provider = DeterministicProvider()
    _PROVIDER_CACHE[cache_key] = provider
    return provider


def configured_narrative_provider(settings: Settings) -> LLMProvider:
    cache_key = _provider_cache_key(settings, narrative=True)
    if cache_key in _NARRATIVE_PROVIDER_CACHE:
        return _NARRATIVE_PROVIDER_CACHE[cache_key]
    if settings.llm_provider == "zai":
        provider: LLMProvider = OpenAICompatibleProvider(
            name="zai",
            base_url=settings.zai_base_url,
            api_key=settings.zai_api_key,
            model=settings.llm_narrative_model,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
            data_policy=settings.llm_data_policy,
        )
    elif settings.llm_provider in {"gnivc", "gnivc_qwen", "qwen"}:
        provider = GnivcQwenProvider(
            settings,
            model=settings.gnivc_narrative_model,
        )
    else:
        provider = DeterministicProvider()
    _NARRATIVE_PROVIDER_CACHE[cache_key] = provider
    return provider


def configured_report_synthesis_provider(settings: Settings) -> LLMProvider:
    if settings.report_synthesis_provider == "configured":
        return configured_narrative_provider(settings)
    if settings.report_synthesis_provider == "deterministic":
        return DeterministicProvider()
    cache_key = (
        "gnivc_gigachat",
        settings.gnivc_base_url,
        _secret_fingerprint(settings.gnivc_api_key),
        settings.report_synthesis_model,
        settings.llm_timeout_seconds,
        settings.llm_max_retries,
        settings.llm_data_policy,
    )
    if cache_key not in _REPORT_SYNTHESIS_PROVIDER_CACHE:
        _REPORT_SYNTHESIS_PROVIDER_CACHE[cache_key] = (
            GnivcGigaChatProvider(settings)
        )
    return _REPORT_SYNTHESIS_PROVIDER_CACHE[cache_key]


async def safe_status(settings: Settings) -> dict:
    provider = configured_provider(settings)
    if isinstance(provider, OpenAICompatibleProvider):
        if not provider._configured:
            health = await provider.healthcheck()
        else:
            # Status endpoint does not perform a paid/network call. The explicit
            # /api/ai/test endpoint owns the synthetic healthcheck.
            health = {
                "status": "configured",
                "provider": provider.name,
                "model": provider.model,
                "fallback": False,
                "circuit_open": provider._breaker.is_open,
            }
    else:
        health = await provider.healthcheck()
    capabilities = provider.capabilities()
    report_provider = configured_report_synthesis_provider(settings)
    report_configured = (
        report_provider.name == "deterministic"
        or bool(getattr(report_provider, "_configured", True))
    )
    return {
        **health,
        "narrative_model": report_provider.model,
        "data_policy": capabilities.data_policy,
        "external_data_allowed": settings.allow_external_llm_data,
        "report_synthesis_provider": report_provider.name,
        "report_synthesis_model": report_provider.model,
        "report_synthesis_status": (
            "configured" if report_configured else "not_configured"
        ),
        "narrative_improvement_enabled": (
            report_provider.name != "deterministic"
            and report_configured
            and settings.llm_data_policy == "aggregates"
            and settings.allow_external_llm_data
        ),
        "last_call_used_fallback": bool(
            getattr(provider, "last_fallback", False)
        ),
    }
