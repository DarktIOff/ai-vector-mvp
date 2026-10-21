from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path

import pytest
from docx import Document

from backend.app.calculations import find_receipts_workbook
from backend.app.analytics_service import execute_analytic_plan
from backend.app.config import Settings
from backend.app.database import SessionLocal
from backend.app.planner import (
    PlanningUnavailableError,
    _metadata_tool,
    _parse_json_object,
    _question_metric_requirements,
    _question_row_requirements,
    _validate_question_semantic_constraints,
    _validate_semantic_query,
    plan_question,
    validate_plan,
)
from backend.app.llm import ProviderCapabilities
from backend.app.query_service import (
    available_periods,
    build_query_plan,
    execute_plan,
    extract_years,
)
from backend.app.reporting import generate_report, update_report_document
from backend.app.schemas import QueryPlan, SemanticQuery


ROOT = Path(__file__).resolve().parents[2]
AUTOMSP = ROOT / "rd" / "AutoMSP"
COMMON_PERIODS = [
    date(year, 6, 30) for year in range(2022, 2027)
]


def test_metadata_tools_resolve_exact_catalog_references_and_scopes() -> None:
    with SessionLocal() as session:
        metric = _metadata_tool(
            session, "search_metric", {"query": "графа 1"}, COMMON_PERIODS
        )
        assert metric["resolution"] == "exact"
        assert [item["semantic_code"] for item in metric["matches"]] == [
            "entity_count"
        ]
        vat = _metadata_tool(
            session, "search_metric", {"query": "НДС"}, COMMON_PERIODS
        )
        assert [item["semantic_code"] for item in vat["matches"]] == [
            "receipts_vat_total"
        ]
        row = _metadata_tool(
            session, "search_row", {"query": "все МСП"}, COMMON_PERIODS
        )
        assert row["resolution"] == "default_scope"
        assert row["enterprise_group_hint"] == "all_msp"
        assert [item["row_code"] for item in row["matches"]] == [1010]
        territory = _metadata_tool(
            session, "search_territory", {"query": "по России"}, COMMON_PERIODS
        )
        assert territory["resolution"] == "exact"
        assert [item["name"] for item in territory["matches"]] == [
            "Российская Федерация"
        ]
        combined = _metadata_tool(
            session,
            "search_metric",
            {
                "query": (
                    "совокупные поступления в бюджет вместе со "
                    "страховыми взносами"
                )
            },
            COMMON_PERIODS,
        )
        assert combined["resolution"] == "exact"
        assert [item["semantic_code"] for item in combined["matches"]] == [
            "receipts_budget_total",
            "receipts_insurance_total",
        ]
        micro = _metadata_tool(
            session,
            "search_row",
            {"query": "микропредприятия"},
            COMMON_PERIODS,
        )
        assert micro["resolution"] == "default_scope"
        assert micro["enterprise_group_hint"] == "micro"
        industries = _metadata_tool(
            session,
            "search_row",
            {"query": "отрасль"},
            COMMON_PERIODS,
        )
        assert industries["resolution"] == "default_scope"
        financial_insurance = _metadata_tool(
            session,
            "search_row",
            {"query": "финансовая и страховая деятельность"},
            COMMON_PERIODS,
        )
        assert financial_insurance["resolution"] == "exact"
        assert [
            item["row_code"] for item in financial_insurance["matches"]
        ] == [1382]
        assert _question_row_requirements(
            session,
            (
                "Покажи бюджетные поступления по отрасли финансовой и "
                "страховой деятельности по России за январь–июнь 2026 года."
            ),
            COMMON_PERIODS,
        ) == [1382]
        assert industries["scope"] == "all_industries"
        compact_total = _metadata_tool(
            session,
            "search_row",
            {
                "query": (
                    "ВСЕГО (стр.2010 = стр.2015+стр.2036 + стр.2087+"
                    "стр.2255+стр.2259+стр.2270+стр.2295+ стр.2320+"
                    "стр.2350+стр.2363+стр.2382+стр.2400+стр.2405+"
                    "стр.2430+стр.2460 +стр.2465+стр.2470+стр.2490+"
                    "стр.2497+стр.2506+стр.2512+стр.2513)"
                )
            },
            COMMON_PERIODS,
        )
        assert compact_total["resolution"] == "exact"
        assert [item["row_code"] for item in compact_total["matches"]] == [
            1010
        ]
        okved_plan = validate_plan(
            session,
            QueryPlan(
                metric_codes=["receipts_budget_total"],
                row_codes=[1020],
                okved_codes=["А 01"],
                periods=[date(2026, 6, 30)],
            ),
            available_periods(session),
        )
        assert okved_plan.row_codes == [1020]


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        (
            "Там же замени НДС на НДФЛ.",
            ["receipts_ndfl_total"],
        ),
        (
            "Там же замени НДФЛ на Акцизы, всего.",
            ["receipts_excise_total"],
        ),
    ],
)
def test_metric_replacement_names_only_the_target(
    question: str, expected: list[str]
) -> None:
    assert _question_metric_requirements(question) == expected


def test_semantic_validator_rejects_silent_generic_tax_substitution() -> None:
    semantic = SemanticQuery(
        query_plan=QueryPlan(
            intent="value",
            metric_codes=["receipts_budget_total"],
            territories=["Российская Федерация"],
            row_codes=[1010],
            periods=[date(2026, 6, 30)],
        ),
        analyses=["comparison"],
        constraint_sources={
            "metric_codes": "question",
            "territories": "default",
            "row_codes": "default",
            "enterprise_groups": "default",
            "periods": "default",
        },
    )
    with SessionLocal() as session:
        with pytest.raises(ValueError, match="молчаливой подстановки"):
            _validate_question_semantic_constraints(
                session,
                "Покажи налоги вариант 1.",
                semantic,
                [],
                available_periods(session),
            )


def test_regional_drill_down_reports_zero_bases_without_failure() -> None:
    plan = QueryPlan(
        intent="regional_industry_rank_growth",
        metric_codes=["receipts_excise_total"],
        enterprise_groups=["all_msp"],
        territories=["Российская Федерация"],
        row_codes=[1129, 1132, 1423],
        periods=[date(2026, 3, 31)],
        period_modes=["cumulative_ytd"],
        analysis_dimension="territory",
        group_by=["industry", "territory"],
        ranking_criteria=["growth_rate"],
        ranking_direction="both",
    )
    with SessionLocal() as session:
        result = execute_analytic_plan(session, plan)

    assert result["status"] == "ready"
    unavailable = next(
        section
        for section in result["analysis_sections"]
        if section["kind"] == "regional_industry_growth_unavailable"
    )
    assert {row["row_code"] for row in unavailable["rows"]} == {
        1129,
        1132,
        1423,
    }
    assert all(row["growth_rate"] is None for row in unavailable["rows"])
    assert "не выделяются" in result["answer"]


def test_metadata_tools_resolve_explicit_row_numbers_and_unique_typos() -> None:
    with SessionLocal() as session:
        periods = available_periods(session)
        row = _metadata_tool(
            session,
            "search_row",
            {
                "query": (
                    "производство металлургическое 1178 "
                    "1179 1182 1185 1190 1200"
                )
            },
            periods,
        )
        named_scope = _metadata_tool(
            session,
            "search_row",
            {
                "query": (
                    "Остальные виды экономической деятельности "
                    "(стр. 1506> или = стр. 1507+1508+1511)"
                )
            },
            periods,
        )
        typo = _metadata_tool(
            session,
            "search_territory",
            {"query": "Сбирский федеральный округ"},
            periods,
        )
        formula_rows = []
        for query in (
            "добыча металлических руд (1065=стр.1075+1080)",
            (
                "производство бумаги и бумажных изделий "
                "(ст.1136> или = стр. 1137+1138)"
            ),
            (
                "деятельность полиграфическая и копирование носителей "
                "информации (ст.1140> или = стр. 1141+1142)"
            ),
        ):
            result = _metadata_tool(
                session,
                "search_row",
                {"query": query},
                periods,
            )
            formula_rows.append(
                [item["row_code"] for item in result["matches"]]
            )
    assert row["resolution"] == "exact"
    assert [item["row_code"] for item in row["matches"]] == [1178]
    assert named_scope["resolution"] == "exact"
    assert [item["row_code"] for item in named_scope["matches"]] == [1506]
    assert typo["resolution"] == "exact"
    assert typo["match_strategy"] == "unique_fuzzy"
    assert [item["name"] for item in typo["matches"]] == [
        "Сибирский федеральный округ"
    ]
    assert formula_rows == [[1065], [1136], [1140]]


def test_quoted_industry_words_do_not_create_analytic_operations() -> None:
    question = (
        "Сколько поступило в бюджет по отрасли «производство "
        "электродвигателей, генераторов, трансформаторов и "
        "распределительных устройств, а также контрольно-измерительной "
        "аппаратуры» по России за январь–июнь 2026 года?"
    )
    semantic = SemanticQuery(
        query_plan=QueryPlan(
            intent="value",
            metric_codes=["receipts_budget_total"],
            territories=["Российская Федерация"],
            row_codes=[1221],
            periods=[date(2026, 6, 30)],
            period_modes=["cumulative_ytd"],
        ),
        analyses=["comparison"],
        constraint_sources={
            "metric_codes": "question",
            "territories": "question",
            "row_codes": "question",
            "enterprise_groups": "default",
            "periods": "question",
        },
    )
    with SessionLocal() as session:
        _validate_question_semantic_constraints(
            session,
            question,
            semantic,
            [],
            available_periods(session),
        )
        semantic.query_plan.row_codes = [1010]
        with pytest.raises(ValueError, match="строку формы"):
            _validate_question_semantic_constraints(
                session,
                question,
                semantic,
                [],
                available_periods(session),
            )


def test_source_backed_absence_closes_as_no_data_without_model_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NoDataProvider:
        name = "gnivc_qwen"
        model = "qwen3.6-35b"
        last_usage = {}

        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, _messages, **_kwargs) -> str:
            self.calls += 1
            return json.dumps(
                {
                    "action": "final",
                    "semantic_query": {
                        "query_plan": {
                            "intent": "value",
                            "metric_codes": ["receipts_excise_cars"],
                            "enterprise_groups": ["all_msp"],
                            "territories": ["Российская Федерация"],
                            "row_codes": [1010],
                            "periods": ["2026-06-30"],
                            "period_modes": ["cumulative_ytd"],
                        },
                        "analyses": ["comparison"],
                        "constraint_sources": {
                            "metric_codes": "question",
                            "territories": "question",
                            "row_codes": "default",
                            "enterprise_groups": "question",
                            "periods": "question",
                        },
                    },
                },
                ensure_ascii=False,
            )

        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(True, True, "metadata_only")

    provider = NoDataProvider()
    monkeypatch.setattr(
        "backend.app.planner.configured_provider",
        lambda _settings: provider,
    )
    monkeypatch.setattr(
        "backend.app.planner._log_agent_call",
        lambda *_args, **_kwargs: None,
    )
    with SessionLocal() as session:
        result = asyncio.run(
            plan_question(
                session,
                Settings(llm_provider="fake", llm_data_policy="metadata_only"),
                (
                    "Какое значение показателя «receipts_excise_cars» "
                    "по всем МСП по Российской Федерации за "
                    "январь–июнь 2026 года?"
                ),
                available_periods(session),
                allow_deterministic_fallback=False,
            )
        )
    assert result.status == "no_data"
    assert result.plan is None
    assert result.semantic_query is not None
    assert result.semantic_query.status == "no_data"
    assert provider.calls == 1


def test_regional_comparable_tool_uses_latest_source_backed_window() -> None:
    with SessionLocal() as session:
        result = _metadata_tool(
            session,
            "comparable_periods",
            {
                "years": [2022, 2023, 2024, 2025, 2026],
                "territories": ["Чеченская Республика", "г. Москва"],
                "enterprise_groups": ["all_msp"],
                "observation_count": 4,
            },
            available_periods(session),
        )
    assert result["scope"] == "regional"
    assert result["latest_common_periods"] == [
        "2023-03-31",
        "2024-03-31",
        "2025-03-31",
        "2026-03-31",
    ]


def test_comparable_action_is_enriched_from_named_question_territories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ComparableFirstProvider:
        name = "gnivc_qwen"
        model = "qwen3.6-35b"
        last_usage = {}

        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, _messages, **_kwargs) -> str:
            self.calls += 1
            if self.calls == 1:
                return json.dumps(
                    {
                        "action": "comparable_periods",
                        "arguments": {
                            "years": [2022, 2023, 2024, 2025, 2026]
                        },
                    },
                    ensure_ascii=False,
                )
            return json.dumps(
                {
                    "action": "final",
                    "semantic_query": {
                        "query_plan": {
                            "intent": "trend",
                            "metric_codes": ["receipts_budget_total"],
                            "enterprise_groups": ["all_msp"],
                            "territories": [
                                "Чеченская Республика",
                                "г. Москва",
                            ],
                            "row_codes": [1010],
                            "periods": [
                                "2023-03-31",
                                "2024-03-31",
                                "2025-03-31",
                                "2026-03-31",
                            ],
                            "period_modes": ["cumulative_ytd"] * 4,
                            "group_by": ["territory"],
                        },
                        "analyses": ["series"],
                        "constraint_sources": {
                            "metric_codes": "question",
                            "territories": "question",
                            "row_codes": "default",
                            "enterprise_groups": "question",
                            "periods": "question",
                        },
                    },
                },
                ensure_ascii=False,
            )

        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(True, True, "metadata_only")

    provider = ComparableFirstProvider()
    monkeypatch.setattr(
        "backend.app.planner.configured_provider",
        lambda _settings: provider,
    )
    monkeypatch.setattr(
        "backend.app.planner._log_agent_call",
        lambda *_args, **_kwargs: None,
    )
    with SessionLocal() as session:
        result = asyncio.run(
            plan_question(
                session,
                Settings(
                    llm_provider="fake",
                    llm_data_policy="metadata_only",
                ),
                (
                    "сравни поступления от МСП по Чеченской республике "
                    "и городу Москве за прошедшие 4 года"
                ),
                available_periods(session),
                allow_deterministic_fallback=False,
            )
        )

    assert result.status == "ready"
    assert result.plan is not None
    assert result.plan.periods == [
        date(2023, 3, 31),
        date(2024, 3, 31),
        date(2025, 3, 31),
        date(2026, 3, 31),
    ]
    first = (result.tool_journal or [])[0]
    assert set(first["arguments"]["territories"]) == {
        "Чеченская Республика",
        "г. Москва",
    }
    assert first["result"]["scope"] == "regional"


def test_semantic_validator_rejects_unrequested_quarter_and_missing_regional_book() -> None:
    semantic = SemanticQuery(
        query_plan=QueryPlan(
            intent="trend",
            metric_codes=["receipts_budget_total"],
            periods=COMMON_PERIODS,
            period_modes=["standalone_quarter"] * len(COMMON_PERIODS),
        ),
        analyses=["series"],
        constraint_sources={
            "metric_codes": "question",
            "territories": "default",
            "row_codes": "default",
            "enterprise_groups": "default",
            "periods": "question",
        },
    )
    with SessionLocal() as session:
        with pytest.raises(ValueError, match="standalone_quarter"):
            _validate_question_semantic_constraints(
                session,
                "Покажи динамику за последние 4 года",
                semantic,
                [],
                available_periods(session),
            )
        regional_plan = QueryPlan(
            intent="trend",
            metric_codes=["receipts_budget_total"],
            territories=["Чеченская Республика", "г. Москва"],
            row_codes=[1010],
            periods=[date(2026, 6, 30)],
        )
        with pytest.raises(ValueError, match="региональной книги"):
            validate_plan(session, regional_plan, available_periods(session))


def test_semantic_validator_preserves_explicit_metrics_and_enterprise_group() -> None:
    semantic = SemanticQuery(
        query_plan=QueryPlan(
            intent="value",
            metric_codes=["receipts_budget_total"],
            enterprise_groups=["all_msp"],
            periods=[date(2026, 6, 30)],
            period_modes=["cumulative_ytd"],
        ),
        analyses=["comparison"],
        constraint_sources={
            "metric_codes": "question",
            "territories": "default",
            "row_codes": "default",
            "enterprise_groups": "question",
            "periods": "question",
        },
    )
    with SessionLocal() as session:
        with pytest.raises(ValueError, match="показатели"):
            _validate_question_semantic_constraints(
                session,
                (
                    "Сколько совокупно поступило в бюджет вместе со "
                    "страховыми взносами по России за январь–июнь 2026?"
                ),
                semantic,
                [],
                available_periods(session),
            )
        with pytest.raises(ValueError, match="группу МСП"):
            _validate_question_semantic_constraints(
                session,
                (
                    "Каковы поступления в бюджет для микропредприятий "
                    "по России за январь–июнь 2026?"
                ),
                semantic,
                [],
                available_periods(session),
            )


def test_semantic_validator_rejects_extra_year_and_wrong_outsider_intent() -> None:
    semantic = SemanticQuery(
        query_plan=QueryPlan(
            intent="outliers",
            metric_codes=["receipts_budget_total"],
            periods=[date(2025, 6, 30), date(2026, 6, 30)],
            period_modes=["cumulative_ytd", "cumulative_ytd"],
            group_by=["industry"],
            analysis_dimension="industry",
            ranking_direction="bottom",
        ),
        analyses=["outliers"],
        ranking_direction="bottom",
        ranking_criterion="growth_rate",
        constraint_sources={
            "metric_codes": "question",
            "territories": "default",
            "row_codes": "default",
            "enterprise_groups": "default",
            "periods": "question",
        },
    )
    semantic = _validate_semantic_query(semantic)
    assert {"regional_rank", "outliers"}.issubset(semantic.analyses)
    with SessionLocal() as session:
        with pytest.raises(ValueError, match="явно запрошенные годы"):
            _validate_question_semantic_constraints(
                session,
                (
                    "Назови 5 аутсайдеров среди отраслей по приросту "
                    "Поступления в консолидированный бюджет по России "
                    "за январь–июнь 2026 года."
                ),
                semantic,
                [],
                available_periods(session),
            )
        semantic.query_plan.periods = [date(2026, 6, 30)]
        semantic.query_plan.period_modes = ["cumulative_ytd"]
        with pytest.raises(ValueError, match="отраслевой рейтинг"):
            _validate_question_semantic_constraints(
                session,
                (
                    "Назови 5 аутсайдеров среди отраслей по приросту "
                    "Поступления в консолидированный бюджет по России "
                    "за январь–июнь 2026 года."
                ),
                semantic,
                [],
                available_periods(session),
            )


def test_comparable_year_modifier_and_unknown_metric_catalog_are_rejected() -> None:
    question = (
        "Покажи динамику поступлений по России за последние "
        "4 сопоставимых года."
    )
    assert extract_years(question, 2026) == [
        2022,
        2023,
        2024,
        2025,
        2026,
    ]
    trend = _validate_semantic_query(
        SemanticQuery(
            query_plan=QueryPlan(
                intent="trend",
                metric_codes=["receipts_budget_total"],
                periods=[
                    date(2022, 6, 30),
                    date(2023, 6, 30),
                    date(2024, 6, 30),
                    date(2025, 6, 30),
                ],
                period_modes=["cumulative_ytd"] * 4,
            ),
            analyses=["series"],
            constraint_sources={
                "metric_codes": "question",
                "territories": "question",
                "row_codes": "default",
                "enterprise_groups": "default",
                "periods": "question",
            },
        )
    )
    unknown = _validate_semantic_query(
        SemanticQuery(
            query_plan=QueryPlan(
                intent="catalog",
                catalog_target="metrics",
                metric_codes=["receipts_vat_total"],
                periods=[],
            ),
            analyses=["resolve"],
            constraint_sources={
                "metric_codes": "question",
                "territories": "question",
                "row_codes": "default",
                "enterprise_groups": "default",
                "periods": "default",
            },
        )
    )
    with SessionLocal() as session:
        periods = available_periods(session)
        with pytest.raises(ValueError, match="5 временных"):
            _validate_question_semantic_constraints(
                session,
                question,
                trend,
                [],
                periods,
            )
        with pytest.raises(ValueError, match="подменять просмотром каталога"):
            _validate_question_semantic_constraints(
                session,
                "Покажи налог на квантовую телепортацию по России.",
                unknown,
                [],
                periods,
            )


def test_semantic_dependencies_and_planner_json_transport_are_normalized() -> None:
    semantic = _validate_semantic_query(
        SemanticQuery(
            query_plan=QueryPlan(
                intent="overview",
                metric_codes=["receipts_vat_total"],
                periods=[date(2026, 6, 30)],
                group_by=["industry"],
                analysis_dimension="industry",
            ),
            analyses=["distribution"],
            constraint_sources={
                "metric_codes": "question",
                "territories": "default",
                "row_codes": "default",
                "enterprise_groups": "default",
                "periods": "question",
            },
        )
    )
    assert "comparison" in semantic.analyses
    assert "industry_rank" in semantic.analyses
    assert semantic.analyses.index("industry_rank") < semantic.analyses.index(
        "distribution"
    )
    parsed = _parse_json_object(
        '<think>готово</think>\n{"probe": true}\n'
        '{"action":"search_metric","arguments":{"query":"НДС"}}'
    )
    assert parsed["action"] == "search_metric"


def test_model_can_correct_one_malformed_agent_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CorrectingProvider:
        name = "gnivc_qwen"
        model = "qwen3.6-35b"
        last_usage = {}

        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, _messages, **_kwargs) -> str:
            self.calls += 1
            if self.calls == 1:
                return "вызову инструмент без JSON"
            return json.dumps(
                {
                    "action": "final",
                    "semantic_query": {
                        "query_plan": {
                            "intent": "value",
                            "metric_codes": ["receipts_vat_total"],
                            "territories": ["Российская Федерация"],
                            "row_codes": [1010],
                            "periods": ["2026-06-30"],
                            "period_modes": ["cumulative_ytd"],
                        },
                        "analyses": ["comparison"],
                        "constraint_sources": {
                            "metric_codes": "question",
                            "territories": "default",
                            "row_codes": "default",
                            "enterprise_groups": "default",
                            "periods": "question",
                        },
                    },
                },
                ensure_ascii=False,
            )

        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(True, True, "metadata_only")

    provider = CorrectingProvider()
    monkeypatch.setattr(
        "backend.app.planner.configured_provider",
        lambda _settings: provider,
    )
    monkeypatch.setattr(
        "backend.app.planner._log_agent_call",
        lambda *_args, **_kwargs: None,
    )
    with SessionLocal() as session:
        result = asyncio.run(
            plan_question(
                session,
                Settings(
                    llm_provider="fake",
                    llm_data_policy="metadata_only",
                ),
                "Покажи НДС по России за январь–июнь 2026 года",
                available_periods(session),
                allow_deterministic_fallback=False,
            )
        )

    assert result.status == "ready"
    assert result.model_calls == 2
    assert (result.tool_journal or [])[0]["action"] == "invalid_json_response"


def test_metric_named_regional_taxes_keeps_dialogue_territory() -> None:
    previous = build_query_plan(
        "Покажи НДФЛ в Республике Бурятия за январь-март 2026",
        [date(2026, 3, 31)],
    )
    previous.territories = ["Республика Бурятия"]
    context = [
        {
            "question": "Покажи НДФЛ в Республике Бурятия",
            "plan": previous.model_dump(mode="json"),
            "result_summary": {"analysis_kinds": [], "analysis_entities": []},
        }
    ]
    with SessionLocal() as session:
        result = asyncio.run(
            plan_question(
                session,
                Settings(llm_provider="deterministic"),
                "Там же замени налог на Региональные налоги, всего.",
                available_periods(session),
                context,
            )
        )
    assert result.status == "ready"
    assert result.plan is not None
    assert result.plan.metric_codes == ["receipts_regional_total"]
    assert result.plan.territories == ["Республика Бурятия"]


def test_past_four_years_two_regions_keeps_all_comparable_periods() -> None:
    question = (
        "сравни поступления от МСП по Чеченской республике и городу "
        "Москве за прошедшие 4 года"
    )
    with SessionLocal() as session:
        periods = available_periods(session)
        planned = asyncio.run(
            plan_question(
                session,
                Settings(llm_provider="deterministic"),
                question,
                periods,
                [],
            )
        )
        assert planned.status == "ready"
        assert planned.plan is not None
        plan = validate_plan(session, planned.plan, periods)
        result = execute_plan(session, plan)

    assert plan.intent == "trend"
    assert plan.metric_codes == ["receipts_budget_total"]
    assert plan.territories == ["Чеченская Республика", "г. Москва"]
    assert [item.isoformat() for item in plan.periods] == [
        "2023-03-31",
        "2024-03-31",
        "2025-03-31",
        "2026-03-31",
    ]
    assert len(result["series"]) == 2
    assert [len(item["points"]) for item in result["series"]] == [4, 4]
    assert {item["territory"] for item in result["series"]} == {
        "Чеченская Республика",
        "г. Москва",
    }


QUERY_CORPUS = [
    # 15 многолетних динамик.
    ("Динамика поступлений от МСП за 2022–2026 годы", "trend"),
    ("Как изменились поступления по МСП за последние 4 года", "trend"),
    ("Покажи тренд НДС за 2022-2026", "trend"),
    ("НДФЛ по годам: 2022, 2023, 2024", "trend"),
    ("Динамика страховых взносов 2023, 2024 и 2025", "trend"),
    ("Покажи УСН за период 2022–2025", "trend"),
    ("Тренд акцизов за 2022—2024", "trend"),
    ("Динамика налога на прибыль 2022–2026", "trend"),
    ("Поступления НДС по годам 2022, 2023, 2024, 2025", "trend"),
    ("Динамика НПД за 2022–2026 годы", "trend"),
    ("Тренд земельного налога 2022–2024", "trend"),
    ("Динамика транспортного налога 2022–2025", "trend"),
    ("Покажи за период 2022–2026 страховые взносы", "trend"),
    ("Динамика спецрежимов за I квартал 2022–2025", "trend"),
    ("Тренд НДС за январь-июнь 2022–2026", "trend"),
    ("По годам НДС и НДФЛ: 2022, 2023, 2024", "trend"),
    ("Какая динамика по поступлениям МСП по РФ?", "trend"),
    # 10 попарных сравнений.
    ("Сравни НДС за 2025 и 2026", "compare"),
    ("Сравни НДФЛ за январь-июнь 2025 и 2026", "compare"),
    ("Налог на прибыль: первый квартал 2025 и первый квартал 2026", "compare"),
    ("Сравнение страховых взносов за 2025 и 2026", "compare"),
    ("Сравни графу 10 за 2025 и 2026", "compare"),
    ("Сравни УСН за 2024 и 2025", "compare"),
    ("НДС и НДФЛ за 2025 и 2026", "compare"),
    ("Сравни микро по акцизам за 2025 и 2026", "compare"),
    ("Сравни спецрежимы за 2024, 2025", "compare"),
    ("Сравни второй квартал 2025 и первый квартал 2026 по НДС", "compare"),
    # 10 значений.
    ("Сколько поступило НДС в 2026 году", "value"),
    ("Поступления НДФЛ за 2026", "value"),
    ("Значение налога на прибыль за январь-июнь 2026", "value"),
    ("Покажи страховые взносы за первый квартал 2026", "value"),
    ("Сколько поступило по УСН за 2026", "value"),
    ("Поступления по спецрежимам в 2026", "value"),
    ("Покажи графу 10 за 2026", "value"),
    ("Значение акцизов за 2026", "value"),
    ("Поступления НПД за 2026", "value"),
    ("Сколько земельного налога за 2026", "value"),
    # 5 долей.
    ("Какова доля поступлений от МСП за 2026", "share"),
    ("Доля НДС за 2026", "share"),
    ("Покажи долю НДФЛ за 2026", "share"),
    ("Доля микро по страховым взносам в 2026", "share"),
    ("Какова доля УСН за январь-июнь 2026", "share"),
    # 5 запросов источника.
    ("Покажи источник НДС за 2026", "source"),
    ("Откуда взяты страховые взносы за 2026", "source"),
    ("Источник НДФЛ за январь-июнь 2026", "source"),
    ("Откуда значение графы 10 за 2026", "source"),
    ("Покажи источник УСН за 2026", "source"),
    # 5 рейтингов.
    ("Покажи лидеров по росту НДС за 2026", "rank_growth"),
    ("Какие отрасли дали наибольший рост НДФЛ за 2026", "rank_growth"),
    ("Лидеры по страховым взносам за 2026", "rank_growth"),
    ("Наибольшее абсолютное отклонение НДС за 2026", "rank_difference"),
    ("Лидеры по абсолютному изменению УСН за 2026", "rank_difference"),
    (
        "Какой средний темп роста по всем регионам по этим отраслям",
        "regional_average_growth",
    ),
    (
        "Какие регионы лидируют по росту по этим отраслям?",
        "regional_industry_rank_growth",
    ),
    (
        "Покажи динамику по России, выбери отрасли и регионы с лучшей "
        "и худшей динамикой, покажи, где лучше всего и где хуже всего",
        "analytical_brief",
    ),
]


@pytest.mark.parametrize(("question", "intent"), QUERY_CORPUS)
def test_russian_query_corpus(question: str, intent: str) -> None:
    plan = build_query_plan(question, COMMON_PERIODS)
    assert plan.intent == intent
    assert plan.metric_codes
    if intent == "trend":
        assert 3 <= len(plan.periods) <= 24


def test_acceptance_trend_has_five_comparable_points() -> None:
    with SessionLocal() as session:
        periods = available_periods(session)
        plan = build_query_plan(
            "Покажи динамику поступлений от МСП за 2022–2026 годы",
            periods,
        )
        plan = validate_plan(session, plan, periods)
        result = execute_plan(session, plan)
    assert plan.intent == "trend"
    assert [item.isoformat() for item in plan.periods] == [
        "2022-06-30",
        "2023-06-30",
        "2024-06-30",
        "2025-06-30",
        "2026-06-30",
    ]
    assert len(result["series"]) == 1
    assert len(result["series"][0]["points"]) == 5
    assert len(result["series"][0]["changes"]) == 4
    assert result["visualization"]["type"] == "line"


def test_implicit_dynamics_uses_all_comparable_years() -> None:
    with SessionLocal() as session:
        periods = available_periods(session)
        plan = validate_plan(
            session,
            build_query_plan(
                "Какая динамика по поступлениям МСП по РФ?",
                periods,
            ),
            periods,
        )
    assert plan.intent == "trend"
    assert plan.metric_codes == ["receipts_budget_total"]
    assert [item.isoformat() for item in plan.periods] == [
        "2022-06-30",
        "2023-06-30",
        "2024-06-30",
        "2025-06-30",
        "2026-06-30",
    ]


def test_analytical_brief_combines_national_industry_and_region_views() -> None:
    question = (
        "Покажи динамику по России, выбери отрасли регионы с лучшей/худшей "
        "динамикой, покажи где лучше всего, где худше всего "
        "(где — это по регионам)"
    )
    with SessionLocal() as session:
        periods = available_periods(session)
        plan = validate_plan(
            session,
            build_query_plan(question, periods),
            periods,
        )
        result = execute_plan(session, plan)

    assert plan.intent == "analytical_brief"
    assert plan.metric_codes == ["receipts_budget_total"]
    assert plan.group_by == ["industry", "territory"]
    assert plan.limit == 3
    assert len(result["series"]) == 1
    assert len(result["series"][0]["points"]) == 5
    assert result["visualization"]["type"] == "line"
    sections = {
        section["kind"]: section for section in result["analysis_sections"]
    }
    assert set(sections) == {
        "industry_growth_best",
        "industry_growth_worst",
        "regional_selected_industries_best",
        "regional_selected_industries_worst",
    }
    assert len(sections["industry_growth_best"]["rows"]) == 3
    assert len(sections["industry_growth_worst"]["rows"]) == 3
    assert len(sections["regional_selected_industries_best"]["rows"]) == 6
    assert len(sections["regional_selected_industries_worst"]["rows"]) == 6
    assert all(
        "industry" in row and "region" in row
        for kind in (
            "regional_selected_industries_best",
            "regional_selected_industries_worst",
        )
        for row in sections[kind]["rows"]
    )
    assert result["numbers_verified"] is True


@pytest.mark.parametrize(
    "question",
    [
        "как изменились поступления по мсп за последние 4 года",
        "динамика поступлений по МСП за последние четыре года",
        "покажи поступления по МСП за 4 последних года",
    ],
)
def test_relative_four_year_change_uses_2022_to_2026(
    question: str,
) -> None:
    with SessionLocal() as session:
        periods = available_periods(session)
        plan = build_query_plan(question, periods)
        plan = validate_plan(session, plan, periods)
        result = execute_plan(session, plan)
    assert plan.intent == "trend"
    assert [item.isoformat() for item in plan.periods] == [
        "2022-06-30",
        "2023-06-30",
        "2024-06-30",
        "2025-06-30",
        "2026-06-30",
    ]
    assert len(result["series"]) == 1
    assert len(result["series"][0]["points"]) == 5
    for year in range(2022, 2027):
        assert f"январь–июнь {year}" in result["answer"]
    assert "Общее изменение" in result["answer"]
    assert "Минимум" in result["answer"]
    assert "максимум" in result["answer"]


def test_best_industries_is_a_two_criterion_ranking() -> None:
    question = "по каким отраслям наилучшая ситуация в стране?"
    with SessionLocal() as session:
        periods = available_periods(session)
        plan = validate_plan(
            session,
            build_query_plan(question, periods),
            periods,
        )
        result = execute_analytic_plan(session, plan)

    assert plan.intent == "rank"
    assert plan.analysis_dimension == "industry"
    assert plan.group_by == ["industry"]
    assert plan.metric_codes == [
        "receipts_budget_total",
        "receipts_insurance_total",
    ]
    assert plan.ranking_criteria == ["value", "growth_rate"]
    assert {item["kind"] for item in result["analysis_sections"]} >= {
        "ranking_value",
        "ranking_growth",
    }
    assert "лидеры по текущему абсолютному значению" in result["answer"]
    assert "лидеры по росту" in result["answer"]


def test_last_two_full_years_exclude_incomplete_2026() -> None:
    with SessionLocal() as session:
        plan = build_query_plan(
            "что по поступлениям мсп за последние 2 полных года?",
            available_periods(session),
        )
    assert plan.intent == "compare"
    assert [item.isoformat() for item in plan.periods] == [
        "2024-12-31",
        "2025-12-31",
    ]


def test_agent_value_candidate_cannot_collapse_explicit_industry_ranking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ContradictoryProvider:
        name = "contradictory_agent"
        model = "test-model"
        last_usage = {}

        async def complete(self, _messages, **_kwargs) -> str:
            return json.dumps(
                {
                    "action": "final",
                    "plan": {
                        "intent": "value",
                        "metric_codes": ["receipts_budget_total"],
                        "row_codes": [1010],
                        "periods": ["2026-06-30"],
                        "group_by": ["industry"],
                        "analysis_dimension": "industry",
                    },
                },
                ensure_ascii=False,
            )

        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(True, True, "metadata_only")

    monkeypatch.setattr(
        "backend.app.planner.configured_provider",
        lambda _settings: ContradictoryProvider(),
    )
    monkeypatch.setattr(
        "backend.app.planner._log_agent_call",
        lambda *_args, **_kwargs: None,
    )
    settings = Settings(
        llm_provider="fake",
        llm_data_policy="metadata_only",
    )
    with SessionLocal() as session:
        result = asyncio.run(
            plan_question(
                session,
                settings,
                "по каким отраслям наилучшая ситуация в стране?",
                available_periods(session),
            )
        )

    assert result.status == "ready"
    assert result.plan is not None
    assert result.plan.intent == "rank"
    assert result.plan.analysis_dimension == "industry"
    assert result.plan.group_by == ["industry"]
    assert result.plan.ranking_criteria == ["value", "growth_rate"]
    assert result.plan.metric_codes == [
        "receipts_budget_total",
        "receipts_insurance_total",
    ]


def test_model_clarification_is_rejected_for_resolved_contextual_industries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnnecessaryClarificationProvider:
        name = "clarifying_agent"
        model = "test-model"
        last_usage = {}

        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, _messages, **_kwargs) -> str:
            self.calls += 1
            if self.calls > 1:
                return json.dumps(
                    {
                        "action": "final",
                        "semantic_query": {
                            "query_plan": {
                                "intent": "regional_average_growth",
                                "metric_codes": [
                                    "receipts_budget_total",
                                    "receipts_insurance_total",
                                ],
                                "territories": ["Российская Федерация"],
                                "row_codes": [1010],
                                "periods": ["2026-06-30"],
                                "group_by": ["industry", "territory"],
                                "analysis_dimension": "industry",
                            },
                            "analyses": ["industry_to_regions"],
                            "ranking_direction": "both",
                            "ranking_criterion": "growth_rate",
                        },
                    },
                    ensure_ascii=False,
                )
            return json.dumps(
                {
                    "action": "clarify",
                    "question": "Какой показатель нужен?",
                    "options": ["Налоговые поступления", "Все поступления"],
                },
                ensure_ascii=False,
            )

        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(True, True, "metadata_only")

    provider = UnnecessaryClarificationProvider()
    monkeypatch.setattr(
        "backend.app.planner.configured_provider",
        lambda _settings: provider,
    )
    monkeypatch.setattr(
        "backend.app.planner._log_agent_call",
        lambda *_args, **_kwargs: None,
    )
    context = [
        {
            "question": "по каким отраслям наилучшая ситуация в стране?",
            "plan": {
                "intent": "rank",
                "metric_codes": [
                    "receipts_budget_total",
                    "receipts_insurance_total",
                ],
                "periods": ["2026-06-30"],
                "row_codes": [1010],
                "group_by": ["industry"],
                "analysis_dimension": "industry",
            },
            "result_summary": {
                "analysis_entities": [
                    {"name": "Обрабатывающие производства", "row_code": 1087},
                    {"name": "Строительство", "row_code": 1270},
                ]
            },
        },
        {
            "question": "Покажи значение за январь-июнь 2026",
            "plan": {
                "intent": "value",
                "metric_codes": [
                    "receipts_budget_total",
                    "receipts_insurance_total",
                ],
                "periods": ["2026-06-30"],
                "row_codes": [1010],
            },
            "result_summary": {
                "analysis_entities": [],
            },
        },
    ]
    settings = Settings(
        llm_provider="fake",
        llm_data_policy="metadata_only",
    )
    with SessionLocal() as session:
        result = asyncio.run(
            plan_question(
                session,
                settings,
                (
                    "какой средний темп роста по всем регионам "
                    "по этим отраслям"
                ),
                available_periods(session),
                context,
            )
        )

    assert result.status == "ready"
    assert result.plan is not None
    assert result.plan.row_codes == [1087, 1270]
    assert result.semantic_query is not None
    assert result.semantic_query.analyses == ["industry_to_regions"]
    assert result.model_calls == 2
    assert any(
        item.get("action") == "clarify"
        and item.get("status") == "rejected"
        for item in result.tool_journal or []
    )


def test_model_retries_an_overlong_clarification_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OverlongClarificationProvider:
        name = "clarifying_agent"
        model = "test-model"
        last_usage = {}

        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, _messages, **_kwargs) -> str:
            self.calls += 1
            question = (
                "Что именно требуется уточнить? " * 30
                if self.calls == 1
                else "Какой показатель производства требуется?"
            )
            return json.dumps(
                {
                    "action": "clarify",
                    "question": question,
                    "options": ["Показатель", "Отрасль"],
                },
                ensure_ascii=False,
            )

        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(True, True, "metadata_only")

    provider = OverlongClarificationProvider()
    monkeypatch.setattr(
        "backend.app.planner.configured_provider",
        lambda _settings: provider,
    )
    monkeypatch.setattr(
        "backend.app.planner._log_agent_call",
        lambda *_args, **_kwargs: None,
    )
    settings = Settings(llm_provider="fake", llm_data_policy="metadata_only")
    with SessionLocal() as session:
        result = asyncio.run(
            plan_question(
                session,
                settings,
                "Покажи производство вариант 11.",
                available_periods(session),
                allow_deterministic_fallback=False,
            )
        )

    assert provider.calls == 2
    assert result.status == "needs_clarification"
    assert result.clarification_question == (
        "Какой показатель производства требуется?"
    )
    assert any(
        item.get("action") == "clarify"
        and item.get("status") == "rejected"
        and "500" in str(item.get("error"))
        for item in result.tool_journal or []
    )


def test_causal_claim_request_is_clarified_before_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_provider(_settings):
        raise AssertionError("causal safety validation must precede the model")

    monkeypatch.setattr(
        "backend.app.planner.configured_provider",
        unexpected_provider,
    )
    with SessionLocal() as session:
        result = asyncio.run(
            plan_question(
                session,
                Settings(llm_provider="fake"),
                (
                    "Найди статистически значимый вывод по базе из 9 "
                    "наблюдений и объяви его причинным."
                ),
                available_periods(session),
                allow_deterministic_fallback=False,
            )
        )

    assert result.status == "needs_clarification"
    assert result.mode == "epistemic-safety-validation"
    assert "причин" in str(result.clarification_question).casefold()


def test_model_clarification_is_rejected_for_resolved_contextual_regions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnnecessaryClarificationProvider:
        name = "clarifying_agent"
        model = "test-model"
        last_usage = {}

        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, _messages, **_kwargs) -> str:
            self.calls += 1
            if self.calls > 1:
                return json.dumps(
                    {
                        "action": "final",
                        "semantic_query": {
                            "query_plan": {
                                "intent": "regional_industry_rank_growth",
                                "metric_codes": [
                                    "receipts_insurance_total"
                                ],
                                "territories": ["Российская Федерация"],
                                "row_codes": [1010],
                                "periods": ["2026-06-30"],
                                "group_by": ["industry", "territory"],
                                "analysis_dimension": "territory",
                            },
                            "analyses": ["industry_to_regions"],
                            "ranking_direction": "both",
                            "ranking_criterion": "growth_rate",
                        },
                    },
                    ensure_ascii=False,
                )
            return json.dumps(
                {
                    "action": "clarify",
                    "question": "Какие поступления имеются в виду?",
                    "options": [
                        "Налоговые поступления",
                        "Страховые взносы",
                    ],
                },
                ensure_ascii=False,
            )

        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(True, True, "metadata_only")

    provider = UnnecessaryClarificationProvider()
    monkeypatch.setattr(
        "backend.app.planner.configured_provider",
        lambda _settings: provider,
    )
    monkeypatch.setattr(
        "backend.app.planner._log_agent_call",
        lambda *_args, **_kwargs: None,
    )
    context = [
        {
            "question": "по каким отраслям наибольший рост?",
            "plan": {
                "intent": "rank_growth",
                "metric_codes": ["receipts_budget_total"],
                "periods": ["2026-06-30"],
                "row_codes": [1010],
                "group_by": ["industry"],
                "analysis_dimension": "industry",
            },
            "result_summary": {
                "analysis_entities": [
                    {"name": "Обрабатывающие производства", "row_code": 1087},
                    {"name": "Строительство", "row_code": 1270},
                ]
            },
        },
        {
            "question": "Покажи страховые взносы",
            "plan": {
                "intent": "value",
                "metric_codes": ["receipts_insurance_total"],
                "periods": ["2026-06-30"],
                "row_codes": [1010],
            },
            "result_summary": {
                "analysis_entities": [],
            },
        },
    ]
    with SessionLocal() as session:
        result = asyncio.run(
            plan_question(
                session,
                Settings(
                    llm_provider="fake",
                    llm_data_policy="metadata_only",
                ),
                (
                    "а какие регионы лидируют по росту "
                    "по этим отраслям?"
                ),
                available_periods(session),
                context,
            )
        )

    assert result.status == "ready"
    assert result.plan is not None
    assert result.plan.row_codes == [1087, 1270]
    assert result.semantic_query is not None
    assert result.semantic_query.analyses == ["industry_to_regions"]
    assert result.model_calls == 2


@pytest.mark.parametrize(
    "question",
    [
        "Покажи значение за 2026 год",
        "Покажи НДС по региону за январь-июнь 2026",
        "Покажи НДС по производству за январь-июнь 2026",
    ],
)
def test_ambiguous_fallback_requires_clarification(question: str) -> None:
    settings = Settings(llm_provider="deterministic", llm_data_policy="none")
    with SessionLocal() as session:
        result = asyncio.run(
            plan_question(
                session,
                settings,
                question,
                available_periods(session),
            )
        )
    assert result.status == "needs_clarification"
    assert result.plan is None
    assert result.clarification_question


def test_agent_uses_metadata_tool_and_repairs_invalid_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeAgentProvider:
        name = "fake_agent"
        model = "test-model"
        last_usage = {"total_tokens": 3}

        def __init__(self) -> None:
            self.calls = 0
            self.responses = [
                {
                    "action": "search_metric",
                    "arguments": {"query": "НДС"},
                },
                {
                    "action": "final",
                    "semantic_query": {
                        "query_plan": {
                            "intent": "value",
                            "metric_codes": ["receipts_vat_total"],
                            "row_codes": [999999],
                            "periods": ["2026-06-30"],
                        },
                        "analyses": ["comparison"],
                        "constraint_sources": {
                            "metric_codes": "question",
                            "territories": "default",
                            "row_codes": "default",
                            "enterprise_groups": "default",
                            "periods": "question",
                        },
                    },
                },
                {
                    "action": "final",
                    "semantic_query": {
                        "query_plan": {
                            "intent": "value",
                            "metric_codes": ["receipts_vat_total"],
                            "row_codes": [1010],
                            "periods": ["2026-06-30"],
                        },
                        "analyses": ["comparison"],
                        "constraint_sources": {
                            "metric_codes": "question",
                            "territories": "default",
                            "row_codes": "default",
                            "enterprise_groups": "default",
                            "periods": "question",
                        },
                    },
                },
            ]

        async def complete(self, _messages, **_kwargs) -> str:
            response = self.responses[self.calls]
            self.calls += 1
            return json.dumps(response, ensure_ascii=False)

        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(True, True, "metadata_only")

    provider = FakeAgentProvider()
    monkeypatch.setattr(
        "backend.app.planner.configured_provider",
        lambda _settings: provider,
    )
    monkeypatch.setattr(
        "backend.app.planner._log_agent_call",
        lambda *_args, **_kwargs: None,
    )
    settings = Settings(llm_provider="fake", llm_data_policy="metadata_only")
    with SessionLocal() as session:
        result = asyncio.run(
            plan_question(
                session,
                settings,
                "Покажи НДС за январь-июнь 2026",
                available_periods(session),
            )
        )
    assert result.status == "ready"
    assert result.plan is not None
    assert result.plan.row_codes == [1010]
    assert provider.calls == 3
    actions = [item["action"] for item in result.tool_journal or []]
    assert actions[0] == "search_metric"
    assert any(item.get("status") == "rejected" for item in result.tool_journal or [])


def test_model_first_semantics_do_not_depend_on_regex_wording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SemanticProvider:
        name = "semantic_agent"
        model = "test-model"
        last_usage = {}

        async def complete(self, _messages, **_kwargs) -> str:
            return json.dumps(
                {
                    "action": "final",
                    "semantic_query": {
                        "query_plan": {
                            "intent": "trend",
                            "metric_codes": ["receipts_vat_total"],
                            "territories": ["Российская Федерация"],
                            "row_codes": [1010],
                            "periods": [
                                "2023-06-30",
                                "2024-06-30",
                                "2025-06-30",
                                "2026-06-30",
                            ],
                        },
                        "analyses": ["series"],
                        "constraint_sources": {
                            "metric_codes": "conversation",
                            "territories": "conversation",
                            "row_codes": "default",
                            "enterprise_groups": "default",
                            "periods": "question",
                        },
                    },
                },
                ensure_ascii=False,
            )

        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(True, True, "metadata_only")

    monkeypatch.setattr(
        "backend.app.planner.configured_provider",
        lambda _settings: SemanticProvider(),
    )
    monkeypatch.setattr(
        "backend.app.planner._log_agent_call",
        lambda *_args, **_kwargs: None,
    )
    with SessionLocal() as session:
        result = asyncio.run(
            plan_question(
                session,
                Settings(
                    llm_provider="fake",
                    llm_data_policy="metadata_only",
                ),
                "Сделай по той истории нормальный разбор, как договаривались",
                available_periods(session),
                allow_deterministic_fallback=False,
            )
        )
    assert result.status == "ready"
    assert result.plan is not None
    assert result.plan.intent == "trend"
    assert result.plan.metric_codes == ["receipts_vat_total"]
    assert result.semantic_query is not None
    assert result.semantic_query.analyses == ["series"]
    assert result.model_calls == 1


def test_model_first_reconciles_inherited_dialogue_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class WrongDefaultProvider:
        name = "semantic_agent"
        model = "test-model"
        last_usage = {}

        async def complete(self, _messages, **_kwargs) -> str:
            return json.dumps(
                {
                    "action": "final",
                    "semantic_query": {
                        "query_plan": {
                            "intent": "rank_growth",
                            "metric_codes": [
                                "receipts_budget_total",
                                "receipts_insurance_total",
                            ],
                            "territories": ["Российская Федерация"],
                            "row_codes": [1010],
                            "periods": ["2026-06-30"],
                            "group_by": ["industry"],
                            "analysis_dimension": "industry",
                            "ranking_criteria": ["growth_rate"],
                            "ranking_direction": "both",
                        },
                        "analyses": ["industry_rank"],
                        "ranking_direction": "both",
                        "ranking_criterion": "growth_rate",
                    },
                },
                ensure_ascii=False,
            )

        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(True, True, "metadata_only")

    monkeypatch.setattr(
        "backend.app.planner.configured_provider",
        lambda _settings: WrongDefaultProvider(),
    )
    monkeypatch.setattr(
        "backend.app.planner._log_agent_call",
        lambda *_args, **_kwargs: None,
    )
    context = [
        {
            "question": "Покажи динамику налога в Алтайском крае",
            "plan": {
                "intent": "trend",
                "metric_codes": ["receipts_ndfl_total"],
                "enterprise_groups": ["all_msp"],
                "territories": ["Алтайский край"],
                "row_codes": [1087],
                "periods": [
                    "2023-03-31",
                    "2024-03-31",
                    "2025-03-31",
                    "2026-03-31",
                ],
            },
            "result_summary": {
                "expected_analyses": ["series"],
                "analysis_entities": [],
            },
        }
    ]
    with SessionLocal() as session:
        result = asyncio.run(
            plan_question(
                session,
                Settings(llm_provider="fake", llm_data_policy="metadata_only"),
                "По каким отраслям рост максимален и минимален?",
                available_periods(session),
                context,
                allow_deterministic_fallback=False,
            )
        )

    assert result.status == "ready"
    assert result.plan is not None
    assert result.plan.metric_codes == ["receipts_ndfl_total"]
    assert result.plan.territories == ["Алтайский край"]
    assert [item.isoformat() for item in result.plan.periods] == [
        "2026-03-31"
    ]
    assert any(
        item.get("action") == "reconcile_constraints"
        for item in result.tool_journal or []
    )


def test_model_first_reconciles_selected_industry_drill_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class WrongOutlierProvider:
        name = "semantic_agent"
        model = "test-model"
        last_usage = {}

        async def complete(self, _messages, **_kwargs) -> str:
            return json.dumps(
                {
                    "action": "final",
                    "semantic_query": {
                        "query_plan": {
                            "intent": "outliers",
                            "metric_codes": [
                                "receipts_budget_total",
                                "receipts_insurance_total",
                            ],
                            "territories": ["Российская Федерация"],
                            "row_codes": [1087, 1270],
                            "periods": ["2026-06-30"],
                            "group_by": ["territory"],
                            "analysis_dimension": "territory",
                        },
                        "analyses": ["regional_rank", "outliers"],
                    },
                },
                ensure_ascii=False,
            )

        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(True, True, "metadata_only")

    monkeypatch.setattr(
        "backend.app.planner.configured_provider",
        lambda _settings: WrongOutlierProvider(),
    )
    monkeypatch.setattr(
        "backend.app.planner._log_agent_call",
        lambda *_args, **_kwargs: None,
    )
    context = [
        {
            "question": "По каким отраслям рост максимален и минимален?",
            "plan": {
                "intent": "rank_growth",
                "metric_codes": ["receipts_ndfl_total"],
                "territories": ["Алтайский край"],
                "row_codes": [1010],
                "periods": ["2026-03-31"],
                "group_by": ["industry"],
                "analysis_dimension": "industry",
            },
            "result_summary": {
                "expected_analyses": ["industry_rank"],
                "analysis_entities": [
                    {"name": "Обрабатывающие производства", "row_code": 1087},
                    {"name": "Строительство", "row_code": 1270},
                ],
            },
        }
    ]
    with SessionLocal() as session:
        result = asyncio.run(
            plan_question(
                session,
                Settings(llm_provider="fake", llm_data_policy="metadata_only"),
                "Оставь эти отрасли и покажи для них регионы-экстремумы.",
                available_periods(session),
                context,
                allow_deterministic_fallback=False,
            )
        )

    assert result.status == "ready"
    assert result.plan is not None
    assert result.plan.metric_codes == ["receipts_ndfl_total"]
    assert result.plan.territories == ["Российская Федерация"]
    assert result.plan.row_codes == [1087, 1270]
    assert [item.isoformat() for item in result.plan.periods] == [
        "2026-03-31"
    ]
    assert result.semantic_query is not None
    assert result.semantic_query.analyses == ["industry_to_regions"]


def test_model_first_metric_switch_changes_only_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StaleMetricProvider:
        name = "semantic_agent"
        model = "test-model"
        last_usage = {}

        async def complete(self, _messages, **_kwargs) -> str:
            return json.dumps(
                {
                    "action": "final",
                    "semantic_query": {
                        "query_plan": {
                            "intent": "outliers",
                            "metric_codes": [
                                "receipts_ndfl_total",
                                "receipts_vat_total",
                            ],
                            "territories": ["Российская Федерация"],
                            "row_codes": [1087, 1270],
                            "periods": ["2026-06-30"],
                            "group_by": ["territory"],
                            "analysis_dimension": "territory",
                        },
                        "analyses": ["regional_rank", "outliers"],
                    },
                },
                ensure_ascii=False,
            )

        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(True, True, "metadata_only")

    monkeypatch.setattr(
        "backend.app.planner.configured_provider",
        lambda _settings: StaleMetricProvider(),
    )
    monkeypatch.setattr(
        "backend.app.planner._log_agent_call",
        lambda *_args, **_kwargs: None,
    )
    context = [
        {
            "question": "Покажи регионы-экстремумы для этих отраслей",
            "plan": {
                "intent": "outliers",
                "metric_codes": ["receipts_ndfl_total"],
                "territories": ["Российская Федерация"],
                "row_codes": [1087, 1270],
                "periods": ["2026-03-31"],
                "group_by": ["territory"],
                "analysis_dimension": "territory",
            },
            "result_summary": {
                "expected_analyses": ["industry_to_regions"],
                "analysis_entities": [
                    {"name": "Обрабатывающие производства", "row_code": 1087},
                    {"name": "Строительство", "row_code": 1270},
                ],
            },
        }
    ]
    with SessionLocal() as session:
        result = asyncio.run(
            plan_question(
                session,
                Settings(llm_provider="fake", llm_data_policy="metadata_only"),
                "Там же замени налог на НДС.",
                available_periods(session),
                context,
                allow_deterministic_fallback=False,
            )
        )

    assert result.status == "ready"
    assert result.plan is not None
    assert result.plan.metric_codes == ["receipts_vat_total"]
    assert result.plan.row_codes == [1087, 1270]
    assert [item.isoformat() for item in result.plan.periods] == [
        "2026-03-31"
    ]
    assert result.semantic_query is not None
    assert result.semantic_query.analyses == ["industry_to_regions"]
    assert result.model_calls == 1


def test_model_first_reconciles_latest_comparable_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class WrongYearEndProvider:
        name = "semantic_agent"
        model = "test-model"
        last_usage = {}

        async def complete(self, _messages, **_kwargs) -> str:
            return json.dumps(
                {
                    "action": "final",
                    "semantic_query": {
                        "query_plan": {
                            "intent": "trend",
                            "metric_codes": ["receipts_budget_total"],
                            "territories": ["Российская Федерация"],
                            "row_codes": [1010],
                            "periods": [
                                "2022-12-31",
                                "2023-12-31",
                                "2024-12-31",
                                "2025-12-31",
                            ],
                        },
                        "analyses": ["series"],
                    },
                },
                ensure_ascii=False,
            )

        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(True, True, "metadata_only")

    monkeypatch.setattr(
        "backend.app.planner.configured_provider",
        lambda _settings: WrongYearEndProvider(),
    )
    monkeypatch.setattr(
        "backend.app.planner._log_agent_call",
        lambda *_args, **_kwargs: None,
    )
    with SessionLocal() as session:
        result = asyncio.run(
            plan_question(
                session,
                Settings(llm_provider="fake", llm_data_policy="metadata_only"),
                (
                    "Покажи динамику Поступления в консолидированный бюджет "
                    "по России за последние 4 сопоставимых года."
                ),
                available_periods(session),
                allow_deterministic_fallback=False,
            )
        )

    assert result.status == "ready"
    assert result.plan is not None
    assert [item.isoformat() for item in result.plan.periods] == [
        "2022-06-30",
        "2023-06-30",
        "2024-06-30",
        "2025-06-30",
        "2026-06-30",
    ]
    assert result.model_calls == 1


def test_model_first_agent_stops_repeated_calls_at_emergency_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LoopingProvider:
        name = "looping_agent"
        model = "test-model"
        last_usage = {}

        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, _messages, **_kwargs) -> str:
            self.calls += 1
            return json.dumps(
                {
                    "action": "search_metric",
                    "arguments": {"query": "НДС"},
                },
                ensure_ascii=False,
            )

        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(True, True, "metadata_only")

    provider = LoopingProvider()
    monkeypatch.setattr(
        "backend.app.planner.configured_provider",
        lambda _settings: provider,
    )
    monkeypatch.setattr(
        "backend.app.planner._log_agent_call",
        lambda *_args, **_kwargs: None,
    )
    settings = Settings(
        llm_provider="fake",
        llm_data_policy="metadata_only",
        analytic_planner_emergency_max_steps=8,
    )
    with SessionLocal() as session:
        with pytest.raises(PlanningUnavailableError):
            asyncio.run(
                plan_question(
                    session,
                    settings,
                    "Покажи НДС за январь-июнь 2026",
                    available_periods(session),
                    allow_deterministic_fallback=False,
                )
            )
    assert provider.calls == 8


def test_model_first_dialogue_recovers_after_repeated_metadata_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecoveringProvider:
        name = "gnivc_qwen"
        model = "qwen3.6-35b"
        last_usage = {}

        def __init__(self) -> None:
            self.calls = 0
            self.saw_navigation_option = False

        async def complete(self, messages, **_kwargs) -> str:
            self.calls += 1
            if self.calls == 1:
                system = messages[0]["content"]
                self.saw_navigation_option = all(
                    token in system
                    for token in (
                        '"transition": "national_total_trend"',
                        '"metric_codes": ["receipts_profit_tax_federal"]',
                        '"row_codes": [1010]',
                        '"analyses": ["series"]',
                    )
                )
            if self.calls <= 5:
                return json.dumps(
                    {
                        "action": "search_row",
                        "arguments": {
                            "query": "производство пищевых продуктов"
                        },
                    },
                    ensure_ascii=False,
                )
            return json.dumps(
                {
                    "action": "final",
                    "semantic_query": {
                        "query_plan": {
                            "intent": "trend",
                            "metric_codes": [
                                "receipts_profit_tax_federal"
                            ],
                            "enterprise_groups": ["all_msp"],
                            "territories": ["Российская Федерация"],
                            "row_codes": [1010],
                            "periods": [
                                "2023-03-31",
                                "2024-03-31",
                                "2025-03-31",
                                "2026-03-31",
                            ],
                            "period_modes": [
                                "cumulative_ytd",
                                "cumulative_ytd",
                                "cumulative_ytd",
                                "cumulative_ytd",
                            ],
                            "operations": [
                                "value",
                                "difference",
                                "growth_rate",
                                "sources",
                            ],
                        },
                        "analyses": ["series"],
                        "constraint_sources": {
                            "metric_codes": "conversation",
                            "territories": "question",
                            "row_codes": "question",
                            "enterprise_groups": "conversation",
                            "periods": "conversation",
                        },
                    },
                },
                ensure_ascii=False,
            )

        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(True, True, "metadata_only")

    conversation_context = [
        {
            "question": (
                "Покажи динамику ЕСХН во Владимирской области за "
                "январь–март за последние три года"
            ),
            "plan": {
                "intent": "trend",
                "metric_codes": ["receipts_eshn"],
                "enterprise_groups": ["all_msp"],
                "territories": ["Владимирская область"],
                "row_codes": [1055],
                "periods": [
                    "2023-03-31",
                    "2024-03-31",
                    "2025-03-31",
                    "2026-03-31",
                ],
                "period_modes": [
                    "cumulative_ytd",
                    "cumulative_ytd",
                    "cumulative_ytd",
                    "cumulative_ytd",
                ],
            },
            "result_summary": {"expected_analyses": ["series"]},
        },
        {
            "question": (
                "Там же замени налог на налог на прибыль в федеральный "
                "бюджет"
            ),
            "plan": {
                "intent": "regional_industry_rank_growth",
                "metric_codes": ["receipts_profit_tax_federal"],
                "enterprise_groups": ["all_msp"],
                "territories": ["Российская Федерация"],
                "row_codes": [1026, 1087, 1090],
                "periods": ["2026-03-31"],
                "period_modes": ["cumulative_ytd"],
            },
            "result_summary": {
                "expected_analyses": ["industry_to_regions"],
                "analysis_entities": [
                    {"row_code": 1026},
                    {"row_code": 1087},
                    {"row_code": 1090},
                ],
            },
        },
    ]
    provider = RecoveringProvider()
    monkeypatch.setattr(
        "backend.app.planner.configured_provider",
        lambda _settings: provider,
    )
    monkeypatch.setattr(
        "backend.app.planner._log_agent_call",
        lambda *_args, **_kwargs: None,
    )
    with SessionLocal() as session:
        result = asyncio.run(
            plan_question(
                session,
                Settings(
                    llm_provider="fake",
                    llm_data_policy="metadata_only",
                ),
                "Вернись к Российской Федерации и уточни динамику.",
                available_periods(session),
                conversation_context,
                allow_deterministic_fallback=False,
            )
        )

    assert provider.saw_navigation_option
    assert result.status == "ready"
    assert result.model_calls == 6
    assert result.plan is not None
    assert result.plan.metric_codes == ["receipts_profit_tax_federal"]
    assert result.plan.territories == ["Российская Федерация"]
    assert result.plan.row_codes == [1010]
    assert [item.isoformat() for item in result.plan.periods] == [
        "2023-03-31",
        "2024-03-31",
        "2025-03-31",
        "2026-03-31",
    ]
    assert sum(
        item.get("status") == "repeated_no_new_information"
        for item in result.tool_journal or []
    ) == 4


def test_model_first_agent_can_finalize_after_six_tool_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class InvestigatingProvider:
        name = "gnivc_qwen"
        model = "qwen3.6-35b"
        last_usage = {}

        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, _messages, **_kwargs) -> str:
            self.calls += 1
            if self.calls <= 6:
                return json.dumps(
                    {
                        "action": "search_metric",
                        "arguments": {"query": f"НДС {self.calls}"},
                    },
                    ensure_ascii=False,
                )
            if self.calls == 7:
                return json.dumps(
                    {
                        "action": "final",
                        "semantic_query": {
                            "query_plan": {
                                "intent": "value",
                                "metric_codes": ["receipts_vat_total"],
                                "territories": ["Российская Федерация"],
                                "row_codes": [1010],
                                "periods": ["2026-06-30"],
                            },
                            "analyses": ["comparison"],
                            "constraint_sources": {
                                "metric_codes": "question",
                                "territories": "default",
                                "row_codes": "default",
                                "enterprise_groups": "default",
                                "periods": "question",
                            },
                        },
                    },
                    ensure_ascii=False,
                )
            return json.dumps(
                {"valid": True, "errors": [], "instruction": ""},
                ensure_ascii=False,
            )

        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(True, True, "metadata_only")

    provider = InvestigatingProvider()
    monkeypatch.setattr(
        "backend.app.planner.configured_provider",
        lambda _settings: provider,
    )
    monkeypatch.setattr(
        "backend.app.planner._log_agent_call",
        lambda *_args, **_kwargs: None,
    )
    with SessionLocal() as session:
        result = asyncio.run(
            plan_question(
                session,
                Settings(
                    llm_provider="fake",
                    llm_data_policy="metadata_only",
                ),
                "Покажи НДС за январь–июнь 2026",
                available_periods(session),
                allow_deterministic_fallback=False,
            )
        )
    assert result.status == "ready"
    assert result.model_calls == 7
    assert provider.calls == 7


def test_semantic_validator_rejects_and_qwen_repairs_wrong_period_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def semantic_payload(intent: str, years: list[int]) -> dict:
        return {
            "action": "final",
            "semantic_query": {
                "query_plan": {
                    "intent": intent,
                    "metric_codes": ["receipts_budget_total"],
                    "territories": ["Российская Федерация"],
                    "row_codes": [1010],
                    "periods": [f"{year}-06-30" for year in years],
                },
                "analyses": ["series" if intent == "trend" else "comparison"],
                "constraint_sources": {
                    "metric_codes": "question",
                    "territories": "default",
                    "row_codes": "default",
                    "enterprise_groups": "default",
                    "periods": "question",
                },
            },
        }

    class ReviewingProvider:
        name = "gnivc_qwen"
        model = "qwen3.6-35b"
        last_usage = {}

        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, _messages, **_kwargs) -> str:
            self.calls += 1
            responses = [
                semantic_payload("compare", [2022, 2023, 2024, 2025]),
                semantic_payload("trend", [2023, 2024, 2025, 2026]),
            ]
            return json.dumps(responses[self.calls - 1], ensure_ascii=False)

        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(True, True, "metadata_only")

    provider = ReviewingProvider()
    monkeypatch.setattr(
        "backend.app.planner.configured_provider",
        lambda _settings: provider,
    )
    monkeypatch.setattr(
        "backend.app.planner._log_agent_call",
        lambda *_args, **_kwargs: None,
    )
    with SessionLocal() as session:
        result = asyncio.run(
            plan_question(
                session,
                Settings(
                    llm_provider="fake",
                    llm_data_policy="metadata_only",
                ),
                "Сравни поступления за прошедшие 4 года",
                available_periods(session),
                allow_deterministic_fallback=False,
            )
        )
    assert result.status == "ready"
    assert result.plan is not None
    assert result.plan.intent == "trend"
    assert [item.year for item in result.plan.periods] == [
        2023,
        2024,
        2025,
        2026,
    ]
    assert result.model_calls == 2
    assert any(
        item.get("action") == "validate_semantic_query"
        and item.get("status") == "rejected"
        for item in result.tool_journal or []
    )


def test_model_clarification_is_not_overridden_by_regex_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnstableClarifyingProvider:
        name = "unstable_agent"
        model = "test-model"
        last_usage = {}

        async def complete(self, _messages, **_kwargs) -> str:
            return json.dumps(
                {
                    "action": "clarify",
                    "question": "Какие поступления имеются в виду?",
                    "options": [
                        "Налоговые поступления",
                        "Страховые взносы",
                    ],
                },
                ensure_ascii=False,
            )

        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(True, True, "metadata_only")

    monkeypatch.setattr(
        "backend.app.planner.configured_provider",
        lambda _settings: UnstableClarifyingProvider(),
    )
    monkeypatch.setattr(
        "backend.app.planner._log_agent_call",
        lambda *_args, **_kwargs: None,
    )
    settings = Settings(
        llm_provider="fake", llm_data_policy="metadata_only"
    )
    with SessionLocal() as session:
        periods = available_periods(session)
        result = asyncio.run(
            plan_question(
                session,
                settings,
                (
                    "Сравни поступления от МСП за "
                    "январь-июнь 2025 и 2026"
                ),
                periods,
                allow_deterministic_fallback=False,
            )
        )
        assert result.status == "needs_clarification"
        assert result.plan is None
        assert result.model_calls == 1


def test_model_revises_clarification_after_exact_metadata_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RevisingProvider:
        name = "gnivc_qwen"
        model = "qwen3.6-35b"
        last_usage = {}

        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, _messages, **_kwargs) -> str:
            self.calls += 1
            responses = [
                {
                    "action": "search_metric",
                    "arguments": {"query": "поступления в бюджет"},
                },
                {
                    "action": "search_territory",
                    "arguments": {"query": "Россия"},
                },
                {
                    "action": "search_row",
                    "arguments": {"query": "все МСП"},
                },
                {
                    "action": "clarify",
                    "question": "Какую отрасль выбрать?",
                    "options": ["Указать отрасль"],
                },
                {
                    "action": "final",
                    "semantic_query": {
                        "query_plan": {
                            "intent": "value",
                            "metric_codes": ["receipts_budget_total"],
                            "enterprise_groups": ["all_msp"],
                            "territories": ["Российская Федерация"],
                            "row_codes": [1010],
                            "periods": ["2026-06-30"],
                            "period_modes": ["cumulative_ytd"],
                            "operations": ["value"],
                        },
                        "analyses": ["comparison"],
                        "constraint_sources": {
                            "metric_codes": "question",
                            "territories": "question",
                            "row_codes": "default",
                            "enterprise_groups": "question",
                            "periods": "question",
                        },
                    },
                },
            ]
            return json.dumps(responses[self.calls - 1], ensure_ascii=False)

        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(True, True, "metadata_only")

    provider = RevisingProvider()
    monkeypatch.setattr(
        "backend.app.planner.configured_provider",
        lambda _settings: provider,
    )
    monkeypatch.setattr(
        "backend.app.planner._log_agent_call",
        lambda *_args, **_kwargs: None,
    )
    with SessionLocal() as session:
        result = asyncio.run(
            plan_question(
                session,
                Settings(
                    llm_provider="fake",
                    llm_data_policy="metadata_only",
                ),
                (
                    "Каковы поступления в бюджет по всем МСП по России "
                    "за январь–июнь 2026 года?"
                ),
                available_periods(session),
                allow_deterministic_fallback=False,
            )
        )

    assert result.status == "ready"
    assert result.plan is not None
    assert result.plan.metric_codes == ["receipts_budget_total"]
    assert result.model_calls == 5
    assert any(
        item.get("action") == "clarify"
        and item.get("status") == "rejected"
        for item in result.tool_journal or []
    )


def test_report_document_matches_docx_and_numeric_edits_are_blocked(
    tmp_path: Path,
) -> None:
    docx_path, _, snapshot = generate_report(
        AUTOMSP / "template.docx",
        tmp_path,
        AUTOMSP / "53778127_1-МСП.xlsx",
        AUTOMSP / "75638291_1-МСП.xlsx",
        find_receipts_workbook(AUTOMSP),
        AUTOMSP / "Коды.xlsx",
    )
    model = snapshot["report_document"]
    assert model["paragraph_count"] == 20
    assert model["table_count"] == 4
    document = Document(docx_path)
    model_paragraphs = [
        block["text"]
        for block in model["blocks"]
        if block["type"] == "paragraph"
    ]
    model_tables = [
        block["rows"]
        for block in model["blocks"]
        if block["type"] == "table"
    ]
    assert model_paragraphs == [
        paragraph.text for paragraph in document.paragraphs
    ]
    assert model_tables == [
        [[cell.text for cell in row.cells] for row in table.rows]
        for table in document.tables
    ]

    prose = next(
        block
        for block in model["blocks"]
        if block["type"] == "paragraph"
        and block["text"] == "А. Поступления от МСП"
    )
    updated = update_report_document(
        docx_path,
        model,
        {prose["id"]: prose["text"] + " — проверено аналитиком"},
    )
    assert updated["version"] == 2
    updated_document = Document(docx_path)
    assert "проверено аналитиком" in updated_document.paragraphs[0].text
    appendices = next(
        paragraph
        for paragraph in updated_document.paragraphs
        if paragraph.text.strip() == "Приложения"
    )
    assert appendices.paragraph_format.page_break_before is True
    assert len(updated_document.tables[2].columns) == 5

    numeric = next(
        block
        for block in updated["blocks"]
        if block["type"] == "paragraph"
        and any(character.isdigit() for character in block["text"])
    )
    with pytest.raises(ValueError, match="Числовые значения"):
        update_report_document(
            docx_path,
            updated,
            {numeric["id"]: numeric["text"] + " 999"},
        )
