from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

from backend.app.analytical_agent import _parse_json_object
from backend.app.config import Settings, get_settings
from backend.app.llm import LLMProvider, configured_provider


EXPECTED_PROVIDER = "gnivc_qwen"
EXPECTED_MODEL = "qwen3.6-35b"


async def run_network_check(
    settings: Settings | None = None,
    *,
    provider: LLMProvider | None = None,
    concurrency: int = 4,
) -> dict[str, Any]:
    """Probe Qwen once and under production certification concurrency.

    Every request has provider retries and fallback disabled. Responses are
    validated as JSON and represented only by a hash; model text and secrets
    never enter the diagnostic artifact.
    """

    base = settings or get_settings()
    strict = base.model_copy(update={"llm_max_retries": 0})
    active = provider or configured_provider(strict)
    configured_correctly = (
        active.name == EXPECTED_PROVIDER and active.model == EXPECTED_MODEL
    )
    if not configured_correctly:
        return {
            "passed": False,
            "provider": active.name,
            "model": active.model,
            "expected_provider": EXPECTED_PROVIDER,
            "expected_model": EXPECTED_MODEL,
            "retries": 0,
            "fallback": False,
            "health": {"status": "configuration_mismatch"},
            "parallel": [],
        }

    health_started = time.perf_counter()
    health = await active.healthcheck()
    health["duration_ms"] = int(
        (time.perf_counter() - health_started) * 1000
    )
    if health.get("status") != "available" or health.get("fallback") is not False:
        return {
            "passed": False,
            "provider": active.name,
            "model": active.model,
            "retries": 0,
            "fallback": False,
            "health": health,
            "parallel": [],
        }

    async def probe(index: int) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            raw = await active.complete(
                [
                    {
                        "role": "system",
                        "content": (
                            "Верни только JSON без пояснений. "
                            'Формат: {"analyses":["comparison"],"probe":N}.'
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"N={index}",
                    },
                ],
                temperature=0,
                max_tokens=128,
                allow_fallback=False,
            )
            parsed = _parse_json_object(raw)
            valid = (
                parsed.get("analyses") == ["comparison"]
                and int(parsed.get("probe")) == index
            )
            return {
                "probe": index,
                "status": "success" if valid else "invalid_json_contract",
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "response_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                "json_keys": sorted(parsed),
            }
        except Exception as exc:
            return {
                "probe": index,
                "status": "error",
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "error_type": type(exc).__name__,
                "error_code": getattr(exc, "code", None),
                "message": str(exc),
            }

    parallel = await asyncio.gather(
        *(probe(index) for index in range(1, concurrency + 1))
    )
    passed = all(item["status"] == "success" for item in parallel)
    return {
        "passed": passed,
        "provider": active.name,
        "model": active.model,
        "retries": 0,
        "fallback": False,
        "health": health,
        "parallel": parallel,
    }


def render_network_check(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
