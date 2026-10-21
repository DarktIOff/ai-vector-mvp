from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    msp_history_root: Path = Path("./rd/1_msp_db")
    automsp_root: Path = Path("./rd/AutoMSP")
    msp_report_template: Path = Path("./rd/AutoMSP/template.docx")
    msp_codes_file: Path = Path("./rd/AutoMSP/Коды.xlsx")
    app_data_dir: Path = Path("./var")
    database_url: str = "sqlite:///./var/ai_vector.db"

    llm_provider: str = "deterministic"
    llm_query_model: str = "glm-5.1"
    llm_narrative_model: str = "glm-5.1"
    zai_base_url: str = "https://api.z.ai/api/paas/v4/"
    zai_api_key: str = ""
    gnivc_base_url: str = "https://api.ai.gnivc.ru/v1"
    gnivc_api_key: str = ""
    gnivc_query_model: str = "qwen3.6-35b"
    gnivc_narrative_model: str = "qwen3.5-122b"
    report_synthesis_provider: str = "gnivc_gigachat"
    report_synthesis_model: str = "GigaChat-Max"
    llm_timeout_seconds: int = 60
    llm_max_retries: int = 1
    llm_data_policy: str = "metadata_only"
    allow_external_llm_data: bool = False
    analytic_agent_v2: bool = False
    # Compatibility setting only; analysis-runs no longer use a global
    # deadline. Provider-level transport timeouts still apply.
    analytic_agent_timeout_seconds: int | None = None
    # Compatibility setting only. Normal agent execution is no longer
    # rejected because it needed more model calls.
    analytic_agent_max_model_calls: int | None = None
    # This is a circuit breaker for a non-converging semantic planner, not a
    # normal execution budget. Repeated calls are stopped earlier by explicit
    # no-progress detection.
    analytic_planner_emergency_max_steps: int = 32
    analytic_agent_max_tool_calls: int = 12
    analytic_planner_mode: str = "model_first"

    app_locale: str = "ru_RU"
    app_timezone: str = "Europe/Moscow"
    cors_origins: Annotated[list[str], NoDecode] = [
        "http://localhost:4173",
        "http://127.0.0.1:4173",
    ]

    @field_validator(
        "msp_history_root",
        "automsp_root",
        "msp_report_template",
        "msp_codes_file",
        "app_data_dir",
        mode="after",
    )
    @classmethod
    def make_absolute(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @field_validator("llm_data_policy")
    @classmethod
    def valid_data_policy(cls, value: str) -> str:
        allowed = {"none", "metadata_only", "aggregates", "full"}
        if value not in allowed:
            raise ValueError(f"LLM_DATA_POLICY должен быть одним из: {sorted(allowed)}")
        return value

    @field_validator("analytic_planner_mode")
    @classmethod
    def valid_analytic_planner_mode(cls, value: str) -> str:
        allowed = {"deterministic", "shadow", "model_first"}
        if value not in allowed:
            raise ValueError(
                "ANALYTIC_PLANNER_MODE должен быть одним из: "
                f"{sorted(allowed)}"
            )
        return value

    @field_validator("report_synthesis_provider")
    @classmethod
    def valid_report_synthesis_provider(cls, value: str) -> str:
        normalized = value.strip().lower()
        allowed = {
            "gigachat",
            "gnivc_gigachat",
            "configured",
            "deterministic",
        }
        if normalized not in allowed:
            raise ValueError(
                "REPORT_SYNTHESIS_PROVIDER должен быть одним из: "
                f"{sorted(allowed)}"
            )
        return normalized

    @field_validator("analytic_planner_emergency_max_steps")
    @classmethod
    def valid_planner_emergency_steps(cls, value: int) -> int:
        if value < 8:
            raise ValueError(
                "ANALYTIC_PLANNER_EMERGENCY_MAX_STEPS должен быть "
                "не меньше 8"
            )
        return value

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: object) -> object:
        if isinstance(value, str):
            candidate = value.strip()
            if candidate.startswith("[") and candidate.endswith("]"):
                import json

                parsed = json.loads(candidate)
                if not isinstance(parsed, list):
                    raise ValueError("CORS_ORIGINS должен быть списком адресов")
                return parsed
            return [
                origin.strip()
                for origin in candidate.split(",")
                if origin.strip()
            ]
        return value

    def prepare_directories(self) -> None:
        self.app_data_dir.mkdir(parents=True, exist_ok=True)
        (self.app_data_dir / "reports").mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.prepare_directories()
    return settings
