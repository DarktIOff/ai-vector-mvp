from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExpectedStatus(str, Enum):
    READY = "ready"
    CLARIFICATION = "clarification"
    NO_DATA = "no_data"


class PeriodExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    end: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    mode: Literal["cumulative_ytd", "standalone_quarter"] = "cumulative_ytd"
    label: str | None = None


class PresentationRequirements(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chart_required: bool = False
    chart_type: Literal["line", "bar", "scatter", "none"] = "none"
    table_required: bool = False
    exact_table_order: bool = False
    hypotheses_allowed: bool = True
    hypotheses_required: bool = False


class DialogueTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)
    expected_status: ExpectedStatus = ExpectedStatus.READY
    metric_codes: list[str] = Field(default_factory=list, max_length=8)
    territories: list[str] = Field(default_factory=list, max_length=114)
    row_codes: list[int] = Field(default_factory=list, max_length=378)
    enterprise_groups: list[str] = Field(default_factory=list, max_length=3)
    periods: list[PeriodExpectation] = Field(default_factory=list, max_length=24)
    required_tools: list[str] = Field(default_factory=list, max_length=12)
    required_analyses: list[str] = Field(default_factory=list, max_length=12)
    ranking_direction: Literal["top", "bottom", "both"] | None = None
    ranking_criterion: Literal["value", "growth_rate", "difference"] | None = None
    allowed_entities: list[str] = Field(default_factory=list, max_length=600)
    forbidden_entities: list[str] = Field(default_factory=list, max_length=600)
    presentation: PresentationRequirements = Field(
        default_factory=PresentationRequirements
    )


class AnalyticEvaluationCase(BaseModel):
    """Versioned, serializable contract for one live certification scenario."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{7,95}$")
    title: str = Field(min_length=3, max_length=300)
    tier: Literal["smoke", "full"] = "full"
    category: str = Field(min_length=2, max_length=64)
    turns: list[DialogueTurn] = Field(min_length=1, max_length=5)
    tags: list[str] = Field(default_factory=list, max_length=20)
    metamorphic_group: str | None = Field(default=None, max_length=96)
    metamorphic_relation: Literal[
        "paraphrase",
        "change_territory",
        "change_metric",
        "ranking_consistency",
        "combined_receipts",
        "chart_table_evidence",
    ] | None = None
    origin: Literal[
        "catalog", "regression", "negative", "dialogue", "generated"
    ]
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_metamorphic_pair(self) -> "AnalyticEvaluationCase":
        if bool(self.metamorphic_group) != bool(self.metamorphic_relation):
            raise ValueError(
                "metamorphic_group и metamorphic_relation задаются вместе"
            )
        return self

    @property
    def question(self) -> str:
        return self.turns[-1].question

    @property
    def is_dialogue(self) -> bool:
        return len(self.turns) > 1

    @property
    def expected_status(self) -> ExpectedStatus:
        return self.turns[-1].expected_status
