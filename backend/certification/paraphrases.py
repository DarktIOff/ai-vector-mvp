from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from backend.app.config import Settings
from backend.app.llm import configured_provider

from .case_models import AnalyticEvaluationCase


PARAPHRASE_PROMPT_VERSION = "semantic-paraphrases-v1"


def _parse_json(raw: str) -> dict[str, Any]:
    candidate = re.sub(
        r"<think>.*?</think>",
        "",
        raw.strip(),
        flags=re.IGNORECASE | re.DOTALL,
    )
    candidate = re.sub(r"```(?:json)?", "", candidate, flags=re.IGNORECASE)
    decoder = json.JSONDecoder()
    objects: list[tuple[int, dict[str, Any]]] = []
    for match in re.finditer(r"\{", candidate):
        try:
            parsed, length = decoder.raw_decode(candidate[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            objects.append((length, parsed))
    if not objects:
        raise ValueError("Генератор перефразировок не вернул JSON-объект")
    return max(objects, key=lambda item: item[0])[1]


def _stable_generated_id(base_id: str, questions: list[str]) -> str:
    payload = "\x1f".join(
        [PARAPHRASE_PROMPT_VERSION, base_id, *questions]
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"generated-paraphrase-{digest}"


def _messages(
    case: AnalyticEvaluationCase,
    variants: int,
) -> list[dict[str, str]]:
    questions = [turn.question for turn in case.turns]
    expected = [
        {
            "metric_codes": turn.metric_codes,
            "territories": turn.territories,
            "row_codes": turn.row_codes,
            "enterprise_groups": turn.enterprise_groups,
            "periods": [item.end for item in turn.periods],
            "required_analyses": turn.required_analyses,
            "ranking_direction": turn.ranking_direction,
            "ranking_criterion": turn.ranking_criterion,
        }
        for turn in case.turns
    ]
    system = (
        "Ты создаёшь тестовые русскоязычные перефразировки аналитических "
        "вопросов. Сохрани абсолютно тот же смысл, сущности, число периодов, "
        "тип периода, операции и связи между ходами. Не отвечай на вопросы. "
        "Варьируй морфологию, порядок слов, разговорный стиль, сокращения и "
        "умеренные опечатки. Не копируй исходные вопросы дословно. Не "
        "добавляй и не удаляй ограничения. Верни "
        "строго JSON вида {\"variants\":[{\"questions\":[...]}]}. Число "
        "questions в каждом варианте обязано совпадать с исходным диалогом."
    )
    payload = {
        "variants_required": variants,
        "source_questions": questions,
        "semantic_contract": expected,
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


async def generate_paraphrase_cases(
    cases: list[AnalyticEvaluationCase],
    settings: Settings,
    *,
    variants_per_case: int = 5,
) -> list[AnalyticEvaluationCase]:
    if variants_per_case < 2:
        raise ValueError("Для metamorphic-группы нужны минимум 2 варианта")
    provider = configured_provider(settings)
    if provider.name != "gnivc_qwen" or provider.model != "qwen3.6-35b":
        raise RuntimeError(
            "Генерация требует gnivc_qwen/qwen3.6-35b без fallback"
        )
    generated: list[AnalyticEvaluationCase] = []
    for case in cases:
        raw = await provider.complete(
            _messages(case, variants_per_case),
            response_format={"type": "json_object"},
            temperature=0.7,
            max_tokens=2400,
            extra_body={"thinking": {"type": "disabled"}},
            allow_fallback=False,
        )
        payload = _parse_json(raw)
        variants = payload.get("variants")
        if not isinstance(variants, list):
            raise ValueError(f"Нет variants для {case.case_id}")
        accepted: list[list[str]] = []
        seen = {tuple(turn.question for turn in case.turns)}
        for item in variants:
            questions: Any = None
            if isinstance(item, dict):
                questions = item.get("questions")
                if questions is None:
                    single = next(
                        (
                            item[key]
                            for key in (
                                "question",
                                "paraphrase",
                                "variant",
                                "text",
                            )
                            if isinstance(item.get(key), str)
                        ),
                        None,
                    )
                    if single is not None:
                        questions = [single]
            elif isinstance(item, str) and len(case.turns) == 1:
                questions = [item]
            elif isinstance(item, list):
                questions = item
            if not isinstance(questions, list):
                continue
            normalized = [str(question).strip() for question in questions]
            key = tuple(normalized)
            if (
                len(normalized) != len(case.turns)
                or not all(normalized)
                or key in seen
            ):
                continue
            seen.add(key)
            accepted.append(normalized)
            if len(accepted) == variants_per_case:
                break
        if len(accepted) < variants_per_case:
            raise ValueError(
                f"Для {case.case_id} получено только {len(accepted)} "
                f"уникальных вариантов из {variants_per_case}"
            )
        group = f"generated-{case.case_id}"[:96]
        for index, questions in enumerate(accepted):
            raw_case = case.model_dump(mode="json")
            raw_case.update(
                {
                    "case_id": _stable_generated_id(case.case_id, questions),
                    "title": f"Перефразировка {case.case_id} #{index + 1}",
                    "tier": "full",
                    "category": "generated_paraphrase",
                    "origin": "generated",
                    "tags": list(
                        dict.fromkeys(
                            [
                                *case.tags,
                                "generated",
                                "paraphrase",
                                "metamorphic",
                            ]
                        )
                    ),
                    "metamorphic_group": group,
                    "metamorphic_relation": "paraphrase",
                    "metadata": {
                        **case.metadata,
                        "source_case_id": case.case_id,
                        "generator_provider": provider.name,
                        "generator_model": provider.model,
                        "prompt_version": PARAPHRASE_PROMPT_VERSION,
                        "split": (
                            "held_out"
                            if index == variants_per_case - 1
                            else "regression"
                        ),
                    },
                }
            )
            for turn, question in zip(
                raw_case["turns"], questions, strict=True
            ):
                turn["question"] = question
            generated.append(AnalyticEvaluationCase.model_validate(raw_case))
    return generated


def write_generated_paraphrases(
    path: Path, cases: list[AnalyticEvaluationCase]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            [item.model_dump(mode="json") for item in cases],
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
