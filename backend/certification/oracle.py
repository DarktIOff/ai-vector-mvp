from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from python_calamine import CalamineWorkbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import (
    MetricDefinition,
    MspFact,
    RowDefinition,
    SourceFile,
    Territory,
)

from .case_models import AnalyticEvaluationCase, DialogueTurn, ExpectedStatus
from .manifest import ACTIVE_SOURCE_STATUSES, CapabilityManifest, normalize


@dataclass(frozen=True)
class OracleRow:
    metric_code: str
    territory: str
    row_code: int
    enterprise_group: str
    period: str
    value: Decimal | None = None
    previous: Decimal | None = None
    difference: Decimal | None = None
    growth_rate: Decimal | None = None
    entity: str | None = None
    section_kind: str | None = None
    unit: str | None = None

    def numbers(self) -> set[Decimal]:
        return {
            item
            for item in (
                self.value,
                self.previous,
                self.difference,
                self.growth_rate,
            )
            if item is not None
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in self.__dict__.items()
            if value is not None
        }


@dataclass
class OracleResult:
    status: ExpectedStatus
    filters: dict[str, Any]
    rows: list[OracleRow] = field(default_factory=list)
    distribution: dict[str, Decimal | int | str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def numbers(self) -> set[Decimal]:
        values = {number for row in self.rows for number in row.numbers()}
        for value in self.distribution.values():
            if isinstance(value, Decimal):
                values.add(value)
        return values

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "filters": self.filters,
            "rows": [item.to_dict() for item in self.rows],
            "distribution": {
                key: str(value) if isinstance(value, Decimal) else value
                for key, value in self.distribution.items()
            },
            "warnings": self.warnings,
        }


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    text = str(value).strip().replace("\xa0", "").replace(" ", "")
    if normalize(text) in {"", "-", "x", "х"}:
        return None
    text = text.replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _integer(value: Any) -> int | None:
    number = _decimal(value)
    if number is None or number != number.to_integral_value():
        return None
    return int(number)


def _territory_match_keys(value: str) -> set[str]:
    normalized = normalize(value)
    normalized = re.sub(r"\s*\([^()]+\)\s*$", "", normalized)
    normalized = re.sub(r"\s*-\s*кузбасс\s*$", "", normalized)
    normalized = re.sub(
        r"\bавтономный\s+округ\b", "ао", normalized
    )
    normalized = re.sub(r"^(?:город\s+|г[.]?\s*)", "", normalized)
    replacements = {
        "российская федерация": "россия",
    }
    canonical = replacements.get(normalized, normalized)
    return {re.sub(r"[^0-9a-zа-я]+", "", canonical)}


class IndependentOracle:
    """Reference calculations that never call planner or analytical tools."""

    def __init__(self, session: Session, manifest: CapabilityManifest):
        self.session = session
        self.manifest = manifest
        self._metric_by_code = {
            item.semantic_code: item
            for item in session.scalars(select(MetricDefinition)).all()
        }
        self._row_by_code = {
            item.base_row_code: item
            for item in session.scalars(select(RowDefinition)).all()
        }
        self._territories = session.scalars(select(Territory)).all()
        self._regional_source_cache: dict[
            tuple[date, str, int], SourceFile | None
        ] = {}
        self._regional_cache: dict[tuple[str, str, int], list[tuple[Any, ...]]] = {}
        self._regional_semantic_column_cache: dict[
            tuple[int, str], dict[str, int]
        ] = {}

    def evaluate(
        self,
        case: AnalyticEvaluationCase,
        turn_index: int | None = None,
    ) -> OracleResult:
        index = len(case.turns) - 1 if turn_index is None else turn_index
        turn = case.turns[index]
        filters = {
            "metric_codes": turn.metric_codes,
            "territories": turn.territories,
            "row_codes": turn.row_codes,
            "enterprise_groups": turn.enterprise_groups,
            "periods": [item.end for item in turn.periods],
            "period_modes": [item.mode for item in turn.periods],
            "ranking_direction": turn.ranking_direction,
            "ranking_criterion": turn.ranking_criterion,
            "required_tools": turn.required_tools,
        }
        if turn.expected_status is not ExpectedStatus.READY:
            return OracleResult(turn.expected_status, filters)
        if not turn.metric_codes or not turn.periods:
            return OracleResult(ExpectedStatus.NO_DATA, filters)
        if "rank_entities" in turn.required_tools:
            rows = self._ranking(turn)
        elif "describe_distribution" in turn.required_tools:
            ranked = self._ranking(turn, complete=True)
            distribution = self._distribution(ranked)
            return OracleResult(
                ExpectedStatus.READY if ranked else ExpectedStatus.NO_DATA,
                filters,
                ranked,
                distribution,
            )
        elif "detect_outliers" in turn.required_tools:
            rows = self._outliers(turn)
        elif "drill_down" in turn.required_tools:
            # Dependency-selected entities are dynamic; exact ranking is
            # checked metamorphically from returned evidence instead.
            rows = []
        else:
            rows = self._value_rows(turn)
        status = ExpectedStatus.READY if rows or "drill_down" in turn.required_tools else ExpectedStatus.NO_DATA
        return OracleResult(status, filters, rows)

    def _value_rows(self, turn: DialogueTurn) -> list[OracleRow]:
        if set(turn.metric_codes) == {
            "receipts_budget_total",
            "receipts_insurance_total",
        } and len(turn.metric_codes) == 2:
            return self._combined_receipts_rows(turn)
        rows: list[OracleRow] = []
        territories = turn.territories or ["Российская Федерация"]
        row_codes = turn.row_codes or [1010]
        groups = turn.enterprise_groups or ["all_msp"]
        for metric_code in turn.metric_codes:
            metric = self._metric_by_code.get(metric_code)
            if metric is None:
                continue
            for territory in territories:
                for row_code in row_codes:
                    for group in groups:
                        for period in turn.periods:
                            value = self.value(
                                metric_code,
                                territory,
                                row_code,
                                group,
                                period.end,
                                period.mode,
                            )
                            if value is None:
                                continue
                            previous = None
                            difference = None
                            growth = None
                            if (
                                "compare_entities" in turn.required_tools
                                and len(turn.periods) == 1
                            ):
                                current_date = date.fromisoformat(period.end)
                                previous_period = date(
                                    current_date.year - 1,
                                    current_date.month,
                                    current_date.day,
                                ).isoformat()
                                previous = self.value(
                                    metric_code,
                                    territory,
                                    row_code,
                                    group,
                                    previous_period,
                                    period.mode,
                                )
                                if previous is not None:
                                    difference = value - previous
                                    growth = (
                                        difference
                                        / abs(previous)
                                        * Decimal("100")
                                        if previous != 0
                                        else None
                                    )
                            rows.append(
                                OracleRow(
                                    metric_code=metric_code,
                                    territory=territory,
                                    row_code=row_code,
                                    enterprise_group=group,
                                    period=period.end,
                                    value=value,
                                    previous=previous,
                                    difference=difference,
                                    growth_rate=growth,
                                    entity=(
                                        self._row_by_code[row_code].name_ru
                                        if row_code in self._row_by_code
                                        else territory
                                    ),
                                    unit=metric.unit,
                                )
                            )
        return rows

    def _combined_receipts_rows(
        self, turn: DialogueTurn
    ) -> list[OracleRow]:
        """Calculate budget plus insurance independently from production."""

        rows: list[OracleRow] = []
        territories = turn.territories or ["Российская Федерация"]
        row_codes = turn.row_codes or [1010]
        groups = turn.enterprise_groups or ["all_msp"]
        for territory in territories:
            for row_code in row_codes:
                for group in groups:
                    for period in turn.periods:
                        components = [
                            self.value(
                                metric_code,
                                territory,
                                row_code,
                                group,
                                period.end,
                                period.mode,
                            )
                            for metric_code in turn.metric_codes
                        ]
                        if any(value is None for value in components):
                            continue
                        value = sum(
                            (item for item in components if item is not None),
                            Decimal("0"),
                        )
                        previous = None
                        difference = None
                        growth = None
                        if (
                            "compare_entities" in turn.required_tools
                            and len(turn.periods) == 1
                        ):
                            current_date = date.fromisoformat(period.end)
                            previous_period = date(
                                current_date.year - 1,
                                current_date.month,
                                current_date.day,
                            ).isoformat()
                            previous_components = [
                                self.value(
                                    metric_code,
                                    territory,
                                    row_code,
                                    group,
                                    previous_period,
                                    period.mode,
                                )
                                for metric_code in turn.metric_codes
                            ]
                            if all(
                                item is not None
                                for item in previous_components
                            ):
                                previous = sum(
                                    (
                                        item
                                        for item in previous_components
                                        if item is not None
                                    ),
                                    Decimal("0"),
                                )
                                difference = value - previous
                                growth = (
                                    difference
                                    / abs(previous)
                                    * Decimal("100")
                                    if previous != 0
                                    else None
                                )
                        rows.append(
                            OracleRow(
                                metric_code="msp_total",
                                territory=territory,
                                row_code=row_code,
                                enterprise_group=group,
                                period=period.end,
                                value=value,
                                previous=previous,
                                difference=difference,
                                growth_rate=growth,
                                entity=(
                                    self._row_by_code[row_code].name_ru
                                    if row_code in self._row_by_code
                                    else territory
                                ),
                                unit="тыс. руб.",
                            )
                        )
        return rows

    def value(
        self,
        metric_code: str,
        territory: str,
        row_code: int,
        group: str,
        period: str,
        period_mode: str = "cumulative_ytd",
    ) -> Decimal | None:
        end = date.fromisoformat(period)
        current = self._cumulative_value(
            metric_code, territory, row_code, group, end
        )
        if current is None or period_mode != "standalone_quarter":
            return current
        quarter = (end.month - 1) // 3 + 1
        if quarter == 1:
            return current
        previous_month = (quarter - 1) * 3
        if previous_month in {3, 12}:
            previous_day = 31
        elif previous_month in {6, 9}:
            previous_day = 30
        else:  # Defensive: only quarter ends are accepted above.
            return None
        previous = self._cumulative_value(
            metric_code,
            territory,
            row_code,
            group,
            date(end.year, previous_month, previous_day),
        )
        return current - previous if previous is not None else None

    def _cumulative_value(
        self,
        metric_code: str,
        territory: str,
        row_code: int,
        group: str,
        period: date,
    ) -> Decimal | None:
        if group == "small_and_medium_without_micro":
            all_msp = self._cumulative_value(
                metric_code, territory, row_code, "all_msp", period
            )
            micro = self._cumulative_value(
                metric_code, territory, row_code, "micro", period
            )
            if all_msp is None or micro is None:
                return None
            return all_msp - micro
        territory_type = next(
            (
                item.territory_type
                for item in self._territories
                if normalize(item.name_ru) == normalize(territory)
            ),
            "region",
        )
        if territory_type == "country":
            return self._national_value(
                metric_code, row_code, group, period
            )
        return self._regional_value(
            metric_code, territory, row_code, group, period
        )

    def _national_value(
        self,
        metric_code: str,
        row_code: int,
        group: str,
        period: date,
    ) -> Decimal | None:
        section = "I" if group == "all_msp" else "II"
        statement = (
            select(MspFact.value)
            .join(MetricDefinition, MetricDefinition.id == MspFact.metric_id)
            .join(RowDefinition, RowDefinition.id == MspFact.row_definition_id)
            .join(SourceFile, SourceFile.id == MspFact.source_file_id)
            .where(
                MetricDefinition.semantic_code == metric_code,
                RowDefinition.base_row_code == row_code,
                MspFact.enterprise_group == group,
                MspFact.section == section,
                MspFact.period_end == period,
                SourceFile.status.in_(ACTIVE_SOURCE_STATUSES),
            )
            .order_by(SourceFile.id.desc())
            .limit(1)
        )
        value = self.session.scalar(statement)
        return Decimal(value) if value is not None else None

    def _regional_source(
        self, period: date, section: str, row_code: int
    ) -> SourceFile | None:
        cache_key = (period, section, row_code)
        if cache_key in self._regional_source_cache:
            return self._regional_source_cache[cache_key]
        kind = "regional_section_i" if section == "I" else "regional_section_ii"
        sources = self.session.scalars(
            select(SourceFile)
            .where(
                SourceFile.workbook_kind == kind,
                SourceFile.period_end == period,
                SourceFile.status.in_(ACTIVE_SOURCE_STATUSES),
            )
            .order_by(SourceFile.id.desc())
        ).all()
        sheet_name = str(row_code if section == "I" else row_code + 1000)
        source = next(
            (
                item
                for item in sources
                if self._regional_sheet_rows(item, sheet_name)
            ),
            None,
        )
        self._regional_source_cache[cache_key] = source
        return source

    def _regional_value(
        self,
        metric_code: str,
        territory: str,
        row_code: int,
        group: str,
        period: date,
    ) -> Decimal | None:
        metric = self._metric_by_code.get(metric_code)
        if metric is None or metric.report_column_number is None:
            return None
        section = "I" if group == "all_msp" else "II"
        source = self._regional_source(period, section, row_code)
        if source is None:
            return None
        source_row_code = row_code if section == "I" else row_code + 1000
        rows = self._regional_sheet_rows(source, str(source_row_code))
        if not rows:
            return None
        header_index, columns = self._header_columns(rows)
        semantic_columns = self._regional_semantic_columns(
            source=source,
            section=section,
            row_code=row_code,
            rows=rows,
            header_index=header_index,
            columns=columns,
        )
        physical_column = semantic_columns.get(metric_code)
        if physical_column is None:
            # Old layouts without a usable country control row retain their
            # literal numbering.  This fallback is deliberately local to the
            # oracle and never calls the production regional adapter.
            physical_column = columns.get(metric.report_column_number)
        if physical_column is None:
            return None
        first_metric = columns.get(min(columns))
        if first_metric is None:
            return None
        territory_column = max(0, first_metric - 3)
        wanted = _territory_match_keys(territory)
        for row in rows[header_index + 1 :]:
            if territory_column >= len(row):
                continue
            candidate = _territory_match_keys(str(row[territory_column] or ""))
            if not wanted.intersection(candidate):
                continue
            if physical_column >= len(row):
                return None
            return _decimal(row[physical_column])
        return None

    def _regional_semantic_columns(
        self,
        *,
        source: SourceFile,
        section: str,
        row_code: int,
        rows: list[tuple[Any, ...]],
        header_index: int,
        columns: dict[int, int],
    ) -> dict[str, int]:
        """Independently align a regional layout to audited national facts.

        Some released regional books print the headers of one template while
        their data columns follow the corresponding national form.  The
        production adapter handles that drift, but the oracle must not call
        it.  Here we reconstruct the mapping from imported, source-audited
        national lineage and accept it only when the regional RF control row
        matches at least four values with at least 90% exact agreement.
        """

        # Every audited numeric sheet in one source has the same physical
        # schema, so one independently cross-checked row establishes the
        # source-level mapping for subsequent industry rows.
        cache_key = (source.id, section)
        cached = self._regional_semantic_column_cache.get(cache_key)
        if cached is not None:
            return cached

        national_source = self.session.scalar(
            select(SourceFile)
            .where(
                SourceFile.workbook_kind == "msp_summary",
                SourceFile.period_end == source.period_end,
                SourceFile.status.in_(ACTIVE_SOURCE_STATUSES),
            )
            .order_by(SourceFile.id.desc())
            .limit(1)
        )
        if national_source is None:
            self._regional_semantic_column_cache[cache_key] = {}
            return {}

        national_rows = self.session.execute(
            select(MspFact, MetricDefinition.semantic_code)
            .join(
                MetricDefinition,
                MetricDefinition.id == MspFact.metric_id,
            )
            .join(
                RowDefinition,
                RowDefinition.id == MspFact.row_definition_id,
            )
            .where(
                MspFact.source_file_id == national_source.id,
                MspFact.section == section,
                RowDefinition.base_row_code == row_code,
            )
        ).all()
        by_source_column: dict[int, tuple[str, Decimal]] = {}
        for fact, semantic_code in national_rows:
            source_column = (fact.lineage_json or {}).get(
                "source_report_column"
            )
            if isinstance(source_column, int):
                by_source_column[source_column] = (
                    semantic_code,
                    Decimal(fact.value),
                )

        first_metric = columns.get(min(columns)) if columns else None
        territory_column = max(0, first_metric - 3) if first_metric is not None else 0
        country_row = next(
            (
                row
                for row in rows[header_index + 1 :]
                if territory_column < len(row)
                and normalize(str(row[territory_column] or ""))
                == "российская федерация"
            ),
            None,
        )
        if country_row is None:
            self._regional_semantic_column_cache[cache_key] = {}
            return {}

        comparable = 0
        matching = 0
        for source_column, physical_column in columns.items():
            expected = by_source_column.get(source_column)
            if expected is None or physical_column >= len(country_row):
                continue
            actual = _decimal(country_row[physical_column])
            if actual is None:
                continue
            comparable += 1
            if actual == expected[1]:
                matching += 1
        if comparable < 4 or Decimal(matching) / comparable < Decimal("0.9"):
            self._regional_semantic_column_cache[cache_key] = {}
            return {}

        mapping = {
            semantic_code: columns[source_column]
            for source_column, (semantic_code, _value) in by_source_column.items()
            if source_column in columns
        }
        self._regional_semantic_column_cache[cache_key] = mapping
        return mapping

    def _regional_sheet_rows(
        self, source: SourceFile, sheet_name: str
    ) -> list[tuple[Any, ...]]:
        key = (source.sha256, sheet_name, source.id)
        if key in self._regional_cache:
            return self._regional_cache[key]
        path = Path(source.absolute_path)
        with path.open("rb") as handle:
            book = CalamineWorkbook.from_filelike(handle)
            try:
                if sheet_name not in {str(item) for item in book.sheet_names}:
                    rows: list[tuple[Any, ...]] = []
                else:
                    rows = [
                        tuple(item)
                        for item in book.get_sheet_by_name(sheet_name).to_python()
                    ]
            finally:
                book.close()
        self._regional_cache[key] = rows
        return rows

    @staticmethod
    def _header_columns(
        rows: list[tuple[Any, ...]],
    ) -> tuple[int, dict[int, int]]:
        best_index = -1
        best: dict[int, int] = {}
        for index, row in enumerate(rows[:30]):
            mapping = {
                number: column
                for column, value in enumerate(row[:100])
                if (number := _integer(value)) is not None
                and 1 <= number <= 100
            }
            if len(mapping) > len(best) and all(
                item in mapping for item in range(1, min(5, len(mapping)) + 1)
            ):
                best_index, best = index, mapping
        return best_index, best

    def _ranking(
        self,
        turn: DialogueTurn,
        *,
        complete: bool = False,
        raw: bool = False,
    ) -> list[OracleRow]:
        metric_code = turn.metric_codes[0]
        current_period = turn.periods[-1].end
        current_date = date.fromisoformat(current_period)
        previous_period = date(
            current_date.year - 1,
            current_date.month,
            current_date.day,
        ).isoformat()
        group = (turn.enterprise_groups or ["all_msp"])[0]
        criterion = turn.ranking_criterion or "growth_rate"
        dimension = (
            "industry"
            if "industry_rank" in turn.required_analyses
            else "territory"
            if "regional_rank" in turn.required_analyses
            or "detect_outliers" in turn.required_tools
            else "territory"
            if not turn.territories
            else "industry"
        )
        candidates: Iterable[tuple[str, str, int]]
        if dimension == "territory":
            candidates = (
                (name, name, (turn.row_codes or [1010])[0])
                for name in self._regional_territory_names(
                    current_date,
                    "II" if group == "micro" else "I",
                )
            )
        else:
            territory = (turn.territories or ["Российская Федерация"])[0]
            candidates = (
                (item.name, territory, item.code)
                for item in self.manifest.rows
                if item.code != 1010
            )
        national_industry_values = (
            self._national_industry_values(
                metric_code,
                group,
                current_date,
                date.fromisoformat(previous_period),
            )
            if dimension == "industry"
            and normalize(territory) == normalize("Российская Федерация")
            else {}
        )
        rows: list[OracleRow] = []
        for entity, territory, row_code in candidates:
            if national_industry_values:
                current = national_industry_values.get(
                    (row_code, current_date)
                )
                previous = national_industry_values.get(
                    (row_code, date.fromisoformat(previous_period))
                )
            else:
                current = self.value(
                    metric_code, territory, row_code, group, current_period
                )
                previous = self.value(
                    metric_code, territory, row_code, group, previous_period
                )
            if current is None or previous is None:
                continue
            difference = current - previous
            growth = (
                difference / abs(previous) * Decimal("100")
                if previous > 0
                else None
            )
            rows.append(
                OracleRow(
                    metric_code=metric_code,
                    territory=territory,
                    row_code=row_code,
                    enterprise_group=group,
                    period=current_period,
                    value=current,
                    previous=previous,
                    difference=difference,
                    growth_rate=growth,
                    entity=entity,
                    unit=self._metric_by_code[metric_code].unit,
                )
            )
        if raw:
            return rows
        key = {
            "value": lambda item: item.value,
            "difference": lambda item: item.difference,
            "growth_rate": lambda item: item.growth_rate,
        }[criterion]
        if criterion == "growth_rate":
            positive_bases = sorted(
                item.previous
                for item in rows
                if item.previous is not None and item.previous > 0
            )
            if positive_bases:
                floor_index = int(
                    (len(positive_bases) - 1) * Decimal("0.10")
                )
                base_floor = positive_bases[floor_index]
                rows = [
                    item
                    for item in rows
                    if item.previous is not None
                    and item.previous >= base_floor
                ]
        ordered = sorted(
            (item for item in rows if key(item) is not None),
            # Candidate rows are already in canonical row-code/catalog order.
            # Keep that order for equal values, exactly as a stable analytical
            # ranking does.  A label-based tie-break would select a different
            # member of a bounded -100% tie while both calculations are equal.
            key=key,
            reverse=True,
        )
        if complete:
            return ordered
        direction = turn.ranking_direction or "both"
        limit = 5
        if direction == "top":
            return ordered[:limit]
        if direction == "bottom":
            return list(reversed(ordered[-limit:]))
        return [
            *(
                item.__class__(
                    **{**item.__dict__, "section_kind": "ranking_growth"}
                )
                for item in ordered[:3]
            ),
            *(
                item.__class__(
                    **{**item.__dict__, "section_kind": "ranking_growth_worst"}
                )
                for item in reversed(ordered[-3:])
            ),
        ]

    def _regional_territory_names(
        self, period: date, section: str
    ) -> list[str]:
        """Read source-backed region names without the production adapter."""

        source = self._regional_source(period, section, 1010)
        if source is None:
            return []
        sheet_name = "1010" if section == "I" else "2010"
        rows = self._regional_sheet_rows(source, sheet_name)
        if not rows:
            return []
        header_index, columns = self._header_columns(rows)
        first_metric = columns.get(min(columns)) if columns else None
        if first_metric is None:
            return []
        territory_column = max(0, first_metric - 3)
        names: set[str] = set()
        for row in rows[header_index + 1 :]:
            if territory_column >= len(row):
                continue
            name = str(row[territory_column] or "").strip()
            normalized = normalize(name)
            if (
                not normalized
                or normalized == "российская федерация"
                or "федеральный округ" in normalized
                or normalized in {"в том числе:", "юго-западные субъекты"}
            ):
                continue
            names.add(name)
        return sorted(names)

    @staticmethod
    def _robust_z(values: dict[str, float]) -> dict[str, float] | None:
        if len(values) < 10:
            return None
        raw = list(values.values())
        median = statistics.median(raw)
        deviations = [abs(value - median) for value in raw]
        scale = 1.4826 * statistics.median(deviations)
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

    @staticmethod
    def _shares(values: list[Decimal]) -> list[float] | None:
        safe = [max(Decimal("0"), item) for item in values]
        total = sum(safe, Decimal("0"))
        if total <= 0:
            return None
        return [float(item / total) for item in safe]

    def _structure_vector(
        self,
        period: str,
        territory: str,
        group: str,
    ) -> list[float] | None:
        components: list[Decimal] = []
        for metric_code in (
            "receipts_profit_tax_total",
            "receipts_ndfl_total",
            "receipts_vat_total",
            "receipts_special_regimes_total",
        ):
            value = self.value(
                metric_code,
                territory,
                1010,
                group,
                period,
            )
            if value is None:
                return None
            components.append(value)
        budget = self.value(
            "receipts_budget_total",
            territory,
            1010,
            group,
            period,
        )
        if budget is None:
            return None
        residual = max(Decimal("0"), budget - sum(components, Decimal("0")))
        return self._shares([*components, residual])

    @staticmethod
    def _js_divergence(first: list[float], second: list[float]) -> float:
        middle = [(left + right) / 2 for left, right in zip(first, second)]

        def divergence(values: list[float], reference: list[float]) -> float:
            return sum(
                value * math.log(value / base)
                for value, base in zip(values, reference)
                if value > 0 and base > 0
            )

        return (
            divergence(first, middle) + divergence(second, middle)
        ) / 2

    @staticmethod
    def _with_section(row: OracleRow, section_kind: str) -> OracleRow:
        return row.__class__(
            **{**row.__dict__, "section_kind": section_kind}
        )

    def _outliers(self, turn: DialogueTurn) -> list[OracleRow]:
        """Independently select the bounded level/growth/composite outliers."""

        population = self._ranking(turn, raw=True)
        by_entity = {str(item.entity): item for item in population}
        current_values = {
            name: math.log1p(max(0.0, float(item.value)))
            for name, item in by_entity.items()
            if item.value is not None
        }
        growth_values = {
            name: math.log(float(item.value / item.previous))
            for name, item in by_entity.items()
            if item.value is not None
            and item.previous is not None
            and item.value > 0
            and item.previous > 0
        }
        z_value = self._robust_z(current_values)
        z_growth = self._robust_z(growth_values)
        selected: list[OracleRow] = []
        if z_value is not None:
            ordered = sorted(z_value, key=lambda name: z_value[name])
            selected.extend(
                self._with_section(by_entity[name], "outliers_value")
                for name in [*ordered[:5], *reversed(ordered[-5:])]
            )
        if z_growth is not None:
            ordered = sorted(z_growth, key=lambda name: z_growth[name])
            selected.extend(
                self._with_section(by_entity[name], "outliers_growth")
                for name in [*ordered[:5], *reversed(ordered[-5:])]
            )

        period = turn.periods[-1].end
        group = (turn.enterprise_groups or ["all_msp"])[0]
        rf_structure = self._structure_vector(
            period, "Российская Федерация", group
        )
        structure_values: dict[str, float] = {}
        if rf_structure is not None:
            for name in by_entity:
                vector = self._structure_vector(period, name, group)
                if vector is not None:
                    structure_values[name] = self._js_divergence(
                        vector, rf_structure
                    )
        z_structure = self._robust_z(structure_values)
        if (
            z_value is not None
            and z_growth is not None
            and z_structure is not None
        ):
            common = set(z_value) & set(z_growth) & set(z_structure)
            scores = {
                name: math.sqrt(
                    (
                        z_value[name] ** 2
                        + z_growth[name] ** 2
                        + z_structure[name] ** 2
                    )
                    / 3
                )
                for name in common
            }
            selected.extend(
                self._with_section(by_entity[name], "outliers_composite")
                for name in sorted(
                    scores,
                    key=lambda item: scores[item],
                    reverse=True,
                )[:5]
            )
        return selected

    def _national_industry_values(
        self,
        metric_code: str,
        group: str,
        current_period: date,
        previous_period: date,
    ) -> dict[tuple[int, date], Decimal]:
        """Read both national ranking periods in one independent SQL pass."""

        sections = (
            ["I", "II"]
            if group == "small_and_medium_without_micro"
            else ["II"]
            if group == "micro"
            else ["I"]
        )
        rows = self.session.execute(
            select(
                RowDefinition.base_row_code,
                MspFact.period_end,
                MspFact.section,
                MspFact.value,
                SourceFile.id,
            )
            .join(MetricDefinition, MetricDefinition.id == MspFact.metric_id)
            .join(RowDefinition, RowDefinition.id == MspFact.row_definition_id)
            .join(SourceFile, SourceFile.id == MspFact.source_file_id)
            .where(
                MetricDefinition.semantic_code == metric_code,
                RowDefinition.base_row_code != 1010,
                MspFact.period_end.in_([previous_period, current_period]),
                MspFact.section.in_(sections),
                SourceFile.status.in_(ACTIVE_SOURCE_STATUSES),
            )
            .order_by(SourceFile.id.desc())
        ).all()
        latest: dict[tuple[int, date, str], Decimal] = {}
        for row_code, period, section, value, _source_id in rows:
            latest.setdefault(
                (int(row_code), period, str(section)),
                Decimal(value),
            )
        result: dict[tuple[int, date], Decimal] = {}
        keys = {(row_code, period) for row_code, period, _ in latest}
        for row_code, period in keys:
            if group == "small_and_medium_without_micro":
                all_msp = latest.get((row_code, period, "I"))
                micro = latest.get((row_code, period, "II"))
                if all_msp is not None and micro is not None:
                    result[(row_code, period)] = all_msp - micro
            else:
                section = sections[0]
                value = latest.get((row_code, period, section))
                if value is not None:
                    result[(row_code, period)] = value
        return result

    @staticmethod
    def _distribution(rows: list[OracleRow]) -> dict[str, Decimal | int | str]:
        values = sorted(
            item.growth_rate
            for item in rows
            if item.growth_rate is not None
        )
        if not values:
            return {}

        def quantile(fraction: Decimal) -> Decimal:
            index = int((Decimal(len(values) - 1) * fraction).to_integral_value())
            return values[index]

        middle = len(values) // 2
        median = (
            values[middle]
            if len(values) % 2
            else (values[middle - 1] + values[middle]) / 2
        )
        return {
            "count": len(values),
            "minimum": values[0],
            "q1": quantile(Decimal("0.25")),
            "median": median,
            "mean": sum(values, Decimal("0")) / len(values),
            "q3": quantile(Decimal("0.75")),
            "maximum": values[-1],
            "measure": "growth_rate",
        }


def exact_decimal_equal(left: Any, right: Any) -> bool:
    left_value, right_value = _decimal(left), _decimal(right)
    return left_value is not None and right_value is not None and left_value == right_value
