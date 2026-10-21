from __future__ import annotations

from collections import defaultdict
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .catalog import resolved_metric_details
from .models import (
    MetricDefinition,
    MspFact,
    RowDefinition,
    SourceFile,
    Territory,
)
from .regional import (
    inspect_regional_workbook,
    read_regional_values,
    resolve_regional_metrics,
    territory_key,
)
from .schemas import QueryPlan


MONTH_NAMES = {
    1: "январь",
    2: "январь–февраль",
    3: "I квартал",
    4: "январь–апрель",
    5: "январь–май",
    6: "первое полугодие",
    7: "январь–июль",
    8: "январь–август",
    9: "9 месяцев",
    10: "январь–октябрь",
    11: "январь–ноябрь",
    12: "год",
}


def _period_label(period: date) -> str:
    return f"{MONTH_NAMES[period.month]} {period.year}"


def _ru_count(value: int, one: str, few: str, many: str) -> str:
    remainder_100 = value % 100
    remainder_10 = value % 10
    if 11 <= remainder_100 <= 14:
        word = many
    elif remainder_10 == 1:
        word = one
    elif 2 <= remainder_10 <= 4:
        word = few
    else:
        word = many
    return f"{value} {word}"


def _regional_scope(plan: QueryPlan) -> bool:
    return (
        "territory" in plan.group_by
        or any(
            territory != "Российская Федерация"
            for territory in plan.territories
        )
    )


def _regional_source_periods(
    session: Session, section: str
) -> set[date]:
    kind = (
        "regional_section_i" if section == "I" else "regional_section_ii"
    )
    return set(
        session.scalars(
            select(SourceFile.period_end).where(
                SourceFile.workbook_kind == kind,
                SourceFile.status.in_(["valid", "valid_with_warnings"]),
                SourceFile.period_end.is_not(None),
            )
        ).all()
    )


def _sections(plan: QueryPlan) -> list[str]:
    groups = set(plan.enterprise_groups)
    sections: list[str] = []
    if "all_msp" in groups or "small_and_medium_without_micro" in groups:
        sections.append("I")
    if "micro" in groups or "small_and_medium_without_micro" in groups:
        sections.append("II")
    return sections or ["I"]


def _regional_periods(
    session: Session,
    plan: QueryPlan,
    *,
    latest_only: bool = False,
) -> list[date]:
    sections = _sections(plan)
    period_sets = [_regional_source_periods(session, item) for item in sections]
    candidates = (
        sorted(
            set.intersection(*period_sets),
            reverse=latest_only,
        )
        if period_sets
        else []
    )
    metric_codes = plan.metric_codes or ["receipts_budget_total"]
    metrics = {
        item.semantic_code: item
        for item in session.scalars(
            select(MetricDefinition).where(
                MetricDefinition.semantic_code.in_(metric_codes)
            )
        ).all()
    }
    if any(
        code not in metrics
        or metrics[code].report_column_number is None
        for code in metric_codes
    ):
        return []
    requested = {
        territory_key(item)
        for item in plan.territories
        if item != "Российская Федерация"
    }
    available: list[date] = []
    for period in candidates:
        period_ok = True
        for section in sections:
            for row_code in plan.row_codes:
                for metric_code in metric_codes:
                    try:
                        values = read_regional_values(
                            session,
                            period=period,
                            section=section,
                            row_code=row_code,
                            report_column=(
                                metrics[
                                    metric_code
                                ].report_column_number
                            ),
                        )
                    except ValueError:
                        period_ok = False
                        break
                    present = {
                        territory_key(item.territory) for item in values
                    }
                    if requested and not requested.issubset(present):
                        period_ok = False
                        break
                if not period_ok:
                    break
            if not period_ok:
                break
        if period_ok:
            available.append(period)
            if latest_only:
                return available
    return sorted(available)


def _national_periods(session: Session, plan: QueryPlan) -> list[date]:
    metric_code = plan.metric_codes[0] if plan.metric_codes else None
    statement = (
        select(MspFact.period_end)
        .join(MetricDefinition)
        .join(RowDefinition)
        .where(RowDefinition.base_row_code == plan.row_codes[0])
        .distinct()
        .order_by(MspFact.period_end)
    )
    if metric_code:
        statement = statement.where(
            MetricDefinition.semantic_code == metric_code
        )
    return list(session.scalars(statement).all())


def _period_answer(
    periods: list[date], plan: QueryPlan, regional: bool
) -> tuple[str, list[dict], dict]:
    if not periods:
        scope = "региональных" if regional else "общероссийских"
        return (
            f"Подтверждённых {scope} периодов для этих фильтров не найдено.",
            [],
            {"total_count": 0, "latest": None},
        )
    by_year: dict[int, list[date]] = defaultdict(list)
    for period in periods:
        by_year[period.year].append(period)
    year_summary = "; ".join(
        f"{year}: "
        + ", ".join(MONTH_NAMES[item.month] for item in year_periods)
        for year, year_periods in sorted(by_year.items())
    )
    pairs = [
        (older, newer)
        for newer in periods
        for older in periods
        if newer.year == older.year + 1 and newer.month == older.month
    ]
    latest = periods[-1]
    pair_text = ""
    if pairs:
        older, newer = max(pairs, key=lambda pair: pair[1])
        pair_text = (
            f" Последняя межгодовая сопоставимая пара: "
            f"{_period_label(older)} и {_period_label(newer)}."
        )
    named_territories = [
        item
        for item in plan.territories
        if item != "Российская Федерация"
    ]
    territory_text = (
        " для выбранных территорий ("
        + "; ".join(named_territories)
        + ")"
        if regional and named_territories
        else ""
    )
    answer = (
        f"Доступно {len(periods)} подтверждённых накопительных периодов"
        f"{territory_text}. Последний общий период — {_period_label(latest)} "
        f"({latest.isoformat()}).{pair_text} По годам: {year_summary}."
    )
    items = [
        {
            "kind": "period",
            "code": item.isoformat(),
            "label": _period_label(item),
            "detail": "накопительно с начала года",
        }
        for item in reversed(periods)
    ]
    return (
        answer,
        items,
        {
            "total_count": len(periods),
            "latest": latest.isoformat(),
            "comparable_pairs": [
                [older.isoformat(), newer.isoformat()]
                for older, newer in pairs
            ],
        },
    )


def _territories(
    session: Session,
) -> tuple[str, list[dict], dict]:
    source = session.scalar(
        select(SourceFile)
        .where(
            SourceFile.workbook_kind == "regional_section_i",
            SourceFile.status.in_(["valid", "valid_with_warnings"]),
            SourceFile.period_end.is_not(None),
        )
        .order_by(SourceFile.period_end.desc(), SourceFile.id.desc())
    )
    if source is None or source.period_end is None:
        return "Региональные источники не зарегистрированы.", [], {
            "total_count": 0
        }
    values = read_regional_values(
        session,
        period=source.period_end,
        section="I",
        row_code=1010,
        report_column=4,
    )
    names = sorted(
        {
            item.territory
            for item in values
            if item.territory_type == "region"
        }
    )
    preview = ", ".join(names[:20])
    answer = (
        f"В последнем региональном источнике за "
        f"{_period_label(source.period_end)} доступны {len(names)} субъектов РФ. "
        f"Первые в алфавитном порядке: {preview}."
    )
    return (
        answer,
        [
            {
                "kind": "territory",
                "code": territory_key(name),
                "label": name,
                "detail": "субъект Российской Федерации",
            }
            for name in names
        ],
        {
            "total_count": len(names),
            "period_end": source.period_end.isoformat(),
        },
    )


def _metrics(
    session: Session, regional: bool
) -> tuple[str, list[dict], dict]:
    metrics = session.scalars(
        select(MetricDefinition).order_by(
            MetricDefinition.report_column_number
        )
    ).all()
    if regional:
        source = session.scalar(
            select(SourceFile)
            .where(
                SourceFile.workbook_kind == "regional_section_i",
                SourceFile.status.in_(["valid", "valid_with_warnings"]),
            )
            .order_by(SourceFile.period_end.desc(), SourceFile.id.desc())
        )
        if source is not None:
            layout = inspect_regional_workbook(Path(source.absolute_path))
            available_codes = set(
                resolve_regional_metrics(
                    session,
                    source=source,
                    layout=layout,
                    section="I",
                    row_code=1010,
                ).resolution.source_to_code.values()
            )
            metrics = [
                item
                for item in metrics
                if item.semantic_code in available_codes
            ]
    codes = [item.semantic_code for item in metrics]
    details = resolved_metric_details(session, codes)
    items = [
        {
            "kind": "metric",
            "code": item["semantic_code"],
            "label": item["display_name"],
            "detail": f"семантический показатель · {item['name_ru']}",
        }
        for item in details
    ]
    preview = ", ".join(item["label"] for item in items[:15])
    scope = "региональной форме" if regional else "каталоге 1-МСП"
    return (
        f"В {scope} доступны "
        f"{_ru_count(len(items), 'показатель', 'показателя', 'показателей')}. "
        f"Основные: {preview}.",
        items,
        {"total_count": len(items), "regional_scope": regional},
    )


def _okved(
    session: Session, regional: bool
) -> tuple[str, list[dict], dict]:
    available_codes: set[int] | None = None
    if regional:
        source = session.scalar(
            select(SourceFile)
            .where(
                SourceFile.workbook_kind == "regional_section_i",
                SourceFile.status.in_(["valid", "valid_with_warnings"]),
            )
            .order_by(SourceFile.period_end.desc(), SourceFile.id.desc())
        )
        if source is not None:
            layout = inspect_regional_workbook(Path(source.absolute_path))
            available_codes = {
                int(name)
                for name in layout.sheet_names
                if name.isdigit()
            }
    rows = session.scalars(
        select(RowDefinition)
        .where(RowDefinition.okved_text.is_not(None))
        .order_by(RowDefinition.base_row_code)
    ).all()
    if available_codes is not None:
        rows = [
            item for item in rows if item.base_row_code in available_codes
        ]
    items = [
        {
            "kind": "okved",
            "code": str(item.base_row_code),
            "label": item.okved_text or f"строка {item.base_row_code}",
            "detail": item.name_ru.split("(стр", 1)[0].strip(),
        }
        for item in rows
    ]
    preview = "; ".join(
        f"{item['label']} — {item['detail']}" for item in items[:12]
    )
    return (
        f"Доступно "
        f"{_ru_count(len(items), 'строка', 'строки', 'строк')} "
        "с кодами или диапазонами ОКВЭД. "
        f"Начало справочника: {preview}.",
        items,
        {"total_count": len(items), "regional_scope": regional},
    )


def _overview(
    session: Session, plan: QueryPlan
) -> tuple[str, list[dict], dict]:
    _, territory_items, territory_summary = _territories(session)
    _, metric_items, metric_summary = _metrics(session, False)
    _, okved_items, okved_summary = _okved(session, False)
    national_periods = _national_periods(session, plan)
    regional_plan = plan.model_copy(
        update={
            "group_by": ["territory"],
            "territories": ["Российская Федерация"],
        }
    )
    regional_periods = _regional_periods(session, regional_plan)
    national_latest = national_periods[-1] if national_periods else None
    regional_latest = regional_periods[-1] if regional_periods else None
    answer = (
        f"В загруженных данных доступны "
        f"{_ru_count(territory_summary['total_count'], 'субъект', 'субъекта', 'субъектов')} "
        f"РФ, {_ru_count(metric_summary['total_count'], 'показатель', 'показателя', 'показателей')} "
        f"формы 1-МСП и "
        f"{_ru_count(okved_summary['total_count'], 'строка', 'строки', 'строк')} "
        f"с ОКВЭД. "
        f"Последний общероссийский период — "
        f"{_period_label(national_latest) if national_latest else 'не найден'}; "
        f"последний региональный период — "
        f"{_period_label(regional_latest) if regional_latest else 'не найден'}. "
        "Можно отдельно спросить о доступных периодах, регионах, налогах "
        "или кодах ОКВЭД."
    )
    items = [
        {
            "kind": "overview",
            "code": "territories",
            "label": "Субъекты РФ",
            "detail": str(len(territory_items)),
        },
        {
            "kind": "overview",
            "code": "metrics",
            "label": "Показатели формы 1-МСП",
            "detail": str(len(metric_items)),
        },
        {
            "kind": "overview",
            "code": "okved",
            "label": "Строки с ОКВЭД",
            "detail": str(len(okved_items)),
        },
        {
            "kind": "overview",
            "code": "regional_latest",
            "label": "Последний региональный период",
            "detail": (
                _period_label(regional_latest)
                if regional_latest
                else "не найден"
            ),
        },
    ]
    return (
        answer,
        items,
        {
            "territories_count": len(territory_items),
            "metrics_count": len(metric_items),
            "okved_count": len(okved_items),
            "national_latest": (
                national_latest.isoformat() if national_latest else None
            ),
            "regional_latest": (
                regional_latest.isoformat() if regional_latest else None
            ),
        },
    )


def execute_catalog_plan(session: Session, plan: QueryPlan) -> dict:
    target = plan.catalog_target
    regional = _regional_scope(plan)
    if target == "overview":
        answer, items, summary = _overview(session, plan)
    elif target == "periods":
        periods = (
            _regional_periods(session, plan)
            if regional
            else _national_periods(session, plan)
        )
        answer, items, summary = _period_answer(periods, plan, regional)
    elif target == "territories":
        answer, items, summary = _territories(session)
    elif target == "metrics":
        answer, items, summary = _metrics(session, regional)
    elif target == "okved":
        answer, items, summary = _okved(session, regional)
    else:
        raise ValueError("Неизвестный тип каталожного запроса")
    limit = plan.limit or 50
    truncated = len(items) > limit
    visible_items = items[:limit]
    warnings = (
        [
            f"Показаны первые {limit} из {len(items)} позиций; "
            "полный каталог доступен через API."
        ]
        if truncated
        else []
    )
    return {
        "query_plan": plan.model_dump(mode="json"),
        "resolved_metrics": resolved_metric_details(
            session, plan.metric_codes
        ),
        "facts": [],
        "calculations": [],
        "catalog_items": visible_items,
        "catalog_summary": {**summary, "truncated": truncated},
        "warnings": warnings,
        "answer": answer,
        "answer_mode": "deterministic_catalog",
        "numbers_verified": True,
    }
