from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .analytical_tools import (
    MAX_MODEL_CONTEXT_ROWS,
    TOOL_REGISTRY,
    execute_tool,
)
from .catalog import resolve_metric_code
from .catalog_service import _regional_periods
from .config import Settings
from .llm import (
    DeterministicProvider,
    LLMProviderError,
    configured_provider,
)
from .models import Conversation, RowDefinition
from .planner import (
    PlanningUnavailableError,
    _enrich_explicit_territories,
    plan_question,
)
from .query_service import (
    _explicit_cumulative_month,
    available_periods,
    build_query_plan,
    extract_years,
)
from .schemas import (
    AnalysisHypothesis,
    AnalysisResult,
    EvidenceClaim,
    EvidenceItem,
    QueryPlan,
    ResearchOperation,
    ResearchPlan,
    SemanticQuery,
)
from .workspace_service import (
    _planner_context,
    format_report_text_numbers,
)
from .workbooks import normalize_text


MAX_TOOL_CALLS = 12
MAX_PARALLEL_TOOLS = 4

ProgressCallback = Callable[
    [str, int, dict[str, Any] | None], Awaitable[None]
]


class ResearchClarification(ValueError):
    def __init__(self, question: str, options: list[str] | None = None):
        super().__init__(question)
        self.question = question
        self.options = options or []


class ResearchNoData(ValueError):
    pass


class AgentUnavailableError(RuntimeError):
    pass


@dataclass
class PlannedResearch:
    research_plan: ResearchPlan
    base_plan: QueryPlan
    planner_mode: str
    warnings: list[str]
    model_calls: int
    semantic_query: SemanticQuery | None = None
    shadow_diff: dict[str, Any] | None = None


@dataclass
class AgentExecution:
    result: dict
    model_calls: int
    tool_calls: int
    duration_ms: int


def _has_broad_analysis_request(question: str) -> bool:
    normalized = normalize_text(question)
    return any(
        token in normalized
        for token in (
            "динами",
            "тренд",
            "трэнд",
            "отрасл",
            "оквэд",
            "регион",
            "ригион",
            "субъект",
            "субьект",
            "аномал",
            "анамал",
            "выбива",
            "выброс",
            "экстрем",
            "распредел",
            "расприд",
            "медиан",
            "структур",
            "сравн",
        )
    )


def _context_base_plan(conversation: Conversation) -> QueryPlan | None:
    for turn in reversed(conversation.turns):
        if turn.status != "ready":
            continue
        result = turn.result_json or {}
        raw = result.get("query_plan")
        if not isinstance(raw, dict):
            raw = turn.plan_json
        if not isinstance(raw, dict) or "metric_codes" not in raw:
            continue
        try:
            plan = QueryPlan.model_validate(raw)
        except ValidationError:
            continue
        selected_codes: list[int] = [
            int(item["row_code"])
            for item in (
                (result.get("context_update") or {}).get(
                    "selected_industries"
                )
                or []
            )
            if isinstance(item, dict)
            and isinstance(item.get("row_code"), int)
            and int(item["row_code"]) != 1010
        ]
        if not selected_codes:
            for section in result.get("analysis_sections", []):
                if str(section.get("kind", "")).endswith(
                    ("worst", "low_base")
                ):
                    continue
                for row in section.get("rows", []):
                    row_code = row.get("row_code")
                    if isinstance(row_code, int) and row_code != 1010:
                        selected_codes.append(row_code)
        if selected_codes:
            plan.row_codes = list(dict.fromkeys(selected_codes))[:6]
        return plan
    return None


def _apply_return_to_country_context(
    conversation: Conversation,
    question: str,
    plan: QueryPlan,
) -> QueryPlan:
    """Handle an explicit return to the national scope in a dialogue."""
    normalized = normalize_text(question)
    returns_to_country = bool(
        re.search(
            r"\b(?:верн\w*|возврат\w*|снова|обратно)\b.*"
            r"\b(?:рф|росси\w*|российск\w*\s+федерац\w*)\b",
            normalized,
        )
    )
    if not returns_to_country:
        return plan
    plan.territories = ["Российская Федерация"]
    plan.include_top_territory = False
    plan.include_bottom_territory = False
    if not any(token in normalized for token in ("динами", "тренд", "трэнд")):
        return plan
    # A ranking or drill-down follow-up usually narrows the previous plan to
    # its latest period. Returning to the national trend must recover the last
    # comparable multi-period slice from the dialogue instead of inventing a
    # new range or silently keeping one point.
    for turn in reversed(conversation.turns):
        if turn.status != "ready":
            continue
        raw = (turn.result_json or {}).get("query_plan") or turn.plan_json
        if not isinstance(raw, dict):
            continue
        try:
            previous = QueryPlan.model_validate(raw)
        except ValidationError:
            continue
        if len(previous.periods) < 2:
            continue
        plan.periods = list(previous.periods)
        plan.period_modes = list(previous.period_modes)
        plan.period_labels = list(previous.period_labels)
        plan.intent = "trend"
        plan.operations = ["value", "difference", "growth_rate", "sources"]
        break
    return plan


def _refers_to_selected_industries(normalized_question: str) -> bool:
    return any(
        phrase in normalized_question
        for phrase in (
            "этим отрасл",
            "эти отрасл",
            "тем же отрасл",
            "оставь эти отрасл",
            "среди них",
        )
    )


def _broad_fallback_plan(
    session: Session,
    conversation: Conversation,
    question: str,
) -> QueryPlan | None:
    periods = available_periods(session)
    try:
        plan = build_query_plan(question, periods)
    except ValueError:
        plan = _context_base_plan(conversation)
    if plan is None:
        return None
    context_plan = _context_base_plan(conversation)
    normalized = normalize_text(question)
    if (
        context_plan is not None
        and _refers_to_selected_industries(normalized)
    ):
        plan.row_codes = list(context_plan.row_codes)
    return plan


_GENERIC_INDUSTRY_TOKENS = {
    "всего",
    "деятельность",
    "производство",
    "работы",
    "работа",
    "услуги",
    "услуга",
    "области",
    "прочие",
    "прочая",
    "прочих",
}
_NON_ENTITY_TOKEN_PREFIXES = (
    "последн",
    "прошедш",
    "поступлен",
    "ситуац",
    "регион",
    "территор",
    "предприят",
    "динамик",
    "значен",
    "показател",
    "росси",
    "федерац",
    "сравнен",
    "сравнит",
    "период",
)


def _is_non_entity_token(token: str) -> bool:
    return token in _GENERIC_INDUSTRY_TOKENS or token.startswith(
        _NON_ENTITY_TOKEN_PREFIXES
    )


def _industry_name_tokens(value: str) -> list[str]:
    label = re.split(r"[\n(]", normalize_text(value), maxsplit=1)[0]
    return [
        token
        for token in re.findall(r"[а-яa-z]+", label)
        if len(token) >= 5 and not _is_non_entity_token(token)
    ]


def _inflected_token_match(left: str, right: str) -> bool:
    common = 0
    for left_char, right_char in zip(left, right):
        if left_char != right_char:
            break
        common += 1
    shortest = min(len(left), len(right))
    required = shortest - 1 if shortest <= 6 else max(7, shortest - 2)
    return common >= required


def _resolve_named_industry(
    session: Session,
    question: str,
) -> RowDefinition | None:
    normalized = normalize_text(question)
    explicit_industry_dimension = bool(
        re.search(r"\b(?:отрасл\w*|оквэд|вид\w*\s+деятельност\w*)\b", normalized)
    )
    if (
        re.search(r"\bпоказател\w*\s*[«\"]", normalized)
        and not explicit_industry_dimension
    ):
        # The quoted phrase is a metric catalog reference, not an industry.
        # Tax names often contain words also present in activity rows
        # ("организаций", "алкогольной продукции", "нефтепродукты").
        return None
    if (
        not explicit_industry_dimension
        and resolve_metric_code(question) is not None
    ):
        # An exact metric such as ``Налог на прибыль организаций`` must not be
        # reinterpreted as the closest activity row merely because their
        # wording overlaps. An explicit industry/OKVED marker remains
        # authoritative for genuine industry questions.
        return None
    rows = session.scalars(
        select(RowDefinition).order_by(RowDefinition.base_row_code)
    ).all()
    # Catalog-generated and user-entered references are commonly quoted.
    # Resolve an exact canonical name, OKVED label or registered alias before
    # fuzzy token scoring. This is important for compact codes such as
    # ``A 01.1``, which intentionally contain no long linguistic tokens.
    quoted = [
        normalize_text(value)
        for value in re.findall(r"[«\"]([^»\"]+)[»\"]", question)
        if normalize_text(value)
    ]
    for reference in quoted:
        exact = [
            row
            for row in rows
            if reference
            in {
                normalize_text(value)
                for value in (
                    row.name_ru,
                    row.okved_text,
                    *(row.aliases_json or []),
                )
                if value
            }
        ]
        if len(exact) == 1:
            return exact[0]
    if re.search(
        r"\b(?:(?:среди|между)\s+отрасл\w*|по\s+отраслям|"
        r"(?:все|какие|какая|лучшие|худшие)\s+отрасл\w*)\b",
        normalized,
    ):
        # This names the industry dimension, not one concrete catalog row.
        return None
    names_insurance_industry = any(
        re.search(pattern, normalized)
        for pattern in (
            r"\bстрахов\w*\s+отрасл",
            r"\bотрасл\w*\s+страхов",
            r"\bстрахов\w*\s+деятельност",
            r"\bдеятельност\w*\s+(?:финансов\w*\s+и\s+)?страхов",
            r"\bфинансов\w*\s+и\s+страхов",
        )
    )
    metric_token_prefixes = (
        "страхов",
        "взнос",
        "налог",
        "бюджет",
        "платеж",
        "платёж",
    )
    question_tokens = [
        token
        for token in re.findall(r"[а-яa-z]+", normalized)
        if len(token) >= 5 and not _is_non_entity_token(token)
        and (
            not token.startswith(metric_token_prefixes)
            or (
                names_insurance_industry
                and token.startswith("страхов")
            )
        )
    ]
    if not question_tokens:
        return None
    ranked: list[tuple[float, int, int, int, RowDefinition]] = []
    for row in rows:
        names = [row.name_ru, *(row.aliases_json or [])]
        best_tokens: list[str] = []
        best_matches = 0
        for name in names:
            candidate_tokens = _industry_name_tokens(str(name))
            matches = sum(
                any(
                    _inflected_token_match(candidate, query)
                    for query in question_tokens
                )
                for candidate in candidate_tokens
            )
            if matches > best_matches:
                best_matches = matches
                best_tokens = candidate_tokens
        if not best_matches or not best_tokens:
            continue
        coverage = best_matches / len(best_tokens)
        ranked.append(
            (
                coverage,
                best_matches,
                -len(best_tokens),
                -len(row.name_ru),
                row,
            )
        )
    if not ranked:
        return None
    winner = max(ranked, key=lambda item: item[:-1])
    coverage, matches = winner[:2]
    # One incidental word from a long catalog label (for example
    # ``предприятия`` in an enterprise-group question) is not an industry
    # reference. A one-token industry such as ``строительство`` still has
    # complete coverage and remains resolvable.
    if coverage < 0.5 and matches < 2:
        return None
    return winner[-1]


def _apply_explicit_industry_scope(
    session: Session,
    question: str,
    plan: QueryPlan,
) -> tuple[QueryPlan, bool, bool]:
    industry = _resolve_named_industry(session, question)
    if industry is None:
        return plan, False, False
    plan.row_codes = [industry.base_row_code]
    plan.okved_codes = []
    normalized = normalize_text(question)
    asks_region = bool(
        re.search(
            r"\b(?:в|по|среди)\s+ка(?:ком|ких|кой)\s+"
            r"(?:регион\w*|субъект\w*|област\w*|кра\w*|республик\w*)\b",
            normalized,
        )
        or re.search(
            r"\b(?:регион\w*|субъект\w*)\s*[- ]*"
            r"(?:экстрем\w*|лидер\w*|аутсайдер\w*)\b",
            normalized,
        )
    )
    if not asks_region:
        return plan, False, True
    plan.intent = "rank_growth"
    plan.analysis_dimension = "territory"
    plan.group_by = ["territory"]
    plan.ranking_criteria = ["growth_rate"]
    plan.territories = ["Российская Федерация"]
    plan.include_top_territory = False
    plan.include_bottom_territory = False
    plan.limit = plan.limit or 5
    if plan.periods:
        plan.periods = plan.periods[-1:]
        plan.period_modes = plan.period_modes[-1:]
        plan.period_labels = plan.period_labels[-1:]
    return plan, True, True


def _has_explicit_period(question: str) -> bool:
    normalized = normalize_text(question)
    return bool(
        extract_years(question)
        or _explicit_cumulative_month(question) is not None
        or re.search(
            r"\b(?:перв\w*|втор\w*|трет\w*|четверт\w*|[1-4]|"
            r"i{1,3}|iv)\s+квартал\w*\b",
            normalized,
            re.IGNORECASE,
        )
    )


def _apply_explicit_territory_scope(
    session: Session,
    question: str,
    plan: QueryPlan,
    *,
    preserve_row_scope: bool = False,
) -> tuple[QueryPlan, bool]:
    """Make a territory named in the current question override chat context."""
    probe = plan.model_copy(deep=True)
    probe.territories = ["Российская Федерация"]
    probe = _enrich_explicit_territories(session, question, probe)
    explicit_territories = [
        item
        for item in probe.territories
        if normalize_text(item) != "российская федерация"
    ]
    if not explicit_territories:
        return plan, False

    plan.territories = explicit_territories
    plan.include_top_territory = False
    plan.include_bottom_territory = False
    if not preserve_row_scope:
        # An explicit named territory starts a fresh aggregate slice. Do not
        # leak a previously selected (or falsely inferred) industry into it.
        plan.row_codes = [1010]
        plan.okved_codes = []
    if not _has_explicit_period(question):
        regional_periods = _regional_periods(
            session,
            plan,
            latest_only=True,
        )
        if regional_periods:
            plan.periods = [regional_periods[0]]
            plan.period_modes = ["cumulative_ytd"]
            plan.period_labels = []
    normalized = normalize_text(question)
    asks_industry = any(
        token in normalized for token in ("отрасл", "оквэд")
    )
    asks_ranking = any(
        token in normalized
        for token in (
            "динамик",
            "рост",
            "паден",
            "снижен",
            "худш",
            "лучш",
            "лидер",
            "наибольш",
            "наименьш",
            "минимал",
            "максимал",
        )
    )
    focused_industry_ranking = (
        asks_industry and asks_ranking and not preserve_row_scope
    )
    if focused_industry_ranking:
        plan.intent = "rank_growth"
        plan.analysis_dimension = "industry"
        plan.group_by = ["industry"]
        plan.ranking_criteria = ["growth_rate"]
        plan.ranking_direction = _requested_direction(question)
        plan.row_codes = [1010]
        plan.okved_codes = []
        plan.limit = plan.limit or 5
    return plan, focused_industry_ranking


def _base_arguments(plan: QueryPlan) -> dict:
    return {
        "metric_codes": list(plan.metric_codes),
        "enterprise_groups": list(plan.enterprise_groups),
        "territories": list(plan.territories),
        "row_codes": list(plan.row_codes),
        "periods": [item.isoformat() for item in plan.periods],
        "period_modes": list(plan.period_modes),
        "period_labels": list(plan.period_labels),
    }


def _candidate_analyses(question: str, plan: QueryPlan) -> list[str]:
    normalized = normalize_text(question)
    result: list[str] = []
    asks_trend = any(
        token in normalized for token in ("динамик", "тренд", "трэнд")
    )
    asks_industry = any(
        token in normalized for token in ("отрасл", "оквэд")
    )
    asks_region = any(
        token in normalized
        for token in (
            "регион",
            "ригион",
            "субъект",
            "субьект",
        )
    )
    asks_region = asks_region or any(
        re.search(pattern, normalized)
        for pattern in (
            r"\bпо\s+каким\s+област",
            r"\bпо\s+каким\s+кра",
            r"\bпо\s+каким\s+республик",
            r"\bпо\s+каким\s+округ",
        )
    )
    asks_outlier = any(
        token in normalized
        for token in (
            "аномал",
            "анамал",
            "выбива",
            "выброс",
            "нетипич",
            "экстрем",
            "экстрим",
        )
    )
    asks_distribution = any(
        token in normalized for token in ("распредел", "расприд", "медиан")
    )
    asks_ranking = any(
        token in normalized
        for token in (
            "лучш",
            "худш",
            "лидер",
            "лидир",
            "аутсайдер",
            "наибольш",
            "наименьш",
            "максимал",
            "минимал",
            "рейтинг",
        )
    )
    ranking_intents = {
        "rank",
        "rank_growth",
        "rank_difference",
        "regional_average_growth",
        "regional_industry_rank_growth",
        "analytical_brief",
    }
    references_selected_industries = _refers_to_selected_industries(
        normalized
    )
    asks_drill_down_sequence = any(
        token in normalized
        for token in ("затем", "потом", "перейди", "выдел")
    )

    def add(name: str) -> None:
        if name not in result:
            result.append(name)

    if plan.intent == "catalog":
        return ["resolve"]
    if (
        plan.intent in {"trend", "analytical_brief"}
        or len(plan.periods) > 2
        or asks_trend
    ):
        add("series")
    if plan.intent in {"value", "compare", "overview", "share", "source"}:
        add("comparison")
    if (
        (
            plan.analysis_dimension == "industry"
            and (
                plan.intent in ranking_intents
                or asks_distribution
                or asks_outlier
            )
        )
        or (
            asks_industry
            and (
                asks_ranking
                or asks_outlier
                or references_selected_industries
                or asks_drill_down_sequence
            )
        )
        or plan.intent in {"analytical_brief", "regional_average_growth"}
    ) and not (asks_region and not asks_industry) and not (
        references_selected_industries and asks_region
    ):
        add("industry_rank")
    if (
        (
            plan.analysis_dimension == "territory"
            and (
                plan.intent in ranking_intents
                or plan.intent == "outliers"
                or asks_outlier
            )
        )
        or (
            asks_region
            and (
                asks_ranking
                or asks_outlier
                or "отклон" in normalized
            )
        )
    ) and not asks_industry:
        add("regional_rank")
    if (
        plan.intent == "outliers"
        or asks_outlier
    ):
        add("outliers")
    if asks_distribution:
        add("distribution")
    if "структур" in normalized or plan.intent == "share":
        add("structure")
    if (
        plan.intent
        in {
            "analytical_brief",
            "regional_average_growth",
            "regional_industry_rank_growth",
        }
        or (
            asks_industry
            and asks_region
            and (
                asks_ranking
                or asks_outlier
                or asks_drill_down_sequence
                or references_selected_industries
            )
        )
    ):
        if not references_selected_industries:
            add("industry_rank")
        add("industry_to_regions")
    if not result:
        add("comparison")
    return result[:8]


def _public_analyses(
    question: str,
    plan: QueryPlan,
    analyses: list[str],
) -> list[str]:
    """Do not execute catalog resolution as an analysis of a known slice."""
    if plan.intent == "catalog":
        return analyses
    visible = [item for item in analyses if item != "resolve"]
    return visible or _candidate_analyses(question, plan)


def _research_questions(analyses: list[str]) -> list[str]:
    labels = {
        "resolve": "Какие сущности и периоды доступны?",
        "series": "Как менялся показатель во времени?",
        "comparison": "Как отличаются выбранные сопоставимые срезы?",
        "industry_rank": "Какие отрасли показывают лучшие и худшие результаты?",
        "regional_rank": "Какие территории показывают лучшие и худшие результаты?",
        "outliers": "Какие региональные значения статистически выделяются?",
        "distribution": "Как распределён показатель между объектами?",
        "structure": "Какова структура выбранного агрегата?",
        "industry_to_regions": (
            "В каких регионах выбранные отрасли имеют экстремальные значения?"
        ),
        "ranked_industry_series": (
            "Как менялся показатель по найденным отраслям "
            "в запрошенной территории?"
        ),
    }
    return [labels[item] for item in analyses if item in labels]


def _requested_direction(question: str) -> str:
    normalized = normalize_text(question)
    asks_top = any(
        token in normalized
        for token in (
            "лучш",
            "лидер",
            "наибольш",
            "максимал",
        )
    ) or re.search(r"\bлидир\w*", normalized) is not None
    asks_bottom = any(
        token in normalized
        for token in (
            "худш",
            "хуже",
            "наименьш",
            "минимал",
            "аутсайдер",
            "паден",
            "снижен",
            "сниж",
            "упал",
            "упав",
            "сокращ",
            "отрицательн",
            "просад",
            "спад",
        )
    )
    if asks_bottom and not asks_top:
        return "bottom"
    if asks_top and not asks_bottom:
        return "top"
    return "both"


def _operation_for(
    analysis: str,
    base: dict,
    *,
    index: int,
    direction: str,
    criterion: str,
    previous_id: str | None = None,
) -> ResearchOperation:
    operation_id = f"op-{index}-{analysis.replace('_', '-')}"
    current_period = base["periods"][-1:]
    current_modes = base["period_modes"][-1:] if base["period_modes"] else []
    current_labels = base["period_labels"][-1:] if base["period_labels"] else []
    current = {
        **base,
        "periods": current_period,
        "period_modes": current_modes,
        "period_labels": current_labels,
    }
    if analysis == "resolve":
        return ResearchOperation(
            id=operation_id,
            tool="resolve_entities",
            goal="Разрешить показатели, отрасли, территории и периоды",
            arguments={"limit": 50},
        )
    if analysis == "series":
        return ResearchOperation(
            id=operation_id,
            tool="build_series",
            goal="Получить сопоставимую динамику показателя",
            arguments=base,
        )
    if analysis == "comparison":
        return ResearchOperation(
            id=operation_id,
            tool="compare_entities",
            goal="Сравнить выбранные срезы",
            arguments=base,
        )
    if analysis in {"industry_rank", "regional_rank"}:
        dimension = (
            "industry" if analysis == "industry_rank" else "territory"
        )
        return ResearchOperation(
            id=operation_id,
            tool="rank_entities",
            goal=(
                "Определить лучшие и худшие отрасли"
                if dimension == "industry"
                else "Определить лучшие и худшие территории"
            ),
            arguments={
                **current,
                "dimension": dimension,
                "criterion": criterion,
                "direction": direction,
                # For a two-sided request keep three leaders and three
                # outsiders. This lets the dependent regional drill-down
                # return both sides for every selected industry without
                # exceeding the 50-row tool limit.
                "limit": 3 if direction == "both" else 5,
            },
        )
    if analysis == "outliers":
        return ResearchOperation(
            id=operation_id,
            tool="detect_outliers",
            goal="Проверить региональные аномалии",
            arguments={
                **current,
                "dimension": "territory",
                "modes": ["value", "growth_rate", "composite"],
            },
        )
    if analysis == "distribution":
        return ResearchOperation(
            id=operation_id,
            tool="describe_distribution",
            goal="Описать распределение по объектам",
            arguments={
                **current,
                "dimension": "industry",
                "measure": criterion,
                "limit": 50,
            },
        )
    if analysis == "structure":
        return ResearchOperation(
            id=operation_id,
            tool="analyze_structure",
            goal="Рассчитать структуру агрегата",
            arguments={
                **current,
                "structure_metric_codes": [
                    "receipts_profit_tax_total",
                    "receipts_ndfl_total",
                    "receipts_vat_total",
                    "receipts_special_regimes_total",
                ],
            },
        )
    if analysis == "industry_to_regions":
        return ResearchOperation(
            id=operation_id,
            tool="drill_down",
            goal="Найти региональные экстремумы в выбранных отраслях",
            arguments={
                **current,
                "from_dimension": "industry",
                "to_dimension": "territory",
                "criterion": criterion,
                "direction": direction,
                "limit": 3,
            },
            depends_on=[previous_id] if previous_id else [],
        )
    if analysis == "ranked_industry_series":
        return ResearchOperation(
            id=operation_id,
            tool="build_series",
            goal=(
                "Построить динамику найденных отраслей "
                "в запрошенной территории"
            ),
            arguments=base,
            depends_on=[previous_id] if previous_id else [],
        )
    raise ValueError(f"Неизвестный вид анализа: {analysis}")


def _make_research_plan(
    question: str,
    base_plan: QueryPlan,
    analyses: list[str],
    *,
    direction: str | None = None,
    criterion: str | None = None,
) -> ResearchPlan:
    base = _base_arguments(base_plan)
    operations: list[ResearchOperation] = []
    industry_operation_id: str | None = None
    direction = direction or base_plan.ranking_direction
    criterion = criterion or (
        base_plan.ranking_criteria[0]
        if base_plan.ranking_criteria
        else "value"
    )
    for index, analysis in enumerate(analyses, start=1):
        operation = _operation_for(
            analysis,
            base,
            index=index,
            direction=direction,
            criterion=criterion,
            previous_id=industry_operation_id,
        )
        operations.append(operation)
        if analysis == "industry_rank":
            industry_operation_id = operation.id
    return ResearchPlan(
        objective=re.sub(r"\s+", " ", question).strip(),
        research_questions=_research_questions(analyses),
        initial_filters=base,
        expected_analyses=analyses,
        operations=operations[:MAX_TOOL_CALLS],
    )


def _parse_json_object(raw: str) -> dict:
    candidate = re.sub(
        r"<think>.*?</think>",
        "",
        raw.strip(),
        flags=re.IGNORECASE | re.DOTALL,
    )
    candidate = re.sub(r"```(?:json)?", "", candidate, flags=re.IGNORECASE)
    decoder = json.JSONDecoder()
    objects: list[tuple[int, int, dict]] = []
    arrays: list[tuple[int, int, list]] = []
    for match in re.finditer(r"[\{\[]", candidate):
        try:
            parsed, length = decoder.raw_decode(candidate[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            objects.append((length, match.start(), parsed))
        elif isinstance(parsed, list):
            arrays.append((length, match.start(), parsed))
    if not objects:
        if arrays:
            # Qwen occasionally returns the requested analyses as a bare
            # array, or as ``analyses: [...]`` without the outer braces.
            # Normalise that transport-level variation to the same contract.
            selected = max(arrays, key=lambda item: (item[0], item[1]))[2]
            return {"analyses": selected}
        raise ValueError("Ответ модели должен содержать JSON-объект")
    # Prefer the largest object: for a ResearchPlan it is the outer contract,
    # not a nested operation. Position breaks ties in favour of the final JSON.
    return max(objects, key=lambda item: (item[0], item[1]))[2]


def _semantic_plan_signature(
    semantic: SemanticQuery | None,
) -> dict[str, Any] | None:
    if semantic is None:
        return None
    plan = semantic.query_plan
    return {
        "status": semantic.status,
        "intent": plan.intent if plan else None,
        "metric_codes": list(plan.metric_codes) if plan else [],
        "territories": list(plan.territories) if plan else [],
        "row_codes": list(plan.row_codes) if plan else [],
        "enterprise_groups": list(plan.enterprise_groups) if plan else [],
        "periods": (
            [item.isoformat() for item in plan.periods] if plan else []
        ),
        "period_modes": list(plan.period_modes) if plan else [],
        "analyses": list(semantic.analyses),
        "ranking_direction": semantic.ranking_direction,
        "ranking_criterion": semantic.ranking_criterion,
        "constraint_sources": dict(semantic.constraint_sources),
    }


def _query_plan_signature(
    plan: QueryPlan,
    analyses: list[str],
    *,
    criterion: str,
) -> dict[str, Any]:
    return {
        "status": "ready",
        "intent": plan.intent,
        "metric_codes": list(plan.metric_codes),
        "territories": list(plan.territories),
        "row_codes": list(plan.row_codes),
        "enterprise_groups": list(plan.enterprise_groups),
        "periods": [item.isoformat() for item in plan.periods],
        "period_modes": list(plan.period_modes),
        "analyses": list(analyses),
        "ranking_direction": plan.ranking_direction,
        "ranking_criterion": criterion,
        "constraint_sources": {},
    }


def _signature_diff(
    primary: dict[str, Any], shadow: dict[str, Any] | None
) -> dict[str, Any]:
    if shadow is None:
        return {"matches": False, "differences": {"shadow": "unavailable"}}
    differences = {
        key: {
            "deterministic": primary.get(key),
            "model_first": shadow.get(key),
        }
        for key in sorted(set(primary) | set(shadow))
        if primary.get(key) != shadow.get(key)
    }
    return {"matches": not differences, "differences": differences}


async def build_research_plan(
    session: Session,
    settings: Settings,
    conversation: Conversation,
    question: str,
    *,
    strict_provider: bool = False,
) -> PlannedResearch:
    planner_settings = settings.model_copy(update={"llm_max_retries": 0})
    provider = configured_provider(planner_settings)
    rollout_mode = (
        "model_first"
        if strict_provider
        else "deterministic"
        if isinstance(provider, DeterministicProvider)
        else settings.analytic_planner_mode
    )
    semantic_result = None
    semantic_error: PlanningUnavailableError | None = None
    if rollout_mode in {"model_first", "shadow"}:
        try:
            semantic_result = await plan_question(
                session,
                planner_settings,
                question,
                available_periods(session),
                _planner_context(conversation),
                allow_deterministic_fallback=False,
            )
        except PlanningUnavailableError as exc:
            semantic_error = exc
            if rollout_mode == "model_first":
                raise AgentUnavailableError(str(exc)) from exc

        if semantic_result is not None and rollout_mode == "model_first":
            if semantic_result.status == "no_data":
                raise ResearchNoData(
                    semantic_result.clarification_question
                    or "Для запроса нет проверенных данных."
                )
            if (
                semantic_result.status == "needs_clarification"
                or semantic_result.plan is None
            ):
                raise ResearchClarification(
                    semantic_result.clarification_question
                    or "Уточните показатель, период или аналитический срез.",
                    semantic_result.clarification_options,
                )
            semantic = semantic_result.semantic_query
            if semantic is None:
                raise AgentUnavailableError(
                    "Модель не вернула обязательный SemanticQuery."
                )
            analyses = _public_analyses(
                question,
                semantic_result.plan,
                list(semantic.analyses),
            )
            research_plan = _make_research_plan(
                question,
                semantic_result.plan,
                analyses,
                direction=semantic.ranking_direction,
                criterion=semantic.ranking_criterion,
            )
            return PlannedResearch(
                research_plan=research_plan,
                base_plan=semantic_result.plan,
                planner_mode=f"research-agent:{provider.name}",
                warnings=semantic_result.warnings,
                model_calls=semantic_result.model_calls,
                semantic_query=semantic,
            )

    deterministic_settings = settings.model_copy(
        update={
            "llm_provider": "deterministic",
            "llm_data_policy": "none",
            "allow_external_llm_data": False,
        }
    )
    planned = await plan_question(
        session,
        deterministic_settings,
        question,
        available_periods(session),
        _planner_context(conversation),
    )
    if planned.status == "no_data":
        raise ResearchNoData(
            planned.clarification_question
            or "Для запроса нет проверенных данных."
        )
    if planned.status == "needs_clarification" or planned.plan is None:
        territory_clarification = (
            "конкретн"
            in str(planned.clarification_question or "").casefold()
            and "территор"
            in str(planned.clarification_question or "").casefold()
        )
        fallback_plan = (
            _broad_fallback_plan(session, conversation, question)
            if _has_broad_analysis_request(question)
            and not territory_clarification
            else None
        )
        if fallback_plan is None:
            raise ResearchClarification(
                planned.clarification_question
                or "Уточните показатель, период или аналитический срез.",
                planned.clarification_options,
            )
        base_plan = fallback_plan
        warnings = [
            *planned.warnings,
            (
                "Широкий аналитический запрос разложен на безопасные "
                "операции; неопределённые сущности унаследованы из диалога."
            ),
        ]
    else:
        base_plan = planned.plan
        warnings = planned.warnings
    (
        base_plan,
        focused_industry_region,
        explicit_industry_selected,
    ) = _apply_explicit_industry_scope(
        session,
        question,
        base_plan,
    )
    base_plan, focused_territory_industries = (
        _apply_explicit_territory_scope(
            session,
            question,
            base_plan,
            preserve_row_scope=(
                explicit_industry_selected
                or _refers_to_selected_industries(
                    normalize_text(question)
                )
            ),
        )
    )
    base_plan = _apply_return_to_country_context(
        conversation,
        question,
        base_plan,
    )
    candidates = (
        ["industry_rank", "ranked_industry_series"]
        if focused_territory_industries
        else _candidate_analyses(question, base_plan)
    )
    if (
        explicit_industry_selected
        and not focused_industry_region
        and base_plan.intent in {"value", "compare", "source", "overview"}
    ):
        # “Значение по отрасли X” is a comparison of that selected row, not a
        # request to rank every industry merely because the word “отрасль” is
        # present in the question.
        candidates = [
            item
            for item in candidates
            if item not in {"industry_rank", "ranked_industry_series"}
        ]
        if "comparison" not in candidates:
            candidates.insert(0, "comparison")
    analyses = candidates
    base_plan.ranking_direction = _requested_direction(question)
    criterion = (
        "difference"
        if base_plan.intent == "rank_difference"
        else (
            base_plan.ranking_criteria[0]
            if base_plan.ranking_criteria
            else "value"
        )
    )
    model_calls = (
        semantic_result.model_calls
        if semantic_result is not None
        else semantic_error.model_calls
        if semantic_error is not None
        else 0
    )
    planner_mode = "research-agent:deterministic"
    shadow_diff = None
    semantic_query = (
        semantic_result.semantic_query if semantic_result is not None else None
    )
    if rollout_mode == "shadow":
        planner_mode = (
            f"research-agent:shadow:{provider.name}"
            if semantic_error is None
            else "research-agent:shadow-error"
        )
        shadow_diff = _signature_diff(
            _query_plan_signature(
                base_plan,
                analyses,
                criterion=criterion,
            ),
            _semantic_plan_signature(semantic_query),
        )
        if semantic_error is not None:
            warnings.append(
                "Shadow-планировщик недоступен: " + str(semantic_error)
            )
    research_plan = _make_research_plan(
        question,
        base_plan,
        analyses,
        direction=base_plan.ranking_direction,
        criterion=criterion,
    )
    return PlannedResearch(
        research_plan=research_plan,
        base_plan=base_plan,
        planner_mode=planner_mode,
        warnings=warnings,
        model_calls=model_calls,
        semantic_query=semantic_query,
        shadow_diff=shadow_diff,
    )


def _inject_drill_down(
    operation: ResearchOperation,
    evidence_by_operation: dict[str, EvidenceItem],
) -> ResearchOperation:
    if operation.tool != "drill_down" or not operation.depends_on:
        return operation
    arguments = dict(operation.arguments)
    direction = str(arguments.get("direction") or "top")
    wanted_section_kinds = {
        "top": {"ranking_growth"},
        "bottom": {"ranking_growth_worst"},
        "both": {"ranking_growth", "ranking_growth_worst"},
    }[direction]
    selected_codes: list[int] = []
    fallback_codes: list[int] = []
    selected_territories: list[str] = []
    for dependency in operation.depends_on:
        evidence = evidence_by_operation.get(dependency)
        if evidence is None:
            continue
        for row in evidence.rows:
            row_code = row.get("row_code")
            if isinstance(row_code, int) and row_code != 1010:
                fallback_codes.append(row_code)
                if row.get("section_kind") in wanted_section_kinds:
                    selected_codes.append(row_code)
            territory = row.get("territory")
            if isinstance(territory, str) and territory:
                selected_territories.append(territory)
    selected_codes = selected_codes or fallback_codes
    if selected_codes:
        arguments["selected_row_codes"] = list(
            dict.fromkeys(selected_codes)
        )[:12]
    if selected_territories and arguments.get("to_dimension") == "industry":
        arguments["selected_territories"] = list(
            dict.fromkeys(selected_territories)
        )[:6]
    return operation.model_copy(update={"arguments": arguments})


def _inject_ranked_industry_series(
    operation: ResearchOperation,
    evidence_by_operation: dict[str, EvidenceItem],
) -> ResearchOperation:
    if (
        operation.tool != "build_series"
        or "ranked-industry-series" not in operation.id
        or not operation.depends_on
    ):
        return operation
    arguments = dict(operation.arguments)
    selected_codes: list[int] = []
    selected_periods: list[str] = []
    for dependency in operation.depends_on:
        evidence = evidence_by_operation.get(dependency)
        if evidence is None:
            continue
        selected_periods.extend(evidence.periods)
        for row in evidence.rows:
            row_code = row.get("row_code")
            if isinstance(row_code, int) and row_code != 1010:
                selected_codes.append(row_code)
    if selected_codes:
        arguments["row_codes"] = list(dict.fromkeys(selected_codes))[:8]
    if selected_periods:
        periods = sorted(dict.fromkeys(selected_periods))
        arguments["periods"] = periods
        arguments["period_modes"] = ["cumulative_ytd"] * len(periods)
        arguments["period_labels"] = periods
    return operation.model_copy(update={"arguments": arguments})


def _inject_operation_dependencies(
    operation: ResearchOperation,
    evidence_by_operation: dict[str, EvidenceItem],
) -> ResearchOperation:
    operation = _inject_drill_down(operation, evidence_by_operation)
    return _inject_ranked_industry_series(
        operation,
        evidence_by_operation,
    )


def _legacy_merge(
    results: list[tuple[ResearchOperation, dict, EvidenceItem]],
) -> dict:
    facts_by_id: dict[str, dict] = {}
    calculations: list[dict] = []
    series: list[dict] = []
    sections: list[dict] = []
    warnings: list[str] = []
    resolved_metrics: dict[str, dict] = {}
    visualization = None
    for operation, result, evidence in results:
        for fact in result.get("facts", []):
            facts_by_id[str(fact.get("fact_id"))] = fact
        calculations.extend(result.get("calculations", []))
        series.extend(result.get("series", []))
        for section in result.get("analysis_sections", []):
            sections.append({**section, "evidence_id": evidence.evidence_id})
        for item in result.get("resolved_metrics", []):
            code = str(item.get("semantic_code"))
            resolved_metrics[code] = item
        warnings.extend(result.get("warnings", []))
        visualization = visualization or result.get("visualization")
    return {
        "facts": list(facts_by_id.values()),
        "calculations": calculations,
        "series": series,
        "analysis_sections": sections,
        "resolved_metrics": list(resolved_metrics.values()),
        "visualization": visualization,
        "warnings": list(dict.fromkeys(warnings)),
    }


NUMERIC_KEYS = {
    "value",
    "current",
    "previous",
    "difference",
    "growth_rate",
    "median_growth_rate",
    "weighted_growth_rate",
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


def _first_numeric(row: dict) -> tuple[str, str] | None:
    for key in (
        "growth_rate",
        "difference",
        "current",
        "value",
        "share",
        "median",
        "mean",
        "score",
        "count",
    ):
        value = row.get(key)
        if value is not None:
            return key, str(value)
    return None


def _numeric_unit(
    key: str,
    row: dict,
    evidence: EvidenceItem,
) -> str:
    if key in {"growth_rate", "share"}:
        return "%"
    return str(row.get("unit") or evidence.unit or "")


def deterministic_claims(
    evidence: list[EvidenceItem],
) -> list[EvidenceClaim]:
    claims: list[EvidenceClaim] = []
    for index, item in enumerate(evidence, start=1):
        usable = [
            row for row in item.rows if _first_numeric(row) is not None
        ]
        if not usable:
            continue
        if item.tool == "build_series":
            grouped: dict[tuple[str, object, str], list[dict]] = {}
            for row in usable:
                grouped.setdefault(
                    (
                        str(row.get("entity") or "объект"),
                        row.get("row_code"),
                        str(row.get("territory") or ""),
                    ),
                    [],
                ).append(row)
            for offset, rows in enumerate(grouped.values(), start=1):
                ordered = sorted(
                    rows,
                    key=lambda row: str(row.get("period") or ""),
                )
                first, last = ordered[0], ordered[-1]
                first_numeric = (
                    ("value", str(first["value"]))
                    if first.get("value") is not None
                    else _first_numeric(first)
                )
                last_numeric = (
                    ("value", str(last["value"]))
                    if last.get("value") is not None
                    else _first_numeric(last)
                )
                assert first_numeric is not None
                assert last_numeric is not None
                first_key, first_value = first_numeric
                _, last_value = last_numeric
                entity = str(first.get("entity") or "объект")
                first_period = str(first.get("period") or "")
                last_period = str(last.get("period") or "")
                unit = _numeric_unit(first_key, first, item)
                if first is not last and first_period and last_period:
                    text = (
                        f"{entity}: {first_period} — {first_value}"
                        f"{(' ' + unit) if unit else ''}; "
                        f"{last_period} — {last_value}"
                        f"{(' ' + unit) if unit else ''}."
                    )
                    numbers = [first_value, last_value]
                    periods = [first_period, last_period]
                else:
                    text = (
                        f"{entity}: значение — {first_value}"
                        f"{(' ' + unit) if unit else ''}."
                    )
                    numbers = [first_value]
                    periods = [first_period] if first_period else []
                claims.append(
                    EvidenceClaim(
                        claim_id=f"claim-{index}-{offset}-verified",
                        text=text,
                        evidence_ids=[item.evidence_id],
                        numbers=numbers,
                        periods=list(dict.fromkeys(periods)),
                        entities=[entity],
                    )
                )
            continue
        if item.tool == "compare_entities":
            grouped: dict[tuple[str, object, str], list[dict]] = {}
            for row in usable:
                grouped.setdefault(
                    (
                        str(row.get("entity") or "объект"),
                        row.get("row_code"),
                        str(row.get("territory") or ""),
                    ),
                    [],
                ).append(row)
            for offset, rows in enumerate(grouped.values(), start=1):
                current = max(
                    rows,
                    key=lambda row: str(row.get("period") or ""),
                )
                current_value = current.get("value")
                if current_value is None:
                    continue
                entity = str(current.get("entity") or "объект")
                period = str(current.get("period") or "")
                unit = str(current.get("unit") or item.unit or "")
                numbers = [str(current_value)]
                parts = [
                    f"{entity}: {period} — {current_value}"
                    f"{(' ' + unit) if unit else ''}"
                ]
                previous = current.get("previous")
                difference = current.get("difference")
                growth_rate = current.get("growth_rate")
                if previous is not None:
                    numbers.append(str(previous))
                    parts.append(
                        f"предыдущий сопоставимый срез — {previous}"
                        f"{(' ' + unit) if unit else ''}"
                    )
                if difference is not None:
                    numbers.append(str(difference))
                    parts.append(
                        f"изменение — {difference}"
                        f"{(' ' + unit) if unit else ''}"
                    )
                if growth_rate is not None:
                    numbers.append(str(growth_rate))
                    parts.append(f"прирост — {growth_rate} %")
                claims.append(
                    EvidenceClaim(
                        claim_id=f"claim-{index}-{offset}-verified",
                        text="; ".join(parts) + ".",
                        evidence_ids=[item.evidence_id],
                        numbers=numbers,
                        periods=[period] if period else [],
                        entities=[entity],
                    )
                )
            continue
        if item.tool in {"rank_entities", "drill_down"}:
            selected_rows: dict[tuple[object, str], dict] = {}
            for row in usable:
                row_code = row.get("row_code")
                section_kind = str(row.get("section_kind") or "")
                if item.tool == "rank_entities":
                    if section_kind not in {
                        "ranking_growth",
                        "ranking_growth_worst",
                    }:
                        continue
                    key: tuple[object, str] = ("ranking", section_kind)
                else:
                    if not isinstance(row_code, int) or row_code == 1010:
                        continue
                    key = (row_code, section_kind)
                selected_rows.setdefault(key, row)
            for offset, row in enumerate(
                selected_rows.values(),
                start=1,
            ):
                numeric = _first_numeric(row)
                assert numeric is not None
                key, value = numeric
                section_kind = str(row.get("section_kind") or "")
                entity = str(
                    row.get("entity")
                    or row.get("territory")
                    or "объект"
                )
                territory = str(row.get("territory") or "")
                subject = entity
                entities = [entity]
                if (
                    item.tool == "rank_entities"
                    and territory
                    and territory != "Российская Федерация"
                    and territory != entity
                ):
                    subject = f"{territory} — {entity}"
                    entities.append(territory)
                unit = _numeric_unit(key, row, item)
                if key == "growth_rate" and section_kind in {
                    "ranking_growth",
                    "regional_industry_growth",
                }:
                    label = "максимальный темп роста"
                elif key == "growth_rate" and section_kind in {
                    "ranking_growth_worst",
                    "regional_industry_growth_worst",
                }:
                    label = "минимальный темп роста"
                else:
                    label = {
                        "growth_rate": "темп роста",
                        "difference": "изменение",
                        "current": "текущее значение",
                        "value": "значение",
                    }.get(key, key)
                claims.append(
                    EvidenceClaim(
                        claim_id=f"claim-{index}-{offset}-verified",
                        text=(
                            f"{subject}: {label} — {value}"
                            f"{(' ' + unit) if unit else ''}."
                        ),
                        evidence_ids=[item.evidence_id],
                        numbers=[value],
                        periods=[],
                        entities=entities,
                    )
                )
            if selected_rows:
                continue
        first = usable[0]
        last = usable[-1]
        first_numeric = _first_numeric(first)
        last_numeric = _first_numeric(last)
        assert first_numeric is not None
        assert last_numeric is not None
        first_key, first_value = first_numeric
        last_key, last_value = last_numeric
        first_entity = str(first.get("entity") or first.get("territory") or "объект")
        last_entity = str(last.get("entity") or last.get("territory") or first_entity)
        first_period = str(first.get("period") or "")
        last_period = str(last.get("period") or "")
        unit = _numeric_unit(first_key, first, item)
        if first is not last and first_period and last_period:
            text = (
                f"{first_entity}: {first_period} — {first_value}"
                f"{(' ' + unit) if unit else ''}; "
                f"{last_period} — {last_value}"
                f"{(' ' + unit) if unit else ''}."
            )
            numbers = [first_value, last_value]
            periods = list(dict.fromkeys([first_period, last_period]))
            entities = [first_entity]
        else:
            label = {
                "growth_rate": "темп роста",
                "difference": "изменение",
                "current": "текущее значение",
                "value": "значение",
                "share": "доля",
                "median": "медиана",
                "mean": "среднее",
                "score": "диагностический индекс",
                "count": "число наблюдений",
            }.get(first_key, first_key)
            text = (
                f"{first_entity}: {label} — {first_value}"
                f"{(' ' + unit) if unit else ''}."
            )
            numbers = [first_value]
            periods = [first_period] if first_period else []
            entities = [first_entity]
        claims.append(
            EvidenceClaim(
                claim_id=f"claim-{index}-verified",
                text=text,
                evidence_ids=[item.evidence_id],
                numbers=numbers,
                periods=periods,
                entities=entities,
            )
        )
    return claims


def deterministic_hypotheses(
    question: str,
    evidence: list[EvidenceItem],
) -> list[AnalysisHypothesis]:
    normalized = normalize_text(question)
    diagnostic = any(
        item.tool in {"detect_outliers", "analyze_structure"}
        for item in evidence
    )
    asks_why = "почему" in normalized or "причин" in normalized
    if not diagnostic and not asks_why:
        return []
    return [
        AnalysisHypothesis(
            hypothesis_id="hypothesis-1-structure",
            text=(
                "Наблюдаемое отклонение может отражать изменение отраслевой "
                "или региональной структуры. Для проверки интерпретации "
                "нужны дополнительные факторные данные."
            ),
            evidence_ids=[item.evidence_id for item in evidence[:3]],
        )
    ]


def _normal_number(value: object) -> str:
    candidate = str(value).strip().replace("\u00a0", "").replace(",", ".")
    try:
        normalized = Decimal(candidate)
    except InvalidOperation:
        return candidate
    return format(normalized.normalize(), "f")


NUMBER_RE = re.compile(r"(?<![A-Za-zА-Яа-я_])[-+]?\d+(?:[.,]\d+)?")


def verify_claims(
    claims: list[EvidenceClaim],
    evidence: list[EvidenceItem],
) -> None:
    by_id = {item.evidence_id: item for item in evidence}
    for claim in claims:
        if any(item not in by_id for item in claim.evidence_ids):
            raise ValueError(
                f"Утверждение {claim.claim_id} ссылается на неизвестное доказательство"
            )
        selected = [by_id[item] for item in claim.evidence_ids]
        allowed_numbers: set[str] = set()
        allowed_periods: set[str] = set()
        allowed_entities: set[str] = set()
        for item in selected:
            allowed_periods.update(item.periods)
            for row in item.rows:
                for key in NUMERIC_KEYS:
                    if row.get(key) is not None:
                        allowed_numbers.add(_normal_number(row[key]))
                for key in ("entity", "territory"):
                    if row.get(key):
                        allowed_entities.add(str(row[key]))
                if row.get("period"):
                    allowed_periods.add(str(row["period"]))
        for value in claim.numbers:
            if _normal_number(value) not in allowed_numbers:
                raise ValueError(
                    f"Неподтверждённое число в {claim.claim_id}: {value}"
                )
        if any(period not in allowed_periods for period in claim.periods):
            raise ValueError(
                f"Неподтверждённый период в {claim.claim_id}"
            )
        if any(entity not in allowed_entities for entity in claim.entities):
            raise ValueError(
                f"Неподтверждённая сущность в {claim.claim_id}"
            )
        allowed_text_numbers = set(allowed_numbers)
        for value in [*allowed_periods, *allowed_entities]:
            allowed_text_numbers.update(
                _normal_number(token) for token in NUMBER_RE.findall(value)
            )
        for token in NUMBER_RE.findall(claim.text):
            if _normal_number(token) not in allowed_text_numbers:
                raise ValueError(
                    f"Неизвестное число в тексте {claim.claim_id}: {token}"
                )


ASSERTIVE_CAUSAL_RE = re.compile(
    r"\b(?:вызвал[аои]?|обусловил[аои]?|привел[аои]?|"
    r"доказывает|является причиной|будет|прогнозируется)\b",
    re.IGNORECASE,
)


def verify_hypotheses(
    hypotheses: list[AnalysisHypothesis],
    evidence: list[EvidenceItem],
) -> None:
    by_id = {item.evidence_id: item for item in evidence}
    for hypothesis in hypotheses:
        if any(item not in by_id for item in hypothesis.evidence_ids):
            raise ValueError(
                f"Гипотеза {hypothesis.hypothesis_id} ссылается "
                "на неизвестное доказательство"
            )
        if ASSERTIVE_CAUSAL_RE.search(hypothesis.text):
            raise ValueError(
                f"Гипотеза {hypothesis.hypothesis_id} утверждает "
                "причинность или прогноз"
            )
        normalized = normalize_text(hypothesis.text)
        if not any(
            marker in normalized
            for marker in ("может", "возможн", "вероятн", "для проверки")
        ):
            raise ValueError(
                f"Гипотеза {hypothesis.hypothesis_id} не маркирована "
                "как возможная интерпретация"
            )
        allowed_numbers: set[str] = set()
        for evidence_id in hypothesis.evidence_ids:
            for row in by_id[evidence_id].rows:
                for key in NUMERIC_KEYS:
                    if row.get(key) is not None:
                        allowed_numbers.add(_normal_number(row[key]))
                if row.get("period"):
                    allowed_numbers.update(
                        _normal_number(token)
                        for token in NUMBER_RE.findall(str(row["period"]))
                    )
        for token in NUMBER_RE.findall(hypothesis.text):
            if _normal_number(token) not in allowed_numbers:
                raise ValueError(
                    f"Неподтверждённое число в гипотезе "
                    f"{hypothesis.hypothesis_id}: {token}"
                )


def _model_evidence_payload(evidence: list[EvidenceItem]) -> list[dict]:
    remaining = MAX_MODEL_CONTEXT_ROWS
    payload = []
    for item in evidence:
        if remaining <= 0:
            break
        rows = item.rows[:remaining]
        remaining -= len(rows)
        payload.append(
            {
                "evidence_id": item.evidence_id,
                "title": item.title,
                "periods": item.periods,
                "unit": item.unit,
                "methodology": item.methodology,
                "sources": item.sources,
                "rows": rows,
            }
        )
    return payload


async def _synthesize_with_model(
    settings: Settings,
    question: str,
    evidence: list[EvidenceItem],
    *,
    strict_provider: bool = False,
) -> tuple[
    list[EvidenceClaim],
    list[AnalysisHypothesis],
    int,
    str,
]:
    provider = configured_provider(settings)
    verified_claims = deterministic_claims(evidence)
    canonical_hypotheses = deterministic_hypotheses(question, evidence)
    if (
        isinstance(provider, DeterministicProvider)
        or settings.llm_data_policy != "aggregates"
        or not settings.allow_external_llm_data
    ):
        if strict_provider:
            raise AgentUnavailableError(
                "Сертификация запрещает synthesis без qwen3.6-35b."
            )
        return (
            verified_claims,
            canonical_hypotheses,
            0,
            "deterministic",
        )
    if not canonical_hypotheses:
        # Factual language is deliberately rendered from typed evidence. Qwen
        # has already participated in the mandatory research-plan call, while
        # a second generative pass would only create a new hallucination
        # surface for a simple value/trend/ranking answer.
        return verified_claims, [], 0, "verified-evidence-template"
    messages = [
        {
            "role": "system",
            "content": (
                "Сформируй только JSON с массивом hypotheses. "
                "Не пересказывай фактические claims: они формируются "
                "типизированным контуром. hypotheses: hypothesis_id, "
                "label='Гипотеза', text, "
                "evidence_ids, status='possible_interpretation'. Не утверждай "
                "причинность и прогноз. Не добавляй чисел. Абсолютных путей, "
                "SQL и первичных строк в контексте нет."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "question": question,
                    "evidence": _model_evidence_payload(evidence),
                },
                ensure_ascii=False,
                default=str,
            ),
        },
    ]
    try:
        raw = await provider.complete(
            messages,
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=1800,
            extra_body={"thinking": {"type": "enabled"}},
            allow_fallback=False,
        )
        parsed = _parse_json_object(raw)
        hypotheses: list[AnalysisHypothesis] = []
        raw_hypotheses = parsed.get("hypotheses", [])
        if isinstance(raw_hypotheses, list):
            for item in raw_hypotheses:
                try:
                    hypothesis = AnalysisHypothesis.model_validate(item)
                    verify_hypotheses([hypothesis], evidence)
                except (ValidationError, ValueError, TypeError):
                    continue
                hypotheses.append(hypothesis)
        # A model hypothesis is optional editorial material. If it violates
        # evidence or causality constraints, use the canonical explicitly
        # labelled interpretation; factual claims never fall back because they
        # are always generated from typed evidence above.
        return (
            verified_claims,
            hypotheses or canonical_hypotheses,
            1,
            f"{provider.name}:{provider.model}:guarded",
        )
    except (
        LLMProviderError,
        ValidationError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        if strict_provider:
            raise AgentUnavailableError(
                "Сертификация запрещает fallback при synthesis."
            ) from exc
        return (
            verified_claims,
            canonical_hypotheses,
            1,
            "deterministic-fallback",
        )


def _selected_context(evidence: list[EvidenceItem]) -> dict:
    industries: dict[int, str] = {}
    territories: list[str] = []
    for item in evidence:
        for row in item.rows:
            row_code = row.get("row_code")
            entity = row.get("entity")
            if isinstance(row_code, int) and row_code != 1010 and entity:
                industries.setdefault(
                    row_code,
                    str(row.get("industry") or entity),
                )
            territory = row.get("territory") or row.get("region")
            if isinstance(territory, str) and territory != "Российская Федерация":
                territories.append(territory)
    return {
        "selected_industries": [
            {"row_code": code, "name": name}
            for code, name in list(industries.items())[:6]
        ],
        "selected_territories": list(dict.fromkeys(territories))[:12],
        "evidence_ids": [item.evidence_id for item in evidence],
    }


async def execute_research(
    session_factory: sessionmaker,
    settings: Settings,
    conversation_id: str,
    question: str,
    progress: ProgressCallback,
    *,
    strict_provider: bool = False,
) -> AgentExecution:
    started = time.perf_counter()
    model_settings = settings.model_copy(update={"llm_max_retries": 0})
    await progress("plan", 5, None)
    with session_factory() as session:
        conversation = session.get(Conversation, conversation_id)
        if conversation is None:
            raise ValueError("Диалог не найден")
        planned = await build_research_plan(
            session,
            model_settings,
            conversation,
            question,
            strict_provider=strict_provider,
        )
    provider = configured_provider(model_settings)
    if strict_provider:
        if (
            provider.name != "gnivc_qwen"
            or provider.model != "qwen3.6-35b"
        ):
            raise AgentUnavailableError(
                "Сертификация требует фактическую модель qwen3.6-35b."
            )
        if planned.planner_mode != "research-agent:gnivc_qwen":
            raise AgentUnavailableError(
                "Сертификация запрещает deterministic planner fallback."
            )
    await progress(
        "plan",
        15,
        {
            "research_plan": planned.research_plan.model_dump(mode="json"),
            "planner_mode": planned.planner_mode,
        },
    )
    completed: set[str] = set()
    evidence_by_operation: dict[str, EvidenceItem] = {}
    completed_results: list[tuple[ResearchOperation, dict, EvidenceItem]] = []
    journal: list[dict] = []
    tool_calls = 0

    def run_one(
        operation: ResearchOperation,
    ) -> tuple[ResearchOperation, dict, EvidenceItem]:
        with session_factory() as tool_session:
            result, evidence = execute_tool(tool_session, operation)
            return operation, result, evidence

    stage_by_tool = {
        "build_series": "получение динамики",
        "compare_entities": "сравнение срезов",
        "rank_entities": "анализ отраслей",
        "describe_distribution": "анализ распределения",
        "detect_outliers": "анализ регионов",
        "analyze_structure": "анализ структуры",
        "drill_down": "анализ регионов",
        "resolve_entities": "проверка справочников",
    }
    total = len(planned.research_plan.operations)
    while len(completed) < total:
        ready = [
            item
            for item in planned.research_plan.operations
            if item.id not in completed
            and all(dependency in completed for dependency in item.depends_on)
        ][:MAX_PARALLEL_TOOLS]
        if not ready:
            raise ValueError("Исследовательский план содержит циклическую зависимость")
        if tool_calls + len(ready) > MAX_TOOL_CALLS:
            raise ValueError("Достигнут лимит 12 аналитических инструментов")
        batch_progress = min(
            75, 15 + round(len(completed) / max(total, 1) * 60)
        )
        stages = list(
            dict.fromkeys(
                stage_by_tool.get(item.tool, "аналитический расчёт")
                for item in ready
            )
        )
        for stage in stages:
            await progress(
                stage,
                batch_progress,
                {
                    "operations": [
                        item.id
                        for item in ready
                        if stage_by_tool.get(
                            item.tool, "аналитический расчёт"
                        )
                        == stage
                    ]
                },
            )
        injected = [
            _inject_operation_dependencies(item, evidence_by_operation)
            for item in ready
        ]
        batch = await asyncio.gather(
            *(asyncio.to_thread(run_one, item) for item in injected),
            return_exceptions=True,
        )
        tool_calls += len(injected)
        successful = 0
        for operation, outcome in zip(injected, batch, strict=True):
            completed.add(operation.id)
            if isinstance(outcome, BaseException):
                journal.append(
                    {
                        "operation_id": operation.id,
                        "tool": operation.tool,
                        "status": "failed",
                        "error": str(outcome),
                    }
                )
                continue
            operation_result, result, evidence = outcome
            evidence_by_operation[operation.id] = evidence
            completed_results.append((operation_result, result, evidence))
            journal.append(
                {
                    "operation_id": operation.id,
                    "tool": operation.tool,
                    "status": "ready",
                    "evidence_id": evidence.evidence_id,
                    "rows": len(evidence.rows),
                }
            )
            successful += 1
        if not successful and not completed_results:
            error = journal[-1].get("error", "нет результата")
            raise ValueError(f"Аналитические инструменты не дали результата: {error}")

    evidence = [item[2] for item in completed_results]
    if sum(len(item.rows) for item in evidence) > MAX_MODEL_CONTEXT_ROWS:
        evidence = _limit_evidence_context(evidence)
    await progress("проверка выводов", 82, None)
    claims, hypotheses, synthesis_calls, synthesis_mode = await _synthesize_with_model(
        model_settings,
        question,
        evidence,
        strict_provider=(
            strict_provider
            or (
                model_settings.analytic_planner_mode == "model_first"
                and not isinstance(provider, DeterministicProvider)
            )
        ),
    )
    model_calls = planned.model_calls + synthesis_calls
    verify_claims(claims, evidence)
    verify_hypotheses(hypotheses, evidence)
    merged = _legacy_merge(completed_results)
    if planned.base_plan.intent != "catalog":
        merged["analysis_sections"] = [
            section
            for section in merged["analysis_sections"]
            if section.get("kind") != "resolved_entities"
        ]
    context = _selected_context(evidence)
    driving_evidence_ids = {
        item.evidence_id for item in evidence if item.tool != "build_series"
    }
    answer_claims = (
        [
            claim
            for claim in claims
            if any(
                evidence_id in driving_evidence_ids
                for evidence_id in claim.evidence_ids
            )
        ]
        if driving_evidence_ids
        else claims
    )
    answer = (
        " ".join(
            format_report_text_numbers(item.text)
            for item in (answer_claims or claims)[:4]
        )
        or "Исследование завершено; подтверждённых числовых утверждений нет."
    )
    result = {
        "status": "ready",
        "query_plan": planned.base_plan.model_dump(mode="json"),
        **merged,
        "answer": answer,
        "answer_mode": "analytic_agent_v2",
        "numbers_verified": True,
        "research_plan": planned.research_plan.model_dump(mode="json"),
        "evidence": [item.model_dump(mode="json") for item in evidence],
        "claims": [item.model_dump(mode="json") for item in claims],
        "hypotheses": [item.model_dump(mode="json") for item in hypotheses],
        "charts": [
            {
                "type": "line",
                "series_key": series.get("key"),
                "title": series.get("label"),
            }
            for series in merged["series"]
        ],
        "tables": (
            [
                {
                    "title": section.get("title"),
                    "kind": section.get("kind"),
                    "evidence_id": section.get("evidence_id"),
                    "rows": section.get("rows", [])[:50],
                }
                for section in merged["analysis_sections"]
            ]
            or [
                {
                    "title": item.title,
                    "kind": item.tool,
                    "evidence_id": item.evidence_id,
                    "rows": item.rows[:50],
                }
                for item in evidence
                if item.rows
                and (
                    planned.base_plan.intent == "catalog"
                    or item.tool != "resolve_entities"
                )
            ]
        ),
        "execution": {
            "model_calls": model_calls,
            "tool_calls": tool_calls,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "provider": provider.name,
            "model": provider.model,
            "fallback": (
                "fallback" in planned.planner_mode
                or "fallback" in synthesis_mode
            ),
            "retries": 0,
            "synthesis_mode": synthesis_mode,
            "tool_journal": journal,
            "semantic_query": (
                planned.semantic_query.model_dump(mode="json")
                if planned.semantic_query is not None
                else None
            ),
            "shadow_diff": planned.shadow_diff,
            "limits": {
                "model_calls": None,
                "planner_emergency_steps": (
                    model_settings.analytic_planner_emergency_max_steps
                ),
                "tool_calls": MAX_TOOL_CALLS,
                "timeout_seconds": None,
                "tool_rows": 50,
                "model_context_rows": MAX_MODEL_CONTEXT_ROWS,
            },
        },
        "context_update": context,
        "planner_mode": planned.planner_mode,
        "warnings": list(
            dict.fromkeys([*planned.warnings, *merged["warnings"]])
        ),
    }
    result = AnalysisResult.model_validate(result).model_dump(mode="json")
    await progress("проверка выводов", 96, {"numbers_verified": True})
    return AgentExecution(
        result=result,
        model_calls=model_calls,
        tool_calls=tool_calls,
        duration_ms=result["execution"]["duration_ms"],
    )


def _limit_evidence_context(
    evidence: list[EvidenceItem],
) -> list[EvidenceItem]:
    remaining = MAX_MODEL_CONTEXT_ROWS
    limited: list[EvidenceItem] = []
    for item in evidence:
        if remaining <= 0:
            break
        rows = item.rows[:remaining]
        remaining -= len(rows)
        limited.append(item.model_copy(update={"rows": rows}))
    return limited
