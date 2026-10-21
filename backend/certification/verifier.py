from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from .case_models import AnalyticEvaluationCase, DialogueTurn, ExpectedStatus
from .manifest import normalize
from .oracle import OracleResult, OracleRow


NUMERIC_FIELDS = {
    "value",
    "current",
    "previous",
    "difference",
    "growth_rate",
    "share",
    "score",
    "minimum",
    "q1",
    "median",
    "mean",
    "q3",
    "maximum",
    "count",
    "regions_count",
}
CAUSAL_RE = re.compile(
    r"\b(?:вызвал[аои]?|обусловил[аои]?|доказал[аои]?|является причиной|"
    r"привел[аои]? к|гарантирует|точно приведет)\b",
    re.IGNORECASE,
)
REFUSAL_MARKERS = (
    "нет данных",
    "данные недоступны",
    "не могу",
    "не раскры",
    "не предостав",
    "уточните",
    "не найден",
    "отсутствует",
)


@dataclass(frozen=True)
class VerificationIssue:
    code: str
    message: str
    expected: Any = None
    actual: Any = None
    location: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CaseVerdict:
    case_id: str
    passed: bool
    issues: list[VerificationIssue] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)
    numeric_differences: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "passed": self.passed,
            "issues": [item.to_dict() for item in self.issues],
            "checks": self.checks,
            "numeric_differences": self.numeric_differences,
        }


def _as_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).replace("\xa0", "").replace(" ", "").replace(",", ".")
    text = text.rstrip("%")
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _normalized_set(values: Iterable[Any]) -> set[str]:
    return {normalize(str(item)) for item in values if item is not None}


def _territory_identity(value: Any) -> str:
    normalized = normalize(str(value))
    normalized = re.sub(r"^(?:город\s+|г[.]?\s*)", "", normalized)
    normalized = re.sub(r"\s*\([^()]+\)\s*$", "", normalized)
    normalized = re.sub(r"\s*-\s*кузбасс\s*$", "", normalized)
    normalized = re.sub(
        r"\bавтономный\s+округ\b", "ао", normalized
    )
    return re.sub(r"[^0-9a-zа-я]+", "", normalized)


def _result_payload(response: dict[str, Any]) -> dict[str, Any]:
    result = response.get("result")
    return result if isinstance(result, dict) else {}


def _actual_status(response: dict[str, Any]) -> str:
    status = str(response.get("status") or "")
    if status == "needs_clarification":
        return ExpectedStatus.CLARIFICATION.value
    return status


def _evidence_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    operations = {
        str(item.get("id")): item
        for item in (result.get("research_plan") or {}).get("operations", [])
        if isinstance(item, dict)
    }
    for evidence in result.get("evidence", []):
        operation = operations.get(str(evidence.get("operation_id")), {})
        arguments = operation.get("arguments") or {}
        default_metric = (arguments.get("metric_codes") or [None])[0]
        default_group = (arguments.get("enterprise_groups") or [None])[0]
        for row in evidence.get("rows", []):
            if not isinstance(row, dict):
                continue
            enriched = dict(row)
            enriched.setdefault("metric_code", default_metric)
            enriched.setdefault("enterprise_group", default_group)
            enriched["evidence_id"] = evidence.get("evidence_id")
            enriched["tool"] = evidence.get("tool")
            rows.append(enriched)
    return rows


def _row_matches(expected: OracleRow, actual: dict[str, Any]) -> bool:
    if actual.get("metric_code") and actual.get("metric_code") != expected.metric_code:
        return False
    if actual.get("row_code") is not None:
        try:
            if int(actual["row_code"]) != expected.row_code:
                return False
        except (TypeError, ValueError):
            return False
    actual_period = actual.get("period") or actual.get("period_end")
    if actual_period and str(actual_period) != expected.period:
        return False
    if actual.get("territory") and _territory_identity(
        actual["territory"]
    ) != _territory_identity(expected.territory):
        return False
    if (
        actual.get("enterprise_group")
        and actual.get("enterprise_group") != expected.enterprise_group
    ):
        return False
    # ``entity`` in a series is a human-readable composite label. The typed
    # metric/row/territory/group/period keys above are the canonical identity
    # and avoid rejecting the same fact because of presentation wording.
    return True


def _numeric_candidates(field: str) -> tuple[str, ...]:
    if field == "value":
        return ("value", "current")
    return (field,)


def _compare_oracle_rows(
    oracle: OracleResult,
    actual_rows: list[dict[str, Any]],
) -> tuple[list[VerificationIssue], list[dict[str, Any]]]:
    issues: list[VerificationIssue] = []
    differences: list[dict[str, Any]] = []
    # Distribution cases use the full oracle population to calculate summary
    # statistics, while the bounded live evidence contains the summary row.
    # Requiring hundreds of population rows in a 50-row evidence envelope
    # would contradict the production limit.
    expected_rows = [] if oracle.distribution else oracle.rows
    for expected in expected_rows:
        candidates = [item for item in actual_rows if _row_matches(expected, item)]
        if not candidates:
            issues.append(
                VerificationIssue(
                    "missing_evidence_row",
                    "В evidence отсутствует строка oracle",
                    expected=expected.to_dict(),
                    location="evidence",
                )
            )
            continue
        for field in ("value", "previous", "difference", "growth_rate"):
            expected_value = getattr(expected, field)
            if expected_value is None:
                continue
            matched = False
            observed: list[str] = []
            for candidate in candidates:
                for actual_field in _numeric_candidates(field):
                    if candidate.get(actual_field) is None:
                        continue
                    actual_value = _as_decimal(candidate[actual_field])
                    if actual_value is not None:
                        observed.append(str(actual_value))
                    if actual_value == expected_value:
                        matched = True
            if not matched:
                difference = {
                    "row": expected.to_dict(),
                    "field": field,
                    "expected": str(expected_value),
                    "actual": observed,
                }
                differences.append(difference)
                issues.append(
                    VerificationIssue(
                        "numeric_mismatch",
                        f"Число {field} не совпало с oracle",
                        expected=str(expected_value),
                        actual=observed,
                        location="evidence",
                    )
                )
    for field, expected in oracle.distribution.items():
        if not isinstance(expected, (Decimal, int)):
            continue
        values = [
            _as_decimal(row.get(field))
            for row in actual_rows
            if row.get(field) is not None
        ]
        if _as_decimal(expected) not in values:
            issues.append(
                VerificationIssue(
                    "distribution_mismatch",
                    f"Статистика {field} не совпала с oracle",
                    expected=str(expected),
                    actual=[str(item) for item in values if item is not None],
                    location="evidence.distribution",
                )
            )
    return issues, differences


def _plan_checks(
    turn: DialogueTurn, result: dict[str, Any]
) -> list[VerificationIssue]:
    issues: list[VerificationIssue] = []
    query_plan = result.get("query_plan") or {}
    expected_fields = {
        "metric_codes": turn.metric_codes,
        "territories": turn.territories,
        "row_codes": turn.row_codes,
        "enterprise_groups": turn.enterprise_groups,
        "periods": [item.end for item in turn.periods],
    }
    for field, expected in expected_fields.items():
        if not expected:
            continue
        actual = query_plan.get(field) or []
        actual_values = [str(item) for item in actual]
        expected_values = [str(item) for item in expected]
        if field == "territories":
            matches = {
                _territory_identity(item) for item in actual_values
            } == {
                _territory_identity(item) for item in expected_values
            }
        else:
            matches = set(actual_values) == set(expected_values)
        if not matches:
            issues.append(
                VerificationIssue(
                    "query_plan_filter_mismatch",
                    f"QueryPlan изменил фильтр {field}",
                    expected=expected_values,
                    actual=actual_values,
                    location=f"query_plan.{field}",
                )
            )
    research = result.get("research_plan") or {}
    actual_analyses = set(research.get("expected_analyses") or [])
    missing_analyses = set(turn.required_analyses) - actual_analyses
    if missing_analyses:
        issues.append(
            VerificationIssue(
                "missing_analysis",
                "ResearchPlan не содержит обязательные виды анализа",
                expected=sorted(turn.required_analyses),
                actual=sorted(actual_analyses),
                location="research_plan.expected_analyses",
            )
        )
    actual_tools = {
        str(item.get("tool"))
        for item in research.get("operations", [])
        if isinstance(item, dict)
    }
    missing_tools = set(turn.required_tools) - actual_tools
    if missing_tools:
        issues.append(
            VerificationIssue(
                "missing_tool",
                "ResearchPlan не содержит обязательные инструменты",
                expected=sorted(turn.required_tools),
                actual=sorted(actual_tools),
                location="research_plan.operations",
            )
        )
    return issues


def _provider_checks(result: dict[str, Any]) -> list[VerificationIssue]:
    execution = result.get("execution") or {}
    issues: list[VerificationIssue] = []
    expected_provider = "gnivc_qwen"
    expected_model = "qwen3.6-35b"
    if execution.get("provider") != expected_provider:
        issues.append(
            VerificationIssue(
                "provider_mismatch",
                "Сценарий выполнен не обязательным провайдером",
                expected=expected_provider,
                actual=execution.get("provider"),
                location="execution.provider",
            )
        )
    if execution.get("model") != expected_model:
        issues.append(
            VerificationIssue(
                "model_mismatch",
                "Фактическая модель не qwen3.6-35b",
                expected=expected_model,
                actual=execution.get("model"),
                location="execution.model",
            )
        )
    if int(execution.get("model_calls") or 0) < 1:
        issues.append(
            VerificationIssue(
                "no_model_calls",
                "Агентный аналитический вопрос выполнен без модели",
                expected=">=1",
                actual=execution.get("model_calls"),
                location="execution.model_calls",
            )
        )
    if execution.get("fallback") is not False or "fallback" in str(
        result.get("planner_mode", "")
    ):
        issues.append(
            VerificationIssue(
                "provider_fallback",
                "Обнаружен fallback",
                expected=False,
                actual={
                    "fallback": execution.get("fallback"),
                    "planner_mode": result.get("planner_mode"),
                },
                location="execution",
            )
        )
    if int(execution.get("retries") or 0) != 0:
        issues.append(
            VerificationIssue(
                "provider_retry",
                "Сценарий не должен повторять обращение к модели",
                expected=0,
                actual=execution.get("retries"),
                location="execution.retries",
            )
        )
    return issues


def _limits_checks(result: dict[str, Any]) -> list[VerificationIssue]:
    execution = result.get("execution") or {}
    issues: list[VerificationIssue] = []
    # Model calls are intentionally not quality-limited: the semantic agent
    # may inspect as much metadata as it needs. The planner has a separate
    # emergency circuit breaker for non-converging loops.
    checks = (("tool_calls", 12),)
    for field, limit in checks:
        value = int(execution.get(field) or 0)
        if value > limit:
            issues.append(
                VerificationIssue(
                    "limit_exceeded",
                    f"Превышен лимит {field}",
                    expected=f"<= {limit}",
                    actual=value,
                    location=f"execution.{field}",
                )
            )
    for evidence in result.get("evidence", []):
        if len(evidence.get("rows") or []) > 50:
            issues.append(
                VerificationIssue(
                    "row_limit_exceeded",
                    "Evidence содержит более 50 строк",
                    expected="<= 50",
                    actual=len(evidence.get("rows") or []),
                    location=f"evidence.{evidence.get('evidence_id')}.rows",
                )
            )
    return issues


def _claim_checks(result: dict[str, Any]) -> list[VerificationIssue]:
    issues: list[VerificationIssue] = []
    evidence_by_id = {
        str(item.get("evidence_id")): item
        for item in result.get("evidence", [])
    }
    for claim in result.get("claims", []):
        ids = claim.get("evidence_ids") or []
        if not ids or any(item not in evidence_by_id for item in ids):
            issues.append(
                VerificationIssue(
                    "invalid_claim_evidence",
                    "Claim не имеет полного evidence",
                    actual=ids,
                    location=f"claims.{claim.get('claim_id')}",
                )
            )
            continue
        allowed_numbers = {
            number
            for evidence_id in ids
            for row in evidence_by_id[evidence_id].get("rows", [])
            for key, value in row.items()
            if key in NUMERIC_FIELDS and (number := _as_decimal(value)) is not None
        }
        for raw in claim.get("numbers") or []:
            number = _as_decimal(raw)
            if number is None or number not in allowed_numbers:
                issues.append(
                    VerificationIssue(
                        "claim_number_without_evidence",
                        "Число claim отсутствует в связанном evidence",
                        expected=sorted(str(item) for item in allowed_numbers),
                        actual=raw,
                        location=f"claims.{claim.get('claim_id')}.numbers",
                    )
                )
        evidence_entities = {
            normalize(str(value))
            for evidence_id in ids
            for row in evidence_by_id[evidence_id].get("rows", [])
            for key in ("entity", "territory", "industry", "region")
            if (value := row.get(key))
        }
        for entity in claim.get("entities") or []:
            if normalize(str(entity)) not in evidence_entities:
                issues.append(
                    VerificationIssue(
                        "claim_entity_without_evidence",
                        "Сущность claim отсутствует в evidence",
                        actual=entity,
                        location=f"claims.{claim.get('claim_id')}.entities",
                    )
                )
    for hypothesis in result.get("hypotheses", []):
        if (
            hypothesis.get("label") != "Гипотеза"
            or hypothesis.get("status") != "possible_interpretation"
            or CAUSAL_RE.search(str(hypothesis.get("text") or ""))
        ):
            issues.append(
                VerificationIssue(
                    "invalid_hypothesis",
                    "Гипотеза не маркирована или утверждает причинность",
                    actual=hypothesis,
                    location=f"hypotheses.{hypothesis.get('hypothesis_id')}",
                )
            )
    return issues


def _presentation_checks(
    turn: DialogueTurn,
    result: dict[str, Any],
    oracle: OracleResult,
) -> list[VerificationIssue]:
    issues: list[VerificationIssue] = []
    presentation = turn.presentation
    charts = result.get("charts") or []
    tables = result.get("tables") or []
    series = result.get("series") or []
    if presentation.chart_required:
        chart_types = {str(item.get("type")) for item in charts}
        visualization_type = str((result.get("visualization") or {}).get("type"))
        if presentation.chart_type not in chart_types | {visualization_type}:
            issues.append(
                VerificationIssue(
                    "missing_chart",
                    "Нет обязательного графика нужного типа",
                    expected=presentation.chart_type,
                    actual=sorted(chart_types | {visualization_type}),
                    location="charts",
                )
            )
        if not series:
            issues.append(
                VerificationIssue(
                    "chart_without_series",
                    "График не содержит рядов",
                    location="series",
                )
            )
    if presentation.table_required and not tables:
        issues.append(
            VerificationIssue(
                "missing_table",
                "Нет обязательной таблицы",
                location="tables",
            )
        )
    if presentation.hypotheses_required and not result.get("hypotheses"):
        issues.append(
            VerificationIssue(
                "missing_hypothesis",
                "Нет обязательной гипотезы",
                location="hypotheses",
            )
        )
    if not presentation.hypotheses_allowed and result.get("hypotheses"):
        issues.append(
            VerificationIssue(
                "unexpected_hypothesis",
                "Для сценария гипотезы запрещены",
                location="hypotheses",
            )
        )
    if presentation.exact_table_order and oracle.rows:
        expected_entities = [normalize(item.entity or "") for item in oracle.rows]
        actual_entities = [
            normalize(str(row.get("entity") or row.get("name") or ""))
            for table in tables
            for row in table.get("rows", [])
            if row.get("entity") or row.get("name")
        ]
        if expected_entities and not all(
            item in actual_entities for item in expected_entities
        ):
            issues.append(
                VerificationIssue(
                    "table_entities_mismatch",
                    "Таблица не содержит все строки рейтинга oracle",
                    expected=expected_entities,
                    actual=actual_entities,
                    location="tables.rows",
                )
            )
    # Every plotted point must occur verbatim in evidence.
    evidence_numbers = {
        (_as_decimal(row.get("value")), str(row.get("period") or ""))
        for row in _evidence_rows(result)
        if row.get("value") is not None
    }
    for item in series:
        for point in item.get("points", []):
            pair = (
                _as_decimal(point.get("value")),
                str(point.get("period_end") or point.get("period") or ""),
            )
            if pair not in evidence_numbers:
                issues.append(
                    VerificationIssue(
                        "chart_evidence_mismatch",
                        "Точка графика отсутствует в evidence",
                        actual={"series": item.get("key"), "point": point},
                        location="series.points",
                    )
                )
    return issues


def verify_case(
    case: AnalyticEvaluationCase,
    response: dict[str, Any],
    oracle: OracleResult,
    *,
    turn_index: int | None = None,
) -> CaseVerdict:
    index = len(case.turns) - 1 if turn_index is None else turn_index
    turn = case.turns[index]
    issues: list[VerificationIssue] = []
    actual_status = _actual_status(response)
    result = _result_payload(response)
    expected_status = turn.expected_status

    # User text is intentionally echoed by the run resource. Disclosure checks
    # inspect generated content only, otherwise an injection containing "SQL"
    # would fail merely because its own question was stored.
    research_plan = dict(result.get("research_plan") or {})
    research_plan.pop("objective", None)
    analytic_evidence = [
        item
        for item in result.get("evidence", [])
        if isinstance(item, dict) and item.get("tool") != "resolve_entities"
    ]
    disclosure_surface = {
        key: result.get(key)
        for key in (
            "answer",
            "claims",
            "hypotheses",
            "warnings",
            "query_plan",
        )
    }
    # ``resolve_entities`` deliberately returns a bounded catalog around the
    # query and can therefore contain the metric being replaced even when no
    # calculation or answer uses it.  Foreign-context checks apply to
    # generated claims, filters and analytical evidence, not to that catalog
    # lookup (or to the echoed user question/objective).
    disclosure_surface["evidence"] = analytic_evidence
    disclosure_surface["research_plan"] = research_plan
    serialized = json.dumps(
        disclosure_surface, ensure_ascii=False, default=str
    )
    for forbidden in turn.forbidden_entities:
        if forbidden.casefold() in serialized.casefold():
            issues.append(
                VerificationIssue(
                    "forbidden_entity",
                    "Ответ раскрыл запрещённую сущность или старый контекст",
                    expected="absent",
                    actual=forbidden,
                    location="response",
                )
            )

    if expected_status is ExpectedStatus.CLARIFICATION:
        if actual_status != ExpectedStatus.CLARIFICATION.value:
            issues.append(
                VerificationIssue(
                    "status_mismatch",
                    "Неоднозначный запрос должен запросить уточнение",
                    expected=expected_status.value,
                    actual=actual_status,
                    location="status",
                )
            )
        return CaseVerdict(case.case_id, not issues, issues, {"status": not issues})

    if expected_status is ExpectedStatus.NO_DATA:
        explicit_refusal = (
            actual_status == ExpectedStatus.NO_DATA.value
            or (
                actual_status == "ready"
                and not result.get("evidence")
                and any(
                    marker
                    in normalize(
                        str(
                            result.get("answer")
                            or response.get("error")
                            or ""
                        )
                    )
                    for marker in REFUSAL_MARKERS
                )
            )
        )
        if actual_status != ExpectedStatus.CLARIFICATION.value and not explicit_refusal:
            issues.append(
                VerificationIssue(
                    "silent_substitution",
                    "Нет явного отказа/уточнения для недоступного запроса",
                    expected="clarification or explicit refusal",
                    actual=actual_status,
                    location="status",
                )
            )
        return CaseVerdict(case.case_id, not issues, issues, {"safe_refusal": not issues})

    if actual_status != "ready" or not result:
        error = response.get("error") or {}
        message = str(error.get("message") or result.get("answer") or "")
        provider_issue = (
            "provider_error"
            if error.get("code") == "provider_unavailable"
            else "provider_fallback"
            if error.get("code") == "provider_fallback"
            or "fallback" in message.casefold()
            else "status_mismatch"
        )
        issues.append(
            VerificationIssue(
                provider_issue,
                "Готовый аналитический сценарий не завершён",
                expected="ready",
                actual=actual_status,
                location="status",
            )
        )
        return CaseVerdict(case.case_id, False, issues, {"status": False})

    plan_issues = _plan_checks(turn, result)
    provider_issues = _provider_checks(result)
    limit_issues = _limits_checks(result)
    claim_issues = _claim_checks(result)
    evidence_issues, differences = _compare_oracle_rows(
        oracle, _evidence_rows(result)
    )
    presentation_issues = _presentation_checks(turn, result, oracle)
    issues.extend(
        [
            *plan_issues,
            *provider_issues,
            *limit_issues,
            *claim_issues,
            *evidence_issues,
            *presentation_issues,
        ]
    )
    checks = {
        "status": True,
        "plans": not plan_issues,
        "provider": not provider_issues,
        "limits": not limit_issues,
        "claims": not claim_issues,
        "oracle": not evidence_issues,
        "presentation": not presentation_issues,
    }
    return CaseVerdict(
        case.case_id,
        not issues,
        issues,
        checks,
        differences,
    )


def verify_metamorphic(records: list[dict[str, Any]]) -> list[VerificationIssue]:
    """Cross-case invariants evaluated only after individual verification."""

    issues: list[VerificationIssue] = []
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in records:
        case = record.get("case") or {}
        group = case.get("metamorphic_group")
        relation = case.get("metamorphic_relation")
        if group and relation:
            groups.setdefault((str(group), str(relation)), []).append(record)
    for (group, relation), items in groups.items():
        ready_items = [
            item
            for item in items
            if (item.get("response") or {}).get("status") == "ready"
            and _result_payload(item.get("response") or {})
        ]
        # A metamorphic relation is meaningful only after every selected
        # member reached a comparable result. Individual failures already
        # explain an incomplete group and must not create secondary noise.
        if len(ready_items) != len(items):
            continue
        items = ready_items
        if relation == "paraphrase" and len(items) >= 2:
            signatures = []
            for item in items:
                result = _result_payload(item.get("response") or {})
                plan = result.get("query_plan") or {}
                research = result.get("research_plan") or {}
                execution = result.get("execution") or {}
                semantic = execution.get("semantic_query") or {}
                signature = {
                    "metrics": plan.get("metric_codes"),
                    "territories": plan.get("territories"),
                    "rows": plan.get("row_codes"),
                    "periods": plan.get("periods"),
                    "period_modes": plan.get("period_modes"),
                    "enterprise_groups": plan.get("enterprise_groups"),
                    "operations": plan.get("operations"),
                    "analysis_dimension": plan.get("analysis_dimension"),
                    "ranking_criteria": plan.get("ranking_criteria"),
                    "ranking_direction": plan.get("ranking_direction"),
                    "analyses": research.get("expected_analyses"),
                    "tools": [
                        operation.get("tool")
                        for operation in research.get("operations", [])
                        if isinstance(operation, dict)
                    ],
                    "semantic_ranking_criterion": semantic.get(
                        "ranking_criterion"
                    ),
                    "constraint_sources": semantic.get(
                        "constraint_sources"
                    ),
                    "numbers": sorted(
                        str(number)
                        for row in _evidence_rows(result)
                        for key, value in row.items()
                        if key in NUMERIC_FIELDS
                        and (number := _as_decimal(value)) is not None
                    ),
                }
                signatures.append(signature)
            if any(item != signatures[0] for item in signatures[1:]):
                issues.append(
                    VerificationIssue(
                        "metamorphic_paraphrase_mismatch",
                        f"Перефразировки группы {group} дали разные планы/числа",
                        actual=signatures,
                    )
                )
        if relation == "combined_receipts":
            for item in items:
                oracle = item.get("oracle") or {}
                values = [
                    _as_decimal(row.get("value"))
                    for row in oracle.get("rows", [])
                ]
                values = [value for value in values if value is not None]
                if not values:
                    continue
                combined_rows = [
                    row
                    for row in oracle.get("rows", [])
                    if row.get("metric_code") == "msp_total"
                    and _as_decimal(row.get("value")) is not None
                ]
                combined = (
                    _as_decimal(combined_rows[0]["value"])
                    if combined_rows
                    else sum(values, Decimal("0"))
                )
                if combined is None:
                    continue
                result = _result_payload(item.get("response") or {})
                actual_numbers = {
                    _as_decimal(value)
                    for claim in result.get("claims", [])
                    for value in claim.get("numbers", [])
                }
                if combined not in actual_numbers:
                    issues.append(
                        VerificationIssue(
                            "metamorphic_combined_receipts_mismatch",
                            "Совокупные поступления не равны бюджету плюс взносы",
                            expected=str(combined),
                            actual=sorted(
                                str(value) for value in actual_numbers if value is not None
                            ),
                        )
                    )
        if relation == "ranking_consistency" and len(items) >= 3:
            top_entities: set[str] = set()
            bottom_entities: set[str] = set()
            distribution_entities: set[str] = set()
            for item in items:
                turns = (item.get("case") or {}).get("turns") or []
                turn = turns[-1] if turns else {}
                entities = {
                    normalize(str(row.get("entity") or ""))
                    for row in (item.get("oracle") or {}).get("rows", [])
                    if row.get("entity")
                }
                direction = turn.get("ranking_direction")
                if direction == "top":
                    top_entities |= entities
                elif direction == "bottom":
                    bottom_entities |= entities
                elif (item.get("case") or {}).get("category") == "distribution":
                    distribution_entities |= entities
            if (
                top_entities & bottom_entities
                or not top_entities <= distribution_entities
                or not bottom_entities <= distribution_entities
            ):
                issues.append(
                    VerificationIssue(
                        "metamorphic_ranking_mismatch",
                        f"Top/bottom группы {group} не согласованы с распределением",
                        expected={
                            "top_and_bottom_disjoint": True,
                            "contained_in_distribution": True,
                        },
                        actual={
                            "top": sorted(top_entities),
                            "bottom": sorted(bottom_entities),
                            "distribution_count": len(distribution_entities),
                        },
                    )
                )
    return issues
