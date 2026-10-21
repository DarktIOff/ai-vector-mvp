from __future__ import annotations

import hashlib
import json
import re
import time
from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .catalog import (
    METRIC_BY_CODE,
    METRIC_BY_COLUMN,
    resolve_metric_code,
    resolve_metric_codes,
)
from .clarifications import resolve_clarification_answer
from .config import Settings
from .llm import (
    LLMProvider,
    LLMProviderError,
    configured_provider,
)
from .models import (
    AiCallLog,
    MetricDefinition,
    MspFact,
    RowDefinition,
    SourceFile,
    Territory,
)
from .regional import territory_key
from .query_service import (
    COLUMN_RE,
    _explicit_cumulative_month,
    _is_multiyear_trend,
    build_query_plan,
    extract_years,
)
from .schemas import QueryPlan, SemanticQuery
from .workbooks import normalize_text


EXPLICIT_OKVED_RE = re.compile(
    r"(?:оквэд|код\s+оквэд)\s*[:№#-]?\s*(\d{2}(?:\.\d+)*)",
    re.IGNORECASE,
)
OKVED_NUMBER_RE = re.compile(r"\d{2}(?:\.\d+)*")
OKVED_RANGE_RE = re.compile(
    r"(\d{2}(?:\.\d+)?)\s*-\s*(\d{2}(?:\.\d+)?)"
)
MONTH_NAMES = {
    1: "январь",
    2: "февраль",
    3: "март",
    4: "апрель",
    5: "май",
    6: "июнь",
    7: "июль",
    8: "август",
    9: "сентябрь",
    10: "октябрь",
    11: "ноябрь",
    12: "декабрь",
}


@dataclass(frozen=True)
class PlannerResult:
    plan: QueryPlan | None
    mode: str
    warnings: list[str]
    status: str = "ready"
    clarification_question: str | None = None
    clarification_options: list[str] | None = None
    tool_journal: list[dict] | None = None
    semantic_query: SemanticQuery | None = None
    model_calls: int = 0


class PlanningUnavailableError(RuntimeError):
    def __init__(self, message: str, *, model_calls: int = 0):
        super().__init__(message)
        self.model_calls = model_calls


def detect_catalog_target(question: str) -> str | None:
    normalized = normalize_text(question)
    asks_for_catalog_list = re.search(
        r"\bкак(?:ие|ой|им|их)\s+"
        r"(?:период\w*|регион\w*|субъект\w*|территори\w*|"
        r"налог\w*|показател\w*|граф\w*|код\w*\s+оквэд|"
        r"вид\w*\s+деятельност\w*)\b",
        normalized,
    )
    availability = (
        "доступ" in normalized
        or "какие есть" in normalized
        or "какой есть" in normalized
        or "есть данные" in normalized
        or "данные есть" in normalized
        or "имеются данные" in normalized
        or "представлен" in normalized
        or "поддержива" in normalized
        or "для какого период" in normalized
        or "за какой период" in normalized
        or "за какие период" in normalized
        or "можно сравнить" in normalized
        or "перечисл" in normalized
        or "покажи список" in normalized
        or (
            asks_for_catalog_list is not None
            and not any(
                token in normalized
                for token in (
                    "больше",
                    "меньше",
                    "максим",
                    "миним",
                    "динамик",
                    "тренд",
                    "трэнд",
                    "измен",
                    "рост",
                    "сниз",
                    "хуже",
                    "худш",
                    "паден",
                    "аномал",
                    "выброс",
                    "отклон",
                    "сравн",
                )
            )
        )
    )
    if not availability:
        return None
    if "оквэд" in normalized or "виды деятельности" in normalized:
        return "okved"
    if (
        "регион" in normalized
        or "субъект" in normalized
        or "территори" in normalized
    ):
        return "territories"
    if (
        "налог" in normalized
        or "показател" in normalized
        or "граф" in normalized
        or "взнос" in normalized
    ) and not (
        "период" in normalized
        or "когда" in normalized
        or "можно сравнить" in normalized
    ):
        return "metrics"
    if (
        "период" in normalized
        or "когда" in normalized
        or "можно сравнить" in normalized
    ):
        return "periods"
    return "overview"


def _catalog_plan(question: str, target: str) -> QueryPlan:
    metric = resolve_metric_code(question)
    normalized = normalize_text(question)
    return QueryPlan(
        intent="catalog",
        catalog_target=target,
        metric_codes=[metric] if metric else [],
        periods=[],
        period_modes=[],
        period_labels=[],
        operations=[],
        row_codes=[1010],
        group_by=(
            ["territory"]
            if "регион" in normalized
            or "субъект" in normalized
            or "территори" in normalized
            else []
        ),
        limit=50,
    )


def _canonical_period_label(period: date, mode: str) -> str:
    if mode == "standalone_quarter":
        quarter = period.month // 3
        return f"{['I', 'II', 'III', 'IV'][quarter - 1]} квартал {period.year}"
    if period.month == 1:
        span = MONTH_NAMES[1]
    else:
        span = f"январь–{MONTH_NAMES[period.month]}"
    return f"{span} {period.year}"


def _period_requirements(plan: QueryPlan) -> list[date]:
    modes = plan.period_modes or ["cumulative_ytd"] * len(plan.periods)
    required: list[date] = []
    for period, mode in zip(plan.periods, modes, strict=True):
        required.append(period)
        if mode == "standalone_quarter" and period.month in {6, 9, 12}:
            previous_month = period.month - 3
            required.append(
                date(
                    period.year,
                    previous_month,
                    monthrange(period.year, previous_month)[1],
                )
            )
    return required


def _resolve_okved_row(
    session: Session, okved_code: str
) -> RowDefinition | None:
    normalized_text = normalize_text(okved_code)
    candidates = session.scalars(
        select(RowDefinition).where(RowDefinition.okved_text.is_not(None))
    ).all()
    full_exact = [
        row
        for row in candidates
        if normalized_text == normalize_text(row.okved_text or "")
    ]
    if full_exact:
        return min(full_exact, key=lambda row: len(row.okved_text or ""))
    number_match = OKVED_NUMBER_RE.search(normalized_text)
    normalized = number_match.group(0) if number_match else normalized_text
    exact = [
        row
        for row in candidates
        if normalized in OKVED_NUMBER_RE.findall(row.okved_text or "")
    ]
    if exact:
        return min(exact, key=lambda row: len(row.okved_text or ""))
    try:
        numeric = Decimal(normalized)
    except InvalidOperation:
        return None
    ranged: list[tuple[Decimal, RowDefinition]] = []
    for row in candidates:
        for start_raw, end_raw in OKVED_RANGE_RE.findall(
            row.okved_text or ""
        ):
            start, end = Decimal(start_raw), Decimal(end_raw)
            if start <= numeric <= end:
                ranged.append((end - start, row))
    if ranged:
        return min(
            ranged,
            key=lambda item: (
                item[0],
                len(item[1].okved_text or ""),
            ),
        )[1]
    return None


def _enrich_explicit_okved(
    session: Session, question: str, plan: QueryPlan
) -> QueryPlan:
    match = EXPLICIT_OKVED_RE.search(question)
    if not match:
        return plan
    code = match.group(1)
    row = _resolve_okved_row(session, code)
    if row is None:
        raise ValueError(
            f"Код ОКВЭД {code} отсутствует в утверждённом справочнике строк"
        )
    plan.row_codes = [row.base_row_code]
    plan.okved_codes = [code]
    return plan


def _apply_deterministic_constraints(
    question: str,
    available_periods: list[date],
    plan: QueryPlan,
) -> QueryPlan:
    """Keep LLM choices only where the question is genuinely ambiguous.

    Explicit analytical shapes such as an industry ranking or a multi-year
    trend are authoritative.  An LLM candidate like ``intent=value`` together
    with ``group_by=industry`` is internally contradictory and would otherwise
    silently collapse an analytical question to one country total.
    """
    catalog_target = detect_catalog_target(question)
    if catalog_target is not None:
        plan.intent = "catalog"
        plan.catalog_target = catalog_target
        metric = resolve_metric_code(question)
        plan.metric_codes = [metric] if metric else []
        plan.periods = []
        plan.period_modes = []
        plan.period_labels = []
        plan.operations = []
        plan.include_top_territory = False
        plan.include_bottom_territory = False
        plan.limit = plan.limit or 50
        return plan
    try:
        parsed = build_query_plan(question, available_periods)
    except ValueError:
        return plan

    # Known aliases and explicit report column numbers are authoritative.
    normalized = normalize_text(question)
    explicit_metric = (
        COLUMN_RE.search(question) is not None
        or resolve_metric_code(question) is not None
        or re.search(
            r"\bпоступлен\w*\s+(?:от|по)\s+мсп\b", normalized
        )
        is not None
    )
    if explicit_metric:
        plan.metric_codes = parsed.metric_codes
    if (
        "микро" in normalized
        or "малых и средних" in normalized
        or "без микро" in normalized
        or "все мсп" in normalized
    ):
        plan.enterprise_groups = parsed.enterprise_groups

    latest_year = (
        max(period.year for period in available_periods)
        if available_periods
        else date.today().year
    )
    years = extract_years(question, latest_year)
    if years:
        plan.periods = parsed.periods
        plan.period_modes = parsed.period_modes
        plan.period_labels = parsed.period_labels
        if _is_multiyear_trend(question, years):
            plan.intent = "trend"

    # These intents are selected by explicit language, not by an inferred
    # numeric answer.  Copy the complete analytical shape so the execution
    # route cannot disagree with its grouping dimension.
    authoritative_analytical_intents = {
        "trend",
        "overview",
        "rank",
        "rank_growth",
        "rank_difference",
        "outliers",
        "regional_average_growth",
        "regional_industry_rank_growth",
        "analytical_brief",
    }
    if parsed.intent in authoritative_analytical_intents:
        plan.intent = parsed.intent
        plan.analysis_dimension = parsed.analysis_dimension
        plan.group_by = list(parsed.group_by)
        plan.ranking_criteria = list(parsed.ranking_criteria)
        plan.outlier_modes = list(parsed.outlier_modes)
        plan.include_top_territory = parsed.include_top_territory
        plan.include_bottom_territory = parsed.include_bottom_territory
        plan.limit = parsed.limit
        plan.metric_codes = list(parsed.metric_codes)
        plan.periods = list(parsed.periods)
        plan.period_modes = list(parsed.period_modes)
        plan.period_labels = list(parsed.period_labels)
    return plan


def _referenced_analysis_rows(
    conversation_context: list[dict],
) -> list[int]:
    entities = next(
        (
            summary.get("analysis_entities", [])
            for item in reversed(conversation_context)
            if isinstance(item, dict)
            and isinstance(
                summary := item.get("result_summary"),
                dict,
            )
            and summary.get("analysis_entities")
        ),
        [],
    )
    return list(
        dict.fromkeys(
            int(item["row_code"])
            for item in entities
            if isinstance(item, dict)
            and str(item.get("row_code", "")).isdigit()
        )
    )


def _referenced_analysis_metrics(
    conversation_context: list[dict],
) -> list[str]:
    for item in reversed(conversation_context):
        if not isinstance(item, dict):
            continue
        summary = item.get("result_summary")
        raw_plan = item.get("plan")
        if (
            isinstance(summary, dict)
            and summary.get("analysis_entities")
            and isinstance(raw_plan, dict)
        ):
            return [
                str(code)
                for code in raw_plan.get("metric_codes", [])
                if str(code).strip()
            ]
    return []


def _apply_conversation_constraints(
    question: str,
    plan: QueryPlan,
    conversation_context: list[dict] | None,
) -> QueryPlan:
    if not conversation_context:
        return plan
    previous_item = next(
        (
            item
            for item in reversed(conversation_context)
            if isinstance(item.get("plan"), dict)
        ),
        None,
    )
    previous_raw = (
        previous_item.get("plan")
        if isinstance(previous_item, dict)
        else None
    )
    if not isinstance(previous_raw, dict):
        return plan
    previous = QueryPlan.model_validate(previous_raw)
    normalized = normalize_text(question)
    replaces_metric = re.search(
        r"\b(?:замен\w*|смен\w*)\b.*?\bна\b",
        normalized,
    )
    explicitly_returns_to_country = re.search(
        r"\b(?:рф|росси\w*|российск\w*\s+федерац\w*)\b",
        normalized,
    )
    if (
        replaces_metric
        and not explicitly_returns_to_country
        and all(
            normalize_text(item) == "российская федерация"
            for item in plan.territories
        )
    ):
        # "Там же замени налог" changes only the metric. A target
        # named "Региональные налоги" must not be mistaken for a new,
        # unnamed geography and reset the inherited territory to Russia.
        plan.territories = list(previous.territories)
    if not resolve_metric_code(question):
        plan.metric_codes = list(previous.metric_codes)
    if not extract_years(question):
        if previous.periods:
            plan.periods = [previous.periods[-1]]
            previous_mode = (
                previous.period_modes[-1]
                if previous.period_modes
                else "cumulative_ytd"
            )
            plan.period_modes = [previous_mode]
            plan.period_labels = []
    if not any(
        token in normalized
        for token in ("микро", "без микро", "малых и средних")
    ):
        plan.enterprise_groups = list(previous.enterprise_groups)
    regional_average_growth = (
        re.search(r"\bсредн\w*\b", normalized) is not None
        and any(token in normalized for token in ("темп", "рост", "динамик"))
        and any(token in normalized for token in ("регион", "субъект"))
        and any(
            token in normalized
            for token in ("отрасл", "оквэд", "вид деятельности")
        )
    )
    regional_industry_leaders = (
        any(token in normalized for token in ("регион", "субъект"))
        and any(
            token in normalized
            for token in ("отрасл", "оквэд", "вид деятельности")
        )
        and any(token in normalized for token in ("рост", "динамик", "темп"))
        and (
            "лидер" in normalized
            or re.search(r"\bлидир\w*", normalized) is not None
            or "наибольш" in normalized
            or "максимал" in normalized
            or re.search(r"\b(?:наи)?лучш\w*\b", normalized) is not None
        )
    )
    if regional_industry_leaders:
        plan.intent = "regional_industry_rank_growth"
        plan.territories = ["Российская Федерация"]
        plan.include_top_territory = False
        plan.include_bottom_territory = False
        plan.analysis_dimension = "territory"
        plan.group_by = ["industry", "territory"]
        plan.ranking_criteria = ["growth_rate"]
        plan.limit = plan.limit or 5
        referenced_rows = _referenced_analysis_rows(conversation_context)
        referenced_metrics = _referenced_analysis_metrics(
            conversation_context
        )
        if referenced_rows:
            plan.row_codes = referenced_rows
            plan.okved_codes = []
        if referenced_metrics:
            plan.metric_codes = referenced_metrics
    elif regional_average_growth:
        plan.intent = "regional_average_growth"
        plan.territories = ["Российская Федерация"]
        plan.include_top_territory = False
        plan.include_bottom_territory = False
        plan.analysis_dimension = "industry"
        plan.group_by = ["industry", "territory"]
        plan.ranking_criteria = []
        plan.limit = None
        referenced_rows = _referenced_analysis_rows(conversation_context)
        referenced_metrics = _referenced_analysis_metrics(
            conversation_context
        )
        if referenced_rows and re.search(
            r"\b(?:этим|эти|этих|выбранн\w*|указанн\w*)\s+"
            r"(?:отрасл\w*|видам\w*\s+деятельност\w*)\b",
            normalized,
        ):
            plan.row_codes = referenced_rows
            plan.okved_codes = []
            if referenced_metrics:
                plan.metric_codes = referenced_metrics
    elif "общая ситуация" in normalized or "в целом по рф" in normalized:
        plan.intent = "overview"
        plan.territories = ["Российская Федерация"]
        plan.analysis_dimension = None
        plan.group_by = []
        plan.row_codes = [1010]
        plan.okved_codes = []
    elif any(
        token in normalized
        for token in ("выбива", "выброс", "аномал", "нетипич")
    ):
        plan.intent = "outliers"
        plan.territories = ["Российская Федерация"]
        plan.include_top_territory = False
        plan.include_bottom_territory = False
        plan.analysis_dimension = "territory"
        plan.group_by = ["territory"]
        plan.outlier_modes = ["value", "growth_rate", "composite"]
    elif (
        re.search(r"\b(?:наи)?лучш\w*\b", normalized) is not None
        or "лидер" in normalized
        or re.search(r"\bлидир\w*", normalized) is not None
        or "наибольш" in normalized
        or "максимал" in normalized
        or re.search(r"\b(?:наи)?худш\w*\b", normalized) is not None
        or "аутсайдер" in normalized
        or "наименьш" in normalized
        or "минимал" in normalized
    ):
        growth_requested = any(
            token in normalized for token in ("динамик", "рост", "темп")
        )
        absolute_requested = "абсолют" in normalized
        plan.intent = (
            "rank_difference"
            if absolute_requested
            else "rank_growth"
            if growth_requested
            else "rank"
        )
        plan.analysis_dimension = (
            "territory"
            if "регион" in normalized or "субъект" in normalized
            else "industry"
            if (
                "отрасл" in normalized
                or "оквэд" in normalized
                or "вид деятельности" in normalized
            )
            else previous.analysis_dimension or "territory"
        )
        plan.territories = (
            list(previous.territories)
            if plan.analysis_dimension == "industry"
            else ["Российская Федерация"]
        )
        plan.include_top_territory = False
        plan.include_bottom_territory = False
        plan.group_by = [plan.analysis_dimension]
        if plan.analysis_dimension == "industry":
            plan.row_codes = [1010]
            plan.okved_codes = []
        plan.ranking_criteria = (
            ["value"]
            if absolute_requested
            else ["growth_rate"]
            if growth_requested
            else ["value", "growth_rate"]
        )
        asks_top = any(
            token in normalized
            for token in ("лучш", "лидер", "наибольш", "максимал")
        )
        asks_bottom = any(
            token in normalized
            for token in ("худш", "аутсайдер", "наименьш", "минимал")
        )
        plan.ranking_direction = (
            "both" if asks_top and asks_bottom else "bottom" if asks_bottom else "top"
        )
        plan.limit = plan.limit or 5
    elif not any(
        token in normalized
        for token in ("рф", "росси", "регион", "субъект", "област", "край")
    ):
        plan.territories = list(previous.territories)
    return plan


def _enrich_explicit_territories(
    session: Session, question: str, plan: QueryPlan
) -> QueryPlan:
    normalized_question = normalize_text(question)
    question_tokens = re.findall(r"[а-яa-z]+", normalized_question)
    ignored = {
        "республика",
        "область",
        "край",
        "автономная",
        "автономный",
        "округ",
        "город",
    }

    def stems(name: str) -> list[str]:
        result: list[str] = []
        for token in re.findall(r"[а-яa-z]+", normalize_text(name)):
            if token in ignored or token == "г" or len(token) < 4:
                continue
            # Preserve the adjective-forming "ск" in region names:
            # "Омская" -> "омск", "Томская" -> "томск". Removing the
            # whole suffix produced two- and three-letter stems that matched
            # unrelated words such as "аномальным" and "ростом".
            stem = re.sub(
                r"(ская|ский|ское|ской|ского)$",
                "ск",
                token,
            )
            stem = re.sub(r"(ая|ий|ый)$", "", stem)
            if stem.endswith(("а", "я")) and len(stem) > 5:
                stem = stem[:-1]
            # Three-letter adjective remnants are too collision-prone:
            # ``Марий`` becomes ``мар`` and was matched by the period word
            # ``марта``. Real distinctive catalog roots used here remain at
            # least four letters after inflection removal (``южн``, ``омск``).
            if len(stem) >= 4:
                result.append(stem)
        return result

    matched: dict[str, str] = {}
    territories = session.scalars(
        select(Territory).order_by(Territory.name_ru)
    ).all()

    def normalized_reference(value: str) -> str:
        value = normalize_text(value)
        value = re.sub(r"[^0-9a-zа-я]+", " ", value)
        value = re.sub(
            r"^(?:в|по)\s+|^территори\w*\s+", "", value
        )
        return re.sub(r"\s+", " ", value).strip()

    def aliases(item: Territory) -> set[str]:
        name = item.name_ru
        values = {
            name,
            name.replace("-", " "),
            name.replace(" ", "-"),
        }
        replacements = {
            "Российская Федерация": {"Россия", "РФ", "по России"},
            "г. Москва": {"Москва", "Москве", "Москвы"},
            "г. Санкт-Петербург": {
                "Санкт-Петербург",
                "Санкт Петербург",
                "СПб",
                "Санкт-Петербурге",
            },
            "г. Севастополь": {"Севастополь", "Севастополе"},
            "Ханты-Мансийский автономный округ - Югра": {
                "ХМАО — Югра",
                "ХМАО-Югра",
                "Югра",
            },
            "Ямало-Ненецкий автономный округ": {"ЯНАО"},
            "Ненецкий автономный округ": {"НАО"},
            "Еврейская автономная область": {"ЕАО"},
        }
        values.update(replacements.get(name, set()))
        normalized_name = normalize_text(name)
        if item.territory_type == "federal_district":
            values.add(
                re.sub(
                    r" федеральный округ$", " ФО", name, flags=re.I
                )
            )
        if normalized_name.startswith("республика "):
            stem = name.split(" ", 1)[1]
            values.update(
                {stem, f"в Республике {stem}", f"Республика {stem}"}
            )
        if normalized_name.endswith(" республика"):
            stem = name[: -len(" Республика")]
            values.update(
                {stem, f"в {stem.replace('ская', 'ской')} Республике"}
            )
        if normalized_name.endswith(" область"):
            stem = name[: -len(" область")]
            inflected = re.sub(r"ая$", "ой", stem)
            values.add(f"в {inflected} области")
        if normalized_name.endswith(" край"):
            stem = name[: -len(" край")]
            inflected = re.sub(r"ий$", "ом", re.sub(r"ый$", "ом", stem))
            values.add(f"в {inflected} крае")
        return {normalized_reference(value) for value in values}

    alias_map = {item.id: aliases(item) for item in territories}

    def resolve_reference(
        reference: str,
        *,
        allow_fuzzy: bool,
    ) -> Territory | None:
        candidate = normalized_reference(reference)
        if not candidate:
            return None
        exact = [
            item for item in territories if candidate in alias_map[item.id]
        ]
        if exact:
            semantic_keys = {territory_key(item.name_ru) for item in exact}
            if len(semantic_keys) == 1:
                canonical = next(
                    (
                        item
                        for item in exact
                        if normalize_text(item.name_ru)
                        == normalize_text(reference)
                    ),
                    None,
                )
                return canonical or exact[0]
            if len(exact) == 1:
                return exact[0]
        if not allow_fuzzy or len(candidate) < 4:
            return None
        # Several source books contain presentation variants of the same
        # territory (for example ``г. Москва``/``город Москва`` and both
        # spellings of Марий Эл).  Rank semantic territories, not physical
        # catalog rows, otherwise a duplicate becomes the runner-up with the
        # same score and prevents safe one-character typo recovery.
        semantic_candidates: dict[str, tuple[float, str, Territory]] = {}
        for item in territories:
            score = max(
                SequenceMatcher(None, candidate, alias).ratio()
                for alias in alias_map[item.id]
            )
            key = territory_key(item.name_ru)
            current = semantic_candidates.get(key)
            ranked_item = (score, item.name_ru, item)
            if current is None or ranked_item[:2] > current[:2]:
                semantic_candidates[key] = ranked_item
        ranked = sorted(semantic_candidates.values())
        best_score, _, best = ranked[-1]
        second_score = ranked[-2][0] if len(ranked) > 1 else 0.0
        # Generated typo variants remove one vowel, while unrelated regions
        # stay well below this threshold. The margin prevents a silent choice
        # between genuinely ambiguous names.
        if best_score >= 0.84 and best_score - second_score >= 0.03:
            return best
        return None

    quoted_matches: dict[str, str] = {}
    for quoted_match in re.finditer(r"[«\"]([^»\"]+)[»\"]", question):
        reference = quoted_match.group(1).strip()
        if not reference:
            continue
        prefix = normalize_text(question[: quoted_match.start()])[-48:]
        # Exact catalog aliases are safe anywhere. Fuzzy matching is limited
        # to a quote explicitly introduced as a territory. Without this
        # boundary, a metric or industry name in quotes could be silently
        # converted into the closest region.
        allow_fuzzy = bool(
            re.search(
                r"(?:территори\w*|регион\w*|субъект\w*)\s*$",
                prefix,
            )
        )
        item = resolve_reference(reference, allow_fuzzy=allow_fuzzy)
        if item is not None:
            quoted_matches[territory_key(item.name_ru)] = item.name_ru
    if quoted_matches:
        # An explicitly quoted territory is authoritative over an LLM guess;
        # retaining the guess would introduce a foreign region into evidence.
        plan.territories = list(quoted_matches.values())
        return plan

    for item in territories:
        if item.territory_type == "country":
            continue
        is_match = item.normalized_name in normalized_question
        if not is_match:
            item_stems = stems(item.name_ru)
            distinctive_stems = [
                stem for stem in item_stems if stem != "федеральн"
            ]
            is_match = bool(distinctive_stems) and all(
                any(token.startswith(stem) for token in question_tokens)
                for stem in item_stems
            )
        if is_match:
            key = territory_key(item.name_ru)
            current = matched.get(key)
            if current is None or ". " in item.name_ru:
                matched[key] = item.name_ru
    mentioned = list(matched.values())
    if not mentioned:
        return plan
    existing = [
        item
        for item in plan.territories
        if normalize_text(item) != "российская федерация"
    ]
    # The LLM can return an official-looking alias ("город Москва") while the
    # workbook catalog contains "г. Москва".  Merge by semantic territory key
    # and let the source-backed catalog spelling win.
    deduplicated: dict[str, str] = {}
    for item in [*existing, *mentioned]:
        deduplicated[territory_key(item)] = item
    plan.territories = list(deduplicated.values())
    return plan


def validate_plan(
    session: Session,
    plan: QueryPlan,
    available_periods: list[date],
) -> QueryPlan:
    if plan.intent in {"rank_growth", "rank_difference"}:
        plan.analysis_dimension = plan.analysis_dimension or (
            plan.group_by[0]
            if plan.group_by and plan.group_by[0] in {"industry", "territory"}
            else "industry"
        )
        plan.ranking_criteria = [
            "growth_rate"
            if plan.intent == "rank_growth"
            else "value"
        ]
    if plan.intent == "regional_industry_rank_growth":
        plan.analysis_dimension = "territory"
        plan.group_by = ["industry", "territory"]
        plan.ranking_criteria = ["growth_rate"]
    if plan.intent == "analytical_brief":
        plan.analysis_dimension = "industry"
        plan.group_by = ["industry", "territory"]
        plan.ranking_criteria = ["growth_rate"]
        plan.limit = plan.limit or 3
    if plan.intent == "rank":
        plan.analysis_dimension = plan.analysis_dimension or "industry"
        plan.ranking_criteria = plan.ranking_criteria or [
            "value",
            "growth_rate",
        ]
    if plan.intent == "outliers":
        plan.analysis_dimension = "territory"
        plan.outlier_modes = plan.outlier_modes or [
            "value",
            "growth_rate",
            "composite",
        ]
    if plan.intent == "catalog" and plan.catalog_target is None:
        raise ValueError("Для каталожного запроса не указан тип справочника")
    if plan.intent != "catalog" and len(plan.metric_codes) == 0:
        raise ValueError("План не содержит показатель")
    if plan.intent != "catalog" and len(plan.periods) == 0:
        raise ValueError("План не содержит период")
    if plan.period_modes and len(plan.period_modes) != len(plan.periods):
        raise ValueError("Режим периода должен быть указан для каждого периода")
    if not plan.period_modes:
        plan.period_modes = ["cumulative_ytd"] * len(plan.periods)
    # Labels are display data, not model authority. Rebuild them from the
    # validated period and mode so an LLM typo cannot alter the answer.
    plan.period_labels = [
        _canonical_period_label(period, mode)
        for period, mode in zip(
            plan.periods, plan.period_modes, strict=True
        )
    ]
    if plan.intent == "catalog":
        plan.operations = []
    elif plan.intent in {
        "compare",
        "trend",
        "overview",
        "rank",
        "outliers",
        "rank_growth",
        "rank_difference",
        "regional_average_growth",
        "regional_industry_rank_growth",
        "analytical_brief",
    }:
        plan.operations = [
            "value",
            "difference",
            "growth_rate",
            "sources",
        ]
    elif plan.intent == "share":
        plan.operations = ["value", "share", "sources"]
    else:
        plan.operations = ["value", "sources"]

    metric_series_count = (
        1
        if set(plan.metric_codes)
        == {"receipts_budget_total", "receipts_insurance_total"}
        else max(1, len(plan.metric_codes))
    )
    series_count = (
        metric_series_count
        * len(plan.territories)
        * len(plan.enterprise_groups)
        * len(plan.row_codes)
    )
    if (
        plan.intent
        not in {
            "catalog",
            "regional_average_growth",
            "regional_industry_rank_growth",
            "analytical_brief",
        }
        and series_count > 8
    ):
        raise ValueError(
            "Запрос образует более 8 независимых рядов; "
            "сузьте показатели, территории, группы МСП или строки"
        )
    if (
        plan.intent
        not in {
            "catalog",
            "regional_average_growth",
            "regional_industry_rank_growth",
            "analytical_brief",
        }
        and series_count * len(plan.periods) > 192
    ):
        raise ValueError(
            "Запрос содержит более 192 точек; "
            "сузьте диапазон лет или состав рядов"
        )

    unknown_metrics = [
        code for code in plan.metric_codes if code not in METRIC_BY_CODE
    ]
    if unknown_metrics:
        raise ValueError(
            "Неизвестные семантические показатели: "
            + ", ".join(unknown_metrics)
        )

    available = set(available_periods)
    missing = sorted(set(_period_requirements(plan)) - available)
    if missing:
        raise ValueError(
            "Для расчёта отсутствуют накопительные данные за: "
            + ", ".join(period.isoformat() for period in missing)
        )

    known_rows = set(
        session.scalars(
            select(RowDefinition.base_row_code).where(
                RowDefinition.base_row_code.in_(plan.row_codes)
            )
        ).all()
    )
    unknown_rows = sorted(set(plan.row_codes) - known_rows)
    if unknown_rows:
        raise ValueError(
            "Неизвестные строки формы: "
            + ", ".join(str(code) for code in unknown_rows)
        )
    metric_ids = {
        item.semantic_code: item.id
        for item in session.scalars(
            select(MetricDefinition).where(
                MetricDefinition.semantic_code.in_(plan.metric_codes)
            )
        ).all()
    }
    row_ids = {
        item.base_row_code: item.id
        for item in session.scalars(
            select(RowDefinition).where(
                RowDefinition.base_row_code.in_(plan.row_codes)
            )
        ).all()
    }
    needed_sections: set[str] = set()
    for group in plan.enterprise_groups:
        needed_sections.update(
            {"I", "II"}
            if group == "small_and_medium_without_micro"
            else {"II"}
            if group == "micro"
            else {"I"}
        )
    required_dates = set(_period_requirements(plan))
    fact_rows = session.execute(
        select(
            MspFact.period_end,
            MspFact.metric_id,
            MspFact.row_definition_id,
            MspFact.section,
        )
        .join(SourceFile)
        .where(
            MspFact.period_end.in_(required_dates),
            MspFact.metric_id.in_(metric_ids.values()),
            MspFact.row_definition_id.in_(row_ids.values()),
            MspFact.section.in_(needed_sections),
            SourceFile.status.in_(["valid", "valid_with_warnings"]),
        )
        .distinct()
    ).all()
    available_fact_keys = set(fact_rows)
    missing_facts: list[str] = []
    for period in sorted(required_dates):
        for metric_code, metric_id in metric_ids.items():
            for row_code, row_id in row_ids.items():
                for section in sorted(needed_sections):
                    if (
                        period,
                        metric_id,
                        row_id,
                        section,
                    ) not in available_fact_keys:
                        missing_facts.append(
                            f"{metric_code}, строка {row_code}, "
                            f"раздел {section}, {period.isoformat()}"
                        )
    if missing_facts:
        preview = "; ".join(missing_facts[:8])
        suffix = (
            f"; ещё {len(missing_facts) - 8}"
            if len(missing_facts) > 8
            else ""
        )
        raise ValueError(
            "В источниках отсутствуют выбранные факты: "
            + preview
            + suffix
        )
    catalog_territories = session.scalars(
        select(Territory).order_by(Territory.name_ru)
    ).all()
    canonical_by_normalized = {
        item.normalized_name: item.name_ru for item in catalog_territories
    }
    canonical_names: list[str] = []
    for requested in plan.territories:
        normalized = normalize_text(requested)
        canonical = canonical_by_normalized.get(normalized)
        if canonical is None:
            raise ValueError(
                f"Территория «{requested}» отсутствует в справочнике"
            )
        if canonical not in canonical_names:
            canonical_names.append(canonical)
    plan.territories = canonical_names
    if any(
        normalize_text(item) != "российская федерация"
        for item in plan.territories
    ):
        required_kinds: set[str] = set()
        for group in plan.enterprise_groups:
            if group in {"all_msp", "small_and_medium_without_micro"}:
                required_kinds.add("regional_section_i")
            if group in {"micro", "small_and_medium_without_micro"}:
                required_kinds.add("regional_section_ii")
        required_kinds = required_kinds or {"regional_section_i"}
        regional_sources = set(
            session.execute(
                select(
                    SourceFile.period_end,
                    SourceFile.workbook_kind,
                ).where(
                    SourceFile.period_end.in_(required_dates),
                    SourceFile.workbook_kind.in_(required_kinds),
                    SourceFile.status.in_(["valid", "valid_with_warnings"]),
                )
            ).all()
        )
        missing_regional = [
            f"{period.isoformat()} ({kind})"
            for period in sorted(required_dates)
            for kind in sorted(required_kinds)
            if (period, kind) not in regional_sources
        ]
        if missing_regional:
            raise ValueError(
                "Нет сопоставимой региональной книги: "
                + ", ".join(missing_regional[:8])
            )
    for okved_code in plan.okved_codes:
        row = _resolve_okved_row(session, okved_code)
        if row is None:
            raise ValueError(
                f"Код ОКВЭД {okved_code} отсутствует в справочнике строк"
            )
        if row.base_row_code not in plan.row_codes:
            raise ValueError(
                f"Код ОКВЭД {okved_code} не соответствует выбранной "
                f"строке {plan.row_codes}"
            )
    return plan


def _catalog_prompt(session: Session) -> str:
    metrics = session.scalars(
        select(MetricDefinition).order_by(
            MetricDefinition.report_column_number
        )
    ).all()
    rows = session.scalars(
        select(RowDefinition)
        .where(RowDefinition.okved_text.is_not(None))
        .order_by(RowDefinition.base_row_code)
    ).all()
    territories = session.scalars(
        select(Territory).order_by(Territory.name_ru)
    ).all()
    metric_lines = [
        f"{item.semantic_code} | графа {item.report_column_number} | "
        f"{item.name_ru}"
        for item in metrics
    ]
    row_lines = [
        f"{item.base_row_code} | {item.okved_text} | "
        f"{item.name_ru.replace(chr(10), ' ')[:120]}"
        for item in rows
    ]
    return (
        "ПОКАЗАТЕЛИ:\n"
        + "\n".join(metric_lines)
        + "\n\nСТРОКИ И ОКВЭД:\n"
        + "\n".join(row_lines)
        + "\n\nТЕРРИТОРИИ (используй точное название):\n"
        + "\n".join(item.name_ru for item in territories)
    )


def _planner_messages(
    session: Session,
    question: str,
    available_periods: list[date],
    conversation_context: list[dict] | None = None,
) -> list[dict[str, str]]:
    schema = SemanticQuery.model_json_schema()
    available = ", ".join(period.isoformat() for period in available_periods)
    focused_context: dict[str, object] | None = None
    navigation_options = _conversation_navigation_options(
        conversation_context
    )
    latest_context = _latest_context_item(conversation_context)
    if latest_context is not None:
        latest_item, latest_plan = latest_context
        focused_context = {
            "previous_question": latest_item.get("question"),
            "inherited_filters": {
                "metric_codes": list(latest_plan.metric_codes),
                "enterprise_groups": list(latest_plan.enterprise_groups),
                "territories": list(latest_plan.territories),
                "row_codes": list(latest_plan.row_codes),
                "periods": [
                    item.isoformat() for item in latest_plan.periods
                ],
                "period_modes": list(latest_plan.period_modes),
            },
            "previous_analyses": _context_expected_analyses(latest_item),
            "selected_industries": (
                (latest_item.get("result_summary") or {}).get(
                    "analysis_entities", []
                )
                if isinstance(latest_item.get("result_summary"), dict)
                else []
            ),
            "navigation_options": navigation_options,
        }
    system = f"""
Ты — ограниченный агент-планировщик запросов по форме 1-МСП.
На каждом шаге верни ровно один JSON-объект действия. У тебя нет доступа к
SQL и числовым фактам. Не придумывай semantic_code, территорию, row_code,
ОКВЭД или период: сначала найди или проверь их инструментом.

Допустимые действия:
{{"action":"search_metric","arguments":{{"query":"..."}}}}
{{"action":"search_territory","arguments":{{"query":"..."}}}}
{{"action":"search_row","arguments":{{"query":"..."}}}}
{{"action":"comparable_periods","arguments":{{"years":[2022,2023],
  "territories":["Российская Федерация"],
  "enterprise_groups":["all_msp"]}}}}
{{"action":"check_availability","arguments":{{"plan":{{...QueryPlan...}}}}}}
{{"action":"validate_plan","arguments":{{"plan":{{...QueryPlan...}}}}}}
{{"action":"final","semantic_query":{{...SemanticQuery...}}}}
{{"action":"clarify","question":"один конкретный вопрос",
  "options":["вариант 1","вариант 2"]}}

Заверши SemanticQuery только после проверки вложенного query_plan. Поле
analyses — это полный список требуемых аналитических операций; backend не
будет повторно угадывать их по словам пользователя. В constraint_sources
укажи для metric_codes, territories, row_codes, enterprise_groups и periods,
взято ли ограничение из question, conversation или default. Если значимая
сущность неоднозначна, задай один конкретный вопрос вместо предположения.

Обязательная модель измерений:
- показатель, группа МСП и отраслевая строка — три разных измерения;
- «все МСП» = enterprise_groups=[all_msp], это не отрасль;
- «микропредприятия» = enterprise_groups=[micro];
- «малые и средние без микро» =
  enterprise_groups=[small_and_medium_without_micro];
- если отрасль/ОКВЭД не названы, row_codes=[1010] — итог по экономике;
- для рейтинга, распределения или drill-down по всем отраслям
  row_codes=[1010] задаёт общий scope; не требуй список ОКВЭД;
- для рейтинга/выбросов по всем регионам
  territories=[Российская Федерация] задаёт общий scope;
- если территория не названа в национальном отраслевом запросе,
  используй territories=[Российская Федерация] как default;
- одно exact-совпадение инструмента авторитетно. Не уточняй уже
  точно названную сущность и не ищи её в другом измерении;
- «НДС» = поступления receipts_vat_total; отдельного accrued-НДС в каталоге нет;
- явное «поступления в бюджет» = receipts_budget_total,
  «начислено» = accrued_*;
- «совокупно/вместе со страховыми взносами» = metric_codes
  [receipts_budget_total, receipts_insurance_total].
Не задавай clarification для любого из этих однозначных случаев.

Различай рейтинг и статистические выбросы:
- «лидеры/аутсайдеры среди отраслей» — отраслевой рейтинг, а не outliers;
- «аутсайдеры» = ranking_direction=bottom;
- «лидеры и аутсайдеры» = ranking_direction=both;
- «по приросту/росту» = ranking_criterion=growth_rate;
- «распределение/медиана между отраслями» требует distribution и
  industry_rank, но не structure;
- региональные «выбросы» требуют outliers и regional_rank;
- последовательность «отрасли, затем регионы» требует industry_rank и
  industry_to_regions.

Правила analyses:
- catalog → [resolve];
- временная динамика → series;
- значение или сравнение → comparison;
- рейтинг отраслей/регионов → industry_rank/regional_rank;
- распределение или медиана → distribution;
- выбросы → outliers;
- структура или доли → structure;
- переход отрасль → регионы → industry_to_regions;
- динамика найденных отраслей → ranked_industry_series.
- ranking_direction и ranking_criterion передают точный смысл рейтинга.

Правила периодов:
- исходники накопительные с начала года;
- I квартал: period_end YYYY-03-31, mode standalone_quarter;
- II квартал: period_end YYYY-06-30, mode standalone_quarter; backend вычтет I квартал;
- III квартал: period_end YYYY-09-30, mode standalone_quarter;
- IV квартал: period_end YYYY-12-31, mode standalone_quarter;
- «январь–июнь», «первое полугодие», «6 месяцев» — cumulative_ytd;
- «январь–март» — cumulative_ytd; только явно названный «I квартал» имеет
  mode=standalone_quarter;
- порядок periods, period_modes и period_labels одинаков;
- periods содержат только названные пользователем наблюдения. Не добавляй
  предыдущий год для расчёта прироста: backend получает его автоматически
  через comparison_basis=previous_year_same_slice;
- многолетняя динамика (три и более года/диапазон) → intent=trend;
- «изменились/динамика за последние N лет» означает N годовых интервалов:
  от latest_year-N до latest_year включительно; например, последние 4 года
  при latest_year=2026 → 2022, 2023, 2024, 2025, 2026;
- «за прошедшие N лет» означает ровно N сопоставимых годовых точек, включая
  последнюю доступную; например, прошедшие 4 года при latest_year=2026 →
  2023, 2024, 2025, 2026;
- попарное сравнение → intent=compare;
- «общая ситуация по РФ» → intent=overview;
- «где/в какой отрасли/регионе лучше всего» → intent=rank,
  ranking_criteria=[value,growth_rate] и соответствующий analysis_dimension;
- «средний темп роста по всем регионам по этим отраслям» →
  intent=regional_average_growth, group_by=[industry,territory]; отрасли
  возьми из analysis_entities предыдущего подтверждённого хода;
- «какие регионы лидируют по росту по этим отраслям» →
  intent=regional_industry_rank_growth, group_by=[industry,territory];
  показатель и отрасли унаследуй из предыдущего подтверждённого хода;
- один запрос с динамикой по РФ, лучшими и худшими отраслями и регионами →
  intent=analytical_brief, group_by=[industry,territory],
  ranking_criteria=[growth_rate], limit=3;
- «какие регионы выбиваются» → intent=outliers,
  analysis_dimension=territory и outlier_modes=[value,growth_rate,composite];
- для trend и compare operations содержат value, difference, growth_rate,
  sources;
- trend без внутригoдового периода использует последний общий сопоставимый
  накопительный срез из результата comparable_periods.
- если названы регионы и нужен comparable_periods, передай эти регионы в
  инструмент; национальное окно нельзя применять к региональному плану.

Правила групп:
- все МСП: all_msp;
- микро: micro;
- малые и средние без микро: small_and_medium_without_micro.

Правила вопросов о доступности:
- общий вопрос «что/какие данные доступны» → intent=catalog,
  catalog_target=overview;
- «за какой/для какого периода доступны данные» или «для какого периода
  можно сравнить» → intent=catalog, catalog_target=periods, periods=[];
- «какие регионы/субъекты доступны» → intent=catalog,
  catalog_target=territories;
- «какие налоги/показатели/графы доступны» → intent=catalog,
  catalog_target=metrics;
- «какие коды ОКВЭД/виды деятельности доступны» → intent=catalog,
  catalog_target=okved;
- в catalog-плане сохрани явно названные фильтры показателя, территории,
  группы МСП и ОКВЭД, но не придумывай конкретный период.

Правила территорий и продолжения разговора:
- используй только точные названия из каталога территорий;
- если вопрос ссылается на «это», «его», «тот же», «сравни с»,
  унаследуй не повторённые metric_codes, periods, period_modes,
  row_codes, enterprise_groups и территории из последнего плана;
- явно названные в новом вопросе параметры заменяют унаследованные;
- «регион с наибольшим/максимальным значением» означает
  include_top_territory=true;
- «регион с наименьшим значением» означает
  include_bottom_territory=true;
- для сравнения территорий intent=compare и group_by содержит territory.

Если пользователь указал ОКВЭД или вид деятельности, выбери точный
base_row_code из каталога и запиши код в okved_codes. Иначе row_codes=[1010].

Доступные накопительные даты (только метаданные): {available}

SemanticQuery JSON Schema:
{json.dumps(schema, ensure_ascii=False)}

ПРЕДЫДУЩИЕ ПОДТВЕРЖДЁННЫЕ ХОДЫ (только вопросы и планы, без чисел):
{json.dumps(conversation_context or [], ensure_ascii=False, default=str)}

ФОКУС ТЕКУЩЕГО ПРОДОЛЖЕНИЯ:
{json.dumps(focused_context, ensure_ascii=False, default=str)}
Если текущий вопрос не заменяет измерение явно, скопируй его из
inherited_filters без поиска default. Фраза о смене только показателя
сохраняет previous_analyses и остальные фильтры. Ссылка на выбранные отрасли
использует selected_industries; запрос их региональных экстремумов требует
industry_to_regions, а не outliers.
navigation_options — проверенные варианты перехода по истории, а не
автоматические правила. Семантически реши, описывает ли текущая формулировка
один из них. Если описывает, примени verified_plan_patch целиком, затем
замени только явно названные в текущем вопросе измерения. Не ищи заново
row_codes, территории и периоды из verified_plan_patch и не переноси в него
selected_industries: эти ограничения уже подтверждены предыдущими ходами.
""".strip()
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": question},
    ]


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
    for match in re.finditer(r"\{", candidate):
        try:
            parsed, length = decoder.raw_decode(candidate[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            objects.append((length, match.start(), parsed))
    if not objects:
        raise ValueError("Ответ модели должен содержать JSON-объект")

    def priority(item: tuple[int, int, dict]) -> tuple[int, int, int]:
        length, position, parsed = item
        contract = int(
            "action" in parsed
            or "semantic_query" in parsed
            or (
                "status" in parsed
                and (
                    "query_plan" in parsed
                    or "clarification_question" in parsed
                    or "no_data_reason" in parsed
                )
            )
        )
        return contract, position, length

    return max(objects, key=priority)[2]


def _provider_is_available(provider: LLMProvider, settings: Settings) -> bool:
    if provider.name == "deterministic" or settings.llm_data_policy == "none":
        return False
    configured = getattr(provider, "_configured", True)
    return bool(configured)


def _validate_semantic_query(semantic: SemanticQuery) -> SemanticQuery:
    """Validate semantic coherence without interpreting the wording."""

    if semantic.status != "ready":
        return semantic
    plan = semantic.query_plan
    if plan is None:
        raise ValueError("SemanticQuery не содержит query_plan")
    analyses = list(dict.fromkeys(semantic.analyses))

    def require(name: str, *, before: str | None = None) -> None:
        if name in analyses:
            return
        if before is not None and before in analyses:
            analyses.insert(analyses.index(before), name)
        else:
            analyses.append(name)

    # These are typed orchestration dependencies of the model-selected plan,
    # not a second interpretation of the user's wording.
    if plan.intent == "catalog":
        analyses = ["resolve"]
    if plan.intent == "trend":
        require("series")
    if plan.intent in {"value", "compare", "overview", "source"}:
        require("comparison")
    if plan.intent == "outliers":
        require("outliers")
        require("regional_rank", before="outliers")
    if plan.intent == "share":
        require("structure")
    if plan.intent in {"rank", "rank_growth", "rank_difference"}:
        required = (
            "regional_rank"
            if plan.analysis_dimension == "territory"
            else "industry_rank"
        )
        require(required)
    if "distribution" in analyses and (
        plan.analysis_dimension == "industry" or "industry" in plan.group_by
    ):
        require("industry_rank", before="distribution")
    if (
        "industry_to_regions" in analyses
        and "industry_rank" not in analyses
        and plan.row_codes == [1010]
    ):
        require("industry_rank", before="industry_to_regions")
    required_sources = {
        "metric_codes",
        "territories",
        "row_codes",
        "enterprise_groups",
        "periods",
    }
    missing_sources = sorted(
        required_sources - semantic.constraint_sources.keys()
    )
    if missing_sources:
        raise ValueError(
            "Не указано происхождение ограничений: "
            + ", ".join(missing_sources)
        )
    plan.ranking_direction = semantic.ranking_direction
    semantic.analyses = analyses
    semantic.query_plan = plan
    return semantic


def _explicit_year_window(question: str) -> tuple[str, int] | None:
    normalized = normalize_text(question)
    patterns = (
        (
            "past",
            r"(?:прошедш\w*\s+(\d+)|(\d+)\s+прошедш\w*)\s+"
            r"(?:(?:полн\w*|сопоставим\w*|календарн\w*)\s+){0,2}"
            r"(?:год\w*|лет)",
        ),
        (
            "latest",
            r"(?:последн\w*\s+(\d+)|(\d+)\s+последн\w*)\s+"
            r"(?:(?:полн\w*|сопоставим\w*|календарн\w*)\s+){0,2}"
            r"(?:год\w*|лет)",
        ),
    )
    for kind, pattern in patterns:
        match = re.search(pattern, normalized)
        if match:
            return kind, int(match.group(1) or match.group(2))
    return None


def _question_metric_requirements(question: str) -> list[str]:
    """Return only metrics that the question names unambiguously.

    This is a validator contract, not a fallback parser: the model still
    builds the plan, while the catalog prevents it from silently changing an
    explicitly named metric.
    """

    normalized = normalize_text(question)
    replacement = re.search(
        r"\b(?:замен\w*|смен\w*)\b.*?\bна\b\s*(?P<target>.+)$",
        normalized,
    )
    if replacement is not None:
        # In an explicit replacement, only the target after ``на`` is a
        # current-question requirement.  The source metric may be named as
        # well ("замени НДС на НДФЛ"), but retaining it would turn a switch
        # into an unintended comparison with stale dialogue context.
        normalized = replacement.group("target").strip(" .,:;!?\t\n")
    column_match = re.search(
        r"(?:граф\w*|колонк\w*)\s*[№#:]?\s*(\d{1,2})\b",
        normalized,
    )
    if column_match:
        seed = METRIC_BY_COLUMN.get(int(column_match.group(1)))
        return [seed.code] if seed is not None else []

    mentions_insurance = "страхов" in normalized and "взнос" in normalized
    mentions_budget_receipts = bool(
        ("поступ" in normalized and "бюджет" in normalized)
        or "бюджетн поступлен" in normalized
        or "поступления от мсп" in normalized
    )
    explicitly_combined = mentions_insurance and mentions_budget_receipts and any(
        marker in normalized
        for marker in ("совокуп", "вмест", "включая", "с учетом")
    )
    if explicitly_combined:
        return ["receipts_budget_total", "receipts_insurance_total"]

    resolved = resolve_metric_codes(normalized)
    if resolved:
        return resolved
    if mentions_budget_receipts:
        return ["receipts_budget_total"]
    return []


_GENERIC_ROW_REFERENCE_TOKENS = {
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
_NON_ROW_REFERENCE_PREFIXES = (
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


def _row_reference_tokens(value: str) -> list[str]:
    label = re.split(r"[\n(]", normalize_text(value), maxsplit=1)[0]
    return [
        token
        for token in re.findall(r"[а-яa-z]+", label)
        if len(token) >= 5
        and token not in _GENERIC_ROW_REFERENCE_TOKENS
        and not token.startswith(_NON_ROW_REFERENCE_PREFIXES)
    ]


def _inflected_row_token_match(left: str, right: str) -> bool:
    common = 0
    for left_char, right_char in zip(left, right):
        if left_char != right_char:
            break
        common += 1
    shortest = min(len(left), len(right))
    required = shortest - 1 if shortest <= 6 else max(7, shortest - 2)
    return common >= required


def _resolve_inflected_row_reference(
    rows: list[RowDefinition], reference: str
) -> RowDefinition | None:
    """Return one strongly supported row despite Russian inflection/order.

    This is catalog matching for the planner's metadata tool, not a query
    parser.  The model still decides which dimension to search; this helper
    makes that search recognize the same canonical row when the user changes
    case or word order.
    """

    query_tokens = _row_reference_tokens(reference)
    if not query_tokens:
        return None
    ranked: list[tuple[float, int, int, int, RowDefinition]] = []
    for row in rows:
        best_tokens: list[str] = []
        best_matches = 0
        for name in [row.name_ru, *(row.aliases_json or [])]:
            candidate_tokens = _row_reference_tokens(str(name))
            matches = sum(
                any(
                    _inflected_row_token_match(candidate, query)
                    for query in query_tokens
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
    if coverage < 0.5 and matches < 2:
        return None
    return winner[-1]


def _question_enterprise_group_requirements(question: str) -> list[str]:
    """Return an explicitly named enterprise group, if present."""

    normalized = normalize_text(question)
    if "без микро" in normalized:
        return ["small_and_medium_without_micro"]
    if re.search(r"\bмикропредприят\w*\b", normalized):
        return ["micro"]
    if re.search(r"\bмсп\b", normalized):
        return ["all_msp"]
    return []


def _explicit_row_code(
    value: str,
    known_row_codes: set[int],
) -> int | None:
    """Resolve the parent report row from a formula or a bare code list."""

    normalized = normalize_text(value)
    patterns = (
        r"(?:\b(?:стр(?:ока)?|ст)\.?\s*)?(\d{4})\s*(?:>|=)",
        r"\b(?:стр(?:ока)?|ст)\.?\s*(\d{4})\b",
        r"\b(\d{4})\b",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, normalized):
            candidate = int(match.group(1))
            if candidate in known_row_codes:
                return candidate
    return None


def _question_row_requirements(
    session: Session,
    question: str,
    available_periods: list[date],
) -> list[int]:
    """Resolve only an explicitly named catalog row from the question."""

    normalized = normalize_text(question)
    known_row_codes = set(
        session.scalars(select(RowDefinition.base_row_code)).all()
    )
    row_code = _explicit_row_code(normalized, known_row_codes)
    if row_code is not None:
        return [row_code]
    activity_named = bool(
        re.search(
            r"\b(?:оквэд|отрасл\w*|вид\w*\s+деятельност\w*)\b",
            normalized,
        )
    )
    if not activity_named:
        return []
    broad_industry_scope = bool(
        re.search(
            r"\b(?:(?:среди|между)\s+отрасл\w*|по\s+отраслям|"
            r"(?:все|какие|какая|лучшие|худшие)\s+отрасл\w*)\b",
            normalized,
        )
    )
    if broad_industry_scope and not re.search(r"[«\"][^»\"]+[»\"]", question):
        return []
    for quoted_match in re.finditer(r"[«\"]([^»\"]+)[»\"]", question):
        prefix = normalize_text(question[: quoted_match.start()])[-64:]
        if not re.search(
            r"(?:оквэд|отрасл\w*|вид\w*\s+деятельност\w*)\s*$",
            prefix,
        ):
            continue
        result = _metadata_tool(
            session,
            "search_row",
            {"query": quoted_match.group(1)},
            available_periods,
        )
        matches = result.get("matches", [])
        if result.get("resolution") == "exact" and len(matches) == 1:
            return [int(matches[0]["row_code"])]
    rows = session.scalars(
        select(RowDefinition).order_by(RowDefinition.base_row_code)
    ).all()
    matched = _resolve_inflected_row_reference(rows, question)
    if matched is not None:
        return [matched.base_row_code]
    return []


def _question_territory_requirements(
    session: Session,
    question: str,
) -> list[str]:
    """Return exact or uniquely typo-corrected territories in the question."""

    normalized = normalize_text(question)
    unquoted = re.sub(r"[«\"][^»\"]+[»\"]", " ", normalized)
    if re.search(r"\b(?:рф|россия|россии)\b", unquoted):
        return ["Российская Федерация"]
    territory_surface = bool(
        re.search(
            r"\b(?:территори\w*|регион\w*|субъект\w*|област\w*|"
            r"кра[йяе]|республик\w*|округ\w*|москв\w*)\b",
            normalized,
        )
    )
    if not territory_surface:
        return []
    provisional = _enrich_explicit_territories(
        session,
        question,
        QueryPlan(territories=[]),
    )
    return list(dict.fromkeys(provisional.territories))


def _verified_clarification_constraints(
    session: Session,
    question: str,
    journal: list[dict],
    available_periods: list[date],
) -> dict[str, object] | None:
    """Describe fully verified dimensions that make clarification invalid.

    A clarification is rejected only after metadata tools have returned exact
    or explicit-scope answers for every entity dimension and the question has
    an explicit period. Ambiguous or unknown entities therefore remain under
    the model's control.
    """

    required_metrics = _question_metric_requirements(question)
    if not required_metrics:
        return None
    exact_metrics: set[str] = set(required_metrics)
    exact_territories: set[str] = set(
        _question_territory_requirements(session, question)
    )
    exact_rows: set[int] = set(
        _question_row_requirements(session, question, available_periods)
    )
    required_groups = _question_enterprise_group_requirements(question)
    group_hints: set[str] = set(required_groups)
    for item in journal:
        result = item.get("result") or {}
        action = item.get("action")
        resolution = result.get("resolution")
        if action == "search_metric" and resolution == "exact":
            exact_metrics.update(
                str(match.get("semantic_code"))
                for match in result.get("matches", [])
                if match.get("semantic_code")
            )
        elif action == "search_territory" and resolution == "exact":
            exact_territories.update(
                str(match.get("name"))
                for match in result.get("matches", [])
                if match.get("name")
            )
        elif action == "comparable_periods":
            exact_territories.update(
                str(value)
                for value in (item.get("arguments") or {}).get(
                    "territories", []
                )
                if str(value).strip()
            )
        elif action == "search_row" and resolution in {
            "exact",
            "default_scope",
        }:
            exact_rows.update(
                int(match.get("row_code"))
                for match in result.get("matches", [])
                if match.get("row_code") is not None
            )
            if result.get("enterprise_group_hint"):
                group_hints.add(str(result["enterprise_group_hint"]))

    activity_named = bool(
        re.search(
            r"\b(?:оквэд|отрасл\w*|вид\w*\s+деятельност\w*)\b",
            normalize_text(question),
        )
    )
    if not activity_named:
        exact_rows.add(1010)

    if not set(required_metrics).issubset(exact_metrics):
        return None
    if not exact_territories or not exact_rows:
        return None
    if required_groups and not set(required_groups).issubset(group_hints):
        return None
    latest_year = (
        max(item.year for item in available_periods)
        if available_periods
        else date.today().year
    )
    if not extract_years(question, latest_year) and not _explicit_year_window(
        question
    ):
        return None
    return {
        "metric_codes": sorted(exact_metrics),
        "territories": sorted(exact_territories),
        "row_codes": sorted(exact_rows),
        "enterprise_groups": required_groups or sorted(group_hints),
        "period_source": "question",
    }


def _latest_context_item(
    conversation_context: list[dict] | None,
) -> tuple[dict, QueryPlan] | None:
    for item in reversed(conversation_context or []):
        raw = item.get("plan") if isinstance(item, dict) else None
        if not isinstance(raw, dict):
            continue
        try:
            return item, QueryPlan.model_validate(raw)
        except ValidationError:
            continue
    return None


def _question_has_period_scope(question: str) -> bool:
    normalized = normalize_text(question)
    return bool(
        extract_years(question)
        or _explicit_year_window(question)
        or _explicit_cumulative_month(question) is not None
        or re.search(
            r"\b(?:перв\w*|втор\w*|трет\w*|четверт\w*|[1-4]|"
            r"i{1,3}|iv)\s+квартал\w*\b",
            normalized,
            re.IGNORECASE,
        )
    )


def _returns_to_country_trend(question: str) -> bool:
    normalized = normalize_text(question)
    return bool(
        re.search(
            r"\b(?:верн\w*|возврат\w*|снова|обратно)\b.*"
            r"\b(?:рф|росси\w*|российск\w*\s+федерац\w*)\b",
            normalized,
        )
        and any(token in normalized for token in ("динами", "тренд", "трэнд"))
    )


def _references_contextual_industries(question: str) -> bool:
    normalized = normalize_text(question)
    return bool(
        re.search(
            r"\b(?:эти|этим|этих|те\s+же|тем\s+же|выбранн\w*|"
            r"найденн\w*)\s+отрасл\w*\b",
            normalized,
        )
        or "оставь эти отрасл" in normalized
    )


def _context_expected_analyses(item: dict) -> list[str]:
    summary = item.get("result_summary")
    if not isinstance(summary, dict):
        return []
    values = summary.get("expected_analyses") or []
    return [str(value) for value in values if str(value).strip()]


def _conversation_navigation_options(
    conversation_context: list[dict] | None,
) -> list[dict[str, object]]:
    """Expose typed dialogue transitions for the model to interpret.

    The options are derived only from already validated plans.  They do not
    inspect the current wording, so deciding whether a transition applies
    remains the semantic planner's job.
    """

    latest_context = _latest_context_item(conversation_context)
    if latest_context is None:
        return []
    _, latest_plan = latest_context

    historical_series: tuple[dict, QueryPlan] | None = None
    for item in reversed(conversation_context or []):
        if not isinstance(item, dict):
            continue
        raw_plan = item.get("plan")
        if not isinstance(raw_plan, dict):
            continue
        try:
            candidate = QueryPlan.model_validate(raw_plan)
        except ValidationError:
            continue
        analyses = _context_expected_analyses(item)
        if len(candidate.periods) >= 2 and (
            candidate.intent == "trend" or "series" in analyses
        ):
            historical_series = item, candidate
            break
    if historical_series is None:
        return []

    source_item, source_plan = historical_series
    period_modes = list(source_plan.period_modes)
    if len(period_modes) != len(source_plan.periods):
        period_modes = ["cumulative_ytd"] * len(source_plan.periods)
    return [
        {
            "transition": "national_total_trend",
            "semantic_request": (
                "Вернуться от отраслевого или регионального анализа к "
                "общей динамике по Российской Федерации"
            ),
            "apply_only_if_current_question_semantically_requests_it": True,
            "source_question_for_time_window": source_item.get("question"),
            "verified_plan_patch": {
                "intent": "trend",
                "metric_codes": list(latest_plan.metric_codes),
                "enterprise_groups": list(latest_plan.enterprise_groups),
                "territories": ["Российская Федерация"],
                "row_codes": [1010],
                "periods": [
                    item.isoformat() for item in source_plan.periods
                ],
                "period_modes": period_modes,
                "analyses": ["series"],
            },
            "discard_from_current_scope": [
                "selected_industries",
                "selected_territories",
                "industry_to_regions",
            ],
        }
    ]


def _canonical_relative_periods(
    session: Session,
    question: str,
    plan: QueryPlan,
    available_periods: list[date],
) -> list[date]:
    requested = _explicit_year_window(question)
    if requested is None or not available_periods:
        return []
    kind, number = requested
    expected_count = number if kind == "past" else number + 1
    latest_year = max(item.year for item in available_periods)
    first_year = latest_year - expected_count + 1
    years = list(range(first_year, latest_year + 1))
    explicit_month = _explicit_cumulative_month(question)
    regional = any(
        normalize_text(item) != "российская федерация"
        for item in plan.territories
    )
    if regional and explicit_month is None:
        return _latest_regional_comparable_window(
            session, plan, expected_count
        )
    if explicit_month is not None:
        month = explicit_month
    else:
        common = _selected_common_months(session, plan, years)
        if not common:
            months_by_year = [
                {
                    item.month
                    for item in available_periods
                    if item.year == year
                }
                for year in years
            ]
            common = (
                set.intersection(*months_by_year)
                if months_by_year and all(months_by_year)
                else set()
            )
        if not common:
            return []
        month = max(common)
    return [
        date(year, month, monthrange(year, month)[1]) for year in years
    ]


def _verified_contextual_clarification_constraints(
    session: Session,
    question: str,
    conversation_context: list[dict] | None,
) -> dict[str, object] | None:
    latest = _latest_context_item(conversation_context)
    if latest is None:
        return None
    _, previous = latest
    normalized = normalize_text(question)
    required_metrics = _question_metric_requirements(question)
    required_territories = _question_territory_requirements(
        session, question
    )
    # A named but unresolved replacement is genuinely ambiguous; context
    # must not turn it into the previous metric or territory.
    if re.search(r"\b(?:замен\w*|смен\w*)\b.*?\bна\b", normalized):
        if not required_metrics:
            return None
    if re.search(
        r"\b(?:област\w*|кра[йяе]|республик\w*|округ\w*|москв\w*)\b",
        normalized,
    ) and not required_territories and not _returns_to_country_trend(question):
        return None
    referential = any(
        marker in normalized
        for marker in (
            "там же",
            "то же",
            "те же",
            "эти отрасл",
            "этим отрасл",
            "оставь",
            "вернись",
            "уточни динами",
        )
    )
    analytical_followup = bool(
        referential
        or re.search(
            r"\b(?:по\s+каким\s+отрасл\w*|максим\w*|миним\w*|"
            r"лидер\w*|аутсайдер\w*|экстрем\w*)\b",
            normalized,
        )
    )
    if not analytical_followup:
        return None
    return {
        "metric_codes": required_metrics or list(previous.metric_codes),
        "territories": required_territories or list(previous.territories),
        "row_codes": list(previous.row_codes),
        "enterprise_groups": list(previous.enterprise_groups),
        "periods": [item.isoformat() for item in previous.periods[-1:]],
        "period_source": "conversation",
    }


def _verified_semantic_constraints(
    session: Session,
    question: str,
    journal: list[dict],
    available_periods: list[date],
    conversation_context: list[dict] | None,
) -> dict[str, object] | None:
    return _verified_clarification_constraints(
        session, question, journal, available_periods
    ) or _verified_contextual_clarification_constraints(
        session, question, conversation_context
    )


def _reconcile_semantic_constraints(
    session: Session,
    question: str,
    semantic: SemanticQuery,
    available_periods: list[date],
    conversation_context: list[dict] | None,
) -> dict[str, object]:
    """Apply only source-backed and dialogue-inheritance invariants.

    The model still chooses the semantic intent.  This boundary prevents an
    otherwise valid interpretation from silently replacing an exact current
    entity, dropping a prior dialogue dimension, or inventing a comparable
    period.  Every material adjustment is returned for the planner journal.
    """

    if semantic.status != "ready" or semantic.query_plan is None:
        return {}
    plan = semantic.query_plan.model_copy(deep=True)
    before = {
        "metric_codes": list(plan.metric_codes),
        "territories": list(plan.territories),
        "row_codes": list(plan.row_codes),
        "enterprise_groups": list(plan.enterprise_groups),
        "periods": [item.isoformat() for item in plan.periods],
        "period_modes": list(plan.period_modes),
        "analyses": list(semantic.analyses),
    }
    required_metrics = _question_metric_requirements(question)
    required_groups = _question_enterprise_group_requirements(question)
    required_territories = _question_territory_requirements(
        session, question
    )
    required_rows = _question_row_requirements(
        session, question, available_periods
    )
    if required_metrics:
        plan.metric_codes = required_metrics
        semantic.constraint_sources["metric_codes"] = "question"
    if required_groups:
        plan.enterprise_groups = required_groups
        semantic.constraint_sources["enterprise_groups"] = "question"
    if required_territories:
        plan.territories = required_territories
        semantic.constraint_sources["territories"] = "question"
    if required_rows:
        plan.row_codes = required_rows
        plan.okved_codes = []
        semantic.constraint_sources["row_codes"] = "question"

    latest = _latest_context_item(conversation_context)
    if latest is not None:
        previous_item, previous = latest
        normalized = normalize_text(question)
        if not required_metrics:
            plan.metric_codes = list(previous.metric_codes)
            semantic.constraint_sources["metric_codes"] = "conversation"
        if not required_groups:
            plan.enterprise_groups = list(previous.enterprise_groups)
            semantic.constraint_sources["enterprise_groups"] = "conversation"

        selected_industry_regions = bool(
            _references_contextual_industries(question)
            and re.search(r"\bрегион\w*\b", normalized)
        )
        if _returns_to_country_trend(question) or selected_industry_regions:
            plan.territories = ["Российская Федерация"]
            semantic.constraint_sources["territories"] = "question"
        elif not required_territories:
            plan.territories = list(previous.territories)
            semantic.constraint_sources["territories"] = "conversation"

        if not _question_has_period_scope(question):
            inherited_periods = list(previous.periods[-1:])
            inherited_modes = list(previous.period_modes[-1:])
            if _returns_to_country_trend(question):
                for item in reversed(conversation_context or []):
                    raw = item.get("plan") if isinstance(item, dict) else None
                    if not isinstance(raw, dict):
                        continue
                    try:
                        candidate = QueryPlan.model_validate(raw)
                    except ValidationError:
                        continue
                    if len(candidate.periods) >= 2:
                        inherited_periods = list(candidate.periods)
                        inherited_modes = list(candidate.period_modes)
                        break
            plan.periods = inherited_periods
            plan.period_modes = inherited_modes
            plan.period_labels = []
            semantic.constraint_sources["periods"] = "conversation"

        metric_switch = bool(
            required_metrics
            and re.search(
                r"\b(?:замен\w*|смен\w*)\b.*?\bна\b", normalized
            )
        )
        if not required_rows and (
            metric_switch or _references_contextual_industries(question)
        ):
            referenced_rows = _referenced_analysis_rows(
                conversation_context or []
            )
            plan.row_codes = referenced_rows or list(previous.row_codes)
            plan.okved_codes = []
            semantic.constraint_sources["row_codes"] = "conversation"
        previous_analyses = _context_expected_analyses(previous_item)
        if metric_switch and previous_analyses:
            semantic.analyses = previous_analyses
        if selected_industry_regions:
            semantic.analyses = ["industry_to_regions"]
            semantic.ranking_direction = "both"
            semantic.ranking_criterion = "growth_rate"
        if (
            selected_industry_regions
            or metric_switch
            and "industry_to_regions" in semantic.analyses
        ):
            plan.intent = "regional_industry_rank_growth"
            plan.analysis_dimension = "territory"
            plan.group_by = ["industry", "territory"]
            plan.ranking_criteria = ["growth_rate"]
            plan.ranking_direction = semantic.ranking_direction
        if _returns_to_country_trend(question):
            plan.intent = "trend"
            plan.analysis_dimension = None
            plan.group_by = []
            plan.row_codes = [1010]
            plan.okved_codes = []
            semantic.analyses = ["series"]

    if (
        not required_rows
        and not _references_contextual_industries(question)
        and plan.analysis_dimension == "industry"
        and "industry_rank" in semantic.analyses
    ):
        # An industry ranking with no named activity is a ranking over the
        # full industry dimension.  A row inherited from the preceding trend
        # is the seed for that earlier chart, not the ranking scope.
        plan.row_codes = [1010]
        plan.okved_codes = []
        semantic.constraint_sources["row_codes"] = "default"

    relative_periods = _canonical_relative_periods(
        session, question, plan, available_periods
    )
    if relative_periods:
        plan.periods = relative_periods
        plan.period_modes = ["cumulative_ytd"] * len(relative_periods)
        plan.period_labels = []
        semantic.constraint_sources["periods"] = "question"

    semantic.constraint_sources.setdefault(
        "metric_codes", "question" if required_metrics else "default"
    )
    semantic.constraint_sources.setdefault(
        "enterprise_groups", "question" if required_groups else "default"
    )
    semantic.constraint_sources.setdefault(
        "territories", "question" if required_territories else "default"
    )
    semantic.constraint_sources.setdefault(
        "row_codes", "question" if required_rows else "default"
    )
    semantic.constraint_sources.setdefault(
        "periods", "question" if _question_has_period_scope(question) else "default"
    )

    semantic.query_plan = plan
    after = {
        "metric_codes": list(plan.metric_codes),
        "territories": list(plan.territories),
        "row_codes": list(plan.row_codes),
        "enterprise_groups": list(plan.enterprise_groups),
        "periods": [item.isoformat() for item in plan.periods],
        "period_modes": list(plan.period_modes),
        "analyses": list(semantic.analyses),
    }
    return {
        key: {"model": before[key], "reconciled": value}
        for key, value in after.items()
        if before[key] != value
    }


def _validate_question_semantic_constraints(
    session: Session,
    question: str,
    semantic: SemanticQuery,
    journal: list[dict],
    available_periods: list[date],
    conversation_context: list[dict] | None = None,
) -> None:
    """Reject contradictions; never infer or repair a plan here."""

    if semantic.status != "ready" or semantic.query_plan is None:
        return
    plan = semantic.query_plan
    normalized = normalize_text(question)
    analytic_surface = re.sub(r"[«\"][^»\"]+[»\"]", " ", normalized)
    if plan.intent == "catalog" and detect_catalog_target(question) is None:
        raise ValueError(
            "Аналитический запрос с неизвестной сущностью нельзя "
            "подменять просмотром каталога; запроси уточнение"
        )
    required_metrics = _question_metric_requirements(question)
    if required_metrics and set(plan.metric_codes) != set(required_metrics):
        raise ValueError(
            "План изменил явно названные показатели: требуется "
            + ", ".join(required_metrics)
        )
    if (
        not required_metrics
        and plan.intent != "catalog"
        and re.search(
            r"\b(?:налог\w*|страхован\w*|услуг\w*|производств\w*)\b",
            analytic_surface,
        )
    ):
        raise ValueError(
            "Общий термин не определяет один показатель или отрасль; "
            "требуется clarification вместо молчаливой подстановки"
        )
    required_groups = _question_enterprise_group_requirements(question)
    if required_groups and set(plan.enterprise_groups) != set(required_groups):
        raise ValueError(
            "План изменил явно названную группу МСП: требуется "
            + ", ".join(required_groups)
        )
    required_territories = _question_territory_requirements(session, question)
    if required_territories and {
        territory_key(item) for item in plan.territories
    } != {territory_key(item) for item in required_territories}:
        raise ValueError(
            "План изменил явно названные территории: требуется "
            + ", ".join(required_territories)
        )
    required_rows = _question_row_requirements(
        session, question, available_periods
    )
    if required_rows and set(plan.row_codes) != set(required_rows):
        raise ValueError(
            "План изменил явно названную строку формы: требуется "
            + ", ".join(str(item) for item in required_rows)
        )
    activity_named = bool(
        required_rows
        or re.search(
            r"\b(?:оквэд|отрасл\w*|вид\w*\s+деятельност\w*)\b",
            analytic_surface,
        )
    )
    broad_industry_scope = plan.analysis_dimension == "industry" or plan.intent in {
        "rank",
        "rank_growth",
        "rank_difference",
        "regional_industry_rank_growth",
        "analytical_brief",
    }
    contextual_row_scope = bool(
        conversation_context
        and not required_rows
        and (
            _references_contextual_industries(question)
            or re.search(
                r"\b(?:там\s+же|то\s+же|те\s+же)\b",
                normalized,
            )
        )
    )
    if (
        not activity_named
        and not broad_industry_scope
        and not contextual_row_scope
        and plan.row_codes != [1010]
    ):
        raise ValueError(
            "Если отрасль или строка не названа, требуется row_codes=[1010]"
        )
    requested_window = _explicit_year_window(question)
    latest_year = (
        max(item.year for item in available_periods)
        if available_periods
        else date.today().year
    )
    if requested_window is None:
        explicit_years = extract_years(question, latest_year)
        planned_years = sorted({item.year for item in plan.periods})
        if explicit_years and planned_years != explicit_years:
            raise ValueError(
                "periods должны содержать только явно запрошенные годы: "
                + ", ".join(str(item) for item in explicit_years)
            )

    asks_industry = bool(re.search(r"\bотрасл\w*\b", analytic_surface))
    asks_region = bool(re.search(r"\bрегион\w*\b", analytic_surface))
    asks_rank = bool(
        re.search(
            r"\b(?:лидер\w*|аутсайдер\w*|лучш\w*|худш\w*)\b",
            analytic_surface,
        )
    )
    contextual_drill_down = bool(
        conversation_context
        and asks_industry
        and asks_region
        and _references_contextual_industries(question)
    )
    asks_drill_down = asks_industry and asks_region and (
        contextual_drill_down
        or any(
            marker in normalized for marker in ("затем", "потом", "для этих")
        )
    )
    if asks_industry and asks_rank and not asks_drill_down:
        if plan.intent not in {"rank", "rank_growth", "rank_difference"}:
            raise ValueError(
                "Лидеры/аутсайдеры среди отраслей — это отраслевой "
                "рейтинг, а не статистические выбросы"
            )
        if plan.analysis_dimension != "industry":
            raise ValueError(
                "Отраслевой рейтинг требует analysis_dimension=industry"
            )
        asks_top = bool(
            re.search(r"\b(?:лидер\w*|лучш\w*)\b", normalized)
        )
        asks_bottom = bool(
            re.search(r"\b(?:аутсайдер\w*|худш\w*)\b", normalized)
        )
        expected_direction = (
            "both"
            if asks_top and asks_bottom
            else "bottom"
            if asks_bottom
            else "top"
        )
        if semantic.ranking_direction != expected_direction:
            raise ValueError(
                "Направление отраслевого рейтинга должно быть "
                + expected_direction
            )
    asks_growth = bool(
        re.search(r"\b(?:прирост\w*|рост\w*)\b", analytic_surface)
    )
    if (
        asks_rank or "распредел" in normalized or asks_drill_down
    ) and asks_growth:
        if semantic.ranking_criterion != "growth_rate":
            raise ValueError(
                "Рейтинг/распределение по приросту требует "
                "ranking_criterion=growth_rate"
            )
    if asks_industry and (
        "распредел" in analytic_surface or "медиан" in analytic_surface
    ):
        required = {"distribution", "industry_rank"}
        if not required.issubset(semantic.analyses):
            raise ValueError(
                "Распределение между отраслями требует analyses="
                "[industry_rank, distribution]"
            )
        if (
            "структур" not in analytic_surface
            and "structure" in semantic.analyses
        ):
            raise ValueError(
                "analysis=structure нельзя добавлять без запроса структуры"
            )
    if asks_region and "выброс" in analytic_surface:
        required = {"regional_rank", "outliers"}
        if not required.issubset(semantic.analyses):
            raise ValueError(
                "Региональные выбросы требуют regional_rank и outliers"
            )
    if asks_drill_down:
        required = (
            {"industry_to_regions"}
            if contextual_drill_down
            else {"industry_rank", "industry_to_regions"}
        )
        if not required.issubset(semantic.analyses):
            raise ValueError(
                "Последовательность отрасли → регионы требует "
                "industry_rank и industry_to_regions"
            )
    explicit_cumulative_month = _explicit_cumulative_month(question)
    if explicit_cumulative_month is not None:
        if any(mode != "cumulative_ytd" for mode in plan.period_modes):
            raise ValueError(
                "Явный накопительный период требует "
                "period_modes=cumulative_ytd"
            )
        if any(
            item.month != explicit_cumulative_month for item in plan.periods
        ):
            raise ValueError(
                "Месяц в periods не совпадает с явно "
                "запрошенным накопительным срезом"
            )
    if "квартал" not in normalized and any(
        mode == "standalone_quarter" for mode in plan.period_modes
    ):
        raise ValueError(
            "standalone_quarter нельзя выбирать без "
            "явного указания отдельного кварта"
        )
    distinct_years = {item.year for item in plan.periods}
    if len(distinct_years) >= 3:
        if plan.intent != "trend":
            raise ValueError(
                "Три и более годовые точки требуют "
                "intent=trend"
            )
        if "series" not in semantic.analyses:
            raise ValueError(
                "Три и более годовые точки требуют "
                "analysis=series"
            )

    requested = requested_window
    if requested is None or not plan.periods:
        return
    kind, number = requested
    expected_count = number if kind == "past" else number + 1
    if len(plan.periods) != expected_count:
        raise ValueError(
            f"Вопрос требует {expected_count} временных "
            f"точек, а план содержит {len(plan.periods)}"
        )

    regional = any(
        normalize_text(item) != "российская федерация"
        for item in plan.territories
    )
    expected = _canonical_relative_periods(
        session, question, plan, available_periods
    )
    if not expected:
        return
    if sorted(plan.periods) != expected:
        raise ValueError(
            "Периоды не совпадают с последними "
            f"{expected_count} сопоставимыми точками: "
            + ", ".join(item.isoformat() for item in expected)
        )


def _security_rejection(question: str) -> PlannerResult | None:
    """Reject control-plane requests before any model sees them."""

    security_surface = normalize_text(question)
    if "причин" in security_surface and any(
        marker in security_surface
        for marker in (
            "статистически значим",
            "корреляц",
            "объяви",
            "докажи",
        )
    ):
        return PlannerResult(
            None,
            "epistemic-safety-validation",
            [],
            status="needs_clarification",
            clarification_question=(
                "Наблюдательные данные 1-МСП не позволяют объявлять "
                "статистическую связь причинной. Уточните описательный "
                "показатель, территорию и сопоставимый период."
            ),
            clarification_options=[
                "Описать динамику",
                "Сравнить территории",
            ],
            tool_journal=[],
        )
    if not any(
        marker in security_surface
        for marker in (
            "sql",
            "drop table",
            "system prompt",
            "системн prompt",
            "абсолютн путь",
            "внутренн путь",
            "ключ api",
            "имена таблиц",
            ".env",
        )
    ):
        return None
    return PlannerResult(
        None,
        "security-validation",
        [],
        status="no_data",
        clarification_question=(
            "SQL, системные инструкции, ключи и внутренние пути не "
            "раскрываются. Сформулируйте аналитический вопрос по "
            "показателям, территориям, отраслям и периодам."
        ),
        clarification_options=["Задать аналитический вопрос"],
        tool_journal=[],
    )


def _metadata_tool(
    session: Session,
    name: str,
    arguments: dict,
    available_periods: list[date],
) -> dict:
    query = normalize_text(str(arguments.get("query", "")))
    if name == "search_metric":
        metrics = session.scalars(
            select(MetricDefinition).order_by(
                MetricDefinition.report_column_number
            )
        ).all()
        column_match = re.search(
            r"(?:граф\w*|колонк\w*)\s*[№#:]?\s*(\d{1,2})\b",
            query,
        )
        direct_codes = (
            [METRIC_BY_COLUMN[int(column_match.group(1))].code]
            if column_match
            and int(column_match.group(1)) in METRIC_BY_COLUMN
            else _question_metric_requirements(query)
        )
        if direct_codes:
            matches = [
                item for item in metrics if item.semantic_code in direct_codes
            ]
            resolution = "exact"
        else:
            exact_aliases = [
                item
                for item in metrics
                if query == normalize_text(item.name_ru)
                or query == normalize_text(item.semantic_code)
                or query
                in {
                    normalize_text(str(alias))
                    for alias in (item.aliases_json or [])
                }
            ]
            matches = exact_aliases or [
                item
                for item in metrics
                if query in normalize_text(item.name_ru)
                or any(
                    token in normalize_text(item.name_ru)
                    for token in query.split()
                    if len(token) >= 4
                )
            ]
            resolution = "exact" if exact_aliases else "candidates"
        return {
            "resolution": resolution if matches else "not_found",
            "matches": [
                {
                    "semantic_code": item.semantic_code,
                    "name": item.name_ru,
                    "column": item.report_column_number,
                    "aliases": item.aliases_json or [],
                }
                for item in matches[:8]
            ],
            "instruction": (
                "Одно точное совпадение авторитетно; "
                "не задавай уточнение."
                if resolution == "exact" and len(matches) == 1
                else "Все точные совпадения составляют явно названный "
                "совокупный показатель; сохрани их вместе."
                if resolution == "exact" and len(matches) > 1
                else "Точного совпадения нет. Кандидаты не являются "
                "подтверждением: уточни неизвестный показатель, если "
                "следующий точный поиск не разрешит его однозначно."
            ),
        }
    if name == "search_territory":
        raw_query = str(arguments.get("query", ""))
        territories = session.scalars(
            select(Territory).order_by(Territory.name_ru)
        ).all()
        russia_scope = bool(
            re.search(r"\b(?:рф|россия|россии)\b", query)
            or "все регион" in query
        )
        if russia_scope:
            matches = [
                item
                for item in territories
                if item.territory_type == "country"
            ]
        else:
            exact_matches = [
                item
                for item in territories
                if query == item.normalized_name
            ]
            matches = exact_matches or [
                item
                for item in territories
                if query in item.normalized_name
                or item.normalized_name in query
                or all(
                    token in item.normalized_name
                    for token in query.split()
                    if len(token) >= 4
                )
            ]
        match_strategy = "catalog"
        if not matches and query:
            provisional = _enrich_explicit_territories(
                session,
                f"территория «{raw_query}»",
                QueryPlan(territories=[]),
            )
            canonical = {
                territory_key(name) for name in provisional.territories
            }
            matches = [
                item
                for item in territories
                if territory_key(item.name_ru) in canonical
            ]
            if len({territory_key(item.name_ru) for item in matches}) == 1:
                matches = [matches[0]]
                match_strategy = "unique_fuzzy"
        return {
            "resolution": "exact" if len(matches) == 1 else "candidates",
            "match_strategy": match_strategy,
            "scope": "all_regions" if "все регион" in query else None,
            "matches": [
                {"name": item.name_ru, "type": item.territory_type}
                for item in matches[:10]
            ],
            "instruction": (
                "Одно точное совпадение авторитетно; "
                "не задавай уточнение."
                if len(matches) == 1
                else "Сопоставь кандидаты с вопросом."
            ),
        }
    if name == "search_row":
        rows = session.scalars(
            select(RowDefinition).order_by(RowDefinition.base_row_code)
        ).all()
        total_row = next(
            (item for item in rows if item.base_row_code == 1010), None
        )
        enterprise_scope = "мсп" in query and not any(
            marker in query
            for marker in ("оквэд", "отрасл", "вид деятел")
        )
        all_industries_scope = any(
            marker in query
            for marker in (
                "все отрасл",
                "среди отрасл",
                "между отрасл",
                "по отраслям",
                "отрасли экономики",
                "разделы оквэд",
                "виды экономической деятельности",
                "все виды деятельности",
                "всего по экономике",
                "общий итог",
            )
        ) or bool(re.fullmatch(r"(?:отрасл\w*|оквэд)", query))
        explicit_enterprise_group = bool(
            re.search(r"\bмикропредприят\w*\b", query)
            or "без микро" in query
            or re.search(r"\bмсп\b", query)
        )
        enterprise_scope = enterprise_scope or explicit_enterprise_group
        explicit_row_code = _explicit_row_code(
            query,
            {item.base_row_code for item in rows},
        )
        compact_query = re.sub(r"\s+", "", query)
        exact_matches = [
            item
            for item in rows
            if (
                explicit_row_code is not None
                and item.base_row_code == explicit_row_code
            )
            or query == normalize_text(item.name_ru)
            or query == normalize_text(item.okved_text or "")
            or compact_query
            == re.sub(r"\s+", "", normalize_text(item.name_ru))
            or compact_query
            == re.sub(
                r"\s+", "", normalize_text(item.okved_text or "")
            )
            or query
            in {
                normalize_text(str(alias))
                for alias in (item.aliases_json or [])
            }
        ]
        if exact_matches:
            matches = exact_matches
            resolution = "exact"
        elif (enterprise_scope or all_industries_scope) and total_row:
            matches = [total_row]
            resolution = "default_scope"
        else:
            inflected_match = _resolve_inflected_row_reference(rows, query)
            substring_matches = [
                item
                for item in rows
                if query and (
                    query in normalize_text(item.name_ru)
                    or query in normalize_text(item.okved_text or "")
                )
            ]
            matches = ([inflected_match] if inflected_match else []) or substring_matches or [
                item
                for item in rows
                if any(
                    token in normalize_text(
                        f"{item.name_ru} {item.okved_text or ''}"
                    )
                    for token in query.split()
                    if len(token) >= 4
                )
            ]
            resolution = (
                "exact"
                if inflected_match is not None or len(substring_matches) == 1
                else "candidates"
            )
        return {
            "resolution": resolution if matches else "not_found",
            "enterprise_group_hint": (
                "small_and_medium_without_micro"
                if "без микро" in query
                else "micro"
                if "микро" in query
                else "all_msp"
                if enterprise_scope
                else None
            ),
            "scope": "all_industries" if all_industries_scope else None,
            "matches": [
                {
                    "row_code": item.base_row_code,
                    "name": item.name_ru[:180],
                    "okved": item.okved_text,
                }
                for item in matches[:10]
            ],
            "instruction": (
                "Группа МСП — enterprise_groups, не отрасль. "
                "Используй row_codes=[1010] как итог по "
                "экономике и не задавай уточнение."
                if enterprise_scope
                else "Для рейтинга/распределения по всем отраслям "
                "row_codes=[1010] задаёт scope; не проси список."
                if all_industries_scope
                else "Одно точное совпадение авторитетно."
                if resolution == "exact" and len(matches) == 1
                else "Сопоставь кандидаты с вопросом."
            ),
        }
    if name == "comparable_periods":
        years = sorted(
            {
                int(value)
                for value in arguments.get("years", [])
                if str(value).isdigit()
            }
        )
        observation_count = int(arguments.get("observation_count") or 0)
        territories = [
            str(value) for value in arguments.get("territories", [])
        ]
        regional = any(
            normalize_text(item) != "российская федерация"
            for item in territories
        )
        if regional and observation_count:
            provisional = QueryPlan(
                intent="trend",
                metric_codes=["receipts_budget_total"],
                enterprise_groups=[
                    str(value)
                    for value in arguments.get(
                        "enterprise_groups", ["all_msp"]
                    )
                ],
                territories=territories,
                row_codes=[1010],
            )
            regional_window = _latest_regional_comparable_window(
                session,
                provisional,
                observation_count,
            )
            return {
                "scope": "regional",
                "years": [item.year for item in regional_window],
                "common_months": (
                    [regional_window[0].month] if regional_window else []
                ),
                "latest_common_periods": [
                    item.isoformat() for item in regional_window
                ],
                "observation_count": observation_count,
                "instruction": (
                    "Используй ровно эти периоды как "
                    "последнее сопоставимое региональное "
                    "окно."
                ),
            }
        if observation_count and len(years) >= observation_count:
            years = years[-observation_count:]
        months_by_year = {
            year: {
                period.month
                for period in available_periods
                if period.year == year
            }
            for year in years
        }
        common = (
            set.intersection(*months_by_year.values())
            if months_by_year and all(months_by_year.values())
            else set()
        )
        return {
            "scope": "national",
            "years": years,
            "common_months": sorted(common),
            "latest_common_periods": [
                date(year, max(common), monthrange(year, max(common))[1]).isoformat()
                for year in years
            ]
            if common
            else [],
            "observation_count": observation_count or len(years),
            "instruction": (
                "Используй latest_common_periods без смены "
                "режима на standalone_quarter, если вопрос "
                "не называет отдельный квартал."
            ),
        }
    if name in {"check_availability", "validate_plan"}:
        try:
            candidate = QueryPlan.model_validate(arguments.get("plan", {}))
            candidate = validate_plan(session, candidate, available_periods)
            return {
                "valid": True,
                "plan": candidate.model_dump(mode="json"),
            }
        except (ValidationError, ValueError) as exc:
            return {"valid": False, "error": str(exc)}
    raise ValueError(f"Инструмент метаданных «{name}» не разрешён")


def _selected_common_months(
    session: Session,
    plan: QueryPlan,
    years: list[int],
) -> set[int]:
    sections: set[str] = set()
    for group in plan.enterprise_groups:
        sections.update(
            {"I", "II"}
            if group == "small_and_medium_without_micro"
            else {"II"}
            if group == "micro"
            else {"I"}
        )
    rows = session.execute(
        select(
            MspFact.period_end,
            MetricDefinition.semantic_code,
            RowDefinition.base_row_code,
            MspFact.section,
        )
        .join(MetricDefinition)
        .join(RowDefinition)
        .join(SourceFile)
        .where(
            MetricDefinition.semantic_code.in_(plan.metric_codes),
            RowDefinition.base_row_code.in_(plan.row_codes),
            MspFact.section.in_(sections),
            SourceFile.status.in_(["valid", "valid_with_warnings"]),
        )
    ).all()
    facts_by_period: dict[date, set[tuple[str, int, str]]] = {}
    for period, metric_code, row_code, section in rows:
        facts_by_period.setdefault(period, set()).add(
            (metric_code, row_code, section)
        )
    required = {
        (metric_code, row_code, section)
        for metric_code in plan.metric_codes
        for row_code in plan.row_codes
        for section in sections
    }
    months_by_year = [
        {
            period.month
            for period, keys in facts_by_period.items()
            if period.year == year and required.issubset(keys)
        }
        for year in years
    ]
    return (
        set.intersection(*months_by_year)
        if months_by_year and all(months_by_year)
        else set()
    )


def _selected_regional_common_months(
    session: Session,
    plan: QueryPlan,
    years: list[int],
) -> set[int]:
    """Return source-backed months present for every requested regional year."""

    required_kinds: set[str] = set()
    for group in plan.enterprise_groups:
        if group in {"all_msp", "small_and_medium_without_micro"}:
            required_kinds.add("regional_section_i")
        if group in {"micro", "small_and_medium_without_micro"}:
            required_kinds.add("regional_section_ii")
    required_kinds = required_kinds or {"regional_section_i"}
    source_rows = session.execute(
        select(SourceFile.period_end, SourceFile.workbook_kind).where(
            SourceFile.workbook_kind.in_(required_kinds),
            SourceFile.status.in_(["valid", "valid_with_warnings"]),
            SourceFile.period_end.is_not(None),
        )
    ).all()
    kinds_by_period: dict[date, set[str]] = {}
    for period, workbook_kind in source_rows:
        kinds_by_period.setdefault(period, set()).add(str(workbook_kind))
    months_by_year = [
        {
            period.month
            for period, kinds in kinds_by_period.items()
            if period.year == year and required_kinds.issubset(kinds)
        }
        for year in years
    ]
    return (
        set.intersection(*months_by_year)
        if months_by_year and all(months_by_year)
        else set()
    )


def _latest_regional_comparable_window(
    session: Session,
    plan: QueryPlan,
    observation_count: int,
) -> list[date]:
    """Find the latest consecutive regional years with one common slice."""

    required_kinds: set[str] = set()
    for group in plan.enterprise_groups:
        if group in {"all_msp", "small_and_medium_without_micro"}:
            required_kinds.add("regional_section_i")
        if group in {"micro", "small_and_medium_without_micro"}:
            required_kinds.add("regional_section_ii")
    required_kinds = required_kinds or {"regional_section_i"}
    source_rows = session.execute(
        select(SourceFile.period_end, SourceFile.workbook_kind).where(
            SourceFile.workbook_kind.in_(required_kinds),
            SourceFile.status.in_(["valid", "valid_with_warnings"]),
            SourceFile.period_end.is_not(None),
        )
    ).all()
    kinds_by_period: dict[date, set[str]] = {}
    for period, workbook_kind in source_rows:
        kinds_by_period.setdefault(period, set()).add(str(workbook_kind))
    months_by_year: dict[int, set[int]] = {}
    for period, kinds in kinds_by_period.items():
        if required_kinds.issubset(kinds):
            months_by_year.setdefault(period.year, set()).add(period.month)
    years = sorted(months_by_year)
    if observation_count <= 0 or len(years) < observation_count:
        return []
    for start in range(len(years) - observation_count, -1, -1):
        window = years[start : start + observation_count]
        if any(
            right - left != 1
            for left, right in zip(window, window[1:], strict=False)
        ):
            continue
        common = set.intersection(
            *(months_by_year[year] for year in window)
        )
        if common:
            month = max(common)
            return [
                date(year, month, monthrange(year, month)[1])
                for year in window
            ]
    return []


def _apply_selected_common_slice(
    session: Session,
    question: str,
    plan: QueryPlan,
) -> QueryPlan:
    regional = any(
        normalize_text(item) != "российская федерация"
        for item in plan.territories
    )
    if (
        (plan.intent != "trend" and not regional)
        or any(mode == "standalone_quarter" for mode in plan.period_modes)
        or _explicit_cumulative_month(question) is not None
    ):
        return plan
    years = sorted({period.year for period in plan.periods})
    common = (
        _selected_regional_common_months(session, plan, years)
        if regional
        else _selected_common_months(session, plan, years)
    )
    if not common:
        return plan
    month = max(common)
    plan.periods = [
        date(year, month, monthrange(year, month)[1]) for year in years
    ]
    plan.period_modes = ["cumulative_ytd"] * len(plan.periods)
    plan.period_labels = []
    return plan


def _clarification_for_error(
    question: str,
    error: Exception,
    available_periods: list[date],
    session: Session | None = None,
    plan: QueryPlan | None = None,
) -> PlannerResult:
    message = str(error)
    latest_year = (
        max(period.year for period in available_periods)
        if available_periods
        else date.today().year
    )
    years = extract_years(question, latest_year)
    options: list[str] = []
    if years and session is not None and plan is not None:
        common = _selected_common_months(session, plan, years)
        if common:
            options.append(
                "Использовать общий накопительный срез по "
                + MONTH_NAMES[max(common)]
            )
    elif years:
        months_by_year = [
            {item.month for item in available_periods if item.year == year}
            for year in years
        ]
        common = (
            set.intersection(*months_by_year)
            if months_by_year and all(months_by_year)
            else set()
        )
        if common:
            month = max(common)
            options.append(
                f"Использовать общий накопительный срез по {MONTH_NAMES[month]}"
            )
    if "показател" in message.lower() or "metric" in message.lower():
        prompt = (
            "Какой именно показатель нужен: поступления в бюджет, "
            "страховые взносы или конкретный налог?"
        )
        options = ["Поступления от МСП, всего", "Конкретный налог"]
    elif "отсутств" in message.lower() or "нет " in message.lower():
        prompt = (
            f"{message}. Какой доступный сопоставимый период использовать?"
        )
        options = options or ["Уточнить период", "Сузить диапазон лет"]
    else:
        prompt = f"Нужно уточнение, чтобы не предполагать данные: {message}"
    return PlannerResult(
        plan=None,
        mode="deterministic",
        warnings=[],
        status="needs_clarification",
        clarification_question=prompt,
        clarification_options=options or None,
        tool_journal=[],
    )


def _semantic_clarification(
    question: str, plan: QueryPlan
) -> PlannerResult | None:
    normalized = normalize_text(question)
    # Quoted fragments are already resolved by the metric, industry and
    # territory catalogs. Words such as ``область`` in a long industry name
    # or ``региональные`` in a metric name are not an unnamed geography.
    unquoted = re.sub(r"[«\"][^»\"]+[»\"]", " ", normalized)
    # Remove the already resolved metric wording before searching for vague
    # geography. In particular, "Региональные налоги" contains the
    # lexical stem "регион", but names a metric rather than a place.
    for metric_code in plan.metric_codes:
        seed = METRIC_BY_CODE.get(metric_code)
        if seed is None:
            continue
        metric_names = [seed.name, *(seed.aliases or ())]
        if seed.short_name:
            metric_names.append(seed.short_name)
        for metric_name in sorted(metric_names, key=len, reverse=True):
            unquoted = unquoted.replace(normalize_text(metric_name), " ")
    unnamed_territory = re.search(
        r"\b(?:по|в)\s+(?:этому|этой|выбранному|нужному)?\s*"
        r"(?:региону|регионе|области|субъекте)\b",
        unquoted,
    ) or re.search(
        r"\b(?:област\w*|республик\w*|кра[йяе]|"
        r"автономн\w+\s+округ\w*|субъект\w*|регион\w*)\b",
        unquoted,
    )
    only_country = all(
        normalize_text(item) == "российская федерация"
        for item in plan.territories
    )
    territory_dimension = bool(
        re.search(
            r"\b(?:регион(?:ы|ах|ам|ами|ов)|"
            r"субъект(?:ы|ах|ам|ами|ов))\b",
            unquoted,
        )
        or re.search(
            r"\b(?:в|по|среди)\s+ка(?:ком|ких|кой)\s+"
            r"(?:регион\w*|субъект\w*|област\w*|кра\w*|республик\w*)\b",
            unquoted,
        )
        or re.search(
            r"\bрегиональн\w*\s+"
            r"(?:отклон\w*|аномал\w*|экстрем\w*|динамик\w*)\b",
            unquoted,
        )
    )
    if (
        unnamed_territory
        and only_country
        and not territory_dimension
        and plan.intent
        not in {
            "rank",
            "outliers",
            "rank_growth",
            "rank_difference",
            "regional_average_growth",
            "regional_industry_rank_growth",
            "analytical_brief",
        }
    ):
        return PlannerResult(
            None,
            "deterministic",
            [],
            status="needs_clarification",
            clarification_question=(
                "Какую конкретно территорию или субъект РФ нужно выбрать?"
            ),
            clarification_options=["Указать субъект РФ", "Вся Россия"],
            tool_journal=[],
        )
    explicit_activity_dimension = re.search(
        r"\b(?:оквэд|отрасл\w*|вид\w*\s+деятельност\w*)\b",
        normalized,
    )
    activity_word = re.search(
        r"\b(?:производств\w*|торговл\w*|строительств\w*|"
        r"транспорт\w*)\b",
        normalized,
    )
    activity_is_metric_name = bool(
        re.search(r"\bтранспорт\w*\s+налог\w*\b", normalized)
    )
    vague_activity = explicit_activity_dimension or (
        activity_word and not activity_is_metric_name
    )
    if (
        vague_activity
        and plan.row_codes == [1010]
        and plan.intent not in {"rank", "outliers"}
        and not plan.intent.startswith("rank_")
        and plan.intent != "analytical_brief"
        and plan.intent != "catalog"
    ):
        return PlannerResult(
            None,
            "deterministic",
            [],
            status="needs_clarification",
            clarification_question=(
                "Какой вид деятельности или код ОКВЭД нужно выбрать?"
            ),
            clarification_options=["Указать ОКВЭД", "Все виды деятельности"],
            tool_journal=[],
        )
    return None


def _log_agent_call(
    session: Session,
    provider: LLMProvider,
    *,
    question: str,
    started: float,
    status: str,
    journal: list[dict],
    error_code: str | None = None,
) -> None:
    session.add(
        AiCallLog(
            provider=provider.name,
            model=provider.model,
            purpose="query_plan_agent",
            status=status,
            duration_ms=int((time.perf_counter() - started) * 1000),
            input_hash=hashlib.sha256(question.encode("utf-8")).hexdigest(),
            token_usage_json={
                "usage": getattr(provider, "last_usage", {}),
                "tool_journal": journal,
            },
            error_code=error_code,
        )
    )
    session.commit()


def _is_confirmed_source_absence(error: Exception) -> bool:
    message = str(error)
    return message.startswith(
        (
            "В источниках отсутствуют выбранные факты:",
            "Для расчёта отсутствуют накопительные данные за:",
            "Нет сопоставимой региональной книги:",
        )
    )


async def plan_question(
    session: Session,
    settings: Settings,
    question: str,
    available_periods: list[date],
    conversation_context: list[dict] | None = None,
    *,
    allow_deterministic_fallback: bool = True,
) -> PlannerResult:
    effective_question = question
    if conversation_context:
        latest_context = conversation_context[-1]
        pending = latest_context.get("pending_question")
        if isinstance(pending, str) and pending.strip():
            effective_question = resolve_clarification_answer(
                pending,
                question,
                clarification_question=str(
                    latest_context.get("clarification_question") or ""
                ),
                clarification_options=latest_context.get(
                    "clarification_options"
                ),
            )
    security_rejection = _security_rejection(effective_question)
    if security_rejection is not None:
        return security_rejection

    provider = configured_provider(settings)
    warnings: list[str] = []
    if _provider_is_available(provider, settings):
        started = time.perf_counter()
        journal: list[dict] = []
        model_calls = 0
        seen_tool_results: dict[str, dict] = {}
        seen_result_signatures: set[str] = set()
        no_progress_steps = 0
        rejected_clarifications = 0
        emergency_max_steps = settings.analytic_planner_emergency_max_steps
        navigation_options = _conversation_navigation_options(
            conversation_context
        )
        messages = _planner_messages(
            session,
            effective_question,
            available_periods,
            conversation_context,
        )
        try:
            while model_calls < emergency_max_steps:
                model_calls += 1
                step = model_calls
                raw = await provider.complete(
                    messages,
                    response_format={"type": "json_object"},
                    temperature=0,
                    max_tokens=1800,
                    extra_body={"thinking": {"type": "disabled"}},
                    allow_fallback=False,
                )
                try:
                    action = _parse_json_object(raw)
                except ValueError as exc:
                    journal.append(
                        {
                            "step": step,
                            "action": "invalid_json_response",
                            "status": "rejected",
                            "error": str(exc),
                            "response_hash": hashlib.sha256(
                                raw.encode("utf-8")
                            ).hexdigest(),
                        }
                    )
                    messages.extend(
                        [
                            {"role": "assistant", "content": raw},
                            {
                                "role": "user",
                                "content": json.dumps(
                                    {
                                        "tool": "validate_action",
                                        "valid": False,
                                        "error": str(exc),
                                        "instruction": (
                                            "Транспортный формат неверен. "
                                            "Продолжи тот же агентный разбор "
                                            "и верни ровно один JSON-объект "
                                            "разрешённого действия без текста "
                                            "вне JSON."
                                        ),
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        ]
                    )
                    continue
                if "action" not in action and (
                    "query_plan" in action
                    or "clarification_question" in action
                    or "no_data_reason" in action
                ):
                    action = {
                        "action": "final",
                        "semantic_query": action,
                    }
                action_name = str(action.get("action", ""))
                if not action_name:
                    journal.append(
                        {
                            "step": step,
                            "action": "invalid_model_response",
                            "status": "rejected",
                            "keys": sorted(action),
                        }
                    )
                    messages.extend(
                        [
                            {"role": "assistant", "content": raw},
                            {
                                "role": "user",
                                "content": json.dumps(
                                    {
                                        "tool": "validate_action",
                                        "valid": False,
                                        "instruction": (
                                            "Ответ не содержит action. "
                                            "Верни одно из разрешённых "
                                            "действий; если план уже valid, "
                                            "верни action=final с полным "
                                            "SemanticQuery."
                                        ),
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        ]
                    )
                    continue
                if action_name == "clarify":
                    clarification = str(action.get("question", "")).strip()
                    if not clarification:
                        raise ValueError("Агент вернул пустой вопрос")
                    if len(clarification) > 500:
                        journal.append(
                            {
                                "step": step,
                                "action": "clarify",
                                "status": "rejected",
                                "error": "Вопрос для уточнения длиннее 500 символов",
                            }
                        )
                        messages.extend(
                            [
                                {"role": "assistant", "content": raw},
                                {
                                    "role": "user",
                                    "content": json.dumps(
                                        {
                                            "tool": "validate_clarification",
                                            "valid": False,
                                            "error": (
                                                "clarification question exceeds "
                                                "500 characters"
                                            ),
                                            "instruction": (
                                                "Сохрани смысл уточнения, но "
                                                "верни один короткий вопрос не "
                                                "длиннее 500 символов и не более "
                                                "шести кратких options."
                                            ),
                                        },
                                        ensure_ascii=False,
                                    ),
                                },
                            ]
                        )
                        continue
                    options = [
                        str(item)
                        for item in action.get("options", [])
                        if str(item).strip()
                    ][:6]
                    verified = _verified_semantic_constraints(
                        session,
                        question,
                        journal,
                        available_periods,
                        conversation_context,
                    )
                    if verified is not None:
                        rejected_clarifications += 1
                        journal.append(
                            {
                                "step": step,
                                "action": "clarify",
                                "status": "rejected",
                                "error": (
                                    "Все измерения уже однозначно "
                                    "подтверждены метаданными"
                                ),
                                "verified_constraints": verified,
                            }
                        )
                        messages.extend(
                            [
                                {"role": "assistant", "content": raw},
                                {
                                    "role": "user",
                                    "content": json.dumps(
                                        {
                                            "tool": "validate_clarification",
                                            "valid": False,
                                            "verified_constraints": verified,
                                            "instruction": (
                                                "Уточнение необоснованно: "
                                                "инструменты уже однозначно "
                                                "подтвердили все измерения. "
                                                "Построй и проверь QueryPlan, "
                                                "затем верни final. Не меняй "
                                                "подтверждённые ограничения."
                                            ),
                                        },
                                        ensure_ascii=False,
                                    ),
                                },
                            ]
                        )
                        if rejected_clarifications >= 3:
                            raise ValueError(
                                "Агент трижды запросил уточнение после "
                                "полной проверки ограничений"
                            )
                        continue
                    journal.append(
                        {"step": step, "action": "clarify", "status": "done"}
                    )
                    _log_agent_call(
                        session,
                        provider,
                        question=question,
                        started=started,
                        status="clarification",
                        journal=journal,
                    )
                    return PlannerResult(
                        None,
                        f"agent:{provider.name}",
                        warnings,
                        status="needs_clarification",
                        clarification_question=clarification,
                        clarification_options=options or None,
                        tool_journal=journal,
                        semantic_query=SemanticQuery(
                            status="clarification",
                            clarification_question=clarification,
                            clarification_options=options,
                        ),
                        model_calls=step,
                    )
                if action_name == "final":
                    try:
                        semantic = SemanticQuery.model_validate(
                            action.get("semantic_query", {})
                        )
                        reconciliation = _reconcile_semantic_constraints(
                            session,
                            question,
                            semantic,
                            available_periods,
                            conversation_context,
                        )
                        semantic = _validate_semantic_query(semantic)
                        if semantic.status == "clarification":
                            verified = _verified_semantic_constraints(
                                session,
                                question,
                                journal,
                                available_periods,
                                conversation_context,
                            )
                            if verified is not None:
                                rejected_clarifications += 1
                                journal.append(
                                    {
                                        "step": step,
                                        "action": "final",
                                        "status": "rejected_clarification",
                                        "verified_constraints": verified,
                                    }
                                )
                                messages.extend(
                                    [
                                        {
                                            "role": "assistant",
                                            "content": raw,
                                        },
                                        {
                                            "role": "user",
                                            "content": json.dumps(
                                                {
                                                    "tool": (
                                                        "validate_clarification"
                                                    ),
                                                    "valid": False,
                                                    "verified_constraints": (
                                                        verified
                                                    ),
                                                    "instruction": (
                                                        "Уточнение уже "
                                                        "разрешено точными "
                                                        "ответами инструментов. "
                                                        "Верни ready QueryPlan "
                                                        "после validate_plan."
                                                    ),
                                                },
                                                ensure_ascii=False,
                                            ),
                                        },
                                    ]
                                )
                                if rejected_clarifications >= 3:
                                    raise ValueError(
                                        "Агент трижды запросил уточнение "
                                        "после полной проверки ограничений"
                                    )
                                continue
                        if reconciliation:
                            journal.append(
                                {
                                    "step": step,
                                    "action": "reconcile_constraints",
                                    "status": "applied",
                                    "changes": reconciliation,
                                }
                            )
                        _validate_question_semantic_constraints(
                            session,
                            question,
                            semantic,
                            journal,
                            available_periods,
                            conversation_context,
                        )
                        if semantic.status == "clarification":
                            journal.append(
                                {
                                    "step": step,
                                    "action": "final",
                                    "status": "clarification",
                                }
                            )
                            _log_agent_call(
                                session,
                                provider,
                                question=question,
                                started=started,
                                status="clarification",
                                journal=journal,
                            )
                            return PlannerResult(
                                None,
                                f"agent:{provider.name}",
                                warnings,
                                status="needs_clarification",
                                clarification_question=(
                                    semantic.clarification_question
                                ),
                                clarification_options=(
                                    semantic.clarification_options or None
                                ),
                                tool_journal=journal,
                                semantic_query=semantic,
                                model_calls=step,
                            )
                        if semantic.status == "no_data":
                            if _explicit_year_window(question) is not None:
                                raise ValueError(
                                    "Относительное окно сопоставимых лет "
                                    "нужно перестроить по последней доступной "
                                    "дате; no_data по отвергнутому окну "
                                    "недопустим"
                                )
                            if (
                                _verified_contextual_clarification_constraints(
                                    session,
                                    question,
                                    conversation_context,
                                )
                                is not None
                            ):
                                raise ValueError(
                                    "Контекст полностью задаёт продолжение; "
                                    "no_data допустим только после проверки "
                                    "унаследованного плана"
                                )
                            availability_checks = [
                                (item.get("result") or {}).get("valid")
                                for item in journal
                                if item.get("action")
                                in {"check_availability", "validate_plan"}
                                and (item.get("result") or {}).get("valid")
                                is not None
                            ]
                            checked_presence = bool(
                                availability_checks
                                and availability_checks[-1] is True
                            )
                            if checked_presence:
                                raise ValueError(
                                    "no_data противоречит уже подтверждённой "
                                    "доступности плана; верни status=ready"
                                )
                            checked_absence = bool(
                                availability_checks
                                and availability_checks[-1] is False
                            )
                            if not checked_absence:
                                raise ValueError(
                                    "no_data требует отрицательной проверки "
                                    "check_availability"
                                )
                            journal.append(
                                {
                                    "step": step,
                                    "action": "final",
                                    "status": "no_data",
                                }
                            )
                            _log_agent_call(
                                session,
                                provider,
                                question=question,
                                started=started,
                                status="no_data",
                                journal=journal,
                            )
                            return PlannerResult(
                                None,
                                f"agent:{provider.name}",
                                warnings,
                                status="no_data",
                                clarification_question=semantic.no_data_reason,
                                tool_journal=journal,
                                semantic_query=semantic,
                                model_calls=step,
                            )
                        try:
                            plan = validate_plan(
                                session,
                                semantic.query_plan,
                                available_periods,
                            )
                        except ValueError as validation_error:
                            if not _is_confirmed_source_absence(
                                validation_error
                            ):
                                raise
                            if _explicit_year_window(question) is not None:
                                raise ValueError(
                                    "Выбранный моделью срез отсутствует, но "
                                    "запрос допускает source-backed "
                                    "сопоставимое окно; исправь plan вместо "
                                    "no_data: "
                                    + str(validation_error)
                                ) from validation_error
                            reason = str(validation_error)
                            semantic = SemanticQuery(
                                status="no_data",
                                no_data_reason=reason,
                            )
                            journal.append(
                                {
                                    "step": step,
                                    "action": "validate_semantic_query",
                                    "status": "confirmed_no_data",
                                    "reason": reason,
                                }
                            )
                            _log_agent_call(
                                session,
                                provider,
                                question=question,
                                started=started,
                                status="no_data",
                                journal=journal,
                            )
                            return PlannerResult(
                                None,
                                f"agent:{provider.name}",
                                warnings,
                                status="no_data",
                                clarification_question=reason,
                                tool_journal=journal,
                                semantic_query=semantic,
                                model_calls=step,
                            )
                        semantic.query_plan = plan
                    except (ValidationError, ValueError) as exc:
                        error_message = str(exc)
                        valid_plan_exists = (
                            _verified_semantic_constraints(
                                session,
                                question,
                                journal,
                                available_periods,
                                conversation_context,
                            )
                            is not None
                        )
                        correction = (
                            "Доступность данных уже подтверждена. Верни "
                            "status=ready и исправь поля, названные в ошибке; "
                            "остальные подтверждённые фильтры не меняй. "
                            "no_data и clarification недопустимы."
                            if valid_plan_exists
                            else (
                                "Исправь SemanticQuery инструментами; "
                                "no_data допустим только после отрицательной "
                                "проверки доступности, clarification — только "
                                "при неразрешимой неоднозначности."
                            )
                        )
                        journal.append(
                            {
                                "step": step,
                                "action": "validate_semantic_query",
                                "status": "rejected",
                                "error": error_message,
                            }
                        )
                        messages.extend(
                            [
                                {"role": "assistant", "content": raw},
                                {
                                    "role": "user",
                                    "content": json.dumps(
                                        {
                                            "tool": "validate_semantic_query",
                                            "valid": False,
                                            "error": error_message,
                                            "instruction": correction,
                                        },
                                        ensure_ascii=False,
                                    ),
                                },
                            ]
                        )
                        continue
                    journal.append(
                        {
                            "step": step,
                            "action": "validate_semantic_query",
                            "status": "valid",
                        }
                    )
                    _log_agent_call(
                        session,
                        provider,
                        question=question,
                        started=started,
                        status="success",
                        journal=journal,
                    )
                    return PlannerResult(
                        plan,
                        f"agent:{provider.name}",
                        warnings,
                        tool_journal=journal,
                        semantic_query=semantic,
                        model_calls=model_calls,
                    )
                allowed = {
                    "search_metric",
                    "search_territory",
                    "search_row",
                    "comparable_periods",
                    "check_availability",
                    "validate_plan",
                }
                if action_name not in allowed:
                    raise ValueError(
                        f"Недопустимое действие агента: {action_name}"
                    )
                arguments = action.get("arguments", {})
                if not isinstance(arguments, dict):
                    raise ValueError("arguments должен быть JSON-объектом")
                arguments = dict(arguments)
                if action_name == "comparable_periods":
                    resolved_territories: dict[str, str] = {}
                    provisional = _enrich_explicit_territories(
                        session,
                        question,
                        QueryPlan(),
                    )
                    for territory_name in provisional.territories:
                        resolved_territories[
                            territory_key(territory_name)
                        ] = territory_name
                    for item in journal:
                        if item.get("action") != "search_territory":
                            continue
                        for match in (item.get("result") or {}).get(
                            "matches", []
                        ):
                            territory_name = str(match.get("name", ""))
                            if territory_name:
                                resolved_territories[
                                    territory_key(territory_name)
                                ] = territory_name
                    if resolved_territories:
                        arguments["territories"] = list(
                            resolved_territories.values()
                        )
                    required_groups = (
                        _question_enterprise_group_requirements(question)
                    )
                    arguments.setdefault(
                        "enterprise_groups",
                        required_groups or ["all_msp"],
                    )
                    requested_window = _explicit_year_window(question)
                    if requested_window is not None:
                        window_kind, window_size = requested_window
                        observation_count = (
                            window_size
                            if window_kind == "past"
                            else window_size + 1
                        )
                        latest_year = (
                            max(item.year for item in available_periods)
                            if available_periods
                            else date.today().year
                        )
                        arguments["years"] = list(
                            range(
                                latest_year - observation_count + 1,
                                latest_year + 1,
                            )
                        )
                        arguments["observation_count"] = observation_count
                        arguments["window_kind"] = window_kind
                signature = json.dumps(
                    {"action": action_name, "arguments": arguments},
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                )
                if signature in seen_tool_results:
                    no_progress_steps += 1
                    cached_result = seen_tool_results[signature]
                    journal.append(
                        {
                            "step": step,
                            "action": action_name,
                            "arguments": arguments,
                            "result": cached_result,
                            "status": "repeated_no_new_information",
                            "no_progress_steps": no_progress_steps,
                        }
                    )
                    messages.extend(
                        [
                            {"role": "assistant", "content": raw},
                            {
                                "role": "user",
                                "content": json.dumps(
                                    {
                                        "tool": action_name,
                                        "result": cached_result,
                                        "repeated": True,
                                        "new_information": False,
                                        "instruction": (
                                            "Этот вызов уже выполнялся и "
                                            "не дал новой информации. Не "
                                            "повторяй его. Сначала проверь, "
                                            "соответствует ли вопрос одному "
                                            "из verified_navigation_options; "
                                            "если да, примени его и верни "
                                            "final. Иначе сформируй final, "
                                            "clarify или no_data. Новый "
                                            "инструмент допустим только для "
                                            "другого ещё не проверенного "
                                            "ограничения."
                                        ),
                                        "verified_navigation_options": (
                                            navigation_options
                                        ),
                                    },
                                    ensure_ascii=False,
                                    default=str,
                                ),
                            },
                        ]
                    )
                    continue
                result = _metadata_tool(
                    session,
                    action_name,
                    arguments,
                    available_periods,
                )
                seen_tool_results[signature] = result
                result_signature = json.dumps(
                    {"action": action_name, "result": result},
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                )
                if result_signature in seen_result_signatures:
                    no_progress_steps += 1
                    journal.append(
                        {
                            "step": step,
                            "action": action_name,
                            "arguments": arguments,
                            "result": result,
                            "status": "repeated_no_new_information",
                            "no_progress_steps": no_progress_steps,
                        }
                    )
                    messages.extend(
                        [
                            {"role": "assistant", "content": raw},
                            {
                                "role": "user",
                                "content": json.dumps(
                                    {
                                        "tool": action_name,
                                        "result": result,
                                        "repeated": True,
                                        "new_information": False,
                                        "instruction": (
                                            "Другая формулировка вызова "
                                            "вернула тот же результат. "
                                            "Не продолжай перебор. "
                                            "Проверь verified_navigation_"
                                            "options, используй "
                                            "подтверждённые сущности и "
                                            "defaults, затем верни final; "
                                            "clarify допустим только при "
                                            "реальной неоднозначности."
                                        ),
                                        "verified_navigation_options": (
                                            navigation_options
                                        ),
                                    },
                                    ensure_ascii=False,
                                    default=str,
                                ),
                            },
                        ]
                    )
                    continue
                seen_result_signatures.add(result_signature)
                no_progress_steps = 0
                journal.append(
                    {
                        "step": step,
                        "action": action_name,
                        "arguments": arguments,
                        "result": result,
                    }
                )
                messages.extend(
                    [
                        {"role": "assistant", "content": raw},
                        {
                            "role": "user",
                            "content": json.dumps(
                                {"tool": action_name, "result": result},
                                ensure_ascii=False,
                                default=str,
                            ),
                        },
                    ]
                )
            raise ValueError(
                "Сработал аварийный предохранитель: "
                f"агент не завершил план за {emergency_max_steps} "
                "шага"
            )
        except (
            LLMProviderError,
            ValidationError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            journal.append(
                {
                    "step": model_calls,
                    "action": "planner_error",
                    "status": "failed",
                    "error": str(exc),
                }
            )
            error_code = (
                exc.code
                if isinstance(exc, LLMProviderError)
                else "invalid_plan"
            )
            _log_agent_call(
                session,
                provider,
                question=question,
                started=started,
                status=(
                    "fallback" if allow_deterministic_fallback else "failed"
                ),
                journal=journal,
                error_code=error_code,
            )
            if not allow_deterministic_fallback:
                raise PlanningUnavailableError(
                    "Семантический планировщик не завершил проверенный план. "
                    f"Диагностика: {error_code}.",
                    model_calls=model_calls,
                ) from exc
            warnings.append(
                f"Агентный план не завершён ({error_code}); применён "
                "безопасный разбор типового запроса."
            )
    elif not allow_deterministic_fallback:
        raise PlanningUnavailableError(
            "Семантический провайдер не настроен или недоступен.",
            model_calls=0,
        )

    catalog_target = detect_catalog_target(effective_question)
    if catalog_target is not None:
        plan = _catalog_plan(effective_question, catalog_target)
    else:
        try:
            plan = build_query_plan(effective_question, available_periods)
        except ValueError as exc:
            if not conversation_context:
                return _clarification_for_error(
                    effective_question, exc, available_periods, session
                )
            previous = conversation_context[-1].get("plan")
            if not isinstance(previous, dict):
                return _clarification_for_error(
                    effective_question, exc, available_periods, session
                )
            plan = QueryPlan.model_validate(previous)
            normalized = normalize_text(effective_question)
            latest_year = (
                max(period.year for period in available_periods)
                if available_periods
                else date.today().year
            )
            mentioned_years = extract_years(
                effective_question, latest_year
            )
            if mentioned_years:
                previous_mode = (
                    plan.period_modes[-1]
                    if plan.period_modes
                    else "cumulative_ytd"
                )
                previous_month = (
                    plan.periods[-1].month if plan.periods else 12
                )
                plan.periods = [
                    date(
                        year,
                        previous_month,
                        monthrange(year, previous_month)[1],
                    )
                    for year in mentioned_years
                ]
                plan.period_modes = [previous_mode] * len(plan.periods)
                plan.period_labels = []
                if _is_multiyear_trend(
                    effective_question, mentioned_years
                ):
                    plan.intent = "trend"
            if "сравн" in normalized:
                plan.intent = "compare"
            if "наибольш" in normalized or "максимал" in normalized:
                plan.include_top_territory = True
                plan.group_by = ["territory"]
            if "наименьш" in normalized or "минимал" in normalized:
                plan.include_bottom_territory = True
                plan.group_by = ["territory"]
    try:
        plan = _enrich_explicit_okved(session, effective_question, plan)
        plan = _enrich_explicit_territories(
            session, effective_question, plan
        )
        plan = _apply_selected_common_slice(
            session, effective_question, plan
        )
        plan = _apply_conversation_constraints(
            effective_question, plan, conversation_context
        )
        # Conversation carry-over can replace a national multi-period window
        # with the latest inherited slice after territory resolution. Recheck
        # source-backed comparability for that final regional scope.
        plan = _apply_selected_common_slice(
            session, effective_question, plan
        )
        clarification = _semantic_clarification(
            effective_question, plan
        )
        if clarification is not None:
            clarification.warnings.extend(warnings)
            return clarification
        plan = validate_plan(session, plan, available_periods)
    except ValueError as exc:
        return _clarification_for_error(
            effective_question,
            exc,
            available_periods,
            session,
            plan,
        )
    return PlannerResult(
        plan, "deterministic", warnings, tool_journal=[]
    )
