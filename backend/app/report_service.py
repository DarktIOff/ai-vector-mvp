from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import distinct, select
from sqlalchemy.orm import Session

from .calculations import (
    BILLION_FROM_MSP,
    MspWorkbookSnapshot,
    REPORT_TAX_COLUMNS,
    _industry_definitions,
    period_words,
    safe_ratio,
)
from .catalog import METRIC_BY_COLUMN
from .config import Settings
from .llm import (
    DeterministicProvider,
    LLMProviderError,
    configured_provider,
)
from .models import SourceFile, Territory
from .regional import (
    RegionalValue,
    read_regional_report_values,
    territory_key,
    territory_type,
)
from .schemas import ReportSpecification
from .workbooks import normalize_text


ACTIVE_SOURCE_STATUSES = ("valid", "valid_with_warnings")
RF_NAME = "Российская Федерация"


def _period_label(period: date) -> str:
    return period_words(period.month, period.year)


def _source_periods(session: Session, workbook_kind: str) -> set[date]:
    return set(
        session.scalars(
            select(distinct(SourceFile.period_end)).where(
                SourceFile.workbook_kind == workbook_kind,
                SourceFile.status.in_(ACTIVE_SOURCE_STATUSES),
                SourceFile.period_end.is_not(None),
                SourceFile.series_kind == "cumulative_ytd",
            )
        ).all()
    )


def common_regional_report_periods(session: Session) -> list[date]:
    """Periods simultaneously available in regional I/II and national 1-MSP."""
    common = (
        _source_periods(session, "regional_section_i")
        & _source_periods(session, "regional_section_ii")
        & _source_periods(session, "msp_summary")
    )
    return sorted(common)


def _territory_aliases(name: str) -> set[str]:
    aliases = {name}
    normalized = normalize_text(name)
    if normalized.startswith("г. ") or normalized.startswith("город "):
        aliases.add(
            re.sub(r"^(?:город\s+|г[.]?\s+)", "", name, flags=re.I)
        )
    parenthetical = re.findall(r"\(([^()]+)\)", name)
    aliases.update(item.strip() for item in parenthetical if item.strip())
    without_parenthetical = re.sub(r"\s*\([^()]+\)\s*$", "", name).strip()
    aliases.add(without_parenthetical)
    if normalize_text(without_parenthetical).startswith("республика "):
        aliases.add(without_parenthetical.split(" ", 1)[1])
    replacements = {
        "Ханты-Мансийский автономный округ - Югра": {"ХМАО", "Югра"},
        "Ямало-Ненецкий автономный округ": {"ЯНАО"},
        "Ненецкий автономный округ": {"НАО"},
        "Еврейская автономная область": {"ЕАО"},
    }
    aliases.update(replacements.get(name, set()))
    return {item.strip() for item in aliases if item.strip()}


def _canonical_name_score(name: str) -> tuple[int, int, str]:
    normalized = normalize_text(name)
    score = 0
    if re.match(r"^г[.]\s+", name, flags=re.I):
        score += 40
    if "автономный округ" in normalized:
        score += 30
    if "(" in name and ")" in name:
        score += 20
    if "кузбасс" in normalized:
        score += 15
    if re.search(r"[.)]$", name) and name.count("(") != name.count(")"):
        score -= 100
    if re.search(r"г[.]\S", name, flags=re.I):
        score -= 20
    if "марий-эл" in normalized:
        score -= 2
    return score, len(name), name


def regional_report_territories(session: Session) -> list[dict]:
    grouped: dict[str, list[str]] = {}
    for item in session.scalars(select(Territory).order_by(Territory.name_ru)):
        if territory_type(item.name_ru) != "region":
            continue
        grouped.setdefault(territory_key(item.name_ru), []).append(item.name_ru)

    result: list[dict] = []
    for names in grouped.values():
        canonical = max(set(names), key=_canonical_name_score)
        aliases: set[str] = set()
        for name in names:
            aliases.update(_territory_aliases(name))
        aliases.update(_territory_aliases(canonical))
        result.append(
            {
                "name": canonical,
                "aliases": sorted(
                    aliases - {canonical},
                    key=lambda value: normalize_text(value),
                ),
            }
        )
    return sorted(result, key=lambda item: normalize_text(item["name"]))


def _comparable_pairs(periods: list[date]) -> list[dict]:
    available = {(item.year, item.month): item for item in periods}
    pairs: list[dict] = []
    for new_period in periods:
        old_period = available.get((new_period.year - 1, new_period.month))
        if old_period is None:
            continue
        pairs.append(
            {
                "old_period_end": old_period.isoformat(),
                "new_period_end": new_period.isoformat(),
                "old_label": _period_label(old_period),
                "new_label": _period_label(new_period),
            }
        )
    return pairs


def report_available_parameters(session: Session) -> dict:
    periods = common_regional_report_periods(session)
    comparable_pairs = _comparable_pairs(periods)
    recommended_pair = comparable_pairs[-1] if comparable_pairs else None
    return {
        "report_modes": [
            {
                "code": "region_vs_rf",
                "name": "Регион и РФ",
                "benchmark": RF_NAME,
            },
            {
                "code": "national",
                "name": "Российская Федерация",
                "benchmark": None,
            },
        ],
        "territories": regional_report_territories(session),
        "periods": [
            {
                "period_end": item.isoformat(),
                "label": _period_label(item),
                "series_kind": "cumulative_ytd",
                "months_count": item.month,
            }
            for item in periods
        ],
        "comparable_pairs": comparable_pairs,
        "recommended_pair": recommended_pair,
        "national_legacy_pair": {
            "old_period_end": "2025-06-30",
            "new_period_end": "2026-06-30",
            "old_label": _period_label(date(2025, 6, 30)),
            "new_label": _period_label(date(2026, 6, 30)),
        },
        "required_benchmark": RF_NAME,
    }


def resolve_report_territory(session: Session, value: str) -> str:
    requested = value.strip()
    if not requested:
        raise ValueError("Выберите регион")
    catalog = regional_report_territories(session)
    requested_normalized = normalize_text(requested)
    requested_key = territory_key(requested)
    exact: list[str] = []
    mentioned: set[str] = set()
    for item in catalog:
        references = [item["name"], *item["aliases"]]
        reference_norms = {normalize_text(reference) for reference in references}
        reference_keys = {territory_key(reference) for reference in references}
        if requested_normalized in reference_norms or requested_key in reference_keys:
            exact.append(item["name"])
            continue
        for reference in references:
            normalized = normalize_text(reference)
            if len(normalized) >= 5 and re.search(
                rf"(?<![а-яa-z]){re.escape(normalized)}(?![а-яa-z])",
                requested_normalized,
            ):
                mentioned.add(item["name"])
                break
    exact = list(dict.fromkeys(exact))
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1 or len(mentioned) > 1:
        raise ValueError(
            "Стандартная справка принимает один регион. Сравнение двух "
            "регионов доступно как аналитический запрос."
        )
    if len(mentioned) == 1:
        return next(iter(mentioned))
    raise ValueError(f"Неизвестный регион: {value}")


def validate_region_report_parameters(
    session: Session,
    *,
    territory: str | None,
    old_period_end: date | None,
    new_period_end: date | None,
) -> tuple[str, date, date]:
    canonical_territory = resolve_report_territory(session, territory or "")
    options = report_available_parameters(session)
    if old_period_end is None and new_period_end is None:
        recommended = options["recommended_pair"]
        if recommended is None:
            raise ValueError("Нет рекомендуемой сопоставимой пары периодов")
        old_period_end = date.fromisoformat(recommended["old_period_end"])
        new_period_end = date.fromisoformat(recommended["new_period_end"])
    elif old_period_end is None or new_period_end is None:
        raise ValueError("Нужно указать оба периода сравнения")

    if old_period_end >= new_period_end:
        raise ValueError("Периоды должны идти в хронологическом порядке")
    if old_period_end.month != new_period_end.month:
        raise ValueError(
            "Накопительные периоды должны иметь одинаковый месяц окончания"
        )
    available = set(common_regional_report_periods(session))
    missing = [
        item.isoformat()
        for item in (old_period_end, new_period_end)
        if item not in available
    ]
    if missing:
        raise ValueError(
            "Период отсутствует одновременно в региональных разделах I/II "
            "и общероссийской 1-МСП: " + ", ".join(missing)
        )
    return canonical_territory, old_period_end, new_period_end


def _national_summary_path(session: Session, period: date) -> Path:
    sources = session.scalars(
        select(SourceFile)
        .where(
            SourceFile.workbook_kind == "msp_summary",
            SourceFile.period_end == period,
            SourceFile.status.in_(ACTIVE_SOURCE_STATUSES),
        )
        .order_by(SourceFile.id.desc())
    ).all()
    for source in sources:
        path = Path(source.absolute_path)
        if path.is_file():
            return path
    raise ValueError(
        f"Нет общероссийской книги 1-МСП за {period.isoformat()}"
    )


def _pair(old: Decimal, new: Decimal, sources: list[dict]) -> dict:
    return {
        "old": old,
        "new": new,
        "growth_rate": safe_ratio(new, old),
        "difference": new - old,
        "sources": sources,
    }


def _national_metric_pair(
    old_snapshot: MspWorkbookSnapshot,
    new_snapshot: MspWorkbookSnapshot,
    report_column: int,
) -> dict:
    old = old_snapshot.value("I", 1010, report_column)
    new = new_snapshot.value("I", 1010, report_column)
    return _pair(
        old.value / BILLION_FROM_MSP,
        new.value / BILLION_FROM_MSP,
        [old.evidence.to_dict(), new.evidence.to_dict()],
    )


def _derived_regional_source(
    section_i: RegionalValue,
    section_ii: RegionalValue,
    *,
    value: Decimal,
    period: date,
    row_code: int,
    report_column: int,
    territory: str,
) -> dict:
    metric = METRIC_BY_COLUMN[report_column]
    return {
        "file": "Производный показатель региональных разделов I/II",
        "absolute_path": "—",
        "sheet": "Производный показатель",
        "cell": "—",
        "row_code": row_code,
        "report_column": report_column,
        "canonical_report_column": report_column,
        "report_column_name": metric.name,
        "semantic_code": metric.code,
        "section": "III",
        "territory": territory,
        "period_end": period.isoformat(),
        "raw_value": str(value),
        "raw_unit": "тыс. руб.",
        "conversion": "тыс. руб. ÷ 1 000 000 = млрд руб.",
        "formula": "малые и средние без микро = Раздел I − Раздел II",
        "lineage": [section_i.source, section_ii.source],
        "read_only": True,
    }


def build_region_vs_rf_report_data(
    session: Session,
    codes_path: Path,
    *,
    territory: str,
    old_period_end: date,
    new_period_end: date,
) -> dict:
    canonical, old_period_end, new_period_end = validate_region_report_parameters(
        session,
        territory=territory,
        old_period_end=old_period_end,
        new_period_end=new_period_end,
    )
    definitions = _industry_definitions(codes_path)
    industry_rows = [item["base_row_code"] for item in definitions]
    report_columns = list(dict.fromkeys([4, 36, *REPORT_TAX_COLUMNS]))
    columns_by_row = {1010: report_columns}
    columns_by_row.update({row_code: [4] for row_code in industry_rows})
    row_codes = [1010, *industry_rows]

    regional: dict[tuple[date, str], dict[tuple[int, int], RegionalValue]] = {}
    for period in (old_period_end, new_period_end):
        for section in ("I", "II"):
            regional[(period, section)] = read_regional_report_values(
                session,
                period=period,
                section=section,
                row_codes=row_codes,
                report_columns=report_columns,
                report_columns_by_row=columns_by_row,
                territory=canonical,
            )

    old_national_path = _national_summary_path(session, old_period_end)
    new_national_path = _national_summary_path(session, new_period_end)
    old_national = MspWorkbookSnapshot(
        old_national_path, old_period_end.isoformat()
    )
    new_national = MspWorkbookSnapshot(
        new_national_path, new_period_end.isoformat()
    )

    def regional_metric_pair(section: str, row_code: int, column: int) -> dict:
        values: list[Decimal] = []
        sources: list[dict] = []
        for period in (old_period_end, new_period_end):
            section_i = regional[(period, "I")][(row_code, column)]
            section_ii = regional[(period, "II")][(row_code, column)]
            if section == "I":
                raw_value = section_i.value
                source = section_i.source
            elif section == "II":
                raw_value = section_ii.value
                source = section_ii.source
            else:
                raw_value = section_i.value - section_ii.value
                source = _derived_regional_source(
                    section_i,
                    section_ii,
                    value=raw_value,
                    period=period,
                    row_code=row_code,
                    report_column=column,
                    territory=canonical,
                )
            values.append(raw_value / BILLION_FROM_MSP)
            sources.append(source)
        return _pair(values[0], values[1], sources)

    rf_budget = _national_metric_pair(old_national, new_national, 4)
    rf_insurance = _national_metric_pair(old_national, new_national, 36)
    rf_total = _pair(
        rf_budget["old"] + rf_insurance["old"],
        rf_budget["new"] + rf_insurance["new"],
        [*rf_budget["sources"], *rf_insurance["sources"]],
    )

    region_budget = regional_metric_pair("I", 1010, 4)
    region_insurance = regional_metric_pair("I", 1010, 36)
    region_total = _pair(
        region_budget["old"] + region_insurance["old"],
        region_budget["new"] + region_insurance["new"],
        [*region_budget["sources"], *region_insurance["sources"]],
    )
    old_share = safe_ratio(region_total["old"], rf_total["old"])
    new_share = safe_ratio(region_total["new"], rf_total["new"])
    if old_share is None or new_share is None:
        raise ValueError("Нельзя рассчитать долю региона: поступления МСП РФ равны нулю")
    region_share = _pair(
        old_share,
        new_share,
        [*region_total["sources"], *rf_total["sources"]],
    )

    micro_budget = regional_metric_pair("II", 1010, 4)
    micro_insurance = regional_metric_pair("II", 1010, 36)
    micro_total = _pair(
        micro_budget["old"] + micro_insurance["old"],
        micro_budget["new"] + micro_insurance["new"],
        [*micro_budget["sources"], *micro_insurance["sources"]],
    )
    small_budget = regional_metric_pair("III", 1010, 4)
    small_insurance = regional_metric_pair("III", 1010, 36)
    small_total = _pair(
        small_budget["old"] + small_insurance["old"],
        small_budget["new"] + small_insurance["new"],
        [*small_budget["sources"], *small_insurance["sources"]],
    )

    table1 = [
        {
            "code": "rf_msp_total",
            "name": (
                "Российская Федерация — всего от МСП "
                "(консолидированный бюджет + страховые взносы)"
            ),
            **rf_total,
        },
        {
            "code": "rf_msp_budget",
            "name": (
                "РФ — поступления в консолидированный бюджет "
                "(без страховых взносов)"
            ),
            **rf_budget,
        },
        {
            "code": "rf_msp_insurance",
            "name": "РФ — страховые взносы",
            **rf_insurance,
        },
        {
            "code": "region_msp_total",
            "name": (
                f"{canonical} — всего от МСП "
                "(консолидированный бюджет + страховые взносы)"
            ),
            **region_total,
        },
        {
            "code": "region_share_rf_msp",
            "name": (
                f"Доля региона «{canonical}» в совокупных поступлениях "
                "от МСП по РФ (бюджет + взносы), %"
            ),
            **region_share,
            "growth_rate": None,
        },
        {
            "code": "region_msp_budget",
            "name": (
                f"{canonical} — поступления в консолидированный бюджет "
                "(без страховых взносов)"
            ),
            **region_budget,
        },
        {
            "code": "region_msp_insurance",
            "name": f"{canonical} — страховые взносы",
            **region_insurance,
        },
        {
            "code": "micro_total",
            "name": (
                f"{canonical} — всего по микропредприятиям "
                "(бюджет + взносы)"
            ),
            **micro_total,
        },
        {
            "code": "micro_budget",
            "name": (
                "Микро — поступления в консолидированный бюджет региона "
                "(без страховых взносов)"
            ),
            **micro_budget,
        },
        {
            "code": "micro_insurance",
            "name": "Микро — страховые взносы",
            **micro_insurance,
        },
        {
            "code": "small_medium_total",
            "name": (
                f"{canonical} — всего по малым и средним без микро "
                "(бюджет + взносы)"
            ),
            **small_total,
        },
        {
            "code": "small_medium_budget",
            "name": (
                "Малые и средние без микро — поступления в "
                "консолидированный бюджет региона (без страховых взносов)"
            ),
            **small_budget,
        },
        {
            "code": "small_medium_insurance",
            "name": "Малые и средние без микро — страховые взносы",
            **small_insurance,
        },
    ]

    table2 = [
        {
            "code": "region_msp_budget",
            "name": (
                "Поступления от МСП в консолидированный бюджет региона, "
                "всего (без страховых взносов)"
            ),
            "group": "all_msp",
            **region_budget,
        }
    ]
    for report_column in REPORT_TAX_COLUMNS:
        for section, group in (
            ("I", "all_msp"),
            ("II", "micro"),
            ("III", "small_and_medium_without_micro"),
        ):
            table2.append(
                {
                    "code": METRIC_BY_COLUMN[report_column].code,
                    "name": METRIC_BY_COLUMN[report_column].name,
                    "group": group,
                    **regional_metric_pair(section, 1010, report_column),
                }
            )

    industries: list[dict] = []
    for definition in definitions:
        groups = {
            group: regional_metric_pair(
                section, definition["base_row_code"], 4
            )
            for section, group in (
                ("I", "all_msp"),
                ("II", "micro"),
                ("III", "small_and_medium_without_micro"),
            )
        }
        industries.append(
            {
                **definition,
                "groups": groups,
                "growth_rate": groups["all_msp"]["growth_rate"],
                "difference": groups["all_msp"]["difference"],
                "share": safe_ratio(
                    groups["all_msp"]["new"], region_budget["new"]
                ),
            }
        )
    industries.sort(
        key=lambda item: (
            item["growth_rate"]
            if item["growth_rate"] is not None
            else Decimal("-1e99")
        ),
        reverse=True,
    )

    regional_paths = sorted(
        {
            source["absolute_path"]
            for values in regional.values()
            for item in values.values()
            for source in [item.source]
        }
    )
    return {
        "report_type": "1-msp",
        "report_mode": "region_vs_rf",
        "territory": canonical,
        "benchmark": RF_NAME,
        "old_year": old_period_end.year,
        "new_year": new_period_end.year,
        "months_count": new_period_end.month,
        "old_period_end": old_period_end.isoformat(),
        "new_period_end": new_period_end.isoformat(),
        "period_text": _period_label(new_period_end),
        "table1": table1,
        "table2": table2,
        "industries": industries,
        "warnings": [],
        "input_files": {
            "old_national_msp": str(old_national_path.resolve()),
            "new_national_msp": str(new_national_path.resolve()),
            "regional_sections": regional_paths,
            "codes": str(codes_path.resolve()),
        },
    }


def _parse_json_object(raw: str) -> dict[str, Any]:
    candidate = re.sub(
        r"<think>.*?</think>",
        "",
        raw.strip(),
        flags=re.IGNORECASE | re.DOTALL,
    )
    candidate = re.sub(r"```(?:json)?", "", candidate, flags=re.IGNORECASE)
    decoder = json.JSONDecoder()
    objects: list[dict[str, Any]] = []
    for match in re.finditer(r"\{", candidate):
        try:
            parsed, _ = decoder.raw_decode(candidate[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            objects.append(parsed)
    if not objects:
        raise ValueError("Ответ GNIVC LOCAL не содержит JSON-спецификацию")
    return objects[-1]


async def interpret_report_command(
    session: Session,
    settings: Settings,
    command: str,
) -> ReportSpecification:
    options = report_available_parameters(session)
    if not options["comparable_pairs"]:
        return ReportSpecification(
            status="no_data",
            no_data_reason="Нет сопоставимых региональных периодов",
        )
    provider = configured_provider(settings)
    if isinstance(provider, DeterministicProvider) or provider.name != "gnivc_qwen":
        raise LLMProviderError(
            "qwen_not_configured",
            "GNIVC LOCAL недоступен; заполните форму вручную",
        )

    model_catalog = {
        "territories": options["territories"],
        "periods": options["periods"],
        "comparable_pairs": options["comparable_pairs"],
        "recommended_pair": options["recommended_pair"],
        "national_legacy_pair": options["national_legacy_pair"],
    }
    system_prompt = f"""
Ты заполняешь параметры стандартной справки 1-МСП. Верни только один JSON.
Числа и содержание отчёта ты не рассчитываешь. Используй только этот каталог:
{json.dumps(model_catalog, ensure_ascii=False)}

Контракт JSON:
{{
  "status": "ready|clarification|no_data",
  "report_mode": "region_vs_rf|national|null",
  "territory": "точное каноническое имя из каталога или Российская Федерация|null",
  "benchmark": "Российская Федерация|null",
  "old_period_end": "YYYY-MM-DD|null",
  "new_period_end": "YYYY-MM-DD|null",
  "clarification_question": "строка|null",
  "no_data_reason": "строка|null"
}}

Для одного региона выбери region_vs_rf и обязательный benchmark РФ. Если даты
не названы, возьми recommended_pair. Для РФ без региона выбери national и
national_legacy_pair. Формулировки «по стране», «по России», «по РФ» и
«по Российской Федерации» означают national. Не объединяй два региона:
объясни, что их сравнение доступно как аналитический запрос. Не угадывай
неизвестные территории или отсутствующие периоды. Для ready заполни все параметры.
""".strip()
    raw = await provider.complete(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": command},
        ],
        temperature=0,
        max_tokens=1200,
        response_format={"type": "json_object"},
        allow_fallback=False,
    )
    specification = ReportSpecification.model_validate(_parse_json_object(raw))
    if specification.status != "ready":
        return specification

    if specification.report_mode == "region_vs_rf":
        try:
            canonical, old_period, new_period = validate_region_report_parameters(
                session,
                territory=specification.territory,
                old_period_end=specification.old_period_end,
                new_period_end=specification.new_period_end,
            )
        except ValueError as exc:
            return ReportSpecification(
                status="no_data",
                no_data_reason=str(exc),
            )
        return specification.model_copy(
            update={
                "territory": canonical,
                "benchmark": RF_NAME,
                "old_period_end": old_period,
                "new_period_end": new_period,
            }
        )

    legacy = options["national_legacy_pair"]
    expected = (
        date.fromisoformat(legacy["old_period_end"]),
        date.fromisoformat(legacy["new_period_end"]),
    )
    actual = (specification.old_period_end, specification.new_period_end)
    if actual != expected:
        return ReportSpecification(
            status="no_data",
            no_data_reason=(
                "Стандартный национальный шаблон доступен только за "
                f"{legacy['old_label']} и {legacy['new_label']}"
            ),
        )
    return specification.model_copy(
        update={"territory": RF_NAME, "benchmark": None}
    )
