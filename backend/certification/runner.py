from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from backend.app.database import SessionLocal, init_database
from backend.app.config import get_settings

from .artifacts import (
    append_jsonl,
    materialize_artifacts,
    read_jsonl,
    write_json,
)
from .case_models import AnalyticEvaluationCase
from .corpus import (
    PARAPHRASES_PATH,
    build_corpus,
    materialize_questions,
    select_cases,
)
from .paraphrases import (
    generate_paraphrase_cases,
    write_generated_paraphrases,
)
from .manifest import CapabilityManifest, build_capability_manifest
from .network_check import render_network_check, run_network_check
from .oracle import IndependentOracle
from .source_audit import audit_all_imported_sources
from .verifier import (
    CaseVerdict,
    VerificationIssue,
    verify_case,
    verify_metamorphic,
)


EXPECTED_PROVIDER = "gnivc_qwen"
EXPECTED_MODEL = "qwen3.6-35b"
DEFAULT_CONCURRENCY = 4
MAX_CONCURRENCY = 16
POLL_INTERVAL_SECONDS = 0.25
RUN_TERMINAL_STATUSES = {
    "ready",
    "failed",
    "cancelled",
    "timed_out",
    "needs_clarification",
    "no_data",
}
ANSI_GREEN = "\033[1;32m"
ANSI_RED = "\033[1;31m"
ANSI_RESET = "\033[0m"


def _terminal_verdict(passed: bool, *, color: bool | None = None) -> str:
    status = "PASS" if passed else "FAIL"
    use_color = sys.stdout.isatty() if color is None else color
    if not use_color:
        return status
    ansi = ANSI_GREEN if passed else ANSI_RED
    return f"{ansi}{status}{ANSI_RESET}"


@dataclass(frozen=True)
class RunnerConfig:
    api_url: str
    run_id: str
    run_dir: Path
    concurrency: int = DEFAULT_CONCURRENCY

    def __post_init__(self) -> None:
        if not 1 <= self.concurrency <= MAX_CONCURRENCY:
            raise ValueError(
                "Параллельность сертификации должна быть от 1 до "
                f"{MAX_CONCURRENCY}"
            )


class CertificationApiClient:
    def __init__(
        self,
        base_url: str,
        *,
        concurrency: int = DEFAULT_CONCURRENCY,
    ):
        self.base_url = base_url.rstrip("/")
        common = {
            "base_url": self.base_url,
            # No scenario deadline: a run remains observable until it reaches
            # a terminal state or the user cancels it. Connection setup still
            # has a transport bound and provider calls keep their own timeout.
            "timeout": httpx.Timeout(None, connect=30.0),
            # Certification always calls a loopback API. Corporate proxy
            # variables must not route localhost traffic to the internet.
            "trust_env": False,
            "headers": {
                "User-Agent": "ai-vector-analytic-certification/1"
            },
        }
        self.client = httpx.AsyncClient(
            limits=httpx.Limits(
                max_connections=max(8, concurrency),
                max_keepalive_connections=max(8, concurrency),
                # Uvicorn's default keep-alive is five seconds. Expire idle
                # polling connections well before that boundary, otherwise a
                # new scenario can race a server-side FIN on a five-second-old
                # socket and lose the response.
                keepalive_expiry=2.0,
            ),
            **common,
        )
        # Lifecycle mutations are exactly-once from the runner's perspective.
        # Never reuse an idle socket for create/start/delete: if a POST response
        # is lost, safely reconstructing its run_id is impossible and retrying
        # could invoke the model twice. Fresh loopback connections avoid that
        # ambiguity without adding scenario or provider retries.
        self.command_client = httpx.AsyncClient(
            limits=httpx.Limits(
                max_connections=max(4, concurrency),
                max_keepalive_connections=0,
            ),
            **common,
        )

    async def __aenter__(self) -> "CertificationApiClient":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        await asyncio.gather(
            self.client.aclose(),
            self.command_client.aclose(),
        )

    async def preflight(self) -> dict[str, Any]:
        health = await self.client.get("/api/health")
        health.raise_for_status()
        if health.json().get("status") != "ok":
            raise RuntimeError("Backend healthcheck не прошёл")
        status_response = await self.client.get("/api/ai/status")
        status_response.raise_for_status()
        status = status_response.json()
        if (
            status.get("provider") != EXPECTED_PROVIDER
            or status.get("model") != EXPECTED_MODEL
            or status.get("fallback") is not False
        ):
            raise RuntimeError(
                "Сертификация требует gnivc_qwen/qwen3.6-35b без fallback: "
                + json.dumps(status, ensure_ascii=False)
            )
        # This is a real provider call, not merely a configuration check.
        probe = await self.client.post(
            "/api/ai/test",
            json={"text": 'Верни строго JSON: {"certification": true}.'},
        )
        probe.raise_for_status()
        provider_health = probe.json()
        if (
            provider_health.get("status") != "available"
            or provider_health.get("provider") != EXPECTED_PROVIDER
            or provider_health.get("model") != EXPECTED_MODEL
            or provider_health.get("fallback") is not False
        ):
            raise RuntimeError(
                "Реальный healthcheck qwen3.6-35b не прошёл: "
                + json.dumps(provider_health, ensure_ascii=False)
            )
        return {
            "backend": health.json(),
            "provider": provider_health,
        }

    async def create_conversation(self, title: str) -> str:
        response = await self.command_client.post(
            "/api/conversations", json={"title": title[:255]}
        )
        response.raise_for_status()
        conversation_id = response.json().get("id")
        if not conversation_id:
            raise RuntimeError("API не вернул id временного диалога")
        return str(conversation_id)

    async def start_analysis(self, conversation_id: str, question: str) -> str:
        response = await self.command_client.post(
            f"/api/conversations/{conversation_id}/analysis-runs",
            json={"question": question, "certification_mode": True},
        )
        response.raise_for_status()
        run_id = response.json().get("run_id")
        if not run_id:
            raise RuntimeError("API не вернул run_id")
        return str(run_id)

    async def wait_analysis(self, run_id: str) -> dict[str, Any]:
        consecutive_transport_errors = 0
        consecutive_not_found = 0
        while True:
            try:
                response = await self.client.get(
                    f"/api/analysis-runs/{run_id}"
                )
            except httpx.TransportError:
                # Polling the same run resource is idempotent and never starts
                # another scenario or model call. Recover a transient closed
                # loopback socket, but surface a persistent transport fault.
                consecutive_transport_errors += 1
                if consecutive_transport_errors > 3:
                    raise
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue
            consecutive_transport_errors = 0
            if response.status_code == 404:
                # The POST already returned this run_id, so polling it cannot
                # create a second scenario or model call. A freshly committed
                # SQLite row can briefly be invisible to another pooled
                # connection under concurrent load; tolerate only that short
                # visibility window and then surface a real missing run.
                consecutive_not_found += 1
                if consecutive_not_found > 8:
                    response.raise_for_status()
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue
            consecutive_not_found = 0
            response.raise_for_status()
            payload = response.json()
            if payload.get("status") in RUN_TERMINAL_STATUSES:
                return payload
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def delete_conversation(self, conversation_id: str) -> None:
        path = f"/api/conversations/{conversation_id}/permanent"
        last_error: Exception | None = None
        for attempt in range(20):
            try:
                response = await self.command_client.delete(path)
                if response.status_code == 404:
                    return
                if response.status_code not in {409, 500, 502, 503, 504}:
                    response.raise_for_status()
                    return
                last_error = httpx.HTTPStatusError(
                    f"Transient cleanup status {response.status_code}",
                    request=response.request,
                    response=response,
                )
            except httpx.TransportError as exc:
                last_error = exc
            if attempt < 19:
                # This is cleanup polling, never a scenario/model retry.  A
                # cancelled analysis needs a short moment to leave processing
                # before its test-owned conversation can be removed.
                await asyncio.sleep(0.5)
        if last_error is not None:
            raise last_error
        raise RuntimeError("Не удалось удалить временный диалог")


def _aggregate_turn_verdicts(
    case_id: str, verdicts: list[CaseVerdict]
) -> CaseVerdict:
    issues = []
    checks: dict[str, bool] = {}
    differences = []
    for index, verdict in enumerate(verdicts, start=1):
        issues.extend(
            VerificationIssue(
                issue.code,
                issue.message,
                issue.expected,
                issue.actual,
                f"turns[{index - 1}].{issue.location or ''}".rstrip("."),
            )
            for issue in verdict.issues
        )
        checks.update(
            {f"turn-{index}:{key}": value for key, value in verdict.checks.items()}
        )
        differences.extend(verdict.numeric_differences)
    return CaseVerdict(
        case_id=case_id,
        passed=all(item.passed for item in verdicts),
        issues=issues,
        checks=checks,
        numeric_differences=differences,
    )


def _is_infrastructure_interruption(record: dict[str, Any]) -> bool:
    """Return true only when a case has no certifiable provider result."""

    response = record.get("response") or {}
    if response.get("status") == "runner_error":
        return True
    error = response.get("error") or {}
    if (
        error.get("code") == "analysis_failed"
        and "database is locked" in str(error.get("message", "")).casefold()
    ):
        return True
    if error.get("code") != "provider_unavailable":
        return False
    diagnostic = str(error.get("message", "")).casefold()
    return "connection_error" in diagnostic or "circuit_open" in diagnostic


def _is_provider_connection_interruption(record: dict[str, Any]) -> bool:
    """Classify only Qwen transport/circuit failures for auto-pause."""

    response = record.get("response") or {}
    error = response.get("error") or {}
    if error.get("code") != "provider_unavailable":
        return False
    diagnostic = str(error.get("message", "")).casefold()
    return "connection_error" in diagnostic or "circuit_open" in diagnostic


async def _run_case(
    api: CertificationApiClient,
    config: RunnerConfig,
    case: AnalyticEvaluationCase,
) -> dict[str, Any]:
    started = time.perf_counter()
    conversation_id: str | None = None
    turn_records: list[dict[str, Any]] = []
    verdicts: list[CaseVerdict] = []
    last_response: dict[str, Any] = {}
    cleanup_error: str | None = None
    runner_stage = "create_conversation"
    try:
        conversation_id = await api.create_conversation(
            f"[certification {config.run_id}] {case.case_id}"
        )
        runner_stage = "build_oracle"
        with SessionLocal() as session:
            oracle_engine = IndependentOracle(
                session, build_capability_manifest(session)
            )
            for index, turn in enumerate(case.turns):
                oracle = oracle_engine.evaluate(case, index)
                runner_stage = f"turn_{index + 1}:start_analysis"
                live_run_id = await api.start_analysis(
                    conversation_id, turn.question
                )
                runner_stage = f"turn_{index + 1}:wait_analysis"
                response = await api.wait_analysis(live_run_id)
                runner_stage = f"turn_{index + 1}:verify"
                verdict = verify_case(
                    case,
                    response,
                    oracle,
                    turn_index=index,
                )
                verdicts.append(verdict)
                last_response = response
                turn_records.append(
                    {
                        "turn": index + 1,
                        "question": turn.question,
                        "run_id": live_run_id,
                        "oracle": oracle.to_dict(),
                        "response": response,
                        "verdict": verdict.to_dict(),
                    }
                )
                if response.get("status") != "ready" and index + 1 < len(case.turns):
                    break
        aggregate = _aggregate_turn_verdicts(case.case_id, verdicts)
    except Exception as exc:
        aggregate = CaseVerdict(
            case_id=case.case_id,
            passed=False,
            issues=[
                VerificationIssue(
                    "runner_error",
                    "Сценарий не был выполнен полностью",
                    actual=(
                        f"{runner_stage}: {type(exc).__name__}: {exc}"
                    ),
                    location="runner",
                )
            ],
            checks={"runner": False},
        )
        error_type = type(exc).__name__
        last_response = {
            "status": "runner_error",
            "error": {
                "code": "runner_transport",
                "stage": runner_stage,
                "type": error_type,
                "message": str(exc) or error_type,
            },
        }
    finally:
        if conversation_id is not None:
            try:
                await api.delete_conversation(conversation_id)
            except Exception as exc:
                cleanup_error = f"{type(exc).__name__}: {exc}"
    if cleanup_error:
        aggregate.issues.append(
            VerificationIssue(
                "cleanup_failed",
                "Не удалось удалить созданный тестом временный диалог",
                actual=cleanup_error,
                location="cleanup",
            )
        )
        aggregate.passed = False
    final_oracle = turn_records[-1]["oracle"] if turn_records else {
        "status": case.expected_status.value,
        "filters": {},
        "rows": [],
    }
    return {
        "case": case.model_dump(mode="json"),
        "attempt": 1,
        "conversation_id": conversation_id,
        "turn_results": turn_records,
        "oracle": final_oracle,
        "response": last_response,
        "verdict": aggregate.to_dict(),
        "duration_ms": int((time.perf_counter() - started) * 1000),
    }


async def run_live_cases(
    cases: list[AnalyticEvaluationCase],
    config: RunnerConfig,
    *,
    existing: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    latest_by_id = {
        str((record.get("case") or {}).get("case_id")): record
        for record in (existing or [])
    }
    interrupted_by_id = {
        case_id: record
        for case_id, record in latest_by_id.items()
        if _is_infrastructure_interruption(record)
    }
    completed_by_id = {
        case_id: record
        for case_id, record in latest_by_id.items()
        if case_id not in interrupted_by_id
    }
    pending = [item for item in cases if item.case_id not in completed_by_id]
    semaphore = asyncio.Semaphore(config.concurrency)
    infrastructure_pause_threshold = config.concurrency
    pause_event = asyncio.Event()
    infrastructure_streak = 0
    checkpoint_path = config.run_dir / "checkpoint.jsonl"
    async with CertificationApiClient(
        config.api_url,
        concurrency=config.concurrency,
    ) as api:
        health = await api.preflight()

        positions = {
            item.case_id: index
            for index, item in enumerate(cases, start=1)
        }

        async def bounded(
            case: AnalyticEvaluationCase,
        ) -> dict[str, Any] | None:
            nonlocal infrastructure_streak
            async with semaphore:
                if pause_event.is_set():
                    return None
                record = await _run_case(api, config, case)
                prior = interrupted_by_id.get(case.case_id) or {}
                record["infrastructure_resume_count"] = (
                    int(prior.get("infrastructure_resume_count") or 0) + 1
                    if prior
                    else 0
                )
                append_jsonl(checkpoint_path, record)
                verdict = _terminal_verdict(record["verdict"]["passed"])
                print(
                    f"[{positions[case.case_id]}/{len(cases)}] "
                    f"[{verdict}] {case.case_id}",
                    flush=True,
                )
                if _is_provider_connection_interruption(record):
                    infrastructure_streak += 1
                    if infrastructure_streak >= infrastructure_pause_threshold:
                        pause_event.set()
                else:
                    infrastructure_streak = 0
                return record

        produced_with_gaps = await asyncio.gather(
            *(bounded(item) for item in pending)
        )
        produced = [item for item in produced_with_gaps if item is not None]
    produced_by_id = {
        str((record.get("case") or {}).get("case_id")): record
        for record in produced
    }
    combined = [
        produced_by_id.get(item.case_id) or completed_by_id.get(item.case_id)
        for item in cases
        if item.case_id in produced_by_id or item.case_id in completed_by_id
    ]
    if pause_event.is_set():
        remaining = len(cases) - len(combined)
        print(
            f"Qwen API returned {infrastructure_pause_threshold} consecutive "
            "connection/circuit "
            "failures; certification paused "
            f"with {remaining} case(s) remaining. Restore the connection "
            f"and run: ./analytic-certification --resume {config.run_id}",
            file=sys.stderr,
            flush=True,
        )
    return combined, health


def _parse_shard(value: str | None) -> tuple[int, int] | None:
    if not value:
        return None
    match = value.split("/", 1)
    if len(match) != 2:
        raise ValueError("Shard задаётся как N/M")
    return int(match[0]), int(match[1])


def _parse_concurrency(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "Параллельность должна быть целым числом"
        ) from exc
    if not 1 <= parsed <= MAX_CONCURRENCY:
        raise argparse.ArgumentTypeError(
            f"Параллельность должна быть от 1 до {MAX_CONCURRENCY}"
        )
    return parsed


def _new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def _frontend_oracle_fixture(
    manifest: CapabilityManifest,
) -> dict[str, Any]:
    periods = [
        item
        for item in manifest.national_periods
        if item[5:10] == manifest.national_periods[-1][5:10]
    ][-5:]
    with SessionLocal() as session:
        oracle = IndependentOracle(session, manifest)
        points = [
            {
                "period_end": period,
                "label": f"январь–июнь {period[:4]}",
                "value": str(
                    oracle.value(
                        "receipts_vat_total",
                        "Российская Федерация",
                        1010,
                        "all_msp",
                        period,
                    )
                ),
                "fact_id": f"oracle-{period}",
            }
            for period in periods
        ]
    numeric = [Decimal(item["value"]) for item in points]
    changes = []
    for previous, current in zip(points, points[1:]):
        left, right = Decimal(previous["value"]), Decimal(current["value"])
        changes.append(
            {
                "from_period": previous["period_end"],
                "to_period": current["period_end"],
                "difference": str(right - left),
                "growth_rate": str(
                    (right - left) / abs(left) * Decimal("100")
                )
                if left
                else None,
            }
        )
    first, last = Decimal(points[0]["value"]), Decimal(points[-1]["value"])
    return {
        "data_fingerprint": manifest.data_fingerprint,
        "series": {
            "key": "oracle-vat-rf",
            "label": "Налог на добавленную стоимость · Российская Федерация · все МСП",
            "metric_code": "receipts_vat_total",
            "territory": "Российская Федерация",
            "enterprise_group": "all_msp",
            "row_code": 1010,
            "unit": "тыс. руб.",
            "points": points,
            "changes": changes,
            "overall_change": {
                "from_period": points[0]["period_end"],
                "to_period": points[-1]["period_end"],
                "difference": str(last - first),
                "growth_rate": str(
                    (last - first) / abs(first) * Decimal("100")
                )
                if first
                else None,
            },
            "minimum": {
                key: value
                for key, value in points[numeric.index(min(numeric))].items()
                if key != "fact_id"
            },
            "maximum": {
                key: value
                for key, value in points[numeric.index(max(numeric))].items()
                if key != "fact_id"
            },
        },
    }


def _latest_resume_dir(root: Path) -> Path:
    candidates = sorted(
        (item for item in root.glob("*") if (item / "run.json").exists()),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise ValueError("Нет незавершённого запуска для --resume")
    return candidates[0]


def _resolve_resume_dir(root: Path, value: str) -> Path:
    if value == "latest":
        return _latest_resume_dir(root)
    direct = Path(value)
    candidates = [direct, root / value]
    for candidate in candidates:
        if (candidate / "run.json").exists():
            return candidate
    raise ValueError(f"Запуск для --resume не найден: {value}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="analytic-certification",
        description="Строгая live-сертификация аналитического агента",
    )
    parser.add_argument("--tier", choices=("smoke", "full"), default="smoke")
    parser.add_argument("--case", dest="case_id")
    parser.add_argument(
        "--resume",
        nargs="?",
        const="latest",
        metavar="RUN_ID_OR_PATH",
        help=(
            "Продолжить последний запуск либо указанный run-id/каталог; "
            "повторяются только инфраструктурно оборванные сценарии"
        ),
    )
    parser.add_argument("--shard", help="Стабильный shard N/M")
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--concurrency",
        type=_parse_concurrency,
        default=None,
        metavar="N",
        help=(
            "Число параллельных сценариев, 1–16; по умолчанию 4, "
            "при --resume сохраняется значение исходного запуска"
        ),
    )
    parser.add_argument(
        "--artifacts-root",
        type=Path,
        default=Path("artifacts/analytic-certification"),
    )
    parser.add_argument(
        "--materialize",
        action="store_true",
        help="Обновить questions.md и capability-manifest.json без live-запуска",
    )
    parser.add_argument(
        "--audit-sources",
        action="store_true",
        help="Запустить отдельный полный аудит фактов и исходных колонок",
    )
    parser.add_argument(
        "--network-check",
        action="store_true",
        help=(
            "Проверить прямую связь с qwen3.6-35b: healthcheck и выбранное "
            "число одновременных JSON-запросов без fallback и повторов"
        ),
    )
    parser.add_argument(
        "--generate-paraphrases",
        action="store_true",
        help="Сгенерировать и версионировать model-based перефразировки",
    )
    parser.add_argument(
        "--paraphrase-variants",
        type=int,
        default=5,
        help="Число вариантов на исходный сценарий (минимум 2)",
    )
    parser.add_argument(
        "--paraphrase-limit",
        type=int,
        default=20,
        help="Максимум исходных сценариев за один запуск генератора",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    requested_concurrency = args.concurrency or DEFAULT_CONCURRENCY
    if args.network_check:
        diagnostic = asyncio.run(
            run_network_check(concurrency=requested_concurrency)
        )
        print(render_network_check(diagnostic), end="")
        return 0 if diagnostic["passed"] else 1

    init_database()
    with SessionLocal() as session:
        manifest = build_capability_manifest(session)
    corpus = build_corpus(manifest)
    if args.generate_paraphrases:
        eligible_categories = {
            "trend",
            "period_regression",
            "dialogue",
            "industry_ranking",
            "distribution",
            "regional_outliers",
            "drill_down",
        }
        bases = [
            item
            for item in corpus
            if item.origin != "generated"
            and item.category in eligible_categories
            and (args.case_id is None or item.case_id == args.case_id)
        ][: max(1, args.paraphrase_limit)]
        if not bases:
            raise ValueError("Нет подходящих исходных сценариев для генерации")
        generated = asyncio.run(
            generate_paraphrase_cases(
                bases,
                get_settings(),
                variants_per_case=args.paraphrase_variants,
            )
        )
        write_generated_paraphrases(PARAPHRASES_PATH, generated)
        print(PARAPHRASES_PATH)
        return 0
    if args.materialize:
        path = materialize_questions(corpus, manifest)
        write_json(path.with_name("capability-manifest.json"), manifest.to_dict())
        write_json(
            Path("src/test/generated-certification-oracle.json"),
            _frontend_oracle_fixture(manifest),
        )
        print(path)
        return 0
    if args.audit_sources:
        run_id = _new_run_id()
        run_dir = args.artifacts_root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        with SessionLocal() as session:
            audit = audit_all_imported_sources(session)
        write_json(run_dir / "source-audit.json", audit)
        print(run_dir / "source-audit.json")
        return 0 if audit["passed"] else 1

    shard = _parse_shard(args.shard)
    selected = select_cases(
        corpus,
        tier=args.tier,
        case_id=args.case_id,
        shard=shard,
    )
    effective_tier = args.tier
    if args.resume:
        run_dir = _resolve_resume_dir(args.artifacts_root, args.resume)
        run_meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        effective_tier = str(run_meta.get("tier") or args.tier)
        if run_meta.get("data_fingerprint") != manifest.data_fingerprint:
            raise ValueError(
                "Нельзя продолжить запуск после изменения проверяемых данных"
            )
        selected_ids = set(run_meta.get("case_ids") or [])
        selected = [item for item in corpus if item.case_id in selected_ids]
        run_id = str(run_meta["run_id"])
        effective_concurrency = args.concurrency or int(
            run_meta.get("max_concurrency") or DEFAULT_CONCURRENCY
        )
        if run_meta.get("max_concurrency") != effective_concurrency:
            run_meta["max_concurrency"] = effective_concurrency
            write_json(run_dir / "run.json", run_meta)
        existing = [
            *read_jsonl(run_dir / "results.jsonl"),
            *read_jsonl(run_dir / "checkpoint.jsonl"),
        ]
    else:
        effective_concurrency = requested_concurrency
        run_id = _new_run_id()
        run_dir = args.artifacts_root / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        existing = []
        write_json(
            run_dir / "run.json",
            {
                "run_id": run_id,
                "tier": args.tier,
                "case_ids": [item.case_id for item in selected],
                "shard": args.shard,
                "api_url": args.api_url,
                "data_fingerprint": manifest.data_fingerprint,
                "attempts_per_case": 1,
                "max_concurrency": effective_concurrency,
                "scenario_timeout_seconds": None,
            },
        )
    config = RunnerConfig(
        api_url=args.api_url,
        run_id=run_id,
        run_dir=run_dir,
        concurrency=effective_concurrency,
    )
    try:
        records, health = asyncio.run(
            run_live_cases(selected, config, existing=existing)
        )
    except Exception as exc:
        write_json(
            run_dir / "preflight-failure.json",
            {"error": f"{type(exc).__name__}: {exc}"},
        )
        print(f"Preflight failed: {exc}", file=sys.stderr)
        return 2
    write_json(run_dir / "health.json", health)
    source_audit = None
    if effective_tier == "full" and not args.case_id and shard is None:
        with SessionLocal() as session:
            source_audit = audit_all_imported_sources(session)
    metamorphic_issues = verify_metamorphic(records)
    coverage = materialize_artifacts(
        run_dir,
        records,
        manifest,
        selected_count=len(selected),
        source_audit=source_audit,
        metamorphic_issues=metamorphic_issues,
    )
    print(run_dir)
    return 0 if coverage["release_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
