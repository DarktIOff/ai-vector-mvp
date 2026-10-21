#!/usr/bin/env python3
"""Audit every imported 1-MSP value against its semantic source column.

The audit has two independent parts:

* national: compare every active database fact with the exact workbook cell
  selected through the normalized header;
* regional: resolve every sheet header, parse every territory/metric cell and
  compare each country control value with the corresponding national fact.

Explicit source markers such as ``X`` are counted as unavailable values and
are not treated as numeric data.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from python_calamine import CalamineWorkbook
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.catalog import resolve_header_metrics
from backend.app.database import SessionLocal
from backend.app.models import (
    MetricDefinition,
    MspFact,
    RowDefinition,
    SourceFile,
)
from backend.app.regional import (
    _layout,
    resolve_regional_metrics,
    territory_type,
)
from backend.app.workbooks import (
    decimal_value,
    find_sheet,
    normalize_text,
    open_reader,
    recognize_header,
)


ACTIVE_STATUSES = ("valid", "valid_with_warnings")
EMPTY_MARKERS = {"", "-", "–", "—"}
UNAVAILABLE_MARKERS = {"x", "х"}


@dataclass
class AuditResult:
    counts: Counter
    issues: Counter
    samples: list[dict[str, Any]]

    @classmethod
    def create(cls) -> "AuditResult":
        return cls(Counter(), Counter(), [])

    def issue(self, kind: str, **context: Any) -> None:
        self.issues[kind] += 1
        if len(self.samples) < 25:
            self.samples.append({"kind": kind, **context})

    def as_dict(self) -> dict[str, Any]:
        return {
            "counts": dict(self.counts),
            "issues": dict(self.issues),
            "samples": self.samples,
        }


def _canonical_row_code(source_code: int, section: str) -> int | None:
    if 1000 <= source_code < 2000:
        return source_code
    if section == "II" and 2000 <= source_code < 3000:
        return source_code - 1000
    return None


def audit_national(*, include_all_imported: bool = False) -> AuditResult:
    result = AuditResult.create()
    with SessionLocal() as session:
        statement = select(SourceFile).where(
            SourceFile.workbook_kind == "msp_summary"
        )
        if include_all_imported:
            # Superseded inventory records can point at a path subsequently
            # replaced by a corrected workbook and contain no imported facts.
            # The exhaustive audit follows every fact-bearing source exactly
            # once, rather than auditing stale inventory metadata as data.
            statement = statement.where(SourceFile.facts.any())
        else:
            statement = statement.where(
                SourceFile.status.in_(ACTIVE_STATUSES)
            )
        sources = session.scalars(
            statement.order_by(SourceFile.period_end, SourceFile.id)
        ).all()
        result.counts["source_files"] = len(sources)
        for source in sources:
            path = Path(source.absolute_path)
            reader = open_reader(path)
            try:
                expected: dict[
                    tuple[str, int, str],
                    tuple[Any, str, int, str],
                ] = {}
                for section, wanted in (
                    ("I", "Раздел I"),
                    ("II", "Раздел II"),
                ):
                    sheet_name = find_sheet(reader, wanted)
                    if sheet_name is None:
                        result.issue(
                            "missing_section",
                            file=path.name,
                            section=section,
                        )
                        continue
                    sheet = reader.sheet(sheet_name)
                    schema = recognize_header(sheet)
                    resolution = resolve_header_metrics(schema.raw_headers)
                    result.counts["headers_checked"] += len(
                        schema.raw_headers
                    )
                    if not resolution.is_complete:
                        result.issue(
                            "semantic_mapping",
                            file=path.name,
                            section=section,
                            unmapped=sorted(resolution.unmapped),
                            duplicates=resolution.duplicate_codes,
                        )
                        continue
                    for row_number in range(
                        schema.header_number_row + 1,
                        sheet.max_row + 1,
                    ):
                        raw_code = decimal_value(
                            sheet.value(row_number, schema.code_column)
                        )
                        if (
                            raw_code is None
                            or raw_code != raw_code.to_integral_value()
                        ):
                            continue
                        source_code = int(raw_code)
                        base_code = _canonical_row_code(
                            source_code,
                            section,
                        )
                        if base_code is None:
                            continue
                        result.counts["industry_rows"] += 1
                        for source_column, physical_column in (
                            schema.metric_columns.items()
                        ):
                            value = decimal_value(
                                sheet.value(row_number, physical_column)
                            )
                            if value is None:
                                continue
                            semantic_code = (
                                resolution.source_to_code[source_column]
                            )
                            key = (section, base_code, semantic_code)
                            expected[key] = (
                                value,
                                sheet.coordinate(
                                    row_number,
                                    physical_column,
                                ),
                                source_column,
                                schema.raw_headers[source_column],
                            )
                            result.counts["numeric_cells"] += 1

                facts = session.execute(
                    select(
                        MspFact,
                        RowDefinition.base_row_code,
                        MetricDefinition.semantic_code,
                    )
                    .join(
                        RowDefinition,
                        RowDefinition.id == MspFact.row_definition_id,
                    )
                    .join(
                        MetricDefinition,
                        MetricDefinition.id == MspFact.metric_id,
                    )
                    .where(MspFact.source_file_id == source.id)
                ).all()
                actual = {
                    (fact.section, base_code, semantic_code): fact
                    for fact, base_code, semantic_code in facts
                }
                result.counts["database_facts"] += len(actual)
                for key, (
                    expected_value,
                    expected_cell,
                    source_column,
                    source_header,
                ) in expected.items():
                    fact = actual.get(key)
                    if fact is None:
                        result.issue(
                            "missing_database_fact",
                            file=path.name,
                            key=key,
                            cell=expected_cell,
                        )
                        continue
                    lineage = fact.lineage_json or {}
                    if (
                        fact.value != expected_value
                        or fact.source_cell != expected_cell
                        or lineage.get("source_report_column")
                        != source_column
                        or lineage.get("source_header") != source_header
                        or lineage.get("mapping_method")
                        != "normalized_header"
                    ):
                        result.issue(
                            "database_source_mismatch",
                            file=path.name,
                            key=key,
                            expected_value=str(expected_value),
                            actual_value=str(fact.value),
                            expected_cell=expected_cell,
                            actual_cell=fact.source_cell,
                        )
                for key in actual.keys() - expected.keys():
                    result.issue(
                        "orphan_database_fact",
                        file=path.name,
                        key=key,
                    )
            finally:
                reader.close()
    return result


def _number(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _regional_layout(
    rows: list[tuple[Any, ...]],
) -> tuple[int, dict[int, int], dict[int, str]] | None:
    best_row = -1
    best_mapping: dict[int, int] = {}
    for row_index, row in enumerate(rows[:30]):
        mapping = {
            number: column + 1
            for column, value in enumerate(row[:80])
            if (number := _number(value)) is not None
            and 1 <= number <= 100
        }
        if len(mapping) > len(best_mapping) and all(
            number in mapping
            for number in range(1, min(5, len(mapping)) + 1)
        ):
            best_row, best_mapping = row_index, mapping
    if len(best_mapping) < 4:
        return None
    raw_headers: dict[int, str] = {}
    for source_column, physical_column in sorted(
        best_mapping.items()
    ):
        parts: list[str] = []
        for header_index in range(max(0, best_row - 6), best_row):
            row = rows[header_index]
            value = (
                row[physical_column - 1]
                if len(row) >= physical_column
                else None
            )
            normalized = normalize_text(value)
            if normalized and normalized not in parts:
                parts.append(normalized)
        raw_headers[source_column] = " → ".join(parts)
    return best_row, best_mapping, raw_headers


def _national_values(
    session: Any,
    period: Any,
    section: str,
) -> dict[tuple[int, str], Any]:
    rows = session.execute(
        select(
            RowDefinition.base_row_code,
            MetricDefinition.semantic_code,
            MspFact.value,
        )
        .join(
            MspFact,
            MspFact.row_definition_id == RowDefinition.id,
        )
        .join(
            MetricDefinition,
            MetricDefinition.id == MspFact.metric_id,
        )
        .join(
            SourceFile,
            SourceFile.id == MspFact.source_file_id,
        )
        .where(
            SourceFile.workbook_kind == "msp_summary",
            SourceFile.status.in_(ACTIVE_STATUSES),
            MspFact.period_end == period,
            MspFact.section == section,
        )
    ).all()
    return {
        (base_code, semantic_code): value
        for base_code, semantic_code, value in rows
    }


def audit_regional() -> AuditResult:
    result = AuditResult.create()
    region_names: set[str] = set()
    industry_codes: set[int] = set()
    seen_sheets: set[tuple[Any, str, int]] = set()
    with SessionLocal() as session:
        sources = session.scalars(
            select(SourceFile)
            .where(
                SourceFile.workbook_kind.like("regional_%"),
                SourceFile.status.in_(ACTIVE_STATUSES),
            )
            .order_by(
                SourceFile.period_end,
                SourceFile.workbook_kind,
                SourceFile.id,
            )
        ).all()
        result.counts["source_files"] = len(sources)
        cached_key: tuple[Any, str] | None = None
        national: dict[tuple[int, str], Any] = {}
        for source in sources:
            section = (
                "I"
                if source.workbook_kind == "regional_section_i"
                else "II"
            )
            cache_key = (source.period_end, section)
            if cache_key != cached_key:
                national = _national_values(
                    session,
                    source.period_end,
                    section,
                )
                cached_key = cache_key
            path = Path(source.absolute_path)
            with path.open("rb") as handle:
                book = CalamineWorkbook.from_filelike(handle)
                try:
                    numeric_names = [
                        str(name)
                        for name in book.sheet_names
                        if str(name).isdigit()
                    ]
                    if not numeric_names:
                        result.issue(
                            "missing_industry_sheets",
                            file=path.name,
                        )
                        continue
                    first_name = numeric_names[0]
                    first_code = int(first_name)
                    first_base = _canonical_row_code(
                        first_code,
                        section,
                    )
                    if first_base is None:
                        result.issue(
                            "invalid_first_sheet_code",
                            file=path.name,
                            sheet=first_name,
                        )
                        continue
                    application_layout, _ = _layout(
                        path,
                        sheet_name=first_name,
                        fallback_as_of=source.as_of_date,
                    )
                    effective = resolve_regional_metrics(
                        session,
                        source=source,
                        layout=application_layout,
                        section=section,
                        row_code=first_base,
                    )
                    result.counts[
                        f"mapping_{effective.mapping_method}"
                    ] += 1
                    result.counts["header_conflicts_corrected"] += len(
                        effective.header_conflicts
                    )

                    for sheet_name in numeric_names:
                        source_code = int(sheet_name)
                        base_code = _canonical_row_code(
                            source_code,
                            section,
                        )
                        if base_code is None:
                            result.issue(
                                "invalid_sheet_code",
                                file=path.name,
                                sheet=sheet_name,
                            )
                            continue
                        sheet_key = (
                            source.period_end,
                            section,
                            base_code,
                        )
                        if sheet_key in seen_sheets:
                            result.issue(
                                "duplicate_industry_sheet",
                                file=path.name,
                                sheet=sheet_name,
                                period=str(source.period_end),
                                section=section,
                            )
                        seen_sheets.add(sheet_key)
                        industry_codes.add(base_code)
                        result.counts["industry_sheets"] += 1

                        rows = [
                            tuple(row)
                            for row in book.get_sheet_by_name(
                                sheet_name
                            ).to_python()
                        ]
                        parsed = _regional_layout(rows)
                        if parsed is None:
                            result.issue(
                                "missing_header",
                                file=path.name,
                                sheet=sheet_name,
                            )
                            continue
                        header_row, metric_columns, raw_headers = parsed
                        local = resolve_header_metrics(raw_headers)
                        result.counts["headers_checked"] += len(
                            raw_headers
                        )
                        if not local.is_complete:
                            result.issue(
                                "semantic_mapping",
                                file=path.name,
                                sheet=sheet_name,
                                unmapped=sorted(local.unmapped),
                                duplicates=local.duplicate_codes,
                            )
                            continue
                        if set(metric_columns) != set(
                            effective.resolution.source_to_code
                        ):
                            result.issue(
                                "inconsistent_sheet_columns",
                                file=path.name,
                                sheet=sheet_name,
                            )
                            continue
                        territory_column = max(
                            1,
                            metric_columns[min(metric_columns)] - 3,
                        )
                        country_seen = False
                        for row_number, row in enumerate(
                            rows[header_row + 1 :],
                            start=header_row + 2,
                        ):
                            if len(row) < territory_column:
                                continue
                            territory = str(
                                row[territory_column - 1] or ""
                            ).strip()
                            kind = territory_type(territory)
                            if kind is None:
                                continue
                            result.counts["territory_rows"] += 1
                            if kind == "region":
                                region_names.add(normalize_text(territory))
                                result.counts["region_rows"] += 1
                            is_country = kind == "country"
                            country_seen = country_seen or is_country
                            for source_column, semantic_code in (
                                effective.resolution.source_to_code.items()
                            ):
                                physical_column = metric_columns[
                                    source_column
                                ]
                                if len(row) < physical_column:
                                    continue
                                raw_value = row[physical_column - 1]
                                if raw_value is None:
                                    continue
                                if isinstance(raw_value, str):
                                    marker = normalize_text(raw_value)
                                    if marker in EMPTY_MARKERS:
                                        continue
                                    if marker in UNAVAILABLE_MARKERS:
                                        result.counts[
                                            "unavailable_source_cells"
                                        ] += 1
                                        continue
                                value = decimal_value(raw_value)
                                if value is None:
                                    result.issue(
                                        "nonnumeric_cell",
                                        file=path.name,
                                        sheet=sheet_name,
                                        row=row_number,
                                        column=physical_column,
                                        value=str(raw_value)[:80],
                                    )
                                    continue
                                result.counts["numeric_cells"] += 1
                                if not is_country:
                                    continue
                                result.counts[
                                    "country_values_checked"
                                ] += 1
                                expected = national.get(
                                    (base_code, semantic_code)
                                )
                                if expected is None:
                                    result.issue(
                                        "country_missing_national",
                                        file=path.name,
                                        period=str(source.period_end),
                                        section=section,
                                        sheet=sheet_name,
                                        semantic_code=semantic_code,
                                    )
                                elif value != expected:
                                    result.issue(
                                        "country_value_mismatch",
                                        file=path.name,
                                        period=str(source.period_end),
                                        section=section,
                                        sheet=sheet_name,
                                        semantic_code=semantic_code,
                                        regional=str(value),
                                        national=str(expected),
                                    )
                        if not country_seen:
                            result.issue(
                                "missing_country_row",
                                file=path.name,
                                sheet=sheet_name,
                            )
                finally:
                    book.close()
    result.counts["distinct_regions"] = len(region_names)
    result.counts["distinct_industry_codes"] = len(industry_codes)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scope",
        choices=("all", "national", "regional"),
        default="all",
    )
    args = parser.parse_args()
    output: dict[str, Any] = {}
    if args.scope in {"all", "national"}:
        output["national"] = audit_national().as_dict()
    if args.scope in {"all", "regional"}:
        output["regional"] = audit_regional().as_dict()
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return int(
        any(section["issues"] for section in output.values())
    )


if __name__ == "__main__":
    raise SystemExit(main())
