from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from backend.app.models import (
    MetricDefinition,
    MspFact,
    RowDefinition,
    SchemaMetricMapping,
    SchemaVersion,
    SourceFile,
    Territory,
)


ACTIVE_SOURCE_STATUSES = ("valid", "valid_with_warnings")


def normalize(value: str) -> str:
    value = value.casefold().replace("ё", "е")
    value = re.sub(r"[‐‑‒–—−]", "-", value)
    return re.sub(r"\s+", " ", value).strip(" .,;:")


def territory_variants(name: str, territory_type: str) -> list[str]:
    """Deterministic linguistic variants used by the generated corpus."""

    variants = {name, name.replace("-", " "), name.replace(" ", "-")}
    normalized = normalize(name)
    replacements = {
        "Российская Федерация": ["Россия", "РФ", "по России"],
        "г. Москва": ["Москва", "Москве", "Москвы"],
        "г. Санкт-Петербург": [
            "Санкт-Петербург",
            "Санкт Петербург",
            "СПб",
            "Санкт-Петербурге",
        ],
        "г. Севастополь": ["Севастополь", "Севастополе"],
        "Ханты-Мансийский автономный округ - Югра": [
            "ХМАО — Югра",
            "ХМАО-Югра",
            "Югра",
        ],
        "Ямало-Ненецкий автономный округ": ["ЯНАО"],
        "Ненецкий автономный округ": ["НАО"],
        "Еврейская автономная область": ["ЕАО"],
    }
    variants.update(replacements.get(name, []))
    if territory_type == "federal_district":
        variants.add(re.sub(r" федеральный округ$", " ФО", name, flags=re.I))
    if normalized.startswith("республика "):
        stem = name.split(" ", 1)[1]
        variants.update({stem, f"в Республике {stem}", f"Республика {stem}"})
    if normalized.endswith(" республика"):
        stem = name[: -len(" Республика")]
        variants.update({stem, f"в {stem.replace('ская', 'ской')} Республике"})
    if normalized.endswith(" область"):
        stem = name[: -len(" область")]
        inflected = re.sub(r"ая$", "ой", stem)
        variants.add(f"в {inflected} области")
    if normalized.endswith(" край"):
        stem = name[: -len(" край")]
        inflected = re.sub(r"ий$", "ом", re.sub(r"ый$", "ом", stem))
        variants.add(f"в {inflected} крае")
    # A stable, recognizable typo exercises fuzzy resolution without turning
    # the corpus itself into a probabilistic generator.
    typo = re.sub(r"([аеёиоуыэюя])", "", name, count=1, flags=re.I)
    if typo != name:
        variants.add(typo)
    return sorted({item for item in variants if item}, key=normalize)


@dataclass(frozen=True)
class MetricCapability:
    code: str
    name: str
    column: int | None
    unit: str
    aliases: tuple[str, ...]
    periods: tuple[str, ...]
    source_schemas: tuple[str, ...]


@dataclass(frozen=True)
class RowCapability:
    code: int
    name: str
    okved: str | None
    aliases: tuple[str, ...]
    periods: tuple[str, ...]


@dataclass(frozen=True)
class TerritoryCapability:
    name: str
    territory_type: str
    official_code: str | None
    aliases: tuple[str, ...]
    periods: tuple[str, ...]


@dataclass(frozen=True)
class CapabilityManifest:
    version: int
    data_fingerprint: str
    facts_count: int
    source_files_count: int
    metrics: tuple[MetricCapability, ...]
    rows: tuple[RowCapability, ...]
    territories: tuple[TerritoryCapability, ...]
    enterprise_groups: tuple[str, ...]
    national_periods: tuple[str, ...]
    regional_periods: tuple[str, ...]
    semantic_mapping: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def metric(self, code: str) -> MetricCapability:
        return next(item for item in self.metrics if item.code == code)

    def row(self, code: int) -> RowCapability:
        return next(item for item in self.rows if item.code == code)

    def territory(self, name: str) -> TerritoryCapability:
        wanted = normalize(name)
        return next(
            item
            for item in self.territories
            if normalize(item.name) == wanted
            or wanted in {normalize(alias) for alias in item.aliases}
        )


def _iso_periods(values: list[date]) -> tuple[str, ...]:
    return tuple(item.isoformat() for item in sorted(set(values)))


def build_capability_manifest(session: Session) -> CapabilityManifest:
    """Build availability exclusively from imported facts and source metadata."""

    metrics = session.scalars(
        select(MetricDefinition).order_by(
            MetricDefinition.report_column_number,
            MetricDefinition.semantic_code,
        )
    ).all()
    rows = session.scalars(
        select(RowDefinition).order_by(RowDefinition.base_row_code)
    ).all()
    territories = session.scalars(
        select(Territory).order_by(Territory.territory_type, Territory.name_ru)
    ).all()

    metric_period_rows = session.execute(
        select(MspFact.metric_id, MspFact.period_end)
        .distinct()
        .order_by(MspFact.metric_id, MspFact.period_end)
    ).all()
    metric_periods: dict[int, list[date]] = {}
    for metric_id, period in metric_period_rows:
        metric_periods.setdefault(metric_id, []).append(period)

    row_period_rows = session.execute(
        select(MspFact.row_definition_id, MspFact.period_end)
        .distinct()
        .order_by(MspFact.row_definition_id, MspFact.period_end)
    ).all()
    row_periods: dict[int, list[date]] = {}
    for row_id, period in row_period_rows:
        row_periods.setdefault(row_id, []).append(period)

    regional_period_values = session.scalars(
        select(distinct(SourceFile.period_end)).where(
            SourceFile.workbook_kind.like("regional_%"),
            SourceFile.status.in_(ACTIVE_SOURCE_STATUSES),
            SourceFile.period_end.is_not(None),
        )
    ).all()
    regional_periods = _iso_periods(list(regional_period_values))
    national_periods = _iso_periods(
        list(session.scalars(select(distinct(MspFact.period_end))).all())
    )

    schemas_by_metric: dict[int, set[str]] = {}
    mapping_rows = session.execute(
        select(
            SchemaMetricMapping.metric_id,
            SchemaVersion.fingerprint,
            SchemaMetricMapping.approved,
        ).join(
            SchemaVersion,
            SchemaVersion.id == SchemaMetricMapping.schema_version_id,
        )
    ).all()
    approved_mappings = 0
    for metric_id, fingerprint, approved in mapping_rows:
        if metric_id is None or not approved:
            continue
        approved_mappings += 1
        schemas_by_metric.setdefault(metric_id, set()).add(fingerprint)

    metric_capabilities = tuple(
        MetricCapability(
            code=item.semantic_code,
            name=item.name_ru,
            column=item.report_column_number,
            unit=item.unit,
            aliases=tuple(item.aliases_json or []),
            periods=_iso_periods(metric_periods.get(item.id, [])),
            source_schemas=tuple(sorted(schemas_by_metric.get(item.id, set()))),
        )
        for item in metrics
    )
    row_capabilities = tuple(
        RowCapability(
            code=item.base_row_code,
            name=item.name_ru,
            okved=item.okved_text,
            aliases=tuple(item.aliases_json or []),
            periods=_iso_periods(row_periods.get(item.id, [])),
        )
        for item in rows
    )
    territory_capabilities = tuple(
        TerritoryCapability(
            name=item.name_ru,
            territory_type=item.territory_type,
            official_code=item.official_code,
            aliases=tuple(territory_variants(item.name_ru, item.territory_type)),
            periods=(
                national_periods
                if item.territory_type == "country"
                else regional_periods
            ),
        )
        for item in territories
    )
    facts_count = int(session.scalar(select(func.count(MspFact.id))) or 0)
    source_files_count = int(
        session.scalar(select(func.count(SourceFile.id))) or 0
    )
    fingerprints = session.execute(
        select(SourceFile.sha256, SourceFile.status).order_by(SourceFile.sha256)
    ).all()
    digest_payload = {
        "facts": facts_count,
        "sources": [tuple(item) for item in fingerprints],
        "metrics": [(item.code, item.column) for item in metric_capabilities],
        "rows": [item.code for item in row_capabilities],
        "territories": [item.name for item in territory_capabilities],
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            digest_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    groups = tuple(
        session.scalars(
            select(distinct(MspFact.enterprise_group)).order_by(
                MspFact.enterprise_group
            )
        ).all()
    )
    # This group is independently derivable as Section I minus Section II.
    if {"all_msp", "micro"} <= set(groups):
        groups = (*groups, "small_and_medium_without_micro")
    return CapabilityManifest(
        version=1,
        data_fingerprint=fingerprint,
        facts_count=facts_count,
        source_files_count=source_files_count,
        metrics=metric_capabilities,
        rows=row_capabilities,
        territories=territory_capabilities,
        enterprise_groups=tuple(dict.fromkeys(groups)),
        national_periods=national_periods,
        regional_periods=regional_periods,
        semantic_mapping={
            "approved_entries": approved_mappings,
            "unapproved_entries": len(mapping_rows) - approved_mappings,
            "metrics_without_approved_schema": [
                item.code
                for item in metric_capabilities
                if not item.source_schemas
            ],
        },
    )
