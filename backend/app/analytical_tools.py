from __future__ import annotations

import hashlib
import json
import statistics
from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .analytics_service import STRUCTURE_METRICS, _ranking_rows
from .catalog import resolved_metric_details
from .models import MetricDefinition, RowDefinition, Territory
from .query_service import available_periods, execute_plan
from .schemas import EvidenceItem, QueryPlan, ResearchOperation


MAX_TOOL_ROWS = 50
MAX_MODEL_CONTEXT_ROWS = 250


class AnalysisFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric_codes: list[str] = Field(min_length=1, max_length=8)
    enterprise_groups: list[
        Literal["all_msp", "micro", "small_and_medium_without_micro"]
    ] = Field(default_factory=lambda: ["all_msp"], min_length=1, max_length=3)
    territories: list[str] = Field(
        default_factory=lambda: ["Российская Федерация"],
        min_length=1,
        max_length=10,
    )
    row_codes: list[int] = Field(default_factory=lambda: [1010], min_length=1, max_length=50)
    periods: list[date] = Field(min_length=1, max_length=24)
    period_modes: list[
        Literal["cumulative_ytd", "standalone_quarter"]
    ] = Field(default_factory=list, max_length=24)
    period_labels: list[str] = Field(default_factory=list, max_length=24)


class ResolveEntitiesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric_query: str | None = Field(default=None, max_length=200)
    industry_query: str | None = Field(default=None, max_length=200)
    territory_query: str | None = Field(default=None, max_length=200)
    period_from: date | None = None
    period_to: date | None = None
    limit: int = Field(default=20, ge=1, le=50)


class SeriesInput(AnalysisFilters):
    pass


class CompareInput(AnalysisFilters):
    comparison_basis: Literal["previous_year_same_slice"] = (
        "previous_year_same_slice"
    )


class RankInput(AnalysisFilters):
    dimension: Literal["industry", "territory"]
    criterion: Literal["value", "growth_rate", "difference"] = "growth_rate"
    direction: Literal["both", "top", "bottom"] = "both"
    limit: int = Field(default=5, ge=1, le=25)


class DistributionInput(AnalysisFilters):
    dimension: Literal["industry", "territory"]
    measure: Literal["value", "growth_rate", "difference"] = "growth_rate"
    limit: int = Field(default=50, ge=10, le=50)


class OutliersInput(AnalysisFilters):
    dimension: Literal["territory"] = "territory"
    modes: list[Literal["value", "growth_rate", "composite"]] = Field(
        default_factory=lambda: ["value", "growth_rate", "composite"],
        min_length=1,
        max_length=3,
    )


class StructureInput(AnalysisFilters):
    structure_metric_codes: list[str] = Field(
        default_factory=lambda: list(STRUCTURE_METRICS),
        min_length=2,
        max_length=8,
    )


class DrillDownInput(AnalysisFilters):
    from_dimension: Literal["industry", "territory"] = "industry"
    to_dimension: Literal["territory", "industry"] = "territory"
    selected_row_codes: list[int] = Field(default_factory=list, max_length=12)
    selected_territories: list[str] = Field(default_factory=list, max_length=12)
    criterion: Literal["value", "growth_rate", "difference"] = "growth_rate"
    direction: Literal["top", "bottom", "both"] = "top"
    limit: int = Field(default=3, ge=1, le=10)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: Callable[[Session, BaseModel], dict]

    def json_schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_model.model_json_schema(),
        }


def _period_labels(filters: AnalysisFilters) -> list[str]:
    return filters.period_labels or [item.isoformat() for item in filters.periods]


def _period_modes(filters: AnalysisFilters) -> list[str]:
    return filters.period_modes or ["cumulative_ytd"] * len(filters.periods)


def _plan(
    filters: AnalysisFilters,
    *,
    intent: str,
    analysis_dimension: str | None = None,
    limit: int | None = None,
) -> QueryPlan:
    return QueryPlan(
        intent=intent,
        metric_codes=filters.metric_codes,
        enterprise_groups=filters.enterprise_groups,
        territories=filters.territories,
        row_codes=filters.row_codes,
        periods=filters.periods,
        period_modes=_period_modes(filters),
        period_labels=_period_labels(filters),
        operations=["value", "difference", "growth_rate", "sources"],
        group_by=[analysis_dimension] if analysis_dimension else [],
        analysis_dimension=analysis_dimension,
        comparison_basis="previous_year_same_slice",
        limit=limit,
    )


def _resolve(session: Session, raw: BaseModel) -> dict:
    args = ResolveEntitiesInput.model_validate(raw)
    metrics = session.scalars(
        select(MetricDefinition).order_by(MetricDefinition.report_column_number)
    ).all()
    industries = session.scalars(
        select(RowDefinition).order_by(RowDefinition.base_row_code)
    ).all()
    territories = session.scalars(
        select(Territory).order_by(Territory.name_ru)
    ).all()

    def contains(candidate: str, query: str | None) -> bool:
        return not query or query.casefold() in candidate.casefold()

    metric_rows = [
        {
            "kind": "metric",
            "code": item.semantic_code,
            "name": item.name_ru,
            "unit": item.unit,
        }
        for item in metrics
        if contains(
            " ".join([item.name_ru, *(item.aliases_json or [])]),
            args.metric_query,
        )
    ]
    industry_rows = [
        {
            "kind": "industry",
            "code": item.base_row_code,
            "name": item.name_ru,
            "okved": item.okved_text,
        }
        for item in industries
        if contains(
            " ".join(
                filter(
                    None,
                    [
                        item.name_ru,
                        item.okved_text,
                        *(item.aliases_json or []),
                    ],
                )
            ),
            args.industry_query,
        )
    ]
    territory_rows = [
        {
            "kind": "territory",
            "code": item.official_code or f"territory-{item.id}",
            "name": item.name_ru,
            "territory_type": item.territory_type,
        }
        for item in territories
        if contains(item.name_ru, args.territory_query)
    ]
    period_rows = [
        {
            "kind": "period",
            "code": item.isoformat(),
            "name": item.isoformat(),
        }
        for item in available_periods(session)
        if (args.period_from is None or item >= args.period_from)
        and (args.period_to is None or item <= args.period_to)
    ]
    per_kind = max(1, args.limit // 4)
    rows = [
        *metric_rows[:per_kind],
        *industry_rows[:per_kind],
        *territory_rows[:per_kind],
        *period_rows[-per_kind:],
    ][: args.limit]
    return {
        "status": "ready",
        "answer": "Сущности разрешены по внутренним справочникам.",
        "facts": [],
        "calculations": [],
        "series": [],
        "analysis_sections": [
            {
                "kind": "resolved_entities",
                "title": "Разрешённые сущности",
                "rows": rows,
                "methodology": "Поиск только по утверждённым справочникам.",
            }
        ],
        "visualization": None,
        "warnings": [],
        "numbers_verified": True,
        "resolved_metrics": [],
    }


def _series(session: Session, raw: BaseModel) -> dict:
    args = SeriesInput.model_validate(raw)
    return execute_plan(session, _plan(args, intent="trend"))


def _compare(session: Session, raw: BaseModel) -> dict:
    args = CompareInput.model_validate(raw)
    if len(args.periods) > 1:
        return execute_plan(session, _plan(args, intent="compare"))

    current = args.periods[0]
    previous = previous_year_period(current)
    modes = _period_modes(args)
    labels = _period_labels(args)
    current_label = labels[0]
    previous_label = (
        current_label.replace(str(current.year), str(previous.year))
        if str(current.year) in current_label
        else previous.isoformat()
    )
    comparison_args = args.model_copy(
        update={
            "periods": [previous, current],
            "period_modes": [modes[0], modes[0]],
            "period_labels": [previous_label, current_label],
        }
    )
    try:
        return execute_plan(
            session,
            _plan(comparison_args, intent="compare"),
        )
    except ValueError:
        # A single current value is still preferable to the old "overview"
        # fallback, which silently replaced the territory and row with RF/1010.
        return execute_plan(session, _plan(args, intent="value"))


def _rank(session: Session, raw: BaseModel) -> dict:
    args = RankInput.model_validate(raw)
    intent = {
        "value": "rank",
        "growth_rate": "rank_growth",
        "difference": "rank_difference",
    }[args.criterion]
    plan = _plan(
        args,
        intent=intent,
        analysis_dimension=args.dimension,
        limit=args.limit,
    )
    if args.criterion in {"value", "growth_rate"}:
        plan.ranking_criteria = [args.criterion]
    plan.ranking_direction = args.direction
    return execute_plan(session, plan)


def _distribution(session: Session, raw: BaseModel) -> dict:
    args = DistributionInput.model_validate(raw)
    plan = _plan(
        args,
        intent={
            "value": "rank",
            "growth_rate": "rank_growth",
            "difference": "rank_difference",
        }[args.measure],
        analysis_dimension=args.dimension,
        limit=args.limit,
    )
    plan.ranking_criteria = [args.measure]
    comparisons, _facts = _ranking_rows(session, plan)
    key = {
        "value": "current",
        "growth_rate": "growth_rate",
        "difference": "difference",
    }[args.measure]
    by_entity: dict[str, Decimal] = {}
    for row in comparisons:
        if args.measure == "growth_rate" and row.get("low_base"):
            continue
        try:
            by_entity[str(row.get("entity"))] = Decimal(str(row.get(key)))
        except (InvalidOperation, TypeError):
            continue
    values = list(by_entity.values())
    if not values:
        raise ValueError("Нет значений для расчёта распределения")
    ordered = sorted(values)

    def quantile(fraction: float) -> Decimal:
        return ordered[round((len(ordered) - 1) * fraction)]

    summary = {
        "entity": args.dimension,
        "count": len(values),
        "minimum": str(min(values)),
        "q1": str(quantile(0.25)),
        "median": str(Decimal(str(statistics.median(values)))),
        # Stable order keeps Decimal context rounding reproducible and lets an
        # independently sorted oracle compare the final digit exactly.
        "mean": str(sum(ordered, Decimal("0")) / len(ordered)),
        "q3": str(quantile(0.75)),
        "maximum": str(max(values)),
        "measure": args.measure,
        "unit": "%" if args.measure == "growth_rate" else None,
    }
    return {
        "status": "ready",
        "answer": "Рассчитано распределение по полному набору сопоставимых агрегатов.",
        "facts": [],
        "calculations": [],
        "series": [],
        "analysis_sections": [
            {
                "kind": "distribution",
                "title": "Распределение показателя",
                "rows": [summary],
                "methodology": (
                    "Описательная статистика по полному набору проверенных "
                    "сопоставимых агрегатов: минимум, квартили, медиана, "
                    "среднее и максимум; малая база исключена тем же "
                    "правилом, что и в рейтинге роста."
                ),
            }
        ],
        "visualization": None,
        "warnings": [],
        "numbers_verified": True,
        "resolved_metrics": resolved_metric_details(
            session, args.metric_codes
        ),
    }


def _outliers(session: Session, raw: BaseModel) -> dict:
    args = OutliersInput.model_validate(raw)
    plan = _plan(
        args,
        intent="outliers",
        analysis_dimension="territory",
        limit=10,
    )
    plan.outlier_modes = args.modes
    return execute_plan(session, plan)


def _structure(session: Session, raw: BaseModel) -> dict:
    args = StructureInput.model_validate(raw)
    component_filter = SeriesInput(
        **args.model_dump(exclude={"structure_metric_codes", "metric_codes"}),
        metric_codes=args.structure_metric_codes,
    )
    result = execute_plan(
        session,
        _plan(
            component_filter,
            intent="compare" if len(args.periods) > 1 else "value",
        ),
    )
    grouped: dict[tuple[str, str], list[dict]] = {}
    for fact in result.get("facts", []):
        grouped.setdefault(
            (str(fact.get("period_end")), str(fact.get("territory"))), []
        ).append(fact)
    rows = []
    for (period, territory), facts in grouped.items():
        total = sum(
            (Decimal(str(item["value"])) for item in facts), Decimal("0")
        )
        for fact in facts:
            value = Decimal(str(fact["value"]))
            rows.append(
                {
                    "entity": fact["metric"],
                    "territory": territory,
                    "period": period,
                    "value": str(value),
                    "share": str(value / total * 100) if total else None,
                    "unit": fact["unit"],
                    "source_fact_ids": [fact["fact_id"]],
                }
            )
    result["analysis_sections"] = [
        {
            "kind": "structure",
            "title": "Структура поступлений",
            "rows": rows[:MAX_TOOL_ROWS],
            "methodology": (
                "Доля компонента в сумме выбранных компонентов за один срез."
            ),
        }
    ]
    result["answer"] = "Структура рассчитана по проверенным компонентам."
    return result


def _drill_down(session: Session, raw: BaseModel) -> dict:
    args = DrillDownInput.model_validate(raw)
    row_codes = args.selected_row_codes or args.row_codes
    territories = args.selected_territories or args.territories
    if args.from_dimension == "industry" and args.to_dimension == "territory":
        plan = QueryPlan(
            intent="regional_industry_rank_growth",
            metric_codes=args.metric_codes,
            enterprise_groups=args.enterprise_groups,
            territories=territories,
            row_codes=row_codes,
            periods=args.periods,
            period_modes=_period_modes(args),
            period_labels=_period_labels(args),
            operations=["value", "difference", "growth_rate", "sources"],
            group_by=["industry", "territory"],
            analysis_dimension="territory",
            ranking_criteria=["growth_rate"],
            ranking_direction=args.direction,
            comparison_basis="previous_year_same_slice",
            limit=args.limit,
        )
        return execute_plan(session, plan)
    rank_args = RankInput(
        metric_codes=args.metric_codes,
        enterprise_groups=args.enterprise_groups,
        territories=territories,
        row_codes=row_codes,
        periods=args.periods,
        period_modes=args.period_modes,
        period_labels=args.period_labels,
        dimension=args.to_dimension,
        criterion=args.criterion,
        direction=args.direction,
        limit=args.limit,
    )
    return _rank(session, rank_args)


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "resolve_entities",
        "Resolve metrics, industries, territories and periods from approved catalogs.",
        ResolveEntitiesInput,
        _resolve,
    ),
    ToolSpec(
        "build_series",
        "Build a comparable time series for typed filters.",
        SeriesInput,
        _series,
    ),
    ToolSpec(
        "compare_entities",
        "Compare periods or entities using deterministic calculations.",
        CompareInput,
        _compare,
    ),
    ToolSpec(
        "rank_entities",
        "Rank industries or territories by level, growth or difference.",
        RankInput,
        _rank,
    ),
    ToolSpec(
        "describe_distribution",
        "Calculate descriptive distribution statistics over aggregates.",
        DistributionInput,
        _distribution,
    ),
    ToolSpec(
        "detect_outliers",
        "Detect regional level, growth and composite outliers.",
        OutliersInput,
        _outliers,
    ),
    ToolSpec(
        "analyze_structure",
        "Calculate the structure of selected aggregate components.",
        StructureInput,
        _structure,
    ),
    ToolSpec(
        "drill_down",
        "Continue from selected industries into regions or the reverse.",
        DrillDownInput,
        _drill_down,
    ),
)

TOOL_REGISTRY = {item.name: item for item in TOOLS}


def tool_schemas() -> list[dict]:
    return [item.json_schema() for item in TOOLS]


def execute_tool(
    session: Session,
    operation: ResearchOperation,
) -> tuple[dict, EvidenceItem]:
    spec = TOOL_REGISTRY.get(operation.tool)
    if spec is None:
        raise ValueError(f"Инструмент {operation.tool} не разрешён")
    arguments = spec.input_model.model_validate(operation.arguments)
    result = spec.handler(session, arguments)
    _truncate_result(result)
    evidence = evidence_from_result(operation, result)
    return result, evidence


def _truncate_result(result: dict) -> None:
    remaining = MAX_TOOL_ROWS
    for section in result.get("analysis_sections", []):
        rows = section.get("rows") or []
        section["rows"] = rows[:remaining]
        remaining -= len(section["rows"])
        if remaining <= 0:
            break
    result["analysis_sections"] = [
        section
        for section in result.get("analysis_sections", [])
        if section.get("rows")
    ]
    result["facts"] = (result.get("facts") or [])[:MAX_TOOL_ROWS]
    for series in result.get("series", []):
        series["points"] = (series.get("points") or [])[:MAX_TOOL_ROWS]


def _anon_source_id(fact: dict) -> str:
    raw = "|".join(
        [
            str(fact.get("fact_id", "")),
            str(fact.get("period_end", "")),
            str(fact.get("metric_code", "")),
        ]
    )
    return "src-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _numeric_fields(row: dict) -> dict:
    keys = {
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
    return {
        key: value
        for key, value in row.items()
        if key in keys and value is not None
    }


def evidence_from_result(
    operation: ResearchOperation,
    result: dict,
) -> EvidenceItem:
    facts = [
        item for item in result.get("facts", []) if isinstance(item, dict)
    ]
    fact_sources = {
        str(item.get("fact_id")): _anon_source_id(item) for item in facts
    }
    rows: list[dict] = []
    for series in result.get("series", []):
        points = series.get("points", [])
        for index, point in enumerate(points):
            row = {
                "entity": series.get("label"),
                "metric_code": series.get("metric_code"),
                "territory": series.get("territory"),
                "row_code": series.get("row_code"),
                "period": point.get("period_end"),
                "period_label": point.get("label"),
                "value": point.get("value"),
                "unit": series.get("unit"),
                "source_ids": [
                    fact_sources.get(
                        str(point.get("fact_id")),
                        "src-derived",
                    )
                ],
            }
            if index == len(points) - 1 and len(points) >= 2:
                previous_value = Decimal(str(points[0].get("value")))
                current_value = Decimal(str(point.get("value")))
                difference = current_value - previous_value
                row.update(
                    {
                        "previous": points[0].get("value"),
                        "difference": str(difference),
                        "growth_rate": (
                            str(
                                difference
                                / abs(previous_value)
                                * Decimal("100")
                            )
                            if previous_value != 0
                            else None
                        ),
                    }
                )
            rows.append(row)
    for section in result.get("analysis_sections", []):
        methodology = section.get("methodology")
        for row in section.get("rows", []):
            if not isinstance(row, dict):
                continue
            source_ids = [
                fact_sources.get(str(fact_id), "src-derived")
                for fact_id in row.get("source_fact_ids", [])
            ]
            rows.append(
                {
                    "section_kind": section.get("kind"),
                    "entity": row.get("entity") or row.get("name"),
                    "row_code": row.get("row_code") or row.get("code"),
                    "period": row.get("period"),
                    "territory": row.get("territory") or row.get("region"),
                    "industry": row.get("industry"),
                    "region": row.get("region"),
                    **_numeric_fields(row),
                    "unit": row.get("unit"),
                    "source_ids": list(dict.fromkeys(source_ids)),
                    "methodology": methodology,
                }
            )
    if not rows:
        for fact in facts:
            rows.append(
                {
                    "entity": fact.get("row_name") or fact.get("territory"),
                    "metric_code": fact.get("metric_code"),
                    "territory": fact.get("territory"),
                    "row_code": fact.get("row_code"),
                    "period": fact.get("period_end"),
                    "value": fact.get("value"),
                    "unit": fact.get("unit"),
                    "source_ids": [fact_sources[str(fact.get("fact_id"))]],
                }
            )
    rows = rows[:MAX_TOOL_ROWS]
    periods = sorted(
        {
            str(value)
            for row in rows
            for value in [row.get("period")]
            if value
        }
        | {
            str(item.get("period_end"))
            for item in facts
            if item.get("period_end")
        }
    )
    units = {
        str(row.get("unit")) for row in rows if row.get("unit")
    } | {str(item.get("unit")) for item in facts if item.get("unit")}
    methodologies = [
        str(section.get("methodology"))
        for section in result.get("analysis_sections", [])
        if section.get("methodology")
    ]
    if not methodologies:
        methodologies = [
            "Детерминированный расчёт по типизированным фильтрам."
        ]
    payload = json.dumps(
        {
            "operation": operation.model_dump(mode="json"),
            "rows": rows,
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    evidence_id = "ev-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return EvidenceItem(
        evidence_id=evidence_id,
        operation_id=operation.id,
        tool=operation.tool,
        title=operation.goal,
        periods=periods,
        unit=next(iter(units)) if len(units) == 1 else None,
        methodology=" ".join(dict.fromkeys(methodologies)),
        sources=sorted(
            {
                source_id
                for row in rows
                for source_id in row.get("source_ids", [])
                if source_id
            }
        ),
        rows=rows,
        verified=bool(result.get("numbers_verified", True)),
    )


def previous_year_period(period: date) -> date:
    year = period.year - 1
    return date(year, period.month, monthrange(year, period.month)[1])
