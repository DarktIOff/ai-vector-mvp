from __future__ import annotations

from dataclasses import asdict, dataclass
from statistics import mean
from typing import Any


QUALITY_THRESHOLD = 0.90


@dataclass(frozen=True)
class EvaluationCase:
    case_id: str
    question: str
    required_analyses: frozenset[str]
    kind: str = "multistep"


@dataclass(frozen=True)
class ModelEvaluation:
    provider: str
    model: str
    cases_total: int
    cases_covered: int
    analytical_coverage: float
    evidence_reference_rate: float
    numbers_verified_rate: float
    hypothesis_label_rate: float
    mean_duration_ms: float
    passed: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_model_results(
    provider: str,
    model: str,
    cases: list[EvaluationCase],
    results: list[dict],
) -> ModelEvaluation:
    if len(cases) != len(results):
        raise ValueError("Число результатов не совпадает с корпусом")
    covered = 0
    claims_total = 0
    claims_with_known_evidence = 0
    hypotheses_total = 0
    hypotheses_labeled = 0
    verified_results = 0
    durations: list[float] = []
    for case, result in zip(cases, results, strict=True):
        research_plan = result.get("research_plan") or {}
        actual = set(research_plan.get("expected_analyses") or [])
        tool_calls = int((result.get("execution") or {}).get("tool_calls", 0))
        no_silent_reduction = (
            len(case.required_analyses) <= 1 or tool_calls > 1
        )
        if case.required_analyses <= actual and no_silent_reduction:
            covered += 1
        known_evidence = {
            str(item.get("evidence_id"))
            for item in result.get("evidence", [])
        }
        for claim in result.get("claims", []):
            claims_total += 1
            references = set(claim.get("evidence_ids") or [])
            if references and references <= known_evidence:
                claims_with_known_evidence += 1
        for hypothesis in result.get("hypotheses", []):
            hypotheses_total += 1
            if (
                hypothesis.get("label") == "Гипотеза"
                and hypothesis.get("status") == "possible_interpretation"
            ):
                hypotheses_labeled += 1
        if result.get("numbers_verified") is True:
            verified_results += 1
        duration = (result.get("execution") or {}).get("duration_ms")
        if isinstance(duration, (int, float)):
            durations.append(float(duration))
    total = len(cases)
    analytical_coverage = covered / total if total else 0.0
    evidence_rate = (
        claims_with_known_evidence / claims_total
        if claims_total
        else 1.0
    )
    hypothesis_rate = (
        hypotheses_labeled / hypotheses_total
        if hypotheses_total
        else 1.0
    )
    verified_rate = verified_results / total if total else 0.0
    passed = (
        analytical_coverage >= QUALITY_THRESHOLD
        and evidence_rate == 1.0
        and verified_rate == 1.0
        and hypothesis_rate == 1.0
    )
    return ModelEvaluation(
        provider=provider,
        model=model,
        cases_total=total,
        cases_covered=covered,
        analytical_coverage=analytical_coverage,
        evidence_reference_rate=evidence_rate,
        numbers_verified_rate=verified_rate,
        hypothesis_label_rate=hypothesis_rate,
        mean_duration_ms=mean(durations) if durations else float("inf"),
        passed=passed,
    )


def select_model(
    evaluations: list[ModelEvaluation],
) -> ModelEvaluation:
    eligible = [item for item in evaluations if item.passed]
    if not eligible:
        raise ValueError(
            "Ни одна модель не прошла порог 90% и evidence-проверки"
        )
    # Quality dominates. At equal quality the faster model wins.
    return sorted(
        eligible,
        key=lambda item: (
            -item.analytical_coverage,
            item.mean_duration_ms,
        ),
    )[0]
