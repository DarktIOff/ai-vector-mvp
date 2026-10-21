from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import MetricDefinition, Territory
from .workbooks import normalize_text


@dataclass(frozen=True)
class MetricSeed:
    column: int
    code: str
    name: str
    aliases: tuple[str, ...] = ()
    short_name: str | None = None


@dataclass(frozen=True)
class HeaderMetricResolution:
    source_to_code: dict[int, str]
    unmapped: dict[int, str]
    duplicate_codes: dict[str, tuple[int, ...]]

    @property
    def is_complete(self) -> bool:
        return not self.unmapped and not self.duplicate_codes


METRICS: tuple[MetricSeed, ...] = (
    MetricSeed(1, "entity_count", "Количество субъектов МСП", ("количество мсп",)),
    MetricSeed(2, "accrued_budget_total", "Начислено в консолидированный бюджет"),
    MetricSeed(3, "accrued_insurance_total", "Начислено страховых взносов"),
    MetricSeed(
        4,
        "receipts_budget_total",
        "Поступления в консолидированный бюджет",
        ("поступления мсп", "всего поступило", "поступления от мсп"),
    ),
    MetricSeed(5, "receipts_federal_taxes_total", "Федеральные налоги и сборы"),
    MetricSeed(
        6,
        "receipts_profit_tax_total",
        "Налог на прибыль организаций",
        ("налог на прибыль", "прибыль"),
        short_name="Налог на прибыль",
    ),
    MetricSeed(7, "receipts_profit_tax_federal", "Налог на прибыль в федеральный бюджет"),
    MetricSeed(
        8,
        "receipts_ndfl_total",
        "Налог на доходы физических лиц",
        ("ндфл", "налог на доходы физических лиц"),
        short_name="НДФЛ",
    ),
    MetricSeed(9, "receipts_ndfl_federal", "НДФЛ в федеральный бюджет"),
    MetricSeed(
        10,
        "receipts_vat_total",
        "Налог на добавленную стоимость",
        ("ндс", "налог на добавленную стоимость"),
        short_name="НДС",
    ),
    MetricSeed(11, "receipts_vat_domestic", "НДС по внутренней реализации"),
    MetricSeed(12, "receipts_excise_total", "Акцизы, всего", ("акцизы",)),
    MetricSeed(13, "receipts_excise_alcohol", "Акцизы на алкогольную продукцию"),
    MetricSeed(14, "receipts_excise_tobacco", "Акцизы на табачную продукцию"),
    MetricSeed(15, "receipts_excise_petroleum", "Акцизы на нефтепродукты"),
    MetricSeed(16, "receipts_excise_cars", "Акцизы на автомобили"),
    MetricSeed(17, "receipts_excise_sugar_drinks", "Акцизы на сахаросодержащие напитки"),
    MetricSeed(18, "receipts_excise_other", "Прочие акцизы"),
    MetricSeed(19, "receipts_natural_resources_total", "Платежи за природные ресурсы"),
    MetricSeed(
        20,
        "receipts_ndpi_total",
        "Налог на добычу полезных ископаемых",
        ("ндпи",),
        short_name="НДПИ",
    ),
    MetricSeed(21, "receipts_ndpi_oil_gas", "НДПИ по нефти и газу"),
    MetricSeed(22, "receipts_ndpi_other", "НДПИ по прочим ископаемым"),
    MetricSeed(23, "receipts_ndd", "Налог на дополнительный доход"),
    MetricSeed(24, "receipts_other_federal", "Остальные федеральные налоги"),
    MetricSeed(25, "receipts_regional_total", "Региональные налоги, всего"),
    MetricSeed(26, "receipts_property_tax", "Налог на имущество организаций"),
    MetricSeed(27, "receipts_transport_tax", "Транспортный налог"),
    MetricSeed(28, "receipts_local_total", "Местные налоги, всего"),
    MetricSeed(29, "receipts_land_tax", "Земельный налог"),
    MetricSeed(
        30,
        "receipts_special_regimes_total",
        "Специальные налоговые режимы",
        ("спецрежимы", "специальные налоговые режимы"),
    ),
    MetricSeed(
        31,
        "receipts_usn",
        "Упрощённая система налогообложения",
        ("усн",),
        short_name="УСН",
    ),
    MetricSeed(
        32,
        "receipts_autousn",
        "Автоматизированная УСН",
        ("автоусн",),
        short_name="АвтоУСН",
    ),
    MetricSeed(
        33,
        "receipts_eshn",
        "Единый сельскохозяйственный налог",
        ("есхн",),
        short_name="ЕСХН",
    ),
    MetricSeed(
        34,
        "receipts_psn",
        "Патентная система налогообложения",
        ("псн",),
        short_name="ПСН",
    ),
    MetricSeed(
        35,
        "receipts_npd",
        "Налог на профессиональный доход",
        ("нпд",),
        short_name="НПД",
    ),
    MetricSeed(
        36,
        "receipts_insurance_total",
        "Страховые взносы, всего",
        (
            "страховые взносы",
            "страховым взносам",
            "страховых взносов",
            "страховыми взносами",
            "взносы",
        ),
        short_name="Страховые взносы",
    ),
    MetricSeed(37, "receipts_insurance_pension", "Взносы на пенсионное страхование"),
    MetricSeed(38, "receipts_insurance_medical", "Взносы на медицинское страхование"),
    MetricSeed(39, "receipts_insurance_social", "Взносы на социальное страхование"),
    MetricSeed(40, "receipts_insurance_accidents", "Взносы от несчастных случаев"),
    MetricSeed(41, "receipts_penalties", "Поступило пеней"),
    MetricSeed(42, "accrued_penalties", "Начислено пеней"),
    MetricSeed(
        43,
        "receipts_security_payment_eaeu",
        "Поступил обеспечительный платёж ЕАЭС",
    ),
    MetricSeed(
        44,
        "accrued_security_payment_eaeu",
        "Начислен обеспечительный платёж ЕАЭС",
    ),
    MetricSeed(
        45,
        "receipts_excise_beer",
        "Акцизы на пиво и напитки на его основе",
        ("акцизы на пиво",),
    ),
    MetricSeed(
        46,
        "receipts_insurance_pension_fixed",
        "Пенсионные взносы в фиксированном размере",
    ),
    MetricSeed(
        47,
        "receipts_insurance_medical_fixed",
        "Медицинские взносы в фиксированном размере",
    ),
    MetricSeed(
        48,
        "receipts_insurance_distributed",
        "Страховые взносы, распределяемые по видам страхования",
    ),
    MetricSeed(
        49,
        "receipts_insurance_fixed_combined",
        "Страховые взносы отдельных категорий в совокупном фиксированном размере",
    ),
    MetricSeed(
        50,
        "receipts_unified_tax_payment_individual",
        "Единый налоговый платёж физического лица",
        ("енп физического лица",),
    ),
)

METRIC_BY_COLUMN = {item.column: item for item in METRICS}
METRIC_BY_CODE = {item.code: item for item in METRICS}


def _semantic_header_text(value: str) -> str:
    normalized = normalize_text(value)
    return re.sub(r"(?<=[а-я])-(?=[а-я])", "", normalized)


def _metric_code_from_header(
    raw_header: str,
    previous_code: str | None,
) -> str | None:
    text = _semantic_header_text(raw_header)
    if "количество субъектов мсп" in text:
        return "entity_count"
    if "начислено к уплате" in text and "консолидированный бюджет" in text:
        return "accrued_budget_total"
    if "начислено по страховым взносам" in text:
        return "accrued_insurance_total"
    if "поступило платежей" in text and "консолидированный бюджет" in text:
        return "receipts_budget_total"
    if "федеральные налоги и сборы" in text and "всего" in text:
        return "receipts_federal_taxes_total"
    if "налог на прибыль организаций" in text:
        return "receipts_profit_tax_total"
    if (
        "федеральный бюджет" in text
        and "налог" not in text
        and previous_code
        in {"receipts_profit_tax_total", "receipts_ndfl_total"}
    ):
        return {
            "receipts_profit_tax_total": "receipts_profit_tax_federal",
            "receipts_ndfl_total": "receipts_ndfl_federal",
        }[previous_code]
    if "налог на доходы физических лиц" in text:
        return "receipts_ndfl_total"
    if "налог на добавленную стоимость" in text:
        return "receipts_vat_total"
    if "реализуемые на территории российской федерации" in text:
        return "receipts_vat_domestic"
    if "акцизы" in text and (
        "всего" in text or "подакцизным товарам" in text
    ):
        return "receipts_excise_total"
    if "табачную продукцию" in text:
        return "receipts_excise_tobacco"
    if "нефтяное сырье" in text or "нефтепродукт" in text:
        return "receipts_excise_petroleum"
    if "сахаросодержащие напитки" in text:
        return "receipts_excise_sugar_drinks"
    if "прочие со 100% нормативом" in text:
        return "receipts_excise_other"
    if "алкогольную продукцию" in text:
        return "receipts_excise_alcohol"
    if "на пиво" in text or "основе пива" in text:
        return "receipts_excise_beer"
    if re.search(r"(?:^|\s)ндд(?:\s|$)", text):
        return "receipts_ndd"
    if "ндпи на нефть" in text or (
        "нефть" in text and "газовый конденсат" in text
    ):
        return "receipts_ndpi_oil_gas"
    if "общераспространенных полезных ископаемых" in text:
        return "receipts_ndpi_other"
    if "налог на добычу полезных ископаемых" in text:
        return "receipts_ndpi_total"
    if "пользование природными ресурсами" in text:
        return "receipts_natural_resources_total"
    if "остальные федеральные налоги" in text:
        return "receipts_other_federal"
    if "региональные налоги" in text and "всего" in text:
        return "receipts_regional_total"
    if "налог на имущество" in text:
        return "receipts_property_tax"
    if "транспортный налог" in text:
        return "receipts_transport_tax"
    if "земельный налог" in text:
        return "receipts_land_tax"
    if "местные налоги" in text:
        return "receipts_local_total"
    if "специальными налоговыми режимами" in text:
        return "receipts_special_regimes_total"
    if "автоматизированная упрощенная система" in text:
        return "receipts_autousn"
    if "упрощенной системы налогообложения" in text:
        return "receipts_usn"
    if "единый сельскохозяйственный налог" in text:
        return "receipts_eshn"
    if "патентной системы налогообложения" in text:
        return "receipts_psn"
    if "налог на профессиональный доход" in text:
        return "receipts_npd"
    if "поступило по страховым взносам" in text:
        return "receipts_insurance_total"
    if "временной нетрудоспособности" in text:
        return "receipts_insurance_social"
    if "распределяемые по видам страхования" in text:
        return "receipts_insurance_distributed"
    if "отдельными категориями плательщиков" in text:
        return "receipts_insurance_fixed_combined"
    if (
        "обязательное пенсионное страхование" in text
        or "бюджет фонда пенсионного и социального страхования" in text
    ):
        return "receipts_insurance_pension"
    if "обязательное медицинское страхование" in text:
        return "receipts_insurance_medical"
    if "фиксированном размере" in text:
        if previous_code == "receipts_insurance_pension":
            return "receipts_insurance_pension_fixed"
        if previous_code == "receipts_insurance_medical":
            return "receipts_insurance_medical_fixed"
    if "поступило суммы пеней" in text:
        return "receipts_penalties"
    if "начислено пеней" in text:
        return "accrued_penalties"
    if "поступило обеспечительный платеж" in text:
        return "receipts_security_payment_eaeu"
    if "начислено обеспечительный платеж" in text:
        return "accrued_security_payment_eaeu"
    if "единый налоговый платеж физического лица" in text:
        return "receipts_unified_tax_payment_individual"
    return None


def resolve_header_metrics(
    raw_headers: dict[int, str],
) -> HeaderMetricResolution:
    source_to_code: dict[int, str] = {}
    unmapped: dict[int, str] = {}
    code_columns: dict[str, list[int]] = {}
    previous_code: str | None = None
    for source_column, raw_header in sorted(raw_headers.items()):
        code = _metric_code_from_header(raw_header, previous_code)
        if code is None:
            unmapped[source_column] = raw_header
            previous_code = None
            continue
        source_to_code[source_column] = code
        code_columns.setdefault(code, []).append(source_column)
        previous_code = code
    duplicate_codes = {
        code: tuple(columns)
        for code, columns in code_columns.items()
        if len(columns) > 1
    }
    return HeaderMetricResolution(
        source_to_code=source_to_code,
        unmapped=unmapped,
        duplicate_codes=duplicate_codes,
    )

TOKEN_RE = re.compile(r"[0-9a-zа-я]+")
RUSSIAN_ENDINGS = tuple(
    sorted(
        {
            "иями",
            "ями",
            "ами",
            "его",
            "ого",
            "ему",
            "ому",
            "ыми",
            "ими",
            "ая",
            "яя",
            "ое",
            "ее",
            "ые",
            "ие",
            "ую",
            "юю",
            "ой",
            "ей",
            "ий",
            "ый",
            "ым",
            "им",
            "ах",
            "ях",
            "ам",
            "ям",
            "ом",
            "ем",
            "ов",
            "ев",
            "а",
            "я",
            "ы",
            "и",
            "у",
            "ю",
            "е",
        },
        key=len,
        reverse=True,
    )
)
ALIAS_STOP_WORDS = {"в", "и", "на", "от", "по", "с", "всего"}


def _russian_stem(token: str) -> str:
    if len(token) <= 4 or not re.fullmatch(r"[а-я]+", token):
        return token
    stem = token
    for ending in RUSSIAN_ENDINGS:
        if stem.endswith(ending) and len(stem) - len(ending) >= 4:
            stem = stem[: -len(ending)]
            break
    return stem.rstrip("ь")


def _metric_alias_matches(normalized_text: str, alias: str) -> bool:
    normalized_alias = normalize_text(alias)
    if not normalized_alias:
        return False
    if normalized_alias in normalized_text:
        return True
    alias_stems = [
        _russian_stem(token)
        for token in TOKEN_RE.findall(normalized_alias)
        if token not in ALIAS_STOP_WORDS
    ]
    if not alias_stems:
        return False
    question_stems = {
        _russian_stem(token)
        for token in TOKEN_RE.findall(normalized_text)
        if token not in ALIAS_STOP_WORDS
    }
    return all(stem in question_stems for stem in alias_stems)


def _quoted_exact_metric_codes(text: str) -> list[str]:
    references = {
        normalize_text(value)
        for value in re.findall(r"[«\"]([^»\"]+)[»\"]", text)
        if normalize_text(value)
    }
    if not references:
        return []
    return [
        seed.code
        for seed in METRICS
        if any(
            reference
            in {
                normalize_text(seed.name),
                normalize_text(seed.code),
                *(normalize_text(alias) for alias in seed.aliases),
            }
            for reference in references
        )
    ]


def seed_catalog(session: Session) -> None:
    existing = {
        item.semantic_code: item
        for item in session.scalars(select(MetricDefinition)).all()
    }
    for seed in METRICS:
        metric = existing.get(seed.code)
        if metric is None:
            session.add(
                MetricDefinition(
                    semantic_code=seed.code,
                    report_column_number=seed.column,
                    name_ru=seed.name,
                    description_ru=seed.name,
                    unit="тыс. руб." if seed.column != 1 else "ед.",
                    aliases_json=list(seed.aliases),
                )
            )
        else:
            metric.report_column_number = seed.column
            metric.name_ru = seed.name
            metric.aliases_json = list(seed.aliases)

    rf = session.scalar(
        select(Territory).where(Territory.normalized_name == "российская федерация")
    )
    if rf is None:
        session.add(
            Territory(
                name_ru="Российская Федерация",
                normalized_name="российская федерация",
                territory_type="country",
                official_code=None,
            )
        )
    session.commit()


def resolve_metric_code(text: str) -> str | None:
    normalized = normalize_text(text)
    quoted_exact = _quoted_exact_metric_codes(text)
    if quoted_exact:
        return quoted_exact[-1]
    exact = [
        (len(seed.name), seed.code)
        for seed in METRICS
        if normalize_text(seed.name) in normalized
        or normalize_text(seed.code) in normalized
    ]
    if exact:
        # A canonical catalog name or semantic code is authoritative.  In
        # particular, ``Начислено страховых взносов`` must not be shadowed by
        # the shorter receipts alias ``страховые взносы``.
        return max(exact)[1]
    if "страхов" in normalized and "взнос" in normalized:
        if "начислен" in normalized:
            return "accrued_insurance_total"
        return "receipts_insurance_total"
    matches: list[tuple[int, str]] = []
    for seed in METRICS:
        for alias in (seed.name, seed.code, *seed.aliases):
            if _metric_alias_matches(normalized, alias):
                matches.append((len(alias), seed.code))
    # A generic phrase such as "поступления МСП" must not shadow an
    # explicitly named tax later in the same question ("по НДС", "по УСН").
    specific = [
        match
        for match in matches
        if match[1] != "receipts_budget_total"
    ]
    return max(specific or matches, default=(0, ""))[1] or None


def resolve_metric_codes(text: str) -> list[str]:
    """Resolve every explicitly named metric, preserving catalog order."""
    normalized = normalize_text(text)
    quoted_exact = _quoted_exact_metric_codes(text)
    if quoted_exact:
        return quoted_exact
    exact_codes = [
        seed.code
        for seed in METRICS
        if normalize_text(seed.name) in normalized
        or normalize_text(seed.code) in normalized
    ]
    if exact_codes:
        return exact_codes
    codes: list[str] = []
    for seed in METRICS:
        aliases = (seed.name, seed.code, *seed.aliases)
        if any(_metric_alias_matches(normalized, alias) for alias in aliases):
            codes.append(seed.code)
    specific = [
        code for code in codes if code != "receipts_budget_total"
    ]
    if specific:
        return specific
    return codes


def resolved_metric_details(
    session: Session, metric_codes: list[str]
) -> list[dict]:
    definitions = {
        item.semantic_code: item
        for item in session.scalars(
            select(MetricDefinition).where(
                MetricDefinition.semantic_code.in_(metric_codes)
            )
        ).all()
    }
    details: list[dict] = []
    for code in metric_codes:
        definition = definitions.get(code)
        seed = METRIC_BY_CODE.get(code)
        if definition is None:
            continue
        short_name = seed.short_name if seed else None
        details.append(
            {
                "semantic_code": code,
                "name_ru": definition.name_ru,
                "short_name_ru": short_name,
                "display_name": short_name or definition.name_ru,
                "report_column_number": definition.report_column_number,
                "unit": definition.unit,
            }
        )
    return details
