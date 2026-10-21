from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ImportRequest(BaseModel):
    paths: list[str] | None = None
    include_regional_metadata: bool = True


class QueryPlan(BaseModel):
    intent: Literal[
        "value",
        "compare",
        "trend",
        "overview",
        "rank",
        "outliers",
        "share",
        "rank_growth",
        "rank_difference",
        "regional_average_growth",
        "regional_industry_rank_growth",
        "analytical_brief",
        "source",
        "catalog",
    ] = "value"
    catalog_target: Literal[
        "overview", "periods", "territories", "metrics", "okved"
    ] | None = None
    metric_codes: list[str] = Field(default_factory=list, max_length=8)
    enterprise_groups: list[
        Literal["all_msp", "micro", "small_and_medium_without_micro"]
    ] = Field(default_factory=lambda: ["all_msp"], min_length=1, max_length=3)
    territories: list[str] = Field(
        default_factory=lambda: ["Российская Федерация"], max_length=10
    )
    include_top_territory: bool = False
    include_bottom_territory: bool = False
    row_codes: list[int] = Field(default_factory=lambda: [1010], max_length=50)
    okved_codes: list[str] = Field(default_factory=list, max_length=50)
    periods: list[date] = Field(default_factory=list, max_length=24)
    period_modes: list[
        Literal["cumulative_ytd", "standalone_quarter"]
    ] = Field(default_factory=list, max_length=24)
    period_labels: list[str] = Field(default_factory=list, max_length=24)
    operations: list[
        Literal["value", "difference", "growth_rate", "share", "sources"]
    ] = Field(default_factory=lambda: ["value"], max_length=5)
    group_by: list[Literal["metric", "enterprise_group", "industry", "territory"]] = (
        Field(default_factory=list, max_length=2)
    )
    analysis_dimension: Literal["territory", "industry"] | None = None
    ranking_criteria: list[Literal["value", "growth_rate"]] = Field(
        default_factory=list, max_length=2
    )
    ranking_direction: Literal["top", "bottom", "both"] = "top"
    outlier_modes: list[
        Literal["value", "growth_rate", "composite"]
    ] = Field(default_factory=list, max_length=3)
    comparison_basis: Literal["previous_year_same_slice"] = (
        "previous_year_same_slice"
    )
    limit: int | None = Field(default=None, ge=1, le=50)


class SemanticQuery(BaseModel):
    """Internal model-owned interpretation before analytical execution."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ready", "clarification", "no_data"] = "ready"
    query_plan: QueryPlan | None = None
    analyses: list[
        Literal[
            "resolve",
            "series",
            "comparison",
            "industry_rank",
            "regional_rank",
            "outliers",
            "distribution",
            "structure",
            "industry_to_regions",
            "ranked_industry_series",
        ]
    ] = Field(default_factory=list, max_length=8)
    ranking_direction: Literal["top", "bottom", "both"] = "top"
    ranking_criterion: Literal["value", "growth_rate", "difference"] = (
        "value"
    )
    constraint_sources: dict[
        str, Literal["question", "conversation", "default"]
    ] = Field(default_factory=dict)
    clarification_question: str | None = Field(default=None, max_length=500)
    clarification_options: list[str] = Field(
        default_factory=list, max_length=6
    )
    no_data_reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def validate_status_payload(self) -> "SemanticQuery":
        if self.status == "ready":
            if self.query_plan is None:
                raise ValueError("Для ready требуется query_plan")
            if not self.analyses:
                raise ValueError("Для ready требуется хотя бы один анализ")
        elif self.status == "clarification":
            if not self.clarification_question:
                raise ValueError("Для clarification требуется вопрос")
        elif not self.no_data_reason:
            raise ValueError("Для no_data требуется причина")
        return self


class QueryRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    plan: QueryPlan | None = None
    conversation_id: str | None = Field(
        default=None, min_length=8, max_length=64
    )


class ReportGenerateRequest(BaseModel):
    report_mode: Literal["national", "region_vs_rf"] = "national"
    territory: str | None = Field(default=None, max_length=255)
    old_period_end: date | None = None
    new_period_end: date | None = None
    old_year: int = 2025
    new_year: int = 2026
    months_count: int = Field(default=6, ge=1, le=12)
    old_file: str | None = None
    new_file: str | None = None
    improve_narrative: bool = False

    @model_validator(mode="after")
    def validate_period_pair(self) -> "ReportGenerateRequest":
        if (self.old_period_end is None) != (self.new_period_end is None):
            raise ValueError(
                "old_period_end и new_period_end должны быть заданы вместе"
            )
        return self


class ReportInterpretRequest(BaseModel):
    command: str = Field(min_length=3, max_length=2000)


class ReportSpecification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ready", "clarification", "no_data"]
    report_mode: Literal["national", "region_vs_rf"] | None = None
    territory: str | None = Field(default=None, max_length=255)
    benchmark: Literal["Российская Федерация"] | None = None
    old_period_end: date | None = None
    new_period_end: date | None = None
    clarification_question: str | None = Field(default=None, max_length=500)
    no_data_reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def validate_status_payload(self) -> "ReportSpecification":
        if self.status == "ready":
            if (
                self.report_mode is None
                or self.territory is None
                or self.old_period_end is None
                or self.new_period_end is None
            ):
                raise ValueError(
                    "Для ready нужны режим, территория и оба периода"
                )
            if self.report_mode == "region_vs_rf" and self.benchmark is None:
                raise ValueError("Для region_vs_rf нужен benchmark РФ")
        elif self.status == "clarification":
            if not self.clarification_question:
                raise ValueError("Для clarification требуется вопрос")
        elif not self.no_data_reason:
            raise ValueError("Для no_data требуется причина")
        return self


class NarrativeRequest(BaseModel):
    enabled: bool = True


class AiTestRequest(BaseModel):
    text: str = "Искусственный тест: значение показателя равно 100."


class ReportDocumentPatch(BaseModel):
    base_version: int = Field(ge=1)
    changed_text_blocks: dict[str, str] = Field(default_factory=dict)


class ConversationCreate(BaseModel):
    title: str | None = Field(default=None, max_length=255)


class ConversationPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    pinned: bool | None = None
    archived: bool | None = None
    status: Literal[
        "draft",
        "processing",
        "needs_review",
        "revision_requested",
        "approved",
    ] | None = None


class ConversationTurnCreate(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class ClarificationAnswer(BaseModel):
    option_id: str | None = Field(default=None, max_length=96)
    text: str | None = Field(default=None, max_length=2000)


class ProjectBlockCreate(BaseModel):
    turn_id: str = Field(min_length=8, max_length=64)


class ProjectPatch(BaseModel):
    base_version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=255)
    ordered_block_ids: list[str] | None = None
    changed_text_blocks: dict[str, str] = Field(default_factory=dict)


class ScenarioCreate(BaseModel):
    conversation_id: str = Field(min_length=8, max_length=64)
    title: str | None = Field(default=None, max_length=255)


class ScenarioRunRequest(BaseModel):
    parameters: dict[str, str] = Field(default_factory=dict)


class AnalysisRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=3, max_length=2000)
    # Certification is an additive, test-only contract. It disables retries
    # and every deterministic fallback without changing normal analysis runs.
    certification_mode: bool = False


class ResearchOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^op-[a-z0-9-]{1,48}$")
    tool: Literal[
        "resolve_entities",
        "build_series",
        "compare_entities",
        "rank_entities",
        "describe_distribution",
        "detect_outliers",
        "analyze_structure",
        "drill_down",
    ]
    goal: str = Field(min_length=3, max_length=500)
    arguments: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list, max_length=4)


class ResearchPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=3, max_length=1000)
    research_questions: list[str] = Field(min_length=1, max_length=8)
    initial_filters: dict[str, Any] = Field(default_factory=dict)
    expected_analyses: list[str] = Field(default_factory=list, max_length=8)
    operations: list[ResearchOperation] = Field(min_length=1, max_length=12)


class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(pattern=r"^ev-[a-f0-9]{16}$")
    operation_id: str
    tool: str
    title: str
    periods: list[str] = Field(default_factory=list, max_length=24)
    unit: str | None = None
    methodology: str
    sources: list[str] = Field(default_factory=list, max_length=100)
    rows: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    verified: bool = True


class EvidenceClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: str = Field(pattern=r"^claim-[a-z0-9-]{1,48}$")
    text: str = Field(min_length=3, max_length=2000)
    evidence_ids: list[str] = Field(min_length=1, max_length=12)
    numbers: list[str] = Field(default_factory=list, max_length=50)
    periods: list[str] = Field(default_factory=list, max_length=24)
    entities: list[str] = Field(default_factory=list, max_length=50)


class AnalysisHypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hypothesis_id: str = Field(pattern=r"^hypothesis-[a-z0-9-]{1,48}$")
    label: Literal["Гипотеза"] = "Гипотеза"
    text: str = Field(min_length=3, max_length=2000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=12)
    status: Literal["possible_interpretation"] = "possible_interpretation"


class AnalysisResult(BaseModel):
    """Versioned agent result while retaining the legacy QueryResult surface."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ready"] = "ready"
    query_id: str | None = None
    turn_id: str | None = None
    conversation_id: str | None = None
    question: str | None = None
    query_plan: dict[str, Any]
    resolved_metrics: list[dict[str, Any]] = Field(default_factory=list)
    facts: list[dict[str, Any]] = Field(default_factory=list)
    calculations: list[dict[str, Any]] = Field(default_factory=list)
    series: list[dict[str, Any]] = Field(default_factory=list)
    analysis_sections: list[dict[str, Any]] = Field(default_factory=list)
    visualization: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)
    answer: str
    answer_mode: Literal["analytic_agent_v2"] = "analytic_agent_v2"
    numbers_verified: Literal[True] = True
    research_plan: ResearchPlan
    evidence: list[EvidenceItem] = Field(default_factory=list, max_length=12)
    claims: list[EvidenceClaim] = Field(default_factory=list)
    hypotheses: list[AnalysisHypothesis] = Field(default_factory=list)
    charts: list[dict[str, Any]] = Field(default_factory=list)
    tables: list[dict[str, Any]] = Field(default_factory=list)
    execution: dict[str, Any]
    context_update: dict[str, Any] = Field(default_factory=dict)
    planner_mode: str
