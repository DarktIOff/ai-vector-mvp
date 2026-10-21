from __future__ import annotations

import re
from calendar import monthrange
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .calculations import json_ready, ru_number, safe_ratio
from .catalog_service import execute_catalog_plan
from .catalog import (
    METRIC_BY_CODE,
    METRIC_BY_COLUMN,
    resolve_metric_code,
    resolve_metric_codes,
    resolved_metric_details,
)
from .models import MetricDefinition, MspFact, RowDefinition, SourceFile, Territory
from .regional import RegionalValue, read_regional_values, territory_key
from .schemas import QueryPlan
from .workbooks import normalize_text


YEAR_RE = re.compile(r"\b(20\d{2})\b")
YEAR_RANGE_RE = re.compile(
    r"\b(20\d{2})\s*[\-–—]\s*(20\d{2})\b"
)
RELATIVE_YEARS_RE = re.compile(
    r"\b(?:последн\w*|прошедш\w*)\s+"
    r"(?P<count>\d{1,2}|один|два|три|четыре|пять|шесть|семь|"
    r"восемь|девять|десять)\s+"
    r"(?:(?:полн\w*|сопоставим\w*|календарн\w*)\s+){0,2}"
    r"(?:год\w*|лет)\b"
)
REVERSED_RELATIVE_YEARS_RE = re.compile(
    r"\b(?P<count>\d{1,2}|один|два|три|четыре|пять|шесть|семь|"
    r"восемь|девять|десять)\s+(?:последн\w*|прошедш\w*)\s+"
    r"(?:(?:полн\w*|сопоставим\w*|календарн\w*)\s+){0,2}"
    r"(?:год\w*|лет)\b"
)
QUARTER_YEAR_RE = re.compile(
    r"\b(перв\w*|втор\w*|трет\w*|четверт\w*|[1-4]|i{1,3}|iv)"
    r"\s+квартал\w*\s+(20\d{2})\b",
    re.IGNORECASE,
)
QUARTER_ONLY_RE = re.compile(
    r"\b(перв\w*|втор\w*|трет\w*|четверт\w*|[1-4]|i{1,3}|iv)"
    r"\s+квартал\w*\b",
    re.IGNORECASE,
)
COLUMN_RE = re.compile(
    r"\b(?:графа|графы|графу|графе|гр\.?|колонка|колонку|столбец)"
    r"\s*№?\s*(\d{1,2})\b",
    re.IGNORECASE,
)
ROMAN_QUARTERS = {1: "I", 2: "II", 3: "III", 4: "IV"}


def _quarter_number(raw: str) -> int:
    normalized = normalize_text(raw)
    if normalized.startswith("перв") or normalized in {"1", "i"}:
        return 1
    if normalized.startswith("втор") or normalized in {"2", "ii"}:
        return 2
    if normalized.startswith("трет") or normalized in {"3", "iii"}:
        return 3
    if normalized.startswith("четвер") or normalized in {"4", "iv"}:
        return 4
    raise ValueError(f"Не удалось распознать квартал: {raw}")


def _quarter_end(year: int, quarter: int) -> date:
    month = quarter * 3
    return date(year, month, monthrange(year, month)[1])


def _period_for_year(question: str, year: int) -> date:
    explicit_month = _explicit_cumulative_month(question)
    if explicit_month is not None:
        return date(
            year,
            explicit_month,
            monthrange(year, explicit_month)[1],
        )
    normalized = normalize_text(question)
    if "январь-июнь" in normalized or "перв" in normalized and "полугод" in normalized:
        return date(year, 6, 30)
    if "январь-март" in normalized or "i кварт" in normalized or "1 кварт" in normalized:
        return date(year, 3, 31)
    if "январь-сентябр" in normalized or "9 месяц" in normalized:
        return date(year, 9, 30)
    return date(year, 12, 31)


def _relative_year_count(question: str) -> int | None:
    normalized = normalize_text(question)
    match = RELATIVE_YEARS_RE.search(normalized)
    if match is None:
        match = REVERSED_RELATIVE_YEARS_RE.search(normalized)
    if match is None:
        return None
    raw = match.group("count")
    words = {
        "один": 1,
        "два": 2,
        "три": 3,
        "четыре": 4,
        "пять": 5,
        "шесть": 6,
        "семь": 7,
        "восемь": 8,
        "девять": 9,
        "десять": 10,
    }
    count = int(raw) if raw.isdigit() else words[raw]
    if count < 1 or count > 23:
        raise ValueError(
            "Относительный диапазон должен быть от 1 до 23 лет"
        )
    return count


def extract_years(
    question: str, latest_year: int | None = None
) -> list[int]:
    """Return explicit years, expanding a range without inventing gaps."""
    # Catalog row labels contain formulas such as ``стр.2010=...``.  Those
    # identifiers are not dates and must never move the requested period.
    year_text = re.sub(
        r"\([^)]*\bстр(?:ока|оки|оку|оке)?[.]?\s*20\d{2}[^)]*\)",
        "",
        question,
        flags=re.IGNORECASE,
    )
    year_text = re.sub(
        r"\bстр(?:ока|оки|оку|оке)?[.]?\s*20\d{2}\b",
        "",
        year_text,
        flags=re.IGNORECASE,
    )
    years = {int(value) for value in YEAR_RE.findall(year_text)}
    for start_raw, end_raw in YEAR_RANGE_RE.findall(year_text):
        start, end = int(start_raw), int(end_raw)
        if start > end:
            start, end = end, start
        if end - start > 23:
            raise ValueError("Диапазон динамики не может превышать 24 года")
        years.update(range(start, end + 1))
    relative_count = _relative_year_count(question)
    if not years and relative_count is not None:
        anchor = latest_year or date.today().year
        if "прошедш" in normalize_text(question):
            # “За прошедшие N лет” asks for N annual observations, including
            # the current comparable YTD slice when that is the latest data.
            years.update(range(anchor - relative_count + 1, anchor + 1))
        else:
            # “Изменились за последние N лет” describes N year-to-year
            # intervals, therefore both endpoints are required.
            years.update(range(anchor - relative_count, anchor + 1))
    return sorted(years)


def _explicit_cumulative_month(question: str) -> int | None:
    normalized = normalize_text(question)
    span_match = re.search(
        r"январ\w*\s*-\s*"
        r"(феврал\w*|март\w*|апрел\w*|ма[йя]\w*|июн\w*|июл\w*|"
        r"август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*)",
        normalized,
    )
    if span_match:
        ending = span_match.group(1)
        stems = (
            "январ",
            "феврал",
            "март",
            "апрел",
            "ма",
            "июн",
            "июл",
            "август",
            "сентябр",
            "октябр",
            "ноябр",
            "декабр",
        )
        for month, stem in enumerate(stems, start=1):
            if ending.startswith(stem):
                return month
    count_match = re.search(r"\b(1[0-2]|[1-9])\s+месяц\w*\b", normalized)
    if count_match:
        return int(count_match.group(1))
    patterns = (
        (12, ("январь-декабрь", "12 месяц", "за год", "годовой")),
        (9, ("январь-сентябр", "9 месяц")),
        (6, ("январь-июн", "первое полугод", "первого полугод", "6 месяц")),
        (3, ("январь-март", "3 месяц")),
    )
    for month, tokens in patterns:
        if any(token in normalized for token in tokens):
            return month
    return None


def _latest_common_month(
    years: list[int], periods: list[date]
) -> int | None:
    months_by_year = {
        year: {period.month for period in periods if period.year == year}
        for year in years
    }
    if not months_by_year or any(not months for months in months_by_year.values()):
        return None
    common = set.intersection(*months_by_year.values())
    return max(common) if common else None


def _is_multiyear_trend(question: str, years: list[int]) -> bool:
    normalized = normalize_text(question)
    return len(years) >= 3 and (
        "динамик" in normalized
        or "тренд" in normalized
        or "по годам" in normalized
        or "за период" in normalized
        or "последн" in normalized
        or "прошедш" in normalized
        or YEAR_RANGE_RE.search(question) is not None
    )


def _combined_budget_and_insurance_requested(normalized: str) -> bool:
    """Recognize an explicit request for the complete MSP receipts total."""
    return (
        "поступ" in normalized
        and "страхов" in normalized
        and "взнос" in normalized
        and any(
            marker in normalized
            for marker in (
                "вмест",
                "включ",
                "с учетом",
                "с учётом",
                "плюс",
                "совокуп",
                "суммар",
            )
        )
    )


def build_query_plan(question: str, available_periods: list[date]) -> QueryPlan:
    normalized = normalize_text(question)
    latest_year = (
        max(period.year for period in available_periods)
        if available_periods
        else date.today().year
    )
    years = extract_years(question, latest_year)
    implicit_multiyear_dynamics = (
        not years
        and any(
            token in normalized
            for token in ("динамик", "тренд", "по годам")
        )
    )
    if implicit_multiyear_dynamics:
        years = sorted({period.year for period in available_periods})[-24:]
    relative_count = _relative_year_count(question)
    if (
        relative_count is not None
        and "полн" in normalized
        and not YEAR_RE.search(question)
    ):
        completed_years = sorted(
            {
                period.year
                for period in available_periods
                if period.month == 12
            }
        )
        years = completed_years[-relative_count:]
    trend_requested = _is_multiyear_trend(question, years)
    quarter_matches = list(QUARTER_YEAR_RE.finditer(normalized))
    period_modes: list[str] = []
    period_labels: list[str] = []
    if quarter_matches:
        explicit_quarters = [
            (_quarter_number(match.group(1)), int(match.group(2)))
            for match in quarter_matches
        ]
        if len(explicit_quarters) == 1 and len(years) > 1:
            quarter_periods = [
                (explicit_quarters[0][0], year) for year in years
            ]
        else:
            quarter_periods = explicit_quarters
        periods = [
            _quarter_end(year, quarter)
            for quarter, year in quarter_periods
        ]
        period_modes = ["standalone_quarter"] * len(periods)
        period_labels = [
            f"{ROMAN_QUARTERS[quarter]} квартал {year}"
            for quarter, year in quarter_periods
        ]
    else:
        quarter_only = QUARTER_ONLY_RE.search(normalized)
        if quarter_only is not None and years:
            quarter = _quarter_number(quarter_only.group(1))
            periods = [_quarter_end(year, quarter) for year in years]
            period_modes = ["standalone_quarter"] * len(periods)
            period_labels = [
                f"{ROMAN_QUARTERS[quarter]} квартал {year}" for year in years
            ]
        elif years:
            explicit_month = _explicit_cumulative_month(question)
            common_month = (
                _latest_common_month(years, available_periods)
                if trend_requested and explicit_month is None
                else None
            )
            selected_month = explicit_month or common_month
            periods = [
                date(year, selected_month, monthrange(year, selected_month)[1])
                if selected_month
                else _period_for_year(question, year)
                for year in years
            ]
        else:
            periods = []
    if not periods and years:
        periods = [_period_for_year(question, year) for year in years]
    elif not quarter_matches and available_periods:
        if not years:
            periods = [max(available_periods)]
    elif not quarter_matches and not periods:
        periods = [date(2026, 6, 30)]

    broad_msp_total = (
        "поступления от мсп" in normalized
        or "поступило от мсп" in normalized
        or "всего от мсп" in normalized
        or re.search(
            r"\bпоступлен\w*\s+(?:(?:от|по)\s+)?мсп\b", normalized
        )
        is not None
    ) and "консолидированн" not in normalized
    industry_dimension_requested = any(
        token in normalized
        for token in ("отрасл", "оквэд", "вид деятельности")
    )
    best_requested = (
        re.search(r"\b(?:наи)?лучш\w*\b", normalized) is not None
        or "лидер" in normalized
        or re.search(r"\bлидир\w*", normalized) is not None
        or "наибольш" in normalized
        or "максимал" in normalized
    )
    worst_requested = (
        re.search(r"\b(?:наи)?худш\w*\b", normalized) is not None
        or "наименьш" in normalized
        or "минимал" in normalized
        or "аутсайдер" in normalized
    )
    analytical_brief_requested = (
        trend_requested
        and industry_dimension_requested
        and any(token in normalized for token in ("регион", "субъект"))
        and best_requested
        and worst_requested
    )
    regional_average_growth_requested = (
        re.search(r"\bсредн\w*\b", normalized) is not None
        and any(token in normalized for token in ("темп", "рост", "динамик"))
        and any(token in normalized for token in ("регион", "субъект"))
        and any(
            token in normalized
            for token in ("отрасл", "оквэд", "вид деятельности")
        )
    )
    regional_industry_leaders_requested = (
        any(token in normalized for token in ("регион", "субъект"))
        and any(
            token in normalized
            for token in ("отрасл", "оквэд", "вид деятельности")
        )
        and any(token in normalized for token in ("рост", "динамик", "темп"))
        and best_requested
    )
    column_match = COLUMN_RE.search(normalized)
    column_metric = (
        METRIC_BY_COLUMN.get(int(column_match.group(1)))
        if column_match
        else None
    )
    metric = (
        column_metric.code
        if column_metric is not None
        else resolve_metric_code(question)
    )
    mentioned_metrics = resolve_metric_codes(question)
    replacement_match = re.search(
        r"\b(?:замен\w*|смен\w*)\b.*?\bна\b\s+(.+)$",
        question,
        flags=re.IGNORECASE,
    )
    if replacement_match is not None:
        replacement_metric = resolve_metric_code(replacement_match.group(1))
        if replacement_metric is not None:
            # In “замени НДС на НДФЛ” both names are present lexically, but
            # only the target after “на” belongs to the new query plan.
            metric = replacement_metric
            mentioned_metrics = [replacement_metric]
    combined_budget_and_insurance = (
        _combined_budget_and_insurance_requested(normalized)
    )
    explicit_quoted_metric = (
        re.search(
            r"\bпоказател\w*\s*[«\"]",
            normalized,
        )
        is not None
    )
    tax_receipts = (
        "налоговые поступления" in normalized
        or "налоговых поступлений" in normalized
        or "поступления налогов" in normalized
    )
    budget_receipts = (
        "поступ" in normalized
        and (
            "в бюджет" in normalized
            or "бюджетн" in normalized
            or "консолидированн" in normalized
        )
    )
    if combined_budget_and_insurance:
        # "Поступления вместе со страховыми взносами" is a derived total,
        # not a request to replace budget receipts with insurance alone.
        metrics = [
            "receipts_budget_total",
            "receipts_insurance_total",
        ]
    elif explicit_quoted_metric and metric is not None:
        # A quoted catalog name/code/alias after “показатель” is an exact
        # reference. Generic aliases such as “поступления от МСП” retain their
        # broader aggregate meaning in ordinary unquoted analytical wording.
        metrics = [metric]
    elif tax_receipts:
        metrics = ["receipts_budget_total"]
    elif budget_receipts:
        metrics = ["receipts_budget_total"]
    elif analytical_brief_requested and metric is None:
        # A multi-year national series must use the indicator that exists in
        # every historical 1-МСП workbook.  Insurance contributions are not
        # comparable across the whole available range.
        metrics = ["receipts_budget_total"]
    elif len(mentioned_metrics) >= 2:
        metrics = mentioned_metrics
    elif trend_requested and broad_msp_total:
        # The historical 1-МСП books before mid-2024 do not expose a
        # comparable insurance-contribution column.  The common long series is
        # therefore the budget-receipts indicator, never a partial sum.
        metrics = ["receipts_budget_total"]
    elif (
        broad_msp_total and metric == "receipts_budget_total"
    ) or metric is None and (
        "поступ" in normalized
        or "сколько" in normalized
        or "доля мсп" in normalized
    ):
        metrics = ["receipts_budget_total", "receipts_insurance_total"]
    elif (
        metric is None
        and industry_dimension_requested
        and (best_requested or regional_average_growth_requested)
    ):
        metrics = ["receipts_budget_total", "receipts_insurance_total"]
    elif metric is None:
        raise ValueError(
            "Не удалось однозначно определить показатель. Уточните налог или вид поступлений."
        )
    else:
        metrics = [metric]

    if "без микро" in normalized or "малых и средних" in normalized:
        groups = ["small_and_medium_without_micro"]
    elif "микро" in normalized:
        groups = ["micro"]
    else:
        groups = ["all_msp"]

    asks_outliers = any(
        token in normalized
        for token in (
            "выбива",
            "выброс",
            "аномал",
            "нетипич",
            "отклоняются от общей",
        )
    )
    asks_best = best_requested
    asks_overview = (
        "общая ситуация" in normalized
        or "в целом по рф" in normalized
        or "в целом по росс" in normalized
    )
    analysis_dimension = None
    if "регион" in normalized or "субъект" in normalized:
        analysis_dimension = "territory"
    elif (
        "отрасл" in normalized
        or "оквэд" in normalized
        or "вид деятельности" in normalized
    ):
        analysis_dimension = "industry"

    if "источник" in normalized or "откуда" in normalized:
        intent = "source"
    elif analytical_brief_requested:
        intent = "analytical_brief"
        analysis_dimension = "industry"
    elif regional_industry_leaders_requested:
        intent = "regional_industry_rank_growth"
        analysis_dimension = "territory"
    elif regional_average_growth_requested:
        intent = "regional_average_growth"
        analysis_dimension = "industry"
    elif asks_outliers:
        intent = "outliers"
        analysis_dimension = "territory"
    elif asks_overview:
        intent = "overview"
    elif asks_best or worst_requested:
        intent = (
            "rank"
            if re.search(r"\b(?:наи)?лучш\w*\b", normalized)
            and not any(
                token in normalized for token in ("динамик", "рост", "темп")
            )
            else "rank_difference"
            if "абсолют" in normalized
            else "rank_growth"
        )
        analysis_dimension = analysis_dimension or "industry"
    elif re.search(r"\bдол(?:я|ю|и|ей)\b", normalized):
        intent = "share"
    elif trend_requested:
        intent = "trend"
    elif len(periods) > 1 or "сравн" in normalized:
        intent = "compare"
    else:
        intent = "value"

    operations = ["value", "sources"]
    if intent in {
        "compare",
        "trend",
        "overview",
        "rank",
        "rank_growth",
        "rank_difference",
        "outliers",
        "regional_average_growth",
        "regional_industry_rank_growth",
        "analytical_brief",
    }:
        operations = ["value", "difference", "growth_rate", "sources"]
    elif intent == "share":
        operations = ["value", "share", "sources"]

    return QueryPlan(
        intent=intent,
        metric_codes=metrics,
        enterprise_groups=groups,
        territories=["Российская Федерация"],
        row_codes=[1010],
        periods=periods,
        period_modes=period_modes,
        period_labels=period_labels,
        operations=operations,
        group_by=(
            ["industry", "territory"]
            if intent
            in {
                "regional_average_growth",
                "regional_industry_rank_growth",
                "analytical_brief",
            }
            else [analysis_dimension]
            if analysis_dimension
            else []
        ),
        analysis_dimension=analysis_dimension,
        ranking_criteria=(
            ["value", "growth_rate"]
            if intent == "rank"
            else ["value"]
            if intent == "rank_difference"
            else ["growth_rate"]
            if intent == "rank_growth"
            or intent == "regional_industry_rank_growth"
            or intent == "analytical_brief"
            else []
        ),
        ranking_direction=(
            "both"
            if asks_best and worst_requested
            else "bottom"
            if worst_requested
            else "top"
        ),
        outlier_modes=(
            ["value", "growth_rate", "composite"]
            if intent == "outliers"
            else []
        ),
        limit=3
        if intent == "analytical_brief"
        else 5
        if intent in {
            "rank",
            "outliers",
            "rank_growth",
            "rank_difference",
            "regional_industry_rank_growth",
        }
        else None,
    )


def available_periods(session: Session) -> list[date]:
    return list(
        session.scalars(
            select(MspFact.period_end).distinct().order_by(MspFact.period_end)
        ).all()
    )


def _fact(
    session: Session,
    period: date,
    metric_code: str,
    group: str,
    row_code: int,
    period_mode: str = "cumulative_ytd",
    period_label: str | None = None,
    territory_name: str = "Российская Федерация",
    regional_cache: dict[tuple[date, str, int, int], list[RegionalValue]]
    | None = None,
) -> dict:
    metric = session.scalar(
        select(MetricDefinition).where(
            MetricDefinition.semantic_code == metric_code
        )
    )
    if metric is None:
        raise ValueError(f"Показатель {metric_code} отсутствует в каталоге")
    row = session.scalar(
        select(RowDefinition).where(RowDefinition.base_row_code == row_code)
    )
    if row is None:
        raise ValueError(f"Строка {row_code} отсутствует в каталоге")

    section = {
        "all_msp": "I",
        "micro": "II",
        "small_and_medium_without_micro": "III",
    }[group]
    cache = regional_cache if regional_cache is not None else {}

    def regional_values(
        source_section: str, source_period: date
    ) -> list[RegionalValue]:
        if metric.report_column_number is None:
            raise ValueError("У показателя не определён номер графы")
        key = (
            source_period,
            source_section,
            row_code,
            metric.report_column_number,
        )
        if key not in cache:
            cache[key] = read_regional_values(
                session,
                period=source_period,
                section=source_section,
                row_code=row_code,
                report_column=metric.report_column_number,
            )
        return cache[key]

    def regional_source(
        source_section: str, source_period: date
    ) -> tuple[Decimal, dict, str] | None:
        normalized = territory_key(territory_name)
        item = next(
            (
                candidate
                for candidate in regional_values(source_section, source_period)
                if territory_key(candidate.territory) == normalized
            ),
            None,
        )
        if item is None:
            return None
        fact_id = (
            f"regional-{source_section}-{source_period.isoformat()}-"
            f"{row_code}-{metric.report_column_number}-"
            f"{normalized.replace(' ', '-')}"
        )
        return item.value, item.source, fact_id

    def source_fact(source_section: str, source_period: date) -> MspFact | None:
        return session.scalar(
            select(MspFact)
            .join(SourceFile)
            .where(
                MspFact.period_end == source_period,
                MspFact.metric_id == metric.id,
                MspFact.row_definition_id == row.id,
                MspFact.section == source_section,
                SourceFile.status.in_(["valid", "valid_with_warnings"]),
            )
            .order_by(SourceFile.id.desc())
        )

    def section_fact(source_section: str) -> tuple[Decimal, dict, str]:
        if normalize_text(territory_name) != "российская федерация":
            current_regional = regional_source(source_section, period)
            if current_regional is None:
                raise ValueError(
                    f"Нет регионального факта {metric.name_ru}, "
                    f"{territory_name}, Раздел {source_section}, период {period}"
                )
            if period_mode != "standalone_quarter" or period.month == 3:
                return current_regional
            previous_month = period.month - 3
            previous_period = date(
                period.year,
                previous_month,
                monthrange(period.year, previous_month)[1],
            )
            previous_regional = regional_source(
                source_section, previous_period
            )
            if previous_regional is None:
                raise ValueError(
                    f"Для отдельного квартала {period_label or period} "
                    f"нет регионального источника за {previous_period}"
                )
            value = current_regional[0] - previous_regional[0]
            return (
                value,
                {
                    "file": "Производный региональный квартальный показатель",
                    "formula": (
                        f"{period_label or period} = накопительно на {period} − "
                        f"накопительно на {previous_period}"
                    ),
                    "lineage": [
                        current_regional[1],
                        previous_regional[1],
                    ],
                    "read_only": True,
                },
                f"derived-{current_regional[2]}-{previous_regional[2]}",
            )
        current = source_fact(source_section, period)
        if current is None:
            raise ValueError(
                f"Нет факта {metric.name_ru}, Раздел {source_section}, "
                f"период {period}"
            )
        if period_mode != "standalone_quarter" or period.month == 3:
            return (
                Decimal(current.value),
                _source_reference(current),
                f"fact-{current.id}",
            )
        if period.month not in {6, 9, 12}:
            raise ValueError(
                f"Период {period} не является концом квартала"
            )
        previous_month = period.month - 3
        previous_period = date(
            period.year,
            previous_month,
            monthrange(period.year, previous_month)[1],
        )
        previous = source_fact(source_section, previous_period)
        if previous is None:
            raise ValueError(
                f"Для отдельного квартала {period_label or period} нужен "
                f"накопительный источник за {previous_period}"
            )
        value = Decimal(current.value) - Decimal(previous.value)
        return (
            value,
            {
                "file": "Производный квартальный показатель",
                "sheet": "—",
                "cell": "—",
                "row_code": row_code,
                "report_column": metric.report_column_number,
                "formula": (
                    f"{period_label or period} = накопительно на {period} − "
                    f"накопительно на {previous_period}"
                ),
                "lineage": [
                    _source_reference(current),
                    _source_reference(previous),
                ],
                "read_only": True,
            },
            f"derived-quarter-{current.id}-{previous.id}",
        )

    if section == "III":
        section_i_value, section_i_source, section_i_id = section_fact("I")
        section_ii_value, section_ii_source, section_ii_id = section_fact("II")
        value = section_i_value - section_ii_value
        source = {
            "file": "Производный показатель",
            "sheet": "—",
            "cell": "—",
            "row_code": row_code,
            "report_column": metric.report_column_number,
            "formula": "Раздел III = Раздел I − Раздел II",
            "lineage": [
                section_i_source,
                section_ii_source,
            ],
        }
        fact_id = f"derived-{section_i_id}-{section_ii_id}"
    else:
        value, source, fact_id = section_fact(section)
    return {
        "fact_id": fact_id,
        "metric_code": metric.semantic_code,
        "metric": metric.name_ru,
        "value": str(value),
        "unit": metric.unit,
        "period_end": period.isoformat(),
        "period_mode": period_mode,
        "period_label": period_label or period.isoformat(),
        "section": section,
        "enterprise_group": group,
        "territory": territory_name,
        "row_code": row_code,
        "row_name": re.split(
            r"\s*\(стр\.?", row.name_ru, maxsplit=1, flags=re.IGNORECASE
        )[0].strip(),
        "okved": row.okved_text,
        "source": source,
    }


def _source_reference(fact: MspFact) -> dict:
    lineage = fact.lineage_json or {}
    return {
        "fact_id": fact.id,
        "file": PathLikeName(fact.source_file.absolute_path),
        "absolute_path": fact.source_file.absolute_path,
        "sheet": fact.source_sheet,
        "cell": fact.source_cell,
        "row_code": fact.source_row_code,
        "report_column": lineage.get(
            "source_report_column",
            fact.metric.report_column_number,
        ),
        "canonical_report_column": fact.metric.report_column_number,
        "semantic_code": fact.metric.semantic_code,
        "report_column_name": fact.metric.name_ru,
        "source_header": lineage.get("source_header"),
        "mapping_method": lineage.get("mapping_method"),
        "section": fact.section,
        "raw_value": str(fact.value),
        "unit": fact.unit,
        "read_only": True,
    }


def PathLikeName(value: str) -> str:
    return value.replace("\\", "/").rsplit("/", 1)[-1]


def execute_plan(session: Session, plan: QueryPlan) -> dict:
    if plan.intent == "catalog":
        result = execute_catalog_plan(session, plan)
        result.setdefault("status", "ready")
        result.setdefault("series", [])
        result.setdefault("visualization", None)
        return json_ready(result)
    if plan.intent in {
        "overview",
        "rank",
        "outliers",
        "rank_growth",
        "rank_difference",
        "regional_average_growth",
        "regional_industry_rank_growth",
        "analytical_brief",
    }:
        from .analytics_service import execute_analytic_plan

        return execute_analytic_plan(session, plan)
    facts: list[dict] = []
    calculations: list[dict] = []
    regional_cache: dict[
        tuple[date, str, int, int], list[RegionalValue]
    ] = {}
    period_modes = plan.period_modes or ["cumulative_ytd"] * len(plan.periods)
    period_labels = plan.period_labels or [
        period.isoformat() for period in plan.periods
    ]
    if len(period_modes) != len(plan.periods) or len(period_labels) != len(
        plan.periods
    ):
        raise ValueError(
            "Количество режимов/подписей периодов не совпадает с периодами"
        )
    resolved_territories = list(plan.territories)
    if plan.include_top_territory or plan.include_bottom_territory:
        ranking_period = plan.periods[-1]
        ranking_mode = period_modes[-1]
        ranking_label = period_labels[-1]
        seed_section = (
            "II" if plan.enterprise_groups[0] == "micro" else "I"
        )
        metric_seed = session.scalar(
            select(MetricDefinition).where(
                MetricDefinition.semantic_code == plan.metric_codes[0]
            )
        )
        if metric_seed is None or metric_seed.report_column_number is None:
            raise ValueError("Показатель для рейтинга не найден")
        candidates = read_regional_values(
            session,
            period=ranking_period,
            section=seed_section,
            row_code=plan.row_codes[0],
            report_column=metric_seed.report_column_number,
        )
        regional_cache[
            (
                ranking_period,
                seed_section,
                plan.row_codes[0],
                metric_seed.report_column_number,
            )
        ] = candidates
        ranked: list[tuple[Decimal, str]] = []
        for candidate in candidates:
            if candidate.territory_type != "region":
                continue
            fact = _fact(
                session,
                ranking_period,
                plan.metric_codes[0],
                plan.enterprise_groups[0],
                plan.row_codes[0],
                ranking_mode,
                ranking_label,
                candidate.territory,
                regional_cache,
            )
            ranked.append((Decimal(fact["value"]), candidate.territory))
        if not ranked:
            raise ValueError("В региональной книге нет субъектов для рейтинга")
        if plan.include_top_territory:
            top_name = max(ranked, key=lambda item: item[0])[1]
            if top_name not in resolved_territories:
                resolved_territories.append(top_name)
        if plan.include_bottom_territory:
            bottom_name = min(ranked, key=lambda item: item[0])[1]
            if bottom_name not in resolved_territories:
                resolved_territories.append(bottom_name)
        plan.territories = resolved_territories

    for period, period_mode, period_label in zip(
        plan.periods, period_modes, period_labels, strict=True
    ):
        for territory_name in resolved_territories:
            for group in plan.enterprise_groups:
                for row_code in plan.row_codes:
                    components = [
                        _fact(
                            session,
                            period,
                            metric_code,
                            group,
                            row_code,
                            period_mode,
                            period_label,
                            territory_name,
                            regional_cache,
                        )
                        for metric_code in plan.metric_codes
                    ]
                    combines_msp_total = {
                        item["metric_code"] for item in components
                    } == {
                        "receipts_budget_total",
                        "receipts_insurance_total",
                    }
                    if len(components) == 1 or not combines_msp_total:
                        facts.extend(components)
                    else:
                        total = sum(
                            (Decimal(item["value"]) for item in components),
                            Decimal("0"),
                        )
                        facts.append(
                            {
                                "fact_id": "derived-total-"
                                + "-".join(item["fact_id"] for item in components),
                                "metric_code": "msp_total",
                                "metric": "Поступления от МСП, всего",
                                "value": str(total),
                                "unit": "тыс. руб.",
                                "period_end": period.isoformat(),
                                "period_mode": period_mode,
                                "period_label": period_label,
                                "section": components[0]["section"],
                                "enterprise_group": group,
                                "territory": territory_name,
                                "row_code": row_code,
                                "row_name": components[0]["row_name"],
                                "okved": components[0]["okved"],
                                "source": {
                                    "file": "Производный показатель",
                                    "formula": "Поступления в бюджет + страховые взносы",
                                    "lineage": [
                                        item["source"] for item in components
                                    ],
                                },
                            }
                        )

    grouped: dict[tuple, list[dict]] = {}
    for fact in facts:
        key = (
            fact["metric_code"],
            fact["territory"],
            fact["enterprise_group"],
            fact["row_code"],
        )
        grouped.setdefault(key, []).append(fact)
    if len(grouped) > 8:
        raise ValueError(
            "Запрос образует более 8 независимых рядов. "
            "Сузьте показатели, территории, группы МСП или строки."
        )
    if len(facts) > 192:
        raise ValueError(
            "Запрос содержит более 192 точек. Сузьте диапазон или состав рядов."
        )

    group_names = {
        "all_msp": "все МСП",
        "micro": "микропредприятия",
        "small_and_medium_without_micro": "малые и средние без микро",
    }
    series: list[dict] = []
    for index, ((metric_code, territory, group, row_code), items) in enumerate(
        grouped.items(),
        start=1,
    ):
        ordered = sorted(items, key=lambda item: item["period_end"])
        first, last = ordered[0], ordered[-1]
        changes = []
        for old, new in zip(ordered, ordered[1:]):
            old_value, new_value = Decimal(old["value"]), Decimal(new["value"])
            changes.append(
                {
                    "from_period": old["period_end"],
                    "to_period": new["period_end"],
                    "difference": str(new_value - old_value),
                    "growth_rate": (
                        str(safe_ratio(new_value, old_value))
                        if old_value != 0
                        else None
                    ),
                }
            )
        first_value, last_value = Decimal(first["value"]), Decimal(last["value"])
        minimum = min(ordered, key=lambda item: Decimal(item["value"]))
        maximum = max(ordered, key=lambda item: Decimal(item["value"]))
        qualifiers = [territory, group_names[group]]
        if row_code != 1010:
            qualifiers.append(first.get("row_name") or f"строка {row_code}")
        label = f"{first['metric']} · " + " · ".join(qualifiers)
        series_key = f"series-{index}"
        series.append(
            {
                "key": series_key,
                "label": label,
                "metric_code": metric_code,
                "territory": territory,
                "enterprise_group": group,
                "row_code": row_code,
                "unit": first["unit"],
                "points": [
                    {
                        "period_end": item["period_end"],
                        "label": item.get("period_label", item["period_end"]),
                        "value": item["value"],
                        "fact_id": item["fact_id"],
                    }
                    for item in ordered
                ],
                "changes": changes,
                "overall_change": {
                    "difference": str(last_value - first_value),
                    "growth_rate": (
                        str(safe_ratio(last_value, first_value))
                        if first_value != 0
                        else None
                    ),
                    "from_period": first["period_end"],
                    "to_period": last["period_end"],
                },
                "minimum": {
                    "period_end": minimum["period_end"],
                    "label": minimum.get("period_label", minimum["period_end"]),
                    "value": minimum["value"],
                },
                "maximum": {
                    "period_end": maximum["period_end"],
                    "label": maximum.get("period_label", maximum["period_end"]),
                    "value": maximum["value"],
                },
            }
        )
        if len(ordered) >= 2 and "difference" in plan.operations:
            calculations.extend(
                [
                    {
                        "operation": "difference",
                        "series_key": series_key,
                        "value": str(last_value - first_value),
                        "unit": last["unit"],
                        "formula": "последний период − первый период",
                        "inputs": [first["fact_id"], last["fact_id"]],
                    },
                    {
                        "operation": "growth_rate",
                        "series_key": series_key,
                        "value": (
                            str(safe_ratio(last_value, first_value))
                            if first_value != 0
                            else None
                        ),
                        "unit": "%",
                        "formula": "последний период / первый период × 100",
                        "inputs": [first["fact_id"], last["fact_id"]],
                    },
                ]
            )

    answer = deterministic_answer(plan, facts, calculations, series)
    warnings = []
    return json_ready(
        {
            "status": "ready",
            "query_plan": plan.model_dump(mode="json"),
            "resolved_metrics": resolved_metric_details(
                session, plan.metric_codes
            ),
            "facts": facts,
            "calculations": calculations,
            "series": series,
            "visualization": (
                {
                    "type": "line",
                    "x_axis": "period",
                    "y_axis": "value",
                    "legend": True,
                    "table_alternative": True,
                }
                if plan.intent == "trend"
                else None
            ),
            "warnings": warnings,
            "answer": answer,
            "answer_mode": "deterministic",
            "numbers_verified": True,
        }
    )


def deterministic_answer(
    plan: QueryPlan,
    facts: list[dict],
    calculations: list[dict],
    series: list[dict] | None = None,
) -> str:
    if not facts:
        return "Подтверждённые факты по запросу не найдены."
    if plan.intent == "trend":
        descriptions: list[str] = []
        for item in series or []:
            divisor = (
                Decimal("1000000")
                if item["unit"] == "тыс. руб."
                else Decimal("1")
            )
            points = "; ".join(
                f"{point['label']} — "
                f"{ru_number(Decimal(point['value']) / divisor)}"
                for point in item["points"]
            )
            overall = item["overall_change"]
            growth = (
                ru_number(Decimal(overall["growth_rate"]))
                if overall["growth_rate"] is not None
                else "не рассчитывается"
            )
            minimum, maximum = item["minimum"], item["maximum"]
            descriptions.append(
                f"{item['label']}: {points}. Общее изменение: "
                f"{ru_number(Decimal(overall['difference']) / divisor, show_plus=True)} "
                f"{'млрд руб.' if item['unit'] == 'тыс. руб.' else item['unit']}; "
                f"темп — {growth}%. Минимум — {minimum['label']} "
                f"({ru_number(Decimal(minimum['value']) / divisor)}), максимум — "
                f"{maximum['label']} "
                f"({ru_number(Decimal(maximum['value']) / divisor)})."
            )
        return " ".join(descriptions)
    territories = list(dict.fromkeys(item["territory"] for item in facts))
    if len(territories) > 1:
        divisor = (
            Decimal("1000000")
            if facts[0]["unit"] == "тыс. руб."
            else Decimal("1")
        )
        distinct_periods = {
            item["period_end"] for item in facts
        }
        if len(distinct_periods) > 1:
            source_number = 1
            comparisons: list[str] = []
            for territory_name in territories:
                territory_facts = sorted(
                    [
                        item
                        for item in facts
                        if item["territory"] == territory_name
                    ],
                    key=lambda item: item["period_end"],
                )
                if len(territory_facts) < 2:
                    continue
                old, new = territory_facts[0], territory_facts[-1]
                old_value = Decimal(old["value"])
                new_value = Decimal(new["value"])
                growth = safe_ratio(new_value, old_value)
                comparisons.append(
                    f"{territory_name}: "
                    f"{old.get('period_label', old['period_end'])} — "
                    f"{ru_number(old_value / divisor)} млрд руб. "
                    f"[Источник {source_number}], "
                    f"{new.get('period_label', new['period_end'])} — "
                    f"{ru_number(new_value / divisor)} млрд руб. "
                    f"[Источник {source_number + 1}]; изменение "
                    f"{ru_number((new_value - old_value) / divisor, show_plus=True)} "
                    f"млрд руб., темп {ru_number(growth)}%"
                )
                source_number += 2
            return f"{facts[0]['metric']}: " + "; ".join(comparisons) + "."
        parts = [
            f"{item['territory']} — "
            f"{ru_number(Decimal(item['value']) / divisor)} млрд руб. "
            f"[Источник {index}]"
            for index, item in enumerate(facts, start=1)
        ]
        highest = max(facts, key=lambda item: Decimal(item["value"]))
        return (
            f"{facts[0]['metric']} за "
            f"{facts[0].get('period_label', facts[0]['period_end'])}: "
            + "; ".join(parts)
            + f". Наибольшее значение среди выбранных — "
            f"{highest['territory']}."
        )
    if plan.intent == "compare" and len(facts) >= 2:
        ordered = sorted(facts, key=lambda item: item["period_end"])
        old, new = ordered[0], ordered[-1]
        difference = next(
            (
                Decimal(item["value"])
                for item in calculations
                if item["operation"] == "difference"
            ),
            Decimal(new["value"]) - Decimal(old["value"]),
        )
        growth = next(
            (
                Decimal(item["value"])
                for item in calculations
                if item["operation"] == "growth_rate" and item["value"] is not None
            ),
            None,
        )
        divisor = Decimal("1000000") if new["unit"] == "тыс. руб." else Decimal("1")
        subject = new["metric"]
        if new.get("territory") != "Российская Федерация":
            subject += f" — {new['territory']}"
        if new.get("row_code") != 1010:
            subject += (
                f" — {new.get('row_name', 'строка ' + str(new['row_code']))}"
            )
            if new.get("okved"):
                subject += f" ({new['okved']})"
        return (
            f"{subject}: за {old.get('period_label', old['period_end'])} — "
            f"{ru_number(Decimal(old['value']) / divisor)} млрд руб. [Источник 1], "
            f"за {new.get('period_label', new['period_end'])} — "
            f"{ru_number(Decimal(new['value']) / divisor)} млрд руб. [Источник 2]. "
            f"Отклонение: {ru_number(difference / divisor, show_plus=True)} млрд руб.; "
            f"темп — {ru_number(growth)}%."
        )
    fact = facts[0]
    divisor = Decimal("1000000") if fact["unit"] == "тыс. руб." else Decimal("1")
    subject = fact["metric"]
    if fact.get("territory") != "Российская Федерация":
        subject += f" — {fact['territory']}"
    if fact.get("row_code") != 1010:
        subject += (
            f" — {fact.get('row_name', 'строка ' + str(fact['row_code']))}"
        )
        if fact.get("okved"):
            subject += f" ({fact['okved']})"
    return (
        f"{subject} за "
        f"{fact.get('period_label', fact['period_end'])} составили "
        f"{ru_number(Decimal(fact['value']) / divisor)} млрд руб. [Источник 1]."
    )
