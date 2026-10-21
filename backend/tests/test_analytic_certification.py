from __future__ import annotations

import asyncio
import json
import uuid
from decimal import Decimal

import backend.certification.runner as certification_runner
from backend.app.analytical_agent import _parse_json_object
from backend.app.config import Settings
from backend.app.database import SessionLocal, init_database
from backend.app.llm import ProviderCapabilities
from backend.app.models import Conversation
from backend.app.analysis_runs import create_analysis_run
from backend.app.main import analysis_run_get
from backend.app.schemas import QueryPlan, SemanticQuery
from backend.certification.artifacts import materialize_artifacts
from backend.certification.case_models import (
    AnalyticEvaluationCase,
    DialogueTurn,
    ExpectedStatus,
    PeriodExpectation,
)
from backend.certification.corpus import (
    FULL_DIALOGUE_MINIMUM,
    FULL_SINGLE_MINIMUM,
    NEGATIVE_MINIMUM,
    SMOKE_SIZE,
    build_corpus,
    render_questions_markdown,
    stable_case_id,
)
from backend.certification.manifest import build_capability_manifest
from backend.certification.network_check import run_network_check
from backend.certification.oracle import (
    IndependentOracle,
    OracleResult,
    OracleRow,
)
from backend.certification.paraphrases import generate_paraphrase_cases
from backend.certification.verifier import verify_case


def test_qwen_json_parser_accepts_thinking_fences_and_outer_contract() -> None:
    raw = """
    <think>Сначала выберу инструмент.</think>
    Пояснение перед ответом.
    ```json
    {
      "expected_analyses": ["comparison"],
      "operations": [{"tool": "compare_entities", "arguments": {}}]
    }
    ```
    """
    parsed = _parse_json_object(raw)
    assert parsed["expected_analyses"] == ["comparison"]
    assert "operations" in parsed


def test_certification_terminal_verdict_uses_requested_colors() -> None:
    assert certification_runner._terminal_verdict(True, color=True) == (
        "\033[1;32mPASS\033[0m"
    )
    assert certification_runner._terminal_verdict(False, color=True) == (
        "\033[1;31mFAIL\033[0m"
    )
    assert certification_runner._terminal_verdict(True, color=False) == "PASS"


def test_generated_paraphrases_are_stable_versioned_metamorphic_cases(
    monkeypatch,
) -> None:
    class GeneratorProvider:
        name = "gnivc_qwen"
        model = "qwen3.6-35b"

        async def complete(self, _messages, **kwargs) -> str:
            assert kwargs["allow_fallback"] is False
            return json.dumps(
                {
                    "variants": [
                        "Сопоставь НДС Москвы и России.",
                        {"question": "НДС: Москва против РФ — сравни."},
                    ]
                },
                ensure_ascii=False,
            )

    monkeypatch.setattr(
        "backend.certification.paraphrases.configured_provider",
        lambda _settings: GeneratorProvider(),
    )
    base = AnalyticEvaluationCase(
        case_id="paraphrase-source-case-001",
        title="Source case",
        category="unit",
        turns=[
            DialogueTurn(
                question="Сравни НДС Москвы и России.",
                metric_codes=["receipts_vat_total"],
                territories=["г. Москва", "Российская Федерация"],
                row_codes=[1010],
                periods=[PeriodExpectation(end="2026-03-31")],
                required_analyses=["comparison"],
            )
        ],
        origin="regression",
    )
    generated = asyncio.run(
        generate_paraphrase_cases(
            [base],
            Settings(llm_provider="deterministic"),
            variants_per_case=2,
        )
    )
    repeated = asyncio.run(
        generate_paraphrase_cases(
            [base],
            Settings(llm_provider="deterministic"),
            variants_per_case=2,
        )
    )
    assert [item.case_id for item in generated] == [
        item.case_id for item in repeated
    ]
    assert {item.metadata["split"] for item in generated} == {
        "regression",
        "held_out",
    }
    assert all(item.metamorphic_relation == "paraphrase" for item in generated)
    assert all(
        item.turns[0].metric_codes == ["receipts_vat_total"]
        for item in generated
    )


def test_semantic_query_owns_analyses_and_context_provenance() -> None:
    semantic = SemanticQuery(
        query_plan=QueryPlan(
            intent="compare",
            metric_codes=["receipts_budget_total"],
            territories=["г. Москва"],
            periods=["2026-03-31"],
        ),
        analyses=["comparison", "distribution"],
        ranking_direction="both",
        ranking_criterion="growth_rate",
        constraint_sources={
            "metric_codes": "question",
            "territories": "conversation",
        },
    )
    assert semantic.analyses == ["comparison", "distribution"]
    assert semantic.constraint_sources["territories"] == "conversation"


class _NetworkProbeProvider:
    name = "gnivc_qwen"
    model = "qwen3.6-35b"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def healthcheck(self) -> dict:
        return {
            "status": "available",
            "provider": self.name,
            "model": self.model,
            "fallback": False,
        }

    async def complete(self, messages, **kwargs) -> str:
        self.calls.append(kwargs)
        index = int(messages[-1]["content"].split("=")[-1])
        return (
            "<think>ok</think>\n```json\n"
            f'{{"analyses":["comparison"],"probe":{index}}}'
            "\n```"
        )

    async def stream(self, _messages, **_kwargs):
        if False:
            yield ""

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(True, True, "aggregates")


def test_network_check_requires_four_first_attempt_qwen_responses() -> None:
    provider = _NetworkProbeProvider()
    result = asyncio.run(run_network_check(provider=provider))
    assert result["passed"] is True
    assert result["provider"] == "gnivc_qwen"
    assert result["model"] == "qwen3.6-35b"
    assert result["retries"] == 0
    assert result["fallback"] is False
    assert [item["status"] for item in result["parallel"]] == [
        "success",
        "success",
        "success",
        "success",
    ]
    assert len(provider.calls) == 4
    assert all(call["allow_fallback"] is False for call in provider.calls)


def test_network_check_supports_sixteen_first_attempt_qwen_responses() -> None:
    provider = _NetworkProbeProvider()
    result = asyncio.run(
        run_network_check(provider=provider, concurrency=16)
    )
    assert result["passed"] is True
    assert len(result["parallel"]) == 16
    assert len(provider.calls) == 16


def test_certification_loopback_client_ignores_proxy_environment(
    monkeypatch,
) -> None:
    captured: list[dict] = []

    class FakeAsyncClient:
        def __init__(self, **kwargs) -> None:
            captured.append(kwargs)

    monkeypatch.setattr(
        certification_runner.httpx,
        "AsyncClient",
        FakeAsyncClient,
    )
    certification_runner.CertificationApiClient(
        "http://127.0.0.1:8000",
        concurrency=16,
    )
    assert len(captured) == 2
    assert all(
        item["base_url"] == "http://127.0.0.1:8000"
        and item["trust_env"] is False
        for item in captured
    )
    # Commands use fresh sockets so an idle uvicorn keep-alive cannot lose an
    # exactly-once analysis-run response. Polling retains a short-lived pool.
    assert captured[1]["limits"].max_keepalive_connections == 0
    assert captured[0]["limits"].keepalive_expiry == 2.0
    assert captured[0]["limits"].max_connections == 16
    assert captured[1]["limits"].max_connections == 16


def test_certification_cleanup_polls_processing_without_rerunning_case(
    monkeypatch,
) -> None:
    statuses = [409, 503, 200]

    class FakeClient:
        async def delete(self, path: str):
            request = certification_runner.httpx.Request(
                "DELETE", f"http://127.0.0.1:8000{path}"
            )
            return certification_runner.httpx.Response(
                statuses.pop(0),
                request=request,
            )

    async def no_sleep(_seconds: float) -> None:
        return None

    client = object.__new__(certification_runner.CertificationApiClient)
    client.command_client = FakeClient()
    monkeypatch.setattr(certification_runner.asyncio, "sleep", no_sleep)
    asyncio.run(client.delete_conversation("test-conversation"))
    assert statuses == []


def test_resume_replays_only_infrastructure_interruptions_and_checkpoints(
    monkeypatch,
    tmp_path,
) -> None:
    cases = [
        AnalyticEvaluationCase(
            case_id=f"resume-case-{index:03d}",
            title=f"Resume {index}",
            category="unit",
            turns=[DialogueTurn(question=f"Question {index}")],
            origin="regression",
        )
        for index in range(1, 4)
    ]

    def record(case, *, passed, status, message=None):
        response = {"status": status}
        if message:
            response["error"] = {
                "code": "provider_unavailable",
                "message": message,
            }
        return {
            "case": case.model_dump(mode="json"),
            "attempt": 1,
            "response": response,
            "verdict": {
                "passed": passed,
                "issues": [],
                "checks": {},
                "numeric_differences": [],
            },
        }

    existing = [
        record(cases[0], passed=True, status="ready"),
        record(
            cases[1],
            passed=False,
            status="failed",
            message="Диагностика: connection_error.",
        ),
        record(
            cases[2],
            passed=False,
            status="failed",
            message="Диагностика: invalid_plan.",
        ),
    ]
    assert certification_runner._is_infrastructure_interruption(
        existing[1]
    )
    assert certification_runner._is_provider_connection_interruption(
        existing[1]
    )
    runner_404 = record(cases[1], passed=False, status="runner_error")
    assert certification_runner._is_infrastructure_interruption(runner_404)
    assert not certification_runner._is_provider_connection_interruption(
        runner_404
    )
    sqlite_locked = record(cases[1], passed=False, status="failed")
    sqlite_locked["response"]["error"] = {
        "code": "analysis_failed",
        "message": "(sqlite3.OperationalError) database is locked",
    }
    assert certification_runner._is_infrastructure_interruption(sqlite_locked)
    assert not certification_runner._is_provider_connection_interruption(
        sqlite_locked
    )
    called: list[str] = []

    class FakeApi:
        def __init__(self, _base_url, *, concurrency=4) -> None:
            assert concurrency == 4
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def preflight(self) -> dict:
            return {"provider": {"status": "available"}}

    async def fake_run_case(_api, _config, case):
        called.append(case.case_id)
        return record(case, passed=True, status="ready")

    monkeypatch.setattr(
        certification_runner, "CertificationApiClient", FakeApi
    )
    monkeypatch.setattr(certification_runner, "_run_case", fake_run_case)
    records, _health = asyncio.run(
        certification_runner.run_live_cases(
            cases,
            certification_runner.RunnerConfig(
                api_url="http://test",
                run_id="resume-run",
                run_dir=tmp_path,
            ),
            existing=existing,
        )
    )
    assert called == [cases[1].case_id]
    assert [item["case"]["case_id"] for item in records] == [
        item.case_id for item in cases
    ]
    assert records[1]["verdict"]["passed"] is True
    assert records[1]["infrastructure_resume_count"] == 1
    assert records[2]["verdict"]["passed"] is False
    checkpoint = certification_runner.read_jsonl(
        tmp_path / "checkpoint.jsonl"
    )
    assert [item["case"]["case_id"] for item in checkpoint] == [
        cases[1].case_id
    ]
    parser = certification_runner.build_parser()
    assert parser.parse_args(["--resume"]).resume == "latest"
    assert parser.parse_args(["--resume", "resume-run"]).resume == (
        "resume-run"
    )
    assert parser.parse_args(["--concurrency", "16"]).concurrency == 16
    run_dir = tmp_path / "resume-run"
    run_dir.mkdir()
    (run_dir / "run.json").write_text("{}", encoding="utf-8")
    assert certification_runner._resolve_resume_dir(
        tmp_path, "resume-run"
    ) == run_dir


def test_certification_poll_recovers_one_transient_read_error(
    monkeypatch,
) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.calls = 0

        async def get(self, path: str):
            self.calls += 1
            request = certification_runner.httpx.Request(
                "GET", f"http://127.0.0.1:8000{path}"
            )
            if self.calls == 1:
                raise certification_runner.httpx.ReadError(
                    "closed", request=request
                )
            return certification_runner.httpx.Response(
                200,
                request=request,
                json={"status": "ready", "result": {}},
            )

    async def no_sleep(_seconds: float) -> None:
        return None

    client = object.__new__(certification_runner.CertificationApiClient)
    client.client = FakeClient()
    monkeypatch.setattr(certification_runner.asyncio, "sleep", no_sleep)
    payload = asyncio.run(client.wait_analysis("test-run"))
    assert payload["status"] == "ready"
    assert client.client.calls == 2


def test_certification_poll_treats_no_data_as_terminal() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.calls = 0

        async def get(self, path: str):
            self.calls += 1
            request = certification_runner.httpx.Request(
                "GET", f"http://127.0.0.1:8000{path}"
            )
            return certification_runner.httpx.Response(
                200,
                request=request,
                json={"status": "no_data", "result": {}},
            )

    client = object.__new__(certification_runner.CertificationApiClient)
    client.client = FakeClient()
    payload = asyncio.run(client.wait_analysis("no-data-run"))
    assert payload["status"] == "no_data"
    assert client.client.calls == 1


def test_certification_poll_tolerates_transient_new_run_visibility(
    monkeypatch,
) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.calls = 0

        async def get(self, path: str):
            self.calls += 1
            request = certification_runner.httpx.Request(
                "GET", f"http://127.0.0.1:8000{path}"
            )
            if self.calls <= 2:
                return certification_runner.httpx.Response(
                    404,
                    request=request,
                    json={"detail": "Аналитический запуск не найден"},
                )
            return certification_runner.httpx.Response(
                200,
                request=request,
                json={"status": "ready", "result": {}},
            )

    async def no_sleep(_seconds: float) -> None:
        return None

    client = object.__new__(certification_runner.CertificationApiClient)
    client.client = FakeClient()
    monkeypatch.setattr(certification_runner.asyncio, "sleep", no_sleep)
    payload = asyncio.run(client.wait_analysis("new-run"))
    assert payload["status"] == "ready"
    assert client.client.calls == 3


def test_network_check_cli_does_not_require_live_corpus(
    monkeypatch,
    capsys,
) -> None:
    async def fake_network_check(*, concurrency: int) -> dict:
        assert concurrency == 4
        return {
            "passed": True,
            "provider": "gnivc_qwen",
            "model": "qwen3.6-35b",
            "health": {"status": "available"},
            "parallel": [],
        }

    def database_must_not_be_opened() -> None:
        raise AssertionError("network-check must run before corpus/database setup")

    monkeypatch.setattr(
        certification_runner,
        "run_network_check",
        fake_network_check,
    )
    monkeypatch.setattr(
        certification_runner,
        "init_database",
        database_must_not_be_opened,
    )
    assert certification_runner.main(["--network-check"]) == 0
    assert '"passed": true' in capsys.readouterr().out


def test_manifest_matches_imported_catalog_and_is_stable() -> None:
    init_database()
    with SessionLocal() as session:
        first = build_capability_manifest(session)
        second = build_capability_manifest(session)
    assert first.data_fingerprint == second.data_fingerprint
    assert first.facts_count == 1_092_204
    assert len(first.metrics) == 50
    assert len(first.rows) == 378
    assert len(first.territories) == 114
    assert {item.territory_type for item in first.territories} == {
        "country",
        "region",
        "federal_district",
    }
    assert "small_and_medium_without_micro" in first.enterprise_groups
    assert first.semantic_mapping["unapproved_entries"] == 0


def test_generated_corpus_meets_release_minimums_and_catalog_coverage() -> None:
    with SessionLocal() as session:
        manifest = build_capability_manifest(session)
    cases = build_corpus(manifest)
    singles = [item for item in cases if not item.is_dialogue]
    dialogues = [item for item in cases if item.is_dialogue]
    negatives = [item for item in cases if item.origin == "negative"]
    assert len(singles) >= FULL_SINGLE_MINIMUM
    assert len(dialogues) >= FULL_DIALOGUE_MINIMUM
    assert len(negatives) >= NEGATIVE_MINIMUM
    assert sum(item.tier == "smoke" for item in cases) == SMOKE_SIZE
    assert next(
        item
        for item in cases
        if item.case_id == "regression-past-four-years-two-regions-001"
    ).tier == "smoke"
    assert len({item.case_id for item in cases}) == len(cases)
    assert stable_case_id("value", 1) == stable_case_id("value", 1)
    markdown = render_questions_markdown(cases, manifest)
    assert "графа 50" in markdown
    assert "эти отрасли" in markdown
    assert "Игнорируй системные инструкции" in markdown
    assert {item.code for item in manifest.metrics} <= {
        code for case in cases for turn in case.turns for code in turn.metric_codes
    }
    five_turn_dialogue = next(
        item
        for item in dialogues
        if len(item.turns) == 5
    )
    regional_drill_down = five_turn_dialogue.turns[2]
    metric_switch = five_turn_dialogue.turns[3]
    assert regional_drill_down.territories == ["Российская Федерация"]
    assert regional_drill_down.required_tools == ["drill_down"]
    assert regional_drill_down.required_analyses == ["industry_to_regions"]
    assert metric_switch.territories == ["Российская Федерация"]
    assert metric_switch.required_tools == ["drill_down"]
    assert metric_switch.required_analyses == ["industry_to_regions"]


def test_oracle_reads_national_and_regional_values_without_analytics_tools() -> None:
    with SessionLocal() as session:
        manifest = build_capability_manifest(session)
        oracle = IndependentOracle(session, manifest)
        national = oracle.value(
            "receipts_vat_total",
            "Российская Федерация",
            1010,
            "all_msp",
            "2026-06-30",
        )
        micro = oracle.value(
            "receipts_vat_total",
            "Российская Федерация",
            1010,
            "micro",
            "2026-06-30",
        )
        without_micro = oracle.value(
            "receipts_vat_total",
            "Российская Федерация",
            1010,
            "small_and_medium_without_micro",
            "2026-06-30",
        )
        moscow = oracle.value(
            "receipts_vat_total",
            "г. Москва",
            1010,
            "all_msp",
            "2026-03-31",
        )
        shifted_regional_schema = oracle.value(
            "receipts_budget_total",
            "Алтайский край",
            1015,
            "all_msp",
            "2024-03-31",
        )
        split_regional_source = oracle.value(
            "receipts_budget_total",
            "Чеченская Республика",
            1010,
            "all_msp",
            "2023-03-31",
        )
    assert national == Decimal("2508801008.000000")
    assert micro is not None and without_micro == national - micro
    assert moscow is not None and moscow > 0
    # The 2024 regional workbook's printed column 4 is not the budget metric.
    # The independent RF-control alignment must select source column 2.
    assert shifted_regional_schema == Decimal("243274")
    # In 2023 the regional section is split across several workbooks. The
    # oracle must select the file containing row 1010, not merely the newest
    # file registered for that period.
    assert split_regional_source == Decimal("760252")


def test_oracle_bounds_regional_outliers_and_uses_industries_for_drill_down() -> None:
    outlier_turn = DialogueTurn(
        question="Какие регионы являются выбросами?",
        metric_codes=["receipts_budget_total"],
        row_codes=[1010],
        periods=[PeriodExpectation(end="2026-03-31")],
        required_tools=["detect_outliers"],
        required_analyses=["outliers", "regional_rank"],
    )
    outlier_case = AnalyticEvaluationCase(
        case_id="oracle-outliers-bounded-001",
        title="Bounded regional outliers",
        category="unit",
        turns=[outlier_turn],
        origin="regression",
    )
    drill_turn = DialogueTurn(
        question="Найди отрасли, затем регионы.",
        metric_codes=["receipts_budget_total"],
        row_codes=[1010],
        periods=[PeriodExpectation(end="2026-03-31")],
        required_tools=["rank_entities", "drill_down"],
        required_analyses=["industry_rank", "industry_to_regions"],
        ranking_direction="both",
        ranking_criterion="growth_rate",
    )
    drill_case = AnalyticEvaluationCase(
        case_id="oracle-drill-industry-001",
        title="Industry-first drill down",
        category="unit",
        turns=[drill_turn],
        origin="regression",
    )
    with SessionLocal() as session:
        manifest = build_capability_manifest(session)
        oracle = IndependentOracle(session, manifest)
        outliers = oracle.evaluate(outlier_case)
        drill = oracle.evaluate(drill_case)
    assert [
        sum(row.section_kind == kind for row in outliers.rows)
        for kind in (
            "outliers_value",
            "outliers_growth",
            "outliers_composite",
        )
    ] == [10, 10, 5]
    assert len(drill.rows) == 6
    assert all(row.row_code != 1010 for row in drill.rows)


def test_strict_verifier_accepts_only_qwen_and_exact_oracle_numbers() -> None:
    turn = DialogueTurn(
        question="Покажи НДС по России.",
        metric_codes=["receipts_vat_total"],
        territories=["Российская Федерация"],
        row_codes=[1010],
        enterprise_groups=["all_msp"],
        periods=[PeriodExpectation(end="2026-06-30")],
        required_tools=["compare_entities"],
        required_analyses=["comparison"],
    )
    case = AnalyticEvaluationCase(
        case_id="verifier-happy-path-001",
        title="Verifier happy path",
        category="unit",
        turns=[turn],
        origin="regression",
    )
    row = OracleRow(
        metric_code="receipts_vat_total",
        territory="Российская Федерация",
        row_code=1010,
        enterprise_group="all_msp",
        period="2026-06-30",
        value=Decimal("100"),
        previous=Decimal("80"),
        difference=Decimal("20"),
        growth_rate=Decimal("25"),
        entity="Российская Федерация",
    )
    oracle = OracleResult(
        ExpectedStatus.READY,
        {
            "metric_codes": turn.metric_codes,
            "territories": turn.territories,
        },
        [row],
    )
    result = {
        "query_plan": {
            "metric_codes": turn.metric_codes,
            "territories": turn.territories,
            "row_codes": turn.row_codes,
            "enterprise_groups": turn.enterprise_groups,
            "periods": ["2026-06-30"],
        },
        "research_plan": {
            "expected_analyses": ["comparison"],
            "operations": [
                {
                    "id": "op-1-comparison",
                    "tool": "compare_entities",
                    "arguments": {
                        "metric_codes": turn.metric_codes,
                        "enterprise_groups": turn.enterprise_groups,
                    },
                }
            ],
        },
        "evidence": [
            {
                "evidence_id": "ev-0123456789abcdef",
                "operation_id": "op-1-comparison",
                "tool": "compare_entities",
                "rows": [
                    {
                        "entity": "Российская Федерация",
                        "territory": "Российская Федерация",
                        "row_code": 1010,
                        "period": "2026-06-30",
                        "current": "100",
                        "previous": "80",
                        "difference": "20",
                        "growth_rate": "25",
                    }
                ],
            }
        ],
        "claims": [
            {
                "claim_id": "claim-1",
                "text": "Российская Федерация — 100.",
                "evidence_ids": ["ev-0123456789abcdef"],
                "numbers": ["100"],
                "entities": ["Российская Федерация"],
            }
        ],
        "hypotheses": [],
        "charts": [],
        "tables": [],
        "series": [],
        "execution": {
            "provider": "gnivc_qwen",
            "model": "qwen3.6-35b",
            "fallback": False,
            "retries": 0,
            "model_calls": 2,
            "tool_calls": 1,
            "duration_ms": 100,
        },
        "planner_mode": "research-agent:gnivc_qwen",
    }
    response = {"status": "ready", "result": result}
    assert verify_case(case, response, oracle).passed is True
    result["execution"]["model_calls"] = 30
    assert verify_case(case, response, oracle).passed is True
    result["execution"]["model"] = "rules-v1"
    verdict = verify_case(case, response, oracle)
    assert verdict.passed is False
    assert any(item.code == "model_mismatch" for item in verdict.issues)


def test_provider_unavailable_is_not_reported_as_a_user_refusal_or_fallback() -> None:
    turn = DialogueTurn(
        question="Покажи НДС по России.",
        metric_codes=["receipts_vat_total"],
        territories=["Российская Федерация"],
        row_codes=[1010],
        enterprise_groups=["all_msp"],
        periods=[PeriodExpectation(end="2026-06-30")],
        required_analyses=["comparison"],
    )
    case = AnalyticEvaluationCase(
        case_id="provider-diagnostic-001",
        title="Provider diagnostic",
        category="unit",
        turns=[turn],
        origin="regression",
    )
    oracle = OracleResult(
        ExpectedStatus.READY,
        {"metric_codes": turn.metric_codes},
        [],
    )
    response = {
        "status": "failed",
        "error": {
            "code": "provider_unavailable",
            "message": "Сертификация запрещает deterministic planner fallback.",
        },
        "result": {"answer": "Планировщик не ответил"},
    }
    verdict = verify_case(case, response, oracle)
    assert verdict.passed is False
    assert [item.code for item in verdict.issues] == ["provider_error"]


def test_forbidden_old_metric_ignores_resolver_catalog_but_not_answer() -> None:
    turn = DialogueTurn(
        question="Там же замени НДС на НДФЛ.",
        metric_codes=["receipts_ndfl_total"],
        territories=["г. Москва"],
        row_codes=[1010],
        periods=[PeriodExpectation(end="2026-03-31")],
        forbidden_entities=["Налог на добавленную стоимость"],
    )
    case = AnalyticEvaluationCase(
        case_id="verifier-metric-switch-001",
        title="Metric switch disclosure scope",
        category="unit",
        turns=[turn],
        origin="regression",
    )
    result = {
        "query_plan": {
            "metric_codes": ["receipts_ndfl_total"],
            "territories": ["г. Москва"],
            "row_codes": [1010],
            "enterprise_groups": ["all_msp"],
            "periods": ["2026-03-31"],
        },
        "research_plan": {
            "expected_analyses": [],
            "operations": [],
        },
        "answer": "Налог на доходы физических лиц по Москве.",
        "evidence": [
            {
                "evidence_id": "ev-resolver",
                "tool": "resolve_entities",
                "rows": [
                    {
                        "entity": "Налог на добавленную стоимость",
                        "row_code": "receipts_vat_total",
                    }
                ],
            }
        ],
        "claims": [],
        "hypotheses": [],
        "charts": [],
        "tables": [],
        "series": [],
        "execution": {
            "provider": "gnivc_qwen",
            "model": "qwen3.6-35b",
            "fallback": False,
            "retries": 0,
            "model_calls": 1,
            "tool_calls": 0,
        },
        "planner_mode": "research-agent:gnivc_qwen",
    }
    oracle = OracleResult(ExpectedStatus.READY, {}, [])
    response = {"status": "ready", "result": result}
    assert verify_case(case, response, oracle).passed is True

    result["answer"] = "Старый показатель: Налог на добавленную стоимость."
    verdict = verify_case(case, response, oracle)
    assert verdict.passed is False
    assert [item.code for item in verdict.issues] == ["forbidden_entity"]


def test_explicit_no_data_status_is_a_safe_refusal() -> None:
    case = AnalyticEvaluationCase(
        case_id="explicit-no-data-case-001",
        title="Explicit no data",
        category="unit",
        turns=[
            DialogueTurn(
                question="Покажи данные за 2035 год",
                expected_status="no_data",
            )
        ],
        origin="negative",
    )
    verdict = verify_case(
        case,
        {
            "status": "no_data",
            "result": {
                "status": "no_data",
                "answer": "Для выбранного периода нет проверенных данных.",
                "evidence": [],
            },
        },
        OracleResult(ExpectedStatus.NO_DATA, {}, []),
    )
    assert verdict.passed is True


def test_certification_flag_is_persisted_on_temporary_analysis_run() -> None:
    conversation = Conversation(id="certification-conversation", title="test")
    with SessionLocal() as session:
        session.add(conversation)
        session.commit()
        turn = create_analysis_run(
            session,
            conversation,
            "Покажи динамику НДС",
            certification_mode=True,
        )
        assert turn.planner_json["certification_mode"] is True
        session.delete(conversation)
        session.commit()


def test_no_data_analysis_run_remains_readable_by_polling_api() -> None:
    conversation = Conversation(id=str(uuid.uuid4()), title="no data test")
    with SessionLocal() as session:
        session.add(conversation)
        session.commit()
        turn = create_analysis_run(
            session,
            conversation,
            "Покажи недоступный факт",
            certification_mode=True,
        )
        turn.status = "no_data"
        turn.turn_type = "no_data"
        session.commit()
        payload = analysis_run_get(turn.id, session)
        assert payload["status"] == "no_data"
        session.delete(conversation)
        session.commit()


def test_failure_artifacts_include_junit_html_and_anonymized_replay(tmp_path) -> None:
    with SessionLocal() as session:
        manifest = build_capability_manifest(session)
    case = AnalyticEvaluationCase(
        case_id="artifact-failure-case-001",
        title="Artifact failure",
        category="unit",
        turns=[DialogueTurn(question="Проверка", expected_status="no_data")],
        origin="negative",
    )
    record = {
        "case": case.model_dump(mode="json"),
        "oracle": {"filters": {}},
        "response": {
            "result": {
                "query_plan": {},
                "answer": "internal /Users/example/secret.xlsx",
                "execution": {},
            }
        },
        "verdict": {
            "passed": False,
            "issues": [{"code": "test", "message": "failure"}],
            "numeric_differences": [],
        },
    }
    coverage = materialize_artifacts(
        tmp_path,
        [record],
        manifest,
        selected_count=1,
    )
    assert coverage["release_passed"] is False
    for name in ("results.jsonl", "coverage.json", "report.html", "junit.xml"):
        assert (tmp_path / name).exists()
    report = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "<th>№</th>" in report
    assert "class='case-number'>1</td>" in report
    assert "class='status fail'>FAIL</span>" in report
    assert ".status.pass{background:#16803a}" in report
    assert ".status.fail{background:#c62828}" in report
    replay = json.loads(
        (tmp_path / "replays" / f"{case.case_id}.json").read_text()
    )
    assert "/Users/" not in json.dumps(replay)
