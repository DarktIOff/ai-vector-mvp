from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .catalog import (
    HeaderMetricResolution,
    resolve_header_metrics,
    seed_catalog,
)
from .models import (
    ImportBatch,
    MetricDefinition,
    MspFact,
    RowDefinition,
    SchemaMetricMapping,
    SchemaVersion,
    SourceFile,
    Territory,
    ValidationResult,
)
from .regional import inspect_regional_workbook, register_regional_file
from .workbooks import (
    HeaderSchema,
    classify_workbook,
    decimal_value,
    extract_as_of_date,
    find_sheet,
    normalize_text,
    open_reader,
    period_from_as_of,
    recognize_header,
    sha256_file,
    source_files,
)

APPROVED_NUMERIC_COUNTS = {26, 27, 32, 43, 44}


@dataclass
class ScanItem:
    path: str
    name: str
    format: str
    size_bytes: int
    workbook_kind: str
    as_of_date: str | None
    numeric_columns: int | None
    status: str
    warnings: list[str]


def scan_root(root: Path) -> dict:
    items: list[ScanItem] = []
    for path in source_files(root):
        warnings: list[str] = []
        try:
            kind = classify_workbook(path, [])
            as_of = None
            numeric_columns = None
            semantic_mapping_ready = False
            if kind in {"regional_section_i", "regional_section_ii"}:
                if path.suffix.casefold() == ".xls":
                    layout = inspect_regional_workbook(path)
                    as_of = layout.as_of_date
                    numeric_columns = layout.numeric_count
                warnings.append(
                    "Поддерживается потоковым территориальным адаптером; "
                    "факты читаются по выбранной строке без массового "
                    "дублирования книги в SQLite."
                )
                status = "ready"
            elif kind in {"analytical_auxiliary", "service_file"}:
                status = "ignored"
            else:
                reader = open_reader(path)
                try:
                    kind = classify_workbook(path, reader.sheet_names)
                    as_of, date_warnings = extract_as_of_date(reader)
                    warnings.extend(
                        date_warnings if kind == "msp_summary" else []
                    )
                    if kind == "msp_summary":
                        section_name = find_sheet(
                            reader, "Раздел I"
                        ) or find_sheet(reader, "Раздел 1")
                        if section_name:
                            header_schema = recognize_header(
                                reader.sheet(section_name)
                            )
                            numeric_columns = header_schema.numeric_count
                            resolution = resolve_header_metrics(
                                header_schema.raw_headers
                            )
                            semantic_mapping_ready = resolution.is_complete
                            if not semantic_mapping_ready:
                                warnings.append(
                                    "Не все заголовки граф однозначно "
                                    "сопоставлены с показателями."
                                )
                            if numeric_columns not in APPROVED_NUMERIC_COUNTS:
                                warnings.append(
                                    "Неизвестная схема граф; требуется "
                                    "утверждение сопоставления."
                                )
                        else:
                            warnings.append("Не найден сводный лист Раздела I")
                    status = (
                        "ready"
                        if kind == "msp_summary"
                        and numeric_columns in APPROVED_NUMERIC_COUNTS
                        and semantic_mapping_ready
                        else "ignored"
                        if kind
                        in {
                            "controller",
                            "codes_dictionary",
                            "national_receipts",
                            "analytical_auxiliary",
                            "service_file",
                        }
                        else "quarantine"
                    )
                finally:
                    reader.close()
        except Exception as exc:
            kind = (
                kind
                if kind in {"regional_section_i", "regional_section_ii"}
                else "unknown"
            )
            as_of = None
            numeric_columns = None
            status = "quarantine"
            warnings.append(str(exc))
        items.append(
            ScanItem(
                path=str(path.resolve()),
                name=path.name,
                format=path.suffix.casefold().lstrip("."),
                size_bytes=path.stat().st_size,
                workbook_kind=kind,
                as_of_date=as_of.isoformat() if as_of else None,
                numeric_columns=numeric_columns,
                status=status,
                warnings=warnings,
            )
        )
    return {
        "root": str(root),
        "files_found": len(items),
        "ready": sum(item.status == "ready" for item in items),
        "quarantine": sum(item.status == "quarantine" for item in items),
        "ignored": sum(item.status == "ignored" for item in items),
        "items": [asdict(item) for item in items],
    }


def _metric_map(session: Session) -> dict[str, MetricDefinition]:
    return {
        metric.semantic_code: metric
        for metric in session.scalars(select(MetricDefinition)).all()
    }


def _rf(session: Session) -> Territory:
    territory = session.scalar(
        select(Territory).where(Territory.normalized_name == "российская федерация")
    )
    if territory is None:
        raise RuntimeError("Каталог территорий не инициализирован")
    return territory


def _schema_version(session: Session, schema: HeaderSchema) -> SchemaVersion:
    resolution = resolve_header_metrics(schema.raw_headers)
    approved = (
        schema.numeric_count in APPROVED_NUMERIC_COUNTS
        and resolution.is_complete
    )
    version = session.scalar(
        select(SchemaVersion).where(SchemaVersion.fingerprint == schema.fingerprint)
    )
    if version is None:
        version = SchemaVersion(
            fingerprint=schema.fingerprint,
            form_name="1-МСП",
            edition_text=f"Автоматически распознанная схема: {schema.numeric_count} граф",
            numeric_column_count=schema.numeric_count,
            header_hash=schema.header_hash,
            status="approved" if approved else "review",
            approved_at=datetime.utcnow() if approved else None,
        )
        session.add(version)
        session.flush()
    else:
        version.numeric_column_count = schema.numeric_count
        version.header_hash = schema.header_hash
        version.status = "approved" if approved else "review"
        version.approved_at = datetime.utcnow() if approved else None
    session.execute(
        delete(SchemaMetricMapping).where(
            SchemaMetricMapping.schema_version_id == version.id
        )
    )
    metric_map = _metric_map(session)
    for source_column, physical_column in schema.metric_columns.items():
        raw = schema.raw_headers.get(source_column, "")
        metric_code = resolution.source_to_code.get(source_column)
        metric = metric_map.get(metric_code or "")
        session.add(
            SchemaMetricMapping(
                schema_version_id=version.id,
                report_column_number=source_column,
                physical_column=physical_column,
                metric_id=metric.id if metric is not None else None,
                raw_header=raw,
                normalized_header=normalize_text(raw),
                confidence=Decimal("1") if metric is not None else Decimal("0"),
                approved=metric is not None,
            )
        )
    return version


def _row_definition(
    session: Session, base_code: int, name: str, okved: str | None, year: int
) -> RowDefinition:
    row = session.scalar(
        select(RowDefinition).where(RowDefinition.base_row_code == base_code)
    )
    aggregation = None
    formula_match = re.search(r"стр\\.\\s*\\d{4}\\s*[=>]+\\s*([^)]*)", name, re.I)
    if formula_match:
        aggregation = formula_match.group(1).strip()
    if row is None:
        row = RowDefinition(
            base_row_code=base_code,
            name_ru=name,
            okved_text=okved,
            hierarchy_level=0 if base_code == 1010 else 1,
            aggregation_expression=aggregation,
            valid_from=datetime(year, 1, 1).date(),
        )
        session.add(row)
        session.flush()
    else:
        if len(name) > len(row.name_ru):
            row.name_ru = name
        if okved and not row.okved_text:
            row.okved_text = okved
        if aggregation and not row.aggregation_expression:
            row.aggregation_expression = aggregation
    return row


def _validation(
    session: Session,
    source_id: int,
    code: str,
    severity: str,
    message: str,
    context: dict | None = None,
) -> None:
    session.add(
        ValidationResult(
            source_file_id=source_id,
            check_code=code,
            severity=severity,
            status="failed" if severity == "error" else "warning",
            message_ru=message,
            context_json=context or {},
        )
    )


def _validate_control_totals(
    session: Session,
    source: SourceFile,
    fact_lookup: dict[tuple[str, int, str], Decimal],
    section_schemas: dict[
        str,
        tuple[HeaderSchema, HeaderMetricResolution],
    ],
) -> int:
    def formula_components(
        schema: HeaderSchema,
        resolution: HeaderMetricResolution,
        target_code: str,
    ) -> list[str]:
        source_column = next(
            (
                column
                for column, code in resolution.source_to_code.items()
                if code == target_code
            ),
            None,
        )
        if source_column is None:
            return []
        raw = schema.raw_headers.get(source_column, "")
        if "=" not in raw:
            return []
        referenced = [
            int(item)
            for item in re.findall(
                r"гр[.]?\s*(\d+)",
                raw.split("=", 1)[1],
            )
        ]
        return [
            resolution.source_to_code[item]
            for item in dict.fromkeys(referenced)
            if item in resolution.source_to_code
        ]

    warnings = 0
    for section in ("I", "II"):
        schema, resolution = section_schemas[section]
        total = fact_lookup.get(
            (section, 1010, "receipts_budget_total")
        )
        budget_codes = formula_components(
            schema,
            resolution,
            "receipts_budget_total",
        )
        components = [
            fact_lookup.get((section, 1010, code))
            for code in budget_codes
        ]
        if (
            total is not None
            and components
            and all(value is not None for value in components)
        ):
            difference = total - sum(components, Decimal("0"))
            if abs(difference) > Decimal("1"):
                warnings += 1
                _validation(
                    session,
                    source.id,
                    "budget_total_formula",
                    "warning",
                    "Контрольное соотношение итоговых поступлений "
                    "не выполнено с допустимой погрешностью.",
                    {
                        "section": section,
                        "difference": str(difference),
                        "formula_components": budget_codes,
                    },
                )
        insurance = fact_lookup.get(
            (section, 1010, "receipts_insurance_total")
        )
        insurance_codes = formula_components(
            schema,
            resolution,
            "receipts_insurance_total",
        )
        insurance_parts = [
            fact_lookup.get((section, 1010, code))
            for code in insurance_codes
        ]
        if (
            insurance is not None
            and insurance_parts
            and all(value is not None for value in insurance_parts)
        ):
            difference = insurance - sum(insurance_parts, Decimal("0"))
            if abs(difference) > Decimal("1"):
                warnings += 1
                _validation(
                    session,
                    source.id,
                    "insurance_total_formula",
                    "warning",
                    "Контрольное соотношение страховых взносов не выполнено.",
                    {"section": section, "difference": str(difference)},
                )
    return warnings


def import_summary_file(
    session: Session, batch: ImportBatch, path: Path, *, replace: bool = False
) -> SourceFile:
    digest = sha256_file(path)
    resolved_path = str(path.resolve())
    existing_by_hash = session.scalar(
        select(SourceFile).where(SourceFile.sha256 == digest)
    )
    existing_by_path = session.scalar(
        select(SourceFile)
        .where(SourceFile.absolute_path == resolved_path)
        .order_by(SourceFile.id.desc())
    )
    existing = existing_by_hash or existing_by_path
    duplicate_versions = session.scalars(
        select(SourceFile).where(
            SourceFile.absolute_path == resolved_path,
            SourceFile.id != existing.id if existing is not None else True,
        )
    ).all()
    for duplicate in duplicate_versions:
        session.execute(
            delete(MspFact).where(
                MspFact.source_file_id == duplicate.id
            )
        )
        duplicate.status = "superseded"
        duplicate.warnings_json = [
            "Версия заменена повторным семантическим импортом "
            "того же абсолютного пути."
        ]
    if existing and not replace:
        return existing

    reader = open_reader(path)
    as_of_date, warnings = extract_as_of_date(reader)
    if as_of_date is None:
        raise ValueError("; ".join(warnings))
    period_start, period_end, report_year, months_count = period_from_as_of(as_of_date)
    section_i_name = find_sheet(reader, "Раздел I") or find_sheet(reader, "Раздел 1")
    section_ii_name = find_sheet(reader, "Раздел II") or find_sheet(reader, "Раздел 2")
    if not section_i_name or not section_ii_name:
        raise ValueError("Не найдены оба сводных листа Раздела I и Раздела II")
    schema = recognize_header(reader.sheet(section_i_name))
    schema_version = _schema_version(session, schema)

    if existing and replace:
        session.execute(delete(MspFact).where(MspFact.source_file_id == existing.id))
        session.execute(
            delete(ValidationResult).where(
                ValidationResult.source_file_id == existing.id
            )
        )
        source = existing
    else:
        source = SourceFile(
            batch_id=batch.id,
            absolute_path=str(path.resolve()),
            sha256=digest,
            size_bytes=path.stat().st_size,
            modified_at=datetime.fromtimestamp(path.stat().st_mtime),
            file_format=path.suffix.casefold().lstrip("."),
            workbook_kind="msp_summary",
        )
        session.add(source)
        session.flush()
    source.as_of_date = as_of_date
    source.absolute_path = resolved_path
    source.sha256 = digest
    source.size_bytes = path.stat().st_size
    source.modified_at = datetime.fromtimestamp(path.stat().st_mtime)
    source.period_start = period_start
    source.period_end = period_end
    source.report_year = report_year
    source.months_count = months_count
    source.series_kind = "cumulative_ytd"
    source.schema_version_id = schema_version.id
    source.status = "valid"
    source.warnings_json = warnings

    if schema_version.status != "approved":
        source.status = "quarantined"
        source.warnings_json = [
            *warnings,
            "Неизвестная схема граф; импорт фактов заблокирован до "
            "утверждения сопоставления.",
        ]
        _validation(
            session,
            source.id,
            "unknown_schema",
            "error",
            source.warnings_json[-1],
            {
                "schema_version_id": schema_version.id,
                "numeric_columns": schema.numeric_count,
            },
        )
        reader.close()
        session.flush()
        return source

    metrics = _metric_map(session)
    territory = _rf(session)
    fact_lookup: dict[tuple[str, int, str], Decimal] = {}
    section_schemas: dict[
        str,
        tuple[HeaderSchema, HeaderMetricResolution],
    ] = {}
    facts_added = 0
    for section, sheet_name, offset, group in (
        ("I", section_i_name, 0, "all_msp"),
        ("II", section_ii_name, 1000, "micro"),
    ):
        sheet = reader.sheet(sheet_name)
        local_schema = recognize_header(sheet)
        resolution = resolve_header_metrics(local_schema.raw_headers)
        if not resolution.is_complete:
            raise ValueError(
                "Семантическое сопоставление заголовков не прошло: "
                f"не распознаны {sorted(resolution.unmapped)}, "
                f"дубли {resolution.duplicate_codes}"
            )
        section_schemas[section] = (local_schema, resolution)
        for row_number in range(local_schema.header_number_row + 1, sheet.max_row + 1):
            raw_code = decimal_value(sheet.value(row_number, local_schema.code_column))
            if raw_code is None or raw_code != raw_code.to_integral_value():
                continue
            source_code = int(raw_code)
            base_code = source_code - offset
            if not 1000 <= base_code < 2000:
                continue
            name = str(sheet.value(row_number, 1) or "").strip()
            okved = str(sheet.value(row_number, 2) or "").strip() or None
            row_definition = _row_definition(
                session, base_code, name or f"Строка {base_code}", okved, report_year
            )
            for source_column, physical_column in local_schema.metric_columns.items():
                metric_code = resolution.source_to_code.get(source_column)
                metric = metrics.get(metric_code or "")
                if metric is None:
                    raise ValueError(
                        f"Показатель {metric_code} отсутствует в каталоге"
                    )
                value = decimal_value(sheet.value(row_number, physical_column))
                if value is None:
                    continue
                session.add(
                    MspFact(
                        source_file_id=source.id,
                        period_start=period_start,
                        period_end=period_end,
                        as_of_date=as_of_date,
                        section=section,
                        enterprise_group=group,
                        territory_id=territory.id,
                        row_definition_id=row_definition.id,
                        source_row_code=source_code,
                        metric_id=metric.id,
                        value=value,
                        unit=metric.unit,
                        source_sheet=sheet_name,
                        source_cell=sheet.coordinate(row_number, physical_column),
                        is_derived=False,
                        lineage_json={
                            "semantic_code": metric.semantic_code,
                            "source_report_column": source_column,
                            "source_header": local_schema.raw_headers.get(
                                source_column,
                                "",
                            ),
                            "mapping_method": "normalized_header",
                        },
                    )
                )
                fact_lookup[
                    (section, base_code, metric.semantic_code)
                ] = value
                facts_added += 1
        session.flush()

    validation_warnings = _validate_control_totals(
        session,
        source,
        fact_lookup,
        section_schemas,
    )
    if warnings or validation_warnings:
        source.status = "valid_with_warnings"
    source.warnings_json = [
        *warnings,
        *(
            [f"Контрольные соотношения: предупреждений {validation_warnings}"]
            if validation_warnings
            else []
        ),
    ]
    reader.close()
    session.flush()
    return source


def _metadata_source(
    session: Session,
    batch: ImportBatch,
    path: Path,
    kind: str,
    status: str,
    warnings: list[str],
) -> SourceFile:
    digest = sha256_file(path)
    existing = session.scalar(select(SourceFile).where(SourceFile.sha256 == digest))
    if existing:
        existing.workbook_kind = kind
        existing.status = status
        existing.warnings_json = warnings
        session.flush()
        return existing
    source = SourceFile(
        batch_id=batch.id,
        absolute_path=str(path.resolve()),
        sha256=digest,
        size_bytes=path.stat().st_size,
        modified_at=datetime.fromtimestamp(path.stat().st_mtime),
        file_format=path.suffix.casefold().lstrip("."),
        workbook_kind=kind,
        status=status,
        warnings_json=warnings,
    )
    session.add(source)
    session.flush()
    if status == "quarantined":
        _validation(
            session,
            source.id,
            "unsupported_workbook_layout",
            "error",
            warnings[0] if warnings else "Формат книги требует утверждённого адаптера.",
        )
    return source


def import_root(
    session: Session,
    root: Path,
    *,
    paths: list[Path] | None = None,
    include_regional_metadata: bool = True,
) -> ImportBatch:
    seed_catalog(session)
    selected = paths or source_files(root)
    batch = ImportBatch(source_root=str(root), files_found=len(selected))
    session.add(batch)
    session.commit()

    for path in selected:
        try:
            kind = classify_workbook(path, [])
            if kind == "unknown":
                reader = open_reader(path)
                try:
                    kind = classify_workbook(path, reader.sheet_names)
                finally:
                    reader.close()
            if kind == "msp_summary":
                before_id = session.scalar(
                    select(SourceFile.id).where(SourceFile.sha256 == sha256_file(path))
                )
                source = import_summary_file(session, batch, path)
                if before_id is None:
                    if source.status == "quarantined":
                        batch.files_quarantined += 1
                    else:
                        batch.files_imported += 1
            elif kind in {"regional_section_i", "regional_section_ii"}:
                if include_regional_metadata:
                    register_regional_file(
                        session, batch, path, kind
                    )
                    batch.files_imported += 1
            elif kind in {
                "codes_dictionary",
                "national_receipts",
                "controller",
                "analytical_auxiliary",
                "service_file",
            }:
                continue
            else:
                _metadata_source(
                    session,
                    batch,
                    path,
                    kind,
                    "quarantined",
                    ["Книга не соответствует известной структуре 1-МСП."],
                )
                batch.files_quarantined += 1
            session.commit()
        except Exception as exc:
            session.rollback()
            _metadata_source(
                session,
                batch,
                path,
                kind if kind != "msp_summary" else "unknown",
                "quarantined",
                [f"Ошибка распознавания: {exc}"],
            )
            batch.files_quarantined += 1
            batch.errors_count += 1
            session.commit()

    batch.status = "completed_with_warnings" if batch.files_quarantined else "completed"
    batch.finished_at = datetime.utcnow()
    session.commit()
    return batch
