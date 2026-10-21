from __future__ import annotations

import math
import re
import statistics
from calendar import monthrange
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from .calculations import json_ready, ru_number
from .catalog import resolved_metric_details
from .models import MetricDefinition, MspFact, RowDefinition, SourceFile
from .query_service import _fact, _source_reference
from .regional import (
    RegionalValue,
    read_regional_industry_values,
    read_regional_values,
    territory_key,
)
from .schemas import QueryPlan


STRUCTURE_METRICS = (
    "receipts_profit_tax_total",
    "receipts_ndfl_total",
    "receipts_vat_total",
    "receipts_special_regimes_total",
)


def _previous_period(period: date) -> date:
    year = period.year - 1
    return date(year, period.month, monthrange(year, period.month)[1])


def _regional_comparable_period(
    session: Session, requested: date
) -> date:
    available = set(
        session.scalars(
            select(SourceFile.period_end)
            .where(
                SourceFile.workbook_kind == "regional_section_i",
                SourceFile.period_end.is_not(None),
                SourceFile.period_end <= requested,
                SourceFile.status.in_(["valid", "valid_with_warnings"]),
            )
            .distinct()
        ).all()
    )
    comparable = [
        period
        for period in available
        if period is not None and _previous_period(period) in available
    ]
    if not comparable:
        raise ValueError(
            "Нет двух сопоставимых региональных срезов год к году"
        )
    return max(comparable)


def _metric_fact(
    session: Session,
    plan: QueryPlan,
    period: date,
    territory: str,
    row_code: int,
    regional_cache: dict,
) -> tuple[dict, list[dict]]:
    components = [
        _fact(
            session,
            period,
            metric_code,
            plan.enterprise_groups[0],
            row_code,
            "cumulative_ytd",
            period.isoformat(),
            territory,
            regional_cache,
        )
        for metric_code in plan.metric_codes
    ]
    if set(plan.metric_codes) != {
        "receipts_budget_total",
        "receipts_insurance_total",
    }:
        return components[0], components
    value = sum((Decimal(item["value"]) for item in components), Decimal("0"))
    combined = {
        **components[0],
        "fact_id": "derived-total-"
        + "-".join(str(item["fact_id"]) for item in components),
        "metric_code": "msp_total",
        "metric": "Поступления от МСП, всего",
        "value": str(value),
        "source": {
            "file": "Производный показатель",
            "formula": "Поступления в бюджет + страховые взносы",
            "lineage": [item["source"] for item in components],
            "read_only": True,
        },
    }
    return combined, components


def _percentile(values: list[Decimal], fraction: Decimal) -> Decimal:
    ordered = sorted(values)
    if not ordered:
        return Decimal("0")
    index = int((len(ordered) - 1) * float(fraction))
    return ordered[index]


def _comparison_row(
    entity: str,
    current: dict,
    previous: dict,
    *,
    low_base: bool = False,
    row_code: int | None = None,
) -> dict:
    current_value = Decimal(current["value"])
    previous_value = Decimal(previous["value"])
    growth = (
        (current_value - previous_value)
        / abs(previous_value)
        * Decimal("100")
        if previous_value > 0
        else None
    )
    return {
        "entity": entity,
        "current": str(current_value),
        "previous": str(previous_value),
        "difference": str(current_value - previous_value),
        "growth_rate": str(growth) if growth is not None else None,
        "low_base": low_base,
        "current_period": current["period_end"],
        "previous_period": previous["period_end"],
        "unit": current["unit"],
        "source_fact_ids": [previous["fact_id"], current["fact_id"]],
        **({"row_code": row_code} if row_code is not None else {}),
    }


def _regional_names(
    session: Session,
    plan: QueryPlan,
    period: date,
    regional_cache: dict,
) -> list[str]:
    metric = session.scalar(
        select(MetricDefinition).where(
            MetricDefinition.semantic_code == plan.metric_codes[0]
        )
    )
    if metric is None or metric.report_column_number is None:
        raise ValueError("Показатель для регионального анализа не найден")
    section = "II" if plan.enterprise_groups[0] == "micro" else "I"
    key = (period, section, 1010, metric.report_column_number)
    values: list[RegionalValue] = read_regional_values(
        session,
        period=period,
        section=section,
        row_code=1010,
        report_column=metric.report_column_number,
    )
    regional_cache[key] = values
    return sorted(
        {
            item.territory
            for item in values
            if item.territory_type == "region"
        }
    )


def _national_industry_ranking_rows(
    session: Session,
    plan: QueryPlan,
    definitions: list[RowDefinition],
    current_period: date,
    previous_period: date,
) -> tuple[list[dict], list[dict]]:
    """Load all national industry facts for two periods in one SQL pass."""

    group = plan.enterprise_groups[0]
    sections = (
        ["I", "II"]
        if group == "small_and_medium_without_micro"
        else ["II"]
        if group == "micro"
        else ["I"]
    )
    row_codes = [item.base_row_code for item in definitions]
    statement = (
        select(MspFact)
        .join(MetricDefinition, MetricDefinition.id == MspFact.metric_id)
        .join(RowDefinition, RowDefinition.id == MspFact.row_definition_id)
        .join(SourceFile, SourceFile.id == MspFact.source_file_id)
        .where(
            MetricDefinition.semantic_code.in_(plan.metric_codes),
            RowDefinition.base_row_code.in_(row_codes),
            MspFact.period_end.in_([previous_period, current_period]),
            MspFact.section.in_(sections),
            SourceFile.status.in_(["valid", "valid_with_warnings"]),
        )
        .options(
            joinedload(MspFact.source_file),
            joinedload(MspFact.metric),
            joinedload(MspFact.row_definition),
        )
        .order_by(SourceFile.id.desc())
    )
    loaded = session.scalars(statement).unique().all()
    latest: dict[tuple[str, int, date, str], MspFact] = {}
    for fact in loaded:
        latest.setdefault(
            (
                fact.metric.semantic_code,
                fact.row_definition.base_row_code,
                fact.period_end,
                fact.section,
            ),
            fact,
        )

    def direct_fact(
        metric_code: str,
        row: RowDefinition,
        period: date,
        section: str,
    ) -> dict | None:
        fact = latest.get((metric_code, row.base_row_code, period, section))
        if fact is None:
            return None
        return {
            "fact_id": f"fact-{fact.id}",
            "metric_code": metric_code,
            "metric": fact.metric.name_ru,
            "value": str(Decimal(fact.value)),
            "unit": fact.metric.unit,
            "period_end": period.isoformat(),
            "period_mode": "cumulative_ytd",
            "period_label": period.isoformat(),
            "section": section,
            "enterprise_group": group,
            "territory": "Российская Федерация",
            "row_code": row.base_row_code,
            "row_name": re.split(
                r"\s*\(стр\.?",
                row.name_ru,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0].strip(),
            "okved": row.okved_text,
            "source": _source_reference(fact),
        }

    def metric_fact(
        metric_code: str,
        row: RowDefinition,
        period: date,
    ) -> dict | None:
        if group != "small_and_medium_without_micro":
            return direct_fact(metric_code, row, period, sections[0])
        all_msp = direct_fact(metric_code, row, period, "I")
        micro = direct_fact(metric_code, row, period, "II")
        if all_msp is None or micro is None:
            return None
        return {
            **all_msp,
            "fact_id": f"derived-{all_msp['fact_id']}-{micro['fact_id']}",
            "value": str(
                Decimal(all_msp["value"]) - Decimal(micro["value"])
            ),
            "section": "III",
            "enterprise_group": group,
            "source": {
                "file": "Производный показатель",
                "formula": "Раздел III = Раздел I − Раздел II",
                "lineage": [all_msp["source"], micro["source"]],
                "read_only": True,
            },
        }

    def combined_fact(
        row: RowDefinition,
        period: date,
    ) -> tuple[dict, list[dict]] | None:
        components = [
            metric_fact(metric_code, row, period)
            for metric_code in plan.metric_codes
        ]
        if any(item is None for item in components):
            return None
        concrete = [item for item in components if item is not None]
        if set(plan.metric_codes) != {
            "receipts_budget_total",
            "receipts_insurance_total",
        }:
            return concrete[0], concrete
        combined = {
            **concrete[0],
            "fact_id": "derived-total-"
            + "-".join(str(item["fact_id"]) for item in concrete),
            "metric_code": "msp_total",
            "metric": "Поступления от МСП, всего",
            "value": str(
                sum(
                    (Decimal(item["value"]) for item in concrete),
                    Decimal("0"),
                )
            ),
            "source": {
                "file": "Производный показатель",
                "formula": "Поступления в бюджет + страховые взносы",
                "lineage": [item["source"] for item in concrete],
                "read_only": True,
            },
        }
        return combined, concrete

    comparisons: list[dict] = []
    all_facts: list[dict] = []
    for row in definitions:
        current_result = combined_fact(row, current_period)
        previous_result = combined_fact(row, previous_period)
        if current_result is None or previous_result is None:
            continue
        current, current_parts = current_result
        previous, previous_parts = previous_result
        all_facts.extend(
            [previous, current, *previous_parts, *current_parts]
        )
        comparison = _comparison_row(
            row.name_ru,
            current,
            previous,
            row_code=row.base_row_code,
        )
        comparison["territory"] = "Российская Федерация"
        comparisons.append(comparison)
    if not comparisons:
        raise ValueError("Нет сопоставимых значений для рейтинга")
    positive_bases = [
        Decimal(item["previous"])
        for item in comparisons
        if Decimal(item["previous"]) > 0
    ]
    base_floor = _percentile(positive_bases, Decimal("0.10"))
    for item in comparisons:
        item["low_base"] = Decimal(item["previous"]) < base_floor
    return comparisons, all_facts


def _regional_industry_ranking_rows(
    session: Session,
    plan: QueryPlan,
    definitions: list[RowDefinition],
    territory: str,
    current_period: date,
    previous_period: date,
) -> tuple[list[dict], list[dict]]:
    """Load one region across all industry sheets in bounded workbook passes."""

    group = plan.enterprise_groups[0]
    sections = (
        ["I", "II"]
        if group == "small_and_medium_without_micro"
        else ["II"]
        if group == "micro"
        else ["I"]
    )
    metrics = {
        item.semantic_code: item
        for item in session.scalars(
            select(MetricDefinition).where(
                MetricDefinition.semantic_code.in_(plan.metric_codes)
            )
        ).all()
    }
    if len(metrics) != len(set(plan.metric_codes)):
        raise ValueError("Показатель для регионального рейтинга не найден")
    row_codes = [item.base_row_code for item in definitions]
    loaded: dict[tuple[str, date, str], dict[int, RegionalValue]] = {}
    for metric_code in plan.metric_codes:
        metric = metrics[metric_code]
        if metric.report_column_number is None:
            raise ValueError("У показателя не определён номер графы")
        for period in (previous_period, current_period):
            for section in sections:
                loaded[(metric_code, period, section)] = (
                    read_regional_industry_values(
                        session,
                        period=period,
                        section=section,
                        row_codes=row_codes,
                        report_column=metric.report_column_number,
                        territory=territory,
                    )
                )

    def direct_fact(
        metric_code: str,
        row: RowDefinition,
        period: date,
        section: str,
    ) -> dict | None:
        item = loaded[(metric_code, period, section)].get(
            row.base_row_code
        )
        if item is None:
            return None
        metric = metrics[metric_code]
        return {
            "fact_id": (
                f"regional-{section}-{period.isoformat()}-"
                f"{row.base_row_code}-{metric.report_column_number}-"
                f"{territory_key(territory)}"
            ),
            "metric_code": metric_code,
            "metric": metric.name_ru,
            "value": str(item.value),
            "unit": metric.unit,
            "period_end": period.isoformat(),
            "period_mode": "cumulative_ytd",
            "period_label": period.isoformat(),
            "section": section,
            "enterprise_group": group,
            "territory": item.territory,
            "row_code": row.base_row_code,
            "row_name": re.split(
                r"\s*\(стр\.?",
                row.name_ru,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0].strip(),
            "okved": row.okved_text,
            "source": item.source,
        }

    def metric_fact(
        metric_code: str,
        row: RowDefinition,
        period: date,
    ) -> dict | None:
        if group != "small_and_medium_without_micro":
            return direct_fact(metric_code, row, period, sections[0])
        all_msp = direct_fact(metric_code, row, period, "I")
        micro = direct_fact(metric_code, row, period, "II")
        if all_msp is None or micro is None:
            return None
        return {
            **all_msp,
            "fact_id": f"derived-{all_msp['fact_id']}-{micro['fact_id']}",
            "value": str(
                Decimal(all_msp["value"]) - Decimal(micro["value"])
            ),
            "section": "III",
            "enterprise_group": group,
            "source": {
                "file": "Производный показатель",
                "formula": "Раздел III = Раздел I − Раздел II",
                "lineage": [all_msp["source"], micro["source"]],
                "read_only": True,
            },
        }

    def combined_fact(
        row: RowDefinition,
        period: date,
    ) -> tuple[dict, list[dict]] | None:
        components = [
            metric_fact(metric_code, row, period)
            for metric_code in plan.metric_codes
        ]
        if any(item is None for item in components):
            return None
        concrete = [item for item in components if item is not None]
        if set(plan.metric_codes) != {
            "receipts_budget_total",
            "receipts_insurance_total",
        }:
            return concrete[0], concrete
        combined = {
            **concrete[0],
            "fact_id": "derived-total-"
            + "-".join(str(item["fact_id"]) for item in concrete),
            "metric_code": "msp_total",
            "metric": "Поступления от МСП, всего",
            "value": str(
                sum(
                    (Decimal(item["value"]) for item in concrete),
                    Decimal("0"),
                )
            ),
            "source": {
                "file": "Производный показатель",
                "formula": "Поступления в бюджет + страховые взносы",
                "lineage": [item["source"] for item in concrete],
                "read_only": True,
            },
        }
        return combined, concrete

    comparisons: list[dict] = []
    all_facts: list[dict] = []
    for row in definitions:
        current_result = combined_fact(row, current_period)
        previous_result = combined_fact(row, previous_period)
        if current_result is None or previous_result is None:
            continue
        current, current_parts = current_result
        previous, previous_parts = previous_result
        all_facts.extend([previous, current, *previous_parts, *current_parts])
        comparison = _comparison_row(
            row.name_ru,
            current,
            previous,
            row_code=row.base_row_code,
        )
        comparison["territory"] = territory
        comparisons.append(comparison)
    if not comparisons:
        raise ValueError("Нет сопоставимых значений для рейтинга")
    positive_bases = [
        Decimal(item["previous"])
        for item in comparisons
        if Decimal(item["previous"]) > 0
    ]
    base_floor = _percentile(positive_bases, Decimal("0.10"))
    for item in comparisons:
        item["low_base"] = Decimal(item["previous"]) < base_floor
    return comparisons, all_facts


def _ranking_rows(
    session: Session, plan: QueryPlan
) -> tuple[list[dict], list[dict]]:
    current_period = plan.periods[-1]
    previous_period = _previous_period(current_period)
    regional_cache: dict = {}
    all_facts: list[dict] = []
    comparisons: list[dict] = []
    if plan.analysis_dimension == "territory":
        entities = [
            (name, 1010, name)
            for name in _regional_names(
                session, plan, current_period, regional_cache
            )
        ]
    else:
        territory = next(
            (
                item
                for item in plan.territories
                if item != "Российская Федерация"
            ),
            plan.territories[0],
        )
        statement = select(RowDefinition).order_by(
            RowDefinition.base_row_code
        )
        if plan.row_codes and plan.row_codes != [1010]:
            statement = statement.where(
                RowDefinition.base_row_code.in_(plan.row_codes)
            )
        else:
            statement = statement.where(
                RowDefinition.base_row_code != 1010
            )
        definitions = session.scalars(statement).all()
        if territory == "Российская Федерация":
            return _national_industry_ranking_rows(
                session,
                plan,
                definitions,
                current_period,
                previous_period,
            )
        return _regional_industry_ranking_rows(
            session,
            plan,
            definitions,
            territory,
            current_period,
            previous_period,
        )
    for label, row_code, territory in entities:
        try:
            current, current_parts = _metric_fact(
                session,
                plan,
                current_period,
                territory,
                row_code,
                regional_cache,
            )
            previous, previous_parts = _metric_fact(
                session,
                plan,
                previous_period,
                territory,
                row_code,
                regional_cache,
            )
        except ValueError:
            continue
        all_facts.extend(
            [previous, current, *previous_parts, *current_parts]
        )
        comparison = _comparison_row(
            label,
            current,
            previous,
            row_code=row_code,
        )
        comparison["territory"] = territory
        comparisons.append(comparison)
    if not comparisons:
        raise ValueError("Нет сопоставимых значений для рейтинга")
    positive_bases = [
        Decimal(item["previous"])
        for item in comparisons
        if Decimal(item["previous"]) > 0
    ]
    base_floor = _percentile(positive_bases, Decimal("0.10"))
    for item in comparisons:
        item["low_base"] = Decimal(item["previous"]) < base_floor
    return comparisons, all_facts


def _rank_result(session: Session, plan: QueryPlan) -> dict:
    comparisons, all_facts = _ranking_rows(session, plan)
    limit = plan.limit or 5
    sections: list[dict] = []
    criteria = plan.ranking_criteria or ["value", "growth_rate"]
    direction = plan.ranking_direction
    if "value" in criteria:
        ordered = sorted(
            comparisons,
            key=lambda item: Decimal(item["current"]),
            reverse=True,
        )
        if direction in {"top", "both"}:
            sections.append(
                {
                    "kind": "ranking_value",
                    "title": "Лидеры по текущему абсолютному значению",
                    "rows": ordered[:limit],
                    "methodology": (
                        "Ранжирование по подтверждённому значению последнего "
                        "доступного накопительного среза."
                    ),
                }
            )
        if direction in {"bottom", "both"}:
            sections.append(
                {
                    "kind": "ranking_value_worst",
                    "title": "Минимальные текущие абсолютные значения",
                    "rows": list(reversed(ordered[-limit:])),
                    "methodology": (
                        "Минимальные подтверждённые значения последнего "
                        "доступного накопительного среза."
                    ),
                }
            )
    if "growth_rate" in criteria:
        eligible = [
            item
            for item in comparisons
            if item["growth_rate"] is not None and not item["low_base"]
        ]
        ordered = sorted(
            eligible,
            key=lambda item: Decimal(item["growth_rate"]),
            reverse=True,
        )
        if direction in {"top", "both"}:
            sections.append(
                {
                    "kind": "ranking_growth",
                    "title": "Лидеры по росту",
                    "rows": ordered[:limit],
                    "methodology": (
                        "Изменение к тому же накопительному срезу предыдущего "
                        "года. Значения ниже 10-го процентиля базы исключены "
                        "из основного рейтинга."
                    ),
                }
            )
        if direction in {"bottom", "both"}:
            sections.append(
                {
                    "kind": "ranking_growth_worst",
                    "title": "Худшая динамика",
                    "rows": list(reversed(ordered[-limit:])),
                    "methodology": (
                        "Минимальный темп роста к тому же накопительному "
                        "срезу предыдущего года после исключения малой базы."
                    ),
                }
            )
        if direction in {"top", "both"}:
            low_base = sorted(
                [
                    item
                    for item in comparisons
                    if item["growth_rate"] is not None and item["low_base"]
                ],
                key=lambda item: Decimal(item["growth_rate"]),
                reverse=True,
            )[:limit]
            if low_base:
                sections.append(
                    {
                        "kind": "ranking_low_base",
                        "title": "Быстрый рост с малой базы",
                        "rows": low_base,
                        "methodology": (
                            "Показано отдельно, чтобы малая база не искажала "
                            "основной рейтинг роста."
                        ),
                    }
                )
    leaders = []
    for section in sections[:2]:
        if section["rows"]:
            row = section["rows"][0]
            leaders.append(
                f"{section['title'].lower()} — {row['entity']}"
            )
    answer = (
        "По проверенным данным: "
        + "; ".join(leaders)
        + ". Абсолютный уровень и рост показаны отдельно."
    )
    selected_ids = {
        fact_id
        for section in sections
        for row in section["rows"]
        for fact_id in row["source_fact_ids"]
    }
    facts = [
        item for item in all_facts if item["fact_id"] in selected_ids
    ]
    return _base_result(session, plan, facts, sections, answer)


def _aggregate_regional_fact(
    samples: list[dict],
    *,
    period: date,
    row_code: int,
    row_name: str,
) -> dict:
    first = samples[0]
    lineage: dict[tuple, dict] = {}
    for sample in samples:
        source = sample.get("source") or {}
        references = source.get("lineage") or [source]
        for reference in references:
            if not isinstance(reference, dict):
                continue
            key = (
                reference.get("file"),
                reference.get("sheet"),
                reference.get("report_column"),
            )
            lineage[key] = {
                field: reference[field]
                for field in (
                    "file",
                    "absolute_path",
                    "sheet",
                    "row_code",
                    "report_column",
                    "read_only",
                )
                if field in reference
            }
    return {
        **first,
        "fact_id": f"regional-industry-aggregate-{row_code}-{period.isoformat()}",
        "value": str(
            sum((Decimal(item["value"]) for item in samples), Decimal("0"))
        ),
        "period_end": period.isoformat(),
        "period_label": period.isoformat(),
        "territory": "Все регионы Российской Федерации",
        "row_code": row_code,
        "row_name": row_name,
        "source": {
            "file": "Региональные формы 1-МСП",
            "formula": (
                f"Сумма подтверждённых значений по {len(samples)} регионам"
            ),
            "lineage": list(lineage.values()),
            "observations": len(samples),
            "read_only": True,
        },
    }


def _regional_average_growth_result(
    session: Session,
    plan: QueryPlan,
) -> dict:
    if plan.row_codes == [1010]:
        raise ValueError(
            "Не определены отрасли из предыдущего аналитического результата"
        )
    current_period = _regional_comparable_period(session, plan.periods[-1])
    previous_period = _previous_period(current_period)
    plan.periods = [current_period]
    plan.period_modes = ["cumulative_ytd"]
    plan.period_labels = [current_period.isoformat()]

    definitions = {
        item.base_row_code: item.name_ru
        for item in session.scalars(
            select(RowDefinition).where(
                RowDefinition.base_row_code.in_(plan.row_codes)
            )
        ).all()
    }
    regional_cache: dict = {}
    region_names = _regional_names(
        session,
        plan,
        current_period,
        regional_cache,
    )
    rows: list[dict] = []
    aggregate_facts: list[dict] = []
    all_observations: list[dict] = []

    for row_code in plan.row_codes:
        row_name = definitions.get(row_code)
        if row_name is None:
            continue
        observations: list[dict] = []
        current_samples: list[dict] = []
        previous_samples: list[dict] = []
        for region_name in region_names:
            try:
                current, _ = _metric_fact(
                    session,
                    plan,
                    current_period,
                    region_name,
                    row_code,
                    regional_cache,
                )
                previous, _ = _metric_fact(
                    session,
                    plan,
                    previous_period,
                    region_name,
                    row_code,
                    regional_cache,
                )
            except ValueError:
                continue
            current_value = Decimal(current["value"])
            previous_value = Decimal(previous["value"])
            if previous_value <= 0:
                continue
            change = (
                current_value / previous_value - Decimal("1")
            ) * Decimal("100")
            observations.append(
                {
                    "region": region_name,
                    "growth_rate": change,
                    "current": current_value,
                    "previous": previous_value,
                }
            )
            current_samples.append(current)
            previous_samples.append(previous)
        if not observations:
            continue

        current_total = sum(
            (item["current"] for item in observations),
            Decimal("0"),
        )
        previous_total = sum(
            (item["previous"] for item in observations),
            Decimal("0"),
        )
        growth_values = [item["growth_rate"] for item in observations]
        average_growth = sum(growth_values, Decimal("0")) / Decimal(
            len(growth_values)
        )
        median_growth = statistics.median(growth_values)
        weighted_growth = (
            current_total / previous_total - Decimal("1")
        ) * Decimal("100")
        minimum = min(observations, key=lambda item: item["growth_rate"])
        maximum = max(observations, key=lambda item: item["growth_rate"])
        current_aggregate = _aggregate_regional_fact(
            current_samples,
            period=current_period,
            row_code=row_code,
            row_name=row_name,
        )
        previous_aggregate = _aggregate_regional_fact(
            previous_samples,
            period=previous_period,
            row_code=row_code,
            row_name=row_name,
        )
        aggregate_facts.extend([previous_aggregate, current_aggregate])
        rows.append(
            {
                "entity": row_name,
                "row_code": row_code,
                "current": str(current_total),
                "previous": str(previous_total),
                "difference": str(current_total - previous_total),
                "growth_rate": str(average_growth),
                "median_growth_rate": str(median_growth),
                "weighted_growth_rate": str(weighted_growth),
                "regions_count": len(observations),
                "minimum_region": {
                    "entity": minimum["region"],
                    "growth_rate": str(minimum["growth_rate"]),
                },
                "maximum_region": {
                    "entity": maximum["region"],
                    "growth_rate": str(maximum["growth_rate"]),
                },
                "current_period": current_period.isoformat(),
                "previous_period": previous_period.isoformat(),
                "unit": current_aggregate["unit"],
                "source_fact_ids": [
                    previous_aggregate["fact_id"],
                    current_aggregate["fact_id"],
                ],
            }
        )
        all_observations.extend(observations)

    if not rows or not all_observations:
        raise ValueError(
            "Нет сопоставимых региональных значений для выбранных отраслей"
        )
    rows.sort(key=lambda item: Decimal(item["growth_rate"]), reverse=True)
    overall_growth_values = [
        item["growth_rate"] for item in all_observations
    ]
    overall_average = sum(
        overall_growth_values, Decimal("0")
    ) / Decimal(len(overall_growth_values))
    overall_median = statistics.median(overall_growth_values)
    overall_current = sum(
        (item["current"] for item in all_observations),
        Decimal("0"),
    )
    overall_previous = sum(
        (item["previous"] for item in all_observations),
        Decimal("0"),
    )
    overall_weighted = (
        overall_current / overall_previous - Decimal("1")
    ) * Decimal("100")
    unique_regions = {
        item["region"] for item in all_observations
    }
    coverage_counts = [int(item["regions_count"]) for item in rows]
    coverage_note = (
        f" По отдельным отраслям в расчёт вошло от "
        f"{min(coverage_counts)} до {max(coverage_counts)} регионов: "
        "наблюдения без положительной базы не включались в процентный "
        "показатель."
        if min(coverage_counts) != max(coverage_counts)
        else ""
    )
    answer = (
        f"Для {len(rows)} отраслей из предыдущего рейтинга по "
        f"{len(unique_regions)} регионам средний арифметический прирост "
        f"составил {ru_number(overall_average, show_plus=True)}% "
        f"(средний темп роста — "
        f"{ru_number(Decimal('100') + overall_average)}%). "
        f"Медианный прирост — "
        f"{ru_number(overall_median, show_plus=True)}%, "
        f"взвешенный по объёму поступлений — "
        f"{ru_number(overall_weighted, show_plus=True)}%. "
        "Расчёты по каждой отрасли показаны отдельно."
        + coverage_note
    )
    section = {
        "kind": "regional_average_growth",
        "title": (
            "Средний рост по регионам для отраслей "
            "из предыдущего рейтинга"
        ),
        "rows": rows,
        "methodology": (
            f"Сопоставлены одинаковые накопительные срезы: "
            f"{previous_period.isoformat()} и {current_period.isoformat()}. "
            "Средний прирост — простое среднее региональных изменений; "
            "медиана снижает влияние крайних значений; взвешенный прирост "
            "рассчитан по суммарному объёму поступлений. Региональные "
            "наблюдения без положительной базы исключены из процентных "
            "расчётов."
        ),
    }
    return _base_result(
        session,
        plan,
        aggregate_facts,
        [section],
        answer,
    )


def _regional_industry_rank_growth_result(
    session: Session,
    plan: QueryPlan,
) -> dict:
    if plan.row_codes == [1010]:
        raise ValueError(
            "Не определены отрасли из предыдущего аналитического результата"
        )
    current_period = _regional_comparable_period(session, plan.periods[-1])
    previous_period = _previous_period(current_period)
    plan.periods = [current_period]
    plan.period_modes = ["cumulative_ytd"]
    plan.period_labels = [current_period.isoformat()]

    definitions = {
        item.base_row_code: item.name_ru
        for item in session.scalars(
            select(RowDefinition).where(
                RowDefinition.base_row_code.in_(plan.row_codes)
            )
        ).all()
    }
    regional_cache: dict = {}
    region_names = _regional_names(
        session,
        plan,
        current_period,
        regional_cache,
    )
    sections: list[dict] = []
    selected_facts: list[dict] = []
    summaries: list[str] = []
    unavailable_rows: list[dict] = []
    unavailable_facts: list[dict] = []
    limit = plan.limit or 5
    direction = plan.ranking_direction

    for row_code in plan.row_codes:
        industry_name = definitions.get(row_code)
        if industry_name is None:
            continue
        comparisons: list[dict] = []
        facts: list[dict] = []
        for region_name in region_names:
            try:
                current, _ = _metric_fact(
                    session,
                    plan,
                    current_period,
                    region_name,
                    row_code,
                    regional_cache,
                )
                previous, _ = _metric_fact(
                    session,
                    plan,
                    previous_period,
                    region_name,
                    row_code,
                    regional_cache,
                )
            except ValueError:
                continue
            comparisons.append(
                _comparison_row(
                    region_name,
                    current,
                    previous,
                    row_code=row_code,
                )
            )
            facts.extend([previous, current])
        positive_bases = [
            Decimal(item["previous"])
            for item in comparisons
            if Decimal(item["previous"]) > 0
        ]
        if not positive_bases:
            previous_samples = [
                item
                for item in facts
                if item["period_end"] == previous_period.isoformat()
            ]
            current_samples = [
                item
                for item in facts
                if item["period_end"] == current_period.isoformat()
            ]
            if previous_samples and current_samples:
                previous_aggregate = _aggregate_regional_fact(
                    previous_samples,
                    period=previous_period,
                    row_code=row_code,
                    row_name=industry_name,
                )
                current_aggregate = _aggregate_regional_fact(
                    current_samples,
                    period=current_period,
                    row_code=row_code,
                    row_name=industry_name,
                )
                unavailable_facts.extend(
                    [previous_aggregate, current_aggregate]
                )
                summary = _comparison_row(
                    industry_name,
                    current_aggregate,
                    previous_aggregate,
                    row_code=row_code,
                )
                summary.update(
                    {
                        "regions_count": len(comparisons),
                        "comparison_status": "no_positive_base",
                    }
                )
                unavailable_rows.append(summary)
            continue
        percentile_floor = _percentile(
            positive_bases,
            Decimal("0.10"),
        )
        materiality_floor = sum(
            positive_bases,
            Decimal("0"),
        ) * Decimal("0.001")
        base_floor = max(percentile_floor, materiality_floor)
        for item in comparisons:
            item["low_base"] = (
                Decimal(item["previous"]) <= 0
                or Decimal(item["previous"]) < base_floor
            )
        eligible = sorted(
            [
                item
                for item in comparisons
                if item["growth_rate"] is not None and not item["low_base"]
            ],
            key=lambda item: Decimal(item["growth_rate"]),
            reverse=True,
        )
        if not eligible:
            continue
        selections: list[tuple[str, list[dict]]] = []
        if direction in {"top", "both"}:
            selections.append(("top", eligible[:limit]))
        if direction in {"bottom", "both"}:
            selections.append(("bottom", list(reversed(eligible[-limit:]))))
        for selected_direction, ranked in selections:
            selected_ids = {
                fact_id
                for item in ranked
                for fact_id in item["source_fact_ids"]
            }
            selected_facts.extend(
                item for item in facts if item["fact_id"] in selected_ids
            )

            def labeled_region(item: dict) -> dict:
                region_name = str(item["entity"])
                return {
                    **item,
                    "entity": f"{industry_name} — {region_name}",
                    "industry": industry_name,
                    "region": region_name,
                }

            labeled = [labeled_region(item) for item in ranked]
            is_top = selected_direction == "top"
            sections.append(
                {
                    "kind": (
                        "regional_industry_growth"
                        if is_top
                        else "regional_industry_growth_worst"
                    ),
                    "title": (
                        f"Лидеры регионов по росту — {industry_name}"
                        if is_top
                        else f"Худшая региональная динамика — {industry_name}"
                    ),
                    "industry_row_code": row_code,
                    "rows": labeled,
                    "methodology": (
                        f"{'Максимальный' if is_top else 'Минимальный'} темп "
                        f"роста к тому же накопительному срезу "
                        f"{previous_period.isoformat()}. Регионы с "
                        "неположительной базой, базой ниже 10-го процентиля "
                        "или менее 0,1% совокупной региональной базы отрасли "
                        "исключены."
                    ),
                }
            )
            selected = labeled[0]
            summaries.append(
                f"{industry_name} — {selected['region']} "
                f"({ru_number(Decimal(selected['growth_rate']), 2, show_plus=True)}%)"
            )

    if unavailable_rows:
        selected_facts.extend(unavailable_facts)
        sections.append(
            {
                "kind": "regional_industry_growth_unavailable",
                "title": "Отрасли без положительной сопоставимой базы",
                "rows": unavailable_rows,
                "methodology": (
                    "Региональные экстремумы по темпу роста не определяются, "
                    "если ни в одном регионе нет положительной базы в том же "
                    f"накопительном срезе {previous_period.isoformat()}. "
                    "Показаны проверенные суммы доступных региональных "
                    "значений без подмены критерия рейтинга."
                ),
            }
        )

    if not sections:
        raise ValueError(
            "Нет сопоставимых региональных значений для выбранных отраслей"
        )
    label = {
        "top": "Региональные лидеры роста",
        "bottom": "Регионы с худшей динамикой",
        "both": "Региональные экстремумы",
    }[direction]
    if summaries:
        answer = (
            f"{label} по выбранным отраслям: "
            + "; ".join(summaries)
            + ". Методика исключения малой базы показана в таблицах."
        )
        if unavailable_rows:
            answer += (
                " Для части выбранных отраслей экстремумы не определяются: "
                "нет положительной сопоставимой региональной базы."
            )
    else:
        answer = (
            "Региональные экстремумы по темпу роста не выделяются: для "
            "выбранных отраслей ни в одном регионе нет положительной "
            "сопоставимой базы. Проверенные значения и методика показаны "
            "в таблице."
        )
    return _base_result(
        session,
        plan,
        selected_facts,
        sections,
        answer,
    )


def _regional_industry_comparisons(
    session: Session,
    plan: QueryPlan,
    *,
    row_code: int,
    current_period: date,
    previous_period: date,
    region_names: list[str],
    regional_cache: dict,
) -> tuple[list[dict], list[dict]]:
    comparisons: list[dict] = []
    facts: list[dict] = []
    for region_name in region_names:
        try:
            current, _ = _metric_fact(
                session,
                plan,
                current_period,
                region_name,
                row_code,
                regional_cache,
            )
            previous, _ = _metric_fact(
                session,
                plan,
                previous_period,
                region_name,
                row_code,
                regional_cache,
            )
        except ValueError:
            continue
        comparisons.append(
            _comparison_row(
                region_name,
                current,
                previous,
                row_code=row_code,
            )
        )
        facts.extend([previous, current])

    positive_bases = [
        Decimal(item["previous"])
        for item in comparisons
        if Decimal(item["previous"]) > 0
    ]
    if not positive_bases:
        return [], facts
    percentile_floor = _percentile(positive_bases, Decimal("0.10"))
    materiality_floor = sum(
        positive_bases,
        Decimal("0"),
    ) * Decimal("0.001")
    base_floor = max(percentile_floor, materiality_floor)
    for item in comparisons:
        item["low_base"] = (
            Decimal(item["previous"]) <= 0
            or Decimal(item["previous"]) < base_floor
        )
    return comparisons, facts


def _analytical_brief_result(
    session: Session,
    plan: QueryPlan,
) -> dict:
    """Build one report-ready result from several deterministic analyses."""
    from .query_service import execute_plan

    trend_plan = plan.model_copy(deep=True)
    trend_plan.intent = "trend"
    trend_plan.analysis_dimension = None
    trend_plan.group_by = []
    trend_plan.ranking_criteria = []
    trend_plan.row_codes = [1010]
    trend_plan.territories = ["Российская Федерация"]
    trend_plan.limit = None
    trend_result = execute_plan(session, trend_plan)

    current_period = _regional_comparable_period(session, plan.periods[-1])
    previous_period = _previous_period(current_period)
    industry_plan = plan.model_copy(deep=True)
    industry_plan.intent = "rank_growth"
    industry_plan.periods = [current_period]
    industry_plan.period_modes = ["cumulative_ytd"]
    industry_plan.period_labels = [current_period.isoformat()]
    industry_plan.analysis_dimension = "industry"
    industry_plan.group_by = ["industry"]
    industry_plan.row_codes = [1010]
    industry_plan.territories = ["Российская Федерация"]
    industry_comparisons, industry_facts = _ranking_rows(
        session,
        industry_plan,
    )
    eligible_industries = [
        item
        for item in industry_comparisons
        if item["growth_rate"] is not None and not item["low_base"]
    ]
    if not eligible_industries:
        raise ValueError("Нет сопоставимых отраслевых значений для обзора")

    limit = plan.limit or 3
    ordered_industries = sorted(
        eligible_industries,
        key=lambda item: Decimal(item["growth_rate"]),
        reverse=True,
    )
    best_industries = ordered_industries[:limit]
    worst_industries = list(reversed(ordered_industries[-limit:]))
    sections: list[dict] = [
        {
            "kind": "industry_growth_best",
            "title": "Отрасли с лучшей динамикой",
            "rows": best_industries,
            "methodology": (
                "Прирост к тому же накопительному срезу предыдущего года. "
                "Отрасли с базой ниже 10-го процентиля исключены."
            ),
        },
        {
            "kind": "industry_growth_worst",
            "title": "Отрасли с худшей динамикой",
            "rows": worst_industries,
            "methodology": (
                "Минимальный прирост к тому же накопительному срезу "
                "предыдущего года после исключения малой базы."
            ),
        },
    ]

    selected_by_code: dict[int, dict] = {}
    for item in [*best_industries, *worst_industries]:
        row_code = item.get("row_code")
        if isinstance(row_code, int):
            selected_by_code.setdefault(row_code, item)

    definitions = {
        item.base_row_code: item.name_ru
        for item in session.scalars(
            select(RowDefinition).where(
                RowDefinition.base_row_code.in_(selected_by_code)
            )
        ).all()
    }
    regional_plan = plan.model_copy(deep=True)
    regional_plan.periods = [current_period]
    regional_plan.period_modes = ["cumulative_ytd"]
    regional_plan.period_labels = [current_period.isoformat()]
    regional_plan.analysis_dimension = "territory"
    regional_plan.territories = ["Российская Федерация"]
    regional_cache: dict = {}
    region_names = _regional_names(
        session,
        regional_plan,
        current_period,
        regional_cache,
    )
    best_regions: list[dict] = []
    worst_regions: list[dict] = []
    regional_facts: list[dict] = []
    selected_regional_fact_ids: set[str] = set()

    for row_code in selected_by_code:
        comparisons, facts = _regional_industry_comparisons(
            session,
            regional_plan,
            row_code=row_code,
            current_period=current_period,
            previous_period=previous_period,
            region_names=region_names,
            regional_cache=regional_cache,
        )
        eligible_regions = sorted(
            (
                item
                for item in comparisons
                if item["growth_rate"] is not None and not item["low_base"]
            ),
            key=lambda item: Decimal(item["growth_rate"]),
            reverse=True,
        )
        if not eligible_regions:
            continue
        industry_name = definitions.get(
            row_code,
            selected_by_code[row_code]["entity"],
        )

        def labeled_region(item: dict) -> dict:
            region_name = item["entity"]
            return {
                **item,
                "entity": f"{industry_name} — {region_name}",
                "industry": industry_name,
                "region": region_name,
            }

        best = labeled_region(eligible_regions[0])
        worst = labeled_region(eligible_regions[-1])
        best_regions.append(best)
        worst_regions.append(worst)
        selected_regional_fact_ids.update(best["source_fact_ids"])
        selected_regional_fact_ids.update(worst["source_fact_ids"])
        regional_facts.extend(facts)

    sections.extend(
        [
            {
                "kind": "regional_selected_industries_best",
                "title": "Лучшие регионы в выбранных отраслях",
                "rows": best_regions,
                "methodology": (
                    "Для каждой выбранной отрасли показан регион с наибольшим "
                    "приростом. Неположительные и нематериальные базы "
                    "исключены."
                ),
            },
            {
                "kind": "regional_selected_industries_worst",
                "title": "Худшие регионы в выбранных отраслях",
                "rows": worst_regions,
                "methodology": (
                    "Для каждой выбранной отрасли показан регион с наименьшим "
                    "приростом после того же фильтра малой базы."
                ),
            },
        ]
    )

    selected_industry_fact_ids = {
        fact_id
        for item in [*best_industries, *worst_industries]
        for fact_id in item["source_fact_ids"]
    }
    selected_facts = [
        item
        for item in industry_facts
        if item["fact_id"] in selected_industry_fact_ids
    ]
    selected_facts.extend(
        item
        for item in regional_facts
        if item["fact_id"] in selected_regional_fact_ids
    )
    selected_facts.extend(trend_result.get("facts", []))

    best_industry = best_industries[0]
    worst_industry = worst_industries[0]
    first_period = trend_plan.periods[0]
    last_period = trend_plan.periods[-1]
    answer = (
        f"Сформирован сводный обзор за {first_period.year}–{last_period.year} "
        f"по сопоставимым накопительным срезам. Лучшая отраслевая динамика: "
        f"{best_industry['entity']} "
        f"({ru_number(Decimal(best_industry['growth_rate']), 2, show_plus=True)}%); "
        f"худшая: {worst_industry['entity']} "
        f"({ru_number(Decimal(worst_industry['growth_rate']), 2, show_plus=True)}%). "
        "Для отобранных отраслей отдельно показаны регионы с лучшей и худшей "
        "динамикой."
    )
    result = _base_result(
        session,
        plan,
        selected_facts,
        sections,
        answer,
    )
    result["series"] = trend_result.get("series", [])
    result["calculations"] = trend_result.get("calculations", [])
    result["visualization"] = trend_result.get("visualization")
    result["warnings"] = list(trend_result.get("warnings", []))
    return result


def _median(values: list[float]) -> float:
    return statistics.median(values)


def _robust_z(values: dict[str, float]) -> dict[str, float] | None:
    if len(values) < 10:
        return None
    raw = list(values.values())
    median = _median(raw)
    deviations = [abs(value - median) for value in raw]
    scale = 1.4826 * _median(deviations)
    if scale == 0:
        ordered = sorted(raw)
        q1 = ordered[int((len(ordered) - 1) * 0.25)]
        q3 = ordered[int((len(ordered) - 1) * 0.75)]
        scale = (q3 - q1) / 1.349
    if scale == 0:
        return {key: 0.0 for key in values}
    return {
        key: max(-5.0, min(5.0, (value - median) / scale))
        for key, value in values.items()
    }


def _js_divergence(first: list[float], second: list[float]) -> float:
    middle = [(left + right) / 2 for left, right in zip(first, second)]

    def divergence(values: list[float], reference: list[float]) -> float:
        return sum(
            value * math.log(value / base)
            for value, base in zip(values, reference)
            if value > 0 and base > 0
        )

    return (divergence(first, middle) + divergence(second, middle)) / 2


def _shares(values: list[Decimal]) -> list[float] | None:
    safe = [max(Decimal("0"), item) for item in values]
    total = sum(safe, Decimal("0"))
    if total <= 0:
        return None
    return [float(item / total) for item in safe]


def _structure_vector(
    session: Session,
    period: date,
    territory: str,
    regional_cache: dict,
) -> list[float] | None:
    values: list[Decimal] = []
    for metric_code in STRUCTURE_METRICS:
        structure_plan = QueryPlan(
            metric_codes=[metric_code],
            periods=[period],
            territories=[territory],
        )
        try:
            fact, _ = _metric_fact(
                session,
                structure_plan,
                period,
                territory,
                1010,
                regional_cache,
            )
        except ValueError:
            return None
        values.append(Decimal(fact["value"]))
    total_plan = QueryPlan(
        metric_codes=["receipts_budget_total"],
        periods=[period],
        territories=[territory],
    )
    try:
        total_fact, _ = _metric_fact(
            session,
            total_plan,
            period,
            territory,
            1010,
            regional_cache,
        )
    except ValueError:
        return None
    residual = max(Decimal("0"), Decimal(total_fact["value"]) - sum(values))
    return _shares([*values, residual])


def _outlier_result(session: Session, plan: QueryPlan) -> dict:
    comparisons, all_facts = _ranking_rows(session, plan)
    regional_cache: dict = {}
    current_values = {
        item["entity"]: math.log1p(max(0.0, float(item["current"])))
        for item in comparisons
    }
    growth_values = {
        item["entity"]: math.log(
            float(Decimal(item["current"]) / Decimal(item["previous"]))
        )
        for item in comparisons
        if Decimal(item["current"]) > 0 and Decimal(item["previous"]) > 0
    }
    z_value = _robust_z(current_values)
    z_growth = _robust_z(growth_values)
    current_period = plan.periods[-1]
    rf_structure = _structure_vector(
        session,
        current_period,
        "Российская Федерация",
        regional_cache,
    )
    structure_values: dict[str, float] = {}
    if rf_structure is not None:
        for item in comparisons:
            vector = _structure_vector(
                session,
                current_period,
                item["entity"],
                regional_cache,
            )
            if vector is not None:
                structure_values[item["entity"]] = _js_divergence(
                    vector, rf_structure
                )
    z_structure = _robust_z(structure_values)
    by_entity = {item["entity"]: dict(item) for item in comparisons}
    sections: list[dict] = []
    modes = plan.outlier_modes or ["value", "growth_rate", "composite"]
    if "value" in modes and z_value is not None:
        rows = sorted(
            (
                {**by_entity[name], "score": str(score)}
                for name, score in z_value.items()
            ),
            key=lambda item: float(item["score"]),
        )
        sections.append(
            {
                "kind": "outliers_value",
                "title": "Отклонения по абсолютному уровню",
                "rows": [*rows[:5], *reversed(rows[-5:])],
                "methodology": "Robust z-score для log1p текущего значения.",
            }
        )
    if "growth_rate" in modes and z_growth is not None:
        rows = sorted(
            (
                {**by_entity[name], "score": str(score)}
                for name, score in z_growth.items()
            ),
            key=lambda item: float(item["score"]),
        )
        sections.append(
            {
                "kind": "outliers_growth",
                "title": "Отклонения по динамике",
                "rows": [*rows[:5], *reversed(rows[-5:])],
                "methodology": (
                    "Robust z-score для логарифма отношения к тому же "
                    "срезу предыдущего года."
                ),
            }
        )
    if (
        "composite" in modes
        and z_value is not None
        and z_growth is not None
        and z_structure is not None
    ):
        common = set(z_value) & set(z_growth) & set(z_structure)
        rows = []
        for name in common:
            score = math.sqrt(
                (
                    z_value[name] ** 2
                    + z_growth[name] ** 2
                    + z_structure[name] ** 2
                )
                / 3
            )
            rows.append(
                {
                    **by_entity[name],
                    "score": str(score),
                    "flagged": score >= 2.5,
                    "components": {
                        "value": str(z_value[name]),
                        "growth_rate": str(z_growth[name]),
                        "structure": str(z_structure[name]),
                    },
                }
            )
        rows.sort(key=lambda item: float(item["score"]), reverse=True)
        sections.append(
            {
                "kind": "outliers_composite",
                "title": "Композитные аномалии",
                "rows": rows[:5],
                "methodology": (
                    "Корень из среднего квадратов robust z-score уровня, "
                    "динамики и налоговой структуры; диагностический порог 2,5."
                ),
                "threshold": "2.5",
                "threshold_exceeded": any(item["flagged"] for item in rows),
            }
        )
    warnings = []
    if z_value is None or z_growth is None:
        warnings.append(
            "Для части диагностики требуется не менее 10 сопоставимых регионов."
        )
    if z_structure is None:
        warnings.append(
            "Структурная компонента недоступна для выбранного среза."
        )
    answer = (
        "Показаны три независимых диагностических среза: абсолютный уровень, "
        "динамика и композитная аномалия. Показатель является аналитической "
        "диагностикой, а не официальной статистикой."
    )
    selected_ids = {
        fact_id
        for section in sections
        for row in section["rows"]
        for fact_id in row.get("source_fact_ids", [])
    }
    selected_facts = [
        item for item in all_facts if item["fact_id"] in selected_ids
    ]
    result = _base_result(
        session, plan, selected_facts, sections, answer
    )
    result["warnings"] = warnings
    return result


def _overview_result(session: Session, plan: QueryPlan) -> dict:
    current_period = plan.periods[-1]
    previous_period = _previous_period(current_period)
    overview_plan = plan.model_copy(deep=True)
    overview_plan.territories = ["Российская Федерация"]
    regional_cache: dict = {}
    current, current_parts = _metric_fact(
        session,
        overview_plan,
        current_period,
        "Российская Федерация",
        1010,
        regional_cache,
    )
    previous, previous_parts = _metric_fact(
        session,
        overview_plan,
        previous_period,
        "Российская Федерация",
        1010,
        regional_cache,
    )
    row = _comparison_row("Российская Федерация", current, previous)
    divisor = Decimal("1000000") if current["unit"] == "тыс. руб." else Decimal("1")
    answer = (
        f"{current['metric']} по РФ: "
        f"{ru_number(Decimal(current['value']) / divisor)} млрд руб.; "
        f"изменение к сопоставимому срезу прошлого года "
        f"{ru_number(Decimal(row['difference']) / divisor, show_plus=True)} "
        f"млрд руб., рост {ru_number(Decimal(row['growth_rate']), show_plus=True)}%."
    )
    section = {
        "kind": "overview",
        "title": "Общая ситуация по Российской Федерации",
        "rows": [row],
        "methodology": "Сравнение одинаковых накопительных срезов двух лет.",
    }
    return _base_result(
        session,
        overview_plan,
        [*previous_parts, *current_parts],
        [section],
        answer,
    )


def _base_result(
    session: Session,
    plan: QueryPlan,
    facts: list[dict],
    sections: list[dict],
    answer: str,
) -> dict:
    unique_facts = {
        str(item["fact_id"]): item
        for item in facts
    }
    return json_ready(
        {
            "status": "ready",
            "query_plan": plan.model_dump(mode="json"),
            "resolved_metrics": resolved_metric_details(
                session, plan.metric_codes
            ),
            "facts": list(unique_facts.values()),
            "calculations": [],
            "series": [],
            "analysis_sections": sections,
            "visualization": None,
            "warnings": [],
            "answer": answer,
            "answer_mode": "deterministic",
            "numbers_verified": True,
        }
    )


def execute_analytic_plan(session: Session, plan: QueryPlan) -> dict:
    if plan.intent == "analytical_brief":
        return _analytical_brief_result(session, plan)
    if plan.intent == "regional_industry_rank_growth":
        return _regional_industry_rank_growth_result(session, plan)
    if plan.intent == "regional_average_growth":
        return _regional_average_growth_result(session, plan)
    if plan.intent == "overview":
        return _overview_result(session, plan)
    regional_industry_ranking = (
        plan.analysis_dimension == "industry"
        and any(
            item != "Российская Федерация"
            for item in plan.territories
        )
    )
    if (
        plan.analysis_dimension == "territory"
        or plan.intent == "outliers"
        or regional_industry_ranking
    ):
        requested = plan.periods[-1]
        selected = _regional_comparable_period(session, requested)
        plan.periods = [selected]
        plan.period_modes = ["cumulative_ytd"]
        plan.period_labels = [selected.isoformat()]
    if plan.intent == "outliers":
        return _outlier_result(session, plan)
    return _rank_result(session, plan)
