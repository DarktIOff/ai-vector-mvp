from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.utcnow()


class ImportBatch(Base):
    __tablename__ = "import_batch"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="processing")
    source_root: Mapped[str] = mapped_column(Text)
    files_found: Mapped[int] = mapped_column(Integer, default=0)
    files_imported: Mapped[int] = mapped_column(Integer, default=0)
    files_quarantined: Mapped[int] = mapped_column(Integer, default=0)
    errors_count: Mapped[int] = mapped_column(Integer, default=0)
    source_files: Mapped[list["SourceFile"]] = relationship(back_populates="batch")


class SchemaVersion(Base):
    __tablename__ = "schema_version"

    id: Mapped[int] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    form_name: Mapped[str] = mapped_column(String(64), default="1-МСП")
    edition_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    numeric_column_count: Mapped[int] = mapped_column(Integer)
    header_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="approved")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SourceFile(Base):
    __tablename__ = "source_file"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("import_batch.id"))
    absolute_path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    modified_at: Mapped[datetime] = mapped_column(DateTime)
    file_format: Mapped[str] = mapped_column(String(16))
    workbook_kind: Mapped[str] = mapped_column(String(64))
    as_of_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    report_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    months_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    series_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    schema_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("schema_version.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), default="valid")
    warnings_json: Mapped[list] = mapped_column(JSON, default=list)
    batch: Mapped[ImportBatch] = relationship(back_populates="source_files")
    facts: Mapped[list["MspFact"]] = relationship(
        back_populates="source_file", cascade="all, delete-orphan"
    )


class MetricDefinition(Base):
    __tablename__ = "metric_definition"

    id: Mapped[int] = mapped_column(primary_key=True)
    semantic_code: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    report_column_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    name_ru: Mapped[str] = mapped_column(Text)
    description_ru: Mapped[str] = mapped_column(Text, default="")
    unit: Mapped[str] = mapped_column(String(32), default="тыс. руб.")
    aggregation_rule: Mapped[str] = mapped_column(String(32), default="sum")
    aliases_json: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="approved")


class SchemaMetricMapping(Base):
    __tablename__ = "schema_metric_mapping"
    __table_args__ = (
        UniqueConstraint(
            "schema_version_id",
            "report_column_number",
            name="uq_schema_report_column",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    schema_version_id: Mapped[int] = mapped_column(ForeignKey("schema_version.id"))
    report_column_number: Mapped[int] = mapped_column(Integer)
    physical_column: Mapped[int] = mapped_column(Integer)
    metric_id: Mapped[int | None] = mapped_column(
        ForeignKey("metric_definition.id"), nullable=True
    )
    raw_header: Mapped[str] = mapped_column(Text)
    normalized_header: Mapped[str] = mapped_column(Text)
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), default=Decimal("1"))
    approved: Mapped[bool] = mapped_column(Boolean, default=True)


class RowDefinition(Base):
    __tablename__ = "row_definition"

    id: Mapped[int] = mapped_column(primary_key=True)
    base_row_code: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    name_ru: Mapped[str] = mapped_column(Text)
    okved_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_row_id: Mapped[int | None] = mapped_column(
        ForeignKey("row_definition.id"), nullable=True
    )
    hierarchy_level: Mapped[int] = mapped_column(Integer, default=0)
    aggregation_expression: Mapped[str | None] = mapped_column(Text, nullable=True)
    aliases_json: Mapped[list] = mapped_column(JSON, default=list)
    valid_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)


class Territory(Base):
    __tablename__ = "territory"

    id: Mapped[int] = mapped_column(primary_key=True)
    name_ru: Mapped[str] = mapped_column(String(255))
    normalized_name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    territory_type: Mapped[str] = mapped_column(String(32))
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("territory.id"), nullable=True
    )
    official_code: Mapped[str | None] = mapped_column(String(64), nullable=True)


class MspFact(Base):
    __tablename__ = "msp_fact"
    __table_args__ = (
        UniqueConstraint(
            "source_file_id",
            "section",
            "territory_id",
            "row_definition_id",
            "metric_id",
            "period_end",
            name="uq_source_fact",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_file_id: Mapped[int] = mapped_column(ForeignKey("source_file.id"), index=True)
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date, index=True)
    as_of_date: Mapped[date] = mapped_column(Date)
    section: Mapped[str] = mapped_column(String(8))
    enterprise_group: Mapped[str] = mapped_column(String(64), index=True)
    territory_id: Mapped[int] = mapped_column(ForeignKey("territory.id"), index=True)
    row_definition_id: Mapped[int] = mapped_column(
        ForeignKey("row_definition.id"), index=True
    )
    source_row_code: Mapped[int] = mapped_column(Integer)
    metric_id: Mapped[int] = mapped_column(ForeignKey("metric_definition.id"), index=True)
    value: Mapped[Decimal] = mapped_column(Numeric(30, 6))
    unit: Mapped[str] = mapped_column(String(32), default="тыс. руб.")
    source_sheet: Mapped[str] = mapped_column(String(255))
    source_cell: Mapped[str] = mapped_column(String(32))
    is_derived: Mapped[bool] = mapped_column(Boolean, default=False)
    lineage_json: Mapped[dict] = mapped_column(JSON, default=dict)

    source_file: Mapped[SourceFile] = relationship(back_populates="facts")
    metric: Mapped[MetricDefinition] = relationship()
    row_definition: Mapped[RowDefinition] = relationship()
    territory: Mapped[Territory] = relationship()


class ValidationResult(Base):
    __tablename__ = "validation_result"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_file_id: Mapped[int] = mapped_column(ForeignKey("source_file.id"), index=True)
    check_code: Mapped[str] = mapped_column(String(96))
    severity: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32))
    message_ru: Mapped[str] = mapped_column(Text)
    context_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ReportRun(Base):
    __tablename__ = "report_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    report_type: Mapped[str] = mapped_column(String(64), default="1-msp")
    status: Mapped[str] = mapped_column(String(32), default="processing")
    old_period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    new_period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    parameters_json: Mapped[dict] = mapped_column(JSON, default=dict)
    facts_snapshot_json: Mapped[dict] = mapped_column(JSON, default=dict)
    output_docx_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_json_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[str] = mapped_column(String(255), default="Геворг М.")


class AiCallLog(Base):
    __tablename__ = "ai_call_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    purpose: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    token_usage_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Conversation(Base):
    __tablename__ = "conversation"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str] = mapped_column(String(255), default="Новый диалог")
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    state_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, index=True
    )
    turns: Mapped[list["ConversationTurn"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="ConversationTurn.sequence",
    )
    project: Mapped["AnalysisProject | None"] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        uselist=False,
    )


class ConversationTurn(Base):
    __tablename__ = "conversation_turn"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id", "sequence", name="uq_conversation_turn_sequence"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversation.id"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(16), default="user")
    turn_type: Mapped[str] = mapped_column(String(32), default="question")
    status: Mapped[str] = mapped_column(String(32), default="ready", index=True)
    content: Mapped[str] = mapped_column(Text)
    plan_json: Mapped[dict] = mapped_column(JSON, default=dict)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    clarification_json: Mapped[dict] = mapped_column(JSON, default=dict)
    planner_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    conversation: Mapped[Conversation] = relationship(back_populates="turns")


class AnalysisProject(Base):
    __tablename__ = "analysis_project"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversation.id"), unique=True, index=True
    )
    title: Mapped[str] = mapped_column(String(255))
    version: Mapped[int] = mapped_column(Integer, default=1)
    document_json: Mapped[dict] = mapped_column(JSON, default=dict)
    output_docx_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )
    conversation: Mapped[Conversation] = relationship(back_populates="project")
    blocks: Mapped[list["AnalysisProjectBlock"]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="AnalysisProjectBlock.position",
    )


class AnalysisProjectBlock(Base):
    __tablename__ = "analysis_project_block"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "turn_id", name="uq_project_conversation_turn"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("analysis_project.id"), index=True
    )
    turn_id: Mapped[str] = mapped_column(
        ForeignKey("conversation_turn.id"), index=True
    )
    position: Mapped[int] = mapped_column(Integer, default=0)
    snapshot_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    project: Mapped[AnalysisProject] = relationship(back_populates="blocks")


class Scenario(Base):
    __tablename__ = "scenario"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    source_conversation_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversation.id"), nullable=True, index=True
    )
    steps_json: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )
